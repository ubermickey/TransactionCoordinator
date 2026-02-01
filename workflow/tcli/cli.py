"""TC command-line interface."""
import json
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import db, engine, notify, overlay, rules

app = typer.Typer(help="Real estate transaction coordinator", no_args_is_help=True)
con = Console()


def _tid(txn_id: str | None) -> str:
    if txn_id:
        return txn_id
    with db.conn() as c:
        t = db.active(c)
    if not t:
        con.print("[red]No transactions. Run:[/] tc new <address>")
        raise typer.Exit(1)
    return t["id"]


# ── Core ─────────────────────────────────────────────────────────────────────

@app.command()
def new(address: str):
    """Create a new transaction."""
    tid = uuid4().hex[:8]
    city = address.split(",")[1].strip() if "," in address else ""
    juris = rules.resolve(city)
    with db.conn() as c:
        c.execute("INSERT INTO txns(id,address,jurisdictions) VALUES(?,?,?)", (tid, address, json.dumps(juris)))
        db.log(c, tid, "created", address)
    engine.init_gates(tid)
    con.print(f"[green]Created[/] {tid} — {address}")
    con.print(f"Jurisdictions: {', '.join(juris)}")


@app.command()
def extract(pdf: Path, form: str = typer.Option(None, "--form", help="CAR form type (rpa, tds, cr1)"), txn_id: str = typer.Option(None, "--txn")):
    """Extract contract terms from a PDF via Claude."""
    tid = _tid(txn_id)
    con.print(f"[yellow]Sending to Claude{f' (using {form} template)' if form else ''}...[/]")
    data = engine.extract(str(pdf), form_type=form)
    with db.conn() as c:
        c.execute("UPDATE txns SET data=?, updated=datetime('now','localtime') WHERE id=?", (json.dumps(data), tid))
    with db.conn() as c:
        db.log(c, tid, "extracted", f"form={form or 'auto'}")
    anchor = (data.get("dates") or {}).get("acceptance")
    if anchor:
        engine.calc_deadlines(tid, date.fromisoformat(anchor), data)
    con.print("[green]Extracted and deadlines calculated.[/]")
    for section, vals in data.items():
        if isinstance(vals, dict):
            for k, v in vals.items():
                con.print(f"  {k}: {v}")


@app.command()
def status(txn_id: str = typer.Option(None, "--txn")):
    """Transaction dashboard."""
    tid = _tid(txn_id)
    with db.conn() as c:
        t = db.txn(c, tid)
    gs = engine.gate_rows(tid)
    dls = engine.deadline_rows(tid)
    data = json.loads(t["data"])
    v = sum(1 for g in gs if g["status"] == "verified")
    today = date.today()

    lines = [f"[bold]{t['address']}[/]", f"Phase: {t['phase']}  |  ID: {tid}"]
    if p := (data.get("parties") or {}):
        lines.append(f"Buyer: {p.get('buyer','?')}  |  Seller: {p.get('seller','?')}")
    if f := (data.get("financial") or {}):
        if pr := f.get("purchase_price"):
            lines.append(f"Price: ${pr:,.0f}")
    lines.append(f"Gates: {v}/{len(gs)} verified  |  Deadlines: {len(dls)} tracked")

    # Next 3 deadlines
    upcoming = []
    for d in dls:
        if not d["due"]:
            continue
        due = date.fromisoformat(d["due"])
        delta = (due - today).days
        if delta >= 0:
            upcoming.append((d["name"], d["due"], delta))
    if upcoming:
        lines.append("")
        for name, due_str, delta in upcoming[:3]:
            color = "red" if delta <= 1 else "yellow" if delta <= 5 else "dim"
            lines.append(f"  [{color}]{due_str}[/] {name} ({delta}d)")

    # Next pending gate
    for g in gs:
        if g["status"] == "pending":
            info = rules.gate(g["gid"])
            if info:
                lines.append(f"\nNext gate: [bold]{g['gid']}[/] {info['name']}")
                break

    con.print(Panel("\n".join(lines), title="Status"))


# ── Deadlines ────────────────────────────────────────────────────────────────

