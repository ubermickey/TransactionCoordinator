"""Deadline calculation, gate management, Claude contract extraction & review."""
import json, base64
from datetime import date, timedelta
from pathlib import Path
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

CONT_MAP = {  # deadline ID -> (contingency type, name, gate)
    "DL-010": ("investigation", "Investigation Contingency", "GATE-022"),
    "DL-020": ("appraisal", "Appraisal Contingency", "GATE-031"),
    "DL-030": ("loan", "Loan Contingency", "GATE-041"),
}
CONT_GATE = {v[0]: v[2] for v in CONT_MAP.values()}

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
                # Anchor dates and past milestones auto-verify — they're reference
                # points, not action items (e.g. acceptance date already happened)
                is_milestone = dl.get("is_anchor") or dl.get("type") == "FIXED"
                status = "verified" if (is_milestone and due <= date.today()) else "pending"
                c.execute(
                    "INSERT OR REPLACE INTO deadlines VALUES(?,?,?,?,?,?)",
                    (txn_id, did, dl["name"], dl["type"], due.isoformat(), status),
                )

        # Auto-populate contingencies from resolved deadlines
        _populate_contingencies(c, txn_id, cont, resolved)

def _populate_contingencies(c, txn_id: str, cont_data: dict, resolved: dict):
    """Insert contingency records from extracted contract data."""
    for did, (ctype, cname, _gate) in CONT_MAP.items():
        if did not in resolved:
            continue
        days = cont_data.get(DL_KEY.get(did)) or 17
        c.execute(
            "INSERT OR IGNORE INTO contingencies(txn,type,name,default_days,deadline_date)"
            " VALUES(?,?,?,?,?)",
            (txn_id, ctype, cname, days, resolved[did].isoformat()),
        )
        # Auto-populate inspection items for investigation contingencies
        if ctype == "investigation":
            row = c.execute(
                "SELECT id FROM contingencies WHERE txn=? AND type='investigation'",
                (txn_id,),
            ).fetchone()
            if row:
                _auto_populate_inspection_items(c, txn_id, row[0])


# ── Inspection Items ────────────────────────────────────────────────────────

INSPECTION_ITEMS = [
    "General Home Inspection",
    "Termite/Pest (WDO) Inspection",
    "Roof Inspection",
    "Chimney/Fireplace Inspection",
    "Sewer/Lateral Line Inspection",
    "Foundation/Structural Inspection",
    "Mold/Environmental Testing",
    "Electrical System",
    "Plumbing",
    "HVAC",
    "Geological/Soil Report",
]

CONDITIONAL_ITEMS = {
    "has_pool": "Pool/Spa Inspection",
    "is_pre_1978": "Lead-Based Paint Testing",
    "has_septic": "Septic Inspection",
}


def _auto_populate_inspection_items(c, txn_id: str, cid: int):
    """Insert default inspection checklist items for an investigation contingency."""
    existing = c.execute(
        "SELECT COUNT(*) FROM contingency_items WHERE contingency_id=?", (cid,)
    ).fetchone()[0]
    if existing > 0:
        return  # already populated

    items = list(INSPECTION_ITEMS)

    # Add conditional items based on property flags
    t = db.txn(c, txn_id)
    if t:
        import json
        props = json.loads(t.get("props") or "{}")
        for flag, item_name in CONDITIONAL_ITEMS.items():
            if props.get(flag):
                items.append(item_name)

    for idx, name in enumerate(items):
        c.execute(
            "INSERT INTO contingency_items(contingency_id,name,sort_order) VALUES(?,?,?)",
            (cid, name, idx),
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


# ── Contract Review (Clause-Level Analysis) ──────────────────────────────────

PLAYBOOK_DIR = Path(__file__).resolve().parent.parent / "playbooks"


def load_playbook(name: str = "california_rpa") -> dict:
    path = PLAYBOOK_DIR / f"{name}.yaml"
    if not path.exists():
        return {}
    import yaml
    return yaml.safe_load(path.read_text())


def _build_review_prompt(playbook: dict, txn_context: dict) -> str:
    """Build the clause-level contract review prompt."""
    positions = playbook.get("standard_positions", {})
    interactions = playbook.get("interaction_rules", [])

    prompt = (
        "You are a California real estate transaction coordinator reviewing a contract.\n"
        "Analyze this document CLAUSE BY CLAUSE against the playbook standards below.\n"
        "Understand how clauses INTERACT — e.g., a waived appraisal contingency on a financed "
        "purchase is dangerous even if each clause alone looks acceptable.\n\n"
    )

    # Transaction context
    if txn_context:
        prompt += "TRANSACTION CONTEXT:\n"
        for k, v in txn_context.items():
            if v:
                prompt += f"  {k}: {v}\n"
        prompt += "\n"

    # Playbook standards
    prompt += "PLAYBOOK STANDARDS:\n"
    for area, rules_data in positions.items():
        prompt += f"\n  {area.upper().replace('_', ' ')}:\n"
        prompt += f"    Acceptable: {rules_data.get('acceptable', 'N/A')}\n"
        if rules_data.get("yellow_if"):
            prompt += f"    Yellow if: {', '.join(rules_data['yellow_if'])}\n"
        if rules_data.get("red_flags"):
            prompt += f"    Red flags: {', '.join(rules_data['red_flags'])}\n"

    # Interaction rules
    if interactions:
        prompt += "\nINTERACTION RULES (check these combinations):\n"
        for rule in interactions:
            prompt += f"  - IF {rule['condition']} THEN {rule['risk']}: {rule['explanation']}\n"

    # Output format
    prompt += """
Return ONLY valid JSON (no markdown fences):
{
  "executive_summary": "2-3 sentence risk overview",
  "overall_risk": "RED or YELLOW or GREEN",
  "clauses": [
    {
      "area": "area name",
      "risk": "RED or YELLOW or GREEN",
      "finding": "what the contract says",
      "standard": "what the playbook expects",
      "suggestion": "recommended change (null for GREEN)"
    }
  ],
  "interactions": [
    {
      "condition": "what was triggered",
      "risk": "RED or YELLOW",
      "explanation": "why this combination is risky",
      "suggestion": "what to do about it"
    }
  ],
  "missing_items": ["items expected but not found in the contract"]
}
"""
    return prompt


def review_contract(pdf_path: str, txn_context: dict | None = None,
                    playbook_name: str = "california_rpa") -> dict:
    """Clause-level contract review using Claude + playbook standards."""
    import anthropic, time

    playbook = load_playbook(playbook_name)
    if not playbook:
        raise ValueError(f"Playbook '{playbook_name}' not found")

    prompt = _build_review_prompt(playbook, txn_context or {})
    data = base64.b64encode(open(pdf_path, "rb").read()).decode()
    client = anthropic.Anthropic()
    msg = [{"role": "user", "content": [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}},
        {"type": "text", "text": prompt},
    ]}]

    last_err = None
    for attempt in range(3):
        try:
            resp = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=8192, messages=msg)
            raw = resp.content[0].text
            result = _parse_json(raw)
            result["_raw"] = raw
            return result
        except anthropic.RateLimitError:
            wait = 30 * (attempt + 1)
            print(f"  Rate limited — waiting {wait}s (attempt {attempt + 1}/3)")
            time.sleep(wait)
        except json.JSONDecodeError as e:
            last_err = e
            if attempt < 2:
                time.sleep(5)
            else:
                raise ValueError(f"Could not parse review response as JSON after 3 attempts: {e}")
    raise RuntimeError(f"Contract review failed after 3 retries: {last_err}")


# ── Extraction ───────────────────────────────────────────────────────────────

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
