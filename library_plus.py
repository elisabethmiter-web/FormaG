"""Form library extras: ordering, quick preview, search and tags.

* order      managers drag forms (and the forms inside each bundle) into the order they want; the library,
             the New packet picker and bundles use that order (templates.position, bundles.template_ids_json)
* preview    "Quick look" side panel: PDF pages, converted Word/Excel pages, pictures, or a fillable form's
             description and fields, without leaving the library
* tags       short labels per form ("Kitchen", "Deposit"), edited inline by managers; search and tag filters
             on the library page (instant with JavaScript, ?q= / ?tag= without)

Nothing here changes app.py: the library and New packet pages get their extra context and their order
through Flask's before_render_template signal. On New packet, a bundle's forms are ticked in the bundle's
own order (static/library-bundles.js, added to that page's HTML by an after_request hook) so the packet
keeps the order the manager set in the library.
"""
import json
import os
import re

from flask import abort, before_render_template, flash, g, jsonify, redirect, request, send_file, url_for

MAX_TAGS = 12
TAG_LEN = 30
MAX_PREVIEW_PAGES = 40


def init_db(conn):
    have = {r[1] for r in conn.execute("PRAGMA table_info(templates)")}
    if have and "position" not in have:
        conn.execute("ALTER TABLE templates ADD COLUMN position INTEGER")
    if have and "tags_json" not in have:
        conn.execute("ALTER TABLE templates ADD COLUMN tags_json TEXT DEFAULT '[]'")


# ---------------------------------------------------------------- pure helpers (no request needed)
def tags_of(t):
    try:
        raw = json.loads((t["tags_json"] if "tags_json" in t.keys() else None) or "[]")
    except (ValueError, TypeError, KeyError, AttributeError):
        return []
    return [str(x) for x in raw if isinstance(x, str) and x.strip()] if isinstance(raw, list) else []


def clean_tags(values, known=()):
    """Trim, cap and de-duplicate tags (case-insensitive). A tag that matches an existing one in another
    case takes the existing spelling, so "kitchen" and "Kitchen" stay one tag."""
    canon = {k.lower(): k for k in known}
    out, seen = [], set()
    for v in values:
        tag = re.sub(r"\s+", " ", str(v or "")).strip().strip(",")[:TAG_LEN].strip()
        if not tag or tag.lower() in seen:
            continue
        seen.add(tag.lower())
        out.append(canon.get(tag.lower(), tag))
        if len(out) >= MAX_TAGS:
            break
    return out


def order_key(t):
    pos = t["position"] if "position" in t.keys() else None
    return (pos is None, pos if pos is not None else 0, (t["name"] or "").lower(), t["id"])


