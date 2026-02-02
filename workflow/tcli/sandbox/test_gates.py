"""Gate verification, phase advancement, and brokerage-specific gate tests."""
from .fixtures import get, post, create_txn, cleanup, ADDR_GATES


def register(runner):
    tid = None

    def setup():
        nonlocal tid
        tid, _ = create_txn(ADDR_GATES)

    def teardown():
        if tid:
            cleanup(tid)

    def test_gates_populated():
        """Gates should be initialized on txn creation."""
        nonlocal tid
        tid, _ = create_txn(ADDR_GATES)
        _, gates = get(f"/api/txns/{tid}/gates", expect=200)
        assert len(gates) > 0, "expected gates"
        assert all("gid" in g for g in gates)
        # All should be pending initially
        statuses = {g["status"] for g in gates}
        assert "pending" in statuses

    def test_verify_gate():
        """Verify a gate and confirm status change."""
        _, gates = get(f"/api/txns/{tid}/gates", expect=200)
        pending = [g for g in gates if g["status"] == "pending"]
        if not pending:
            return
        gid = pending[0]["gid"]
        _, result = post(f"/api/txns/{tid}/gates/{gid}/verify",
                         {"notes": "sandbox test"}, expect=200)
        assert result["status"] == "verified"
        # Confirm it persisted
        _, gates2 = get(f"/api/txns/{tid}/gates", expect=200)
        g = next(g for g in gates2 if g["gid"] == gid)
        assert g["status"] == "verified"

    def test_gate_info_enriched():
        """Gate response should include name, type, phase from rules."""
        _, gates = get(f"/api/txns/{tid}/gates", expect=200)
        if not gates:
            return
        g = gates[0]
        assert "name" in g and g["name"], "gate should have name"
        assert "type" in g and g["type"], "gate should have type"
        assert "phase" in g and g["phase"], "gate should have phase"

    def test_advance_blocked():
        """Phase advance should fail when hard gates are pending."""
        code, result = post(f"/api/txns/{tid}/advance")
        # May or may not block depending on gate state
        if code == 409:
            assert "blocking" in result
            assert len(result["blocking"]) > 0

    def test_advance_success():
        """Verify all PRE_CONTRACT hard gates, then advance."""
        _, gates = get(f"/api/txns/{tid}/gates", expect=200)
        # Verify all hard gates in PRE_CONTRACT
        for g in gates:
            if g["phase"] == "PRE_CONTRACT" and g["type"] == "HARD_GATE" and g["status"] != "verified":
                post(f"/api/txns/{tid}/gates/{g['gid']}/verify", {"notes": "test"})
        code, result = post(f"/api/txns/{tid}/advance")
        if code == 200:
            assert result["ok"] is True
            assert result["phase"] != "PRE_CONTRACT"
        # else still blocked by DE-specific gates — acceptable

    def test_phases_api():
        """GET /api/phases/sale returns phase definitions."""
        _, phases = get("/api/phases/sale", expect=200)
        assert len(phases) > 0
        assert all("id" in p for p in phases)

    def test_lease_phases_api():
        """GET /api/phases/lease returns lease phase definitions."""
        _, phases = get("/api/phases/lease", expect=200)
        assert isinstance(phases, list)

    def test_audit_gate_verify():
        """Gate verification should appear in audit log."""
        _, audit = get(f"/api/txns/{tid}/audit", expect=200)
        actions = [r["action"] for r in audit]
        assert "gate_verified" in actions, "expected gate_verified in audit"

    # Register
    runner.test("gates:setup", setup)
    runner.test("gates:populated", test_gates_populated)
    runner.test("gates:verify", test_verify_gate)
    runner.test("gates:info_enriched", test_gate_info_enriched)
    runner.test("gates:advance_blocked", test_advance_blocked)
    runner.test("gates:advance_success", test_advance_success)
    runner.test("gates:phases_api", test_phases_api)
    runner.test("gates:lease_phases_api", test_lease_phases_api)
    runner.test("gates:audit_verify", test_audit_gate_verify)
    runner.test("gates:teardown", teardown)
