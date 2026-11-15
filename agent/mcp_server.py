"""DeckAgent's MCP server: the only way the assistant reaches the game, as a fixed list of tools.
Standard library only, MCP over stdio (one JSON-RPC message per line), so it runs on the Mac or the Deck.

    python3 agent/mcp_server.py        (the launcher, agent/deckagent, starts it for claude / codex)

Tools:
  get_player_state(fields)            (game) where you are: position, world position, zone, who you are
                                      (faction, level, class), combat; only the fields asked for
  find_npc(query, near, faction)      (database only) NPCs by name or title ("priest trainer"), nearest
                                      to `near` first; never asks the game anything
  set_waypoint(map_id, x, y)          (game) a WayPad waypoint (x, y 0-100, like /way), made active;
                                      returns its id
  send_chat(channel, text, to)        (game) say / yell / party / raid / guild / instance, or a whisper
                                      to a character or a Battle.net friend
  get_online(lists)                   (game) who's online: Battle.net friends (and their character),
                                      character friends, guild
  ask_player(question, options, ...)  (game) ask Ben with a prompt (yes/no dialog or a list) and wait
                                      for his answer
  remember(section, note)             (its notes) one line under People or Ben's rules in CLAUDE.md

There is deliberately no way to run Lua, send slash commands or touch combat, movement, targeting or
casting: only these named requests, answered by addons/DeckAgent in the game.
"""
import json, os, sys, traceback

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent import common, gamedb, memory  # noqa: E402

PROTOCOL = "2025-06-18"

