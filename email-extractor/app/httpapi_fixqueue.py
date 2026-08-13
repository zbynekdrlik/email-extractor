"""The fix-queue feature — flag a message, list the queue, resolve it (#268 krok 7).

Moved VERBATIM out of `app/httpapi.py` (no behavior change) — see the design comment on
#268 for exactly what moved and why. Reunites `api_fix`/`api_fix_queue`/
`api_fix_resolve`, which the pre-split file had spread across two regions ~850 lines
apart — a purely historical accident of the order the routes were added over time, not
a design choice (see the #268 plan's own step-7 note).
"""
from __future__ import annotations

import logging

from flask import Flask, abort, jsonify, request
from psycopg.types.json import Json

from . import db
from .httpapi_common import CATEGORIES, FIX_STATUSES, PROBLEM_TYPES, Deps

log = logging.getLogger("email_extractor.httpapi")


def register(app: Flask, deps: Deps) -> None:
    @app.post("/api/message/<int:mid>/fix")
    def api_fix(mid: int):
        body = request.get_json(force=True, silent=True) or {}
        ptype = body.get("problem_type")
        if ptype not in PROBLEM_TYPES:
            abort(400)
        expected = body.get("expected_category")
        if expected is not None and expected not in CATEGORIES:
            abort(400)
        desc = (body.get("description") or "").strip()
        # One transaction: the fix row and its timeline event commit together, so a
        # failed second write cannot leave an orphan fix row that a client retry
        # then duplicates (#25).
        with deps.db_tx() as c:
            m = c.execute(
                """SELECT message_id, subject, category, proc_status, proc_outcome
                   FROM messages WHERE id=%s""", (mid,)).fetchone()
            if not m:
                abort(404)
            snapshot = {"subject": m[1], "category": m[2],
                        "proc_status": m[3], "proc_outcome": m[4]}
            fid = c.execute(
                """INSERT INTO fix_requests
                       (message_id, problem_type, expected_category, description,
                        snapshot, created_by)
                   VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                (m[0], ptype, expected, desc, Json(snapshot), "dashboard")).fetchone()[0]
            # rollup=False: flagging an email for fixing is a side annotation; it must
            # not overwrite the message's real proc_status (a done order stays done).
            db.log_event(c, m[0], "dashboard", "fix_requested", "review",
                         outcome="na opravu: " + ptype + (f" → {expected}" if expected else ""),
                         detail={"fix_id": fid, "problem_type": ptype,
                                 "expected_category": expected}, rollup=False)
        log.info("fix_requested #%s type=%s -> fix #%s", mid, ptype, fid)
        return jsonify(ok=True, id=mid, fix_id=fid)

    @app.get("/api/fix-queue")
    def api_fix_queue():
        status = request.args.get("status", "")
        try:
            offset = max(0, int(request.args.get("offset", 0)))
        except ValueError:
            offset = 0
        try:
            limit = min(200, max(1, int(request.args.get("limit", 50))))
        except ValueError:
            limit = 50
        where, params = [], []
        if status:
            where.append("f.status = %s")
            params.append(status)
        wsql = ("WHERE " + " AND ".join(where)) if where else ""
        with deps.db() as c:
            total = c.execute(
                f"SELECT count(*) FROM fix_requests f {wsql}", params).fetchone()[0]
            rows = c.execute(
                f"""SELECT f.id, f.message_id, f.problem_type, f.expected_category,
                           f.description, f.status, f.created_at, f.created_by,
                           f.resolved_at, f.resolution,
                           m.id, m.subject, m.from_addr, m.category
                    FROM fix_requests f
                    LEFT JOIN messages m ON m.message_id = f.message_id
                    {wsql} ORDER BY f.id DESC LIMIT %s OFFSET %s""",
                params + [limit, offset]).fetchall()
        return jsonify(total=total, offset=offset, limit=limit, items=[{
            "id": r[0], "message_id": r[1], "problem_type": r[2], "expected_category": r[3],
            "description": r[4], "status": r[5],
            "created_at": r[6].isoformat() if r[6] else None, "created_by": r[7],
            "resolved_at": r[8].isoformat() if r[8] else None, "resolution": r[9],
            "msg_id": r[10], "subject": r[11], "from": r[12], "category": r[13],
        } for r in rows])

    @app.post("/api/fix/<int:fid>/resolve")
    def api_fix_resolve(fid: int):
        body = request.get_json(force=True, silent=True) or {}
        status = body.get("status", "fixed")
        if status not in FIX_STATUSES:
            abort(400)
        resolution = (body.get("resolution") or "").strip()
        with deps.db() as c:
            row = c.execute("SELECT message_id FROM fix_requests WHERE id=%s",
                            (fid,)).fetchone()
            if not row:
                abort(404)
            resolved = "now()" if status in ("fixed", "wontfix") else "NULL"
            c.execute(
                f"UPDATE fix_requests SET status=%s, resolution=%s, resolved_at={resolved} "
                f"WHERE id=%s", (status, resolution, fid))
            db.log_event(c, row[0], "dashboard", "fix_resolved", "ok",
                         outcome=f"fix #{fid} → {status}" + (f": {resolution}" if resolution else ""),
                         detail={"fix_id": fid, "status": status, "resolution": resolution},
                         rollup=False)
        log.info("fix #%s resolved -> %s", fid, status)
        return jsonify(ok=True, id=fid, status=status)
