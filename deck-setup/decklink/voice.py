"""DeckAgent's voice, in the link keeper service (agentd.py): answers what you asked by voice.

The asking is Decktation's (its "agent binding", L5+R5 in Ben's setup: a DeckKit fork of Decktation
that hands that binding's transcript to a command instead of typing it). The command is
`decklink-agent-ask` (install-on-deck.sh), which sends the text here: {"cmd": "ask", "text": ...}.
Decktation's status hook shows Listening and Transcribing as usual (decktation-hook.sh leaves the box
up after an agent transcript); this then:
  1. runs `deckagent "<text>"`,
  2. closes DeckKit's dictation indicator (DeckLink channel 2, as the hook does),
  3. speaks the answer with Piper (pw-play), or shows it ("speak or show", below).
One question at a time. Starting a new recording cuts the spoken answer off (any binding: the mic
would hear it); starting a new agent question also drops one still being worked on (agentd calls
interrupt() on the hook's "recording" state), so you can just ask again.

Speak or show (through the link keeper's "show", addons/DeckAgent/Show.lua):
  - every answer is echoed into chat with its question, so it can be read again (the chat peek);
  - a long one (over SPOKEN_MAX characters or two sentences) is spoken as its first sentence plus
    "The rest is on screen", and shown in full on a card;
  - with Quiet on (the game's DeckKit menu), nothing is spoken: the answer goes on a card;
  - if the assistant already put a card or reading view up for this question (show_card,
    show_reading), that's the full version: no second card, and a long spoken part is cut to its
    first sentence plus "It's on screen";
  - a last line starting "Heard as:" (how she read misheard words) goes into chat only, never spoken.
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


class Speaker:
    """Speaks with Piper through pw-play; stop() cuts it off."""

    def __init__(self):
        self.lock = threading.Lock()
        self.procs = []

    def __call__(self, text):
        if not text:
            return
        with self.lock:
            piper = subprocess.Popen([os.path.join(PIPER_DIR, "piper", "piper"), "--model",
                                      os.path.join(PIPER_DIR, "en_US-lessac-medium.onnx"), "--output-raw"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            play = subprocess.Popen(["pw-play", "--rate", "22050", "--channels", "1", "--format", "s16", "-"],
                                    stdin=piper.stdout, stderr=subprocess.DEVNULL)
            piper.stdout.close()
            self.procs = [piper, play]
        try:
            piper.stdin.write(text.encode())
            piper.stdin.close()
        except BrokenPipeError:  # stopped already
            pass
        try:
            play.wait(timeout=120)
        finally:
            with self.lock:
                if self.procs == [piper, play]:
                    self.procs = []

    def stop(self):
        with self.lock:
            procs, self.procs = self.procs, []
        for p in procs:
            if p.poll() is None:
                p.kill()
        return bool(procs)


SPOKEN_MAX = 200
ON_SCREEN = "The rest is on screen."


def clean_answer(text):
    """The assistant's answer as something to say and echo: no "Sources:" block (Claude adds one after
    a web search), links as their titles, no bare URLs, no markdown markers, one paragraph."""
    text = re.split(r"\n\s*\**sources\**:?\**\s*\n", "\n" + (text or "").strip(), maxsplit=1, flags=re.I)[0]
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)  # [title](url) -> title
    text = re.sub(r"\(?https?://\S+\)?", "", text)
    lines = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:#+|[-*\u2022]|\d+[.)])\s+", "", line)
        line = re.sub(r"[*_`]{1,3}([^*_`]+)[*_`]{1,3}", r"\1", line).strip()
        if line:
            lines.append(line if re.search(r"[.!?:]$", line) else line + ".")
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


HEARD = re.compile(r"^\s*heard as:", re.I)


def split_heard(text):
    """(the answer without its "Heard as:" lines, those lines joined): they're for chat, not speech."""
    keep, heard = [], []
    for line in (text or "").splitlines():
        (heard if HEARD.match(line) else keep).append(line.strip())
    return "\n".join(keep), " ".join(heard)


def sentences(text):
    return [p for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p]


def spoken_part(answer):
    """(what to say, whether the full answer needs showing)."""
    parts = sentences(answer)
    if len(answer) <= SPOKEN_MAX and len(parts) <= 2:
        return answer, False
    return parts[0] + " " + ON_SCREEN, True


