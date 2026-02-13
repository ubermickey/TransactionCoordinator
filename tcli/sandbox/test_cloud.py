"""Cloud approval and cloud-event tracking tests."""
from .fixtures import get, post, delete, create_simple_txn, cleanup

ADDR_CLOUD = "8 Cloud Test Ave, Beverly Hills, CA 90210"


def register(runner):
    tid = None

    def setup():
        nonlocal tid
        tid, _ = create_simple_txn(ADDR_CLOUD)

    def teardown():
        if tid:
            cleanup(tid)

    def test_chat_blocked_without_approval():
        """Cloud chat requires transaction approval."""
        code, data = post("/api/chat", {
            "message": "status update",
            "txn_id": tid,
        })
        assert code == 403, f"expected 403, got {code}"
        assert data.get("code") == "cloud_approval_required"
        assert data.get("requires_approval") is True
        assert data.get("txn") == tid

    def test_events_logged_for_blocked():
        """Blocked cloud call should create a cloud_events record."""
        _, events = get(f"/api/txns/{tid}/cloud-events?limit=20", expect=200)
        assert isinstance(events, list), "expected list response"
        blocked = [
            e for e in events
            if e.get("service") == "anthropic"
            and e.get("operation") == "chat"
            and e.get("outcome") == "blocked"
        ]
        assert blocked, "expected at least one blocked chat cloud event"

    def test_approve_then_chat_path():
        """After approval, chat should no longer fail with approval-required."""
        _, approval = post(f"/api/txns/{tid}/cloud-approval", {
            "minutes": 30,
            "note": "sandbox test",
        }, expect=201)
        assert approval.get("active") is True
        assert (approval.get("remaining_seconds") or 0) > 0

        code, data = post("/api/chat", {
            "message": "hello",
            "txn_id": tid,
        })
        assert code != 403, "chat still blocked after approval"
        assert data.get("code") != "cloud_approval_required"

    def test_revoke_then_block_again():
        """Revoking approval should block cloud chat again."""
        _, revoked = delete(f"/api/txns/{tid}/cloud-approval", expect=200)
        assert revoked.get("active") is False

        code, data = post("/api/chat", {
            "message": "hello again",
            "txn_id": tid,
        })
        assert code == 403
        assert data.get("code") == "cloud_approval_required"

    def test_approval_status_endpoint():
        """GET approval endpoint returns active + remaining_seconds state."""
        _, status0 = get(f"/api/txns/{tid}/cloud-approval", expect=200)
        assert "active" in status0
        assert "remaining_seconds" in status0

        _, granted = post(f"/api/txns/{tid}/cloud-approval", {
            "minutes": 1,
            "note": "status check",
        }, expect=201)
        assert granted.get("active") is True

        _, status1 = get(f"/api/txns/{tid}/cloud-approval", expect=200)
        assert status1.get("active") is True
        assert (status1.get("remaining_seconds") or 0) > 0

    runner.test("cloud:setup", setup)
    runner.test("cloud:chat_blocked_without_approval", test_chat_blocked_without_approval)
    runner.test("cloud:events_logged_for_blocked", test_events_logged_for_blocked)
    runner.test("cloud:approve_then_chat_path", test_approve_then_chat_path)
    runner.test("cloud:revoke_then_block_again", test_revoke_then_block_again)
    runner.test("cloud:approval_status_endpoint", test_approval_status_endpoint)
    runner.test("cloud:teardown", teardown)
