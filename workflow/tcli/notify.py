"""Email (SMTP) and push notifications (Pushover / ntfy)."""
import os, smtplib
from email.mime.text import MIMEText

import httpx


def _env(k, default=""):
    return os.environ.get(k, default)


def email(to: str, subject: str, body: str, html=False):
    msg = MIMEText(body, "html" if html else "plain")
    msg["Subject"] = subject
    msg["From"] = _env("TC_SMTP_FROM") or _env("TC_SMTP_USER")
    msg["To"] = to
    with smtplib.SMTP(_env("TC_SMTP_HOST", "smtp.gmail.com"), int(_env("TC_SMTP_PORT", "587"))) as s:
        s.starttls()
        s.login(_env("TC_SMTP_USER"), _env("TC_SMTP_PASS"))
        s.send_message(msg)


def push(title: str, body: str, priority: int = 0, url: str = ""):
    if tok := _env("TC_PUSHOVER_TOKEN"):
        httpx.post("https://api.pushover.net/1/messages.json", data={
            "token": tok, "user": _env("TC_PUSHOVER_USER"),
            "title": title, "message": body, "priority": min(priority, 1), "url": url,
        })
    if topic := _env("TC_NTFY_TOPIC"):
        hdrs = {"Title": title, "Priority": str(max(1, min(5, priority + 3)))}
        if url:
            hdrs["Click"] = url
        httpx.post(f"https://ntfy.sh/{topic}", headers=hdrs, content=body.encode())


def alert(title: str, body: str, to: str = "", **kw):
    """Best-effort send to all configured channels."""
    try:
        push(title, body, **kw)
    except Exception:
        pass
    if to:
        try:
            email(to, title, body)
        except Exception:
            pass
