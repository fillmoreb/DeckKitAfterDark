"""Downloads the reference data and (re)builds the agent's game database.

    python3 -m agent.update_data          (from the repo root)

Sources (fetched into $DECKAGENT_HOME/data, never committed: they aren't ours to redistribute):
  QuestieDB (github.com/Questie/QuestieDB): Forever NPCs with spawn coordinates converted for 1.60,
    and its areaId -> UiMapID table
  wago.tools: Forever's UiMap (map names, parents) and UiMapAssignment (map <-> world)
"""
import datetime, os, sys, urllib.request

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "agent"
from agent import common, gamedb  # noqa: E402

QUESTIE = "https://raw.githubusercontent.com/Questie/QuestieDB/HEAD/"
SOURCES = {
    "foreverNpcDB.lua": QUESTIE + "data/Forever/foreverNpcDB.lua",
    "areaIdToUiMapId.lua": QUESTIE + "support/Forever/Zones/areaIdToUiMapId.lua",
    "UiMap.csv": "https://wago.tools/db2/UiMap/csv?product=wowf",
    "UiMapAssignment.csv": "https://wago.tools/db2/UiMapAssignment/csv?product=wowf",
}


def fetch(name, url):
    path = os.path.join(common.DATA_DIR, name)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (DeckAgent data update)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    if len(data) < 100:
        raise RuntimeError("%s: suspiciously small download (%d bytes)" % (url, len(data)))
    with open(path + ".tmp", "wb") as f:
        f.write(data)
    os.replace(path + ".tmp", path)
    return path


def build(paths, db_path):
    read = lambda n: open(paths[n], encoding="utf-8").read()
    tmp = db_path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    db = gamedb.connect(tmp)
    gamedb.load_maps(db, read("UiMap.csv"), read("UiMapAssignment.csv"))
    gamedb.load_npcs(db, read("foreverNpcDB.lua"), read("areaIdToUiMapId.lua"))
    gamedb.set_meta(db, built=datetime.datetime.now().isoformat(timespec="seconds"),
                    **{"source:" + n: u for n, u in SOURCES.items()})
    db.commit()
    counts = {t: db.execute("SELECT count(*) FROM %s" % t).fetchone()[0] for t in ("npc", "spawn", "map")}
    db.close()
    os.replace(tmp, db_path)  # whole database or nothing
    return counts


def main():
    os.makedirs(common.DATA_DIR, exist_ok=True)
    paths = {}
    for name, url in SOURCES.items():
        print("fetching", name)
        paths[name] = fetch(name, url)
    counts = build(paths, common.DB_PATH)
    print("built %s: %d NPCs, %d spawns, %d maps" % (common.DB_PATH, counts["npc"], counts["spawn"], counts["map"]))


if __name__ == "__main__":
    main()
