"""DeckAgent's link keeper: a small service on the Deck that owns DeckLink for the assistant, so each
question doesn't pay to start the screen reader and open the link again.

    python3 -m decklink.agentd [--idle 25]        (the decklink-agent user service runs it)

It listens on a Unix socket ($XDG_RUNTIME_DIR/deckagent.sock, this user only) for one JSON line per
request and answers with one JSON line:
  {"cmd": "state", "fields": ["pos", "world", ...]}   -> the game's reply (agent.prepare: every
  {"cmd": "way", "map": 1454, "x": 36, "y": 87.7}     -> agent.COMMANDS request, checked and built there)
  {"cmd": "warm"}       open the link now (a question is coming: L5+R5 went down)
  {"cmd": "status"}     whether the link is open, and how long it's been idle
  {"cmd": "dictation", "state": "recording", "binding": "agent"}   post a dictation state (the hook)
  {"cmd": "ask", "text": "..."}   answer a question by voice: run deckagent, speak the answer (voice.py)
  {"cmd": "hook", "op": "add" | "list" | "remove" | "clear", ...}   the assistant's hooks (hooks.py)
Errors come back as {"ok": false, "error": "..."}.

Hooks: while any game hook is set, the keeper glances at the strip about once a second (one paused
capture pipeline, ~2% of a core) for DeckLink's attention flag; when it's up it opens the link,
collects the events (EVENTS) and hands each fired hook to the voice. Timers fire from here. Setting a
hook in the game and saving its note happen under the keeper's lock, as does collecting the events and
taking their notes, so a hook can't fire before its note is there.

The link opens on the first request and closes after --idle seconds without one (the game closes its
end after 30 s without a frame anyway). While it's closed nothing reads the screen: the GStreamer
reader costs ~16% of a core while it runs (AddonTools lab E7), so it's stopped too.
"""
import argparse, json, os, select, socket, socketserver, sys, threading, time

from . import agent, dictation, hooks, link

GLANCE_S = 1.0  # seconds between glances at the strip while game hooks are set

SOCKET = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid(), "deckagent.sock")


