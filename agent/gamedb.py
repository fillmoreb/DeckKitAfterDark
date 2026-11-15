"""The agent's game database (SQLite): who is where, built from downloaded data (update_data.py).

Tables:
  npc     id, name, title, faction ("A", "H", "AH" or NULL: whom it's friendly to), flags, levels
  spawn   npc, map (UiMapID), x, y (0-100, like /way), cont (continent), wx, wy (world yards)
  map     id, name, parent, cont, the world rectangle (for map <-> world)
  quest   id, name, level, required level, objectives text (its one-line summary), next in chain,
          zone (QuestieDB zoneOrSort: an area ID when > 0), races (bitmask, 0/NULL any), pre (JSON list of
          prerequisite quest IDs)
  area    id, name, kind ("dungeon"/"zone"): area IDs quests are filed under, with names
  quest_giver   quest, kind ("npc"/"object"), id, role ("start"/"end")
  quest_goal    quest, n (order within its kind), kind ("monster"/"object"/"item"), target id, text
  item    id, name;  item_source  item, kind ("npc" drop, "object" container, "vendor"), id
  object  id, name;  object_spawn  object, map, x, y, cont, wx, wy (like spawn)
  meta    key, value (sources and when they were fetched)

World position from a map position (WoW's maps put world +X up and +Y left):
  wx = max_x - y * (max_x - min_x),  wy = max_y - x * (max_y - min_y)   (x, y 0-1)
"""
import csv, io, json, math, re, sqlite3, time

from . import luadata

SCHEMA = """
CREATE TABLE IF NOT EXISTS npc (id INTEGER PRIMARY KEY, name TEXT, title TEXT, faction TEXT, flags INTEGER,
    min_level INTEGER, max_level INTEGER);
CREATE TABLE IF NOT EXISTS spawn (npc INTEGER, map INTEGER, x REAL, y REAL, cont INTEGER, wx REAL, wy REAL);
CREATE INDEX IF NOT EXISTS spawn_npc ON spawn (npc);
CREATE TABLE IF NOT EXISTS map (id INTEGER PRIMARY KEY, name TEXT, parent INTEGER, cont INTEGER,
    min_x REAL, min_y REAL, max_x REAL, max_y REAL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS quest (id INTEGER PRIMARY KEY, name TEXT, level INTEGER, req_level INTEGER, text TEXT,
    next INTEGER, zone INTEGER, races INTEGER, pre TEXT);
CREATE INDEX IF NOT EXISTS quest_zone ON quest (zone);
CREATE TABLE IF NOT EXISTS area (id INTEGER, name TEXT, kind TEXT);
CREATE TABLE IF NOT EXISTS quest_giver (quest INTEGER, kind TEXT, id INTEGER, role TEXT);
CREATE INDEX IF NOT EXISTS quest_giver_quest ON quest_giver (quest);
CREATE TABLE IF NOT EXISTS quest_goal (quest INTEGER, n INTEGER, kind TEXT, target INTEGER, text TEXT);
CREATE INDEX IF NOT EXISTS quest_goal_quest ON quest_goal (quest);
CREATE TABLE IF NOT EXISTS item (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS item_source (item INTEGER, kind TEXT, id INTEGER);
CREATE INDEX IF NOT EXISTS item_source_item ON item_source (item);
CREATE TABLE IF NOT EXISTS object (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS object_spawn (object INTEGER, map INTEGER, x REAL, y REAL, cont INTEGER, wx REAL,
    wy REAL);
CREATE INDEX IF NOT EXISTS object_spawn_object ON object_spawn (object);
"""

# QuestieDB npcKeys (positions, 0-based here)
N_NAME, N_MINLVL, N_MAXLVL, N_SPAWNS, N_FRIENDLY, N_TITLE, N_FLAGS = 0, 3, 4, 6, 12, 13, 14
# questKeys, itemKeys, objectKeys (0-based)
Q_NAME, Q_STARTS, Q_ENDS, Q_REQLVL, Q_LEVEL, Q_RACES, Q_TEXT, Q_GOALS, Q_PREGROUP, Q_PRESINGLE, Q_ZONE, Q_NEXT = (
    0, 1, 2, 3, 4, 5, 7, 9, 11, 12, 16, 21)
HORDE_RACES, ALLIANCE_RACES = 2 | 16 | 32 | 128, 1 | 4 | 8 | 64  # orc undead tauren troll / human dwarf nelf gnome
I_NAME, I_NPCS, I_OBJECTS, I_VENDORS = 0, 1, 2, 13
O_NAME, O_SPAWNS = 0, 3


