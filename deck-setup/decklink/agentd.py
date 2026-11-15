"""DeckAgent's link keeper: a small service on the Deck that owns DeckLink for the assistant, so each
question doesn't pay to start the screen reader and open the link again.

    python3 -m decklink.agentd [--idle 25]        (the decklink-agent user service runs it)

It listens on a Unix socket ($XDG_RUNTIME_DIR/deckagent.sock, this user only) for one JSON line per
request and answers with one JSON line:
  {"cmd": "state", "fields": ["pos", "world", ...]}   -> agent.state reply
  {"cmd": "way", "map": 1454, "x": 36, "y": 87.7}     -> agent.way reply
  {"cmd": "warm"}       open the link now (a question is coming: L5+R5 went down)
  {"cmd": "status"}     whether the link is open, and how long it's been idle
  {"cmd": "dictation", "state": "recording", "binding": "agent"}   post a dictation state (the hook)
  {"cmd": "ask", "text": "..."}   answer a question by voice: run deckagent, speak the answer (voice.py)
  {"cmd": "hook", "op": "add" | "list" | "remove" | "clear", ...}   the assistant's hooks (hooks.py)
Errors come back as {"ok": false, "error": "..."}.

Hooks: while any game hook is set, the keeper glances at the strip about once a second (one paused
capture pipeline, ~2% of a core) for DeckLink's attention flag; when it's up it opens the link,
collects the events (EVENTS) and hands each fired hook to the voice. Timers fire from here.

The link opens on the first request and closes after --idle seconds without one (the game closes its
end after 30 s without a frame anyway). While it's closed nothing reads the screen: the GStreamer
reader costs ~16% of a core while it runs (AddonTools lab E7), so it's stopped too.
"""
import argparse, json, os, socket, socketserver, sys, threading, time

