"""Signature review, manual add/delete, DocuSign/Email sandbox, reminders."""
from .fixtures import get, post, delete, create_simple_txn, cleanup, ADDR_SIGS


def register(runner):
    tid = None
    manual_sig_id = None
    doc_code = "RPA"  # fallback

    def setup():
        nonlocal tid, doc_code
        # Use simple txn (no brokerage) to avoid slow manifest scanning
        tid, _ = create_simple_txn(ADDR_SIGS)
        # Manually add a doc so we have something to attach signatures to
        # (no brokerage = no auto-populated docs)

    def teardown():
        if tid:
            cleanup(tid)

    def test_signatures_list():
        """GET signatures returns items and summary."""
        _, data = get(f"/api/txns/{tid}/signatures", expect=200)
        assert "items" in data
        assert "summary" in data
        s = data["summary"]
        assert "total" in s and "filled" in s and "empty" in s

    def test_manual_add():
        """Add a manual signature field."""
        nonlocal manual_sig_id
        _, sig = post(f"/api/txns/{tid}/signatures/add", {
            "doc_code": doc_code,
            "field_name": "Sandbox Test Signature",
            "field_type": "signature",
            "page": 1,
            "note": "added by sandbox test",
        }, expect=201)
        assert sig["field_name"] == "Sandbox Test Signature"
        assert sig["source"] == "manual"
        assert sig["review_status"] == "manual"
        manual_sig_id = sig["id"]

    def test_manual_add_initials():
        """Add an initials field."""
        _, sig = post(f"/api/txns/{tid}/signatures/add", {
            "doc_code": doc_code,
            "field_name": "Buyer Initials p3",
            "field_type": "initials",
            "page": 3,
        }, expect=201)
        assert sig["field_type"] == "initials"

    def test_manual_add_validation():
        """Adding without doc_code or field_name should fail."""
        code, _ = post(f"/api/txns/{tid}/signatures/add", {
            "doc_code": "", "field_name": "",
        })
        assert code == 400

    def test_bad_field_type():
        """Invalid field_type should fail."""
        code, _ = post(f"/api/txns/{tid}/signatures/add", {
            "doc_code": doc_code, "field_name": "Test", "field_type": "stamp",
        })
        assert code == 400

    def test_review_signature():
        """Review a signature field."""
        if not manual_sig_id:
            return
        _, result = post(f"/api/txns/{tid}/signatures/{manual_sig_id}/review", {
            "status": "reviewed", "note": "looks good",
        }, expect=200)
        assert result["review_status"] == "reviewed"

    def test_flag_signature():
        """Flag a signature field."""
        if not manual_sig_id:
            return
        _, result = post(f"/api/txns/{tid}/signatures/{manual_sig_id}/review", {
            "status": "flagged", "note": "missing date",
        }, expect=200)
        assert result["review_status"] == "flagged"

    def test_review_bad_status():
        """Review with invalid status should fail."""
        if not manual_sig_id:
            return
        code, _ = post(f"/api/txns/{tid}/signatures/{manual_sig_id}/review", {
            "status": "invalid",
        })
        assert code == 400

    def test_send_for_signing():
        """Send signature via DocuSign sandbox."""
        if not manual_sig_id:
            return
        _, env = post(f"/api/txns/{tid}/signatures/{manual_sig_id}/send", {
            "email": "buyer@example.com",
            "name": "Test Buyer",
            "provider": "docusign",
        }, expect=201)
        assert env["status"] == "sent"
        assert "mock-" in env["envelope_id"]
        assert env["recipient_email"] == "buyer@example.com"

    def test_send_missing_email():
        """Send without email should fail."""
        if not manual_sig_id:
            return
        code, _ = post(f"/api/txns/{tid}/signatures/{manual_sig_id}/send", {
            "email": "", "name": "",
        })
        assert code == 400

    def test_simulate_sign():
        """Simulate signing in sandbox mode."""
        if not manual_sig_id:
            return
        _, result = post(f"/api/txns/{tid}/signatures/{manual_sig_id}/simulate", expect=200)
        assert result["status"] == "signed"
        assert result["signed_at"] is not None

    def test_signed_field_is_filled():
        """After simulate, the signature field should be marked filled."""
        if not manual_sig_id:
            return
        _, data = get(f"/api/txns/{tid}/signatures", expect=200)
        field = next((s for s in data["items"] if s["id"] == manual_sig_id), None)
        if field:
            assert field["is_filled"] == 1

    def test_reminder():
        """Send a reminder for an unsigned field."""
        # Create a new unsigned sig
        _, sig = post(f"/api/txns/{tid}/signatures/add", {
            "doc_code": doc_code,
            "field_name": "Reminder Test Field",
            "field_type": "initials",
            "page": 5,
        }, expect=201)
        sid = sig["id"]
        # Send it so signer_email is set
        post(f"/api/txns/{tid}/signatures/{sid}/send", {
            "email": "signer@example.com", "name": "Signer",
        })
        _, result = post(f"/api/txns/{tid}/signatures/{sid}/remind", expect=200)
        assert result["reminder_count"] >= 1

    def test_outbox():
        """Outbox should contain sandbox emails."""
        _, items = get(f"/api/txns/{tid}/outbox", expect=200)
        assert len(items) > 0, "expected outbox entries from send/remind"
        assert all(i["status"] == "sandbox" for i in items)

    def test_envelopes():
        """Envelope tracking should have records."""
        _, items = get(f"/api/txns/{tid}/envelopes", expect=200)
        assert len(items) > 0

    def test_delete_manual():
        """Can delete manual signature fields."""
        if not manual_sig_id:
            return
        _, result = delete(f"/api/txns/{tid}/signatures/{manual_sig_id}", expect=200)
        assert result["ok"] is True

    def test_delete_nonexistent():
        """Delete nonexistent sig should 404."""
        code, _ = delete(f"/api/txns/{tid}/signatures/99999")
        assert code == 404

    def test_sandbox_status():
        """Sandbox status endpoint should confirm sandbox mode."""
        _, data = get("/api/sandbox-status", expect=200)
        assert data["sandbox"] is True

    # Register all tests
    runner.test("sigs:setup", setup)
    runner.test("sigs:list", test_signatures_list)
    runner.test("sigs:manual_add", test_manual_add)
    runner.test("sigs:manual_add_initials", test_manual_add_initials)
    runner.test("sigs:manual_add_validation", test_manual_add_validation)
    runner.test("sigs:bad_field_type", test_bad_field_type)
    runner.test("sigs:review", test_review_signature)
    runner.test("sigs:flag", test_flag_signature)
    runner.test("sigs:review_bad_status", test_review_bad_status)
    runner.test("sigs:send_signing", test_send_for_signing)
    runner.test("sigs:send_missing_email", test_send_missing_email)
    runner.test("sigs:simulate_sign", test_simulate_sign)
    runner.test("sigs:signed_is_filled", test_signed_field_is_filled)
    runner.test("sigs:reminder", test_reminder)
    runner.test("sigs:outbox", test_outbox)
    runner.test("sigs:envelopes", test_envelopes)
    runner.test("sigs:delete_manual", test_delete_manual)
    runner.test("sigs:delete_nonexistent", test_delete_nonexistent)
    runner.test("sigs:sandbox_status", test_sandbox_status)
    runner.test("sigs:teardown", teardown)
