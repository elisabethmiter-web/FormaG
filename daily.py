"""Everyday features for shops that send links themselves (the app sends no emails or texts):

 1  quick send       "Open in my email", "Text from my phone" and "Copy message": the staff member's own email or
                     phone opens with the message and link filled in (mailto: / sms: links), and the packet logs it
 2  what's new       "New since your last visit" on the dashboard, a badge on the menu and optional desktop alerts
 3  follow-up list   who to remind today: overdue, not opened, opened but not finished, second owners waiting
 4  second owners    "Waiting on Chloe" on the dashboard and in the follow-up list
 5  copies           "Download all" (ZIP) for clients, second owners and staff
 6  backups          nightly backup of the database and files, kept on the disk and copied off-site to any
                     S3-compatible storage (Backblaze B2, Amazon S3, Wasabi...) when configured
 7  restarts         /healthz, and signing pages that wait for the server instead of losing the signature
 8  access code      optional 4-digit code the client must enter before opening their forms
 9  client lookup    earlier clients suggested while typing on New packet
10  faster previews  Word/Excel previews, PDF pages and photo copies prepared in the background when files arrive
11  time zones       each company picks its own time zone
12  sign-in help     who to contact when a manager forgets their password
13  verify           a public page where anyone can check a signed PDF is authentic and unchanged, plus the seal
                     certificate to download (no purchased certificate needed)
14  demo grace       a few days' grace after a demo ends before the account locks
15  help             an in-app Help page
"""
import datetime as dt
import fcntl
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone

from flask import (Response, abort, flash, g, jsonify, redirect, render_template, request, send_file, session,
                   url_for)
from markupsafe import escape
from werkzeug.utils import secure_filename

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

BACKUP_KEEP = max(1, int(os.environ.get("BACKUP_KEEP", "7")))
RESTORE_FOLDERS = ("templates", "signed", "client_uploads", "brand", "seal")
SUPPORT_NAME = os.environ.get("SUPPORT_NAME", "")
SUPPORT_PHONE = os.environ.get("SUPPORT_PHONE", "")
HELP_VIDEO_URL = os.environ.get("HELP_VIDEO_URL", "")
NEW_EVENT_TYPES = ("viewed", "signed", "completed", "uploaded", "confirmed")
TIMEZONES = [
    ("America/St_Johns", "Newfoundland"), ("America/Halifax", "Atlantic (Halifax)"), ("America/Moncton", "Atlantic (Moncton)"),
    ("America/Toronto", "Eastern (Toronto)"), ("America/Winnipeg", "Central (Winnipeg)"), ("America/Regina", "Saskatchewan"),
    ("America/Edmonton", "Mountain (Edmonton, Calgary)"), ("America/Vancouver", "Pacific (Vancouver)"),
    ("America/Whitehorse", "Yukon"), ("America/New_York", "US Eastern"), ("America/Chicago", "US Central"),
    ("America/Denver", "US Mountain"), ("America/Phoenix", "Arizona"), ("America/Los_Angeles", "US Pacific"),
    ("America/Anchorage", "Alaska"), ("Pacific/Honolulu", "Hawaii"),
]
COLUMNS = {
    "packets": {"access_code": "TEXT DEFAULT ''", "last_nudge_at": "TEXT"},
    "users": {"last_seen_at": "TEXT", "email_app": "TEXT DEFAULT ''"},
    "companies": {"timezone": "TEXT DEFAULT ''"},
}


# Where "Open in my email" writes the message: the computer's email app, or webmail in a new tab.
EMAIL_APPS = {
    "app": ("my email", "The email app on this computer or phone (Outlook, Apple Mail, Thunderbird…)"),
    "outlook": ("Outlook", "Outlook on the web: work or school (Microsoft 365)"),
    "gmail": ("Gmail", "Gmail, or Google Workspace"),
    "outlookcom": ("Outlook.com", "Outlook.com, Hotmail or Live (personal)"),
}


def compose_url(kind, to, subject, body):
    """A link that opens a new email with everything filled in."""
    q = urllib.parse.quote
    if kind == "gmail":
        return f"https://mail.google.com/mail/?view=cm&fs=1&to={q(to)}&su={q(subject)}&body={q(body)}"
    if kind == "outlook":
        return f"https://outlook.office.com/mail/deeplink/compose?to={q(to)}&subject={q(subject)}&body={q(body)}"
    if kind == "outlookcom":
        return f"https://outlook.live.com/mail/0/deeplink/compose?to={q(to)}&subject={q(subject)}&body={q(body)}"
    return f"mailto:{q(to)}?subject={q(subject)}&body={q(body)}"


