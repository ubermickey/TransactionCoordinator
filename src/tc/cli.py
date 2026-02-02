"""Transaction Coordinator CLI.

Usage:
    tc new "123 Main St, Beverly Hills, CA 90210"
    tc extract document.pdf
    tc status
    tc deadlines
    tc gates
    tc gate review GATE-010
    tc gate verify GATE-010
    tc checklist
    tc taxes
    tc validate <envelope_id>
    tc notify "Custom message"
    tc email reminder <deadline_id>
    tc digest
"""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from tc.config import get_settings

app = typer.Typer(name="tc", help="AI-powered Transaction Coordinator for California real estate")
console = Console()

# Sub-command groups
gate_app = typer.Typer(help="Agent verification gates")
email_app = typer.Typer(help="Email operations")
app.add_typer(gate_app, name="gate")
app.add_typer(email_app, name="email")


# ---------------------------------------------------------------------------
# Helper: get current transaction
# ---------------------------------------------------------------------------

def _get_current_txn():
    """Load the most recently updated transaction, or prompt to select."""
    from tc.models import Transaction
    settings = get_settings()
    txns = Transaction.list_all(settings.data_path)
    if not txns:
        console.print("[red]No transactions found. Run 'tc new' first.[/red]")
        raise typer.Exit(1)
    # Return most recently updated
    return sorted(txns, key=lambda t: t.updated_at, reverse=True)[0]


# ---------------------------------------------------------------------------
# tc new
# ---------------------------------------------------------------------------

@app.command()
def new(address: str = typer.Argument(..., help="Property address")):
    """Create a new transaction."""
    from tc.engine.deadlines import calculate_deadlines
    from tc.engine.gates import initialize_gates
    from tc.models import Transaction

    settings = get_settings()
    txn = Transaction(
        id=uuid.uuid4().hex[:12],
        address=address,
    )

    # Detect city from address
    addr_lower = address.lower()
    if "beverly hills" in addr_lower:
        txn.city = "Beverly Hills"
        txn.jurisdictions = ["california", "los_angeles_county", "beverly_hills"]
    elif "los angeles" in addr_lower:
        txn.city = "Los Angeles"
        txn.jurisdictions = ["california", "los_angeles"]
    else:
        txn.jurisdictions = ["california"]

    # Initialize gates
    txn.gates = initialize_gates(txn, settings.workflow_dir)

    txn.save(settings.data_path)
    console.print(f"\n[green]Transaction created:[/green] {txn.id}")
    console.print(f"  Address: {txn.address}")
    console.print(f"  Jurisdictions: {', '.join(txn.jurisdictions)}")
    console.print(f"  Gates initialized: {len(txn.gates)}")
    console.print(f"\nNext: run [bold]tc extract <RPA.pdf>[/bold] to extract contract terms")

    # Create Google Drive folders if configured
    if settings.has_google():
        try:
            from tc.integrations.google_drive import (
                create_private_review_folders,
                create_transaction_folders,
            )
            year = str(date.today().year)
            console.print("\nCreating Google Drive folders...")
            txn_folders = create_transaction_folders(address, year)
            txn.drive_folder_id = txn_folders["root"]
            review_folders = create_private_review_folders(address, year)
            txn.private_review_folder_id = review_folders["root"]
            txn.save(settings.data_path)
            console.print("[green]  Transaction folder created[/green]")
            console.print("[green]  Private review folder created (agent-only)[/green]")
        except Exception as e:
            console.print(f"[yellow]  Google Drive setup skipped: {e}[/yellow]")


# ---------------------------------------------------------------------------
# tc extract
# ---------------------------------------------------------------------------

@app.command()
def extract(document: str = typer.Argument(..., help="Path to PDF document")):
    """Extract contract terms from a document using AI."""
    from tc.engine.deadlines import calculate_deadlines
    from tc.engine.extraction import apply_extraction_to_transaction, extract_from_pdf

    settings = get_settings()
    txn = _get_current_txn()

    console.print(f"\nExtracting terms from [bold]{document}[/bold]...")
    extraction = extract_from_pdf(document)

    doc_type = extraction.get("document_type", "Unknown")
    confidence = extraction.get("confidence", 0)
    console.print(f"  Document type: {doc_type} (confidence: {confidence:.0%})")

    changes = apply_extraction_to_transaction(txn, extraction)
    if changes:
        console.print("\n[green]Extracted terms:[/green]")
        for change in changes:
            console.print(f"  {change}")

    # Recalculate deadlines
    txn.deadlines = calculate_deadlines(txn)
    console.print(f"\n  Deadlines calculated: {len(txn.deadlines)}")

    # Show flags
    flags = extraction.get("flags", [])
    if flags:
        console.print(f"\n[yellow]Flags ({len(flags)}):[/yellow]")
        for flag in flags:
            severity = flag.get("severity", "yellow").upper()
            color = {"RED": "red", "ORANGE": "yellow", "YELLOW": "yellow"}.get(severity, "yellow")
            console.print(f"  [{color}][{severity}][/{color}] {flag.get('field', '?')}: {flag.get('issue', '')}")

    txn.save(settings.data_path)
    console.print(f"\n[green]Transaction {txn.id} updated.[/green]")


