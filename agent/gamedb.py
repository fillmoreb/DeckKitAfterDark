"""The agent's game database (SQLite): who is where, built from downloaded data (update_data.py).

Tables:
  npc     id, name, title, faction ("A", "H", "AH" or NULL: whom it's friendly to), flags, levels
  spawn   npc, map (UiMapID), x, y (0-100, like /way), cont (continent), wx, wy (world yards)
  map     id, name, parent, cont, the world rectangle (for map <-> world)
  meta    key, value (sources and when they were fetched)

World position from a map position (WoW's maps put world +X up and +Y left):
  wx = max_x - y * (max_x - min_x),  wy = max_y - x * (max_y - min_y)   (x, y 0-1)
"""
import csv, io, math, sqlite3, time

from . import luadata

SCHEMA = """
CREATE TABLE IF NOT EXISTS npc (id INTEGER PRIMARY KEY, name TEXT, title TEXT, faction TEXT, flags INTEGER,
    min_level INTEGER, max_level INTEGER);
CREATE TABLE IF NOT EXISTS spawn (npc INTEGER, map INTEGER, x REAL, y REAL, cont INTEGER, wx REAL, wy REAL);
CREATE INDEX IF NOT EXISTS spawn_npc ON spawn (npc);
CREATE TABLE IF NOT EXISTS map (id INTEGER PRIMARY KEY, name TEXT, parent INTEGER, cont INTEGER,
    min_x REAL, min_y REAL, max_x REAL, max_y REAL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# QuestieDB npcKeys (positions, 0-based here)
N_NAME, N_MINLVL, N_MAXLVL, N_SPAWNS, N_FRIENDLY, N_TITLE, N_FLAGS = 0, 3, 4, 6, 12, 13, 14


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
    zb = luadata.blocks(zones_lua)
    area_to_map = {}
    for key in ("ZoneDB.private.areaIdToUiMapId", "ZoneDB.private.areaIdToUiMapIdOverride"):  # override last
        if key in zb:
            area_to_map.update(luadata.loads(zb[key]))
    npcs = luadata.loads(luadata.blocks(npc_lua)["QuestieDB.npcData"])
    rects = map_rects(db)
    db.execute("DELETE FROM npc")
    db.execute("DELETE FROM spawn")
    for nid, e in npcs.items():
        if not isinstance(e, (list, dict)):
            continue
        get = (lambda k: e[k] if k < len(e) else None) if isinstance(e, list) else (lambda k: e.get(k + 1))
        db.execute("INSERT INTO npc VALUES (?,?,?,?,?,?,?)",
                   (nid, get(N_NAME), get(N_TITLE), get(N_FRIENDLY), get(N_FLAGS), get(N_MINLVL), get(N_MAXLVL)))
        for area, coords in (get(N_SPAWNS) or {}).items():
            mid = area_to_map.get(area)
            if not mid:
                continue
            for c in coords if isinstance(coords, list) else []:
                if not (isinstance(c, list) and len(c) >= 2) or c[0] < 0:  # -1,-1: inside an instance
                    continue
                cont = wx = wy = None
                if mid in rects:
                    cont, wx, wy = to_world(rects[mid], c[0] / 100, c[1] / 100)
                db.execute("INSERT INTO spawn VALUES (?,?,?,?,?,?,?)", (nid, mid, c[0], c[1], cont, wx, wy))


def set_meta(db, **values):
    for k, v in values.items():
        db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, str(v)))


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
