"""iCal subscription feed: global + per-txn, VCALENDAR format, content type."""
from .fixtures import get, post, create_simple_txn, cleanup, api, ADDR_CONT

ADDR_CAL = "8 Calendar Ct, Beverly Hills, CA 90210"


def register(runner):
    tid = None

    def setup():
        nonlocal tid
        tid, _ = create_simple_txn(ADDR_CAL)
        # Add a contingency with a deadline so the feed has content
        post(f"/api/txns/{tid}/contingencies", {
            "type": "investigation",
            "days": 17,
            "deadline_date": "2026-02-20",
        }, expect=201)
        # Add a disclosure with a due date
        post(f"/api/txns/{tid}/disclosures", {
            "type": "tds",
            "due_date": "2026-02-18",
        }, expect=201)

    def teardown():
        if tid:
            cleanup(tid)

    def test_global_feed():
        """GET /api/calendar.ics returns text/calendar."""
        import urllib.request
        resp = urllib.request.urlopen("http://localhost:5001/api/calendar.ics", timeout=10)
        assert resp.status == 200
        ct = resp.headers.get("Content-Type", "")
        assert "text/calendar" in ct, f"Expected text/calendar, got {ct}"
        body = resp.read().decode()
        assert "BEGIN:VCALENDAR" in body
        assert "END:VCALENDAR" in body
        assert "VERSION:2.0" in body

    def test_global_has_events():
        """Global feed should contain our test contingency and disclosure."""
        import urllib.request
        body = urllib.request.urlopen(
            "http://localhost:5001/api/calendar.ics", timeout=10
        ).read().decode()
        assert "BEGIN:VEVENT" in body
        assert "Investigation Contingency" in body or "Contingency" in body

    def test_txn_feed():
        """GET /api/txns/<tid>/calendar.ics returns valid iCal."""
        import urllib.request
        resp = urllib.request.urlopen(
            f"http://localhost:5001/api/txns/{tid}/calendar.ics", timeout=10
        )
        assert resp.status == 200
        body = resp.read().decode()
        assert "BEGIN:VCALENDAR" in body
        assert "BEGIN:VEVENT" in body

    def test_txn_feed_has_alarm():
        """Events should include VALARM reminders."""
        import urllib.request
        body = urllib.request.urlopen(
            f"http://localhost:5001/api/txns/{tid}/calendar.ics", timeout=10
        ).read().decode()
        assert "BEGIN:VALARM" in body
        assert "TRIGGER:" in body

    def test_txn_feed_calname():
        """Per-txn feed should have address in X-WR-CALNAME."""
        import urllib.request
        body = urllib.request.urlopen(
            f"http://localhost:5001/api/txns/{tid}/calendar.ics", timeout=10
        ).read().decode()
        assert "X-WR-CALNAME:" in body
        assert "8 Calendar Ct" in body

    def test_txn_404():
        """Nonexistent txn calendar should 404."""
        code, _ = get("/api/txns/nonexistent-id/calendar.ics")
        assert code == 404

    def test_empty_txn_feed():
        """Txn with no dates should still return valid empty calendar."""
        tid2, _ = create_simple_txn("9 Empty Cal St, Beverly Hills, CA 90210")
        import urllib.request
        body = urllib.request.urlopen(
            f"http://localhost:5001/api/txns/{tid2}/calendar.ics", timeout=10
        ).read().decode()
        assert "BEGIN:VCALENDAR" in body
        assert "END:VCALENDAR" in body
        # No events expected
        assert "BEGIN:VEVENT" not in body
        cleanup(tid2)

    # Register
    runner.test("cal:setup", setup)
    runner.test("cal:global_feed", test_global_feed)
    runner.test("cal:global_events", test_global_has_events)
    runner.test("cal:txn_feed", test_txn_feed)
    runner.test("cal:txn_alarm", test_txn_feed_has_alarm)
    runner.test("cal:txn_calname", test_txn_feed_calname)
    runner.test("cal:txn_404", test_txn_404)
    runner.test("cal:empty_feed", test_empty_txn_feed)
    runner.test("cal:teardown", teardown)
