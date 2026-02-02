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
                days = cont.get(DL_KEY.get(did)) or dl.get("default_offset_days") or dl.get("fixed_days", 0)
                if days is None:
                    continue
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

# ── Phase Order ──────────────────────────────────────────────────────────────

SALE_PHASE_ORDER = [p["id"] for p in rules.phases()]
LEASE_PHASE_ORDER = [p["id"] for p in rules.lease_phases()]


def phase_order(txn_type: str = "sale") -> list[str]:
    """Return the phase list for a transaction type."""
    if txn_type == "lease":
        return LEASE_PHASE_ORDER
    return SALE_PHASE_ORDER


def default_phase(txn_type: str = "sale") -> str:
    """Return the first phase for a transaction type."""
    return phase_order(txn_type)[0]

# ── Gates ────────────────────────────────────────────────────────────────────

def init_gates(txn_id: str, brokerage: str = ""):
    """Create gate records. Includes DE-specific gates when brokerage matches."""
    with db.conn() as c:
        for g in rules.gates():
            c.execute("INSERT OR IGNORE INTO gates(txn,gid) VALUES(?,?)", (txn_id, g["id"]))
        if brokerage:
            for g in rules.de_gates(brokerage):
                c.execute("INSERT OR IGNORE INTO gates(txn,gid) VALUES(?,?)", (txn_id, g["id"]))


def verify(txn_id: str, gate_id: str, notes: str = ""):
    with db.conn() as c:
        c.execute(
            "UPDATE gates SET status='verified', verified=datetime('now','localtime'), notes=? "
            "WHERE txn=? AND gid=?",
            (notes, txn_id, gate_id),
        )
        db.log(c, txn_id, "gate_verified", gate_id)


def gate_rows(txn_id: str) -> list[dict]:
    with db.conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM gates WHERE txn=? ORDER BY gid", (txn_id,))]


def deadline_rows(txn_id: str) -> list[dict]:
    with db.conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM deadlines WHERE txn=? ORDER BY due", (txn_id,))]

# ── Phase Advancement ────────────────────────────────────────────────────────

def _txn_phase_order(txn_id: str) -> list[str]:
    """Get the phase order for a transaction based on its type."""
    with db.conn() as c:
        t = db.txn(c, txn_id)
    return phase_order(t.get("txn_type", "sale"))


def can_advance(txn_id: str) -> tuple[bool, list[str]]:
    """Check if all gates for current phase are verified."""
    with db.conn() as c:
        t = db.txn(c, txn_id)
    phase = t["phase"]
    txn_type = t.get("txn_type", "sale")
    brokerage = t.get("brokerage", "")

    # Check standard phases
    all_phases = rules.phases() if txn_type != "lease" else rules.lease_phases()
    phase_def = next((p for p in all_phases if p["id"] == phase), None)
    if not phase_def:
        return False, ["Unknown phase"]

    # Combine standard + brokerage gates for lookup
    all_gates_defs = {g["id"]: g for g in rules.gates()}
    if brokerage:
        for g in rules.de_gates(brokerage):
            all_gates_defs[g["id"]] = g

    blocking = []
    for g in gate_rows(txn_id):
        info = all_gates_defs.get(g["gid"])
        if info and info["phase"] == phase and g["status"] != "verified" and info["type"] == "HARD_GATE":
            blocking.append(f"{g['gid']}: {info['name']}")
    return len(blocking) == 0, blocking


def advance_phase(txn_id: str) -> str | None:
    """Move to next phase if gates allow. Returns new phase or None."""
    ok, blocking = can_advance(txn_id)
    if not ok:
        return None
    with db.conn() as c:
        t = db.txn(c, txn_id)
    po = phase_order(t.get("txn_type", "sale"))
    idx = po.index(t["phase"]) if t["phase"] in po else -1
    if idx + 1 >= len(po):
        return None
    new_phase = po[idx + 1]
    with db.conn() as c:
        c.execute("UPDATE txns SET phase=?, updated=datetime('now','localtime') WHERE id=?", (new_phase, txn_id))
        db.log(c, txn_id, "phase_advanced", f"{t['phase']} -> {new_phase}")
    return new_phase

# ── Extraction ───────────────────────────────────────────────────────────────

def _build_prompt(form_template: dict | None = None) -> str:
    """Build extraction prompt, optionally using a CAR form template."""
    base = {
        "parties": {"buyer":"","seller":"","buyer_agent":"","seller_agent":"","escrow_company":""},
        "property": {"address":"","city":"","state":"CA","zip":"","apn":""},
        "financial": {"purchase_price":0,"deposit":0,"loan_amount":0,"down_payment":0},
        "dates": {"acceptance":"YYYY-MM-DD","close_of_escrow":"YYYY-MM-DD"},
        "contingencies": {"investigation_days":17,"appraisal_days":17,"loan_days":17,"deposit_days":3},
        "hoa": False,
        "flags": [],
    }
    prompt = "Extract all contract terms from this document.\nReturn ONLY valid JSON (no markdown):\n"
    prompt += json.dumps(base, indent=2) + "\n"
    prompt += "Use actual values from the document. ISO dates. null for missing values.\n"
    if form_template:
        prompt += f"\nThis is a CAR {form_template['form']['code']} ({form_template['form']['name']}).\n"
        prompt += "Field locations:\n"
        for fid, f in form_template.get("fields", {}).items():
            prompt += f"  - Section {f.get('section','?')}: {f.get('label',fid)} -> {f.get('maps_to',fid)}\n"
        if form_template.get("flags"):
            prompt += "\nAlso check for:\n"
            for flag in form_template["flags"]:
                prompt += f"  - {flag}\n"
    return prompt


def _parse_json(text: str) -> dict:
    """Extract JSON from Claude response (handles fences, prose, etc.)."""
    text = text.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            text = text[start:end]
    return json.loads(text)


def extract(pdf_path: str, form_type: str | None = None) -> dict:
    import anthropic, time

    template = rules.form_template(form_type) if form_type else None
    prompt = _build_prompt(template)
    data = base64.b64encode(open(pdf_path, "rb").read()).decode()
    client = anthropic.Anthropic()
    msg = [{"role": "user", "content": [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}},
        {"type": "text", "text": prompt},
    ]}]

    for attempt in range(3):
        try:
            resp = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=4096, messages=msg)
            return _parse_json(resp.content[0].text)
        except anthropic.RateLimitError:
            wait = 30 * (attempt + 1)
            print(f"  Rate limited — waiting {wait}s (attempt {attempt + 1}/3)")
            time.sleep(wait)
        except json.JSONDecodeError as e:
            raise ValueError(f"Could not parse Claude response as JSON: {e}")
    raise RuntimeError("Rate limit exceeded after 3 retries")
