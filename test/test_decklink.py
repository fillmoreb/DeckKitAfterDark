"""DeckLink tests: the Python host side against the real addon code (through test/harness.lua).

    python3 -m unittest discover -s test     (needs lua5.1, or lua with LUA=...)
"""
import base64, os, random, shutil, struct, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "deck-setup"))
from decklink import codec, dictation, keys, link, strip, telemetry  # noqa: E402

LUA = os.environ.get("LUA") or shutil.which("lua5.1") or shutil.which("lua")


def bitstr(frame):
    return "".join(str(b) for b in codec.bits(frame))


class Game:
    """One harness run: returns (printed lines, Status or None)."""

    @staticmethod
    def run(code):
        with tempfile.TemporaryDirectory() as tmp:
            pic = os.path.join(tmp, "strip.ppm")
            out = subprocess.run([LUA, os.path.join(HERE, "harness.lua")], input=code.encode(),
                                 capture_output=True, env=dict(os.environ, PICTURE=pic))
            if out.returncode != 0:
                raise AssertionError("harness failed:\n" + out.stderr.decode())
            with open(pic, "rb") as f:
                data = f.read()
        head, pixels = data.split(b"\n", 1)
        _, w, h = head.split()
        frame = strip.Frame(int(w), int(h), int(w) * 3, pixels)
        try:
            status = strip.decode(frame)
        except strip.NoStrip:
            status = None
        return out.stdout.decode().splitlines(), status


def hello(seq=1):
    return 'taps("%s") frame()' % bitstr(codec.build(0, seq, bytes([codec.OP_HELLO])))


ECHO = 'DeckLink.On(5, function(p) print("GOT " .. p) end) login() '


class Codec(unittest.TestCase):
    def test_round_trip(self):
        for payload in (b"", b"x", bytes(range(256)) * 3):
            self.assertEqual(codec.parse(codec.build(9, 200, payload)), (9, 200, payload))

    def test_crc_catches_a_flipped_bit(self):
        f = bytearray(codec.build(1, 1, b"hello"))
        f[5] ^= 0x10
        self.assertIsNone(codec.parse(bytes(f)))

    def test_crc_matches_lua(self):
        self.assertEqual(codec.crc16(b"123456789"), 0x29B1)  # the CCITT-FALSE check value
        lines, _ = Game.run('print("CRC " .. CODEC.CRC("123456789"))')
        self.assertIn("CRC %d" % 0x29B1, lines)

    def test_build_matches_lua(self):
        lines, _ = Game.run('print("HEX " .. (CODEC.Build(9, 200, "abc"):gsub(".", function(c) return ("%02x"):format(c:byte()) end)))')
        self.assertIn("HEX " + codec.build(9, 200, b"abc").hex(), lines)


