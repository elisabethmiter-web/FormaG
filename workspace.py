"""Team, Company, Follow-up, New packet, Packet page and Account improvements.

Team      1 activity cards per person   2 invite links instead of temporary passwords
          3 edit name/email/role in place, deactivate with Undo   4 move someone's open packets to another person
Company   6 live preview of branding, contact line and wording    8 storage breakdown + biggest files
          9 send a test packet to myself
Follow-up 13 snooze   14 progress for today   15 one-click call notes
Packet    18 timeline with time between steps   (19 live status is in static/packet-live.js)
Account   20 which desktop pop-ups each person wants
"""
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from flask import abort, flash, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash

INVITE_DAYS = 7
NOTIFY_KINDS = {"viewed": "A client opens their forms", "signed": "A client signs a form", "completed": "Every form is signed",
                "uploaded": "A client sends photos or files", "support": "An answer arrives in the support chat"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS invites (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  created_by TEXT DEFAULT '',
  used_at TEXT
);
"""
COLUMNS = {"packets": {"snooze_until": "TEXT"}, "users": {"notify_json": "TEXT DEFAULT ''"}}


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


def span(seconds):
    """3 min / 2 h 10 min / 3 days"""
    s = max(0, int(seconds))
    if s < 3600:
        return f"{max(1, s // 60)} min"
    if s < 86400:
        h, m = s // 3600, (s % 3600) // 60
        return f"{h} h" + (f" {m} min" if m and h < 10 else "")
    d = s // 86400
    return f"{d} day{'s' if d != 1 else ''}"


def register(A):
    app, db = A.app, A.db

    def manager_json():
        if not g.get("user") or not A.is_manager():
            abort(403)

    def wants_json():
        return request.is_json or request.accept_mimetypes.best == "application/json"

    # ================================================================ Team
    def team_stats():
        """Per person: packets sent and signed this month, overdue now, open, last seen, last 8 weeks sent."""
        now = utcnow()
        local = A.to_local(now)
        month_start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
        monday = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        weeks = [((monday - timedelta(weeks=k)).astimezone(timezone.utc), (monday - timedelta(weeks=k - 1)).astimezone(timezone.utc))
                 for k in range(7, -1, -1)]
        out = {}
        for u in db().execute("SELECT * FROM users WHERE company_id=?", (A.cid(),)).fetchall():
            ps = db().execute("SELECT * FROM packets WHERE company_id=? AND created_by_id=?", (A.cid(), u["id"])).fetchall()
            sent_m = sum(1 for p in ps if (p["sent_at"] or "") >= month_start)
            done_m = sum(1 for p in ps if (p["completed_at"] or "") >= month_start)
            open_ = [p for p in ps if not p["completed_at"] and not p["voided_at"] and p["sent_at"]]
            overdue = sum(1 for p in open_ if A.deadline_info(p).get("overdue"))
            spark = [sum(1 for p in ps if (d := parse(p["sent_at"])) and a <= d < b) for a, b in weeks]
            inv = db().execute("SELECT * FROM invites WHERE user_id=? AND used_at IS NULL ORDER BY created_at DESC LIMIT 1",
                               (u["id"],)).fetchone()
            pending = bool(inv and u["must_change_password"] and (parse(inv["created_at"]) or now) > now - timedelta(days=INVITE_DAYS))
            out[u["id"]] = {"sent_month": sent_m, "signed_month": done_m, "overdue": overdue, "open": len(open_),
                            "spark": spark, "seen": u["last_seen_at"] or u["last_login_at"], "invite_pending": pending,
                            "total": len(ps)}
        return out

    app.jinja_env.globals["team_stats"] = team_stats

    def new_invite(user_id):
        db().execute("UPDATE invites SET used_at=? WHERE user_id=? AND used_at IS NULL", (A.now(), user_id))   # old links stop working
        tok = secrets.token_urlsafe(24)
        db().execute("INSERT INTO invites (token, user_id, created_at, created_by) VALUES (?,?,?,?)", (tok, user_id, A.now(), A.actor()))
        return (A.BASE_URL or request.url_root.rstrip("/")) + url_for("accept_invite", token=tok)

    def invite_message(name, link):
        return (f"Hi {name.split(' ')[0]},\n\nYou've been added to {A.biz()} on {A.PLATFORM_NAME}. Open this link to choose your "
                f"password and sign in (it works for {INVITE_DAYS} days):\n{link}\n\nThank you,\n{g.user['name'] if g.user['id'] else A.biz()}")

    @app.route("/team/invite", methods=["POST"])
    @A.login_required
    def team_invite():
        if not A.is_manager():
            abort(403)
        name = request.form.get("name", "").strip()[:80]
        email = request.form.get("email", "").strip()[:120]
        role = request.form.get("role", "staff")
        problem = None
        if not name or "@" not in email:
            problem = "Add a name and a valid email address."
        elif role not in A.ROLE_LABELS:
            problem = "Choose Staff or Management."
        elif db().execute("SELECT 1 FROM users WHERE email=? COLLATE NOCASE", (email,)).fetchone():
            problem = "That email already has a login. Each person needs their own email address."
        how = request.form.get("how", "invite")
        pw = request.form.get("password", "")
        if not problem and how == "password":
            problem = A.valid_password(pw)
        if problem:
            flash(problem, "error")
            return redirect(url_for("team") + "#add-person")
        if how == "password":
            db().execute("INSERT INTO users (name, email, password_hash, role, must_change_password, created_at, company_id) "
                         "VALUES (?,?,?,?,1,?,?)", (name, email, generate_password_hash(pw), role, A.now(), A.cid()))
            db().commit()
            flash(f"Login created for {name} ({A.ROLE_LABELS[role]}). Give them the temporary password; "
                  f"they'll choose their own when they first sign in.", "ok")
            return redirect(url_for("team"))
        uid = db().execute("INSERT INTO users (name, email, password_hash, role, must_change_password, created_at, company_id) "
                           "VALUES (?,?,?,?,1,?,?)", (name, email, generate_password_hash(secrets.token_urlsafe(32)), role,
                                                       A.now(), A.cid())).lastrowid
        link = new_invite(uid)
        db().commit()
        session["invite_shown"] = {"name": name, "email": email, "link": link, "message": invite_message(name, link)}
        return redirect(url_for("team") + "#invite-ready")

    @app.route("/team/<int:uid>/invite", methods=["POST"])
    @A.login_required
    def team_reinvite(uid):
        if not A.is_manager():
            abort(403)
        u = db().execute("SELECT * FROM users WHERE id=? AND company_id=?", (uid, A.cid())).fetchone() or abort(404)
        db().execute("UPDATE users SET must_change_password=1 WHERE id=?", (uid,))
        link = new_invite(uid)
        db().commit()
        session["invite_shown"] = {"name": u["name"], "email": u["email"], "link": link, "message": invite_message(u["name"], link)}
        return redirect(url_for("team") + "#invite-ready")

    @app.context_processor
    def inject_invite():
        if request.endpoint == "team":
            return {"invite_shown": session.pop("invite_shown", None)}
        return {}

    @app.route("/invite/<token>", methods=["GET", "POST"])
    def accept_invite(token):
        inv = db().execute("SELECT * FROM invites WHERE token=?", (token,)).fetchone()
        u = db().execute("SELECT * FROM users WHERE id=? AND active=1", (inv["user_id"],)).fetchone() if inv else None
        expired = not inv or inv["used_at"] or not u or (parse(inv["created_at"]) or utcnow()) < utcnow() - timedelta(days=INVITE_DAYS)
        c = db().execute("SELECT * FROM companies WHERE id=?", (u["company_id"],)).fetchone() if u else None
        if expired:
            return render_template("invite.html", expired=True, company=c), 410
        error = None
        if request.method == "POST":
            pw = request.form.get("password", "")
            error = A.valid_password(pw) or (None if pw == request.form.get("confirm", "") else "The passwords don't match.")
            if not error:
                h = generate_password_hash(pw)
                db().execute("UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?", (h, u["id"]))
                db().execute("UPDATE invites SET used_at=? WHERE token=?", (A.now(), token))
                db().execute("UPDATE users SET last_login_at=? WHERE id=?", (A.now(), u["id"]))
                db().commit()
                session.clear()
                session["uid"], session["pwv"] = u["id"], h[-12:]
                session["seen"] = utcnow().timestamp()
                session["remember"] = False
                flash(f"Welcome, {u['name'].split(' ')[0]}! Your password is set. Have a look at Help for a quick tour.", "ok")
                return redirect(url_for("dashboard"))
        return render_template("invite.html", expired=False, u=u, company=c, error=error)

    @app.route("/team/<int:uid>/edit", methods=["POST"])
    @A.login_required
    def team_edit(uid):
        """Inline edits from the Team page: name, email or role, saved as you change them."""
        manager_json()
        u = db().execute("SELECT * FROM users WHERE id=? AND company_id=?", (uid, A.cid())).fetchone() or abort(404)
        data = request.get_json(silent=True) or request.form
        field, value = data.get("field"), str(data.get("value", "")).strip()
        if field == "name":
            if not value:
                return jsonify({"error": "The name can't be empty."}), 400
            db().execute("UPDATE users SET name=? WHERE id=?", (value[:80], uid))
            msg = "Name saved."
        elif field == "email":
            if "@" not in value:
                return jsonify({"error": "Enter a valid email address."}), 400
            if db().execute("SELECT 1 FROM users WHERE email=? COLLATE NOCASE AND id != ?", (value, uid)).fetchone():
                return jsonify({"error": "That email already has a login."}), 400
            db().execute("UPDATE users SET email=? WHERE id=?", (value[:120], uid))
            msg = f"Email saved. {u['name'].split(' ')[0]} now signs in with {value}."
        elif field == "role":
            if value not in A.ROLE_LABELS:
                return jsonify({"error": "Choose Staff or Management."}), 400
            if uid == g.user["id"] and value != "manager":
                return jsonify({"error": "You can't remove your own management access. Ask another manager."}), 400
            db().execute("UPDATE users SET role=? WHERE id=?", (value, uid))
            msg = f"{u['name']} is now {A.ROLE_LABELS[value]}."
        elif field == "active":
            on = value in ("1", "true", "True", "on")
            if uid == g.user["id"] and not on:
                return jsonify({"error": "You can't deactivate your own login."}), 400
            db().execute("UPDATE users SET active=? WHERE id=?", (1 if on else 0, uid))
            msg = f"{u['name']} {'can sign in again' if on else 'can no longer sign in'}."
        else:
            return jsonify({"error": "Nothing to change."}), 400
        db().commit()
        return jsonify({"ok": True, "message": msg})

    @app.route("/team/<int:uid>/reassign", methods=["POST"])
    @A.login_required
    def team_reassign(uid):
        """Move someone's open packets (not signed or cancelled) to another person."""
        if not A.is_manager():
            abort(403)
        u = db().execute("SELECT * FROM users WHERE id=? AND company_id=?", (uid, A.cid())).fetchone() or abort(404)
        to = request.form.get("to", "")
        t = db().execute("SELECT * FROM users WHERE id=? AND company_id=? AND active=1 AND id != ?",
                         (int(to) if to.isdigit() else 0, A.cid(), uid)).fetchone()
        if not t:
            flash("Choose who should take over the packets.", "error")
            return redirect(url_for("team"))
        rows = db().execute("SELECT * FROM packets WHERE company_id=? AND created_by_id=? AND completed_at IS NULL AND voided_at IS NULL",
                            (A.cid(), uid)).fetchall()
        has_rep = "sales_rep" in rows[0].keys() if rows else False
        for p in rows:
            db().execute("UPDATE packets SET created_by_id=?, created_by=? WHERE id=?", (t["id"], t["name"], p["id"]))
            if has_rep and (p["sales_rep"] or "") == u["name"]:
                db().execute("UPDATE packets SET sales_rep=? WHERE id=?", (t["name"], p["id"]))
            A.log_event(p["id"], "edited", f"Moved from {u['name']} to {t['name']} · by {A.actor()}")
        db().commit()
        n = len(rows)
        flash(f"{n} open packet{'s' if n != 1 else ''} moved from {u['name']} to {t['name']}." if n
              else f"{u['name']} has no open packets to move.", "ok")
        return redirect(url_for("team"))

    # ================================================================ Company
    def storage_breakdown(company_id):
        """Space used by kind, and the biggest files, for the Company page."""
        cats = {"library": 0, "sent": 0, "signed": 0, "client": 0, "brand": 0}
        files = {}

        def add(cat, folder, f, label, link):
            if not f:
                return
            path = os.path.join(folder, os.path.basename(f))
            if path in files:
                return
            try:
                size = os.path.getsize(path)
            except OSError:
                return
            files[path] = {"cat": cat, "size": size, "label": label, "link": link}
            cats[cat] += size

        for t in db().execute("SELECT id, name, file_path FROM templates WHERE company_id=? AND file_path IS NOT NULL", (company_id,)):
            add("library", A.UPLOAD_DIR, t["file_path"], f"{t['name']} (library form)", url_for("library"))
        for r in db().execute("SELECT i.*, p.client_name FROM packet_items i JOIN packets p ON p.id=i.packet_id WHERE p.company_id=?",
                              (company_id,)):
            link = url_for("packet_detail", pid=r["packet_id"])
            if r["template_file"]:
                add("sent", A.UPLOAD_DIR, r["template_file"], f"{r['template_name']} · {r['client_name']}", link)
            for f in (r["signed_pdf_path"], r["signature_path"]):
                add("signed", A.SIGNED_DIR, f, f"Signed {r['template_name']} · {r['client_name']}", link)
            try:
                for o in json.loads(r["config_json"] or "{}").get("options", []):
                    for f in (o.get("file"), o.get("display")):
                        add("sent", A.UPLOAD_DIR, f, f"{r['template_name']} picture · {r['client_name']}", link)
            except (ValueError, AttributeError):
                pass
        try:
            for r in db().execute("SELECT f.*, p.client_name FROM client_files f JOIN packets p ON p.id=f.packet_id WHERE p.company_id=?",
                                  (company_id,)):
                files[f"client:{r['id']}"] = {"cat": "client", "size": r["size"] or 0, "label": f"{r['name']} · from {r['client_name']}",
                                              "link": url_for("packet_detail", pid=r["packet_id"])}
                cats["client"] += r["size"] or 0
        except Exception:  # noqa: BLE001  (edition without client files)
            pass
        c = db().execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
        if c and "logo_file" in c.keys() and c["logo_file"]:
            add("brand", os.path.join(A.DATA_DIR, "brand"), c["logo_file"], "Your logo", url_for("company_settings") + "#brand")
        labels = {"library": "Library forms", "sent": "Files sent to clients", "signed": "Signed copies",
                  "client": "Photos and files from clients", "brand": "Logo"}
        biggest = sorted(files.values(), key=lambda f: -f["size"])[:8]
        return {"cats": [{"key": k, "label": labels[k], "bytes": v} for k, v in cats.items()], "biggest": biggest,
                "total": sum(cats.values())}

    app.jinja_env.globals["storage_breakdown"] = storage_breakdown

    @app.route("/company/test-packet", methods=["POST"])
    @A.login_required
    def company_test_packet():
        """A real packet to yourself, to see exactly what clients see."""
        if not g.user["id"]:
            flash("Sign in as a company user to send yourself a test.", "error")
            return redirect(url_for("company_settings"))
        t = db().execute("SELECT * FROM templates WHERE company_id=? AND archived=0 ORDER BY id LIMIT 1", (A.cid(),)).fetchone()
        token = secrets.token_urlsafe(24)
        expires = (utcnow() + timedelta(days=7)).isoformat()
        pid = db().execute("INSERT INTO packets (token, client_name, client_email, message, status, created_at, expires_at, created_by, "
                           "created_by_id, company_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (token, f"{g.user['name']} (test)", g.user["email"],
                            "This is a test packet you sent to yourself, to see what your clients see.", "draft", A.now(), expires,
                            A.actor(), g.user["id"], A.cid())).lastrowid
        if t:
            db().execute("INSERT INTO packet_items (packet_id, template_id, position, template_name, template_kind, template_file, "
                         "template_sha256, fields_json, template_body, file_name, file_mime) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (pid, t["id"], 0, t["name"], t["kind"], t["file_path"], t["file_sha256"], t["fields_json"],
                          t["description"] if t["kind"] == "form" else "", t["file_name"], t["file_mime"]))
        else:
            db().execute("INSERT INTO packet_items (packet_id, template_id, position, template_name, template_kind, fields_json, "
                         "template_body, adhoc) VALUES (?,?,?,?,?,?,?,1)",
                         (pid, None, 0, "Sample agreement", "form",
                          json.dumps([{"id": "f_phone", "label": "Phone number", "type": "phone", "required": False}]),
                          f"This is a sample agreement from {A.biz()}. Fill in your phone number and sign below to see how signing works."))
        A.log_event(pid, "created", f"Test packet to yourself · by {A.actor()}")
        db().commit()
        A.mark_sent(pid, "link")
        flash("Test packet made, addressed to you. Open the email, send it, then open the link from your inbox (or on your phone) "
              "to see exactly what clients see. Archive it when you're done.", "ok")
        return redirect(url_for("packet_detail", pid=pid, compose=1))

    # ================================================================ Follow-up
    @app.route("/packets/<int:pid>/snooze", methods=["POST"])
    @A.login_required
    def packet_snooze(pid):
        p = db().execute("SELECT * FROM packets WHERE id=?", (pid,)).fetchone() or abort(404)
        how = (request.get_json(silent=True) or request.form).get("until", "")
        local = A.to_local(utcnow())
        morning = lambda d: (local + timedelta(days=d)).replace(hour=8, minute=0, second=0, microsecond=0).astimezone(timezone.utc)  # noqa: E731
        until = {"tomorrow": morning(1), "3d": morning(3), "1w": morning(7)}.get(how)
        if how == "clear" or not until:
            db().execute("UPDATE packets SET snooze_until=NULL WHERE id=?", (pid,))
            A.log_event(pid, "followup", f"Back on the follow-up list · by {A.actor()}")
            msg = f"{p['client_name']} is back on the list."
        else:
            db().execute("UPDATE packets SET snooze_until=? WHERE id=?", (until.isoformat(), pid))
            label = A.to_local(until).strftime("%a %b %-d")
            A.log_event(pid, "followup", f"Snoozed until {label} · by {A.actor()}")
            msg = f"{p['client_name']} is snoozed until {label} morning."
        db().commit()
        if wants_json():
            return jsonify({"ok": True, "message": msg})
        flash(msg, "ok")
        return redirect(request.referrer or url_for("follow_up"))

    @app.route("/packets/<int:pid>/call-note", methods=["POST"])
    @A.login_required
    def packet_call_note(pid):
        """One-click follow-up note: fills the next follow-up slot (or adds to the notes when all are used)."""
        p = db().execute("SELECT * FROM packets WHERE id=?", (pid,)).fetchone() or abort(404)
        note = str((request.get_json(silent=True) or request.form).get("note", "")).strip()[:300]
        if not note:
            return jsonify({"error": "Write a note first."}), 400
        fu = A.followups_of(p)
        slot = next((i for i, f in enumerate(fu) if not f.get("done")), None)
        if slot is not None:
            fu[slot] = {"done": True, "at": A.now(), "by": A.actor(), "note": note}
            db().execute("UPDATE packets SET followups_json=?, last_nudge_at=? WHERE id=?", (json.dumps(fu), A.now(), pid))
            msg = f"Follow-up {slot + 1} recorded for {p['client_name']}: {note}"
        else:
            stamp = A.to_local(utcnow()).strftime("%b %-d %-I:%M %p")
            notes = ((p["notes"] or "") + f"\n{stamp} · {A.actor()}: {note}").strip()
            db().execute("UPDATE packets SET notes=?, last_nudge_at=? WHERE id=?", (notes, A.now(), pid))
            msg = f"Added to {p['client_name']}'s notes (all 5 follow-ups are used): {note}"
        A.log_event(pid, "followup", f"{note} · by {A.actor()}")
        db().commit()
        return jsonify({"ok": True, "message": msg})

    # ================================================================ Packet timeline
    def packet_timeline(p):
        """Steps from created to signed, with the time between them."""
        steps = [("Created", p["created_at"], "")]
        if p["sent_at"]:
            steps.append(("Sent", p["sent_at"], ""))
        if p["first_viewed_at"] if "first_viewed_at" in p.keys() else None:
            steps.append(("Opened", p["first_viewed_at"], ""))
        for it in db().execute("SELECT template_name, signed_at, signer_name FROM packet_items WHERE packet_id=? AND signed_at IS NOT NULL "
                               "ORDER BY signed_at", (p["id"],)):
            steps.append((f"Signed {A.daily.pretty_name(it['template_name'])}",
                          it["signed_at"], it["signer_name"] or ""))
        try:
            for s in db().execute("SELECT name, completed_at FROM packet_signers WHERE packet_id=? AND completed_at IS NOT NULL", (p["id"],)):
                steps.append((f"Second owner signed", s["completed_at"], s["name"]))
        except Exception:  # noqa: BLE001
            pass
        if p["completed_at"]:
            steps.append(("All signed", p["completed_at"], ""))
        if p["voided_at"]:
            steps.append(("Cancelled", p["voided_at"], ""))
        steps = [s for s in steps if parse(s[1])]
        steps.sort(key=lambda s: parse(s[1]))
        out, prev = [], None
        for label, at, who in steps:
            d = parse(at)
            out.append({"label": label, "at": at, "who": who, "gap": span((d - prev).total_seconds()) if prev else "",
                        "done": True})
            prev = d
        if not p["completed_at"] and not p["voided_at"]:
            out.append({"label": "Waiting for the client" if p["sent_at"] else "Not sent yet", "at": None, "who": "",
                        "gap": span((utcnow() - prev).total_seconds()) + " so far" if prev else "", "done": False})
        total = span((parse(p["completed_at"]) - parse(p["sent_at"])).total_seconds()) \
            if p["completed_at"] and p["sent_at"] and parse(p["completed_at"]) and parse(p["sent_at"]) else ""
        return {"steps": out, "total": total}

    app.jinja_env.globals["packet_timeline"] = packet_timeline

    # ================================================================ Account: desktop pop-ups
    def notify_prefs(u=None):
        u = u or g.get("user")
        try:
            saved = json.loads((u.get("notify_json") if u else "") or "{}")
        except (ValueError, TypeError):
            saved = {}
        return {k: bool(saved.get(k, True)) for k in NOTIFY_KINDS}

    app.jinja_env.globals["notify_prefs"] = notify_prefs
    app.jinja_env.globals["NOTIFY_KINDS"] = NOTIFY_KINDS

    @app.route("/account/notifications", methods=["POST"])
    @A.login_required
    def account_notifications():
        prefs = {k: request.form.get(k) == "on" for k in NOTIFY_KINDS}
        if g.user["id"]:
            db().execute("UPDATE users SET notify_json=? WHERE id=?", (json.dumps(prefs), g.user["id"]))
            db().commit()
        flash("Pop-up choices saved." + ("" if any(prefs.values()) else " You won't get any desktop pop-ups."), "ok")
        return redirect(url_for("account") + "#notifications")

    # recent clients for New packet chips
    def recent_clients(limit=6):
        cond, args = A.packet_scope()
        rows = db().execute(f"SELECT client_name, client_email, MAX(created_at) AS last FROM packets WHERE {cond} "
                            f"AND client_name NOT LIKE '% (test)' GROUP BY client_name COLLATE NOCASE ORDER BY last DESC LIMIT ?",
                            args + [limit]).fetchall()
        return [{"name": r[0], "email": r[1] or ""} for r in rows]

    app.jinja_env.globals["recent_clients"] = recent_clients

    def library_names(n=2):
        return [A.daily.pretty_name(r[0]) for r in db().execute(
            "SELECT name FROM templates WHERE company_id=? AND archived=0 ORDER BY id LIMIT ?", (A.cid(), n))]

    app.jinja_env.globals["library_names"] = library_names
