"""Job details printed on forms (4) and "I've read this page" / initials (3).

Job details
    Managers place "Job detail" boxes (kind "jobfield", with a label such as "Job address" and an optional
    default) on library PDFs, or type placeholders like {Job address} (or {Colour=White} with a default) in a
    fillable form's text. New packet asks for one value per distinct label. When the packet is created, each
    PDF item gets its own filled copy (values stamped into the boxes) that becomes the item's template_file,
    so previews, the client's viewer, the signed PDF and sealing all show it; fillable forms get the values
    substituted into their text snapshot. Built-in details come from the packet itself: {Client name}, and in
    the fabricator edition {SO#} and {Sales rep}.

Read-this-page and initials
    In the placement editor managers place "Initials" boxes (per owner) and tick pages the client must confirm
    they read. On signing, each owner ticks one box per page and types initials; the server refuses to sign
    otherwise. Confirmations (with times) are kept in the item's answers_json under "_confirm", initials are
    stamped in the boxes, and the signing record lists both.
"""
import hashlib
import io
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

LABEL_MAX = 40
VALUE_MAX = 200
TITLE_MAX = 80
# {Job address} or {Colour=White}: starts with a letter, digit or #, no braces or line breaks inside
PLACEHOLDER = re.compile(r"\{([A-Za-z0-9#][^{}\n]{0,%d})\}" % (LABEL_MAX + VALUE_MAX + 1))
INITIALS_OK = re.compile(r"[^\W\d_](?:[^\W\d_]|[.'\- ])*")

# label (normalised) -> packets column; SO# and Sales rep only exist in the fabricator edition
BUILTINS_ALL = {"client name": "client_name", "client": "client_name"}
BUILTINS_FAB = {"so#": "so_number", "so #": "so_number", "so number": "so_number", "sales order": "so_number",
                "sales order #": "so_number", "sales rep": "sales_rep"}

COLUMNS = {
    "templates": {"read_pages_json": "TEXT DEFAULT '[]'"},
    "packet_items": {"read_pages_json": "TEXT DEFAULT '[]'", "job_details_json": "TEXT DEFAULT '[]'"},
    "packets": {"job_details_json": "TEXT DEFAULT '[]'"},
}


def init_db(conn):
    for table, cols in COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not have:
            continue
        for col, spec in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {spec}")


# ------------------------------------------------------------------ pure helpers
def norm(label):
    return " ".join(str(label or "").split()).casefold()


def clean_label(label):
    return " ".join(str(label or "").split())[:LABEL_MAX]


def field_key(label):
    """Form field name for a job detail on New packet (the same label always gives the same name)."""
    return "jd_" + hashlib.sha1(norm(label).encode("utf-8")).hexdigest()[:10]


def _loads(raw, default):
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return default
    return v if isinstance(v, type(default)) else default


def _col(row, name, default=None):
    try:
        return row[name]
    except (KeyError, IndexError):
        return default


def placeholders(text):
    """[(label, default)] for each {Label} / {Label=default} in a form's text, in order."""
    out = []
    for m in PLACEHOLDER.finditer(text or ""):
        label, _, default = m.group(1).partition("=")
        label = clean_label(label)
        if label:
            out.append((label, " ".join(default.split())[:VALUE_MAX]))
    return out


def substitute(text, values):
    """Replace {Label} / {Label=default} with the job detail values (by normalised label)."""
    def rep(m):
        label, _, default = m.group(1).partition("=")
        v = values.get(norm(label))
        if v is None:
            return m.group(0)
        return v or " ".join(default.split())
    return PLACEHOLDER.sub(rep, text or "")


def initials_of(name):
    words = [w for w in re.split(r"[\s\-]+", name or "") if w and w[0].isalpha()]
    if not words:
        return ""
    if len(words) == 1:
        return words[0][0].upper()
    return (words[0][0] + words[-1][0]).upper()


def clean_initials(raw):
    v = " ".join(str(raw or "").split())[:8].upper()
    letters = sum(ch.isalpha() for ch in v)
    return v if v and INITIALS_OK.fullmatch(v) and letters <= 6 else ""


