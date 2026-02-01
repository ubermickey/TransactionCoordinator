"""TC command-line interface."""
import json
from datetime import date
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
    engine.init_gates(tid)
    con.print(f"[green]Created[/] {tid} — {address}")
    con.print(f"Jurisdictions: {', '.join(juris)}")


@app.command()
def extract(pdf: Path, txn_id: str = typer.Option(None, "--txn")):
    """Extract contract terms from a PDF via Claude."""
    tid = _tid(txn_id)
    con.print("[yellow]Sending to Claude...[/]")
    data = engine.extract(str(pdf))
    with db.conn() as c:
        c.execute("UPDATE txns SET data=?, updated=datetime('now','localtime') WHERE id=?", (json.dumps(data), tid))
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
    v = sum(1 for g in gs if g["status"] == "verified")
    con.print(Panel(
        f"[bold]{t['address']}[/]\nPhase: {t['phase']}  |  ID: {tid}\n"
        f"Gates: {v}/{len(gs)} verified  |  Deadlines: {len(dls)} tracked",
        title="Status",
    ))


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


# ── List ─────────────────────────────────────────────────────────────────────

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
    tbl.add_column("Created")
    for r in rows:
        tbl.add_row(r["id"], r["address"], r["phase"], r["created"])
    con.print(tbl)


if __name__ == "__main__":
    app()