class In(unittest.TestCase):
    def test_closed_until_hello(self):
        _, status = Game.run(ECHO)
        self.assertIsNone(status)  # no strip while nobody's listening

    def test_hello_opens_and_shows_ready(self):
        _, s = Game.run(ECHO + hello(7))
        self.assertTrue(s.flags & strip.FLAG_OPEN and s.flags & strip.FLAG_READY and s.flags & strip.FLAG_BOUND)
        self.assertEqual((s.rx_seq, s.rx_count, s.bad), (7, 1, 0))

    def test_frame_delivered(self):
        lines, s = Game.run(ECHO + hello() + ' taps("%s") frame()' % bitstr(codec.build(5, 2, b"hi there")))
        self.assertIn("GOT hi there", lines)
        self.assertEqual((s.rx_seq, s.rx_count, s.bad), (2, 2, 0))

    def test_back_to_back_frames_in_one_burst(self):
        burst = bitstr(codec.build(5, 2, b"one")) + bitstr(codec.build(5, 3, b"two"))
        lines, s = Game.run(ECHO + hello() + ' taps("%s") frame()' % burst)
        self.assertEqual([l for l in lines if l.startswith("GOT")], ["GOT one", "GOT two"])
        self.assertEqual(s.rx_seq, 3)

    def test_a_lost_tap_is_a_bad_frame_not_bad_data(self):
        bits = bitstr(codec.build(5, 2, b"hello"))
        lines, s = Game.run(ECHO + hello() + ' taps("%s") later(0.5) taps("%s") frame()'
                            % (bits[:20] + bits[21:], bitstr(codec.build(5, 3, b"after"))))
        self.assertNotIn("GOT hello", lines)
        self.assertIn("GOT after", lines)  # the next frame, after a pause, is fine
        self.assertEqual((s.bad, s.rx_seq), (1, 3))

    def test_an_extra_tap_is_a_bad_frame(self):
        bits = bitstr(codec.build(5, 2, b"hello"))
        lines, s = Game.run(ECHO + hello() + ' taps("%s") later(0.5) frame()' % (bits[:20] + "1" + bits[20:]))
        self.assertNotIn("GOT hello", lines)
        self.assertEqual(s.bad, 1)

    def test_a_resent_frame_is_delivered_once(self):
        bits = bitstr(codec.build(5, 2, b"once"))
        lines, s = Game.run(ECHO + hello() + ' taps("%s") taps("%s") frame()' % (bits, bits))
        self.assertEqual(lines.count("GOT once"), 1)
        self.assertEqual(s.rx_count, 3)

    def test_held_modifier_still_counts(self):
        bits = bitstr(codec.build(5, 2, b"m"))
        keys_lua = " ".join('key("SHIFT-%s")' % ("F9" if b == "0" else "F10") for b in bits)
        lines, _ = Game.run(ECHO + hello() + " " + keys_lua + " frame()")
        self.assertIn("GOT m", lines)

    def test_text_focus_is_not_ready(self):
        _, s = Game.run(ECHO + hello() + " focus(true) frame()")
        self.assertTrue(s.flags & strip.FLAG_FOCUS)
        self.assertFalse(s.flags & strip.FLAG_READY)

    def test_bound_after_combat(self):
        _, s = Game.run('combat(true) login() ' + 'taps("%s") combat(false) ' % bitstr(
            codec.build(0, 1, bytes([codec.OP_HELLO]))) + hello(2))
        self.assertIsNotNone(s)  # the first hello hit no binding; the one after combat opened it
        self.assertEqual(s.rx_seq, 2)

    def test_closes_after_silence_and_on_bye(self):
        _, s = Game.run(ECHO + hello() + " later(31) frame()")
        self.assertIsNone(s)
        _, s = Game.run(ECHO + hello() + ' taps("%s") frame()' % bitstr(codec.build(0, 2, bytes([codec.OP_BYE]))))
        self.assertIsNone(s)

    def test_handler_error_is_reported_not_fatal(self):
        code = ('DeckLink.On(6, function() error("boom") end) ' + ECHO + hello()
                + ' taps("%s") taps("%s") frame() print("ERRORS " .. errorsSeen())'
                % (bitstr(codec.build(6, 2, b"x")), bitstr(codec.build(5, 3, b"still"))))
        lines, _ = Game.run(code)
        self.assertIn("GOT still", lines)
        self.assertIn("ERRORS 1", lines)


