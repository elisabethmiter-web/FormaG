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

import mailer
import pdfgen
import preview

# ---------------------------------------------------------------- config
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
DB_PATH = os.path.join(DATA_DIR, "formsign.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "templates")
SIGNED_DIR = os.path.join(DATA_DIR, "signed")
for d in (DATA_DIR, UPLOAD_DIR, SIGNED_DIR):
    os.makedirs(d, exist_ok=True)

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
BUSINESS_NAME = os.environ.get("BUSINESS_NAME", "FormSign")
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
  file_mime TEXT
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
  notes TEXT DEFAULT ''
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
  last_login_at TEXT
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
    conn.executescript(SCHEMA)
    # Older packets only stored the creator's name: link them to that login, or to the owner if none matches.
    conn.execute("UPDATE packets SET created_by_id = COALESCE((SELECT u.id FROM users u WHERE u.name = "
                 "packets.created_by COLLATE NOCASE ORDER BY u.id LIMIT 1), 0) WHERE created_by_id IS NULL")
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


@app.context_processor
def inject_globals():
    return {"business_name": BUSINESS_NAME, "csrf_token": csrf_token,
            "email_enabled": mailer.enabled()}


# ---------------------------------------------------------------- auth + csrf
def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


@app.before_request
def check_csrf():
    if request.method == "POST":
        sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token", "")
        if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Your session expired. Reload the page and try again.")


# ---------------------------------------------------------------- users & roles
# Two kinds of sign-in:
#   * staff      – create, send and track packets; can view the form library but not change it
#   * manager    – everything staff can do, plus add/edit/remove library forms and manage team logins
# The ADMIN_PASSWORD environment variable is the owner login (username "admin"). It always works,
# has manager rights, and is how the first team accounts are created.
ROLE_LABELS = {"staff": "Staff", "manager": "Management"}
OWNER = {"id": 0, "name": "Owner", "email": "admin", "role": "manager", "active": 1, "must_change_password": 0}
_failed_logins = {}  # ip -> [timestamps]; slows down password guessing


@app.before_request
def load_user():
    g.user = None
    uid = session.get("uid")
    if uid is None:
        return
    if uid == 0:
        if ADMIN_PASSWORD:
            g.user = OWNER
        return
    row = db().execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
    if row and session.get("pwv") == row["password_hash"][-12:]:
        g.user = dict(row)
    else:
        session.pop("uid", None)  # deactivated or password changed elsewhere


def is_manager():
    return bool(g.get("user")) and g.user["role"] == "manager"


@app.context_processor
def inject_user():
    return {"current_user": g.get("user"), "is_manager": is_manager(), "ROLE_LABELS": ROLE_LABELS}


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not g.get("user"):
            return redirect(url_for("login", next=request.path))
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


def actor():
    return g.user["name"] if g.get("user") else ""


# ---------------------------------------------------------------- who sees which packets
# Management and the owner see every packet. Staff see the packets they created, plus any packet
# where they are named as the sales rep (so a manager can set one up for them).
def packet_scope():
    """SQL condition (and its arguments) limiting packets to the ones the signed-in user may see."""
    if is_manager():
        return "1=1", []
    return "(created_by_id = ? OR (sales_rep != '' AND sales_rep = ? COLLATE NOCASE))", [g.user["id"], g.user["name"]]


@app.before_request
def guard_packet_access():
    """Every staff page about one packet (/packets/<pid>/...) checks the packet is theirs."""
    pid = (request.view_args or {}).get("pid")
    if pid is None or not g.get("user") or is_manager():
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
        if len(recent) >= 8:
            error = "Too many attempts. Wait 15 minutes and try again."
        elif email.lower() == "admin":
            if ADMIN_PASSWORD and hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
                user = OWNER
        else:
            row = db().execute("SELECT * FROM users WHERE email=? AND active=1", (email,)).fetchone()
            if row and check_password_hash(row["password_hash"], password):
                user = dict(row)
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
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
        if not error:
            _failed_logins[ip].append(datetime.now(timezone.utc).timestamp())
            error = "That email and password don't match an active account."
    return render_template("login.html", error=error, email=request.form.get("email", ""))


def valid_password(pw):
    if len(pw) < 8:
        return "Use at least 8 characters."
    return None


@app.route("/account", methods=["GET", "POST"])
def account():
    if not g.get("user"):
        return redirect(url_for("login"))
    if g.user["id"] == 0:
        flash("The owner login's password is the ADMIN_PASSWORD setting on your server.", "ok")
        return redirect(url_for("dashboard"))
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
            problem = "Someone already has a login with that email."
        else:
            problem = valid_password(pw)
        if problem:
            flash(problem, "error")
        else:
            db().execute("INSERT INTO users (name, email, password_hash, role, created_at) VALUES (?,?,?,?,?)",
                         (name, email, generate_password_hash(pw), role, now()))
            db().commit()
            flash(f"Login created for {name} ({ROLE_LABELS[role]}). Give them the temporary password; "
                  f"they'll choose their own when they first sign in.", "ok")
            return redirect(url_for("team"))
    users = db().execute("SELECT u.*, (SELECT COUNT(*) FROM packets p WHERE p.created_by_id = u.id) AS packet_count "
                         "FROM users u ORDER BY active DESC, role DESC, name COLLATE NOCASE").fetchall()
    return render_template("team.html", users=users, form=request.form)


@app.route("/team/<int:uid>", methods=["POST"])
@manager_required
def team_update(uid):
    u = db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone() or abort(404)
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
            new_owner = db().execute("SELECT id, name FROM users WHERE id=? AND id != ?", (to, uid)).fetchone() \
                if to.isdigit() and to != "0" else None
            n = db().execute("UPDATE packets SET created_by_id=? WHERE created_by_id=?",
                             (new_owner["id"] if new_owner else 0, uid)).rowcount
            db().execute("DELETE FROM users WHERE id=?", (uid,))
            where = f"moved to {new_owner['name']}" if new_owner else "kept, visible to management"
            flash(f"{u['name']}'s login was removed." + (f" Their {n} packet{'s' if n != 1 else ''} {'were' if n != 1 else 'was'} {where}." if n else ""), "ok")
    db().commit()
    return redirect(url_for("team"))


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
def sales_reps():
    cond, args = packet_scope()
    return [r[0] for r in db().execute(
        f"SELECT DISTINCT sales_rep FROM packets WHERE sales_rep != '' AND {cond} ORDER BY sales_rep COLLATE NOCASE",
        args)]


def still_needed(pid):
    """Names of the forms the client still has to sign or approve."""
    return [r[0] for r in db().execute(
        "SELECT template_name FROM packet_items WHERE packet_id=? AND requires_signature=1 AND signed_at IS NULL "
        "ORDER BY position", (pid,))]


@app.route("/")
@login_required
def dashboard():
    q = request.args.get("q", "").strip()[:100]
    rep_filter = request.args.get("rep", "").strip()[:80]
    cond, args = packet_scope()
    sql = f"SELECT * FROM packets WHERE {cond}"
    if q:
        like = f"%{q.replace('%', '').replace('_', '')}%"
        sql += (" AND (so_number LIKE ? OR sales_rep LIKE ? OR client_name LIKE ? OR client_email LIKE ?"
                " OR REPLACE(REPLACE(UPPER(so_number), 'SO', ''), '#', '') LIKE ?)")
        bare = q.upper().replace("SO", "").replace("#", "").strip()
        args += [like, like, like, like, f"%{bare}%" if bare else like]
    if rep_filter:
        sql += " AND sales_rep = ?"
        args.append(rep_filter)
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
    has_templates = db().execute("SELECT 1 FROM templates WHERE archived=0 LIMIT 1").fetchone()
    return render_template("dashboard.html", packets=packets, counts=counts, show=filt, sign_hours=SIGN_WITHIN_HOURS,
                           has_templates=bool(has_templates), q=q, rep=rep_filter, reps=sales_reps())


# ---------------------------------------------------------------- admin: form library
@app.route("/library")
@login_required
def library():
    rows = db().execute("SELECT * FROM templates WHERE archived=0 ORDER BY name COLLATE NOCASE").fetchall()
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
app.jinja_env.globals.update(sales_reps=lambda: sales_reps())


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
    db().execute(
        "INSERT INTO templates (name, kind, description, file_path, file_sha256, fields_json, created_at, "
        "file_name, file_mime) VALUES (?,?,?,?,?,?,?,?,?)",
        (name or os.path.splitext(up["name"])[0], up["kind"], request.form.get("description", "").strip(),
         up["stored"], up["sha256"], json.dumps(fields), now(), up["name"], up["mime"]))
    db().commit()
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
            "INSERT INTO templates (name, kind, description, fields_json, created_at) VALUES (?,?,?,?,?)",
            (name, "form", request.form.get("body", "").strip(), json.dumps(fields), now()))
        db().commit()
        flash("Form created.", "ok")
        return redirect(url_for("library"))
    return render_template("form_builder.html", form=None, fields_json="[]", name="", body="")


