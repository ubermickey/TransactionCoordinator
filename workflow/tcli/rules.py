"""YAML rule loading, jurisdiction resolution, tax calculation."""
import yaml
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # tc/ -> workflow/
JURIS_DIR = ROOT.parent / "jurisdictions"              # -> TransactionCoordinator/jurisdictions/


@cache
def _load(path: Path):
    return yaml.safe_load(path.read_text())


def phases():    return _load(ROOT / "phases.yaml")["phases"]
def gates():     return _load(ROOT / "agent_verification_gates.yaml")["gates"]
def deadlines(): return _load(ROOT / "deadlines.yaml")["deadlines"]


def gate(gid: str) -> dict | None:
    return next((g for g in gates() if g["id"] == gid), None)


def jurisdiction(name: str):
    return _load(JURIS_DIR / f"{name}.yaml")


def resolve(city: str) -> list[str]:
    """Jurisdiction file names for a city."""
    c = city.strip().lower()
    if "beverly hills" in c: return ["california", "beverly_hills"]
    if "los angeles" in c:   return ["california", "los_angeles"]
    return ["california"]


FORMS_DIR = ROOT / "forms"


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
