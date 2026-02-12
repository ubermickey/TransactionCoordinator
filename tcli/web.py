"""Flask web UI for Transaction Coordinator."""
import json
import os
import re
import time
import urllib.request
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import yaml
from flask import Flask, Response, jsonify, render_template, request, send_from_directory
from werkzeug.exceptions import RequestEntityTooLarge

from . import cloud_guard, checklist, contract_scanner, db, doc_versions, engine, integrations, rules
from .engine import CONT_GATE

app = Flask(__name__)
_MAX_UPLOAD_MB = 25
try:
    _MAX_UPLOAD_MB = max(1, int(os.environ.get("TC_MAX_UPLOAD_MB", "25")))
except (TypeError, ValueError):
    _MAX_UPLOAD_MB = 25
app.config["MAX_CONTENT_LENGTH"] = _MAX_UPLOAD_MB * 1024 * 1024
_CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _txn_dict(t: dict) -> dict:
    """Normalise a txn row for JSON output."""
    t = dict(t)
    t["data"] = json.loads(t.get("data") or "{}")
    t["jurisdictions"] = json.loads(t.get("jurisdictions") or "[]")
    t["props"] = json.loads(t.get("props") or "{}")
    return t


def _doc_stats(c, tid: str) -> dict:
    rows = c.execute(
        "SELECT status, COUNT(*) as cnt FROM docs WHERE txn=? GROUP BY status",
        (tid,),
    ).fetchall()
    stats = {r["status"]: r["cnt"] for r in rows}
    total = sum(stats.values())
    recv = stats.get("received", 0) + stats.get("verified", 0)
    return {"total": total, "received": recv, "stats": stats}


def _is_safe_component(value: str) -> bool:
    """Return True only for single path components (no traversal/separators)."""
    if not value or "\x00" in value:
        return False
    p = Path(value)
    return value == p.name and ".." not in p.parts and "/" not in value and "\\" not in value


def _validate_doc_path_inputs(folder: str, filename: str, require_pdf: bool = False):
    """Validate folder/filename values used by document package endpoints."""
    if not _is_safe_component(folder) or not _is_safe_component(filename):
        return jsonify({"error": "invalid folder or filename"}), 400
    if require_pdf and Path(filename).suffix.lower() != ".pdf":
        return jsonify({"error": "only PDF files are allowed"}), 400
    return None


def _validate_pdf_upload(file_storage):
    """Quick content sniffing to reject obvious non-PDF uploads."""
    try:
        head = file_storage.stream.read(5)
        file_storage.stream.seek(0)
    except Exception:
        return False
    return head == b"%PDF-"


def _cloud_blocked_response(tid: str, message: str = "cloud approval required"):
    return jsonify({
        "error": message,
        "code": "cloud_approval_required",
        "requires_approval": True,
        "txn": tid,
    }), 403


@app.errorhandler(RequestEntityTooLarge)
def upload_too_large(_err):
    return jsonify({"error": f"File too large. Max upload size is {_MAX_UPLOAD_MB} MB."}), 413


@app.after_request
def set_security_headers(resp):
    resp.headers.setdefault("Content-Security-Policy", _CSP_POLICY)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    return resp


# ── Pages ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ── Address Validation ───────────────────────────────────────────────────

@app.route("/api/validate-address", methods=["POST"])
def validate_address():
    """Validate address via US Census Bureau geocoder and return canonical form."""
    body = request.json or {}
    address = (body.get("address") or "").strip()
    if not address:
        return jsonify({"valid": False, "error": "No address provided"}), 400

    try:
        encoded = urllib.parse.quote(address)
        url = (
            f"https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
            f"?address={encoded}&benchmark=Public_AR_Current&format=json"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "TC/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())

        matches = data.get("result", {}).get("addressMatches", [])
        if not matches:
            return jsonify({"valid": False, "error": "Address not found"})

        m = matches[0]
        parts = m.get("addressComponents", {})
        matched = m.get("matchedAddress", address)
        coords = m.get("coordinates", {})

        # Also get county from the geographies endpoint
        county = ""
        try:
            geo_url = (
                f"https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
                f"?address={encoded}&benchmark=Public_AR_Current"
                f"&vintage=Current_Current&format=json"
            )
            geo_req = urllib.request.Request(geo_url, headers={"User-Agent": "TC/1.0"})
            with urllib.request.urlopen(geo_req, timeout=6) as geo_resp:
                geo_data = json.loads(geo_resp.read())
            geo_matches = geo_data.get("result", {}).get("addressMatches", [])
            if geo_matches:
                geos = geo_matches[0].get("geographies", {})
                counties = geos.get("Counties", [])
                if counties:
                    county = counties[0].get("NAME", "")
        except Exception:
            pass

        return jsonify({
            "valid": True,
            "matched_address": matched,
            "street": f"{parts.get('preQualifier', '')} {parts.get('preDirection', '')} {parts.get('streetName', '')} {parts.get('suffixType', '')} {parts.get('suffixDirection', '')}".strip(),
            "city": parts.get("city", ""),
            "state": parts.get("state", ""),
            "zip": parts.get("zip", ""),
            "county": county,
            "lat": coords.get("y"),
            "lng": coords.get("x"),
        })
    except Exception as e:
        # Don't block creation on geocoder failure — just warn
        return jsonify({"valid": None, "error": f"Validation unavailable: {str(e)}"})


# ── Transactions ─────────────────────────────────────────────────────────────

@app.route("/api/txns")
def list_txns():
    with db.conn() as c:
        rows = c.execute("SELECT * FROM txns ORDER BY created DESC").fetchall()
        out = []
        for r in rows:
            t = _txn_dict(r)
            gs = engine.gate_rows(t["id"])
            t["gate_count"] = len(gs)
            t["gates_verified"] = sum(1 for g in gs if g["status"] == "verified")
            t["doc_stats"] = _doc_stats(c, t["id"])
            out.append(t)
    return jsonify(out)


@app.route("/api/txns", methods=["POST"])
def create_txn():
    body = request.json or {}
    address = (body.get("address") or "").strip()
    if not address:
        return jsonify({"error": "address required"}), 400
    txn_type = body.get("type", "sale")
    party_role = body.get("role", "listing")
    brokerage = body.get("brokerage", "")
    acceptance_date = body.get("acceptance_date", "")
    acceptance_dt = None
    if acceptance_date:
        try:
            acceptance_dt = date.fromisoformat(acceptance_date)
            acceptance_date = acceptance_dt.isoformat()
        except ValueError:
            return jsonify({"error": "acceptance_date must be YYYY-MM-DD"}), 400

    tid = uuid4().hex[:8]
    city = address.split(",")[1].strip() if "," in address else ""
    juris = rules.resolve(city)
    initial_phase = engine.default_phase(txn_type)

    # Build initial data with dates
    txn_data = {
        "dates": {},
        "contingencies": {"investigation_days": 17, "appraisal_days": 17,
                          "loan_days": 21, "deposit_days": 3},
    }
    if acceptance_dt:
        txn_data["dates"]["acceptance"] = acceptance_date
        # Default COE 30 days from acceptance for CA transactions
        coe = (acceptance_dt + timedelta(days=30)).isoformat()
        txn_data["dates"]["close_of_escrow"] = coe
        txn_data["dates"]["_confirmed"] = False  # unconfirmed until contracts arrive

    with db.conn() as c:
        c.execute(
            "INSERT INTO txns(id,address,phase,jurisdictions,txn_type,party_role,brokerage,props,data) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (tid, address, initial_phase, json.dumps(juris), txn_type, party_role,
             brokerage, "{}", json.dumps(txn_data)),
        )
        db.log(c, tid, "created", f"{address} type={txn_type} role={party_role} brokerage={brokerage}")

    engine.init_gates(tid, brokerage=brokerage)

    # Auto-calculate deadlines from acceptance date
    if acceptance_dt:
        try:
            engine.calc_deadlines(tid, acceptance_dt, txn_data)
        except Exception:
            pass  # don't block creation on deadline calc failure

    # Populate docs if brokerage set
    if brokerage:
        docs = checklist.resolve(txn_type, party_role, brokerage, {})
        with db.conn() as c:
            for d in docs:
                c.execute(
                    "INSERT OR IGNORE INTO docs(txn,code,name,phase,status) VALUES(?,?,?,?,?)",
                    (tid, d["code"], d["name"], d["phase"], "required"),
                )
            db.log(c, tid, "docs_populated", f"{len(docs)} documents from {brokerage}")

    # Auto-populate default party placeholders
    _init_default_parties(tid, txn_type, party_role, brokerage)

    with db.conn() as c:
        t = _txn_dict(db.txn(c, tid))
    return jsonify(t), 201


def _init_default_parties(tid: str, txn_type: str, party_role: str, brokerage: str):
    """Create placeholder party records for a new transaction."""
    if txn_type == "lease":
        roles = [
            ("seller", "Landlord (TBD)"),
            ("buyer", "Tenant (TBD)"),
        ]
    else:
        roles = [
            ("seller", "Seller (TBD)"),
            ("buyer", "Buyer (TBD)"),
        ]

    if party_role == "listing":
        roles.append(("seller_agent", "Listing Agent (You)"))
        roles.append(("buyer_agent", "Buyer's Agent (TBD)"))
    else:
        roles.append(("buyer_agent", "Buyer's Agent (You)"))
        roles.append(("seller_agent", "Listing Agent (TBD)"))

    roles.extend([
        ("escrow_officer", "Escrow Officer (TBD)"),
        ("title_rep", "Title Representative (TBD)"),
        ("lender", "Lender (TBD)"),
    ])

    with db.conn() as c:
        for role, name in roles:
            company = brokerage.replace("_", " ").title() if role.endswith("_agent") and "(You)" in name else ""
            c.execute(
                "INSERT OR IGNORE INTO parties(txn,role,name,company)"
                " VALUES(?,?,?,?)",
                (tid, role, name, company),
            )


@app.route("/api/txns/<tid>")
def get_txn(tid):
    with db.conn() as c:
        row = db.txn(c, tid)
        if not row:
            return jsonify({"error": "not found"}), 404
        t = _txn_dict(row)
        t["doc_stats"] = _doc_stats(c, tid)
    gs = engine.gate_rows(tid)
    t["gate_count"] = len(gs)
    t["gates_verified"] = sum(1 for g in gs if g["status"] == "verified")
    t["deadlines"] = engine.deadline_rows(tid)
    return jsonify(t)


@app.route("/api/txns/<tid>", methods=["DELETE"])
def delete_txn(tid):
    with db.conn() as c:
        t = db.txn(c, tid)
        if not t:
            return jsonify({"error": "not found"}), 404
        db.log(c, tid, "deleted", t["address"])
        c.execute("DELETE FROM field_annotations WHERE txn=?", (tid,))
        c.execute("DELETE FROM disclosures WHERE txn=?", (tid,))
        c.execute("DELETE FROM parties WHERE txn=?", (tid,))
        c.execute(
            "DELETE FROM contingency_items"
            " WHERE contingency_id IN (SELECT id FROM contingencies WHERE txn=?)",
            (tid,),
        )
        c.execute("DELETE FROM contingencies WHERE txn=?", (tid,))
        c.execute("DELETE FROM outbox WHERE txn=?", (tid,))
        c.execute("DELETE FROM envelope_tracking WHERE txn=?", (tid,))
        c.execute("DELETE FROM sig_reviews WHERE txn=?", (tid,))
        c.execute("DELETE FROM docs WHERE txn=?", (tid,))
        c.execute("DELETE FROM deadlines WHERE txn=?", (tid,))
        c.execute("DELETE FROM gates WHERE txn=?", (tid,))
        c.execute("DELETE FROM txns WHERE id=?", (tid,))
    return jsonify({"ok": True})


# ── Documents ────────────────────────────────────────────────────────────────

@app.route("/api/txns/<tid>/docs")
def get_docs(tid):
    with db.conn() as c:
        rows = c.execute(
            "SELECT * FROM docs WHERE txn=? ORDER BY phase, code", (tid,)
        ).fetchall()
    return jsonify([dict(r) for r in rows])


def _doc_action(tid, code, status, field):
    with db.conn() as c:
        row = c.execute("SELECT * FROM docs WHERE txn=? AND code=?", (tid, code)).fetchone()
        if not row:
            return jsonify({"error": "doc not found"}), 404
        if status == "na":
            c.execute("UPDATE docs SET status='na', notes=? WHERE txn=? AND code=?",
                       (request.json.get("note", "") if request.json else "", tid, code))
        else:
            c.execute(
                f"UPDATE docs SET status=?, {field}=datetime('now','localtime') WHERE txn=? AND code=?",
                (status, tid, code),
            )
        db.log(c, tid, f"doc_{status}", code)
        updated = c.execute("SELECT * FROM docs WHERE txn=? AND code=?", (tid, code)).fetchone()
    return jsonify(dict(updated))


@app.route("/api/txns/<tid>/docs/<code>/receive", methods=["POST"])
def doc_receive(tid, code):
    return _doc_action(tid, code, "received", "received")


@app.route("/api/txns/<tid>/docs/<code>/verify", methods=["POST"])
def doc_verify(tid, code):
    return _doc_action(tid, code, "verified", "verified")


@app.route("/api/txns/<tid>/docs/<code>/na", methods=["POST"])
def doc_na(tid, code):
    return _doc_action(tid, code, "na", "notes")


@app.route("/api/txns/<tid>/docs/<code>/unverify", methods=["POST"])
def doc_unverify(tid, code):
    """Revert a verified document back to received status."""
    with db.conn() as c:
        row = c.execute("SELECT * FROM docs WHERE txn=? AND code=?", (tid, code)).fetchone()
        if not row:
            return jsonify({"error": "doc not found"}), 404
        if row["status"] != "verified":
            return jsonify({"error": "doc is not verified"}), 400
        c.execute(
            "UPDATE docs SET status='received', verified=NULL WHERE txn=? AND code=?",
            (tid, code),
        )
        db.log(c, tid, "doc_unverified", code)
        updated = c.execute("SELECT * FROM docs WHERE txn=? AND code=?", (tid, code)).fetchone()
    return jsonify(dict(updated))


@app.route("/api/txns/<tid>/docs/<code>/reset", methods=["POST"])
def doc_reset(tid, code):
    """Reset any document back to required status."""
    with db.conn() as c:
        row = c.execute("SELECT * FROM docs WHERE txn=? AND code=?", (tid, code)).fetchone()
        if not row:
            return jsonify({"error": "doc not found"}), 404
        c.execute(
            "UPDATE docs SET status='required', received=NULL, verified=NULL, notes='' WHERE txn=? AND code=?",
            (tid, code),
        )
        db.log(c, tid, "doc_reset", code)
        updated = c.execute("SELECT * FROM docs WHERE txn=? AND code=?", (tid, code)).fetchone()
    return jsonify(dict(updated))


# ── Gates ────────────────────────────────────────────────────────────────────

@app.route("/api/txns/<tid>/gates")
def get_gates(tid):
    rows = engine.gate_rows(tid)
    out = []
    for g in rows:
        info = rules.gate(g["gid"]) or {}
        g["name"] = info.get("name", "")
        g["type"] = info.get("type", "")
        g["phase"] = info.get("phase", "")
        g["legal_basis"] = info.get("legal_basis", {})
        g["what_agent_verifies"] = info.get("what_agent_verifies", [])
        g["ai_prepares"] = info.get("ai_prepares", [])
        g["cannot_proceed_until"] = info.get("cannot_proceed_until", "")
        g["notes_info"] = info.get("notes", "")
        out.append(g)
    return jsonify(out)


@app.route("/api/txns/<tid>/gates/<gid>/verify", methods=["POST"])
def verify_gate(tid, gid):
    notes = ""
    if request.json:
        notes = request.json.get("notes", "")
    engine.verify(tid, gid, notes)
    return jsonify({"ok": True, "gid": gid, "status": "verified"})


@app.route("/api/txns/<tid>/gates/<gid>/reset", methods=["POST"])
def reset_gate(tid, gid):
    """Reset a gate back to pending status."""
    with db.conn() as c:
        row = c.execute("SELECT * FROM gates WHERE txn=? AND gid=?", (tid, gid)).fetchone()
        if not row:
            return jsonify({"error": "gate not found"}), 404
        c.execute(
            "UPDATE gates SET status='pending', verified=NULL, notes='' WHERE txn=? AND gid=?",
            (tid, gid),
        )
        db.log(c, tid, "gate_reset", gid)
    return jsonify({"ok": True, "gid": gid, "status": "pending"})


# ── Properties ───────────────────────────────────────────────────────────────

@app.route("/api/txns/<tid>/props")
def get_props(tid):
    with db.conn() as c:
        t = db.txn(c, tid)
        if not t:
            return jsonify({"error": "not found"}), 404
    return jsonify(json.loads(t.get("props") or "{}"))


@app.route("/api/txns/<tid>/props", methods=["POST"])
def set_props(tid):
    body = request.json or {}
    flag = body.get("flag", "")
    value = body.get("value", False)
    if not flag:
        return jsonify({"error": "flag required"}), 400

    with db.conn() as c:
        t = db.txn(c, tid)
        if not t:
            return jsonify({"error": "not found"}), 404
        props = json.loads(t.get("props") or "{}")
        props[flag] = bool(value)
        c.execute(
            "UPDATE txns SET props=?, updated=datetime('now','localtime') WHERE id=?",
            (json.dumps(props), tid),
        )
        db.log(c, tid, "props_updated", f"{flag}={props[flag]}")

    # Re-resolve docs
    brokerage = t.get("brokerage", "")
    new_docs = []
    if brokerage:
        with db.conn() as c:
            existing = {r["code"] for r in c.execute("SELECT code FROM docs WHERE txn=?", (tid,))}
        new_docs = checklist.re_resolve(
            t.get("txn_type", "sale"), t.get("party_role", "listing"),
            brokerage, props, existing,
        )
        if new_docs:
            with db.conn() as c:
                for d in new_docs:
                    c.execute(
                        "INSERT OR IGNORE INTO docs(txn,code,name,phase,status) VALUES(?,?,?,?,?)",
                        (tid, d["code"], d["name"], d["phase"], "required"),
                    )
                db.log(c, tid, "docs_re_resolved", f"+{len(new_docs)} docs from props change")

    return jsonify({"props": props, "new_docs": len(new_docs)})


# ── Deadlines ────────────────────────────────────────────────────────────────