@app.route("/library/<int:tid>/edit", methods=["GET", "POST"])
@manager_required
def edit_form(tid):
    t = db().execute("SELECT * FROM templates WHERE id=? AND archived=0", (tid,)).fetchone() or abort(404)
    if request.method == "POST":
        name = request.form.get("name", "").strip() or t["name"]
        fields = parse_fields_from_request()
        if t["kind"] == "form" and not fields:
            flash("A fillable form needs at least one field.", "error")
            return redirect(url_for("edit_form", tid=tid))
        db().execute("UPDATE templates SET name=?, description=?, fields_json=? WHERE id=?",
                     (name, request.form.get("body", "").strip(), json.dumps(fields), tid))
        db().commit()
        flash("Saved. Packets already sent keep the version they were sent with.", "ok")
        return redirect(url_for("library"))
    return render_template("form_builder.html", form=t, fields_json=t["fields_json"],
                           name=t["name"], body=t["description"])


@app.route("/library/<int:tid>/archive", methods=["POST"])
@manager_required
def archive_form(tid):
    db().execute("UPDATE templates SET archived=1 WHERE id=?", (tid,))
    db().commit()
    flash("Form removed from the library. Past packets are unaffected.", "ok")
    return redirect(url_for("library"))


@app.route("/library/<int:tid>/file")
@login_required
def template_file(tid):
    t = db().execute("SELECT * FROM templates WHERE id=?", (tid,)).fetchone() or abort(404)
    if not t["file_path"]:
        abort(404)
    return send_stored(t["file_path"], t["file_mime"] or "application/pdf", t["file_name"] or f"{t['name']}.pdf")


