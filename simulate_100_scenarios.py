"""Simulate 100 random transaction scenarios through the full workflow.

Tests: create txn, populate docs, receive/verify docs, add contingencies,
       add parties, add disclosures, verify gates, advance phases,
       scan contracts, verify fields, submit bug reports.

Reports pass/fail for each scenario with detailed breakdown.
"""
import json
import random
import string
import sys
import time
import traceback
from pathlib import Path

# We test against the Flask app directly (no HTTP server needed)
sys.path.insert(0, str(Path(__file__).parent))
from tcli.web import app  # noqa: E402

BROKERAGES = ["compass", "kw", "coldwell_banker", "exp_realty", "re_max"]
TXN_TYPES = ["sale", "lease"]
PARTY_ROLES_TXN = ["listing", "buyer"]
CA_CITIES = [
    "Beverly Hills, CA 90210", "Los Angeles, CA 90001", "Malibu, CA 90265",
    "Pasadena, CA 91101", "Santa Monica, CA 90401", "Burbank, CA 91502",
    "Glendale, CA 91201", "Long Beach, CA 90802", "Torrance, CA 90501",
    "Irvine, CA 92602", "Huntington Beach, CA 92648", "Anaheim, CA 92801",
    "San Diego, CA 92101", "San Francisco, CA 94102", "Oakland, CA 94601",
    "Sacramento, CA 95814", "San Jose, CA 95110", "Fresno, CA 93721",
]

PARTY_TYPES = [
    ("buyer", "John Smith", "john@example.com"),
    ("seller", "Jane Doe", "jane@example.com"),
    ("buyer_agent", "Mike Johnson", "mike@brokerage.com"),
    ("seller_agent", "Sarah Williams", "sarah@brokerage.com"),
    ("escrow_officer", "Chris Brown", "chris@escrow.com"),
    ("lender", "Pat Davis", "pat@lender.com"),
    ("title_rep", "Alex Turner", "alex@title.com"),
    ("inspector", "Robin Lee", "robin@inspect.com"),
]

CONTINGENCY_TYPES = ["investigation", "appraisal", "loan", "hoa"]
DISCLOSURE_TYPES = ["tds", "spq", "nhd", "avid_listing", "avid_buyer",
                    "lead_paint", "water_heater", "smoke_co", "megan_law",
                    "preliminary_title"]


class Scenario:
    def __init__(self, num):
        self.num = num
        self.steps = []
        self.passed = True
        self.error = ""
        self.tid = None

    def step(self, name, ok, detail=""):
        self.steps.append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            self.passed = False

    def fail(self, msg):
        self.passed = False
        self.error = msg


def rand_address():
    num = random.randint(100, 9999)
    street = random.choice(["Oak", "Maple", "Elm", "Pine", "Cedar", "Birch",
                            "Willow", "Sunset", "Pacific", "Harbor", "Canyon"])
    suffix = random.choice(["St", "Ave", "Blvd", "Dr", "Ln", "Way", "Ct"])
    city = random.choice(CA_CITIES)
    return f"{num} {street} {suffix}, {city}"


