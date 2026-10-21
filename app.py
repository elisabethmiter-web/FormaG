"""FormSign — store forms, send them to clients as a signing packet,
and get confirmation once every form is signed.

Run locally:  ADMIN_PASSWORD=changeme python app.py
"""
import base64
import hashlib
import hmac
import json
import logging

try:
    import fcntl  # Linux/macOS servers
except ImportError:  # Windows: run without the start-up lock
    fcntl = None
import mimetypes
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_file, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import admin_api
import extras
import workflow
import daily
import pdfgen
import seal
import preview

# ---------------------------------------------------------------- config
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
DB_PATH = os.path.join(DATA_DIR, "formsign.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "templates")
SIGNED_DIR = os.path.join(DATA_DIR, "signed")
for d in (DATA_DIR, UPLOAD_DIR, SIGNED_DIR):
    os.makedirs(d, exist_ok=True)

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
PLATFORM_NAME = os.environ.get("PLATFORM_NAME", "FormSign")   # your service's name (login page, owner screens)
BUSINESS_NAME = os.environ.get("BUSINESS_NAME", "")            # only used to name the first company when upgrading
ALLOW_SIGNUP = os.environ.get("ALLOW_SIGNUP", "1") == "1"      # companies can create their own account
INCLUDED_STORAGE_GB = int(os.environ.get("INCLUDED_STORAGE_GB", "25"))
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
LINK_DAYS = int(os.environ.get("LINK_EXPIRY_DAYS", "30"))
SIGN_WITHIN_HOURS = int(os.environ.get("SIGN_WITHIN_HOURS", "48"))   # target time for the client to finish
FOLLOWUP_SLOTS = 5
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(os.environ.get("TIMEZONE", "America/Toronto"))
except Exception:  # noqa: BLE001  (no time-zone data installed)
    LOCAL_TZ = timezone.utc

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB per upload
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
if os.environ.get("HTTPS", "1") == "1" and BASE_URL.startswith("https"):
    app.config["SESSION_COOKIE_SECURE"] = True

FIELD_TYPES = ["text", "textarea", "email", "phone", "date", "number", "checkbox", "initials"]


# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended')),
  notify_email TEXT DEFAULT '',
  storage_gb INTEGER NOT NULL DEFAULT 25,
  source TEXT NOT NULL DEFAULT 'owner',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS templates (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('pdf','file','form')),
  description TEXT DEFAULT '',
  file_path TEXT,
  file_sha256 TEXT,
  fields_json TEXT DEFAULT '[]',
  archived INTEGER DEFAULT 0,
  created_at TEXT NOT NULL,
  file_name TEXT,
  file_mime TEXT,
  company_id INTEGER
);
CREATE TABLE IF NOT EXISTS packets (
  id INTEGER PRIMARY KEY,
  token TEXT UNIQUE NOT NULL,
  client_name TEXT NOT NULL,
  client_email TEXT NOT NULL,
  message TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',
  created_at TEXT NOT NULL,
  sent_at TEXT,
  first_viewed_at TEXT,
  completed_at TEXT,
  expires_at TEXT,
  voided_at TEXT,
  so_number TEXT DEFAULT '',
  sales_rep TEXT DEFAULT '',
  created_by TEXT DEFAULT '',
  created_by_id INTEGER,
  followups_json TEXT DEFAULT '[]',
  notes TEXT DEFAULT '',
  company_id INTEGER
);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT UNIQUE NOT NULL COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('staff','manager')),
  active INTEGER NOT NULL DEFAULT 1,
  must_change_password INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  last_login_at TEXT,
  company_id INTEGER
);
CREATE TABLE IF NOT EXISTS packet_items (
  id INTEGER PRIMARY KEY,
  packet_id INTEGER NOT NULL REFERENCES packets(id),
  template_id INTEGER REFERENCES templates(id),
  position INTEGER NOT NULL,
  template_name TEXT NOT NULL,
  template_kind TEXT NOT NULL,
  template_file TEXT,
  template_sha256 TEXT,
  fields_json TEXT DEFAULT '[]',
  template_body TEXT DEFAULT '',
  answers_json TEXT,
  signer_name TEXT,
  signature_path TEXT,
  signed_at TEXT,
  signed_pdf_path TEXT,
  signed_sha256 TEXT,
  ip TEXT,
  user_agent TEXT,
  file_name TEXT,
  file_mime TEXT,
  requires_signature INTEGER NOT NULL DEFAULT 1,
  adhoc INTEGER NOT NULL DEFAULT 0,
  downloaded_at TEXT,
  config_json TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  packet_id INTEGER NOT NULL REFERENCES packets(id),
  type TEXT NOT NULL,
  detail TEXT DEFAULT '',
  ip TEXT,
  at TEXT NOT NULL
);
"""


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def _rebuild_if(conn, table, outdated):
    """Upgrade a table from an older version of the app, keeping its rows."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if not row or not outdated(row[0]):
        return
    old_cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(f"ALTER TABLE {table} RENAME TO {table}_old;")
    conn.executescript(SCHEMA)
    new_cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    cols = ", ".join(c for c in old_cols if c in new_cols)
    conn.executescript(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM {table}_old; DROP TABLE {table}_old;")


def init_db():
    # Several server workers start at the same time; only one may upgrade the database.
    if fcntl is None:
        return _init_db_locked()
    with open(os.path.join(DATA_DIR, ".dbinit.lock"), "w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        try:
            _init_db_locked()
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)


def _init_db_locked():
    conn = sqlite3.connect(DB_PATH)
    _rebuild_if(conn, "templates", lambda sql: "'file'" not in sql or "file_mime" not in sql)
    _rebuild_if(conn, "packet_items", lambda sql: "template_id INTEGER NOT NULL" in sql or "requires_signature" not in sql
                or "config_json" not in sql)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(packets)")}
    for col in ("so_number", "sales_rep", "created_by", "notes"):
        if cols and col not in cols:
            conn.execute(f"ALTER TABLE packets ADD COLUMN {col} TEXT DEFAULT ''")
    if cols and "followups_json" not in cols:
        conn.execute("ALTER TABLE packets ADD COLUMN followups_json TEXT DEFAULT '[]'")
    if cols and "created_by_id" not in cols:
        conn.execute("ALTER TABLE packets ADD COLUMN created_by_id INTEGER")
    for table in ("templates", "packets", "users"):
        tcols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if tcols and "company_id" not in tcols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN company_id INTEGER")
    conn.executescript(SCHEMA)
    conn.executescript("CREATE INDEX IF NOT EXISTS ix_packets_company ON packets(company_id);"
                       "CREATE INDEX IF NOT EXISTS ix_templates_company ON templates(company_id);"
                       "CREATE INDEX IF NOT EXISTS ix_users_company ON users(company_id);")
    # Upgrading from the single-company version: everything that exists becomes the first company.
    orphans = sum(conn.execute(f"SELECT COUNT(*) FROM {t} WHERE company_id IS NULL").fetchone()[0]
                  for t in ("templates", "packets", "users"))
    if orphans:
        first = conn.execute("SELECT id FROM companies ORDER BY id LIMIT 1").fetchone()
        if first:
            first_id = first[0]
        else:
            first_id = conn.execute("INSERT INTO companies (name, storage_gb, source, created_at) VALUES (?,?,?,?)",
                                    (BUSINESS_NAME or "My company", INCLUDED_STORAGE_GB, "owner",
                                     datetime.now(timezone.utc).replace(microsecond=0).isoformat())).lastrowid
        for t in ("templates", "packets", "users"):
            conn.execute(f"UPDATE {t} SET company_id=? WHERE company_id IS NULL", (first_id,))
    # Older packets only stored the creator's name: link them to that login, or to the owner if none matches.
    conn.execute("UPDATE packets SET created_by_id = COALESCE((SELECT u.id FROM users u WHERE u.name = "
                 "packets.created_by COLLATE NOCASE ORDER BY u.id LIMIT 1), 0) WHERE created_by_id IS NULL")
    admin_api.init_db(conn)  # Admin Console links
    extras.init_db(conn)  # demo, branding, storage check
    workflow.init_db(conn)  # second owners, client files, bundles, signature boxes
    daily.init_db(conn)  # access codes, last visit, time zones
    conn.commit()
    conn.close()


init_db()


# ---------------------------------------------------------------- helpers
def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or ""


def log_event(packet_id, type_, detail=""):
    db().execute("INSERT INTO events (packet_id, type, detail, ip, at) VALUES (?,?,?,?,?)",
                 (packet_id, type_, detail, client_ip(), now()))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def signing_url(token):
    base = BASE_URL or request.url_root.rstrip("/")
    return f"{base}/s/{token}"


@app.template_filter("when")
def fmt_when(value):
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    loc = to_local(dt)
    return f"{loc.strftime('%b %d, %Y')} · {loc.strftime('%I:%M %p').lstrip('0')} {loc.strftime('%Z')}"


def to_local(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ)


def human_span(seconds):
    seconds = abs(int(seconds))
    if seconds < 3600:
        return f"{max(1, seconds // 60)} min"
    if seconds < 48 * 3600:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} days"


