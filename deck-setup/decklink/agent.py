"""The game side of DeckAgent, asked from the Deck: one binary request on channel 3, one reply.
addons/DeckAgent answers: Agent.lua's header documents the framing and its ops, each other op file
(Chat, People, Ask, Show, Quests, Checklist, Hooks, Inventory) its own; keep them in step (test/test_agent.py
checks the bytes both ways). Every request is checked and built here (prepare), for the CLI and the
link keeper alike. agent/mcp_server.py is the usual caller.

    decklink agent state [FIELD ...]     where you are; FIELDs: pos world zone who combat brain voice
                                         (default: all)
    decklink agent way MAP X Y           a waypoint (X, Y 0-100, like /way), made active
    decklink agent waypoints [remove ID | remove-active | remove-all]   list (or remove) waypoints
    decklink agent character             his character sheet: equipped item IDs, stats, money, spec (numbers)
    decklink agent bags [bank]           what he carries (or had in the bank when he last opened it)
    decklink agent item-info ID[:SUFFIX] ...   exact stats of items, as the game computes them
    decklink agent prices ID ...         auction (now, market), vendor and disenchant prices (MarketDeck)
    decklink agent mark LABEL ID ... | unmark [LABEL]   flag items in his bags (PackPad's lilac star)
    decklink agent hook add JSON | list | remove ID | clear   the assistant's hooks (through the link
                                         keeper only: it keeps their notes; see hooks.py)
    decklink agent checklist JSON | clear   put up a self-ticking checklist ({"title", "items": [{"label",
                                         "track", its fields, "done"}]}; see CHECK_KINDS), or take it down
    decklink agent chat CHANNEL TARGET TEXT   send chat for you (CHANNEL: say yell party raid guild
                                         whisper bnet instance; TARGET "" unless whisper/bnet)
    decklink agent friends [LIST ...]    who's online; LISTs: bnet friends guild (default: all)
    decklink agent people [--minutes N] [KIND ...]   who's been around lately: KINDs whispers chat
                                         trades invites group target (default: all); chat last
    decklink agent invite NAME           invite a character to your group
    decklink agent widget show [--save] [--account] [--mark MARK] NAME JSON | hide NAME | list |
                         delete [--account | --character] NAME   live displays (DeckKit.Widgets, Widgets.lua)
    decklink agent displays [hide KIND [NAME] | hide-all]   what of the assistant's is on screen (KIND: widget
                                         checklist card reading window), or take it down
    decklink agent ledger values JSON | totals KIND [MINUTES] [--account] | hidden   what you've gained, live
                                         numbers (DeckKit.Measures) and what the game hid from the ledger
    decklink agent prompt [--list] [--seconds N] QUESTION OPTION OPTION [...]   ask Ben with a prompt in
                                         the game and wait for his answer (2 options: a dialog)
    decklink agent show KIND [--seconds N] [--item ID] TITLE TEXT   show something: KIND echo (TITLE
                                         the question, TEXT the answer, into chat), card, or reading
    decklink agent layout [--wait SECONDS] JSON   a DeckKit layout in its own window (addons/DeckKit/Layout.lua);
                                         with --wait and reply buttons, waits for Ben's pick: {"answer": its value}
    decklink agent quests                the quest log: IDs, complete/failed, objective counts (no text)
    decklink agent quest-status ID ...   each quest's status: not taken, in the log, ready, done

Prints the reply as JSON and exits 1 when the game says no.
"""
import json, struct, time

from . import codec

