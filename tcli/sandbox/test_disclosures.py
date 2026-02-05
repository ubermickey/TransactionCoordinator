"""Disclosure tracking: add, receive, review, waive, urgency, audit."""
from .fixtures import get, post, create_simple_txn, cleanup, ADDR_DISC


def register(runner):
    tid = None
    disc_ids = {}  # type -> id

    def setup():
        nonlocal tid
        tid, _ = create_simple_txn(ADDR_DISC)

    def teardown():
        if tid:
            cleanup(tid)

    def test_list_empty():
        """Fresh transaction should have no disclosures."""
        _, data = get(f"/api/txns/{tid}/disclosures", expect=200)
        assert "items" in data
        assert "summary" in data
        assert len(data["items"]) == 0
        assert data["summary"]["total"] == 0

    def test_add_tds():
        """Add TDS disclosure."""
        _, d = post(f"/api/txns/{tid}/disclosures", {
            "type": "tds",
            "due_date": "2026-02-08",
            "notes": "seller to complete",
        }, expect=201)
        assert d["type"] == "tds"
        assert d["name"] == "Transfer Disclosure Statement"
        assert d["status"] == "pending"
        assert d["responsible"] == "seller"
        disc_ids["tds"] = d["id"]

    def test_add_spq():
        """Add SPQ disclosure."""
        _, d = post(f"/api/txns/{tid}/disclosures", {
            "type": "spq",
            "due_date": "2026-02-08",
        }, expect=201)
        assert d["type"] == "spq"
        assert d["name"] == "Seller Property Questionnaire"
        disc_ids["spq"] = d["id"]

    def test_add_nhd():
        """Add NHD disclosure."""
        _, d = post(f"/api/txns/{tid}/disclosures", {
            "type": "nhd",
            "due_date": "2026-02-08",
        }, expect=201)
        assert d["type"] == "nhd"
        disc_ids["nhd"] = d["id"]

    def test_add_avid():
        """Add AVID disclosures (listing + buyer)."""
        _, d1 = post(f"/api/txns/{tid}/disclosures", {
            "type": "avid_listing",
            "due_date": "2026-02-08",
        }, expect=201)
        assert d1["responsible"] == "seller_agent"
        disc_ids["avid_listing"] = d1["id"]

        _, d2 = post(f"/api/txns/{tid}/disclosures", {
            "type": "avid_buyer",
            "due_date": "2026-02-08",
        }, expect=201)
        assert d2["responsible"] == "buyer_agent"
        disc_ids["avid_buyer"] = d2["id"]

    def test_add_lead_paint():
        """Add lead-based paint disclosure."""
        _, d = post(f"/api/txns/{tid}/disclosures", {
            "type": "lead_paint",
        }, expect=201)
        assert d["type"] == "lead_paint"
        disc_ids["lead_paint"] = d["id"]

    def test_add_duplicate_blocked():
        """Adding same type should fail (UNIQUE)."""
        code, _ = post(f"/api/txns/{tid}/disclosures", {
            "type": "tds",
        })
        assert code == 409

    def test_add_type_required():
        """Missing type should fail."""
        code, _ = post(f"/api/txns/{tid}/disclosures", {"notes": "test"})
        assert code == 400

    def test_list_with_computed():
        """GET should return days_until_due and urgency."""
        _, data = get(f"/api/txns/{tid}/disclosures", expect=200)
        assert len(data["items"]) == 6
        for item in data["items"]:
            assert "days_until_due" in item
            assert "urgency" in item

    def test_summary_counts():
        """Summary should reflect correct counts."""
        _, data = get(f"/api/txns/{tid}/disclosures", expect=200)
        s = data["summary"]
        assert s["total"] == 6
        assert s["pending"] == 6
        assert s["received"] == 0

    def test_receive_tds():
        """Receive TDS disclosure."""
        did = disc_ids.get("tds")
        if not did:
            return
        _, d = post(f"/api/txns/{tid}/disclosures/{did}/receive", expect=200)
        assert d["status"] == "received"
        assert d["received_date"] is not None

    def test_review_tds():
        """Review received TDS."""
        did = disc_ids.get("tds")
        if not did:
            return
        _, d = post(f"/api/txns/{tid}/disclosures/{did}/review", {
            "reviewer": "Agent Smith",
        }, expect=200)
        assert d["status"] == "reviewed"
        assert d["reviewed_date"] is not None
        assert d["reviewer"] == "Agent Smith"

    def test_waive_lead_paint():
        """Waive lead paint (not pre-1978)."""
        did = disc_ids.get("lead_paint")
        if not did:
            return
        _, d = post(f"/api/txns/{tid}/disclosures/{did}/waive", expect=200)
        assert d["status"] == "waived"

    def test_summary_after_actions():
        """Summary should reflect received, reviewed, waived."""
        _, data = get(f"/api/txns/{tid}/disclosures", expect=200)
        s = data["summary"]
        assert s["reviewed"] >= 1
        assert s["waived"] >= 1

    def test_not_found():
        """Nonexistent disclosure should 404."""
        code, _ = post(f"/api/txns/{tid}/disclosures/99999/receive")
        assert code == 404

    def test_add_other():
        """Add a custom 'other' disclosure."""
        _, d = post(f"/api/txns/{tid}/disclosures", {
            "type": "other",
            "name": "Mello-Roos District Disclosure",
            "due_date": "2026-02-15",
            "notes": "check with title",
        }, expect=201)
        assert d["type"] == "other"

    def test_audit_logged():
        """Disclosure actions should appear in audit."""
        _, audit = get(f"/api/txns/{tid}/audit", expect=200)
        actions = [r["action"] for r in audit]
        assert "disc_added" in actions
        assert "disc_received" in actions
        assert "disc_reviewed" in actions
        assert "disc_waived" in actions

    # Register
    runner.test("disc:setup", setup)
    runner.test("disc:list_empty", test_list_empty)
    runner.test("disc:add_tds", test_add_tds)
    runner.test("disc:add_spq", test_add_spq)
    runner.test("disc:add_nhd", test_add_nhd)
    runner.test("disc:add_avid", test_add_avid)
    runner.test("disc:add_lead_paint", test_add_lead_paint)
    runner.test("disc:add_duplicate", test_add_duplicate_blocked)
    runner.test("disc:add_type_required", test_add_type_required)
    runner.test("disc:list_computed", test_list_with_computed)
    runner.test("disc:summary_counts", test_summary_counts)
    runner.test("disc:receive_tds", test_receive_tds)
    runner.test("disc:review_tds", test_review_tds)
    runner.test("disc:waive_lead_paint", test_waive_lead_paint)
    runner.test("disc:summary_after", test_summary_after_actions)
    runner.test("disc:not_found", test_not_found)
    runner.test("disc:add_other", test_add_other)
    runner.test("disc:audit", test_audit_logged)
    runner.test("disc:teardown", teardown)
