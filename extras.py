"""Account and client-contact features:

* storage check     warns the owner and managers when the server saves data to temporary storage
* forgot password   a one-time email link (60 minutes) to choose a new password
* auto reminders    emails (and texts) clients who haven't finished signing, on each company's schedule
* branding          each company's logo and colour on client signing pages and client emails
* text messages     send the signing link and reminders by SMS through Twilio (see sms.py)
* 14-day demo       companies that sign up themselves (and companies the owner starts as a demo) get TRIAL_DAYS
                    days; the account locks on its own when the demo ends, until the platform owner gives it
                    another 14 days or marks it as paid
"""
import hashlib
import io
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import abort, flash, g, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import generate_password_hash

import sms

TRIAL_DAYS = int(os.environ.get("TRIAL_DAYS", "14"))
TRIAL_WARN_DAYS = 3
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL") or os.environ.get("ADMIN_EMAIL", "")
CHECK_SECONDS = max(30, int(os.environ.get("REMINDER_CHECK_SECONDS", "300")))
RESET_MINUTES = 60
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS password_resets (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT
);
"""
COMPANY_COLS = {
    "plan": "TEXT DEFAULT 'paid'",          # 'trial' or 'paid'; companies that existed before stay 'paid'
    "trial_ends_at": "TEXT",
    "trial_notice_at": "TEXT",
    "paused_reason": "TEXT DEFAULT ''",     # 'trial_ended' when the trial paused it
    "brand_color": "TEXT DEFAULT ''",
    "logo_file": "TEXT DEFAULT ''",
    "reminders_on": "INTEGER DEFAULT 1",
    "reminder_days": "INTEGER DEFAULT 2",
    "reminder_max": "INTEGER DEFAULT 3",
}
PACKET_COLS = {"client_phone": "TEXT DEFAULT ''", "reminder_count": "INTEGER DEFAULT 0", "last_reminder_at": "TEXT"}


def init_db(conn):
    conn.executescript(SCHEMA)
    for table, wanted in (("companies", COMPANY_COLS), ("packets", PACKET_COLS)):
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, spec in wanted.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {spec}")


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


# ---------------------------------------------------------------- storage check
def _on_mounted_disk(path):
    p = os.path.realpath(path)
    while p and p != os.path.dirname(p):
        if os.path.ismount(p):
            return True
        p = os.path.dirname(p)
    return False


def storage_is_temporary(data_dir):
    """True when the data folder isn't on a persistent disk. Checked on Render (where it matters) unless
    STORAGE_CHECK=0; STORAGE_CHECK=1 forces the check anywhere."""
    mode = os.environ.get("STORAGE_CHECK", "auto")
    if mode == "0" or (mode == "auto" and not os.environ.get("RENDER")):
        return False
    return not _on_mounted_disk(data_dir)


# ---------------------------------------------------------------- trial
def start_trial(conn, company_id):
    conn.execute("UPDATE companies SET plan='trial', trial_ends_at=? WHERE id=?",
                 (iso(utcnow() + timedelta(days=TRIAL_DAYS)), company_id))


def trial_days_left(c):
    if not c or c.get("plan") != "trial":
        return None
    end = parse(c.get("trial_ends_at"))
    if not end:
        return None
    return max(0, -(-int((end - utcnow()).total_seconds()) // 86400))  # round up: 1.2 days left -> 2


def paused_message(company, platform):
    if company and company.get("paused_reason") == "trial_ended":
        who = f" contact {SUPPORT_EMAIL}" if SUPPORT_EMAIL else f" contact {platform}"
        return f"Your company's {TRIAL_DAYS}-day demo has ended. To keep using {platform},{who}. Nothing has been deleted."
    return f"Your company's {platform} account is paused. Contact {platform} to turn it back on."


def owner_action(conn, company_id, action, days=None):
    """Plan changes from the owner's Companies screen or the Admin Console. Returns a message, or None if the
    action isn't a plan action."""
    c = conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
    if action == "mark_paid":
        conn.execute("UPDATE companies SET plan='paid', status='active', paused_reason='' WHERE id=?", (company_id,))
        return f"{c['name']} is marked as paid and active. It no longer has a demo end date."
    if action == "extend_trial":
        try:
            days = max(1, min(int(days or TRIAL_DAYS), 365))
        except (TypeError, ValueError):
            days = TRIAL_DAYS
        start = max(parse(c["trial_ends_at"]) or utcnow(), utcnow())
        end = start + timedelta(days=days)
        conn.execute("UPDATE companies SET plan='trial', trial_ends_at=?, trial_notice_at=NULL, status='active', "
                     "paused_reason='' WHERE id=?", (iso(end), company_id))
        return f"{c['name']}'s demo now ends {end.strftime('%b %d, %Y')} ({days} more days). Its team can sign in."
    if action == "activate":
        # turning on a company whose demo already ended means it's now a paying customer
        if c["plan"] == "trial" and (parse(c["trial_ends_at"]) or utcnow()) <= utcnow():
            conn.execute("UPDATE companies SET plan='paid', status='active', paused_reason='' WHERE id=?",
                         (company_id,))
            return f"{c['name']} is active again and marked as paid (its demo had ended)."
        conn.execute("UPDATE companies SET status='active', paused_reason='' WHERE id=?", (company_id,))
        return None
    if action == "suspend":
        conn.execute("UPDATE companies SET paused_reason='' WHERE id=?", (company_id,))
    return None


