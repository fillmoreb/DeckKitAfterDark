"""Sends bits into the game as key taps: F9 is a 0, F10 a 1 (addons/DeckLink binds them).

Each bit is a whole tap, down then up, and never two keys down at once: with two held, the game loses
the first key's release (AddonTools lab/RESULTS.md E6). The game queues every tap, however fast, but
handling key input costs it about 0.17 ms per press or release: a burst all lands in one frame and
freezes the game (1,000 taps froze it 336 ms; a 1 KB frame, ~2.7 s). So taps are paced: GAP_MS between
presses and releases spreads them over frames with no visible cost (2,000 taps in 4.4 s, worst frame
25 ms against a 23 ms baseline): about 450 bits/s, 55 bytes/s. gap_ms=0 is a burst, for when a freeze
doesn't matter.

Two ways in:
  ydotool  Decktation's daemon (a uinput keyboard). Its socket is root's, so this needs root.
  xdotool  XTEST on WoW's Xwayland display :1, as the desktop user (deck).
"""
import fcntl, os, shutil, subprocess

KEYS = ("F9", "F10")  # bit 0, bit 1
KEYCODES = (67, 68)  # KEY_F9, KEY_F10 in linux/input-event-codes.h
YDOTOOL = "/home/deck/homebrew/plugins/decktation/bin/ydotool"
YDOTOOL_SOCKET = "/tmp/decktation-ydotool.sock"
CHUNK = 2000  # taps per command, to keep argument lists sane
# Two senders tapping at once mix their bits into garbage, so a frame's taps hold this lock (shared by
# root and deck, hence world-writable).
LOCK = "/tmp/decklink-keys.lock"
GAP_MS = 1


class SendError(Exception):
    pass


def default_tool():
    """ydotool when its socket is usable by this user, else xdotool."""
    if os.path.exists(YDOTOOL) and os.access(YDOTOOL_SOCKET, os.W_OK):
        return "ydotool"
    return "xdotool"


class Keys:
    def __init__(self, tool=None, display=":1", gap_ms=GAP_MS):
        self.tool = tool or default_tool()
        self.gap_ms = gap_ms
        self.display = display
        if self.tool == "xdotool" and not shutil.which("xdotool"):
            raise SendError("no xdotool")

    def commands(self, bits):
        """The command lines that tap out `bits` (a list of 0/1)."""
        out = []
        for start in range(0, len(bits), CHUNK):
            chunk = bits[start:start + CHUNK]
            if self.tool == "ydotool":
                cmd = [YDOTOOL, "key", "-d", str(int(self.gap_ms))]
                for b in chunk:
                    cmd += ["%d:1" % KEYCODES[b], "%d:0" % KEYCODES[b]]
            else:
                # xdotool's delay is per tap (down and up), so twice the gap
                cmd = ["xdotool", "key", "--delay", str(int(2 * self.gap_ms))] + [KEYS[b] for b in chunk]
            out.append(cmd)
        return out

    def send(self, bits):
        env = dict(os.environ)
        if self.tool == "ydotool":
            env["YDOTOOL_SOCKET"] = YDOTOOL_SOCKET
        else:
            env["DISPLAY"] = self.display
        try:
            fd = os.open(LOCK, os.O_RDWR)  # an existing one: O_CREAT on another user's file in /tmp is refused
        except FileNotFoundError:
            fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o666)
        try:
            try:
                os.fchmod(fd, 0o666)
            except OSError:
                pass  # someone else's file; it was made writable when it was created
            fcntl.flock(fd, fcntl.LOCK_EX)
            for cmd in self.commands(list(bits)):
                r = subprocess.run(cmd, env=env, capture_output=True, timeout=30)
                if r.returncode != 0:
                    raise SendError("%s failed: %s" % (self.tool, r.stderr.decode(errors="replace").strip()))
        finally:
            os.close(fd)