class Keeper:
    def __init__(self, idle, make_link=None, clock=time.monotonic, hook_store=None):
        self.idle, self.clock = idle, clock
        self.lock = threading.Lock()
        self.lk = self.grabber = None
        self.last = 0
        self._make = make_link or self._real_link
        self.voice = None  # voice.Voice, with --voice
        self.dictation_keys = None  # set in serve(): one key sender for dictation states
        self.quiet = False  # the game's "Quiet" setting, from the last reply that carried it
        self.collected = []  # fired game hooks taken (with their notes) but not yet handed to the voice
        self.shown_at = 0  # time.monotonic() of the last card or reading view the assistant put up
        self._hooks = hook_store  # hooks.Hooks, made when first needed
        self._hooks_made = threading.Lock()
        # Seconds spent waiting on Ben's answers to prompts, ever (and since when, for the one up now),
        # so the voice doesn't count that time against the assistant: it takes prompt_waited() as a
        # question starts and subtracts the difference. One question runs at a time, and a dropped one's
        # prompt ends within a second (cancel_requests), so the difference is that question's own.
        self._waited, self._waiting_since = 0.0, None
        # Bumped by cancel_requests() (the voice dropped a question): requests that came in before it
        # are its, so they're dropped rather than run, and a prompt waiting for Ben is taken down.
        self.epoch = 0

    @property
    def hooks(self):
        with self._hooks_made:
            if self._hooks is None:
                self._hooks = hooks.Hooks()
        return self._hooks

    def prompt_waited(self):
        """Seconds spent waiting on Ben's answers to prompts so far, the one up now included."""
        since = self._waiting_since
        return self._waited + (time.monotonic() - since if since is not None else 0.0)

    def cancel_requests(self):
        """The question in progress was dropped: what it asked for and hasn't got yet isn't wanted."""
        self.epoch += 1

    def _grabber(self):
        """The one screen reader (two at once garble each other's colors): the link's, and the glances'."""
        if self.grabber is None:
            self.grabber = link.screen()
        return self.grabber

    def _real_link(self):
        return link.Link(reader=self._grabber().frame)

    def _ensure(self):
        if self.lk is None:
            t0 = time.monotonic()
            lk = self._make()
            t1 = time.monotonic()
            try:
                lk.open()
            except Exception:
                self._drop()
                raise
            self.lk = lk
            print("link opened: reader %.2f s, hello %.2f s" % (t1 - t0, time.monotonic() - t1), flush=True)
        self.last = self.clock()
        return self.lk

    def _drop(self):
        if self.lk is not None:
            try:
                self.lk.close()
            except Exception:
                pass
        self.lk = None
        if self.grabber is not None:
            try:
                if self._hooks is not None and self.hooks.any_game():
                    self.grabber.pause()  # glances still need it
                    return
                self.grabber.close()
            except Exception:
                pass
            self.grabber = None

    def handle(self, req, client_gone=None):
        """One request. `client_gone()`: the asker hung up (its process was ended), so the request
        isn't wanted any more; like a cancel_requests() since it came in, it's dropped unrun, or a
        prompt it's waiting on is taken down."""
        epoch = self.epoch

        def cancelled():
            return self.epoch != epoch or bool(client_gone and client_gone())
        cmd = req.get("cmd")
        if cmd == "dictation":  # the dictation hook (decktation-hook.sh), without starting a Python per state
            state, binding = req.get("state"), req.get("binding", "chat")
            if state not in dictation.STATES or binding not in dictation.BINDINGS:
                return {"ok": False, "error": "bad state or binding"}
            if state == "recording" and self.voice is not None:  # talking again: stop the spoken answer
                self.voice.interrupt(drop_question=binding == "agent")
            dictation.post(state, binding, sender=self.dictation_keys)
            return {"ok": True}
        if cmd == "ask":  # a question by voice (Decktation's agent binding, via decklink-agent-ask)
            if self.voice is None:
                return {"ok": False, "error": "voice is off (start with --voice)"}
            return {"ok": self.voice.ask(req.get("text", "")), "started": True}
        if cmd == "hook":
            return self.hook(req, cancelled)
        prepared = None
        if cmd not in ("status", "warm"):
            try:  # checked and built before the link opens (or is dropped): a bad request costs nothing
                prepared = agent.prepare(req)
            except (KeyError, TypeError, ValueError) as e:
                return {"ok": False, "error": str(e) or e.__class__.__name__}
        with self.lock:
            if cmd == "status":
                return {"ok": True, "open": self.lk is not None,
                        "idle_s": round(self.clock() - self.last, 1) if self.lk else None}
            try:
                if cancelled():  # it waited for the lock behind a request of a question since dropped
                    return {"ok": False, "cancelled": True, "error": "the question it was for was dropped"}
                if cmd == "warm":
                    self._ensure()
                    return {"ok": True, "open": True}
                lk = self._ensure()
                if prepared.waits:  # a prompt, or a layout waiting for a reply: Ben's time, not the model's
                    self._waiting_since = time.monotonic()
                    try:
                        reply = prepared.run(lk, cancelled)
                    finally:
                        self._waited += time.monotonic() - self._waiting_since
                        self._waiting_since = None
                else:
                    reply = prepared.run(lk)
                if cmd == "show" and req.get("kind") != "echo" and reply.get("ok"):
                    self.shown_at = time.monotonic()
                if "quiet" in reply:  # remembered for the voice: speak answers or only show them
                    self.quiet = reply["quiet"]
                return reply
            except (link.LinkError, agent.Malformed, KeyError, ValueError) as e:
                # A link that failed mid-request may be in any state: start clean next time.
                self._drop()
                return {"ok": False, "error": str(e) or e.__class__.__name__}
            finally:
                if self.lk is not None:
                    self.last = self.clock()

    # Hooks ---------------------------------------------------------------------------------------------

    def hook(self, req, cancelled=lambda: False):
        op = req.get("op")
        dropped = {"ok": False, "cancelled": True, "error": "the question it was for was dropped"}
        if op in ("add", "remove", "clear") and cancelled():
            return dropped
        try:
            if op == "add":
                when, note, action = req.get("when") or {}, req.get("note", ""), req.get("action", "wake")
                if action not in hooks.ACTIONS:
                    return {"ok": False, "error": "action is say or wake"}
                if when.get("track") == "timer":
                    minutes = float(when.get("minutes") or 0)
                    if not 0 < minutes <= 24 * 60:
                        return {"ok": False, "error": "a timer is 1 minute to 24 hours"}
                    return {"ok": True, "id": self.hooks.add_timer(minutes, note, action, req.get("desc", ""))}
                agent.build_hooks("add", when, req.get("minutes", 0))  # a bad one is refused before the link opens
                # The note is saved before the lock goes: until then the glance can't collect this hook's
                # event and find no note for it.
                with self.lock:
                    if cancelled():  # dropped while it waited for the lock (behind a prompt, say)
                        return dropped
                    reply = agent.hooks(self._ensure(), "add", when, req.get("minutes", 0))
                    if reply.get("ok") and not reply.get("already"):
                        self.hooks.add_game(reply["id"], note, action, req.get("desc", ""), req.get("minutes", 0))
                return reply
            if op == "list":
                game = None
                with self.lock:
                    if self.hooks.any_game():
                        # Hooks that fired are off the game's list but waiting as events: collected first
                        # (handed to the next glance), so pruning below can't take their notes.
                        self._collect_events()
                        r = agent.hooks(self._ensure(), "list")
                        game = r.get("hooks") if r.get("ok") else None
                        if game is not None:  # notes for hooks the game no longer has (a removal whose
                            self.hooks.keep_game({g["id"] for g in game})  # answer was lost) go
                    return {"ok": True, "hooks": self.hooks.listing(game)}
            # Removing: the game first, the note only once the game says so. If the game can't be
            # reached the hook is still set there, so its note stays (an answer lost after the game
            # removed it leaves a stale note, which the next list takes away).
            if op == "remove":
                hid = str(req.get("id", ""))
                with self.lock:
                    if cancelled():
                        return dropped
                    if hid.isdigit():
                        r = agent.hooks(self._ensure(), "remove", hid=int(hid))
                        if not r.get("ok"):
                            return {"ok": False, "error": r.get("reason") or "the game didn't remove it"}
                        kind = self.hooks.remove(hid) or ("game" if r.get("removed") else None)
                    else:
                        kind = self.hooks.remove(hid)
                return {"ok": kind is not None, "removed": hid if kind else None}
            if op == "clear":
                with self.lock:
                    if cancelled():
                        return dropped
                    r = agent.hooks(self._ensure(), "remove-all")
                    if not r.get("ok"):
                        return {"ok": False, "error": r.get("reason") or "the game didn't remove them"}
                    self.hooks.clear()
                return {"ok": True}
            return {"ok": False, "error": "hook op is add, list, remove or clear"}
        except (link.LinkError, agent.Malformed, KeyError, ValueError) as e:
            with self.lock:
                self._drop()
            return {"ok": False, "error": str(e) or e.__class__.__name__}

    def _collect_events(self):
        """Takes the game's fired-hook events (EVENTS) and their notes into self.collected, for the next
        glance to hand to the voice. Called with the lock held."""
        reply = agent.events(self._ensure())
        self.last = self.clock()
        for ev in reply.get("events", []):
            e = self.hooks.pop_game(ev["id"])
            if e is None:
                print("hooks: game hook %s fired, but there's no note for it here" % ev["id"], flush=True)
                continue
            self.collected.append((e, ev.get("seconds_ago", 0)))

    def glance(self):
        """Fires due timers; if game hooks are set, looks at the strip for DeckLink's attention flag and
        collects the events. Called about once a second. Returns the fired [(entry, seconds_ago)]."""
        fired = [(e, 0) for _, e in self.hooks.due_timers()]
        self.hooks.expire()
        with self.lock:  # events a hook list already took from the game
            fired.extend(self.collected)
            self.collected = []
        if not self.hooks.any_game():
            return fired
        try:
            with self.lock:
                status = self.lk.read() if self.lk is not None else self._grabber().glance()
                if not link.attention(status):
                    return fired
                self._collect_events()
                fired.extend(self.collected)
                self.collected = []
        except (link.LinkError, agent.Malformed, OSError) as e:
            print("hooks: glance failed: %s" % e, flush=True)
            with self.lock:
                self._drop()
        return fired

    def dispatch(self, fired):
        """Each fired hook to the voice: a reminder said, or the assistant woken with its note."""
        for e, ago in fired:
            print("hooks: fired (%s) %s" % (e["action"], e.get("desc") or e["note"][:60]), flush=True)
            if self.voice is None:
                continue
            title = "Hook: " + (e.get("desc") or "something happened")
            if e["action"] == "say":
                self.voice.remind(e["note"] or e.get("desc") or "Reminder.", title)
            else:
                self.voice.wake(hooks.wake_prompt(e.get("desc"), e["note"], ago), title)

    def tick(self):
        """Closes the link once it's been idle long enough, else marks it open (for the cover, and so
        telemetry holds off: its frames take >1 s of key taps). Called every second."""
        with self.lock:
            if self.lk is None:
                return
            if self.clock() - self.last >= self.idle:
                self._drop()
                print("link closed (idle)", flush=True)
            else:
                link.mark_open(time.time())


