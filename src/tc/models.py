"""Core data models for transactions, gates, deadlines, and documents."""

from __future__ import annotations

import json
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Phase(str, Enum):
    PRE_CONTRACT = "PRE_CONTRACT"
    CONTRACT_EXECUTION = "CONTRACT_EXECUTION"
    INSPECTION = "INSPECTION"
    APPRAISAL = "APPRAISAL"
    FINANCING = "FINANCING"
    TITLE_ESCROW = "TITLE_ESCROW"
    PRE_CLOSING = "PRE_CLOSING"
    CLOSING = "CLOSING"
    POST_CLOSING = "POST_CLOSING"


class GateType(str, Enum):
    HARD_GATE = "HARD_GATE"
    SOFT_GATE = "SOFT_GATE"


class GateStatus(str, Enum):
    PENDING = "pending"
    AWAITING_REVIEW = "awaiting_review"
    VERIFIED = "verified"
    BLOCKED = "blocked"


class DeadlineType(str, Enum):
    REVIEWABLE = "REVIEWABLE"
    FIXED = "FIXED"


class DeadlineStatus(str, Enum):
    UPCOMING = "upcoming"
    DUE_SOON = "due_soon"  # within 3 days
    DUE_TODAY = "due_today"
    OVERDUE = "overdue"
    COMPLETED = "completed"


class HighlightColor(str, Enum):
    YELLOW = "YELLOW"    # verify value is correct
    ORANGE = "ORANGE"    # AI detected anomaly
    RED = "RED"          # critical / legal obligation
    BLUE = "BLUE"        # REVIEWABLE / contract-dependent
    GREEN = "GREEN"      # AI verified, no action needed
    PURPLE = "PURPLE"    # jurisdiction-specific


class DocumentStatus(str, Enum):
    PENDING = "pending"
    SENT_FOR_SIGNATURE = "sent_for_signature"
    SIGNED = "signed"
    VALIDATED = "validated"
    FILED = "filed"
    REJECTED = "rejected"


# ---------------------------------------------------------------------------
# Party / Stakeholder
# ---------------------------------------------------------------------------

class Party(BaseModel):
    name: str
    role: str  # buyer, seller, buyer_agent, listing_agent, lender, escrow, title
    email: str = ""
    phone: str = ""


# ---------------------------------------------------------------------------
# Deadline
# ---------------------------------------------------------------------------

class Deadline(BaseModel):
    id: str
    name: str
    deadline_type: DeadlineType
    date: date | None = None
    offset_days: int | None = None
    offset_from: str | None = None
    source: str = ""
    gate_id: str = ""
    status: DeadlineStatus = DeadlineStatus.UPCOMING
    completed_date: date | None = None
    notes: str = ""


# ---------------------------------------------------------------------------
# Gate Verification Record
# ---------------------------------------------------------------------------

class GateVerification(BaseModel):
    gate_id: str
    gate_name: str
    gate_type: GateType
    phase: Phase
    status: GateStatus = GateStatus.PENDING
    review_copy_path: str = ""
    items_to_verify: int = 0
    red_items: int = 0
    verified_at: datetime | None = None
    agent_notes: str = ""


# ---------------------------------------------------------------------------
# Highlight Annotation (for review overlay)
# ---------------------------------------------------------------------------

class HighlightAnnotation(BaseModel):
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    color: HighlightColor
    field_name: str = ""
    annotation_text: str = ""
    gate_id: str = ""
    legal_citation: str = ""
    action_needed: str = ""


# ---------------------------------------------------------------------------
# Document Record
# ---------------------------------------------------------------------------

class DocumentRecord(BaseModel):
    id: str = ""
    name: str
    doc_type: str  # RPA, TDS, SPQ, NHD, CR1, etc.
    phase: Phase
    file_path: str = ""
    docusign_envelope_id: str = ""
    status: DocumentStatus = DocumentStatus.PENDING
    received_date: date | None = None
    signed_date: date | None = None
    validation_passed: bool = False
    validation_errors: list[str] = Field(default_factory=list)
    filed_to_drive: bool = False
    uploaded_to_skyslope: bool = False


# ---------------------------------------------------------------------------
# Transaction (the master record)
# ---------------------------------------------------------------------------

class Transaction(BaseModel):
    id: str = ""
    address: str
    city: str = ""
    state: str = "CA"
    zip_code: str = ""
    apn: str = ""

    # Jurisdictions that apply
    jurisdictions: list[str] = Field(default_factory=lambda: ["california"])

    # Current phase
    current_phase: Phase = Phase.PRE_CONTRACT

    # Parties
    parties: list[Party] = Field(default_factory=list)

    # Contract terms (extracted from RPA)
    acceptance_date: date | None = None
    purchase_price: float | None = None
    deposit_amount: float | None = None
    loan_amount: float | None = None
    close_of_escrow: date | None = None

    # Contingency periods (REVIEWABLE — from contract)
    investigation_days: int | None = 17
    appraisal_days: int | None = 17
    loan_days: int | None = 17
    deposit_delivery_days: int | None = 3
    walkthrough_days_before_coe: int | None = 5

    # Property details
    property_type: str = ""  # SFR, condo, multi-unit, PUD
    year_built: int | None = None
    has_hoa: bool = False
    hoa_name: str = ""

    # Deadlines
    deadlines: list[Deadline] = Field(default_factory=list)

    # Gates
    gates: list[GateVerification] = Field(default_factory=list)

    # Documents
    documents: list[DocumentRecord] = Field(default_factory=list)

    # Google Drive
    drive_folder_id: str = ""
    private_review_folder_id: str = ""

    # Metadata
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    def save(self, data_dir: Path) -> None:
        """Persist transaction to disk."""
        txn_dir = data_dir / "transactions"
        txn_dir.mkdir(parents=True, exist_ok=True)
        file_path = txn_dir / f"{self.id}.json"
        file_path.write_text(self.model_dump_json(indent=2))

    @classmethod
    def load(cls, data_dir: Path, txn_id: str) -> Transaction:
        """Load transaction from disk."""
        file_path = data_dir / "transactions" / f"{txn_id}.json"
        return cls.model_validate_json(file_path.read_text())

    @classmethod
    def list_all(cls, data_dir: Path) -> list[Transaction]:
        """List all transactions."""
        txn_dir = data_dir / "transactions"
        if not txn_dir.exists():
            return []
        transactions = []
        for f in sorted(txn_dir.glob("*.json")):
            transactions.append(cls.model_validate_json(f.read_text()))
        return transactions


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

class Notification(BaseModel):
    title: str
    body: str
    priority: str = "normal"  # low, normal, high, urgent
    url: str = ""
    gate_id: str = ""
    transaction_id: str = ""