def deadline_info(p):
    """48-hour signing target: when it's due and whether it's been missed."""
    if not p["sent_at"] or p["completed_at"] or p["voided_at"]:
        return {"due_at": None, "overdue": False, "label": ""}
    due = datetime.fromisoformat(p["sent_at"]) + timedelta(hours=SIGN_WITHIN_HOURS)
    left = (due - datetime.now(timezone.utc)).total_seconds()
    return {"due_at": due.isoformat(), "overdue": left < 0,
            "label": f"Overdue by {human_span(left)}" if left < 0 else f"Due in {human_span(left)}"}


def followups_of(p):
    try:
        fu = json.loads(p["followups_json"] or "[]")
    except (ValueError, TypeError):
        fu = []
    fu = [f for f in fu if isinstance(f, dict)][:FOLLOWUP_SLOTS]
    while len(fu) < FOLLOWUP_SLOTS:
        fu.append({"done": False, "at": None, "by": "", "note": ""})
    return fu


def last_followup(p):
    fu = followups_of(p)
    done = [(i + 1, f) for i, f in enumerate(fu) if f.get("done")]
    if not done:
        return None
    n, f = done[-1]
    return {"n": n, "at": f.get("at"), "by": f.get("by", ""), "note": f.get("note", "")}


@app.template_filter("day")
def fmt_day(value):
    if not value:
        return "—"
    try:
        return to_local(datetime.fromisoformat(value)).strftime("%b %d, %Y")
    except ValueError:
        return value


def biz():
    """Name shown to clients and on documents: the company the page or packet belongs to."""
    c = g.get("company")
    return c["name"] if c else PLATFORM_NAME


def cid():
    """The signed-in user's company id."""
    return g.company["id"]


def get_company(company_id):
    row = db().execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
    return dict(row) if row else None


@app.context_processor
def inject_globals():
    return {"business_name": biz(), "platform_name": PLATFORM_NAME, "csrf_token": csrf_token}


# ---------------------------------------------------------------- auth + csrf
def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


@app.before_request
def check_csrf():
    if request.method == "POST" and not request.path.startswith("/api/admin/"):  # admin API uses its own token  # /u/ = one-click unsubscribe from email apps
        sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token", "")
        if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Your session expired. Reload the page and try again.")


# ---------------------------------------------------------------- users & roles
# Two kinds of sign-in:
#   * staff      – create, send and track packets; can view the form library but not change it
#   * manager    – everything staff can do, plus add/edit/remove library forms and manage team logins
# Every login belongs to one company and only ever sees that company's forms, packets and team.
# The ADMIN_PASSWORD environment variable is the platform owner login (username "admin"): it manages
# companies on the Companies screen and can open any company with management rights to help it.
ROLE_LABELS = {"staff": "Staff", "manager": "Management"}
OWNER = {"id": 0, "name": "Platform owner", "email": "admin", "role": "manager", "active": 1,
         "must_change_password": 0}
_failed_logins = {}  # ip -> [timestamps]; slows down password guessing


@app.before_request
def load_user():
    g.user = None
    g.company = None
    uid = session.get("uid")
    if uid is None:
        return
    if uid == 0:
        if ADMIN_PASSWORD:
            g.user = OWNER
            if session.get("as_company"):  # the owner has opened one company to help it
                g.company = get_company(session["as_company"])
        return
    row = db().execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
    company = get_company(row["company_id"]) if row else None
    if row and company and company["status"] == "active" and session.get("pwv") == row["password_hash"][-12:]:
        g.user = dict(row)
        g.company = company
    else:
        session.pop("uid", None)  # deactivated, company suspended, or password changed elsewhere


def is_owner():
    return bool(g.get("user")) and g.user["id"] == 0


def is_manager():
    return bool(g.get("user")) and g.user["role"] == "manager"


@app.context_processor
def inject_user():
    return {"current_user": g.get("user"), "is_manager": is_manager(), "ROLE_LABELS": ROLE_LABELS,
            "in_company": bool(g.get("company"))}


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not g.get("user"):
            return redirect(url_for("login", next=request.path))
        if not g.get("company"):  # platform owner who hasn't opened a company
            return redirect(url_for("platform"))
        if g.user.get("must_change_password") and request.endpoint not in ("account", "logout"):
            flash("Choose your own password before continuing.", "error")
            return redirect(url_for("account"))
        return view(*args, **kwargs)
    return wrapper


def manager_required(view):
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not is_manager():
            return render_template("error.html", title="Management access only",
                                   message="Only management can change the form library or team logins. "
                                           "Ask a manager if a form needs adding or removing."), 403
        return view(*args, **kwargs)
    return wrapper


def owner_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not is_owner():
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def actor():
    return g.user["name"] if g.get("user") else ""


# ---------------------------------------------------------------- who sees which packets
# Management and the owner see every packet. Staff see only the packets they created.
def packet_scope():
    """SQL condition (and its arguments) limiting packets to the ones the signed-in user may see."""
    if is_manager():
        return "company_id = ?", [cid()]
    return "company_id = ? AND created_by_id = ?", [cid(), g.user["id"]]


@app.before_request
def guard_packet_access():
    """Every page about one packet (/packets/<pid>/...) checks the packet belongs to this company,
    and for staff that it is theirs."""
    pid = (request.view_args or {}).get("pid")
    if pid is None or not g.get("user") or not g.get("company"):
        return
    cond, args = packet_scope()
    if not db().execute(f"SELECT 1 FROM packets WHERE id=? AND {cond}", [pid] + args).fetchone():
        abort(404)


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        ip = client_ip()
        recent = [t for t in _failed_logins.get(ip, []) if t > datetime.now(timezone.utc).timestamp() - 900]
        _failed_logins[ip] = recent
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        user = None
        suspended = False
        if len(recent) >= 8:
            error = "Too many attempts. Wait 15 minutes and try again."
        elif email.lower() == "admin":
            if ADMIN_PASSWORD and hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
                user = OWNER
        else:
            row = db().execute("SELECT * FROM users WHERE email=? AND active=1", (email,)).fetchone()
            if row and check_password_hash(row["password_hash"], password):
                company = get_company(row["company_id"])
                if company and company["status"] == "active":
                    user = dict(row)
                else:
                    suspended = True
                    error = extras.paused_message(company, PLATFORM_NAME)
        if user:
            session.clear()
            session["uid"] = user["id"]
            if user["id"]:
                session["pwv"] = user["password_hash"][-12:]
                db().execute("UPDATE users SET last_login_at=? WHERE id=?", (now(), user["id"]))
                db().commit()
            session.permanent = True
            _failed_logins.pop(ip, None)
            nxt = request.args.get("next", "")
            if not user["id"]:
                return redirect(url_for("platform"))
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
        if not error and not suspended:
            _failed_logins[ip].append(datetime.now(timezone.utc).timestamp())
            error = "That email and password don't match an active account."
    return render_template("login.html", error=error, email=request.form.get("email", ""), allow_signup=ALLOW_SIGNUP)