def _getter(e):
    """Field k (0-based) of a QuestieDB entry, which luadata reads as a list or (with holes) a dict."""
    if isinstance(e, list):
        return lambda k: e[k] if k < len(e) else None
    return lambda k: e.get(k + 1)


def _plain(v):
    """A field meant to be a string or number (the export writes some missing ones as {})."""
    return None if isinstance(v, (list, dict)) else v


def _ids(v):
    """A QuestieDB id list (list, or dict with holes) -> [int]."""
    if isinstance(v, dict):
        v = [v[k] for k in sorted(v)]
    return [x for x in (v or []) if isinstance(x, int)]


def _area_maps(zones_lua):
    zb = luadata.blocks(zones_lua)
    area_to_map = {}
    for key in ("ZoneDB.private.areaIdToUiMapId", "ZoneDB.private.areaIdToUiMapIdOverride"):  # override last
        if key in zb:
            area_to_map.update(luadata.loads(zb[key]))
    return area_to_map


def _spawns(spawns, area_to_map, rects):
    """QuestieDB spawns {areaId: [[x, y], ...]} -> [(map, x, y, cont, wx, wy)], skipping instance ones."""
    out = []
    for area, coords in (spawns or {}).items() if isinstance(spawns, dict) else []:
        mid = area_to_map.get(area)
        if not mid:
            continue
        for c in coords if isinstance(coords, list) else []:
            if not (isinstance(c, list) and len(c) >= 2) or c[0] < 0:  # -1,-1: inside an instance
                continue
            cont = wx = wy = None
            if mid in rects:
                cont, wx, wy = to_world(rects[mid], c[0] / 100, c[1] / 100)
            out.append((mid, c[0], c[1], cont, wx, wy))
    return out


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def _rows(csv_text):
    return list(csv.DictReader(io.StringIO(csv_text)))


def load_maps(db, uimap_csv, assignment_csv):
    """UiMap (names, parents) + UiMapAssignment (world rectangles) from wago.tools CSVs."""
    rects = {}
    for r in _rows(assignment_csv):
        # Only whole-map assignments (UiMin 0,0 to UiMax 1,1): the map's own rectangle.
        if (float(r["UiMin_0"]), float(r["UiMin_1"]), float(r["UiMax_0"]), float(r["UiMax_1"])) != (0, 0, 1, 1):
            continue
        rects.setdefault(int(r["UiMapID"]), (int(r["MapID"]), float(r["Region_0"]), float(r["Region_1"]),
                                             float(r["Region_3"]), float(r["Region_4"])))
    db.execute("DELETE FROM map")
    for r in _rows(uimap_csv):
        mid = int(r["ID"])
        cont, min_x, min_y, max_x, max_y = rects.get(mid, (None,) * 5)
        db.execute("INSERT INTO map VALUES (?,?,?,?,?,?,?,?)",
                   (mid, r["Name_lang"], int(r["ParentUiMapID"] or 0), cont, min_x, min_y, max_x, max_y))


def map_rects(db):
    return {r["id"]: r for r in db.execute("SELECT * FROM map WHERE cont IS NOT NULL")}


def to_world(rect, x, y):
    """Map position (0-1) on a map with a world rectangle -> (cont, wx, wy)."""
    return (rect["cont"], rect["max_x"] - y * (rect["max_x"] - rect["min_x"]),
            rect["max_y"] - x * (rect["max_y"] - rect["min_y"]))


def load_npcs(db, npc_lua, zones_lua):
    """QuestieDB's Forever NPC file + its areaId -> UiMapID table."""
    area_to_map = _area_maps(zones_lua)
    npcs = luadata.entities(npc_lua, "QuestieDB.npcData")
    rects = map_rects(db)
    db.execute("DELETE FROM npc")
    db.execute("DELETE FROM spawn")
    for nid, e in npcs.items():
        if not isinstance(e, (list, dict)):
            continue
        get = _getter(e)
        db.execute("INSERT INTO npc VALUES (?,?,?,?,?,?,?)",
                   (nid,) + tuple(_plain(get(k)) for k in (N_NAME, N_TITLE, N_FRIENDLY, N_FLAGS, N_MINLVL, N_MAXLVL)))
        for sp in _spawns(get(N_SPAWNS), area_to_map, rects):
            db.execute("INSERT INTO spawn VALUES (?,?,?,?,?,?,?)", (nid,) + sp)


