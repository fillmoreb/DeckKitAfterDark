"""The game side of DeckAgent, asked from the Deck: one binary request on channel 3, one reply.
addons/DeckAgent/Agent.lua answers and documents the packet layout; keep the two in step
(test/test_agent.py checks the bytes both ways). agent/mcp_server.py is the usual caller.

    decklink agent state [FIELD ...]     where you are; FIELDs: pos world zone who combat brain voice
                                         (default: all)
    decklink agent way MAP X Y           a waypoint (X, Y 0-100, like /way), made active
    decklink agent waypoints [remove ID | remove-active | remove-all]   list (or remove) waypoints
    decklink agent hook add JSON | list | remove ID | clear   the assistant's hooks (through the link
                                         keeper only: it keeps their notes; see hooks.py)
    decklink agent checklist JSON | clear   put up a self-ticking checklist ({"title", "items": [{"label",
                                         "track", its fields, "done"}]}; see CHECK_KINDS), or take it down
    decklink agent chat CHANNEL TARGET TEXT   send chat for you (CHANNEL: say yell party raid guild
                                         whisper bnet instance; TARGET "" unless whisper/bnet)
    decklink agent friends [LIST ...]    who's online; LISTs: bnet friends guild (default: all)
    decklink agent prompt [--list] [--seconds N] QUESTION OPTION OPTION [...]   ask Ben with a prompt in
                                         the game and wait for his answer (2 options: a dialog)
    decklink agent show KIND [--seconds N] [--item ID] TITLE TEXT   show something: KIND echo (TITLE
                                         the question, TEXT the answer, into chat), card, or reading
    decklink agent quests                the quest log: IDs, complete/failed, objective counts (no text)
    decklink agent quest-status ID ...   each quest's status: not taken, in the log, ready, done

Prints the reply as JSON and exits 1 when the game says no.
"""
import json, struct, time

from . import codec

CHANNEL = 3
VERSION = 1
OP_STATE, OP_WAY, OP_CHAT, OP_FRIENDS, OP_ASK, OP_ANSWER, OP_SHOW, OP_QUESTS, OP_QUEST_STATUS, OP_WAYPOINTS, OP_CHECKLIST, OP_HOOKS, OP_EVENTS = (
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13)
ASK_RESULT = {1: "shown", 2: "queued"}
NO_ANSWER = 255
KEEPALIVE_S = 10  # the game closes the link after 30 s without a frame
CHANNELS = ["say", "yell", "party", "raid", "guild", "whisper", "bnet", "instance"]  # CHAT byte 1 = index + 1
CHAT_RESULT = {1: "sent", 2: "previewed", 3: "previewed after combat"}
LISTS = ["bnet", "friends", "guild"]  # FRIENDS byte 1 bits
FIELDS = ["pos", "world", "zone", "who", "combat", "brain", "voice"]  # mask bit 0, 1, 2, ...
BRAIN = {1: "haiku", 2: "sonnet", 3: "opus"}  # 0: the default
SHOW_KINDS = ["echo", "card", "reading"]  # SHOW byte 1 = index + 1
SHOW_RESULT = {1: "shown", 2: "queued"}
QUEST_STATUS = {0: "not taken", 1: "in log", 2: "ready to turn in", 3: "done"}
GOAL_KINDS = {1: "monster", 2: "item", 3: "object", 4: "event", 5: "reputation", 6: "other"}
# Longest text per kind, in UTF-8 bytes. A reading view is one frame (codec.MAX_IN) whatever the title.
SHOW_TEXT = {"echo": 1000, "card": 600, "reading": codec.MAX_IN - 9 - 255}
STATUS = {0: "ok", 1: "unknown op", 2: "bad arguments", 3: "refused", 4: "failed in the game", 5: "unknown version"}
VIA = {1: "waypad", 2: "mappin"}
FACTION = {0: None, 1: "A", 2: "H"}
CLASS = {1: "WARRIOR", 2: "PALADIN", 3: "HUNTER", 4: "ROGUE", 5: "PRIEST", 6: "DEATHKNIGHT", 7: "SHAMAN",
         8: "MAGE", 9: "WARLOCK", 10: "MONK", 11: "DRUID"}
