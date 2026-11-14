"""The host's end of the link: keys in (keys.py), the strip out (strip.py), stop-and-wait both ways.

    with Link() as link:              # opens: hello by keys until the strip shows up
        link.send(7, b"hello addon")  # blocks until the strip shows the game got it; resends if not
        ch, payload = link.recv(5)    # waits for the game's next frame and acks it; None on timeout

Sending waits while the game isn't ready (a text box has focus: keys would go there, E5/E6).
Channel 0 is the link's own (codec.OP_*); addons use 1-255.
"""
import os, time

from . import codec, keys, strip

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


def open_since():
    """When the link was last seen open, per OPEN_FILE (0: closed or unknown)."""
    try:
        with open(OPEN_FILE) as f:
            return float(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


class LinkError(Exception):
    pass


class Link:
    def __init__(self, sender=None, reader=None, clock=time.monotonic, sleep=time.sleep):
        self.keys = sender or keys.Keys()
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

    def send(self, channel, payload, timeout=10, tries=3):
        """Sends one frame and waits until the strip shows it arrived (its seq as the last good one)."""
        seq, frame = self._frame(channel, bytes(payload))
        for _ in range(tries):
            s = self.wait(lambda s: s.flags & strip.FLAG_READY, timeout, "the game to be ready (chat open?)")
            bad = s.bad
            self.keys.send(list(codec.bits(frame)))
            try:
                self.wait(lambda s: s.rx_seq == seq or s.bad != bad, 2.0, "the frame to arrive")
            except LinkError:
                continue
            if self.status.rx_seq == seq:
                return
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
        try:
            self.send(0, bytes([codec.OP_BYE]), timeout=2, tries=1)
        except LinkError:
            pass
        mark_open(0)

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()
