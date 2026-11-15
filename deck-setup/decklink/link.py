"""The host's end of the link: keys in (keys.py), the strip out (strip.py), stop-and-wait both ways.

    with Link() as link:              # opens: hello by keys until the strip shows up
        link.send(7, b"hello addon")  # blocks until the strip shows the game got it; resends if not
        ch, payload = link.recv(5)    # waits for the game's next frame and acks it; None on timeout

Sending waits while the game isn't ready (a text box has focus: keys would go there, E5/E6).
Channel 0 is the link's own (codec.OP_*); addons use 1-255.
"""
import base64, os, time

from . import codec, keys, strip

# Frames this big or bigger go by paste (the clipboard, ~0.3 s whatever the size) rather than key taps
# (~55 bytes a second), when a clipboard tool is there. Smaller ones aren't worth the focus change.
PASTE_MIN = 40

# When the link was last seen open (epoch seconds as text; 0 after a bye), for the cover (cover.py),
# which shows only while this is fresh. Shared by root and deck, hence world-writable.
OPEN_FILE = "/tmp/decklink-open"
OPEN_FOR = 30  # the game closes the link after this long without a frame (Link.lua OPEN_FOR)


def mark_open(when):
    try:
        try:
            fd = os.open(OPEN_FILE, os.O_WRONLY | os.O_TRUNC)  # no O_CREAT on another user's /tmp file
        except FileNotFoundError:
            fd = os.open(OPEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
            os.fchmod(fd, 0o666)
        os.write(fd, b"%.1f" % when)
        os.close(fd)
    except OSError:
        pass  # the cover is a nicety; the link works without it


# While a dictation is under way (dictation.py), its states shouldn't wait behind telemetry's taps:
# this file says so, and telemetry holds off while it's fresh. A hold older than HOLD_FOR is stale
# (a hook that never said done), so it can't stop telemetry for good.
HOLD_FILE = "/tmp/decklink-hold"
HOLD_FOR = 60


def hold(on):
    try:
        if on:
            try:
                fd = os.open(HOLD_FILE, os.O_WRONLY | os.O_TRUNC)
            except FileNotFoundError:
                fd = os.open(HOLD_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
                os.fchmod(fd, 0o666)
            os.write(fd, b"%.1f" % time.time())
            os.close(fd)
        elif os.path.exists(HOLD_FILE):
            os.remove(HOLD_FILE)
    except OSError:
        pass


def held():
    try:
        return time.time() - os.path.getmtime(HOLD_FILE) < HOLD_FOR
    except OSError:
        return False


def open_since():
    """When the link was last seen open, per OPEN_FILE (0: closed or unknown)."""
    try:
        with open(OPEN_FILE) as f:
            return float(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


# How long after its taps a frame has to show as arrived before it's sent again. The taps are all in
# when keys.send returns, and the strip shows the frame within a few game frames (measured ~0.25 s
# round trip on the Deck, 2026-11-14); an occasional frame is lost outright, and waiting 2 s for it
# cost every such request 2 s.
ARRIVE_S = 0.8


class LinkError(Exception):
    pass


class Link:
    def __init__(self, sender=None, reader=None, clock=time.monotonic, sleep=time.sleep, clipboard=None):
        self.keys = sender or keys.Keys()
        self._clip = clipboard  # clipboard.Clipboard, made when first needed; False: never paste
        self._reader = reader
        self.clock, self.sleep = clock, sleep
        self.seq = int(clock() * 1000) % 255 + 1  # 1-255; a fresh start rarely repeats the game's last
        self.last_in = None  # seq of the last outbound frame from the game we delivered
        self.status = None

    # Reading the strip ------------------------------------------------------------------------------

    def read(self):
        """The strip's Status now, or None when there's no strip (link closed, or DeckLink missing)."""
        if self._reader is None:
            self._reader = strip.Grabber().frame
        try:
            self.status = strip.decode(self._reader())
        except strip.NoStrip:
            self.status = None
        if self.status is not None and self.status.flags & strip.FLAG_OPEN:
            now = time.time()
            if now - getattr(self, "_marked", 0) > 1:
                mark_open(now)
                self._marked = now
        return self.status

    def wait(self, done, timeout, what):
        deadline = self.clock() + timeout
        while True:
            s = self.read()
            if s is not None and done(s):
                return s
            if self.clock() >= deadline:
                raise LinkError("timed out waiting for " + what)
            self.sleep(0.02)

    # Sending ------------------------------------------------------------------------------------------

    def _next_seq(self):
        self.seq = self.seq % 255 + 1
        return self.seq

    def _frame(self, channel, payload):
        if len(payload) > codec.MAX_IN:
            raise LinkError("payload over %d bytes" % codec.MAX_IN)
        seq = self._next_seq()
        return seq, codec.build(channel, seq, payload)

    def open(self, timeout=10):
        """Sends hello until the strip shows the link open."""
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            seq, frame = self._frame(0, bytes([codec.OP_HELLO]))
            self.keys.send(list(codec.bits(frame)))
            try:
                self.wait(lambda s: s.flags & strip.FLAG_OPEN and s.rx_seq == seq, 1.0, "hello")
                return self
            except LinkError:
                continue
        raise LinkError("the game didn't answer (DeckLink loaded? in the world? chat closed?)")

    def clipboard(self):
        if self._clip is None:
            from .clipboard import Clipboard
            self._clip = Clipboard()
        return self._clip if self._clip and self._clip.available else None

    def send(self, channel, payload, timeout=10, tries=3):
        """Sends one frame and waits until the strip shows it arrived (its seq as the last good one).
        Returns the frame's seq (replies can name it). Big frames go by paste when they can."""
        payload = bytes(payload)
        if len(payload) >= PASTE_MIN and hasattr(self.keys, "paste") and self.clipboard():
            try:
                return self.send_by_paste(channel, payload)
            except (LinkError, OSError) as e:
                print("decklink: paste failed (%s); sending by keys" % e, flush=True)
        return self.send_by_keys(channel, payload, timeout, tries)

    def send_by_paste(self, channel, payload, tries=2):
        """The frame as one line on the clipboard: a PASTE frame (by keys) has DeckLink focus its box,
        Ctrl+V puts the line in, and the strip shows the frame's seq once it checked out. The clipboard
        is given back what it had."""
        if len(payload) > codec.MAX_IN:
            raise LinkError("payload over %d bytes" % codec.MAX_IN)
        seq = self._next_seq()
        line = ("DLP1 %d %d %d %04x " % (seq, channel, len(payload), codec.crc16(payload))).encode() \
            + base64.b64encode(payload)
        clip = self.clipboard()
        saved = clip.get()
        clip.set(line)
        try:
            for _ in range(tries):
                self.send_by_keys(0, bytes([codec.OP_PASTE]))
                try:
                    self.wait(lambda s: s.flags & strip.FLAG_FOCUS, 1.0, "the paste box to take focus")
                    self.keys.paste()
                    self.wait(lambda s: s.rx_seq == seq, 2.0, "the pasted frame")
                    return seq
                except LinkError:
                    continue
            raise LinkError("pasted frame not received after %d tries" % tries)
        finally:
            if saved:
                try:
                    clip.set(saved)
                except OSError:
                    pass

    def send_by_keys(self, channel, payload, timeout=10, tries=3):
        seq, frame = self._frame(channel, bytes(payload))
        for _ in range(tries):
            s = self.wait(lambda s: s.flags & strip.FLAG_READY, timeout, "the game to be ready (chat open?)")
            bad = s.bad
            self.keys.send(list(codec.bits(frame)))
            try:
                self.wait(lambda s: s.rx_seq == seq or s.bad != bad, ARRIVE_S, "the frame to arrive")
            except LinkError:
                continue
            if self.status.rx_seq == seq:
                return seq
        raise LinkError("frame not received after %d tries" % tries)

    # Receiving ------------------------------------------------------------------------------------------

    def recv(self, timeout=5):
        """The game's next outbound frame, as (channel, payload), acked; None on timeout."""
        deadline = self.clock() + timeout
        while True:
            s = self.read()
            if s is not None and s.out is not None:
                channel, seq, payload = codec.parse(s.out)
                self.send(0, bytes([codec.OP_ACK, seq]))
                if seq != self.last_in:  # a resend after a slow ack is the same frame again
                    self.last_in = seq
                    return channel, payload
            if self.clock() >= deadline:
                return None
            self.sleep(0.02)

    def ping(self, payload=b"ping", timeout=5):
        """Round trip: the game echoes a ping on channel 0. Returns the seconds it took."""
        start = self.clock()
        self.send(0, bytes([codec.OP_PING]) + bytes(payload))
        while True:
            got = self.recv(max(0.1, timeout - (self.clock() - start)))
            if got is None:
                raise LinkError("no ping reply")
            if got == (0, bytes([codec.OP_PING]) + bytes(payload)):
                return self.clock() - start

    def close(self):
        """Says bye and doesn't wait: the game takes the strip down as it closes, so there's no ack to see."""
        try:
            _, frame = self._frame(0, bytes([codec.OP_BYE]))
            self.keys.send(list(codec.bits(frame)))
        except Exception:
            pass
        mark_open(0)

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()
