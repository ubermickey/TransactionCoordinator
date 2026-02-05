"""Contingency tracker: auto-population, manual add, remove, NBP, waive."""
from .fixtures import get, post, create_simple_txn, cleanup, ADDR_CONT


def register(runner):
    tid = None
    cont_ids = {}  # type -> id

    def setup():
        nonlocal tid
        # Simple txn (no brokerage) so no auto-populated contingencies
        tid, _ = create_simple_txn(ADDR_CONT)

    def teardown():
        if tid:
            cleanup(tid)

    def test_list_empty():
        """Fresh simple transaction should have no contingencies."""
        _, data = get(f"/api/txns/{tid}/contingencies", expect=200)
        assert "items" in data
        assert "summary" in data
        assert isinstance(data["items"], list)
        assert data["summary"]["total"] == 0

    def test_manual_add_investigation():
        """Add investigation contingency manually."""
        _, c = post(f"/api/txns/{tid}/contingencies", {
            "type": "investigation",
            "days": 17,
            "deadline_date": "2026-02-18",
            "notes": "sandbox test",
        }, expect=201)
        assert c["type"] == "investigation"
        assert c["status"] == "active"
        assert c["default_days"] == 17
        assert c["deadline_date"] == "2026-02-18"
        cont_ids["investigation"] = c["id"]

    def test_add_duplicate_blocked():
        """Adding same type again should fail (UNIQUE constraint)."""
        code, _ = post(f"/api/txns/{tid}/contingencies", {
            "type": "investigation", "days": 17,
        })
        assert code == 409

    def test_add_appraisal():
        """Add appraisal contingency."""
        _, c = post(f"/api/txns/{tid}/contingencies", {
            "type": "appraisal",
            "days": 17,
            "deadline_date": "2026-02-18",
        }, expect=201)
        assert c["type"] == "appraisal"
        assert c["name"] == "Appraisal Contingency"
        cont_ids["appraisal"] = c["id"]

    def test_add_loan():
        """Add loan contingency with custom days."""
        _, c = post(f"/api/txns/{tid}/contingencies", {
            "type": "loan",
            "days": 21,
            "deadline_date": "2026-02-22",
        }, expect=201)
        assert c["type"] == "loan"
        assert c["default_days"] == 21
        cont_ids["loan"] = c["id"]

    def test_add_type_required():
        """Missing type should fail."""
        code, _ = post(f"/api/txns/{tid}/contingencies", {"days": 17})
        assert code == 400

    def test_list_with_computed():
        """GET should return days_remaining and urgency."""
        _, data = get(f"/api/txns/{tid}/contingencies", expect=200)
        assert len(data["items"]) == 3
        for item in data["items"]:
            assert "days_remaining" in item
            assert "urgency" in item
            assert item["urgency"] in ("ok", "soon", "urgent", "overdue")
            assert "related_gate" in item
            assert "related_deadline" in item

    def test_related_links():
        """Contingencies should link to correct gates and deadlines."""
        _, data = get(f"/api/txns/{tid}/contingencies", expect=200)
        by_type = {c["type"]: c for c in data["items"]}
        if "investigation" in by_type:
            assert by_type["investigation"]["related_gate"] == "GATE-022"
            assert by_type["investigation"]["related_deadline"] == "DL-010"
        if "appraisal" in by_type:
            assert by_type["appraisal"]["related_gate"] == "GATE-031"
        if "loan" in by_type:
            assert by_type["loan"]["related_gate"] == "GATE-041"

    def test_summary_counts():
        """Summary should reflect correct counts."""
        _, data = get(f"/api/txns/{tid}/contingencies", expect=200)
        s = data["summary"]
        assert s["total"] == 3
        assert s["active"] == 3
        assert s["removed"] == 0

    def test_remove_investigation():
        """Remove investigation (CR-1 signed) and verify gate auto-verified."""
        cid = cont_ids.get("investigation")
        if not cid:
            return
        _, c = post(f"/api/txns/{tid}/contingencies/{cid}/remove", expect=200)
        assert c["status"] == "removed"
        assert c["removed_at"] is not None

        # Gate GATE-022 should be auto-verified
        _, gates = get(f"/api/txns/{tid}/gates", expect=200)
        gate_022 = next((g for g in gates if g["gid"] == "GATE-022"), None)
        if gate_022:
            assert gate_022["status"] == "verified", \
                "GATE-022 should be auto-verified on investigation removal"

    def test_remove_already_removed():
        """Removing an already-removed contingency is idempotent."""
        cid = cont_ids.get("investigation")
        if not cid:
            return
        _, c = post(f"/api/txns/{tid}/contingencies/{cid}/remove", expect=200)
        assert c["status"] == "removed"

    def test_nbp_on_active():
        """Issue NBP on an active contingency."""
        cid = cont_ids.get("appraisal")
        if not cid:
            return
        _, c = post(f"/api/txns/{tid}/contingencies/{cid}/nbp", expect=200)
        assert c["nbp_sent_at"] is not None
        assert c["nbp_expires_at"] is not None

    def test_nbp_on_removed_blocked():
        """NBP on a removed contingency should fail."""
        cid = cont_ids.get("investigation")
        if not cid:
            return
        code, _ = post(f"/api/txns/{tid}/contingencies/{cid}/nbp")
        assert code == 400

    def test_nbp_days_remaining():
        """After NBP, nbp_days_remaining should be computed."""
        _, data = get(f"/api/txns/{tid}/contingencies", expect=200)
        nbp_items = [c for c in data["items"] if c.get("nbp_sent_at")]
        assert len(nbp_items) > 0, "should have at least one NBP item"
        for item in nbp_items:
            assert item["nbp_days_remaining"] is not None

    def test_waive():
        """Waive loan contingency and verify gate auto-verified."""
        cid = cont_ids.get("loan")
        if not cid:
            return
        _, c = post(f"/api/txns/{tid}/contingencies/{cid}/waive", expect=200)
        assert c["status"] == "waived"
        assert c["waived_at"] is not None

        # Gate GATE-041 should be auto-verified
        _, gates = get(f"/api/txns/{tid}/gates", expect=200)
        gate_041 = next((g for g in gates if g["gid"] == "GATE-041"), None)
        if gate_041:
            assert gate_041["status"] == "verified"

    def test_summary_after_actions():
        """Summary should reflect removals and waivers."""
        _, data = get(f"/api/txns/{tid}/contingencies", expect=200)
        s = data["summary"]
        assert s["removed"] >= 1
        assert s["waived"] >= 1

    def test_add_other_type():
        """Add a custom 'other' contingency."""
        _, c = post(f"/api/txns/{tid}/contingencies", {
            "type": "other",
            "name": "Special Pool Inspection",
            "days": 10,
            "deadline_date": "2026-02-11",
            "notes": "pool + spa inspection",
        }, expect=201)
        assert c["type"] == "other"
        assert c["name"] == "Special Pool Inspection"
        assert c["notes"] == "pool + spa inspection"

    def test_add_hoa():
        """Add HOA contingency."""
        _, c = post(f"/api/txns/{tid}/contingencies", {
            "type": "hoa",
            "days": 5,
            "deadline_date": "2026-02-06",
        }, expect=201)
        assert c["type"] == "hoa"
        assert c["name"] == "HOA Document Review"

    def test_not_found():
        """Nonexistent contingency returns 404."""
        code, _ = post(f"/api/txns/{tid}/contingencies/99999/remove")
        assert code == 404

    def test_wrong_txn_404():
        """Contingency from different txn returns 404."""
        cid = cont_ids.get("appraisal")
        if not cid:
            return
        code, _ = post(f"/api/txns/nonexistent/contingencies/{cid}/remove")
        assert code == 404

    def test_audit_logged():
        """Contingency actions should appear in audit."""
        _, audit = get(f"/api/txns/{tid}/audit", expect=200)
        actions = [r["action"] for r in audit]
        assert "cont_added" in actions
        assert "cont_removed" in actions
        assert "cont_nbp" in actions
        assert "cont_waived" in actions

    # Register all tests
    runner.test("cont:setup", setup)
    runner.test("cont:list_empty", test_list_empty)
    runner.test("cont:add_investigation", test_manual_add_investigation)
    runner.test("cont:add_duplicate", test_add_duplicate_blocked)
    runner.test("cont:add_appraisal", test_add_appraisal)
    runner.test("cont:add_loan", test_add_loan)
    runner.test("cont:add_type_required", test_add_type_required)
    runner.test("cont:list_computed", test_list_with_computed)
    runner.test("cont:related_links", test_related_links)
    runner.test("cont:summary_counts", test_summary_counts)
    runner.test("cont:remove_investigation", test_remove_investigation)
    runner.test("cont:remove_idempotent", test_remove_already_removed)
    runner.test("cont:nbp_on_active", test_nbp_on_active)
    runner.test("cont:nbp_on_removed", test_nbp_on_removed_blocked)
    runner.test("cont:nbp_days_remaining", test_nbp_days_remaining)
    runner.test("cont:waive", test_waive)
    runner.test("cont:summary_after_actions", test_summary_after_actions)
    runner.test("cont:add_other", test_add_other_type)
    runner.test("cont:add_hoa", test_add_hoa)
    runner.test("cont:not_found_404", test_not_found)
    runner.test("cont:wrong_txn_404", test_wrong_txn_404)
    runner.test("cont:audit_logged", test_audit_logged)
    runner.test("cont:teardown", teardown)
