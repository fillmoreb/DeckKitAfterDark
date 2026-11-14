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

There is deliberately no way to run Lua, send slash commands or touch combat, movement, targeting or
casting: only these named requests, answered by addons/DeckAgent in the game.
"""
import json, os, sys, traceback

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent import common, gamedb  # noqa: E402

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


HANDLERS = {"get_player_state": tool_get_player_state, "find_npc": tool_find_npc, "set_waypoint": tool_set_waypoint}


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