TOOLS = [
    {"name": "get_player_state",
     "description": "Ask the game about the player. `fields` picks what to fetch (default all): "
                    "'position' (map_id and x/y 0-100), 'zone' (zone name), 'who' (faction H/A, level, class), "
                    "'combat' (in combat or not). Ask only for what the question needs: for 'nearest X' "
                    "that's position and who (for the faction).",
     "inputSchema": {"type": "object", "properties": {
         "fields": {"type": "array", "items": {"type": "string", "enum": ["position", "zone", "who", "combat"]},
                    "uniqueItems": True}},
         "additionalProperties": False}},
    {"name": "find_npc",
     "description": "Search the local game database (QuestieDB's Forever NPCs). Give a `role` for a kind of "
                    "NPC (flight_master, innkeeper, banker, auctioneer, stable_master, battlemaster, repair, "
                    "vendor, trainer, spirit_healer, guild_master: matched by the game's NPC flags, so a "
                    "Horde 'Wind Rider Master' counts as a flight master), and/or `query` words to match in "
                    "the name or title ('priest trainer', 'Thrall'). This doesn't ask the game anything: for "
                    "'nearest', pass the player's map_id, x and y as `near` (results come nearest first, with "
                    "distance in yards; null on another continent) and their faction as `faction` (only NPCs "
                    "friendly to it). Each result has its nearest spawn: map_id, x, y (0-100) and zone.",
     "inputSchema": {"type": "object", "properties": {
         "role": {"type": "string", "enum": ["flight_master", "innkeeper", "banker", "auctioneer", "stable_master",
                                             "battlemaster", "repair", "vendor", "trainer", "spirit_healer",
                                             "guild_master"]},
         "query": {"type": "string", "description": "words to match in the NPC's name or title"},
         "near": {"type": "object", "description": "a position to sort by: map_id and x, y (0-100)",
                  "properties": {"map_id": {"type": "integer"}, "x": {"type": "number"}, "y": {"type": "number"}},
                  "required": ["map_id", "x", "y"], "additionalProperties": False},
         "faction": {"type": "string", "enum": ["H", "A"],
                     "description": "only NPCs friendly to this faction (leave out to include all, e.g. mobs)"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 5}},
         "additionalProperties": False}},
    {"name": "set_waypoint",
     "description": "Put a waypoint in the game (WayPad) and make it the active one. x and y are 0-100 on "
                    "map_id, like /way. Do it straight away when the player asked for it. Returns the "
                    "waypoint's id. The waypoint has no name in the game, so say in your answer what it is.",
     "inputSchema": {"type": "object", "properties": {
         "map_id": {"type": "integer"}, "x": {"type": "number", "minimum": 0, "maximum": 100},
         "y": {"type": "number", "minimum": 0, "maximum": 100}},
         "required": ["map_id", "x", "y"], "additionalProperties": False}},
    {"name": "send_chat",
     "description": "Send a chat message for Ben, in his voice. channel: say, yell, party, raid, guild, instance, "
                    "whisper (to a character: `to` is its name, Name-Realm if needed) or bnet (a Battle.net "
                    "whisper: `to` is the friend's BattleTag, its name part, or account_id from get_online). "
                    "Send it when he asks, without asking back. At most 255 bytes: shorten it yourself, never "
                    "split. The result is 'sent', or 'previewed' (it waits in the game for Ben to press A; "
                    "tell him) or 'previewed after combat'. Public channels (Trade, General) aren't possible.",
     "inputSchema": {"type": "object", "properties": {
         "channel": {"type": "string", "enum": ["say", "yell", "party", "raid", "guild", "instance", "whisper",
                                                "bnet"]},
         "text": {"type": "string", "minLength": 1},
         "to": {"type": "string", "description": "for whisper and bnet"}},
         "required": ["channel", "text"], "additionalProperties": False}},
    {"name": "get_online",
     "description": "Who's online right now. lists: bnet (Battle.net friends: battletag, account_id, and the "
                    "character, realm, level, class and zone when they're in WoW), friends (character friends) "
                    "and guild. Default all. 'more' true means the list was cut short.",
     "inputSchema": {"type": "object", "properties": {
         "lists": {"type": "array", "items": {"type": "string", "enum": ["bnet", "friends", "guild"]},
                   "uniqueItems": True}},
         "additionalProperties": False}},
    {"name": "ask_player",
     "description": "Ask Ben something in the game and wait for his answer (he answers with the pad: no need "
                    "for him to speak again). Two options make a dialog: A picks the first, B the second "
                    "(e.g. ['Yes', 'No'], ['Confirm', 'Cancel']). More (up to 6), or list: true, make a list "
                    "he picks from with the d-pad. Keep it short: the question at most ~120 characters, each "
                    "option ~24. Returns answer (the option's text), or null when he cancelled or didn't "
                    "answer in time. Use it whenever you need a choice or a confirmation from him.",
     "inputSchema": {"type": "object", "properties": {
         "question": {"type": "string", "minLength": 1, "maxLength": 160},
         "options": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 40},
                     "minItems": 2, "maxItems": 6},
         "list": {"type": "boolean", "default": False},
         "seconds": {"type": "integer", "minimum": 5, "maximum": 120, "default": 45}},
         "required": ["question", "options"], "additionalProperties": False}},
    {"name": "remember",
     "description": "Write one line into your notes (CLAUDE.md in your folder, read before every question), so "
                    "you know it next time. section 'people': who someone is, e.g. 'Edna: BattleTag AMI#1362; "
                    "characters Heatherjr Locklear (warlock)'. section 'rules': something Ben told you to "
                    "always or never do. Use it the moment he tells you, before carrying on.",
     "inputSchema": {"type": "object", "properties": {
         "section": {"type": "string", "enum": ["people", "rules"]},
         "note": {"type": "string", "minLength": 3, "maxLength": 300}},
         "required": ["section", "note"], "additionalProperties": False}},
    {"name": "remember_fact",
     "description": "Keep a fact for later, in your memory database (not the notes): anything worth knowing "
                    "next time that isn't who someone is or one of Ben's rules. One short, self-contained "
                    "sentence about one subject, e.g. about 'Edna', fact 'Hates Stratholme; prefers to heal.' "
                    "or about 'Ben', fact 'Wants the Robes of Arcana from Scarlet Monastery.' Facts that seem "
                    "related to a question are shown to you before you start ([From your memory ...]). When a "
                    "fact has changed, pass the old one's id as `replaces` instead of keeping both.",
     "inputSchema": {"type": "object", "properties": {
         "about": {"type": "string", "minLength": 1, "maxLength": 60,
                   "description": "The subject: a person, character, place, dungeon, item... or Ben"},
         "fact": {"type": "string", "minLength": 3, "maxLength": 300},
         "replaces": {"type": "integer", "description": "id of an older fact this one corrects or updates"}},
         "required": ["about", "fact"], "additionalProperties": False}},
    {"name": "recall",
     "description": "Search your memory database: facts matching any of the query's words, best first, with "
                    "ids and the day each was saved. `about` alone lists everything about that subject.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "maxLength": 200},
         "about": {"type": "string", "maxLength": 60},
         "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8}},
         "additionalProperties": False}},
    {"name": "forget_fact",
     "description": "Forget a fact (by id) that's wrong or that Ben asks you to forget.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}},
         "required": ["id"], "additionalProperties": False}},
]