REPLY_TIMEOUT = 8  # seconds for the game's answer once the request is in


class Malformed(ValueError):
    pass


def op_byte(op):
    return VERSION << 6 | op


def build_state(fields=FIELDS):
    mask = 0
    for f in fields:
        mask |= 1 << FIELDS.index(f)
    return bytes([op_byte(OP_STATE), mask])


def fraction(v):
    """0-100 (like /way) -> uint16 0-65535."""
    v = float(v)
    if not 0 <= v <= 100:
        raise ValueError("X and Y are 0-100")
    return round(v / 100 * 65535)


def build_way(map_id, x, y):
    return struct.pack(">BHHH", op_byte(OP_WAY), int(map_id), fraction(x), fraction(y))


def _pstr(text, limit=255):
    data = text.encode("utf-8")
    if len(data) > limit:
        raise ValueError("over %d bytes" % limit)
    return bytes([len(data)]) + data


def build_chat(channel, target, text):
    if channel not in CHANNELS:
        raise ValueError("unknown channel %r (channels: %s)" % (channel, " ".join(CHANNELS)))
    if not text.strip():
        raise ValueError("nothing to send")
    try:
        body = _pstr(target or "") + _pstr(text)
    except ValueError as e:
        raise ValueError("text " + str(e) + " (chat's limit)")
    return bytes([op_byte(OP_CHAT), CHANNELS.index(channel) + 1]) + body


def build_friends(lists=LISTS):
    mask = 0
    for name in lists:
        if name not in LISTS:
            raise ValueError("unknown list %r (lists: %s)" % (name, " ".join(LISTS)))
        mask |= 1 << LISTS.index(name)
    return bytes([op_byte(OP_FRIENDS), mask])


def build_ask(question, options, as_list=False, seconds=30):
    if not 2 <= len(options) <= 6:
        raise ValueError("2-6 options")
    try:
        body = _pstr(question, 160) + bytes([len(options)]) + b"".join(_pstr(o, 40) for o in options)
    except ValueError as e:
        raise ValueError("question (160 bytes) or an option (40 bytes) is too long: " + str(e))
    if any(not o.strip() for o in options):
        raise ValueError("empty option")
    return bytes([op_byte(OP_ASK), 1 if as_list else 0, max(5, min(120, int(seconds)))]) + body


def build_show(kind, title, text, seconds=0, item=0):
    """SHOW: kind echo/card/reading, a title (echo: the question) and the text (echo: the answer). An
    echo too long is cut (it's a copy of what was said); a card or reading view too long is refused."""
    if kind not in SHOW_KINDS:
        raise ValueError("kind is one of: " + " ".join(SHOW_KINDS))
    t = (title or "").encode("utf-8")
    if len(t) > 120:
        raise ValueError("title over 120 bytes")
    body = (text or "").encode("utf-8")
    limit = SHOW_TEXT[kind]
    if len(body) > limit:
        if kind != "echo":
            raise ValueError("%s text over %d bytes (%d)" % (kind, limit, len(body)))
        body = body[:limit - 3].decode("utf-8", "ignore").encode("utf-8") + "…".encode("utf-8")
    if kind != "echo" and not body.strip() and not t.strip():
        raise ValueError("nothing to show")
    seconds = max(0, min(255, int(seconds)))
    return (bytes([op_byte(OP_SHOW), SHOW_KINDS.index(kind) + 1, seconds]) + struct.pack(">I", int(item))
            + bytes([len(t)]) + t + struct.pack(">H", len(body)) + body)


