"""Jurisdiction rule loader and compliance checker.

Loads YAML rule files and generates compliance checklists
based on property address and jurisdiction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ComplianceItem:
    rule_id: str
    jurisdiction: str
    name: str
    citation: str
    description: str = ""
    gate_id: str = ""
    completed: bool = False
    notes: str = ""


@dataclass
class ComplianceChecklist:
    address: str
    jurisdictions: list[str]
    items: list[ComplianceItem] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def completed_count(self) -> int:
        return sum(1 for i in self.items if i.completed)

    @property
    def pending_count(self) -> int:
        return self.total - self.completed_count


def load_jurisdiction(jurisdictions_dir: str | Path, name: str) -> dict:
    """Load a jurisdiction YAML file."""
    path = Path(jurisdictions_dir) / f"{name}.yaml"
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def generate_checklist(address: str, jurisdictions: list[str],
                       jurisdictions_dir: str | Path) -> ComplianceChecklist:
    """Generate a compliance checklist for a property based on its jurisdictions.

    Jurisdictions should be ordered from broadest to most specific:
    ["california", "los_angeles", "beverly_hills"]
    """
    checklist = ComplianceChecklist(address=address, jurisdictions=jurisdictions)

    for jur_name in jurisdictions:
        data = load_jurisdiction(jurisdictions_dir, jur_name)
        if not data:
            continue

        jur_label = data.get("jurisdiction", {}).get("name", jur_name)

        # Required forms
        for form in data.get("required_forms", []):
            checklist.items.append(ComplianceItem(
                rule_id=form.get("id", ""),
                jurisdiction=jur_label,
                name=form.get("name", ""),
                citation=form.get("citation", ""),
                description=f"Required for: {form.get('required_for', 'all transactions')}",
                gate_id=form.get("agent_gate", ""),
            ))

        # Retrofit requirements
        for retro in data.get("retrofit_requirements", []):
            checklist.items.append(ComplianceItem(
                rule_id=retro.get("id", ""),
                jurisdiction=jur_label,
                name=retro.get("name", ""),
                citation=retro.get("citation", ""),
                description=retro.get("requirement", ""),
                gate_id=retro.get("agent_gate", ""),
            ))

        # Tax rules (informational — for closing disclosure verification)
        for tax in data.get("tax_rules", []):
            name = tax.get("name", "")
            rate = tax.get("rate")
            per = tax.get("per")
            rate_str = f" ({rate}/{per})" if rate and per else ""
            tiers = tax.get("tiers")
            if tiers:
                rate_str = " (tiered — see rules)"
            checklist.items.append(ComplianceItem(
                rule_id=tax.get("id", ""),
                jurisdiction=jur_label,
                name=f"{name}{rate_str}",
                citation=tax.get("citation", ""),
                description=tax.get("note", ""),
                gate_id=tax.get("agent_gate", ""),
            ))

        # Compliance requirements (city-specific)
        for comp in data.get("compliance_requirements", []):
            checklist.items.append(ComplianceItem(
                rule_id=comp.get("id", ""),
                jurisdiction=jur_label,
                name=comp.get("name", ""),
                citation=comp.get("citation", ""),
                description=comp.get("description", ""),
                gate_id=comp.get("agent_gate", ""),
            ))

        # Rent stabilization
        rs = data.get("rent_stabilization")
        if rs:
            for item in (rs if isinstance(rs, list) else [rs]):
                checklist.items.append(ComplianceItem(
                    rule_id=item.get("id", ""),
                    jurisdiction=jur_label,
                    name=item.get("name", "Rent Stabilization"),
                    citation=item.get("citation", ""),
                    description=item.get("description", ""),
                    gate_id=item.get("agent_gate", ""),
                ))

    return checklist


def calculate_transfer_taxes(price: float, jurisdictions: list[str],
                             jurisdictions_dir: str | Path) -> dict[str, float]:
    """Calculate all applicable transfer taxes for a sale price."""
    taxes: dict[str, float] = {}

    for jur_name in jurisdictions:
        data = load_jurisdiction(jurisdictions_dir, jur_name)
        for tax in data.get("tax_rules", []):
            if "rate" not in tax and "tiers" not in tax:
                continue

            name = tax.get("name", jur_name)
            tiers = tax.get("tiers")

            if tiers:
                # Tiered tax (e.g., Measure ULA)
                for tier in tiers:
                    t_min = tier.get("threshold_min", 0)
                    t_max = tier.get("threshold_max") or float("inf")
                    if t_min <= price <= t_max:
                        rate = tier["rate"]
                        per = tier["per"]
                        taxes[name] = price * (rate / per)
                        break
            else:
                rate = tax["rate"]
                per = tax["per"]
                # Some taxes apply_when conditions
                applies_when = tax.get("applies_when", "")
                if "exceeds" in applies_when.lower():
                    # Skip if price doesn't meet threshold (handled by tiers)
                    continue
                taxes[name] = price * (rate / per)

    taxes["TOTAL"] = sum(taxes.values())
    return taxes