# ---------------------------------------------------------------------------
# tc status
# ---------------------------------------------------------------------------

@app.command()
def status():
    """Show current transaction status."""
    from tc.engine.deadlines import update_deadline_statuses
    from tc.models import DeadlineStatus, GateStatus

    txn = _get_current_txn()
    update_deadline_statuses(txn.deadlines)

    console.print(f"\n[bold]{txn.address}[/bold]")
    console.print(f"  ID: {txn.id}")
    console.print(f"  Phase: {txn.current_phase.value}")
    console.print(f"  Jurisdictions: {', '.join(txn.jurisdictions)}")

    if txn.purchase_price:
        console.print(f"  Price: ${txn.purchase_price:,.2f}")
    if txn.acceptance_date:
        console.print(f"  Acceptance: {txn.acceptance_date}")
    if txn.close_of_escrow:
        console.print(f"  COE: {txn.close_of_escrow}")

    # Gate summary
    verified = sum(1 for g in txn.gates if g.status == GateStatus.VERIFIED)
    awaiting = sum(1 for g in txn.gates if g.status == GateStatus.AWAITING_REVIEW)
    pending = sum(1 for g in txn.gates if g.status == GateStatus.PENDING)
    console.print(f"\n  Gates: {verified} verified, {awaiting} awaiting review, {pending} pending")

    # Upcoming deadlines
    upcoming = [d for d in txn.deadlines
                if d.status in (DeadlineStatus.DUE_SOON, DeadlineStatus.DUE_TODAY, DeadlineStatus.OVERDUE)]
    if upcoming:
        console.print(f"\n  [yellow]Urgent deadlines:[/yellow]")
        for dl in upcoming:
            color = {"due_soon": "yellow", "due_today": "red", "overdue": "red"}.get(dl.status.value, "white")
            console.print(f"    [{color}]{dl.status.value.upper()}: {dl.name} — {dl.date}[/{color}]")


# ---------------------------------------------------------------------------
# tc deadlines
# ---------------------------------------------------------------------------

@app.command()
def deadlines():
    """Show all deadlines for the current transaction."""
    from tc.engine.deadlines import update_deadline_statuses

    txn = _get_current_txn()
    update_deadline_statuses(txn.deadlines)

    table = Table(title=f"Deadlines — {txn.address}")
    table.add_column("ID", style="dim")
    table.add_column("Deadline")
    table.add_column("Date")
    table.add_column("Type")
    table.add_column("Status")
    table.add_column("Gate")

    for dl in sorted(txn.deadlines, key=lambda d: d.date or date.max):
        status_color = {
            "upcoming": "green",
            "due_soon": "yellow",
            "due_today": "red",
            "overdue": "red bold",
            "completed": "dim",
        }.get(dl.status.value, "white")

        type_color = "blue" if dl.deadline_type.value == "REVIEWABLE" else "dim"

        table.add_row(
            dl.id,
            dl.name,
            str(dl.date) if dl.date else "TBD",
            f"[{type_color}]{dl.deadline_type.value}[/{type_color}]",
            f"[{status_color}]{dl.status.value.upper()}[/{status_color}]",
            dl.gate_id,
        )

    console.print(table)


# ---------------------------------------------------------------------------
# tc gates
# ---------------------------------------------------------------------------

@app.command()
def gates():
    """Show all verification gates and their status."""
    txn = _get_current_txn()

    table = Table(title=f"Verification Gates — {txn.address}")
    table.add_column("Gate", style="bold")
    table.add_column("Name")
    table.add_column("Phase")
    table.add_column("Type")
    table.add_column("Status")
    table.add_column("Verified At")

    for g in txn.gates:
        status_color = {
            "pending": "dim",
            "awaiting_review": "yellow",
            "verified": "green",
            "blocked": "red",
        }.get(g.status.value, "white")

        type_style = "red bold" if g.gate_type.value == "HARD_GATE" else "yellow"

        table.add_row(
            g.gate_id,
            g.gate_name,
            g.phase.value,
            f"[{type_style}]{g.gate_type.value}[/{type_style}]",
            f"[{status_color}]{g.status.value.upper()}[/{status_color}]",
            str(g.verified_at.strftime("%Y-%m-%d %H:%M")) if g.verified_at else "",
        )

    console.print(table)