@app.route("/api/txns/<tid>/deadlines")
def get_deadlines(tid):
    rows = engine.deadline_rows(tid)
    today = date.today()

    # Check if dates are confirmed from contract extraction
    with db.conn() as c:
        t = db.txn(c, tid)
        # Auto-verify anchor/milestone deadlines that are in the past
        anchor_ids = {dl["id"] for dl in rules.deadlines() if dl.get("is_anchor")}
        for d in rows:
            if (d.get("did") in anchor_ids and d.get("status") == "pending"
                    and d.get("due") and date.fromisoformat(d["due"]) <= today):
                c.execute("UPDATE deadlines SET status='verified' WHERE txn=? AND did=?",
                          (tid, d["did"]))
                d["status"] = "verified"

    txn_data = json.loads((t or {}).get("data") or "{}")
    dates_confirmed = (txn_data.get("dates") or {}).get("_confirmed", False)

    for d in rows:
        if d.get("due"):
            d["days_remaining"] = (date.fromisoformat(d["due"]) - today).days
        else:
            d["days_remaining"] = None
        d["confirmed"] = dates_confirmed
    return jsonify(rows)


# ── Phase ────────────────────────────────────────────────────────────────────

@app.route("/api/txns/<tid>/advance", methods=["POST"])
def advance_phase(tid):
    with db.conn() as c:
        txn = db.txn(c, tid)
    if not txn:
        return jsonify({"error": "not found"}), 404

    ok, blocking = engine.can_advance(tid)
    if not ok:
        # Build rich blocker response
        blockers = {"gates": [], "missing_docs": [], "open_contingencies": [], "unsigned_fields": [], "parties": []}
        with db.conn() as c:
            phase = txn["phase"]
            brokerage = txn.get("brokerage", "")

            # Blocking gates with details
            all_gates_defs = {g["id"]: g for g in rules.gates()}
            if brokerage:
                for g in rules.de_gates(brokerage):
                    all_gates_defs[g["id"]] = g
            for g in engine.gate_rows(tid):
                info = all_gates_defs.get(g["gid"])
                if info and info["phase"] == phase and g["status"] != "verified" and info["type"] == "HARD_GATE":
                    blockers["gates"].append({
                        "gid": g["gid"],
                        "name": info["name"],
                        "what_to_verify": info.get("what_agent_verifies", []),
                    })

            # Missing required docs for current phase
            missing = c.execute(
                "SELECT code, name FROM docs WHERE txn=? AND phase=? AND status='required'",
                (tid, phase),
            ).fetchall()
            blockers["missing_docs"] = [{"code": d["code"], "name": d["name"]} for d in missing]

            # Active contingencies
            active = c.execute(
                "SELECT id, name, type, deadline_date FROM contingencies"
                " WHERE txn=? AND status='active'",
                (tid,),
            ).fetchall()
            blockers["open_contingencies"] = [dict(a) for a in active]

            # Unsigned signature fields
            unsigned = c.execute(
                "SELECT id, field_name, doc_code FROM sig_reviews"
                " WHERE txn=? AND is_filled=0 AND review_status='pending' LIMIT 10",
                (tid,),
            ).fetchall()
            blockers["unsigned_fields"] = [dict(u) for u in unsigned]

            # Key parties for contact
            parties = c.execute(
                "SELECT role, name, email, phone FROM parties WHERE txn=? AND name != '' AND name NOT LIKE '%%(TBD)%%'",
                (tid,),
            ).fetchall()
            blockers["parties"] = [dict(p) for p in parties]

        return jsonify({"ok": False, "blocking": blocking, "blockers": blockers}), 409
    new = engine.advance_phase(tid)
    if new:
        return jsonify({"ok": True, "phase": new})
    return jsonify({"ok": False, "blocking": ["Already at final phase"]}), 409


# ── Audit ────────────────────────────────────────────────────────────────────

@app.route("/api/txns/<tid>/audit")
def get_audit(tid):
    with db.conn() as c:
        rows = c.execute(
            "SELECT * FROM audit WHERE txn=? ORDER BY ts DESC", (tid,)
        ).fetchall()
    return jsonify([dict(r) for r in rows])


# ── Signatures ───────────────────────────────────────────────────────────

_sig_populated_txns: set[str] = set()  # in-memory cache of already-scanned txns
_manifest_cache: dict | None = None    # cached manifest sig fields


def _norm(s: str) -> str:
    """Normalize a name for fuzzy matching: lowercase, strip non-alpha, collapse spaces."""
    return re.sub(r"[^a-z ]+", " ", s.lower()).strip()


def _norm_words(s: str) -> set[str]:
    """Extract meaningful words (3+ chars) from a name."""
    return {w for w in _norm(s).split() if len(w) >= 3}


# Map common checklist codes to canonical form names
_CODE_ALIASES = {
    "rpa": "residential purchase agreement",
    "ad": "agency disclosure",
    "tds": "transfer disclosure statement",
    "spq": "seller property questionnaire",
    "avid": "agent visual inspection disclosure",
    "nhd": "natural hazard disclosure",
    "bia": "buyer inspection advisory",
    "sbsa": "statewide buyer and seller advisory",
    "wfi": "wire fraud",
    "prbs": "possible representation of both",
    "cr1": "contingency removal",
    "mca": "market conditions advisory",
    "dia": "disclosure information advisory",
    "fha": "fire hardening",
    "sq": "square foot",
    "lbp": "lead based paint",
    "hoa": "homeowners association",
    "wcpf": "water conserving",
    "wd": "wildfire disaster",
}


def _get_manifest_sigs() -> dict:
    """Load and cache all manifest signature fields."""
    global _manifest_cache
    if _manifest_cache is not None:
        return _manifest_cache

    _manifest_cache = {}
    manifest_dir = doc_versions.MANIFEST_DIR
    if not manifest_dir.exists():
        return _manifest_cache

    for folder in manifest_dir.iterdir():
        if not folder.is_dir() or folder.name.startswith("_"):
            continue
        for mfile in folder.glob("*.yaml"):
            if mfile.name.startswith("_"):
                continue
            try:
                with open(mfile) as f:
                    m = yaml.safe_load(f) or {}
            except Exception:
                continue
            fields = m.get("field_map", [])
            sig_fields = [
                f for f in fields
                if f.get("category") in ("signature", "signature_area", "entry_signature")
            ]
            if not sig_fields:
                continue
            manifest_name = m.get("document_name", mfile.stem)
            _manifest_cache[manifest_name.lower()] = {
                "folder": folder.name,
                "filename": mfile.stem,
                "manifest_name": manifest_name,
                "norm_words": _norm_words(manifest_name),
                "sig_fields": sig_fields,
            }
    return _manifest_cache


def _match_manifest(code: str, doc_name: str, manifests: dict) -> dict | None:
    """Find the best manifest match for a doc code + name. Returns manifest data or None."""
    # Try alias-based match first (most reliable)
    alias = _CODE_ALIASES.get(code.lower(), "")
    if alias:
        alias_words = _norm_words(alias)
        for _key, mdata in manifests.items():
            if alias_words <= mdata["norm_words"]:  # subset match
                return mdata

    # Word overlap: require 60%+ of doc name words present in manifest name
    doc_words = _norm_words(doc_name)
    if len(doc_words) >= 2:
        best, best_score = None, 0
        for _key, mdata in manifests.items():
            overlap = len(doc_words & mdata["norm_words"])
            score = overlap / len(doc_words)
            if score > best_score and score >= 0.6:
                best, best_score = mdata, score
        if best:
            return best

    # Normalized substring match (handles underscores vs spaces)
    norm_name = _norm(doc_name)
    for _key, mdata in manifests.items():
        norm_mname = _norm(mdata["manifest_name"])
        if len(norm_name) >= 8 and (norm_name in norm_mname or norm_mname in norm_name):
            return mdata

    return None


def _populate_sig_reviews(c, tid):
    """Lazy-load signature fields from manifests into sig_reviews for a txn."""
    if tid in _sig_populated_txns:
        return
    _sig_populated_txns.add(tid)

    docs = c.execute("SELECT code, name, folder, filename FROM docs WHERE txn=?", (tid,)).fetchall()
    if not docs:
        return

    manifests = _get_manifest_sigs()
    if not manifests:
        return

    for doc in docs:
        code = doc["code"]
        existing = c.execute(
            "SELECT 1 FROM sig_reviews WHERE txn=? AND doc_code=? LIMIT 1",
            (tid, code),
        ).fetchone()
        if existing:
            continue

        # Try uploaded file's manifest first (exact folder/filename match)
        mdata = None
        if doc["folder"] and doc["filename"]:
            for _key, m in manifests.items():
                if m["folder"] == doc["folder"] and m["filename"] == doc["filename"]:
                    mdata = m
                    break

        # Fall back to fuzzy name matching
        if not mdata:
            mdata = _match_manifest(code, doc["name"], manifests)

        if not mdata:
            continue

        for sf in mdata["sig_fields"]:
            field_name = sf.get("field", "")
            is_initials = bool(re.search(r"initial", field_name, re.IGNORECASE))
            field_type = "initials" if is_initials else "signature"
            bbox = sf.get("bbox", {})
            filled = 1 if sf.get("filled") else 0
            try:
                c.execute(
                    "INSERT OR IGNORE INTO sig_reviews"
                    "(txn, doc_code, folder, filename, field_name, field_type,"
                    " page, bbox, is_filled, review_status, source)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        tid, code, mdata["folder"], mdata["filename"],
                        field_name, field_type,
                        sf.get("page", 0), json.dumps(bbox),
                        filled, "pending", "auto",
                    ),
                )
            except Exception:
                pass


@app.route("/api/txns/<tid>/sig-counts")
def get_sig_counts(tid):
    """Fast signature counts for dashboard — no manifest scan."""
    with db.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) as total,"
            " SUM(is_filled) as filled"
            " FROM sig_reviews WHERE txn=?",
            (tid,),
        ).fetchone()
    total = row["total"] or 0
    filled = row["filled"] or 0
    return jsonify({"total": total, "filled": filled, "pending": total - filled})


@app.route("/api/txns/<tid>/signatures")
def get_signatures(tid):
    """List all signature/initial fields for a transaction."""
    with db.conn() as c:
        t = db.txn(c, tid)
        if not t:
            return jsonify({"error": "not found"}), 404

        # Lazy populate from manifests
        _populate_sig_reviews(c, tid)

        rows = c.execute(
            "SELECT sr.*, d.name as doc_name FROM sig_reviews sr"
            " LEFT JOIN docs d ON sr.txn = d.txn AND sr.doc_code = d.code"
            " WHERE sr.txn=? ORDER BY sr.doc_code, sr.page, sr.id",
            (tid,),
        ).fetchall()

        items = [dict(r) for r in rows]

        # Summary counts
        total = len(items)
        filled = sum(1 for i in items if i["is_filled"])
        empty = total - filled
        reviewed = sum(1 for i in items if i["review_status"] == "reviewed")
        flagged = sum(1 for i in items if i["review_status"] == "flagged")

    return jsonify({
        "items": items,
        "summary": {
            "total": total,
            "filled": filled,
            "empty": empty,
            "reviewed": reviewed,
            "flagged": flagged,
        },
    })


@app.route("/api/txns/<tid>/signatures/<int:sig_id>/review", methods=["POST"])
def review_signature(tid, sig_id):
    """Mark a signature field as reviewed or flagged."""
    body = request.json or {}
    status = body.get("status", "reviewed")
    note = body.get("note", "")
    if status not in ("reviewed", "flagged"):
        return jsonify({"error": "status must be reviewed or flagged"}), 400

    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM sig_reviews WHERE id=? AND txn=?", (sig_id, tid)
        ).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.execute(
            "UPDATE sig_reviews SET review_status=?, reviewer_note=?, reviewed_at=?"
            " WHERE id=?",
            (status, note, now, sig_id),
        )
        db.log(c, tid, f"sig_{status}", f"{row['field_name']} p{row['page']} ({row['doc_code']})")
        updated = c.execute("SELECT * FROM sig_reviews WHERE id=?", (sig_id,)).fetchone()
    return jsonify(dict(updated))


@app.route("/api/txns/<tid>/signatures/add", methods=["POST"])
def add_signature(tid):
    """Manually add a signature/initials field."""
    body = request.json or {}
    doc_code = body.get("doc_code", "")
    field_name = body.get("field_name", "").strip()
    field_type = body.get("field_type", "signature")
    page = body.get("page", 1)
    note = body.get("note", "")

    if not doc_code or not field_name:
        return jsonify({"error": "doc_code and field_name required"}), 400
    if field_type not in ("signature", "initials"):
        return jsonify({"error": "field_type must be signature or initials"}), 400

    with db.conn() as c:
        t = db.txn(c, tid)
        if not t:
            return jsonify({"error": "txn not found"}), 404
        try:
            c.execute(
                "INSERT INTO sig_reviews"
                "(txn, doc_code, field_name, field_type, page, bbox,"
                " is_filled, review_status, reviewer_note, source)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (tid, doc_code, field_name, field_type, page, "{}",
                 0, "manual", note, "manual"),
            )
            new_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        except Exception as e:
            return jsonify({"error": str(e)}), 409
        db.log(c, tid, "sig_manual_add", f"{field_name} p{page} ({doc_code})")
        row = c.execute("SELECT * FROM sig_reviews WHERE id=?", (new_id,)).fetchone()
    return jsonify(dict(row)), 201


@app.route("/api/txns/<tid>/signatures/<int:sig_id>", methods=["DELETE"])
def delete_signature(tid, sig_id):
    """Delete a manually added signature field."""
    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM sig_reviews WHERE id=? AND txn=?", (sig_id, tid)
        ).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        if row["source"] != "manual":
            return jsonify({"error": "can only delete manually added fields"}), 403
        c.execute("DELETE FROM sig_reviews WHERE id=?", (sig_id,))
        db.log(c, tid, "sig_deleted", f"{row['field_name']} p{row['page']} ({row['doc_code']})")
    return jsonify({"ok": True})


# ── Follow-ups (DocuSign / SkySlope / Email sandbox) ────────────────────────

@app.route("/api/sandbox-status")
def sandbox_status():
    return jsonify({"email_sandbox": integrations.EMAIL_SANDBOX,
                     "sandbox": integrations.EMAIL_SANDBOX})


@app.route("/api/docusign-status")
def docusign_status():
    """Check DocuSign configuration status and get setup instructions."""
    return jsonify(integrations.docusign_status())


@app.route("/api/txns/<tid>/cloud-approval")
def get_cloud_approval(tid):
    """Get cloud approval status for a transaction."""
    with db.conn() as c:
        row = db.txn(c, tid)
        if not row:
            return jsonify({"error": "not found"}), 404
        approval = cloud_guard.get_approval(c, tid)
    return jsonify(approval)


@app.route("/api/txns/<tid>/cloud-approval", methods=["POST"])
def grant_cloud_approval(tid):
    """Grant time-limited approval for cloud calls on this transaction."""
    body = request.get_json(silent=True) or {}
    minutes = body.get("minutes", cloud_guard.DEFAULT_APPROVAL_TTL_MIN)
    note = (body.get("note") or "").strip()
    granted_by = (body.get("granted_by") or "ui").strip() or "ui"

    with db.conn() as c:
        row = db.txn(c, tid)
        if not row:
            return jsonify({"error": "not found"}), 404
        approval = cloud_guard.grant_approval(
            c, tid, ttl_min=minutes, granted_by=granted_by, note=note
        )
        db.log(c, tid, "cloud_approval_granted",
               f"{approval['remaining_seconds']}s by {granted_by}")
    return jsonify(approval), 201


@app.route("/api/txns/<tid>/cloud-approval", methods=["DELETE"])
def revoke_cloud_approval(tid):
    """Revoke cloud-call approval for a transaction."""
    body = request.get_json(silent=True) or {}
    note = (body.get("note") or "").strip()
    with db.conn() as c:
        row = db.txn(c, tid)
        if not row:
            return jsonify({"error": "not found"}), 404
        approval = cloud_guard.revoke_approval(c, tid, note=note)
        db.log(c, tid, "cloud_approval_revoked", note[:80] if note else "revoked")
    return jsonify(approval)


@app.route("/api/txns/<tid>/cloud-events")
def get_cloud_events(tid):
    """List cloud usage events for a transaction."""
    service = (request.args.get("service") or "").strip()
    operation = (request.args.get("operation") or "").strip()
    outcome = (request.args.get("outcome") or "").strip()
    limit = request.args.get("limit", type=int) or 100
    limit = max(1, min(limit, 500))

    with db.conn() as c:
        row = db.txn(c, tid)
        if not row:
            return jsonify({"error": "not found"}), 404
        q = "SELECT * FROM cloud_events WHERE txn=?"
        params = [tid]
        if service:
            q += " AND service=?"
            params.append(service)
        if operation:
            q += " AND operation=?"
            params.append(operation)
        if outcome:
            q += " AND outcome=?"
            params.append(outcome)
        q += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = c.execute(q, params).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/txns/<tid>/signatures/<int:sig_id>/send", methods=["POST"])
def send_signature(tid, sig_id):
    """Send a signature field for signing via DocuSign (or sandbox mock)."""
    body = request.json or {}
    email_addr = body.get("email", "").strip()
    name = body.get("name", "").strip()
    provider = body.get("provider", "docusign")
    if not email_addr or not name:
        return jsonify({"error": "email and name required"}), 400
    if provider != "docusign":
        return jsonify({"error": f"unsupported provider: {provider}"}), 400

    try:
        with db.conn() as c:
            row = c.execute(
                "SELECT * FROM sig_reviews WHERE id=? AND txn=?", (sig_id, tid)
            ).fetchone()
            if not row:
                return jsonify({"error": "signature field not found"}), 404
            result = integrations.send_for_signature(
                c, tid, sig_id, email_addr, name, provider
            )
    except NotImplementedError as exc:
        return jsonify({"error": str(exc)}), 503
    except Exception:
        return jsonify({"error": "failed to send signature request"}), 502
    return jsonify(result), 201


@app.route("/api/txns/<tid>/signatures/<int:sig_id>/remind", methods=["POST"])
def remind_signature(tid, sig_id):
    """Send a follow-up reminder for an unsigned field."""
    with db.conn() as c:
        result = integrations.send_reminder(c, tid, sig_id)
        if not result:
            return jsonify({"error": "no signer email set or field not found"}), 400
    return jsonify(result)


