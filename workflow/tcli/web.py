"""Flask web UI for Transaction Coordinator."""
import json
import os
from datetime import date
from pathlib import Path
from uuid import uuid4

from flask import Flask, jsonify, render_template, request

from . import checklist, db, doc_versions, engine, rules

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
    address = body.get("address", "").strip()
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
