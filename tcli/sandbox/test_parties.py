"""Party/Contact management: CRUD, roles, validation, cross-txn isolation."""
from .fixtures import get, post, delete, create_simple_txn, cleanup, ADDR_PARTY


def register(runner):
    tid = None
    party_id = None

    def setup():
        nonlocal tid
        tid, _ = create_simple_txn(ADDR_PARTY)

    def teardown():
        if tid:
            cleanup(tid)

    def test_list_empty():
        """Fresh transaction should have no parties."""
        _, data = get(f"/api/txns/{tid}/parties", expect=200)
        assert isinstance(data, list)
        assert len(data) == 0

    def test_add_buyer():
        """Add a buyer party."""
        nonlocal party_id
        _, p = post(f"/api/txns/{tid}/parties", {
            "role": "buyer",
            "name": "Jane Smith",
            "email": "jane@example.com",
            "phone": "(555) 123-4567",
            "company": "Smith Realty",
            "license_no": "DRE#12345",
        }, expect=201)
        assert p["role"] == "buyer"
        assert p["name"] == "Jane Smith"
        assert p["email"] == "jane@example.com"
        assert p["role_name"] == "Buyer"
        party_id = p["id"]

    def test_add_seller():
        """Add a seller party."""
        _, p = post(f"/api/txns/{tid}/parties", {
            "role": "seller",
            "name": "John Doe",
            "email": "john@example.com",
        }, expect=201)
        assert p["role"] == "seller"
        assert p["name"] == "John Doe"

    def test_add_escrow_officer():
        """Add escrow officer."""
        _, p = post(f"/api/txns/{tid}/parties", {
            "role": "escrow_officer",
            "name": "Maria Garcia",
            "company": "Pacific Escrow",
            "email": "maria@pacificescrow.com",
        }, expect=201)
        assert p["role"] == "escrow_officer"
        assert p["role_name"] == "Escrow Officer"

    def test_add_agents():
        """Add buyer and seller agents."""
        _, ba = post(f"/api/txns/{tid}/parties", {
            "role": "buyer_agent",
            "name": "Agent One",
            "license_no": "DRE#00001",
        }, expect=201)
        assert ba["role"] == "buyer_agent"
        _, sa = post(f"/api/txns/{tid}/parties", {
            "role": "seller_agent",
            "name": "Agent Two",
            "license_no": "DRE#00002",
        }, expect=201)
        assert sa["role"] == "seller_agent"

    def test_list_with_parties():
        """Should list all added parties."""
        _, data = get(f"/api/txns/{tid}/parties", expect=200)
        assert len(data) == 5
        roles = {p["role"] for p in data}
        assert "buyer" in roles
        assert "seller" in roles
        assert "escrow_officer" in roles

    def test_update_party():
        """Update a party's info."""
        if not party_id:
            return
        code, result = api_put(f"/api/txns/{tid}/parties/{party_id}", {
            "name": "Jane Smith-Johnson",
            "phone": "(555) 999-0000",
        })
        assert code == 200
        assert result["name"] == "Jane Smith-Johnson"
        assert result["phone"] == "(555) 999-0000"
        # Email should be unchanged
        assert result["email"] == "jane@example.com"

    def test_delete_party():
        """Delete a party."""
        # Add a throwaway party
        _, p = post(f"/api/txns/{tid}/parties", {
            "role": "inspector",
            "name": "Temp Inspector",
        }, expect=201)
        _, result = delete(f"/api/txns/{tid}/parties/{p['id']}", expect=200)
        assert result["ok"] is True
        # Verify gone
        _, data = get(f"/api/txns/{tid}/parties", expect=200)
        assert not any(x["id"] == p["id"] for x in data)

    def test_add_missing_name():
        """Adding without name should fail."""
        code, _ = post(f"/api/txns/{tid}/parties", {
            "role": "buyer", "name": "",
        })
        assert code == 400

    def test_add_missing_role():
        """Adding without role should fail."""
        code, _ = post(f"/api/txns/{tid}/parties", {
            "role": "", "name": "Test",
        })
        assert code == 400

    def test_add_invalid_role():
        """Adding with invalid role should fail."""
        code, _ = post(f"/api/txns/{tid}/parties", {
            "role": "wizard", "name": "Gandalf",
        })
        assert code == 400

    def test_delete_nonexistent():
        """Delete nonexistent party should 404."""
        code, _ = delete(f"/api/txns/{tid}/parties/99999")
        assert code == 404

    def test_cross_txn_isolation():
        """Parties from one txn should not appear in another."""
        tid2, _ = create_simple_txn(ADDR_PARTY + " (iso)")
        _, data = get(f"/api/txns/{tid2}/parties", expect=200)
        assert len(data) == 0
        cleanup(tid2)

    def test_audit_logged():
        """Party actions should appear in audit."""
        _, audit = get(f"/api/txns/{tid}/audit", expect=200)
        actions = [r["action"] for r in audit]
        assert "party_added" in actions
        assert "party_deleted" in actions

    # Helper for PUT
    import json
    import urllib.request
    import urllib.error
    from .fixtures import BASE

    def api_put(path, body):
        url = f"{BASE}{path}"
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method="PUT")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, {"error": raw[:200]}

    # Register
    runner.test("party:setup", setup)
    runner.test("party:list_empty", test_list_empty)
    runner.test("party:add_buyer", test_add_buyer)
    runner.test("party:add_seller", test_add_seller)
    runner.test("party:add_escrow", test_add_escrow_officer)
    runner.test("party:add_agents", test_add_agents)
    runner.test("party:list_all", test_list_with_parties)
    runner.test("party:update", test_update_party)
    runner.test("party:delete", test_delete_party)
    runner.test("party:missing_name", test_add_missing_name)
    runner.test("party:missing_role", test_add_missing_role)
    runner.test("party:invalid_role", test_add_invalid_role)
    runner.test("party:delete_404", test_delete_nonexistent)
    runner.test("party:isolation", test_cross_txn_isolation)
    runner.test("party:audit", test_audit_logged)
    runner.test("party:teardown", teardown)