CHANNEL = 3
VERSION = 1
OP_STATE, OP_WAY, OP_CHAT, OP_FRIENDS, OP_ASK, OP_ANSWER, OP_SHOW, OP_QUESTS, OP_QUEST_STATUS, OP_WAYPOINTS, OP_CHECKLIST, OP_HOOKS, OP_EVENTS, OP_CHARACTER, OP_BAGS, OP_ITEM_INFO, OP_PRICES, OP_MARKS, OP_DISMISS = (
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19)
OP_PEOPLE, OP_INVITE = 20, 21
OP_WIDGET, OP_LEDGER, OP_DISPLAYS = 22, 23, 24
DISPLAY_KINDS = ["widget", "checklist", "card", "reading", "window"]  # DISPLAYS kind byte = index + 1
WIDGET_ACTIONS = {"show": 1, "hide": 2, "list": 3, "delete": 4}
LEDGER_KINDS = ["loot", "created", "coin", "money", "xp", "rep", "skill", "quest"]  # LEDGER totals byte 2 = index + 1
MAX_MEASURES = 20
ASK_RESULT = {1: "shown", 2: "queued"}
NO_ANSWER = 255
KEEPALIVE_S = 10  # the game closes the link after 30 s without a frame
CHANNELS = ["say", "yell", "party", "raid", "guild", "whisper", "bnet", "instance"]  # CHAT byte 1 = index + 1
CHAT_RESULT = {1: "sent", 2: "previewed", 3: "previewed after combat"}
LISTS = ["bnet", "friends", "guild"]  # FRIENDS byte 1 bits
PEOPLE_KINDS = ["whispers", "chat", "trades", "invites", "group", "target"]  # PEOPLE byte 1 bits
# PEOPLE's entry kinds (People.lua) -> what the reply calls them.
PERSON_KIND = {1: "whisper", 2: "bnet_whisper", 3: "chat", 4: "trade", 5: "invite", 6: "group", 7: "target"}
DIRECTION = {1: "from_them", 2: "from_you"}
FIELDS = ["pos", "world", "zone", "who", "combat", "brain", "voice", "target"]  # mask bit 0, 1, 2, ...
BRAIN = {1: "haiku", 2: "sonnet", 3: "opus"}  # 0: the default
# Inventory.lua's stat codes (keep in step).
STAT_CODES = {1: "strength", 2: "agility", 3: "stamina", 4: "intellect", 5: "spirit", 6: "armor",
              7: "holy_resistance", 8: "fire_resistance", 9: "nature_resistance", 10: "frost_resistance",
              11: "shadow_resistance", 12: "arcane_resistance", 13: "dps", 14: "spell_power", 15: "healing",
              16: "attack_power", 17: "crit_rating", 18: "hit_rating", 19: "defense_rating", 20: "mana_per_5",
              21: "health_per_5", 22: "block_value", 23: "spell_crit_rating", 24: "spell_hit_rating",
              25: "ranged_attack_power", 26: "dodge_rating", 27: "parry_rating", 28: "block_rating",
              29: "spell_penetration"}
SLOT_NAMES = {1: "head", 2: "neck", 3: "shoulder", 4: "shirt", 5: "chest", 6: "waist", 7: "legs", 8: "feet",
              9: "wrist", 10: "hands", 11: "finger1", 12: "finger2", 13: "trinket1", 14: "trinket2", 15: "back",
              16: "main_hand", 17: "off_hand", 18: "ranged", 19: "tabard"}
INVTYPES = {0: None, 1: "head", 2: "neck", 3: "shoulder", 4: "shirt", 5: "chest", 6: "waist", 7: "legs", 8: "feet",
            9: "wrist", 10: "hands", 11: "finger", 12: "trinket", 13: "one-hand", 14: "shield", 15: "ranged",
            16: "back", 17: "two-hand", 18: "bag", 19: "tabard", 20: "chest (robe)", 21: "main hand", 22: "off hand",
            23: "held in off hand", 24: "ammo", 25: "thrown", 26: "ranged (gun/crossbow/wand)", 27: "quiver",
            28: "relic"}
QUALITIES = {0: "poor", 1: "common", 2: "uncommon", 3: "rare", 4: "epic", 5: "legendary", 6: "artifact"}
SHOW_KINDS = ["echo", "card", "reading", "layout"]  # SHOW byte 1 = index + 1
SHOW_RESULT = {1: "shown", 2: "queued"}
QUEST_STATUS = {0: "not taken", 1: "in log", 2: "ready to turn in", 3: "done"}
GOAL_KINDS = {1: "monster", 2: "item", 3: "object", 4: "event", 5: "reputation", 6: "other"}
# Longest text per kind, in UTF-8 bytes. A reading view is one frame (codec.MAX_IN) whatever the title.
SHOW_TEXT = {"echo": 1000, "card": 600, "reading": codec.MAX_IN - 9 - 255, "layout": codec.MAX_IN - 9 - 255}
LAYOUT_WAIT_MAX = 300  # seconds a layout may wait for Ben's reply
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


def build_people(kinds=None, minutes=0):
    mask = 0
    for name in kinds or []:
        if name not in PEOPLE_KINDS:
            raise ValueError("unknown kind %r (kinds: %s)" % (name, " ".join(PEOPLE_KINDS)))
        mask |= 1 << PEOPLE_KINDS.index(name)
    if not 0 <= int(minutes) <= 255:
        raise ValueError("minutes is 0-255")
    return bytes([op_byte(OP_PEOPLE), mask, int(minutes)])


def build_invite(name):
    name = (name or "").strip()
    if not name or any(c in name for c in "|\n\r\t"):
        raise ValueError("invite needs a character name")
    return bytes([op_byte(OP_INVITE)]) + _pstr(name, 64)


def _json_field(value, limit=3500):
    data = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(data) > limit:
        raise ValueError("over %d bytes of JSON (%d)" % (limit, len(data)))
    return struct.pack(">H", len(data)) + data


