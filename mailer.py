"""Optional email via SMTP. If SMTP_HOST isn't set, the app works link-only."""
import mimetypes
import os
import smtplib
import ssl
from email.message import EmailMessage
from html import escape


def enabled():
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_FROM"))


def admin_email():
    return os.environ.get("ADMIN_EMAIL") or os.environ.get("SMTP_FROM")


def _send(msg):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
            if user:
                s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls(context=ctx)
            if user:
                s.login(user, password)
            s.send_message(msg)


def _wrap(inner):
    return f"""<div style="font-family:Arial,Helvetica,sans-serif;max-width:560px;margin:0 auto;
color:#1d2433;line-height:1.5">{inner}</div>"""


def send_request(to, client_name, business, form_names, link, message, expires_at):
    msg = EmailMessage()
    msg["Subject"] = f"{business}: {len(form_names)} form{'s' if len(form_names) != 1 else ''} to sign"
    msg["From"] = os.environ["SMTP_FROM"]
    msg["To"] = to
    if os.environ.get("ADMIN_EMAIL"):
        msg["Reply-To"] = os.environ["ADMIN_EMAIL"]
    items = "\n".join(f"  • {n}" for n in form_names)
    note = f"\n{message}\n" if message else ""
    msg.set_content(
        f"Hi {client_name},\n{note}\n{business} has sent you the following to review and sign:\n{items}\n\n"
        f"Open your forms here:\n{link}\n\nThe link works until {expires_at[:10]}.\n")
    lis = "".join(f"<li>{escape(n)}</li>" for n in form_names)
    note_html = f"<p>{escape(message)}</p>" if message else ""
    msg.add_alternative(_wrap(
        f"<p>Hi {escape(client_name)},</p>{note_html}"
        f"<p>{escape(business)} has sent you the following to review and sign:</p><ul>{lis}</ul>"
        f'<p><a href="{escape(link)}" style="display:inline-block;background:#24408e;color:#fff;'
        f'padding:12px 20px;border-radius:6px;text-decoration:none">Review and sign</a></p>'
        f"<p style='color:#5b6475;font-size:13px'>The link works until {escape(expires_at[:10])}. "
        f"If the button doesn't work, paste this address into your browser:<br>{escape(link)}</p>"),
        subtype="html")
    _send(msg)


def send_completed(to, client_name, business, ref, files, admin_link=None):
    msg = EmailMessage()
    if admin_link:
        msg["Subject"] = f"Signed: {client_name} completed all forms ({ref})"
        body = (f"{client_name} has signed every form in packet {ref}.\n"
                f"Signed copies are attached.\n\nView the packet: {admin_link}\n")
    else:
        msg["Subject"] = f"{business}: your signed forms ({ref})"
        body = (f"Hi {client_name},\n\nThanks — we've received all your signed forms "
                f"(confirmation {ref}). Copies are attached for your records.\n\n{business}\n")
    msg["From"] = os.environ["SMTP_FROM"]
    msg["To"] = to
    msg.set_content(body)
    for name, path in files:
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        maintype, subtype = mime.split("/", 1)
        with open(path, "rb") as fh:
            msg.add_attachment(fh.read(), maintype=maintype, subtype=subtype, filename=name)
    _send(msg)
