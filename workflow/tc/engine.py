"""Deadline calculation, gate management, Claude contract extraction."""
import json, base64
from datetime import date, timedelta
from . import db, rules

# ── Dates ────────────────────────────────────────────────────────────────────

def add_days(start: date, n: int, business=False) -> date:
    if not business:
        return start + timedelta(days=n)
    d, step = start, 1 if n > 0 else -1
    remaining = abs(n)
    while remaining:
        d += timedelta(days=step)
        if d.weekday() < 5:
            remaining -= 1
    return d

# ── Deadlines ────────────────────────────────────────────────────────────────

DL_KEY = {                              # map deadline IDs -> extraction keys
    "DL-001": "deposit_days",
    "DL-010": "investigation_days",
    "DL-020": "appraisal_days",
    "DL-030": "loan_days",
}

def calc_deadlines(txn_id: str, anchor: date, data: dict):
    dates = data.get("dates", {})
    cont = data.get("contingencies", {})
    coe_str = dates.get("close_of_escrow")
    resolved = {"DL-000": anchor}
    if coe_str:
        resolved["DL-060"] = resolved["close_of_escrow"] = date.fromisoformat(coe_str)
    if hoa := data.get("hoa_document_delivery"):
        resolved["hoa_document_delivery_date"] = date.fromisoformat(hoa)

    with db.conn() as c:
        for dl in rules.deadlines():
            did, due = dl["id"], None

            if dl.get("is_anchor"):
                due = anchor
            elif did == "DL-060" and "DL-060" in resolved:
                due = resolved["DL-060"]
            elif dl.get("fixed_date") and "DL-060" in resolved:   # 1099-S
                due = date(resolved["DL-060"].year + 1, 1, 31)
            elif dl.get("fixed_years") and "DL-060" in resolved:
                coe = resolved["DL-060"]
                due = coe.replace(year=coe.year + dl["fixed_years"])
            elif "default_offset_days" in dl or "fixed_days" in dl:
                ref = dl.get("offset_from", "DL-000")
                base = resolved.get(ref)
                if not base:
                    continue
                days = cont.get(DL_KEY.get(did), dl.get("default_offset_days", dl.get("fixed_days", 0)))
                biz = dl.get("day_type") == "business_days"
                if dl.get("direction") == "before":
                    days = -abs(days)
                due = add_days(base, days, biz)

            if due:
                resolved[did] = due
                c.execute(
                    "INSERT OR REPLACE INTO deadlines VALUES(?,?,?,?,?,?)",
                    (txn_id, did, dl["name"], dl["type"], due.isoformat(), "pending"),
                )

# ── Gates ────────────────────────────────────────────────────────────────────

def init_gates(txn_id: str):
    with db.conn() as c:
        for g in rules.gates():
            c.execute("INSERT OR IGNORE INTO gates(txn,gid) VALUES(?,?)", (txn_id, g["id"]))


def verify(txn_id: str, gate_id: str, notes: str = ""):
    with db.conn() as c:
        c.execute(
            "UPDATE gates SET status='verified', verified=datetime('now','localtime'), notes=? "
            "WHERE txn=? AND gid=?",
            (notes, txn_id, gate_id),
        )


def gate_rows(txn_id: str) -> list[dict]:
    with db.conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM gates WHERE txn=? ORDER BY gid", (txn_id,))]


def deadline_rows(txn_id: str) -> list[dict]:
    with db.conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM deadlines WHERE txn=? ORDER BY due", (txn_id,))]

# ── Extraction ───────────────────────────────────────────────────────────────

PROMPT = """\
Extract all contract terms from this real estate purchase agreement.
Return ONLY valid JSON (no markdown):
{
  "parties": {"buyer":"","seller":"","buyer_agent":"","seller_agent":"","escrow_company":""},
  "property": {"address":"","city":"","state":"CA","zip":"","apn":""},
  "financial": {"purchase_price":0,"deposit":0,"loan_amount":0,"down_payment":0},
  "dates": {"acceptance":"YYYY-MM-DD","close_of_escrow":"YYYY-MM-DD"},
  "contingencies": {"investigation_days":17,"appraisal_days":17,"loan_days":17,"deposit_days":3},
  "hoa": false,
  "flags": []
}
Use actual values from the document. ISO dates. null for missing values."""


def extract(pdf_path: str) -> dict:
    import anthropic

    data = base64.b64encode(open(pdf_path, "rb").read()).decode()
    resp = anthropic.Anthropic().messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        messages=[{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}},
            {"type": "text", "text": PROMPT},
        ]}],
    )
    return json.loads(resp.content[0].text)