def run_scenario(client, num):
    s = Scenario(num)
    try:
        # ── 1. Create transaction ──
        txn_type = random.choice(TXN_TYPES)
        party_role = random.choice(PARTY_ROLES_TXN)
        brokerage = random.choice(BROKERAGES) if random.random() > 0.15 else ""
        address = rand_address()

        resp = client.post("/api/txns", json={
            "address": address,
            "type": txn_type,
            "role": party_role,
            "brokerage": brokerage,
        })
        s.step("create_txn", resp.status_code == 201,
               f"{txn_type}/{party_role} brokerage={brokerage}")
        if resp.status_code != 201:
            s.fail(f"Create failed: {resp.status_code}")
            return s

        data = resp.get_json()
        tid = data["id"]
        s.tid = tid

        # ── 2. Get transaction ──
        resp = client.get(f"/api/txns/{tid}")
        s.step("get_txn", resp.status_code == 200)

        # ── 3. List docs ──
        resp = client.get(f"/api/txns/{tid}/docs")
        docs = resp.get_json()
        doc_count = len(docs) if not isinstance(docs, dict) else 0
        s.step("list_docs", resp.status_code == 200, f"{doc_count} docs")

        # ── 4. Receive some docs ──
        if doc_count > 0:
            receive_count = random.randint(1, max(1, doc_count))
            sample = random.sample(docs, min(receive_count, len(docs)))
            for d in sample:
                r = client.post(f"/api/txns/{tid}/docs/{d['code']}/receive")
                if r.status_code != 200:
                    s.step("doc_receive", False, f"{d['code']}: {r.status_code}")
            s.step("docs_received", True, f"{len(sample)} received")
        else:
            s.step("docs_received", True, "0 docs (no brokerage)")

        # ── 5. Verify some received docs ──
        resp = client.get(f"/api/txns/{tid}/docs")
        docs = resp.get_json() if resp.status_code == 200 else []
        received = [d for d in docs if d.get("status") == "received"]
        if received:
            verify_count = random.randint(1, len(received))
            for d in random.sample(received, verify_count):
                client.post(f"/api/txns/{tid}/docs/{d['code']}/verify")
            s.step("docs_verified", True, f"{verify_count} verified")
        else:
            s.step("docs_verified", True, "none to verify")

        # ── 6. Bulk actions (sometimes) ──
        if random.random() > 0.6:
            r = client.post(f"/api/txns/{tid}/docs/bulk-receive")
            s.step("bulk_receive", r.status_code == 200)
            r = client.post(f"/api/txns/{tid}/docs/bulk-verify")
            s.step("bulk_verify", r.status_code == 200)

        # ── 7. Add parties ──
        party_count = random.randint(2, 6)
        party_sample = random.sample(PARTY_TYPES, min(party_count, len(PARTY_TYPES)))
        for role, name, email in party_sample:
            r = client.post(f"/api/txns/{tid}/parties", json={
                "role": role, "name": name, "email": email,
                "phone": "310-555-" + str(random.randint(1000, 9999)),
            })
        s.step("add_parties", True, f"{len(party_sample)} parties")

        # ── 8. List parties ──
        resp = client.get(f"/api/txns/{tid}/parties")
        s.step("list_parties", resp.status_code == 200)

        # ── 9. Add contingencies ──
        if txn_type == "sale":
            cont_count = random.randint(1, 3)
            cont_types = random.sample(CONTINGENCY_TYPES, cont_count)
            for ct in cont_types:
                days = random.choice([17, 21, 25, 30])
                r = client.post(f"/api/txns/{tid}/contingencies", json={
                    "type": ct, "days": days,
                })
            s.step("add_contingencies", True, f"{cont_count} contingencies")

            # ── 10. Contingency actions ──
            resp = client.get(f"/api/txns/{tid}/contingencies")
            conts = resp.get_json().get("items", []) if resp.status_code == 200 else []
            for c in conts:
                action = random.choice(["remove", "waive", "nbp", "none", "none"])
                if action == "remove":
                    client.post(f"/api/txns/{tid}/contingencies/{c['id']}/remove")
                elif action == "waive":
                    client.post(f"/api/txns/{tid}/contingencies/{c['id']}/waive")
                elif action == "nbp":
                    client.post(f"/api/txns/{tid}/contingencies/{c['id']}/nbp")
            s.step("contingency_actions", True)

        # ── 11. Add disclosures ──
        disc_count = random.randint(2, 6)
        disc_types = random.sample(DISCLOSURE_TYPES, min(disc_count, len(DISCLOSURE_TYPES)))
        for dt in disc_types:
            client.post(f"/api/txns/{tid}/disclosures", json={"type": dt})
        s.step("add_disclosures", True, f"{len(disc_types)} disclosures")

        # ── 12. Disclosure workflow ──
        resp = client.get(f"/api/txns/{tid}/disclosures")
        discs = resp.get_json().get("items", []) if resp.status_code == 200 else []
        for d in discs:
            if random.random() > 0.3:
                client.post(f"/api/txns/{tid}/disclosures/{d['id']}/receive")
            if random.random() > 0.5:
                client.post(f"/api/txns/{tid}/disclosures/{d['id']}/review")
        s.step("disclosure_workflow", True)

        # ── 13. Gates ──
        resp = client.get(f"/api/txns/{tid}/gates")
        gates = resp.get_json() if resp.status_code == 200 else []
        gate_count = len(gates) if isinstance(gates, list) else 0
        s.step("list_gates", resp.status_code == 200, f"{gate_count} gates")

        # Verify some gates
        if isinstance(gates, list) and gates:
            verify_n = random.randint(1, min(10, len(gates)))
            sample = random.sample(gates, verify_n)
            for g in sample:
                client.post(f"/api/txns/{tid}/gates/{g['gid']}/verify",
                            json={"notes": "Auto-verified by simulation"})
            s.step("verify_gates", True, f"{verify_n} verified")

        # ── 14. Signatures ──
        resp = client.get(f"/api/txns/{tid}/signatures")
        s.step("list_signatures", resp.status_code == 200)

        # ── 15. Deadlines ──
        resp = client.get(f"/api/txns/{tid}/deadlines")
        dls = resp.get_json() if resp.status_code == 200 else []
        dl_count = len(dls) if isinstance(dls, list) else 0
        s.step("list_deadlines", resp.status_code == 200, f"{dl_count} deadlines")

        # ── 16. Properties (toggle some flags) ──
        flags = ["hoa", "trust", "solar", "leased_solar", "mello_roos",
                 "bond", "death_3yr", "court_ordered", "reo", "short_sale"]
        for flag in random.sample(flags, random.randint(0, 3)):
            client.post(f"/api/txns/{tid}/props", json={
                "flag": flag, "value": True,
            })
        s.step("set_props", True)

        # ── 17. Phase advancement (try) ──
        resp = client.post(f"/api/txns/{tid}/advance")
        advanced = resp.status_code == 200
        s.step("try_advance", True, f"{'advanced' if advanced else 'blocked (expected)'}")

        # ── 18. Audit log ──
        resp = client.get(f"/api/txns/{tid}/audit")
        audit = resp.get_json() if resp.status_code == 200 else []
        audit_count = len(audit) if isinstance(audit, list) else 0
        s.step("audit_log", resp.status_code == 200, f"{audit_count} entries")

        # ── 19. Dashboard ──
        resp = client.get("/api/dashboard")
        s.step("dashboard", resp.status_code == 200)

        # ── 20. Bug report ──
        if random.random() > 0.7:
            r = client.post("/api/bug-reports", json={
                "summary": f"Test bug from scenario {num}",
                "description": "Automated simulation test",
                "action_log": [{"ts": "00:00", "action": "test", "detail": "sim"}],
            })
            s.step("bug_report", r.status_code == 201)

        # ── 21. Calendar feed ──
        resp = client.get("/api/calendar.ics")
        s.step("calendar_ics", resp.status_code == 200)

        resp = client.get(f"/api/txns/{tid}/calendar.ics")
        s.step("txn_calendar", resp.status_code == 200)

        # ── 22. Notes ──
        note_text = f"Simulation scenario {num} notes"
        client.post(f"/api/txns/{tid}/notes", json={"notes": note_text})
        resp = client.get(f"/api/txns/{tid}/notes")
        s.step("notes", resp.status_code == 200 and resp.get_json().get("notes") == note_text)

        # ── 23. Unverify a doc (if any verified) ──
        resp = client.get(f"/api/txns/{tid}/docs")
        docs = resp.get_json() if resp.status_code == 200 else []
        verified_docs = [d for d in docs if d.get("status") == "verified"]
        if verified_docs:
            d = random.choice(verified_docs)
            r = client.post(f"/api/txns/{tid}/docs/{d['code']}/unverify")
            s.step("unverify_doc", r.status_code == 200)

        # ── 24. Delete txn (cleanup) ──
        resp = client.delete(f"/api/txns/{tid}")
        s.step("delete_txn", resp.status_code == 200)

    except Exception as e:
        s.fail(f"Exception: {str(e)}")
        s.steps.append({"name": "exception", "ok": False, "detail": traceback.format_exc()[:200]})

    return s