# CHECKLIST item kinds: name -> (kind byte, the item fields packed after it, struct format).
CHECK_KINDS = {
    "manual": (0, (), ""), "quest_taken": (1, ("quest_id",), "I"), "quest_ready": (2, ("quest_id",), "I"),
    "quest_turned_in": (3, ("quest_id",), "I"), "objective": (4, ("quest_id", "objective"), "IB"),
    "item_count": (5, ("item_id", "count"), "IH"), "level": (6, ("level",), "B"), "zone": (7, ("map_id",), "H"),
    "xp": (8, ("xp",), "I"),
}


def _condition(it, n):
    """A condition dict ({track, its fields, "not": true to turn it around}) -> its bytes (Conditions.lua:
    the kind byte + 128 for NOT)."""
    track = it.get("track") or "manual"
    if track not in CHECK_KINDS:
        raise ValueError("item %d: track is one of %s" % (n, " ".join(CHECK_KINDS)))
    kind, fields, fmt = CHECK_KINDS[track]
    missing = [f for f in fields if it.get(f) is None]
    if missing:
        raise ValueError("item %d (%s) needs %s" % (n, track, ", ".join(missing)))
    if it.get("not") and track == "manual":
        raise ValueError("item %d: manual can't be turned around" % n)
    return bytes([kind | (0x80 if it.get("not") else 0)]) + (
        struct.pack(">" + fmt, *[int(it[f]) for f in fields]) if fmt else b"")


KIND_NAMES = {v[0]: k for k, v in CHECK_KINDS.items()}
HOOK_ACTIONS = {"add": 1, "remove": 2, "remove-all": 3, "list": 4}


def build_hooks(action, when=None, minutes=0, hid=0):
    """HOOKS: add (when: a condition dict, not manual; minutes until it expires, 0 = 240), remove (hid),
    remove-all, list."""
    if action not in HOOK_ACTIONS:
        raise ValueError("action is one of: " + " ".join(HOOK_ACTIONS))
    out = bytes([op_byte(OP_HOOKS), HOOK_ACTIONS[action]])
    if action == "add":
        if (when or {}).get("track", "manual") == "manual":
            raise ValueError("a hook needs a condition the game can see (not manual)")
        out += _condition(when, 1) + struct.pack(">H", max(0, min(65535, int(minutes))))
    elif action == "remove":
        out += struct.pack(">H", int(hid))
    return out


def build_events():
    return bytes([op_byte(OP_EVENTS)])


def build_checklist(title=None, items=None, clear=False):
    """CHECKLIST: set (title + items: {label, track: a CHECK_KINDS name, its fields, done}) or clear."""
    if clear:
        return bytes([op_byte(OP_CHECKLIST), 2])
    items = items or []
    if not 1 <= len(items) <= 12:
        raise ValueError("a checklist has 1-12 items")
    out = bytes([op_byte(OP_CHECKLIST), 1]) + _pstr(title or "", 60) + bytes([len(items)])
    for n, it in enumerate(items, 1):
        cond = _condition(it, n)
        if not (it.get("label") or "").strip():
            raise ValueError("item %d needs a label" % n)
        out += cond + bytes([1 if it.get("done") else 0]) + _pstr(it["label"].strip(), 80)
    return out


WAYPOINT_ACTIONS = {"list": 1, "remove": 2, "remove-active": 3, "remove-all": 4}


def build_waypoints(action="list", wid=0):
    if action not in WAYPOINT_ACTIONS:
        raise ValueError("action is one of: " + " ".join(WAYPOINT_ACTIONS))
    return bytes([op_byte(OP_WAYPOINTS), WAYPOINT_ACTIONS[action]]) + (struct.pack(">H", int(wid)) if action == "remove" else b"")


def build_quests():
    return bytes([op_byte(OP_QUESTS)])


def build_quest_status(ids):
    ids = [int(i) for i in ids]
    if not 1 <= len(ids) <= 255:
        raise ValueError("1-255 quest IDs")
    return bytes([op_byte(OP_QUEST_STATUS), len(ids)]) + b"".join(struct.pack(">I", i) for i in ids)


