"""Optional text messages (SMS) through Twilio. If the three TWILIO_ settings aren't filled in, texting is
simply hidden and the app works by email and link as before.

Settings:
  TWILIO_ACCOUNT_SID   starts with AC
  TWILIO_AUTH_TOKEN    from the Twilio console
  TWILIO_FROM          your Twilio phone number (+16135550123) or a Messaging Service id (starts with MG)
"""
import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request


def enabled():
    return all(os.environ.get(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM"))


def normalize(phone):
    """A phone number in international form (+16135550123), or None if it doesn't look like one.
    10-digit numbers are taken as Canada/US."""
    raw = (phone or "").strip()
    if re.search(r"[A-Za-z]", raw):   # extensions and words can't receive texts
        return None
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+") and 8 <= len(digits) <= 15:
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None


def pretty(phone):
    p = normalize(phone)
    if p and p.startswith("+1") and len(p) == 12:
        return f"({p[2:5]}) {p[5:8]}-{p[8:]}"
    return p or (phone or "")


def send(to, body):
    """Send one text. Raises RuntimeError with Twilio's explanation if it fails."""
    number = normalize(to)
    if not number:
        raise RuntimeError("that isn't a valid mobile number")
    sid, token, sender = (os.environ[k] for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM"))
    base = os.environ.get("TWILIO_API_BASE", "https://api.twilio.com").rstrip("/")
    fields = {"To": number, "Body": body[:1500]}
    fields["MessagingServiceSid" if sender.startswith("MG") else "From"] = sender
    req = urllib.request.Request(f"{base}/2010-04-01/Accounts/{sid}/Messages.json",
                                 data=urllib.parse.urlencode(fields).encode(), method="POST",
                                 headers={"Authorization": "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode()})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}").get("sid", "")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("message")
        except Exception:  # noqa: BLE001
            msg = None
        raise RuntimeError(msg or f"Twilio answered with error {e.code}") from None
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"couldn't reach Twilio ({e.__class__.__name__})") from None