@app.route("/api/txns/<tid>/signatures/<int:sig_id>/simulate", methods=["POST"])
def simulate_signature(tid, sig_id):
    """Testing utility: simulate a signer completing the signature."""

    with db.conn() as c:
        # Find the envelope tracking record for this sig
        env = c.execute(
            "SELECT * FROM envelope_tracking WHERE sig_review_id=? AND txn=?"
            " ORDER BY id DESC LIMIT 1",
            (sig_id, tid),
        ).fetchone()
        if not env:
            return jsonify({"error": "no envelope found — send for signing first"}), 404
        result = integrations.simulate_sign(c, env["id"])
        if not result:
            return jsonify({"error": "simulation failed"}), 500
    return jsonify(result)


@app.route("/api/txns/<tid>/outbox")
def get_outbox(tid):
    """View all sent/queued/sandbox emails for a transaction."""
    with db.conn() as c:
        items = integrations.get_outbox(c, tid)
    return jsonify(items)


@app.route("/api/txns/<tid>/envelopes")
def get_envelopes(tid):
    """View all DocuSign/SkySlope envelope tracking records."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT et.*, sr.field_name, sr.doc_code, sr.page"
            " FROM envelope_tracking et"
            " LEFT JOIN sig_reviews sr ON et.sig_review_id = sr.id"
            " WHERE et.txn=? ORDER BY et.sent_at DESC",
            (tid,),
        ).fetchall()
    return jsonify([dict(r) for r in rows])


# ── Contingencies ─────────────────────────────────────────────────────────

CONT_DL = {"investigation": "DL-010", "appraisal": "DL-020", "loan": "DL-030", "hoa": "DL-040"}
CONT_NAMES = {
    "investigation": "Investigation Contingency",
    "appraisal": "Appraisal Contingency",
    "loan": "Loan Contingency",
    "hoa": "HOA Document Review",
}


def _contingency_for_txn(c, tid: str, cid: int):
    """Return contingency row only when it belongs to the transaction."""
    return c.execute(
        "SELECT * FROM contingencies WHERE id=? AND txn=?",
        (cid, tid),
    ).fetchone()


@app.route("/api/txns/<tid>/contingencies")
def get_contingencies(tid):
    """List contingencies with days_remaining and urgency computed in SQL."""
    with db.conn() as c:
        if not db.txn(c, tid):
            return jsonify({"error": "not found"}), 404
        items = [dict(r) for r in c.execute(
            "SELECT *,"
            " CAST(julianday(deadline_date) - julianday('now','localtime') AS INTEGER) AS days_remaining,"
            " CASE"
            "   WHEN julianday(deadline_date) - julianday('now','localtime') < 0 THEN 'overdue'"
            "   WHEN julianday(deadline_date) - julianday('now','localtime') <= 2 THEN 'urgent'"
            "   WHEN julianday(deadline_date) - julianday('now','localtime') <= 5 THEN 'soon'"
            "   ELSE 'ok'"
            " END AS urgency,"
            " CASE WHEN nbp_expires_at IS NOT NULL"
            "   THEN CAST(julianday(nbp_expires_at) - julianday('now','localtime') AS INTEGER)"
            " END AS nbp_days_remaining"
            " FROM contingencies WHERE txn=? ORDER BY deadline_date",
            (tid,),
        ).fetchall()]
        for item in items:
            item["related_gate"] = CONT_GATE.get(item["type"], "")
            item["related_deadline"] = CONT_DL.get(item["type"], "")
            # Add inspection items progress counts
            ci = c.execute(
                "SELECT COUNT(*) AS total,"
                " SUM(status='complete') AS done"
                " FROM contingency_items WHERE contingency_id=?",
                (item["id"],),
            ).fetchone()
            item["items_total"] = ci["total"] if ci else 0
            item["items_done"] = ci["done"] if ci else 0
        summary = c.execute(
            "SELECT COUNT(*) AS total,"
            " SUM(status='active') AS active,"
            " SUM(status='removed') AS removed,"
            " SUM(status='waived') AS waived,"
            " SUM(status='active' AND julianday(deadline_date) < julianday('now','localtime')) AS overdue"
            " FROM contingencies WHERE txn=?",
            (tid,),
        ).fetchone()
    return jsonify({"items": items, "summary": dict(summary)})


@app.route("/api/txns/<tid>/contingencies", methods=["POST"])
def add_contingency(tid):
    """Manually add a contingency."""
    body = request.json or {}
    ctype = body.get("type", "").strip()
    days = body.get("days", 17)
    deadline = body.get("deadline_date", "")
    notes = body.get("notes", "")
    if not ctype:
        return jsonify({"error": "type required"}), 400
    try:
        days_int = int(days)
    except (TypeError, ValueError):
        return jsonify({"error": "days must be a positive integer"}), 400
    if days_int < 1:
        return jsonify({"error": "days must be a positive integer"}), 400
    if deadline:
        try:
            deadline = date.fromisoformat(deadline).isoformat()
        except ValueError:
            return jsonify({"error": "deadline_date must be YYYY-MM-DD"}), 400

    with db.conn() as c:
        t = db.txn(c, tid)
        if not t:
            return jsonify({"error": "txn not found"}), 404
    name = CONT_NAMES.get(ctype, body.get("name", ctype.replace("_", " ").title() + " Contingency"))

    # If no deadline given, compute from acceptance date
    if not deadline:
        data = json.loads(t.get("data") or "{}")
        acceptance = (data.get("dates") or {}).get("acceptance")
        if acceptance:
            deadline = (date.fromisoformat(acceptance) + timedelta(days=days_int)).isoformat()

    with db.conn() as c:
        try:
            c.execute(
                "INSERT INTO contingencies(txn,type,name,default_days,deadline_date,notes)"
                " VALUES(?,?,?,?,?,?)",
                (tid, ctype, name, days_int, deadline, notes),
            )
        except Exception:
            return jsonify({"error": f"contingency '{ctype}' already exists for this transaction"}), 409
        cid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        # Auto-populate inspection items for investigation contingencies
        if ctype == "investigation":
            engine._auto_populate_inspection_items(c, tid, cid)
        db.log(c, tid, "cont_added", f"{name} ({days_int}d, due {deadline})")
        row = c.execute("SELECT * FROM contingencies WHERE id=?", (cid,)).fetchone()
    return jsonify(dict(row)), 201


@app.route("/api/txns/<tid>/contingencies/<int:cid>/remove", methods=["POST"])
def remove_contingency(tid, cid):
    """Mark contingency as removed (CR-1 signed). Auto-verifies linked gate."""
    with db.conn() as c:
        if not (row := c.execute("SELECT * FROM contingencies WHERE id=? AND txn=?", (cid, tid)).fetchone()):
            return jsonify({"error": "not found"}), 404
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.execute(
            "UPDATE contingencies SET status='removed', removed_at=? WHERE id=?",
            (now, cid),
        )
        # Auto-verify the linked gate
        if gid := CONT_GATE.get(row["type"]):
            c.execute(
                "UPDATE gates SET status='verified', verified=datetime('now','localtime')"
                " WHERE txn=? AND gid=? AND status='pending'",
                (tid, gid),
            )
            db.log(c, tid, "gate_verified", f"{gid} (via contingency removal)")
        db.log(c, tid, "cont_removed", f"{row['name']} removed (CR-1)")
        updated = c.execute("SELECT * FROM contingencies WHERE id=?", (cid,)).fetchone()
    return jsonify(dict(updated))


@app.route("/api/txns/<tid>/contingencies/<int:cid>/nbp", methods=["POST"])
def nbp_contingency(tid, cid):
    """Record Notice to Buyer to Perform. Computes 2-day expiry."""
    with db.conn() as c:
        if not (row := c.execute("SELECT * FROM contingencies WHERE id=? AND txn=?", (cid, tid)).fetchone()):
            return jsonify({"error": "not found"}), 404
        if row["status"] != "active":
            return jsonify({"error": "can only issue NBP on active contingencies"}), 400
        now = date.today()
        expires = (now + timedelta(days=2)).isoformat()
        c.execute(
            "UPDATE contingencies SET nbp_sent_at=?, nbp_expires_at=? WHERE id=?",
            (now.isoformat(), expires, cid),
        )
        db.log(c, tid, "cont_nbp", f"NBP issued for {row['name']}, expires {expires}")
        updated = c.execute("SELECT * FROM contingencies WHERE id=?", (cid,)).fetchone()
    return jsonify(dict(updated))


@app.route("/api/txns/<tid>/contingencies/<int:cid>/waive", methods=["POST"])
def waive_contingency(tid, cid):
    """Mark contingency as waived (per original contract terms)."""
    with db.conn() as c:
        if not (row := c.execute("SELECT * FROM contingencies WHERE id=? AND txn=?", (cid, tid)).fetchone()):
            return jsonify({"error": "not found"}), 404
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.execute(
            "UPDATE contingencies SET status='waived', waived_at=? WHERE id=?",
            (now, cid),
        )
        if gid := CONT_GATE.get(row["type"]):
            c.execute(
                "UPDATE gates SET status='verified', verified=datetime('now','localtime')"
                " WHERE txn=? AND gid=? AND status='pending'",
                (tid, gid),
            )
        db.log(c, tid, "cont_waived", f"{row['name']} waived")
        updated = c.execute("SELECT * FROM contingencies WHERE id=?", (cid,)).fetchone()
    return jsonify(dict(updated))


# ── Contingency Items (Inspection Checklist) ────────────────────────────────

@app.route("/api/txns/<tid>/contingencies/<int:cid>/items")
def get_cont_items(tid, cid):
    """List inspection checklist items for a contingency."""
    with db.conn() as c:
        if not _contingency_for_txn(c, tid, cid):
            return jsonify({"error": "not found"}), 404
        items = c.execute(
            "SELECT * FROM contingency_items WHERE contingency_id=? ORDER BY sort_order, id",
            (cid,),
        ).fetchall()
    return jsonify([dict(r) for r in items])


@app.route("/api/txns/<tid>/contingencies/<int:cid>/items", methods=["POST"])
def add_cont_item(tid, cid):
    """Add a custom inspection item."""
    body = request.json or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    with db.conn() as c:
        if not _contingency_for_txn(c, tid, cid):
            return jsonify({"error": "not found"}), 404
        # Get max sort_order
        mx = c.execute(
            "SELECT COALESCE(MAX(sort_order),0) FROM contingency_items WHERE contingency_id=?",
            (cid,),
        ).fetchone()[0]
        c.execute(
            "INSERT INTO contingency_items(contingency_id,name,inspector,scheduled_date,notes,sort_order)"
            " VALUES(?,?,?,?,?,?)",
            (cid, name, body.get("inspector", ""), body.get("scheduled_date", ""),
             body.get("notes", ""), mx + 1),
        )
        iid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.log(c, tid, "cont_item_added", f"{name} to contingency {cid}")
        row = c.execute("SELECT * FROM contingency_items WHERE id=?", (iid,)).fetchone()
    return jsonify(dict(row)), 201


@app.route("/api/txns/<tid>/contingencies/<int:cid>/items/<int:iid>", methods=["PUT"])
def update_cont_item(tid, cid, iid):
    """Update an inspection item (status, inspector, dates, notes)."""
    body = request.json or {}
    with db.conn() as c:
        if not _contingency_for_txn(c, tid, cid):
            return jsonify({"error": "not found"}), 404
        row = c.execute(
            "SELECT * FROM contingency_items WHERE id=? AND contingency_id=?", (iid, cid)
        ).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404

        updates = []
        params = []
        for field in ("name", "status", "inspector", "scheduled_date", "notes"):
            if field in body:
                updates.append(f"{field}=?")
                params.append(body[field])

        # Auto-set completed_date when status becomes complete
        new_status = body.get("status")
        if new_status == "complete" and row["status"] != "complete":
            updates.append("completed_date=datetime('now','localtime')")
        elif new_status and new_status != "complete":
            updates.append("completed_date=NULL")

        if updates:
            params.append(iid)
            c.execute(f"UPDATE contingency_items SET {','.join(updates)} WHERE id=?", params)
            db.log(c, tid, "cont_item_updated", f"item {iid} in contingency {cid}")

        updated = c.execute("SELECT * FROM contingency_items WHERE id=?", (iid,)).fetchone()
    return jsonify(dict(updated))


@app.route("/api/txns/<tid>/contingencies/<int:cid>/items/<int:iid>", methods=["DELETE"])
def delete_cont_item(tid, cid, iid):
    """Delete an inspection item."""
    with db.conn() as c:
        if not _contingency_for_txn(c, tid, cid):
            return jsonify({"error": "not found"}), 404
        row = c.execute(
            "SELECT id FROM contingency_items WHERE id=? AND contingency_id=?",
            (iid, cid),
        ).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        c.execute("DELETE FROM contingency_items WHERE id=? AND contingency_id=?", (iid, cid))
        db.log(c, tid, "cont_item_deleted", f"item {iid} in contingency {cid}")
    return jsonify({"ok": True})


# ── Parties ──────────────────────────────────────────────────────────────────

PARTY_ROLES = {
    "buyer", "seller", "buyer_agent", "seller_agent", "escrow_officer",
    "lender", "title_rep", "inspector", "appraiser", "transaction_coordinator", "other",
}
PARTY_ROLE_NAMES = {
    "buyer": "Buyer", "seller": "Seller",
    "buyer_agent": "Buyer's Agent", "seller_agent": "Seller's Agent",
    "escrow_officer": "Escrow Officer", "lender": "Lender",
    "title_rep": "Title Representative", "inspector": "Inspector",
    "appraiser": "Appraiser", "transaction_coordinator": "Transaction Coordinator",
    "other": "Other",
}


@app.route("/api/txns/<tid>/parties")
def get_parties(tid):
    with db.conn() as c:
        if not db.txn(c, tid):
            return jsonify({"error": "not found"}), 404
        rows = c.execute(
            "SELECT * FROM parties WHERE txn=? ORDER BY role, name", (tid,)
        ).fetchall()
    items = [dict(r) for r in rows]
    for item in items:
        item["role_name"] = PARTY_ROLE_NAMES.get(item["role"], item["role"])
    return jsonify(items)


@app.route("/api/txns/<tid>/parties", methods=["POST"])
def add_party(tid):
    body = request.json or {}
    role = body.get("role", "").strip()
    name = body.get("name", "").strip()
    if not role or not name:
        return jsonify({"error": "role and name required"}), 400
    if role not in PARTY_ROLES:
        return jsonify({"error": f"invalid role: {role}"}), 400

    with db.conn() as c:
        if not db.txn(c, tid):
            return jsonify({"error": "txn not found"}), 404
        c.execute(
            "INSERT INTO parties(txn,role,name,email,phone,company,license_no,notes)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (tid, role, name, body.get("email", ""), body.get("phone", ""),
             body.get("company", ""), body.get("license_no", ""),
             body.get("notes", "")),
        )
        pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.log(c, tid, "party_added", f"{role}: {name}")
        row = c.execute("SELECT * FROM parties WHERE id=?", (pid,)).fetchone()
    result = dict(row)
    result["role_name"] = PARTY_ROLE_NAMES.get(result["role"], result["role"])
    return jsonify(result), 201


@app.route("/api/txns/<tid>/parties/<int:pid>", methods=["PUT"])
def update_party(tid, pid):
    body = request.json or {}
    with db.conn() as c:
        if not (row := c.execute("SELECT * FROM parties WHERE id=? AND txn=?", (pid, tid)).fetchone()):
            return jsonify({"error": "not found"}), 404
        sets, vals = [], []
        for field in ("name", "email", "phone", "company", "license_no", "notes", "role"):
            if field in body:
                sets.append(f"{field}=?")
                vals.append(body[field])
        if not sets:
            return jsonify({"error": "no fields to update"}), 400
        vals.extend([pid, tid])
        c.execute(f"UPDATE parties SET {','.join(sets)} WHERE id=? AND txn=?", vals)
        db.log(c, tid, "party_updated", f"{row['role']}: {row['name']}")
        updated = c.execute("SELECT * FROM parties WHERE id=?", (pid,)).fetchone()
    result = dict(updated)
    result["role_name"] = PARTY_ROLE_NAMES.get(result["role"], result["role"])
    return jsonify(result)


@app.route("/api/txns/<tid>/parties/<int:pid>", methods=["DELETE"])
def delete_party(tid, pid):
    with db.conn() as c:
        if not (row := c.execute("SELECT * FROM parties WHERE id=? AND txn=?", (pid, tid)).fetchone()):
            return jsonify({"error": "not found"}), 404
        c.execute("DELETE FROM parties WHERE id=?", (pid,))
        db.log(c, tid, "party_deleted", f"{row['role']}: {row['name']}")
    return jsonify({"ok": True})


# ── Disclosures ─────────────────────────────────────────────────────────────

DISC_TYPES = {
    "tds": ("Transfer Disclosure Statement", "seller", 7),
    "spq": ("Seller Property Questionnaire", "seller", 7),
    "nhd": ("Natural Hazard Disclosure", "seller", 7),
    "avid_listing": ("Agent Visual Inspection (Listing)", "seller_agent", 7),
    "avid_buyer": ("Agent Visual Inspection (Buyer)", "buyer_agent", 7),
    "lead_paint": ("Lead-Based Paint Disclosure", "seller", 7),
    "water_heater": ("Water Heater Statement of Compliance", "seller", 0),
    "smoke_co": ("Smoke/CO Detector Compliance", "seller", 0),
    "megan_law": ("Megan's Law Disclosure", "seller_agent", 0),
    "preliminary_title": ("Preliminary Title Report", "title_rep", 7),
    "hoa_docs": ("HOA Documents Package", "seller", 3),
    "local": ("Local Supplemental Disclosures", "seller", 7),
    "other": ("Other Disclosure", "", 0),
}


@app.route("/api/txns/<tid>/disclosures")
def get_disclosures(tid):
    """List disclosures with computed days_until_due."""
    with db.conn() as c:
        if not db.txn(c, tid):
            return jsonify({"error": "not found"}), 404
        items = [dict(r) for r in c.execute(
            "SELECT *,"
            " CASE WHEN due_date IS NOT NULL"
            "   THEN CAST(julianday(due_date) - julianday('now','localtime') AS INTEGER)"
            " END AS days_until_due,"
            " CASE"
            "   WHEN due_date IS NULL THEN 'none'"
            "   WHEN julianday(due_date) - julianday('now','localtime') < 0 THEN 'overdue'"
            "   WHEN julianday(due_date) - julianday('now','localtime') <= 2 THEN 'urgent'"
            "   WHEN julianday(due_date) - julianday('now','localtime') <= 5 THEN 'soon'"
            "   ELSE 'ok'"
            " END AS urgency"
            " FROM disclosures WHERE txn=? ORDER BY due_date",
            (tid,),
        ).fetchall()]
        summary = c.execute(
            "SELECT COUNT(*) AS total,"
            " SUM(status='pending') AS pending,"
            " SUM(status='ordered') AS ordered,"
            " SUM(status='received') AS received,"
            " SUM(status='reviewed') AS reviewed,"
            " SUM(status IN ('waived','na')) AS waived"
            " FROM disclosures WHERE txn=?",
            (tid,),
        ).fetchone()
    return jsonify({"items": items, "summary": dict(summary)})


@app.route("/api/txns/<tid>/disclosures", methods=["POST"])
def add_disclosure(tid):
    body = request.json or {}
    dtype = body.get("type", "").strip()
    if not dtype:
        return jsonify({"error": "type required"}), 400
    info = DISC_TYPES.get(dtype, ("Other Disclosure", "", 0))
    name = body.get("name", info[0])
    responsible = body.get("responsible", info[1])
    due_date = body.get("due_date", "")

    # Auto-compute due_date from acceptance + default days if not provided
    if not due_date and info[2] > 0:
        with db.conn() as c:
            t = db.txn(c, tid)
            if not t:
                return jsonify({"error": "txn not found"}), 404
            data = json.loads(t.get("data") or "{}")
            acceptance = (data.get("dates") or {}).get("acceptance")
            if acceptance:
                due_date = (date.fromisoformat(acceptance) + timedelta(days=info[2])).isoformat()

    with db.conn() as c:
        try:
            c.execute(
                "INSERT INTO disclosures(txn,type,name,responsible,due_date,notes)"
                " VALUES(?,?,?,?,?,?)",
                (tid, dtype, name, responsible, due_date, body.get("notes", "")),
            )
        except Exception:
            return jsonify({"error": f"disclosure '{dtype}' already exists"}), 409
        did = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.log(c, tid, "disc_added", f"{name} ({dtype})")
        row = c.execute("SELECT * FROM disclosures WHERE id=?", (did,)).fetchone()
    return jsonify(dict(row)), 201


@app.route("/api/txns/<tid>/disclosures/<int:did>/receive", methods=["POST"])
def receive_disclosure(tid, did):
    with db.conn() as c:
        if not (row := c.execute("SELECT * FROM disclosures WHERE id=? AND txn=?", (did, tid)).fetchone()):
            return jsonify({"error": "not found"}), 404
        c.execute(
            "UPDATE disclosures SET status='received', received_date=datetime('now','localtime')"
            " WHERE id=?", (did,),
        )
        db.log(c, tid, "disc_received", row["name"])
        updated = c.execute("SELECT * FROM disclosures WHERE id=?", (did,)).fetchone()
    return jsonify(dict(updated))


@app.route("/api/txns/<tid>/disclosures/<int:did>/review", methods=["POST"])
def review_disclosure(tid, did):
    body = request.json or {}
    reviewer = body.get("reviewer", "")
    with db.conn() as c:
        if not (row := c.execute("SELECT * FROM disclosures WHERE id=? AND txn=?", (did, tid)).fetchone()):
            return jsonify({"error": "not found"}), 404
        c.execute(
            "UPDATE disclosures SET status='reviewed', reviewed_date=datetime('now','localtime'),"
            " reviewer=? WHERE id=?",
            (reviewer, did),
        )
        db.log(c, tid, "disc_reviewed", row["name"])
        updated = c.execute("SELECT * FROM disclosures WHERE id=?", (did,)).fetchone()
    return jsonify(dict(updated))


@app.route("/api/txns/<tid>/disclosures/<int:did>/waive", methods=["POST"])
def waive_disclosure(tid, did):
    with db.conn() as c:
        if not (row := c.execute("SELECT * FROM disclosures WHERE id=? AND txn=?", (did, tid)).fetchone()):
            return jsonify({"error": "not found"}), 404
        c.execute("UPDATE disclosures SET status='waived' WHERE id=?", (did,))
        db.log(c, tid, "disc_waived", row["name"])
        updated = c.execute("SELECT * FROM disclosures WHERE id=?", (did,)).fetchone()
    return jsonify(dict(updated))


# ── Dashboard (cross-transaction urgency) ────────────────────────────────────

@app.route("/api/dashboard")
def dashboard():
    """Aggregate urgency across all transactions — the TC command center."""
    with db.conn() as c:
        txns = c.execute("SELECT * FROM txns ORDER BY created DESC").fetchall()
        items = []
        for row in txns:
            t = _txn_dict(row)
            tid = t["id"]

            # Deadline urgency
            urgent_dl = c.execute(
                "SELECT name, due,"
                " CAST(julianday(due) - julianday('now','localtime') AS INTEGER) AS days_left"
                " FROM deadlines WHERE txn=? AND status='pending'"
                " AND julianday(due) - julianday('now','localtime') <= 5"
                " ORDER BY due LIMIT 3",
                (tid,),
            ).fetchall()

            # Active contingencies near deadline
            urgent_cont = c.execute(
                "SELECT name, deadline_date, status,"
                " CAST(julianday(deadline_date) - julianday('now','localtime') AS INTEGER) AS days_left"
                " FROM contingencies WHERE txn=? AND status='active'"
                " AND julianday(deadline_date) - julianday('now','localtime') <= 5"
                " ORDER BY deadline_date LIMIT 3",
                (tid,),
            ).fetchall()

            # Pending hard gates count
            all_gates_defs = {g["id"]: g for g in rules.gates()}
            brokerage = t.get("brokerage", "")
            if brokerage:
                for g in rules.de_gates(brokerage):
                    all_gates_defs[g["id"]] = g
            gate_rows = c.execute("SELECT * FROM gates WHERE txn=?", (tid,)).fetchall()
            pending_hard = sum(
                1 for g in gate_rows
                if g["status"] != "verified"
                and all_gates_defs.get(g["gid"], {}).get("type") == "HARD_GATE"
                and all_gates_defs.get(g["gid"], {}).get("phase") == t["phase"]
            )

            # Doc stats
            ds = _doc_stats(c, tid)

            # Notes
            notes = t.get("data", {}).get("notes", "")

            # Determine overall health
            overdue_count = sum(1 for d in urgent_dl if d["days_left"] < 0) + \
                            sum(1 for d in urgent_cont if d["days_left"] < 0)
            soon_count = len(urgent_dl) + len(urgent_cont) - overdue_count

            health = "green"
            if soon_count > 0:
                health = "yellow"
            if overdue_count > 0:
                health = "red"

            # Signature tracking
            sig_total = c.execute(
                "SELECT COUNT(*) FROM sig_reviews WHERE txn=?", (tid,)
            ).fetchone()[0]
            sig_signed = c.execute(
                "SELECT COUNT(*) FROM sig_reviews WHERE txn=? AND is_filled=1", (tid,)
            ).fetchone()[0]
            sig_sent = c.execute(
                "SELECT COUNT(DISTINCT sig_review_id) FROM envelope_tracking WHERE txn=? AND status='sent'",
                (tid,),
            ).fetchone()[0]

            items.append({
                "id": tid,
                "address": t["address"],
                "phase": t["phase"],
                "txn_type": t.get("txn_type", "sale"),
                "party_role": t.get("party_role", "listing"),
                "brokerage": brokerage,
                "health": health,
                "overdue": overdue_count,
                "soon": soon_count,
                "pending_hard_gates": pending_hard,
                "doc_stats": ds,
                "urgent_deadlines": [dict(d) for d in urgent_dl],
                "urgent_contingencies": [dict(d) for d in urgent_cont],
                "sig_total": sig_total,
                "sig_signed": sig_signed,
                "sig_pending": sig_sent,
                "notes": notes,
            })

    return jsonify(items)


# ── Notes ────────────────────────────────────────────────────────────────────

@app.route("/api/txns/<tid>/notes", methods=["GET"])
def get_notes(tid):
    with db.conn() as c:
        t = db.txn(c, tid)
        if not t:
            return jsonify({"error": "not found"}), 404
        data = json.loads(t.get("data") or "{}")
    return jsonify({"notes": data.get("notes", "")})


@app.route("/api/txns/<tid>/notes", methods=["POST"])
def save_notes(tid):
    body = request.json or {}
    notes = body.get("notes", "")
    with db.conn() as c:
        t = db.txn(c, tid)
        if not t:
            return jsonify({"error": "not found"}), 404
        data = json.loads(t.get("data") or "{}")
        data["notes"] = notes
        c.execute(
            "UPDATE txns SET data=?, updated=datetime('now','localtime') WHERE id=?",
            (json.dumps(data), tid),
        )
        db.log(c, tid, "notes_updated", f"{len(notes)} chars")
    return jsonify({"ok": True, "notes": notes})


# ── Bulk Document Actions ────────────────────────────────────────────────────

@app.route("/api/txns/<tid>/docs/bulk-receive", methods=["POST"])
def bulk_receive(tid):
    """Mark all 'required' docs as 'received'."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT code FROM docs WHERE txn=? AND status='required'", (tid,)
        ).fetchall()
        codes = [r["code"] for r in rows]
        if not codes:
            return jsonify({"updated": 0})
        c.execute(
            "UPDATE docs SET status='received', received=datetime('now','localtime')"
            " WHERE txn=? AND status='required'",
            (tid,),
        )
        db.log(c, tid, "bulk_received", f"{len(codes)} documents")
    return jsonify({"updated": len(codes), "codes": codes})


