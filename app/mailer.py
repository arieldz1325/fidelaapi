"""
Transactional email through Brevo's REST API (no SDK needed).

- Without BREVO_API_KEY the email is printed to the console instead (local development).
- With FIDELA_MAIL_REDIRECT set, every email goes to that address, so tests never reach real clients.
- Every attempt is recorded in email_log.
- Callers schedule `send` as a background task: a slow or failing provider never blocks a request.
"""

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import settings
from .db import now, transaction

log = logging.getLogger("fidela.mail")

BREVO_URL = "https://api.brevo.com/v3/smtp/email"
TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class Email:
    template: str
    to_email: str
    to_name: str
    subject: str
    html: str
    text: str
    document_id: str | None = None


def send(email: Email) -> None:
    recipient, subject = email.to_email, email.subject
    if settings.mail_redirect:
        recipient = settings.mail_redirect
        subject = f"[DEV → {email.to_email}] {subject}"

    if not settings.brevo_api_key:
        log.info("Email not sent (no BREVO_API_KEY) — %s\nTo: %s\nSubject: %s\n\n%s",
                 email.template, recipient, subject, email.text)
        _record(email, recipient, subject, "console")
        return

    payload = {
        "sender": {"name": settings.mail_from_name, "email": settings.mail_from},
        "replyTo": {"name": settings.mail_from_name, "email": settings.mail_from},
        "to": [{"email": recipient, "name": email.to_name or recipient}],
        "subject": subject,
        "htmlContent": email.html,
        "textContent": email.text,
        "tags": [email.template],
    }
    request = urllib.request.Request(
        BREVO_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"api-key": settings.brevo_api_key, "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            message_id = json.loads(response.read().decode("utf-8") or "{}").get("messageId")
        _record(email, recipient, subject, "sent", provider_id=message_id)
        log.info("Email %s sent to %s (%s)", email.template, recipient, message_id)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        _record(email, recipient, subject, "failed", error=f"HTTP {error.code}: {detail}")
        log.error("Email %s to %s failed: HTTP %s %s", email.template, recipient, error.code, detail)
    except (urllib.error.URLError, TimeoutError) as error:
        _record(email, recipient, subject, "failed", error=str(error))
        log.error("Email %s to %s failed: %s", email.template, recipient, error)


def _record(email: Email, sent_to: str, subject: str, status: str,
            provider_id: str | None = None, error: str | None = None) -> None:
    with transaction() as db:
        db.execute(
            """INSERT INTO email_log (template, to_email, sent_to, subject, status, provider_id, error, document_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (email.template, email.to_email, sent_to, subject, status, provider_id, error, email.document_id, now()),
        )