def init_db(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS text_links (id TEXT PRIMARY KEY, body TEXT NOT NULL, created_at TEXT NOT NULL)")
    for table, cols in COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, spec in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {spec}")


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(d):
    return d.replace(microsecond=0).isoformat()


def parse(ts):
    try:
        d = datetime.fromisoformat(ts)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- off-site copies (S3-compatible, AWS Signature V4)
def _sign(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sigv4_headers(method, url, region, service, key_id, secret, payload_hash, amz_date, extra_headers=None):
    """Headers for one request signed with AWS Signature Version 4 (works with S3, Backblaze B2, Wasabi...)."""
    u = urllib.parse.urlsplit(url)
    host = u.netloc
    path = urllib.parse.quote(u.path or "/", safe="/~")
    query = "&".join(f"{urllib.parse.quote(k, safe='~')}={urllib.parse.quote(v, safe='~')}"
                     for k, v in sorted(urllib.parse.parse_qsl(u.query, keep_blank_values=True)))
    headers = {"host": host, "x-amz-date": amz_date}
    if service == "s3":
        headers["x-amz-content-sha256"] = payload_hash
    for k, v in (extra_headers or {}).items():
        headers[k.lower()] = v
    signed = ";".join(sorted(headers))
    canonical = "\n".join([method, path, query,
                           "".join(f"{k}:{' '.join(str(headers[k]).split())}\n" for k in sorted(headers)),
                           signed, payload_hash])
    day = amz_date[:8]
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = _sign(("AWS4" + secret).encode(), day)
    k = _sign(k, region)
    k = _sign(k, service)
    k = _sign(k, "aws4_request")
    signature = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    out = {k2: v for k2, v in headers.items() if k2 != "host"}
    out["Authorization"] = f"AWS4-HMAC-SHA256 Credential={key_id}/{scope}, SignedHeaders={signed}, Signature={signature}"
    return out


def offsite_settings():
    s = {k: os.environ.get(f"BACKUP_S3_{k}", "").strip() for k in ("ENDPOINT", "BUCKET", "KEY_ID", "SECRET", "REGION")}
    return s if s["ENDPOINT"] and s["BUCKET"] and s["KEY_ID"] and s["SECRET"] else None


def offsite_upload(path, name):
    """Copy one backup file to the S3-compatible bucket. Raises on failure."""
    s = offsite_settings()
    endpoint = s["ENDPOINT"] if s["ENDPOINT"].startswith("http") else "https://" + s["ENDPOINT"]
    url = f"{endpoint.rstrip('/')}/{s['BUCKET']}/{urllib.parse.quote(name)}"
    size = os.path.getsize(path)
    amz_date = utcnow().strftime("%Y%m%dT%H%M%SZ")
    headers = sigv4_headers("PUT", url, s["REGION"] or "us-east-1", "s3", s["KEY_ID"], s["SECRET"], "UNSIGNED-PAYLOAD",
                            amz_date, {"content-type": "application/zip"})
    headers["Content-Length"] = str(size)
    with open(path, "rb") as fh:
        req = urllib.request.Request(url, data=fh, method="PUT", headers=headers)
        with urllib.request.urlopen(req, timeout=600) as r:
            if r.status >= 300:
                raise RuntimeError(f"storage answered {r.status}")


def register(A):
    app, db = A.app, A.db
    backup_dir = os.path.join(A.DATA_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    state = {"thread": None, "fails": {}}

    def biz_tz():
        c = g.get("company") if g else None
        name = (c or {}).get("timezone") if c else ""
        if name and ZoneInfo:
            try:
                return ZoneInfo(name)
            except Exception:  # noqa: BLE001
                pass
        return A.LOCAL_TZ

    # ------------------------------------------------ 11  time zone per company
    def to_local(d):
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        try:
            return d.astimezone(biz_tz())
        except RuntimeError:  # outside a request
            return d.astimezone(A.LOCAL_TZ)

    A.to_local = to_local

    @app.route("/company/timezone", methods=["POST"])
    @A.manager_required
    def company_timezone():
        tz = request.form.get("timezone", "")
        if tz not in {t for t, _ in TIMEZONES}:
            flash("Choose a time zone from the list.", "error")
        else:
            db().execute("UPDATE companies SET timezone=? WHERE id=?", (tz, A.cid()))
            db().commit()
            flash("Time zone saved. Dates and deadlines now show in your local time.", "ok")
        return redirect(url_for("company_settings") + "#timezone")

    # ------------------------------------------------ 1  quick send (the staff member's own email or phone)
    def first_name(name):
        return (name or "").split(" ")[0] or name

    def quick_message(p, signer=None, reminder=False):
        """The message for a packet (or for the second owner when `signer` is given): plain text, an HTML version
        with a clickable named link (pasted into the email), a short text message, and links that open them."""
        name = signer["name"] if signer else p["client_name"]
        email = (signer["email"] if signer else p["client_email"]) or ""
        link = (A.BASE_URL or request.url_root.rstrip("/")) + (f"/c/{signer['token']}" if signer else f"/s/{p['token']}")
        forms = [r[0] for r in db().execute("SELECT template_name FROM packet_items WHERE packet_id=? AND requires_signature=1 "
                                            "ORDER BY position", (p["id"],))] or \
            [r[0] for r in db().execute("SELECT template_name FROM packet_items WHERE packet_id=? ORDER BY position", (p["id"],))]
        business = A.biz()
        dl = A.deadline_info(p) if not reminder else {"due_at": None}
        due_text = f"Please complete by {A.fmt_when(dl['due_at'])}." if dl.get("due_at") else ""
        sender = g.user["name"] if g.get("user") and g.user["id"] else business
        if reminder:
            subject = f"Reminder: your forms from {business} are waiting for your signature"
            intro = f"Just a friendly reminder that {business} is still waiting for you to review and sign:"
        else:
            subject = f"{business}: {len(forms)} form{'s' if len(forms) != 1 else ''} to sign"
            intro = f"{business} has sent you the following to review and sign:"
        if signer:
            intro += f" (you sign as the second owner, after {first_name(p['client_name'])})"
        code_text = "You'll be asked for an access code: we'll give it to you separately." if p["access_code"] else ""
        # the clickable text shown instead of the web address
        if len(forms) <= 2:
            link_text = f"{business} – " + " & ".join(forms) if forms else f"{business} – your forms"
        else:
            link_text = f"{business} – {forms[0]}, {forms[1]} and {len(forms) - 2} more"
        body = (f"Hi {first_name(name)},\n\n{intro}\n" + "".join(f"  - {f}\n" for f in forms) +
                f"\nClick here to open your forms ({link_text}):\n{link}\n" + (f"\n{due_text}\n" if due_text else "") +
                (f"\n{code_text}\n" if code_text else "") + f"\nThank you,\n{sender}\n{business}\n")
        e = escape
        html = (
            f'<div style="font-family:Segoe UI,Arial,sans-serif;font-size:15px;line-height:1.5;color:#1f2937">'
            f"<p>Hi {e(first_name(name))},</p><p>{e(intro)}</p>"
            f'<ul style="margin:0 0 14px 0">' + "".join(f"<li>{e(f)}</li>" for f in forms) + "</ul>"
            f'<p><a href="{e(link)}" style="display:inline-block;background:#1f3a8a;color:#ffffff;text-decoration:none;'
            f'font-weight:600;padding:10px 18px;border-radius:6px">{e(link_text)}</a></p>'
            + (f"<p>{e(due_text)}</p>" if due_text else "") + (f"<p>{e(code_text)}</p>" if code_text else "")
            + f"<p>Thank you,<br>{e(sender)}<br>{e(business)}</p></div>")
        short = (f"{business}: Hi {first_name(name)}, "
                 + ("a reminder that your forms are waiting for your signature: " if reminder else
                    "please review and sign your forms here: ") + link + (" (access code given separately)" if p["access_code"] else ""))
        q = urllib.parse.quote
        choice = email_app()
        return {"subject": subject, "body": body, "html": str(html), "link_text": link_text, "short": short,
                "email": email, "link": link,
                "mailto": compose_url("app", email, subject, body),
                "compose": compose_url(choice, email, subject, body),       # message as plain text (no paste needed)
                "compose_paste": compose_url(choice, email, subject, ""),   # empty message: the copied one is pasted in
                "compose_web": choice != "app", "compose_label": "Email", "email_app": choice,
                "others": [{"key": k, "label": v[0], "url": compose_url(k, email, subject, body),
                            "paste": compose_url(k, email, subject, "")} for k, v in EMAIL_APPS.items() if k != choice],
                "sms": f"sms:?&body={q(short)}",
                "qr": url_for("text_qr", pid=p["id"], who=signer["id"] if signer else "", kind="reminder" if reminder else "send")}

    app.jinja_env.globals["quick_message"] = quick_message

    def email_app():
        """This person's choice of where emails open (default: the email app on their computer)."""
        u = g.get("user")
        if not u:
            return "app"
        v = session.get("email_app") if u["id"] == 0 else u.get("email_app")
        return v if v in EMAIL_APPS else "app"

    app.jinja_env.globals["email_app"] = email_app
    app.jinja_env.globals["EMAIL_APPS"] = EMAIL_APPS

    # ------------------------------------------------ texting from a computer: scan a code with your phone
    TEXT_LINK_MINUTES = 30

    @app.route("/packets/<int:pid>/text-qr.svg")
    @A.login_required
    def text_qr(pid):
        """QR code that opens the text message on the staff member's phone (works for 30 minutes)."""
        from reportlab.graphics import renderSVG
        from reportlab.graphics.barcode.qr import QrCodeWidget
        from reportlab.graphics.shapes import Drawing
        p = db().execute("SELECT * FROM packets WHERE id=?", (pid,)).fetchone() or abort(404)
        signer = None
        if request.args.get("who", "").isdigit():
            signer = db().execute("SELECT * FROM packet_signers WHERE id=? AND packet_id=?",
                                  (int(request.args["who"]), pid)).fetchone() or abort(404)
        short = quick_message(p, signer, request.args.get("kind") == "reminder")["short"]
        db().execute("DELETE FROM text_links WHERE created_at < ?", (iso(utcnow() - timedelta(days=1)),))
        tid = secrets.token_urlsafe(9)
        db().execute("INSERT INTO text_links (id, body, created_at) VALUES (?,?,?)", (tid, short, A.now()))
        db().commit()
        url = (A.BASE_URL or request.url_root.rstrip("/")) + url_for("text_on_phone", tid=tid)
        w = QrCodeWidget(url, barLevel="M", barBorder=4)
        x0, y0, x1, y1 = w.getBounds()
        size = 240
        dr = Drawing(size, size, transform=[size / (x1 - x0), 0, 0, size / (y1 - y0), 0, 0])
        dr.add(w)
        return Response(renderSVG.drawToString(dr), mimetype="image/svg+xml", headers={"Cache-Control": "no-store"})

    @app.route("/t/<tid>")
    def text_on_phone(tid):
        """Opened on the staff member's phone from the QR code: one tap opens Messages with the text ready."""
        r = db().execute("SELECT * FROM text_links WHERE id=?", (tid,)).fetchone()
        if not r or (parse(r["created_at"]) or utcnow()) < utcnow() - timedelta(minutes=TEXT_LINK_MINUTES):
            return render_template("text_phone.html", text=None), 410
        return render_template("text_phone.html", text=r["body"], sms=f"sms:?&body={urllib.parse.quote(r['body'])}")

    @app.route("/packets/<int:pid>/compose.json")
    @A.login_required
    def packet_compose(pid):
        """The link that opens a new email for this packet; logged as sent by email."""
        p = db().execute("SELECT * FROM packets WHERE id=?", (pid,)).fetchone() or abort(404)
        qs = quick_message(p)
        if p["status"] == "draft":
            db().execute("UPDATE packets SET status='sent', sent_at=? WHERE id=?", (A.now(), pid))
        db().execute("UPDATE packets SET last_nudge_at=? WHERE id=?", (A.now(), pid))
        A.log_event(pid, "sent", f"link for {p['client_name']} via staff member's email · by {A.actor()}")
        db().commit()
        return jsonify({"url": qs["compose"], "web": qs["compose_web"]})

    @app.route("/account/email-app", methods=["POST"])
    @A.login_required
    def set_email_app():
        """Saved from the Account page, or when someone picks another email from the ▾ menu."""
        v = request.form.get("email_app", "")
        if v in EMAIL_APPS:
            if g.user["id"] == 0:
                session["email_app"] = v
            else:
                db().execute("UPDATE users SET email_app=? WHERE id=?", (v, g.user["id"]))
                db().commit()
        if request.form.get("quiet"):
            return ("", 204)
        flash(f"Emails will open in {EMAIL_APPS[v][1] if v in EMAIL_APPS else 'your email app'}.", "ok")
        return redirect(request.form.get("next") or url_for("account"))

    @app.route("/packets/<int:pid>/nudge", methods=["POST"])
    @A.login_required
    def packet_nudge(pid):
        """Logged when staff open their email or phone, or copy the message, from the packet."""
        p = db().execute("SELECT * FROM packets WHERE id=?", (pid,)).fetchone()
        via = {"email": "staff member's email", "phone": "staff member's phone", "copy": "copied message"}.get(
            request.form.get("via"), "copied message")
        reminder = request.form.get("kind") == "reminder"
        who = p["client_name"]
        sid = request.form.get("who", "")
        if sid.isdigit():
            s = db().execute("SELECT name FROM packet_signers WHERE id=? AND packet_id=?", (int(sid), pid)).fetchone()
            who = f"{s['name']} (second owner)" if s else who
        if p["status"] == "draft":
            db().execute("UPDATE packets SET status='sent', sent_at=? WHERE id=?", (A.now(), pid))
        db().execute("UPDATE packets SET last_nudge_at=? WHERE id=?", (A.now(), pid))
        A.log_event(pid, "reminder" if reminder else "sent",
                    f"{'reminder' if reminder else 'link'} for {who} via {via} · by {A.actor()}")
        db().commit()
        return ("", 204)

    # ------------------------------------------------ 2  what's new since the last visit
    def last_seen():
        if not g.get("user"):
            return None
        if g.user["id"] == 0:
            return session.get("owner_seen", {}).get(str(A.cid())) or iso(utcnow() - timedelta(days=2))
        return g.user.get("last_seen_at") or iso(utcnow() - timedelta(days=2))

    def updates_since(ts, limit=30):
        cond, args = A.packet_scope()
        cond = cond.replace("company_id", "p.company_id").replace("created_by_id", "p.created_by_id") \
                   .replace("sales_rep", "p.sales_rep")
        rows = db().execute(
            f"SELECT e.*, p.client_name, p.id AS pid FROM events e JOIN packets p ON p.id = e.packet_id "
            f"WHERE {cond} AND e.at > ? AND e.type IN ({','.join('?' * len(NEW_EVENT_TYPES))}) "
            f"ORDER BY e.at DESC, e.id DESC LIMIT ?", args + [ts, *NEW_EVENT_TYPES, limit]).fetchall()
        return [dict(r) for r in rows]

    def update_text(e):
        who = e["client_name"]
        d = e["detail"] or ""
        if e["type"] == "viewed":
            return f"{d.replace('by ', '').replace(' (second owner)', '')} (second owner) opened their link" \
                if "(second owner)" in d else f"{who} opened the signing link"
        if e["type"] == "signed":
            return f"{who}: signed “{d}”" if "second owner" not in d else d.replace(" — signed by ", ": signed by ")
        if e["type"] == "completed":
            return f"{who}: every form is signed"
        if e["type"] == "uploaded":
            return f"{who} added files: {d}"
        if e["type"] == "confirmed":
            return f"{who} confirmed receipt of the files"
        return f"{who}: {d}"

    @app.context_processor
    def inject_daily():
        out = {"support_name": SUPPORT_NAME, "support_phone": SUPPORT_PHONE, "help_video_url": HELP_VIDEO_URL,
               "timezones": TIMEZONES, "new_count": 0, "followup_count": 0, "update_text": update_text,
               "grace_days_left": None}
        try:
            if g.get("user") and g.get("company") and request.endpoint not in ("static",):
                out["new_count"] = len(updates_since(last_seen(), limit=99))
                out["followup_count"] = sum(1 for f in followups() if not f["contacted_today"])
            c = g.get("company")
            if c and A.extras.trial_days_left(c) == 0:
                out["grace_days_left"] = A.extras.grace_days_left(c)
        except Exception:  # noqa: BLE001  (never break a page over a badge)
            app.logger.exception("Couldn't count updates")
        return out

    @app.route("/updates/seen", methods=["POST"])
    @A.login_required
    def updates_seen():
        if g.user["id"] == 0:
            seen = dict(session.get("owner_seen", {}))
            seen[str(A.cid())] = A.now()
            session["owner_seen"] = seen
        else:
            db().execute("UPDATE users SET last_seen_at=? WHERE id=?", (A.now(), g.user["id"]))
            db().commit()
        return redirect(url_for("dashboard"))

    @app.route("/updates.json")
    @A.login_required
    def updates_json():
        after = request.args.get("after") or A.now()
        rows = updates_since(after, limit=10)
        return jsonify({"now": A.now(), "updates": [{"text": update_text(e), "url": url_for("packet_detail", pid=e["pid"]),
                                                     "at": e["at"]} for e in rows]})

    app.jinja_env.globals["recent_updates"] = lambda: updates_since(last_seen())

    # ------------------------------------------------ 3, 4  follow-up list and second owners waiting
    def waiting_on(pid):
        """Second owners who have forms ready for them."""
        out = []
        for s in db().execute("SELECT * FROM packet_signers WHERE packet_id=? AND completed_at IS NULL", (pid,)):
            ready = db().execute("SELECT COUNT(*) FROM packet_items i WHERE i.packet_id=? AND i.requires_signature=1 AND "
                                 "i.signed_at IS NOT NULL AND i.id NOT IN (SELECT item_id FROM cosignatures WHERE signer_id=?)",
                                 (pid, s["id"])).fetchone()[0]
            if ready:
                out.append(dict(s, ready=ready))
        return out

    app.jinja_env.globals["waiting_on"] = waiting_on

    def followups():
        """Open packets that need a nudge today, most urgent first."""
        cond, args = A.packet_scope()
        nowdt = utcnow()
        out = []
        for p in db().execute(f"SELECT * FROM packets WHERE {cond} AND completed_at IS NULL AND voided_at IS NULL "
                              f"AND sent_at IS NOT NULL", args).fetchall():
            st = A.effective_status(p)
            if st not in ("sent", "viewed", "in_progress"):
                continue
            last_contact = max([x for x in (p["last_nudge_at"], p["sent_at"]) if x] or [p["created_at"]])
            last_fu = A.last_followup(p)
            if last_fu and last_fu.get("at") and last_fu["at"] > last_contact:
                last_contact = last_fu["at"]
            contacted_today = (parse(last_contact) or nowdt) > nowdt - timedelta(hours=20)
            reasons = []
            dl = A.deadline_info(p)
            sent = parse(p["sent_at"]) or nowdt
            waiting = waiting_on(p["id"])
            if dl.get("overdue"):
                reasons.append(("Overdue", 0))
            if st == "sent" and nowdt - sent > timedelta(days=2):
                reasons.append(("Not opened in 2 days", 1))
            if st in ("viewed", "in_progress") and not waiting and nowdt - (parse(p["first_viewed_at"]) or sent) > timedelta(days=1):
                reasons.append(("Opened but not finished", 2))
            for s in waiting:
                reasons.append((f"Waiting on {s['name']} (second owner)", 1))
            if reasons:
                out.append({"p": p, "reasons": [r for r, _ in reasons], "rank": min(k for _, k in reasons),
                            "waiting": waiting, "contacted_today": contacted_today, "last_contact": last_contact})
        out.sort(key=lambda f: (f["contacted_today"], f["rank"], f["p"]["sent_at"] or ""))
        return [f for f in out if not f["contacted_today"]] + [f for f in out if f["contacted_today"]]

    @app.route("/follow-up")
    @A.login_required
    def follow_up():
        items = followups()
        return render_template("followup.html", items=items, due=[f for f in items if not f["contacted_today"]],
                               done=[f for f in items if f["contacted_today"]])

    # ------------------------------------------------ 5  download everything as one ZIP
    def zip_response(files, name):
        buf = io.BytesIO()
        used = set()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for arc, path in files:
                if not path or not os.path.exists(path):
                    continue
                base, ext = os.path.splitext(arc)
                n, final = 2, arc
                while final.lower() in used:
                    final, n = f"{base} ({n}){ext}", n + 1
                used.add(final.lower())
                z.write(path, final)
        buf.seek(0)
        return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=name)

    def signed_files(pid, only=None):
        out = []
        for it in db().execute("SELECT * FROM packet_items WHERE packet_id=? ORDER BY position", (pid,)):
            if it["signed_pdf_path"] and (only is None or it["id"] in only):
                out.append((f"{secure_filename(it['template_name']) or 'document'}-signed.pdf",
                            os.path.join(A.SIGNED_DIR, it["signed_pdf_path"])))
            elif not it["requires_signature"] and it["template_file"]:
                out.append((it["file_name"] or secure_filename(it["template_name"]), os.path.join(A.UPLOAD_DIR, it["template_file"])))
        return out

    @app.route("/s/<token>/copies.zip")
    def client_copies_zip(token):
        p = A.load_packet(token)
        if p["voided_at"]:
            abort(410)
        files = signed_files(p["id"])
        if not files:
            abort(404)
        return zip_response(files, f"{secure_filename(A.biz())}-{A.packet_ref(p)}.zip")

    @app.route("/c/<token>/copies.zip")
    def cosign_copies_zip(token):
        s = db().execute("SELECT * FROM packet_signers WHERE token=?", (token,)).fetchone() or abort(404)
        p = db().execute("SELECT * FROM packets WHERE id=?", (s["packet_id"],)).fetchone()
        g.company = A.get_company(p["company_id"])
        if p["voided_at"]:
            abort(410)
        mine = {r[0] for r in db().execute("SELECT item_id FROM cosignatures WHERE signer_id=?", (s["id"],))}
        files = signed_files(p["id"], only=mine if not p["completed_at"] else None)
        if not files:
            abort(404)
        return zip_response(files, f"{secure_filename(A.biz())}-{A.packet_ref(p)}.zip")

    @app.route("/packets/<int:pid>/download-all.zip")
    @A.login_required
    def packet_zip(pid):
        p = db().execute("SELECT * FROM packets WHERE id=?", (pid,)).fetchone()
        files = signed_files(pid)
        for f in db().execute("SELECT * FROM client_files WHERE packet_id=? ORDER BY id", (pid,)):
            files.append((f"files-from-client/{f['name']}", os.path.join(A.DATA_DIR, "client_uploads", f["stored"])))
        tmp = os.path.join(A.SIGNED_DIR, f"audit-{pid}-{secrets.token_hex(4)}.pdf")
        try:
            items = [A.cosign_names(dict(i)) for i in db().execute("SELECT * FROM packet_items WHERE packet_id=? ORDER BY position", (pid,))]
            events = [dict(e) for e in db().execute("SELECT * FROM events WHERE packet_id=? ORDER BY at, id", (pid,))]
            A.pdfgen.audit_trail(tmp, A.biz(), dict(p), items, events)
            A.seal.seal_pdf(tmp, A.DATA_DIR, A.biz(), reason=f"Audit trail · {A.packet_ref(p)}", location=A.PLATFORM_NAME)
            files.append((f"audit-trail-{A.packet_ref(p)}.pdf", tmp))
            resp = zip_response(files, f"{secure_filename(p['client_name']) or 'packet'}-{A.packet_ref(p)}.zip")
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return resp

    # ------------------------------------------------ 6  backups
    def backup_status():
        files = sorted((f for f in os.listdir(backup_dir) if f.startswith("formsign-backup-") and f.endswith(".zip")),
                       reverse=True)
        try:
            with open(os.path.join(backup_dir, "status.json")) as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            st = {}
        latest = files[0] if files else None
        return {"latest": latest, "latest_at": st.get("finished_at") if latest else None,
                "latest_bytes": os.path.getsize(os.path.join(backup_dir, latest)) if latest else 0,
                "count": len(files), "offsite": bool(offsite_settings()), "offsite_ok": st.get("offsite_ok"),
                "offsite_error": st.get("offsite_error") or "", "error": st.get("error") or "", "keep": BACKUP_KEEP}

    A.backup_status = backup_status

    def backup_lock():
        """Only one server process backs up or restores at a time. Returns the lock file, or None if busy."""
        lock = open(os.path.join(backup_dir, ".lock"), "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            return None
        return lock

    def backup_unlock(lock):
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()

    def make_backup():
        """One ZIP with the database and every stored file."""
        lock = backup_lock()
        if not lock:
            return None
        try:
            return _make_backup()
        finally:
            backup_unlock(lock)

    def _make_backup():
        status = {"started_at": iso(utcnow())}
        try:
            stamp = datetime.now(A.LOCAL_TZ).strftime("%Y%m%d-%H%M%S")
            name = f"formsign-backup-{stamp}.zip"
            tmp_db = os.path.join(backup_dir, f".db-{secrets.token_hex(4)}.sqlite")
            src = sqlite3.connect(A.DB_PATH)
            dst = sqlite3.connect(tmp_db)
            with dst:
                src.backup(dst)   # consistent copy even while the app is in use
            src.close(); dst.close()
            tmp_zip = os.path.join(backup_dir, f".{name}.part")
            with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
                z.write(tmp_db, "formsign.db")
                for folder in ("templates", "signed", "client_uploads", "brand", "seal"):
                    root = os.path.join(A.DATA_DIR, folder)
                    for dirpath, _dirs, fnames in os.walk(root):
                        for fn in fnames:
                            full = os.path.join(dirpath, fn)
                            z.write(full, os.path.relpath(full, A.DATA_DIR))
                z.writestr("README.txt", "FormSign backup. To restore: stop the app, unzip into the DATA_DIR folder "
                                         "(formsign.db and the folders go at its top level), then start the app.\n")
            os.remove(tmp_db)
            os.replace(tmp_zip, os.path.join(backup_dir, name))
            status.update(finished_at=iso(utcnow()), file=name)
            if offsite_settings():
                try:
                    offsite_upload(os.path.join(backup_dir, name), name)
                    status["offsite_ok"] = True
                except Exception as exc:  # noqa: BLE001
                    status.update(offsite_ok=False, offsite_error=str(exc)[:300])
                    app.logger.error("Off-site backup copy failed: %s", exc)
            for old in sorted((f for f in os.listdir(backup_dir) if f.startswith("formsign-backup-") and f.endswith(".zip")),
                              reverse=True)[BACKUP_KEEP:]:
                os.remove(os.path.join(backup_dir, old))
            return name
        except Exception as exc:  # noqa: BLE001
            status["error"] = str(exc)[:300]
            app.logger.exception("Backup failed")
            return None
        finally:
            with open(os.path.join(backup_dir, "status.json"), "w") as fh:
                json.dump(status, fh)

    A.make_backup = make_backup

    # ------------------------------------------------ 6b  restore a backup
    def backup_files():
        """Backups on this server's disk, newest first: nightly ones and ones uploaded from the console."""
        out = []
        for f in os.listdir(backup_dir):
            if f.endswith(".zip") and (f.startswith("formsign-backup-") or f.startswith("formsign-upload-")):
                full = os.path.join(backup_dir, f)
                out.append({"name": f, "bytes": os.path.getsize(full),
                            "at": iso(datetime.fromtimestamp(os.path.getmtime(full), timezone.utc)),
                            "kind": "uploaded" if f.startswith("formsign-upload-") else "nightly"})
        return sorted(out, key=lambda x: x["at"], reverse=True)

    def check_backup_zip(path):
        """Raises ValueError with a plain message if the file isn't a usable FormSign backup."""
        try:
            z = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError):
            raise ValueError("That file isn't a ZIP file.") from None
        with z:
            names = z.namelist()
            if "formsign.db" not in names:
                raise ValueError("That ZIP isn't a FormSign backup (formsign.db is missing).")
            for n in names:
                parts = n.replace("\\", "/").split("/")
                if n.startswith("/") or ".." in parts or (parts[0] not in RESTORE_FOLDERS
                                                          and n not in ("formsign.db", "README.txt")):
                    raise ValueError(f"That ZIP has an unexpected file in it ({n[:80]}).")
            tmp = os.path.join(backup_dir, f".check-{secrets.token_hex(4)}.sqlite")
            try:
                with z.open("formsign.db") as src, open(tmp, "wb") as out:
                    shutil.copyfileobj(src, out)
                con = sqlite3.connect(tmp)
                try:
                    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if not {"companies", "users", "packets"} <= tables:
                        raise ValueError("That backup's database is from a different app.")
                    if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise ValueError("That backup's database is damaged.")
                    companies = con.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
                    packets = con.execute("SELECT COUNT(*) FROM packets").fetchone()[0]
                finally:
                    con.close()
            except sqlite3.DatabaseError:
                raise ValueError("That backup's database can't be read.") from None
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
        return {"companies": companies, "packets": packets, "files": sum(1 for n in names if "/" in n)}

    def restart_soon():
        """Under gunicorn, ask the main process to restart the workers gracefully so nothing cached survives."""
        import signal
        import sys
        if "gunicorn" not in os.path.basename(sys.argv[0] if sys.argv else ""):
            return False
        threading.Timer(1.5, lambda: os.kill(os.getppid(), signal.SIGHUP)).start()
        return True

    def restore_backup(name):
        """Put a backup back: saves today's data first, then swaps in the backup's database and files.
        Returns a dict for the console; raises ValueError with a plain message."""
        if name not in {f["name"] for f in backup_files()}:
            raise ValueError("That backup isn't on the server any more.")
        lock = backup_lock()
        if not lock:
            raise ValueError("A backup or restore is already running. Try again in a minute.")
        work = os.path.join(A.DATA_DIR, f"restore-tmp-{secrets.token_hex(4)}")
        try:
            src_zip = os.path.join(backup_dir, name)
            info = check_backup_zip(src_zip)
            with zipfile.ZipFile(src_zip) as z:   # unpack first: the safety backup may prune old ZIPs
                z.extractall(work)
            safety = _make_backup()
            if not safety:
                raise ValueError("Couldn't save today's data first, so nothing was changed. "
                                 + (backup_status().get("error") or ""))
            # files: copy over what's there (newer files that aren't in the backup are left alone)
            for folder in RESTORE_FOLDERS:
                src = os.path.join(work, folder)
                if os.path.isdir(src):
                    shutil.copytree(src, os.path.join(A.DATA_DIR, folder), dirs_exist_ok=True)
            # database: copied page by page into the live file, under SQLite's own lock
            restored = sqlite3.connect(os.path.join(work, "formsign.db"))
            live = sqlite3.connect(A.DB_PATH, timeout=30)
            try:
                with live:
                    restored.backup(live)
            finally:
                restored.close(); live.close()
            A.init_db()   # adds any columns newer than the backup
            try:
                A.seal._identity = None
            except AttributeError:
                pass
            result = {"restored": name, "safety": safety, **info, "at": iso(utcnow())}
            with open(os.path.join(backup_dir, "restore.json"), "w") as fh:
                json.dump(result, fh)
            app.logger.warning("Backup %s restored (today's data saved as %s)", name, safety)
            result["restarted"] = restart_soon()
            return result
        finally:
            shutil.rmtree(work, ignore_errors=True)
            backup_unlock(lock)

    def save_uploaded_backup(stream, length):
        """Store a backup ZIP sent from the console so it can be restored. Keeps the 3 newest uploads."""
        free = shutil.disk_usage(backup_dir).free
        if length and length * 2.5 > free:
            raise ValueError("There isn't enough free space on the app's disk to restore that backup. "
                             "Make the disk bigger on Render first.")
        stamp = datetime.now(A.LOCAL_TZ).strftime("%Y%m%d-%H%M%S")
        name = f"formsign-upload-{stamp}.zip"
        tmp = os.path.join(backup_dir, f".{name}.part")
        try:
            with open(tmp, "wb") as out:
                shutil.copyfileobj(stream, out, 1024 * 1024)
            info = check_backup_zip(tmp)
            os.replace(tmp, os.path.join(backup_dir, name))
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        for old in sorted(f for f in os.listdir(backup_dir) if f.startswith("formsign-upload-"))[:-3]:
            os.remove(os.path.join(backup_dir, old))
        return {"file": name, **info}

    def last_restore():
        try:
            with open(os.path.join(backup_dir, "restore.json")) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    A.backup_files, A.restore_backup, A.save_uploaded_backup = backup_files, restore_backup, save_uploaded_backup
    A.check_backup_zip, A.last_restore = check_backup_zip, last_restore

    def backup_due():
        st = backup_status()
        last = parse(st["latest_at"]) if st["latest_at"] else None
        if not last:
            return True
        age = utcnow() - last
        hour = datetime.now(A.LOCAL_TZ).hour
        return age > timedelta(hours=36) or (age > timedelta(hours=20) and 1 <= hour <= 5)

    def _loop():
        time.sleep(30)
        while True:
            try:
                with app.app_context():
                    if os.environ.get("BACKUPS", "1") != "0" and backup_due():
                        make_backup()
            except Exception:  # noqa: BLE001
                app.logger.exception("Backup check failed")
            time.sleep(600)

    @app.before_request
    def daily_before():
        if state["thread"] is None and not app.config.get("EXTRAS_NO_THREAD"):
            state["thread"] = threading.Thread(target=_loop, daemon=True, name="backups")
            state["thread"].start()
        return access_gate()

    # ------------------------------------------------ 7  restarts: health check for the signing pages
    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True, "time": A.now()})

    # ------------------------------------------------ 8  access code
    GATE = re.compile(r"^/(s|c)/([^/]+)(/.*)?$")

    def gate_packet(kind, token):
        if kind == "s":
            return db().execute("SELECT * FROM packets WHERE token=?", (token,)).fetchone()
        s = db().execute("SELECT packet_id FROM packet_signers WHERE token=?", (token,)).fetchone()
        return db().execute("SELECT * FROM packets WHERE id=?", (s[0],)).fetchone() if s else None

    def access_gate():
        m = GATE.match(request.path)
        if not m:
            return None
        kind, token, rest = m.group(1), m.group(2), m.group(3) or ""
        if rest == "/code":
            return None
        p = gate_packet(kind, token)
        if not p or not p["access_code"]:
            return None
        if str(p["id"]) in session.get("codes_ok", {}):
            return None
        k = session.get("kiosk")
        if k and k.get("pid") == p["id"]:
            return None   # signing in person: staff are right there
        return redirect(f"/{kind}/{token}/code")

    def code_page(kind, token):
        p = gate_packet(kind, token) or abort(404)
        g.company = A.get_company(p["company_id"])
        if not p["access_code"]:
            return redirect(f"/{kind}/{token}")
        key = str(p["id"])
        fails = [t for t in state["fails"].get(key, []) if t > time.time() - 900]
        state["fails"][key] = fails
        error = None
        if request.method == "POST":
            if len(fails) >= 5:
                error = "Too many tries. Wait 15 minutes, or ask the business for the code again."
            elif hmac.compare_digest(re.sub(r"\D", "", request.form.get("code", "")), p["access_code"]):
                ok = dict(session.get("codes_ok", {}))
                ok[key] = True
                session["codes_ok"] = ok
                state["fails"].pop(key, None)
                return redirect(f"/{kind}/{token}")
            else:
                state["fails"][key] = fails + [time.time()]
                time.sleep(0.5)
                error = "That code isn't right. Check the code the business gave you."
        return render_template("access_code.html", p=p, error=error)

    @app.route("/s/<token>/code", methods=["GET", "POST"])
    def client_code(token):
        return code_page("s", token)

    @app.route("/c/<token>/code", methods=["GET", "POST"])
    def cosign_code(token):
        return code_page("c", token)

    def new_code():
        return f"{secrets.randbelow(10000):04d}"

    @app.route("/packets/<int:pid>/access-code", methods=["POST"])
    @A.login_required
    def packet_access_code(pid):
        action = request.form.get("action")
        code = new_code() if action in ("on", "new") else ""
        db().execute("UPDATE packets SET access_code=? WHERE id=?", (code, pid))
        A.log_event(pid, "details", (f"access code {'changed' if action == 'new' else 'turned on'}" if code else
                                     "access code turned off") + f" · by {A.actor()}")
        db().commit()
        flash(f"Access code: {code}. Give it to your client by phone or in person, not in the same message as the link."
              if code else "Access code turned off: the link opens without a code.", "ok")
        return redirect(url_for("packet_detail", pid=pid))

    def packet_created(pid):
        """Called by New packet after saving: access code and background previews."""
        if request.form.get("access_code") == "on":
            db().execute("UPDATE packets SET access_code=? WHERE id=?", (new_code(), pid))
            A.log_event(pid, "details", f"access code turned on · by {A.actor()}")

    A.daily_packet_created = packet_created

    # ------------------------------------------------ 9  earlier clients while typing
    @app.route("/packets/clients.json")
    @A.login_required
    def clients_json():
        q = request.args.get("q", "").strip()[:60]
        if len(q) < 2:
            return jsonify([])
        cond, args = A.packet_scope()
        like = f"%{q.replace('%', '').replace('_', '')}%"
        rows = db().execute(f"SELECT client_name, client_email, MAX(created_at) AS last FROM packets WHERE {cond} AND "
                            f"(client_name LIKE ? OR client_email LIKE ?) GROUP BY client_name COLLATE NOCASE, "
                            f"client_email COLLATE NOCASE ORDER BY last DESC LIMIT 8", args + [like, like]).fetchall()
        return jsonify([{"name": r[0], "email": r[1] or ""} for r in rows])

    # ------------------------------------------------ 10  prepare previews in the background
    def warm(item_rows):
        def job():
            with app.app_context():
                for it in item_rows:
                    try:
                        if it["template_kind"] == "file" and (it["file_mime"] or "") in A.IMAGE_MIMES:
                            if hasattr(A, "make_display_copy"):   # Fabricator app shrinks big photos
                                A.make_display_copy(it["template_file"])
                            continue
                        v = A.item_viewer(it)   # converts Word/Excel to a PDF preview the first time
                        if v.get("mode") == "pages":
                            for n in range(min(v["count"], 3)):
                                out = os.path.join(A.PAGE_DIR, f"{os.path.splitext(v['pdf'])[0]}-{n}.png")
                                A.preview.page_png(os.path.join(A.UPLOAD_DIR, v["pdf"]), n, out)
                    except Exception:  # noqa: BLE001
                        app.logger.warning("Background preview failed for %s", it["template_file"], exc_info=True)
        if app.config.get("EXTRAS_NO_THREAD"):
            job()
        else:
            threading.Thread(target=job, daemon=True, name="previews").start()

    def warm_packet(pid):
        warm([dict(r) for r in db().execute("SELECT * FROM packet_items WHERE packet_id=? AND template_file IS NOT NULL",
                                            (pid,))])

    def warm_template(tid):
        t = db().execute("SELECT * FROM templates WHERE id=?", (tid,)).fetchone()
        if t and t["file_path"]:
            warm([{"id": 0, "template_kind": t["kind"], "template_file": t["file_path"], "file_name": t["file_name"],
                   "file_mime": t["file_mime"]}])

    A.warm_packet, A.warm_template = warm_packet, warm_template

    # ------------------------------------------------ 13  verify a signed PDF (no purchased certificate needed)
    @app.route("/verify", methods=["GET", "POST"])
    def verify():
        result = None
        if request.method == "POST":
            f = request.files.get("file")
            if not f or not f.filename:
                result = {"ok": False, "message": "Choose the signed PDF to check."}
            else:
                h = hashlib.sha256()
                for chunk in iter(lambda: f.stream.read(65536), b""):
                    h.update(chunk)
                digest = h.hexdigest()
                row = db().execute(
                    "SELECT i.template_name, i.signer_name, i.signed_at, i.id AS iid, p.*, c.name AS company_name "
                    "FROM packet_items i JOIN packets p ON p.id=i.packet_id JOIN companies c ON c.id=p.company_id "
                    "WHERE i.signed_sha256=?", (digest,)).fetchone()
                if row:
                    co = [r[0] for r in db().execute("SELECT signer_name FROM cosignatures WHERE item_id=?", (row["iid"],))]
                    result = {"ok": True, "document": row["template_name"], "business": row["company_name"],
                              "signers": [row["signer_name"]] + co, "signed_at": row["signed_at"],
                              "ref": A.packet_ref(row), "digest": digest}
                else:
                    result = {"ok": False, "digest": digest,
                              "message": "This file doesn't match any signed document. It may have been changed after "
                                         "signing, or it may be an older copy replaced when a second owner signed."}
        info = A.seal.certificate_summary(A.DATA_DIR, A.PLATFORM_NAME)
        der = cert_der()
        info["fingerprint"] = ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())
        return render_template("verify.html", result=result, cert=info)

    def cert_der():
        from cryptography.hazmat.primitives import serialization
        _key, cert, _extra = A.seal._load_identity(A.DATA_DIR, A.PLATFORM_NAME)
        return cert.public_bytes(serialization.Encoding.DER)

    @app.route("/verify/certificate.cer")
    def verify_certificate():
        return send_file(io.BytesIO(cert_der()), mimetype="application/pkix-cert", as_attachment=True,
                         download_name=f"{secure_filename(A.PLATFORM_NAME) or 'FormSign'}-seal-certificate.cer")

    # ------------------------------------------------ 15  help
    @app.route("/help")
    @A.login_required
    def help_page():
        return render_template("help.html", has_approvals=hasattr(A, "collect_approvals"))
