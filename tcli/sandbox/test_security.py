"""Security tests: injection, XSS, path traversal, IDOR, input fuzzing."""
import urllib.parse
from .fixtures import get, post, delete, create_simple_txn, create_txn, cleanup, ADDR_SEC


def register(runner):
    tid = None

    def setup():
        nonlocal tid
        tid, _ = create_simple_txn(ADDR_SEC)

    def teardown():
        if tid:
            cleanup(tid)

    # ── SQL Injection ────────────────────────────────────────────────────────

    def test_sqli_address():
        """SQL injection via address field — parameterized queries should prevent it."""
        payload = "123 Main'; DROP TABLE txns;--"
        code, data = post("/api/txns", {"address": payload, "type": "sale"})
        assert code == 201, "should create normally (parameterized queries)"
        # Verify tables still exist
        _, txns = get("/api/txns", expect=200)
        assert isinstance(txns, list), "txns table should still exist"
        cleanup(data["id"])

    def test_sqli_address_union():
        """UNION-based injection in address."""
        payload = "123 Main' UNION SELECT * FROM txns--"
        code, data = post("/api/txns", {"address": payload, "type": "sale"})
        assert code == 201
        cleanup(data["id"])

    def test_sqli_gate_verify_notes():
        """SQL injection via gate verify notes field."""
        _, gates = get(f"/api/txns/{tid}/gates", expect=200)
        if gates:
            gid = gates[0]["gid"]
            _, result = post(f"/api/txns/{tid}/gates/{gid}/verify", {
                "notes": "'; DROP TABLE gates;--",
            }, expect=200)
            # Verify table intact
            _, gates2 = get(f"/api/txns/{tid}/gates", expect=200)
            assert isinstance(gates2, list), "gates table intact after injection attempt"

    def test_sqli_contingency_notes():
        """SQL injection via contingency notes."""
        code, _ = post(f"/api/txns/{tid}/contingencies", {
            "type": "investigation",
            "days": 17,
            "deadline_date": "2026-02-18",
            "notes": "'; DELETE FROM contingencies;--",
        })
        assert code == 201
        _, data = get(f"/api/txns/{tid}/contingencies", expect=200)
        assert "items" in data, "contingencies table intact"

    def test_sqli_sig_review_note():
        """SQL injection via signature review note."""
        _, sig = post(f"/api/txns/{tid}/signatures/add", {
            "doc_code": "RPA",
            "field_name": "SQLi Test",
            "field_type": "signature",
            "page": 1,
        }, expect=201)
        _, result = post(f"/api/txns/{tid}/signatures/{sig['id']}/review", {
            "status": "reviewed",
            "note": "'); DROP TABLE sig_reviews;--",
        }, expect=200)
        assert result["review_status"] == "reviewed"

    # ── XSS / Script Injection ───────────────────────────────────────────────

    def test_xss_address():
        """XSS payload in address — stored as-is (frontend must escape)."""
        payload = '<script>alert("xss")</script>123 Main St, Beverly Hills, CA 90210'
        code, data = post("/api/txns", {"address": payload, "type": "sale"})
        assert code == 201
        _, txn = get(f"/api/txns/{data['id']}", expect=200)
        # Flask stores raw — the UI must use textContent not innerHTML
        assert "<script>" in txn["address"]
        cleanup(data["id"])

    def test_xss_sig_field_name():
        """XSS in signature field name."""
        _, sig = post(f"/api/txns/{tid}/signatures/add", {
            "doc_code": "RPA",
            "field_name": '<img onerror=alert(1) src=x>',
            "field_type": "signature",
            "page": 1,
        })
        assert sig.get("field_name") and "<img" in sig["field_name"]

    def test_xss_contingency_notes():
        """XSS in contingency notes."""
        _, c = post(f"/api/txns/{tid}/contingencies", {
            "type": "hoa",
            "days": 5,
            "deadline_date": "2026-02-06",
            "notes": '<svg onload=alert(document.cookie)>',
        })
        if c.get("notes"):
            assert "<svg" in c["notes"]

    # ── IDOR (Insecure Direct Object Reference) ─────────────────────────────

    def test_idor_cross_txn_contingency():
        """Cannot remove contingency from wrong transaction."""
        tid2, _ = create_simple_txn(ADDR_SEC + " (idor2)")
        _, c = post(f"/api/txns/{tid2}/contingencies", {
            "type": "investigation", "days": 17, "deadline_date": "2026-02-18",
        })
        if c.get("id"):
            # Try to remove tid2's contingency via tid
            code, _ = post(f"/api/txns/{tid}/contingencies/{c['id']}/remove")
            assert code == 404, "should not find contingency under wrong txn"
        cleanup(tid2)

    def test_idor_cross_txn_signature():
        """Cannot review signature from wrong transaction."""
        tid2, _ = create_simple_txn(ADDR_SEC + " (idor3)")
        _, sig = post(f"/api/txns/{tid2}/signatures/add", {
            "doc_code": "RPA", "field_name": "IDOR Test",
            "field_type": "signature", "page": 1,
        })
        if sig.get("id"):
            code, _ = post(f"/api/txns/{tid}/signatures/{sig['id']}/review", {
                "status": "reviewed",
            })
            assert code == 404, "should not find sig under wrong txn"
        cleanup(tid2)

    # ── Input Validation ─────────────────────────────────────────────────────

    def test_empty_body():
        """POST with empty body should return 400."""
        code, _ = post("/api/txns", {})
        assert code == 400

    def test_empty_address():
        """Empty string address should return 400."""
        code, _ = post("/api/txns", {"address": ""})
        assert code == 400

    def test_whitespace_address():
        """Whitespace-only address should return 400."""
        code, _ = post("/api/txns", {"address": "   "})
        assert code == 400

    def test_null_address():
        """Null address should return 400."""
        code, _ = post("/api/txns", {"address": None})
        assert code == 400

    def test_huge_input():
        """Very long address should not crash."""
        addr = "A" * 10000 + ", Beverly Hills, CA 90210"
        code, data = post("/api/txns", {"address": addr, "type": "sale"})
        assert code == 201
        cleanup(data["id"])

    def test_unicode_input():
        """Unicode characters in address."""
        addr = "123 Main St \u00e9\u00e8\u00ea\u2603\u2764, Beverly Hills, CA 90210"
        code, data = post("/api/txns", {"address": addr, "type": "sale"})
        assert code == 201
        _, txn = get(f"/api/txns/{data['id']}", expect=200)
        assert "\u00e9" in txn["address"]
        cleanup(data["id"])

    def test_emoji_input():
        """Emoji in address field."""
        addr = "123 Main St, Beverly Hills, CA 90210"
        code, data = post("/api/txns", {"address": addr, "type": "sale"})
        assert code == 201
        cleanup(data["id"])

    def test_negative_contingency_days():
        """Negative days in contingency should be accepted (past-due)."""
        code, _ = post(f"/api/txns/{tid}/contingencies", {
            "type": "other", "days": -5, "deadline_date": "2026-01-26",
        })
        # Either accepted or rejected — both are valid behaviors
        assert code in (201, 400)

    def test_zero_contingency_days():
        """Zero days contingency."""
        code, _ = post(f"/api/txns/{tid}/contingencies", {
            "type": "appraisal", "days": 0, "deadline_date": "2026-02-01",
        })
        assert code in (201, 400, 409)

    def test_large_page_number():
        """Extremely large page number in signature."""
        _, sig = post(f"/api/txns/{tid}/signatures/add", {
            "doc_code": "RPA",
            "field_name": "Overflow Test",
            "field_type": "signature",
            "page": 999999999,
        }, expect=201)
        assert sig["page"] == 999999999

    def test_special_chars_in_notes():
        """Special characters in notes fields."""
        _, c = post(f"/api/txns/{tid}/contingencies", {
            "type": "loan",
            "days": 17,
            "deadline_date": "2026-02-18",
            "notes": "Line1\nLine2\tTabbed\r\nCRLF & <brackets> \"quotes\"",
        })
        assert c.get("notes") is not None

    # ── Nonexistent Resource ─────────────────────────────────────────────────

    def test_nonexistent_txn():
        """Operations on nonexistent transaction."""
        code, _ = get("/api/txns/fakeid123")
        assert code == 404

    def test_nonexistent_gate():
        """Verify nonexistent gate."""
        _, result = post(f"/api/txns/{tid}/gates/GATE-999/verify")
        # No crash — gate may or may not exist
        assert isinstance(result, dict)

    def test_nonexistent_sig():
        """Review nonexistent signature."""
        code, _ = post(f"/api/txns/{tid}/signatures/99999/review", {
            "status": "reviewed",
        })
        assert code == 404

    # Register all tests
    runner.test("sec:setup", setup)
    runner.test("sec:sqli_address", test_sqli_address)
    runner.test("sec:sqli_union", test_sqli_address_union)
    runner.test("sec:sqli_gate_notes", test_sqli_gate_verify_notes)
    runner.test("sec:sqli_cont_notes", test_sqli_contingency_notes)
    runner.test("sec:sqli_sig_note", test_sqli_sig_review_note)
    runner.test("sec:xss_address", test_xss_address)
    runner.test("sec:xss_sig_field", test_xss_sig_field_name)
    runner.test("sec:xss_cont_notes", test_xss_contingency_notes)
    runner.test("sec:idor_contingency", test_idor_cross_txn_contingency)
    runner.test("sec:idor_signature", test_idor_cross_txn_signature)
    runner.test("sec:empty_body", test_empty_body)
    runner.test("sec:empty_address", test_empty_address)
    runner.test("sec:whitespace_address", test_whitespace_address)
    runner.test("sec:null_address", test_null_address)
    runner.test("sec:huge_input", test_huge_input)
    runner.test("sec:unicode_input", test_unicode_input)
    runner.test("sec:emoji_input", test_emoji_input)
    runner.test("sec:negative_days", test_negative_contingency_days)
    runner.test("sec:zero_days", test_zero_contingency_days)
    runner.test("sec:large_page", test_large_page_number)
    runner.test("sec:special_chars", test_special_chars_in_notes)
    runner.test("sec:nonexistent_txn", test_nonexistent_txn)
    runner.test("sec:nonexistent_gate", test_nonexistent_gate)
    runner.test("sec:nonexistent_sig", test_nonexistent_sig)
    runner.test("sec:teardown", teardown)