_signups = {}  # ip -> [timestamps]; stops one visitor creating many companies


@app.route("/signup", methods=["GET", "POST"])
def signup():
    """A new company creates its own account; the person signing up becomes its first manager."""
    if not ALLOW_SIGNUP:
        abort(404)
    if g.get("user"):
        return redirect(url_for("dashboard"))
    form = request.form
    if request.method == "POST":
        ip = client_ip()
        recent = [t for t in _signups.get(ip, []) if t > datetime.now(timezone.utc).timestamp() - 3600]
        company = form.get("company", "").strip()[:100]
        name = form.get("name", "").strip()[:80]
        email = form.get("email", "").strip()[:120]
        pw = form.get("password", "")
        problem = None
        if len(recent) >= 5:
            problem = "Too many new accounts from this connection. Try again in an hour."
        elif not company or not name or "@" not in email:
            problem = "Add your company name, your name and a valid email address."
        elif db().execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            problem = "That email already has a login. Sign in instead, or use a different email."
        elif pw != form.get("confirm", ""):
            problem = "The passwords don't match."
        else:
            problem = valid_password(pw)
        if problem:
            flash(problem, "error")
        else:
            new_cid = db().execute("INSERT INTO companies (name, notify_email, storage_gb, source, created_at) "
                                   "VALUES (?,?,?,?,?)", (company, email, INCLUDED_STORAGE_GB, "signup", now())).lastrowid
            extras.start_trial(db(), new_cid)  # self sign-up starts a 14-day demo that locks on its own
            h = generate_password_hash(pw)
            uid = db().execute("INSERT INTO users (name, email, password_hash, role, must_change_password, created_at, "
                               "company_id) VALUES (?,?,?,?,0,?,?)", (name, email, h, "manager", now(), new_cid)).lastrowid
            db().commit()
            _signups[ip] = recent + [datetime.now(timezone.utc).timestamp()]
            session.clear()
            session["uid"], session["pwv"] = uid, h[-12:]
            session.permanent = True
            flash(f"Welcome to {PLATFORM_NAME}. Your {extras.TRIAL_DAYS}-day demo has started. Add your forms "
                  f"to the library, then add your team under Team.", "ok")
            return redirect(url_for("library"))
    return render_template("signup.html", form=form)


def valid_password(pw):
    if len(pw) < 8:
        return "Use at least 8 characters."
    return None


@app.route("/account", methods=["GET", "POST"])
def account():
    if not g.get("user"):
        return redirect(url_for("login"))
    if g.user["id"] == 0:
        flash("The platform owner's password is the ADMIN_PASSWORD setting on your server.", "ok")
        return redirect(url_for("platform"))
    if request.method == "POST":
        current, new, confirm = (request.form.get(k, "") for k in ("current", "new", "confirm"))
        problem = None
        if not check_password_hash(g.user["password_hash"], current):
            problem = "Your current password is incorrect."
        elif new != confirm:
            problem = "The new passwords don't match."
        else:
            problem = valid_password(new)
        if problem:
            flash(problem, "error")
        else:
            h = generate_password_hash(new)
            db().execute("UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?", (h, g.user["id"]))
            db().commit()
            session["pwv"] = h[-12:]
            flash("Password changed.", "ok")
            return redirect(url_for("dashboard"))
    return render_template("account.html")


@app.route("/team", methods=["GET", "POST"])
@manager_required
def team():
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:80]
        email = request.form.get("email", "").strip()[:120]
        role = request.form.get("role", "staff")
        pw = request.form.get("password", "")
        problem = None
        if not name or "@" not in email:
            problem = "Add a name and a valid email address."
        elif role not in ROLE_LABELS:
            problem = "Choose Staff or Management."
        elif db().execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            problem = "That email already has a login. Each person needs their own email address."
        else:
            problem = valid_password(pw)
        if problem:
            flash(problem, "error")
        else:
            db().execute("INSERT INTO users (name, email, password_hash, role, created_at, company_id) "
                         "VALUES (?,?,?,?,?,?)", (name, email, generate_password_hash(pw), role, now(), cid()))
            db().commit()
            flash(f"Login created for {name} ({ROLE_LABELS[role]}). Give them the temporary password; "
                  f"they'll choose their own when they first sign in.", "ok")
            return redirect(url_for("team"))
    users = db().execute("SELECT u.*, (SELECT COUNT(*) FROM packets p WHERE p.created_by_id = u.id) AS packet_count "
                         "FROM users u WHERE u.company_id=? ORDER BY active DESC, role DESC, name COLLATE NOCASE", (cid(),)).fetchall()
    return render_template("team.html", users=users, form=request.form)


@app.route("/team/<int:uid>", methods=["POST"])
@manager_required
def team_update(uid):
    u = db().execute("SELECT * FROM users WHERE id=? AND company_id=?", (uid, cid())).fetchone() or abort(404)
    action = request.form.get("action")
    if action == "role" and request.form.get("role") in ROLE_LABELS:
        if uid == g.user["id"] and request.form["role"] != "manager":
            flash("You can't remove your own management access. Ask another manager.", "error")
        else:
            db().execute("UPDATE users SET role=? WHERE id=?", (request.form["role"], uid))
            flash(f"{u['name']} is now {ROLE_LABELS[request.form['role']]}.", "ok")
    elif action == "password":
        pw = request.form.get("password", "")
        problem = valid_password(pw)
        if problem:
            flash(problem, "error")
        else:
            db().execute("UPDATE users SET password_hash=?, must_change_password=1 WHERE id=?",
                         (generate_password_hash(pw), uid))
            flash(f"Temporary password set for {u['name']}. They'll choose a new one at next sign-in.", "ok")
    elif action in ("deactivate", "activate"):
        if uid == g.user["id"]:
            flash("You can't deactivate your own login.", "error")
        else:
            db().execute("UPDATE users SET active=? WHERE id=?", (1 if action == "activate" else 0, uid))
            flash(f"{u['name']} {'can sign in again' if action == 'activate' else 'can no longer sign in'}.", "ok")
    elif action == "remove":
        if uid == g.user["id"]:
            flash("You can't remove your own login. Ask another manager.", "error")
        else:
            # Their packets are never deleted: they move to another login, or to management only.
            to = request.form.get("transfer_to", "0")
            new_owner = db().execute("SELECT id, name FROM users WHERE id=? AND id != ? AND company_id=?",
                                     (to, uid, cid())).fetchone() \
                if to.isdigit() and to != "0" else None
            n = db().execute("UPDATE packets SET created_by_id=? WHERE created_by_id=?",
                             (new_owner["id"] if new_owner else 0, uid)).rowcount
            db().execute("DELETE FROM users WHERE id=?", (uid,))
            where = f"moved to {new_owner['name']}" if new_owner else "kept, visible to management"
            flash(f"{u['name']}'s login was removed." + (f" Their {n} packet{'s' if n != 1 else ''} {'were' if n != 1 else 'was'} {where}." if n else ""), "ok")
    db().commit()
    return redirect(url_for("team"))


# ---------------------------------------------------------------- company settings & storage
def company_storage_bytes(company_id):
    """Disk space used by one company's library files, packet files and signed copies."""
    files = set()
    for (f,) in db().execute("SELECT file_path FROM templates WHERE company_id=? AND file_path IS NOT NULL",
                             (company_id,)):
        files.add((UPLOAD_DIR, f))
    for r in db().execute("SELECT i.template_file, i.signed_pdf_path, i.signature_path, i.config_json "
                          "FROM packet_items i JOIN packets p ON p.id = i.packet_id WHERE p.company_id=?",
                          (company_id,)):
        if r["template_file"]:
            files.add((UPLOAD_DIR, r["template_file"]))
        for f in (r["signed_pdf_path"], r["signature_path"]):
            if f:
                files.add((SIGNED_DIR, f))
        try:
            for o in json.loads(r["config_json"] or "{}").get("options", []):
                for f in (o.get("file"), o.get("display")):
                    if f:
                        files.add((UPLOAD_DIR, f))
        except (ValueError, AttributeError):
            pass
    total = client_upload_bytes(company_id)   # photos and files clients sent
    for folder, f in files:
        try:
            total += os.path.getsize(os.path.join(folder, os.path.basename(f)))
        except OSError:
            pass
    return total