@app.command()
def deadlines(txn_id: str = typer.Option(None, "--txn")):
    """Show all deadlines."""
    tid = _tid(txn_id)
    tbl = Table(title="Deadlines")
    tbl.add_column("ID")
    tbl.add_column("Name")
    tbl.add_column("Type")
    tbl.add_column("Due")
    tbl.add_column("Days")
    today = date.today()
    for d in engine.deadline_rows(tid):
        due = date.fromisoformat(d["due"]) if d["due"] else None
        delta = (due - today).days if due else None
        style = "red" if delta is not None and delta < 0 else "yellow" if delta is not None and delta <= 3 else ""
        days_str = str(delta) if delta is not None else "—"
        tbl.add_row(d["did"], d["name"], d["type"], d["due"] or "—", days_str, style=style)
    con.print(tbl)


# ── Gates ────────────────────────────────────────────────────────────────────

@app.command()
def gates(txn_id: str = typer.Option(None, "--txn")):
    """Show verification gates."""
    tid = _tid(txn_id)
    tbl = Table(title="Agent Verification Gates")
    tbl.add_column("Gate")
    tbl.add_column("Name")
    tbl.add_column("Type")
    tbl.add_column("Status")
    tbl.add_column("Verified")
    for g in engine.gate_rows(tid):
        info = rules.gate(g["gid"])
        style = "green" if g["status"] == "verified" else "dim"
        tbl.add_row(
            g["gid"],
            info["name"] if info else "?",
            info["type"] if info else "?",
            g["status"],
            g["verified"] or "—",
            style=style,
        )
    con.print(tbl)


@app.command()
def info(gate_id: str):
    """Show full details for a verification gate (no sign-off)."""
    g = rules.gate(gate_id)
    if not g:
        con.print(f"[red]Unknown gate: {gate_id}[/]")
        raise typer.Exit(1)
    con.print(f"\n[bold]{g['id']} — {g['name']}[/]  ({g['type']})")
    con.print(f"\n[red]Legal basis:[/] {g['legal_basis']['statute']}")
    con.print(f"[red]Obligation:[/] {g['legal_basis']['obligation']}")
    con.print(f"[red]Liability:[/]  {g['legal_basis']['liability']}")
    con.print(f"\n[yellow]What you verify:[/]")
    for item in g.get("what_agent_verifies", []):
        con.print(f"  \u2610 {item}")
    con.print(f"\n[green]AI prepares:[/]")
    for item in g.get("ai_prepares", []):
        con.print(f"  - {item}")
    con.print(f"\n[dim]Cannot proceed until: {g.get('cannot_proceed_until', '?')}[/]")
    if g.get("notes"):
        con.print(f"\n[bold]Note:[/] {g['notes']}")


@app.command()
def verify(gate_id: str, notes: str = "", txn_id: str = typer.Option(None, "--txn")):
    """Sign off on a verification gate."""
    tid = _tid(txn_id)
    info = rules.gate(gate_id)
    if not info:
        con.print(f"[red]Unknown gate: {gate_id}[/]")
        raise typer.Exit(1)
    con.print(f"\n[bold]{info['name']}[/]")
    con.print(f"Legal: {info['legal_basis']['statute']}")
    con.print(f"Liability: {info['legal_basis']['liability']}\n")
    for item in info.get("what_agent_verifies", []):
        con.print(f"  \u2610 {item}")
    if typer.confirm("\nI verify all items above are confirmed"):
        engine.verify(tid, gate_id, notes)
        con.print(f"[green]\u2713 {gate_id} verified[/]")
        notify.alert(f"Gate {gate_id} verified", info["name"])
    else:
        con.print("[yellow]Cancelled[/]")


# ── Review ───────────────────────────────────────────────────────────────────

@app.command()
def review(gate_id: str, pdf: Path, txn_id: str = typer.Option(None, "--txn")):
    """Generate an agent-only review copy for a gate."""
    tid = _tid(txn_id)
    out_dir = db.DB.parent / "reviews" / tid
    path = overlay.review_copy(str(pdf), gate_id, out_dir)
    con.print(f"[green]Review copy:[/] {path}")
    notify.alert(f"Review ready: {gate_id}", str(path))


# ── Jurisdiction ─────────────────────────────────────────────────────────────

@app.command()
def taxes(txn_id: str = typer.Option(None, "--txn")):
    """Calculate transfer taxes."""
    tid = _tid(txn_id)
    with db.conn() as c:
        t = db.txn(c, tid)
    data = json.loads(t["data"])
    price = (data.get("financial") or {}).get("purchase_price") or typer.prompt("Purchase price", type=float)
    juris = json.loads(t["jurisdictions"])
    tbl = Table(title=f"Transfer Taxes — ${price:,.0f}")
    tbl.add_column("Tax")
    tbl.add_column("Amount", justify="right")
    total = 0.0
    for name, amt in rules.calc_taxes(float(price), juris):
        tbl.add_row(name, f"${amt:,.2f}")
        total += amt
    tbl.add_row("[bold]Total[/]", f"[bold]${total:,.2f}[/]")
    con.print(tbl)


