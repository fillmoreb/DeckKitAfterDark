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
interrupt() on the hook's "recording" state), so you can just ask again. Each question's deckagent runs
in a process group of its own (with claude and its MCP server under it), and dropping the question ends
the whole group: nothing of the old question is left to act on the game.

The assistant gets MODEL_S seconds per question, not counting time spent waiting for Ben to answer one
of its prompts (ask_player, up to a couple of minutes: the link keeper counts it, prompt_waited).
Dropping a question also cancels what it asked the link keeper for (cancel_requests): requests still
waiting to run are dropped, and a prompt up for it is taken down in the game.

Speak or show (through the link keeper's "show", addons/DeckAgent/Show.lua):
  - every answer is echoed into chat with its question, so it can be read again (the chat peek);
  - a long one (over SPOKEN_MAX characters; never one through respond, which caps `say` at 160) is
    spoken as the sentences that fit plus "The rest is on screen", and shown in full on a card;
  - with Quiet on (the game's DeckKit menu), nothing is spoken: the answer goes on a card. The launcher
    fetches the setting with Ben's position before each question (and a reminder asks for it), so
    the answer follows a change made just before;
  - if the assistant already put a card or reading view up for this question (respond), that's the
    full version: no second card, and a long spoken part is cut to the sentences that fit;
  - last lines starting "Heard as:" (how she read misheard words) or "Note:" (a source or caveat with
    nothing on screen to carry it) go into chat only, never spoken.
"""
import os, re, signal, subprocess, threading, time

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


HEARD = re.compile(r"^\s*(heard as|note):", re.I)  # chat-only lines (the launcher writes them)


def split_heard(text):
    """(the answer without its "Heard as:" lines, those lines joined): they're for chat, not speech."""
    keep, heard = [], []
    for line in (text or "").splitlines():
        (heard if HEARD.match(line) else keep).append(line.strip())
    return "\n".join(keep), " ".join(heard)


def sentences(text):
    return [p for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p]


def leading(answer, limit=SPOKEN_MAX):
    """As many whole sentences from the start as fit in `limit` characters (at least the first)."""
    parts, out = sentences(answer), ""
    for p in parts:
        if out and len(out) + 1 + len(p) > limit:
            break
        out = (out + " " + p).strip()
    return out


def spoken_part(answer):
    """(what to say, whether the full answer needs showing). Only length counts: an answer through
    respond is at most 160 characters and is said whole, however many sentences it has."""
    if len(answer) <= SPOKEN_MAX:
        return answer, False
    return leading(answer, SPOKEN_MAX - len(ON_SCREEN) - 1) + " " + ON_SCREEN, True


# What Whisper tends to "hear" in silence or breath noise: never a question.
PHANTOMS = {"you", "thank you", "thanks", "thanks for watching", "thank you for watching", "bye", "okay", "ok",
            "uh", "um", "hmm", "mm"}


def is_phantom(text):
    words = re.sub(r"[^a-z' ]", " ", (text or "").lower()).split()
    return not words or " ".join(words) in PHANTOMS


MODEL_S = 120  # the assistant's time per question, prompts it waits on not counted
KILL_GRACE_S = 2  # how long a dropped question's processes get to stop before they're killed


def end_group(p, grace=KILL_GRACE_S):
    """Ends a process and everything in its group (deckagent: claude, its MCP server): asked to stop,
    then killed after `grace` seconds; the process itself is reaped."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(p.pid, sig)
        except (ProcessLookupError, PermissionError):  # the group is gone already
            break
        if sig == signal.SIGTERM:
            try:
                p.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
    p.wait()


class Voice:
    def __init__(self, indicator=None, run=None, say=None, game=None):
        self.game = game  # the link keeper (agentd.Keeper): handle(req), quiet, shown_at; None: say only
        self.indicator = indicator or Indicator()
        self.run = run or self._run
        self.say = say or Speaker()
        self.lock = threading.Lock()
        self.busy = False
        self.gen = 0  # bumped by interrupt(): an answer from an older question is dropped
        self.proc = None  # (gen, Popen): the running deckagent and the question it belongs to
        self.waiting = []  # hook wake-ups that came while a question was being answered: (prompt, title)

    def _waited(self):
        """The link keeper's running total of time spent waiting on Ben's prompt answers (0 without)."""
        waited = getattr(self.game, "prompt_waited", None)
        return waited() if callable(waited) else 0.0

    def model_seconds(self, since, waited_at_start=0.0):
        """Seconds the assistant has had since `since`, without Ben's time answering its prompts
        (the keeper's total now, less `waited_at_start`, its total when the question started)."""
        return time.monotonic() - since - (self._waited() - waited_at_start)

    def _run(self, cmd, timeout=MODEL_S, gen=None, **kw):
        """subprocess.run in a process group of its own, which interrupt() ends whole. `timeout` is the
        assistant's time (model_seconds), so a prompt waiting on Ben doesn't use it up."""
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        with self.lock:
            dropped = gen is not None and gen != self.gen  # interrupted while it was starting
            if not dropped:
                self.proc = (gen, p)
        if dropped:
            end_group(p)
        start, waited = time.monotonic(), self._waited()
        try:
            while True:
                try:
                    out, err = p.communicate(timeout=1.0)
                    break
                except subprocess.TimeoutExpired:
                    if self.model_seconds(start, waited) > timeout:
                        log("deckagent took over %d s: stopped" % timeout)
                        end_group(p)
        finally:
            with self.lock:
                if self.proc is not None and self.proc[1] is p:  # never a newer question's
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
            proc, self.proc = self.proc, None
        cancel = getattr(self.game, "cancel_requests", None)
        if callable(cancel):  # what it asked the game for and hasn't got (a prompt up) goes too
            cancel()
        if proc is not None:  # in the background: the dictation state shouldn't wait for it
            threading.Thread(target=end_group, args=(proc[1],), daemon=True).start()
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

    def wake(self, prompt, title):
        """A hook fired: the assistant gets its note (hooks.wake_prompt) and does the next step, and the
        answer is spoken like any other (no dictation box: nothing was asked). One question at a time:
        a wake-up during a question waits for it."""
        with self.lock:
            if self.busy:
                self.waiting.append((prompt, title))
                return
            self.busy = True
            gen = self.gen
        threading.Thread(target=self._answer, args=(prompt, gen, title), daemon=True).start()

    def remind(self, note, title):
        """A "say" hook: its note, spoken and in chat, with no model call."""
        log("reminder %r" % note)
        self.refresh_quiet()  # nothing asked the game lately: Quiet may have changed since
        self.deliver(title, note, time.monotonic(), heading=True)

    def refresh_quiet(self):
        """Asks the game for its Quiet setting (the link keeper remembers it from the reply)."""
        if self.game is None:
            return
        try:
            self.game.handle({"cmd": "state", "fields": ["voice"]})
        except Exception as e:
            log("quiet: couldn't ask the game (%s); using the last answer" % e)

    def _answer(self, text, gen=0, title=None):
        t0 = time.monotonic()
        log("asked %r" % (title or text))
        if title is None:
            self.indicator.show("thinking")
        try:
            out = self.run([DECKAGENT, text], timeout=MODEL_S, gen=gen)
            if gen != self.gen:
                log("answer to %r dropped (asked something else)" % text)
                return
            spoken, heard = split_heard(out.stdout)
            answer, heard = clean_answer(spoken), clean_answer(heard)  # the whole answer: its last line may be a source link
            if not answer or out.returncode:
                log("deckagent exit %s: %s" % (out.returncode, (out.stderr or "").strip()[-500:]))
            log("answered %r (%.1f s)" % (answer, time.monotonic() - t0))
            if title is None:
                self.indicator.show("done")
            if not answer:
                self.say("Sorry, I didn't get an answer.")
                return
            self.deliver(title or text, answer, t0, heard, heading=title is not None)
        except Exception as e:
            if gen != self.gen:
                return
            log("failed:", e)
            self.indicator.show("error")
            self.say("Sorry, that didn't work.")
        finally:
            nxt = None
            with self.lock:
                if gen == self.gen:
                    self.busy = False
                    if self.waiting:
                        nxt = self.waiting.pop(0)
                        self.busy = True
            if nxt:
                threading.Thread(target=self._answer, args=(nxt[0], self.gen, nxt[1]), daemon=True).start()

    def deliver(self, question, answer, asked_at, heard="", heading=False):
        """Speak or show (module docstring). The showing runs beside the speech: it may have to open
        the link first (~1 s), and the answer shouldn't wait for that."""
        game = self.game
        quiet = bool(game and getattr(game, "quiet", False))
        shown_already = bool(game and getattr(game, "shown_at", 0) >= asked_at)
        say, long = spoken_part(answer)
        card = (long or quiet) and not shown_already

        def show():
            echo = answer + ((" (" + heard + ")") if heard else "")
            line = question if heading else "You asked: " + question
            for req in [{"cmd": "show", "kind": "echo", "title": line, "text": echo}] + (
                    [{"cmd": "show", "kind": "card", "title": "Decky", "text": answer[:600]}] if card else []):
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
        if shown_already and long:  # she showed the full version: say what fits of hers
            say = leading(answer)
        self.say(say)
