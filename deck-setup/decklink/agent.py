"""The game side of DeckAgent, asked from the Deck: one binary request on channel 3, one reply.
addons/DeckAgent/Agent.lua answers and documents the packet layout; keep the two in step
(test/test_agent.py checks the bytes both ways). agent/mcp_server.py is the usual caller.

    decklink agent state [FIELD ...]     where you are; FIELDs: pos world zone who combat brain (default: all)
    decklink agent way MAP X Y           a waypoint (X, Y 0-100, like /way), made active
    decklink agent chat CHANNEL TARGET TEXT   send chat for you (CHANNEL: say yell party raid guild
                                         whisper bnet instance; TARGET "" unless whisper/bnet)
    decklink agent friends [LIST ...]    who's online; LISTs: bnet friends guild (default: all)
    decklink agent prompt [--list] [--seconds N] QUESTION OPTION OPTION [...]   ask Ben with a prompt in
                                         the game and wait for his answer (2 options: a dialog)

Prints the reply as JSON and exits 1 when the game says no.
"""
import json, struct, time

from . import codec

CHANNEL = 3
VERSION = 1
OP_STATE, OP_WAY, OP_CHAT, OP_FRIENDS, OP_ASK, OP_ANSWER = 1, 2, 3, 4, 5, 6
ASK_RESULT = {1: "shown", 2: "queued"}
NO_ANSWER = 255
KEEPALIVE_S = 10  # the game closes the link after 30 s without a frame
CHANNELS = ["say", "yell", "party", "raid", "guild", "whisper", "bnet", "instance"]  # CHAT byte 1 = index + 1
CHAT_RESULT = {1: "sent", 2: "previewed", 3: "previewed after combat"}
LISTS = ["bnet", "friends", "guild"]  # FRIENDS byte 1 bits
FIELDS = ["pos", "world", "zone", "who", "combat", "brain"]  # mask bit 0, 1, 2, ...
BRAIN = {1: "haiku", 2: "sonnet", 3: "opus"}  # 0: the default
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
            else:  # brain
                (b,), i = _unpack(">B", p, i)
                v = {"model": BRAIN.get(b)}
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