@app.template_filter("gb")
def fmt_gb(n):
    gb = (n or 0) / 1024 ** 3
    return f"{gb:.2f} GB" if gb >= 0.1 else f"{(n or 0) / 1024 ** 2:.1f} MB"


@app.route("/company", methods=["GET", "POST"])
@manager_required
def company_settings():
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:100]
        if not name:
            flash("Your company name can't be empty. Clients see it on every packet.", "error")
        else:
            db().execute("UPDATE companies SET name=? WHERE id=?", (name, cid()))
            db().commit()
            flash("Company details saved.", "ok")
            return redirect(url_for("company_settings"))
    used = company_storage_bytes(cid())
    users = db().execute("SELECT COUNT(*) FROM users WHERE company_id=? AND active=1", (cid(),)).fetchone()[0]
    return render_template("company.html", c=g.company, used=used, users=users,
                           limit=g.company["storage_gb"] * 1024 ** 3)


@app.route("/platform", methods=["GET", "POST"])
@owner_required
def platform():
    """Platform owner: every company using this server."""
    form = request.form
    if request.method == "POST":
        company = form.get("company", "").strip()[:100]
        name = form.get("name", "").strip()[:80]
        email = form.get("email", "").strip()[:120]
        pw = form.get("password", "")
        gb = form.get("storage_gb", str(INCLUDED_STORAGE_GB))
        problem = None
        if not company or not name or "@" not in email:
            problem = "Add the company name and its first manager's name and email."
        elif not gb.isdigit() or not 1 <= int(gb) <= 100000:
            problem = "Storage must be a whole number of GB."
        elif db().execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            problem = "That email already has a login in another company."
        else:
            problem = valid_password(pw)
        if problem:
            flash(problem, "error")
        else:
            new_cid = db().execute("INSERT INTO companies (name, notify_email, storage_gb, source, created_at) "
                                   "VALUES (?,?,?,?,?)", (company, email, int(gb), "owner", now())).lastrowid
            db().execute("INSERT INTO users (name, email, password_hash, role, created_at, company_id) "
                         "VALUES (?,?,?,?,?,?)", (name, email, generate_password_hash(pw), "manager", now(), new_cid))
            if form.get("demo") == "on":
                extras.start_trial(db(), new_cid)   # locks on its own after 14 days
            db().commit()
            flash(f"{company} is set up. Send {name} the sign-in address, their email and the temporary password; "
                  f"they'll choose their own password and can then add their team.", "ok")
            return redirect(url_for("platform"))
    rows = db().execute(
        "SELECT c.*, (SELECT COUNT(*) FROM users u WHERE u.company_id=c.id AND u.active=1) AS user_count, "
        "(SELECT COUNT(*) FROM users u WHERE u.company_id=c.id AND u.active=1 AND u.role='manager') AS manager_count, "
        "(SELECT COUNT(*) FROM packets p WHERE p.company_id=c.id) AS packet_count, "
        "(SELECT MAX(COALESCE(p.sent_at, p.created_at)) FROM packets p WHERE p.company_id=c.id) AS last_packet, "
        "(SELECT MAX(u.last_login_at) FROM users u WHERE u.company_id=c.id) AS last_login "
        "FROM companies c ORDER BY c.status, c.name COLLATE NOCASE").fetchall()
    companies = [{**dict(r), "used": company_storage_bytes(r["id"])} for r in rows]
    return render_template("platform.html", companies=companies, form=form, allow_signup=ALLOW_SIGNUP,
                           default_gb=INCLUDED_STORAGE_GB)


@app.route("/platform/<int:company_id>", methods=["POST"])
@owner_required
def platform_update(company_id):
    c = get_company(company_id) or abort(404)
    action = request.form.get("action")
    if action == "open":
        session["as_company"] = company_id
        flash(f"You're now in {c['name']} with management access. Use “Back to all companies” when you're done.", "ok")
        return redirect(url_for("dashboard"))
    plan_msg = extras.owner_action(db(), company_id, action, request.form.get("days"))
    if action in ("mark_paid", "extend_trial"):
        db().commit()
        flash(plan_msg, "ok")
        return redirect(url_for("platform"))
    if action in ("suspend", "activate"):
        db().execute("UPDATE companies SET status=? WHERE id=?",
                     ("suspended" if action == "suspend" else "active", company_id))
        flash(plan_msg, "ok") if plan_msg else flash(f"{c['name']} is {'paused: its team can no longer sign in. Signing links already sent still work' if action == 'suspend' else 'active again: its team can sign in'}.", "ok")
    elif action == "storage":
        gb = request.form.get("storage_gb", "")
        if gb.isdigit() and 1 <= int(gb) <= 100000:
            db().execute("UPDATE companies SET storage_gb=? WHERE id=?", (int(gb), company_id))
            flash(f"{c['name']} now has {gb} GB of storage.", "ok")
        else:
            flash("Storage must be a whole number of GB.", "error")
    db().commit()
    return redirect(url_for("platform"))


@app.route("/platform/exit", methods=["POST"])
@owner_required
def platform_exit():
    session.pop("as_company", None)
    return redirect(url_for("platform"))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- packet status
def packet_progress(packet_id):
    row = db().execute(
        "SELECT COUNT(*) total, SUM(signed_at IS NOT NULL) signed FROM packet_items "
        "WHERE packet_id=? AND requires_signature=1",
        (packet_id,)).fetchone()
    return row["signed"] or 0, row["total"] or 0


def effective_status(p):
    if p["voided_at"]:
        return "voided"
    if p["completed_at"]:
        return "completed"
    if p["expires_at"] and p["expires_at"] < now() and p["status"] != "draft":
        return "expired"
    return p["status"]


# ---------------------------------------------------------------- admin: dashboard
def creators():
    """(id, name) of everyone who has created packets, for the management filter."""
    return [(r[0], r[1] or "Owner") for r in db().execute(
        "SELECT created_by_id, MAX(created_by) FROM packets WHERE created_by_id IS NOT NULL AND company_id=? "
        "GROUP BY created_by_id ORDER BY MAX(created_by) COLLATE NOCASE", (cid(),))]


def still_needed(pid):
    """Names of the forms the client still has to sign or approve."""
    return [r[0] for r in db().execute(
        "SELECT template_name FROM packet_items WHERE packet_id=? AND requires_signature=1 AND signed_at IS NULL "
        "ORDER BY position", (pid,))]