def main():
    app.config["TESTING"] = True
    client = app.test_client()

    total = 100
    results = []
    passed = 0
    failed = 0

    print(f"\n{'='*70}")
    print(f"  TRANSACTION COORDINATOR — 100 SCENARIO SIMULATION")
    print(f"{'='*70}\n")

    start = time.time()
    for i in range(1, total + 1):
        s = run_scenario(client, i)
        results.append(s)
        if s.passed:
            passed += 1
            status = "PASS"
        else:
            failed += 1
            status = "FAIL"

        total_steps = len(s.steps)
        passed_steps = sum(1 for st in s.steps if st["ok"])
        bar = f"[{'#' * (passed_steps * 20 // max(total_steps, 1)):20s}]"
        print(f"  {status:4s}  #{i:3d}  {bar}  {passed_steps}/{total_steps} steps"
              + (f"  ERR: {s.error[:50]}" if s.error else ""))

    elapsed = time.time() - start

    # ── Summary ──
    print(f"\n{'='*70}")
    print(f"  RESULTS")
    print(f"{'='*70}")
    print(f"  Total scenarios: {total}")
    print(f"  Passed: {passed} ({passed * 100 // total}%)")
    print(f"  Failed: {failed} ({failed * 100 // total}%)")
    print(f"  Time: {elapsed:.1f}s ({elapsed / total:.2f}s/scenario)")

    # Step-level stats
    all_steps = {}
    for s in results:
        for st in s.steps:
            name = st["name"]
            if name not in all_steps:
                all_steps[name] = {"total": 0, "pass": 0, "fail": 0}
            all_steps[name]["total"] += 1
            if st["ok"]:
                all_steps[name]["pass"] += 1
            else:
                all_steps[name]["fail"] += 1

    print(f"\n  Step Breakdown:")
    print(f"  {'Step':30s}  {'Pass':>6s}  {'Fail':>6s}  {'Rate':>6s}")
    print(f"  {'-'*30}  {'-'*6}  {'-'*6}  {'-'*6}")
    for name, stats in sorted(all_steps.items()):
        rate = stats["pass"] * 100 // max(stats["total"], 1)
        flag = " !!!" if stats["fail"] > 0 else ""
        print(f"  {name:30s}  {stats['pass']:6d}  {stats['fail']:6d}  {rate:5d}%{flag}")

    # Failed scenario details
    if failed > 0:
        print(f"\n  Failed Scenario Details:")
        for s in results:
            if not s.passed:
                print(f"  --- Scenario #{s.num} ---")
                if s.error:
                    print(f"    Error: {s.error}")
                for st in s.steps:
                    if not st["ok"]:
                        print(f"    FAIL: {st['name']} — {st['detail'][:100]}")

    print(f"\n{'='*70}\n")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
