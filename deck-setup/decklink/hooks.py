"""DeckAgent's hooks, the Deck's half: what the assistant wants done when something happens.

The game keeps only a hook's condition, as numbers (addons/DeckAgent/Hooks.lua), and gives back its id.
Here, keyed by that id, is the rest: the assistant's note to itself, what to do when it fires ("say":
speak the note as a reminder; "wake": wake the assistant with the event and its note), and a short
description for the chat ("Stepping Stones turned in"). Timers ("in 20 minutes") need no game at all:
they live only here. All of it is in ~/.deckagent/hooks.json, so it survives restarts.

The link keeper (agentd.py) runs it: while any game hook is set it glances at the strip about once a
second for DeckLink's attention flag, collects the events, and hands each fired hook to the voice.
Its threads (requests, the glance) share one Hooks: a lock inside covers the state and every save, and
each save writes its own temporary file, so two can't tear hooks.json or trip over each other.
"""
import json, os, tempfile, threading, time

HOME = os.path.expanduser(os.environ.get("DECKAGENT_HOME", "~/.deckagent"))
PATH = os.path.join(HOME, "hooks.json")
DEFAULT_MINUTES = 240
ACTIONS = ("say", "wake")


class Hooks:
    def __init__(self, path=PATH, clock=time.time):
        self.path, self.clock = path, clock
        self.lock = threading.RLock()
        self.data = {"game": {}, "timers": {}, "next_timer": 1}
        try:
            with open(path) as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass

    def save(self):
        with self.lock:
            folder = os.path.dirname(self.path)
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".hooks.", suffix=".tmp", dir=folder)
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(self.data, f, indent=1)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise

    @staticmethod
    def _entry(note, action, desc, expires):
        if action not in ACTIONS:
            raise ValueError("action is say or wake")
        return {"note": note or "", "action": action, "desc": desc or "", "expires": expires}

    def add_game(self, hid, note, action, desc, minutes=0):
        with self.lock:
            now = self.clock()
            e = self._entry(note, action, desc, now + (minutes or DEFAULT_MINUTES) * 60)
            e["created"] = now
            self.data["game"][str(hid)] = e
            self.save()
            return e

    def add_timer(self, minutes, note, action, desc=""):
        with self.lock:
            n = self.data.get("next_timer", 1)
            self.data["next_timer"] = n + 1
            tid = "t%d" % n
            now = self.clock()
            e = self._entry(note, action, desc or "%g minutes passed" % minutes, now + minutes * 60)
            e["created"], e["fire_at"] = now, now + minutes * 60
            self.data["timers"][tid] = e
            self.save()
            return tid

    def any_game(self):
        with self.lock:
            return bool(self.data["game"])

    def pop_game(self, hid):
        with self.lock:
            e = self.data["game"].pop(str(hid), None)
            if e is not None:
                self.save()
            return e

    def keep_game(self, ids):
        """Drops the notes of game hooks not in `ids` (the game's own list): they're gone there."""
        with self.lock:
            keep = {str(i) for i in ids}
            gone = [hid for hid in self.data["game"] if hid not in keep]
            for hid in gone:
                del self.data["game"][hid]
            if gone:
                self.save()
            return gone

    def due_timers(self):
        """Timers whose time has come, removed: [(id, entry)]."""
        with self.lock:
            now = self.clock()
            due = [(tid, e) for tid, e in self.data["timers"].items() if e["fire_at"] <= now]
            for tid, _ in due:
                del self.data["timers"][tid]
            if due:
                self.save()
            return due

    def expire(self):
        """Drops game hooks past their expiry (the game drops its side at the same time)."""
        with self.lock:
            now = self.clock()
            gone = [hid for hid, e in self.data["game"].items() if e["expires"] <= now]
            for hid in gone:
                del self.data["game"][hid]
            if gone:
                self.save()
            return gone

    def remove(self, hid):
        hid = str(hid)
        with self.lock:
            for kind in ("game", "timers"):
                if hid in self.data[kind]:
                    del self.data[kind][hid]
                    self.save()
                    return kind
            return None

    def clear(self):
        with self.lock:
            self.data["game"], self.data["timers"] = {}, {}
            self.save()

    def listing(self, game_hooks=None):
        """Every hook: the game's (id, track, minutes left; with this side's note) and the timers."""
        with self.lock:
            now = self.clock()
            out = []
            for g in game_hooks if game_hooks is not None else [{"id": int(h)} for h in self.data["game"]]:
                e = self.data["game"].get(str(g["id"]), {})
                out.append(dict(g, kind="game", note=e.get("note"), action=e.get("action"), desc=e.get("desc")))
            for tid, e in sorted(self.data["timers"].items()):
                out.append({"id": tid, "kind": "timer", "note": e["note"], "action": e["action"], "desc": e["desc"],
                            "minutes_left": max(0, round((e["fire_at"] - now) / 60, 1))})
            return out


def wake_prompt(desc, note, seconds_ago=0):
    """What the assistant is woken with when a "wake" hook fires."""
    when = " (%d s ago)" % seconds_ago if seconds_ago and seconds_ago > 5 else ""
    return ("[A hook you set fired: %s%s. Your note to yourself: %s]\n"
            "Do what the note says for Ben now, then respond briefly." % (desc or "something happened", when,
                                                                         note or "(none)"))
