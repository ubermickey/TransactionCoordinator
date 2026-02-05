"""Flask web UI for Transaction Coordinator."""
import json
import os
import re
import urllib.request
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import yaml
from flask import Flask, Response, jsonify, render_template, request, send_from_directory

from . import checklist, contract_scanner, db, doc_versions, engine, integrations, rules
from .engine import CONT_GATE

app = Flask(__name__)


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
    if acceptance_date:
        txn_data["dates"]["acceptance"] = acceptance_date
        # Default COE 30 days from acceptance for CA transactions
        coe = (date.fromisoformat(acceptance_date) + timedelta(days=30)).isoformat()
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
    if acceptance_date:
        try:
            anchor = date.fromisoformat(acceptance_date)
            engine.calc_deadlines(tid, anchor, txn_data)
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
    ok, blocking = engine.can_advance(tid)
    if not ok:
        return jsonify({"ok": False, "blocking": blocking}), 409
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


def _get_manifest_sigs() -> dict:
    """Load and cache all manifest signature fields. Returns {lower_name: [{fields...}]}."""
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
                if f.get("category") in ("signature", "signature_area")
            ]
            if not sig_fields:
                continue
            manifest_name = m.get("document_name", mfile.stem)
            _manifest_cache[manifest_name.lower()] = {
                "folder": folder.name,
                "filename": mfile.stem,
                "manifest_name": manifest_name,
                "sig_fields": sig_fields,
            }
    return _manifest_cache


def _populate_sig_reviews(c, tid):
    """Lazy-load signature fields from manifests into sig_reviews for a txn."""
    # Skip if already scanned this session
    if tid in _sig_populated_txns:
        return
    _sig_populated_txns.add(tid)

    docs = c.execute("SELECT code, name FROM docs WHERE txn=?", (tid,)).fetchall()
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

        # Match against cached manifests
        for mname_lower, mdata in manifests.items():
            mname = mdata["manifest_name"]
            if not (code.lower() in mname_lower
                    or mname_lower in doc["name"].lower()
                    or doc["name"].lower() in mname_lower):
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
            break  # matched this doc, move to next


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
    return jsonify({"email_sandbox": integrations.EMAIL_SANDBOX})


@app.route("/api/docusign-status")
def docusign_status():
    """Check DocuSign configuration status and get setup instructions."""
    return jsonify(integrations.docusign_status())


@app.route("/api/txns/<tid>/signatures/<int:sig_id>/send", methods=["POST"])
def send_signature(tid, sig_id):
    """Send a signature field for signing via DocuSign (or sandbox mock)."""
    body = request.json or {}
    email_addr = body.get("email", "").strip()
    name = body.get("name", "").strip()
    provider = body.get("provider", "docusign")
    if not email_addr or not name:
        return jsonify({"error": "email and name required"}), 400

    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM sig_reviews WHERE id=? AND txn=?", (sig_id, tid)
        ).fetchone()
        if not row:
            return jsonify({"error": "signature field not found"}), 404
        result = integrations.send_for_signature(
            c, tid, sig_id, email_addr, name, provider
        )
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
    name = CONT_NAMES.get(ctype, body.get("name", ctype.replace("_", " ").title() + " Contingency"))

    # If no deadline given, compute from acceptance date
    if not deadline:
        with db.conn() as c:
            t = db.txn(c, tid)
            if not t:
                return jsonify({"error": "txn not found"}), 404
            data = json.loads(t.get("data") or "{}")
            acceptance = (data.get("dates") or {}).get("acceptance")
            if acceptance:
                from datetime import timedelta
                deadline = (date.fromisoformat(acceptance) + timedelta(days=int(days))).isoformat()

    with db.conn() as c:
        try:
            c.execute(
                "INSERT INTO contingencies(txn,type,name,default_days,deadline_date,notes)"
                " VALUES(?,?,?,?,?,?)",
                (tid, ctype, name, days, deadline, notes),
            )
        except Exception:
            return jsonify({"error": f"contingency '{ctype}' already exists for this transaction"}), 409
        cid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.log(c, tid, "cont_added", f"{name} ({days}d, due {deadline})")
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
    category = request.args.get("category")
    page = request.args.get("page", type=int)
    fields = doc_versions.field_locations(folder, filename, category, page)
    return jsonify(fields)


@app.route("/api/doc-packages/<path:folder>/<filename>/manifest")
def doc_manifest(folder, filename):
    """Get the full manifest for a document."""
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
    car_dir = doc_versions.CAR_DIR / folder
    if not car_dir.exists():
        return jsonify({"error": "folder not found"}), 404
    pdf_path = car_dir / filename
    if not pdf_path.exists():
        return jsonify({"error": "file not found"}), 404
    return send_from_directory(str(car_dir), filename, mimetype="application/pdf")