def build_widget(action, name="", spec=None, save=False, account=False, mark="", scope=None):
    """WIDGET: show (a layout as a live tracker; saved as an action with save), hide, list or delete."""
    if action not in WIDGET_ACTIONS:
        raise ValueError("action is one of: " + " ".join(WIDGET_ACTIONS))
    head = bytes([op_byte(OP_WIDGET), WIDGET_ACTIONS[action]])
    if action == "list":
        return head
    name = (name or "").strip()
    if not name or any(c in name for c in "|\n\r\t") or len(name.encode("utf-8")) > 40:
        raise ValueError("a widget needs a name of 1-40 bytes")
    if action == "hide":
        return head + _pstr(name)
    if action == "delete":
        return head + bytes([{None: 0, "character": 1, "account": 2}[scope]]) + _pstr(name)
    if not isinstance(spec, dict) or not isinstance(spec.get("type"), str):
        raise ValueError("a widget is a DeckKit layout: an object with a type")
    flags = (1 if save else 0) | (2 if account else 0)
    return head + bytes([flags]) + _pstr(name) + _pstr(mark or "", 40) + _json_field(spec)


def build_ledger(action, measures=None, kind=None, minutes=0, account=False):
    """LEDGER: values (live numbers for DeckKit.Measures tables), totals (of a kind, by item / faction / skill) or
    hidden (what the game withheld from the ledger this session)."""
    if action == "values":
        if not isinstance(measures, list) or not 1 <= len(measures) <= MAX_MEASURES:
            raise ValueError("values needs 1-%d measures" % MAX_MEASURES)
        if not all(isinstance(m, dict) and isinstance(m.get("measure"), str) for m in measures):
            raise ValueError("each measure is an object with a measure name")
        return bytes([op_byte(OP_LEDGER), 1]) + _json_field(measures)
    if action == "totals":
        if kind not in LEDGER_KINDS:
            raise ValueError("kind is one of: " + " ".join(LEDGER_KINDS))
        if not 0 <= int(minutes) <= 65535:
            raise ValueError("minutes is 0-65535")
        return struct.pack(">BBBHB", op_byte(OP_LEDGER), 2, LEDGER_KINDS.index(kind) + 1, int(minutes),
                           1 if account else 0)
    if action == "hidden":
        return bytes([op_byte(OP_LEDGER), 3])
    raise ValueError("action is one of: values totals hidden")


def build_displays(action="list", kind=None, name=""):
    """DISPLAYS: list what of hers is up (and her saved widgets), hide one, or hide all."""
    if action == "list":
        return bytes([op_byte(OP_DISPLAYS), 1])
    if action == "hide-all":
        return bytes([op_byte(OP_DISPLAYS), 3])
    if action == "hide":
        if kind not in DISPLAY_KINDS:
            raise ValueError("kind is one of: " + " ".join(DISPLAY_KINDS))
        if kind == "widget" and not (name or "").strip():
            raise ValueError("hiding a widget needs its name")
        return bytes([op_byte(OP_DISPLAYS), 2, DISPLAY_KINDS.index(kind) + 1]) + _pstr((name or "").strip(), 60)
    raise ValueError("action is one of: list hide hide-all")


def parse_displays(p, action):
    reply = parse_reply(p)
    if not reply.get("ok"):
        return reply
    i = 3
    if action == "list":
        (count,), i = _unpack(">B", p, i)
        shown = []
        for _ in range(count):
            (kind,), i = _unpack(">B", p, i)
            name, i = _string(p, i)
            (flags,), i = _unpack(">B", p, i)
            d = {"kind": DISPLAY_KINDS[kind - 1] if 1 <= kind <= len(DISPLAY_KINDS) else kind, "name": name,
                 "on_screen": bool(flags & 4)}
            if d["kind"] == "widget":
                d["saved"] = bool(flags & 1)
                if d["saved"]:
                    d["scope"] = "account" if flags & 2 else "character"
            shown.append(d)
        reply["displays"] = shown
    else:
        (n,), i = _unpack(">B", p, i)
        reply["hidden"] = n
    return reply


def _number(text):
    if text == "":
        return None
    v = float(text)
    return int(v) if v == int(v) else v


def parse_widget(p, action):
    reply = parse_reply(p)
    if not reply.get("ok"):
        return reply
    i = 3
    if action == "list":
        (count,), i = _unpack(">B", p, i)
        widgets = []
        for _ in range(count):
            name, i = _string(p, i)
            (flags,), i = _unpack(">B", p, i)
            widgets.append({"name": name, "scope": "account" if flags & 1 else "character", "shown": bool(flags & 2)})
        reply["widgets"] = widgets
    else:
        (done,), i = _unpack(">B", p, i)
        reply["done"] = done == 1
    return reply


