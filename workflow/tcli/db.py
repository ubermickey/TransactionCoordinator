"""SQLite persistence — single file, zero config."""
import sqlite3
from contextlib import contextmanager
from os import environ
from pathlib import Path

DB = Path(environ.get("TC_DATA_DIR", str(Path.home() / ".tc"))).expanduser() / "tc.db"

SCHEMA = """\
CREATE TABLE IF NOT EXISTS txns(
  id TEXT PRIMARY KEY, address TEXT, phase TEXT DEFAULT 'PRE_CONTRACT',
  jurisdictions TEXT DEFAULT '[]', data TEXT DEFAULT '{}',
  created TEXT DEFAULT(datetime('now','localtime')),
  updated TEXT DEFAULT(datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS gates(
  txn TEXT, gid TEXT, status TEXT DEFAULT 'pending',
  triggered TEXT, verified TEXT, notes TEXT,
  PRIMARY KEY(txn, gid)
);
CREATE TABLE IF NOT EXISTS deadlines(
  txn TEXT, did TEXT, name TEXT, type TEXT, due TEXT,
  status TEXT DEFAULT 'pending',
  PRIMARY KEY(txn, did)
);"""


@contextmanager
def conn():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    try:
        yield c
        c.commit()
    finally:
        c.close()


def txn(c, tid):
    r = c.execute("SELECT * FROM txns WHERE id=?", (tid,)).fetchone()
    return dict(r) if r else None


def active(c):
    r = c.execute("SELECT * FROM txns ORDER BY created DESC LIMIT 1").fetchone()
    return dict(r) if r else None