# ---------------------------------------------------------------- admin: packets
@app.route("/packets/new", methods=["GET", "POST"])
@login_required
def new_packet():
    templates = db().execute("SELECT * FROM templates WHERE archived=0 ORDER BY name COLLATE NOCASE").fetchall()
    if request.method == "POST":
        name = request.form.get("client_name", "").strip()
        email = request.form.get("client_email", "").strip()
        ids = [int(x) for x in request.form.getlist("template_ids") if x.isdigit()]
        adhoc = [f for f in request.files.getlist("adhoc_files") if f and f.filename]
        if not name or "@" not in email or not (ids or adhoc):
            flash("Add the client's name, a valid email, and at least one form or file.", "error")
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
            "so_number, sales_rep, created_by, created_by_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (token, name, email, request.form.get("message", "").strip(), "draft", now(), expires,
             request.form.get("so_number", "").strip()[:40], request.form.get("sales_rep", "").strip()[:80],
             actor(), g.user["id"]))
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
        db().commit()
        if request.form.get("action") == "send_email" and mailer.enabled():
            return send_packet_email(pid)
        mark_sent(pid, "link")
        flash("Packet ready. Copy the signing link below and send it to your client.", "ok")
        return redirect(url_for("packet_detail", pid=pid))
    pre = [int(x) for x in request.args.getlist("t") if x.isdigit()]
    return render_template("packet_new.html", templates=templates, form={}, selected=pre)


def mark_sent(pid, how):
    p = db().execute("SELECT * FROM packets WHERE id=?", (pid,)).fetchone()
    if p["status"] == "draft":
        db().execute("UPDATE packets SET status='sent', sent_at=? WHERE id=?", (now(), pid))
    log_event(pid, "sent", "by email" if how == "email" else "link created")
    db().commit()


def send_packet_email(pid):
    p = db().execute("SELECT * FROM packets WHERE id=?", (pid,)).fetchone()
    items = db().execute("SELECT template_name FROM packet_items WHERE packet_id=? ORDER BY position",
                         (pid,)).fetchall()
    try:
        mailer.send_request(p["client_email"], p["client_name"], BUSINESS_NAME,
                            [i["template_name"] for i in items], signing_url(p["token"]),
                            p["message"], p["expires_at"])
        mark_sent(pid, "email")
        flash(f"Sent to {p['client_email']}.", "ok")
    except Exception as exc:  # noqa: BLE001
        log_event(pid, "email_failed", str(exc)[:200])
        db().commit()
        flash(f"The email didn't go out ({exc}). You can still copy the link below.", "error")
        mark_sent(pid, "link")
    return redirect(url_for("packet_detail", pid=pid))


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