# Tool field names -> the game's (decklink agent state FIELD...). Position always brings the world
# position too: the database sorts by it, and it's 5 bytes.
FIELDS = {"position": ["pos", "world"], "zone": ["zone"], "who": ["who"], "combat": ["combat"]}


def tool_get_player_state(args):
    asked = args.get("fields") or list(FIELDS)
    game_fields = [g for f in asked for g in FIELDS[f]]
    s = common.game("state", *game_fields)
    if not s.get("ok"):
        raise common.GameError(s.get("reason") or s.get("status") or "the game said no")
    out = {}
    if "map" in s:
        out.update(map_id=s["map"], x=s["x"], y=s["y"])
    for k in ("zone", "faction", "level", "class", "in_combat"):
        if k in s:
            out[k] = s[k]
    missing = [f for f in asked if not any(g in s.get("fields", []) for g in FIELDS[f])]
    if missing:
        out["unavailable"] = missing
    return out


def tool_find_npc(args):
    """Database only: never asks the game."""
    if not os.path.exists(common.DB_PATH):
        raise common.GameError("no game database yet: run `python3 -m agent.update_data`")
    near = args.get("near")
    state = {"map": int(near["map_id"]), "x": float(near["x"]) / 100, "y": float(near["y"]) / 100} if near else None
    db = gamedb.connect(common.DB_PATH)
    try:
        if not (args.get("query") or args.get("role")):
            raise common.GameError("find_npc needs a role or query words")
        try:
            found = gamedb.find_npc(db, args.get("query", ""), state, args.get("faction"), int(args.get("limit") or 5),
                                    role=args.get("role"))
        except ValueError as e:
            raise common.GameError(str(e))
    finally:
        db.close()
    out = {"results": found, "source": "local database (QuestieDB Forever data)"}
    if not found:
        out["note"] = "nothing in the local database matches; try other words, or look it up on the web"
    elif not near:
        out["note"] = "no position given, so these aren't sorted by distance"
    return out


def tool_set_waypoint(args):
    reply = common.game("way", int(args["map_id"]), "%.2f" % float(args["x"]), "%.2f" % float(args["y"]))
    if not reply.get("ok"):
        raise common.GameError(reply.get("reason") or reply.get("status") or "the game refused the waypoint")
    return {"done": True, "waypoint_id": reply.get("waypoint"), "via": reply.get("via")}


def tool_send_chat(args):
    channel, text, to = args["channel"], args["text"], (args.get("to") or "").strip()
    if channel in ("whisper", "bnet") and not to:
        raise common.GameError("%s needs `to`" % channel)
    if len(text.encode("utf-8")) > 255:
        raise common.GameError("the message is over 255 bytes: shorten it")
    reply = common.game("chat", channel, to if channel in ("whisper", "bnet") else "", text)
    if not reply.get("ok"):
        raise common.GameError(reply.get("reason") or reply.get("error") or reply.get("status") or "not sent")
    return {"result": reply.get("result"), "channel": channel, "to": to or None, "text": text}


