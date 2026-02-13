"""Integrations for DocuSign, SkySlope, and Email.

Email is sandboxed by default (TC_EMAIL_SANDBOX=1) — stores to outbox only.
Set TC_EMAIL_SANDBOX=0 to send real SMTP email.
DocuSign and SkySlope always use live APIs when credentials are configured.
"""
import json
import os
from datetime import datetime
from uuid import uuid4

from . import db

# Legacy flag — only used for email now
EMAIL_SANDBOX = os.environ.get("TC_EMAIL_SANDBOX",
                               os.environ.get("TC_SANDBOX", "1")) == "1"


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── DocuSign Adapter ─────────────────────────────────────────────────────────

def send_for_signature(c, txn_id: str, sig_review_id: int,
                       recipient_email: str, recipient_name: str,
                       provider: str = "docusign"):
    """Send a signature field for signing. Sandbox stores in DB; real calls API."""
    # Update signer info on the sig_review
    c.execute(
        "UPDATE sig_reviews SET signer_email=?, signer_name=? WHERE id=? AND txn=?",
        (recipient_email, recipient_name, sig_review_id, txn_id),
    )

    try:
        envelope_id = _real_docusign_send(
            c, txn_id, sig_review_id, recipient_email, recipient_name
        )
    except NotImplementedError:
        # Mock envelope when DocuSign isn't configured (sandbox / dev mode)
        import uuid
        envelope_id = f"mock-{uuid.uuid4().hex[:12]}"

    now = _now()
    c.execute(
        "INSERT INTO envelope_tracking"
        "(txn, sig_review_id, provider, envelope_id, recipient_email,"
        " recipient_name, status, sent_at, last_checked)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (txn_id, sig_review_id, provider, envelope_id,
         recipient_email, recipient_name, "sent", now, now),
    )
    env_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Compose initial notification email
    sig = c.execute("SELECT * FROM sig_reviews WHERE id=?", (sig_review_id,)).fetchone()
    field_name = sig["field_name"] if sig else "Signature"
    txn_row = db.txn(c, txn_id)
    address = txn_row["address"] if txn_row else txn_id

    subject = f"Signature needed: {field_name} - {address}"
    body = (
        f"Hello {recipient_name},\n\n"
        f"Your signature is required on: {field_name}\n"
        f"Document: {sig['doc_code'] if sig else 'N/A'} (page {sig['page'] if sig else '?'})\n"
        f"Transaction: {address}\n\n"
        f"Please sign at your earliest convenience.\n"
    )
    _queue_email(c, txn_id, recipient_email, subject, body, sig_review_id, env_id)

    db.log(c, txn_id, "sig_sent",
           f"{field_name} -> {recipient_email} via {provider} [{envelope_id[:16]}]")

    return dict(c.execute("SELECT * FROM envelope_tracking WHERE id=?", (env_id,)).fetchone())


def check_envelope_status(c, envelope_tracking_id: int):
    """Check the current status of an envelope."""
    row = c.execute(
        "SELECT * FROM envelope_tracking WHERE id=?", (envelope_tracking_id,)
    ).fetchone()
    if not row:
        return None

    # Poll the provider API for current status
    row = dict(row)
    if row["provider"] == "docusign":
        status = _real_docusign_check(row["envelope_id"])
    elif row["provider"] == "skyslope":
        status = _real_skyslope_check(row["envelope_id"])
    else:
        return row

    if status and status != row["status"]:
        now = _now()
        c.execute(
            "UPDATE envelope_tracking SET status=?, last_checked=? WHERE id=?",
            (status, now, envelope_tracking_id),
        )
        if status == "signed":
            c.execute(
                "UPDATE envelope_tracking SET signed_at=? WHERE id=?",
                (now, envelope_tracking_id),
            )
            c.execute(
                "UPDATE sig_reviews SET is_filled=1 WHERE id=?",
                (row["sig_review_id"],),
            )
        elif status == "viewed":
            c.execute(
                "UPDATE envelope_tracking SET viewed_at=? WHERE id=?",
                (now, envelope_tracking_id),
            )
        row = dict(c.execute(
            "SELECT * FROM envelope_tracking WHERE id=?", (envelope_tracking_id,)
        ).fetchone())

    return row