def _string(p, i):
    if i >= len(p):
        raise Malformed("string past the end")
    n = p[i]
    if i + 1 + n > len(p):
        raise Malformed("string longer than the packet")
    return p[i + 1:i + 1 + n].decode("utf-8", "replace"), i + 1 + n


def _unpack(fmt, p, i):
    size = struct.calcsize(fmt)
    if i + size > len(p):
        raise Malformed("packet too short")
    return struct.unpack_from(fmt, p, i), i + size


def parse_reply(p, asked=FIELDS):
    """Reply bytes -> dict: op, seq, ok, status, then the op's fields (or the reason it failed).
    For STATE, `asked` is the request's fields: each is in the packet, in mask order, but only the ones
    the game could give (set in the echoed mask) are returned."""
    if len(p) < 3:
        raise Malformed("reply too short")
    out = {"op": p[0] & 0x3F, "seq": p[1], "ok": p[2] == 0, "status": STATUS.get(p[2], "status %d" % p[2])}
    i = 3
    if p[2] != 0:
        if i < len(p):
            out["reason"], i = _string(p, i)
        return out
    if out["op"] == OP_STATE:
        (sent,), i = _unpack(">B", p, i)
        given = {f for b, f in enumerate(FIELDS) if sent >> b & 1}
        out["fields"] = [f for f in FIELDS if f in given and f in asked]  # what the game could give
        for f in FIELDS:
            if f not in asked:
                continue
            if f == "pos":
                (m, x, y), i = _unpack(">HHH", p, i)
                v = {"map": m, "x": round(x / 65535 * 100, 2), "y": round(y / 65535 * 100, 2)}
            elif f == "world":
                (c, wx, wy), i = _unpack(">Bhh", p, i)
                v = {"cont": c, "wx": wx, "wy": wy}
            elif f == "zone":
                name, i = _string(p, i)
                v = {"zone": name}
            elif f == "who":
                (fac, lvl, cls), i = _unpack(">BBB", p, i)
                v = {"faction": FACTION.get(fac), "level": lvl, "class": CLASS.get(cls, cls)}
            elif f == "combat":
                (c,), i = _unpack(">B", p, i)
                v = {"in_combat": c == 1}
            elif f == "brain":
                (b,), i = _unpack(">B", p, i)
                v = {"model": BRAIN.get(b)}
            else:  # voice
                (q,), i = _unpack(">B", p, i)
                v = {"quiet": q == 1}
            if f in given:
                out.update(v)
    elif out["op"] == OP_WAY:
        (via, wid), i = _unpack(">BH", p, i)
        out["via"], out["waypoint"] = VIA.get(via, via), wid
    elif out["op"] == OP_CHAT:
        (result,), i = _unpack(">B", p, i)
        out["result"] = CHAT_RESULT.get(result, result)
    elif out["op"] == OP_ASK:
        (result,), i = _unpack(">B", p, i)
        out["result"] = ASK_RESULT.get(result, result)
    elif out["op"] == OP_SHOW:
        (result, quiet), i = _unpack(">BB", p, i)
        out["result"], out["quiet"] = SHOW_RESULT.get(result, result), quiet == 1
    elif out["op"] == OP_QUESTS:
        (flags, count), i = _unpack(">BB", p, i)
        quests = []
        for _ in range(count):
            (qid, qflags, n), i = _unpack(">IBB", p, i)
            goals = []
            for _ in range(n):
                (kind, have, need), i = _unpack(">BBB", p, i)
                goals.append({"kind": GOAL_KINDS.get(kind & 0x7F, "other"), "done": bool(kind & 0x80),
                              "have": have, "need": need})
            quests.append({"id": qid, "complete": bool(qflags & 1), "failed": bool(qflags & 2), "objectives": goals})
        out["quests"], out["more"] = quests, bool(flags & 1)
    elif out["op"] == OP_HOOKS:
        (action,), i = _unpack(">B", p, i)
        if action == 1:  # add: id, already true
            (hid, already), i = _unpack(">HB", p, i)
            out["id"], out["already"] = hid, already == 1
        elif action in (2, 3):  # remove / remove-all
            (out["removed"],), i = _unpack(">B", p, i)
        else:  # list
            (n,), i = _unpack(">B", p, i)
            hooks = []
            for _ in range(n):
                (hid, kind, mins), i = _unpack(">HBH", p, i)
                hooks.append({"id": hid, "track": KIND_NAMES.get(kind & 0x7F, kind), "not": bool(kind & 0x80),
                              "minutes_left": mins})
            out["hooks"] = hooks
    elif out["op"] == OP_EVENTS:
        (n,), i = _unpack(">B", p, i)
        events = []
        for _ in range(n):
            (hid, kind, ago), i = _unpack(">HBH", p, i)
            events.append({"id": hid, "track": KIND_NAMES.get(kind & 0x7F, kind), "not": bool(kind & 0x80),
                           "seconds_ago": ago})
        out["events"] = events
    elif out["op"] == OP_CHECKLIST:
        (n,), i = _unpack(">B", p, i)
        out["done"] = [b == 1 for b in _unpack(">%dB" % n, p, i)[0]]
    elif out["op"] == OP_WAYPOINTS:
        (removed, n, active), i = _unpack(">BBH", p, i)
        points = []
        for _ in range(n):
            (wid, m, x, y), i = _unpack(">HHHH", p, i)
            points.append({"id": wid, "map": m, "x": round(x / 65535 * 100, 2), "y": round(y / 65535 * 100, 2),
                           "active": wid == active and active != 0})
        out["removed"], out["waypoints"] = removed, points
    elif out["op"] == OP_QUEST_STATUS:
        (n,), i = _unpack(">B", p, i)
        out["status"] = [QUEST_STATUS.get(b, b) for b in _unpack(">%dB" % n, p, i)[0]]
    elif out["op"] == OP_ANSWER:
        (choice,), i = _unpack(">B", p, i)
        out["choice"] = None if choice == NO_ANSWER else choice
    elif out["op"] == OP_FRIENDS:
        (flags, count), i = _unpack(">BB", p, i)
        people = []
        for _ in range(count):
            (kind,), i = _unpack(">B", p, i)
            if kind == 1:
                (aid,), i = _unpack(">I", p, i)
                tag, i = _string(p, i)
                (in_wow,), i = _unpack(">B", p, i)
                person = {"list": "bnet", "account_id": aid, "battletag": tag, "in_wow": in_wow == 1}
                if in_wow:
                    person["character"], i = _string(p, i)
                    person["realm"], i = _string(p, i)
                    (person["level"],), i = _unpack(">B", p, i)
                    person["class"], i = _string(p, i)
                    person["zone"], i = _string(p, i)
            elif kind in (2, 3):
                name, i = _string(p, i)
                (level,), i = _unpack(">B", p, i)
                cls, i = _string(p, i)
                zone, i = _string(p, i)
                person = {"list": "friends" if kind == 2 else "guild", "name": name, "level": level,
                          "class": cls, "zone": zone}
            else:
                raise Malformed("unknown entry kind %d" % kind)
            people.append(person)
        out["people"], out["more"] = people, bool(flags & 1)
    return out