def parse_ledger(p, action, keys=None):
    reply = parse_reply(p)
    if not reply.get("ok"):
        return reply
    i = 3
    if action == "values":
        (count,), i = _unpack(">B", p, i)
        values = []
        for _ in range(count):
            text, i = _string(p, i)
            values.append(_number(text))
        reply["values"] = values
    elif action == "totals":
        (flags, count), i = _unpack(">BB", p, i)
        totals = []
        for _ in range(count):
            key, i = _string(p, i)
            amount, i = _string(p, i)
            totals.append({"key": key, "amount": _number(amount)})
        reply["totals"], reply["more"] = totals, bool(flags & 1)
    else:
        (count,), i = _unpack(">B", p, i)
        rows = []
        for _ in range(count):
            kind, i = _string(p, i)
            context, i = _string(p, i)
            (seen, hidden), i = _unpack(">HH", p, i)
            rows.append({"kind": kind, "context": context, "seen": seen, "hidden": hidden})
        reply["hidden"] = rows
    return reply


def _trade_side(p, i):
    (money, n), i = _unpack(">IB", p, i)
    items = []
    for _ in range(n):
        name, i = _string(p, i)
        (count,), i = _unpack(">H", p, i)
        items.append({"name": name, "count": count})
    return {"money": money, "items": items}, i


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


def build_dismiss(seq):
    """DISMISS: take down the prompt the ASK frame `seq` put up (nobody waits for its answer now)."""
    return bytes([op_byte(OP_DISMISS), int(seq) & 0xFF])


def card_seconds(text):
    """How long a card stays up when nobody says: long enough to read it (8 s plus ~15 characters a
    second, at most a minute). The one rule for every card, the assistant's (respond) and the voice's."""
    return int(min(60, 8 + len(text or "") / 15))


def build_show(kind, title, text, seconds=None, item=0):
    """SHOW: kind echo/card/reading, a title (echo: the question) and the text (echo: the answer). An
    echo too long is cut (it's a copy of what was said); a card or reading view too long is refused.
    seconds: a card's time on screen (0: until he closes it; None: card_seconds)."""
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
    if seconds is None:
        seconds = card_seconds(body.decode("utf-8", "ignore")) if kind == "card" else 0
    seconds = max(0, min(255, int(seconds)))
    return (bytes([op_byte(OP_SHOW), SHOW_KINDS.index(kind) + 1, seconds]) + struct.pack(">I", int(item))
            + bytes([len(t)]) + t + struct.pack(">H", len(body)) + body)


def layout_packet(spec, wait=0):
    """SHOW kind layout: a DeckKit layout (addons/DeckKit/Layout.lua's header has the node types) as
    compact JSON. Each reply value in it (a piece's buttons: {"a": {"reply": value}}) goes as its number,
    1-n, and the values come back in order, to turn Ben's answer into the value again. wait: seconds the
    game is told someone waits for that answer (0 when there's nothing to reply with)."""
    if not isinstance(spec, dict) or not isinstance(spec.get("type"), str):
        raise ValueError("a layout is a node: an object with a type")
    replies = []

    def walk(node):
        if isinstance(node, list):
            return [walk(x) for x in node]
        if not isinstance(node, dict):
            return node
        out = {}
        for k, v in node.items():
            if k == "buttons" and isinstance(v, dict):
                out[k] = {}
                for key, act in v.items():
                    if isinstance(act, dict) and "reply" in act:
                        replies.append(act["reply"])
                        act = dict(act, reply=len(replies))
                    out[k][key] = act
            else:
                out[k] = walk(v)
        return out

    text = json.dumps(walk(spec), separators=(",", ":"), ensure_ascii=False)
    if len(replies) > 250:
        raise ValueError("more than 250 reply buttons")
    wait = max(0, min(LAYOUT_WAIT_MAX, int(wait or 0))) if replies else 0
    return build_show("layout", "", text, seconds=wait), replies


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


def build_character():
    return bytes([op_byte(OP_CHARACTER)])


def build_bags(bank=False):
    return bytes([op_byte(OP_BAGS), 2 if bank else 1])


def build_item_info(items):
    """items: [item ID or (item ID, suffix)], 1-30."""
    items = [(i, 0) if isinstance(i, int) else (int(i[0]), int(i[1])) for i in items]
    if not 1 <= len(items) <= 30:
        raise ValueError("1-30 items")
    return bytes([op_byte(OP_ITEM_INFO), len(items)]) + b"".join(struct.pack(">Ih", i, sfx) for i, sfx in items)


def build_prices(ids):
    ids = [int(i) for i in ids]
    if not 1 <= len(ids) <= 60:
        raise ValueError("1-60 item IDs")
    return bytes([op_byte(OP_PRICES), len(ids)]) + b"".join(struct.pack(">I", i) for i in ids)