class Out(unittest.TestCase):
    def test_send_shows_on_the_strip_until_acked(self):
        code = ECHO + hello() + ' print("SENT", DeckLink.Send(9, "from the game")) frame()'
        lines, s = Game.run(code)
        self.assertIn("SENT\ttrue", lines)
        self.assertEqual(codec.parse(s.out)[0::2], (9, b"from the game"))
        seq = codec.parse(s.out)[1]
        ack = bitstr(codec.build(0, 2, bytes([codec.OP_ACK, seq])))
        _, s = Game.run(code + ' DeckLink.Send(9, "second") frame() taps("%s") frame()' % ack)
        self.assertEqual(codec.parse(s.out)[2], b"second")

    def test_send_refused_while_closed_or_too_long(self):
        lines, _ = Game.run(ECHO + 'print("CLOSED", DeckLink.Send(9, "x"))')
        self.assertIn("CLOSED\tfalse", lines)
        lines, _ = Game.run(ECHO + hello() + ' print("MAX", DeckLink.MaxPayload())'
                            ' print("LONG", DeckLink.Send(9, string.rep("x", DeckLink.MaxPayload() + 1)))'
                            ' print("FITS", DeckLink.Send(9, string.rep("x", DeckLink.MaxPayload())))')
        self.assertIn("LONG\tfalse", lines)
        self.assertIn("FITS\ttrue", lines)
        biggest = int([l for l in lines if l.startswith("MAX")][0].split()[1])
        self.assertGreater(biggest, 500)

    def test_max_payload_known_before_the_strip_shows(self):
        lines, _ = Game.run(ECHO + 'print("MAX", DeckLink.MaxPayload())')
        self.assertGreater(int([l for l in lines if l.startswith("MAX")][0].split()[1]), 500)

    def test_biggest_frame_reads_back(self):
        _, s = Game.run(ECHO + hello() + ' local p = {} for i = 1, DeckLink.MaxPayload() do p[i] = string.char(i % 256) end'
                        ' DeckLink.Send(9, table.concat(p)) frame()')
        ch, _, payload = codec.parse(s.out)
        self.assertEqual(payload, bytes(i % 256 for i in range(1, len(payload) + 1)))

    def test_ping_is_echoed(self):
        ping = bytes([codec.OP_PING]) + b"abc"
        _, s = Game.run(ECHO + hello() + ' taps("%s") frame()' % bitstr(codec.build(0, 2, ping)))
        self.assertEqual(codec.parse(s.out)[0::2], (0, ping))


class Paste(unittest.TestCase):
    """Frames by paste: a PASTE control frame, then the line in DeckLink's hidden edit box."""

    def line(self, seq, channel, payload, crc=None):
        crc = codec.crc16(payload) if crc is None else crc
        return "DLP1 %d %d %d %04x %s" % (seq, channel, len(payload), crc, base64.b64encode(payload).decode())

    def test_base64_matches_python(self):
        data = bytes(random.Random(5).randrange(256) for _ in range(300))
        lines, _ = Game.run('local s = CODEC_FROM_B64("%s") local t = {} for i = 1, #s do t[i] = s:byte(i) end'
                            ' print("B", table.concat(t, ","))' % base64.b64encode(data).decode())
        self.assertIn("B\t" + ",".join(map(str, data)), lines)

    def test_pasted_frame_is_delivered_and_acked(self):
        payload = b"a long payload, " * 20
        paste = bitstr(codec.build(0, 2, bytes([codec.OP_PASTE])))
        code = (ECHO + hello() + ' taps("%s") frame() print("FOCUS", tostring(GetCurrentKeyBoardFocus() ~= nil))'
                ' DeckLinkPaste:SetText("%s") frame() print("FOCUS", tostring(GetCurrentKeyBoardFocus() ~= nil))'
                % (paste, self.line(9, 5, payload)))
        lines, s = Game.run(code)
        self.assertEqual([l for l in lines if l.startswith("FOCUS")], ["FOCUS\ttrue", "FOCUS\tfalse"])
        self.assertIn("GOT " + payload.decode(), lines)
        self.assertEqual(s.rx_seq, 9)

    def test_a_bad_paste_is_counted_not_delivered(self):
        paste = bitstr(codec.build(0, 2, bytes([codec.OP_PASTE])))
        lines, s = Game.run(ECHO + hello() + ' taps("%s") frame() DeckLinkPaste:SetText("%s") frame()'
                            % (paste, self.line(9, 5, b"x" * 50, crc=1)))
        self.assertFalse(any(l.startswith("GOT") for l in lines))
        self.assertEqual(s.bad, 1)
        self.assertNotEqual(s.rx_seq, 9)

    def test_a_frame_sent_again_runs_once_whichever_way_it_comes(self):
        # The paste ran, but its ack went unseen: the host sends another PASTE (a new seq, channel 0),
        # pastes the same line, then falls back to keys with the same seq. One delivery, each one acked.
        payload = b"x" * 60
        paste = lambda seq: 'taps("%s") frame() ' % bitstr(codec.build(0, seq, bytes([codec.OP_PASTE])))
        line = 'DeckLinkPaste:SetText("%s") frame() ' % self.line(9, 5, payload)
        code = (ECHO + hello() + paste(2) + line + paste(3) + line
                + 'taps("%s") frame() ' % bitstr(codec.build(5, 9, payload)))
        lines, s = Game.run(code)
        self.assertEqual(lines.count("GOT " + payload.decode()), 1)
        self.assertEqual(s.rx_seq, 9)

    def test_a_new_session_or_a_long_gap_forgets_the_seqs(self):
        frame = 'taps("%s") frame() ' % bitstr(codec.build(5, 9, b"again"))
        lines, _ = Game.run(ECHO + hello(1) + frame + hello(2) + frame + "later(31) " + frame)
        self.assertEqual(lines.count("GOT again"), 3)

    def test_the_box_gives_focus_back_if_nothing_comes(self):
        paste = bitstr(codec.build(0, 2, bytes([codec.OP_PASTE])))
        lines, _ = Game.run(ECHO + hello() + ' taps("%s") frame() later(4) frame()'
                            ' print("FOCUS", tostring(GetCurrentKeyBoardFocus() ~= nil))' % paste)
        self.assertIn("FOCUS\tfalse", lines)


