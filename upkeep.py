"""Quick search (10), installable app (11) and retention rules (15).

10  Ctrl+K (or ⌘K, or the 🔍 button) opens a search box: clients, packets, SO#, forms, team and pages.
11  The app can be installed on phones, tablets and computers (web app manifest + a small service worker
    that keeps the look-and-feel files cached and shows a friendly page when there's no connection).
15  Each company chooses: archive finished packets after N months, and delete them for good after N years.
    A background job does it daily; every deletion is logged (reference and dates only, no client details).
"""
import fcntl
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import Response, abort, flash, g, jsonify, redirect, render_template, request, send_from_directory, url_for

ARCHIVE_CHOICES = (0, 3, 6, 12, 24)          # months
DELETE_CHOICES = (0, 1, 2, 3, 5, 7, 10)      # years
SCHEMA = """
CREATE TABLE IF NOT EXISTS retention_log (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL,
  ref TEXT NOT NULL,
  created_at TEXT,
  finished_at TEXT,
  forms INTEGER DEFAULT 0,
  files INTEGER DEFAULT 0,
  deleted_at TEXT NOT NULL,
  rule TEXT DEFAULT ''
);
"""
COLUMNS = {"companies": {"retain_archive_months": "INTEGER DEFAULT 0", "retain_delete_years": "INTEGER DEFAULT 0",
                         "retain_set_by": "TEXT DEFAULT ''", "retain_set_at": "TEXT"}}


