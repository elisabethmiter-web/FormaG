"""Packet workflow features:

* two owners      a packet can need a second signer: each owner gets their own link, and the packet only
                  completes when both have signed every form (the second owner signs after the first)
* client uploads  ask the client for photos or files (optional for the client; never blocks completion)
* bundles         saved sets of library forms, picked in one click on the New packet page
* in person       "Sign now on this device": the client signs on the staff member's tablet; the device is
                  locked to signing until staff enter their password
* placed boxes    staff place signature, name and date boxes on a library PDF's pages
* export          download packets as an Excel spreadsheet
"""
import base64
import hashlib
import io
import json
import mimetypes
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

from flask import abort, flash, g, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

import sms

UPLOAD_MAX_BYTES = 25 * 1024 * 1024
UPLOAD_MAX_FILES = 30
UPLOAD_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".pdf", ".doc", ".docx", ".xls",
              ".xlsx", ".csv", ".txt", ".dwg", ".dxf", ".rtf", ".odt", ".ods", ".mp4", ".mov"}
BOX_KINDS = {"signature": "Signature", "name": "Printed name", "date": "Date"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS packet_signers (
  id INTEGER PRIMARY KEY,
  packet_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  email TEXT DEFAULT '',
  phone TEXT DEFAULT '',
  token TEXT UNIQUE NOT NULL,
  created_at TEXT NOT NULL,
  sent_at TEXT,
  first_viewed_at TEXT,
  completed_at TEXT,
  turn_notified_at TEXT,
  reminder_count INTEGER DEFAULT 0,
  last_reminder_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_signers_packet ON packet_signers(packet_id);
CREATE TABLE IF NOT EXISTS cosignatures (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL,
  signer_id INTEGER NOT NULL,
  signer_name TEXT NOT NULL,
  signature_path TEXT NOT NULL,
  signed_at TEXT NOT NULL,
  ip TEXT,
  user_agent TEXT,
  UNIQUE (item_id, signer_id)
);
CREATE TABLE IF NOT EXISTS client_files (
  id INTEGER PRIMARY KEY,
  packet_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  stored TEXT NOT NULL,
  mime TEXT DEFAULT '',
  size INTEGER DEFAULT 0,
  sha256 TEXT DEFAULT '',
  uploaded_at TEXT NOT NULL,
  ip TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_client_files_packet ON client_files(packet_id);
CREATE TABLE IF NOT EXISTS bundles (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  template_ids_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);
"""
COLUMNS = {
    "packets": {"upload_request": "TEXT DEFAULT ''"},
    "templates": {"placements_json": "TEXT DEFAULT '[]'"},
    "packet_items": {"placements_json": "TEXT DEFAULT '[]'"},
}


def init_db(conn):
    conn.executescript(SCHEMA)
    for table, cols in COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, spec in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {spec}")


def clean_placements(raw, pages):
    """Validated signature/name/date boxes: [{page, kind, signer, x, y, w, h}] with x/y/w/h as fractions of the
    page (from its top-left corner). `pages` is the page count."""
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    if not isinstance(data, list) or len(data) > 80:
        return None
    out = []
    for b in data:
        try:
            box = {"page": int(b["page"]), "kind": str(b["kind"]), "signer": int(b.get("signer", 1)),
                   "x": round(float(b["x"]), 5), "y": round(float(b["y"]), 5),
                   "w": round(float(b["w"]), 5), "h": round(float(b["h"]), 5)}
        except (KeyError, TypeError, ValueError):
            return None
        if (box["kind"] not in BOX_KINDS or box["signer"] not in (1, 2) or not 0 <= box["page"] < pages
                or not (0 <= box["x"] <= 1 and 0 <= box["y"] <= 1 and 0.005 <= box["w"] <= 1 and 0.005 <= box["h"] <= 1)
                or box["x"] + box["w"] > 1.001 or box["y"] + box["h"] > 1.001):
            return None
        out.append(box)
    return out


def register(A):
    app, db = A.app, A.db
    files_dir = os.path.join(A.DATA_DIR, "client_uploads")
    os.makedirs(files_dir, exist_ok=True)

    # ------------------------------------------------ helpers
    def cosigners(pid):
        return [dict(r) for r in db().execute("SELECT * FROM packet_signers WHERE packet_id=? ORDER BY id", (pid,))]

    def sign_items(pid):
        return db().execute("SELECT * FROM packet_items WHERE packet_id=? AND requires_signature=1 ORDER BY position",
                            (pid,)).fetchall()

    def cosigned_ids(signer_id):
        return {r[0] for r in db().execute("SELECT item_id FROM cosignatures WHERE signer_id=?", (signer_id,))}

    def client_uploads(pid):
        return [dict(r) for r in db().execute("SELECT * FROM client_files WHERE packet_id=? ORDER BY id", (pid,))]

    def first_name(name):
        return (name or "").split(" ")[0] or name

    def packet_extras(p):
        """Everything the packet pages show about second owners, client files and versions."""
        pid = p["id"]
        items = sign_items(pid)
        cos = []
        for s in cosigners(pid):
            done = cosigned_ids(s["id"])
            ready = [i for i in items if i["signed_at"] and i["id"] not in done]
            cos.append({**s, "signed": len([i for i in items if i["id"] in done]), "total": len(items),
                        "ready": len(ready), "link": cosign_url(s["token"])})
        ups = client_uploads(pid)
        primary_done = bool(items) and all(i["signed_at"] for i in items)
        waiting = []
        for s in cos:
            if s["signed"] < s["total"]:
                waiting.append(f"{s['name']} to sign")
        return {"cosigners": cos, "uploads": ups, "primary_done": primary_done, "waiting": waiting,
                "upload_request": p["upload_request"] or ""}

    def cosign_url(token):
        base = A.BASE_URL or (request.url_root.rstrip("/") if request else "")
        return f"{base}/c/{token}"

    @app.context_processor
    def inject_workflow():
        k = session.get("kiosk") if request else None
        return {"packet_extras": packet_extras, "kiosk": k, "box_kinds": BOX_KINDS}

    def client_upload_bytes(company_id):
        r = db().execute("SELECT COALESCE(SUM(f.size),0) FROM client_files f JOIN packets p ON p.id=f.packet_id "
                         "WHERE p.company_id=?", (company_id,)).fetchone()
        return r[0] or 0

    A.client_upload_bytes = client_upload_bytes

    # ------------------------------------------------ completion
    def maybe_complete(pid):
        """Complete the packet once the client and every second owner have signed everything.
        Returns True when the packet is (now) complete."""
        p = db().execute("SELECT * FROM packets WHERE id=?", (pid,)).fetchone()
        if p["completed_at"]:
            return True
        signed, total = A.packet_progress(pid)
        if total == 0 or signed < total:
            return False
        items = [i["id"] for i in sign_items(pid)]
        pending = [s for s in cosigners(pid) if not set(items) <= cosigned_ids(s["id"])]
        if pending:
            for s in pending:   # the first owner has finished: tell the second owner it's their turn (once)
                if s["turn_notified_at"]:
                    continue
                n = db().execute("UPDATE packet_signers SET turn_notified_at=? WHERE id=? AND turn_notified_at IS NULL",
                                 (A.now(), s["id"])).rowcount
                db().commit()
                if n and session.get("kiosk", {}).get("pid") != pid:
                    notify_cosigner(p, s, turn=True)
            return False
        A.complete_packet(pid)
        return True

    A.maybe_complete = maybe_complete

    # ------------------------------------------------ sending to the second owner
    def notify_cosigner(p, s, via=("email", "text"), turn=False, quiet=True):
        """Email and/or text the second owner their own link. Returns the channels used."""
        company = A.get_company(p["company_id"])
        link = cosign_url(s["token"])
        names = [i["template_name"] for i in sign_items(p["id"])]
        used = []
        if "email" in via and A.mailer.enabled() and "@" in (s["email"] or ""):
            try:
                msg = p["message"] or ""
                if turn:
                    msg = f"{first_name(p['client_name'])} has signed. It's your turn to sign as the second owner."
                A.mailer.send_request(s["email"], s["name"], company["name"], names, link, msg, p["expires_at"],
                                      reply_to=(A.staff_recipients(p) or [None])[0],
                                      brand=A.extras.brand_of(company, A.BASE_URL or request.url_root.rstrip("/")))
                used.append("email")
            except Exception as exc:  # noqa: BLE001
                A.log_event(p["id"], "email_failed", f"to {s['name']}: {str(exc)[:160]}")
                if not quiet:
                    flash(f"The email to {s['name']} didn't go out ({exc}).", "error")
        if "text" in via and sms.enabled() and s["phone"]:
            try:
                body = (f"{company['name']}: Hi {first_name(s['name'])}, "
                        + ("it's your turn to sign: " if turn else "please review and sign as the second owner: ") + link)
                sms.send(s["phone"], body)
                used.append("text")
            except RuntimeError as exc:
                A.log_event(p["id"], "text_failed", f"to {s['name']}: {str(exc)[:160]}")
                if not quiet:
                    flash(f"The text to {s['name']} didn't go out ({exc}).", "error")
        if used:
            db().execute("UPDATE packet_signers SET sent_at=COALESCE(sent_at, ?), last_reminder_at=? WHERE id=?",
                         (A.now(), A.now(), s["id"]))
            A.log_event(p["id"], "sent", f"{'turn notice' if turn else 'link'} to {s['name']} (second owner) by "
                                         f"{' and '.join(used)}")
        db().commit()
        return used

    def notify_cosigners(pid, via):
        p = db().execute("SELECT * FROM packets WHERE id=?", (pid,)).fetchone()
        for s in cosigners(pid):
            if not s["completed_at"]:
                notify_cosigner(p, s, via=(via,))

    A.notify_cosigners = notify_cosigners

    # ------------------------------------------------ new packet: checks and extras
    def validate_packet_extras(form):
        """Problems with the new-packet form's extra parts (second owner, files), or None."""
        if form.get("signers") == "2":
            name = form.get("cosigner_name", "").strip()
            email = form.get("cosigner_email", "").strip()
            phone = form.get("cosigner_phone", "").strip()
            if not name:
                return "Add the second owner's name, or choose “One client signs”."
            if email and "@" not in email:
                return "The second owner's email doesn't look right."
            if phone and not sms.normalize(phone):
                return "The second owner's mobile number doesn't look right. Use 10 digits, like 613-555-0123."
            if not email and not phone and form.get("action") != "in_person":
                return "Add the second owner's email (or mobile number) so they get their own link."
            if email and email.lower() == form.get("client_email", "").strip().lower():
                return "Each owner needs their own email address, so each gets their own signing link."
        if form.get("ask_files") == "on" and not form.get("upload_request", "").strip():
            return "Say which files or photos the client should send."
        return None

    A.validate_packet_extras = validate_packet_extras

    def after_packet_created(pid):
        """Called by New packet after the packet and its forms are saved (same database transaction)."""
        form = request.form
        # signature boxes placed on library PDFs travel with the packet
        db().execute("UPDATE packet_items SET placements_json=(SELECT COALESCE(t.placements_json,'[]') FROM templates t "
                     "WHERE t.id=packet_items.template_id) WHERE packet_id=? AND template_id IS NOT NULL", (pid,))
        if form.get("signers") == "2" and form.get("cosigner_name", "").strip():
            phone = form.get("cosigner_phone", "").strip()
            db().execute("INSERT INTO packet_signers (packet_id, name, email, phone, token, created_at) VALUES (?,?,?,?,?,?)",
                         (pid, form.get("cosigner_name", "").strip()[:120], form.get("cosigner_email", "").strip()[:120],
                          sms.normalize(phone) or "" if phone else "", secrets.token_urlsafe(24), A.now()))
            A.log_event(pid, "details", f"two owners sign: second owner {form.get('cosigner_name', '').strip()[:120]}")
        if form.get("ask_files") == "on":
            db().execute("UPDATE packets SET upload_request=? WHERE id=?", (form.get("upload_request", "").strip()[:500], pid))

    A.after_packet_created = after_packet_created

    def packet_form_defaults():
        """Empty New packet form."""
        return {}

    A.packet_form_defaults = packet_form_defaults

    def bundle_selection(pre):
        b = request.args.get("bundle", "")
        if b.isdigit():
            row = db().execute("SELECT template_ids_json FROM bundles WHERE id=? AND company_id=?", (int(b), A.cid())).fetchone()
            if row:
                return [int(x) for x in json.loads(row[0] or "[]")]
        return pre

    A.bundle_selection = bundle_selection

    def bundles_for(company_id):
        out = []
        for r in db().execute("SELECT * FROM bundles WHERE company_id=? ORDER BY name COLLATE NOCASE", (company_id,)):
            ids = [int(x) for x in json.loads(r["template_ids_json"] or "[]")]
            live = {t[0]: t[1] for t in db().execute(
                f"SELECT id, name FROM templates WHERE archived=0 AND company_id=? AND id IN ({','.join('?' * len(ids)) or 'NULL'})",
                [company_id] + ids)}
            out.append({"id": r["id"], "name": r["name"], "ids": [i for i in ids if i in live],
                        "names": [live[i] for i in ids if i in live]})
        return out

    app.jinja_env.globals["bundles_for"] = bundles_for
    app.jinja_env.globals["current_company_id"] = lambda: g.company["id"] if g.get("company") else 0
    app.jinja_env.globals["effective_status"] = A.effective_status

    # ------------------------------------------------ second owner: signing pages
    def load_cosigner(token):
        s = db().execute("SELECT * FROM packet_signers WHERE token=?", (token,)).fetchone() or abort(404)
        p = db().execute("SELECT * FROM packets WHERE id=?", (s["packet_id"],)).fetchone() or abort(404)
        g.company = A.get_company(p["company_id"])
        return dict(s), p

    @app.route("/c/<token>")
    def cosign_packet(token):
        s, p = load_cosigner(token)
        blocked = A.client_block(p)
        if blocked:
            return blocked
        if not s["first_viewed_at"]:
            db().execute("UPDATE packet_signers SET first_viewed_at=? WHERE id=?", (A.now(), s["id"]))
            A.log_event(p["id"], "viewed", f"by {s['name']} (second owner)")
            db().commit()
        items = sign_items(p["id"])
        done = cosigned_ids(s["id"])
        rows = [{"it": i, "done": i["id"] in done, "ready": bool(i["signed_at"]) and i["id"] not in done} for i in items]
        nxt = next((r["it"] for r in rows if r["ready"]), None)
        return render_template("cosign_packet.html", p=p, s=s, rows=rows, next_item=nxt,
                               all_done=all(r["done"] for r in rows) and bool(rows))

    def cosign_signers(p, it, s_row, extra=None):
        """Both owners' signatures for one item, first owner first."""
        signers = [{"name": it["signer_name"], "sig_path": os.path.join(A.SIGNED_DIR, it["signature_path"]),
                    "signed_at": it["signed_at"], "ip": it["ip"], "user_agent": it["user_agent"],
                    "label": "Owner 1 signature", "email": p["client_email"]}]
        for c in db().execute("SELECT c.*, s.email FROM cosignatures c JOIN packet_signers s ON s.id=c.signer_id "
                              "WHERE c.item_id=? ORDER BY c.id", (it["id"],)):
            signers.append({"name": c["signer_name"], "sig_path": os.path.join(A.SIGNED_DIR, c["signature_path"]),
                            "signed_at": c["signed_at"], "ip": c["ip"], "user_agent": c["user_agent"],
                            "label": f"Owner {len(signers) + 1} signature", "email": c["email"]})
        return signers

    @app.route("/c/<token>/i/<int:iid>", methods=["GET", "POST"])
    def cosign_item(token, iid):
        s, p = load_cosigner(token)
        blocked = A.client_block(p)
        if blocked:
            return blocked
        it = db().execute("SELECT * FROM packet_items WHERE id=? AND packet_id=? AND requires_signature=1",
                          (iid, p["id"])).fetchone() or abort(404)
        if iid in cosigned_ids(s["id"]):
            return redirect(url_for("cosign_packet", token=token))
        if not it["signed_at"]:
            flash(f"{first_name(p['client_name'])} hasn't signed “{it['template_name']}” yet. You'll sign it after them.", "error")
            return redirect(url_for("cosign_packet", token=token))
        errors = {}
        if request.method == "POST":
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
                ts = A.now()
                sig_name = f"cosig-{p['id']}-{iid}-{secrets.token_hex(4)}.png"
                with open(os.path.join(A.SIGNED_DIR, sig_name), "wb") as fh:
                    fh.write(sig_bytes)
                n = db().execute("INSERT OR IGNORE INTO cosignatures (item_id, signer_id, signer_name, signature_path, "
                                 "signed_at, ip, user_agent) VALUES (?,?,?,?,?,?,?)",
                                 (iid, s["id"], signer, sig_name, ts, A.client_ip(),
                                  request.headers.get("User-Agent", "")[:300])).rowcount
                if n:
                    try:
                        answers = json.loads(it["answers_json"] or "{}")
                    except ValueError:
                        answers = {}
                    out_name, digest = A.render_item_pdf(p, it, answers, cosign_signers(p, it, s),
                                                         method=A.signing_method(p))
                    old = it["signed_pdf_path"]
                    db().execute("UPDATE packet_items SET signed_pdf_path=?, signed_sha256=? WHERE id=?",
                                 (out_name, digest, iid))
                    A.log_event(p["id"], "signed", f"{it['template_name']} — signed by {signer} (second owner)")
                    left = [i for i in sign_items(p["id"]) if i["id"] not in cosigned_ids(s["id"])]
                    if not left:
                        db().execute("UPDATE packet_signers SET completed_at=? WHERE id=?", (A.now(), s["id"]))
                    db().commit()
                    if old and old != out_name:
                        try:
                            os.remove(os.path.join(A.SIGNED_DIR, os.path.basename(old)))
                        except OSError:
                            pass
                    if maybe_complete(p["id"]):
                        return redirect(url_for("cosign_done", token=token))
                flash(f"“{it['template_name']}” signed.", "ok")
                nxt = next((i for i in sign_items(p["id"]) if i["signed_at"] and i["id"] not in cosigned_ids(s["id"])), None)
                if nxt:
                    return redirect(url_for("cosign_item", token=token, iid=nxt["id"]))
                return redirect(url_for("cosign_packet", token=token))
        pages = 0
        try:
            pages = min(A.preview.page_count(os.path.join(A.SIGNED_DIR, it["signed_pdf_path"])), 200)
        except Exception:  # noqa: BLE001
            app.logger.exception("Could not count pages of %s", it["signed_pdf_path"])
        items = sign_items(p["id"])
        return render_template("cosign_form.html", p=p, s=s, it=it, pages=pages, errors=errors,
                               items=items, done=cosigned_ids(s["id"]),
                               signer_default=request.form.get("signer_name", s["name"]))

    @app.route("/c/<token>/page/<int:iid>/<int:n>.png")
    def cosign_page(token, iid, n):
        s, p = load_cosigner(token)
        if A.client_block(p):
            abort(410)
        it = db().execute("SELECT * FROM packet_items WHERE id=? AND packet_id=?", (iid, p["id"])).fetchone() or abort(404)
        if not it["signed_pdf_path"]:
            abort(404)
        src = os.path.join(A.SIGNED_DIR, it["signed_pdf_path"])
        if not 0 <= n < A.preview.page_count(src):
            abort(404)
        out = os.path.join(A.PAGE_DIR, f"signed-{os.path.splitext(it['signed_pdf_path'])[0]}-{n}.png")
        A.preview.page_png(src, n, out)
        return send_file(out, mimetype="image/png", max_age=3600)

    @app.route("/c/<token>/signed/<int:iid>")
    def cosign_signed(token, iid):
        s, p = load_cosigner(token)
        it = db().execute("SELECT * FROM packet_items WHERE id=? AND packet_id=?", (iid, p["id"])).fetchone() or abort(404)
        if not it["signed_pdf_path"] or (iid not in cosigned_ids(s["id"]) and not p["completed_at"]):
            abort(404)
        return send_file(os.path.join(A.SIGNED_DIR, it["signed_pdf_path"]), mimetype="application/pdf", as_attachment=True,
                         download_name=f"{secure_filename(it['template_name'])}-signed.pdf")

    @app.route("/c/<token>/done")
    def cosign_done(token):
        s, p = load_cosigner(token)
        items = sign_items(p["id"])
        return render_template("cosign_done.html", p=p, s=s, items=items, done=cosigned_ids(s["id"]))

    # ------------------------------------------------ staff: second owner, files, versions
    @app.route("/packets/<int:pid>/cosigner/<int:sid>/send", methods=["POST"])
    @A.login_required
    def cosigner_send(pid, sid):
        p = db().execute("SELECT * FROM packets WHERE id=?", (pid,)).fetchone()
        s = db().execute("SELECT * FROM packet_signers WHERE id=? AND packet_id=?", (sid, pid)).fetchone() or abort(404)
        via = request.form.get("via", "email")
        used = notify_cosigner(p, dict(s), via=(via,), quiet=False)
        if used:
            flash(f"{'Emailed' if via == 'email' else 'Texted'} {s['name']} their signing link.", "ok")
        elif via == "email" and not (A.mailer.enabled() and "@" in (s["email"] or "")):
            flash(f"Add an email for {s['name']} (or turn on email) first. You can also copy their link.", "error")
        elif via == "text" and not (sms.enabled() and s["phone"]):
            flash(f"Add a mobile number for {s['name']} (or turn on texting) first.", "error")
        return redirect(url_for("packet_detail", pid=pid) + "#owners")

    @app.route("/packets/<int:pid>/cosigner/<int:sid>/edit", methods=["POST"])
    @A.login_required
    def cosigner_edit(pid, sid):
        s = db().execute("SELECT * FROM packet_signers WHERE id=? AND packet_id=?", (sid, pid)).fetchone() or abort(404)
        email = request.form.get("email", "").strip()[:120]
        phone_raw = request.form.get("phone", "").strip()
        phone = sms.normalize(phone_raw) if phone_raw else ""
        if email and "@" not in email:
            flash("That email doesn't look right.", "error")
        elif phone_raw and not phone:
            flash("That mobile number doesn't look right. Use 10 digits, like 613-555-0123.", "error")
        else:
            db().execute("UPDATE packet_signers SET email=?, phone=? WHERE id=?", (email, phone, sid))
            A.log_event(pid, "details", f"{s['name']}'s contact details updated by {A.actor()}")
            db().commit()
            flash("Saved.", "ok")
        return redirect(url_for("packet_detail", pid=pid) + "#owners")

    @app.route("/packets/<int:pid>/files-request", methods=["POST"])
    @A.login_required
    def files_request(pid):
        text = request.form.get("upload_request", "").strip()[:500]
        db().execute("UPDATE packets SET upload_request=? WHERE id=?", (text, pid))
        A.log_event(pid, "details", (f"asked the client for files: {text}" if text else "stopped asking for files")
                    + f" · by {A.actor()}")
        db().commit()
        flash("Saved. Your client sees the request on their signing page." if text else "File request removed.", "ok")
        return redirect(url_for("packet_detail", pid=pid) + "#client-files")

    @app.route("/packets/<int:pid>/client-files/<int:fid>")
    @A.login_required
    def client_file_download(pid, fid):
        f = db().execute("SELECT * FROM client_files WHERE id=? AND packet_id=?", (fid, pid)).fetchone() or abort(404)
        inline = (f["mime"] or "").startswith("image/") or f["mime"] == "application/pdf"
        return send_file(os.path.join(files_dir, f["stored"]), mimetype=f["mime"] or "application/octet-stream",
                         as_attachment=not inline, download_name=f["name"])

    # ------------------------------------------------ client: uploads
    @app.route("/s/<token>/upload", methods=["POST"])
    def client_upload(token):
        p = A.load_packet(token)
        blocked = A.client_block(p)
        if blocked:
            return blocked
        files = [f for f in request.files.getlist("files") if f and f.filename]
        have = len(client_uploads(p["id"]))
        saved, problems = [], []
        for f in files:
            name = secure_filename(f.filename) or "file"
            ext = os.path.splitext(name)[1].lower()
            if have + len(saved) >= UPLOAD_MAX_FILES:
                problems.append(f"{f.filename}: at most {UPLOAD_MAX_FILES} files per packet")
                continue
            if ext not in UPLOAD_EXT:
                problems.append(f"{f.filename}: this type of file can't be added (photos, PDF, Word, Excel or drawings work)")
                continue
            data = f.read(UPLOAD_MAX_BYTES + 1)
            if len(data) > UPLOAD_MAX_BYTES:
                problems.append(f"{f.filename}: over 25 MB")
                continue
            if not data:
                continue
            stored = f"{p['id']}-{secrets.token_hex(8)}{ext}"
            with open(os.path.join(files_dir, stored), "wb") as fh:
                fh.write(data)
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            db().execute("INSERT INTO client_files (packet_id, name, stored, mime, size, sha256, uploaded_at, ip) "
                         "VALUES (?,?,?,?,?,?,?,?)", (p["id"], f.filename[:200], stored, mime, len(data),
                                                     hashlib.sha256(data).hexdigest(), A.now(), A.client_ip()))
            saved.append(f.filename[:80])
        if saved:
            A.log_event(p["id"], "uploaded", f"{len(saved)} file{'s' if len(saved) != 1 else ''}: {', '.join(saved)[:180]}")
        db().commit()
        for msg in problems[:5]:
            flash(msg, "error")
        if saved:
            flash(f"Thanks, {len(saved)} file{'s' if len(saved) != 1 else ''} added.", "ok")
        elif not problems:
            flash("Choose at least one file first.", "error")
        return redirect(url_for("client_done" if p["completed_at"] else "client_packet", token=token) + "#files")

    @app.route("/s/<token>/upload/<int:fid>/delete", methods=["POST"])
    def client_upload_delete(token, fid):
        p = A.load_packet(token)
        if A.client_block(p):
            abort(410)
        f = db().execute("SELECT * FROM client_files WHERE id=? AND packet_id=?", (fid, p["id"])).fetchone() or abort(404)
        db().execute("DELETE FROM client_files WHERE id=?", (fid,))
        A.log_event(p["id"], "uploaded", f"client removed {f['name'][:120]}")
        db().commit()
        try:
            os.remove(os.path.join(files_dir, f["stored"]))
        except OSError:
            pass
        flash(f"Removed {f['name']}.", "ok")
        return redirect(url_for("client_done" if p["completed_at"] else "client_packet", token=token) + "#files")

    # ------------------------------------------------ bundles
    def bundle_ids_from_form():
        valid = {r[0] for r in db().execute("SELECT id FROM templates WHERE archived=0 AND company_id=?", (A.cid(),))}
        return [int(x) for x in request.form.getlist("template_ids") if x.isdigit() and int(x) in valid]

    @app.route("/library/bundles", methods=["POST"])
    @A.manager_required
    def bundle_create():
        name = request.form.get("name", "").strip()[:80]
        ids = bundle_ids_from_form()
        if not name or not ids:
            flash("Give the bundle a name and tick at least one form.", "error")
        else:
            db().execute("INSERT INTO bundles (company_id, name, template_ids_json, created_at) VALUES (?,?,?,?)",
                         (A.cid(), name, json.dumps(ids), A.now()))
            db().commit()
            flash(f"Bundle “{name}” saved. Pick it at the top of the New packet page.", "ok")
        return redirect(url_for("library") + "#bundles")

    @app.route("/library/bundles/<int:bid>", methods=["POST"])
    @A.manager_required
    def bundle_update(bid):
        b = db().execute("SELECT * FROM bundles WHERE id=? AND company_id=?", (bid, A.cid())).fetchone() or abort(404)
        if request.form.get("action") == "delete":
            db().execute("DELETE FROM bundles WHERE id=?", (bid,))
            db().commit()
            flash(f"Bundle “{b['name']}” removed. The forms stay in the library.", "ok")
            return redirect(url_for("library") + "#bundles")
        name = request.form.get("name", "").strip()[:80]
        ids = bundle_ids_from_form()
        if not name or not ids:
            flash("A bundle needs a name and at least one form.", "error")
        else:
            db().execute("UPDATE bundles SET name=?, template_ids_json=? WHERE id=?", (name, json.dumps(ids), bid))
            db().commit()
            flash(f"Bundle “{name}” saved.", "ok")
        return redirect(url_for("library") + "#bundles")

    # ------------------------------------------------ in-person signing (locks the device to signing)
    def kiosk_for(pid):
        p = db().execute("SELECT * FROM packets WHERE id=?", (pid,)).fetchone()
        return {"pid": pid, "tokens": [p["token"]] + [s["token"] for s in cosigners(pid)],
                "by": A.actor(), "uid": g.user["id"], "started": A.now()}

    def start_in_person(pid, who="1"):
        p = db().execute("SELECT * FROM packets WHERE id=?", (pid,)).fetchone()
        if p["completed_at"] or p["voided_at"]:
            flash("This packet is closed.", "error")
            return redirect(url_for("packet_detail", pid=pid))
        session["kiosk"] = kiosk_for(pid)
        target = url_for("client_packet", token=p["token"])
        label = p["client_name"]
        if who != "1":
            s = db().execute("SELECT * FROM packet_signers WHERE id=? AND packet_id=?", (who, pid)).fetchone()
            if s:
                target, label = url_for("cosign_packet", token=s["token"]), s["name"]
        if p["status"] == "draft":
            A.mark_sent(pid, "link")
        A.log_event(pid, "details", f"in-person signing started for {label} on {A.actor()}'s device")
        db().commit()
        return redirect(target)

    A.start_in_person = start_in_person

    @app.route("/packets/<int:pid>/in-person", methods=["POST"])
    @A.login_required
    def in_person(pid):
        return start_in_person(pid, request.form.get("who", "1"))

    def signing_method(p):
        k = session.get("kiosk") if request else None
        if k and k.get("pid") == p["id"]:
            return f"In person, on a device provided by {A.biz()} (started by {k.get('by') or 'staff'})"
        return "Remotely, from the signing link"

    A.signing_method = signing_method

    @app.before_request
    def kiosk_guard():
        k = session.get("kiosk")
        if not k:
            return None
        path = request.path
        allowed = path.startswith(("/static/", "/brand/", "/kiosk")) or path == "/favicon.ico"
        allowed = allowed or any(path.startswith(f"/s/{t}") or path.startswith(f"/c/{t}") for t in k.get("tokens", []))
        if not allowed:
            return redirect(url_for("kiosk_unlock"))
        return None

    @app.route("/kiosk", methods=["GET", "POST"])
    def kiosk_unlock():
        k = session.get("kiosk")
        if not k:
            return redirect(url_for("dashboard"))
        p = db().execute("SELECT * FROM packets WHERE id=?", (k["pid"],)).fetchone()
        g.company = A.get_company(p["company_id"]) if p else None
        error = None
        if request.method == "POST":
            pw = request.form.get("password", "")
            ok = False
            if k.get("uid") == 0:
                ok = bool(A.ADMIN_PASSWORD) and pw == A.ADMIN_PASSWORD
            else:
                u = db().execute("SELECT * FROM users WHERE id=? AND active=1", (k.get("uid"),)).fetchone()
                ok = bool(u) and check_password_hash(u["password_hash"], pw)
            if ok:
                session.pop("kiosk", None)
                if p:
                    A.log_event(p["id"], "details", f"in-person signing ended by {k.get('by')}")
                    db().commit()
                return redirect(url_for("packet_detail", pid=k["pid"]) if p else url_for("dashboard"))
            time.sleep(1)
            error = "That password isn't right."
        links = []
        if p:
            links = [(p["client_name"], url_for("client_packet", token=p["token"]))] + [
                (s["name"], url_for("cosign_packet", token=s["token"])) for s in cosigners(p["id"])]
        return render_template("kiosk.html", k=k, p=p, error=error, links=links)

    # ------------------------------------------------ signature boxes on library PDFs
    def template_pdf(tid):
        t = db().execute("SELECT * FROM templates WHERE id=? AND company_id=?", (tid, A.cid())).fetchone() or abort(404)
        if t["kind"] != "pdf" or not t["file_path"]:
            abort(404)
        return t, os.path.join(A.UPLOAD_DIR, t["file_path"])

    @app.route("/library/<int:tid>/place", methods=["GET", "POST"])
    @A.manager_required
    def place_boxes(tid):
        t, path = template_pdf(tid)
        from pypdf import PdfReader
        try:
            reader = PdfReader(path)
            pages = [{"w": float(pg.mediabox.width), "h": float(pg.mediabox.height),
                      "rotated": bool((pg.rotation or 0) % 360)} for pg in reader.pages][:100]
        except Exception:  # noqa: BLE001
            flash("This PDF can't be opened to place boxes.", "error")
            return redirect(url_for("library"))
        if request.method == "POST":
            boxes = clean_placements(request.form.get("placements_json", "[]"), len(pages))
            if boxes is None:
                flash("The boxes couldn't be saved. Reload the page and try again.", "error")
            else:
                boxes = [b for b in boxes if not pages[b["page"]]["rotated"]]
                db().execute("UPDATE templates SET placements_json=? WHERE id=?", (json.dumps(boxes), tid))
                db().commit()
                flash(f"Saved {len(boxes)} box{'es' if len(boxes) != 1 else ''} on “{t['name']}”. New packets with this form "
                      f"show the signatures right on the page.", "ok")
                return redirect(url_for("library"))
        try:
            boxes = json.loads(t["placements_json"] or "[]")
        except ValueError:
            boxes = []
        return render_template("place.html", t=t, pages=pages, boxes=boxes)

    @app.route("/library/<int:tid>/page/<int:n>.png")
    @A.login_required
    def template_page(tid, n):
        t, path = template_pdf(tid)
        if not 0 <= n < A.preview.page_count(path):
            abort(404)
        out = os.path.join(A.PAGE_DIR, f"tpl-{os.path.splitext(t['file_path'])[0]}-{n}.png")
        A.preview.page_png(path, n, out)
        return send_file(out, mimetype="image/png", max_age=3600)

    def placement_count(t):
        try:
            return len(json.loads(t["placements_json"] or "[]"))
        except (ValueError, KeyError, IndexError):
            return 0

    app.jinja_env.globals["placement_count"] = placement_count

    # ------------------------------------------------ export to Excel
    @app.route("/packets/export.xlsx")
    @A.login_required
    def export_packets():
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
        cond, args = A.packet_scope()
        sql = f"SELECT * FROM packets WHERE {cond}"
        q = request.args.get("q", "").strip()[:100]
        if q:
            like = f"%{q.replace('%', '').replace('_', '')}%"
            sql += " AND (so_number LIKE ? OR sales_rep LIKE ? OR client_name LIKE ? OR client_email LIKE ?)"
            args += [like] * 4
        rep = request.args.get("rep", "").strip()[:80]
        if rep:
            sql += " AND sales_rep = ?"
            args.append(rep)
        for key, op in (("from", ">="), ("to", "<")):
            v = request.args.get(key, "")
            try:
                d = datetime.strptime(v, "%Y-%m-%d")
            except ValueError:
                continue
            if key == "to":
                d += timedelta(days=1)
            sql += f" AND created_at {op} ?"
            args.append(d.replace(tzinfo=A.LOCAL_TZ).astimezone(timezone.utc).isoformat())
        rows = db().execute(sql + " ORDER BY COALESCE(sent_at, created_at) DESC, id DESC", args).fetchall()
        labels = {"draft": "Draft", "sent": "Sent", "viewed": "Opened", "in_progress": "In progress",
                  "completed": "Completed", "voided": "Cancelled", "expired": "Expired"}
        wb = Workbook()
        ws = wb.active
        ws.title = "Packets"
        head = ["Confirmation", "Client", "Client email", "Client mobile", "Second owner", "SO#", "Sales rep",
                "Created by", "Created", "Sent", "Opened", "Completed", "Status", "Signed", "Forms",
                "Files from client", "Overdue"]
        ws.append(head)

        def local(ts):
            if not ts:
                return None
            try:
                return A.to_local(datetime.fromisoformat(ts)).replace(tzinfo=None)
            except ValueError:
                return None
        for p in rows:
            signed, total = A.packet_progress(p["id"])
            cs = cosigners(p["id"])
            st = A.effective_status(p)
            dl = A.deadline_info(p) if st in ("sent", "viewed", "in_progress") else {"overdue": False}
            ws.append([A.packet_ref(p), p["client_name"], p["client_email"], sms.pretty(p["client_phone"]) if p["client_phone"] else "",
                       ", ".join(s["name"] for s in cs), p["so_number"] or "", p["sales_rep"] or "", p["created_by"] or "",
                       local(p["created_at"]), local(p["sent_at"]), local(p["first_viewed_at"]), local(p["completed_at"]),
                       labels.get(st, st), f"{signed} of {total}",
                       "; ".join(r[0] for r in db().execute("SELECT template_name FROM packet_items WHERE packet_id=? "
                                                             "ORDER BY position", (p["id"],))),
                       len(client_uploads(p["id"])), "Yes" if dl.get("overdue") else ""])
        bold = Font(bold=True, color="FFFFFF")
        fill = PatternFill("solid", fgColor="24408E")
        for c in ws[1]:
            c.font, c.fill = bold, fill
            c.alignment = Alignment(vertical="center")
        widths = [20, 24, 28, 16, 22, 12, 18, 18, 17, 17, 17, 17, 13, 9, 50, 10, 9]
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        for col in "IJKL":
            for c in ws[col][1:]:
                c.number_format = "yyyy-mm-dd hh:mm"
        ws.freeze_panes = "B2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(head))}{max(ws.max_row, 1)}"
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        stamp = datetime.now(A.LOCAL_TZ).strftime("%Y-%m-%d")
        return send_file(buf, as_attachment=True, download_name=f"packets-{stamp}.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    # ------------------------------------------------ reminders for second owners
    def run_cosigner_reminders(conn):
        if not (A.mailer.enabled() or sms.enabled()):
            return 0
        nowdt = datetime.now(timezone.utc).replace(microsecond=0)
        done = 0
        rows = conn.execute(
            "SELECT s.*, c.reminder_days, c.reminder_max FROM packet_signers s JOIN packets p ON p.id=s.packet_id "
            "JOIN companies c ON c.id=p.company_id WHERE c.status='active' AND c.reminders_on=1 AND s.completed_at IS NULL "
            "AND p.completed_at IS NULL AND p.voided_at IS NULL AND p.expires_at > ? AND s.sent_at IS NOT NULL "
            "AND COALESCE(s.reminder_count,0) < c.reminder_max", (nowdt.isoformat(),)).fetchall()
        for s in rows:
            last_raw = s["last_reminder_at"] or s["sent_at"]
            try:
                last = datetime.fromisoformat(last_raw)
            except (TypeError, ValueError):
                continue
            if nowdt - last < timedelta(days=max(1, s["reminder_days"] or 2)):
                continue
            ready = conn.execute("SELECT COUNT(*) FROM packet_items i WHERE i.packet_id=? AND i.requires_signature=1 AND "
                                 "i.signed_at IS NOT NULL AND i.id NOT IN (SELECT item_id FROM cosignatures WHERE signer_id=?)",
                                 (s["packet_id"], s["id"])).fetchone()[0]
            if not ready:
                continue  # still waiting for the first owner: nothing for them to do yet
            n = conn.execute("UPDATE packet_signers SET reminder_count=COALESCE(reminder_count,0)+1, last_reminder_at=? "
                             "WHERE id=? AND COALESCE(last_reminder_at, sent_at)=?", (A.now(), s["id"], last_raw)).rowcount
            conn.commit()
            if not n:
                continue
            p = conn.execute("SELECT * FROM packets WHERE id=?", (s["packet_id"],)).fetchone()
            company = A.get_company(p["company_id"])
            g.company = company
            base = A.BASE_URL or getattr(A, "BACKGROUND_BASE", "")
            link = f"{base}/c/{s['token']}"
            names = [r[0] for r in conn.execute("SELECT template_name FROM packet_items WHERE packet_id=? AND "
                                                "requires_signature=1 ORDER BY position", (p["id"],))]
            how = []
            if A.mailer.enabled() and "@" in (s["email"] or ""):
                try:
                    A.mailer.send_request(s["email"], s["name"], company["name"], names, link, "", p["expires_at"],
                                          reply_to=(A.staff_recipients(p) or [None])[0],
                                          brand=A.extras.brand_of(company, base), reminder=True)
                    how.append("email")
                except Exception:  # noqa: BLE001
                    pass
            if sms.enabled() and s["phone"]:
                try:
                    sms.send(s["phone"], f"{company['name']}: Hi {first_name(s['name'])}, a reminder that your forms are "
                                         f"waiting for your signature: {link}")
                    how.append("text")
                except RuntimeError:
                    pass
            if how:
                conn.execute("INSERT INTO events (packet_id, type, detail, ip, at) VALUES (?,?,?,?,?)",
                             (p["id"], "reminder", f"automatic reminder to {s['name']} (second owner) by "
                                                   f"{' and '.join(how)}", "", A.now()))
                done += 1
            else:
                conn.execute("UPDATE packet_signers SET reminder_count=MAX(reminder_count-1,0) WHERE id=?", (s["id"],))
            conn.commit()
        return done

    A.run_cosigner_reminders = run_cosigner_reminders

    def cosign_names(d):
        """Audit trail rows: both owners' names on each document."""
        names = [r[0] for r in db().execute("SELECT signer_name FROM cosignatures WHERE item_id=? ORDER BY id", (d["id"],))]
        if names and d.get("signer_name"):
            d["signer_name"] = f"{d['signer_name']} and {', '.join(names)}"
        return d

    A.cosign_names = cosign_names