@app.route("/api/txns/<tid>/docs/bulk-verify", methods=["POST"])
def bulk_verify(tid):
    """Mark all 'received' docs as 'verified'."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT code FROM docs WHERE txn=? AND status='received'", (tid,)
        ).fetchall()
        codes = [r["code"] for r in rows]
        if not codes:
            return jsonify({"updated": 0})
        c.execute(
            "UPDATE docs SET status='verified', verified=datetime('now','localtime')"
            " WHERE txn=? AND status='received'",
            (tid,),
        )
        db.log(c, tid, "bulk_verified", f"{len(codes)} documents")
    return jsonify({"updated": len(codes), "codes": codes})


# ── Meta ─────────────────────────────────────────────────────────────────────

@app.route("/api/brokerages")
def get_brokerages():
    return jsonify(rules.brokerage_list())


@app.route("/api/phases/<txn_type>")
def get_phases(txn_type):
    if txn_type == "lease":
        return jsonify(rules.lease_phases())
    return jsonify(rules.phases())


# ── Document Analysis ────────────────────────────────────────────────────────

@app.route("/api/doc-packages")
def doc_packages():
    """List all CAR Contract Package folders and their PDFs."""
    car_dir = doc_versions.CAR_DIR
    if not car_dir.exists():
        return jsonify([])
    packages = []
    for folder in sorted(d for d in car_dir.iterdir() if d.is_dir()):
        pdfs = sorted(p.name for p in folder.glob("*.pdf"))
        packages.append({"folder": folder.name, "files": pdfs, "count": len(pdfs)})
    return jsonify(packages)


@app.route("/api/doc-packages/<path:folder>/<filename>/fields")
def doc_fields(folder, filename):
    """Get field locations for a document, with optional category/page filters."""
    if err := _validate_doc_path_inputs(folder, filename):
        return err
    category = request.args.get("category")
    page = request.args.get("page", type=int)
    fields = doc_versions.field_locations(folder, filename, category, page)
    return jsonify(fields)


@app.route("/api/doc-packages/<path:folder>/<filename>/manifest")
def doc_manifest(folder, filename):
    """Get the full manifest for a document."""
    if err := _validate_doc_path_inputs(folder, filename):
        return err
    m = doc_versions.load_manifest(folder, filename)
    if not m:
        return jsonify({"error": "manifest not found"}), 404
    return jsonify(m)


@app.route("/api/doc-versions/status")
def doc_version_status():
    """Check for document changes since last scan."""
    changes = doc_versions.check_changes()
    return jsonify({
        "total": changes["total_current"],
        "new": len(changes["added"]),
        "changed": len(changes["changed"]),
        "removed": len(changes["removed"]),
        "unchanged": len(changes["unchanged"]),
        "added_files": changes["added"],
        "changed_files": changes["changed"],
    })


@app.route("/api/doc-versions/history")
def doc_version_history():
    """Version history log."""
    db_data = doc_versions.load_version_db()
    return jsonify(db_data.get("history", []))


# ── PDF Viewer / Field Annotations ────────────────────────────────────────────

@app.route("/api/doc-packages/<path:folder>/<filename>/pdf")
def serve_pdf(folder, filename):
    """Serve a CAR contract PDF file."""
    if err := _validate_doc_path_inputs(folder, filename, require_pdf=True):
        return err
    car_root = doc_versions.CAR_DIR.resolve()
    car_dir = (car_root / folder).resolve()
    try:
        car_dir.relative_to(car_root)
    except ValueError:
        return jsonify({"error": "invalid folder"}), 400
    if not car_dir.is_dir():
        return jsonify({"error": "folder not found"}), 404
    pdf_path = (car_dir / filename).resolve()
    try:
        pdf_path.relative_to(car_dir)
    except ValueError:
        return jsonify({"error": "invalid filename"}), 400
    if not pdf_path.is_file():
        return jsonify({"error": "file not found"}), 404
    return send_from_directory(str(car_dir), filename, mimetype="application/pdf")


@app.route("/api/field-annotations/<path:folder>/<filename>")
def get_field_annotations(folder, filename):
    """Get all field annotations for a document."""
    if err := _validate_doc_path_inputs(folder, filename):
        return err
    txn_id = request.args.get("txn", "")
    with db.conn() as c:
        rows = c.execute(
            "SELECT field_idx, status FROM field_annotations"
            " WHERE txn=? AND folder=? AND filename=?",
            (txn_id, folder, filename),
        ).fetchall()
    annotations = {str(r["field_idx"]): r["status"] for r in rows}
    return jsonify({"annotations": annotations})


@app.route("/api/field-annotations/<path:folder>/<filename>", methods=["POST"])
def save_field_annotation(folder, filename):
    """Upsert a single field annotation and log to audit."""
    if err := _validate_doc_path_inputs(folder, filename):
        return err
    body = request.json or {}
    txn_id = body.get("txn", "")
    field_idx = body.get("field_idx")
    status = body.get("status", "")
    field_name = body.get("field_name", "")
    if field_idx is None or status not in ("filled", "empty", "optional", "ignored"):
        return jsonify({"error": "field_idx and valid status required"}), 400
    with db.conn() as c:
        c.execute(
            "INSERT INTO field_annotations(txn, folder, filename, field_idx, status)"
            " VALUES(?,?,?,?,?)"
            " ON CONFLICT(txn, folder, filename, field_idx)"
            " DO UPDATE SET status=excluded.status, updated_at=datetime('now','localtime')",
            (txn_id, folder, filename, int(field_idx), status),
        )
        if txn_id:
            db.log(c, txn_id, "field_toggle",
                   f"{filename} field #{field_idx} '{field_name}' → {status}")
    return jsonify({"ok": True, "field_idx": field_idx, "status": status})


@app.route("/api/field-annotations/<path:folder>/<filename>/bulk", methods=["POST"])
def bulk_field_annotations(folder, filename):
    """Upsert multiple field annotations at once."""
    if err := _validate_doc_path_inputs(folder, filename):
        return err
    body = request.json or {}
    txn_id = body.get("txn", "")
    annotations = body.get("annotations", {})
    if not annotations:
        return jsonify({"error": "annotations required"}), 400
    valid = ("filled", "empty", "optional", "ignored")
    count = 0
    with db.conn() as c:
        for idx_str, status in annotations.items():
            if status not in valid:
                continue
            c.execute(
                "INSERT INTO field_annotations(txn, folder, filename, field_idx, status)"
                " VALUES(?,?,?,?,?)"
                " ON CONFLICT(txn, folder, filename, field_idx)"
                " DO UPDATE SET status=excluded.status, updated_at=datetime('now','localtime')",
                (txn_id, folder, filename, int(idx_str), status),
            )
            count += 1
        if txn_id and count:
            db.log(c, txn_id, "field_annotations_saved",
                   f"{filename}: {count} field annotations saved")
    return jsonify({"ok": True, "saved": count})


# ── Contract Scanner / Verification Workflow ──────────────────────────────────

@app.route("/api/contracts/scan", methods=["POST"])
def scan_contracts():
    """Scan PDF folders, detect filled/empty fields, populate DB."""
    body = request.json or {}
    target = body.get("target", "filled")  # car, testing, filled, or a path
    scan_dirs = contract_scanner.SCAN_DIRS
    results = []

    if target == "all":
        for key, path in scan_dirs.items():
            if not path.exists():
                continue
            # For "filled" dir, scan each scenario subfolder
            if key == "filled":
                for scenario_dir in sorted(d for d in path.iterdir() if d.is_dir()):
                    r = contract_scanner.scan_folder(scenario_dir, scenario=scenario_dir.name)
                    results.extend(r)
            else:
                r = contract_scanner.scan_folder(path, scenario=key)
                results.extend(r)
    elif target == "filled":
        filled_dir = scan_dirs.get("filled")
        if filled_dir and filled_dir.exists():
            for scenario_dir in sorted(d for d in filled_dir.iterdir() if d.is_dir()):
                r = contract_scanner.scan_folder(scenario_dir, scenario=scenario_dir.name)
                results.extend(r)
    elif target in scan_dirs:
        path = scan_dirs[target]
        if path.exists():
            results = contract_scanner.scan_folder(path, scenario=target)
    else:
        return jsonify({"error": f"unknown target: {target}"}), 400

    if results:
        contract_scanner.populate_db(results)

    return jsonify({
        "scanned": len(results),
        "total_fields": sum(r["total_fields"] for r in results),
        "filled": sum(r["filled_fields"] for r in results),
        "unfilled_mandatory": sum(r["unfilled_mandatory"] for r in results),
    })


@app.route("/api/contracts")
def list_contracts():
    """List all scanned contracts with summary stats."""
    scenario = request.args.get("scenario", "")
    status = request.args.get("status", "")
    with db.conn() as c:
        q = "SELECT * FROM contracts"
        params = []
        clauses = []
        if scenario:
            clauses.append("scenario=?")
            params.append(scenario)
        if status:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY folder, filename, scenario"
        rows = c.execute(q, params).fetchall()
    items = [dict(r) for r in rows]
    # Summary
    total = len(items)
    unverified = sum(1 for i in items if i["status"] == "unverified")
    verified = sum(1 for i in items if i["status"] == "verified")
    return jsonify({
        "items": items,
        "summary": {"total": total, "unverified": unverified, "verified": verified},
    })


@app.route("/api/contracts/<int:cid>")
def get_contract(cid):
    """Get contract details with all fields."""
    with db.conn() as c:
        ct = c.execute("SELECT * FROM contracts WHERE id=?", (cid,)).fetchone()
        if not ct:
            return jsonify({"error": "not found"}), 404
        fields = c.execute(
            "SELECT * FROM contract_fields WHERE contract_id=?"
            " ORDER BY page, field_idx",
            (cid,),
        ).fetchall()
    return jsonify({
        "contract": dict(ct),
        "fields": [dict(f) for f in fields],
    })


@app.route("/api/contracts/<int:cid>/fields/unfilled")
def get_unfilled_fields(cid):
    """Get only unfilled, unverified fields for the verification workflow."""
    with db.conn() as c:
        ct = c.execute("SELECT * FROM contracts WHERE id=?", (cid,)).fetchone()
        if not ct:
            return jsonify({"error": "not found"}), 404
        fields = c.execute(
            "SELECT * FROM contract_fields WHERE contract_id=?"
            " AND is_filled=0 AND status='unverified'"
            " ORDER BY mandatory DESC, page, field_idx",
            (cid,),
        ).fetchall()
    return jsonify({
        "contract": dict(ct),
        "fields": [dict(f) for f in fields],
    })


@app.route("/api/contracts/<int:cid>/fields/verify-queue")
def get_verify_queue(cid):
    """Get ALL fields in priority order for guided verification.

    Mode query param:
      quick  — mandatory unfilled only (fastest)
      review — all unfilled (standard)
      full   — every field including filled (thorough)
    """
    mode = request.args.get("mode", "review")
    with db.conn() as c:
        ct = c.execute("SELECT * FROM contracts WHERE id=?", (cid,)).fetchone()
        if not ct:
            return jsonify({"error": "not found"}), 404
        if mode == "quick":
            fields = c.execute(
                "SELECT * FROM contract_fields WHERE contract_id=?"
                " AND is_filled=0 AND mandatory=1 AND status NOT IN ('verified','ignored')"
                " ORDER BY page, field_idx",
                (cid,),
            ).fetchall()
        elif mode == "full":
            # Everything: unfilled mandatory first, then unfilled optional, then filled
            fields = c.execute(
                "SELECT * FROM contract_fields WHERE contract_id=?"
                " ORDER BY"
                "  CASE WHEN status IN ('verified','ignored') THEN 2 ELSE 0 END,"
                "  CASE WHEN is_filled=0 AND mandatory=1 THEN 0"
                "       WHEN is_filled=0 AND mandatory=0 THEN 1"
                "       ELSE 2 END,"
                "  page, field_idx",
                (cid,),
            ).fetchall()
        else:  # review
            fields = c.execute(
                "SELECT * FROM contract_fields WHERE contract_id=?"
                " AND status NOT IN ('verified','ignored')"
                " ORDER BY"
                "  CASE WHEN is_filled=0 AND mandatory=1 THEN 0"
                "       WHEN is_filled=0 AND mandatory=0 THEN 1"
                "       ELSE 2 END,"
                "  page, field_idx",
                (cid,),
            ).fetchall()

        # Stats for progress display
        all_fields = c.execute(
            "SELECT is_filled, mandatory, status FROM contract_fields WHERE contract_id=?",
            (cid,),
        ).fetchall()
    total = len(all_fields)
    verified = sum(1 for f in all_fields if f["status"] in ("verified", "ignored"))
    flagged = sum(1 for f in all_fields if f["status"] == "flagged")
    unfilled_mand = sum(1 for f in all_fields if not f["is_filled"] and f["mandatory"])
    unfilled_opt = sum(1 for f in all_fields if not f["is_filled"] and not f["mandatory"])
    filled = sum(1 for f in all_fields if f["is_filled"])
    return jsonify({
        "contract": dict(ct),
        "fields": [dict(f) for f in fields],
        "stats": {
            "total": total, "verified": verified, "flagged": flagged,
            "unfilled_mandatory": unfilled_mand, "unfilled_optional": unfilled_opt,
            "filled": filled, "queue_size": len(fields),
        },
        "mode": mode,
    })


@app.route("/api/contracts/<int:cid>/fields/<int:fid>/crop")
def field_crop_image(cid, fid):
    """Serve a cropped PNG screenshot of a field from the actual PDF.

    Query params:
      zoom — render zoom factor (default 2.0, max 4.0)
      padding — padding around field in points (default 20, max 80)
      highlight — if 1, draw a highlight box around the field (default 1)
    """
    with db.conn() as c:
        ct = c.execute("SELECT * FROM contracts WHERE id=?", (cid,)).fetchone()
        if not ct:
            return jsonify({"error": "contract not found"}), 404
        field = c.execute(
            "SELECT * FROM contract_fields WHERE id=? AND contract_id=?",
            (fid, cid),
        ).fetchone()
        if not field:
            return jsonify({"error": "field not found"}), 404

    source = ct["source_path"]
    if not Path(source).exists():
        return jsonify({"error": "PDF file not found"}), 404

    bbox = json.loads(field["bbox"]) if isinstance(field["bbox"], str) else field["bbox"]
    zoom = min(float(request.args.get("zoom", 3.0)), 4.0)
    padding = min(int(request.args.get("padding", 40)), 80)
    highlight = request.args.get("highlight", "1") == "1"

    png = contract_scanner.render_field_crop(
        source, field["page"], bbox, padding=padding, zoom=zoom,
    )
    if not png:
        return jsonify({"error": "crop failed"}), 500

    # Optionally draw highlight rectangle on the crop
    if highlight and png:
        import io
        from PIL import Image, ImageDraw
        try:
            img = Image.open(io.BytesIO(png))
            draw = ImageDraw.Draw(img)
            # The field is centered in the crop with padding*zoom on each side
            fx0 = padding * zoom
            fy0 = padding * zoom
            fw = (bbox.get("x1", 0) - bbox.get("x0", 0)) * zoom
            fh = (bbox.get("y1", 0) - bbox.get("y0", 0)) * zoom
            # Determine color based on field status
            is_filled = field["is_filled"]
            mandatory = field["mandatory"]
            if is_filled:
                color = (52, 199, 89, 80)  # green
            elif mandatory:
                color = (255, 59, 48, 100)  # red
            else:
                color = (255, 204, 0, 80)  # yellow
            # Draw semi-transparent highlight
            overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
            od = ImageDraw.Draw(overlay)
            od.rectangle([fx0 - 2, fy0 - 2, fx0 + fw + 2, fy0 + fh + 2],
                         outline=color[:3], width=3)
            od.rectangle([fx0, fy0, fx0 + fw, fy0 + fh],
                         fill=(*color[:3], 30))
            img = Image.alpha_composite(img.convert("RGBA"), overlay)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            png = buf.getvalue()
        except Exception:
            pass  # Fall back to unhighlighted crop

    return Response(png, mimetype="image/png")


@app.route("/api/contracts/<int:cid>/fields/<int:fid>/verify", methods=["POST"])
def verify_field(cid, fid):
    """Mark a field as verified or ignored."""
    body = request.json or {}
    new_status = body.get("status", "verified")
    notes = body.get("notes", "")
    if new_status not in ("verified", "ignored", "flagged", "unverified"):
        return jsonify({"error": "invalid status"}), 400

    with db.conn() as c:
        field = c.execute(
            "SELECT * FROM contract_fields WHERE id=? AND contract_id=?",
            (fid, cid),
        ).fetchone()
        if not field:
            return jsonify({"error": "not found"}), 404
        now = "datetime('now','localtime')"
        if new_status in ("verified", "ignored"):
            c.execute(
                f"UPDATE contract_fields SET status=?, verified_at={now}, notes=?"
                " WHERE id=?",
                (new_status, notes, fid),
            )
        else:
            c.execute(
                "UPDATE contract_fields SET status=?, verified_at=NULL, notes=?"
                " WHERE id=?",
                (new_status, notes, fid),
            )
        # Update contract verified_count
        vc = c.execute(
            "SELECT COUNT(*) as cnt FROM contract_fields"
            " WHERE contract_id=? AND status IN ('verified','ignored')",
            (cid,),
        ).fetchone()["cnt"]
        total = c.execute(
            "SELECT total_fields FROM contracts WHERE id=?", (cid,),
        ).fetchone()["total_fields"]
        ct_status = "verified" if vc >= total else "unverified"
        c.execute(
            "UPDATE contracts SET verified_count=?, status=? WHERE id=?",
            (vc, ct_status, cid),
        )
        updated = c.execute(
            "SELECT * FROM contract_fields WHERE id=?", (fid,),
        ).fetchone()
    return jsonify(dict(updated))


@app.route("/api/contracts/<int:cid>/verify-filled", methods=["POST"])
def verify_all_filled(cid):
    """Auto-verify all filled fields in a contract (quick approve)."""
    with db.conn() as c:
        ct = c.execute("SELECT * FROM contracts WHERE id=?", (cid,)).fetchone()
        if not ct:
            return jsonify({"error": "not found"}), 404
        c.execute(
            "UPDATE contract_fields SET status='verified',"
            " verified_at=datetime('now','localtime')"
            " WHERE contract_id=? AND is_filled=1 AND status='unverified'",
            (cid,),
        )
        updated = c.execute(
            "SELECT COUNT(*) as cnt FROM contract_fields"
            " WHERE contract_id=? AND is_filled=1 AND status='verified'",
            (cid,),
        ).fetchone()["cnt"]
        vc = c.execute(
            "SELECT COUNT(*) as cnt FROM contract_fields"
            " WHERE contract_id=? AND status IN ('verified','ignored')",
            (cid,),
        ).fetchone()["cnt"]
        ct_status = "verified" if vc >= ct["total_fields"] else "unverified"
        c.execute(
            "UPDATE contracts SET verified_count=?, status=? WHERE id=?",
            (vc, ct_status, cid),
        )
    return jsonify({"ok": True, "verified": updated, "total_verified": vc})


@app.route("/api/contracts/<int:cid>/annotated-pdf")
def contract_annotated_pdf(cid):
    """Serve an annotated PDF with color-coded circles for a scanned contract."""
    with db.conn() as c:
        ct = c.execute("SELECT * FROM contracts WHERE id=?", (cid,)).fetchone()
        if not ct:
            return jsonify({"error": "not found"}), 404
        fields = c.execute(
            "SELECT * FROM contract_fields WHERE contract_id=? ORDER BY page, field_idx",
            (cid,),
        ).fetchall()

    source = ct["source_path"]
    if not Path(source).exists():
        return jsonify({"error": "PDF not found"}), 404

    # Generate into a temp file
    import tempfile
    tmp = Path(tempfile.mktemp(suffix=".pdf"))
    contract_scanner.generate_annotated_pdf(
        source, [dict(f) for f in fields], tmp,
    )
    data = tmp.read_bytes()
    tmp.unlink(missing_ok=True)

    return Response(data, mimetype="application/pdf",
                    headers={"Content-Disposition": f"inline; filename=annotated-{ct['filename']}"})


# ── Contract Review (Clause-Level Analysis) ──────────────────────────────────

@app.route("/api/txns/<tid>/review-contract", methods=["POST"])
def review_contract(tid):
    """Run clause-level contract review against a playbook.

    Accepts either:
      - {"doc_code": "rpa"} to review a previously uploaded doc
      - A multipart file upload with key "file"
      - {"contract_id": 5} to review a scanned contract by ID
    """
    service = "anthropic"
    operation = "review_contract"
    endpoint = "anthropic://messages.create"
    model = "claude-sonnet-4-20250514"
    with db.conn() as c:
        row = db.txn(c, tid)
        if not row:
            return jsonify({"error": "transaction not found"}), 404
        try:
            cloud_guard.require_approval(c, tid, service, operation)
        except cloud_guard.CloudApprovalRequired as exc:
            cloud_guard.log_cloud_event(
                c,
                txn=tid,
                service=service,
                operation=operation,
                endpoint=endpoint,
                model=model,
                approved=0,
                outcome="blocked",
                status_code=403,
                error=str(exc),
            )
            return _cloud_blocked_response(tid, str(exc))
        t = _txn_dict(row)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        with db.conn() as c:
            cloud_guard.log_cloud_event(
                c,
                txn=tid,
                service=service,
                operation=operation,
                endpoint=endpoint,
                model=model,
                approved=1,
                outcome="error",
                status_code=500,
                error="ANTHROPIC_API_KEY not set",
            )
        return jsonify({"error": "ANTHROPIC_API_KEY not set"}), 500

    playbook_name = (request.form or request.json or {}).get("playbook", "california_rpa")

    # Build transaction context for the review prompt
    txn_data = t.get("data", {})
    txn_context = {
        "address": t.get("address", ""),
        "transaction_type": t.get("txn_type", "sale"),
        "phase": t.get("phase", ""),
        "brokerage": t.get("brokerage", ""),
        "purchase_price": (txn_data.get("financial") or {}).get("purchase_price"),
        "close_of_escrow": (txn_data.get("dates") or {}).get("close_of_escrow"),
        "property_flags": t.get("props", {}),
    }

    # Resolve PDF path
    pdf_path = None
    doc_code = None

    if "file" in request.files:
        # Direct file upload
        f = request.files["file"]
        import tempfile
        tmp = Path(tempfile.mktemp(suffix=".pdf"))
        f.save(str(tmp))
        pdf_path = str(tmp)
    else:
        body = request.json or {}
        if body.get("contract_id"):
            # Review a scanned contract
            with db.conn() as c:
                ct = c.execute("SELECT * FROM contracts WHERE id=?",
                               (body["contract_id"],)).fetchone()
            if not ct:
                return jsonify({"error": "contract not found"}), 404
            pdf_path = ct["source_path"]
        elif body.get("doc_code"):
            # Review an uploaded transaction doc
            doc_code = body["doc_code"]
            with db.conn() as c:
                doc = c.execute("SELECT * FROM docs WHERE txn=? AND code=?",
                                (tid, doc_code)).fetchone()
            if not doc or not doc.get("file_path"):
                return jsonify({"error": f"No uploaded file for doc '{doc_code}'"}), 404
            pdf_path = doc["file_path"]

    if not pdf_path or not Path(pdf_path).exists():
        return jsonify({"error": "No PDF file found to review"}), 400

    request_bytes = 0
    try:
        request_bytes = Path(pdf_path).stat().st_size
    except Exception:
        request_bytes = 0

    # Run the review
    started = time.perf_counter()
    try:
        result = engine.review_contract(pdf_path, txn_context, playbook_name)
        latency_ms = int((time.perf_counter() - started) * 1000)
        response_bytes = len(json.dumps(result).encode("utf-8"))
        with db.conn() as c:
            cloud_guard.log_cloud_event(
                c,
                txn=tid,
                service=service,
                operation=operation,
                endpoint=endpoint,
                model=model,
                approved=1,
                outcome="success",
                status_code=200,
                latency_ms=latency_ms,
                request_bytes=request_bytes,
                response_bytes=response_bytes,
                meta={"playbook": playbook_name, "doc_code": doc_code or ""},
            )
    except Exception as e:
        latency_ms = int((time.perf_counter() - started) * 1000)
        with db.conn() as c:
            cloud_guard.log_cloud_event(
                c,
                txn=tid,
                service=service,
                operation=operation,
                endpoint=endpoint,
                model=model,
                approved=1,
                outcome="error",
                status_code=500,
                latency_ms=latency_ms,
                request_bytes=request_bytes,
                error=str(e),
                meta={"playbook": playbook_name, "doc_code": doc_code or ""},
            )
        return jsonify({"error": f"Review failed: {str(e)}"}), 500
    finally:
        # Clean up temp file if we created one
        if "file" in request.files:
            Path(pdf_path).unlink(missing_ok=True)

    # Store the review
    with db.conn() as c:
        c.execute(
            "INSERT INTO contract_reviews(txn, doc_code, playbook, overall_risk,"
            " executive_summary, clauses, interactions, missing_items, raw_response)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (tid, doc_code or "", playbook_name,
             result.get("overall_risk", "GREEN"),
             result.get("executive_summary", ""),
             json.dumps(result.get("clauses", [])),
             json.dumps(result.get("interactions", [])),
             json.dumps(result.get("missing_items", [])),
             result.get("_raw", "")),
        )
        review_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.log(c, tid, "contract_reviewed",
               f"Playbook: {playbook_name}, Risk: {result.get('overall_risk', '?')}")

    return jsonify({
        "id": review_id,
        "overall_risk": result.get("overall_risk", "GREEN"),
        "executive_summary": result.get("executive_summary", ""),
        "clauses": result.get("clauses", []),
        "interactions": result.get("interactions", []),
        "missing_items": result.get("missing_items", []),
    })


@app.route("/api/txns/<tid>/contract-reviews")
def list_contract_reviews(tid):
    """List all contract reviews for a transaction."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT * FROM contract_reviews WHERE txn=? ORDER BY created_at DESC",
            (tid,),
        ).fetchall()
    items = []
    for r in rows:
        item = dict(r)
        item["clauses"] = json.loads(item.get("clauses") or "[]")
        item["interactions"] = json.loads(item.get("interactions") or "[]")
        item["missing_items"] = json.loads(item.get("missing_items") or "[]")
        del item["raw_response"]  # don't send raw to client
        items.append(item)
    return jsonify(items)