@app.route("/")
@login_required
def dashboard():
    q = request.args.get("q", "").strip()[:100]
    by_filter = request.args.get("by", "").strip()
    by_filter = int(by_filter) if by_filter.lstrip("-").isdigit() and is_manager() else None
    cond, args = packet_scope()
    sql = f"SELECT * FROM packets WHERE {cond}"
    if q:
        like = f"%{q.replace('%', '').replace('_', '')}%"
        sql += " AND (client_name LIKE ? OR client_email LIKE ?)"
        args += [like, like]
    if by_filter is not None:
        sql += " AND created_by_id = ?"
        args.append(by_filter)
    # Newest first, so the packets sent most recently are always at the top.
    rows = db().execute(sql + " ORDER BY COALESCE(sent_at, created_at) DESC, id DESC", args).fetchall()
    packets = []
    counts = {"awaiting": 0, "viewed": 0, "completed": 0, "overdue": 0}
    for p in rows:
        signed, total = packet_progress(p["id"])
        st = effective_status(p)
        if st == "sent":
            counts["awaiting"] += 1
        elif st in ("viewed", "in_progress"):
            counts["viewed"] += 1
        elif st == "completed":
            counts["completed"] += 1
        dl = deadline_info(p) if st in ("sent", "viewed", "in_progress") else {"due_at": None, "overdue": False, "label": ""}
        if dl["overdue"]:
            counts["overdue"] += 1
        packets.append({**dict(p), "signed": signed, "total": total, "state": st,
                        "deadline": dl, "last_fu": last_followup(p),
                        "missing": still_needed(p["id"]) if st not in ("completed", "voided") else []})
    filt = request.args.get("show", "all")
    if filt == "open":
        packets = [p for p in packets if p["state"] in ("sent", "viewed", "in_progress", "draft")]
    elif filt == "awaiting":   # sent, client hasn't opened it yet
        packets = [p for p in packets if p["state"] == "sent"]
    elif filt == "overdue":
        packets = [p for p in packets if p["deadline"]["overdue"]]
    elif filt == "completed":
        packets = [p for p in packets if p["state"] == "completed"]
    has_templates = db().execute("SELECT 1 FROM templates WHERE archived=0 AND company_id=? LIMIT 1",
                                 (cid(),)).fetchone()
    return render_template("dashboard.html", packets=packets, counts=counts, show=filt, sign_hours=SIGN_WITHIN_HOURS,
                           has_templates=bool(has_templates), q=q, by=by_filter, creators=creators() if is_manager() else [],
                           by_name=dict(creators()).get(by_filter, "") if by_filter is not None else "")


# ---------------------------------------------------------------- admin: form library
@app.route("/library")
@login_required
def library():
    rows = db().execute("SELECT * FROM templates WHERE archived=0 AND company_id=? ORDER BY name COLLATE NOCASE",
                         (cid(),)).fetchall()
    templates = [{**dict(t), "field_count": len(json.loads(t["fields_json"] or "[]"))} for t in rows]
    return render_template("library.html", templates=templates)


def save_upload(f):
    """Store any uploaded file. Returns dict with stored name, original name, mime, sha256 and kind
    ('pdf' for a readable PDF we can stamp, 'file' for everything else)."""
    original = os.path.basename(f.filename or "file")[:200] or "file"
    stored = f"{secrets.token_hex(8)}-{secure_filename(original) or 'file'}"
    path = os.path.join(UPLOAD_DIR, stored)
    f.save(path)
    if os.path.getsize(path) == 0:
        os.remove(path)
        raise ValueError(f"“{original}” is empty.")
    with open(path, "rb") as fh:
        head = fh.read(5)
    kind = "pdf" if head == b"%PDF-" and pdfgen.is_readable_pdf(path) else "file"
    mime = "application/pdf" if head == b"%PDF-" else (mimetypes.guess_type(original)[0] or f.mimetype
                                                       or "application/octet-stream")
    if kind == "file":
        try:  # prepare the on-page preview now so the client's page opens quickly
            preview.ensure_preview(UPLOAD_DIR, stored, original)
        except Exception:  # noqa: BLE001
            app.logger.exception("Preview failed for %s", original)
    return {"stored": stored, "name": original, "mime": mime, "sha256": sha256_file(path), "kind": kind}


PAGE_DIR = os.path.join(DATA_DIR, "pages")
os.makedirs(PAGE_DIR, exist_ok=True)


def item_viewer(it):
    """How to show a packet item's document on the signing page."""
    kind, stored = it["template_kind"], it["template_file"]
    if not stored:
        return {"mode": "none"}
    pdf = None
    if kind == "pdf":
        pdf = stored
    elif kind == "file":
        if (it["file_mime"] or "") in IMAGE_MIMES:
            return {"mode": "image"}
        try:
            pv = preview.ensure_preview(UPLOAD_DIR, stored, it["file_name"] or stored)
        except Exception:  # noqa: BLE001
            app.logger.exception("Preview failed for item %s", it["id"])
            pv = None
        if pv and pv["type"] == "html":
            with open(os.path.join(UPLOAD_DIR, pv["file"]), encoding="utf-8") as fh:
                return {"mode": "html", "html": fh.read()}
        if pv:
            pdf = pv["file"]
    if not pdf:
        return {"mode": "download"}
    try:
        count = preview.page_count(os.path.join(UPLOAD_DIR, pdf))
    except Exception:  # noqa: BLE001
        app.logger.exception("Could not read pages of %s", pdf)
        return {"mode": "download"}
    return {"mode": "pages", "pdf": pdf, "count": min(count, 300)}


IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


@app.template_filter("fromjson")
def _fromjson(v):
    try:
        return json.loads(v) if v else {}
    except ValueError:
        return {}


INLINE_MIMES = {"application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp"}


def send_stored(stored, mime, download_name, force_download=False):
    """Serve an uploaded file. Only PDFs and common images are shown in the browser; everything
    else is sent as a download so it can never run as a web page."""
    inline = mime in INLINE_MIMES and not force_download
    return send_file(os.path.join(UPLOAD_DIR, stored), mimetype=mime if inline else "application/octet-stream",
                     as_attachment=not inline, download_name=download_name)


@app.template_filter("filetype")
def filetype_label(name, mime=""):
    ext = os.path.splitext(name or "")[1].lower().lstrip(".")
    labels = {"pdf": "PDF", "doc": "Word document", "docx": "Word document", "xls": "Excel spreadsheet",
              "xlsx": "Excel spreadsheet", "csv": "Spreadsheet (CSV)", "ppt": "PowerPoint", "pptx": "PowerPoint",
              "jpg": "Image", "jpeg": "Image", "png": "Image", "gif": "Image", "webp": "Image", "heic": "Image",
              "txt": "Text file", "zip": "ZIP archive", "dwg": "CAD drawing", "dxf": "CAD drawing",
              "rtf": "Text document", "odt": "Document", "pages": "Pages document", "numbers": "Numbers spreadsheet"}
    return labels.get(ext, (ext.upper() + " file") if ext else "File")


@app.route("/library/upload", methods=["POST"])
@manager_required
def upload_pdf():
    f = request.files.get("file")
    name = request.form.get("name", "").strip()
    if not f or not f.filename:
        flash("Choose a file to upload.", "error")
        return redirect(url_for("library"))
    try:
        up = save_upload(f)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("library"))
    fields = parse_fields_from_request()
    new_tid = db().execute(
        "INSERT INTO templates (name, kind, description, file_path, file_sha256, fields_json, created_at, "
        "file_name, file_mime, company_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (name or os.path.splitext(up["name"])[0], up["kind"], request.form.get("description", "").strip(),
         up["stored"], up["sha256"], json.dumps(fields), now(), up["name"], up["mime"], cid())).lastrowid
    db().commit()
    warm_template(new_tid)   # prepare the preview in the background
    flash("Added to your library.", "ok")
    return redirect(url_for("library"))


def parse_fields_from_request():
    raw = request.form.get("fields_json", "[]")
    try:
        items = json.loads(raw)
    except ValueError:
        return []
    fields = []
    for i, it in enumerate(items if isinstance(items, list) else []):
        label = str(it.get("label", "")).strip()[:200]
        ftype = it.get("type", "text")
        if not label or ftype not in FIELD_TYPES:
            continue
        fields.append({"id": f"f{i+1}", "label": label, "type": ftype,
                       "required": bool(it.get("required")),
                       "help": str(it.get("help", "")).strip()[:300]})
    return fields


@app.route("/library/new", methods=["GET", "POST"])
@manager_required
def new_form():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        fields = parse_fields_from_request()
        if not name or not fields:
            flash("Give the form a name and at least one field.", "error")
            return render_template("form_builder.html", form=None,
                                   fields_json=request.form.get("fields_json", "[]"),
                                   name=name, body=request.form.get("body", ""))
        db().execute(
            "INSERT INTO templates (name, kind, description, fields_json, created_at, company_id) VALUES (?,?,?,?,?,?)",
            (name, "form", request.form.get("body", "").strip(), json.dumps(fields), now(), cid()))
        db().commit()
        flash("Form created.", "ok")
        return redirect(url_for("library"))
    return render_template("form_builder.html", form=None, fields_json="[]", name="", body="")


