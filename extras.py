"""Account and client-contact features:

* storage check     warns the owner and managers when the server saves data to temporary storage
* branding          each company's logo and colour on the client signing pages
* 14-day demo       companies that sign up themselves (and companies the owner starts as a demo) get TRIAL_DAYS
                    days; the account locks on its own when the demo ends, until the platform owner gives it
                    another 14 days or marks it as paid
"""
import io
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import abort, flash, g, redirect, render_template, request, send_file, session, url_for


TRIAL_DAYS = int(os.environ.get("TRIAL_DAYS", "14"))
GRACE_DAYS = max(0, int(os.environ.get("DEMO_GRACE_DAYS", "3")))   # after the demo ends, before the account locks
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL") or os.environ.get("ADMIN_EMAIL") or "liz@helmmsp.ca"
CHECK_SECONDS = max(30, int(os.environ.get("REMINDER_CHECK_SECONDS", "300")))
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

SCHEMA = """
"""
COMPANY_COLS = {
    "plan": "TEXT DEFAULT 'paid'",          # 'trial' or 'paid'; companies that existed before stay 'paid'
    "trial_ends_at": "TEXT",
    "trial_notice_at": "TEXT",
    "paused_reason": "TEXT DEFAULT ''",     # 'trial_ended' when the trial paused it
    "brand_color": "TEXT DEFAULT ''",
    "logo_file": "TEXT DEFAULT ''",
}
PACKET_COLS = {}


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


def lock_at(c):
    """When a demo company locks: the end of the demo plus the grace days."""
    end = parse((c or {}).get("trial_ends_at"))
    return end + timedelta(days=GRACE_DAYS) if end else None


def grace_days_left(c):
    """Days of grace left after the demo ended (None when not in grace)."""
    if not c or c.get("plan") != "trial":
        return None
    end, lock = parse(c.get("trial_ends_at")), lock_at(c)
    if not end or end > utcnow() or not lock or lock <= utcnow():
        return None
    return max(1, -(-int((lock - utcnow()).total_seconds()) // 86400))


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
    state = {"thread": None, "last_trial_check": 0.0}
    temporary = storage_is_temporary(A.DATA_DIR)
    if temporary:
        app.logger.error("STORAGE WARNING: %s is not on a persistent disk. Everything (companies, packets, signed "
                         "copies) is erased when the server restarts. Add a disk and set DATA_DIR to its mount path.",
                         A.DATA_DIR)
    A.STORAGE_TEMPORARY = temporary
    brand_dir = os.path.join(A.DATA_DIR, "brand")
    os.makedirs(brand_dir, exist_ok=True)

    # ------------------------------------------------ page context
    @app.context_processor
    def inject_extras():
        c = g.get("company")
        return {"storage_temporary": temporary, "support_email": SUPPORT_EMAIL,
                "trial_days_left": trial_days_left(c), "brand": brand_of(c)}

    @app.before_request
    def extras_before():
        if state["thread"] is None and not app.config.get("EXTRAS_NO_THREAD"):
            state["thread"] = threading.Thread(target=_loop, daemon=True, name="demo-check")
            state["thread"].start()
        # a trial that has just ended pauses the company right away, even between background checks
        if time.time() - state["last_trial_check"] > 60:
            state["last_trial_check"] = time.time()
            expire_trials(db())
        c = g.get("company")
        if c and c.get("plan") == "trial" and g.get("user") and g.user["id"] != 0:
            lock = lock_at(c)
            if lock and lock <= utcnow():
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
            except Exception:  # noqa: BLE001
                app.logger.exception("Background demo check failed; will try again")
            time.sleep(CHECK_SECONDS)

    def expire_trials(conn):
        """Lock companies whose demo has ended (the managers see the days left at the top of every page)."""
        nowdt = utcnow()
        for c in conn.execute("SELECT * FROM companies WHERE plan='trial' AND status='active' AND trial_ends_at IS NOT "
                              "NULL AND trial_ends_at <= ?", (iso(nowdt - timedelta(days=GRACE_DAYS)),)).fetchall():
            n = conn.execute("UPDATE companies SET status='suspended', paused_reason='trial_ended' WHERE id=? AND "
                             "status='active'", (c["id"],)).rowcount
            conn.commit()
            if n:
                app.logger.info("Demo ended: %s (company %s) is now locked", c["name"], c["id"])

    A.expire_trials = expire_trials

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
        flash("Branding saved. Your clients see it on the signing pages.", "ok")
        return redirect(url_for("company_settings") + "#brand")
