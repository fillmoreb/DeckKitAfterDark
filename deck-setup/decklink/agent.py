"""The game side of DeckAgent, asked from the Deck: one binary request on channel 3, one reply.
addons/DeckAgent/Agent.lua answers and documents the packet layout; keep the two in step
(test/test_agent.py checks the bytes both ways). agent/mcp_server.py is the usual caller.

    decklink agent state [FIELD ...]     where you are; FIELDs: pos world zone who combat (default: all)
    decklink agent way MAP X Y           a waypoint (X, Y 0-100, like /way), made active

Prints the reply as JSON and exits 1 when the game says no.
"""
import json, struct, time

CHANNEL = 3
VERSION = 1
OP_STATE, OP_WAY = 1, 2
FIELDS = ["pos", "world", "zone", "who", "combat"]  # mask bit 0, 1, 2, ...
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
            else:  # combat
                (c,), i = _unpack(">B", p, i)
                v = {"in_combat": c == 1}
            if f in given:
                out.update(v)
    elif out["op"] == OP_WAY:
        (via, wid), i = _unpack(">BH", p, i)
        out["via"], out["waypoint"] = VIA.get(via, via), wid
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


def state(lk, fields=FIELDS):
    return parse_reply(request(lk, build_state(fields)), fields)


def way(lk, map_id, x, y):
    return parse_reply(request(lk, build_way(map_id, x, y)))


def via_service(argv):
    """`decklink agent ...` through the link keeper (agentd) when it's running: (exit code, printed) or
    None when there's no service (the caller then opens the link itself)."""
    from . import agentd
    if not argv or argv[0] not in ("state", "way"):
        return None
    if argv[0] == "state":
        req = {"cmd": "state", "fields": argv[1:] or FIELDS}
    elif len(argv) == 4:
        req = {"cmd": "way", "map": int(argv[1]), "x": float(argv[2]), "y": float(argv[3])}
    else:
        return None
    try:
        reply = agentd.ask(req)
    except OSError:
        return None
    return (0 if reply.get("ok") else 1), json.dumps(reply)


def main(lk, argv):
    """`decklink agent ...` with an open Link. Returns the exit code."""
    if not argv or argv[0] not in ("state", "way"):
        print(__doc__)
        return 2
    if argv[0] == "state":
        fields = argv[1:] or FIELDS
        bad = [f for f in fields if f not in FIELDS]
        if bad:
            raise ValueError("unknown field %s (fields: %s)" % (", ".join(bad), " ".join(FIELDS)))
        reply = state(lk, fields)
    else:
        if len(argv) != 4:
            print("usage: decklink agent way MAP X Y")
            return 2
        reply = way(lk, argv[1], argv[2], argv[3])
    print(json.dumps(reply))
    return 0 if reply.get("ok") else 1