def init_db(conn):
    conn.executescript(SCHEMA)
    for table, cols in COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, spec in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {spec}")


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def make_icons(static_dir):
    """App icons for phones/tablets (drawn once): a white page with a signature line on the accent blue."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return
    blue, white, grey = (36, 64, 142), (255, 255, 255), (196, 204, 222)
    for size, name, pad in ((192, "icon-192.png", .20), (512, "icon-512.png", .20), (512, "icon-maskable.png", .30),
                            (180, "apple-touch-icon.png", .20)):
        path = os.path.join(static_dir, name)
        if os.path.exists(path):
            continue
        k = 4                                   # draw large, then shrink for smooth edges
        S = size * k
        im = Image.new("RGB", (S, S), blue)
        d = ImageDraw.Draw(im)
        inner = S * (1 - 2 * pad)
        w, h = inner * 0.74, inner
        x0, y0 = (S - w) / 2, (S - h) / 2
        d.rounded_rectangle([x0, y0, x0 + w, y0 + h], radius=inner * 0.07, fill=white)
        lw = max(2, int(inner * 0.045))
        for i in range(3):                      # text lines
            y = y0 + h * (0.20 + i * 0.13)
            d.rounded_rectangle([x0 + w * 0.16, y, x0 + w * (0.84 if i < 2 else 0.6), y + lw], radius=lw / 2, fill=grey)
        base = y0 + h * 0.80                    # signature line
        d.rounded_rectangle([x0 + w * 0.16, base, x0 + w * 0.84, base + lw * 0.7], radius=lw / 2, fill=grey)
        pts = [(0.16, 0.74), (0.26, 0.62), (0.33, 0.73), (0.42, 0.60), (0.50, 0.74), (0.60, 0.64), (0.70, 0.70), (0.84, 0.62)]
        d.line([(x0 + w * px, y0 + h * py) for px, py in pts], fill=blue, width=int(lw * 1.25), joint="curve")
        im = im.resize((size, size), Image.LANCZOS)
        try:
            im.save(path, "PNG", optimize=True)
        except OSError:
            pass


SW = r"""// FormSign service worker: keeps a copy of the app's look-and-feel files, shows a friendly page offline.
const CACHE = 'formsign-%(version)s';
const OFFLINE = '/offline';
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll([OFFLINE, '/static/style.css', '/static/icon-192.png'])).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', (e) => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin) return;
  if (url.pathname.startsWith('/static/')) {          // look-and-feel files: always fresh, the saved copy only when offline
    e.respondWith(caches.open(CACHE).then((c) => fetch(req).then((r) => { if (r.ok) c.put(req, r.clone()); return r; })
      .catch(() => c.match(req).then((hit) => hit || Response.error()))));
    return;
  }
  if (req.mode === 'navigate') {                        // pages: always live; the offline page only when there's no connection
    e.respondWith(fetch(req).catch(() => caches.match(OFFLINE)));
  }
});
"""


def register(A):
    app, db = A.app, A.db
    make_icons(os.path.join(os.path.dirname(os.path.abspath(A.__file__)), "static"))

    # ================================================================ 10 quick search
    PAGES = [("Packets", "dashboard", "Who still needs to sign"), ("New packet", "new_packet", "Send forms to a client"),
             ("Follow-up", "follow_up", "Who to remind today"), ("Form library", "library", "Your forms and bundles"),
             ("Help", "help_page", "How-tos, guided tours"), ("Your account", "account", "Password, pop-ups"),
             ("Team", "team", "Logins and activity"), ("Company", "company_settings", "Logo, wording, storage, retention")]

    def page_url(endpoint):
        try:
            return url_for(endpoint)
        except Exception:  # noqa: BLE001  (edition without that page)
            return None

    @app.route("/search.json")
    @A.login_required
    def quick_search():
        q = request.args.get("q", "").strip()[:80]
        low = q.lower()
        out = []
        manager = A.is_manager()
        for label, ep, hint in PAGES:
            if ep in ("team", "company_settings") and not manager:
                continue
            u = page_url(ep)
            if u and (not low or low in label.lower() or low in hint.lower()):
                out.append({"kind": "Page", "label": label, "hint": hint, "url": u})
        if len(low) >= 2:
            like = f"%{q.replace('%', '').replace('_', '')}%"
            cond, args = A.packet_scope()
            cols = {r[1] for r in db().execute("PRAGMA table_info(packets)")}
            extra = " OR so_number LIKE ?" if "so_number" in cols else ""
            rows = db().execute(f"SELECT * FROM packets WHERE {cond} AND (client_name LIKE ? OR client_email LIKE ?{extra} OR id IN "
                                f"(SELECT packet_id FROM packet_items WHERE template_name LIKE ?)) "
                                f"ORDER BY COALESCE(sent_at, created_at) DESC LIMIT 8",
                                args + [like, like] + ([like] if extra else []) + [like]).fetchall()
            seen = set()
            for p in rows:
                st = A.effective_status(p)
                label = {"draft": "Draft", "sent": "Sent", "viewed": "Opened", "in_progress": "In progress", "completed": "Signed",
                         "expired": "Expired", "voided": "Cancelled"}.get(st, st)
                so = p["so_number"] if "so_number" in p.keys() and p["so_number"] else ""
                out.append({"kind": "Packet", "label": p["client_name"], "url": url_for("packet_detail", pid=p["id"]),
                            "hint": " · ".join(x for x in (label, f"SO# {so}" if so else "", A.app.jinja_env.filters["day"](p["sent_at"] or p["created_at"])) if x)})
                key = (p["client_name"].lower(), (p["client_email"] or "").lower())
                if key not in seen and len(seen) < 3 and low in p["client_name"].lower():
                    seen.add(key)
                    out.append({"kind": "Action", "label": f"New packet for {p['client_name']}",
                                "hint": p["client_email"] or "", "url": url_for("new_packet", client=p["client_name"], email=p["client_email"] or "")})
            for t in db().execute("SELECT id, name, kind FROM templates WHERE company_id=? AND archived=0 AND name LIKE ? ORDER BY name LIMIT 5",
                                  (A.cid(), like)):
                out.append({"kind": "Form", "label": A.daily.pretty_name(t["name"]), "hint": "Form library",
                            "url": url_for("library", q=t["name"])})
            if manager:
                for u in db().execute("SELECT id, name, email FROM users WHERE company_id=? AND (name LIKE ? OR email LIKE ?) LIMIT 4",
                                      (A.cid(), like, like)):
                    out.append({"kind": "Person", "label": u["name"], "hint": u["email"] + " · Team", "url": url_for("team")})
        if not low:
            out.append({"kind": "Action", "label": "New packet", "hint": "Start a new packet", "url": url_for("new_packet")})
        return jsonify({"q": q, "results": out[:20]})

    # ================================================================ 11 installable app
    @app.route("/manifest.webmanifest")
    def upkeep_manifest():
        name = A.PLATFORM_NAME
        body = {"name": name, "short_name": name[:12], "start_url": "/?source=app", "scope": "/", "display": "standalone",
                "background_color": "#f6f7f9", "theme_color": "#24408e",
                "description": "Send forms for signature and see who still needs to sign.",
                "icons": [{"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                          {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
                          {"src": "/static/icon-maskable.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}],
                "shortcuts": [{"name": "New packet", "url": "/packets/new"}, {"name": "Follow-up", "url": "/follow-up"}]}
        return Response(json.dumps(body), mimetype="application/manifest+json", headers={"Cache-Control": "public, max-age=3600"})

    @app.route("/sw.js")
    def upkeep_sw():
        version = getattr(A.daily, "APP_VERSION", "1")
        return Response(SW % {"version": version}, mimetype="application/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @app.route("/offline")
    def upkeep_offline():
        return render_template("offline.html")

    @app.route("/apple-touch-icon.png")
    def upkeep_apple_icon():
        return send_from_directory(os.path.join(os.path.dirname(os.path.abspath(A.__file__)), "static"), "apple-touch-icon.png")

    # ================================================================ 15 retention rules
    @app.route("/company/retention", methods=["POST"])
    @A.login_required
    def company_retention():
        if not A.is_manager():
            abort(403)
        try:
            months = int(request.form.get("archive_months", "0"))
            years = int(request.form.get("delete_years", "0"))
        except ValueError:
            months = years = 0
        months = months if months in ARCHIVE_CHOICES else 0
        years = years if years in DELETE_CHOICES else 0
        c = g.company
        if years and years != (c.get("retain_delete_years") or 0) and request.form.get("understand") != "on":
            flash("Tick the box to confirm you understand packets will be deleted for good. Nothing was changed.", "error")
            return redirect(url_for("company_settings") + "#retention")
        db().execute("UPDATE companies SET retain_archive_months=?, retain_delete_years=?, retain_set_by=?, retain_set_at=? WHERE id=?",
                     (months, years, A.actor(), A.now(), A.cid()))
        db().commit()
        msg = []
        msg.append(f"Finished packets are archived after {months} months." if months else "Finished packets aren't archived automatically.")
        msg.append(f"They're deleted for good {years} year{'s' if years != 1 else ''} after they finished." if years else "Nothing is deleted automatically.")
        flash(" ".join(msg) + " The rules run once a day.", "ok")
        return redirect(url_for("company_settings") + "#retention")

    def finished_at(p):
        return p["completed_at"] or p["voided_at"]

    def due_for_delete(company_id, years, within_days=0):
        cutoff = (utcnow() - timedelta(days=365 * years) + timedelta(days=within_days)).isoformat()
        return db().execute("SELECT * FROM packets WHERE company_id=? AND COALESCE(completed_at, voided_at) IS NOT NULL "
                            "AND COALESCE(completed_at, voided_at) < ? ORDER BY COALESCE(completed_at, voided_at)",
                            (company_id, cutoff)).fetchall()

    def retention_info(company_id):
        c = db().execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
        years = (c["retain_delete_years"] or 0) if c else 0
        soon = []
        if years:
            for p in due_for_delete(company_id, years, within_days=30)[:50]:
                when = (datetime.fromisoformat(finished_at(p)) + timedelta(days=365 * years))
                soon.append({"ref": A.packet_ref(p), "client": p["client_name"], "when": when.isoformat(), "url": url_for("packet_detail", pid=p["id"])})
        log = [dict(r) for r in db().execute("SELECT * FROM retention_log WHERE company_id=? ORDER BY id DESC LIMIT 20", (company_id,))]
        total = db().execute("SELECT COUNT(*) FROM retention_log WHERE company_id=?", (company_id,)).fetchone()[0]
        return {"months": (c["retain_archive_months"] or 0) if c else 0, "years": years, "soon": soon, "log": log, "log_total": total,
                "set_by": (c["retain_set_by"] or "") if c else "", "set_at": c["retain_set_at"] if c else None,
                "archive_choices": ARCHIVE_CHOICES, "delete_choices": DELETE_CHOICES}

    app.jinja_env.globals["retention_info"] = retention_info

    def referenced(path_value, skip_packet):
        """True if a file is still used by a library form or another packet (library files are shared)."""
        if db().execute("SELECT 1 FROM templates WHERE file_path=? LIMIT 1", (path_value,)).fetchone():
            return True
        return bool(db().execute("SELECT 1 FROM packet_items WHERE packet_id != ? AND (template_file=? OR signed_pdf_path=? "
                                 "OR signature_path=?) LIMIT 1", (skip_packet, path_value, path_value, path_value)).fetchone())

    def remove(folder, name):
        if not name:
            return 0
        path = os.path.join(folder, os.path.basename(name))
        try:
            os.remove(path)
            return 1
        except OSError:
            return 0

    def delete_packet(p, rule):
        """Deletes one finished packet and its files for good, and logs it (reference and dates only)."""
        pid, n = p["id"], 0
        items = db().execute("SELECT * FROM packet_items WHERE packet_id=?", (pid,)).fetchall()
        for it in items:
            for f, folder in ((it["template_file"], A.UPLOAD_DIR), (it["signed_pdf_path"], A.SIGNED_DIR), (it["signature_path"], A.SIGNED_DIR)):
                if f and not referenced(f, pid):
                    n += remove(folder, f)
                    stem = os.path.splitext(os.path.basename(f))[0]
                    n += remove(A.UPLOAD_DIR, f"disp-{stem}.jpg")
                    try:
                        for pg in os.listdir(A.PAGE_DIR):
                            if pg.startswith(stem + "-"):
                                n += remove(A.PAGE_DIR, pg)
                    except OSError:
                        pass
            try:
                for o in json.loads(it["config_json"] or "{}").get("options", []):
                    for f in (o.get("file"), o.get("display")):
                        if f and not referenced(f, pid):
                            n += remove(A.UPLOAD_DIR, f)
            except (ValueError, AttributeError, KeyError, IndexError):
                pass
            try:
                for cs in db().execute("SELECT signature_path FROM cosignatures WHERE item_id=?", (it["id"],)):
                    n += remove(A.SIGNED_DIR, cs[0])
                db().execute("DELETE FROM cosignatures WHERE item_id=?", (it["id"],))
            except Exception:  # noqa: BLE001  (edition without second owners)
                pass
            db().execute("DELETE FROM form_drafts WHERE item_id=?", (it["id"],)) if _has("form_drafts") else None
        if _has("client_files"):
            for f in db().execute("SELECT stored FROM client_files WHERE packet_id=?", (pid,)):
                n += remove(os.path.join(A.DATA_DIR, "client_uploads"), f[0])
            db().execute("DELETE FROM client_files WHERE packet_id=?", (pid,))
        for table in ("packet_signers", "events", "packet_items"):
            if _has(table):
                db().execute(f"DELETE FROM {table} WHERE packet_id=?", (pid,))
        db().execute("DELETE FROM packets WHERE id=?", (pid,))
        db().execute("INSERT INTO retention_log (company_id, ref, created_at, finished_at, forms, files, deleted_at, rule) VALUES (?,?,?,?,?,?,?,?)",
                     (p["company_id"], A.packet_ref(p), p["created_at"], finished_at(p), len(items), n, A.now(), rule))
        return n

    _tables = {}

    def _has(table):
        if table not in _tables:
            _tables[table] = bool(db().execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())
        return _tables[table]

    def run_retention():
        """Archive and delete by each company's rules. Returns {company_id: (archived, deleted)}."""
        out = {}
        now = utcnow()
        for c in db().execute("SELECT * FROM companies WHERE COALESCE(retain_archive_months,0) > 0 OR COALESCE(retain_delete_years,0) > 0").fetchall():
            arch = dele = 0
            months, years = c["retain_archive_months"] or 0, c["retain_delete_years"] or 0
            if months:
                cutoff = (now - timedelta(days=round(30.44 * months))).isoformat()
                rows = db().execute("SELECT id FROM packets WHERE company_id=? AND archived_at IS NULL AND COALESCE(completed_at, voided_at) IS NOT NULL "
                                    "AND COALESCE(completed_at, voided_at) < ?", (c["id"], cutoff)).fetchall()
                for r in rows:
                    db().execute("UPDATE packets SET archived_at=? WHERE id=?", (A.now(), r[0]))
                    db().execute("INSERT INTO events (packet_id, type, detail, ip, at) VALUES (?,?,?,?,?)",   # no request here
                                 (r[0], "archived", f"Archived automatically ({months}-month rule)", "", A.now()))
                arch = len(rows)
            if years:
                for p in due_for_delete(c["id"], years):
                    delete_packet(p, f"{years}-year rule")
                    dele += 1
            db().commit()
            if arch or dele:
                out[c["id"]] = (arch, dele)
        return out

    A.run_retention = run_retention
    state = {"thread": None}

    def _loop():
        time.sleep(60)
        while True:
            try:
                lockf = open(os.path.join(A.DATA_DIR, ".retention.lock"), "w")
                try:
                    fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    lockf.close()
                    lockf = None
                if lockf:
                    stamp = os.path.join(A.DATA_DIR, ".retention.last")
                    last = os.path.getmtime(stamp) if os.path.exists(stamp) else 0
                    if time.time() - last > 20 * 3600:
                        with app.app_context():
                            done = run_retention()
                        open(stamp, "w").write(json.dumps({"at": A.now(), "done": {str(k): v for k, v in done.items()}}))
                    fcntl.flock(lockf, fcntl.LOCK_UN)
                    lockf.close()
            except Exception:  # noqa: BLE001
                app.logger.exception("Retention check failed")
            time.sleep(3600)

    @app.before_request
    def start_retention_thread():
        if state["thread"] is None and not app.config.get("EXTRAS_NO_THREAD") and not app.config.get("TESTING"):
            state["thread"] = threading.Thread(target=_loop, daemon=True, name="retention")
            state["thread"].start()