def load_quests(db, quest_lua):
    """QuestieDB's Forever quest file: names, levels, who starts and ends them, and their goals."""
    quests = luadata.entities(quest_lua, "QuestieDB.questData")
    for t in ("quest", "quest_giver", "quest_goal"):
        db.execute("DELETE FROM " + t)
    for qid, e in quests.items():
        if not isinstance(e, (list, dict)):
            continue
        get = _getter(e)
        text = get(Q_TEXT)
        text = " ".join(t for t in _ids_or_strs(text)) or None
        pre = _ids(get(Q_PREGROUP)) + _ids(get(Q_PRESINGLE))
        db.execute("INSERT INTO quest VALUES (?,?,?,?,?,?,?,?,?)",
                   (qid, _plain(get(Q_NAME)), _plain(get(Q_LEVEL)), _plain(get(Q_REQLVL)), text,
                    _plain(get(Q_NEXT)) or None, _plain(get(Q_ZONE)), _plain(get(Q_RACES)),
                    json.dumps(pre) if pre else None))
        for field, role in ((Q_STARTS, "start"), (Q_ENDS, "end")):
            by = get(field)
            by = by if isinstance(by, list) else [by.get(k) for k in (1, 2, 3)] if isinstance(by, dict) else []
            for kind, ids in zip(("npc", "object", "item"), by):
                for i in _ids(ids):
                    db.execute("INSERT INTO quest_giver VALUES (?,?,?,?)", (qid, kind, i, role))
        goals = get(Q_GOALS)
        goals = goals if isinstance(goals, list) else [goals.get(k) for k in range(1, 6)] if isinstance(goals, dict) else []
        for kind, entries in zip(("monster", "object", "item", None, "monster"), goals):
            if kind is None or not entries:
                continue
            entries = entries if isinstance(entries, list) else [entries[k] for k in sorted(entries)]
            n = db.execute("SELECT count(*) FROM quest_goal WHERE quest = ? AND kind = ?", (qid, kind)).fetchone()[0]
            for g in entries:
                if not isinstance(g, list) or not g:
                    continue
                if isinstance(g[0], list):  # kill credit: {{creature, ...}, baseCreature, baseText}
                    target, text = (g[1] if len(g) > 1 else None), (g[2] if len(g) > 2 else None)
                else:
                    target, text = g[0], (g[1] if len(g) > 1 and isinstance(g[1], str) else None)
                n += 1
                db.execute("INSERT INTO quest_goal VALUES (?,?,?,?,?)", (qid, n, kind, target, text))


def _ids_or_strs(v):
    if isinstance(v, dict):
        v = [v[k] for k in sorted(v)]
    if isinstance(v, str):
        return [v]
    return [x for x in (v or []) if isinstance(x, str)]


def load_items(db, item_lua):
    """QuestieDB's Forever item file: names and where they come from (drops, containers, vendors)."""
    items = luadata.entities(item_lua, "QuestieDB.itemData")
    db.execute("DELETE FROM item")
    db.execute("DELETE FROM item_source")
    for iid, e in items.items():
        if not isinstance(e, (list, dict)):
            continue
        get = _getter(e)
        db.execute("INSERT INTO item VALUES (?,?)", (iid, _plain(get(I_NAME))))
        for field, kind in ((I_NPCS, "npc"), (I_OBJECTS, "object"), (I_VENDORS, "vendor")):
            for i in _ids(get(field)):
                db.execute("INSERT INTO item_source VALUES (?,?,?)", (iid, kind, i))


def load_objects(db, object_lua, zones_lua):
    """QuestieDB's Forever object file: names and spawns."""
    objects = luadata.entities(object_lua, "QuestieDB.objectData")
    area_to_map, rects = _area_maps(zones_lua), map_rects(db)
    db.execute("DELETE FROM object")
    db.execute("DELETE FROM object_spawn")
    for oid, e in objects.items():
        if not isinstance(e, (list, dict)):
            continue
        get = _getter(e)
        db.execute("INSERT INTO object VALUES (?,?)", (oid, _plain(get(O_NAME))))
        for sp in _spawns(get(O_SPAWNS), area_to_map, rects):
            db.execute("INSERT INTO object_spawn VALUES (?,?,?,?,?,?,?)", (oid,) + sp)