@app.route("/library/<int:tid>/edit", methods=["GET", "POST"])
@manager_required
def edit_form(tid):
    t = db().execute("SELECT * FROM templates WHERE id=? AND archived=0 AND company_id=?",
                     (tid, cid())).fetchone() or abort(404)
    if request.method == "POST":
        name = request.form.get("name", "").strip() or t["name"]
        fields = parse_fields_from_request()
        if t["kind"] == "form" and not fields:
            flash("A fillable form needs at least one field.", "error")
            return redirect(url_for("edit_form", tid=tid))
        db().execute("UPDATE templates SET name=?, description=?, fields_json=? WHERE id=? AND company_id=?",
                     (name, request.form.get("body", "").strip(), json.dumps(fields), tid, cid()))
        db().commit()
        flash("Saved. Packets already sent keep the version they were sent with.", "ok")
        return redirect(url_for("library"))
    return render_template("form_builder.html", form=t, fields_json=t["fields_json"],
                           name=t["name"], body=t["description"])


@app.route("/library/<int:tid>/archive", methods=["POST"])
@manager_required
def archive_form(tid):
    db().execute("UPDATE templates SET archived=1 WHERE id=? AND company_id=?", (tid, cid()))
    db().commit()
    flash("Form removed from the library. Past packets are unaffected.", "ok")
    return redirect(url_for("library"))


@app.route("/library/<int:tid>/file")
@login_required
def template_file(tid):
    t = db().execute("SELECT * FROM templates WHERE id=? AND company_id=?", (tid, cid())).fetchone() or abort(404)
    if not t["file_path"]:
        abort(404)
    return send_stored(t["file_path"], t["file_mime"] or "application/pdf", t["file_name"] or f"{t['name']}.pdf")


# ---------------------------------------------------------------- admin: packets
@app.route("/packets/new", methods=["GET", "POST"])
@login_required
def new_packet():
    templates = db().execute("SELECT * FROM templates WHERE archived=0 AND company_id=? ORDER BY name COLLATE NOCASE",
                         (cid(),)).fetchall()
    if request.method == "POST":
        name = request.form.get("client_name", "").strip()
        email = request.form.get("client_email", "").strip()
        ids = [int(x) for x in request.form.getlist("template_ids") if x.isdigit()]
        ids = [i for i in ids if i in {t["id"] for t in templates}]  # only this company's forms
        extra_problem = validate_packet_extras(request.form)
        if extra_problem:
            flash(extra_problem, "error")
            return render_template("packet_new.html", templates=templates, form=request.form, selected=ids)
        adhoc = [f for f in request.files.getlist("adhoc_files") if f and f.filename]
        in_person = request.form.get("action") == "in_person"
        email_ok = not email or "@" in email   # optional, kept for the record
        if not name or not email_ok or not (ids or adhoc):
            flash("Add the client's name and at least one form or file (and check the email address, if you entered one).", "error")
            return render_template("packet_new.html", templates=templates, form=request.form,
                                   selected=ids)
        uploads = []
        try:
            for i, f in enumerate(adhoc):
                up = save_upload(f)
                up["sign"] = request.form.get(f"adhoc_sign_{i}") == "on"
                uploads.append(up)
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template("packet_new.html", templates=templates, form=request.form, selected=ids)
        token = secrets.token_urlsafe(24)
        days = int(request.form.get("expiry_days") or LINK_DAYS)
        expires = (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()
        cur = db().execute(
            "INSERT INTO packets (token, client_name, client_email, message, status, created_at, expires_at, "
            "created_by, created_by_id, company_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (token, name, email, request.form.get("message", "").strip(), "draft", now(), expires,
             actor(), g.user["id"], cid()))
        pid = cur.lastrowid
        by_id = {t["id"]: t for t in templates}
        for pos, tid in enumerate(ids):
            t = by_id.get(tid)
            if not t:
                continue
            # Snapshot the template so later edits never change what the client signed.
            db().execute(
                "INSERT INTO packet_items (packet_id, template_id, position, template_name, template_kind, "
                "template_file, template_sha256, fields_json, template_body, file_name, file_mime) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (pid, tid, pos, t["name"], t["kind"], t["file_path"], t["file_sha256"], t["fields_json"],
                 t["description"] if t["kind"] == "form" else "", t["file_name"], t["file_mime"]))
        # One-off files: attached to this packet only, never added to the library.
        for k, up in enumerate(uploads):
            db().execute(
                "INSERT INTO packet_items (packet_id, template_id, position, template_name, template_kind, "
                "template_file, template_sha256, fields_json, file_name, file_mime, requires_signature, adhoc) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
                (pid, None, len(ids) + k, os.path.splitext(up["name"])[0][:120] or up["name"], up["kind"],
                 up["stored"], up["sha256"], "[]", up["name"], up["mime"], 1 if up["sign"] else 0))
        parts = [f"{len(ids)} form(s) from library"] if ids else []
        if uploads:
            parts.append(f"{len(uploads)} one-off file(s)")
        log_event(pid, "created", ", ".join(parts) + (f" · by {actor()}" if actor() else ""))
        after_packet_created(pid)   # second owner, file request, signature boxes
        daily_packet_created(pid)   # access code
        db().commit()
        warm_packet(pid)   # prepare previews in the background
        if in_person:
            return start_in_person(pid)
        mark_sent(pid, "link")
        flash("Packet ready. Copy the signing link below and send it to your client.", "ok")
        return redirect(url_for("packet_detail", pid=pid))
    pre = [int(x) for x in request.args.getlist("t") if x.isdigit()]
    return render_template("packet_new.html", templates=templates, form=packet_form_defaults(),
                           selected=bundle_selection(pre))


def mark_sent(pid, how):
    p = db().execute("SELECT * FROM packets WHERE id=?", (pid,)).fetchone()
    if p["status"] == "draft":
        db().execute("UPDATE packets SET status='sent', sent_at=? WHERE id=?", (now(), pid))
    log_event(pid, "sent", "by email" if how == "email" else "link created")
    db().commit()


@app.route("/packets/<int:pid>")
@login_required
def packet_detail(pid):
    p = db().execute("SELECT * FROM packets WHERE id=?", (pid,)).fetchone() or abort(404)
    items = db().execute("SELECT * FROM packet_items WHERE packet_id=? ORDER BY position", (pid,)).fetchall()
    events = db().execute("SELECT * FROM events WHERE packet_id=? ORDER BY at DESC, id DESC", (pid,)).fetchall()
    signed, total = packet_progress(pid)
    return render_template("packet_detail.html", p=p, items=items, events=events, signed=signed,
                           deadline=deadline_info(p), followups=followups_of(p), sign_hours=SIGN_WITHIN_HOURS,
                           total=total, state=effective_status(p), link=signing_url(p["token"]))


@app.route("/packets/<int:pid>/followups", methods=["POST"])
@login_required
def save_followups(pid):
    p = db().execute("SELECT * FROM packets WHERE id=?", (pid,)).fetchone() or abort(404)
    old = followups_of(p)
    new, changes = [], []
    for i in range(FOLLOWUP_SLOTS):
        done = request.form.get(f"fu_done_{i}") == "on"
        note = request.form.get(f"fu_note_{i}", "").strip()[:500]
        o = old[i]
        if done and not o.get("done"):
            entry = {"done": True, "at": now(), "by": actor(), "note": note}
            changes.append(f"follow-up {i + 1} recorded" + (f": {note}" if note else ""))
        elif done:
            entry = dict(o, note=note)
            if note != o.get("note"):
                changes.append(f"follow-up {i + 1} note updated")
        else:
            entry = {"done": False, "at": None, "by": "", "note": note}
            if o.get("done"):
                changes.append(f"follow-up {i + 1} unticked")
        new.append(entry)
    notes = request.form.get("notes", "").strip()[:5000]
    if notes != (p["notes"] or ""):
        changes.append("notes updated")
    db().execute("UPDATE packets SET followups_json=?, notes=? WHERE id=?", (json.dumps(new), notes, pid))
    for c in changes[:6]:
        log_event(pid, "followup", f"{c[:180]} · by {actor()}")
    db().commit()
    flash("Follow-ups saved." if changes else "No changes to save.", "ok")
    return redirect(url_for("packet_detail", pid=pid) + "#followups")