def request(lk, packet, timeout=REPLY_TIMEOUT):
    """Sends one request on an open Link and returns the raw reply payload (LinkError on none)."""
    from .link import LinkError
    seq = lk.send(CHANNEL, packet)
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise LinkError("no reply from the game (is DeckAgent installed?)")
        got = lk.recv(left)
        if got is None:
            continue
        channel, payload = got
        if channel == CHANNEL and len(payload) >= 2 and payload[0] == packet[0] and payload[1] == seq:
            return payload


def ask(lk, question, options, as_list=False, seconds=30, clock=time.monotonic):
    """Shows a prompt and waits for Ben's answer: {"answer": option text or None, "choice": 1-6, 0 for
    cancelled, None for no answer}. Keeps the link alive while it waits."""
    from .link import LinkError
    packet = build_ask(question, options, as_list, seconds)
    seq = lk.send(CHANNEL, packet)
    shown = None
    deadline = clock() + int(packet[2]) + REPLY_TIMEOUT + 120  # a queued prompt (combat) can wait longer
    keepalive = clock() + KEEPALIVE_S
    while True:
        now = clock()
        if now >= deadline:
            raise LinkError("no answer from the game")
        if now >= keepalive:
            lk.send(0, bytes([codec.OP_PING]))  # any frame keeps the game's end open
            keepalive = now + KEEPALIVE_S
        got = lk.recv(min(1.0, deadline - now))
        if got is None:
            continue
        channel, payload = got
        if channel != CHANNEL or len(payload) < 3 or payload[1] != seq:
            continue
        reply = parse_reply(payload)
        if reply["op"] == OP_ASK:
            if not reply["ok"]:
                return reply
            shown = reply["result"]
        elif reply["op"] == OP_ANSWER:
            choice = reply["choice"]
            answer = options[choice - 1] if choice and choice <= len(options) else None
            return {"ok": True, "shown": shown, "choice": choice, "answer": answer}