# ---------------------------------------------------------------------------
# tc gate review / tc gate verify
# ---------------------------------------------------------------------------

@gate_app.command("review")
def gate_review(gate_id: str = typer.Argument(..., help="Gate ID (e.g., GATE-010)")):
    """Generate a review copy for a gate and send notification."""
    from tc.engine.gates import trigger_gate_review
    from tc.integrations.notifications import notify_gate_review

    settings = get_settings()
    txn = _get_current_txn()

    gate = next((g for g in txn.gates if g.gate_id == gate_id), None)
    if not gate:
        console.print(f"[red]Gate {gate_id} not found.[/red]")
        raise typer.Exit(1)

    console.print(f"\nGenerating review copy for [bold]{gate_id}: {gate.gate_name}[/bold]...")

    # In a full implementation, this would:
    # 1. Find the relevant document for this gate
    # 2. Run the overlay generator with gate-specific highlights
    # 3. Upload to private review folder
    # 4. Send email + push notification

    review_path = f"review_{gate_id}_{txn.id}.pdf"
    trigger_gate_review(txn, gate_id, review_path, items_to_verify=0, red_items=0)
    txn.save(settings.data_path)

    console.print(f"  Status: [yellow]AWAITING REVIEW[/yellow]")

    # Send push notification
    if settings.has_pushover() or settings.has_ntfy():
        notify_gate_review(gate_id, gate.gate_name, txn.address, 0, 0)
        console.print("  [green]Push notification sent[/green]")

    # Send email notification
    if settings.has_google() and settings.agent_email:
        try:
            from tc.integrations.email_client import send_gate_review_notification
            send_gate_review_notification(
                gate_id=gate_id,
                gate_name=gate.gate_name,
                address=txn.address,
                items_to_verify=0,
                red_items=0,
                review_link="",
            )
            console.print("  [green]Email notification sent[/green]")
        except Exception as e:
            console.print(f"  [yellow]Email skipped: {e}[/yellow]")


@gate_app.command("verify")
def gate_verify(
    gate_id: str = typer.Argument(..., help="Gate ID (e.g., GATE-010)"),
    notes: str = typer.Option("", "--notes", "-n", help="Agent notes"),
):
    """Sign off on a gate as verified."""
    from tc.engine.gates import verify_gate

    settings = get_settings()
    txn = _get_current_txn()

    gate = verify_gate(txn, gate_id, notes)
    if not gate:
        console.print(f"[red]Gate {gate_id} not found.[/red]")
        raise typer.Exit(1)

    txn.save(settings.data_path)
    console.print(f"\n[green]✓ {gate_id}: {gate.gate_name} — VERIFIED[/green]")
    if notes:
        console.print(f"  Notes: {notes}")
    console.print(f"  Verified at: {gate.verified_at}")

    # Check if phase can advance
    from tc.engine.gates import can_advance_phase
    if can_advance_phase(txn):
        console.print(f"\n  [green]All gates for {txn.current_phase.value} are verified.[/green]")
        console.print(f"  Run [bold]tc advance[/bold] to move to the next phase.")


# ---------------------------------------------------------------------------
# tc checklist
# ---------------------------------------------------------------------------

@app.command()
def checklist():
    """Show jurisdiction compliance checklist."""
    from tc.jurisdictions.loader import generate_checklist

    settings = get_settings()
    txn = _get_current_txn()

    cl = generate_checklist(txn.address, txn.jurisdictions, settings.jurisdictions_path)

    table = Table(title=f"Compliance Checklist — {txn.address}")
    table.add_column("ID", style="dim")
    table.add_column("Jurisdiction", style="bold")
    table.add_column("Requirement")
    table.add_column("Citation", style="dim")
    table.add_column("Gate")
    table.add_column("Status")

    for item in cl.items:
        status = "[green]✓[/green]" if item.completed else "[red]☐[/red]"
        table.add_row(
            item.rule_id,
            item.jurisdiction,
            item.name,
            item.citation,
            item.gate_id,
            status,
        )

    console.print(table)
    console.print(f"\n  Total: {cl.total} | Completed: {cl.completed_count} | Pending: {cl.pending_count}")


# ---------------------------------------------------------------------------
# tc taxes
# ---------------------------------------------------------------------------

