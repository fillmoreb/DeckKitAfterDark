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
Errors come back as {"ok": false, "error": "..."}.

The link opens on the first request and closes after --idle seconds without one (the game closes its
end after 30 s without a frame anyway). While it's closed nothing reads the screen: the GStreamer
reader costs ~16% of a core while it runs (AddonTools lab E7), so it's stopped too.
"""
import argparse, json, os, socket, socketserver, sys, threading, time

from . import agent, dictation, keys, link, strip

SOCKET = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid(), "deckagent.sock")


class Keeper:
    def __init__(self, idle, make_link=None, clock=time.monotonic):
        self.idle, self.clock = idle, clock
        self.lock = threading.Lock()
        self.lk = self.grabber = None
        self.last = 0
        self._make = make_link or self._real_link
        self.voice = None  # voice.Voice, with --voice
        self.dictation_keys = None  # set in serve(): one key sender for dictation states

    def _real_link(self):
        self.grabber = strip.Grabber()
        return link.Link(sender=keys.Keys(), reader=self.grabber.frame)

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
        if self.grabber is not None:
            try:
                self.grabber.close()
            except Exception:
                pass
        self.lk = self.grabber = None

    def handle(self, req):
        cmd = req.get("cmd")
        if cmd == "dictation":  # the dictation hook (decktation-hook.sh), without starting a Python per state
            state, binding = req.get("state"), req.get("binding", "chat")
            if state not in dictation.STATES or binding not in dictation.BINDINGS:
                return {"ok": False, "error": "bad state or binding"}
            dictation.post(state, binding, sender=self.dictation_keys)
            return {"ok": True}
        if cmd == "ask":  # a question by voice (Decktation's agent binding, via decklink-agent-ask)
            if self.voice is None:
                return {"ok": False, "error": "voice is off (start with --voice)"}
            return {"ok": self.voice.ask(req.get("text", "")), "started": True}
        with self.lock:
            if cmd == "status":
                return {"ok": True, "open": self.lk is not None,
                        "idle_s": round(self.clock() - self.last, 1) if self.lk else None}
            try:
                if cmd == "warm":
                    self._ensure()
                    return {"ok": True, "open": True}
                if cmd == "state":
                    fields = req.get("fields") or agent.FIELDS
                    if any(f not in agent.FIELDS for f in fields):
                        return {"ok": False, "error": "unknown field"}
                    return agent.state(self._ensure(), fields)
                if cmd == "way":
                    return agent.way(self._ensure(), req["map"], req["x"], req["y"])
                return {"ok": False, "error": "unknown cmd %r" % cmd}
            except (link.LinkError, keys.SendError, strip.NoStrip, agent.Malformed, KeyError, ValueError) as e:
                # A link that failed mid-request may be in any state: start clean next time.
                self._drop()
                return {"ok": False, "error": str(e) or e.__class__.__name__}
            finally:
                if self.lk is not None:
                    self.last = self.clock()

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
        keeper.voice = voice.Voice()
    serve(keeper, a.socket)


if __name__ == "__main__":
    sys.exit(main())