@app.route("/api/txns/<tid>/contract-reviews/<int:rid>")
def get_contract_review(tid, rid):
    """Get a single contract review."""
    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM contract_reviews WHERE id=? AND txn=?",
            (rid, tid),
        ).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    item = dict(row)
    item["clauses"] = json.loads(item.get("clauses") or "[]")
    item["interactions"] = json.loads(item.get("interactions") or "[]")
    item["missing_items"] = json.loads(item.get("missing_items") or "[]")
    del item["raw_response"]
    return jsonify(item)


# ── Document Upload ───────────────────────────────────────────────────────────

@app.route("/api/txns/<tid>/upload", methods=["POST"])
def upload_document(tid):
    """Upload a PDF document to a transaction. Stores in CAR Contract Packages/<tid>/."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename or not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are accepted"}), 400
    if not _validate_pdf_upload(f):
        return jsonify({"error": "Uploaded file does not look like a valid PDF"}), 400

    with db.conn() as c:
        t = db.txn(c, tid)
        if not t:
            return jsonify({"error": "transaction not found"}), 404

    # Store in a transaction-specific folder
    upload_dir = doc_versions.CAR_DIR / f"uploads_{tid}"
    upload_dir.mkdir(parents=True, exist_ok=True)
    original_name = Path(f.filename).name
    safe_stem = re.sub(r"[^\w\-. ()]", "_", Path(original_name).stem).strip(" ._")
    if not safe_stem:
        safe_stem = "upload"
    safe_name = f"{safe_stem[:80]}_{uuid4().hex[:8]}.pdf"
    dest = upload_dir / safe_name
    f.save(str(dest))

    # Try to auto-scan the document for fields
    result = {
        "filename": safe_name,
        "original_filename": original_name,
        "folder": f"uploads_{tid}",
        "path": str(dest),
    }
    try:
        scan_result = contract_scanner.scan_pdf(dest, f"uploads_{tid}", scenario=tid)
        if scan_result:
            contract_scanner.populate_db([scan_result])
            fields = scan_result.get("fields", [])
            with db.conn() as c:
                row = c.execute(
                    "SELECT id FROM contracts WHERE folder=? AND filename=? AND scenario=?",
                    (f"uploads_{tid}", safe_name, tid),
                ).fetchone()
                if row:
                    result["contract_id"] = row["id"]
                db.log(c, tid, "doc_uploaded", f"{safe_name}: {len(fields)} fields detected")
            result["fields_detected"] = len(fields)
            result["fields"] = fields
    except Exception as e:
        result["scan_error"] = str(e)

    # Try to match to a checklist doc code and auto-receive
    matched_code = _match_upload_to_doc(
        tid, safe_name, folder=f"uploads_{tid}", match_name=original_name
    )
    if matched_code:
        result["matched_doc"] = matched_code

    # Auto-split: detect multiple forms in the PDF
    split_results = _split_multi_doc_pdf(tid, str(dest), safe_name)
    if split_results:
        result["split_docs"] = split_results

    # If an RPA was detected (directly or via split), extract dates and auto-populate
    rpa_detected = matched_code == "RPA" or (split_results and any(s.get("code") == "RPA" for s in split_results))
    if rpa_detected:
        rpa_result = _extract_rpa_and_populate(tid, str(dest), split_results)
        if rpa_result:
            result["rpa_extraction"] = rpa_result

    return jsonify(result), 201


@app.route("/api/txns/<tid>/reanalyze-rpa", methods=["POST"])
def reanalyze_rpa(tid):
    """Re-extract dates and contingencies from an already-uploaded RPA."""
    with db.conn() as c:
        t = db.txn(c, tid)
        if not t:
            return jsonify({"error": "transaction not found"}), 404
        rpa_doc = c.execute("SELECT * FROM docs WHERE txn=? AND code='RPA'", (tid,)).fetchone()
        if not rpa_doc or rpa_doc["status"] == "required":
            return jsonify({"error": "No RPA uploaded for this transaction"}), 404

    # Find the RPA file path
    folder = rpa_doc["folder"] or f"uploads_{tid}"
    filename = rpa_doc["filename"] or ""
    rpa_path = None
    if filename:
        rpa_path = str(doc_versions.CAR_DIR / folder / filename)
    if not rpa_path or not Path(rpa_path).exists():
        # Try to find any RPA PDF in the uploads directory
        upload_dir = doc_versions.CAR_DIR / f"uploads_{tid}"
        if upload_dir.exists():
            for f in upload_dir.iterdir():
                if f.suffix.lower() == ".pdf" and "rpa" in f.name.lower():
                    rpa_path = str(f)
                    break
    if not rpa_path or not Path(rpa_path).exists():
        return jsonify({"error": "RPA file not found on disk"}), 404

    result = _extract_rpa_and_populate(tid, rpa_path)
    return jsonify(result)


def _match_upload_to_doc(
    tid: str, filename: str, folder: str = "", match_name: str = ""
) -> str | None:
    """Try to match an uploaded filename to a checklist doc code and mark received."""
    needle = (match_name or filename).lower()
    # Common CAR form abbreviation matching
    code_patterns = {
        "rpa": "RPA", "tds": "TDS", "spq": "SPQ", "nhd": "NHD",
        "ad": "AD", "bia": "BIA", "wfi": "WFI", "sbsa": "SBSA",
        "avid": "AVID", "prbs": "PRBS", "cr1": "CR1",
        "appraisal": "APPR_RPT", "inspection": "INSP_RPT",
        "pest": "PEST_RPT", "termite": "PEST_RPT",
        "title": "PTITLE", "closing_disc": "CLOSING_DISC",
        "grant_deed": "GRANT_DEED", "w9": "W9_SELLER",
        "loan": "LOAN_COMMIT",
    }
    matched = None
    for pattern, code in code_patterns.items():
        if pattern in needle:
            matched = code
            break
    if matched:
        with db.conn() as c:
            row = c.execute("SELECT * FROM docs WHERE txn=? AND code=?", (tid, matched)).fetchone()
            if row and row["status"] == "required":
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                c.execute(
                    "UPDATE docs SET status='received', received=?, folder=?, filename=?"
                    " WHERE txn=? AND code=?",
                    (now, folder, filename, tid, matched),
                )
                db.log(
                    c,
                    tid,
                    "doc_received",
                    f"{matched} auto-matched from upload: {match_name or filename}",
                )
        return matched
    return None


def _match_text_to_code(page_text: str) -> str | None:
    """Match page text content to a CAR form code by detecting form headers."""
    text = page_text.upper()
    # CAR form header patterns — ordered by specificity
    header_patterns = [
        # Must check specific forms before generic patterns
        ("RESIDENTIAL PURCHASE AGREEMENT", "RPA"),
        ("TRANSFER DISCLOSURE STATEMENT", "TDS"),
        ("SELLER PROPERTY QUESTIONNAIRE", "SPQ"),
        ("NATURAL HAZARD DISCLOSURE", "NHD"),
        ("AGENT VISUAL INSPECTION DISCLOSURE", "AVID"),
        ("STATEWIDE BUYER AND SELLER ADVISORY", "SBSA"),
        ("BUYER'S INVESTIGATION ADVISORY", "BIA"),
        ("BUYER INSPECTION ADVISORY", "BIA"),
        ("WIRE FRAUD AND ELECTRONIC FUNDS TRANSFER", "WFI"),
        ("WIRE FRAUD ADVISORY", "WFI"),
        ("DISCLOSURE REGARDING REAL ESTATE AGENCY RELATIONSHIP", "AD"),
        ("(C.A.R. FORM AD,", "AD"),
        ("POSSIBLE REPRESENTATION OF MORE THAN ONE", "PRBS"),
        ("CONTINGENCY REMOVAL", "CR1"),
        ("LEAD-BASED PAINT", "LBP"),
        ("EARTHQUAKE HAZARDS REPORT", "EQFZ"),
        ("FIRE HAZARD SEVERITY", "FIRZ"),
        ("FIRE HARDENING AND DEFENSIBLE SPACE", "FIRZ"),
        ("FLOOD HAZARD", "FLD"),
        ("SMOKE DETECTOR", "SMOKE_CO"),
        ("WATER HEATER AND SMOKE DETECTOR", "WHSD"),
        ("WATER-CONSERVING PLUM", "WHSD"),
        ("MEGAN'S LAW", "MEG"),
        ("SUPPLEMENTAL STATUTORY", "SSD"),
        ("SUPPLEMENTAL DISCLOSURES", "DE_SUPP"),
        ("REQUEST FOR REPAIR", "RR"),
        ("FAIR HOUSING AND DISCRIMINATION ADVISORY", "FHDA"),
        ("BUYER HOMEOWNERS' INSURANCE ADVISORY", "BHIA"),
        ("BUYER HOMEOWNERS ASSOCIATION ADVISORY", "HOA_DOCS"),
        ("BUYER REPRESENTATION AGREEMENT", "BUYER_REP_AGR"),
        ("BUYER REPRESENTATION AND BROKER COMPENSATION", "BUYER_REP_AGR"),
        ("RESIDENTIAL LISTING AGREEMENT", "DE_LISTING_AGR"),
        ("LEASE LISTING AGREEMENT", "DE_LEASE_LIST"),
        ("MARKET CONDITIONS ADVISORY", "MCA"),
        ("DISCLOSURE INFORMATION ADVISORY", "AD"),
        ("AFFILIATED BUSINESS ARRANGEMENT", "ABDA"),
        ("APPRAISAL REPORT", "APPR_RPT"),
        ("HOME INSPECTION REPORT", "INSP_RPT"),
        ("PEST INSPECTION", "PEST_RPT"),
        ("TERMITE INSPECTION", "PEST_RPT"),
        ("PRELIMINARY TITLE", "PTITLE"),
        ("CLOSING DISCLOSURE", "CLOSING_DISC"),
        ("HOA DOCUMENTS", "HOA_DOCS"),
        ("CALIFORNIA CONSUMER PRIVACY ACT", "CCPA"),
        ("FEDERAL REPORTING REQUIREMENT", "FRR"),
        ("SQUARE FOOT AND LOT SIZE", "SQFT"),
    ]
    for pattern, code in header_patterns:
        if pattern in text:
            return code
    return None


def _split_multi_doc_pdf(tid: str, pdf_path: str, original_name: str) -> list[dict]:
    """Scan a multi-page PDF for different CAR forms. Split and auto-file each."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return []

    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return []

    if doc.page_count < 2:
        doc.close()
        return []

    # Scan each page for form headers
    page_codes = []
    for i in range(doc.page_count):
        text = doc[i].get_text()
        code = _match_text_to_code(text)
        page_codes.append(code)

    # Group consecutive pages by form code
    segments = []  # [(code, start_page, end_page)]
    current_code = None
    start = 0
    for i, code in enumerate(page_codes):
        if code and code != current_code:
            if current_code:
                segments.append((current_code, start, i - 1))
            current_code = code
            start = i
        elif not code and current_code:
            # Continuation page (no header) — belongs to current form
            pass
    if current_code:
        segments.append((current_code, start, doc.page_count - 1))

    # Only split if we found multiple distinct forms
    if len(segments) < 2:
        doc.close()
        return []

    upload_dir = doc_versions.CAR_DIR / f"uploads_{tid}"
    upload_dir.mkdir(parents=True, exist_ok=True)
    results = []

    for code, start_pg, end_pg in segments:
        # Extract pages into a new PDF
        split_doc = fitz.open()
        split_doc.insert_pdf(doc, from_page=start_pg, to_page=end_pg)
        split_name = f"{code}_{original_name}"
        split_path = upload_dir / split_name
        split_doc.save(str(split_path))
        split_doc.close()

        # Auto-match to checklist — use the detected code directly
        matched = None
        with db.conn() as c:
            row = c.execute("SELECT * FROM docs WHERE txn=? AND code=?", (tid, code)).fetchone()
            if row and row["status"] == "required":
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                c.execute(
                    "UPDATE docs SET status='received', received=?, folder=?, filename=?"
                    " WHERE txn=? AND code=?",
                    (now, f"uploads_{tid}", split_name, tid, code),
                )
                db.log(c, tid, "doc_received", f"{code} extracted from {original_name} (pages {start_pg+1}-{end_pg+1})")
                matched = code

        results.append({
            "code": code,
            "filename": split_name,
            "pages": f"{start_pg + 1}-{end_pg + 1}",
            "matched": matched,
        })

    doc.close()

    # Log the split
    with db.conn() as c:
        for r in results:
            db.log(c, tid, "doc_split", f"Extracted {r['code']} (pages {r['pages']}) from {original_name}")
        db.log(c, tid, "pdf_split", f"Split {original_name} into {len(segments)} documents: {', '.join(s[0] for s in segments)}")

    return results