@app.command()
def checklist(txn_id: str = typer.Option(None, "--txn")):
    """Show jurisdiction compliance checklist."""
    tid = _tid(txn_id)
    with db.conn() as c:
        t = db.txn(c, tid)
    for name in json.loads(t["jurisdictions"]):
        j = rules.jurisdiction(name)
        con.print(f"\n[bold]{j['jurisdiction']['name']}[/]")
        for section in ("required_forms", "retrofit_requirements", "compliance_requirements", "hoa_rules"):
            for item in j.get(section, []):
                con.print(f"  \u2610 {item['name']}  [dim]{item.get('citation', '')}[/]")


# ── Notifications ────────────────────────────────────────────────────────────

@app.command(name="push")
def push_msg(message: str, title: str = "TC Alert"):
    """Send a push notification."""
    notify.push(title, message)
    con.print("[green]Sent.[/]")


@app.command(name="email")
def send_email(to: str, subject: str, body: str):
    """Send an email from your alias."""
    notify.email(to, subject, body)
    con.print("[green]Sent.[/]")


# ── Digest ───────────────────────────────────────────────────────────────────

@app.command()
def digest():
    """Daily digest — upcoming deadlines and pending gates across all transactions."""
    today = date.today()
    urgent, upcoming, pending_gates = [], [], []
    with db.conn() as c:
        for row in c.execute("SELECT * FROM txns ORDER BY created DESC"):
            t = dict(row)
            for d in engine.deadline_rows(t["id"]):
                if not d["due"]:
                    continue
                due = date.fromisoformat(d["due"])
                delta = (due - today).days
                if delta < 0:
                    urgent.append((t["address"], d["name"], f"OVERDUE by {-delta}d"))
                elif delta <= 3:
                    urgent.append((t["address"], d["name"], f"In {delta}d"))
                elif delta <= 14:
                    upcoming.append((t["address"], d["name"], f"In {delta}d"))
            for g in engine.gate_rows(t["id"]):
                if g["status"] == "pending":
                    info = rules.gate(g["gid"])
                    pending_gates.append((t["address"], g["gid"], info["name"] if info else "?"))

    if urgent:
        tbl = Table(title="URGENT", style="red")
        tbl.add_column("Transaction"); tbl.add_column("Deadline"); tbl.add_column("When")
        for row in urgent:
            tbl.add_row(*row)
        con.print(tbl)
    if upcoming:
        tbl = Table(title="Upcoming (next 14 days)")
        tbl.add_column("Transaction"); tbl.add_column("Deadline"); tbl.add_column("When")
        for row in upcoming:
            tbl.add_row(*row)
        con.print(tbl)
    if pending_gates:
        tbl = Table(title=f"Pending Gates ({len(pending_gates)})")
        tbl.add_column("Transaction"); tbl.add_column("Gate"); tbl.add_column("Name")
        for row in pending_gates[:10]:
            tbl.add_row(*row)
        if len(pending_gates) > 10:
            con.print(f"  [dim]...and {len(pending_gates) - 10} more[/]")
        con.print(tbl)
    if not urgent and not upcoming:
        con.print("[green]All clear — no urgent deadlines.[/]")

    # Send push summary if configured
    if urgent:
        notify.alert(
            f"TC: {len(urgent)} urgent deadline(s)",
            "\n".join(f"{a}: {n} ({w})" for a, n, w in urgent[:5]),
            priority=1,
        )


# ── Phase Advancement ────────────────────────────────────────────────────────

@app.command()
def advance(txn_id: str = typer.Option(None, "--txn")):
    """Advance to next phase (if all HARD_GATE gates are verified)."""
    tid = _tid(txn_id)
    ok, blocking = engine.can_advance(tid)
    if not ok:
        con.print("[red]Cannot advance. Blocking gates:[/]")
        for b in blocking:
            con.print(f"  {b}")
        return
    new = engine.advance_phase(tid)
    if new:
        con.print(f"[green]Advanced to {new}[/]")
        notify.alert("Phase advanced", new)
    else:
        con.print("[dim]Already at final phase.[/]")


# ── Forms ────────────────────────────────────────────────────────────────────

