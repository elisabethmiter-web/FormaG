"""Everyday improvements.

 3  stay signed in      "Stay signed in on this computer" at sign-in; otherwise the sign-in ends when the browser closes
 4  idle sign-out       signed out after IDLE_MINUTES without activity (default 8 hours), with a one-minute warning
 7  duplicate           "Same forms, another client" and "Send again with changes" from any packet
 8  message wording     each company can write its own email and text wording (Company page)
 9  edit client         fix the client's (or second owner's) name and email after the packet is made
10  archive             hide finished packets from the dashboard without deleting them
12  need help?          the business's phone and email at the bottom of every signing page
13  finish later        the client's answers are saved as they type; they can close and come back
14  activity            last sign-in and packets this month per company (Admin Console)
15  announcement        one message shown in every company's app (Admin Console)
16  usage report        packets per company per month (Admin Console, Excel)
"""
import hmac
import json
import os
import re
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import abort, flash, g, jsonify, redirect, render_template, request, session, url_for
from markupsafe import Markup, escape

IDLE_MINUTES = max(5, int(os.environ.get("IDLE_MINUTES", "480")))
BACKGROUND = {"updates_json", "support_unread", "static", "healthz", "brand_logo"}   # requests that aren't "activity"

COLUMNS = {
    "packets": {"archived_at": "TEXT"},
    "companies": {"contact_name": "TEXT DEFAULT ''", "contact_phone": "TEXT DEFAULT ''", "contact_email": "TEXT DEFAULT ''",
                  "msg_subject": "TEXT DEFAULT ''", "msg_body": "TEXT DEFAULT ''", "msg_rem_subject": "TEXT DEFAULT ''",
                  "msg_rem_body": "TEXT DEFAULT ''", "msg_text": "TEXT DEFAULT ''"},
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS form_drafts (
  item_id INTEGER PRIMARY KEY,
  packet_id INTEGER NOT NULL,
  data_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# Placeholders a company can use in its own message wording.
PLACEHOLDERS = {
    "{client}": "client's first name", "{client_full}": "client's full name", "{company}": "your company name",
    "{forms}": "list of the forms", "{link}": "the link to the forms", "{due}": "the 'please complete by' date",
    "{sender}": "your name",
}
DEFAULT_WORDING = {
    "msg_subject": "{company}: forms to sign",
    "msg_body": "Hi {client},\n\n{company} has sent you the following to review and sign:\n{forms}\n\n{link}\n\n{due}\n\nThank you,\n{sender}",
    "msg_rem_subject": "Reminder: your forms from {company} are waiting for your signature",
    "msg_rem_body": "Hi {client},\n\nJust a friendly reminder that {company} is still waiting for you to review and sign:\n{forms}\n\n{link}\n\nThank you,\n{sender}",
    "msg_text": "{company}: Hi {client}, please review and sign your forms here: {link}",
}


def init_db(conn):
    conn.executescript(SCHEMA)
    for table, cols in COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, spec in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {spec}")


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse(ts):
    try:
        d = datetime.fromisoformat(ts)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def fill(template, values):
    """Replaces {placeholders}; unknown ones are left as typed."""
    return re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1), m.group(0))), template or "")


def render_wording(c, kind, values, html_values=None):
    """A company's own wording (or None to use the standard one). kind: 'first', 'reminder' or 'text'."""
    keys = {"first": ("msg_subject", "msg_body"), "reminder": ("msg_rem_subject", "msg_rem_body"), "text": (None, "msg_text")}[kind]
    body_t = (c or {}).get(keys[1]) or ""
    if not body_t.strip():
        return None
    subject_t = ((c or {}).get(keys[0]) or DEFAULT_WORDING[keys[0]]) if keys[0] else ""
    text = fill(body_t, values)
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
    out = {"subject": fill(subject_t, values).strip(), "body": text}
    if html_values is not None:
        # HTML: escape the wording, then swap in the button and the list
        esc = {k: str(escape(v)) for k, v in values.items()}
        esc.update(html_values)
        parts = [p for p in re.split(r"\n{2,}", str(escape(body_t)))]
        html = "".join(f"<p>{fill(p, esc).replace(chr(10), '<br>')}</p>" for p in parts if fill(p, esc).strip())
        out["html"] = ('<div style="font-family:Segoe UI,Arial,sans-serif;font-size:15px;line-height:1.5;color:#1f2937">'
                       + html + "</div>")
    return out


