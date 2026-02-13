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
);
CREATE TABLE IF NOT EXISTS contingency_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contingency_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  status TEXT DEFAULT 'pending',
  inspector TEXT DEFAULT '',
  scheduled_date TEXT,
  completed_date TEXT,
  notes TEXT DEFAULT '',
  sort_order INTEGER DEFAULT 0,
  FOREIGN KEY(contingency_id) REFERENCES contingencies(id)
);
CREATE TABLE IF NOT EXISTS parties(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  txn TEXT,
  role TEXT,
  name TEXT,
  email TEXT DEFAULT '',
  phone TEXT DEFAULT '',
  company TEXT DEFAULT '',
  license_no TEXT DEFAULT '',
  notes TEXT DEFAULT '',
  created TEXT DEFAULT(datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS disclosures(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  txn TEXT,
  type TEXT,
  name TEXT,
  status TEXT DEFAULT 'pending',
  responsible TEXT DEFAULT '',
  due_date TEXT,
  ordered_date TEXT,
  received_date TEXT,
  reviewed_date TEXT,
  reviewer TEXT DEFAULT '',
  notes TEXT DEFAULT '',
  UNIQUE(txn, type)
);
CREATE TABLE IF NOT EXISTS field_annotations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  txn TEXT DEFAULT '',
  folder TEXT NOT NULL,
  filename TEXT NOT NULL,
  field_idx INTEGER NOT NULL,
  status TEXT NOT NULL,
  updated_at TEXT DEFAULT(datetime('now','localtime')),
  UNIQUE(txn, folder, filename, field_idx)
);
CREATE TABLE IF NOT EXISTS contracts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  folder TEXT NOT NULL,
  filename TEXT NOT NULL,
  scenario TEXT DEFAULT '',
  source_path TEXT DEFAULT '',
  page_count INTEGER DEFAULT 0,
  total_fields INTEGER DEFAULT 0,
  filled_fields INTEGER DEFAULT 0,
  unfilled_mandatory INTEGER DEFAULT 0,
  unfilled_optional INTEGER DEFAULT 0,
  verified_count INTEGER DEFAULT 0,
  status TEXT DEFAULT 'unverified',
  scanned_at TEXT DEFAULT(datetime('now','localtime')),
  verified_at TEXT,
  UNIQUE(folder, filename, scenario)
);
CREATE TABLE IF NOT EXISTS contract_fields(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id INTEGER NOT NULL,
  field_idx INTEGER NOT NULL,
  page INTEGER NOT NULL,
  category TEXT NOT NULL,
  field_name TEXT DEFAULT '',
  bbox TEXT DEFAULT '{}',
  mandatory INTEGER DEFAULT 1,
  is_filled INTEGER DEFAULT 0,
  status TEXT DEFAULT 'unverified',
  verified_at TEXT,
  notes TEXT DEFAULT '',
  FOREIGN KEY(contract_id) REFERENCES contracts(id),
  UNIQUE(contract_id, field_idx)
);
CREATE TABLE IF NOT EXISTS bug_reports(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  summary TEXT NOT NULL,
  description TEXT DEFAULT '',
  screenshot TEXT DEFAULT '',
  action_log TEXT DEFAULT '[]',
  url TEXT DEFAULT '',
  status TEXT DEFAULT 'open',
  created_at TEXT DEFAULT(datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS review_notes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  page TEXT NOT NULL,
  note TEXT NOT NULL,
  status TEXT DEFAULT 'pending',
  created_at TEXT DEFAULT(datetime('now','localtime')),
  resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS features(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  category TEXT DEFAULT '',
  description TEXT DEFAULT '',
  status TEXT DEFAULT 'active',
  files TEXT DEFAULT '[]',
  depends_on TEXT DEFAULT '[]',
  created_at TEXT DEFAULT(datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS cloud_approvals(
  txn TEXT PRIMARY KEY,
  granted_at TEXT,
  expires_at TEXT,
  granted_by TEXT DEFAULT 'ui',
  note TEXT DEFAULT '',
  revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS cloud_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  txn TEXT,
  service TEXT NOT NULL,
  operation TEXT NOT NULL,
  endpoint TEXT DEFAULT '',
  model TEXT DEFAULT '',
  approved INTEGER DEFAULT 0,
  outcome TEXT DEFAULT 'blocked',
  status_code INTEGER,
  latency_ms INTEGER,
  request_bytes INTEGER DEFAULT 0,
  response_bytes INTEGER DEFAULT 0,
  error TEXT DEFAULT '',
  meta TEXT DEFAULT '{}',
  created_at TEXT DEFAULT(datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS contract_reviews(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  txn TEXT NOT NULL,
  doc_code TEXT DEFAULT '',
  playbook TEXT DEFAULT 'california_rpa',
  overall_risk TEXT DEFAULT 'GREEN',
  executive_summary TEXT DEFAULT '',
  clauses TEXT DEFAULT '[]',
  interactions TEXT DEFAULT '[]',
  missing_items TEXT DEFAULT '[]',
  raw_response TEXT DEFAULT '',
  created_at TEXT DEFAULT(datetime('now','localtime')),
  FOREIGN KEY(txn) REFERENCES txns(id)
);"""

# Columns added after initial schema — migrated on connect
_MIGRATIONS = [
    ("txns", "txn_type", "TEXT DEFAULT 'sale'"),
    ("txns", "party_role", "TEXT DEFAULT 'listing'"),
    ("txns", "brokerage", "TEXT DEFAULT ''"),
    ("txns", "props", "TEXT DEFAULT '{}'"),
    ("docs", "file_path", "TEXT DEFAULT ''"),
    ("docs", "folder", "TEXT DEFAULT ''"),
    ("docs", "filename", "TEXT DEFAULT ''"),
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