@app.command()
def forms():
    """List available CAR form templates."""
    tbl = Table(title="CAR Form Templates")
    tbl.add_column("Code")
    tbl.add_column("Name")
    tbl.add_column("Version")
    tbl.add_column("Fields")
    for t in rules.form_templates():
        f = t["form"]
        tbl.add_row(f["code"], f["name"], f.get("version", "?"), str(len(t.get("fields", {}))))
    con.print(tbl)
    con.print("[dim]Use --form CODE with extract to use a template.[/]")


@app.command(name="form-diff")
def form_diff(form_file: Path):
    """Show fields in a form template (for reviewing updates)."""
    import yaml
    t = yaml.safe_load(form_file.read_text())
    f = t["form"]
    con.print(f"[bold]{f['code']} — {f['name']}[/]  v{f.get('version','?')}")
    con.print(f"Last verified: {f.get('last_verified','?')}\n")
    for fid, field in t.get("fields", {}).items():
        req = " [red]*[/]" if field.get("required") else ""
        rev = " [blue]REVIEWABLE[/]" if field.get("reviewable") else ""
        con.print(f"  {field.get('section','?'):8s} {field.get('label', fid)}{req}{rev}")
        con.print(f"           -> {field.get('maps_to', '?')}  [dim]({field.get('type','text')})[/]")
    if t.get("flags"):
        con.print("\n[yellow]Flags:[/]")
        for flag in t["flags"]:
            con.print(f"  - {flag}")


# ── List & Delete ────────────────────────────────────────────────────────────

@app.command(name="list")
def list_txns():
    """List all transactions."""
    with db.conn() as c:
        rows = c.execute("SELECT * FROM txns ORDER BY created DESC").fetchall()
    if not rows:
        con.print("[dim]No transactions.[/]")
        return
    tbl = Table(title="Transactions")
    tbl.add_column("ID")
    tbl.add_column("Address")
    tbl.add_column("Phase")
    tbl.add_column("Gates", justify="right")
    tbl.add_column("Created")
    for r in rows:
        gs = engine.gate_rows(r["id"])
        v = sum(1 for g in gs if g["status"] == "verified")
        tbl.add_row(r["id"], r["address"], r["phase"], f"{v}/{len(gs)}", r["created"])
    con.print(tbl)


@app.command()
def delete(txn_id: str):
    """Delete a transaction and its gates/deadlines."""
    with db.conn() as c:
        t = db.txn(c, txn_id)
    if not t:
        con.print(f"[red]Not found: {txn_id}[/]")
        raise typer.Exit(1)
    if typer.confirm(f"Delete {t['address']} ({txn_id})?"):
        with db.conn() as c:
            db.log(c, txn_id, "deleted", t["address"])
            c.execute("DELETE FROM deadlines WHERE txn=?", (txn_id,))
            c.execute("DELETE FROM gates WHERE txn=?", (txn_id,))
            c.execute("DELETE FROM txns WHERE id=?", (txn_id,))
        con.print(f"[green]Deleted {txn_id}[/]")


# ── Timeline ─────────────────────────────────────────────────────────────────

@app.command()
def timeline(txn_id: str = typer.Option(None, "--txn"), weeks: int = 8):
    """Visual deadline timeline."""
    tid = _tid(txn_id)
    dls = engine.deadline_rows(tid)
    today = date.today()
    end = today + timedelta(weeks=weeks)
    con.print(f"\n[bold]Timeline[/]  {today} → {end}  ({weeks} weeks)\n")
    bar_width = 60
    total_days = (end - today).days or 1
    for d in dls:
        if not d["due"]:
            continue
        due = date.fromisoformat(d["due"])
        if due < today - timedelta(days=7) or due > end:
            continue
        offset = max(0, min(bar_width, int((due - today).days / total_days * bar_width)))
        delta = (due - today).days
        color = "red" if delta < 0 else "red" if delta <= 1 else "yellow" if delta <= 5 else "green"
        marker = "│" if delta >= 0 else "X"
        bar = "─" * offset + f"[{color}]{marker}[/{color}]" + "─" * (bar_width - offset)
        label = d["name"][:28]
        con.print(f"  {d['due']}  {bar}  [{color}]{label}[/{color}]  ({delta}d)")
    con.print(f"\n  {'TODAY':>10}  │{'':>{bar_width}}│")
    con.print(f"  {'':>10}  {today!s}{'':>{bar_width - 10}}{end!s}")


# ── Export ───────────────────────────────────────────────────────────────────