def _extract_rpa_data(pdf_path: str) -> dict:
    """Extract dates, contingency periods, and key terms from an RPA PDF.

    Scans the text of the RPA looking for:
    - Purchase price
    - Close of escrow days/date
    - Contingency day counts (investigation, appraisal, loan)
    - Acceptance date / date prepared
    Returns a dict of extracted values.
    """
    try:
        import fitz
    except ImportError:
        return {}

    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return {}

    # Combine text from all pages
    full_text = ""
    for i in range(doc.page_count):
        page_text = doc[i].get_text()
        # Only process RPA pages (skip other forms in a combined packet)
        if i == 0 or "RESIDENTIAL PURCHASE AGREEMENT" in page_text.upper() or full_text:
            if full_text or "RESIDENTIAL PURCHASE AGREEMENT" in page_text.upper():
                full_text += page_text + "\n"
                # Stop if we hit a new form header that isn't RPA
                if i > 0 and full_text:
                    code = _match_text_to_code(page_text)
                    if code and code != "RPA":
                        break
    doc.close()

    if not full_text:
        return {}

    extracted = {}
    lines = full_text.split("\n")

    # ── Purchase Price ──
    for i, line in enumerate(lines):
        if "Purchase Price" in line and "$" in line:
            m = re.search(r'\$\s*([\d,]+(?:\.\d{2})?)', line)
            if m:
                extracted["purchase_price"] = m.group(1).replace(",", "")
                break
        # Also check next line for the dollar amount
        if "Purchase Price" in line and i + 1 < len(lines):
            m = re.search(r'\$\s*([\d,]+(?:\.\d{2})?)', lines[i + 1])
            if m:
                extracted["purchase_price"] = m.group(1).replace(",", "")
                break

    # ── Close of Escrow ──
    for i, line in enumerate(lines):
        if "Close Of" in line and "Escrow" in line:
            # Look for "X Days after Acceptance"
            context = " ".join(lines[i:i+3])
            m = re.search(r'(\d+)\s*(?:\(or\s*(\d+)?\s*\))?\s*Days?\s*after\s*Acceptance', context, re.IGNORECASE)
            if m:
                coe_days = int(m.group(2)) if m.group(2) else int(m.group(1))
                extracted["coe_days"] = coe_days
            # Look for explicit date "OR on (date)"
            dm = re.search(r'OR\s*on\s+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})', context, re.IGNORECASE)
            if dm:
                extracted["coe_date_raw"] = dm.group(1)
            break

    # ── Contingency Day Counts ──
    # Pattern: "17 (or X) Days after Acceptance" near contingency labels
    contingency_patterns = [
        (r'Loan\(?s?\)?', "loan_days"),
        (r'Appraisal', "appraisal_days"),
        (r'Investigation\s*(?:of\s*Property)?', "investigation_days"),
    ]
    for i, line in enumerate(lines):
        for pattern, key in contingency_patterns:
            if key not in extracted and re.search(pattern, line, re.IGNORECASE):
                # Search this line and next few for day count
                context = " ".join(lines[max(0,i-2):i+4])
                m = re.search(r'(\d+)\s*\(or\s*(\d+)?\s*\)\s*Days?\s*after\s*Acceptance', context, re.IGNORECASE)
                if m:
                    days = int(m.group(2)) if m.group(2) else int(m.group(1))
                    extracted[key] = days

    # ── "No contingency" flags and waivers ──
    waived = {}
    full_text_upper = full_text.upper()

    # Loan contingency waivers
    if any(p in full_text_upper for p in [
        "NO LOAN CONTINGENCY", "WAIVE LOAN CONTINGENCY", "WAIVES LOAN CONTINGENCY",
        "WITHOUT LOAN CONTINGENCY", "LOAN CONTINGENCY IS REMOVED",
        "LOAN CONTINGENCY: NONE", "LOAN CONTINGENCY WAIVED"
    ]):
        waived["loan"] = "waived"
        extracted["no_loan_contingency"] = True

    # Cash purchase = no loan contingency needed
    if any(p in full_text_upper for p in [
        "ALL CASH", "CASH OFFER", "CASH PURCHASE", "NO FINANCING",
        "BUYER WILL PAY ALL CASH", "ALL-CASH"
    ]):
        waived["loan"] = "not_required"
        extracted["cash_purchase"] = True
        extracted["no_loan_contingency"] = True

    # Appraisal contingency waivers
    if any(p in full_text_upper for p in [
        "NO APPRAISAL CONTINGENCY", "WAIVE APPRAISAL CONTINGENCY", "WAIVES APPRAISAL",
        "WITHOUT APPRAISAL CONTINGENCY", "APPRAISAL CONTINGENCY IS REMOVED",
        "APPRAISAL CONTINGENCY: NONE", "APPRAISAL CONTINGENCY WAIVED",
        "APPRAISAL WAIVER"
    ]):
        waived["appraisal"] = "waived"
        extracted["no_appraisal_contingency"] = True

    # Investigation/inspection contingency waivers
    if any(p in full_text_upper for p in [
        "NO INVESTIGATION CONTINGENCY", "WAIVE INVESTIGATION", "AS-IS", "AS IS",
        "SOLD AS-IS", "PROPERTY SOLD AS IS", "WITHOUT INVESTIGATION CONTINGENCY",
        "INVESTIGATION CONTINGENCY WAIVED", "NO INSPECTION CONTINGENCY"
    ]):
        waived["investigation"] = "waived"
        extracted["no_investigation_contingency"] = True

    # Check for 0-day contingency periods (effectively waived)
    for i, line in enumerate(lines):
        context = " ".join(lines[max(0,i-1):i+3])
        # Look for "0 Days" or "(0)" days patterns near contingency labels
        if re.search(r'(?:loan|financing).*?(?:0|zero)\s*(?:\(0\))?\s*days?', context, re.IGNORECASE):
            if "loan" not in waived:
                waived["loan"] = "waived"
        if re.search(r'appraisal.*?(?:0|zero)\s*(?:\(0\))?\s*days?', context, re.IGNORECASE):
            if "appraisal" not in waived:
                waived["appraisal"] = "waived"
        if re.search(r'investigation.*?(?:0|zero)\s*(?:\(0\))?\s*days?', context, re.IGNORECASE):
            if "investigation" not in waived:
                waived["investigation"] = "waived"

    if waived:
        extracted["contingency_waivers"] = waived

    # ── Date Prepared ──
    for line in lines:
        if "Date Prepared" in line:
            m = re.search(r'Date Prepared[:\s]*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})', line)
            if m:
                extracted["date_prepared"] = m.group(1)
            break

    # ── Acceptance Date (typically on last page) ──
    for line in lines:
        if "Date" in line and "Acceptance" in line:
            m = re.search(r'(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})', line)
            if m:
                extracted["acceptance_date"] = m.group(1)

    # ── Party Names ──
    # Look for Buyer and Seller names near the top of the document
    parties = {}
    for i, line in enumerate(lines[:50]):  # Party info is usually in first ~50 lines
        # Buyer patterns: "Buyer:" or "Buyer(s):" followed by name
        if re.search(r'Buyer\(?s?\)?[:\s]', line, re.IGNORECASE):
            # Check same line and next line for name
            for check_line in [line, lines[i+1] if i+1 < len(lines) else ""]:
                # Skip if it looks like a field label
                if "address" in check_line.lower() or "signature" in check_line.lower():
                    continue
                # Extract text after "Buyer:" or similar
                m = re.search(r'Buyer\(?s?\)?[:\s]+([A-Z][a-zA-Z\s,\.]+?)(?:\s*$|,\s*(?:and|&))', check_line, re.IGNORECASE)
                if m and len(m.group(1).strip()) > 3:
                    parties["buyer_name"] = m.group(1).strip()
                    break
                # Also try just getting a capitalized name after Buyer
                m2 = re.search(r'Buyer\(?s?\)?[:\s]+([A-Z][a-z]+\s+[A-Z][a-z]+)', check_line)
                if m2:
                    parties["buyer_name"] = m2.group(1).strip()
                    break
        # Seller patterns
        if re.search(r'Seller\(?s?\)?[:\s]', line, re.IGNORECASE):
            for check_line in [line, lines[i+1] if i+1 < len(lines) else ""]:
                if "address" in check_line.lower() or "signature" in check_line.lower():
                    continue
                m = re.search(r'Seller\(?s?\)?[:\s]+([A-Z][a-zA-Z\s,\.]+?)(?:\s*$|,\s*(?:and|&))', check_line, re.IGNORECASE)
                if m and len(m.group(1).strip()) > 3:
                    parties["seller_name"] = m.group(1).strip()
                    break
                m2 = re.search(r'Seller\(?s?\)?[:\s]+([A-Z][a-z]+\s+[A-Z][a-z]+)', check_line)
                if m2:
                    parties["seller_name"] = m2.group(1).strip()
                    break
    if parties:
        extracted["parties"] = parties

    # Set defaults for anything not found
    extracted.setdefault("investigation_days", 17)
    extracted.setdefault("appraisal_days", 17)
    extracted.setdefault("loan_days", 21)
    extracted.setdefault("coe_days", 30)

    return extracted