def build_marks(label, ids=None, clear=False):
    """MARKS: flag item IDs under a label (PackPad's star and tooltip line), or clear a label ("" all)."""
    lab = _pstr(label or "", 60)
    if clear:
        return bytes([op_byte(OP_MARKS), 2]) + lab
    ids = [int(i) for i in ids or []]
    if not 1 <= len(ids) <= 40:
        raise ValueError("1-40 item IDs")
    if not (label or "").strip():
        raise ValueError("a label (what they're for)")
    return bytes([op_byte(OP_MARKS), 1]) + lab + bytes([len(ids)]) + b"".join(struct.pack(">I", i) for i in ids)


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
            elif f == "voice":
                (q,), i = _unpack(">B", p, i)
                v = {"quiet": q == 1}
            else:  # target
                (npc, lvl, tflags, hp), i = _unpack(">IBBB", p, i)
                v = {"target": None if not tflags & 1 else {
                    "npc_id": npc or None, "level": None if lvl == 255 else lvl, "attackable": bool(tflags & 2),
                    "dead": bool(tflags & 4), "elite": bool(tflags & 8), "rare": bool(tflags & 16),
                    "player": bool(tflags & 32), "tapped_by_others": bool(tflags & 64), "health_pct": hp}}
            if f in given:
                out.update(v)
    elif out["op"] == OP_WAY:
        (via, wid), i = _unpack(">BH", p, i)
        out["via"], out["waypoint"] = VIA.get(via, via), wid
        # The route DeckKit planned to it (ForeverNav): left out by a game from before routes, 0 steps for none; its
        # flags byte (1: walks measured) left out by a game from before it.
        if i < len(p):
            (count,), i = _unpack(">B", p, i)
            if count:
                (total,), i = _unpack(">H", p, i)
                steps = []
                for _ in range(count):
                    (seconds,), i = _unpack(">H", p, i)
                    text, i = _string(p, i)
                    steps.append({"text": text, "seconds": seconds})
                out["route"] = {"seconds": total, "steps": steps}
                if i < len(p):
                    (flags,), i = _unpack(">B", p, i)
                    out["route"]["measured"] = bool(flags & 1)
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
    elif out["op"] == OP_CHARACTER:
        (level, money, free, slots, spec), i = _unpack(">BIBBH", p, i)
        stats, i = _unpack(">10H", p, i)
        names = ["strength", "agility", "stamina", "intellect", "spirit", "armor", "spell_power", "healing",
                 "max_health", "max_mana"]
        (n,), i = _unpack(">B", p, i)
        equipped = []
        for _ in range(n):
            (slot, item, enchant, suffix, dur), i = _unpack(">BIHhB", p, i)
            equipped.append({"slot": SLOT_NAMES.get(slot, slot), "item_id": item, "enchant_id": enchant or None,
                             "suffix_id": suffix or None, "durability_pct": None if dur == 255 else dur})
        out.update({"level": level, "money_copper": money, "bag_free": free, "bag_slots": slots,
                    "spec_id": spec or None, "stats": dict(zip(names, stats)), "equipped": equipped})
    elif out["op"] == OP_BAGS:
        (flags, free, slots, age, n), i = _unpack(">BBBHB", p, i)
        items = []
        for _ in range(n):
            (item, count), i = _unpack(">IH", p, i)
            items.append({"item_id": item, "count": count})
        out.update({"items": items, "more": bool(flags & 1), "bank_never_seen": bool(flags & 2), "free": free,
                    "slots": slots, "minutes_old": age})
    elif out["op"] == OP_ITEM_INFO:
        (n,), i = _unpack(">B", p, i)
        items = []
        for _ in range(n):
            (item, status), i = _unpack(">IB", p, i)
            entry = {"item_id": item, "status": {0: "ok", 1: "loading", 2: "no such item"}.get(status, status)}
            if status == 0:
                (q, ilvl, req, inv, cls, sub, ns_), i = _unpack(">BHBBBBB", p, i)
                stats = {}
                for _ in range(ns_):
                    (code, value), i = _unpack(">Bh", p, i)
                    stats[STAT_CODES.get(code, "stat%d" % code)] = value / 10 if code == 13 else value
                entry.update({"quality": QUALITIES.get(q, q), "item_level": ilvl, "required_level": req,
                              "slot": INVTYPES.get(inv, inv), "class_id": cls, "subclass_id": sub, "stats": stats})
            items.append(entry)
        out["items"] = items
    elif out["op"] == OP_PRICES:
        (flags, n), i = _unpack(">BB", p, i)
        prices = []
        for _ in range(n):
            (item, now, qty, market, days, hours, vendor, de), i = _unpack(">IIHIBHII", p, i)
            prices.append({"item_id": item, "auction_now": now or None, "quantity_up": qty, "market": market or None,
                           "days_of_history": days, "hours_since_scan": None if hours == 65535 else hours,
                           "vendor": vendor or None, "disenchant": de or None})
        out["prices"], out["market_installed"] = prices, not flags & 1
    elif out["op"] == OP_MARKS:
        (flags, n), i = _unpack(">BB", p, i)
        out["carried"] = list(_unpack(">%dH" % n, p, i)[0]) if n else []
        out["packpad_installed"] = not flags & 1
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
    elif out["op"] == OP_DISMISS:
        (done,), i = _unpack(">B", p, i)
        out["dismissed"] = done == 1
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
    elif out["op"] == OP_PEOPLE:
        (flags, count), i = _unpack(">BB", p, i)
        people = []
        for _ in range(count):
            (kind,), i = _unpack(">B", p, i)
            if kind not in PERSON_KIND:
                raise Malformed("unknown person kind %d" % kind)
            name, i = _string(p, i)
            (age,), i = _unpack(">H", p, i)
            e = {"kind": PERSON_KIND[kind], "name": name, "seconds_ago": age}
            if kind in (1, 2):
                (direction,), i = _unpack(">B", p, i)
                e["direction"] = DIRECTION.get(direction, direction)
                if kind == 2:
                    e["character"], i = _string(p, i)
                e["text"], i = _string(p, i)
            elif kind == 3:
                e["channel"], i = _string(p, i)
                e["text"], i = _string(p, i)
            elif kind == 4:
                e["they_gave"], i = _trade_side(p, i)
                e["you_gave"], i = _trade_side(p, i)
            elif kind == 6:
                (gflags,), i = _unpack(">B", p, i)
                e["leader"] = bool(gflags & 1)
            people.append(e)
        out["people"], out["more"] = people, bool(flags & 1)
    elif out["op"] == OP_INVITE:
        (sent,), i = _unpack(">B", p, i)
        out["invited"] = sent == 1
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