@app.command()
def export(txn_id: str = typer.Option(None, "--txn"), out: Path = typer.Option(None, "--out")):
    """Export transaction as JSON (backup/integration)."""
    tid = _tid(txn_id)
    with db.conn() as c:
        t = db.txn(c, tid)
    payload = {
        "transaction": {**t, "data": json.loads(t["data"]), "jurisdictions": json.loads(t["jurisdictions"])},
        "gates": engine.gate_rows(tid),
        "deadlines": engine.deadline_rows(tid),
        "audit": [],
    }
    with db.conn() as c:
        for r in c.execute("SELECT * FROM audit WHERE txn=? ORDER BY ts", (tid,)):
            payload["audit"].append(dict(r))
    text = json.dumps(payload, indent=2, default=str)
    if out:
        out.write_text(text)
        con.print(f"[green]Exported to {out}[/]")
    else:
        con.print(text)


# ── Audit Log ────────────────────────────────────────────────────────────────

@app.command(name="log")
def audit_log(txn_id: str = typer.Option(None, "--txn"), limit: int = 20):
    """Show audit trail for a transaction."""
    tid = _tid(txn_id)
    tbl = Table(title="Audit Log")
    tbl.add_column("Time")
    tbl.add_column("Action")
    tbl.add_column("Detail")
    with db.conn() as c:
        rows = c.execute("SELECT * FROM audit WHERE txn=? ORDER BY ts DESC LIMIT ?", (tid, limit)).fetchall()
    for r in rows:
        tbl.add_row(r["ts"], r["action"], r["detail"])
    con.print(tbl)


# ── Summary ──────────────────────────────────────────────────────────────────

@app.command()
def summary(txn_id: str = typer.Option(None, "--txn")):
    """Full transaction summary for agent reference."""
    tid = _tid(txn_id)
    with db.conn() as c:
        t = db.txn(c, tid)
    data = json.loads(t["data"])
    gs = engine.gate_rows(tid)
    dls = engine.deadline_rows(tid)
    today = date.today()
    v = sum(1 for g in gs if g["status"] == "verified")

    con.print(Panel(f"[bold]{t['address']}[/]", title="Transaction Summary"))
    con.print(f"  ID: {tid}  |  Phase: {t['phase']}  |  Created: {t['created']}")
    con.print(f"  Jurisdictions: {', '.join(json.loads(t['jurisdictions']))}")

    if p := data.get("parties"):
        con.print(f"\n[bold]Parties[/]")
        for k, val in p.items():
            con.print(f"  {k}: {val}")
    if f := data.get("financial"):
        con.print(f"\n[bold]Financial[/]")
        for k, val in f.items():
            if val:
                con.print(f"  {k}: ${val:,.0f}" if isinstance(val, (int, float)) else f"  {k}: {val}")
    if d := data.get("dates"):
        con.print(f"\n[bold]Key Dates[/]")
        for k, val in d.items():
            con.print(f"  {k}: {val}")
    if ct := data.get("contingencies"):
        con.print(f"\n[bold]Contingencies[/]")
        for k, val in ct.items():
            con.print(f"  {k}: {val} days")

    con.print(f"\n[bold]Gates[/]  {v}/{len(gs)} verified")
    for g in gs:
        if g["status"] == "pending":
            info = rules.gate(g["gid"])
            con.print(f"  [dim]pending[/]  {g['gid']} {info['name'] if info else '?'}")

    overdue = []
    upcoming = []
    for d in dls:
        if not d["due"]:
            continue
        delta = (date.fromisoformat(d["due"]) - today).days
        if delta < 0:
            overdue.append((d, delta))
        elif delta <= 7:
            upcoming.append((d, delta))
    if overdue:
        con.print(f"\n[red bold]OVERDUE ({len(overdue)})[/]")
        for d, delta in overdue:
            con.print(f"  [red]{d['due']}[/] {d['name']} ({-delta}d overdue)")
    if upcoming:
        con.print(f"\n[yellow bold]Upcoming (7 days)[/]")
        for d, delta in upcoming:
            con.print(f"  [yellow]{d['due']}[/] {d['name']} ({delta}d)")


# ── Import ───────────────────────────────────────────────────────────────────