def _auto_update_parties_from_extraction(c, tid: str, extracted_parties: dict):
    """Auto-update placeholder party records with extracted names from RPA."""
    role_map = {
        "buyer_name": "buyer",
        "seller_name": "seller",
        "listing_agent": "seller_agent",
        "buyer_agent": "buyer_agent",
    }
    for extract_key, role in role_map.items():
        name = extracted_parties.get(extract_key)
        if not name:
            continue
        # Find placeholder party with this role
        row = c.execute(
            "SELECT * FROM parties WHERE txn=? AND role=? AND name LIKE '%(TBD)%'",
            (tid, role),
        ).fetchone()
        if row:
            c.execute("UPDATE parties SET name=? WHERE id=?", (name, row["id"]))
            db.log(c, tid, "party_auto_filled", f"Updated {role} to '{name}' from RPA")


def _parse_date_flexible(date_str: str) -> date | None:
    """Parse dates in various formats: MM/DD/YYYY, MM-DD-YYYY, M/D/YY, etc."""
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%m-%d-%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _extract_rpa_and_populate(tid: str, pdf_path: str, split_results: list | None = None) -> dict:
    """Extract RPA data from PDF and auto-populate deadlines + contingencies.

    If the PDF is a combined packet with split results, find the RPA portion.
    """
    # Determine the actual RPA PDF path
    rpa_path = pdf_path
    if split_results:
        for sr in split_results:
            if sr.get("code") == "RPA" and sr.get("filename"):
                candidate = doc_versions.CAR_DIR / f"uploads_{tid}" / sr["filename"]
                if candidate.exists():
                    rpa_path = str(candidate)
                    break

    extracted = _extract_rpa_data(rpa_path)
    if not extracted:
        return {}

    result = {"extracted": extracted}

    with db.conn() as c:
        t = db.txn(c, tid)
        if not t:
            return result

        txn_data = json.loads(t.get("data") or "{}")

        # Try to determine acceptance date
        acceptance = None
        if "acceptance_date" in extracted:
            acceptance = _parse_date_flexible(extracted["acceptance_date"])
        if not acceptance and txn_data.get("dates", {}).get("acceptance"):
            acceptance = date.fromisoformat(txn_data["dates"]["acceptance"])
        if not acceptance and "date_prepared" in extracted:
            acceptance = _parse_date_flexible(extracted["date_prepared"])
        # Fallback: use transaction creation date as acceptance date
        if not acceptance:
            created_str = t.get("created", "")
            if created_str:
                try:
                    acceptance = date.fromisoformat(created_str[:10])
                except (ValueError, TypeError):
                    acceptance = date.today()
            else:
                acceptance = date.today()
            result["acceptance_provisional"] = True

        # Update transaction data with extracted values
        if "dates" not in txn_data:
            txn_data["dates"] = {}
        if "contingencies" not in txn_data:
            txn_data["contingencies"] = {}

        txn_data["dates"]["acceptance"] = acceptance.isoformat()

        coe_days = extracted.get("coe_days", 30)
        coe_date = acceptance + timedelta(days=coe_days)
        if "coe_date_raw" in extracted:
            parsed_coe = _parse_date_flexible(extracted["coe_date_raw"])
            if parsed_coe:
                coe_date = parsed_coe
        txn_data["dates"]["close_of_escrow"] = coe_date.isoformat()

        # Contingency day counts
        txn_data["contingencies"]["investigation_days"] = extracted.get("investigation_days", 17)
        txn_data["contingencies"]["appraisal_days"] = extracted.get("appraisal_days", 17)
        txn_data["contingencies"]["loan_days"] = extracted.get("loan_days", 21)
        txn_data["contingencies"]["deposit_days"] = 3

        # Purchase price
        if "purchase_price" in extracted:
            if "financial" not in txn_data:
                txn_data["financial"] = {}
            try:
                txn_data["financial"]["purchase_price"] = int(float(extracted["purchase_price"]))
            except (ValueError, TypeError):
                pass

        # Party names from RPA
        if extracted.get("parties"):
            if "parties" not in txn_data:
                txn_data["parties"] = {}
            for key, val in extracted["parties"].items():
                if val and not txn_data["parties"].get(key):
                    txn_data["parties"][key] = val
            # Also auto-update party records if we found names
            _auto_update_parties_from_extraction(c, tid, extracted["parties"])

        # Contingency waivers - auto-mark contingencies as waived/not_required
        waivers = extracted.get("contingency_waivers", {})
        if waivers:
            txn_data["contingencies"]["waivers"] = waivers
            waiver_msgs = []
            for cont_type, status in waivers.items():
                # Update contingency record if it exists
                row = c.execute(
                    "SELECT * FROM contingencies WHERE txn=? AND type=?",
                    (tid, cont_type),
                ).fetchone()
                if row:
                    # Mark as waived or not_required
                    new_status = "waived" if status == "waived" else "not_required"
                    c.execute(
                        "UPDATE contingencies SET status=?, waived_at=datetime('now','localtime'), "
                        "notes=COALESCE(notes,'') || ? WHERE txn=? AND type=?",
                        (new_status, f" [Auto-detected from RPA: {status}]", tid, cont_type),
                    )
                    waiver_msgs.append(f"{cont_type}={status}")
                else:
                    # Create contingency record marked as waived/not_required
                    cont_names = {
                        "loan": "Loan Contingency",
                        "appraisal": "Appraisal Contingency",
                        "investigation": "Investigation Contingency",
                    }
                    c.execute(
                        "INSERT OR IGNORE INTO contingencies(txn, type, name, status, default_days, notes) "
                        "VALUES(?, ?, ?, ?, 0, ?)",
                        (tid, cont_type, cont_names.get(cont_type, cont_type),
                         "waived" if status == "waived" else "not_required",
                         f"Auto-detected from RPA: {status}"),
                    )
                    waiver_msgs.append(f"{cont_type}={status}")
            if waiver_msgs:
                db.log(c, tid, "contingencies_auto_waived", ", ".join(waiver_msgs))
                result["contingencies_waived"] = waivers

        # Save updated txn data
        c.execute("UPDATE txns SET data=?, updated=datetime('now','localtime') WHERE id=?",
                  (json.dumps(txn_data), tid))
        db.log(c, tid, "rpa_extracted",
               f"acceptance={acceptance.isoformat()} COE={coe_date.isoformat()} "
               f"inv={extracted.get('investigation_days')}d apr={extracted.get('appraisal_days')}d "
               f"loan={extracted.get('loan_days')}d")

    # Calculate deadlines and contingencies using the engine
    try:
        engine.calc_deadlines(tid, acceptance, txn_data)
        result["deadlines_populated"] = True
        result["acceptance_date"] = acceptance.isoformat()
        result["coe_date"] = coe_date.isoformat()
    except Exception as e:
        result["deadline_error"] = str(e)

    return result


# ── Bug Reports ───────────────────────────────────────────────────────────────

