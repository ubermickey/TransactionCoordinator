"""Shared test data and HTTP helpers for sandbox tests."""
import json
import time
import urllib.request
import urllib.error
import urllib.parse

BASE = "http://localhost:5001"


def api(method: str, path: str, body: dict | None = None,
        expect: int = None, retries: int = 2) -> tuple[int, dict]:
    """Minimal HTTP client — handles HTML error pages, retries on lock."""
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")

    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                code = resp.status
                raw = resp.read().decode()
        except urllib.error.HTTPError as e:
            code = e.code
            raw = e.read().decode()
        except Exception as e:
            if attempt < retries:
                time.sleep(1)
                continue
            raise

        # Handle non-JSON responses (Flask debug HTML pages)
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            if "database is locked" in raw:
                if attempt < retries:
                    time.sleep(2)
                    continue
                result = {"error": "database is locked"}
            elif code >= 400:
                result = {"error": f"HTTP {code}", "raw": raw[:200]}
            else:
                result = {"error": "non-JSON response", "raw": raw[:200]}

        # Retry on database lock
        if isinstance(result, dict) and "database is locked" in str(result.get("error", "")):
            if attempt < retries:
                time.sleep(2)
                continue

        if expect is not None:
            assert code == expect, f"Expected {expect}, got {code}: {json.dumps(result)[:200]}"
        return code, result

    return code, result


def get(path, **kw):  return api("GET", path, **kw)
def post(path, body=None, **kw): return api("POST", path, body, **kw)
def delete(path, **kw): return api("DELETE", path, **kw)


def create_txn(address="123 Test St, Beverly Hills, CA 90210",
               txn_type="sale", role="listing", brokerage="douglas_elliman"):
    """Create a transaction, return (tid, txn_dict)."""
    code, data = post("/api/txns", {
        "address": address, "type": txn_type,
        "role": role, "brokerage": brokerage,
    }, expect=201)
    return data["id"], data


def create_simple_txn(address="100 Simple St, Beverly Hills, CA 90210"):
    """Create a transaction without brokerage (faster, no manifest scan)."""
    code, data = post("/api/txns", {
        "address": address, "type": "sale",
        "role": "listing", "brokerage": "",
    }, expect=201)
    return data["id"], data


def cleanup(tid: str):
    """Delete a transaction (ignore errors)."""
    try:
        delete(f"/api/txns/{tid}")
    except Exception:
        pass


# Addresses for test isolation
ADDR_CORE    = "1 Core Test Dr, Beverly Hills, CA 90210"
ADDR_GATES   = "2 Gate Test Ln, Beverly Hills, CA 90210"
ADDR_SIGS    = "3 Sig Test Blvd, Beverly Hills, CA 90210"
ADDR_CONT    = "4 Contingency Way, Beverly Hills, CA 90210"
ADDR_SEC     = "5 Security Ave, Beverly Hills, CA 90210"