# How long a prompt may take beyond its own seconds: the reply to the request, and a prompt queued
# behind a fight (it shows when combat ends). Callers further out wait longer (via_service, the MCP
# server's ask_player, the launcher's MCP_TOOL_TIMEOUT), so this is the deadline that runs out first.
PROMPT_EXTRA_S = REPLY_TIMEOUT + 120


def ask(lk, question, options, as_list=False, seconds=30, clock=time.monotonic):
    """Shows a prompt and waits for Ben's answer: {"answer": option text or None, "choice": 1-6, 0 for
    cancelled, None for no answer}. Keeps the link alive while it waits."""
    return _await_answer(lk, build_ask(question, options, as_list, seconds), options, clock)


def _await_answer(lk, packet, options, clock=time.monotonic, cancelled=None):
    """Sends the ASK packet and waits for Ben's answer. `cancelled()` true (checked about once a
    second): the question it was for was dropped, so the prompt is taken down in the game (DISMISS) and
    the wait ends at once, rather than holding the link for minutes."""
    from .link import LinkError
    seq = lk.send(CHANNEL, packet)
    shown = None
    deadline = clock() + int(packet[2]) + PROMPT_EXTRA_S
    keepalive = clock() + KEEPALIVE_S
    while True:
        now = clock()
        if now >= deadline:
            raise LinkError("no answer from the game")
        if cancelled is not None and cancelled():
            try:
                request(lk, build_dismiss(seq))
            except LinkError:
                pass  # it times out by itself in the game
            return {"ok": False, "cancelled": True, "error": "the question it was for was dropped"}
        if now >= keepalive:
            lk.keepalive()
            keepalive = now + KEEPALIVE_S
        got = lk.recv(min(1.0, deadline - now))
        if got is None:
            continue
        channel, payload = got
        if channel != CHANNEL or len(payload) < 3 or payload[1] != seq:
            continue
        reply = parse_reply(payload)
        if reply["op"] in (OP_ASK, OP_SHOW):  # the request's own reply (a prompt, or a layout with replies)
            if not reply["ok"]:
                return reply
            shown = reply["result"]
        elif reply["op"] == OP_ANSWER:
            choice = reply["choice"]
            answer = options[choice - 1] if choice and choice <= len(options) else None
            return {"ok": True, "shown": shown, "choice": choice, "answer": answer}


def hooks(lk, action, when=None, minutes=0, hid=0):
    """HOOKS for the link keeper (agentd.py), which keeps each hook's note: never a CLI command."""
    return parse_reply(request(lk, build_hooks(action, when, minutes, hid)))


def events(lk):
    """EVENTS: the fired hooks the game has queued (the link keeper's glance)."""
    return parse_reply(request(lk, build_events()))


