"""YAML rule loading, jurisdiction resolution, tax calculation."""
import yaml
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # tc/ -> workflow/
JURIS_DIR = ROOT.parent / "jurisdictions"              # -> TransactionCoordinator/jurisdictions/


@cache
def _load(path: Path):
    return yaml.safe_load(path.read_text())


def phases():       return _load(ROOT / "phases.yaml")["phases"]
def lease_phases(): return _load(ROOT / "phases.yaml").get("lease_phases", [])
def gates():        return _load(ROOT / "agent_verification_gates.yaml")["gates"]
def deadlines():    return _load(ROOT / "deadlines.yaml")["deadlines"]


def de_gates(brokerage_name: str) -> list[dict]:
    """Return brokerage-specific gates from agent_verification_gates.yaml."""
    data = _load(ROOT / "agent_verification_gates.yaml")
    all_de = data.get("de_gates", [])
    return [g for g in all_de if g.get("brokerage", "") == brokerage_name]


def gate(gid: str) -> dict | None:
    """Find a gate by ID in standard gates + DE gates."""
    g = next((g for g in gates() if g["id"] == gid), None)
    if g:
        return g
    data = _load(ROOT / "agent_verification_gates.yaml")
    return next((g for g in data.get("de_gates", []) if g["id"] == gid), None)


def jurisdiction(name: str):
    return _load(JURIS_DIR / f"{name}.yaml")


def resolve(city: str) -> list[str]:
    """Jurisdiction file names for a city."""
    c = city.strip().lower()
    if "beverly hills" in c: return ["california", "beverly_hills"]
    if "los angeles" in c:   return ["california", "los_angeles"]
    return ["california"]


BROKERAGE_DIR = ROOT / "brokerages"
FORMS_DIR = ROOT / "forms"


def brokerage(name: str) -> dict | None:
    """Load brokerage YAML by name (e.g. 'douglas_elliman')."""
    if not name:
        return None
    path = BROKERAGE_DIR / f"{name}.yaml"
    return _load(path) if path.exists() else None


def brokerage_list() -> list[str]:
    """Return available brokerage config names."""
    if not BROKERAGE_DIR.exists():
        return []
    return sorted(p.stem for p in BROKERAGE_DIR.glob("*.yaml"))


def form_template(code: str | None) -> dict | None:
    if not code:
        return None
    path = FORMS_DIR / f"{code.lower()}.yaml"
    return _load(path) if path.exists() else None


def form_templates() -> list[dict]:
    if not FORMS_DIR.exists():
        return []
    return [_load(p) for p in sorted(FORMS_DIR.glob("*.yaml"))]


def all_rules(names: list[str], section: str) -> list[dict]:
    out = []
    for n in names:
        out.extend(jurisdiction(n).get(section, []))
    return out


def calc_taxes(price: float, names: list[str]) -> list[tuple[str, float]]:
    taxes, seen = [], set()
    # Process most-specific jurisdiction first so local rules take precedence
    for r in all_rules(list(reversed(names)), "tax_rules"):
        if "tiers" in r:
            for t in r["tiers"]:
                lo, hi = t["threshold_min"], t.get("threshold_max")
                if price >= lo and (hi is None or price <= hi):
                    taxes.append((r["name"], price * t["rate"] / t["per"]))
                    break
        elif "rate" in r:
            key = (r["rate"], r["per"], r.get("basis"))
            if key in seen:
                continue  # skip duplicate (e.g., county tax in both CA + LA)
            seen.add(key)
            taxes.append((r["name"], price * r["rate"] / r["per"]))
    return taxes
