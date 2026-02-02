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
);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, txn TEXT,
  action TEXT, detail TEXT,
  ts TEXT DEFAULT(datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS docs(
  txn TEXT, code TEXT, name TEXT, phase TEXT,
  status TEXT DEFAULT 'required',
  received TEXT, verified TEXT, notes TEXT,
  PRIMARY KEY(txn, code)
);
CREATE TABLE IF NOT EXISTS sig_reviews(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  txn TEXT,
  doc_code TEXT,
  folder TEXT,
  filename TEXT,
  field_name TEXT,
  field_type TEXT DEFAULT 'signature',
  page INTEGER,
  bbox TEXT,
  is_filled INTEGER DEFAULT 0,
  review_status TEXT DEFAULT 'pending',
  reviewer_note TEXT DEFAULT '',
  source TEXT DEFAULT 'auto',
  reviewed_at TEXT,
  UNIQUE(txn, doc_code, field_name, page)
);
CREATE TABLE IF NOT EXISTS envelope_tracking(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  txn TEXT,
  sig_review_id INTEGER,
  provider TEXT DEFAULT 'docusign',
  envelope_id TEXT,
  recipient_email TEXT,
  recipient_name TEXT,
  status TEXT DEFAULT 'created',
  sent_at TEXT,
  viewed_at TEXT,
  signed_at TEXT,
  last_checked TEXT,
  FOREIGN KEY(sig_review_id) REFERENCES sig_reviews(id)
);
CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  txn TEXT,
  channel TEXT DEFAULT 'email',
  to_addr TEXT,
  subject TEXT,
  body TEXT,
  status TEXT DEFAULT 'queued',
  created_at TEXT DEFAULT(datetime('now','localtime')),
  sent_at TEXT,
  related_sig_id INTEGER,
  related_envelope_id INTEGER
);
CREATE TABLE IF NOT EXISTS contingencies(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  txn TEXT,
  type TEXT,
  name TEXT,
  status TEXT DEFAULT 'active',
  default_days INTEGER,
  deadline_date TEXT,
  removed_at TEXT,
  waived_at TEXT,
  nbp_sent_at TEXT,
  nbp_expires_at TEXT,
  cr1_sig_review_id INTEGER,
  notes TEXT DEFAULT '',
  UNIQUE(txn, type)
);"""

# Columns added after initial schema — migrated on connect
_MIGRATIONS = [
    ("txns", "txn_type", "TEXT DEFAULT 'sale'"),
    ("txns", "party_role", "TEXT DEFAULT 'listing'"),
    ("txns", "brokerage", "TEXT DEFAULT ''"),
    ("txns", "props", "TEXT DEFAULT '{}'"),
    ("sig_reviews", "signer_email", "TEXT DEFAULT ''"),
    ("sig_reviews", "signer_name", "TEXT DEFAULT ''"),
    ("sig_reviews", "last_reminder_at", "TEXT"),
    ("sig_reviews", "reminder_count", "INTEGER DEFAULT 0"),
]


def _migrate(c: sqlite3.Connection):
    """Add new columns to existing tables if missing."""
    for table, col, typedef in _MIGRATIONS:
        try:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")
        except sqlite3.OperationalError:
            pass  # column already exists


@contextmanager
def conn():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    _migrate(c)
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


def log(c, txn_id: str, action: str, detail: str = ""):
    c.execute("INSERT INTO audit(txn,action,detail) VALUES(?,?,?)", (txn_id, action, detail))
