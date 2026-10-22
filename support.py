"""Support chat: company staff write to the platform owner from inside the app; the owner answers from the Admin Console.

Staff:   the "Support" button at the bottom-right of every page opens the chat (one ongoing conversation per person).
Owner:   GET/POST /api/admin/support... (Bearer ADMIN_API_TOKEN), used by the Admin Console's Support inbox.
Alerts:  each new message from staff sends a free push notification to the owner's phone through ntfy
         (set NTFY_TOPIC; install the ntfy app and subscribe to the same topic).
"""
import hashlib
import hmac
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import abort, g, jsonify, request

NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "").strip()          # only for a protected topic or your own ntfy server
CONSOLE_URL = os.environ.get("CONSOLE_URL", "").rstrip("/")    # the Admin Console's address: tapping the alert opens the chat
SUPPORT_HOURS = os.environ.get("SUPPORT_HOURS", "Monday to Friday, 9 am to 5 pm Eastern")
MAX_LEN = 2000

SCHEMA = """
CREATE TABLE IF NOT EXISTS support_threads (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',          -- open | resolved (by the owner) | ended (by the staff member)
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  unread_owner INTEGER NOT NULL DEFAULT 0,      -- messages from staff the owner hasn't read
  unread_user INTEGER NOT NULL DEFAULT 0,       -- answers the staff member hasn't read
  UNIQUE (company_id, user_id)
);
CREATE TABLE IF NOT EXISTS support_messages (
  id INTEGER PRIMARY KEY,
  thread_id INTEGER NOT NULL REFERENCES support_threads(id),
  sender TEXT NOT NULL,                         -- user | owner | system (chat started / ended)
  author TEXT NOT NULL,
  body TEXT NOT NULL,
  page TEXT DEFAULT '',                         -- the page the staff member was on
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_support_msgs ON support_messages(thread_id, id);
"""


