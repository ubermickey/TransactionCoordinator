"""Core CRUD tests: transactions, documents, phases, properties, audit."""
from .fixtures import get, post, delete, create_txn, cleanup, ADDR_CORE


def register(runner):
    tid = None

    def setup():
        nonlocal tid
        tid, _ = create_txn(ADDR_CORE)

    def teardown():
        if tid:
            cleanup(tid)

    def test_create_txn():
        """Create a transaction and verify fields."""
        nonlocal tid
        tid, data = create_txn(ADDR_CORE + " (create)")
        assert data["address"].startswith("1 Core Test Dr")
        assert data["phase"] == "PRE_CONTRACT"
        assert data["txn_type"] == "sale"
        assert data["party_role"] == "listing"
        cleanup(tid)
        tid, _ = create_txn(ADDR_CORE)

    def test_list_txns():
        """GET /api/txns returns at least our test txn."""
        _, txns = get("/api/txns", expect=200)
        ids = [t["id"] for t in txns]
        assert tid in ids, f"txn {tid} not in list"

    def test_get_txn():
        """GET /api/txns/<tid> returns correct data."""
        _, data = get(f"/api/txns/{tid}", expect=200)
        assert data["id"] == tid
        assert "gate_count" in data
        assert "doc_stats" in data

    def test_get_txn_404():
        """GET nonexistent txn returns 404."""
        code, _ = get("/api/txns/nonexistent")
        assert code == 404

    def test_docs_populated():
        """Brokerage transaction should have docs populated."""
        _, docs = get(f"/api/txns/{tid}/docs", expect=200)
        assert len(docs) > 0, "expected docs from brokerage checklist"
        assert all(d["status"] == "required" for d in docs)

    def test_doc_receive_verify():
        """Receive then verify a document."""
        _, docs = get(f"/api/txns/{tid}/docs", expect=200)
        if not docs:
            return
        code = docs[0]["code"]
        _, d = post(f"/api/txns/{tid}/docs/{code}/receive", expect=200)
        assert d["status"] == "received"
        _, d = post(f"/api/txns/{tid}/docs/{code}/verify", expect=200)
        assert d["status"] == "verified"

    def test_doc_na():
        """Mark a doc as N/A."""
        _, docs = get(f"/api/txns/{tid}/docs", expect=200)
        if len(docs) < 2:
            return
        code = docs[1]["code"]
        _, d = post(f"/api/txns/{tid}/docs/{code}/na", {"note": "not applicable"}, expect=200)
        assert d["status"] == "na"

    def test_props_flag():
        """Set a property flag and verify re-resolve."""
        _, result = post(f"/api/txns/{tid}/props", {"flag": "is_condo", "value": True}, expect=200)
        assert result["props"]["is_condo"] is True

    def test_deadlines():
        """GET /api/txns/<tid>/deadlines returns list."""
        _, rows = get(f"/api/txns/{tid}/deadlines", expect=200)
        assert isinstance(rows, list)

    def test_audit_log():
        """Audit should have at least the creation entry."""
        _, rows = get(f"/api/txns/{tid}/audit", expect=200)
        assert len(rows) > 0
        actions = [r["action"] for r in rows]
        assert "created" in actions

    def test_dashboard():
        """GET /api/dashboard returns health data for all transactions."""
        _, items = get("/api/dashboard", expect=200)
        assert isinstance(items, list)
        mine = next((d for d in items if d["id"] == tid), None)
        assert mine is not None, "our txn should be in dashboard"
        assert "health" in mine
        assert mine["health"] in ("green", "yellow", "red")
        assert "doc_stats" in mine
        assert "overdue" in mine

    def test_notes_save():
        """Save and retrieve transaction notes."""
        _, res = post(f"/api/txns/{tid}/notes", {"notes": "Buyer wants quick close"}, expect=200)
        assert res["ok"] is True
        _, data = get(f"/api/txns/{tid}/notes", expect=200)
        assert data["notes"] == "Buyer wants quick close"

    def test_notes_empty():
        """Save empty notes."""
        _, res = post(f"/api/txns/{tid}/notes", {"notes": ""}, expect=200)
        assert res["ok"] is True

    def test_bulk_receive():
        """Bulk receive all required documents."""
        _, docs_before = get(f"/api/txns/{tid}/docs", expect=200)
        required = [d for d in docs_before if d["status"] == "required"]
        _, res = post(f"/api/txns/{tid}/docs/bulk-receive", expect=200)
        assert res["updated"] == len(required)
        # All should now be received
        _, docs_after = get(f"/api/txns/{tid}/docs", expect=200)
        still_required = [d for d in docs_after if d["status"] == "required"]
        assert len(still_required) == 0

    def test_bulk_verify():
        """Bulk verify all received documents."""
        _, docs_before = get(f"/api/txns/{tid}/docs", expect=200)
        received = [d for d in docs_before if d["status"] == "received"]
        _, res = post(f"/api/txns/{tid}/docs/bulk-verify", expect=200)
        assert res["updated"] == len(received)

    def test_bulk_empty():
        """Bulk actions on empty sets return 0."""
        _, res = post(f"/api/txns/{tid}/docs/bulk-receive", expect=200)
        assert res["updated"] == 0
        _, res = post(f"/api/txns/{tid}/docs/bulk-verify", expect=200)
        assert res["updated"] == 0

    def test_delete_txn():
        """Delete transaction and verify 404."""
        _, _ = delete(f"/api/txns/{tid}", expect=200)
        code, _ = get(f"/api/txns/{tid}")
        assert code == 404

    # Register
    runner.test("core:create_txn", test_create_txn)
    runner.test("core:list_txns", test_list_txns)
    runner.test("core:get_txn", test_get_txn)
    runner.test("core:get_txn_404", test_get_txn_404)
    runner.test("core:docs_populated", test_docs_populated)
    runner.test("core:doc_receive_verify", test_doc_receive_verify)
    runner.test("core:doc_na", test_doc_na)
    runner.test("core:props_flag", test_props_flag)
    runner.test("core:deadlines", test_deadlines)
    runner.test("core:audit_log", test_audit_log)
    runner.test("core:dashboard", test_dashboard)
    runner.test("core:notes_save", test_notes_save)
    runner.test("core:notes_empty", test_notes_empty)
    runner.test("core:bulk_receive", test_bulk_receive)
    runner.test("core:bulk_verify", test_bulk_verify)
    runner.test("core:bulk_empty", test_bulk_empty)
    runner.test("core:delete_txn", test_delete_txn)