@app.command()
def taxes():
    """Calculate transfer taxes for the current transaction."""
    from tc.jurisdictions.loader import calculate_transfer_taxes

    settings = get_settings()
    txn = _get_current_txn()

    if not txn.purchase_price:
        console.print("[red]No purchase price set. Run 'tc extract' first.[/red]")
        raise typer.Exit(1)

    result = calculate_transfer_taxes(txn.purchase_price, txn.jurisdictions,
                                      settings.jurisdictions_path)

    table = Table(title=f"Transfer Taxes — {txn.address} (${txn.purchase_price:,.2f})")
    table.add_column("Tax")
    table.add_column("Amount", justify="right")

    for name, amount in result.items():
        style = "bold" if name == "TOTAL" else ""
        table.add_row(name, f"${amount:,.2f}", style=style)

    console.print(table)


# ---------------------------------------------------------------------------
# tc validate
# ---------------------------------------------------------------------------

@app.command()
def validate(envelope_id: str = typer.Argument(..., help="DocuSign envelope ID")):
    """Validate a DocuSign envelope (signature completeness check)."""
    from tc.integrations.docusign_client import validate_envelope

    report = validate_envelope(envelope_id)

    console.print(f"\nValidation Report: {report.document_name}")
    console.print(f"Envelope: {report.envelope_id}")

    for result in report.results:
        icon = "[green]✓[/green]" if result.passed else "[red]✗[/red]"
        console.print(f"  {icon} [{result.severity}] {result.name}")
        if result.details and not result.passed:
            console.print(f"      {result.details}")

    if report.all_passed:
        console.print(f"\n[green]All checks passed.[/green]")
    else:
        console.print(f"\n[red]FAILED: {report.critical_failures} critical, {report.warnings} warnings[/red]")


# ---------------------------------------------------------------------------
# tc notify
# ---------------------------------------------------------------------------

@app.command()
def notify(message: str = typer.Argument(..., help="Message to send")):
    """Send a push notification to your phone."""
    from tc.integrations.notifications import send_push
    from tc.models import Notification

    txn = _get_current_txn()
    sent = send_push(Notification(
        title=txn.address,
        body=message,
        priority="normal",
    ))

    if sent:
        console.print("[green]Push notification sent.[/green]")
    else:
        console.print("[red]No notification providers configured. Check .env[/red]")


# ---------------------------------------------------------------------------
# tc email reminder
# ---------------------------------------------------------------------------

@email_app.command("reminder")
def email_reminder(deadline_id: str = typer.Argument(..., help="Deadline ID (e.g., DL-010)")):
    """Send a deadline reminder email."""
    from tc.integrations.email_client import send_deadline_reminder

    txn = _get_current_txn()
    dl = next((d for d in txn.deadlines if d.id == deadline_id), None)
    if not dl:
        console.print(f"[red]Deadline {deadline_id} not found.[/red]")
        raise typer.Exit(1)

    days = (dl.date - date.today()).days if dl.date else 0
    send_deadline_reminder(
        address=txn.address,
        deadline_name=dl.name,
        deadline_date=str(dl.date),
        days_remaining=days,
    )
    console.print(f"[green]Reminder sent for {dl.name} ({dl.date})[/green]")


@email_app.command("send")
def email_send(
    to: str = typer.Argument(..., help="Recipient email"),
    subject: str = typer.Option(..., "--subject", "-s"),
    body: str = typer.Option(..., "--body", "-b"),
    from_alias: str = typer.Option(None, "--from", help="Send-as alias email"),
):
    """Send a custom email from your alias."""
    from tc.integrations.email_client import send_email

    msg_id = send_email(
        to=to,
        subject=subject,
        body_html=f"<html><body><p>{body}</p></body></html>",
        body_text=body,
        from_alias=from_alias,
    )
    console.print(f"[green]Email sent (ID: {msg_id})[/green]")


# ---------------------------------------------------------------------------
# tc digest
# ---------------------------------------------------------------------------

