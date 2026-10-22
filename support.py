"""Support chat: company staff write to the platform owner from inside the app; the owner answers from the Admin Console.

Staff:   the "Support" button at the bottom-right of every page opens the chat (one ongoing conversation per person).
Owner:   GET/POST /api/admin/support... (Bearer ADMIN_API_TOKEN), used by the Admin Console's Support inbox.
Alerts:  each new message from staff sends a free push notification to the owner's phone through ntfy
         (set NTFY_TOPIC; install the ntfy app and subscribe to the same topic).
"""
import hmac
import os
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
  status TEXT NOT NULL DEFAULT 'open',          -- open | resolved
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  unread_owner INTEGER NOT NULL DEFAULT 0,      -- messages from staff the owner hasn't read
  unread_user INTEGER NOT NULL DEFAULT 0,       -- answers the staff member hasn't read
  UNIQUE (company_id, user_id)
);
CREATE TABLE IF NOT EXISTS support_messages (
  id INTEGER PRIMARY KEY,
  thread_id INTEGER NOT NULL REFERENCES support_threads(id),
  sender TEXT NOT NULL,                         -- user | owner
  author TEXT NOT NULL,
  body TEXT NOT NULL,
  page TEXT DEFAULT '',                         -- the page the staff member was on
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_support_msgs ON support_messages(thread_id, id);
"""


def init_db(conn):
    conn.executescript(SCHEMA)


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
    recent = {}   # user id -> send times, to stop runaway messages

    def thread_for(user, company_id, create=False):
        t = db().execute("SELECT * FROM support_threads WHERE company_id=? AND user_id=?", (company_id, user["id"])).fetchone()
        if not t and create:
            now = A.now()
            db().execute("INSERT INTO support_threads (company_id, user_id, created_at, updated_at) VALUES (?,?,?,?)",
                         (company_id, user["id"], now, now))
            t = db().execute("SELECT * FROM support_threads WHERE company_id=? AND user_id=?", (company_id, user["id"])).fetchone()
        return t

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
            now = A.now()
            page = (request.form.get("page") or "")[:200]
            db().execute("INSERT INTO support_messages (thread_id, sender, author, body, page, created_at) VALUES (?,?,?,?,?,?)",
                         (t["id"], "user", g.user["name"], body, page, now))
            db().execute("UPDATE support_threads SET status='open', updated_at=?, unread_owner=unread_owner+1 WHERE id=?",
                         (now, t["id"]))
            db().commit()
            company = g.company["name"]
            click = ""
            if CONSOLE_URL:   # tapping the alert (or Reply) opens this exact chat in the Admin Console
                host = urllib.parse.urlsplit(getattr(A, "BASE_URL", "") or request.host_url).netloc or request.host
                click = f"{CONSOLE_URL}/support/go/{host}/{t['id']}"
            notify_owner(f"{company}: {g.user['name']}", body[:300], click, reply=bool(click))
        t = thread_for(g.user, cid)
        if not t:
            return jsonify({"messages": [], "status": "new", "unread": 0, "hours": SUPPORT_HOURS})
        after = request.args.get("after", "0")
        msgs = messages(t["id"], int(after) if after.isdigit() else 0)
        if request.method == "GET" and t["unread_user"] and request.args.get("seen", "1") == "1":
            db().execute("UPDATE support_threads SET unread_user=0 WHERE id=?", (t["id"],))
            db().commit()
        return jsonify({"messages": msgs, "status": t["status"], "hours": SUPPORT_HOURS,
                        "unread": 0 if request.args.get("seen", "1") == "1" else t["unread_user"]})

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
                "unread": t["unread_owner"], "last": dict(last) if last else None}

    THREADS = ("SELECT t.*, c.name AS company_name, u.name AS user_name, u.email AS user_email, u.role AS user_role "
               "FROM support_threads t JOIN companies c ON c.id=t.company_id JOIN users u ON u.id=t.user_id")

    @app.route("/api/admin/support")
    @api
    def admin_api_support():
        status = request.args.get("status", "")
        sql, args = THREADS, []
        if status in ("open", "resolved"):
            sql += " WHERE t.status=?"
            args.append(status)
        rows = db().execute(sql + " ORDER BY t.unread_owner > 0 DESC, t.updated_at DESC LIMIT 200", args).fetchall()
        unread = db().execute("SELECT COALESCE(SUM(unread_owner),0) FROM support_threads").fetchone()[0]
        return jsonify({"threads": [thread_json(r) for r in rows], "unread": unread,
                        "alerts": bool(NTFY_TOPIC)})

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
            elif action in ("resolve", "reopen"):
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
        notify_owner(f"{A.PLATFORM_NAME}: test alert", "Support chat alerts are working.",
                     f"{CONSOLE_URL}/support" if CONSOLE_URL else "")
        return jsonify({"ok": True, "message": "Test alert sent. It should reach your phone within a few seconds."})

    A.notify_owner = notify_owner