def tool_get_online(args):
    reply = common.game("friends", *(args.get("lists") or []))
    if not reply.get("ok"):
        raise common.GameError(reply.get("reason") or reply.get("error") or "the game said no")
    people = reply.get("people", [])
    try:  # who plays which character, for next time (memory.py)
        with memory.connect() as db:
            memory.record_online(db, people)
    except Exception as e:
        print("memory: couldn't record characters: %s" % e, file=sys.stderr)
    return {"people": people, "more": reply.get("more", False)}


def tool_ask_player(args):
    seconds = int(args.get("seconds") or 45)
    cmd = ["prompt"] + (["--list"] if args.get("list") else []) + ["--seconds", str(seconds), args["question"]]
    reply = common.game(*(cmd + list(args["options"])), timeout=seconds + 200)
    if not reply.get("ok"):
        raise common.GameError(reply.get("reason") or reply.get("error") or reply.get("status") or "couldn't ask")
    out = {"answer": reply.get("answer")}
    if reply.get("answer") is None:
        out["why"] = "cancelled" if reply.get("choice") == 0 else "no answer in time"
    if reply.get("shown") == "queued":
        out["note"] = "he was in combat, so it showed after the fight"
    return out


NOTES_HEADINGS = {"people": "## People", "rules": "## Ben's rules"}


def tool_remember(args):
    """Adds the note as a bullet at the end of its section (made if missing), keeping the rest as is."""
    heading = NOTES_HEADINGS[args["section"]]
    note = " ".join(args["note"].split())
    path = os.path.join(common.HOME, "CLAUDE.md")
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        lines = ["# DeckAgent's notes"]
    if heading not in lines:
        lines += ["", heading]
    start = lines.index(heading) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    while end > start and not lines[end - 1].strip():
        end -= 1
    lines.insert(end, "- " + note)
    with open(path + ".tmp", "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(path + ".tmp", path)
    return {"saved": True, "section": args["section"], "note": note}


def tool_remember_fact(args):
    with memory.connect() as db:
        fid = memory.add_fact(db, args["about"], args["fact"], args.get("replaces"))
    return {"saved": True, "id": fid}


def tool_recall(args):
    if not (args.get("query") or "").strip() and not args.get("about"):
        raise ValueError("recall needs a query or an about")
    with memory.connect() as db:
        found = memory.recall(db, args.get("query") or "", args.get("about"), int(args.get("limit") or 8))
    return {"facts": [{"id": f["id"], "about": f["about"], "fact": f["fact"], "saved": memory.day(f["created"])}
                      for f in found]}


def tool_forget_fact(args):
    with memory.connect() as db:
        if not memory.forget(db, int(args["id"])):
            raise ValueError("there's no fact #%s" % args["id"])
    return {"forgotten": int(args["id"])}


HANDLERS = {"get_player_state": tool_get_player_state, "find_npc": tool_find_npc, "set_waypoint": tool_set_waypoint,
            "send_chat": tool_send_chat, "get_online": tool_get_online, "ask_player": tool_ask_player,
            "remember": tool_remember, "remember_fact": tool_remember_fact, "recall": tool_recall,
            "forget_fact": tool_forget_fact}


def handle(msg):
    """One JSON-RPC message -> the response dict, or None for notifications."""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None  # notifications (initialized, cancelled...) need no answer
    if method == "initialize":
        asked = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
        result = {"protocolVersion": asked, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "deckagent", "version": "0.1"},
                  "instructions": "Ben's game (WoW: Forever on a Steam Deck). These tools are the only way "
                                  "to see or change anything in the game."}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = msg.get("params") or {}
        fn = HANDLERS.get(params.get("name"))
        if not fn:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}}
        try:
            text, is_error = json.dumps(fn(params.get("arguments") or {})), False
        except common.GameError as e:
            text, is_error = str(e), True
        except Exception as e:  # a bug here shouldn't kill the session
            traceback.print_exc(file=sys.stderr)
            text, is_error = "internal error: %s" % e, True
        result = {"content": [{"type": "text", "text": text}], "isError": is_error}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main(stdin=sys.stdin, stdout=sys.stdout):
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        reply = handle(msg)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


if __name__ == "__main__":
    main()
