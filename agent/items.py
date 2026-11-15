"""What the assistant has learned about items: their exact stats as the game computes them (ITEM_INFO),
kept in ~/.deckagent/items.db so each item is asked about once, not every time.

An item the game hasn't seen this session costs a request to Blizzard's server; the game paces those
(addons/DeckAgent/Inventory.lua) and this keeps the count down further: answers are reused for
MAX_AGE_DAYS (in case Forever changes an item), and only item IDs that exist in the game database are
ever asked about (invalid IDs are the ones that used to get clients disconnected for flooding).
"""
import json, os, sqlite3, time

from . import common

PATH = os.path.join(common.HOME, "items.db")
MAX_AGE_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS item_info (id INTEGER, suffix INTEGER, at REAL, info TEXT, PRIMARY KEY (id, suffix));
"""


def connect(path=None):
    path = path or PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def get(db, item_id, suffix=0, now=None):
    row = db.execute("SELECT at, info FROM item_info WHERE id = ? AND suffix = ?", (item_id, suffix or 0)).fetchone()
    if not row or (now or time.time()) - row[0] > MAX_AGE_DAYS * 86400:
        return None
    return json.loads(row[1])


def put(db, item_id, suffix, info, now=None):
    db.execute("INSERT OR REPLACE INTO item_info VALUES (?,?,?,?)",
               (item_id, suffix or 0, now or time.time(), json.dumps(info)))
    db.commit()
