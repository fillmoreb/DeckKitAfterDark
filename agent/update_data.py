"""Downloads the reference data and (re)builds the agent's game database.

    python3 -m agent.update_data               (from the repo root, or ~/.local/lib/deckagent on the Deck)
    python3 -m agent.update_data --if-changed  only when QuestieDB has a new commit (the daily timer,
                                               deckagent-update.timer, runs this)

Sources (fetched into $DECKAGENT_HOME/data, never committed: they aren't ours to redistribute):
  QuestieDB (github.com/Questie/QuestieDB), the whole repository at its latest commit. Its own
    tools/export-forever.lua (run with Lua 5.1: one installed here, else the static one QuestieDB
    ships for Linux x64; LuaJIT only as a last resort, as it can't load files this big) writes Forever's merged
    databases: the Classic base, Forever's changes and QuestieDB's Forever corrections, one entity
    per line. Without a Lua, or if the export fails, the raw data/Forever files are used instead:
    Forever's own rows only, uncorrected (about two thirds of the quests), and the build says so.
    Plus its areaId -> UiMapID table.
  wago.tools: Forever's UiMap (map names, parents) and UiMapAssignment (map <-> world)

The database is built beside the old one and swapped in whole, so the assistant never reads half of
one; a failed update leaves the old one in place.
"""
import argparse, datetime, os, shutil, subprocess, sys, tarfile, urllib.request

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "agent"
from agent import common, gamedb  # noqa: E402

REPO = "Questie/QuestieDB"
COMMIT_URL = "https://api.github.com/repos/%s/commits/HEAD" % REPO
TARBALL_URL = "https://codeload.github.com/%s/tar.gz/%s"
# What the export needs from the repository (the rest, ~250 MB of tools and translations, isn't unpacked).
NEEDED = ("src/", "generator/", "tools/export/", "tools/export-forever.lua", "data/", "support/",
          "tools/lua-binary/linux-x64/")
BUNDLED_LUA = "tools/lua-binary/linux-x64/lua"  # QuestieDB's static Lua 5.1.5, for Linux x64
EXPORTED = "src/corrections/Forever/combined"
KINDS = ("Npc", "Quest", "Item", "Object")
ZONES = "support/Forever/Zones/areaIdToUiMapId.lua"
DUNGEONS = "support/Forever/Zones/dungeons.lua"
WAGO = {
    "UiMap.csv": "https://wago.tools/db2/UiMap/csv?product=wowf",
    "UiMapAssignment.csv": "https://wago.tools/db2/UiMapAssignment/csv?product=wowf",
}
UA = {"User-Agent": "Mozilla/5.0 (DeckAgent data update)"}


def log(*parts):
    print(*parts, flush=True)


def download(url, path, accept=None, timeout=120):
    headers = dict(UA, **({"Accept": accept} if accept else {}))
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
        with open(path + ".tmp", "wb") as f:
            shutil.copyfileobj(r, f)
    if os.path.getsize(path + ".tmp") < 100:
        raise RuntimeError("%s: suspiciously small download" % url)
    os.replace(path + ".tmp", path)
    return path


def upstream_commit():
    """QuestieDB's latest commit (a small API call, so a daily check costs next to nothing)."""
    req = urllib.request.Request(COMMIT_URL, headers=dict(UA, Accept="application/vnd.github.sha"))
    with urllib.request.urlopen(req, timeout=30) as r:
        sha = r.read().decode().strip()
    if len(sha) != 40:
        raise RuntimeError("unexpected commit id from GitHub: %r" % sha[:60])
    return sha


def built_commit(db_path):
    """The QuestieDB commit the database was built from, if it was built with the corrections (a build
    without them is never current: the next check tries again)."""
    if not os.path.exists(db_path):
        return None
    try:
        db = gamedb.connect(db_path)
        try:
            meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
        finally:
            db.close()
    except Exception:
        return None
    return meta.get("questiedb_commit") if meta.get("corrections") == "yes" else None


