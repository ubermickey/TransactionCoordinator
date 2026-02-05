"""Document checklist resolution engine for brokerage-specific requirements."""
import json
from . import rules


def resolve(txn_type: str, party_role: str, brokerage: str, props: dict) -> list[dict]:
    """Return flat list of required documents for a transaction.

    Loads the brokerage YAML and filters forms by:
      1. Transaction type + party role  (section key: e.g. sale_listing)
      2. Conditional flags from props dict

    Each returned dict has: code, name, phase, role, source, required.
    """
    cfg = rules.brokerage(brokerage)
    if cfg is None:
        return []

    # Build section key: "sale_listing", "lease_buyer", etc.
    section = f"{txn_type}_{party_role}"
    forms = cfg.get(section, [])

    out = []
    for f in forms:
        if f["required"] == "conditional":
            flag = f.get("condition", "")
            if not props.get(flag):
                continue
        out.append({
            "code": f["code"],
            "name": f["name"],
            "phase": f["phase"],
            "role": f["role"],
            "source": f["source"],
            "required": f["required"],
        })
    return out


def re_resolve(txn_type: str, party_role: str, brokerage: str,
               props: dict, existing_codes: set) -> list[dict]:
    """Re-resolve checklist after props change.

    Returns only NEW documents not already tracked (doesn't remove received docs).
    """
    full = resolve(txn_type, party_role, brokerage, props)
    return [f for f in full if f["code"] not in existing_codes]
