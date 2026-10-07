"""
Branded emails. Every builder returns an Email with an HTML version (table layout and inline
styles, which is what email clients render reliably) and a plain-text version (helps deliverability).
All user-provided values are HTML-escaped.
"""

from html import escape
from urllib.parse import urlencode

from .config import settings
from .mailer import Email

ACCENT = "#4f46e5"
TEXT = "#18181b"
MUTED = "#71717a"


def track_url(reference: str, email: str) -> str:
    return f"{settings.public_url}/track?{urlencode({'reference': reference, 'email': email})}"


def _layout(preheader: str, heading: str, paragraphs: list[str], button: tuple[str, str] | None = None,
            highlight: tuple[str, str] | None = None, quote: tuple[str, str] | None = None, footnote: str = "") -> str:
    """paragraphs may contain trusted markup (<strong>); dynamic values must be escaped by the caller.
    quote is (label, plain text) and is escaped here."""
    body = "".join(
        f'<p style="margin:0 0 16px;font-size:15px;line-height:1.6;color:{TEXT};">{p}</p>' for p in paragraphs
    )
    if quote and quote[1].strip():
        label, text = quote
        body += (
            f'<div style="margin:0 0 20px;padding:14px 16px;border-left:3px solid {ACCENT};background:#f5f5ff;border-radius:6px;">'
            f'<div style="font-size:12px;font-weight:600;color:{MUTED};margin-bottom:4px;">{escape(label)}</div>'
            f'<div style="font-size:14px;line-height:1.6;color:{TEXT};">{escape(text.strip()).replace(chr(10), "<br>")}</div></div>'
        )
    highlight_html = ""
    if highlight:
        label, value = highlight
        highlight_html = f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:8px 0 24px;">
          <tr><td style="padding:16px 20px;border:1px solid #e6e6ea;border-radius:12px;background:#fafafa;text-align:center;">
            <div style="font-size:12px;color:{MUTED};">{label}</div>
            <div style="margin-top:4px;font-family:Menlo,Consolas,monospace;font-size:22px;font-weight:700;letter-spacing:2px;color:{TEXT};">{value}</div>
          </td></tr>
        </table>"""
    button_html = ""
    if button:
        label, url = button
        button_html = f"""
        <table role="presentation" cellpadding="0" cellspacing="0" style="margin:8px 0 24px;">
          <tr><td style="border-radius:10px;background:{ACCENT};">
            <a href="{escape(url)}" style="display:inline-block;padding:13px 26px;font-size:15px;font-weight:600;color:#ffffff;text-decoration:none;border-radius:10px;">{label}</a>
          </td></tr>
        </table>"""
    footnote_html = (
        f'<p style="margin:0;font-size:13px;line-height:1.6;color:{MUTED};">{footnote}</p>' if footnote else ""
    )

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f4f6;font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <span style="display:none;max-height:0;overflow:hidden;opacity:0;">{escape(preheader)}</span>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f6;padding:32px 12px;">
    <tr><td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;">
        <tr><td style="padding:0 4px 20px;">
          <table role="presentation" cellpadding="0" cellspacing="0"><tr>
            <td style="width:32px;height:32px;border-radius:9px;background:{ACCENT};color:#ffffff;font-weight:700;font-size:16px;text-align:center;">F</td>
            <td style="padding-left:10px;font-size:17px;font-weight:700;color:{TEXT};">Fidela Translations</td>
          </tr></table>
        </td></tr>
        <tr><td style="padding:36px 36px 28px;background:#ffffff;border:1px solid #e6e6ea;border-radius:16px;">
          <h1 style="margin:0 0 20px;font-size:22px;line-height:1.3;color:{TEXT};">{heading}</h1>
          {body}{highlight_html}{button_html}{footnote_html}
        </td></tr>
        <tr><td style="padding:20px 8px;font-size:12px;line-height:1.6;color:{MUTED};text-align:center;">
          Fidela Translations · Certified translations for newcomers to Canada<br>
          Questions? Reply to this email or write to <a href="mailto:{settings.mail_from}" style="color:{MUTED};">{settings.mail_from}</a>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


def _text(*lines: str) -> str:
    return "\n\n".join(lines) + f"\n\n—\nFidela Translations\n{settings.mail_from}"


# ---------- Clients ----------

def order_received(name: str, email: str, reference: str, file_count: int, document_id: str) -> Email:
    files = f"{file_count} {'file' if file_count == 1 else 'files'}"
    url = track_url(reference, email)
    return Email(
        template="order_received",
        to_email=email,
        to_name=name,
        subject=f"We received your documents — Order {reference}",
        document_id=document_id,
        html=_layout(
            preheader=f"Order {reference}: {files} received. Keep this email to track your translation.",
            heading=f"Thank you, {escape(name.split()[0])}.",
            paragraphs=[
                f"We received <strong>{files}</strong> for translation. A certified translator will review your "
                "documents and we’ll keep you posted by email.",
                "Keep your order reference. You can use it at any time to check the status of your translation.",
            ],
            highlight=("Your order reference", escape(reference)),
            button=("Track your order", url),
            footnote="If you didn’t request this translation, just reply to this email and let us know.",
        ),
        text=_text(
            f"Thank you, {name.split()[0]}.",
            f"We received {files} for translation. A certified translator will review your documents.",
            f"Your order reference: {reference}",
            f"Track your order: {url}",
        ),
    )


def translation_ready(name: str, email: str, reference: str, document_id: str) -> Email:
    url = track_url(reference, email)
    return Email(
        template="translation_ready",
        to_email=email,
        to_name=name,
        subject=f"Your certified translation is ready — Order {reference}",
        document_id=document_id,
        html=_layout(
            preheader=f"Order {reference} has been translated and certified.",
            heading="Your translation is ready",
            paragraphs=[
                f"Good news, {escape(name.split()[0])}: your documents have been translated and certified by "
                f"your translator (order <strong style='white-space:nowrap;'>{escape(reference)}</strong>).",
                "Our team will send you the final certified document shortly.",
            ],
            button=("View order status", url),
        ),
        text=_text(
            f"Good news, {name.split()[0]}: your documents have been translated and certified (order {reference}).",
            "Our team will send you the final certified document shortly.",
            f"Order status: {url}",
        ),
    )


def clearer_copy_requested(name: str, email: str, reference: str, message: str, document_id: str) -> Email:
    upload_url = f"{settings.public_url}/#upload"
    note = message.strip()
    return Email(
        template="clearer_copy_requested",
        to_email=email,
        to_name=name,
        subject=f"Action needed: please send a clearer copy — Order {reference}",
        document_id=document_id,
        html=_layout(
            preheader=f"We need a clearer copy of your documents for order {reference}.",
            heading="We need a clearer copy",
            paragraphs=[
                f"Hi {escape(name.split()[0])}, we started working on your order <strong style='white-space:nowrap;'>{escape(reference)}</strong>, "
                "but part of the document is hard to read.",
                "Please upload a new photo or scan: the whole page visible, good light, no glare, and the text in focus. "
                f"Mention your order reference <strong style='white-space:nowrap;'>{escape(reference)}</strong> in the notes.",
            ],
            quote=("Note from your translator", note),
            button=("Upload a clearer copy", upload_url),
        ),
        text=_text(
            f"Hi {name.split()[0]}, part of your document for order {reference} is hard to read.",
            *([f"Note from your translator: {note}"] if note else []),
            "Please upload a new photo or scan (whole page, good light, no glare) and mention your order reference "
            f"{reference} in the notes: {upload_url}",
        ),
    )


# ---------- Staff ----------

def staff_new_order(to_email: str, reference: str, client_name: str, file_names: list[str], notes: str,
                    document_id: str) -> Email:
    url = f"{settings.public_url}/dashboard"
    files = ", ".join(file_names)
    return Email(
        template="staff_new_order",
        to_email=to_email,
        to_name="",
        subject=f"New order {reference} · {len(file_names)} {'file' if len(file_names) == 1 else 'files'}",
        document_id=document_id,
        html=_layout(
            preheader=f"{client_name} uploaded {len(file_names)} file(s).",
            heading=f"New order {escape(reference)}",
            paragraphs=[
                f"<strong>{escape(client_name)}</strong> uploaded: {escape(files)}.",
                *([f"Client note: “{escape(notes)}”"] if notes else []),
                "The AI draft is being prepared and will appear in the queue when it’s ready.",
            ],
            button=("Open dashboard", url),
        ),
        text=_text(
            f"New order {reference} from {client_name}.",
            f"Files: {files}",
            *([f"Client note: {notes}"] if notes else []),
            f"Dashboard: {url}",
        ),
    )


def welcome_set_password(name: str, email: str, role: str, link: str) -> Email:
    profile = "certified translator" if role == "translator" else role
    return Email(
        template="welcome_set_password",
        to_email=email,
        to_name=name,
        subject="Welcome to Fidela Translations — set your password",
        html=_layout(
            preheader="Your Fidela account is ready. Set your password to sign in.",
            heading=f"Welcome, {escape(name.split()[0])}",
            paragraphs=[
                f"An account was created for you on Fidela Translations as <strong>{escape(profile)}</strong>.",
                "Choose your password to sign in. This link can be used once and expires in "
                f"{settings.set_password_hours} hours.",
            ],
            button=("Set my password", link),
            footnote="If you weren’t expecting this invitation, you can ignore this email.",
        ),
        text=_text(
            f"Welcome, {name.split()[0]}. An account was created for you on Fidela Translations as {profile}.",
            f"Set your password (valid {settings.set_password_hours} hours, one use): {link}",
        ),
    )


def password_reset(name: str, email: str, link: str) -> Email:
    return Email(
        template="password_reset",
        to_email=email,
        to_name=name,
        subject="Reset your Fidela password",
        html=_layout(
            preheader="Use this link to choose a new password.",
            heading="Reset your password",
            paragraphs=[
                f"Hi {escape(name.split()[0])}, we received a request to reset your password.",
                f"This link can be used once and expires in {settings.reset_password_minutes} minutes.",
            ],
            button=("Choose a new password", link),
            footnote="If you didn’t ask for this, ignore this email — your password stays the same.",
        ),
        text=_text(
            f"Hi {name.split()[0]}, we received a request to reset your password.",
            f"Choose a new password (valid {settings.reset_password_minutes} minutes, one use): {link}",
            "If you didn't ask for this, ignore this email.",
        ),
    )
