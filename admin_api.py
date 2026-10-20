"""Private admin API used by the FormSign Admin Console (a separate site that manages every company
across all FormSign apps from one place).

It is off unless ADMIN_API_TOKEN is set. Every request must send that token as
"Authorization: Bearer <token>". The console never sees passwords or documents: it reads company
statistics and can pause a company, change its storage, add a company, or create a one-time link that
signs the platform owner into a company (valid for 60 seconds, usable once).
"""
import hashlib
import hmac
import re
import os
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import abort, flash, jsonify, redirect, request, session, url_for
from werkzeug.security import generate_password_hash

import extras

TOKEN = os.environ.get("ADMIN_API_TOKEN", "")
SSO_SECONDS = 60
INVOICE_AMOUNT = os.environ.get("INVOICE_DEFAULT_AMOUNT", "185.00")   # suggested amount for a new invoice
INVOICE_CURRENCY = os.environ.get("INVOICE_CURRENCY", "CAD")
PAY_METHODS = ["Interac e-Transfer", "Cheque", "Credit card", "Bank transfer", "Cash", "Other"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL,
  number TEXT NOT NULL,
  period TEXT DEFAULT '',
  issued_on TEXT NOT NULL,
  due_on TEXT,
  amount_cents INTEGER NOT NULL DEFAULT 0,
  currency TEXT NOT NULL DEFAULT 'CAD',
  status TEXT NOT NULL DEFAULT 'unpaid',
  paid_on TEXT,
  method TEXT DEFAULT '',
  note TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_invoices_company ON invoices(company_id);
CREATE TABLE IF NOT EXISTS admin_sso (
  token_hash TEXT PRIMARY KEY,
  company_id INTEGER NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT
);
"""


def init_db(conn):
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
    if "admin_notes" not in cols:
        conn.execute("ALTER TABLE companies ADD COLUMN admin_notes TEXT DEFAULT ''")


def register(A):
    app, db = A.app, A.db

    def num(v, default=0):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    def iso(dt):
        return dt.replace(microsecond=0).isoformat()

    def api(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not TOKEN:
                abort(404)
            sent = request.headers.get("Authorization", "")
            if not sent.startswith("Bearer ") or not hmac.compare_digest(sent[7:].encode(), TOKEN.encode()):
                return jsonify({"error": "unauthorized"}), 401
            return view(*args, **kwargs)
        return wrapper

    def stats(cid):
        d = db()
        cutoff30 = iso(datetime.now(timezone.utc) - timedelta(days=30))
        overdue_before = iso(datetime.now(timezone.utc) - timedelta(hours=A.SIGN_WITHIN_HOURS))
        p = d.execute(
            "SELECT COUNT(*) total, "
            "SUM(completed_at IS NOT NULL) completed, "
            "SUM(completed_at IS NULL AND voided_at IS NULL AND status != 'draft') open, "
            "SUM(completed_at IS NULL AND voided_at IS NULL AND sent_at IS NOT NULL AND sent_at < ?) overdue, "
            "SUM(COALESCE(sent_at, created_at) >= ?) last30, "
            "MAX(COALESCE(sent_at, created_at)) last_packet "
            "FROM packets WHERE company_id=?", (overdue_before, cutoff30, cid)).fetchone()
        u = d.execute("SELECT SUM(active=1) users, SUM(active=1 AND role='manager') managers, MAX(last_login_at) last_login "
                      "FROM users WHERE company_id=?", (cid,)).fetchone()
        try:
            camp = d.execute("SELECT COUNT(*) FROM campaigns WHERE company_id=? AND status IN ('sent','sending')",
                             (cid,)).fetchone()[0]
            contacts = d.execute("SELECT COUNT(*) FROM contacts WHERE company_id=?", (cid,)).fetchone()[0]
        except Exception:  # noqa: BLE001  (older database without campaigns)
            camp = contacts = 0
        last = max([x for x in (p["last_packet"], u["last_login"]) if x] or [""]) or None
        return {"packets": p["total"] or 0, "completed": p["completed"] or 0, "open": p["open"] or 0,
                "overdue": p["overdue"] or 0, "packets_30d": p["last30"] or 0, "users": u["users"] or 0,
                "managers": u["managers"] or 0, "last_login": u["last_login"], "last_packet": p["last_packet"],
                "last_activity": last, "campaigns": camp, "contacts": contacts,
                "storage_bytes": A.company_storage_bytes(cid)}

    # ------------------------------------------------ invoices (payment tracking for the platform owner)
    def today():
        return datetime.now(A.LOCAL_TZ).date().isoformat()

    def day_ok(v):
        if not v:
            return True
        try:
            datetime.strptime(v, "%Y-%m-%d")
            return True
        except (TypeError, ValueError):
            return False

    def cents(v):
        try:
            c = round(float(str(v).replace("$", "").replace(",", "").strip()) * 100)
        except (TypeError, ValueError):
            return None
        return c if 0 <= c <= 100_000_000 else None

    def invoice_json(r):
        d = dict(r)
        d["overdue"] = d["status"] == "unpaid" and bool(d.get("due_on")) and d["due_on"] < today()
        d["amount"] = f"{d['amount_cents'] / 100:.2f}"
        return d

    def billing_stats(cid):
        rows = [invoice_json(r) for r in db().execute("SELECT * FROM invoices WHERE company_id=?", (cid,))]
        unpaid = [r for r in rows if r["status"] == "unpaid"]
        year = today()[:4]
        paid = [r for r in rows if r["status"] == "paid"]
        return {"invoices": len(rows), "unpaid_count": len(unpaid), "unpaid_cents": sum(r["amount_cents"] for r in unpaid),
                "overdue_count": sum(r["overdue"] for r in unpaid),
                "overdue_cents": sum(r["amount_cents"] for r in unpaid if r["overdue"]),
                "paid_cents_year": sum(r["amount_cents"] for r in paid if (r["paid_on"] or "")[:4] == year),
                "last_paid_on": max([r["paid_on"] for r in paid if r["paid_on"]] or [None]),
                "next_due_on": min([r["due_on"] for r in unpaid if r["due_on"]] or [None]),
                "currency": (rows[0]["currency"] if rows else INVOICE_CURRENCY)}

    def invoice_defaults(cid):
        now_local = datetime.now(A.LOCAL_TZ)
        base = f"INV-{now_local:%Y%m}-{cid:03d}"
        number, n = base, 2
        while db().execute("SELECT 1 FROM invoices WHERE company_id=? AND number=?", (cid, number)).fetchone():
            number, n = f"{base}-{n}", n + 1
        return {"number": number, "period": now_local.strftime("%B %Y"), "issued_on": now_local.date().isoformat(),
                "due_on": (now_local.date() + timedelta(days=15)).isoformat(), "amount": INVOICE_AMOUNT,
                "currency": INVOICE_CURRENCY, "methods": PAY_METHODS}

    def company_json(c):
        c = dict(c)
        return {"id": c["id"], "name": c["name"], "status": c["status"], "notify_email": c.get("notify_email") or "",
                "mailing_address": c.get("mailing_address") or "", "storage_gb": c["storage_gb"],
                "source": c.get("source") or "", "created_at": c["created_at"], "admin_notes": c.get("admin_notes") or "",
                "plan": c.get("plan") or "paid", "trial_ends_at": c.get("trial_ends_at"),
                "trial_days_left": extras.trial_days_left(c), "paused_reason": c.get("paused_reason") or "",
                "billing": billing_stats(c["id"]),
                **stats(c["id"])}

    @app.route("/api/admin/summary")
    @api
    def admin_api_summary():
        rows = db().execute("SELECT * FROM companies ORDER BY name COLLATE NOCASE").fetchall()
        return jsonify({"app": A.PLATFORM_NAME, "time": A.now(), "sign_within_hours": A.SIGN_WITHIN_HOURS,
                        "storage_temporary": bool(getattr(A, "STORAGE_TEMPORARY", False)), "trial_days": extras.TRIAL_DAYS,
                        "companies": [company_json(r) for r in rows]})

    @app.route("/api/admin/companies/<int:cid>")
    @api
    def admin_api_company(cid):
        c = db().execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone() or abort(404)
        users = [dict(r) for r in db().execute(
            "SELECT id, name, email, role, active, must_change_password, created_at, last_login_at, "
            "(SELECT COUNT(*) FROM packets p WHERE p.created_by_id = u.id) AS packet_count "
            "FROM users u WHERE company_id=? ORDER BY active DESC, role DESC, name COLLATE NOCASE", (cid,))]
        packets = []
        for r in db().execute("SELECT * FROM packets WHERE company_id=? ORDER BY COALESCE(sent_at, created_at) DESC, id DESC "
                              "LIMIT 15", (cid,)):
            signed, total = A.packet_progress(r["id"])
            packets.append({"id": r["id"], "client_name": r["client_name"], "created_by": r["created_by"],
                            "state": A.effective_status(r), "sent_at": r["sent_at"] or r["created_at"],
                            "signed": signed, "total": total})
        return jsonify({"company": company_json(c), "users": users, "packets": packets})

    @app.route("/api/admin/companies/<int:cid>/invoices", methods=["GET", "POST"])
    @api
    def admin_api_invoices(cid):
        c = db().execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone() or abort(404)
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            action = data.get("action")
            iid = num(data.get("id"))
            row = db().execute("SELECT * FROM invoices WHERE id=? AND company_id=?", (iid, cid)).fetchone() if iid else None
            if action in ("create", "update"):
                if action == "update" and not row:
                    return jsonify({"error": "Invoice not found."}), 404
                number = str(data.get("number", "")).strip()[:40]
                amount = cents(data.get("amount"))
                issued = str(data.get("issued_on") or today())[:10]
                due = str(data.get("due_on") or "")[:10] or None
                if not number:
                    return jsonify({"error": "Add an invoice number."}), 400
                if amount is None:
                    return jsonify({"error": "Enter the amount, like 185.00."}), 400
                if not (day_ok(issued) and day_ok(due)):
                    return jsonify({"error": "Use dates like 2026-11-01."}), 400
                if due and due < issued:
                    return jsonify({"error": "The due date can't be before the invoice date."}), 400
                dup = db().execute("SELECT id FROM invoices WHERE company_id=? AND number=? AND id != ?",
                                   (cid, number, iid or 0)).fetchone()
                if dup:
                    return jsonify({"error": f"Invoice {number} already exists for this company."}), 400
                vals = (number, str(data.get("period", "")).strip()[:60], issued, due, amount,
                        (str(data.get("currency") or INVOICE_CURRENCY).strip().upper() or INVOICE_CURRENCY)[:3],
                        str(data.get("note", "")).strip()[:500])
                if action == "create":
                    status = "paid" if data.get("status") == "paid" else "unpaid"
                    paid_on = (str(data.get("paid_on") or today())[:10]) if status == "paid" else None
                    if not day_ok(paid_on):
                        return jsonify({"error": "Use dates like 2026-11-01."}), 400
                    new = db().execute("INSERT INTO invoices (number, period, issued_on, due_on, amount_cents, currency, note, "
                                       "status, paid_on, method, company_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                       vals + (status, paid_on, str(data.get("method", ""))[:40] if status == "paid" else "",
                                               cid, A.now())).lastrowid
                    msg, iid = f"Invoice {number} added.", new
                else:
                    db().execute("UPDATE invoices SET number=?, period=?, issued_on=?, due_on=?, amount_cents=?, currency=?, "
                                 "note=?, updated_at=? WHERE id=?", vals + (A.now(), iid))
                    msg = f"Invoice {number} updated."
            elif action == "paid":
                if not row:
                    return jsonify({"error": "Invoice not found."}), 404
                paid_on = str(data.get("paid_on") or today())[:10]
                if not day_ok(paid_on):
                    return jsonify({"error": "Use dates like 2026-11-01."}), 400
                db().execute("UPDATE invoices SET status='paid', paid_on=?, method=?, updated_at=? WHERE id=?",
                             (paid_on, str(data.get("method", ""))[:40], A.now(), iid))
                msg = f"Invoice {row['number']} marked as paid on {paid_on}."
            elif action == "unpaid":
                if not row:
                    return jsonify({"error": "Invoice not found."}), 404
                db().execute("UPDATE invoices SET status='unpaid', paid_on=NULL, method='', updated_at=? WHERE id=?",
                             (A.now(), iid))
                msg = f"Invoice {row['number']} marked as not paid."
            elif action == "delete":
                if not row:
                    return jsonify({"error": "Invoice not found."}), 404
                db().execute("DELETE FROM invoices WHERE id=?", (iid,))
                msg = f"Invoice {row['number']} deleted."
            else:
                return jsonify({"error": "Unknown action."}), 400
            db().commit()
            return jsonify({"ok": True, "message": msg, "id": iid})
        rows = [invoice_json(r) for r in db().execute(
            "SELECT * FROM invoices WHERE company_id=? ORDER BY issued_on DESC, id DESC", (cid,))]
        return jsonify({"company": company_json(c), "invoices": rows, "billing": billing_stats(cid),
                        "defaults": invoice_defaults(cid), "today": today()})

    @app.route("/api/admin/invoices")
    @api
    def admin_api_all_invoices():
        """Every company's invoices (unpaid only with ?status=unpaid), newest first."""
        sql = "SELECT i.*, c.name AS company_name FROM invoices i JOIN companies c ON c.id = i.company_id"
        if request.args.get("status") == "unpaid":
            sql += " WHERE i.status='unpaid'"
        rows = [invoice_json(r) for r in db().execute(sql + " ORDER BY COALESCE(i.due_on, i.issued_on), i.id")]
        return jsonify({"invoices": rows, "today": today()})

    @app.route("/api/admin/companies", methods=["POST"])
    @api
    def admin_api_create():
        data = request.get_json(silent=True) or {}
        name = str(data.get("company", "")).strip()[:100]
        mname = str(data.get("name", "")).strip()[:80]
        email = str(data.get("email", "")).strip()[:120]
        pw = str(data.get("password", ""))
        gb = num(data.get("storage_gb"), A.INCLUDED_STORAGE_GB) or A.INCLUDED_STORAGE_GB
        if not name or not mname or "@" not in email:
            return jsonify({"error": "Add the company name and its first manager's name and email."}), 400
        if len(pw) < 8:
            return jsonify({"error": "The temporary password needs at least 8 characters."}), 400
        if db().execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            return jsonify({"error": "That email already has a login in this app."}), 400
        cid = db().execute("INSERT INTO companies (name, notify_email, storage_gb, source, created_at) VALUES (?,?,?,?,?)",
                           (name, email, max(1, min(gb, 100000)), "owner", A.now())).lastrowid
        db().execute("INSERT INTO users (name, email, password_hash, role, created_at, company_id) VALUES (?,?,?,?,?,?)",
                     (mname, email, generate_password_hash(pw), "manager", A.now(), cid))
        if data.get("demo") in (True, "on", "1", 1, "true"):
            extras.start_trial(db(), cid)   # 14-day demo that locks on its own
        db().commit()
        return jsonify({"ok": True, "id": cid})

    @app.route("/api/admin/companies/<int:cid>/action", methods=["POST"])
    @api
    def admin_api_action(cid):
        db().execute("SELECT 1 FROM companies WHERE id=?", (cid,)).fetchone() or abort(404)
        data = request.get_json(silent=True) or {}
        action = data.get("action")
        if action in ("mark_paid", "extend_trial"):
            msg = extras.owner_action(db(), cid, action, data.get("days"))
            db().commit()
            return jsonify({"ok": True, "message": msg})
        if action in ("suspend", "activate"):
            msg = extras.owner_action(db(), cid, action)
            db().execute("UPDATE companies SET status=? WHERE id=?", ("suspended" if action == "suspend" else "active", cid))
            db().commit()
            return jsonify({"ok": True, "message": msg})
        elif action == "storage":
            gb = num(data.get("storage_gb"))
            if not 1 <= gb <= 100000:
                return jsonify({"error": "Storage must be between 1 and 100000 GB."}), 400
            db().execute("UPDATE companies SET storage_gb=? WHERE id=?", (gb, cid))
        elif action == "notes":
            db().execute("UPDATE companies SET admin_notes=? WHERE id=?", (str(data.get("notes", ""))[:4000], cid))
        elif action == "reset_password":
            uid = num(data.get("user_id")); pw = str(data.get("password", ""))
            if len(pw) < 8:
                return jsonify({"error": "The temporary password needs at least 8 characters."}), 400
            n = db().execute("UPDATE users SET password_hash=?, must_change_password=1, active=1 WHERE id=? AND company_id=?",
                             (generate_password_hash(pw), uid, cid)).rowcount
            if not n:
                return jsonify({"error": "User not found in this company."}), 404
        else:
            return jsonify({"error": "Unknown action."}), 400
        db().commit()
        return jsonify({"ok": True})

    @app.route("/api/admin/companies/<int:cid>/login-link", methods=["POST"])
    @api
    def admin_api_login_link(cid):
        db().execute("SELECT 1 FROM companies WHERE id=?", (cid,)).fetchone() or abort(404)
        raw = secrets.token_urlsafe(32)
        db().execute("INSERT INTO admin_sso (token_hash, company_id, expires_at) VALUES (?,?,?)",
                     (hashlib.sha256(raw.encode()).hexdigest(), cid,
                      iso(datetime.now(timezone.utc) + timedelta(seconds=SSO_SECONDS))))
        db().execute("DELETE FROM admin_sso WHERE expires_at < ?", (iso(datetime.now(timezone.utc) - timedelta(days=1)),))
        db().commit()
        base = A.BASE_URL or request.url_root.rstrip("/")
        return jsonify({"url": f"{base}/admin-sso/{raw}", "expires_in": SSO_SECONDS})

    @app.route("/admin-sso/<raw>")
    def admin_sso(raw):
        """Opened in the platform owner's browser from the console: signs in as the owner inside one company."""
        if not TOKEN:
            abort(404)
        h = hashlib.sha256(raw.encode()).hexdigest()
        row = db().execute("SELECT * FROM admin_sso WHERE token_hash=?", (h,)).fetchone()
        if not row or row["used_at"] or row["expires_at"] < A.now():
            return A.render_template("error.html", title="This admin link has expired",
                                     message="Go back to the Admin Console and click Open again."), 410
        db().execute("UPDATE admin_sso SET used_at=? WHERE token_hash=?", (A.now(), h))
        db().commit()
        company = A.get_company(row["company_id"])
        session.clear()
        session["uid"] = 0
        session["as_company"] = row["company_id"]
        session.permanent = True
        flash(f"Opened {company['name']} from the Admin Console with management access.", "ok")
        return redirect(url_for("dashboard"))