def unpack(tarball, dest):
    """Only the NEEDED parts, and only plain files and folders inside dest (it's someone else's archive)."""
    root = os.path.realpath(dest)
    with tarfile.open(tarball) as tar:
        top = None
        for m in tar:
            parts = m.name.split("/", 1)
            top = top or parts[0]
            rel = parts[1] if len(parts) > 1 else ""
            if not rel.startswith(NEEDED) or not (m.isfile() or m.isdir()):
                continue
            target = os.path.realpath(os.path.join(dest, m.name))
            if not target.startswith(root + os.sep):
                raise RuntimeError("archive entry outside the folder: %s" % m.name)
            tar.extract(m, dest, set_attrs=False, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
    return os.path.join(dest, top)


def _lua_version(path):
    try:
        v = subprocess.run([path, "-v"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return v.stdout + v.stderr


def find_lua(repo=None):
    """A Lua 5.1 for QuestieDB's export: $DECKAGENT_LUA, an installed lua5.1 (or `lua` if it's 5.1),
    QuestieDB's own static one (Linux x64), else LuaJIT (5.1, but it can't load QuestieDB's biggest
    files today: "more than 65536 constants"). None if there's none."""
    candidates = [os.environ.get("DECKAGENT_LUA"), shutil.which("lua5.1"), shutil.which("lua")]
    bundled = repo and os.path.join(repo, BUNDLED_LUA)
    if bundled and os.path.exists(bundled):
        os.chmod(bundled, 0o755)
        candidates.append(bundled)
    candidates.append(shutil.which("luajit"))
    for path in candidates:
        if path and ("Lua 5.1" in _lua_version(path) or "LuaJIT" in _lua_version(path)):
            return path
    return None


def export(repo, lua):
    """Runs QuestieDB's own export; {kind: path} of the merged, corrected files, or raises."""
    out = subprocess.run([lua, "tools/export-forever.lua", "--include-authored"], cwd=repo, capture_output=True,
                         text=True, timeout=600)
    if out.returncode:
        raise RuntimeError("export failed: " + (out.stderr or out.stdout).strip()[-400:])
    return {k: os.path.join(repo, EXPORTED, "forever%sDB.lua" % k) for k in KINDS}


def build(files, db_path, meta):
    """files: {"Npc"/"Quest"/"Item"/"Object"/"zones"/"UiMap.csv"/"UiMapAssignment.csv": path}."""
    read = lambda n: open(files[n], encoding="utf-8").read()
    tmp = db_path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    db = gamedb.connect(tmp)
    gamedb.load_maps(db, read("UiMap.csv"), read("UiMapAssignment.csv"))
    zones = read("zones")
    gamedb.load_npcs(db, read("Npc"), zones)
    gamedb.load_quests(db, read("Quest"))
    gamedb.load_items(db, read("Item"))
    gamedb.load_objects(db, read("Object"), zones)
    gamedb.load_areas(db, read("dungeons"), zones)
    gamedb.set_meta(db, built=datetime.datetime.now().isoformat(timespec="seconds"), **meta)
    db.commit()
    counts = {t: db.execute("SELECT count(*) FROM %s" % t).fetchone()[0]
              for t in ("npc", "spawn", "map", "quest", "item", "object")}
    db.close()
    os.replace(tmp, db_path)  # whole database or nothing
    return counts


def update(if_changed=False, db_path=None):
    db_path = db_path or common.DB_PATH
    os.makedirs(common.DATA_DIR, exist_ok=True)
    sha = upstream_commit()
    if if_changed and built_commit(db_path) == sha:
        log("QuestieDB unchanged (%s): nothing to do" % sha[:10])
        return None
    work = os.path.join(common.DATA_DIR, "questiedb")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    try:
        log("fetching QuestieDB", sha[:10])
        tarball = download(TARBALL_URL % (REPO, sha), os.path.join(work, "questiedb.tar.gz"), timeout=600)
        repo = unpack(tarball, work)
        os.remove(tarball)
        files, corrected = {}, "no"
        lua = find_lua(repo)
        if lua:
            try:
                log("exporting with", lua)
                files = export(repo, lua)
                corrected = "yes"
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
                log("QuestieDB's export didn't work (%s): using its raw Forever files, uncorrected" % e)
        else:
            log("no Lua 5.1 or LuaJIT here: using QuestieDB's raw Forever files, uncorrected")
        if not files:
            files = {k: os.path.join(repo, "data/Forever/forever%sDB.lua" % k) for k in KINDS}
        files["zones"] = os.path.join(repo, ZONES)
        files["dungeons"] = os.path.join(repo, DUNGEONS)
        for name, url in WAGO.items():
            log("fetching", name)
            files[name] = download(url, os.path.join(common.DATA_DIR, name))
        counts = build(files, db_path, {"questiedb_commit": sha, "corrections": corrected,
                                        "source:questiedb": "github.com/%s @ %s" % (REPO, sha),
                                        **{"source:" + n: u for n, u in WAGO.items()}})
    finally:
        shutil.rmtree(work, ignore_errors=True)
    log("built %s from QuestieDB %s (corrections: %s): %s" % (
        db_path, sha[:10], corrected, ", ".join("%d %s" % (n, t) for t, n in counts.items())))
    return counts


def main(argv=None):
    p = argparse.ArgumentParser(prog="agent.update_data", description=__doc__.split("\n\n")[0])
    p.add_argument("--if-changed", action="store_true", help="skip when QuestieDB has no new commit")
    a = p.parse_args(argv)
    update(a.if_changed)


if __name__ == "__main__":
    main()