@app.route("/packets/<int:pid>/extend", methods=["POST"])
@login_required
def extend_packet(pid):
    exp = (datetime.now(timezone.utc) + timedelta(days=LINK_DAYS)).replace(microsecond=0).isoformat()
    db().execute("UPDATE packets SET expires_at=? WHERE id=?", (exp, pid))
    log_event(pid, "extended", f"link valid until {exp[:10]} · by {actor()}")
    db().commit()
    flash(f"Link extended by {LINK_DAYS} days.", "ok")
    return redirect(url_for("packet_detail", pid=pid))


@app.route("/packets/<int:pid>/void", methods=["POST"])
@login_required
def void_packet(pid):
    db().execute("UPDATE packets SET voided_at=? WHERE id=? AND completed_at IS NULL", (now(), pid))
    log_event(pid, "voided", f"by {actor()}")
    db().commit()
    flash("Packet cancelled. The signing link no longer works.", "ok")
    return redirect(url_for("packet_detail", pid=pid))


@app.route("/packets/<int:pid>/items/<int:iid>/signed.pdf")
@login_required
def admin_signed_pdf(pid, iid):
    it = db().execute("SELECT * FROM packet_items WHERE id=? AND packet_id=?", (iid, pid)).fetchone() or abort(404)
    if not it["signed_pdf_path"]:
        abort(404)
    return send_file(os.path.join(SIGNED_DIR, it["signed_pdf_path"]), mimetype="application/pdf",
                     download_name=f"{secure_filename(it['template_name'])}-signed.pdf")


@app.route("/packets/<int:pid>/items/<int:iid>/original")
@login_required
def admin_original(pid, iid):
    it = db().execute("SELECT * FROM packet_items WHERE id=? AND packet_id=?", (iid, pid)).fetchone() or abort(404)
    if not it["template_file"]:
        abort(404)
    return send_stored(it["template_file"], it["file_mime"] or "application/pdf",
                       it["file_name"] or f"{it['template_name']}.pdf", force_download=True)


@app.route("/packets/<int:pid>/audit.pdf")
@login_required
def admin_audit_pdf(pid):
    p = db().execute("SELECT * FROM packets WHERE id=?", (pid,)).fetchone() or abort(404)
    items = db().execute("SELECT * FROM packet_items WHERE packet_id=? ORDER BY position", (pid,)).fetchall()
    events = db().execute("SELECT * FROM events WHERE packet_id=? ORDER BY at, id", (pid,)).fetchall()
    path = os.path.join(SIGNED_DIR, f"audit-{pid}-{secrets.token_hex(4)}.pdf")
    pdfgen.audit_trail(path, biz(), dict(p), [cosign_names(dict(i)) for i in items], [dict(e) for e in events])
    seal.seal_pdf(path, DATA_DIR, biz(), reason=f"Audit trail · {packet_ref(p)}", location=PLATFORM_NAME)
    return send_file(path, mimetype="application/pdf", download_name=f"audit-trail-{pid}.pdf")


# ---------------------------------------------------------------- client side
def load_packet(token):
    p = db().execute("SELECT * FROM packets WHERE token=?", (token,)).fetchone()
    if not p:
        abort(404)
    g.company = get_company(p["company_id"])
    return p


def client_block(p):
    """Return a template response if the link can't be used, else None."""
    st = effective_status(p)
    if st == "voided":
        return render_template("client_closed.html", p=p, reason="cancelled")
    if st == "expired":
        return render_template("client_closed.html", p=p, reason="expired")
    return None


@app.route("/s/<token>")
def client_packet(token):
    p = load_packet(token)
    blocked = client_block(p)
    if blocked:
        return blocked
    if not p["first_viewed_at"]:
        db().execute("UPDATE packets SET first_viewed_at=?, status=CASE WHEN status IN ('sent','draft') "
                     "THEN 'viewed' ELSE status END WHERE id=?", (now(), p["id"]))
        log_event(p["id"], "viewed", request.headers.get("User-Agent", "")[:200])
        db().commit()
        p = load_packet(token)
    items = db().execute("SELECT * FROM packet_items WHERE packet_id=? ORDER BY position", (p["id"],)).fetchall()
    signed, total = packet_progress(p["id"])
    if p["completed_at"]:
        return redirect(url_for("client_done", token=token))
    sign_items = [i for i in items if i["requires_signature"]]
    info_items = [i for i in items if not i["requires_signature"]]
    next_item = next((i for i in sign_items if not i["signed_at"]), None)
    return render_template("client_packet.html", p=p, items=sign_items, info_items=info_items, signed=signed,
                           deadline=deadline_info(p),
                           total=total, next_item=next_item)


@app.route("/s/<token>/confirm", methods=["POST"])
def client_confirm(token):
    """For packets that only contain files to review (nothing to sign): the client confirms receipt."""
    p = load_packet(token)
    blocked = client_block(p)
    if blocked:
        return blocked
    signed, total = packet_progress(p["id"])
    if total == 0 and not p["completed_at"]:
        log_event(p["id"], "confirmed", "client confirmed receipt of the files")
        db().commit()
        complete_packet(p["id"], reason="client confirmed receipt")
    return redirect(url_for("client_done", token=token))


def render_item_pdf(p, it, answers, signers, method=""):
    """Write the signed PDF of one packet item and seal it. `signers` is the client, plus the second owner when
    two owners sign: [{"name", "sig_path", "signed_at", "ip", "user_agent", "label", "email"}].
    Returns (file name, SHA-256 of the file)."""
    fields = json.loads(it["fields_json"] or "[]")
    body = it["template_body"] or ""
    first = signers[0]
    try:
        placements = json.loads(it["placements_json"] or "[]")
    except (ValueError, IndexError, KeyError):
        placements = []
    out_name = f"{p['id']}-{it['id']}-{secrets.token_hex(6)}.pdf"
    out_path = os.path.join(SIGNED_DIR, out_name)
    meta = {"business": biz(), "packet_ref": packet_ref(p), "client_name": p["client_name"],
            "client_email": p["client_email"], "signer_name": first["name"], "signed_at": first["signed_at"],
            "ip": first.get("ip"), "user_agent": first.get("user_agent"), "method": method,
            "verify_url": (BASE_URL or request.url_root.rstrip("/")) + "/verify",
            "form_name": it["template_name"], "original_sha256": it["template_sha256"],
            }
    if len(signers) > 1:
        meta["signers"] = signers
    source = (os.path.join(UPLOAD_DIR, it["template_file"])
              if it["template_kind"] == "pdf" else None)
    if it["template_kind"] == "file":
        v = item_viewer(it)
        if v["mode"] == "pages":
            source = os.path.join(UPLOAD_DIR, v["pdf"])  # signed copy shows the document's pages
    attachment = None
    if it["template_kind"] == "file":
        fpath = os.path.join(UPLOAD_DIR, it["template_file"])
        attachment = {"name": it["file_name"] or it["template_name"], "type": filetype_label(it["file_name"]),
                      "size": os.path.getsize(fpath), "sha256": it["template_sha256"],
                      "image": fpath if (it["file_mime"] or "").startswith("image/") else None}
    pdfgen.signed_document(out_path, source, body, fields, answers, first["sig_path"], meta,
                           placements=placements, local_day=fmt_day, attachment=attachment)
    seal.seal_pdf(out_path, DATA_DIR, biz(), reason=f"Signed electronically · {packet_ref(p)}", location=PLATFORM_NAME)
    return out_name, sha256_file(out_path)


