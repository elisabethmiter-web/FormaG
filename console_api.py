"""Extra owner API used by the Admin Console (company timeline, health data, support reply templates,
"Liz is typing" in the staff chat, and a backup history).

Same protection as admin_api.py: off unless ADMIN_API_TOKEN is set, and every /api/admin/... request must send
"Authorization: Bearer <token>". Keep this file identical in every FormSign app.

  GET  /api/admin/companies/<cid>/timeline   what happened to one company, newest first
  GET  /api/admin/health-data[?company=<cid>] numbers for the console's health score (packets the 30 days before, chats waiting)
  GET  /api/admin/support/waiting            open chats waiting for an answer, and since when
  POST /api/admin/support/<tid>/typing       the owner is typing an answer (shown to the staff member for 6 seconds)
  GET/POST /api/admin/reply-templates        saved answers for the support inbox
  GET  /api/admin/backup/history             every backup made (or failed), with its size
  GET  /support/typing.json                  staff side: is the owner typing an answer to me?

Monthly invoices, invoice PDFs, "What's new" notes and the console's Status page:
  GET/POST /api/admin/invoices/run-monthly   preview ({"dry_run": true} or GET) or create this month's invoices now
  GET  /api/admin/invoices/<iid>/pdf         the invoice as a PDF (with HST and a PAID stamp once paid)
  POST /api/admin/companies/<cid>/billing-settings   custom monthly price (blank = default) and billing address
  GET/POST /api/admin/whatsnew               release notes shown in the app ("What's new"); DELETE /api/admin/whatsnew/<key>
  GET  /api/admin/status                     version, last restart, recent errors, slow pages, 5-minute heartbeats
  POST /api/admin/status/clear               {"what": "errors" | "slow"}
  GET  /whatsnew.json, POST /whatsnew/seen   staff side: the notes, and which ones this person has read

A background thread (started with the first request) writes a heartbeat every 5 minutes and, in the first days of
each month, creates that month's invoice for every active paying company (AUTO_INVOICES=0 turns that off).
"""
import fcntl
import hmac
import io
import json
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import wraps

from flask import Response, abort, g, got_request_exception, jsonify, request

TYPING_SECONDS = 6
SCHEMA = """
CREATE TABLE IF NOT EXISTS company_events (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  detail TEXT DEFAULT '',
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_company_events ON company_events(company_id, at);
CREATE TABLE IF NOT EXISTS reply_templates (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS support_typing (
  thread_id INTEGER PRIMARY KEY,
  name TEXT DEFAULT '',
  at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS backup_history (
  id INTEGER PRIMARY KEY,
  key TEXT UNIQUE NOT NULL,
  file TEXT DEFAULT '',
  at TEXT NOT NULL,
  bytes INTEGER DEFAULT 0,
  ok INTEGER NOT NULL DEFAULT 1,
  error TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS invoice_runs (
  id INTEGER PRIMARY KEY,
  period TEXT NOT NULL,
  at TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'auto',
  created INTEGER NOT NULL DEFAULT 0,
  numbers TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS whatsnew (
  id INTEGER PRIMARY KEY,
  key TEXT UNIQUE NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  day TEXT NOT NULL,
  important INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS whatsnew_state (
  user_id INTEGER NOT NULL,
  note_id INTEGER NOT NULL,
  read_at TEXT,
  toasted_at TEXT,
  PRIMARY KEY (user_id, note_id)
);
CREATE TABLE IF NOT EXISTS app_errors (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  method TEXT DEFAULT '',
  path TEXT DEFAULT '',
  error_type TEXT DEFAULT '',
  message TEXT DEFAULT '',
  trace TEXT DEFAULT '',
  company_id INTEGER
);
CREATE TABLE IF NOT EXISTS slow_requests (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  method TEXT DEFAULT '',
  path TEXT DEFAULT '',
  ms INTEGER NOT NULL,
  status INTEGER
);
CREATE TABLE IF NOT EXISTS heartbeats (
  slot INTEGER PRIMARY KEY,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_starts (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  pid INTEGER
);
"""
SLOT_SECONDS = 300            # one heartbeat per 5 minutes
KEEP_ROWS = 200               # errors and slow pages kept
PROCESS_STARTED = datetime.now(timezone.utc).replace(microsecond=0)

# what the console's actions mean on the company timeline
ACTION_EVENTS = {"mark_paid": "marked_paid", "extend_trial": "demo_extended", "suspend": "paused",
                 "activate": "turned_on", "storage": "storage", "reset_password": "password_reset"}


def init_db(conn):
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
    if cols and "monthly_price_cents" not in cols:
        conn.execute("ALTER TABLE companies ADD COLUMN monthly_price_cents INTEGER")   # NULL = the default price
    if cols and "billing_address" not in cols:
        conn.execute("ALTER TABLE companies ADD COLUMN billing_address TEXT DEFAULT ''")
    icols = {r[1] for r in conn.execute("PRAGMA table_info(invoices)")}
    if icols:
        for col, kind in (("subtotal_cents", "INTEGER"), ("tax_cents", "INTEGER"), ("tax_rate", "REAL"),
                          ("tax_label", "TEXT DEFAULT ''"), ("auto", "INTEGER NOT NULL DEFAULT 0")):
            if col not in icols:
                conn.execute(f"ALTER TABLE invoices ADD COLUMN {col} {kind}")
        # an automatic invoice is made at most once per company and month
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_invoices_auto ON invoices(company_id, period) WHERE auto=1")


# ---------------------------------------------------------------- invoice settings (read when used, so tests can change them)
def env_cents(name, default):
    return parse_cents(os.environ.get(name, default)) or parse_cents(default) or 0


