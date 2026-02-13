"""Cloud approval + event logging helpers.

All cloud-bound operations should pass through this module so approval policy
and audit logging remain consistent.
"""
import json
from datetime import datetime, timedelta

DEFAULT_APPROVAL_TTL_MIN = 30
APPROVAL_REQUIRED = True
BLOCK_NO_TXN = True


class CloudApprovalRequired(RuntimeError):
    """Raised when a cloud operation is attempted without active approval."""

    def __init__(self, txn: str, service: str, operation: str, message: str = ""):
        super().__init__(message or "cloud approval required")
        self.txn = txn
        self.service = service
        self.operation = operation


def _now() -> datetime:
    return datetime.now()


def _now_str() -> str:
    return _now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _normalize_approval(txn: str, row: dict | None) -> dict:
    base = {
        "txn": txn,
        "granted_at": None,
        "expires_at": None,
        "granted_by": "ui",
        "note": "",
        "revoked_at": None,
        "active": False,
        "remaining_seconds": 0,
    }
    if not row:
        return base

    base.update(dict(row))
    expires = _parse_dt(base.get("expires_at"))
    revoked = base.get("revoked_at")
    if expires and not revoked and expires > _now():
        base["active"] = True
        base["remaining_seconds"] = max(0, int((expires - _now()).total_seconds()))
    return base


def get_approval(c, txn: str) -> dict:
    row = c.execute("SELECT * FROM cloud_approvals WHERE txn=?", (txn,)).fetchone()
    return _normalize_approval(txn, dict(row) if row else None)


def is_approved(c, txn: str) -> bool:
    return bool(get_approval(c, txn).get("active"))


def grant_approval(c, txn: str, ttl_min: int = DEFAULT_APPROVAL_TTL_MIN,
                   granted_by: str = "ui", note: str = "") -> dict:
    minutes = DEFAULT_APPROVAL_TTL_MIN
    try:
        minutes = int(ttl_min)
    except (TypeError, ValueError):
        minutes = DEFAULT_APPROVAL_TTL_MIN
    minutes = min(1440, max(1, minutes))

    now = _now()
    expires = now + timedelta(minutes=minutes)
    c.execute(
        "INSERT INTO cloud_approvals(txn, granted_at, expires_at, granted_by, note, revoked_at)"
        " VALUES(?,?,?,?,?,NULL)"
        " ON CONFLICT(txn) DO UPDATE SET"
        " granted_at=excluded.granted_at,"
        " expires_at=excluded.expires_at,"
        " granted_by=excluded.granted_by,"
        " note=excluded.note,"
        " revoked_at=NULL",
        (
            txn,
            now.strftime("%Y-%m-%d %H:%M:%S"),
            expires.strftime("%Y-%m-%d %H:%M:%S"),
            (granted_by or "ui")[:40],
            (note or "")[:500],
        ),
    )
    return get_approval(c, txn)


def revoke_approval(c, txn: str, note: str = "") -> dict:
    row = c.execute("SELECT * FROM cloud_approvals WHERE txn=?", (txn,)).fetchone()
    if row:
        prev_note = (row["note"] or "").strip()
        merged_note = prev_note
        if note:
            merged_note = (prev_note + " | " + note).strip(" |") if prev_note else note
        c.execute(
            "UPDATE cloud_approvals SET revoked_at=?, note=? WHERE txn=?",
            (_now_str(), merged_note[:500], txn),
        )
    else:
        c.execute(
            "INSERT INTO cloud_approvals(txn, granted_by, note, revoked_at)"
            " VALUES(?,?,?,?)",
            (txn, "ui", (note or "")[:500], _now_str()),
        )
    return get_approval(c, txn)


def require_approval(c, txn: str, service: str, operation: str) -> bool:
    if not APPROVAL_REQUIRED:
        return True
    if BLOCK_NO_TXN and not txn:
        raise CloudApprovalRequired(
            txn, service, operation, "txn_id is required for cloud calls"
        )
    if not is_approved(c, txn):
        raise CloudApprovalRequired(
            txn, service, operation, "cloud approval required"
        )
    return True


def log_cloud_event(
    c,
    *,
    txn: str | None,
    service: str,
    operation: str,
    endpoint: str = "",
    model: str = "",
    approved: int = 0,
    outcome: str = "blocked",
    status_code: int | None = None,
    latency_ms: int | None = None,
    request_bytes: int = 0,
    response_bytes: int = 0,
    error: str = "",
    meta: dict | None = None,
) -> dict:
    payload = json.dumps(meta or {}, separators=(",", ":"), sort_keys=True)
    c.execute(
        "INSERT INTO cloud_events("
        " txn, service, operation, endpoint, model, approved, outcome, status_code,"
        " latency_ms, request_bytes, response_bytes, error, meta"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            txn or "",
            (service or "")[:80],
            (operation or "")[:80],
            (endpoint or "")[:255],
            (model or "")[:120],
            1 if approved else 0,
            (outcome or "blocked")[:20],
            status_code,
            latency_ms,
            int(request_bytes or 0),
            int(response_bytes or 0),
            (error or "")[:500],
            payload,
        ),
    )
    rid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    row = c.execute("SELECT * FROM cloud_events WHERE id=?", (rid,)).fetchone()
    return dict(row) if row else {}