class Prepared:
    """A request checked and its packet built, before any link opens (a bad one shouldn't cost one):
    run(lk) sends it on an open Link and returns the parsed reply."""

    def __init__(self, cmd, packet, finish=None, answer_options=None):
        self.cmd, self.packet = cmd, packet
        self._finish = finish or parse_reply
        self._options = answer_options  # a prompt: wait for Ben's answer, not just the reply

    @property
    def waits(self):
        """Does it wait for Ben (a prompt, a layout with replies)? Then that time isn't the model's."""
        return self._options is not None

    def run(self, lk, cancelled=None):
        """`cancelled`: a prompt's wait ends (and the prompt goes) once it's true."""
        if self._options is not None:
            return _await_answer(lk, self.packet, self._options, cancelled=cancelled)
        return self._finish(request(lk, self.packet))


# Link keeper / CLI requests (see _request_for) -> their packets. state, quest-status and prompt are
# in prepare(): their replies need the request to read them.
BUILDERS = {
    "way": lambda r: build_way(r["map"], r["x"], r["y"]),
    "chat": lambda r: build_chat(r["channel"], r.get("target", ""), r["text"]),
    "friends": lambda r: build_friends(r.get("lists") or LISTS),
    "people": lambda r: build_people(r.get("kinds"), r.get("minutes", 0)),
    "invite": lambda r: build_invite(r["name"]),
    "character": lambda r: build_character(),
    "bags": lambda r: build_bags(r.get("bank", False)),
    "item-info": lambda r: build_item_info(r["items"]),
    "prices": lambda r: build_prices(r["ids"]),
    "mark": lambda r: build_marks(r.get("label", ""), r.get("ids"), r.get("clear", False)),
    "checklist": lambda r: build_checklist(r.get("title"), r.get("items"), r.get("clear", False)),
    "waypoints": lambda r: build_waypoints(r.get("action", "list"), r.get("id", 0)),
    "quests": lambda r: build_quests(),
    "show": lambda r: build_show(r["kind"], r.get("title", ""), r.get("text", ""), r.get("seconds"),
                                 r.get("item", 0)),
}
COMMANDS = set(BUILDERS) | {"state", "quest-status", "prompt", "layout", "widget", "ledger", "displays"}


def prepare(req):
    """One link keeper request -> Prepared. ValueError or KeyError when it's bad (nothing is sent)."""
    cmd = req.get("cmd")
    if cmd == "state":
        fields = list(req.get("fields") or FIELDS)
        bad = [f for f in fields if f not in FIELDS]
        if bad:
            raise ValueError("unknown field %s (fields: %s)" % (", ".join(bad), " ".join(FIELDS)))
        return Prepared(cmd, build_state(fields), lambda p: parse_reply(p, fields))
    if cmd == "widget":
        action = req.get("action", "list")
        packet = build_widget(action, req.get("name", ""), req.get("spec"), req.get("save", False),
                              req.get("account", False), req.get("mark", ""), req.get("scope"))
        return Prepared(cmd, packet, lambda p: parse_widget(p, action))
    if cmd == "displays":
        action = req.get("action", "list")
        return Prepared(cmd, build_displays(action, req.get("kind"), req.get("name", "")),
                        lambda p: parse_displays(p, action))
    if cmd == "ledger":
        action = req.get("action", "hidden")
        packet = build_ledger(action, req.get("measures"), req.get("kind"), req.get("minutes", 0),
                              req.get("account", False))
        return Prepared(cmd, packet, lambda p: parse_ledger(p, action))
    if cmd == "quest-status":
        ids = [int(i) for i in req["ids"]]

        def finish(p):
            reply = parse_reply(p)
            if reply.get("ok"):
                reply["quests"] = dict(zip(ids, reply.pop("status")))
            return reply
        return Prepared(cmd, build_quest_status(ids), finish)
    if cmd == "prompt":
        options = list(req["options"])
        return Prepared(cmd, build_ask(req["question"], options, req.get("list", False), req.get("seconds", 30)),
                        answer_options=options)
    if cmd == "layout":
        packet, replies = layout_packet(req["spec"], req.get("wait", 0))
        if packet[2] and replies:  # waiting: the answer turns back into its reply value
            return Prepared(cmd, packet, answer_options=replies)
        return Prepared(cmd, packet)
    if cmd == "hook":
        raise ValueError("hooks go through the link keeper (the decklink-agent service), which keeps their notes")
    if cmd not in BUILDERS:
        raise ValueError("unknown cmd %r" % cmd)
    return Prepared(cmd, BUILDERS[cmd](req))


def service_wait(req):
    """Seconds a request may take beyond the usual, waiting for Ben: a prompt's own seconds, a layout's
    wait, plus what PROMPT_EXTRA_S allows for the reply and a fight in between."""
    if req["cmd"] == "prompt":
        return int(req.get("seconds", 30)) + 150
    if req["cmd"] == "layout" and req.get("wait"):
        return min(LAYOUT_WAIT_MAX, int(req["wait"])) + 150
    return 0


