"""Interactive Packets dashboard: the data behind live updates, instant search and sort, charts,
quick actions on each row, the preview panel and bulk actions. The page still works without it
(the server-rendered list); static/dashboard.js takes over once this data has loaded."""
import json
import statistics
from datetime import datetime, timedelta, timezone

from flask import abort, g, jsonify, request, url_for

LABELS = {"draft": "Draft", "sent": "Sent", "viewed": "Opened", "in_progress": "In progress", "completed": "Signed",
          "expired": "Expired", "voided": "Cancelled"}
EVENT_LABELS = {"created": "Packet created", "sent": "Sent", "viewed": "Opened by client", "signed": "Signed",
                "completed": "Completed", "voided": "Cancelled", "extended": "Link extended", "reminder": "Reminder",
                "details": "Details updated", "followup": "Follow-up", "downloaded": "Downloaded by client",
                "confirmed": "Confirmed receipt", "uploaded": "Files from client", "edited": "Client details changed",
                "archived": "Archive", "saved": "Saved to finish later"}
WEEKS = 12


def parse(ts):
    try:
        d = datetime.fromisoformat(ts)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def register(A):
    app, db = A.app, A.db

    def version():
        cond, args = A.packet_scope()
        r = db().execute(f"SELECT (SELECT MAX(id) FROM events WHERE packet_id IN (SELECT id FROM packets WHERE {cond})), "
                         f"(SELECT COUNT(*) FROM packets WHERE {cond})", args + args).fetchone()
        return f"{r[0] or 0}-{r[1] or 0}-{datetime.now(timezone.utc).strftime('%Y%m%d%H')}"   # hourly: deadlines move on

    def day(ts):
        return A.app.jinja_env.filters["day"](ts) if ts else ""

    waiting_on = app.jinja_env.globals.get("waiting_on")

    def row(p):
        signed, total = A.packet_progress(p["id"])
        st = A.effective_status(p)
        open_ = st in ("sent", "viewed", "in_progress")
        dl = A.deadline_info(p) if open_ else {"due_at": None, "overdue": False, "label": ""}
        refusals = 0
        if hasattr(A, "decision_is_negative"):
            refusals = sum(1 for r in db().execute(
                "SELECT template_kind, answers_json FROM packet_items WHERE packet_id=? AND template_kind IN "
                "('layout','drawing') AND signed_at IS NOT NULL", (p["id"],)) if A.decision_is_negative(r[0], r[1]))
        fu = A.last_followup(p) or None
        forms = [r[0] for r in db().execute("SELECT template_name FROM packet_items WHERE packet_id=? ORDER BY position",
                                            (p["id"],))]
        keys = p.keys()
        return {
            "id": p["id"], "client": p["client_name"], "email": p["client_email"] or "",
            "so": p["so_number"] if "so_number" in keys else "", "rep": p["sales_rep"] if "sales_rep" in keys else "",
            "by": p["created_by"] or "", "by_id": p["created_by_id"] if "created_by_id" in keys else None,
            "state": st, "label": LABELS.get(st, st), "signed": signed, "total": total, "refusals": refusals,
            "missing": A.still_needed(p["id"]) if st not in ("completed", "voided") and hasattr(A, "still_needed") else [],
            "waiting": [w["name"] for w in waiting_on(p["id"])] if open_ and waiting_on else [],
            "forms": forms, "created_at": p["created_at"], "sent_at": p["sent_at"], "completed_at": p["completed_at"],
            "voided_at": p["voided_at"], "archived": bool(p["archived_at"]) if "archived_at" in keys else False,
            "sent_day": day(p["sent_at"]), "done_day": day(p["completed_at"]),
            "deadline": {"label": dl.get("label") or "", "overdue": bool(dl.get("overdue")), "due_at": dl.get("due_at")},
            "fu": {"n": fu.get("n"), "day": day(fu.get("at")), "note": fu.get("note") or ""} if fu else None,
            "url": url_for("packet_detail", pid=p["id"]),
        }

    def charts(rows):
        now = datetime.now(timezone.utc)
        local_now = A.to_local(now)
        monday = (local_now - timedelta(days=local_now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        weeks = []
        for k in range(WEEKS - 1, -1, -1):
            start = monday - timedelta(weeks=k)
            end = start + timedelta(weeks=1)
            s_utc, e_utc = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
            sent = sum(1 for r in rows if (d := parse(r["sent_at"])) and s_utc <= d < e_utc)
            done = sum(1 for r in rows if (d := parse(r["completed_at"])) and s_utc <= d < e_utc)
            weeks.append({"start": s_utc.isoformat(), "end": e_utc.isoformat(), "label": start.strftime("%b %-d"),
                          "sent": sent, "signed": done})
        cutoff = now - timedelta(days=90)
        spans = [(parse(r["completed_at"]) - parse(r["sent_at"])).total_seconds() / 3600 for r in rows
                 if r["completed_at"] and r["sent_at"] and parse(r["completed_at"]) >= cutoff
                 and parse(r["completed_at"]) >= parse(r["sent_at"])]
        within = sum(1 for h in spans if h <= A.SIGN_WITHIN_HOURS)
        return {"weeks": weeks,
                "median_hours": round(statistics.median(spans), 1) if spans else None,
                "within_pct": round(100 * within / len(spans)) if spans else None,
                "completed_90d": len(spans), "sign_hours": A.SIGN_WITHIN_HOURS}

    @app.route("/dashboard.json")
    @A.login_required
    def dashboard_json():
        v = version()
        if request.args.get("v") == v:
            return jsonify({"unchanged": True, "version": v})
        cond, args = A.packet_scope()
        ps = db().execute(f"SELECT * FROM packets WHERE {cond} ORDER BY COALESCE(sent_at, created_at) DESC, id DESC "
                          f"LIMIT 3000", args).fetchall()
        rows = [row(p) for p in ps]
        return jsonify({"version": v, "packets": rows, "charts": charts(rows), "manager": A.is_manager(),
                        "now": datetime.now(timezone.utc).isoformat()})

    def get_packet(pid):
        cond, args = A.packet_scope()
        return db().execute(f"SELECT * FROM packets WHERE id=? AND {cond}", [pid] + args).fetchone() or abort(404)

    @app.route("/packets/<int:pid>/preview.json")
    @A.login_required
    def packet_preview(pid):
        p = get_packet(pid)
        items = [{"name": r["template_name"], "signed": bool(r["signed_at"]), "day": day(r["signed_at"]),
                  "by": r["signer_name"] or "", "sign": bool(r["requires_signature"])}
                 for r in db().execute("SELECT * FROM packet_items WHERE packet_id=? ORDER BY position", (pid,))]
        events = [{"label": EVENT_LABELS.get(e["type"], e["type"].title()), "detail": e["detail"] or "",
                   "when": A.app.jinja_env.filters["when"](e["at"])}
                  for e in db().execute("SELECT * FROM events WHERE packet_id=? ORDER BY at DESC, id DESC LIMIT 8", (pid,))]
        r = row(p)
        r.update(items=items, events=events, message=p["message"] or "", access_code=bool(p["access_code"]),
                 link=(A.BASE_URL or request.url_root.rstrip("/")) + f"/s/{p['token']}",
                 expires_day=day(p["expires_at"]), emailed=A.app.jinja_env.globals["already_emailed"](pid))
        return jsonify(r)

    @app.route("/packets/<int:pid>/quick.json")
    @A.login_required
    def packet_quick(pid):
        """The ready-made email/text for a packet (a reminder once it has been sent from someone's email)."""
        p = get_packet(pid)
        reminder = A.app.jinja_env.globals["already_emailed"](pid)
        qs = A.app.jinja_env.globals["quick_message"](p, None, reminder)
        return jsonify({"mailto": qs["mailto"], "html": qs["html"], "text": qs["body"], "short": qs["short"],
                        "link": qs["link"], "reminder": bool(reminder), "client": p["client_name"],
                        "nudge": url_for("packet_nudge", pid=pid), "open": p["status"] not in ("completed", "voided")})

    @app.route("/packets/bulk", methods=["POST"])
    @A.login_required
    def packets_bulk():
        data = request.get_json(silent=True) or {}
        ids = [int(x) for x in data.get("ids") or [] if str(x).isdigit()][:500]
        action = data.get("action")
        cond, args = A.packet_scope()
        mine = [r[0] for r in db().execute(f"SELECT id FROM packets WHERE {cond} AND id IN ({','.join('?' * len(ids))})",
                                            args + ids)] if ids else []
        if action not in ("archive", "unarchive") or not mine:
            return jsonify({"error": "Pick at least one packet."}), 400
        for pid in mine:
            if action == "archive":
                db().execute("UPDATE packets SET archived_at=? WHERE id=? AND archived_at IS NULL", (A.now(), pid))
                A.log_event(pid, "archived", f"Archived · by {A.actor()}")
            else:
                db().execute("UPDATE packets SET archived_at=NULL WHERE id=?", (pid,))
                A.log_event(pid, "archived", f"Taken out of the archive · by {A.actor()}")
        db().commit()
        n = len(mine)
        return jsonify({"ok": True, "count": n,
                        "message": f"{n} packet{'s' if n != 1 else ''} {'archived' if action == 'archive' else 'back on the dashboard'}."})