def parse_cents(v):
    """'185', '$1,185.50' -> cents; None if it isn't an amount."""
    try:
        d = Decimal(str(v).replace("$", "").replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite() or d < 0 or d > 1_000_000:
        return None
    return int((d * 100).quantize(Decimal("1"), ROUND_HALF_UP))


def invoice_settings():
    try:
        rate = float(os.environ.get("INVOICE_TAX_RATE", "13") or 0)
    except ValueError:
        rate = 13.0
    try:
        due_days = max(0, min(int(os.environ.get("INVOICE_DUE_DAYS", "15")), 120))
    except ValueError:
        due_days = 15
    email = os.environ.get("INVOICE_FROM_EMAIL", "liz@helmmsp.ca")
    return {"default_cents": env_cents("INVOICE_DEFAULT_AMOUNT", "185.00"),
            "currency": (os.environ.get("INVOICE_CURRENCY", "CAD") or "CAD").upper()[:3],
            "tax_rate": max(0.0, min(rate, 50.0)), "tax_label": os.environ.get("INVOICE_TAX_LABEL", "HST") or "Tax",
            "due_days": due_days, "prefix": re.sub(r"[^A-Za-z0-9]", "", os.environ.get("INVOICE_PREFIX", "FS"))[:8] or "FS",
            "from_name": os.environ.get("INVOICE_FROM_NAME", "Helm MSP Inc."),
            "from_address": [x.strip() for x in re.split(r"[|\n]", os.environ.get("INVOICE_FROM_ADDRESS", "").replace("\\n", "\n"))
                             if x.strip()],
            "from_email": email, "hst_number": os.environ.get("HST_NUMBER", "").strip(),
            "pay_instructions": os.environ.get("INVOICE_PAY_INSTRUCTIONS", "")
            or f"Pay by Interac e-Transfer to {email}. Include the invoice number.",
            "auto": os.environ.get("AUTO_INVOICES", "1") != "0",
            "catchup_days": 5}


def tax_of(subtotal_cents, rate):
    return int((Decimal(subtotal_cents) * Decimal(str(rate)) / 100).quantize(Decimal("1"), ROUND_HALF_UP))


def rate_label(rate):
    return f"{rate:g}%"


def money(cents, currency=""):
    return f"${(cents or 0) / 100:,.2f}" + (f" {currency}" if currency else "")


def long_day(v):
    try:
        d = datetime.strptime(str(v)[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return v or ""
    return f"{d:%B} {d.day}, {d.year}"


def monthly_plan(conn, today, create=False, kind="auto"):
    """This month's automatic invoices: what exists, what would be (or now is) created, and who's skipped.
    conn: a sqlite3 connection with sqlite3.Row rows. today: the local date."""
    st = invoice_settings()
    first = today.replace(day=1)
    period = first.strftime("%B %Y")
    issued = first
    due = max(first, today) + timedelta(days=st["due_days"])
    base = f"{st['prefix']}-{first:%Y}-{first:%m}-"
    seq = 0
    for (num_,) in conn.execute("SELECT number FROM invoices WHERE number LIKE ?", (base + "%",)):
        tail = num_[len(base):]
        if tail.isdigit():
            seq = max(seq, int(tail))
    have = {}
    for r in conn.execute("SELECT company_id, number FROM invoices WHERE period=?", (period,)):
        have.setdefault(r[0], r[1])
    out = {"period": period, "issued_on": issued.isoformat(), "due_on": due.isoformat(), "today": today.isoformat(),
           "would_create": [], "created": [], "already": [], "skipped": [],
           "settings": {k: st[k] for k in ("default_cents", "currency", "tax_rate", "tax_label", "due_days", "prefix", "auto")}}
    for c in conn.execute("SELECT * FROM companies ORDER BY name COLLATE NOCASE"):
        c = dict(c)
        if c.get("status") != "active":
            out["skipped"].append({"company_id": c["id"], "company": c["name"], "reason": "Paused"})
            continue
        if (c.get("plan") or "paid") == "trial":
            out["skipped"].append({"company_id": c["id"], "company": c["name"], "reason": "On a demo"})
            continue
        if c["id"] in have:
            out["already"].append({"company_id": c["id"], "company": c["name"], "number": have[c["id"]]})
            continue
        custom = c.get("monthly_price_cents")
        sub = custom if isinstance(custom, int) and custom >= 0 else st["default_cents"]
        tax = tax_of(sub, st["tax_rate"])
        seq += 1
        number = f"{base}{seq:04d}"
        while conn.execute("SELECT 1 FROM invoices WHERE number=?", (number,)).fetchone():
            seq += 1
            number = f"{base}{seq:04d}"
        row = {"company_id": c["id"], "company": c["name"], "number": number, "subtotal_cents": sub, "tax_cents": tax,
               "total_cents": sub + tax, "custom_price": isinstance(custom, int)}
        out["would_create"].append(row)
        if create:
            try:
                conn.execute(
                    "INSERT INTO invoices (company_id, number, period, issued_on, due_on, amount_cents, currency, status, note, "
                    "created_at, subtotal_cents, tax_cents, tax_rate, tax_label, auto) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                    (c["id"], number, period, issued.isoformat(), due.isoformat(), sub + tax, st["currency"], "unpaid", "",
                     iso(utcnow()), sub, tax, st["tax_rate"], st["tax_label"]))
                out["created"].append(row)
            except sqlite3.IntegrityError:   # another server process made it a moment ago
                seq -= 1
    if create:
        conn.execute("INSERT INTO invoice_runs (period, at, kind, created, numbers) VALUES (?,?,?,?,?)",
                     (period, iso(utcnow()), kind, len(out["created"]), ", ".join(r["number"] for r in out["created"])[:2000]))
        conn.execute("DELETE FROM invoice_runs WHERE id NOT IN (SELECT id FROM invoice_runs ORDER BY id DESC LIMIT 100)")
        conn.commit()
    last = conn.execute("SELECT * FROM invoice_runs ORDER BY id DESC LIMIT 1").fetchone()
    out["last_run"] = dict(last) if last else None
    out["totals"] = {"count": len(out["would_create"]), "subtotal_cents": sum(r["subtotal_cents"] for r in out["would_create"]),
                     "tax_cents": sum(r["tax_cents"] for r in out["would_create"]),
                     "total_cents": sum(r["total_cents"] for r in out["would_create"])}
    return out


def invoice_pdf(inv, company, paid_stamp=True):
    """A one-page invoice in the same quiet style as the signed documents."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    st = invoice_settings()
    ink, muted, rule, accent = (colors.HexColor(x) for x in ("#1d2433", "#5b6475", "#d5d9e2", "#24408e"))
    good = colors.HexColor("#1d7a4f")
    body = ParagraphStyle("b", fontName="Helvetica", fontSize=10, leading=14, textColor=ink)
    small = ParagraphStyle("s", fontName="Helvetica", fontSize=8.5, leading=11.5, textColor=muted)
    label = ParagraphStyle("l", fontName="Helvetica-Bold", fontSize=8, leading=11, textColor=muted)
    h1 = ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=15, leading=19, textColor=ink)
    big = ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=22, leading=26, textColor=accent, alignment=2)
    right = ParagraphStyle("r", parent=body, alignment=2)
    rightb = ParagraphStyle("rb", parent=right, fontName="Helvetica-Bold")

    def p(text, style=body):
        return Paragraph(escape(str(text or "")).replace("\n", "<br/>"), style)

    cur = inv.get("currency") or st["currency"]
    total = inv.get("amount_cents") or 0
    sub = inv.get("subtotal_cents")
    tax = inv.get("tax_cents")
    has_tax = sub is not None and tax is not None
    if not has_tax:
        sub, tax = total, 0
    rate = inv.get("tax_rate") if inv.get("tax_rate") is not None else st["tax_rate"]
    tlabel = inv.get("tax_label") or st["tax_label"]
    period = inv.get("period") or ""

    sender = [p(st["from_name"], h1)] + [p(x, small) for x in st["from_address"]] + [p(st["from_email"], small)]
    if st["hst_number"]:
        sender.append(p(f"{tlabel} registration no. {st['hst_number']}", small))
    meta = [[p("Invoice no.", label), p(inv.get("number"), rightb)], [p("Invoice date", label), p(long_day(inv.get("issued_on")), right)]]
    if inv.get("due_on"):
        meta.append([p("Due date", label), p(long_day(inv.get("due_on")), right)])
    if period:
        meta.append([p("Period", label), p(period, right)])
    mt = Table(meta, colWidths=[1.0 * inch, 1.9 * inch])
    mt.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    head = Table([[sender, [p("INVOICE", big), Spacer(1, 8), mt]]], colWidths=[3.6 * inch, 2.9 * inch])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))

    bill = [p("BILL TO", label), p(company.get("name") or "", ParagraphStyle("bn", parent=body, fontName="Helvetica-Bold"))]
    if company.get("contact_name"):
        bill.append(p(company["contact_name"]))
    addr = (company.get("billing_address") or company.get("mailing_address") or "").strip()
    if addr:
        bill.append(p(addr))
    mail = company.get("contact_email") or company.get("notify_email")
    if mail:
        bill.append(p(mail))

    line = f"FormSign monthly subscription — {period}" if period else "FormSign subscription"
    if inv.get("note"):
        line += f"\n{inv['note']}"
    rows = [[p("DESCRIPTION", label), p("AMOUNT", ParagraphStyle("la", parent=label, alignment=2))],
            [p(line), p(money(sub), right)]]
    tot = []
    if has_tax:
        tot += [["", p("Subtotal", right), p(money(sub), right)],
                ["", p(f"{tlabel} {rate_label(rate)}", right), p(money(tax), right)]]
    tot += [["", p(f"Total ({cur})", rightb), p(money(total), rightb)]]
    items = Table(rows, colWidths=[5.0 * inch, 1.5 * inch])
    items.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, 0), 0.8, ink), ("LINEBELOW", (0, 1), (-1, -1), 0.5, rule),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    totals = Table(tot, colWidths=[3.5 * inch, 1.5 * inch, 1.5 * inch])
    totals.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                                ("LINEABOVE", (1, -1), (-1, -1), 0.8, ink)]))
    paid = inv.get("status") == "paid"
    story = [head, Spacer(1, 22), *bill, Spacer(1, 20), items, Spacer(1, 6), totals, Spacer(1, 26)]
    if paid:
        story.append(p(f"Paid on {long_day(inv.get('paid_on'))}" + (f" by {inv['method']}" if inv.get("method") else "")
                       + ". Thank you!", ParagraphStyle("pd", parent=body, textColor=good, fontName="Helvetica-Bold")))
    else:
        story += [p("HOW TO PAY", label), Spacer(1, 2), p(st["pay_instructions"])]
        if inv.get("due_on"):
            story.append(p(f"Please pay by {long_day(inv['due_on'])}.", small))
    story += [Spacer(1, 18), p(f"Questions about this invoice? Email {st['from_email']}.", small)]

    def deco(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(accent)
        canvas.setLineWidth(3)
        canvas.line(doc.leftMargin, letter[1] - 0.45 * inch, letter[0] - doc.rightMargin, letter[1] - 0.45 * inch)
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(muted)
        canvas.drawString(doc.leftMargin, 0.5 * inch, f"{st['from_name']} · Invoice {inv.get('number') or ''}")
        if paid and paid_stamp:
            canvas.translate(letter[0] - 2.75 * inch, letter[1] - 3.55 * inch)
            canvas.rotate(18)
            canvas.setStrokeColor(good)
            canvas.setFillColor(good)
            canvas.setLineWidth(3)
            canvas.roundRect(-0.2 * inch, -0.18 * inch, 1.75 * inch, 0.72 * inch, 8, stroke=1, fill=0)
            canvas.setFont("Helvetica-Bold", 30)
            canvas.drawString(0.02 * inch, 0, "PAID")
        canvas.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=inch, rightMargin=inch, topMargin=0.8 * inch, bottomMargin=0.8 * inch,
                            title=f"Invoice {inv.get('number') or ''}", author=st["from_name"])
    doc.build(story, onFirstPage=deco, onLaterPages=deco)
    return buf.getvalue()


def email_text(inv, company_name=""):
    """Short ready-to-paste message to send with the invoice PDF."""
    st = invoice_settings()
    cur = inv.get("currency") or st["currency"]
    lines = ["Hello,", "", f"Here is your FormSign invoice {inv.get('number')}" + (f" for {inv['period']}" if inv.get("period") else "") + ".", ""]
    amount = f"Amount: {money(inv.get('amount_cents'), cur)}"
    if inv.get("tax_cents") is not None and inv.get("subtotal_cents") is not None:
        amount += f" ({money(inv['subtotal_cents'])} + {money(inv['tax_cents'])} {inv.get('tax_label') or st['tax_label']})"
    lines.append(amount)
    if inv.get("due_on"):
        lines.append(f"Due date: {long_day(inv['due_on'])}")
    lines += ["", st["pay_instructions"], "", "The invoice is attached as a PDF. Thank you!", "", st["from_name"]]
    return "\n".join(lines)


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.replace(microsecond=0).isoformat()


def parse(ts):
    try:
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def register(A):
    app, db = A.app, A.db
    token = os.environ.get("ADMIN_API_TOKEN", "")
    backup_dir = os.path.join(A.DATA_DIR, "backups")
    state = {"synced": 0.0}

    def api(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not token:
                abort(404)
            sent = request.headers.get("Authorization", "")
            if not sent.startswith("Bearer ") or not hmac.compare_digest(sent[7:].encode(), token.encode()):
                return jsonify({"error": "unauthorized"}), 401
            return view(*args, **kwargs)
        return wrapper

    def log_event(cid, kind, detail=""):
        db().execute("INSERT INTO company_events (company_id, kind, detail, at) VALUES (?,?,?,?)",
                     (cid, kind, str(detail)[:300], A.now()))

    # ------------------------------------------------ record owner actions as they happen
    @app.after_request
    def console_api_record(resp):
        if request.method != "POST" or resp.status_code >= 400:
            return resp
        try:
            path = request.path
            m = re.fullmatch(r"/api/admin/companies/(\d+)/action", path)
            if m and resp.status_code == 200:
                data = request.get_json(silent=True) or {}
                kind = ACTION_EVENTS.get(data.get("action"))
                if kind:
                    detail = ""
                    if kind == "demo_extended":
                        detail = f"{data.get('days') or 14} days"
                    elif kind == "storage":
                        detail = f"{data.get('storage_gb')} GB"
                    elif kind == "password_reset":
                        u = db().execute("SELECT name FROM users WHERE id=?", (data.get("user_id"),)).fetchone()
                        detail = u["name"] if u else ""
                    log_event(int(m.group(1)), kind, detail)
                    db().commit()
            elif path == "/api/admin/companies" and resp.status_code == 200:
                data = request.get_json(silent=True) or {}
                body = resp.get_json(silent=True) or {}
                if body.get("id"):
                    if data.get("demo") in (True, "on", "1", 1, "true"):
                        log_event(int(body["id"]), "demo_started", "14-day demo")
                        db().commit()
            elif re.fullmatch(r"/platform/\d+", path) and resp.status_code in (200, 302):
                kind = ACTION_EVENTS.get(request.form.get("action"))
                if kind in ("marked_paid", "demo_extended", "paused", "turned_on"):
                    log_event(int(path.rsplit("/", 1)[1]), kind,
                              f"{request.form.get('days') or 14} days" if kind == "demo_extended" else "")
                    db().commit()
            elif re.fullmatch(r"/api/admin/support/\d+", path):
                # an answer was sent: the "typing" note goes away at once
                db().execute("DELETE FROM support_typing WHERE thread_id=?", (int(path.rsplit("/", 1)[1]),))
                db().commit()
            elif path in ("/api/admin/backup", "/api/admin/backup/restore"):
                sync_backups(force=True)
        except Exception:  # noqa: BLE001  (never break the real answer)
            app.logger.exception("console_api: couldn't record an owner action")
        return resp

    # ------------------------------------------------ company timeline
    def timeline(cid):
        c = db().execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
        if not c:
            return None
        c = dict(c)
        ev = []

        def add(at, kind, title, detail="", group="account"):
            if at:
                ev.append({"at": at, "kind": kind, "title": title, "detail": detail, "group": group})

        add(c.get("created_at"), "created", "Signed up" if c.get("source") == "signup" else "Company added",
            "Signed up on the website" + (" (14-day demo)" if c.get("plan") == "trial" else "")
            if c.get("source") == "signup" else "Added from the Admin Console or the owner screen")
        titles = {"demo_started": "Demo started", "demo_extended": "Demo extended", "marked_paid": "Marked as paid",
                  "paused": "Paused", "turned_on": "Turned back on", "storage": "Storage changed",
                  "password_reset": "Password reset", "price": "Monthly price changed"}
        for r in db().execute("SELECT * FROM company_events WHERE company_id=? ORDER BY at DESC LIMIT 300", (cid,)):
            add(r["at"], r["kind"], titles.get(r["kind"], r["kind"].replace("_", " ").capitalize()), r["detail"] or "",
                "usage" if r["kind"] == "password_reset" else "account")
        if c.get("plan") == "trial" and c.get("trial_ends_at") and (parse(c["trial_ends_at"]) or utcnow()) <= utcnow():
            add(c["trial_ends_at"], "demo_ended", "Demo ended", "")
        # invoices
        for r in db().execute("SELECT * FROM invoices WHERE company_id=?", (cid,)):
            amount = f"${r['amount_cents'] / 100:,.2f} {r['currency'] or ''}".strip()
            add(r["issued_on"], "invoice", f"Invoice {r['number']} sent", amount + (f" · {r['period']}" if r["period"] else ""), "billing")
            if r["status"] == "paid" and r["paid_on"]:
                add(r["paid_on"], "payment", f"Invoice {r['number']} paid", amount + (f" · {r['method']}" if r["method"] else ""), "billing")
        # usage
        p = db().execute("SELECT MIN(COALESCE(sent_at, created_at)) first, MAX(COALESCE(sent_at, created_at)) last, "
                         "COUNT(*) n, MIN(completed_at) first_done FROM packets WHERE company_id=?", (cid,)).fetchone()
        if p["n"]:
            add(p["first"], "packet", "First packet sent", "", "usage")
            if p["first_done"]:
                add(p["first_done"], "completed", "First packet completed by a client", "", "usage")
            if p["n"] > 1:
                add(p["last"], "packet", "Latest packet", f"{p['n']} packets in total", "usage")
        for r in db().execute("SELECT name, role, created_at FROM users WHERE company_id=? ORDER BY created_at", (cid,)):
            add(r["created_at"], "user", f"Login added: {r['name']}", "Management" if r["role"] == "manager" else "Staff", "usage")
        last = db().execute("SELECT name, COALESCE(last_seen_at, last_login_at) AS at FROM users WHERE company_id=? "
                            "AND COALESCE(last_seen_at, last_login_at) IS NOT NULL ORDER BY at DESC LIMIT 1", (cid,)).fetchone()
        if last:
            add(last["at"], "login", f"Last seen: {last['name']}", "", "usage")
        # support
        for r in db().execute("SELECT t.created_at, t.status, u.name FROM support_threads t JOIN users u ON u.id=t.user_id "
                              "WHERE t.company_id=?", (cid,)):
            add(r["created_at"], "support", f"Support chat started by {r['name']}",
                "Resolved" if r["status"] == "resolved" else "Open", "support")
        ev.sort(key=lambda e: (parse(e["at"]) or utcnow()), reverse=True)
        return ev[:300]

    @app.route("/api/admin/companies/<int:cid>/timeline")
    @api
    def console_api_timeline(cid):
        ev = timeline(cid)
        if ev is None:
            abort(404)
        return jsonify({"events": ev, "recording_since": (db().execute(
            "SELECT MIN(at) FROM company_events").fetchone()[0])})

    # ------------------------------------------------ numbers for the health score
    def waiting_threads(company_id=None):
        """Open chats whose newest message is from staff: thread id -> (company id, waiting since)."""
        sql = ("SELECT t.id, t.company_id, (SELECT MIN(m.created_at) FROM support_messages m WHERE m.thread_id=t.id "
               "AND m.sender='user' AND m.id > COALESCE((SELECT MAX(o.id) FROM support_messages o WHERE o.thread_id=t.id "
               "AND o.sender='owner'), 0)) AS since FROM support_threads t WHERE t.status='open'")
        args = []
        if company_id:
            sql += " AND t.company_id=?"
            args.append(company_id)
        return {r["id"]: (r["company_id"], r["since"]) for r in db().execute(sql, args) if r["since"]}

    @app.route("/api/admin/health-data")
    @api
    def console_api_health_data():
        nowdt = utcnow()
        c30, c60 = iso(nowdt - timedelta(days=30)), iso(nowdt - timedelta(days=60))
        one = request.args.get("company", "")
        sql = ("SELECT company_id, SUM(COALESCE(sent_at, created_at) >= ? AND COALESCE(sent_at, created_at) < ?) prev30 "
               "FROM packets")
        args = [c60, c30]
        if one.isdigit():
            sql += " WHERE company_id=?"
            args.append(int(one))
        out = {}
        for r in db().execute(sql + " GROUP BY company_id", args):
            out.setdefault(str(r["company_id"]), {})["packets_prev30"] = r["prev30"] or 0
        for _tid, (cid, since) in waiting_threads(int(one) if one.isdigit() else None).items():
            d = out.setdefault(str(cid), {})
            d["support_waiting"] = d.get("support_waiting", 0) + 1
            d["support_waiting_since"] = min(d.get("support_waiting_since") or since, since)
        return jsonify({"companies": out, "time": A.now()})

    # ------------------------------------------------ support: waiting, typing, reply templates
    @app.route("/api/admin/support/waiting")
    @api
    def console_api_support_waiting():
        return jsonify({"threads": {str(t): since for t, (_c, since) in waiting_threads().items()}, "time": A.now()})

    @app.route("/api/admin/support/<int:tid>/typing", methods=["POST"])
    @api
    def console_api_typing(tid):
        db().execute("SELECT 1 FROM support_threads WHERE id=?", (tid,)).fetchone() or abort(404)
        data = request.get_json(silent=True) or {}
        if data.get("stop"):
            db().execute("DELETE FROM support_typing WHERE thread_id=?", (tid,))
        else:
            db().execute("INSERT INTO support_typing (thread_id, name, at) VALUES (?,?,?) ON CONFLICT(thread_id) "
                         "DO UPDATE SET name=excluded.name, at=excluded.at",
                         (tid, str(data.get("name") or "Support")[:40], time.time()))
        db().commit()
        return jsonify({"ok": True})

    @app.route("/support/typing.json")
    @A.login_required
    def console_support_typing():
        """Staff side, polled while the support chat is open: is the owner writing an answer right now?"""
        u = g.get("user")
        if not u or not g.get("company") or u["id"] == 0:
            return jsonify({"typing": False})
        r = db().execute("SELECT s.name, s.at FROM support_typing s JOIN support_threads t ON t.id=s.thread_id "
                         "WHERE t.company_id=? AND t.user_id=?", (A.cid(), u["id"])).fetchone()
        typing = bool(r and time.time() - r["at"] < TYPING_SECONDS)
        return jsonify({"typing": typing, "name": r["name"] if typing else ""})

    bg = getattr(sys.modules.get("everyday"), "BACKGROUND", None)
    if isinstance(bg, set):
        bg.add("console_support_typing")   # checking for "typing" doesn't count as activity (idle sign-out still works)

    @app.route("/api/admin/reply-templates", methods=["GET", "POST"])
    @api
    def console_api_reply_templates():
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            action = data.get("action")
            title = str(data.get("title", "")).strip()[:80]
            body = str(data.get("body", "")).strip()[:2000]
            tid = data.get("id")
            if action in ("create", "update"):
                if not title or not body:
                    return jsonify({"error": "Give the answer a short name and some text."}), 400
                if action == "create":
                    db().execute("INSERT INTO reply_templates (title, body, created_at) VALUES (?,?,?)", (title, body, A.now()))
                    msg = f"Saved answer “{title}” added."
                else:
                    n = db().execute("UPDATE reply_templates SET title=?, body=?, updated_at=? WHERE id=?",
                                     (title, body, A.now(), tid)).rowcount
                    if not n:
                        return jsonify({"error": "That saved answer doesn't exist any more."}), 404
                    msg = f"Saved answer “{title}” updated."
            elif action == "delete":
                n = db().execute("DELETE FROM reply_templates WHERE id=?", (tid,)).rowcount
                if not n:
                    return jsonify({"error": "That saved answer doesn't exist any more."}), 404
                msg = "Saved answer deleted."
            else:
                return jsonify({"error": "Unknown action."}), 400
            db().commit()
            return jsonify({"ok": True, "message": msg})
        rows = [dict(r) for r in db().execute("SELECT id, title, body, created_at, updated_at FROM reply_templates "
                                              "ORDER BY title COLLATE NOCASE")]
        return jsonify({"templates": rows})

    # ------------------------------------------------ backup history
    def sync_backups(force=False):
        """Record backups made since last time. The nightly backup runs in its own thread, so instead of hooking it we
        read the backups folder and status.json (the app only keeps the last few) and remember each one by name."""
        if not force and time.time() - state["synced"] < 300:
            return
        state["synced"] = time.time()
        if not os.path.isdir(backup_dir):
            return
        rows = []
        try:
            with open(os.path.join(backup_dir, "status.json")) as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            st = {}
        if st.get("error") and st.get("started_at"):
            rows.append(("failed:" + st["started_at"], "", st["started_at"], 0, 0, str(st["error"])[:300]))
        for f in os.listdir(backup_dir):
            if f.startswith("formsign-backup-") and f.endswith(".zip"):
                full = os.path.join(backup_dir, f)
                try:
                    at = st.get("finished_at") if st.get("file") == f and st.get("finished_at") else \
                        iso(datetime.fromtimestamp(os.path.getmtime(full), timezone.utc))
                    rows.append((f, f, at, os.path.getsize(full), 1, ""))
                except OSError:
                    continue
        if rows:
            db().executemany("INSERT OR IGNORE INTO backup_history (key, file, at, bytes, ok, error) VALUES (?,?,?,?,?,?)", rows)
            db().commit()

    orig_make_backup = getattr(A, "make_backup", None)
    if orig_make_backup and not getattr(orig_make_backup, "_console_api", False):
        def make_backup_recorded(*a, **k):
            name = orig_make_backup(*a, **k)
            try:
                with app.app_context():
                    sync_backups(force=True)
            except Exception:  # noqa: BLE001
                app.logger.exception("console_api: couldn't record the backup")
            return name
        make_backup_recorded._console_api = True
        A.make_backup = make_backup_recorded

    @app.before_request
    def console_api_sync():
        if time.time() - state["synced"] >= 300 and request.endpoint not in ("static",):
            try:
                sync_backups()
            except Exception:  # noqa: BLE001
                app.logger.exception("console_api: backup history sync failed")

    @app.route("/api/admin/backup/history")
    @api
    def console_api_backup_history():
        sync_backups(force=True)
        try:
            days = max(1, min(int(request.args.get("days", "60")), 400))
        except ValueError:
            days = 60
        since = iso(utcnow() - timedelta(days=days))
        tz = getattr(A, "LOCAL_TZ", timezone.utc)
        items = []
        for r in db().execute("SELECT * FROM backup_history WHERE at >= ? ORDER BY at", (since,)):
            dt = parse(r["at"])
            items.append({"file": r["file"], "at": r["at"], "day": dt.astimezone(tz).date().isoformat() if dt else "",
                          "bytes": r["bytes"] or 0, "ok": bool(r["ok"]), "error": r["error"] or ""})
        first = db().execute("SELECT MIN(at) FROM backup_history").fetchone()[0]
        return jsonify({"items": items, "recording_since": first, "today": datetime.now(tz).date().isoformat()})

    # ================================================ monthly invoices
    def local_today():
        return datetime.now(getattr(A, "LOCAL_TZ", timezone.utc)).date()

    def raw_conn():
        conn = sqlite3.connect(A.DB_PATH, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def invoice_lock(blocking):
        """Only one server process creates invoices at a time. Returns the lock file, or None if busy."""
        lock = open(os.path.join(A.DATA_DIR, ".monthly-invoices.lock"), "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except OSError:
            lock.close()
            return None
        return lock

    def run_monthly(today=None, kind="auto", create=True):
        today = today or local_today()
        lock = invoice_lock(blocking=kind != "auto") if create else None
        if create and not lock:
            return None
        try:
            conn = raw_conn()
            try:
                return monthly_plan(conn, today, create=create, kind=kind)
            finally:
                conn.close()
        finally:
            if lock:
                fcntl.flock(lock, fcntl.LOCK_UN)
                lock.close()

    def auto_invoices_due(today=None):
        """The automatic run happens in the first days of the month (catching up if the app was asleep on the 1st)."""
        st = invoice_settings()
        today = today or local_today()
        return st["auto"] and today.day <= st["catchup_days"]

    def auto_invoices(today=None):
        today = today or local_today()
        if not auto_invoices_due(today):
            return None
        plan = run_monthly(today, kind="auto", create=False)
        if not plan["would_create"]:
            return plan
        return run_monthly(today, kind="auto", create=True)

    A.run_monthly_invoices, A.auto_invoices = run_monthly, auto_invoices

    @app.route("/api/admin/invoices/run-monthly", methods=["GET", "POST"])
    @api
    def console_api_run_monthly():
        data = request.get_json(silent=True) or {}
        dry = request.method == "GET" or bool(data.get("dry_run"))
        out = run_monthly(kind="manual", create=not dry)
        out["dry_run"] = dry
        if not dry:
            n = len(out["created"])
            out["message"] = (f"{n} invoice{'s' if n != 1 else ''} created for {out['period']}." if n else
                              f"Nothing to create: every paying company already has an invoice for {out['period']}.")
        return jsonify(out)

    def invoice_row(iid):
        r = db().execute("SELECT * FROM invoices WHERE id=?", (iid,)).fetchone()
        if not r:
            return None, None
        c = db().execute("SELECT * FROM companies WHERE id=?", (r["company_id"],)).fetchone()
        return dict(r), dict(c) if c else {"name": ""}

    @app.route("/api/admin/invoices/<int:iid>/pdf")
    @api
    def console_api_invoice_pdf(iid):
        inv, c = invoice_row(iid)
        if not inv:
            abort(404)
        pdf = invoice_pdf(inv, c)
        name = re.sub(r"[^A-Za-z0-9._-]", "-", f"invoice-{inv['number']}") + ".pdf"
        return Response(pdf, mimetype="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.route("/api/admin/invoices/<int:iid>/email")
    @api
    def console_api_invoice_email(iid):
        inv, c = invoice_row(iid)
        if not inv:
            abort(404)
        return jsonify({"text": email_text(inv, c.get("name")), "number": inv["number"]})

    @app.route("/api/admin/companies/<int:cid>/billing-settings", methods=["POST"])
    @api
    def console_api_billing_settings(cid):
        c = db().execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone() or abort(404)
        data = request.get_json(silent=True) or {}
        raw = str(data.get("monthly_price") or "").strip()
        cents = None
        if raw:
            cents = parse_cents(raw)
            if cents is None:
                return jsonify({"error": "Enter the monthly price like 185.00, or leave it blank for the standard price."}), 400
        addr = str(data.get("billing_address") if data.get("billing_address") is not None else (c["billing_address"] or ""))
        db().execute("UPDATE companies SET monthly_price_cents=?, billing_address=? WHERE id=?", (cents, addr.strip()[:500], cid))
        log_event(cid, "price", "Monthly price " + (money(cents) if cents is not None else "set back to the standard price"))
        db().commit()
        st = invoice_settings()
        return jsonify({"ok": True, "message": f"Monthly price for {c['name']}: " +
                        (f"{money(cents)} + {st['tax_label']}." if cents is not None else f"standard ({money(st['default_cents'])} + {st['tax_label']}).")})

    def public_settings():
        st = invoice_settings()
        return {"default_price": f"{st['default_cents'] / 100:.2f}", "tax_rate": st["tax_rate"], "tax_label": st["tax_label"],
                "currency": st["currency"], "due_days": st["due_days"], "pay_instructions": st["pay_instructions"],
                "from_name": st["from_name"], "from_email": st["from_email"], "auto": st["auto"], "prefix": st["prefix"]}

    def company_billing_extra(cid):
        r = db().execute("SELECT monthly_price_cents, billing_address FROM companies WHERE id=?", (cid,)).fetchone()
        if not r:
            return {}
        p = r["monthly_price_cents"]
        return {"monthly_price": f"{p / 100:.2f}" if p is not None else "", "billing_address": r["billing_address"] or ""}

    # ================================================ What's new (release notes from the platform owner)
    def note_json(r):
        return {"id": r["id"], "key": r["key"], "title": r["title"], "body": r["body"], "day": r["day"],
                "important": bool(r["important"]), "created_at": r["created_at"], "updated_at": r["updated_at"]}

    @app.route("/api/admin/whatsnew", methods=["GET", "POST"])
    @api
    def console_api_whatsnew():
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            key = re.sub(r"[^A-Za-z0-9_-]", "", str(data.get("key") or ""))[:40]
            title = str(data.get("title") or "").strip()[:120]
            body = str(data.get("body") or "").strip()[:4000]
            day = str(data.get("day") or "").strip()[:10] or local_today().isoformat()
            try:
                datetime.strptime(day, "%Y-%m-%d")
            except ValueError:
                return jsonify({"error": "Use a date like 2026-11-01."}), 400
            if not key or not title:
                return jsonify({"error": "Give the note a title."}), 400
            imp = 1 if data.get("important") in (True, 1, "1", "on", "true") else 0
            old = db().execute("SELECT id FROM whatsnew WHERE key=?", (key,)).fetchone()
            if old:
                db().execute("UPDATE whatsnew SET title=?, body=?, day=?, important=?, updated_at=? WHERE id=?",
                             (title, body, day, imp, A.now(), old["id"]))
            else:
                db().execute("INSERT INTO whatsnew (key, title, body, day, important, created_at) VALUES (?,?,?,?,?,?)",
                             (key, title, body, day, imp, A.now()))
            db().commit()
            return jsonify({"ok": True, "message": f"“{title}” {'updated' if old else 'posted'}."})
        rows = db().execute("SELECT w.*, (SELECT COUNT(*) FROM whatsnew_state s WHERE s.note_id=w.id AND s.read_at IS NOT NULL "
                            "AND s.user_id != 0) AS reads FROM whatsnew w ORDER BY day DESC, id DESC").fetchall()
        return jsonify({"notes": [{**note_json(r), "reads": r["reads"]} for r in rows],
                        "people": db().execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]})

    @app.route("/api/admin/whatsnew/<key>", methods=["DELETE"])
    @api
    def console_api_whatsnew_delete(key):
        r = db().execute("SELECT id FROM whatsnew WHERE key=?", (key,)).fetchone()
        if not r:
            return jsonify({"error": "That note doesn't exist any more."}), 404
        db().execute("DELETE FROM whatsnew_state WHERE note_id=?", (r["id"],))
        db().execute("DELETE FROM whatsnew WHERE id=?", (r["id"],))
        db().commit()
        return jsonify({"ok": True, "message": "Note deleted."})

    def my_notes():
        u = g.get("user") or {}
        uid = u.get("id", 0) if isinstance(u, dict) else u["id"]
        joined = (u.get("created_at") if isinstance(u, dict) else None) or ""
        rows = db().execute("SELECT w.*, s.read_at, s.toasted_at FROM whatsnew w LEFT JOIN whatsnew_state s "
                            "ON s.note_id=w.id AND s.user_id=? ORDER BY w.day DESC, w.id DESC LIMIT 50", (uid,)).fetchall()
        notes = []
        for r in rows:
            # notes from before this person's login was made count as read
            unread = not r["read_at"] and not (joined and r["created_at"] < joined)
            notes.append({**note_json(r), "unread": unread, "toasted": bool(r["toasted_at"])})
        return uid, notes

    @app.route("/whatsnew.json")
    @A.login_required
    def console_whatsnew_json():
        _uid, notes = my_notes()
        toast = next((n for n in notes if n["important"] and n["unread"] and not n["toasted"]), None)
        for n in notes:
            n.pop("key", None)
        return jsonify({"notes": notes, "unread": sum(n["unread"] for n in notes),
                        "toast": {"id": toast["id"], "title": toast["title"], "body": toast["body"][:240]} if toast else None})

    @app.route("/whatsnew/seen", methods=["POST"])
    @A.login_required
    def console_whatsnew_seen():
        uid, notes = my_notes()
        data = request.get_json(silent=True) or {}
        known = {n["id"] for n in notes}
        ids = [int(i) for i in (data.get("ids") or []) if str(i).isdigit() and int(i) in known]
        col = "toasted_at" if data.get("kind") == "toast" else "read_at"
        for i in ids:
            db().execute(f"INSERT INTO whatsnew_state (user_id, note_id, {col}) VALUES (?,?,?) "
                         f"ON CONFLICT(user_id, note_id) DO UPDATE SET {col}=COALESCE({col}, excluded.{col})", (uid, i, A.now()))
        db().commit()
        return jsonify({"ok": True, "unread": sum(n["unread"] and n["id"] not in ids for n in notes) if col == "read_at" else None})

    if isinstance(bg, set):
        bg.add("console_whatsnew_json")   # checking for notes doesn't count as activity

    # ================================================ status: errors, slow pages, restarts, heartbeats
    try:
        SLOW_MS = int(float(os.environ.get("SLOW_REQUEST_SECONDS", "2")) * 1000)
    except ValueError:
        SLOW_MS = 2000
    SECRETISH = re.compile(r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key|authorization)\b(\s*[=:]\s*|\s+)(['\"]?)[^\s'\",;)]+")

    def record(sql, args, table):
        """Write to the status tables on a separate connection (the request's own may be mid-transaction)."""
        try:
            conn = sqlite3.connect(A.DB_PATH, timeout=5)
            try:
                conn.execute(sql, args)
                if table:
                    conn.execute(f"DELETE FROM {table} WHERE id <= (SELECT MAX(id) FROM {table}) - ?", (KEEP_ROWS,))
                conn.commit()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001  (never make a bad moment worse)
            app.logger.exception("console_api: couldn't record status data")

    def path_pattern():
        """The page pattern (/s/<token>), never the real address: links can carry signing tokens."""
        rule = getattr(request, "url_rule", None)
        if rule is not None:
            return rule.rule[:200]
        return re.sub(r"/[A-Za-z0-9_-]{16,}", "/…", request.path)[:200]

    def on_exception(sender, exception, **_extra):
        try:
            frames = traceback.extract_tb(exception.__traceback__)[-6:]
            trace = "\n".join(f"{os.path.basename(f.filename)}:{f.lineno} in {f.name}" for f in frames)
            msg = SECRETISH.sub(lambda m: m.group(1) + m.group(2) + "***", str(exception))[:400]
            company = g.get("company")
            record("INSERT INTO app_errors (at, method, path, error_type, message, trace, company_id) VALUES (?,?,?,?,?,?,?)",
                   (iso(utcnow()), request.method, path_pattern(), exception.__class__.__name__, msg, trace,
                    company["id"] if company else None), "app_errors")
        except Exception:  # noqa: BLE001
            app.logger.exception("console_api: couldn't record an error")

    got_request_exception.connect(on_exception, app, weak=False)

    def start_timer():
        g._cs_t0 = time.perf_counter()

    app.before_request_funcs.setdefault(None, []).insert(0, start_timer)   # first, so the whole request is timed

    @app.after_request
    def console_api_slow(resp):
        t0 = g.get("_cs_t0")
        if t0 is not None:
            ms = int((time.perf_counter() - t0) * 1000)
            if ms >= SLOW_MS and not request.path.startswith(("/api/admin/backup", "/static/")):
                record("INSERT INTO slow_requests (at, method, path, ms, status) VALUES (?,?,?,?,?)",
                       (iso(utcnow()), request.method, path_pattern(), ms, resp.status_code), "slow_requests")
        return resp

    record("INSERT INTO app_starts (at, pid) VALUES (?,?)", (iso(PROCESS_STARTED), os.getpid()), "app_starts")

    def heartbeat():
        slot = int(time.time() // SLOT_SECONDS)
        if state.get("beat") == slot:
            return
        conn = sqlite3.connect(A.DB_PATH, timeout=5)
        try:
            conn.execute("INSERT OR IGNORE INTO heartbeats (slot, at) VALUES (?,?)", (slot, iso(utcnow())))
            if slot % 12 == 0:
                conn.execute("DELETE FROM heartbeats WHERE slot < ?", (slot - 8 * 86400 // SLOT_SECONDS,))
            conn.commit()
        finally:
            conn.close()
        state["beat"] = slot

    A.console_heartbeat = heartbeat

    def _loop():
        last_invoice_check = 0.0
        while True:
            try:
                heartbeat()
            except Exception:  # noqa: BLE001
                app.logger.exception("console_api: heartbeat failed")
            if time.time() - last_invoice_check >= 1800:
                last_invoice_check = time.time()
                try:
                    with app.app_context():
                        auto_invoices()
                except Exception:  # noqa: BLE001
                    app.logger.exception("console_api: monthly invoices failed")
            time.sleep(60)

    @app.before_request
    def console_api_thread():
        if state.get("thread") is None and not app.config.get("EXTRAS_NO_THREAD"):
            state["thread"] = threading.Thread(target=_loop, daemon=True, name="console-heartbeat")
            state["thread"].start()

    def status_counts():
        day_ago = iso(utcnow() - timedelta(days=1))
        d = db()
        return {"errors_24h": d.execute("SELECT COUNT(*) FROM app_errors WHERE at >= ?", (day_ago,)).fetchone()[0],
                "slow_24h": d.execute("SELECT COUNT(*) FROM slow_requests WHERE at >= ?", (day_ago,)).fetchone()[0],
                "started_at": d.execute("SELECT MAX(at) FROM app_starts").fetchone()[0],
                "version": app_version()}

    def app_version():
        return getattr(sys.modules.get("daily"), "APP_VERSION", "") or app.jinja_env.globals.get("APP_VERSION", "")

    @app.route("/api/admin/status")
    @api
    def console_api_status():
        d = db()
        now_slot = int(time.time() // SLOT_SECONDS)
        since = now_slot - 7 * 86400 // SLOT_SECONDS
        week_ago = iso(utcnow() - timedelta(days=7))
        starts = [r[0] for r in d.execute("SELECT at FROM app_starts WHERE at >= ? ORDER BY at", (week_ago,))]
        restarts = []
        for at in starts:   # several server processes start together: count them as one restart
            if not restarts or ((parse(at) or utcnow()) - (parse(restarts[-1]) or utcnow())).total_seconds() > 120:
                restarts.append(at)
        return jsonify({
            **status_counts(), "process_started_at": iso(PROCESS_STARTED), "time": A.now(), "slow_ms": SLOW_MS,
            "restarts_7d": restarts[-20:],
            "errors": [dict(r) for r in d.execute("SELECT * FROM app_errors ORDER BY id DESC LIMIT 50")],
            "errors_7d": d.execute("SELECT COUNT(*) FROM app_errors WHERE at >= ?", (week_ago,)).fetchone()[0],
            "slow": [dict(r) for r in d.execute("SELECT * FROM slow_requests ORDER BY id DESC LIMIT 50")],
            "slow_7d": d.execute("SELECT COUNT(*) FROM slow_requests WHERE at >= ?", (week_ago,)).fetchone()[0],
            "slot_seconds": SLOT_SECONDS, "now_slot": now_slot,
            "heartbeats": [r[0] for r in d.execute("SELECT slot FROM heartbeats WHERE slot > ? ORDER BY slot", (since,))],
            "first_heartbeat": d.execute("SELECT MIN(slot) FROM heartbeats").fetchone()[0],
            "heartbeat_running": bool(state.get("thread")) and state["thread"].is_alive(),
            "invoices": {"last_run": dict(r) if (r := d.execute("SELECT * FROM invoice_runs ORDER BY id DESC LIMIT 1").fetchone()) else None,
                         "auto": invoice_settings()["auto"]}})

    @app.route("/api/admin/status/clear", methods=["POST"])
    @api
    def console_api_status_clear():
        what = (request.get_json(silent=True) or {}).get("what")
        table = {"errors": "app_errors", "slow": "slow_requests"}.get(what)
        if not table:
            return jsonify({"error": "Unknown list."}), 400
        n = db().execute(f"DELETE FROM {table}").rowcount
        db().commit()
        return jsonify({"ok": True, "message": f"{n} {'error' if what == 'errors' else 'slow page'}{'s' if n != 1 else ''} cleared."})

    # ================================================ extra data on answers the console already asks for
    CO_PATH = re.compile(r"/api/admin/companies/(\d+)(/invoices)?")

    @app.after_request
    def console_api_enrich(resp):
        if request.method != "GET" or resp.status_code != 200 or not request.path.startswith("/api/admin/") or not resp.is_json:
            return resp
        try:
            path = request.path
            m = CO_PATH.fullmatch(path)
            if not (m or path in ("/api/admin/summary", "/api/admin/invoices")):
                return resp
            data = resp.get_json(silent=True)
            if not isinstance(data, dict):
                return resp
            if path == "/api/admin/summary":
                data["status"] = status_counts()
            else:
                data["invoice_settings"] = public_settings()
                if m and isinstance(data.get("company"), dict):
                    data["company"].update(company_billing_extra(int(m.group(1))))
                if m and m.group(2) and isinstance(data.get("defaults"), dict):
                    extra = company_billing_extra(int(m.group(1)))
                    if extra.get("monthly_price"):
                        data["defaults"]["amount"] = extra["monthly_price"]
            resp.set_data(json.dumps(data))
        except Exception:  # noqa: BLE001
            app.logger.exception("console_api: couldn't add billing data")
        return resp

    @app.after_request
    def console_api_invoice_edit(resp):
        """Editing an invoice's total in the console keeps its subtotal and tax in step."""
        if request.method == "POST" and resp.status_code == 200 and re.fullmatch(r"/api/admin/companies/\d+/invoices", request.path):
            try:
                data = request.get_json(silent=True) or {}
                if data.get("action") == "update" and str(data.get("id", "")).isdigit():
                    r = db().execute("SELECT * FROM invoices WHERE id=?", (int(data["id"]),)).fetchone()
                    if r and r["subtotal_cents"] is not None and r["tax_rate"] is not None \
                            and r["subtotal_cents"] + (r["tax_cents"] or 0) != r["amount_cents"]:
                        sub = int((Decimal(r["amount_cents"]) / (1 + Decimal(str(r["tax_rate"])) / 100)).quantize(Decimal("1"), ROUND_HALF_UP))
                        db().execute("UPDATE invoices SET subtotal_cents=?, tax_cents=? WHERE id=?",
                                     (sub, r["amount_cents"] - sub, r["id"]))
                        db().commit()
            except Exception:  # noqa: BLE001
                app.logger.exception("console_api: couldn't update the invoice tax")
        return resp