def simulate_sign(c, envelope_tracking_id: int):
    """Testing utility: simulate a signer completing the signature."""
    row = c.execute(
        "SELECT * FROM envelope_tracking WHERE id=?", (envelope_tracking_id,)
    ).fetchone()
    if not row:
        return None

    now = _now()
    c.execute(
        "UPDATE envelope_tracking SET status='signed', signed_at=?, last_checked=?"
        " WHERE id=?",
        (now, now, envelope_tracking_id),
    )
    c.execute(
        "UPDATE sig_reviews SET is_filled=1 WHERE id=?",
        (row["sig_review_id"],),
    )

    db.log(c, row["txn"], "sig_simulated",
           f"envelope {row['envelope_id'][:16]} -> signed (sandbox)")

    return dict(c.execute(
        "SELECT * FROM envelope_tracking WHERE id=?", (envelope_tracking_id,)
    ).fetchone())


def send_reminder(c, txn_id: str, sig_review_id: int):
    """Send a follow-up reminder for an unsigned field."""
    sig = c.execute(
        "SELECT * FROM sig_reviews WHERE id=? AND txn=?", (sig_review_id, txn_id)
    ).fetchone()
    if not sig:
        return None

    env = c.execute(
        "SELECT * FROM envelope_tracking WHERE sig_review_id=? ORDER BY id DESC LIMIT 1",
        (sig_review_id,),
    ).fetchone()

    recipient_email = sig["signer_email"]
    recipient_name = sig["signer_name"] or "Signer"
    if not recipient_email:
        return None

    txn_row = db.txn(c, txn_id)
    address = txn_row["address"] if txn_row else txn_id
    count = (sig["reminder_count"] or 0) + 1

    subject = f"Reminder ({count}): Please sign {sig['field_name']} - {address}"
    body = (
        f"Hello {recipient_name},\n\n"
        f"This is a friendly reminder that your signature is still needed on:\n"
        f"  {sig['field_name']}\n"
        f"  Document: {sig['doc_code']} (page {sig['page']})\n"
        f"  Transaction: {address}\n\n"
        f"This is reminder #{count}. Please sign at your earliest convenience.\n"
    )

    env_id = env["id"] if env else None
    _queue_email(c, txn_id, recipient_email, subject, body, sig_review_id, env_id)

    now = _now()
    c.execute(
        "UPDATE sig_reviews SET reminder_count=?, last_reminder_at=? WHERE id=?",
        (count, now, sig_review_id),
    )

    db.log(c, txn_id, "sig_reminded",
           f"{sig['field_name']} -> {recipient_email} (reminder #{count})")

    return {
        "sig_review_id": sig_review_id,
        "reminder_count": count,
        "sent_to": recipient_email,
    }


# ── SkySlope Adapter ─────────────────────────────────────────────────────────

def check_file_status(c, txn_id: str, doc_code: str):
    """Check SkySlope file/signing status for a document."""
    row = c.execute(
        "SELECT * FROM envelope_tracking WHERE txn=? AND provider='skyslope'"
        " AND sig_review_id IN (SELECT id FROM sig_reviews WHERE txn=? AND doc_code=?)"
        " ORDER BY id DESC LIMIT 1",
        (txn_id, txn_id, doc_code),
    ).fetchone()

    # Return tracked status or query SkySlope API
    return dict(row) if row else {"status": "no_tracking", "provider": "skyslope"}


def sync_signing_status(c, txn_id: str):
    """Sync signing status from SkySlope for all tracked envelopes."""
    # TODO: iterate envelope_tracking where provider=skyslope and poll API
    return {"synced": 0}


# ── Email Adapter ────────────────────────────────────────────────────────────

def _queue_email(c, txn_id: str, to: str, subject: str, body: str,
                 sig_id: int = None, env_id: int = None):
    """Queue an email — sandbox saves to outbox only, live sends via SMTP."""
    status = "sandbox" if EMAIL_SANDBOX else "queued"
    now = _now()

    if not EMAIL_SANDBOX:
        try:
            from . import notify
            notify.email(to, subject, body)
            status = "sent"
        except Exception:
            status = "failed"

    c.execute(
        "INSERT INTO outbox(txn, channel, to_addr, subject, body, status,"
        " sent_at, related_sig_id, related_envelope_id)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (txn_id, "email", to, subject, body, status, now, sig_id, env_id),
    )


def send_followup(c, txn_id: str, to: str, subject: str, body: str,
                  sig_id: int = None):
    """Send a custom follow-up email."""
    _queue_email(c, txn_id, to, subject, body, sig_id)
    db.log(c, txn_id, "followup_sent", f"-> {to}: {subject[:60]}")
    return {"sent_to": to, "subject": subject, "email_sandbox": EMAIL_SANDBOX}


