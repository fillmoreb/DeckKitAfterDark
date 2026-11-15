"""Reads DeckLink's strip, the game's side of the link: one row of colored cells along the bottom edge
of the screen (addons/DeckLink/Strip.lua; keep the two in step).

    cells    4 sync (red, green, blue, white; the red one's width is the cell size), 1 bits-per-channel
             cell (B as on/off red 4, green 2, blue 1), 2^B gray ramp cells (every level, so gamma is
             measured, not assumed), then a bit stream, B bits in each of red, green, blue per cell:
    status   8 format | 8 flags | 16 heartbeat | 8 last good inbound seq | 16 inbound frames received |
             16 bad inbound frames | 16 outbound frame length L (0: none) | 16 CRC-16 of those 11 bytes
    outbound L bytes: one frame (codec.py) waiting for the host's ack

The encoding is DeckDev's strip (AddonTools addons/DeckDev/Strip.lua) cut down to one row. Frames come
from gamescope's PipeWire stream through GStreamer (AddonTools deck/deckstrip.py's Grabber), which
leaves overlays (MangoHud) out. Runs as the desktop user.
"""
import collections, json, os, struct, subprocess

from . import codec

FORMAT = 1
SYNC = [(1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 1)]
STATUS_BYTES = 11
ROWS = 16  # the bottom rows the Grabber keeps: decode reads at most 16 (a cell is 1-16 px)
FLAG_OPEN, FLAG_READY, FLAG_BOUND, FLAG_FOCUS = 1, 2, 4, 8

Status = collections.namedtuple("Status", "flags heartbeat rx_seq rx_count bad out")


class NoStrip(Exception):
    pass


class Frame:
    """One RGB picture: rows of (r, g, b)."""

    def __init__(self, width, height, stride, data):
        self.width, self.height, self.stride, self.data = width, height, stride, data

    def row(self, y):
        line = self.data[y * self.stride:y * self.stride + self.width * 3]
        return list(zip(line[0::3], line[1::3], line[2::3]))


class Grabber:
    """gamescope's PipeWire stream, decoded by GStreamer into RGB; frame() returns the newest, cut to its
    bottom ROWS rows before converting (measured on the Deck: about 9% of a core above idle instead of
    26% for whole frames). The crop is learned from the first frame (and again when the screen size changes)."""

    def __init__(self):
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        os.environ.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
        dump = json.loads(subprocess.run(["pw-dump"], capture_output=True, timeout=5).stdout or b"[]")
        node = next((o["id"] for o in dump if o.get("type", "").endswith("Node")
                     and ((o.get("info") or {}).get("props") or {}).get("node.name") == "gamescope"), None)
        if node is None:
            raise NoStrip("no gamescope PipeWire stream (Game Mode only)")
        self.Gst, self.node = Gst, node
        self._start(0)

    def _start(self, top):
        """(Re)build the pipeline cropping `top` rows off. Changing videocrop's size on a playing pipeline
        stalls pipewiresrc's renegotiation (seen on the Deck: no frames after it), so it's rebuilt.
        gamescope offers BGRx and NV12. Straight into videoconvert it picks BGRx (exact colors), but with
        videocrop in between it falls to NV12, whose YUV round trip shifts the strip's colors enough to
        fail its checksum most of the time: so BGRx is asked for by name."""
        Gst = self.Gst
        if getattr(self, "pipeline", None) is not None:
            self.pipeline.set_state(Gst.State.NULL)
        self.top = top
        self.pipeline = Gst.parse_launch(
            "pipewiresrc path=%d always-copy=true ! video/x-raw,format=BGRx ! videocrop top=%d ! "
            "videoconvert ! video/x-raw,format=RGB ! "
            "appsink name=sink max-buffers=1 drop=true sync=false" % (self.node, top))
        self.sink = self.pipeline.get_by_name("sink")
        self.pipeline.set_state(Gst.State.PLAYING)

    def frame(self, timeout=3.0):
        Gst = self.Gst
        sample = self.sink.emit("try-pull-sample", int(timeout * Gst.SECOND))
        if sample is None:
            raise NoStrip("no frame from PipeWire")
        caps = sample.get_caps().get_structure(0)
        width, height = caps.get_value("width"), caps.get_value("height")
        if height != ROWS and height + self.top > ROWS:  # uncropped yet, or the stream changed size
            self._start(height + self.top - ROWS)
        buf = sample.get_buffer()
        data = buf.extract_dup(0, buf.get_size())
        return Frame(width, height, len(data) // height, data)

    def close(self):
        self.pipeline.set_state(self.Gst.State.NULL)


class _Bits:
    def __init__(self, pixels, lut, bits):
        self.value = 0
        for p in pixels:
            for c in range(3):
                self.value = (self.value << bits) | lut[c][p[c]]
        self.left = len(pixels) * 3 * bits

    def take(self, width):
        if width > self.left:
            raise NoStrip("stream ran out")
        self.left -= width
        return (self.value >> self.left) & ((1 << width) - 1)


def decode(frame):
    """The Status in a Frame, or raises NoStrip (no strip on screen: the link is closed or DeckLink
    isn't loaded). out is the outbound frame's bytes, None when there's none or it didn't check out."""
    on = lambda p: tuple(1 if v > 127 else 0 for v in p)
    bottom = frame.row(frame.height - 1)
    cell = 0
    while cell < len(bottom) and on(bottom[cell]) == (1, 0, 0):
        cell += 1
    if not 1 <= cell <= 16:
        raise NoStrip("no strip")
    row = frame.row(frame.height - cell + cell // 2)
    cells = [row[i * cell + cell // 2] for i in range(len(row) // cell)]
    if [on(p) for p in cells[:len(SYNC)]] != SYNC:
        raise NoStrip("bad sync")
    r, g, b = on(cells[len(SYNC)])
    bits = r * 4 + g * 2 + b
    if not 1 <= bits <= 6:
        raise NoStrip("bad bits cell")
    levels = 1 << bits
    first = len(SYNC) + 1 + levels
    ramp = cells[len(SYNC) + 1:first]
    lut = [[min(range(levels), key=lambda n: abs(ramp[n][c] - v)) for v in range(256)] for c in range(3)]
    stream = _Bits(cells[first:], lut, bits)
    status = bytes(stream.take(8) for _ in range(STATUS_BYTES))
    if stream.take(16) != codec.crc16(status):
        raise NoStrip("status checksum failed")
    version, flags, heartbeat, rx_seq, rx_count, bad, out_len = struct.unpack(">BBHBHHH", status)
    if version != FORMAT:
        raise NoStrip("strip format %d, this reader reads %d" % (version, FORMAT))
    out = None
    if out_len:
        try:
            out = bytes(stream.take(8) for _ in range(out_len))
        except NoStrip:
            out = None
        if out is not None and codec.parse(out) is None:
            out = None
    return Status(flags, heartbeat, rx_seq, rx_count, bad, out)