class NotRunning(OSError):
    """The service couldn't be reached: nothing was sent, so the caller may do the work itself."""


class NoAnswer(Exception):
    """The request went in, but no answer came back (a timeout, the service went away): it may have been
    carried out. Never an OSError, so a caller falling back on "not running" can't take it for one."""


def ask(req, path=SOCKET, timeout=30):
    """A client's side: one request to the running service. NotRunning when it can't be reached,
    NoAnswer when it took the request but its answer didn't come."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        try:
            s.connect(path)
        except OSError as e:
            raise NotRunning(e.errno, str(e)) from e
        try:
            s.sendall(json.dumps(req).encode() + b"\n")
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
            return json.loads(data)
        except (OSError, ValueError) as e:
            raise NoAnswer(str(e) or e.__class__.__name__) from e
    finally:
        s.close()


def serve(keeper, path=SOCKET):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            line = self.rfile.readline()
            conn = self.connection

            def gone():
                """The client sends one line and waits: anything readable now is its end closing."""
                try:
                    readable, _, _ = select.select([conn], [], [], 0)
                    return bool(readable) and conn.recv(1, socket.MSG_PEEK) == b""
                except OSError:
                    return True
            try:
                reply = keeper.handle(json.loads(line), gone)
            except ValueError:
                reply = {"ok": False, "error": "not JSON"}
            try:
                self.wfile.write(json.dumps(reply).encode() + b"\n")
            except OSError:  # the asker hung up (its question was dropped): nobody to answer
                pass

    if os.path.exists(path):
        os.remove(path)
    old = os.umask(0o177)  # the socket is this user's only
    try:
        server = socketserver.ThreadingUnixStreamServer(path, Handler)
    finally:
        os.umask(old)

    def ticker():
        while True:
            time.sleep(1)
            keeper.tick()
    threading.Thread(target=ticker, daemon=True).start()

    def glancer():
        while True:
            time.sleep(GLANCE_S)
            try:
                keeper.dispatch(keeper.glance())
            except Exception as e:  # never let one bad glance stop hooks for good
                print("hooks: %s" % e, flush=True)
    threading.Thread(target=glancer, daemon=True).start()
    print("deckagent link keeper on %s (idle close after %d s)" % (path, keeper.idle), flush=True)
    try:
        server.serve_forever()
    finally:
        with keeper.lock:
            keeper._drop()
        os.remove(path)


def preload():
    """Loads GStreamer now (~2 s in a fresh process), so the first question after a boot doesn't wait."""
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
    except Exception as e:  # not on the Deck, or no GStreamer: the first open just pays for it
        print("preload skipped: %s" % e, flush=True)