def register(A):
    app, db = A.app, A.db
    token = os.environ.get("ADMIN_API_TOKEN", "")

    # ------------------------------------------------ 3 & 4  stay signed in / sign out after inactivity
    @app.before_request
    def idle_check():
        if session.get("uid") is None or session.get("remember") or session.get("kiosk") \
                or request.endpoint in ("login", "logout", "signed_out_idle", "static", "signup"):
            return None
        now = utcnow().timestamp()
        seen = session.get("seen")
        if seen and now - seen > IDLE_MINUTES * 60:
            session.clear()
            if request.path.endswith(".json") or request.accept_mimetypes.best == "application/json" \
                    or request.method == "POST" and request.headers.get("X-Requested-With"):
                return jsonify({"error": "signed out"}), 401
            flash("You were signed out after a period of inactivity. Sign in again to continue.", "ok")
            return redirect(url_for("login", next=request.path if request.method == "GET" else None))
        if request.endpoint not in BACKGROUND and not (request.endpoint == "support_chat" and request.method == "GET"):
            if not seen or now - seen > 60:
                session["seen"] = now
        return None

    @app.route("/session/ping")
    def session_ping():
        return jsonify({"ok": bool(session.get("uid") is not None)})

    @app.route("/signed-out")
    def signed_out_idle():
        if session.get("uid") is not None and not session.get("remember"):
            session.clear()
            flash("You were signed out after a period of inactivity. Sign in again to continue.", "ok")
        return redirect(url_for("login"))

    @app.after_request
    def remember_choice(resp):
        """After a successful sign-in: keep the session only if "Stay signed in" was ticked."""
        if request.endpoint in ("login", "signup") and request.method == "POST" and session.get("uid") is not None \
                and resp.status_code in (301, 302, 303):
            keep = request.form.get("remember") == "on"
            session["remember"] = keep
            session.permanent = keep
            session["seen"] = utcnow().timestamp()
        return resp

    @app.context_processor
    def inject_everyday():
        out = {"idle_seconds": None, "idle_minutes": IDLE_MINUTES, "announcement": announcement(), "help_line": None,
               "wording_defaults": DEFAULT_WORDING, "placeholders": PLACEHOLDERS}
        if g.get("user") is not None and session.get("uid") is not None and not session.get("remember") \
                and not session.get("kiosk"):
            out["idle_seconds"] = IDLE_MINUTES * 60
        p = g.get("client_packet")
        if p is not None:
            out["help_line"] = help_line(p)
        return out

    # ------------------------------------------------ 7  duplicate a packet
    def duplicate_prefill(pid, same_client):
        """(form dict, selected template ids) for New packet, copied from packet `pid`; None if not allowed."""
        cond, args = A.packet_scope()
        p = db().execute(f"SELECT * FROM packets WHERE id=? AND {cond}", [pid] + args).fetchone()
        if not p:
            return None
        ids = [r[0] for r in db().execute(
            "SELECT i.template_id FROM packet_items i JOIN templates t ON t.id=i.template_id "
            "WHERE i.packet_id=? AND t.archived=0 AND t.company_id=? ORDER BY i.position", (pid, A.cid()))]
        form = dict(A.packet_form_defaults())
        form.update({"message": p["message"] or ""})
        for k in ("so_number", "sales_rep"):
            if k in p.keys() and p[k] and same_client:
                form[k] = p[k]
        if p["upload_request"] if "upload_request" in p.keys() else "":
            form["ask_files"], form["upload_request"] = "on", p["upload_request"]
        if same_client:
            form.update({"client_name": p["client_name"], "client_email": p["client_email"] or ""})
            if p["access_code"]:
                form["access_code"] = "on"
            s = db().execute("SELECT * FROM packet_signers WHERE packet_id=? ORDER BY id LIMIT 1", (pid,)).fetchone()
            if s:
                form.update({"signers": "2", "cosigner_name": s["name"], "cosigner_email": s["email"] or ""})
        skipped = db().execute("SELECT COUNT(*) FROM packet_items WHERE packet_id=? AND (template_id IS NULL OR "
                               "template_id NOT IN (SELECT id FROM templates WHERE archived=0))", (pid,)).fetchone()[0]
        form["_dup_note"] = (f"Copied from {p['client_name']}'s packet."
                             + (f" {skipped} one-off file{'s' if skipped != 1 else ''} or approval{'s' if skipped != 1 else ''} "
                                f"{'weren' if skipped != 1 else 'wasn'}'t copied: add {'them' if skipped != 1 else 'it'} again if needed."
                                if skipped else ""))
        return form, ids

    A.duplicate_prefill = duplicate_prefill

    def second_signer(pid):
        return db().execute("SELECT * FROM packet_signers WHERE packet_id=? ORDER BY id LIMIT 1", (pid,)).fetchone()

    app.jinja_env.globals["second_signer"] = second_signer

    # ------------------------------------------------ 9  edit the client's details
    @app.route("/packets/<int:pid>/client", methods=["POST"])
    @A.login_required
    def edit_client(pid):
        p = db().execute("SELECT * FROM packets WHERE id=?", (pid,)).fetchone() or abort(404)
        name = request.form.get("client_name", "").strip()[:120]
        email = request.form.get("client_email", "").strip()[:200]
        if not name or (email and "@" not in email):
            flash("Add the client's name, and check the email address if you entered one.", "error")
            return redirect(url_for("packet_detail", pid=pid))
        changes = []
        if name != p["client_name"]:
            changes.append(f"name {p['client_name']} → {name}")
        if email != (p["client_email"] or ""):
            changes.append(f"email {p['client_email'] or '(none)'} → {email or '(none)'}")
        sid = request.form.get("signer_id", "")
        if sid.isdigit():
            s = db().execute("SELECT * FROM packet_signers WHERE id=? AND packet_id=?", (int(sid), pid)).fetchone()
            sname = request.form.get("cosigner_name", "").strip()[:120]
            semail = request.form.get("cosigner_email", "").strip()[:200]
            if s and sname and (not semail or "@" in semail):
                if sname != s["name"] or semail != (s["email"] or ""):
                    db().execute("UPDATE packet_signers SET name=?, email=? WHERE id=?", (sname, semail, s["id"]))
                    changes.append(f"second owner {s['name']} → {sname}" + (f" ({semail})" if semail else ""))
        if changes:
            db().execute("UPDATE packets SET client_name=?, client_email=? WHERE id=?", (name, email, pid))
            A.log_event(pid, "edited", "Client details changed: " + "; ".join(changes) + f" · by {A.actor()}")
            db().commit()
            flash("Client details saved. Signed copies already made keep the name that was signed.", "ok")
        return redirect(url_for("packet_detail", pid=pid))

    # ------------------------------------------------ 10  archive
    @app.route("/packets/<int:pid>/archive", methods=["POST"])
    @A.login_required
    def archive_packet(pid):
        p = db().execute("SELECT * FROM packets WHERE id=?", (pid,)).fetchone() or abort(404)
        if request.form.get("undo"):
            db().execute("UPDATE packets SET archived_at=NULL WHERE id=?", (pid,))
            A.log_event(pid, "archived", f"Taken out of the archive · by {A.actor()}")
            flash("Back on the dashboard.", "ok")
        else:
            db().execute("UPDATE packets SET archived_at=? WHERE id=?", (A.now(), pid))
            A.log_event(pid, "archived", f"Archived · by {A.actor()}")
            flash(f"{p['client_name']}'s packet is archived: it's hidden from the dashboard but nothing is deleted. "
                  f"Find it under the Archived tab or by searching.", "ok")
        db().commit()
        return redirect(request.form.get("next") or url_for("dashboard"))

    @app.route("/packets/archive-old", methods=["POST"])
    @A.login_required
    def archive_old():
        days = int(request.form.get("days", "30")) if request.form.get("days", "30").isdigit() else 30
        cond, args = A.packet_scope()
        cutoff = (utcnow() - timedelta(days=days)).isoformat()
        rows = db().execute(f"SELECT id FROM packets WHERE {cond} AND archived_at IS NULL AND "
                            f"((completed_at IS NOT NULL AND completed_at < ?) OR (voided_at IS NOT NULL AND voided_at < ?))",
                            args + [cutoff, cutoff]).fetchall()
        for r in rows:
            db().execute("UPDATE packets SET archived_at=? WHERE id=?", (A.now(), r[0]))
            A.log_event(r[0], "archived", f"Archived with other finished packets · by {A.actor()}")
        db().commit()
        flash(f"{len(rows)} finished packet{'s' if len(rows) != 1 else ''} older than {days} days archived." if rows
              else f"No finished packets older than {days} days to archive.", "ok")
        return redirect(url_for("dashboard"))

    # ------------------------------------------------ 8 & 12  company settings: message wording and contact line
    @app.route("/company/messages", methods=["POST"])
    @A.login_required
    def company_messages():
        if not A.is_manager():
            abort(403)
        if request.form.get("reset"):
            vals = {k: "" for k in DEFAULT_WORDING}
            flash("Back to the standard wording.", "ok")
        else:
            vals = {k: (request.form.get(k) or "").replace("\r\n", "\n").strip()[:3000] for k in DEFAULT_WORDING}
            for body_key in ("msg_body", "msg_rem_body", "msg_text"):
                if vals[body_key] and "{link}" not in vals[body_key]:
                    vals[body_key] += ("\n\n{link}" if body_key != "msg_text" else " {link}")
            # a field left exactly as the standard wording isn't a custom one
            vals = {k: ("" if v.strip() == DEFAULT_WORDING[k].strip() else v) for k, v in vals.items()}
            flash("Message wording saved. It's used the next time anyone opens an email or text.", "ok")
        db().execute("UPDATE companies SET " + ", ".join(f"{k}=?" for k in vals) + " WHERE id=?", list(vals.values()) + [A.cid()])
        db().commit()
        return redirect(url_for("company_settings") + "#messages")

    @app.route("/company/contact", methods=["POST"])
    @A.login_required
    def company_contact():
        if not A.is_manager():
            abort(403)
        email = request.form.get("contact_email", "").strip()[:200]
        if email and "@" not in email:
            flash("Check the email address.", "error")
            return redirect(url_for("company_settings") + "#contact")
        db().execute("UPDATE companies SET contact_name=?, contact_phone=?, contact_email=? WHERE id=?",
                     (request.form.get("contact_name", "").strip()[:80], request.form.get("contact_phone", "").strip()[:40],
                      email, A.cid()))
        db().commit()
        flash("Saved. Your clients see it at the bottom of their signing pages.", "ok")
        return redirect(url_for("company_settings") + "#contact")

    def help_line(p):
        """Who the client should contact: the company's contact details, or else the person who sent the packet."""
        c = db().execute("SELECT * FROM companies WHERE id=?", (p["company_id"],)).fetchone()
        if not c:
            return None
        name, phone, email = c["contact_name"] or "", c["contact_phone"] or "", c["contact_email"] or ""
        if not (phone or email):
            u = db().execute("SELECT name, email FROM users WHERE id=?", (p["created_by_id"],)).fetchone() \
                if "created_by_id" in p.keys() and p["created_by_id"] else None
            if u:
                name, email = name or u["name"], u["email"]
        if not (phone or email):
            return None
        return {"name": name, "phone": phone, "email": email, "company": c["name"],
                "tel": re.sub(r"[^\d+]", "", phone)}

    A.help_line = help_line

    @app.before_request
    def find_client_packet():
        """On the client's pages (/s/<token>, /c/<token>), remember which packet it is (help line, language)."""
        m = re.match(r"^/(s|c)/([^/]+)", request.path)
        if not m:
            return
        if m.group(1) == "s":
            p = db().execute("SELECT * FROM packets WHERE token=?", (m.group(2),)).fetchone()
        else:
            p = db().execute("SELECT p.* FROM packets p JOIN packet_signers s ON s.packet_id=p.id WHERE s.token=?",
                             (m.group(2),)).fetchone()
        if p:
            g.client_packet = p

    # ------------------------------------------------ 13  the client's answers are kept as they type
    def open_item(tok, iid):
        p = db().execute("SELECT * FROM packets WHERE token=?", (tok,)).fetchone()
        if not p or p["voided_at"] or p["completed_at"]:
            return None, None
        it = db().execute("SELECT * FROM packet_items WHERE id=? AND packet_id=?", (iid, p["id"])).fetchone()
        if not it or it["signed_at"]:
            return p, None
        return p, it

    @app.route("/s/<tok>/f/<int:iid>/draft", methods=["GET", "POST"])
    def form_draft(tok, iid):
        p, it = open_item(tok, iid)
        if not it:
            return jsonify({"data": {}}), (404 if not p else 200)
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            if request.headers.get("X-CSRF-Token", "") != session.get("csrf", "") or not session.get("csrf"):
                return jsonify({"error": "Reload the page and try again."}), 400
            clean = {str(k)[:80]: (str(v)[:5000] if not isinstance(v, bool) else v)
                     for k, v in list((data.get("data") or {}).items())[:300]
                     if k not in ("csrf", "signature", "consent")}
            db().execute("INSERT INTO form_drafts (item_id, packet_id, data_json, updated_at) VALUES (?,?,?,?) "
                         "ON CONFLICT(item_id) DO UPDATE SET data_json=excluded.data_json, updated_at=excluded.updated_at",
                         (iid, p["id"], json.dumps(clean), A.now()))
            if data.get("later"):
                A.log_event(p["id"], "saved", f"{it['template_name']}: client saved answers to finish later")
            db().commit()
            return jsonify({"ok": True, "saved_at": A.now()})
        r = db().execute("SELECT data_json, updated_at FROM form_drafts WHERE item_id=?", (iid,)).fetchone()
        return jsonify({"data": json.loads(r["data_json"]) if r else {}, "saved_at": r["updated_at"] if r else None})

    def draft_for(item_id):
        r = db().execute("SELECT data_json FROM form_drafts WHERE item_id=?", (item_id,)).fetchone()
        return json.loads(r[0]) if r else {}

    app.jinja_env.globals["form_draft"] = draft_for

    # ------------------------------------------------ 15  announcement from the platform owner
    def setting(key, default=None):
        r = db().execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_setting(key, value):
        db().execute("INSERT INTO app_settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value)))

    def announcement():
        try:
            a = setting("announcement")
        except Exception:  # noqa: BLE001  (first start, before the table exists)
            return None
        if not a or not a.get("text"):
            return None
        until = parse(a.get("until")) if a.get("until") else None
        if until and until < utcnow():
            return None
        return a

    # ------------------------------------------------ owner API for the Admin Console (14, 15, 16)
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

    def activity_stats(company_id):
        """Who's using it: last sign-in or visit, people active in the last 30 days, packets this month."""
        month_start = utcnow().replace(day=1, hour=0, minute=0, second=0).isoformat()
        cutoff = (utcnow() - timedelta(days=30)).isoformat()
        row = db().execute("SELECT MAX(COALESCE(last_seen_at, last_login_at)) AS last, "
                           "SUM(CASE WHEN COALESCE(last_seen_at, last_login_at) >= ? THEN 1 ELSE 0 END) AS active "
                           "FROM users WHERE company_id=? AND active=1", (cutoff, company_id)).fetchone()
        month = db().execute("SELECT COUNT(*) FROM packets WHERE company_id=? AND created_at >= ?",
                             (company_id, month_start)).fetchone()[0]
        return {"last_active_at": row["last"], "active_users_30d": row["active"] or 0, "packets_month": month}

    A.activity_stats = activity_stats

    @app.route("/api/admin/announcement", methods=["GET", "POST"])
    @api
    def admin_api_announcement():
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            text = str(data.get("text", "")).strip()[:500]
            if not text:
                set_setting("announcement", None)
            else:
                until = str(data.get("until") or "").strip()
                if until and re.fullmatch(r"\d{4}-\d{2}-\d{2}", until):
                    until = until + "T23:59:59+00:00"
                set_setting("announcement", {"id": A.now(), "text": text,
                                             "level": "warn" if data.get("level") == "warn" else "info",
                                             "until": until or None})
            db().commit()
        return jsonify({"announcement": setting("announcement")})

    @app.route("/api/admin/trend")
    @api
    def admin_api_trend():
        """Packets created and completed per week (weeks start Monday 00:00 UTC, so apps line up), all companies."""
        weeks = max(4, min(int(request.args.get("weeks", "12")) if request.args.get("weeks", "12").isdigit() else 12, 52))
        now = utcnow()
        monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0)
        out = []
        for k in range(weeks - 1, -1, -1):
            s_, e_ = (monday - timedelta(weeks=k)).isoformat(), (monday - timedelta(weeks=k - 1)).isoformat()
            out.append({"start": s_[:10], "created": db().execute("SELECT COUNT(*) FROM packets WHERE created_at>=? AND created_at<?", (s_, e_)).fetchone()[0],
                        "completed": db().execute("SELECT COUNT(*) FROM packets WHERE completed_at>=? AND completed_at<?", (s_, e_)).fetchone()[0]})
        return jsonify({"weeks": out})

    @app.route("/api/admin/usage")
    @api
    def admin_api_usage():
        month = request.args.get("month", "") or utcnow().strftime("%Y-%m")
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            return jsonify({"error": "month must be YYYY-MM"}), 400
        y, mth = int(month[:4]), int(month[5:])
        start = datetime(y, mth, 1, tzinfo=timezone.utc)
        end = datetime(y + (mth == 12), 1 if mth == 12 else mth + 1, 1, tzinfo=timezone.utc)
        s, e = start.isoformat(), end.isoformat()
        rows = []
        for c in db().execute("SELECT * FROM companies ORDER BY name COLLATE NOCASE").fetchall():
            cid = c["id"]
            q = lambda sql, *a: db().execute(sql, (cid, *a)).fetchone()[0]  # noqa: E731
            rows.append({
                "company_id": cid, "company": c["name"], "status": c["status"],
                "plan": (c["plan"] if "plan" in c.keys() else "") or "paid",
                "packets_created": q("SELECT COUNT(*) FROM packets WHERE company_id=? AND created_at>=? AND created_at<?", s, e),
                "packets_completed": q("SELECT COUNT(*) FROM packets WHERE company_id=? AND completed_at>=? AND completed_at<?", s, e),
                "documents_signed": q("SELECT COUNT(*) FROM packet_items i JOIN packets p ON p.id=i.packet_id "
                                      "WHERE p.company_id=? AND i.signed_at>=? AND i.signed_at<?", s, e),
                "users": q("SELECT COUNT(*) FROM users WHERE company_id=? AND active=1"),
                **activity_stats(cid),
            })
        return jsonify({"month": month, "companies": rows})
