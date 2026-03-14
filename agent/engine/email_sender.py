"""
Email Sender — Actual email delivery for Grippy.

Supports two backends:
  1. SendGrid API (production) — set SENDGRID_API_KEY
  2. SMTP (fallback) — set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS

If neither is configured, emails are logged but not sent (dev mode).
"""

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────

SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
FROM_EMAIL = os.environ.get("GRIPPY_FROM_EMAIL", "complaints@grippy.ai")
FROM_NAME = os.environ.get("GRIPPY_FROM_NAME", "Grippy Complaint Assistant")


# ──────────────────────────────────────────────────────────────────────
# SendGrid Backend
# ──────────────────────────────────────────────────────────────────────

def _send_via_sendgrid(
    to_email: str,
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    from_email: Optional[str] = None,
    from_name: Optional[str] = None,
    reply_to: Optional[str] = None,
) -> dict[str, Any]:
    """Send email via SendGrid API."""
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail, Email, To, Content, ReplyTo

        sg = SendGridAPIClient(api_key=SENDGRID_API_KEY)

        message = Mail(
            from_email=Email(from_email or FROM_EMAIL, from_name or FROM_NAME),
            to_emails=To(to_email),
            subject=subject,
            plain_text_content=Content("text/plain", body_text),
        )

        if body_html:
            message.add_content(Content("text/html", body_html))

        if reply_to:
            message.reply_to = ReplyTo(reply_to)

        response = sg.send(message)

        logger.info(
            "Email sent via SendGrid to %s (status: %d)",
            to_email,
            response.status_code,
        )

        return {
            "success": True,
            "backend": "sendgrid",
            "status_code": response.status_code,
            "message_id": response.headers.get("X-Message-Id", ""),
        }

    except Exception as exc:
        logger.error("SendGrid send failed: %s", exc)
        return {
            "success": False,
            "backend": "sendgrid",
            "error": str(exc),
        }


# ──────────────────────────────────────────────────────────────────────
# SMTP Backend
# ──────────────────────────────────────────────────────────────────────

def _send_via_smtp(
    to_email: str,
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    from_email: Optional[str] = None,
    from_name: Optional[str] = None,
    reply_to: Optional[str] = None,
) -> dict[str, Any]:
    """Send email via SMTP."""
    try:
        sender = from_email or FROM_EMAIL
        sender_name = from_name or FROM_NAME

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{sender_name} <{sender}>"
        msg["To"] = to_email
        if reply_to:
            msg["Reply-To"] = reply_to

        msg.attach(MIMEText(body_text, "plain"))
        if body_html:
            msg.attach(MIMEText(body_html, "html"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(sender, [to_email], msg.as_string())

        logger.info("Email sent via SMTP to %s", to_email)
        return {
            "success": True,
            "backend": "smtp",
        }

    except Exception as exc:
        logger.error("SMTP send failed: %s", exc)
        return {
            "success": False,
            "backend": "smtp",
            "error": str(exc),
        }


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────

def send_email(
    to_email: str,
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    from_email: Optional[str] = None,
    from_name: Optional[str] = None,
    reply_to: Optional[str] = None,
) -> dict[str, Any]:
    """
    Send an email using the best available backend.

    Priority: SendGrid > SMTP > Dev Mode (log only).

    Returns:
        dict with keys: success, backend, error (if any)
    """
    if SENDGRID_API_KEY:
        return _send_via_sendgrid(
            to_email, subject, body_text, body_html,
            from_email, from_name, reply_to,
        )

    if SMTP_HOST and SMTP_USER:
        return _send_via_smtp(
            to_email, subject, body_text, body_html,
            from_email, from_name, reply_to,
        )

    # Dev mode: log the email
    logger.info(
        "DEV MODE — Email not sent (no backend configured)\n"
        "  To: %s\n  Subject: %s\n  Body: %s...",
        to_email,
        subject,
        body_text[:200],
    )
    return {
        "success": False,
        "backend": "dev_mode",
        "message": "Email was not sent because no SENDGRID_API_KEY or SMTP backend is configured.",
    }


def get_email_status() -> dict[str, Any]:
    """Return email backend status for health checks."""
    if SENDGRID_API_KEY:
        return {"backend": "sendgrid", "status": "configured"}
    if SMTP_HOST and SMTP_USER:
        return {"backend": "smtp", "status": "configured", "host": SMTP_HOST}
    return {"backend": "dev_mode", "status": "no_backend_configured"}