def state(lk, fields=FIELDS):
    return parse_reply(request(lk, build_state(fields)), fields)


def way(lk, map_id, x, y):
    return parse_reply(request(lk, build_way(map_id, x, y)))


def chat(lk, channel, target, text):
    return parse_reply(request(lk, build_chat(channel, target, text)))


def friends(lk, lists=LISTS):
    return parse_reply(request(lk, build_friends(lists)))


def hooks(lk, action, when=None, minutes=0, hid=0):
    return parse_reply(request(lk, build_hooks(action, when, minutes, hid)))


def events(lk):
    return parse_reply(request(lk, build_events()))


def checklist(lk, title=None, items=None, clear=False):
    return parse_reply(request(lk, build_checklist(title, items, clear)))


def waypoints(lk, action="list", wid=0):
    return parse_reply(request(lk, build_waypoints(action, wid)))


def quests(lk):
    return parse_reply(request(lk, build_quests()))


def quest_status(lk, ids):
    reply = parse_reply(request(lk, build_quest_status(ids)))
    if reply.get("ok"):
        reply["quests"] = dict(zip([int(i) for i in ids], reply.pop("status")))
    return reply


def show(lk, kind, title, text, seconds=0, item=0):
    return parse_reply(request(lk, build_show(kind, title, text, seconds, item)))


def via_service(argv):
    """`decklink agent ...` through the link keeper (agentd) when it's running: (exit code, printed) or
    None when there's no service (the caller then opens the link itself)."""
    from . import agentd
    req = _request_for(argv)
    if req is None:
        return None
    wait = 30 + (int(req.get("seconds", 30)) + 150 if req["cmd"] == "prompt" else 0)
    try:
        reply = agentd.ask(req, timeout=wait)
    except OSError:
        return None
    return (0 if reply.get("ok") else 1), json.dumps(reply)