def via_service(argv):
    """`decklink agent ...` through the link keeper (agentd) when it's running: (exit code, printed) or
    None when there's no service (the caller then opens the link itself).

    Only a service that couldn't be reached at all means "no service". Once the request is in, the
    keeper may be carrying it out (a chat message sent, a waypoint set) however its answer fails to come
    back, so it's reported as unknown and never sent again by another link."""
    from . import agentd
    req = _request_for(argv)
    if req is None:
        return None
    wait = 30 + service_wait(req)
    try:
        reply = agentd.ask(req, timeout=wait)
    except agentd.NotRunning:
        return None
    except agentd.NoAnswer as e:
        reply = {"ok": False, "error": "the link keeper took the request but its answer never came (%s): it may "
                                       "have been carried out, so it wasn't sent again" % e, "unknown": True}
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
    if argv[0] == "people":
        rest, minutes = list(argv[1:]), 0
        if rest[:1] == ["--minutes"] and len(rest) > 1:
            rest.pop(0)
            minutes = int(rest.pop(0))
        return {"cmd": "people", "kinds": rest, "minutes": minutes}
    if argv[0] == "invite" and len(argv) == 2:
        return {"cmd": "invite", "name": argv[1]}
    if argv[0] == "widget" and len(argv) >= 2:
        action, rest = argv[1], list(argv[2:])
        req = {"cmd": "widget", "action": action}
        while rest and rest[0].startswith("--"):
            flag = rest.pop(0)
            if flag == "--save":
                req["save"] = True
            elif flag == "--account":
                req["account"], req["scope"] = True, "account"
            elif flag == "--character":
                req["scope"] = "character"
            elif flag == "--mark" and rest:
                req["mark"] = rest.pop(0)
            else:
                return None
        if action == "list" and not rest:
            return req
        if action in ("hide", "delete") and len(rest) == 1:
            req["name"] = rest[0]
            return req
        if action == "show" and len(rest) == 2:
            req["name"], req["spec"] = rest[0], json.loads(rest[1])
            return req
        return None
    if argv[0] == "displays":
        if len(argv) == 1:
            return {"cmd": "displays", "action": "list"}
        if argv[1] == "hide-all" and len(argv) == 2:
            return {"cmd": "displays", "action": "hide-all"}
        if argv[1] == "hide" and len(argv) in (3, 4):
            return {"cmd": "displays", "action": "hide", "kind": argv[2], "name": argv[3] if len(argv) == 4 else ""}
        return None
    if argv[0] == "ledger" and len(argv) >= 2:
        if argv[1] == "values" and len(argv) == 3:
            return {"cmd": "ledger", "action": "values", "measures": json.loads(argv[2])}
        if argv[1] == "totals" and len(argv) >= 3:
            rest = [a for a in argv[3:] if a != "--account"]
            return {"cmd": "ledger", "action": "totals", "kind": argv[2], "minutes": int(rest[0]) if rest else 0,
                    "account": "--account" in argv}
        if argv[1] == "hidden" and len(argv) == 2:
            return {"cmd": "ledger", "action": "hidden"}
        return None
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
    if argv[0] == "character" and len(argv) == 1:
        return {"cmd": "character"}
    if argv[0] == "bags" and len(argv) <= 2:
        return {"cmd": "bags", "bank": argv[1:] == ["bank"]}
    if argv[0] == "mark" and len(argv) > 2:
        return {"cmd": "mark", "label": argv[1], "ids": [int(a) for a in argv[2:]]}
    if argv[0] == "unmark" and len(argv) <= 2:
        return {"cmd": "mark", "label": argv[1] if len(argv) == 2 else "", "clear": True}
    if argv[0] == "prices" and len(argv) > 1:
        return {"cmd": "prices", "ids": [int(a) for a in argv[1:]]}
    if argv[0] == "item-info" and len(argv) > 1:
        return {"cmd": "item-info", "items": [[int(x) for x in (a.split(":") + ["0"])[:2]] for a in argv[1:]]}
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
    if argv[0] == "layout" and len(argv) >= 2:
        req, rest = {"cmd": "layout"}, list(argv[1:])
        if rest[0] == "--wait" and len(rest) > 2:
            rest.pop(0)
            req["wait"] = int(rest.pop(0))
        if len(rest) != 1:
            return None
        req["spec"] = json.loads(rest[0])
        return req
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


def main(lk, argv):
    """`decklink agent ...` with an open Link. Returns the exit code."""
    req = _request_for(argv)
    if req is None:
        print(__doc__)
        return 2
    reply = prepare(req).run(lk)
    print(json.dumps(reply))
    return 0 if reply.get("ok") else 1