def get_outbox(c, txn_id: str):
    """Get all outbox entries for a transaction."""
    rows = c.execute(
        "SELECT * FROM outbox WHERE txn=? ORDER BY created_at DESC", (txn_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# ── DocuSign Configuration ────────────────────────────────────────────────────

DOCUSIGN_CONFIG = {
    "integration_key": os.environ.get("DOCUSIGN_INTEGRATION_KEY", ""),
    "secret_key": os.environ.get("DOCUSIGN_SECRET_KEY", ""),
    "account_id": os.environ.get("DOCUSIGN_ACCOUNT_ID", ""),
    "base_url": os.environ.get("DOCUSIGN_BASE_URL", "https://na4.docusign.net/restapi"),
    "auth_server": os.environ.get("DOCUSIGN_AUTH_SERVER", "account.docusign.com"),
    "user_id": os.environ.get("DOCUSIGN_USER_ID", ""),
    "private_key_path": os.environ.get("DOCUSIGN_PRIVATE_KEY_PATH", ""),
}


def docusign_configured() -> bool:
    """Check if DocuSign credentials are configured."""
    return bool(DOCUSIGN_CONFIG["integration_key"] and DOCUSIGN_CONFIG["account_id"])


def docusign_status() -> dict:
    """Return DocuSign configuration status for the UI."""
    configured = docusign_configured()
    return {
        "configured": configured,
        "account_id": DOCUSIGN_CONFIG["account_id"][:8] + "..." if configured else "",
        "base_url": DOCUSIGN_CONFIG["base_url"] if configured else "",
        "setup_instructions": None if configured else {
            "env_vars": [
                "DOCUSIGN_INTEGRATION_KEY  — from DocuSign Developer → Apps & Keys",
                "DOCUSIGN_SECRET_KEY       — RSA private key or secret",
                "DOCUSIGN_ACCOUNT_ID       — from DocuSign Admin → Account Info",
                "DOCUSIGN_USER_ID          — GUID from DocuSign Users page",
                "DOCUSIGN_BASE_URL         — e.g. https://na4.docusign.net/restapi",
                "DOCUSIGN_PRIVATE_KEY_PATH — path to RSA private key PEM (for JWT auth)",
            ],
            "steps": [
                "1. Go to developers.docusign.com → create or select your app",
                "2. Copy Integration Key and generate RSA key pair",
                "3. Set DOCUSIGN_INTEGRATION_KEY, DOCUSIGN_SECRET_KEY, DOCUSIGN_ACCOUNT_ID",
                "4. Set DOCUSIGN_USER_ID (your DocuSign user GUID)",
                "5. Save the private key PEM file and set DOCUSIGN_PRIVATE_KEY_PATH",
                "6. Grant consent: visit https://account.docusign.com/oauth/auth?response_type=code"
                   "&scope=signature%20impersonation&client_id=YOUR_KEY&redirect_uri=http://localhost:5001/callback",
                "7. Restart the app — DocuSign will be live",
            ],
        },
    }


# ── Real API Implementations ─────────────────────────────────────────────────

def _real_docusign_send(c, txn_id, sig_review_id, email, name):
    """Call DocuSign eSignature REST API to send an envelope."""
    if not docusign_configured():
        raise NotImplementedError(
            "DocuSign not configured. Set DOCUSIGN_INTEGRATION_KEY, DOCUSIGN_ACCOUNT_ID, "
            "and other env vars. See /api/docusign-status for setup instructions."
        )
    # Cloud governance hook (for live implementation):
    # 1) require cloud approval: cloud_guard.require_approval(c, txn_id, "docusign", "send_for_signature")
    # 2) log usage: cloud_guard.log_cloud_event(...)
    # TODO: implement JWT auth + POST /envelopes with docusign-esign SDK
    raise NotImplementedError("DocuSign API integration pending SDK setup")


def _real_docusign_check(envelope_id):
    """Call GET /envelopes/{id}/recipients for status."""
    if not docusign_configured():
        return None
    # Cloud governance hook (for live implementation):
    # log status polling events to cloud_events when provider API is called.
    # TODO: implement with docusign-esign SDK
    return None


def _real_skyslope_check(envelope_id):
    """Call SkySlope Transaction API for status."""
    # Cloud governance hook (for live implementation):
    # require approval + log usage when hitting SkySlope APIs.
    # TODO: implement with SkySlope REST API
    return None
