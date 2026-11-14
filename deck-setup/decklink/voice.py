"""DeckAgent's voice, in the link keeper service (agentd.py): answers what you asked by voice.

The asking is Decktation's (its "agent binding", L5+R5 in Ben's setup: a DeckKit fork of Decktation
that hands that binding's transcript to a command instead of typing it). The command is
`decklink-agent-ask` (install-on-deck.sh), which sends the text here: {"cmd": "ask", "text": ...}.
Decktation's status hook shows Listening and Transcribing as usual (decktation-hook.sh leaves the box
up after an agent transcript); this then:
  1. runs `deckagent "<text>"`,
  2. closes DeckKit's dictation indicator (DeckLink channel 2, as the hook does),
  3. speaks the answer with Piper (pw-play).
One question at a time: another one while it's answering is turned away (spoken).
"""
import os, re, subprocess, threading, time

from . import dictation

PIPER_DIR = "/home/deck/.local/share/deckagent/piper"
DECKAGENT = "/home/deck/.local/bin/deckagent"


def log(*parts):
    print("voice:", *parts, flush=True)


class Indicator:
    """DeckKit's dictation box, through DeckDictation (dictation.py), as the agent binding."""

    def __init__(self, sender=None):
        self.sender = sender

    def show(self, state):
        try:
            dictation.post(state, "agent", sender=self.sender)
        except Exception as e:
            log("indicator", state, "failed:", e)


def speak(text):
    if not text:
        return
    piper = subprocess.Popen([os.path.join(PIPER_DIR, "piper", "piper"), "--model",
                              os.path.join(PIPER_DIR, "en_US-lessac-medium.onnx"), "--output-raw"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    play = subprocess.Popen(["pw-play", "--rate", "22050", "--channels", "1", "--format", "s16", "-"],
                            stdin=piper.stdout, stderr=subprocess.DEVNULL)
    piper.stdout.close()
    piper.stdin.write(text.encode())
    piper.stdin.close()
    play.wait(timeout=120)


# What Whisper tends to "hear" in silence or breath noise: never a question.
PHANTOMS = {"you", "thank you", "thanks", "thanks for watching", "thank you for watching", "bye", "okay", "ok",
            "uh", "um", "hmm", "mm"}


def is_phantom(text):
    words = re.sub(r"[^a-z' ]", " ", (text or "").lower()).split()
    return not words or " ".join(words) in PHANTOMS


class Voice:
    def __init__(self, indicator=None, run=subprocess.run, say=speak):
        self.indicator = indicator or Indicator()
        self.run, self.say = run, say
        self.lock = threading.Lock()
        self.busy = False

    def ask(self, text):
        """Starts answering `text` in the background. False if it isn't a question (empty, or one of
        Whisper's phantoms on silence) or a question is already being answered."""
        text = (text or "").strip()
        if is_phantom(text):
            log("ignored %r (no question in it)" % text)
            self.indicator.show("done")
            return False
        with self.lock:
            if self.busy:
                threading.Thread(target=self.say, args=("One moment, I'm still on the last one.",), daemon=True).start()
                return False
            self.busy = True
        threading.Thread(target=self._answer, args=(text,), daemon=True).start()
        return True

    def _answer(self, text):
        t0 = time.monotonic()
        log("asked %r" % text)
        self.indicator.show("thinking")
        try:
            out = self.run([DECKAGENT, text], capture_output=True, text=True, timeout=120)
            answer = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
            if not answer or out.returncode:
                log("deckagent exit %s: %s" % (out.returncode, (out.stderr or "").strip()[-500:]))
            log("answered %r (%.1f s)" % (answer, time.monotonic() - t0))
            self.indicator.show("done")
            self.say(answer or "Sorry, I didn't get an answer.")
        except Exception as e:
            log("failed:", e)
            self.indicator.show("error")
            self.say("Sorry, that didn't work.")
        finally:
            with self.lock:
                self.busy = False