def _request_for(argv):
    """`decklink agent ...` arguments -> the link keeper's request dict, or None if they don't parse."""
    if not argv:
        return None
    if argv[0] == "state":
        return {"cmd": "state", "fields": argv[1:] or FIELDS}
    if argv[0] == "way" and len(argv) == 4:
        return {"cmd": "way", "map": int(argv[1]), "x": float(argv[2]), "y": float(argv[3])}
    if argv[0] == "chat" and len(argv) == 4:
        return {"cmd": "chat", "channel": argv[1], "target": argv[2], "text": argv[3]}
    if argv[0] == "friends":
        return {"cmd": "friends", "lists": argv[1:] or LISTS}
    if argv[0] == "prompt":
        rest, as_list, seconds = list(argv[1:]), False, 30
        while rest and rest[0].startswith("--"):
            flag = rest.pop(0)
            if flag == "--list":
                as_list = True
            elif flag == "--seconds" and rest:
                seconds = int(rest.pop(0))
        if len(rest) < 3:
            return None
        return {"cmd": "prompt", "question": rest[0], "options": rest[1:], "list": as_list, "seconds": seconds}
    if argv[0] == "hook" and len(argv) >= 2:
        if argv[1] == "add" and len(argv) == 3:
            return dict(json.loads(argv[2]), cmd="hook", op="add")
        if argv[1] in ("list", "clear") and len(argv) == 2:
            return {"cmd": "hook", "op": argv[1]}
        if argv[1] == "remove" and len(argv) == 3:
            return {"cmd": "hook", "op": "remove", "id": argv[2]}
        return None
    if argv[0] == "checklist" and len(argv) == 2:
        if argv[1] == "clear":
            return {"cmd": "checklist", "clear": True}
        spec = json.loads(argv[1])
        return {"cmd": "checklist", "title": spec.get("title", ""), "items": spec.get("items", [])}
    if argv[0] == "waypoints":
        if len(argv) == 1:
            return {"cmd": "waypoints", "action": "list"}
        if argv[1] == "remove" and len(argv) == 3:
            return {"cmd": "waypoints", "action": "remove", "id": int(argv[2])}
        if argv[1] in ("remove-active", "remove-all") and len(argv) == 2:
            return {"cmd": "waypoints", "action": argv[1]}
        return None
    if argv[0] == "quests" and len(argv) == 1:
        return {"cmd": "quests"}
    if argv[0] == "quest-status" and len(argv) > 1:
        return {"cmd": "quest-status", "ids": [int(a) for a in argv[1:]]}
    if argv[0] == "show" and len(argv) >= 2:
        req, rest = {"cmd": "show", "kind": argv[1]}, list(argv[2:])
        while rest and rest[0] in ("--seconds", "--item") and len(rest) > 1:
            flag = rest.pop(0)[2:]
            req[flag] = int(rest.pop(0))
        if len(rest) != 2:
            return None
        req["title"], req["text"] = rest
        return req
    return None


def handle(lk, req):
    """One link keeper request (see _request_for) on an open Link -> the parsed reply."""
    cmd = req["cmd"]
    if cmd == "state":
        fields = req.get("fields") or FIELDS
        bad = [f for f in fields if f not in FIELDS]
        if bad:
            raise ValueError("unknown field %s (fields: %s)" % (", ".join(bad), " ".join(FIELDS)))
        return state(lk, fields)
    if cmd == "way":
        return way(lk, req["map"], req["x"], req["y"])
    if cmd == "chat":
        return chat(lk, req["channel"], req.get("target", ""), req["text"])
    if cmd == "friends":
        return friends(lk, req.get("lists") or LISTS)
    if cmd == "hook":
        raise ValueError("hooks go through the link keeper (the decklink-agent service), which keeps their notes")
    if cmd == "checklist":
        return checklist(lk, req.get("title"), req.get("items"), req.get("clear", False))
    if cmd == "waypoints":
        return waypoints(lk, req.get("action", "list"), req.get("id", 0))
    if cmd == "quests":
        return quests(lk)
    if cmd == "quest-status":
        return quest_status(lk, req["ids"])
    if cmd == "show":
        return show(lk, req["kind"], req.get("title", ""), req.get("text", ""), req.get("seconds", 0),
                    req.get("item", 0))
    if cmd == "prompt":
        return ask(lk, req["question"], req["options"], req.get("list", False), req.get("seconds", 30))
    raise ValueError("unknown cmd %r" % cmd)


def main(lk, argv):
    """`decklink agent ...` with an open Link. Returns the exit code."""
    req = _request_for(argv)
    if req is None:
        print(__doc__)
        return 2
    reply = handle(lk, req)
    print(json.dumps(reply))
    return 0 if reply.get("ok") else 1