def register(A):
    app = A.app
    db = A.db

    def company_templates():
        rows = db().execute("SELECT * FROM templates WHERE archived=0 AND company_id=?", (A.cid(),)).fetchall()
        return sorted(rows, key=order_key)

    def tag_counts(rows=None):
        counts = {}
        for t in rows if rows is not None else company_templates():
            for tag in tags_of(t):
                counts[tag] = counts.get(tag, 0) + 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))

    def wants_json():
        return request.is_json or request.headers.get("X-Requested-With") == "fetch" or \
            "application/json" in request.headers.get("Accept", "")

    def save_positions(ids):
        for pos, tid in enumerate(ids):
            db().execute("UPDATE templates SET position=? WHERE id=? AND company_id=?", (pos, tid, A.cid()))
        db().commit()

    def full_order(requested):
        """The library order with `requested` (a list of ids) first in that order, the rest after it."""
        current = [t["id"] for t in company_templates()]
        live = set(current)
        head, seen = [], set()
        for x in requested:
            if isinstance(x, (int, str)) and str(x).isdigit() and int(x) in live and int(x) not in seen:
                head.append(int(x))
                seen.add(int(x))
        return head + [i for i in current if i not in seen]

    def requested_ids():
        if request.is_json:
            data = request.get_json(silent=True) or {}
            return list(data.get("ids") or [])[:2000]
        return request.form.getlist("ids")[:2000]

    def moved(ids, item, direction):
        if item not in ids:
            return ids
        i = ids.index(item)
        j = i - 1 if direction == "up" else i + 1
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
        return ids

    # ------------------------------------------------ library order
    @app.route("/library/order", methods=["POST"])
    @A.manager_required
    def library_order():
        ids = full_order(requested_ids())
        save_positions(ids)
        if wants_json():
            return jsonify(ok=True, ids=ids)
        flash("Order saved.", "ok")
        return redirect(url_for("library"))

    @app.route("/library/<int:tid>/move", methods=["POST"])
    @A.manager_required
    def library_move(tid):
        ids = moved([t["id"] for t in company_templates()], tid, request.form.get("dir", ""))
        if tid not in ids:
            abort(404)
        save_positions(ids)
        if wants_json():
            return jsonify(ok=True, ids=ids)
        return redirect(url_for("library") + f"#form-{tid}")

    # ------------------------------------------------ bundle order
    def load_bundle(bid):
        b = db().execute("SELECT * FROM bundles WHERE id=? AND company_id=?", (bid, A.cid())).fetchone() or abort(404)
        try:
            ids = [int(x) for x in json.loads(b["template_ids_json"] or "[]")]
        except (ValueError, TypeError):
            ids = []
        return b, ids

    def save_bundle(bid, ids):
        db().execute("UPDATE bundles SET template_ids_json=? WHERE id=? AND company_id=?", (json.dumps(ids), bid, A.cid()))
        db().commit()

    @app.route("/library/bundles/<int:bid>/order", methods=["POST"])
    @A.manager_required
    def bundle_order(bid):
        _b, ids = load_bundle(bid)
        want = [int(x) for x in requested_ids() if str(x).isdigit()]
        # Only a re-ordering: forms can't be added or dropped here (the bundle's Edit form does that).
        new = [i for i in dict.fromkeys(want) if i in ids] + [i for i in ids if i not in want]
        save_bundle(bid, new)
        if wants_json():
            return jsonify(ok=True, ids=new)
        flash("Bundle order saved.", "ok")
        return redirect(url_for("library") + "#bundles")

    @app.route("/library/bundles/<int:bid>/move", methods=["POST"])
    @A.manager_required
    def bundle_move(bid):
        _b, ids = load_bundle(bid)
        tid = request.form.get("tid", "")
        if tid.isdigit():
            ids = moved(ids, int(tid), request.form.get("dir", ""))
            save_bundle(bid, ids)
        if wants_json():
            return jsonify(ok=True, ids=ids)
        return redirect(url_for("library") + f"#bundle-{bid}")

    def bundle_templates(b, templates):
        """The library's templates for a bundle's Edit form: the bundle's forms first, in the bundle's order,
        then the others. The existing save keeps the order the boxes appear in."""
        ids = list(b.get("ids") or [])
        by_id = {t["id"]: t for t in templates}
        return [by_id[i] for i in ids if i in by_id] + [t for t in templates if t["id"] not in ids]

    app.jinja_env.globals["lp_bundle_templates"] = bundle_templates

    # ------------------------------------------------ tags
    @app.route("/library/<int:tid>/tags", methods=["POST"])
    @A.manager_required
    def library_tags(tid):
        t = db().execute("SELECT * FROM templates WHERE id=? AND archived=0 AND company_id=?",
                         (tid, A.cid())).fetchone() or abort(404)
        others = [x for x in company_templates() if x["id"] != tid]
        known = [tag for tag, _n in tag_counts(others)]
        if request.is_json:
            raw = (request.get_json(silent=True) or {}).get("tags") or []
            values = raw if isinstance(raw, list) else []
        else:
            action, one = request.form.get("action", "set"), request.form.get("tag", "")
            current = tags_of(t)
            if action == "add":
                values = current + [one]
            elif action == "remove":
                values = [x for x in current if x.lower() != one.strip().lower()]
            else:
                values = request.form.get("tags", "").split(",")
        tags = clean_tags(values, known)
        db().execute("UPDATE templates SET tags_json=? WHERE id=? AND company_id=?", (json.dumps(tags), tid, A.cid()))
        db().commit()
        if wants_json():
            return jsonify(ok=True, tags=tags, all=tag_counts())
        flash(f"Tags saved for “{t['name']}”.", "ok")
        return redirect(url_for("library") + f"#form-{tid}")

    # ------------------------------------------------ quick preview
    KIND_LABELS = {"pdf": "PDF document", "file": "File", "form": "Fillable form", "specs": "Room-by-room spec form",
                   "layout": "Stone & seam layout", "drawing": "Template drawing"}

    def kind_label(t):
        if t["kind"] in ("pdf", "file") and t["file_name"]:
            return app.jinja_env.filters["filetype"](t["file_name"])
        return KIND_LABELS.get(t["kind"], "Form")

    def converted_pdf(t):
        """Stored name of the PDF preview of a Word/Excel/... library file, or None."""
        if t["kind"] != "file" or not t["file_path"]:
            return None
        try:
            pv = A.preview.ensure_preview(A.UPLOAD_DIR, t["file_path"], t["file_name"] or t["file_path"])
        except Exception:  # noqa: BLE001
            app.logger.exception("Preview failed for template %s", t["id"])
            return None
        return pv

    @app.route("/library/<int:tid>/preview.json")
    @A.login_required
    def library_preview(tid):
        t = db().execute("SELECT * FROM templates WHERE id=? AND archived=0 AND company_id=?",
                         (tid, A.cid())).fetchone() or abort(404)
        try:
            fields = json.loads(t["fields_json"] or "[]")
        except ValueError:
            fields = []
        out = {"id": t["id"], "name": t["name"], "kind": t["kind"], "kind_label": kind_label(t),
               "description": (t["description"] or "")[:4000], "tags": tags_of(t),
               "fields": [{"label": f.get("label", ""), "type": f.get("type", "text"), "required": bool(f.get("required")),
                           "help": f.get("help", "")} for f in fields if isinstance(f, dict)][:200],
               "send_url": url_for("new_packet", t=t["id"]), "mode": "none", "pages": [], "page_count": 0}
        if t["file_path"]:
            out["file_url"] = url_for("template_file", tid=t["id"])
            out["file_name"] = t["file_name"] or ""
        if A.is_manager() and t["kind"] != "specs":
            out["edit_url"] = url_for("edit_form", tid=t["id"])
        if t["kind"] == "pdf" and t["file_path"]:
            try:
                n = A.preview.page_count(os.path.join(A.UPLOAD_DIR, t["file_path"]))
            except Exception:  # noqa: BLE001
                n = 0
            out.update(mode="pages" if n else "download", page_count=n,
                       pages=[url_for("template_page", tid=t["id"], n=i) for i in range(min(n, MAX_PREVIEW_PAGES))])
        elif t["kind"] == "file" and t["file_path"]:
            if (t["file_mime"] or "") in A.IMAGE_MIMES:
                out["mode"] = "image"
                out["image"] = url_for("template_file", tid=t["id"])
            else:
                pv = converted_pdf(t)
                if pv and pv["type"] == "html":
                    with open(os.path.join(A.UPLOAD_DIR, pv["file"]), encoding="utf-8") as fh:
                        out.update(mode="html", html=fh.read()[:400000])
                elif pv:
                    try:
                        n = A.preview.page_count(os.path.join(A.UPLOAD_DIR, pv["file"]))
                    except Exception:  # noqa: BLE001
                        n = 0
                    out.update(mode="pages" if n else "download", page_count=n,
                               pages=[url_for("library_preview_page", tid=t["id"], n=i)
                                      for i in range(min(n, MAX_PREVIEW_PAGES))])
                else:
                    out["mode"] = "download"
        elif t["kind"] == "specs":
            spec = getattr(getattr(A, "forms", None), "SPEC_FORM", None) or {}
            out["mode"] = "form"
            out["description"] = out["description"] or spec.get("description", "")
            out["sections"] = [{"name": k, "items": v} for k, v in (spec.get("categories") or {}).items()]
        else:
            out["mode"] = "form"
        return jsonify(out)

    @app.route("/library/<int:tid>/preview/<int:n>.png")
    @A.login_required
    def library_preview_page(tid, n):
        t = db().execute("SELECT * FROM templates WHERE id=? AND company_id=?", (tid, A.cid())).fetchone() or abort(404)
        pv = converted_pdf(t)
        if not pv or pv["type"] != "pdf":
            abort(404)
        path = os.path.join(A.UPLOAD_DIR, pv["file"])
        if not 0 <= n < A.preview.page_count(path):
            abort(404)
        out = os.path.join(A.PAGE_DIR, f"tplprev-{os.path.splitext(pv['file'])[0]}-{n}.png")
        A.preview.page_png(path, n, out)
        return send_file(out, mimetype="image/png", max_age=3600)

    # ------------------------------------------------ page context: order, tags, search (no app.py changes)
    def matches(t, q, wanted):
        tags = [x.lower() for x in tags_of(t)]
        if any(w.lower() not in tags for w in wanted):
            return False
        if not q:
            return True
        hay = " ".join([t["name"] or "", t["description"] or "", " ".join(tags), kind_label(t)]).lower()
        return all(word in hay for word in q.lower().split())

    def tag_href(tag, selected, q):
        sel = [x for x in selected if x.lower() != tag.lower()] if tag.lower() in [s.lower() for s in selected] \
            else selected + [tag]
        from urllib.parse import urlencode
        params = ([("q", q)] if q else []) + [("tag", x) for x in sel]
        return url_for("library") + ("?" + urlencode(params) if params else "") + "#forms"

    def on_render(sender, template, context, **_kw):
        name = getattr(template, "name", "")
        if name not in ("library.html", "packet_new.html") or not g.get("company"):
            return
        templates = context.get("templates")
        if not isinstance(templates, list):
            return
        try:
            ordered = sorted(templates, key=order_key)
        except (KeyError, TypeError, IndexError):
            return
        context["templates"] = ordered
        if name != "library.html":
            return
        q = request.args.get("q", "").strip()[:100]
        selected = clean_tags(request.args.getlist("tag"))
        enriched = []
        for t in ordered:
            d = dict(t) if not isinstance(t, dict) else t
            d["tags"] = tags_of(t)
            d["kind_label"] = kind_label(t)
            d["lp_match"] = matches(t, q, selected)
            d["search_text"] = " ".join([d.get("name") or "", (d.get("description") or "")[:2000],
                                         d["kind_label"]]).lower()  # tags are matched separately
            enriched.append(d)
        context["templates"] = enriched
        context.update(lp_q=q, lp_selected=selected, lp_tags=tag_counts(ordered),
                       lp_selected_lower=[s.lower() for s in selected],
                       lp_tag_href=lambda tag: tag_href(tag, selected, q),
                       lp_shown=sum(1 for d in enriched if d["lp_match"]))

    before_render_template.connect(on_render, app, weak=False)

    # ------------------------------------------------ New packet: tick a bundle's forms in the bundle's order
    BUNDLE_JS = "library-bundles.js"

    @app.after_request
    def bundle_order_script(resp):
        if request.endpoint != "new_packet" or request.method != "GET" or resp.status_code != 200 \
                or resp.mimetype != "text/html" or resp.direct_passthrough or not g.get("company"):
            return resp
        body = resp.get_data(as_text=True)
        if "bundle-btn" not in body and "bundle=" not in request.query_string.decode("latin-1"):
            return resp
        pre = []
        b = request.args.get("bundle", "")
        if b.isdigit():
            row = db().execute("SELECT template_ids_json FROM bundles WHERE id=? AND company_id=?",
                               (int(b), A.cid())).fetchone()
            try:
                pre = [int(x) for x in json.loads(row[0] or "[]")] if row else []
            except (ValueError, TypeError):
                pre = []
        tag = (f'<script src="{url_for("static", filename=BUNDLE_JS)}" data-bundle-order="{json.dumps(pre)}" '
               f'id="lp-bundle-order"></script>')
        i = body.rfind("</body>")
        if i < 0:
            return resp
        resp.set_data(body[:i] + tag + "\n" + body[i:])
        return resp