@app.command(name="import")
def import_txn(file: Path):
    """Restore a transaction from exported JSON."""
    payload = json.loads(file.read_text())
    t = payload["transaction"]
    tid = t["id"]
    with db.conn() as c:
        if db.txn(c, tid):
            con.print(f"[red]Transaction {tid} already exists.[/]")
            raise typer.Exit(1)
        c.execute(
            "INSERT INTO txns(id,address,phase,jurisdictions,data,created,updated) VALUES(?,?,?,?,?,?,?)",
            (tid, t["address"], t["phase"], json.dumps(t["jurisdictions"]),
             json.dumps(t["data"]), t["created"], t["updated"]),
        )
        for g in payload.get("gates", []):
            c.execute("INSERT OR IGNORE INTO gates(txn,gid,status,triggered,verified,notes) VALUES(?,?,?,?,?,?)",
                      (g["txn"], g["gid"], g["status"], g.get("triggered"), g.get("verified"), g.get("notes")))
        for d in payload.get("deadlines", []):
            c.execute("INSERT OR IGNORE INTO deadlines(txn,did,name,type,due,status) VALUES(?,?,?,?,?,?)",
                      (d["txn"], d["did"], d["name"], d["type"], d.get("due"), d["status"]))
        db.log(c, tid, "imported", str(file))
    con.print(f"[green]Imported {tid} — {t['address']}[/]")


# ── Cron ─────────────────────────────────────────────────────────────────────

@app.command()
def cron():
    """Show crontab entry for daily digest reminders."""
    import sys
    venv_python = Path(sys.executable)
    root = Path(__file__).resolve().parent.parent
    entry = f"0 8 * * * cd {root} && {venv_python} -m tcli digest 2>&1 >> ~/.tc/digest.log"
    con.print("[bold]Add this to your crontab (crontab -e):[/]\n")
    con.print(f"  {entry}\n")
    con.print("[dim]Runs daily at 8am. Sends push notification if urgent deadlines exist.[/]")


# ── Report ───────────────────────────────────────────────────────────────────

@app.command()
def report(txn_id: str = typer.Option(None, "--txn"), out: Path = typer.Option(None, "--out")):
    """Generate broker compliance report (text)."""
    tid = _tid(txn_id)
    with db.conn() as c:
        t = db.txn(c, tid)
    data = json.loads(t["data"])
    gs = engine.gate_rows(tid)
    dls = engine.deadline_rows(tid)
    juris = json.loads(t["jurisdictions"])
    today = date.today()
    v = sum(1 for g in gs if g["status"] == "verified")

    lines = [
        "=" * 60,
        "TRANSACTION COMPLIANCE REPORT",
        "=" * 60,
        f"Property:       {t['address']}",
        f"Transaction ID: {tid}",
        f"Phase:          {t['phase']}",
        f"Jurisdictions:  {', '.join(juris)}",
        f"Report Date:    {today}",
        f"Gates:          {v}/{len(gs)} verified",
        "",
    ]

    if p := data.get("parties"):
        lines += ["PARTIES", "-" * 40]
        for k, val in p.items():
            lines.append(f"  {k}: {val}")
        lines.append("")
    if f := data.get("financial"):
        lines += ["FINANCIAL", "-" * 40]
        for k, val in f.items():
            if val:
                lines.append(f"  {k}: ${val:,.0f}" if isinstance(val, (int, float)) else f"  {k}: {val}")
        lines.append("")

    lines += ["VERIFICATION GATES", "-" * 40]
    for g in gs:
        info = rules.gate(g["gid"])
        status = "VERIFIED" if g["status"] == "verified" else "PENDING"
        mark = "[x]" if g["status"] == "verified" else "[ ]"
        name = info["name"] if info else "?"
        line = f"  {mark} {g['gid']} {name}"
        if g.get("verified"):
            line += f"  (verified {g['verified']})"
        lines.append(line)
    lines.append("")

    lines += ["DEADLINES", "-" * 40]
    for d in dls:
        if not d["due"]:
            continue
        delta = (date.fromisoformat(d["due"]) - today).days
        flag = " ** OVERDUE **" if delta < 0 else " * URGENT *" if delta <= 3 else ""
        lines.append(f"  {d['due']}  {d['name']}  ({delta}d){flag}")
    lines.append("")

    lines += ["JURISDICTION COMPLIANCE", "-" * 40]
    for name in juris:
        j = rules.jurisdiction(name)
        lines.append(f"  {j['jurisdiction']['name']}:")
        for section in ("required_forms", "retrofit_requirements", "compliance_requirements"):
            for item in j.get(section, []):
                lines.append(f"    [ ] {item['name']}  ({item.get('citation', '')})")
    lines += ["", "=" * 60, f"Generated by TC CLI — {today}", "=" * 60]

    text = "\n".join(lines)
    if out:
        out.write_text(text)
        con.print(f"[green]Report saved to {out}[/]")
    else:
        con.print(text)


if __name__ == "__main__":
    app()
