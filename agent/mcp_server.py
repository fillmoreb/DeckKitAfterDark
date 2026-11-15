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
  get_waypoints(near)                 (game + database) his waypoints: zone, x, y, which is active, distance
  remove_waypoints(which, ids)        (game) remove the active one, all, or some by id
  send_chat(channel, text, to)        (game) say / yell / party / raid / guild / instance, or a whisper
                                      to a character or a Battle.net friend
  get_online(lists)                   (game) who's online: Battle.net friends (and their character),
                                      character friends, guild
  ask_player(question, options, ...)  (game) ask Ben with a prompt (yes/no dialog or a list) and wait
                                      for his answer
  remember(section, note)             (its notes) one line under People or Ben's rules in CLAUDE.md
  find_quests(place, name, ...)       (database + game) quests for a dungeon/zone or by name, with his status
  get_quests(near)                    (game + database) his quest log: names, objectives with his progress,
                                      what drops the items, who takes each quest in
  get_character()                     (game + database) his gear with exact stats, his stats, money, bags
  get_bags(bank)                      (game + database) what he carries, or had in the bank
  get_item_info(item_ids)             (game, remembered) exact stats of items: rewards, drops, upgrades
  find_items(name)                    (database) item IDs by name words
  flag_items(item_ids, label)         (game) a lilac star on those items in his bags (PackPad), and how many he has
  clear_flags(label)                  (game) takes flags off (one label, or all)
  get_prices(item_ids)                (game) auction, vendor and disenchant prices (DeckKit's MarketDeck)
  set_hook(when, note, action, ...)   (game or timer) "tell me when...": wakes her (or says a reminder)
  list_hooks() / remove_hook(id)      her hooks
  track_checklist(title, items)       (game) a checklist that stays up and ticks itself off from the game
  show_layout(layout, wait)           (game) a window built from pieces (DeckKit layouts): items, quests,
                                      checks, progress, waypoints, reply buttons; can wait for his pick
  clear_checklist()                   (game) takes it down
  respond(say, display, ...)          (game) her answer, always last: what's spoken, and the one display
                                      that fits (none, text card, list, checklist, reading view); the
                                      only way she puts a card or reading view up

There is deliberately no way to run Lua, send slash commands or touch combat, movement, targeting or
casting: only these named requests, answered by addons/DeckAgent in the game.
"""
import json, os, re, sys, traceback

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent import common, gamedb, items, memory  # noqa: E402
import time  # noqa: E402

PROTOCOL = "2025-06-18"

# A layout node (addons/DeckKit/Layout.lua's header has the full vocabulary), nested a few levels deep
# explicitly rather than by $ref.
LAYOUT_TYPES = ["list", "section", "row", "text", "heading", "item", "quest", "check", "progress", "waypoint",
                "icon", "button", "space"]


def _layout_node(depth):
    node = {"type": "object", "properties": {"type": {"type": "string", "enum": LAYOUT_TYPES}},
            "required": ["type"]}
    if depth > 0:
        child = _layout_node(depth - 1)
        node["properties"]["items"] = {"type": "array", "maxItems": 30, "items": child}
        node["properties"]["footer"] = child
    return node


LAYOUT_NODE = _layout_node(4)

TOOLS = [
    {"name": "get_player_state",
     "description": "Ask the game about the player. `fields` picks what to fetch (default all): "
                    "'position' (map_id and x/y 0-100), 'zone' (zone name), 'who' (faction H/A, level, class), "
                    "'combat' (in combat or not), 'target' (what he has targeted: name, level, elite/rare, "
                    "hostile, health, quest items it drops). Ask only for what the question needs: for 'nearest X' "
                    "that's position and who (for the faction).",
     "inputSchema": {"type": "object", "properties": {
         "fields": {"type": "array", "items": {"type": "string", "enum": ["position", "zone", "who", "combat",
                                                                         "target"]},
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
    {"name": "get_waypoints",
     "description": "Ben's waypoints (WayPad): each one's id, zone, x and y (0-100), which is active (the "
                    "one his arrow points to), and the distance from `near` (his position) in yards when "
                    "it's on his continent. Use it before removing some, to find which.",
     "inputSchema": {"type": "object", "properties": {
         "near": {"type": "object", "description": "his position: map_id and x, y (0-100)",
                  "properties": {"map_id": {"type": "integer"}, "x": {"type": "number"}, "y": {"type": "number"}},
                  "required": ["map_id", "x", "y"], "additionalProperties": False}},
         "additionalProperties": False}},
    {"name": "remove_waypoints",
     "description": "Remove waypoints: `which` 'active' (the one his arrow points to), 'all', or 'ids' "
                    "(those in `ids`, from get_waypoints or set_waypoint). Returns how many went and what's "
                    "left. When he asks, do it; don't ask first.",
     "inputSchema": {"type": "object", "properties": {
         "which": {"type": "string", "enum": ["active", "all", "ids"]},
         "ids": {"type": "array", "items": {"type": "integer", "minimum": 0}, "maxItems": 50}},
         "required": ["which"], "additionalProperties": False}},
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
    {"name": "get_quests",
     "description": "Ben's quest log, right now: each quest's name, level and summary, whether it's complete "
                    "(ready to turn in) or failed, and its objectives with his progress (what, have/need, done; "
                    "for items, who drops or sells them), plus turned_in_at: every NPC or object that takes it in, "
                    "with where (like find_quests'). The game sends only IDs and counts; the names come from "
                    "the local database (QuestieDB Forever). Pass his position as `near` to get each one's "
                    "nearest spawn and distance, nearest first. Use it for "
                    "anything about his quests: what he still needs, what's done, where to turn in.",
     "inputSchema": {"type": "object", "properties": {
         "near": {"type": "object", "description": "his position: map_id and x, y (0-100)",
                  "properties": {"map_id": {"type": "integer"}, "x": {"type": "number"}, "y": {"type": "number"}},
                  "required": ["map_id", "x", "y"], "additionalProperties": False}},
         "additionalProperties": False}},
    {"name": "find_quests",
     "description": "Quests from the local database (QuestieDB Forever, corrected): those filed under a "
                    "dungeon or zone (`place`: 'Ragefire Chasm', 'Silverpine'), and/or with words in their "
                    "name (`name`). Each comes with level, summary, who gives it and where it's turned in "
                    "(each at its spawn nearest `near`; some exist in two places, and chains can cross zones), "
                    "prerequisites, and Ben's status from the game: 'not taken', 'in log', "
                    "'ready to turn in' or 'done'. Use it for 'which quests are there for X', 'what can I "
                    "still pick up', 'where do I get Y': before the web. Pass `faction` to leave out the "
                    "other faction's quests.",
     "inputSchema": {"type": "object", "properties": {
         "place": {"type": "string", "description": "dungeon or zone name words"},
         "name": {"type": "string", "description": "words in the quest's name"},
         "faction": {"type": "string", "enum": ["A", "H"]},
         "near": {"type": "object", "description": "his position: map_id and x, y (0-100)",
                  "properties": {"map_id": {"type": "integer"}, "x": {"type": "number"}, "y": {"type": "number"}},
                  "required": ["map_id", "x", "y"], "additionalProperties": False}},
         "additionalProperties": False}},
    {"name": "get_character",
     "description": "Ben's character sheet: each equipped item (slot, name, quality, item level and exact "
                    "stats as the game computes them, durability), his stats with gear (strength ... spirit, "
                    "armor, spell power, healing, max health and mana), money, free bag slots, spec ID, and "
                    "which slots need repair. Use it for upgrades ('is this better than what I have?'), "
                    "repairs, 'what should I enchant'.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_bags",
     "description": "What Ben carries: item names and counts (stacks added up), free slots, and with "
                    "MarketDeck each item's prices (auction now and market, vendor, disenchant, best way to "
                    "sell). bank: true "
                    "gives the bank instead, as it was the last time he opened it (it can't be read "
                    "otherwise). For an item's stats, get_item_info.",
     "inputSchema": {"type": "object", "properties": {"bank": {"type": "boolean"}}, "additionalProperties": False}},
    {"name": "find_items",
     "description": "Items in the local database (QuestieDB Forever) by words in their name: id and name, "
                    "exact matches first. Use it to turn item names (a recipe's reagents, a quest item) into "
                    "the IDs flag_items, get_prices and get_item_info take.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string", "minLength": 2},
         "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8}},
         "required": ["name"], "additionalProperties": False}},
    {"name": "flag_items",
     "description": "Flag items in Ben's bags: every stack of them gets a lilac star in its slot (PackPad), and "
                    "its tooltip says `label` ('Red Linen Bag'). Flags stay until cleared. Returns how many of "
                    "each he carries, so you can say what he still needs. A new call with the same label "
                    "replaces that set; different labels coexist.",
     "inputSchema": {"type": "object", "properties": {
         "item_ids": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 1, "maxItems": 40},
         "label": {"type": "string", "minLength": 1, "maxLength": 60}},
         "required": ["item_ids", "label"], "additionalProperties": False}},
    {"name": "clear_flags",
     "description": "Take flags off Ben's items: one `label`'s set, or all of yours with no label.",
     "inputSchema": {"type": "object", "properties": {"label": {"type": "string", "maxLength": 60}},
                     "additionalProperties": False}},
    {"name": "get_prices",
     "description": "What items are worth, from DeckKit's MarketDeck: the auction house's lowest buyout now "
                    "(and how many are up), the two-week market price, the vendor price, the estimated "
                    "disenchant value, and the best way to sell each (the auction house's 5% cut counted). "
                    "Prices are from his own auction house snapshots, so they're as fresh as his last visit. "
                    "get_bags already includes these for what he carries.",
     "inputSchema": {"type": "object", "properties": {
         "item_ids": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 1, "maxItems": 60}},
         "required": ["item_ids"], "additionalProperties": False}},
    {"name": "get_item_info",
     "description": "Exact stats of items by ID, as the game computes them (quality, item level, required "
                    "level, slot, armor, DPS, stats): for quest rewards, drops, comparing with his gear. Up "
                    "to 30. Answers are remembered; an item the game hasn't seen is fetched from Blizzard's "
                    "server, paced, so it may say 'still loading': ask again a moment later. Ask only for "
                    "items you need, with IDs from the tools (never guessed).",
     "inputSchema": {"type": "object", "properties": {
         "item_ids": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 1, "maxItems": 30}},
         "required": ["item_ids"], "additionalProperties": False}},
    {"name": "set_hook",
     "description": "Be told when something happens, so you can act on it later without Ben asking: a "
                    "reminder ('remind me at level 20 to train', 'in 20 minutes check the auction') or the "
                    "next step of a plan ('when he takes quest X, set the waypoint to the next giver'). "
                    "`when`: what fires it: `track` quest_taken / quest_ready / quest_turned_in (quest_id), "
                    "objective (quest_id + objective), item_count (item_id + count), level, zone (map_id), "
                    "xp (XP gained from now), or timer (minutes, 1-1440); `not` turns any but timer around "
                    "(leaving a zone, dropping an item, abandoning a quest). `action`: say (when it fires, your "
                    "`note` is spoken to him as is: write it as the reminder he should hear) or wake (you're "
                    "woken with the event and your `note`: write it as instructions to yourself, with every "
                    "ID and detail you'll need, since you may not remember this conversation). It fires "
                    "once; game hooks expire after `expires_minutes` (default 240). Use real IDs from "
                    "get_quests / find_quests / find_npc. If the condition is already true it isn't set: "
                    "act now instead.",
     "inputSchema": {"type": "object", "properties": {
         "when": {"type": "object", "properties": {
             "track": {"type": "string", "enum": ["quest_taken", "quest_ready", "quest_turned_in", "objective",
                                                  "item_count", "level", "zone", "xp", "timer"]},
             "quest_id": {"type": "integer"}, "objective": {"type": "integer", "minimum": 1},
             "item_id": {"type": "integer"}, "count": {"type": "integer", "minimum": 1},
             "level": {"type": "integer", "minimum": 1, "maximum": 255}, "map_id": {"type": "integer"},
             "xp": {"type": "integer", "minimum": 1}, "minutes": {"type": "number", "minimum": 1, "maximum": 1440},
             "not": {"type": "boolean", "description": "turn it around: fires when it stops being true ('left "
                                                       "Orgrimmar' = zone 1454 with not; 'no longer carrying "
                                                       "it' = item_count with not)"}},
             "required": ["track"], "additionalProperties": False},
         "note": {"type": "string", "minLength": 1, "maxLength": 600},
         "action": {"type": "string", "enum": ["say", "wake"]},
         "expires_minutes": {"type": "integer", "minimum": 1, "maximum": 10080}},
         "required": ["when", "note", "action"], "additionalProperties": False}},
    {"name": "list_hooks",
     "description": "Your hooks that haven't fired yet: id, what fires them, your note, what they do, time left.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "remove_hook",
     "description": "Remove one hook by its id (from set_hook or list_hooks), or all of them with id 'all'.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string", "minLength": 1}},
                     "required": ["id"], "additionalProperties": False}},
    {"name": "track_checklist",
     "description": "Put up a checklist that STAYS on Ben's screen and ticks itself off as he plays (no "
                    "need to ask you again): for a plan he'll work through over time, like the quests to "
                    "gather before a dungeon, a prep list, or a route of quest givers. It replaces any list "
                    "up, survives a reload, and fades a minute after everything's done. Each item: a short "
                    "`label` and what ticks it (`track`): quest_taken / quest_ready / quest_turned_in "
                    "(quest_id), objective (quest_id + objective, 1-based, in the quest log's order), "
                    "item_count (item_id + count), level, zone (map_id: in it or a place inside it), xp "
                    "(XP gained from now), or manual (`done` as given; nothing ticks it). Use real IDs from "
                    "get_quests / find_quests. Returns which are already done. For a one-off look ('what "
                    "do I still need?') use respond's checklist display instead.",
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string", "maxLength": 60},
         "items": {"type": "array", "minItems": 1, "maxItems": 12, "items": {
             "type": "object", "properties": {
                 "label": {"type": "string", "minLength": 1, "maxLength": 80},
                 "track": {"type": "string", "enum": ["quest_taken", "quest_ready", "quest_turned_in", "objective",
                                                      "item_count", "level", "zone", "xp", "manual"]},
                 "quest_id": {"type": "integer"}, "objective": {"type": "integer", "minimum": 1},
                 "item_id": {"type": "integer"}, "count": {"type": "integer", "minimum": 1},
                 "level": {"type": "integer", "minimum": 1, "maximum": 255}, "map_id": {"type": "integer"},
                 "xp": {"type": "integer", "minimum": 1}, "done": {"type": "boolean"}},
             "required": ["label", "track"], "additionalProperties": False}}},
         "required": ["title", "items"], "additionalProperties": False}},
    {"name": "show_layout",
     "description": "Put a window on Ben's screen built from pieces (DeckKit layouts): for answers worth "
                    "looking at and acting on with the pad, richer than respond's displays. Real IDs make "
                    "real pieces: the game fills in names, icons, quality colors, tooltips and live state.\n"
                    "Containers (with `items`): list (one under another), section (`title` heading, then "
                    "items), row (side by side on one line).\n"
                    "Pieces: text (`text`, `style`: body/dim/small/heading/good/bad/gold), heading (`text`), "
                    "item (`id`, `count`), quest (`id`, `text` = its name), check (`bind`: ticks itself from "
                    "the game; or `toggle`: true for one Ben ticks), progress (`bind` an item count, `text`), "
                    "waypoint (`map`, `x`, `y` 0-100, `label`: A sets it as his waypoint, holding Y shows "
                    "the map), button (`text`, `buttons`: {\"a\": {\"reply\": \"value\", \"label\": "
                    "\"Pick\"}}), space (`width`; none: pushes the rest of a row right).\n"
                    "bind: {quest, state: taken/ready/done} | {quest, objective} | {item, count} | {level} | "
                    "{zone}.\n"
                    "The top node may have `footer` (a row kept in view under the scrolling part: put "
                    "reply buttons there), `title` (the tab label), `id` (a short name: Ben's own ticks are "
                    "kept per id) and `width` (360-900, default 560).\n"
                    "Typical: a list of rows [item, check bound to {item, count}] for a shopping list; "
                    "rows [quest, check bound to the quest] for quests to pick up; waypoints for places; "
                    "reply buttons when you need him to pick (then wait: seconds to wait for it, and you "
                    "get answer: the reply's value, or null if he closed it). After showing one, respond "
                    "with display none and say it's on screen. Keep it to what fits one screen.",
     "inputSchema": {"type": "object", "properties": {
         "layout": LAYOUT_NODE,
         "wait": {"type": "integer", "minimum": 0, "maximum": 240, "default": 0}},
         "required": ["layout"], "additionalProperties": False}},
    {"name": "clear_checklist",
     "description": "Take down the checklist you put up with track_checklist (when he's done with it or "
                    "asks).",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "respond",
     "description": "Your answer to Ben. ALWAYS finish by calling this, exactly once, after any other "
                    "tools; write nothing after it. `say` is all he hears: one or two short sentences, at "
                    "most 160 characters, no lists, no coordinates unless they're the whole answer. "
                    "`display` is required: pick what fits the answer.\n"
                    "- none: a fact, a yes/no, something you did (a waypoint you set).\n"
                    "- text: a short explanation or tip, 1-4 sentences, in `text`.\n"
                    "- list: several separate things (names, places, options), in `items`, one short "
                    "line each.\n"
                    "- checklist: things he has to get or do (quests to pick up, objectives, items), in "
                    "`items` with `done` true/false on each from his real progress.\n"
                    "- reading: long text (a walkthrough, a guide, a summary of a web page), in `text`; "
                    "'# ' headings and '- ' bullets allowed.\n"
                    "With a display, `say` gives the gist and that it's on screen ('Two quests left to "
                    "pick up, both in Thunder Bluff; they're on screen.'). `note`: one short line under a "
                    "card (a source, a caveat). `heard`: if you read misheard words as something else, "
                    "how ('\"rage fire\" is Ragefire Chasm'): it goes to his chat, never spoken.",
     "inputSchema": {"type": "object", "properties": {
         "say": {"type": "string", "minLength": 1, "maxLength": 160},
         "display": {"type": "string", "enum": ["none", "text", "list", "checklist", "reading"]},
         "title": {"type": "string", "maxLength": 60},
         "text": {"type": "string", "maxLength": 3500},
         "items": {"type": "array", "maxItems": 12, "items": {
             "type": "object", "properties": {
                 "text": {"type": "string", "minLength": 1, "maxLength": 80},
                 "done": {"type": "boolean"}},
             "required": ["text"], "additionalProperties": False}},
         "note": {"type": "string", "maxLength": 120},
         "heard": {"type": "string", "maxLength": 160}},
         "required": ["say", "display"], "additionalProperties": False}},
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
FIELDS = {"position": ["pos", "world"], "zone": ["zone"], "who": ["who"], "combat": ["combat"], "target": ["target"]}


def target_line(t, db_path=None):
    """The game's target field with its name from the database: a dict, or None."""
    if not t:
        return None
    out = dict(t)
    if t.get("npc_id") and os.path.exists(db_path or common.DB_PATH):
        db = gamedb.connect(db_path or common.DB_PATH)
        try:
            r = db.execute("SELECT name, title FROM npc WHERE id = ?", (t["npc_id"],)).fetchone()
            if r:
                out["name"], out["title"] = r["name"], r["title"]
            drops = [d["name"] for d in db.execute(
                "SELECT i.name FROM item_source s JOIN item i ON i.id = s.item WHERE s.kind = 'npc' AND s.id = ? "
                "LIMIT 6", (t["npc_id"],))]
            if drops:
                out["quest_items_it_drops"] = drops
        finally:
            db.close()
    return out


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
    if "target" in asked and "target" in s:
        out["target"] = target_line(s["target"]) or "none"
    missing = [f for f in asked if not any(g in s.get("fields", []) for g in FIELDS[f])]
    if missing:
        out["unavailable"] = missing
    return out


def _money(copper):
    g, rest = divmod(int(copper or 0), 10000)
    return "%dg %ds %dc" % (g, rest // 100, rest % 100)


def _names(ids):
    """{item ID: name} from the game database (QuestieDB's Forever items)."""
    if not ids or not os.path.exists(common.DB_PATH):
        return {}
    db = gamedb.connect(common.DB_PATH)
    try:
        q = ",".join("?" * len(ids))
        return {r["id"]: r["name"] for r in db.execute("SELECT id, name FROM item WHERE id IN (%s)" % q, list(ids))}
    finally:
        db.close()


def lookup_items(pairs, known=None, sleep=time.sleep, tries=8):
    """[(item ID, suffix)] -> {(id, suffix): info}: remembered answers first (items.db), then the game,
    for IDs the database knows only (`known`: {id: name}; None skips the check, for items the game
    itself named). Items still loading after about 8 seconds come back as such."""
    out, want = {}, []
    idb = items.connect()
    try:
        for key in pairs:
            hit = items.get(idb, *key)
            if hit:
                out[key] = hit
            elif known is not None and key[0] not in known:
                out[key] = {"item_id": key[0], "status": "not in the database (not asked: it may not exist)"}
            else:
                want.append(key)
        for attempt in range(tries):
            if not want:
                break
            batch = want[:30]
            reply = common.game("item-info", *["%d:%d" % k for k in batch])
            if not reply.get("ok"):
                break
            loading = False
            for key, e in zip(batch, reply.get("items", [])):
                if e["status"] == "ok":
                    items.put(idb, key[0], key[1], e)
                    out[key] = e
                elif e["status"] == "loading":
                    loading = True
                else:
                    out[key] = e
            want = [k for k in want if k not in out]
            if loading and attempt < tries - 1:
                sleep(1.0)
        for key in want:
            out[key] = {"item_id": key[0], "status": "still loading from the server: ask again in a moment"}
    finally:
        idb.close()
    return out


def tool_get_character(args):
    c = common.game("character")
    if not c.get("ok"):
        raise common.GameError(c.get("reason") or c.get("error") or "the game said no")
    pairs = [(e["item_id"], e["suffix_id"] or 0) for e in c["equipped"]]
    names = _names([p[0] for p in pairs])
    info = lookup_items(pairs)  # equipped items are in the game's cache: no server requests
    gear = []
    for e, key in zip(c["equipped"], pairs):
        i = info.get(key, {})
        g = {"slot": e["slot"], "item_id": e["item_id"], "name": names.get(e["item_id"]), "quality": i.get("quality"),
             "item_level": i.get("item_level"), "stats": i.get("stats")}
        if e["durability_pct"] is not None:
            g["durability_pct"] = e["durability_pct"]
        if e["enchant_id"]:
            g["enchanted"] = True
        if e["suffix_id"]:
            g["suffix_id"] = e["suffix_id"]
        gear.append(g)
    worn = [g["slot"] for g in gear if g.get("durability_pct") is not None and g["durability_pct"] < 25]
    out = {"level": c["level"], "spec_id": c["spec_id"], "money": _money(c["money_copper"]), "stats": c["stats"],
           "bags": "%d of %d slots free" % (c["bag_free"], c["bag_slots"]), "equipped": gear}
    if worn:
        out["needs_repair"] = worn
    return out


def price_view(p):
    """A PRICES entry in words: auction now / market, vendor, disenchant, and the best way to sell it."""
    out = {}
    if p.get("auction_now"):
        out["auction_now"] = _money(p["auction_now"]) + " (%d up)" % p["quantity_up"]
    if p.get("market") and p.get("days_of_history", 0) >= 2:
        out["auction_market"] = "%s (%d days)" % (_money(p["market"]), p["days_of_history"])
    if p.get("vendor"):
        out["vendor"] = _money(p["vendor"])
    if p.get("disenchant"):
        out["disenchant"] = "~" + _money(p["disenchant"])
    ah = p.get("market") or p.get("auction_now") or 0
    ah = int(ah * 0.95)  # the auction house keeps 5% of a sale
    options = {"auction house": ah, "vendor": p.get("vendor") or 0, "disenchant": p.get("disenchant") or 0}
    best = max(options, key=options.get)
    if options[best]:
        out["best"] = best
    return out


def _prices(ids):
    """The game's prices (MarketDeck), asked once: ({item ID: price_view}, how it went: {"ok", "reason"
    when not, "installed": MarketDeck there, "scan_hours": each answer's hours since its auction house
    scan}). get_prices and get_bags both use it."""
    views, meta = {}, {"ok": True, "installed": True, "scan_hours": []}
    ids = list(ids)
    while ids:
        batch, ids = ids[:60], ids[60:]
        r = common.game("prices", *batch)
        if not r.get("ok"):
            meta.update(ok=False, reason=r.get("reason") or r.get("error") or "the game said no")
            break
        if not r.get("market_installed"):
            meta["installed"] = False
            break
        got = r.get("prices", [])
        for p in got:
            views[p["item_id"]] = price_view(p)
            if p.get("hours_since_scan") is not None:
                meta["scan_hours"].append(p["hours_since_scan"])
        if len(got) < len(batch):  # cut at one strip frame: ask again for the rest
            ids = batch[len(got):] + ids
            if not got:
                break
    return views, meta


def tool_find_items(args):
    if not os.path.exists(common.DB_PATH):
        raise common.GameError("no game database yet: run deckagent-update")
    words = [w for w in args["name"].lower().split() if w]
    limit = int(args.get("limit") or 8)
    db = gamedb.connect(common.DB_PATH)
    try:
        where = " AND ".join("lower(name) LIKE ?" for _ in words)
        rows = db.execute("SELECT id, name FROM item WHERE " + where + " ORDER BY lower(name) = ? DESC, length(name), name "
                          "LIMIT ?", ["%" + w + "%" for w in words] + [args["name"].lower(), limit]).fetchall()
    finally:
        db.close()
    return {"items": [{"item_id": r["id"], "name": r["name"]} for r in rows]}


def tool_flag_items(args):
    ids = [int(i) for i in args["item_ids"]][:40]
    reply = common.game("mark", args["label"], *ids)
    if not reply.get("ok"):
        raise common.GameError(reply.get("reason") or reply.get("error") or "the game said no")
    if not reply.get("packpad_installed"):
        return {"flagged": False, "note": "flags need DeckKit's PackPad addon (it isn't installed)"}
    names = _names(ids)
    carried = reply.get("carried", [])
    items = [{"item_id": i, "name": names.get(i), "carried": carried[k] if k < len(carried) else None}
             for k, i in enumerate(ids)]
    return {"flagged": True, "label": args["label"], "items": items,
            "missing": [it["name"] or it["item_id"] for it in items if not it["carried"]]}


def tool_clear_flags(args):
    label = (args.get("label") or "").strip()
    reply = common.game("unmark", *([label] if label else []))
    if not reply.get("ok"):
        raise common.GameError(reply.get("reason") or reply.get("error") or "the game said no")
    return {"cleared": label or "all"}


def tool_get_prices(args):
    ids = [int(i) for i in args["item_ids"]][:60]
    views, meta = _prices(ids)
    if not meta["ok"]:
        raise common.GameError(meta["reason"])
    if not meta["installed"]:
        return {"note": "auction prices need DeckKit's MarketDeck addon (it isn't installed); vendor prices only via "
                        "get_item_info"}
    names = _names(ids)
    scans = meta["scan_hours"]
    out = {"items": [dict({"item_id": i, "name": names.get(i)}, **views.get(i, {})) for i in ids]}
    if not scans:
        out["note"] = "no auction snapshot yet: prices fill in after he opens the auction house once"
    elif min(scans) > 48:
        out["note"] = "the newest auction snapshot is %d hours old" % min(scans)
    return out


def tool_get_bags(args):
    bank = bool(args.get("bank"))
    b = common.game("bags", *(["bank"] if bank else []))
    if not b.get("ok"):
        raise common.GameError(b.get("reason") or b.get("error") or "the game said no")
    if b.get("bank_never_seen"):
        return {"bank": "not seen yet: it's only readable while he has it open, so ask him to visit the bank once"}
    ids = [i["item_id"] for i in b["items"]]
    names = _names(ids)
    prices, _ = _prices(ids)  # with MarketDeck: auction, vendor, disenchant and the best way to sell each
    out = {"items": [dict({"item_id": i["item_id"], "name": names.get(i["item_id"]), "count": i["count"]},
                          **prices.get(i["item_id"], {})) for i in b["items"]],
           "free_slots": b["free"], "slots": b["slots"]}
    if bank:
        out["as_of_minutes_ago"] = b["minutes_old"]
    if b.get("more"):
        out["note"] = "more items than fit in one reply: the list is cut short"
    return out


def tool_get_item_info(args):
    ids = [int(i) for i in args["item_ids"]][:30]
    names = _names(ids)
    info = lookup_items([(i, 0) for i in ids], known=names if os.path.exists(common.DB_PATH) else None)
    return {"items": [dict(info[(i, 0)], name=names.get(i)) for i in ids]}


def tool_find_npc(args):
    """Database only: never asks the game."""
    if not os.path.exists(common.DB_PATH):
        raise common.GameError("no game database yet: run `python3 -m agent.update_data`")
    near = args.get("near")
    state = gamedb.near_state(near)
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


def _waypoint_list(points, state=None):
    """The game's waypoints (map, x, y 0-100) with zone names and distance from `state`."""
    db = gamedb.connect(common.DB_PATH) if os.path.exists(common.DB_PATH) else None
    try:
        here = gamedb.player_world(db, state) if (db and state) else None
        rects = gamedb.map_rects(db) if db else {}
        out = []
        for p in points:
            w = {"id": p["id"], "x": p["x"], "y": p["y"], "map_id": p["map"], "active": p["active"]}
            r = db.execute("SELECT name FROM map WHERE id = ?", (p["map"],)).fetchone() if db else None
            w["zone"] = r["name"] if r else None
            if here and p["map"] in rects:
                cont, wx, wy = gamedb.to_world(rects[p["map"]], p["x"] / 100, p["y"] / 100)
                if cont == here[0]:
                    w["distance_yards"] = round(((wx - here[1]) ** 2 + (wy - here[2]) ** 2) ** 0.5)
            out.append(w)
        return out
    finally:
        if db:
            db.close()


def tool_get_waypoints(args):
    reply = common.game("waypoints")
    if not reply.get("ok"):
        raise common.GameError(reply.get("reason") or reply.get("error") or "the game said no")
    state = gamedb.near_state(args.get("near"))
    return {"waypoints": _waypoint_list(reply.get("waypoints", []), state)}


def tool_remove_waypoints(args):
    which = args["which"]
    if which == "ids":
        ids = args.get("ids") or []
        if not ids:
            raise common.GameError("which 'ids' needs `ids`")
        removed, missing, reply = 0, [], None
        for wid in ids:
            reply = common.game("waypoints", "remove", wid)
            if reply.get("ok"):
                removed += reply.get("removed", 0)
            else:
                missing.append(wid)
        if reply is None or (not reply.get("ok") and not removed):
            reply = common.game("waypoints")
        out = {"removed": removed, "left": _waypoint_list(reply.get("waypoints", []))}
        if missing:
            out["not_found"] = missing
        return out
    reply = common.game("waypoints", "remove-" + which)
    if not reply.get("ok"):
        raise common.GameError(reply.get("reason") or reply.get("error") or "the game said no")
    return {"removed": reply.get("removed", 0), "left": _waypoint_list(reply.get("waypoints", []))}


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


def tool_find_quests(args):
    if not (args.get("place") or args.get("name")):
        raise common.GameError("find_quests needs a place or name words")
    if not os.path.exists(common.DB_PATH):
        raise common.GameError("no game database yet: run deckagent-update")
    state = gamedb.near_state(args.get("near"))
    db = gamedb.connect(common.DB_PATH)
    try:
        found = gamedb.find_quests(db, args.get("place", ""), args.get("name", ""), args.get("faction"), state)
    finally:
        db.close()
    if not found:
        return {"quests": [], "note": "nothing in the local database matches; try other words"}
    try:  # his status for each: the game sends a number per quest ID
        reply = common.game("quest-status", *[q["id"] for q in found])
        status = {int(k): v for k, v in (reply.get("quests") or {}).items()} if reply.get("ok") else {}
    except common.GameError:
        status = {}
    for q in found:
        q["status"] = status.get(q["id"], "unknown (the game didn't say)")
    return {"quests": found}


def tool_get_quests(args):
    reply = common.game("quests")
    if not reply.get("ok"):
        raise common.GameError(reply.get("reason") or reply.get("error") or "the game said no")
    state = gamedb.near_state(args.get("near"))
    if not os.path.exists(common.DB_PATH):  # IDs and counts are still worth something
        return {"quests": reply["quests"], "note": "no game database yet, so no names: run agent.update_data"}
    db = gamedb.connect(common.DB_PATH)
    try:
        quests = gamedb.quest_log(db, reply["quests"], state)
    finally:
        db.close()
    out = {"quests": quests}
    if reply.get("more"):
        out["note"] = "the log was longer than one reply: some quests are missing"
    return out


SHOW_TEXT = {"card": 600, "reading": 3832}  # bytes; deck-setup/decklink/agent.py SHOW_TEXT (tested equal)


def _show(kind, title, text, seconds=None, item=None):
    """SHOW a card or reading view. Without `seconds` a card stays up as long as the host side's
    card_seconds says (deck-setup/decklink/agent.py), the same rule as the voice's cards."""
    limit = SHOW_TEXT[kind]
    if len(text.encode("utf-8")) > limit:
        raise common.GameError("the text is over %d bytes: shorten it" % limit)
    args = ["show", kind]
    if seconds is not None:
        args += ["--seconds", int(seconds)]
    if item:
        args += ["--item", int(item)]
    reply = common.game(*(args + [title, text]))
    if not reply.get("ok"):
        raise common.GameError(reply.get("reason") or reply.get("error") or reply.get("status") or "not shown")
    return {"result": reply.get("result")}


def card_text(items, note=None, text=None):
    """respond's text or items -> the card's markup (addons/DeckAgent/Show.lua): the paragraph, or
    "[x] " / "[ ] " checklist lines and "- " bullets; then the note."""
    lines = []
    if text and text.strip():
        # A paragraph: no line may look like list markup to the card.
        lines += [re.sub(r"^\s*(?:\[[ xX]?\]|[-*#]+)\s+", "", l) for l in text.strip().splitlines()]
    for it in items:
        line = " ".join(str(it.get("text", "")).split())
        if not line:
            continue
        done = it.get("done")
        lines.append(("[x] " if done else "[ ] ") + line if done is not None else "- " + line)
    if note and note.strip():
        lines += ["", " ".join(note.split())]
    return "\n".join(lines)


def describe(when):
    """A hook's condition in words, with names from the database ("Stepping Stones turned in")."""
    track = when.get("track")
    db = gamedb.connect(common.DB_PATH) if os.path.exists(common.DB_PATH) else None

    def name(table, i):
        r = db.execute("SELECT name FROM %s WHERE id = ?" % table, (i,)).fetchone() if db and i else None
        return r["name"] if r else "#%s" % i
    try:
        q = name("quest", when.get("quest_id")) if when.get("quest_id") else None
        if when.get("not"):
            return {
                "quest_taken": lambda: "%s no longer in the log" % q,
                "quest_ready": lambda: "%s no longer ready" % q,
                "quest_turned_in": lambda: "%s not turned in" % q,
                "objective": lambda: "%s objective %s no longer done" % (q, when.get("objective")),
                "item_count": lambda: "fewer than %s %s in bags" % (when.get("count"), name("item", when.get("item_id"))),
                "level": lambda: "below level %s" % when.get("level"),
                "zone": lambda: "left %s" % name("map", when.get("map_id")),
                "xp": lambda: "under %s XP gained" % when.get("xp"),
            }.get(track, lambda: "not " + (track or "something"))()
        return {
            "quest_taken": lambda: "%s taken" % q,
            "quest_ready": lambda: "%s ready to turn in" % q,
            "quest_turned_in": lambda: "%s turned in" % q,
            "objective": lambda: "%s objective %s done" % (q, when.get("objective")),
            "item_count": lambda: "%s of %s in bags" % (when.get("count"), name("item", when.get("item_id"))),
            "level": lambda: "level %s reached" % when.get("level"),
            "zone": lambda: "arrived in %s" % name("map", when.get("map_id")),
            "xp": lambda: "%s XP gained" % when.get("xp"),
            "timer": lambda: "%g minutes passed" % float(when.get("minutes") or 0),
        }.get(track, lambda: track or "something")()
    finally:
        if db:
            db.close()


def tool_set_hook(args):
    when = args["when"]
    if when.get("not") and when.get("track") == "timer":
        raise common.GameError("a timer can't be turned around")
    req = {"when": when, "note": args["note"], "action": args["action"], "desc": describe(when),
           "minutes": int(args.get("expires_minutes") or 0)}
    reply = common.game("hook", "add", json.dumps(req))
    if not reply.get("ok"):
        raise common.GameError(reply.get("reason") or reply.get("error") or "the game said no")
    if reply.get("already"):
        return {"set": False, "already_true": True, "what": req["desc"],
                "note": "that's already true right now: act on it now instead"}
    return {"set": True, "id": str(reply["id"]), "what": req["desc"], "action": args["action"]}


def tool_list_hooks(args):
    reply = common.game("hook", "list")
    if not reply.get("ok"):
        raise common.GameError(reply.get("error") or "couldn't list hooks")
    return {"hooks": reply.get("hooks", [])}


def tool_remove_hook(args):
    hid = args["id"].strip()
    reply = common.game("hook", "clear") if hid == "all" else common.game("hook", "remove", hid)
    if not reply.get("ok"):
        raise common.GameError(reply.get("error") or "no hook with id %s" % hid)
    return {"removed": hid}


def tool_show_layout(args):
    wait = int(args.get("wait") or 0)
    reply = common.game("layout", "--wait", wait, json.dumps(args["layout"], separators=(",", ":")),
                        timeout=wait + 90)
    if not reply.get("ok"):
        raise common.GameError(reply.get("reason") or reply.get("error") or reply.get("status") or "couldn't show it")
    out = {"shown": "after the fight (he was in combat)" if (reply.get("shown") or reply.get("result")) == "queued"
           else "on screen"}
    if "choice" in reply:
        out["answer"] = reply.get("answer")
        if reply.get("answer") is None:
            out["why"] = {0: "he closed it", 255: "replaced or no answer in time"}.get(reply.get("choice"), "no answer")
    return out


def tool_track_checklist(args):
    items = args["items"]
    reply = common.game("checklist", json.dumps({"title": args.get("title", ""), "items": items}))
    if not reply.get("ok"):
        raise common.GameError(reply.get("reason") or reply.get("error") or "the game said no")
    done = reply.get("done") or []
    return {"shown": True, "items": [{"label": it["label"], "done": bool(done[i]) if i < len(done) else None}
                                     for i, it in enumerate(items)]}


def tool_clear_checklist(args):
    reply = common.game("checklist", "clear")
    if not reply.get("ok"):
        raise common.GameError(reply.get("reason") or reply.get("error") or "the game said no")
    return {"cleared": True}


def tool_respond(args):
    """Shows the display (the spoken part is the launcher's: it reads `say` from this call)."""
    say, display = " ".join(args["say"].split()), args["display"]
    if len(say) > 160:
        raise common.GameError("`say` is over 160 characters: shorten it to one or two short sentences")
    title, text, items, note = args.get("title") or "", (args.get("text") or "").strip(), args.get("items") or [], args.get("note")
    if display == "none":
        return {"ok": True, "spoken": say}
    if display in ("text", "reading") and not text:
        raise common.GameError("display %s needs `text`" % display)
    if display in ("list", "checklist") and not items:
        raise common.GameError("display %s needs `items`" % display)
    if display == "checklist" and any("done" not in it for it in items):
        raise common.GameError("a checklist needs `done` (true or false) on every item")
    if display == "list":
        items = [{"text": it["text"]} for it in items]
    if display == "reading":
        result = _show("reading", title, text)
    else:
        body = card_text(items, note, text if display == "text" else None)
        if len(body.encode("utf-8")) > SHOW_TEXT["card"]:
            raise common.GameError("too much for a card: fewer or shorter items, or display reading")
        result = _show("card", title, body)  # on screen for decklink's card_seconds(body)
    return {"ok": True, "spoken": say, "shown": display, "result": result.get("result")}


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
            "forget_fact": tool_forget_fact, "get_quests": tool_get_quests, "find_quests": tool_find_quests,
            "respond": tool_respond, "track_checklist": tool_track_checklist, "show_layout": tool_show_layout,
            "set_hook": tool_set_hook, "get_character": tool_get_character, "get_bags": tool_get_bags,
            "get_item_info": tool_get_item_info, "get_prices": tool_get_prices, "find_items": tool_find_items, "flag_items": tool_flag_items,
            "clear_flags": tool_clear_flags, "list_hooks": tool_list_hooks, "remove_hook": tool_remove_hook,
            "clear_checklist": tool_clear_checklist, "get_waypoints": tool_get_waypoints, "remove_waypoints": tool_remove_waypoints}


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
