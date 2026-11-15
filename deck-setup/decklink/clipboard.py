"""The game's clipboard (Xwayland :1, where WoW runs), for DeckLink's frames by paste (link.py).

xclip, as the desktop user: ~/.local/bin/xclip, else on the PATH, else the copy Decktation ships.
"""
import os, shutil, subprocess

CANDIDATES = [os.path.expanduser("~/.local/bin/xclip"), shutil.which("xclip") or "",
              "/home/deck/homebrew/plugins/decktation/bin/xclip"]


def find_xclip():
    return next((p for p in CANDIDATES if p and os.access(p, os.X_OK)), None)


class Clipboard:
    def __init__(self, display=":1", xclip=None):
        self.display = display
        self.xclip = xclip or find_xclip()

    @property
    def available(self):
        return self.xclip is not None

    def _env(self):
        return dict(os.environ, DISPLAY=self.display)

    def get(self):
        """What's on the clipboard now (text), or None."""
        try:
            r = subprocess.run([self.xclip, "-selection", "clipboard", "-o"], env=self._env(),
                               capture_output=True, timeout=1)
            return r.stdout if r.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    def set(self, data):
        """xclip forks and keeps serving the selection until something else takes it."""
        subprocess.run([self.xclip, "-selection", "clipboard", "-i"], input=data, env=self._env(),
                       check=True, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