from . import agent, dictation, hooks, keys, link, strip

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
        self.shown_at = 0  # time.monotonic() of the last card or reading view the assistant put up
        self._hooks = hook_store  # hooks.Hooks, made when first needed

    @property
    def hooks(self):
        if self._hooks is None:
            self._hooks = hooks.Hooks()
        return self._hooks

    def _grabber(self):
        """The one screen reader (two at once garble each other's colors): the link's, and the glances'."""
        if self.grabber is None:
            self.grabber = strip.Grabber()
        return self.grabber

    def _real_link(self):
        return link.Link(sender=keys.Keys(), reader=self._grabber().frame)

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

    def handle(self, req):
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
            return self.hook(req)
        with self.lock:
            if cmd == "status":
                return {"ok": True, "open": self.lk is not None,
                        "idle_s": round(self.clock() - self.last, 1) if self.lk else None}
            try:
                if cmd == "warm":
                    self._ensure()
                    return {"ok": True, "open": True}
                if cmd in ("state", "way", "chat", "friends", "prompt", "show", "quests", "quest-status", "waypoints", "checklist",
                           "character", "bags", "item-info", "prices", "mark"):
                    if cmd == "state" and any(f not in agent.FIELDS for f in req.get("fields") or agent.FIELDS):
                        return {"ok": False, "error": "unknown field"}
                    # Packets are built (and checked) before the link opens: a bad request shouldn't cost one.
                    {"chat": lambda: agent.build_chat(req["channel"], req.get("target", ""), req["text"]),
                     "friends": lambda: agent.build_friends(req.get("lists") or agent.LISTS),
                     "prompt": lambda: agent.build_ask(req["question"], req["options"], req.get("list", False),
                                                       req.get("seconds", 30)),
                     "checklist": lambda: agent.build_checklist(req.get("title"), req.get("items"),
                                                                req.get("clear", False)),
                     "mark": lambda: agent.build_marks(req.get("label", ""), req.get("ids"), req.get("clear", False)),
                     "prices": lambda: agent.build_prices(req["ids"]),
                     "item-info": lambda: agent.build_item_info(req["items"]),
                     "waypoints": lambda: agent.build_waypoints(req.get("action", "list"), req.get("id", 0)),
                     "quest-status": lambda: agent.build_quest_status(req["ids"]),
                     "show": lambda: agent.build_show(req["kind"], req.get("title", ""), req.get("text", ""),
                                                      req.get("seconds", 0), req.get("item", 0))
                     }.get(cmd, lambda: None)()
                    reply = agent.handle(self._ensure(), req)
                    if cmd == "show" and req.get("kind") != "echo" and reply.get("ok"):
                        self.shown_at = time.monotonic()
                    if "quiet" in reply:  # remembered for the voice: speak answers or only show them
                        self.quiet = reply["quiet"]
                    return reply
                return {"ok": False, "error": "unknown cmd %r" % cmd}
            except (link.LinkError, keys.SendError, strip.NoStrip, agent.Malformed, KeyError, ValueError) as e:
                # A link that failed mid-request may be in any state: start clean next time.
                self._drop()
                return {"ok": False, "error": str(e) or e.__class__.__name__}
            finally:
                if self.lk is not None:
                    self.last = self.clock()

    # Hooks ---------------------------------------------------------------------------------------------

    def hook(self, req):
        op = req.get("op")
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
                with self.lock:
                    reply = agent.hooks(self._ensure(), "add", when, req.get("minutes", 0))
                if reply.get("ok") and not reply.get("already"):
                    self.hooks.add_game(reply["id"], note, action, req.get("desc", ""), req.get("minutes", 0))
                return reply
            if op == "list":
                game = None
                if self.hooks.any_game():
                    with self.lock:
                        r = agent.hooks(self._ensure(), "list")
                    game = r.get("hooks") if r.get("ok") else None
                return {"ok": True, "hooks": self.hooks.listing(game)}
            if op == "remove":
                hid = str(req.get("id", ""))
                kind = self.hooks.remove(hid)
                if hid.isdigit():
                    with self.lock:
                        agent.hooks(self._ensure(), "remove", hid=int(hid))
                    kind = kind or "game"
                return {"ok": kind is not None, "removed": hid if kind else None}
            if op == "clear":
                self.hooks.clear()
                with self.lock:
                    agent.hooks(self._ensure(), "remove-all")
                return {"ok": True}
            return {"ok": False, "error": "hook op is add, list, remove or clear"}
        except (link.LinkError, keys.SendError, strip.NoStrip, agent.Malformed, KeyError, ValueError) as e:
            with self.lock:
                self._drop()
            return {"ok": False, "error": str(e) or e.__class__.__name__}

    def glance(self):
        """Fires due timers; if game hooks are set, looks at the strip for DeckLink's attention flag and
        collects the events. Called about once a second. Returns the fired [(entry, seconds_ago)]."""
        fired = [(e, 0) for _, e in self.hooks.due_timers()]
        self.hooks.expire()
        if not self.hooks.any_game():
            return fired
        try:
            with self.lock:
                status = self.lk.read() if self.lk is not None else self._grabber().glance()
                if status is None or not status.flags & strip.FLAG_ATTENTION:
                    return fired
                reply = agent.events(self._ensure())
                self.last = self.clock()
        except (link.LinkError, keys.SendError, strip.NoStrip, agent.Malformed, OSError) as e:
            print("hooks: glance failed: %s" % e, flush=True)
            with self.lock:
                self._drop()
            return fired
        for ev in reply.get("events", []):
            e = self.hooks.pop_game(ev["id"])
            if e is None:
                print("hooks: game hook %s fired, but there's no note for it here" % ev["id"], flush=True)
                continue
            fired.append((e, ev.get("seconds_ago", 0)))
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


def ask(req, path=SOCKET, timeout=30):
    """A client's side: one request to the running service. Raises OSError when it isn't running."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
        s.sendall(json.dumps(req).encode() + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    finally:
        s.close()
    return json.loads(data)


def serve(keeper, path=SOCKET):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            line = self.rfile.readline()
            try:
                reply = keeper.handle(json.loads(line))
            except ValueError:
                reply = {"ok": False, "error": "not JSON"}
            self.wfile.write(json.dumps(reply).encode() + b"\n")

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
        except OSError as e:
            print("decklink.agentd: the service isn't running (%s)" % e, file=sys.stderr)
            return 1
        print(json.dumps(reply))
        return 0 if reply.get("ok") else 1
    preload()
    keeper = Keeper(a.idle)
    # A dictation state is a 64-bit frame: sent in one burst it costs the game ~20 ms (lab E6: ~0.34 ms
    # per bit), less than a frame, and arrives ~0.1 s sooner than paced taps.
    keeper.dictation_keys = keys.Keys("xdotool", gap_ms=0)
    if a.voice:
        from . import voice
        keeper.voice = voice.Voice(game=keeper)
    serve(keeper, a.socket)


if __name__ == "__main__":
    sys.exit(main())