def set_meta(db, **values):
    for k, v in values.items():
        db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, str(v)))


def near_state(near):
    """An MCP tool's `near` ({map_id, x, y}, x and y 0-100 like /way) -> the state dict the searches
    here take ({map, x, y}, x and y 0-1, like the game's STATE reply), or None without one."""
    if not near:
        return None
    return {"map": int(near["map_id"]), "x": float(near["x"]) / 100, "y": float(near["y"]) / 100}


def player_world(db, state):
    """(cont, wx, wy) for a DeckAgent state reply: the game's own world position, else from its map."""
    if state.get("cont") is not None and state.get("wx") is not None:
        return state["cont"], state["wx"], state["wy"]
    rect = map_rects(db).get(state.get("map"))
    if rect and state.get("x") is not None:
        return to_world(rect, state["x"], state["y"])
    return None


# NPC roles: QuestieDB's npcFlags bit (classic layout) and, for the few NPCs whose flags are 0, the
# titles that mean the same. Checked against the Forever data 2026-11-14: 0x80 also marks some mount
# vendors, so innkeeper also needs "innkeeper" in the name or title.
ROLES = {
    "flight_master": (0x8, ["wind rider master", "gryphon master", "hippogryph master", "bat handler", "flight master"]),
    "innkeeper": (0x80, ["innkeeper"]),
    "banker": (0x100, ["banker"]),
    "auctioneer": (0x1000, ["auctioneer"]),
    "stable_master": (0x2000, ["stable master"]),
    "battlemaster": (0x800, ["battlemaster"]),
    "repair": (0x4000, []),
    "vendor": (0x4, []),
    "trainer": (0x10, ["trainer"]),
    "spirit_healer": (0x20, ["spirit healer"]),
    "guild_master": (0x200 | 0x400, ["guild master"]),
}
MUST_NAME = {"innkeeper": "innkeeper"}  # role -> a word the name or title has to contain as well

# Words that say how to search, not what for ("the nearest innkeeper").
FILLER = {"nearest", "closest", "near", "nearby", "the", "a", "an", "me", "my", "to", "for", "find", "where", "is",
          "any", "some", "local"}


def find_npc(db, query="", state=None, faction=None, limit=5, role=None):
    """NPCs matching `query` (words in the name or title) and/or `role` (a ROLES key), nearest first."""
    words = [w for w in (query or "").lower().split() if w not in FILLER]
    where, args = [], []
    for w in words:
        where.append("(lower(n.name) LIKE ? OR lower(coalesce(n.title,'')) LIKE ?)")
        args += ["%" + w + "%"] * 2
    if role:
        if role not in ROLES:
            raise ValueError("unknown role %r (roles: %s)" % (role, ", ".join(ROLES)))
        bit, titles = ROLES[role]
        alts = ["(coalesce(n.flags,0) & ?) != 0"]
        args.append(bit)
        for t in titles:
            alts.append("lower(coalesce(n.title,'')) = ?")
            args.append(t)
        where.append("(" + " OR ".join(alts) + ")")
        if role in MUST_NAME:
            where.append("(lower(n.name) LIKE ? OR lower(coalesce(n.title,'')) LIKE ?)")
            args += ["%" + MUST_NAME[role] + "%"] * 2
    if not where:
        return []
    return find_npc_once(db, " AND ".join(where), args, state, faction, limit)


def find_npc_once(db, where, args, state=None, faction=None, limit=5):
    """The search itself: NPCs matching the SQL `where` (on npc n), friendly to `faction` ("A"/"H") if
    given, nearest first when `state` (the player's DeckAgent state reply) gives a position.
    Each result has the nearest spawn: map, x, y (0-100), zone, distance (yards, same continent only)."""
    args = list(args)
    if faction in ("A", "H"):
        where += " AND n.faction LIKE ?"
        args.append("%" + faction + "%")
    here = player_world(db, state) if state else None
    best = {}
    for r in db.execute("SELECT n.id, n.name, n.title, n.faction, s.map, s.x, s.y, s.cont, s.wx, s.wy, m.name AS zone "
                        "FROM npc n JOIN spawn s ON s.npc = n.id LEFT JOIN map m ON m.id = s.map WHERE " + where, args):
        dist = None
        if here and r["cont"] is not None and r["cont"] == here[0]:
            dist = math.hypot(r["wx"] - here[1], r["wy"] - here[2])
        key = (dist is None, dist if dist is not None else 0)
        if r["id"] not in best or key < best[r["id"]][0]:
            best[r["id"]] = (key, r, dist)
    ranked = sorted(best.values(), key=lambda b: (b[0], b[1]["name"]))
    out = []
    for _, r, dist in ranked[:limit]:
        out.append({"id": r["id"], "name": r["name"], "title": r["title"], "friendly_to": r["faction"],
                    "zone": r["zone"], "map_id": r["map"], "x": round(r["x"], 1), "y": round(r["y"], 1),
                    "distance_yards": None if dist is None else round(dist)})
    return out