@app.route("/api/field-annotations/<path:folder>/<filename>")
def get_field_annotations(folder, filename):
    """Get all field annotations for a document."""
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


@app.route("/api/contracts/<int:cid>/fields/<int:fid>/crop")
def field_crop_image(cid, fid):
    """Serve a cropped PNG screenshot of a field from the actual PDF."""
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
    png = contract_scanner.render_field_crop(source, field["page"], bbox)
    if not png:
        return jsonify({"error": "crop failed"}), 500

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


# ── Document Upload ───────────────────────────────────────────────────────────

@app.route("/api/txns/<tid>/upload", methods=["POST"])
def upload_document(tid):
    """Upload a PDF document to a transaction. Stores in CAR Contract Packages/<tid>/."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename or not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are accepted"}), 400

    with db.conn() as c:
        t = db.txn(c, tid)
        if not t:
            return jsonify({"error": "transaction not found"}), 404

    # Store in a transaction-specific folder
    upload_dir = doc_versions.CAR_DIR / f"uploads_{tid}"
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r'[^\w\-. ()]', '_', f.filename)
    dest = upload_dir / safe_name
    f.save(str(dest))

    # Try to auto-scan the document for fields
    result = {"filename": safe_name, "folder": f"uploads_{tid}", "path": str(dest)}
    try:
        scan_result = contract_scanner.scan_pdf(str(dest))
        if scan_result:
            # Store in contracts table
            with db.conn() as c:
                c.execute(
                    "INSERT INTO contracts(folder, filename, scenario) VALUES(?,?,?)",
                    (f"uploads_{tid}", safe_name, tid),
                )
                cid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
                fields = scan_result.get("fields", [])
                for idx, fld in enumerate(fields):
                    c.execute(
                        "INSERT INTO contract_fields(contract_id, field_idx, label, category,"
                        " page, bbox, is_filled, fill_confidence, context, ul_bbox)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (cid, idx, fld.get("label", ""), fld.get("category", ""),
                         fld.get("page", 0), json.dumps(fld.get("bbox", [])),
                         1 if fld.get("is_filled") else 0,
                         fld.get("fill_confidence", 0),
                         fld.get("context", ""),
                         json.dumps(fld.get("ul_bbox", []))),
                    )
                db.log(c, tid, "doc_uploaded", f"{safe_name}: {len(fields)} fields detected")
            result["contract_id"] = cid
            result["fields_detected"] = len(fields)
            result["fields"] = fields
    except Exception as e:
        result["scan_error"] = str(e)

    # Try to match to a checklist doc code and auto-receive
    matched_code = _match_upload_to_doc(tid, safe_name, folder=f"uploads_{tid}")
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


def _match_upload_to_doc(tid: str, filename: str, folder: str = "") -> str | None:
    """Try to match an uploaded filename to a checklist doc code and mark received."""
    name_lower = filename.lower()
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
        if pattern in name_lower:
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
                db.log(c, tid, "doc_received", f"{matched} auto-matched from upload: {filename}")
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

    # ── "No contingency" flags ──
    for i, line in enumerate(lines):
        lu = line.upper()
        if "NO LOAN CONTINGENCY" in lu:
            extracted["no_loan_contingency"] = True
        if "NO APPRAISAL CONTINGENCY" in lu:
            extracted["no_appraisal_contingency"] = True

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

    # Set defaults for anything not found
    extracted.setdefault("investigation_days", 17)
    extracted.setdefault("appraisal_days", 17)
    extracted.setdefault("loan_days", 21)
    extracted.setdefault("coe_days", 30)

    return extracted


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
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not set. Add it to your .env file."}), 500

    body = request.json or {}
    message = body.get("message", "").strip()
    if not message:
        return jsonify({"error": "message required"}), 400

    tid = body.get("txn_id")
    history = body.get("history", [])

    # Build transaction context
    context_parts = []
    if tid:
        with db.conn() as c:
            row = db.txn(c, tid)
            if row:
                t = _txn_dict(row)
                t["doc_stats"] = _doc_stats(c, tid)
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
    import urllib.request
    import urllib.error

    api_body = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1024,
        "system": system_prompt,
        "messages": api_messages,
    })

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=api_body.encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        reply = ""
        for block in result.get("content", []):
            if block.get("type") == "text":
                reply += block.get("text", "")
        return jsonify({"reply": reply or "No response generated."})
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        return jsonify({"error": f"Claude API error ({e.code}): {error_body[:200]}"}), 502
    except Exception as e:
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


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=True)