def fit_text(c, text, x, y, w, h, font="Helvetica", centre=False):
    """Write `text` inside the box (x, y = bottom-left, in points): shrink to fit, wrap onto a second line when
    the box is tall enough, and cut with … as a last resort."""
    from reportlab.lib.utils import simpleSplit
    text = " ".join(str(text or "").split())
    if not text:
        return
    size = max(6.0, min(h * 0.62, 12.0))
    while size > 6 and c.stringWidth(text, font, size) > w - 2:
        size -= 0.5
    lines = [text]
    if c.stringWidth(text, font, size) > w - 2:
        lines = simpleSplit(text, font, size, w - 2) or [text]
        fit = max(1, int(h // (size * 1.15)))
        if len(lines) > fit:
            lines = lines[:fit]
            last = lines[-1]
            while last and c.stringWidth(last + "…", font, size) > w - 2:
                last = last[:-1]
            lines[-1] = last.rstrip() + "…"
    c.setFont(font, size)
    lead = size * 1.15
    base = y + max(1.5, h * 0.18) + lead * (len(lines) - 1)
    if len(lines) > 1:
        base = min(y + h - size, base)
    for i, line in enumerate(lines):
        if centre:
            c.drawCentredString(x + w / 2, base - i * lead, line)
        else:
            c.drawString(x + 1, base - i * lead, line)


def fill_pdf(src, out, boxes, values):
    """Copy `src` to `out` with each job detail box's value printed in it. values: {norm(label): text}."""
    from pypdf import PdfReader, PdfWriter
    from reportlab.lib import colors
    from reportlab.pdfgen import canvas as rl_canvas
    by_page = {}
    for b in boxes:
        if b.get("kind") == "jobfield" and values.get(norm(b.get("label"))):
            by_page.setdefault(int(b.get("page", 0)), []).append(b)
    reader = PdfReader(src)
    writer = PdfWriter()
    for n, page in enumerate(reader.pages):
        if by_page.get(n) and not (page.rotation or 0) % 360:
            w, h = float(page.mediabox.width), float(page.mediabox.height)
            buf = io.BytesIO()
            c = rl_canvas.Canvas(buf, pagesize=(w, h))
            c.setFillColor(colors.HexColor("#1d2433"))
            for b in by_page[n]:
                bx, bw, bh = float(b["x"]) * w, float(b["w"]) * w, float(b["h"]) * h
                by = h - float(b["y"]) * h - bh
                fit_text(c, values[norm(b["label"])], bx, by, bw, bh)
            c.save()
            buf.seek(0)
            page.merge_translated_page(PdfReader(buf).pages[0], float(page.mediabox.left), float(page.mediabox.bottom))
        writer.add_page(page)
    if reader.metadata:
        try:
            writer.add_metadata({k: str(v) for k, v in reader.metadata.items() if isinstance(k, str)})
        except Exception:  # noqa: BLE001
            pass
    with open(out, "wb") as fh:
        writer.write(fh)


def clean_read_pages(form, pages):
    """[{page, title}] from the placement editor's per-page toggles (pages = page count)."""
    out = []
    seen = set()
    for v in form.getlist("read_page"):
        if not str(v).isdigit():
            continue
        n = int(v)
        if 0 <= n < pages and n not in seen:
            seen.add(n)
            out.append({"page": n, "title": " ".join(form.get(f"read_title_{n}", "").split())[:TITLE_MAX]})
    return sorted(out, key=lambda r: r["page"])


# ------------------------------------------------------------------ app wiring
def register(A):
    app, db = A.app, A.db
    from flask import request

    def builtins():
        out = dict(BUILTINS_ALL)
        if hasattr(A, "sales_reps"):   # fabricator edition: SO# and sales rep are packet fields
            out.update(BUILTINS_FAB)
        return out

    def template_details(t):
        """[{label, default}] job details a library form uses, in order, without repeats."""
        found = []
        if t["kind"] == "pdf":
            for b in _loads(_col(t, "placements_json", "[]"), []):
                if isinstance(b, dict) and b.get("kind") == "jobfield" and clean_label(b.get("label")):
                    found.append((clean_label(b["label"]), str(b.get("default") or "")[:VALUE_MAX]))
        elif t["kind"] == "form":
            found = placeholders(_col(t, "description", "") or "")
        out, seen = [], {}
        for label, default in found:
            k = norm(label)
            if k in seen:
                if default and not seen[k]["default"]:
                    seen[k]["default"] = default
                continue
            seen[k] = {"label": label, "default": default}
            out.append(seen[k])
        return out

    def details_for(templates):
        """Job details across several forms, one per distinct label: {"fields": [...], "builtins": [...]}.
        Each entry: key, label, default, required, tids (forms using it), forms (their names)."""
        bmap = builtins()
        fields, built, idx = [], [], {}
        for t in templates:
            for d in template_details(t):
                k = norm(d["label"])
                target = built if k in bmap else fields
                if k not in idx:
                    idx[k] = {"key": field_key(d["label"]), "label": d["label"], "default": d["default"],
                              "tids": [], "forms": [], "column": bmap.get(k)}
                    target.append(idx[k])
                e = idx[k]
                if d["default"] and not e["default"]:
                    e["default"] = d["default"]
                if t["id"] not in e["tids"]:
                    e["tids"].append(t["id"])
                    e["forms"].append(t["name"])
        for e in fields:
            e["required"] = not e["default"]
        return {"fields": fields, "builtins": built}

    def selected_templates(ids):
        if not ids:
            return []
        rows = {r["id"]: r for r in db().execute(
            f"SELECT * FROM templates WHERE company_id=? AND id IN ({','.join('?' * len(ids))})", [A.cid()] + list(ids))}
        return [rows[i] for i in ids if i in rows]

    def value_of(e, form, packet=None):
        if e.get("column"):
            if packet is not None:
                return str(_col(packet, e["column"], "") or "").strip()
            return form.get(e["column"], "").strip()[:VALUE_MAX]
        return " ".join(form.get(e["key"], "").split())[:VALUE_MAX] or e["default"]

    # ---------------------------------------------------------- New packet
    def check(form, ids):
        """Problem with the job details for the ticked forms, or None."""
        d = details_for(selected_templates(ids))
        missing = [e for e in d["fields"] if not value_of(e, form)]
        if missing:
            names = ", ".join(f"“{e['label']}”" for e in missing[:6])
            return (f"Fill in the job detail{'s' if len(missing) != 1 else ''} {names}: "
                    f"{'they are' if len(missing) != 1 else 'it is'} printed on the forms you picked.")
        names = {"so_number": "SO#", "sales_rep": "sales rep", "client_name": "client's name"}
        for e in d["builtins"]:
            if not value_of(e, form):
                return f"Add the {names.get(e['column'], e['label'])}: “{e['forms'][0]}” prints it on the page."
        return None

    A.jobfields_check = check

    def apply(pid):
        """After New packet saved the items: filled PDF copies, form text, read-page settings, stored values."""
        form = request.form
        p = db().execute("SELECT * FROM packets WHERE id=?", (pid,)).fetchone()
        items = db().execute("SELECT * FROM packet_items WHERE packet_id=? AND template_id IS NOT NULL ORDER BY position",
                             (pid,)).fetchall()
        tmap = {t["id"]: t for t in selected_templates([i["template_id"] for i in items])}
        d = details_for([tmap[i["template_id"]] for i in items if i["template_id"] in tmap])
        entries = d["fields"] + d["builtins"]
        values = {norm(e["label"]): value_of(e, form, p) for e in entries}
        for k, col in builtins().items():   # built-ins also match their other spellings
            values.setdefault(k, str(_col(p, col, "") or "").strip())
        packet_list = [[e["label"], values[norm(e["label"])]] for e in d["fields"]]
        for it in items:
            t = tmap.get(it["template_id"])
            if not t:
                continue
            rp = _loads(_col(t, "read_pages_json", "[]"), [])
            if rp:
                db().execute("UPDATE packet_items SET read_pages_json=? WHERE id=?", (json.dumps(rp), it["id"]))
            used = template_details(t)
            if not used:
                continue
            item_list = [[e["label"], values.get(norm(e["label"]), "")] for e in used
                         if norm(e["label"]) not in builtins()]
            if t["kind"] == "pdf" and it["template_file"]:
                boxes = _loads(_col(t, "placements_json", "[]"), [])
                src = os.path.join(A.UPLOAD_DIR, it["template_file"])
                base = re.sub(r"^[0-9a-f]{16}-", "", it["template_file"])[:80]
                stored = f"{secrets.token_hex(8)}-filled-{base}"
                try:
                    fill_pdf(src, os.path.join(A.UPLOAD_DIR, stored), boxes, values)
                except Exception:  # noqa: BLE001
                    app.logger.exception("Could not print job details on %s", it["template_file"])
                    A.log_event(pid, "details", f"job details could not be printed on “{it['template_name']}”")
                    stored = None
                if stored:
                    db().execute("UPDATE packet_items SET template_file=?, template_sha256=?, job_details_json=? WHERE id=?",
                                 (stored, A.sha256_file(os.path.join(A.UPLOAD_DIR, stored)), json.dumps(item_list), it["id"]))
            elif t["kind"] == "form":
                db().execute("UPDATE packet_items SET template_body=?, job_details_json=? WHERE id=?",
                             (substitute(it["template_body"] or "", values), json.dumps(item_list), it["id"]))
        if packet_list:
            db().execute("UPDATE packets SET job_details_json=? WHERE id=?", (json.dumps(packet_list), pid))
            A.log_event(pid, "details", "job details: " + "; ".join(f"{k}: {v}" for k, v in packet_list)[:400])

    A.jobfields_apply = apply

    def prefill(pid):
        """New packet copied from another packet: the same job details again."""
        row = db().execute("SELECT job_details_json FROM packets WHERE id=? AND company_id=?", (pid, A.cid())).fetchone()
        return {field_key(k): v for k, v in _loads(row[0] if row else "[]", []) if isinstance(k, str)}

    A.jobfields_prefill = prefill

    def packet_details(p):
        return [(k, v) for k, v in _loads(_col(p, "job_details_json", "[]"), []) if isinstance(k, str)]

    # ---------------------------------------------------------- placement editor
    def save_read_pages(tid, form, pages):
        rp = clean_read_pages(form, pages)
        db().execute("UPDATE templates SET read_pages_json=? WHERE id=?", (json.dumps(rp), tid))
        return rp

    A.jobfields_save_read_pages = save_read_pages

    def read_pages_of(row):
        return {r["page"]: r.get("title", "") for r in _loads(_col(row, "read_pages_json", "[]"), [])
                if isinstance(r, dict) and isinstance(r.get("page"), int)}

    # ---------------------------------------------------------- signing
    def sign_state(it, signer=1):
        """What this owner must do on a PDF item before signing, or None."""
        if it["template_kind"] != "pdf":
            return None
        pages = [{"page": r["page"], "n": r["page"] + 1, "title": r.get("title", "")}
                 for r in _loads(_col(it, "read_pages_json", "[]"), []) if isinstance(r, dict) and isinstance(r.get("page"), int)]
        boxes = [b for b in _loads(_col(it, "placements_json", "[]"), [])
                 if isinstance(b, dict) and b.get("kind") == "initials" and int(b.get("signer", 1)) == signer]
        if not pages and not boxes:
            return None
        nums = sorted({int(b["page"]) + 1 for b in boxes})
        box_pages = ("page " if len(nums) == 1 else "pages ") + (
            ", ".join(map(str, nums[:-1])) + " and " + str(nums[-1]) if len(nums) > 1 else "".join(map(str, nums)))
        return {"pages": pages, "initials": bool(boxes), "signer": signer, "box_pages": box_pages,
                "boxes": [{k: b[k] for k in ("page", "x", "y", "w", "h")} for b in boxes]}

    def when_ok(raw, now):
        try:
            t = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        return t.astimezone(timezone.utc).replace(microsecond=0) if now - timedelta(days=3) <= t <= now + timedelta(minutes=2) else None

    def sign_check(it, form, signer, answers, errors):
        """Server-side check of the read-this-page boxes and initials; records them in answers["_confirm"]."""
        st = sign_state(it, signer)
        if not st:
            return
        now = datetime.now(timezone.utc).replace(microsecond=0)
        missing = [r["n"] for r in st["pages"] if form.get(f"read_{r['page']}") != "on"]
        if missing:
            nums = ", ".join(str(n) for n in missing)
            errors["read_pages"] = (f"Tick the box to confirm you've read page {nums}." if len(missing) == 1 else
                                    f"Tick a box for each page to confirm you've read it (still to tick: pages {nums}).")
        ini = clean_initials(form.get("initials", ""))
        if st["initials"] and not ini:
            errors["initials"] = ("Type your initials (letters only, for example SL). They're printed in the initials boxes "
                                  "on the document." if form.get("initials", "").strip() else
                                  "Type your initials. They're printed in the initials boxes on the document.")
        if missing or (st["initials"] and not ini):
            return
        confirm = {"pages": [{"page": r["n"], "title": r["title"],
                              "at": (when_ok(form.get(f"read_at_{r['page']}", ""), now) or now).isoformat()}
                             for r in st["pages"]]}
        if st["initials"]:
            confirm["initials"] = ini
        answers.setdefault("_confirm", {})[str(signer)] = confirm

    A.jobfields_sign_check = sign_check

    def save_confirm(iid, answers, extra):
        """Second owner: add their confirmations to the item's answers (already loaded into `answers`)."""
        if not extra.get("_confirm"):
            return answers
        answers.setdefault("_confirm", {}).update(extra["_confirm"])
        db().execute("UPDATE packet_items SET answers_json=? WHERE id=?", (json.dumps(answers), iid))
        return answers

    A.jobfields_save_confirm = save_confirm

    def local_time(iso, day_of=None):
        try:
            t = A.to_local(datetime.fromisoformat(iso))
        except (TypeError, ValueError):
            return ""
        s = t.strftime("%I:%M %p").lstrip("0")
        try:
            if day_of and A.to_local(datetime.fromisoformat(day_of)).date() != t.date():
                s = t.strftime("%b %d, ").replace(" 0", " ") + s
        except (TypeError, ValueError):
            pass
        return s

    def confirm_text(c, signed_at):
        pages = c.get("pages") or []
        parts = []
        if pages:
            parts.append(", ".join(f"{r['page']} (at {local_time(r.get('at'), signed_at)})" for r in pages))
        if c.get("initials"):
            parts.append(f"Initials: {c['initials']}")
        return " · ".join(parts)

    def sign_meta(p, it, answers, signers):
        """Extra signing-record rows and initials for render_item_pdf's meta."""
        out = {"extra_rows": []}
        jd = [(k, v) for k, v in _loads(_col(it, "job_details_json", "[]"), []) if isinstance(k, str)]
        if jd:
            out["extra_rows"].append(("Job details", " · ".join(f"{k}: {v or '—'}" for k, v in jd)))
        conf = (answers or {}).get("_confirm") if isinstance(answers, dict) else None
        conf = conf if isinstance(conf, dict) else {}
        multi = len(signers) > 1
        for n, s in enumerate(signers, 1):
            c = conf.get(str(n)) or {}
            if c.get("initials"):
                s["initials"] = c["initials"]
            text = confirm_text(c, s.get("signed_at"))
            if not text:
                continue
            label = "Pages confirmed as read" if c.get("pages") else "Initials"
            if not c.get("pages"):
                text = c["initials"]
            out["extra_rows"].append((f"Signer {n}: {label.lower()}" if multi else label, text))
        if signers and signers[0].get("initials"):
            out["initials"] = signers[0]["initials"]
        return out

    A.jobfields_meta = sign_meta

    def staff_confirmations(it):
        """'Pages confirmed: 2, 3 · Initials SL' lines for the packet page."""
        conf = _loads(_col(it, "answers_json", "") or "{}", {}).get("_confirm") or {}
        return [confirm_text(conf[k], _col(it, "signed_at")) for k in sorted(conf) if isinstance(conf[k], dict)]

    app.jinja_env.globals.update(
        job_details_for=details_for, packet_job_details=packet_details, read_pages_of=read_pages_of,
        read_confirm=sign_state, initials_of=initials_of, job_field_key=field_key, item_confirmations=staff_confirmations,
        job_detail_builtins=lambda: sorted({"client_name": "Client name", "so_number": "SO#", "sales_rep": "Sales rep"}[c]
                                           for c in set(builtins().values())))