SOURCES_SHOWN = 4  # drop sources per item objective


def _name(db, kind, target):
    table = {"monster": "npc", "object": "object", "item": "item"}.get(kind)
    if not table or target is None:
        return None
    r = db.execute("SELECT name FROM %s WHERE id = ?" % table, (target,)).fetchone()
    return r["name"] if r else None


def _item_sources(db, item):
    out = []
    for r in db.execute("SELECT s.kind, coalesce(n.name, o.name) AS name FROM item_source s "
                        "LEFT JOIN npc n ON s.kind != 'object' AND n.id = s.id "
                        "LEFT JOIN object o ON s.kind = 'object' AND o.id = s.id WHERE s.item = ? "
                        "ORDER BY s.kind = 'vendor', s.kind = 'object'", (item,)):
        name = r["name"] and (r["name"] + (" (vendor)" if r["kind"] == "vendor" else ""))
        if name and name not in out:
            out.append(name)
        if len(out) >= SOURCES_SHOWN:
            break
    return out


def quest_log(db, game_quests, state=None):
    """The game's QUESTS reply (IDs and counts only) with names from the database: each quest's name,
    level, summary, objectives (what, have/need, done; for items, who drops or sells them) and
    turned_in_at, every NPC or object that takes it in (quest_givers, as find_quests gives them). The
    game lists objectives kills and objects first, then items, each in the quest's order: they're
    matched to the database's by kind, in order."""
    out = []
    for q in game_quests:
        row = db.execute("SELECT * FROM quest WHERE id = ?", (q["id"],)).fetchone()
        entry = {"id": q["id"], "name": row["name"] if row else None, "level": row["level"] if row else None,
                 "complete": q["complete"], "failed": q["failed"]}
        if row and row["text"]:
            entry["summary"] = row["text"]
        if not row:
            entry["note"] = "not in the quest database (new in Forever?): only its progress is known"
        goals = {}
        for g in db.execute("SELECT * FROM quest_goal WHERE quest = ? ORDER BY kind, n", (q["id"],)):
            goals.setdefault(g["kind"], []).append(g)
        objectives = []
        for o in q["objectives"]:
            pool = goals.get(o["kind"]) or []
            g = pool.pop(0) if pool else None
            obj = {"kind": o["kind"], "have": o["have"], "need": o["need"], "done": o["done"]}
            if g is not None:
                obj["what"] = g["text"] or _name(db, g["kind"], g["target"])
                if g["kind"] == "item" and g["target"] is not None:
                    obj["from"] = _item_sources(db, g["target"])
            objectives.append(obj)
        entry["objectives"] = objectives
        enders = quest_givers(db, q["id"], "end", state)
        if enders:
            entry["turned_in_at"] = enders
        out.append(entry)
    return out


_DUNGEON = re.compile(r'^\s*\[(\d+)\]\s*=\s*\{"((?:[^"\\]|\\.)+)",(nil|\{[\d,\s]*\})', re.M)


def load_areas(db, dungeons_lua, zones_lua):
    """Names for the area IDs quests are filed under: QuestieDB's dungeons (with their other area IDs;
    read by pattern, as the file has a little code in it) and, for zones, the UiMap each area maps to."""
    db.execute("DELETE FROM area")
    for m in _DUNGEON.finditer(dungeons_lua):
        name = m.group(2)
        ids = [int(m.group(1))] + [int(x) for x in re.findall(r"\d+", m.group(3))]
        for i in ids:
            db.execute("INSERT INTO area VALUES (?,?,?)", (i, name, "dungeon"))
    seen = {r[0] for r in db.execute("SELECT id FROM area")}
    for area, mid in _area_maps(zones_lua).items():
        if area in seen:
            continue
        r = db.execute("SELECT name FROM map WHERE id = ?", (mid,)).fetchone()
        if r:
            db.execute("INSERT INTO area VALUES (?,?,?)", (area, r["name"], "zone"))