def main(argv=None):
    p = argparse.ArgumentParser(prog="decklink.agentd", description=__doc__.split("\n\n")[0])
    p.add_argument("--idle", type=float, default=25, help="seconds without a request before closing the link")
    p.add_argument("--socket", default=SOCKET)
    p.add_argument("--voice", action="store_true", help="also answer voice questions (voice.py)")
    p.add_argument("--ask", metavar="TEXT", help="client: hand TEXT to the running service as a voice question")
    a = p.parse_args(argv)
    if a.ask is not None:  # decklink-agent-ask: Decktation's agent binding command
        try:
            reply = ask({"cmd": "ask", "text": a.ask}, a.socket)
        except NotRunning as e:
            print("decklink.agentd: the service isn't running (%s)" % e, file=sys.stderr)
            return 1
        except NoAnswer as e:
            print("decklink.agentd: the service took the question but didn't answer (%s)" % e, file=sys.stderr)
            return 1
        print(json.dumps(reply))
        return 0 if reply.get("ok") else 1
    preload()
    keeper = Keeper(a.idle)
    # A dictation state is a 64-bit frame: sent in one burst it costs the game ~20 ms (lab E6: ~0.34 ms
    # per bit), less than a frame, and arrives ~0.1 s sooner than paced taps.
    keeper.dictation_keys = link.keys_sender("xdotool", gap_ms=0)
    if a.voice:
        from . import voice
        keeper.voice = voice.Voice(game=keeper)
    serve(keeper, a.socket)


if __name__ == "__main__":
    sys.exit(main())
