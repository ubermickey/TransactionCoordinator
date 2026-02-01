"""Push notification system — Pushover and ntfy support.

Sends push notifications to your phone for gate reviews, deadline
reminders, and critical alerts.
"""

from __future__ import annotations

import httpx

from tc.config import get_settings
from tc.models import Notification


def send_push(notification: Notification) -> bool:
    """Send a push notification via all configured providers.

    Returns True if at least one provider succeeded.
    """
    settings = get_settings()
    sent = False

    if settings.has_pushover():
        sent = _send_pushover(notification, settings) or sent

    if settings.has_ntfy():
        sent = _send_ntfy(notification, settings) or sent

    return sent


def _send_pushover(notification: Notification, settings) -> bool:
    """Send via Pushover (https://pushover.net).

    Pushover costs $5 one-time per platform (iOS/Android/Desktop).
    Excellent reliability and supports priority levels, sounds, and URLs.
    """
    priority_map = {
        "low": -1,
        "normal": 0,
        "high": 1,
        "urgent": 2,  # requires acknowledgment
    }
    priority = priority_map.get(notification.priority, 0)

    payload: dict = {
        "token": settings.pushover_api_token,
        "user": settings.pushover_user_key,
        "title": notification.title,
        "message": notification.body,
        "priority": priority,
    }

    if notification.url:
        payload["url"] = notification.url
        payload["url_title"] = "Open Review Copy"

    # Urgent priority requires retry/expire params
    if priority == 2:
        payload["retry"] = 300    # retry every 5 min
        payload["expire"] = 3600  # stop after 1 hour

    try:
        resp = httpx.post("https://api.pushover.net/1/messages.json", data=payload)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def _send_ntfy(notification: Notification, settings) -> bool:
    """Send via ntfy (https://ntfy.sh).

    ntfy is free and self-hostable. Install the ntfy app on your phone
    and subscribe to your topic to receive notifications.
    """
    priority_map = {
        "low": "2",
        "normal": "3",
        "high": "4",
        "urgent": "5",
    }

    headers: dict = {
        "Title": notification.title,
        "Priority": priority_map.get(notification.priority, "3"),
        "Tags": "house,clipboard",
    }

    if notification.url:
        headers["Click"] = notification.url
        headers["Actions"] = f"view, Open Review, {notification.url}"

    url = f"{settings.ntfy_server}/{settings.ntfy_topic}"
    try:
        resp = httpx.post(url, content=notification.body, headers=headers)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


# ---------------------------------------------------------------------------
# Convenience functions for common notification types
# ---------------------------------------------------------------------------

def notify_gate_review(gate_id: str, gate_name: str, address: str,
                       items: int, red_items: int, review_url: str = "") -> bool:
    """Send push notification for a gate requiring agent review."""
    priority = "urgent" if red_items > 0 else "high"
    body = f"Gate {gate_id}: {items} items to verify"
    if red_items:
        body += f" ({red_items} CRITICAL)"

    return send_push(Notification(
        title=f"{address} — {gate_name}",
        body=body,
        priority=priority,
        url=review_url,
        gate_id=gate_id,
    ))


def notify_deadline(deadline_name: str, address: str,
                    days_remaining: int) -> bool:
    """Send push notification for an upcoming or overdue deadline."""
    if days_remaining < 0:
        priority = "urgent"
        title = f"OVERDUE: {address}"
    elif days_remaining == 0:
        priority = "urgent"
        title = f"DUE TODAY: {address}"
    elif days_remaining <= 2:
        priority = "high"
        title = f"DUE SOON: {address}"
    else:
        priority = "normal"
        title = f"Reminder: {address}"

    body = f"{deadline_name} — "
    if days_remaining < 0:
        body += f"overdue by {abs(days_remaining)} day(s)"
    elif days_remaining == 0:
        body += "due today"
    else:
        body += f"{days_remaining} day(s) remaining"

    return send_push(Notification(
        title=title,
        body=body,
        priority=priority,
    ))


def notify_document_complete(doc_name: str, address: str,
                             validation_passed: bool) -> bool:
    """Send push notification when a document is signed and validated."""
    if validation_passed:
        return send_push(Notification(
            title=f"{address} — Document Signed",
            body=f"{doc_name} — signed and validated successfully",
            priority="normal",
        ))
    else:
        return send_push(Notification(
            title=f"{address} — Document Issue",
            body=f"{doc_name} — signed but VALIDATION FAILED. Review required.",
            priority="high",
        ))
