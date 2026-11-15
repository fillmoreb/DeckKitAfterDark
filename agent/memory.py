"""Decky's memory database (~/.deckagent/memory.db), separate from the game data in deckagent.db (which
update_data.py rebuilds).

facts: what Decky chooses to keep (remember_fact): one self-contained sentence about one subject,
searched with recall (SQLite FTS5, ranked by match, then recency). A fact that changes is replaced, not
edited: the old one stays, marked replaced, and stops coming back. "Forget that" marks one forgotten.
The launcher (deckagent) searches facts with the question's own words before the brain starts.

characters: every character seen online through get_online, recorded automatically (no model involved),
so "Edna's character" finds all of her characters, not just the ones Ben named. Keyed by name and realm;
a Battle.net friend's characters carry the BattleTag and account ID.
"""
import os, re, sqlite3, time

from agent import common

PATH = os.path.join(common.HOME, "memory.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS characters (
    name TEXT NOT NULL, realm TEXT NOT NULL DEFAULT '',
    battletag TEXT, account_id INTEGER, source TEXT NOT NULL,
    class TEXT, level INTEGER, zone TEXT,
    first_seen REAL NOT NULL, last_seen REAL NOT NULL,
    PRIMARY KEY (name, realm)
);
CREATE INDEX IF NOT EXISTS characters_battletag ON characters (battletag);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY, about TEXT NOT NULL, fact TEXT NOT NULL, created REAL NOT NULL,
    replaced_by INTEGER, forgotten INTEGER NOT NULL DEFAULT 0
);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_search USING fts5 (about, fact, tokenize = 'porter unicode61');
"""

# Words that match everything and mean nothing to a search.
STOPWORDS = set("""a an and are as at be but by can could did do does for from get give got had has have he her
hers him his how i if in into is it its me my no not of on or our she should so some than that the their them
then there these they this to up us was we were what when where which who why will with would you your yours
about again also any just like make more most much now one only other out over please really tell them want
going gonna know think see look need let lets yeah okay ok hey decky""".split())


def words(text):
    """The searchable words in some text: 3+ letters (or digits), not stopwords, without possessives."""
    out = []
    for w in re.findall(r"[^\W_]+(?:'[^\W_]+)?", (text or "").lower()):
        w = re.sub(r"'s$", "", w).replace("'", "")
        if (len(w) >= 3 or w.isdigit()) and w not in STOPWORDS and w not in out:
            out.append(w)
    return out


def add_fact(db, about, fact, replaces=None, now=None):
    """Saves a fact; with `replaces`, that older fact stops coming back. Returns the new id."""
    about, fact = " ".join(about.split()), " ".join(fact.split())
    if not about or not fact:
        raise ValueError("a fact needs a subject (about) and the fact")
    if replaces is not None and not db.execute("SELECT 1 FROM facts WHERE id = ?", (replaces,)).fetchone():
        raise ValueError("there's no fact #%s to replace" % replaces)
    cur = db.execute("INSERT INTO facts (about, fact, created) VALUES (?,?,?)", (about, fact, now or time.time()))
    fid = cur.lastrowid
    db.execute("INSERT INTO facts_search (rowid, about, fact) VALUES (?,?,?)", (fid, about, fact))
    if replaces is not None:
        db.execute("UPDATE facts SET replaced_by = ? WHERE id = ?", (fid, replaces))
    db.commit()
    return fid


def forget(db, fid):
    """Marks a fact forgotten. False if there's no such fact."""
    n = db.execute("UPDATE facts SET forgotten = 1 WHERE id = ?", (fid,)).rowcount
    db.commit()
    return n > 0


def recall(db, query, about=None, limit=8):
    """Live facts matching any of the query's words (or all facts about `about` when the query has none),
    best match first, newer first among equals."""
    terms = words(query)
    live = "f.replaced_by IS NULL AND f.forgotten = 0"
    args = []
    where = live
    if about:
        where += " AND f.about = ? COLLATE NOCASE"
        args.append(" ".join(about.split()))
    if terms:
        match = " OR ".join('"%s"*' % t for t in terms)
        sql = ("SELECT f.*, bm25(facts_search) AS rank FROM facts_search JOIN facts f ON f.id = facts_search.rowid "
               "WHERE facts_search MATCH ? AND " + where + " ORDER BY rank, f.created DESC LIMIT ?")
        rows = db.execute(sql, [match] + args + [limit])
    elif about:
        rows = db.execute("SELECT f.* FROM facts f WHERE " + where + " ORDER BY f.created DESC LIMIT ?", args + [limit])
    else:
        return []
    return [{k: r[k] for k in ("id", "about", "fact", "created")} for r in rows]


def day(t):
    return time.strftime("%b %-d", time.localtime(t))



def connect(path=None):
    path = path or PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def record_online(db, people, now=None):
    """Records the characters in a get_online reply. Returns how many it saw."""
    now = now or time.time()
    n = 0
    for p in people:
        if p.get("list") == "bnet":
            if not p.get("in_wow") or not p.get("character"):
                continue
            row = (p["character"], p.get("realm") or "", p.get("battletag"), p.get("account_id"), "bnet")
        elif p.get("name"):
            row = (p["name"], p.get("realm") or "", None, None, p.get("list") or "friend")
        else:
            continue
        db.execute("""INSERT INTO characters (name, realm, battletag, account_id, source, class, level, zone,
                          first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?)
                      ON CONFLICT (name, realm) DO UPDATE SET
                          battletag = COALESCE(excluded.battletag, battletag),
                          account_id = COALESCE(excluded.account_id, account_id),
                          source = excluded.source, class = excluded.class, level = excluded.level,
                          zone = excluded.zone, last_seen = excluded.last_seen""",
                   row + (p.get("class"), p.get("level"), p.get("zone"), now, now))
        n += 1
    db.commit()
    return n


def characters_of(db, battletag):
    """A BattleTag's characters, most recently seen first."""
    return [dict(r) for r in db.execute(
        "SELECT * FROM characters WHERE battletag = ? COLLATE NOCASE ORDER BY last_seen DESC", (battletag,))]


def ago(t, now=None):
    days = int(((now or time.time()) - t) // 86400)
    return "today" if days == 0 else "yesterday" if days == 1 else "%d days ago" % days
