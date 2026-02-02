"""Flask web UI for Transaction Coordinator."""
import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import yaml
from flask import Flask, jsonify, render_template, request

from . import checklist, db, doc_versions, engine, integrations, rules
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

    tid = uuid4().hex[:8]
    city = address.split(",")[1].strip() if "," in address else ""
    juris = rules.resolve(city)
    initial_phase = engine.default_phase(txn_type)

    with db.conn() as c:
        c.execute(
            "INSERT INTO txns(id,address,phase,jurisdictions,txn_type,party_role,brokerage,props) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (tid, address, initial_phase, json.dumps(juris), txn_type, party_role, brokerage, "{}"),
        )
        db.log(c, tid, "created", f"{address} type={txn_type} role={party_role} brokerage={brokerage}")

    engine.init_gates(tid, brokerage=brokerage)

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

    with db.conn() as c:
        t = _txn_dict(db.txn(c, tid))
    return jsonify(t), 201


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
    for d in rows:
        if d.get("due"):
            d["days_remaining"] = (date.fromisoformat(d["due"]) - today).days
        else:
            d["days_remaining"] = None
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

def _populate_sig_reviews(c, tid):
    """Lazy-load signature fields from manifests into sig_reviews for a txn."""
    docs = c.execute("SELECT code, name FROM docs WHERE txn=?", (tid,)).fetchall()
    if not docs:
        return

    for doc in docs:
        code = doc["code"]
        # Check if already populated for this doc
        existing = c.execute(
            "SELECT 1 FROM sig_reviews WHERE txn=? AND doc_code=? LIMIT 1",
            (tid, code),
        ).fetchone()
        if existing:
            continue

        # Try to find a matching manifest
        # doc code format varies — scan all manifest folders
        manifest_dir = doc_versions.MANIFEST_DIR
        if not manifest_dir.exists():
            continue

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

                # Match manifest to doc by code or name similarity
                manifest_name = m.get("document_name", mfile.stem)
                if not (code.lower() in manifest_name.lower()
                        or manifest_name.lower() in doc["name"].lower()
                        or doc["name"].lower() in manifest_name.lower()):
                    continue

                for sf in sig_fields:
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
                                tid, code, folder.name, mfile.stem,
                                field_name, field_type,
                                sf.get("page", 0), json.dumps(bbox),
                                filled, "pending", "auto",
                            ),
                        )
                    except Exception:
                        pass
                break  # matched this doc, move to next


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
    return jsonify({"sandbox": integrations.SANDBOX})


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
    """Sandbox only: simulate a signer completing the signature."""
    if not integrations.SANDBOX:
        return jsonify({"error": "simulate only available in sandbox mode"}), 403

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


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=True)