@app.route("/api/bug-reports", methods=["GET"])
def list_bug_reports():
    """List all bug reports."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT * FROM bug_reports ORDER BY created_at DESC"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/bug-reports", methods=["POST"])
def create_bug_report():
    """Create a new bug report with optional screenshot."""
    body = request.json or {}
    summary = (body.get("summary") or "").strip()
    if not summary:
        return jsonify({"error": "summary required"}), 400
    description = body.get("description", "")
    screenshot = body.get("screenshot", "")  # base64 data URL
    action_log = json.dumps(body.get("action_log", []))
    url = body.get("url", "")

    with db.conn() as c:
        c.execute(
            "INSERT INTO bug_reports(summary, description, screenshot, action_log, url)"
            " VALUES(?,?,?,?,?)",
            (summary, description, screenshot, action_log, url),
        )
        bid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        row = c.execute("SELECT * FROM bug_reports WHERE id=?", (bid,)).fetchone()
    return jsonify(dict(row)), 201


@app.route("/api/bug-reports/<int:bid>/resolve", methods=["POST"])
def resolve_bug_report(bid):
    """Mark a bug report as resolved."""
    with db.conn() as c:
        row = c.execute("SELECT * FROM bug_reports WHERE id=?", (bid,)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        c.execute("UPDATE bug_reports SET status='resolved' WHERE id=?", (bid,))
        updated = c.execute("SELECT * FROM bug_reports WHERE id=?", (bid,)).fetchone()
    return jsonify(dict(updated))


# ── Review Notes ─────────────────────────────────────────────────────────────

@app.route("/api/review-notes", methods=["GET"])
def list_review_notes():
    """List review notes, optionally filtered by page and/or status."""
    page = request.args.get("page")
    status = request.args.get("status")
    with db.conn() as c:
        q = "SELECT * FROM review_notes WHERE 1=1"
        params = []
        if page:
            q += " AND page=?"
            params.append(page)
        if status:
            q += " AND status=?"
            params.append(status)
        q += " ORDER BY created_at DESC"
        rows = c.execute(q, params).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/review-notes", methods=["POST"])
def create_review_note():
    """Create a new review note."""
    body = request.json or {}
    page = (body.get("page") or "").strip()
    note = (body.get("note") or "").strip()
    if not page or not note:
        return jsonify({"error": "page and note required"}), 400
    with db.conn() as c:
        c.execute(
            "INSERT INTO review_notes(page, note) VALUES(?,?)",
            (page, note),
        )
        nid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        row = c.execute("SELECT * FROM review_notes WHERE id=?", (nid,)).fetchone()
    return jsonify(dict(row)), 201


@app.route("/api/review-notes/<int:nid>/resolve", methods=["POST"])
def resolve_review_note(nid):
    """Mark a review note as done."""
    with db.conn() as c:
        row = c.execute("SELECT * FROM review_notes WHERE id=?", (nid,)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        c.execute(
            "UPDATE review_notes SET status='done', resolved_at=datetime('now','localtime') WHERE id=?",
            (nid,),
        )
        updated = c.execute("SELECT * FROM review_notes WHERE id=?", (nid,)).fetchone()
    return jsonify(dict(updated))


@app.route("/api/review-notes/<int:nid>", methods=["DELETE"])
def delete_review_note(nid):
    """Delete a review note."""
    with db.conn() as c:
        row = c.execute("SELECT * FROM review_notes WHERE id=?", (nid,)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        c.execute("DELETE FROM review_notes WHERE id=?", (nid,))
    return jsonify({"ok": True})


# ── Chat (Claude-powered) ────────────────────────────────────────────────────

@app.route("/api/chat", methods=["POST"])
def chat():
    """Send a message to Claude with full transaction context."""
    body = request.json or {}
    message = body.get("message", "").strip()
    if not message:
        return jsonify({"error": "message required"}), 400

    tid = (body.get("txn_id") or "").strip()
    history = body.get("history", [])
    service = "anthropic"
    operation = "chat"
    endpoint = "https://api.anthropic.com/v1/messages"
    model = "claude-sonnet-4-20250514"

    if not tid:
        with db.conn() as c:
            cloud_guard.log_cloud_event(
                c,
                txn="",
                service=service,
                operation=operation,
                endpoint=endpoint,
                model=model,
                approved=0,
                outcome="blocked",
                status_code=403,
                error="txn_id is required for cloud chat",
            )
        return _cloud_blocked_response("", "txn_id is required for cloud chat")

    with db.conn() as c:
        row = db.txn(c, tid)
        if not row:
            return jsonify({"error": "transaction not found"}), 404
        try:
            cloud_guard.require_approval(c, tid, service, operation)
        except cloud_guard.CloudApprovalRequired as exc:
            cloud_guard.log_cloud_event(
                c,
                txn=tid,
                service=service,
                operation=operation,
                endpoint=endpoint,
                model=model,
                approved=0,
                outcome="blocked",
                status_code=403,
                error=str(exc),
            )
            return _cloud_blocked_response(tid, str(exc))
        t = _txn_dict(row)
        t["doc_stats"] = _doc_stats(c, tid)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        with db.conn() as c:
            cloud_guard.log_cloud_event(
                c,
                txn=tid,
                service=service,
                operation=operation,
                endpoint=endpoint,
                model=model,
                approved=1,
                outcome="error",
                status_code=500,
                error="ANTHROPIC_API_KEY not set",
            )
        return jsonify({"error": "ANTHROPIC_API_KEY not set. Add it to your .env file."}), 500

    # Build transaction context
    context_parts = []
    context_parts.append(f"Transaction: {t['address']} (ID: {tid})")
    context_parts.append(f"Type: {t['txn_type']}, Role: {t['party_role']}, Phase: {t['phase']}")
    if t.get("brokerage"):
        context_parts.append(f"Brokerage: {t['brokerage']}")
    ds = t["doc_stats"]
    context_parts.append(f"Docs: {ds['received']}/{ds['total']} received")

    # Gates
    gates = engine.gate_rows(tid)
    gate_info = rules.gate
    verified = sum(1 for g in gates if g["status"] == "verified")
    context_parts.append(f"Gates: {verified}/{len(gates)} verified")
    pending_gates = [g for g in gates if g["status"] != "verified"]
    if pending_gates:
        names = []
        for g in pending_gates[:5]:
            info = gate_info(g["gid"]) or {}
            names.append(info.get("name", g["gid"]))
        context_parts.append(f"Pending gates: {', '.join(names)}")

    # Deadlines
    dl_rows = engine.deadline_rows(tid)
    today = date.today()
    if dl_rows:
        urgent = []
        for d in dl_rows:
            if d.get("due"):
                days = (date.fromisoformat(d["due"]) - today).days
                if days <= 5:
                    urgent.append(f"{d['name']} ({days}d)")
        if urgent:
            context_parts.append(f"Urgent deadlines: {', '.join(urgent[:5])}")

    # Recent audit
    with db.conn() as c:
        audit_rows = c.execute(
            "SELECT action, detail, ts FROM audit WHERE txn=? ORDER BY ts DESC LIMIT 5",
            (tid,),
        ).fetchall()
    if audit_rows:
        audit_lines = [f"  {r['ts']}: {r['action']} - {r['detail']}" for r in audit_rows]
        context_parts.append("Recent activity:\n" + "\n".join(audit_lines))

    context = "\n".join(context_parts) if context_parts else "No transaction selected."

    # Build messages for Claude
    system_prompt = (
        "You are a California real estate transaction coordinator assistant. "
        "You help agents track documents, verify compliance gates, manage deadlines, "
        "and navigate the transaction process. Be concise, practical, and specific. "
        "Reference specific documents, gates, or deadlines when relevant.\n\n"
        f"Current transaction context:\n{context}"
    )

    api_messages = []
    # Include recent history
    for h in history[:-1]:  # exclude the latest (it's the current message)
        role = "user" if h.get("role") == "user" else "assistant"
        api_messages.append({"role": role, "content": h.get("content", "")})
    api_messages.append({"role": "user", "content": message})

    # Call Anthropic API
    api_body = json.dumps({
        "model": model,
        "max_tokens": 1024,
        "system": system_prompt,
        "messages": api_messages,
    })
    request_bytes = len(api_body.encode("utf-8"))

    req = urllib.request.Request(
        endpoint,
        data=api_body.encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )

    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            code = resp.status
            result = json.loads(raw.decode("utf-8"))
        reply = ""
        for block in result.get("content", []):
            if block.get("type") == "text":
                reply += block.get("text", "")
        latency_ms = int((time.perf_counter() - start) * 1000)
        with db.conn() as c:
            cloud_guard.log_cloud_event(
                c,
                txn=tid,
                service=service,
                operation=operation,
                endpoint=endpoint,
                model=model,
                approved=1,
                outcome="success",
                status_code=code,
                latency_ms=latency_ms,
                request_bytes=request_bytes,
                response_bytes=len(raw),
            )
        return jsonify({"reply": reply or "No response generated."})
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        latency_ms = int((time.perf_counter() - start) * 1000)
        with db.conn() as c:
            cloud_guard.log_cloud_event(
                c,
                txn=tid,
                service=service,
                operation=operation,
                endpoint=endpoint,
                model=model,
                approved=1,
                outcome="error",
                status_code=e.code,
                latency_ms=latency_ms,
                request_bytes=request_bytes,
                response_bytes=len(error_body.encode("utf-8")),
                error=error_body[:200],
            )
        return jsonify({"error": f"Claude API error ({e.code}): {error_body[:200]}"}), 502
    except Exception as e:
        latency_ms = int((time.perf_counter() - start) * 1000)
        with db.conn() as c:
            cloud_guard.log_cloud_event(
                c,
                txn=tid,
                service=service,
                operation=operation,
                endpoint=endpoint,
                model=model,
                approved=1,
                outcome="error",
                status_code=500,
                latency_ms=latency_ms,
                request_bytes=request_bytes,
                error=str(e),
            )
        return jsonify({"error": f"Chat error: {str(e)}"}), 500


# ── iCal Subscription Feed ───────────────────────────────────────────────────

def _ical_escape(text: str) -> str:
    """Escape special chars per RFC 5545."""
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _vevent(uid: str, dtstart: str, summary: str, description: str = "",
            categories: str = "", alarm_days: int = 2) -> str:
    """Build a single VEVENT block (all-day event)."""
    ds = dtstart.replace("-", "")  # 2026-02-18 → 20260218
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTART;VALUE=DATE:{ds}",
        f"SUMMARY:{_ical_escape(summary)}",
    ]
    if description:
        lines.append(f"DESCRIPTION:{_ical_escape(description)}")
    if categories:
        lines.append(f"CATEGORIES:{categories}")
    lines.append(f"DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}")
    if alarm_days > 0:
        lines.extend([
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:Due in {alarm_days} days",
            f"TRIGGER:-P{alarm_days}D",
            "END:VALARM",
        ])
    lines.append("END:VEVENT")
    return "\r\n".join(lines)


def _build_ical(events: list[str], cal_name: str = "TC Deadlines") -> str:
    """Wrap VEVENT blocks in a VCALENDAR."""
    header = "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//TransactionCoordinator//TC//EN",
        f"X-WR-CALNAME:{cal_name}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-TIMEZONE:America/Los_Angeles",
    ])
    footer = "END:VCALENDAR"
    body = "\r\n".join(events) if events else ""
    return header + "\r\n" + body + ("\r\n" if body else "") + footer + "\r\n"


@app.route("/api/calendar.ics")
def calendar_all():
    """iCal feed of all deadlines, contingencies, and disclosure due dates."""
    with db.conn() as c:
        events = []

        # Deadlines across all transactions
        for r in c.execute(
            "SELECT d.*, t.address FROM deadlines d"
            " JOIN txns t ON t.id = d.txn"
            " WHERE d.due IS NOT NULL AND d.status != 'met'"
        ).fetchall():
            r = dict(r)
            events.append(_vevent(
                uid=f"dl-{r['txn']}-{r['did']}@tc",
                dtstart=r["due"],
                summary=f"[Deadline] {r['name']}",
                description=f"{r['address']} — {r['did']}",
                categories="Deadline",
            ))

        # Contingency deadlines
        for r in c.execute(
            "SELECT g.*, t.address FROM contingencies g"
            " JOIN txns t ON t.id = g.txn"
            " WHERE g.deadline_date IS NOT NULL AND g.status = 'active'"
        ).fetchall():
            r = dict(r)
            events.append(_vevent(
                uid=f"cont-{r['txn']}-{r['type']}@tc",
                dtstart=r["deadline_date"],
                summary=f"[Contingency] {r['name']}",
                description=f"{r['address']} — remove or cancel by this date",
                categories="Contingency",
                alarm_days=3,
            ))

        # Disclosure due dates
        for r in c.execute(
            "SELECT d.*, t.address FROM disclosures d"
            " JOIN txns t ON t.id = d.txn"
            " WHERE d.due_date IS NOT NULL AND d.status = 'pending'"
        ).fetchall():
            r = dict(r)
            events.append(_vevent(
                uid=f"disc-{r['txn']}-{r['type']}@tc",
                dtstart=r["due_date"],
                summary=f"[Disclosure] {r['name']}",
                description=f"{r['address']} — {r['responsible']}",
                categories="Disclosure",
            ))

        # NBP expiry dates
        for r in c.execute(
            "SELECT g.*, t.address FROM contingencies g"
            " JOIN txns t ON t.id = g.txn"
            " WHERE g.nbp_expires_at IS NOT NULL AND g.status = 'active'"
        ).fetchall():
            r = dict(r)
            events.append(_vevent(
                uid=f"nbp-{r['txn']}-{r['type']}@tc",
                dtstart=r["nbp_expires_at"][:10],
                summary=f"[NBP Expires] {r['name']}",
                description=f"{r['address']} — buyer must respond or seller may cancel",
                categories="NBP",
                alarm_days=1,
            ))

    body = _build_ical(events)
    return Response(body, mimetype="text/calendar",
                    headers={"Content-Disposition": "inline; filename=tc-deadlines.ics"})


@app.route("/api/txns/<tid>/calendar.ics")
def calendar_txn(tid):
    """iCal feed for a single transaction."""
    with db.conn() as c:
        txn = c.execute("SELECT * FROM txns WHERE id=?", (tid,)).fetchone()
        if not txn:
            return jsonify({"error": "not found"}), 404

        addr = txn["address"]
        events = []

        for r in c.execute(
            "SELECT * FROM deadlines WHERE txn=? AND due IS NOT NULL AND status != 'met'",
            (tid,),
        ).fetchall():
            r = dict(r)
            events.append(_vevent(
                uid=f"dl-{tid}-{r['did']}@tc",
                dtstart=r["due"],
                summary=f"[Deadline] {r['name']}",
                description=f"{addr} — {r['did']}",
                categories="Deadline",
            ))

        for r in c.execute(
            "SELECT * FROM contingencies WHERE txn=? AND deadline_date IS NOT NULL AND status='active'",
            (tid,),
        ).fetchall():
            r = dict(r)
            events.append(_vevent(
                uid=f"cont-{tid}-{r['type']}@tc",
                dtstart=r["deadline_date"],
                summary=f"[Contingency] {r['name']}",
                description=f"{addr} — remove or cancel by this date",
                categories="Contingency",
                alarm_days=3,
            ))

        for r in c.execute(
            "SELECT * FROM disclosures WHERE txn=? AND due_date IS NOT NULL AND status='pending'",
            (tid,),
        ).fetchall():
            r = dict(r)
            events.append(_vevent(
                uid=f"disc-{tid}-{r['type']}@tc",
                dtstart=r["due_date"],
                summary=f"[Disclosure] {r['name']}",
                description=f"{addr} — {r['responsible']}",
                categories="Disclosure",
            ))

        for r in c.execute(
            "SELECT * FROM contingencies WHERE txn=? AND nbp_expires_at IS NOT NULL AND status='active'",
            (tid,),
        ).fetchall():
            r = dict(r)
            events.append(_vevent(
                uid=f"nbp-{tid}-{r['type']}@tc",
                dtstart=r["nbp_expires_at"][:10],
                summary=f"[NBP Expires] {r['name']}",
                description=f"{addr} — buyer must respond or seller may cancel",
                categories="NBP",
                alarm_days=1,
            ))

    short_addr = addr.split(",")[0] if "," in addr else addr
    body = _build_ical(events, cal_name=f"TC: {short_addr}")
    return Response(body, mimetype="text/calendar",
                    headers={"Content-Disposition": f"inline; filename=tc-{tid[:8]}.ics"})


# ── Features Tracker ─────────────────────────────────────────────────────────

@app.route("/api/features")
def list_features():
    """List all features with their dependencies."""
    with db.conn() as c:
        rows = c.execute("SELECT * FROM features ORDER BY category, name").fetchall()
    items = []
    for r in rows:
        item = dict(r)
        item["files"] = json.loads(item.get("files") or "[]")
        item["depends_on"] = json.loads(item.get("depends_on") or "[]")
        items.append(item)
    return jsonify(items)


@app.route("/api/features", methods=["POST"])
def create_feature():
    """Create or update a feature."""
    body = request.json or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    category = body.get("category", "")
    description = body.get("description", "")
    status = body.get("status", "active")
    files = json.dumps(body.get("files", []))
    depends_on = json.dumps(body.get("depends_on", []))

    with db.conn() as c:
        c.execute("""
            INSERT INTO features(name, category, description, status, files, depends_on)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                category=excluded.category,
                description=excluded.description,
                status=excluded.status,
                files=excluded.files,
                depends_on=excluded.depends_on
        """, (name, category, description, status, files, depends_on))
        row = c.execute("SELECT * FROM features WHERE name=?", (name,)).fetchone()
    item = dict(row)
    item["files"] = json.loads(item.get("files") or "[]")
    item["depends_on"] = json.loads(item.get("depends_on") or "[]")
    return jsonify(item), 201


@app.route("/api/features/<int:fid>", methods=["DELETE"])
def delete_feature(fid):
    """Remove a feature from tracking."""
    with db.conn() as c:
        row = c.execute("SELECT * FROM features WHERE id=?", (fid,)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        c.execute("DELETE FROM features WHERE id=?", (fid,))
    return jsonify({"ok": True})


@app.route("/api/features/init", methods=["POST"])
def init_features():
    """Pre-populate features table with known app features."""
    features = [
        {"name": "Transaction CRUD", "category": "core", "files": ["db.py", "web.py", "app.js"], "depends_on": []},
        {"name": "Document Checklist", "category": "docs", "files": ["checklist.py", "web.py"], "depends_on": ["Transaction CRUD"]},
        {"name": "Compliance Gates", "category": "compliance", "files": ["rules/*.yaml", "engine.py"], "depends_on": ["Transaction CRUD"]},
        {"name": "Deadline Tracking", "category": "dates", "files": ["engine.py", "rules/deadlines.yaml"], "depends_on": ["Transaction CRUD"]},
        {"name": "Signature Review", "category": "signatures", "files": ["web.py", "app.js"], "depends_on": ["Document Checklist"]},
        {"name": "Follow-up Pipeline", "category": "integrations", "files": ["integrations.py", "web.py"], "depends_on": ["Signature Review"]},
        {"name": "Outbox", "category": "comms", "files": ["db.py", "web.py"], "depends_on": ["Follow-up Pipeline"]},
        {"name": "Chat Panel", "category": "ai", "files": ["web.py", "app.js"], "depends_on": ["Transaction CRUD"]},
        {"name": "Review Mode", "category": "feedback", "files": ["db.py", "web.py", "app.js"], "depends_on": []},
        {"name": "Contract Annotation", "category": "pdf", "files": ["contract_scanner.py"], "depends_on": ["Document Checklist"]},
        {"name": "Bulk Verification", "category": "docs", "files": ["app.js"], "depends_on": ["Document Checklist"]},
        {"name": "Dashboard", "category": "ui", "files": ["app.js"], "depends_on": ["Transaction CRUD"]},
        {"name": "Contract Review", "category": "pdf", "files": ["web.py", "app.js", "engine.py"], "depends_on": ["Document Checklist", "Contract Annotation"]},
        {"name": "Cloud Usage Tracker", "category": "integrations", "files": ["db.py", "web.py", "app.js", "cloud_guard.py"], "depends_on": ["Transaction CRUD"]},
        {"name": "Cloud Approval Gate", "category": "security", "files": ["web.py", "cloud_guard.py", "app.js"], "depends_on": ["Cloud Usage Tracker"]},
    ]
    count = 0
    with db.conn() as c:
        for f in features:
            c.execute("""
                INSERT OR IGNORE INTO features(name, category, files, depends_on)
                VALUES(?, ?, ?, ?)
            """, (f["name"], f["category"], json.dumps(f["files"]), json.dumps(f["depends_on"])))
            count += 1
    return jsonify({"ok": True, "initialized": count})


# ── Parties Import from Signatures ───────────────────────────────────────────

@app.route("/api/txns/<tid>/signatures/detected-parties")
def detect_parties_from_sigs(tid):
    """Detect party information from signature field names and RPA data."""
    with db.conn() as c:
        # Get signature fields
        sig_rows = c.execute(
            "SELECT DISTINCT field_name, field_type, signer_name, signer_email FROM sig_reviews WHERE txn=?",
            (tid,),
        ).fetchall()

        # Get transaction data (may have RPA-extracted party info)
        txn = db.txn(c, tid)
        txn_data = json.loads((txn or {}).get("data") or "{}")
        rpa_parties = txn_data.get("parties") or {}

    # Role detection patterns
    role_patterns = [
        (r"buyer", "buyer"),
        (r"purchaser", "buyer"),
        (r"tenant", "buyer"),  # lease context
        (r"seller", "seller"),
        (r"vendor", "seller"),
        (r"landlord", "seller"),  # lease context
        (r"listing.?agent", "seller_agent"),
        (r"seller.?agent", "seller_agent"),
        (r"buyer.?agent", "buyer_agent"),
        (r"selling.?agent", "buyer_agent"),
        (r"escrow", "escrow_officer"),
        (r"title", "title_rep"),
    ]

    detected = {}  # role -> {fields: [], names: [], emails: []}
    for row in sig_rows:
        fname = (row["field_name"] or "").lower()
        for pattern, role in role_patterns:
            if re.search(pattern, fname, re.IGNORECASE):
                if role not in detected:
                    detected[role] = {"fields": [], "names": [], "emails": []}
                detected[role]["fields"].append(row["field_name"])
                if row["signer_name"]:
                    detected[role]["names"].append(row["signer_name"])
                if row["signer_email"]:
                    detected[role]["emails"].append(row["signer_email"])
                break

    # Also check RPA-extracted party names
    rpa_role_map = {
        "buyer_name": "buyer",
        "seller_name": "seller",
        "listing_agent": "seller_agent",
        "buyer_agent": "buyer_agent",
        "escrow_officer": "escrow_officer",
    }
    for rpa_key, role in rpa_role_map.items():
        if rpa_parties.get(rpa_key):
            if role not in detected:
                detected[role] = {"fields": [], "names": [], "emails": []}
            detected[role]["names"].append(rpa_parties[rpa_key])

    # Build party suggestions
    suggestions = []
    role_labels = {
        "buyer": "Buyer",
        "seller": "Seller",
        "buyer_agent": "Buyer's Agent",
        "seller_agent": "Listing Agent",
        "escrow_officer": "Escrow Officer",
        "title_rep": "Title Representative",
    }
    for role, data in detected.items():
        # Use actual name if found, otherwise use role label
        names = list(set(data.get("names", [])))
        suggested = names[0] if names else role_labels.get(role, role)
        suggestions.append({
            "role": role,
            "role_label": role_labels.get(role, role),
            "source_fields": list(set(data.get("fields", [])))[:5],
            "suggested_name": suggested,
            "detected_names": names[:3],
            "detected_emails": list(set(data.get("emails", [])))[:3],
        })

    return jsonify({"detected": suggestions})


@app.route("/api/txns/<tid>/parties/import", methods=["POST"])
def import_parties(tid):
    """Import parties from detected signature roles."""
    body = request.json or {}
    parties = body.get("parties", [])
    if not parties:
        return jsonify({"error": "parties list required"}), 400

    imported = []
    with db.conn() as c:
        for p in parties:
            role = p.get("role", "")
            name = p.get("name", "").strip()
            if not role or not name:
                continue
            # Check if party with this role already exists and has real data
            existing = c.execute(
                "SELECT * FROM parties WHERE txn=? AND role=?", (tid, role)
            ).fetchone()
            if existing and "(TBD)" not in existing["name"]:
                continue  # Don't overwrite real party data
            if existing:
                # Update placeholder
                c.execute(
                    "UPDATE parties SET name=? WHERE id=?",
                    (name, existing["id"]),
                )
                imported.append({"role": role, "name": name, "action": "updated"})
            else:
                # Insert new
                c.execute(
                    "INSERT INTO parties(txn, role, name) VALUES(?, ?, ?)",
                    (tid, role, name),
                )
                imported.append({"role": role, "name": name, "action": "created"})
        if imported:
            db.log(c, tid, "parties_imported", f"Imported {len(imported)} parties from signatures")

    return jsonify({"imported": imported})


# ── Closing Plan / Schedule Data ─────────────────────────────────────────────

@app.route("/api/txns/<tid>/closing-plan")
def get_closing_plan(tid):
    """Get all data needed for closing plan modal."""
    with db.conn() as c:
        txn = db.txn(c, tid)
        if not txn:
            return jsonify({"error": "not found"}), 404

        # Deadlines
        deadlines = c.execute(
            "SELECT * FROM deadlines WHERE txn=? ORDER BY due",
            (tid,),
        ).fetchall()

        # Docs
        docs = c.execute(
            "SELECT * FROM docs WHERE txn=? AND status != 'verified' AND status != 'na'"
            " ORDER BY phase, code",
            (tid,),
        ).fetchall()

        # Signatures
        sigs = c.execute(
            "SELECT * FROM sig_reviews WHERE txn=? AND is_filled=0"
            " ORDER BY doc_code, page",
            (tid,),
        ).fetchall()

        # Contingencies
        conts = c.execute(
            "SELECT * FROM contingencies WHERE txn=? AND status='active'"
            " ORDER BY deadline_date",
            (tid,),
        ).fetchall()

        # Gates
        gates_rows = engine.gate_rows(tid)
        blocking = [g for g in gates_rows if g["status"] != "verified"]

    # Calculate days to close
    txn_data = json.loads(txn.get("data") or "{}")
    coe_date = (txn_data.get("dates") or {}).get("close_of_escrow")
    days_to_close = None
    if coe_date:
        try:
            coe = date.fromisoformat(coe_date)
            days_to_close = (coe - date.today()).days
        except Exception:
            pass

    return jsonify({
        "days_to_close": days_to_close,
        "coe_date": coe_date,
        "pending_docs": [dict(d) for d in docs],
        "pending_signatures": [dict(s) for s in sigs],
        "active_contingencies": [dict(c) for c in conts],
        "blocking_gates": blocking,
        "all_deadlines": [dict(d) for d in deadlines],
    })


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        port = int(os.environ.get("PORT", "5001"))
    except (TypeError, ValueError):
        port = 5001
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="127.0.0.1", port=port, debug=debug_mode)