class Telemetry(unittest.TestCase):
    READINGS = [("v", 2), ("bat", 87.0), ("st", "d"), ("ac", 0), ("bw", 9.43), ("apu", 7.118), ("cpu", 23.96),
                ("tapu", 66.0), ("fan", 3223.0)]

    def test_encode_is_17_bytes_at_fixed_places(self):
        data = telemetry.encode(self.READINGS)
        self.assertEqual(len(data), 17)
        mask = 1 << 0 | 1 << 1 | 1 << 2 | 1 << 3 | 1 << 4 | 1 << 5 | 1 << 8 | 1 << 11
        self.assertEqual(data, struct.pack(">BHBBhHBBBBBBH", 2, mask, 87, 0, 94, 71, 24, 0, 0, 66, 0, 0, 3223))

    def test_charging_and_plugged_in(self):
        data = telemetry.encode([("st", "c"), ("ac", 1), ("bw", -12.04)])
        self.assertEqual(data[4], 1 | 4)
        self.assertEqual(struct.unpack(">h", data[5:7])[0], -120)

    def test_arrives_without_hello_and_parses(self):
        frame = codec.build(telemetry.CHANNEL, 9, telemetry.encode(self.READINGS))
        lines, s = Game.run('login() taps("%s") later(2) frame() local t = DeckTelemetry.Get()'
                            ' print("T", t.bat, t.st, tostring(t.ac), t.bw, t.apu, t.fan, t.age, DeckTelemetry.Get("cpu"),'
                            ' tostring(t.gpu))' % bitstr(frame))
        self.assertIsNone(s)  # no strip: nothing was opened
        self.assertIn("T\t87\tdischarging\tfalse\t9.4\t7.1\t3223\t2\t24\tnil", lines)  # gpu wasn't sent

    def test_listeners_and_unknown_format(self):
        good = codec.build(1, 2, telemetry.encode([("bat", 50)]))
        future = codec.build(1, 3, bytes([3]) + telemetry.encode([("bat", 10)])[1:])
        short = codec.build(1, 4, b"v=1;bat=10")
        lines, _ = Game.run('login() local h = DeckTelemetry.OnUpdate(function(t) print("UPDATE", t.bat) end)'
                            ' taps("%s") taps("%s") taps("%s") print("BAT", DeckTelemetry.Get("bat"))'
                            % (bitstr(good), bitstr(future), bitstr(short)))
        self.assertEqual([l for l in lines if l.startswith(("UPDATE", "BAT"))], ["UPDATE\t50", "BAT\t50"])

    def test_nothing_before_the_first_update(self):
        lines, _ = Game.run('login() print("NONE", tostring(DeckTelemetry.Get()), tostring(DeckTelemetry.Age()))')
        self.assertIn("NONE\tnil\tnil", lines)


