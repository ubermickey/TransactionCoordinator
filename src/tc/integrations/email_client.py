"""Gmail API email client with alias (send-as) support.

Sends emails FROM the configured alias address so transaction
communications come from your professional email, not your personal one.
"""

from __future__ import annotations

import base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from googleapiclient.discovery import build

from tc.config import get_settings
from tc.integrations.google_drive import get_credentials


def get_gmail_service():
    """Get an authenticated Gmail API service."""
    return build("gmail", "v1", credentials=get_credentials())


def send_email(
    to: str | list[str],
    subject: str,
    body_html: str,
    body_text: str = "",
    cc: str | list[str] | None = None,
    bcc: str | list[str] | None = None,
    from_alias: str | None = None,
) -> str:
    """Send an email using Gmail API with alias support.

    Args:
        to: Recipient(s)
        subject: Email subject
        body_html: HTML body
        body_text: Plain text body (optional fallback)
        cc: CC recipients
        bcc: BCC recipients
        from_alias: Send-as email address. Defaults to GMAIL_SEND_AS_EMAIL.

    Returns:
        Message ID of the sent email.
    """
    settings = get_settings()
    service = get_gmail_service()

    # Build the email
    msg = MIMEMultipart("alternative")
    msg["To"] = ", ".join(to) if isinstance(to, list) else to
    msg["Subject"] = subject

    # Set the From address to the alias
    sender = from_alias or settings.gmail_send_as_email or settings.agent_email
    msg["From"] = sender

    if cc:
        msg["Cc"] = ", ".join(cc) if isinstance(cc, list) else cc
    if bcc:
        msg["Bcc"] = ", ".join(bcc) if isinstance(bcc, list) else bcc

    # Add plain text and HTML parts
    if body_text:
        msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    # Encode and send
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

    sent = service.users().messages().send(
        userId="me",
        body={"raw": raw},
    ).execute()

    return sent.get("id", "")


def send_gate_review_notification(
    gate_id: str,
    gate_name: str,
    address: str,
    items_to_verify: int,
    red_items: int,
    review_link: str,
    red_item_descriptions: list[str] | None = None,
) -> str:
    """Send a gate review notification email to the agent."""
    settings = get_settings()

    red_list = ""
    if red_item_descriptions:
        red_list = "\n".join(f"  - {item}" for item in red_item_descriptions)

    body_html = f"""\
<html><body style="font-family: Arial, sans-serif;">
<h2 style="color: #333;">{address}</h2>
<h3>Review Required: {gate_name}</h3>
<table style="border-collapse: collapse; margin: 16px 0;">
<tr><td style="padding: 4px 12px; font-weight: bold;">Gate:</td>
    <td style="padding: 4px 12px;">{gate_id} — {gate_name}</td></tr>
<tr><td style="padding: 4px 12px; font-weight: bold;">Items to verify:</td>
    <td style="padding: 4px 12px;">{items_to_verify}</td></tr>
<tr><td style="padding: 4px 12px; font-weight: bold; color: #c00;">Critical items:</td>
    <td style="padding: 4px 12px; color: #c00;">{red_items}</td></tr>
</table>
{"<h4 style='color: #c00;'>Critical Items:</h4><pre>" + red_list + "</pre>" if red_list else ""}
<p><a href="{review_link}" style="background: #2563eb; color: white; padding: 10px 20px;
   text-decoration: none; border-radius: 4px; display: inline-block; margin-top: 12px;">
   Open Review Copy</a></p>
<p style="color: #666; font-size: 12px;">After review, mark gate as verified in the workflow dashboard.</p>
</body></html>
"""
    return send_email(
        to=settings.agent_email,
        subject=f"{address} — Review Required: {gate_name}",
        body_html=body_html,
    )


def send_deadline_reminder(
    address: str,
    deadline_name: str,
    deadline_date: str,
    days_remaining: int,
    recipients: list[str] | None = None,
) -> str:
    """Send a deadline reminder email."""
    settings = get_settings()
    to = recipients or [settings.agent_email]

    urgency = ""
    if days_remaining <= 0:
        urgency = "OVERDUE: "
    elif days_remaining == 1:
        urgency = "URGENT: "

    body_html = f"""\
<html><body style="font-family: Arial, sans-serif;">
<h2>{urgency}{address}</h2>
<h3>Deadline: {deadline_name}</h3>
<p><strong>Date:</strong> {deadline_date}</p>
<p><strong>Days remaining:</strong> {days_remaining if days_remaining >= 0 else f"OVERDUE by {abs(days_remaining)} day(s)"}</p>
</body></html>
"""
    return send_email(
        to=to,
        subject=f"{urgency}{address} — {deadline_name} ({deadline_date})",
        body_html=body_html,
    )