@app.route("/s/<token>/f/<int:iid>", methods=["GET", "POST"])
def client_form(token, iid):
    p = load_packet(token)
    blocked = client_block(p)
    if blocked:
        return blocked
    it = db().execute("SELECT * FROM packet_items WHERE id=? AND packet_id=?", (iid, p["id"])).fetchone() or abort(404)
    if it["signed_at"] or not it["requires_signature"]:
        return redirect(url_for("client_packet", token=token))
    fields = json.loads(it["fields_json"] or "[]")
    items = db().execute("SELECT id, template_name, signed_at FROM packet_items WHERE packet_id=? "
                         "AND requires_signature=1 ORDER BY position", (p["id"],)).fetchall()
    errors, answers = {}, {}
    body = it["template_body"] or ""

    if request.method == "POST":
        for f in fields:
            if f["type"] == "checkbox":
                answers[f["id"]] = request.form.get(f["id"]) == "on"
                if f["required"] and not answers[f["id"]]:
                    errors[f["id"]] = "Please tick this box to continue."
            else:
                val = request.form.get(f["id"], "").strip()[:5000]
                answers[f["id"]] = val
                if f["required"] and not val:
                    errors[f["id"]] = "This field is required."
        signer = request.form.get("signer_name", "").strip()[:200]
        sig_data = request.form.get("signature", "")
        if not signer:
            errors["signer_name"] = "Type your full name."
        if request.form.get("consent") != "on":
            errors["consent"] = "Please agree to sign electronically."
        sig_bytes = None
        if sig_data.startswith("data:image/png;base64,"):
            try:
                sig_bytes = base64.b64decode(sig_data.split(",", 1)[1], validate=True)
            except ValueError:
                sig_bytes = None
        if not sig_bytes or len(sig_bytes) < 200 or len(sig_bytes) > 2_000_000:
            errors["signature"] = "Draw or type your signature."

        if not errors:
            ts = now()
            sig_name = f"sig-{p['id']}-{iid}-{secrets.token_hex(4)}.png"
            sig_path = os.path.join(SIGNED_DIR, sig_name)
            with open(sig_path, "wb") as fh:
                fh.write(sig_bytes)
            signers = [{"name": signer, "sig_path": sig_path, "signed_at": ts, "ip": client_ip(),
                        "user_agent": request.headers.get("User-Agent", "")[:300], "label": "Signature",
                        "email": p["client_email"]}]
            out_name, digest = render_item_pdf(p, it, answers, signers, method=signing_method(p))
            meta = {"ip": signers[0]["ip"], "user_agent": signers[0]["user_agent"]}
            db().execute(
                "UPDATE packet_items SET answers_json=?, signer_name=?, signature_path=?, signed_at=?, "
                "signed_pdf_path=?, signed_sha256=?, ip=?, user_agent=? WHERE id=? AND signed_at IS NULL",
                (json.dumps(answers), signer, sig_name, ts, out_name, digest, meta["ip"], meta["user_agent"], iid))
            db().execute("UPDATE packets SET status='in_progress' WHERE id=? AND completed_at IS NULL", (p["id"],))
            log_event(p["id"], "signed", it["template_name"])
            signed, total = packet_progress(p["id"])
            db().commit()
            if signed == total:
                if maybe_complete(p["id"]):   # also waits for a second owner and any required files
                    return redirect(url_for("client_done", token=token))
                return redirect(url_for("client_packet", token=token))
            nxt = db().execute("SELECT id FROM packet_items WHERE packet_id=? AND signed_at IS NULL "
                               "AND requires_signature=1 ORDER BY position",
                               (p["id"],)).fetchone()
            flash(f"“{it['template_name']}” signed.", "ok")
            return redirect(url_for("client_form", token=token, iid=nxt["id"]))

    return render_template("client_form.html", p=p, it=it, fields=fields, items=items, errors=errors,
                           answers=answers, body=body,
                           viewer=item_viewer(it),
                           signer_default=request.form.get("signer_name", p["client_name"]))


def packet_ref(p):
    return f"FS-{p['id']:05d}-{p['token'][:6].upper()}"


app.jinja_env.globals["packet_ref"] = packet_ref


def complete_packet(pid, reason="all forms signed"):
    db().execute("UPDATE packets SET status='completed', completed_at=? WHERE id=? AND completed_at IS NULL",
                 (now(), pid))
    log_event(pid, "completed", reason)
    db().commit()


@app.route("/s/<token>/done")
def client_done(token):
    p = load_packet(token)
    if p["voided_at"]:
        return render_template("client_closed.html", p=p, reason="cancelled")
    if not p["completed_at"]:
        return redirect(url_for("client_packet", token=token))
    items = db().execute("SELECT * FROM packet_items WHERE packet_id=? ORDER BY position", (p["id"],)).fetchall()
    return render_template("client_done.html", p=p, items=items)


@app.route("/s/<token>/doc/<int:iid>")
def client_doc(token, iid):
    p = load_packet(token)
    if client_block(p):
        abort(410)
    it = db().execute("SELECT * FROM packet_items WHERE id=? AND packet_id=?", (iid, p["id"])).fetchone() or abort(404)
    if not it["template_file"]:
        abort(404)
    download = request.args.get("download") == "1"
    if download and not it["downloaded_at"]:
        db().execute("UPDATE packet_items SET downloaded_at=? WHERE id=?", (now(), iid))
        log_event(p["id"], "downloaded", it["file_name"] or it["template_name"])
        db().commit()
    return send_stored(it["template_file"], it["file_mime"] or "application/pdf",
                       it["file_name"] or f"{it['template_name']}.pdf", force_download=download)


@app.route("/s/<token>/page/<int:iid>/<int:n>.png")
def client_page(token, iid, n):
    p = load_packet(token)
    if client_block(p):
        abort(410)
    it = db().execute("SELECT * FROM packet_items WHERE id=? AND packet_id=?", (iid, p["id"])).fetchone() or abort(404)
    v = item_viewer(it)
    if v["mode"] != "pages" or not 0 <= n < v["count"]:
        abort(404)
    out = os.path.join(PAGE_DIR, f"{os.path.splitext(v['pdf'])[0]}-{n}.png")
    preview.page_png(os.path.join(UPLOAD_DIR, v["pdf"]), n, out)
    return send_file(out, mimetype="image/png", max_age=86400)


@app.route("/s/<token>/signed/<int:iid>")
def client_signed(token, iid):
    p = load_packet(token)
    if p["voided_at"]:
        abort(410)
    it = db().execute("SELECT * FROM packet_items WHERE id=? AND packet_id=?", (iid, p["id"])).fetchone() or abort(404)
    if not it["signed_pdf_path"]:
        abort(404)
    return send_file(os.path.join(SIGNED_DIR, it["signed_pdf_path"]), mimetype="application/pdf",
                     as_attachment=True, download_name=f"{secure_filename(it['template_name'])}-signed.pdf")


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", title="Page not found",
                           message="This link doesn't match anything. Check that you copied the whole address."), 404


@app.errorhandler(400)
def bad_request(e):
    return render_template("error.html", title="Something went wrong",
                           message=getattr(e, "description", "Reload the page and try again.")), 400


@app.errorhandler(500)
def server_error(e):
    ref = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    app.logger.error("Server error ref %s on %s %s: %s", ref, request.method, request.path,
                     getattr(e, "original_exception", e), exc_info=getattr(e, "original_exception", None))
    return render_template("error.html", title="Something went wrong on our side",
                           message=f"Please try again in a moment. If it keeps happening, quote error "
                                   f"reference {ref}."), 500


@app.errorhandler(413)
def too_large(_e):
    return render_template("error.html", title="File too large",
                           message="Uploads are limited to 20 MB."), 413


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if request.path.startswith("/s/"):
        resp.headers.setdefault("X-Robots-Tag", "noindex")
    return resp


admin_api.register(__import__("sys").modules[__name__])
extras.register(__import__("sys").modules[__name__])
workflow.register(__import__("sys").modules[__name__])
daily.register(__import__("sys").modules[__name__])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=os.environ.get("DEBUG") == "1")