class Dictation(unittest.TestCase):
    def test_states_reach_deckkit(self):
        packets = [dictation.encode("recording"), dictation.encode("transcribing", "agent"), bytes([9, 0]),
                   b"done", dictation.encode("thinking", "agent"), dictation.encode("done", "agent")]
        taps = " ".join('taps("%s")' % bitstr(codec.build(2, i + 1, p)) for i, p in enumerate(packets))
        lines, s = Game.run("login() " + taps)
        self.assertEqual([l for l in lines if l.startswith("DICTATION")],
                         ["DICTATION claimed",  # at load: the grips never drive the indicator
                          "DICTATION recording chat", "DICTATION transcribing agent", "DICTATION thinking agent",
                          "DICTATION done agent"])
        self.assertIsNone(s)  # fire and forget: no strip

    def test_dictation_holds_the_keys_until_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = link.HOLD_FILE
            link.HOLD_FILE = os.path.join(tmp, "hold")
            try:
                sent = []
                sender = type("S", (), {"send": lambda self, bits: sent.append(bits)})()
                dictation.post("recording", sender=sender)
                self.assertTrue(link.held())
                dictation.post("transcribing", "agent", sender=sender)
                self.assertTrue(link.held())
                dictation.post("done", "agent", sender=sender)
                self.assertFalse(link.held())
                self.assertEqual(len(sent), 3)
            finally:
                link.HOLD_FILE = old

    def test_hook_maps_decktation_events(self):
        """Runs the real hook with a stub decklink: what it posts for each event, per binding."""
        with open(os.path.join(HERE, "..", "deck-setup", "decktation-hook.sh")) as f:
            text = f.read()
        with tempfile.TemporaryDirectory() as tmp:
            stub = os.path.join(tmp, "decklink")
            with open(stub, "w") as f:
                f.write('#!/bin/sh\necho "$@"\n')
            os.chmod(stub, 0o755)
            hook = os.path.join(tmp, "hook.sh")
            with open(hook, "w") as f:
                f.write(text.replace("/home/deck/.local/bin/decklink", stub).replace("runuser -u deck -- ", ""))

            def run(event, binding="chat"):
                out = subprocess.run(["sh", hook], capture_output=True, text=True,
                                     env=dict(os.environ, DECKTATION_EVENT=event, DECKTATION_BINDING=binding))
                words = out.stdout.split()  # e.g. --tool xdotool dictation recording --binding chat
                return (words[words.index("dictation") + 1], words[-1]) if "dictation" in words else None
            for event, state in (("record_start", "recording"), ("record_stop", "transcribing"),
                                 ("transcription_done", "done"), ("record_abort", "done"), ("error", "error")):
                self.assertEqual(run(event), (state, "chat"), event)
            self.assertIsNone(run("something_new"))
            # The agent binding: DeckAgent closes the box once it has answered, not the transcript.
            self.assertIsNone(run("transcription_done", "agent"))
            self.assertEqual(run("record_start", "agent"), ("recording", "agent"))
            self.assertEqual(run("record_abort", "agent"), ("done", "agent"))


class FakeClipboard:
    def __init__(self, data):
        self.data = data
        self.available = True

    def get(self):
        return self.data

    def set(self, data):
        self.data = data