@app.route("/packets/<int:pid>/details", methods=["POST"])
@login_required
def update_packet_details(pid):
    db().execute("UPDATE packets SET so_number=?, sales_rep=? WHERE id=?",
                 (request.form.get("so_number", "").strip()[:40], request.form.get("sales_rep", "").strip()[:80], pid))
    log_event(pid, "details", f"SO# / sales rep updated by {actor()}")
    db().commit()
    flash("Details saved.", "ok")
    return redirect(url_for("packet_detail", pid=pid))


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


@app.route("/packets/<int:pid>/email", methods=["POST"])
@login_required
def email_packet(pid):
    if not mailer.enabled():
        flash("Email isn't set up yet. Add the SMTP settings to turn it on.", "error")
        return redirect(url_for("packet_detail", pid=pid))
    return send_packet_email(pid)


@app.route("/packets/<int:pid>/remind", methods=["POST"])
@login_required
def remind_packet(pid):
    if mailer.enabled():
        return send_packet_email(pid)
    log_event(pid, "reminder", "copied link")
    db().commit()
    return redirect(url_for("packet_detail", pid=pid))


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
    pdfgen.audit_trail(path, BUSINESS_NAME, dict(p), [dict(i) for i in items], [dict(e) for e in events])
    return send_file(path, mimetype="application/pdf", download_name=f"audit-trail-{pid}.pdf")


# ---------------------------------------------------------------- client side
def load_packet(token):
    p = db().execute("SELECT * FROM packets WHERE token=?", (token,)).fetchone()
    if not p:
        abort(404)
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
            out_name = f"{p['id']}-{iid}-{secrets.token_hex(6)}.pdf"
            out_path = os.path.join(SIGNED_DIR, out_name)
            meta = {"business": BUSINESS_NAME, "packet_ref": packet_ref(p), "client_name": p["client_name"],
                    "client_email": p["client_email"], "signer_name": signer, "signed_at": ts,
                    "ip": client_ip(), "user_agent": request.headers.get("User-Agent", "")[:300],
                    "form_name": it["template_name"], "original_sha256": it["template_sha256"],
                    "so_number": p["so_number"] or ""}
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
            pdfgen.signed_document(out_path, source, body, fields, answers, sig_path, meta, attachment=attachment)
            digest = sha256_file(out_path)
            db().execute(
                "UPDATE packet_items SET answers_json=?, signer_name=?, signature_path=?, signed_at=?, "
                "signed_pdf_path=?, signed_sha256=?, ip=?, user_agent=? WHERE id=? AND signed_at IS NULL",
                (json.dumps(answers), signer, sig_name, ts, out_name, digest, meta["ip"], meta["user_agent"], iid))
            db().execute("UPDATE packets SET status='in_progress' WHERE id=? AND completed_at IS NULL", (p["id"],))
            log_event(p["id"], "signed", it["template_name"])
            signed, total = packet_progress(p["id"])
            db().commit()
            if signed == total:
                complete_packet(p["id"])
                return redirect(url_for("client_done", token=token))
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
    if mailer.enabled():
        p = db().execute("SELECT * FROM packets WHERE id=?", (pid,)).fetchone()
        items = db().execute("SELECT * FROM packet_items WHERE packet_id=? ORDER BY position", (pid,)).fetchall()
        files = [(f"{secure_filename(i['template_name'])}-signed.pdf", os.path.join(SIGNED_DIR, i["signed_pdf_path"]))
                 for i in items if i["signed_pdf_path"]]
        # Non-PDF originals (Word, Excel, images...) go along so everyone has the actual files.
        files += [(i["file_name"], os.path.join(UPLOAD_DIR, i["template_file"])) for i in items
                  if i["template_kind"] == "file" or (not i["requires_signature"] and i["template_file"])]
        try:
            mailer.send_completed(p["client_email"], p["client_name"], BUSINESS_NAME, packet_ref(p), files,
                                  admin_link=None)
            mailer.send_completed(mailer.admin_email(), p["client_name"], BUSINESS_NAME, packet_ref(p), files,
                                  admin_link=(BASE_URL or "") + url_for("packet_detail", pid=pid))
            log_event(pid, "receipt_emailed", "client and owner")
        except Exception as exc:  # noqa: BLE001
            log_event(pid, "email_failed", str(exc)[:200])
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=os.environ.get("DEBUG") == "1")
