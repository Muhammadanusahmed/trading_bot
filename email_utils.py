"""Shared email sender used by every layer that needs to alert a human."""

import json
import os
import smtplib
import sys
import time
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from pathlib import Path
from dotenv import load_dotenv

from agent_status import append_jsonl, utc_now

load_dotenv()

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 465))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")           # Gmail: App Password. Brevo: the SMTP key.
FROM_EMAIL = os.environ.get("FROM_EMAIL", SMTP_USER)       # Brevo: must be a verified sender, may differ from SMTP_USER
NOTIFY_TO = [addr.strip() for addr in os.environ.get("NOTIFY_TO", SMTP_USER or "").split(",") if addr.strip()]

ERROR_ALERT_COOLDOWN_MINUTES = int(os.environ.get("ERROR_ALERT_COOLDOWN_MINUTES", 30))
_ERROR_STATE_FILE = Path(__file__).parent / "error_alert_state.json"
EMAIL_LOG = Path(__file__).parent / "data" / "email_log.jsonl"


def _log_email(agent, subject, status, error=None):
    append_jsonl(EMAIL_LOG, {
        "time": utc_now(),
        "agent": agent or Path(sys.argv[0]).stem,  # the script that is running
        "subject": subject,
        "to": NOTIFY_TO,
        "status": status,  # sent / failed / skipped
        "error": str(error)[:300] if error else None,
    })


def send_email(subject, body, agent=None):
    """Send to NOTIFY_TO and record the attempt in data/email_log.jsonl.
    Raises on failure, as before, so callers keep their own handling."""
    if not SMTP_USER or not SMTP_PASSWORD:
        print(f"[email skipped - SMTP_USER/SMTP_PASSWORD not set in .env] {subject}: {body}")
        _log_email(agent, subject, "skipped", "SMTP_USER/SMTP_PASSWORD not set")
        return
    try:
        _send(subject, body)
    except Exception as e:
        _log_email(agent, subject, "failed", e)
        raise
    _log_email(agent, subject, "sent")


def _send(subject, body):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = f"Crypto Trading Bot <{FROM_EMAIL}>"
    msg["To"] = ", ".join(NOTIFY_TO)
    # missing Date/Message-ID headers are a strong spam signal - MIMEText doesn't set them
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=FROM_EMAIL.split("@")[-1])

    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(FROM_EMAIL, NOTIFY_TO, msg.as_string())
    else:
        # e.g. Brevo's smtp-relay.brevo.com uses STARTTLS on port 587
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(FROM_EMAIL, NOTIFY_TO, msg.as_string())


def alert_error(component, error):
    """Email that `component` hit an error, at most once per
    ERROR_ALERT_COOLDOWN_MINUTES so a repeating error doesn't spam the inbox
    every loop iteration."""
    state = json.loads(_ERROR_STATE_FILE.read_text()) if _ERROR_STATE_FILE.exists() else {}
    last_sent = state.get(component, 0)
    now = time.time()
    if now - last_sent < ERROR_ALERT_COOLDOWN_MINUTES * 60:
        return

    try:
        send_email(f"Crypto bot: {component} hit an error", str(error), agent=component)
        state[component] = now
        _ERROR_STATE_FILE.write_text(json.dumps(state))
    except Exception as email_error:
        print(f"Also failed to send error alert email: {email_error}")