class FakeGame:
    """The game, simulated for Link: keys go to a bit decoder, the strip is a Status."""

    def __init__(self, drop_first=0):
        self.rx_seq = self.rx_count = self.bad = 0
        self.seen = {}  # channel -> last seq delivered on it (Link.lua's seenSeq)
        self.lose_paste_acks = 0  # pastes delivered while the strip keeps showing the old seq
        self.open = False
        self.queue = []
        self.drop_first = drop_first
        self.received = []
        self.focus, self.pastes, self.clipboard, self.paste_broken = False, 0, None, False

    def paste(self):
        """Ctrl+V into DeckLink's box: the line on the clipboard, read as Link.lua's ReadPaste does."""
        if not self.focus or self.paste_broken:
            return
        self.focus = False
        _, seq, ch, length, crc, data = self.clipboard.data.decode().split(" ")
        payload = base64.b64decode(data)
        if len(payload) == int(length) and codec.crc16(payload) == int(crc, 16):
            self.pastes += 1
            self.deliver(int(ch), int(seq), payload)
            if self.lose_paste_acks:  # it ran, but the host never sees the ack
                self.lose_paste_acks -= 1
                self.rx_seq = -1

    def deliver(self, ch, seq, payload):
        """Link.lua's Deliver: every good frame is acked, but a seq runs once per channel."""
        self.rx_seq = seq
        if self.seen.get(ch) == seq:
            return
        self.seen[ch] = seq
        if ch == 0 and payload[0] == codec.OP_HELLO:
            self.open = True
            self.seen = {0: seq}
        elif ch == 0 and payload[0] == codec.OP_ACK:
            if self.queue and self.queue[0][1] == payload[1]:
                self.queue.pop(0)
        elif ch == 0 and payload[0] == codec.OP_PASTE:
            self.focus = True
        elif ch == 0 and payload[0] == codec.OP_PING:
            self.queue.append(codec.parse(codec.build(0, len(self.queue) + 50, payload)))
        else:
            self.received.append((ch, payload))

    def send(self, bits):
        data = bytes(int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8))
        if self.drop_first:
            self.drop_first -= 1
            self.bad += 1
            return
        ch, seq, payload = codec.parse(data)
        self.rx_count += 1
        self.deliver(ch, seq, payload)

    def read(self):
        if not self.open:
            raise strip.NoStrip("closed")
        if self.focus and self.paste_broken:  # Link.lua's box gives its focus back when nothing comes
            self.unfocus_in = getattr(self, "unfocus_in", 5) - 1
            if self.unfocus_in <= 0:
                self.focus, self.unfocus_in = False, 5
        out = codec.build(*self.queue[0]) if self.queue else None
        flags = strip.FLAG_OPEN | strip.FLAG_BOUND | (strip.FLAG_FOCUS if self.focus else strip.FLAG_READY)
        return strip.Status(flags, 1, self.rx_seq, self.rx_count, self.bad, out)


