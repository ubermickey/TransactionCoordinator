"""Deadline calculation engine.

Computes all REVIEWABLE and FIXED deadlines from contract terms and
jurisdiction rules. Handles amendments, cascading changes, and status updates.
"""

from __future__ import annotations

from datetime import date, timedelta

from tc.models import Deadline, DeadlineStatus, DeadlineType, Transaction


# US federal holidays (approximate — production should use a holiday library)
_FEDERAL_HOLIDAYS_2026 = {
    date(2026, 1, 1),   # New Year's
    date(2026, 1, 19),  # MLK Day
    date(2026, 2, 16),  # Presidents Day
    date(2026, 3, 31),  # César Chávez Day (CA)
    date(2026, 5, 25),  # Memorial Day
    date(2026, 7, 3),   # Independence Day (observed)
    date(2026, 9, 7),   # Labor Day
    date(2026, 10, 12), # Columbus Day
    date(2026, 11, 11), # Veterans Day
    date(2026, 11, 26), # Thanksgiving
    date(2026, 12, 25), # Christmas
}


def add_business_days(start: date, days: int) -> date:
    """Add business days (skip weekends and federal holidays)."""
    current = start
    added = 0
    while added < days:
        current += timedelta(days=1)
        if current.weekday() < 5 and current not in _FEDERAL_HOLIDAYS_2026:
            added += 1
    return current


def add_calendar_days(start: date, days: int) -> date:
    """Add calendar days."""
    return start + timedelta(days=days)


def subtract_business_days(end: date, days: int) -> date:
    """Subtract business days from a date."""
    current = end
    subtracted = 0
    while subtracted < days:
        current -= timedelta(days=1)
        if current.weekday() < 5 and current not in _FEDERAL_HOLIDAYS_2026:
            subtracted += 1
    return current


def calculate_deadlines(txn: Transaction) -> list[Deadline]:
    """Calculate all deadlines for a transaction from its contract terms.

    REVIEWABLE deadlines come from the contract (txn fields).
    FIXED deadlines come from statute and are the same for every deal.
    """
    if not txn.acceptance_date:
        return []

    accept = txn.acceptance_date
    deadlines: list[Deadline] = []

    # --- Earnest Money Deposit ---
    if txn.deposit_delivery_days is not None:
        deadlines.append(Deadline(
            id="DL-001",
            name="Earnest Money Deposit Delivery",
            deadline_type=DeadlineType.REVIEWABLE,
            date=add_business_days(accept, txn.deposit_delivery_days),
            offset_days=txn.deposit_delivery_days,
            offset_from="acceptance_date",
            source="RPA Section 3A",
            gate_id="GATE-011",
        ))

    # --- Investigation Contingency ---
    if txn.investigation_days is not None:
        deadlines.append(Deadline(
            id="DL-010",
            name="Investigation Contingency Deadline",
            deadline_type=DeadlineType.REVIEWABLE,
            date=add_calendar_days(accept, txn.investigation_days),
            offset_days=txn.investigation_days,
            offset_from="acceptance_date",
            source="RPA Section 14B(1)",
            gate_id="GATE-022",
        ))

    # --- Appraisal Contingency ---
    if txn.appraisal_days is not None:
        deadlines.append(Deadline(
            id="DL-020",
            name="Appraisal Contingency Deadline",
            deadline_type=DeadlineType.REVIEWABLE,
            date=add_calendar_days(accept, txn.appraisal_days),
            offset_days=txn.appraisal_days,
            offset_from="acceptance_date",
            source="RPA Section 14B(2)",
            gate_id="GATE-031",
        ))

    # --- Loan Contingency ---
    if txn.loan_days is not None:
        deadlines.append(Deadline(
            id="DL-030",
            name="Loan Contingency Deadline",
            deadline_type=DeadlineType.REVIEWABLE,
            date=add_calendar_days(accept, txn.loan_days),
            offset_days=txn.loan_days,
            offset_from="acceptance_date",
            source="RPA Section 14B(3)",
            gate_id="GATE-041",
        ))

    # --- Close of Escrow dependent deadlines ---
    if txn.close_of_escrow:
        coe = txn.close_of_escrow

        # Closing Disclosure (FIXED — 3 business days before COE)
        deadlines.append(Deadline(
            id="DL-050",
            name="Closing Disclosure Delivery Deadline",
            deadline_type=DeadlineType.FIXED,
            date=subtract_business_days(coe, 3),
            source="TRID (TILA-RESPA Integrated Disclosure Rule)",
            gate_id="GATE-061",
        ))

        # Final Walkthrough (REVIEWABLE)
        wt_days = txn.walkthrough_days_before_coe or 5
        deadlines.append(Deadline(
            id="DL-051",
            name="Final Walkthrough",
            deadline_type=DeadlineType.REVIEWABLE,
            date=add_calendar_days(coe, -wt_days),
            offset_days=-wt_days,
            offset_from="close_of_escrow",
            source="RPA Section 16",
            gate_id="GATE-062",
        ))

        # Close of Escrow
        deadlines.append(Deadline(
            id="DL-060",
            name="Close of Escrow",
            deadline_type=DeadlineType.REVIEWABLE,
            date=coe,
            source="RPA Section 1C",
            gate_id="GATE-071",
        ))

        # File Retention (FIXED — 3 years from COE)
        deadlines.append(Deadline(
            id="DL-070",
            name="File Retention Expiry",
            deadline_type=DeadlineType.FIXED,
            date=date(coe.year + 3, coe.month, coe.day),
            source="Business & Professions Code §10148",
            gate_id="GATE-080",
        ))

    return deadlines


def update_deadline_statuses(deadlines: list[Deadline], today: date | None = None) -> None:
    """Update status of each deadline based on current date."""
    today = today or date.today()
    for dl in deadlines:
        if dl.status == DeadlineStatus.COMPLETED:
            continue
        if dl.date is None:
            continue
        days_until = (dl.date - today).days
        if days_until < 0:
            dl.status = DeadlineStatus.OVERDUE
        elif days_until == 0:
            dl.status = DeadlineStatus.DUE_TODAY
        elif days_until <= 3:
            dl.status = DeadlineStatus.DUE_SOON
        else:
            dl.status = DeadlineStatus.UPCOMING


def get_reminders_due(deadlines: list[Deadline], today: date | None = None) -> list[Deadline]:
    """Return deadlines that need reminders sent today."""
    today = today or date.today()
    reminder_days = {7, 5, 3, 2, 1, 0}
    due: list[Deadline] = []
    for dl in deadlines:
        if dl.status == DeadlineStatus.COMPLETED or dl.date is None:
            continue
        days_until = (dl.date - today).days
        if days_until in reminder_days or days_until < 0:
            due.append(dl)
    return due
