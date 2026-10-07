"""
Maintenance commands.

  .venv/bin/python -m app.cli send-sample-emails [address]
      Sends one example of every email template (to FIDELA_MAIL_REDIRECT or the given address)
      so the designs can be checked in a real inbox.
"""

import sys

from . import email_templates as templates
from . import mailer
from .config import settings
from .db import init_db

SAMPLE_LINK = f"{settings.public_url}/reset-password?token=EXAMPLE"


def send_sample_emails(address: str | None) -> None:
    if address:
        settings.mail_redirect = address
    if not settings.mail_redirect:
        sys.exit("Set FIDELA_MAIL_REDIRECT or pass an address, so samples never reach real people.")
    init_db()
    samples = [
        templates.order_received("Edward Camilo Lopez", "client@example.com", "FD-7KQ2MX", 2, None),
        templates.staff_new_order("staff@example.com", "FD-7KQ2MX", "Edward Camilo Lopez",
                                  ["passport.jpg", "birth-certificate.pdf"], "Needed for my IRCC work permit", None),
        templates.clearer_copy_requested("Edward Camilo Lopez", "client@example.com", "FD-7KQ2MX",
                                         "The bottom of page 2 is cut off — please include the whole page.", None),
        templates.translation_ready("Edward Camilo Lopez", "client@example.com", "FD-7KQ2MX", None),
        templates.welcome_set_password("Ana Torres", "ana@example.com", "translator", SAMPLE_LINK + "&welcome=1"),
        templates.password_reset("Ana Torres", "ana@example.com", SAMPLE_LINK),
    ]
    for email in samples:
        mailer.send(email)
    print(f"Sent {len(samples)} sample emails to {settings.mail_redirect}. Check data/fidela.sqlite3 → email_log for results.")


if __name__ == "__main__":
    command, *rest = sys.argv[1:] or ["help"]
    if command == "send-sample-emails":
        send_sample_emails(rest[0] if rest else None)
    else:
        print(__doc__)