def card_seconds(text):
    return int(min(60, 8 + len(text) / 15))


# What Whisper tends to "hear" in silence or breath noise: never a question.
PHANTOMS = {"you", "thank you", "thanks", "thanks for watching", "thank you for watching", "bye", "okay", "ok",
            "uh", "um", "hmm", "mm"}


def is_phantom(text):
    words = re.sub(r"[^a-z' ]", " ", (text or "").lower()).split()
    return not words or " ".join(words) in PHANTOMS


class Voice:
    def __init__(self, indicator=None, run=None, say=None, game=None):
        self.game = game  # the link keeper (agentd.Keeper): handle(req), quiet, shown_at; None: say only
        self.indicator = indicator or Indicator()
        self.run = run or self._run
        self.say = say or Speaker()
        self.lock = threading.Lock()
        self.busy = False
        self.gen = 0  # bumped by interrupt(): an answer from an older question is dropped
        self.proc = None  # the running deckagent (the default runner)

    def _run(self, cmd, timeout=120, **kw):
        """subprocess.run, but interrupt() can kill it."""
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.proc = p
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = p.communicate()
        finally:
            self.proc = None
        return subprocess.CompletedProcess(cmd, p.returncode, out, err)

    def interrupt(self, drop_question=True):
        """A new recording started: stop talking, and (agent binding) drop the question in progress."""
        stop = getattr(self.say, "stop", None)
        if stop and stop():
            log("stopped speaking")
        if not drop_question:
            return
        with self.lock:
            if not self.busy:
                return
            self.gen += 1
            self.busy = False
            proc = self.proc
        if proc and proc.poll() is None:
            proc.kill()
        log("dropped the last question")

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
            gen = self.gen
        threading.Thread(target=self._answer, args=(text, gen), daemon=True).start()
        return True

    def _answer(self, text, gen=0):
        t0 = time.monotonic()
        log("asked %r" % text)
        self.indicator.show("thinking")
        try:
            out = self.run([DECKAGENT, text], capture_output=True, text=True, timeout=120)
            if gen != self.gen:
                log("answer to %r dropped (asked something else)" % text)
                return
            spoken, heard = split_heard(out.stdout)
            answer, heard = clean_answer(spoken), clean_answer(heard)  # the whole answer: its last line may be a source link
            if not answer or out.returncode:
                log("deckagent exit %s: %s" % (out.returncode, (out.stderr or "").strip()[-500:]))
            log("answered %r (%.1f s)" % (answer, time.monotonic() - t0))
            self.indicator.show("done")
            if not answer:
                self.say("Sorry, I didn't get an answer.")
                return
            self.deliver(text, answer, t0, heard)
        except Exception as e:
            if gen != self.gen:
                return
            log("failed:", e)
            self.indicator.show("error")
            self.say("Sorry, that didn't work.")
        finally:
            with self.lock:
                if gen == self.gen:
                    self.busy = False

    def deliver(self, question, answer, asked_at, heard=""):
        """Speak or show (module docstring). The showing runs beside the speech: it may have to open
        the link first (~1 s), and the answer shouldn't wait for that."""
        game = self.game
        quiet = bool(game and getattr(game, "quiet", False))
        shown_already = bool(game and getattr(game, "shown_at", 0) >= asked_at)
        say, long = spoken_part(answer)
        card = (long or quiet) and not shown_already

        def show():
            echo = answer + ((" (" + heard + ")") if heard else "")
            for req in [{"cmd": "show", "kind": "echo", "title": question, "text": echo}] + (
                    [{"cmd": "show", "kind": "card", "title": "Decky", "text": answer[:600],
                      "seconds": card_seconds(answer)}] if card else []):
                try:
                    reply = game.handle(req)
                    if not reply.get("ok"):
                        log("show %s failed: %s" % (req["kind"], reply.get("error") or reply.get("reason")))
                except Exception as e:
                    log("show %s failed: %s" % (req["kind"], e))
        if game is not None:
            threading.Thread(target=show, daemon=True).start()
        if quiet:
            log("quiet: shown, not spoken")
            return
        if shown_already and long:
            first = sentences(answer)[0]
            say = first if re.search(r"\bscreen\b", first, re.I) else first + " It's on screen."
        self.say(say)