@app.command()
def digest():
    """Generate and send a daily digest of all active transactions."""
    from tc.engine.deadlines import get_reminders_due, update_deadline_statuses
    from tc.models import DeadlineStatus, GateStatus, Transaction

    settings = get_settings()
    txns = Transaction.list_all(settings.data_path)

    if not txns:
        console.print("[dim]No active transactions.[/dim]")
        return

    console.print(f"\n[bold]Daily Digest — {date.today()}[/bold]\n")

    all_pending_gates: list[tuple[str, str, str]] = []
    all_urgent_deadlines: list[tuple[str, str, str, str]] = []

    for txn in txns:
        update_deadline_statuses(txn.deadlines)

        pending = [g for g in txn.gates if g.status in (GateStatus.AWAITING_REVIEW, GateStatus.PENDING)]
        urgent = [d for d in txn.deadlines
                  if d.status in (DeadlineStatus.DUE_SOON, DeadlineStatus.DUE_TODAY, DeadlineStatus.OVERDUE)]

        for g in pending:
            all_pending_gates.append((txn.address, g.gate_id, g.gate_name))
        for d in urgent:
            all_urgent_deadlines.append((txn.address, d.name, str(d.date), d.status.value))

        console.print(f"  [bold]{txn.address}[/bold] — {txn.current_phase.value}")
        console.print(f"    Pending gates: {len(pending)} | Urgent deadlines: {len(urgent)}")

    # Send email digest
    if settings.has_google() and settings.agent_email and (all_pending_gates or all_urgent_deadlines):
        try:
            from tc.integrations.email_client import send_email

            gates_html = ""
            if all_pending_gates:
                gates_html = "<h3>Pending Gates</h3><ul>"
                for addr, gid, gname in all_pending_gates:
                    gates_html += f"<li><b>{addr}</b> — {gid}: {gname}</li>"
                gates_html += "</ul>"

            deadlines_html = ""
            if all_urgent_deadlines:
                deadlines_html = "<h3>Urgent Deadlines</h3><ul>"
                for addr, dname, ddate, dstatus in all_urgent_deadlines:
                    color = "red" if "overdue" in dstatus else "orange"
                    deadlines_html += f'<li style="color:{color}"><b>{addr}</b> — {dname} ({ddate}) [{dstatus.upper()}]</li>'
                deadlines_html += "</ul>"

            send_email(
                to=settings.agent_email,
                subject=f"TC Daily Digest — {date.today()} — {len(txns)} active transactions",
                body_html=f"<html><body><h2>Daily Digest — {date.today()}</h2>{gates_html}{deadlines_html}</body></html>",
            )
            console.print("\n[green]Digest email sent.[/green]")
        except Exception as e:
            console.print(f"\n[yellow]Email digest skipped: {e}[/yellow]")

    # Send push summary
    if settings.has_pushover() or settings.has_ntfy():
        from tc.integrations.notifications import send_push
        from tc.models import Notification

        body = f"{len(txns)} active transactions"
        if all_pending_gates:
            body += f"\n{len(all_pending_gates)} gates awaiting review"
        if all_urgent_deadlines:
            body += f"\n{len(all_urgent_deadlines)} urgent deadlines"

        send_push(Notification(
            title=f"TC Digest — {date.today()}",
            body=body,
            priority="normal",
        ))
        console.print("[green]Push digest sent.[/green]")


# ---------------------------------------------------------------------------
# tc advance
# ---------------------------------------------------------------------------

@app.command()
def advance():
    """Advance to the next transaction phase (if all gates verified)."""
    from tc.engine.gates import advance_phase

    settings = get_settings()
    txn = _get_current_txn()

    new_phase = advance_phase(txn)
    if new_phase:
        txn.save(settings.data_path)
        console.print(f"\n[green]Advanced to: {new_phase.value}[/green]")
    else:
        console.print(f"\n[red]Cannot advance — not all gates in {txn.current_phase.value} are verified.[/red]")
        from tc.engine.gates import get_phase_gates
        from tc.models import GateStatus
        pending = [g for g in get_phase_gates(txn, txn.current_phase) if g.status != GateStatus.VERIFIED]
        for g in pending:
            console.print(f"  [yellow]☐ {g.gate_id}: {g.gate_name} — {g.status.value}[/yellow]")


# ---------------------------------------------------------------------------
# tc list
# ---------------------------------------------------------------------------

@app.command("list")
def list_transactions():
    """List all transactions."""
    from tc.models import Transaction

    settings = get_settings()
    txns = Transaction.list_all(settings.data_path)

    if not txns:
        console.print("[dim]No transactions. Run 'tc new' to create one.[/dim]")
        return

    table = Table(title="Transactions")
    table.add_column("ID", style="dim")
    table.add_column("Address")
    table.add_column("Phase")
    table.add_column("Price", justify="right")
    table.add_column("COE")

    for txn in txns:
        table.add_row(
            txn.id,
            txn.address,
            txn.current_phase.value,
            f"${txn.purchase_price:,.0f}" if txn.purchase_price else "",
            str(txn.close_of_escrow) if txn.close_of_escrow else "",
        )

    console.print(table)


if __name__ == "__main__":
    app()
