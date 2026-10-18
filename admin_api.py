"""Private admin API used by the FormSign Admin Console (a separate site that manages every company
across all FormSign apps from one place).

It is off unless ADMIN_API_TOKEN is set. Every request must send that token as
"Authorization: Bearer <token>". The console never sees passwords or documents: it reads company
statistics and can pause a company, change its storage, add a company, or create a one-time link that
signs the platform owner into a company (valid for 60 seconds, usable once).
"""
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import abort, flash, jsonify, redirect, request, session, url_for
from werkzeug.security import generate_password_hash

import extras

TOKEN = os.environ.get("ADMIN_API_TOKEN", "")
SSO_SECONDS = 60

SCHEMA = """
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

    def company_json(c):
        c = dict(c)
        return {"id": c["id"], "name": c["name"], "status": c["status"], "notify_email": c.get("notify_email") or "",
                "mailing_address": c.get("mailing_address") or "", "storage_gb": c["storage_gb"],
                "source": c.get("source") or "", "created_at": c["created_at"], "admin_notes": c.get("admin_notes") or "",
                "plan": c.get("plan") or "paid", "trial_ends_at": c.get("trial_ends_at"),
                "trial_days_left": extras.trial_days_left(c), "paused_reason": c.get("paused_reason") or "",
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