# ---------------------------------------------------------------- branding
def brand_of(c, base_url=""):
    """Colour and logo for one company: {'color', 'ink', 'logo_url'} (values may be empty)."""
    c = c or {}
    color = c.get("brand_color") or ""
    out = {"color": "", "ink": "", "logo_url": ""}
    if HEX.match(color):
        r, g_, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
        lin = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in (r, g_, b)]
        lum = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
        out["color"] = color.lower()
        out["ink"] = "#ffffff" if lum < 0.42 else "#10131a"   # readable text on the button
    if c.get("logo_file"):
        out["logo_url"] = f"{base_url}/brand/{c['id']}/{c['logo_file']}"
    return out


# ---------------------------------------------------------------- register
def register(A):
    app, db = A.app, A.db
    state = {"base": "", "thread": None, "last_trial_check": 0.0}
    temporary = storage_is_temporary(A.DATA_DIR)
    if temporary:
        app.logger.error("STORAGE WARNING: %s is not on a persistent disk. Everything (companies, packets, signed "
                         "copies) is erased when the server restarts. Add a disk and set DATA_DIR to its mount path.",
                         A.DATA_DIR)
    A.STORAGE_TEMPORARY = temporary
    brand_dir = os.path.join(A.DATA_DIR, "brand")
    os.makedirs(brand_dir, exist_ok=True)

    def base_url():
        return A.BASE_URL or state["base"]

    def add_event(conn, pid, type_, detail):
        conn.execute("INSERT INTO events (packet_id, type, detail, ip, at) VALUES (?,?,?,?,?)",
                     (pid, type_, detail, "", A.now()))

    def managers_of(conn, company_id):
        return [r["email"] for r in conn.execute("SELECT email FROM users WHERE company_id=? AND active=1 AND "
                                                 "role='manager'", (company_id,))]

    # ------------------------------------------------ page context
    @app.context_processor
    def inject_extras():
        c = g.get("company")
        return {"storage_temporary": temporary, "sms_enabled": sms.enabled(), "support_email": SUPPORT_EMAIL,
                "trial_days_left": trial_days_left(c), "brand": brand_of(c), "sms_pretty": sms.pretty,
                "reminder_info": reminder_info}

    @app.before_request
    def extras_before():
        if not state["base"] and request.url_root:
            state["base"] = request.url_root.rstrip("/")   # for links in background emails when BASE_URL is empty
            A.BACKGROUND_BASE = state["base"]
        if state["thread"] is None and not app.config.get("EXTRAS_NO_THREAD"):
            state["thread"] = threading.Thread(target=_loop, daemon=True, name="reminders")
            state["thread"].start()
        # a trial that has just ended pauses the company right away, even between background checks
        if time.time() - state["last_trial_check"] > 60:
            state["last_trial_check"] = time.time()
            expire_trials(db())
        c = g.get("company")
        if c and c.get("plan") == "trial" and g.get("user") and g.user["id"] != 0:
            end = parse(c.get("trial_ends_at"))
            if end and end <= utcnow():
                expire_trials(db())
                session.pop("uid", None)
                flash(paused_message(A.get_company(c["id"]), A.PLATFORM_NAME), "error")
                return redirect(url_for("login"))

    # ------------------------------------------------ background work
    def _loop():
        time.sleep(5)
        while True:
            try:
                with app.app_context():
                    expire_trials(db())
                    run_reminders(db())
                    if hasattr(A, "run_cosigner_reminders"):
                        A.run_cosigner_reminders(db())
            except Exception:  # noqa: BLE001
                app.logger.exception("Background reminders check failed; will try again")
            time.sleep(CHECK_SECONDS)

    def expire_trials(conn):
        """Pause companies whose trial has ended, and warn their managers a few days before."""
        nowdt = utcnow()
        for c in conn.execute("SELECT * FROM companies WHERE plan='trial' AND status='active' AND trial_ends_at IS NOT "
                              "NULL AND trial_notice_at IS NULL AND trial_ends_at < ? AND trial_ends_at > ?",
                              (iso(nowdt + timedelta(days=TRIAL_WARN_DAYS)), iso(nowdt))).fetchall():
            n = conn.execute("UPDATE companies SET trial_notice_at=? WHERE id=? AND trial_notice_at IS NULL",
                             (A.now(), c["id"])).rowcount
            conn.commit()
            if n and A.mailer.enabled():
                end = parse(c["trial_ends_at"]).strftime("%b %d, %Y")
                contact = f"reply to this email or contact {SUPPORT_EMAIL}" if SUPPORT_EMAIL else "reply to this email"
                for to in managers_of(conn, c["id"]):
                    try:
                        A.mailer.send_notice(to, f"{A.PLATFORM_NAME}: your demo ends {end}", [
                            f"Your {A.PLATFORM_NAME} demo for {c['name']} ends on {end}.",
                            f"To keep sending packets after that, {contact}. Your forms, packets and signed copies "
                            f"stay safe either way."], reply_to=SUPPORT_EMAIL or None)
                    except Exception as exc:  # noqa: BLE001
                        app.logger.warning("Trial notice to %s failed: %s", to, exc)
        for c in conn.execute("SELECT * FROM companies WHERE plan='trial' AND status='active' AND trial_ends_at IS NOT "
                              "NULL AND trial_ends_at <= ?", (iso(nowdt),)).fetchall():
            n = conn.execute("UPDATE companies SET status='suspended', paused_reason='trial_ended' WHERE id=? AND "
                             "status='active'", (c["id"],)).rowcount
            conn.commit()
            if not n:
                continue
            app.logger.info("Trial ended: %s (company %s) is now paused", c["name"], c["id"])
            if not A.mailer.enabled():
                continue
            contact = f"contact {SUPPORT_EMAIL}" if SUPPORT_EMAIL else "reply to this email"
            for to in managers_of(conn, c["id"]):
                try:
                    A.mailer.send_notice(to, f"{A.PLATFORM_NAME}: your demo has ended", [
                        f"Your {A.PLATFORM_NAME} demo for {c['name']} has ended, so your team's sign-in is "
                        f"locked.", f"To continue, {contact}. Nothing has been deleted, and signing links already "
                        f"sent to your clients keep working."], reply_to=SUPPORT_EMAIL or None)
                except Exception as exc:  # noqa: BLE001
                    app.logger.warning("Trial-ended notice to %s failed: %s", to, exc)
            owner = A.mailer.admin_email()
            if owner:
                try:
                    A.mailer.send_notice(owner, f"Demo ended: {c['name']}", [
                        f"{c['name']}'s demo ended and the company is now locked.",
                        f"Manager: {', '.join(managers_of(conn, c['id'])) or 'none'}.",
                        "Give it another 14 days or mark it as paid from the Companies screen or the Admin Console."])
                except Exception as exc:  # noqa: BLE001
                    app.logger.warning("Trial-ended notice to owner failed: %s", exc)

    def reminder_info(p):
        """For the packet page: how automatic reminders stand for one packet."""
        c = g.get("company") or {}
        if not c.get("reminders_on"):
            return {"on": False}
        days, most = c.get("reminder_days") or 2, c.get("reminder_max") or 3
        sent = p["reminder_count"] or 0
        last = parse(p["last_reminder_at"] or p["sent_at"])
        nxt = (last + timedelta(days=days)) if last and sent < most else None
        exp = parse(p["expires_at"])
        if nxt and exp and nxt > exp:
            nxt = None
        can = (A.mailer.enabled() and "@" in (p["client_email"] or "")) or (sms.enabled() and p["client_phone"])
        return {"on": True, "sent": sent, "max": most, "days": days, "next": iso(nxt) if nxt else None, "can": bool(can)}

    def run_reminders(conn):
        """Email (and text) every client who still hasn't finished, on their company's schedule."""
        if not (A.mailer.enabled() or sms.enabled()):
            return 0
        base = base_url()
        if not base:
            return 0
        nowdt = utcnow()
        done = 0
        rows = conn.execute(
            "SELECT p.*, c.name AS company_name, c.reminder_days, c.reminder_max FROM packets p "
            "JOIN companies c ON c.id = p.company_id WHERE c.status='active' AND c.reminders_on=1 "
            "AND p.completed_at IS NULL AND p.voided_at IS NULL AND p.sent_at IS NOT NULL AND p.expires_at > ? "
            "AND COALESCE(p.reminder_count, 0) < c.reminder_max", (iso(nowdt),)).fetchall()
        for p in rows:
            last_raw = p["last_reminder_at"] or p["sent_at"]
            last = parse(last_raw)
            if not last or nowdt - last < timedelta(days=max(1, p["reminder_days"] or 2)):
                continue
            use_email = A.mailer.enabled() and "@" in (p["client_email"] or "")
            use_text = sms.enabled() and bool(p["client_phone"])
            if not (use_email or use_text):
                continue
            # claim it, so two server processes never send the same reminder twice
            n = conn.execute("UPDATE packets SET last_reminder_at=?, reminder_count=COALESCE(reminder_count,0)+1 "
                             "WHERE id=? AND COALESCE(last_reminder_at, sent_at)=? AND COALESCE(reminder_count,0)=?",
                             (A.now(), p["id"], last_raw, p["reminder_count"] or 0)).rowcount
            conn.commit()
            if not n:
                continue
            company = A.get_company(p["company_id"])
            g.company = company
            link = f"{base}/s/{p['token']}"
            items = [r["template_name"] for r in conn.execute(
                "SELECT template_name FROM packet_items WHERE packet_id=? AND requires_signature=1 AND signed_at IS "
                "NULL ORDER BY position", (p["id"],))] or [r["template_name"] for r in conn.execute(
                    "SELECT template_name FROM packet_items WHERE packet_id=? ORDER BY position", (p["id"],))]
            label = f"automatic reminder {(p['reminder_count'] or 0) + 1} of {p['reminder_max']}"
            how = []
            if use_email:
                try:
                    A.mailer.send_request(p["client_email"], p["client_name"], company["name"], items, link, "",
                                          p["expires_at"], reply_to=(A.staff_recipients(p) or [None])[0],
                                          brand=brand_of(company, base), reminder=True)
                    how.append("email")
                except Exception as exc:  # noqa: BLE001
                    add_event(conn, p["id"], "email_failed", f"{label}: {str(exc)[:160]}")
            if use_text:
                try:
                    sms.send(p["client_phone"], text_body(company["name"], p["client_name"], link, reminder=True))
                    how.append("text")
                except Exception as exc:  # noqa: BLE001
                    add_event(conn, p["id"], "text_failed", f"{label}: {str(exc)[:160]}")
            if how:
                add_event(conn, p["id"], "reminder", f"{label} by {' and '.join(how)}")
                done += 1
            else:  # nothing went out: don't use up a reminder; try again after the usual wait
                conn.execute("UPDATE packets SET reminder_count=MAX(COALESCE(reminder_count,0)-1, 0) WHERE id=?",
                             (p["id"],))
            conn.commit()
        return done

    A.run_reminders = run_reminders
    A.expire_trials = expire_trials

    # ------------------------------------------------ text messages
    def text_body(business, client_name, link, reminder=False):
        first = (client_name or "").split(" ")[0]
        if reminder:
            return f"{business}: Hi {first}, a reminder that your forms are still waiting for your signature: {link}"
        return f"{business}: Hi {first}, please review and sign your forms here: {link}"

    def text_packet(pid):
        """Send the signing link by text. Returns True if it went out (and flashes the result)."""
        p = db().execute("SELECT * FROM packets WHERE id=?", (pid,)).fetchone()
        if not sms.enabled():
            flash("Texting isn't set up on this server yet.", "error")
            return False
        if not p["client_phone"]:
            flash("Add the client's mobile number first.", "error")
            return False
        try:
            sms.send(p["client_phone"], text_body(A.biz(), p["client_name"], A.signing_url(p["token"])))
        except RuntimeError as exc:
            A.log_event(pid, "text_failed", str(exc)[:200])
            db().commit()
            flash(f"The text didn't go out ({exc}). You can still copy the link below.", "error")
            return False
        if p["status"] == "draft":
            db().execute("UPDATE packets SET status='sent', sent_at=? WHERE id=?", (A.now(), pid))
        db().execute("UPDATE packets SET last_reminder_at=? WHERE id=?", (A.now(), pid))
        A.log_event(pid, "sent", f"by text to {sms.pretty(p['client_phone'])} · by {A.actor()}")
        db().commit()
        flash(f"Texted to {sms.pretty(p['client_phone'])}.", "ok")
        if hasattr(A, "notify_cosigners"):
            A.notify_cosigners(pid, "text")
        return True

    A.text_packet = text_packet

    @app.route("/packets/<int:pid>/text", methods=["POST"])
    @A.login_required
    def text_packet_view(pid):
        text_packet(pid)
        return redirect(url_for("packet_detail", pid=pid))

    @app.route("/packets/<int:pid>/phone", methods=["POST"])
    @A.login_required
    def packet_phone(pid):
        raw = request.form.get("client_phone", "").strip()
        phone = sms.normalize(raw) if raw else ""
        if raw and not phone:
            flash("That doesn't look like a mobile number. Use 10 digits, like 613-555-0123.", "error")
        else:
            db().execute("UPDATE packets SET client_phone=? WHERE id=?", (phone, pid))
            A.log_event(pid, "details", f"mobile number {'updated' if phone else 'removed'} by {A.actor()}")
            db().commit()
            flash("Mobile number saved." if phone else "Mobile number removed.", "ok")
            if phone and request.form.get("send") == "1":
                text_packet(pid)
        return redirect(url_for("packet_detail", pid=pid))

    # ------------------------------------------------ forgot password
    _asks = {}

    @app.route("/forgot", methods=["GET", "POST"])
    def forgot_password():
        sent = False
        if request.method == "POST":
            email = request.form.get("email", "").strip()[:120]
            key = A.client_ip()
            recent = [t for t in _asks.get(key, []) if t > time.time() - 3600]
            _asks[key] = recent + [time.time()]
            if len(recent) < 5 and A.mailer.enabled() and "@" in email:
                u = db().execute("SELECT u.* FROM users u JOIN companies c ON c.id=u.company_id WHERE u.email=? "
                                 "AND u.active=1", (email,)).fetchone()
                if u:
                    asked = db().execute("SELECT COUNT(*) FROM password_resets WHERE user_id=? AND created_at > ?",
                                         (u["id"], iso(utcnow() - timedelta(hours=1)))).fetchone()[0]
                    if asked < 3:
                        raw = secrets.token_urlsafe(32)
                        db().execute("INSERT INTO password_resets (token_hash, user_id, created_at, expires_at) "
                                     "VALUES (?,?,?,?)", (hashlib.sha256(raw.encode()).hexdigest(), u["id"], A.now(),
                                                          iso(utcnow() + timedelta(minutes=RESET_MINUTES))))
                        db().execute("DELETE FROM password_resets WHERE expires_at < ?",
                                     (iso(utcnow() - timedelta(days=2)),))
                        db().commit()
                        try:
                            A.mailer.send_password_reset(u["email"], u["name"], f"{base_url()}/reset/{raw}",
                                                         A.PLATFORM_NAME, RESET_MINUTES)
                        except Exception as exc:  # noqa: BLE001
                            app.logger.warning("Password reset email to %s failed: %s", email, exc)
            sent = True  # same answer whether or not the email has a login, so nobody can test addresses
        return render_template("forgot.html", sent=sent, email_enabled=A.mailer.enabled(),
                               email=request.form.get("email", ""))

    def reset_row(raw):
        row = db().execute("SELECT r.*, u.name, u.email FROM password_resets r JOIN users u ON u.id = r.user_id "
                           "WHERE r.token_hash=? AND u.active=1", (hashlib.sha256(raw.encode()).hexdigest(),)).fetchone()
        if not row or row["used_at"] or row["expires_at"] < A.now():
            return None
        return row

    @app.route("/reset/<raw>", methods=["GET", "POST"])
    def reset_password(raw):
        row = reset_row(raw)
        if not row:
            return render_template("error.html", title="This reset link has expired",
                                   message="Reset links work once, for 60 minutes. Ask for a new one from the "
                                           "sign-in page."), 410
        error = None
        if request.method == "POST":
            pw = request.form.get("password", "")
            error = A.valid_password(pw) or (None if pw == request.form.get("confirm") else "The passwords don't match.")
            if not error:
                db().execute("UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
                             (generate_password_hash(pw), row["user_id"]))
                db().execute("UPDATE password_resets SET used_at=? WHERE user_id=? AND used_at IS NULL",
                             (A.now(), row["user_id"]))
                db().commit()
                session.clear()
                flash("Your password is changed. Sign in with your new password.", "ok")
                return redirect(url_for("login"))
        return render_template("reset.html", row=row, error=error)

    # ------------------------------------------------ branding
    @app.route("/brand/<int:company_id>/<name>")
    def brand_logo(company_id, name):
        c = A.get_company(company_id)
        if not c or not c.get("logo_file") or name != c["logo_file"]:
            abort(404)
        resp = send_file(os.path.join(brand_dir, os.path.basename(name)), mimetype="image/png", max_age=86400 * 30)
        return resp

    @app.route("/company/brand", methods=["POST"])
    @A.manager_required
    def company_brand():
        color = request.form.get("brand_color", "").strip()
        if request.form.get("reset_color"):
            color = ""
        if color and not HEX.match(color):
            flash("Choose the colour with the colour picker, or enter it like #1f6f43.", "error")
            return redirect(url_for("company_settings") + "#brand")
        logo = g.company.get("logo_file") or ""
        f = request.files.get("logo")
        if request.form.get("remove_logo"):
            logo = ""
        elif f and f.filename:
            data = f.read(5 * 1024 * 1024 + 1)
            if len(data) > 5 * 1024 * 1024:
                flash("The logo is over 5 MB. Use a smaller image.", "error")
                return redirect(url_for("company_settings") + "#brand")
            try:
                from PIL import Image
                img = Image.open(io.BytesIO(data))
                img.load()
                img = img.convert("RGBA")
                img.thumbnail((900, 300))
            except Exception:  # noqa: BLE001
                flash("That file isn't an image we can use. Upload a PNG or JPG.", "error")
                return redirect(url_for("company_settings") + "#brand")
            logo = f"logo-{secrets.token_hex(6)}.png"
            img.save(os.path.join(brand_dir, logo), "PNG", optimize=True)
        old = g.company.get("logo_file") or ""
        db().execute("UPDATE companies SET brand_color=?, logo_file=? WHERE id=?", (color.lower(), logo, A.cid()))
        db().commit()
        if old and old != logo:
            try:
                os.remove(os.path.join(brand_dir, os.path.basename(old)))
            except OSError:
                pass
        flash("Branding saved. Your clients see it on the signing pages and in the emails they get.", "ok")
        return redirect(url_for("company_settings") + "#brand")

    @app.route("/company/reminders", methods=["POST"])
    @A.manager_required
    def company_reminders():
        on = 1 if request.form.get("reminders_on") == "on" else 0
        try:
            days = int(request.form.get("reminder_days", "2"))
            most = int(request.form.get("reminder_max", "3"))
        except ValueError:
            days, most = 0, 0
        if not (1 <= days <= 14 and 1 <= most <= 10):
            flash("Choose between 1 and 14 days, and between 1 and 10 reminders.", "error")
        else:
            db().execute("UPDATE companies SET reminders_on=?, reminder_days=?, reminder_max=? WHERE id=?",
                         (on, days, most, A.cid()))
            db().commit()
            flash("Automatic reminders are on." if on else "Automatic reminders are off.", "ok")
        return redirect(url_for("company_settings") + "#reminders")