THREAD_COLUMNS = {"contact_company": "TEXT DEFAULT ''", "contact_name": "TEXT DEFAULT ''", "contact_email": "TEXT DEFAULT ''",
                  "contact_confirmed_at": "TEXT", "ended_at": "TEXT", "ended_by": "TEXT DEFAULT ''"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def init_db(conn):
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(support_threads)")}
    for col, spec in THREAD_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE support_threads ADD COLUMN {col} {spec}")


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def notify_owner(title, message, click="", reply=False):
    """Push notification to the owner's phone (ntfy). Never raises; runs in the background."""
    if not NTFY_TOPIC:
        return False

    def send():
        import json
        payload = {"topic": NTFY_TOPIC, "title": title[:120], "message": message, "tags": ["speech_balloon"], "priority": 4}
        if click:
            payload["click"] = click
        if click and reply:   # ntfy can't take a typed answer, so "Reply" opens the chat where you type it
            payload["actions"] = [{"action": "view", "label": "Reply", "url": click, "clear": True}]
        headers = {"Content-Type": "application/json"}
        if NTFY_TOKEN:
            headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
        try:
            req = urllib.request.Request(NTFY_URL, data=json.dumps(payload).encode("utf-8"), method="POST", headers=headers)
            urllib.request.urlopen(req, timeout=10).close()
        except Exception as exc:  # noqa: BLE001
            print(f"Support alert not sent: {exc}")
    threading.Thread(target=send, daemon=True, name="support-alert").start()
    return True


def register(A):
    app, db = A.app, A.db
    token = os.environ.get("ADMIN_API_TOKEN", "")
    app_id = hashlib.sha256(token.encode()).hexdigest()[:12] if token else ""

    def chat_link(tid):
        """Where tapping a phone alert goes: that chat in the Admin Console (tid 0 = the test alert)."""
        if not CONSOLE_URL:
            return ""
        ident = app_id or urllib.parse.urlsplit(getattr(A, "BASE_URL", "") or request.host_url).netloc or request.host
        return f"{CONSOLE_URL}/support/go/{ident}/{tid}"
    recent = {}   # user id -> send times, to stop runaway messages

    def thread_for(user, company_id, create=False):
        t = db().execute("SELECT * FROM support_threads WHERE company_id=? AND user_id=?", (company_id, user["id"])).fetchone()
        if not t and create:
            now = A.now()
            db().execute("INSERT INTO support_threads (company_id, user_id, created_at, updated_at) VALUES (?,?,?,?)",
                         (company_id, user["id"], now, now))
            t = db().execute("SELECT * FROM support_threads WHERE company_id=? AND user_id=?", (company_id, user["id"])).fetchone()
        return t

    def needs_intro(t):
        """A new conversation starts with the person confirming their company, name and email."""
        return (not t) or t["status"] in ("ended", "resolved") or not (t["contact_confirmed_at"] or "")

    def intro_defaults(t):
        return {"company": (t["contact_company"] if t and t["contact_company"] else g.company["name"]),
                "name": (t["contact_name"] if t and t["contact_name"] else g.user["name"]),
                "email": (t["contact_email"] if t and t["contact_email"] else g.user["email"])}

    def system_note(tid, text, now):
        db().execute("INSERT INTO support_messages (thread_id, sender, author, body, created_at) VALUES (?,?,?,?,?)",
                     (tid, "system", "FormSign", text, now))

    def messages(tid, after=0):
        return [dict(r) for r in db().execute(
            "SELECT id, sender, author, body, created_at FROM support_messages WHERE thread_id=? AND id>? ORDER BY id",
            (tid, after))]

    def staff_only():
        if not g.get("user") or not g.get("company") or g.user["id"] == 0:
            abort(404)

    # ------------------------------------------------ staff side
    @app.route("/support/chat.json", methods=["GET", "POST"])
    @A.login_required
    def support_chat():
        """GET: the conversation (marks answers as read). POST: send a message."""
        staff_only()
        cid = A.cid()
        if request.method == "POST":
            body = (request.form.get("body") or "").strip()[:MAX_LEN]
            if not body:
                return jsonify({"error": "Type a message first."}), 400
            times = [t for t in recent.get(g.user["id"], []) if t > time.time() - 600]
            if len(times) >= 30:
                return jsonify({"error": "That's a lot of messages in a few minutes. Please wait a little."}), 429
            recent[g.user["id"]] = times + [time.time()]
            t = thread_for(g.user, cid, create=True)
            if needs_intro(t):
                return jsonify({"error": "Please confirm your details first.", "need_intro": True, "intro": intro_defaults(t)}), 409
            now = A.now()
            page = (request.form.get("page") or "")[:200]
            db().execute("INSERT INTO support_messages (thread_id, sender, author, body, page, created_at) VALUES (?,?,?,?,?,?)",
                         (t["id"], "user", t["contact_name"] or g.user["name"], body, page, now))
            db().execute("UPDATE support_threads SET status='open', updated_at=?, unread_owner=unread_owner+1 WHERE id=?",
                         (now, t["id"]))
            db().commit()
            company = t["contact_company"] or g.company["name"]
            click = chat_link(t["id"])   # tapping the alert (or Reply) opens this exact chat in the Admin Console
            notify_owner(f"{company}: {t['contact_name'] or g.user['name']}", body[:300], click, reply=bool(click))
        t = thread_for(g.user, cid)
        if not t:
            return jsonify({"messages": [], "status": "new", "unread": 0, "hours": SUPPORT_HOURS,
                            "need_intro": True, "intro": intro_defaults(None)})
        after = request.args.get("after", "0")
        msgs = messages(t["id"], int(after) if after.isdigit() else 0)
        if request.method == "GET" and t["unread_user"] and request.args.get("seen", "1") == "1":
            db().execute("UPDATE support_threads SET unread_user=0 WHERE id=?", (t["id"],))
            db().commit()
        return jsonify({"messages": msgs, "status": t["status"], "hours": SUPPORT_HOURS,
                        "need_intro": needs_intro(t), "intro": intro_defaults(t),
                        "unread": 0 if request.args.get("seen", "1") == "1" else t["unread_user"]})

    @app.route("/support/start.json", methods=["POST"])
    @A.login_required
    def support_start():
        """Before a conversation starts: the person confirms their company, name and email."""
        staff_only()
        f = request.form
        company, name = (f.get("company") or "").strip()[:120], (f.get("name") or "").strip()[:120]
        email, confirm = (f.get("email") or "").strip()[:200], (f.get("email_confirm") or "").strip()[:200]
        errors = {}
        if not company:
            errors["company"] = "Enter your company name."
        if not name:
            errors["name"] = "Enter your name."
        if not EMAIL_RE.match(email):
            errors["email"] = "Enter a valid email address."
        elif email.lower() != confirm.lower():
            errors["email_confirm"] = "The two email addresses don't match."
        if errors:
            return jsonify({"error": "Please check the highlighted boxes.", "errors": errors}), 400
        t = thread_for(g.user, A.cid(), create=True)
        now = A.now()
        db().execute("UPDATE support_threads SET contact_company=?, contact_name=?, contact_email=?, contact_confirmed_at=?, "
                     "status='open', ended_at=NULL, ended_by='', updated_at=? WHERE id=?",
                     (company, name, email, now, now, t["id"]))
        system_note(t["id"], f"Chat started by {name} ({company}, {email})", now)
        db().commit()
        return jsonify({"ok": True, "messages": messages(t["id"], int(f.get("after") or 0) if str(f.get("after") or "0").isdigit() else 0),
                        "need_intro": False})

    @app.route("/support/end.json", methods=["POST"])
    @A.login_required
    def support_end():
        """The person closes the conversation: it's marked as finished for the owner too."""
        staff_only()
        t = thread_for(g.user, A.cid())
        if not t or t["status"] == "ended":
            return jsonify({"ok": True, "need_intro": True})
        now, who = A.now(), (t["contact_name"] or g.user["name"])
        db().execute("UPDATE support_threads SET status='ended', ended_at=?, ended_by=?, updated_at=?, "
                     "unread_owner=unread_owner+1, unread_user=0 WHERE id=?", (now, who, now, t["id"]))
        system_note(t["id"], f"{who} ended the chat", now)
        db().commit()
        return jsonify({"ok": True, "need_intro": True})

    @app.route("/support/unread.json")
    @A.login_required
    def support_unread():
        staff_only()
        t = thread_for(g.user, A.cid())
        return jsonify({"unread": t["unread_user"] if t else 0})

    @app.context_processor
    def inject_support():
        u = g.get("user")
        show = bool(u and g.get("company") and u["id"] != 0 and not u.get("must_change_password"))
        unread = 0
        if show:
            try:
                t = thread_for(u, g.company["id"])
                unread = t["unread_user"] if t else 0
            except Exception:  # noqa: BLE001
                pass
        return {"support_chat": show, "support_unread": unread, "support_hours": SUPPORT_HOURS}

    # ------------------------------------------------ owner side (Admin Console)
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

    def thread_json(t):
        last = db().execute("SELECT sender, body, created_at FROM support_messages WHERE thread_id=? ORDER BY id DESC LIMIT 1",
                            (t["id"],)).fetchone()
        return {"id": t["id"], "company_id": t["company_id"], "company": t["company_name"], "user": t["user_name"],
                "email": t["user_email"], "role": t["user_role"], "status": t["status"], "updated_at": t["updated_at"],
                "unread": t["unread_owner"], "last": dict(last) if last else None,
                "contact": {"company": t["contact_company"] or "", "name": t["contact_name"] or "", "email": t["contact_email"] or "",
                            "confirmed_at": t["contact_confirmed_at"]},
                "ended_at": t["ended_at"], "ended_by": t["ended_by"] or ""}

    THREADS = ("SELECT t.*, c.name AS company_name, u.name AS user_name, u.email AS user_email, u.role AS user_role "
               "FROM support_threads t JOIN companies c ON c.id=t.company_id JOIN users u ON u.id=t.user_id")

    @app.route("/api/admin/support")
    @api
    def admin_api_support():
        status = request.args.get("status", "")
        sql, args = THREADS, []
        if status == "open":
            sql += " WHERE t.status='open'"
        elif status == "resolved":              # finished: resolved by the owner or ended by the staff member
            sql += " WHERE t.status IN ('resolved', 'ended')"
        rows = db().execute(sql + " ORDER BY t.unread_owner > 0 DESC, t.updated_at DESC LIMIT 200", args).fetchall()
        unread = db().execute("SELECT COALESCE(SUM(unread_owner),0) FROM support_threads").fetchone()[0]
        return jsonify({"threads": [thread_json(r) for r in rows], "unread": unread,
                        "alerts": bool(NTFY_TOPIC), "console_url": CONSOLE_URL, "app_id": app_id})

    @app.route("/api/admin/support/<int:tid>", methods=["GET", "POST"])
    @api
    def admin_api_support_thread(tid):
        t = db().execute(THREADS + " WHERE t.id=?", (tid,)).fetchone() or abort(404)
        data = request.get_json(silent=True) or {}
        if request.method == "POST":
            action = data.get("action", "reply")
            now = A.now()
            if action == "reply":
                body = str(data.get("body", "")).strip()[:MAX_LEN]
                if not body:
                    return jsonify({"error": "Type a reply first."}), 400
                author = str(data.get("author") or "Support")[:80]
                db().execute("INSERT INTO support_messages (thread_id, sender, author, body, created_at) VALUES (?,?,?,?,?)",
                             (tid, "owner", author, body, now))
                db().execute("UPDATE support_threads SET updated_at=?, unread_user=unread_user+1, unread_owner=0 WHERE id=?",
                             (now, tid))
            elif action in ("resolve", "reopen"):   # reopening an ended chat is allowed too
                db().execute("UPDATE support_threads SET status=?, unread_owner=0 WHERE id=?",
                             ("resolved" if action == "resolve" else "open", tid))
            else:
                return jsonify({"error": "unknown action"}), 400
            db().commit()
            t = db().execute(THREADS + " WHERE t.id=?", (tid,)).fetchone()
        elif request.args.get("seen", "1") == "1" and t["unread_owner"]:
            db().execute("UPDATE support_threads SET unread_owner=0 WHERE id=?", (tid,))
            db().commit()
        after = request.args.get("after", "0")
        msgs = [dict(r) for r in db().execute(
            "SELECT id, sender, author, body, page, created_at FROM support_messages WHERE thread_id=? AND id>? ORDER BY id",
            (tid, int(after) if after.isdigit() else 0))]
        return jsonify({"thread": thread_json(t), "messages": msgs})

    @app.route("/api/admin/support/test-alert", methods=["POST"])
    @api
    def admin_api_support_test():
        if not NTFY_TOPIC:
            return jsonify({"error": "NTFY_TOPIC isn't set on this app."}), 400
        link = chat_link(0)
        notify_owner(f"{A.PLATFORM_NAME}: test alert",
                     "Support chat alerts are working. Tap this alert (or Reply) to check it opens your Admin Console."
                     if link else "Support chat alerts are working. Add CONSOLE_URL to this app so tapping an alert opens the chat.",
                     link, reply=bool(link))
        return jsonify({"ok": True, "message": "Test alert sent. It should reach your phone within a few seconds."})

    A.notify_owner = notify_owner