class Host(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, link, "OPEN_FILE", link.OPEN_FILE)
        link.OPEN_FILE = os.path.join(self.tmp.name, "open")

    def test_close_marks_the_link_closed_for_the_cover(self):
        lk = self.make(FakeGame()).open()
        link.mark_open(123.0)
        self.assertEqual(link.open_since(), 123.0)
        lk.close()
        self.assertEqual(link.open_since(), 0)

    def make(self, game):
        lk = link.Link(sender=game, reader=None, sleep=lambda s: None)
        lk.read = lambda: setattr(lk, "status", self._read(game)) or lk.status
        return lk

    @staticmethod
    def _read(game):
        try:
            return game.read()
        except strip.NoStrip:
            return None

    def test_open_send_recv(self):
        game = FakeGame()
        lk = self.make(game).open()
        lk.send(5, b"payload")
        self.assertEqual(game.received, [(5, b"payload")])
        game.queue.append((9, 77, b"up"))
        self.assertEqual(lk.recv(1), (9, b"up"))
        self.assertEqual(game.queue, [])

    def test_resends_a_bad_frame(self):
        game = FakeGame()
        lk = self.make(game).open()
        game.drop_first = 1
        lk.send(5, b"again")
        self.assertEqual(game.received, [(5, b"again")])

    def test_ping(self):
        game = FakeGame()
        lk = self.make(game).open()
        self.assertGreaterEqual(lk.ping(b"xyz"), 0)

    def test_big_frames_go_by_paste_and_the_clipboard_is_given_back(self):
        game = FakeGame()
        clip = FakeClipboard(b"Ben's own clipboard")
        game.clipboard = clip
        lk = link.Link(sender=game, reader=None, sleep=lambda s: None, clipboard=clip)
        lk.read = lambda: setattr(lk, "status", self._read(game)) or lk.status
        lk.open()
        big = bytes(range(200))
        lk.send(5, big)
        lk.send(5, b"small")
        self.assertEqual(game.received, [(5, big), (5, b"small")])
        self.assertEqual(game.pastes, 1)  # only the big one
        self.assertEqual(clip.data, b"Ben's own clipboard")

    def test_paste_falls_back_to_keys(self):
        game = FakeGame()
        game.clipboard = FakeClipboard(None)
        game.paste_broken = True
        tick = iter(range(1000000))
        lk = link.Link(sender=game, reader=None, sleep=lambda s: None, clipboard=game.clipboard,
                       clock=lambda: next(tick) * 0.05)
        lk.read = lambda: setattr(lk, "status", self._read(game)) or lk.status
        lk.open()
        lk.send(5, bytes(100))
        self.assertEqual(game.received, [(5, bytes(100))])

    def paster(self, game):
        game.clipboard = FakeClipboard(None)
        tick = iter(range(1000000))
        lk = link.Link(sender=game, reader=None, sleep=lambda s: None, clipboard=game.clipboard,
                       clock=lambda: next(tick) * 0.05)
        lk.read = lambda: setattr(lk, "status", self._read(game)) or lk.status
        return lk.open()

    def test_a_paste_whose_ack_was_missed_runs_once(self):
        game = FakeGame()
        game.lose_paste_acks = 1
        lk = self.paster(game)
        seq = lk.send(5, bytes(100))
        self.assertEqual(game.received, [(5, bytes(100))])
        self.assertEqual((game.pastes, game.rx_seq), (2, seq))  # pasted again, acked, not run again

    def test_the_key_fallback_keeps_the_frame_seq(self):
        game = FakeGame()
        game.lose_paste_acks = 2  # both paste tries ran unseen: the frame then goes by keys
        lk = self.paster(game)
        seq = lk.send(5, bytes(100))
        self.assertEqual(game.received, [(5, bytes(100))])
        self.assertEqual(game.rx_seq, seq)

    def test_frames_too_big_to_finish_by_keys_are_refused(self):
        # By keys, KEYS_MAX bytes take ~9 s, so three tries fit the game's 30 s windows; one byte more
        # is refused without a tap (when its paste failed, it isn't sent at all).
        game = FakeGame()
        lk = self.make(game).open()
        lk.send_by_keys(5, bytes(link.KEYS_MAX))
        self.assertEqual(game.received, [(5, bytes(link.KEYS_MAX))])
        taps = game.rx_count
        with self.assertRaises(link.LinkError):
            lk.send_by_keys(5, bytes(link.KEYS_MAX + 1))
        self.assertEqual(game.rx_count, taps)
        self.assertLess((link.KEYS_MAX + codec.OVERHEAD) * 8 * 3 / 450, 30)  # bits at ~450 a second, 3 tries
        broken = FakeGame()
        broken.paste_broken = True
        with self.assertRaises(link.LinkError):
            self.paster(broken).send(5, bytes(link.KEYS_MAX + 1))
        self.assertEqual(broken.received, [])

    def test_key_commands(self):
        k = keys.Keys.__new__(keys.Keys)
        k.tool, k.gap_ms = "ydotool", 1
        (cmd,) = k.commands([0, 1])
        self.assertEqual(cmd[2:4], ["-d", "1"])
        self.assertEqual(cmd[-4:], ["67:1", "67:0", "68:1", "68:0"])
        k.tool = "xdotool"
        (cmd,) = k.commands([0, 1])
        self.assertEqual(cmd[-2:], ["F9", "F10"])
        self.assertEqual(len(k.commands([0] * (keys.CHUNK + 1))), 2)


if __name__ == "__main__":
    unittest.main()
