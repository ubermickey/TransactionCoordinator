"""Gate management — initialize, track, and enforce verification gates."""

from __future__ import annotations

from datetime import datetime

import yaml

from tc.models import GateStatus, GateType, GateVerification, Phase, Transaction


def load_gate_definitions(workflow_dir: str) -> list[dict]:
    """Load gate definitions from the YAML specification."""
    path = f"{workflow_dir}/agent_verification_gates.yaml"
    with open(path) as f:
        data = yaml.safe_load(f)
    return data.get("gates", [])


def initialize_gates(txn: Transaction, workflow_dir: str) -> list[GateVerification]:
    """Create gate tracking records for a transaction from YAML definitions."""
    definitions = load_gate_definitions(workflow_dir)
    gates: list[GateVerification] = []
    for defn in definitions:
        gate = GateVerification(
            gate_id=defn["id"],
            gate_name=defn["name"],
            gate_type=GateType(defn["type"]),
            phase=Phase(defn["phase"]),
            status=GateStatus.PENDING,
        )
        # Skip HOA gate if no HOA
        if defn["id"] == "GATE-051" and not txn.has_hoa:
            continue
        gates.append(gate)
    return gates


def get_pending_gates(txn: Transaction) -> list[GateVerification]:
    """Return all gates not yet verified."""
    return [g for g in txn.gates if g.status != GateStatus.VERIFIED]


def get_phase_gates(txn: Transaction, phase: Phase) -> list[GateVerification]:
    """Return gates for a specific phase."""
    return [g for g in txn.gates if g.phase == phase]


def verify_gate(txn: Transaction, gate_id: str, notes: str = "") -> GateVerification | None:
    """Mark a gate as verified by the agent."""
    for gate in txn.gates:
        if gate.gate_id == gate_id:
            if gate.status == GateStatus.VERIFIED:
                return gate
            gate.status = GateStatus.VERIFIED
            gate.verified_at = datetime.now()
            gate.agent_notes = notes
            txn.updated_at = datetime.now()
            return gate
    return None


def trigger_gate_review(txn: Transaction, gate_id: str, review_copy_path: str,
                        items_to_verify: int = 0, red_items: int = 0) -> GateVerification | None:
    """Move a gate to awaiting_review state with review copy info."""
    for gate in txn.gates:
        if gate.gate_id == gate_id:
            gate.status = GateStatus.AWAITING_REVIEW
            gate.review_copy_path = review_copy_path
            gate.items_to_verify = items_to_verify
            gate.red_items = red_items
            txn.updated_at = datetime.now()
            return gate
    return None


def can_advance_phase(txn: Transaction) -> bool:
    """Check if all gates for the current phase are verified."""
    phase_gates = get_phase_gates(txn, txn.current_phase)
    return all(g.status == GateStatus.VERIFIED for g in phase_gates)


def advance_phase(txn: Transaction) -> Phase | None:
    """Advance to the next phase if all current gates are verified."""
    if not can_advance_phase(txn):
        return None
    phase_order = list(Phase)
    current_idx = phase_order.index(txn.current_phase)
    if current_idx < len(phase_order) - 1:
        txn.current_phase = phase_order[current_idx + 1]
        txn.updated_at = datetime.now()
        return txn.current_phase
    return None