def quest_givers(db, qid, role, state=None):
    """Who starts (or ends) a quest, NPCs and objects alike: [{name, zone, map_id, x, y, distance_yards}]
    (each at its spawn nearest `state`; just a name when it has no spawn outside an instance), nearest
    first when there's a position, NPCs before objects otherwise."""
    out = []
    for g in db.execute("SELECT kind, id FROM quest_giver WHERE quest = ? AND role = ? ORDER BY kind = 'npc' DESC",
                        (qid, role)):
        if g["kind"] == "npc":
            spot = find_npc_once(db, "n.id = ?", [g["id"]], state, None, 1)
            if spot:
                out.append({k: spot[0][k] for k in ("name", "zone", "map_id", "x", "y", "distance_yards")})
                continue
        if g["kind"] == "object":
            spot = nearest_object(db, g["id"], state)
            if spot:
                out.append(spot)
                continue
        name = _name(db, {"npc": "monster"}.get(g["kind"], g["kind"]), g["id"])
        if name:
            out.append({"name": name + (" (an item: it starts the quest)" if g["kind"] == "item" else "")})
    # Nearest first; those with no distance (another continent, no spawn) keep their order after.
    out.sort(key=lambda g: (g.get("distance_yards") is None, g.get("distance_yards") or 0))
    return out


def nearest_object(db, oid, state=None):
    """An object's spawn nearest to `state` (same continent), else its first: {name, zone, map_id, x, y,
    distance_yards}, or None when it has no spawn outside an instance. Objects often exist once per
    faction's side of the world (a note in the Barrens and one in Westfall), so nearest matters."""
    here = player_world(db, state) if state else None
    best = None
    for r in db.execute("SELECT o.name, s.map, s.x, s.y, s.cont, s.wx, s.wy, m.name AS zone FROM object_spawn s "
                        "JOIN object o ON o.id = s.object LEFT JOIN map m ON m.id = s.map WHERE s.object = ?", (oid,)):
        dist = None
        if here and r["cont"] is not None and r["cont"] == here[0]:
            dist = math.hypot(r["wx"] - here[1], r["wy"] - here[2])
        key = (dist is None, dist or 0)
        if best is None or key < best[0]:
            best = (key, r, dist)
    if not best:
        return None
    _, r, dist = best
    return {"name": r["name"], "zone": r["zone"], "map_id": r["map"], "x": round(r["x"], 1), "y": round(r["y"], 1),
            "distance_yards": None if dist is None else round(dist)}


def find_quests(db, place="", name="", faction=None, state=None, limit=30):
    """Quests filed under a place (dungeon or zone name words) and/or with words in their name, for
    `faction` ("A"/"H": others' race-locked quests left out), lowest level first. Each: id, name,
    level, required level, where it's filed, summary, givers and turned_in_at (each with its nearest
    spawn when `state` has a position; a chain can start in one zone and end in another), prerequisites
    (id and name)."""
    where, args = [], []
    for w in (place or "").lower().split():
        where.append("q.zone IN (SELECT id FROM area WHERE lower(name) LIKE ?)")
        args.append("%" + w + "%")
    for w in (name or "").lower().split():
        where.append("lower(q.name) LIKE ?")
        args.append("%" + w + "%")
    if not where:
        return []
    if faction in ("A", "H"):
        mask = HORDE_RACES if faction == "H" else ALLIANCE_RACES
        where.append("(coalesce(q.races, 0) = 0 OR (q.races & ?) != 0)")
        args.append(mask)
    out = []
    for q in db.execute("SELECT q.*, (SELECT name FROM area WHERE id = q.zone LIMIT 1) AS place FROM quest q WHERE "
                        + " AND ".join(where) + " ORDER BY coalesce(q.level, 0), q.name LIMIT ?", args + [limit]):
        pre = []
        for pid in json.loads(q["pre"] or "[]"):
            r = db.execute("SELECT name FROM quest WHERE id = ?", (pid,)).fetchone()
            pre.append({"id": pid, "name": r["name"] if r else None})
        entry = {"id": q["id"], "name": q["name"], "level": q["level"], "required_level": q["req_level"],
                 "filed_under": q["place"], "summary": q["text"], "givers": quest_givers(db, q["id"], "start", state),
                 "turned_in_at": quest_givers(db, q["id"], "end", state)}
        if pre:
            entry["prerequisites"] = pre
        out.append(entry)
    return out
