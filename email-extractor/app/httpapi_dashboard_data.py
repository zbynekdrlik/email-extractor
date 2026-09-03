"""Dashboard's main data API — message list/search + detail, plus the operator's
manual actions (reclassify, reprocess) (#268 krok 6).

Moved VERBATIM out of `app/httpapi.py` (no behavior change) — see the design comment
on #268 for exactly what moved and why. `_busy` moves here too — it is used ONLY by
the two operator-action endpoints below (`api_reclassify`/`api_reprocess`), nowhere
else in the file. It has no dependency on `cfg`/`_db`/`data_dir` (only `db.
active_claim`/`db.CLAIM_STALE_MINUTES`/`log`), so — like `_parse_emails_field` in
`httpapi_common.py` before it — it becomes a plain module-level function rather than
staying a nested closure: the route functions below (nested inside `register()`)
resolve it via ordinary module scope, same as every other split step in this chain.
"""
from __future__ import annotations

import logging

from flask import Flask, abort, jsonify, request

from . import db
from .httpapi_common import CATEGORIES, Deps, _escape_like, _valid_date

log = logging.getLogger("email_extractor.httpapi")


def _busy(mid: int, c):
    """409 body when an n8n worker holds this message, else None. Clearing its
    claim would let a second worker re-claim it → the same order processed and
    forwarded twice (#25)."""
    held = db.active_claim(c, mid)
    if held is None:
        return None
    log.info("operator action on #%s refused — claimed by a worker since %s", mid, held)
    return jsonify(error=f"Mail sa práve spracúva (od {held:%H:%M}). "
                         f"Skús to znova po dokončení, najneskôr za "
                         f"{db.CLAIM_STALE_MINUTES} minút.",
                   claimed_at=held.isoformat()), 409


def register(app: Flask, deps: Deps) -> None:
    @app.get("/api/messages")
    def api_messages():
        cat = request.args.get("category", "")
        state = request.args.get("state", "")       # done|review|error|processing|onfix
        rev = request.args.get("reviewed", "")      # no|confirmed|corrected
        q = (request.args.get("q", "") or "").strip()
        dfrom = request.args.get("from", "")
        dto = request.args.get("to", "")
        try:
            offset = max(0, int(request.args.get("offset", 0)))
        except ValueError:
            offset = 0
        try:
            limit = min(200, max(1, int(request.args.get("limit", 50))))
        except ValueError:
            limit = 50

        where, params = [], []
        if cat:
            where.append("m.category = %s")
            params.append(cat)
        if state == "done":
            where.append("m.processed = true")
        elif state == "review":
            # #238 review: a DL run that is honestly "partial" (some but not all
            # documents made it) needs the same warehouse attention as "review" — it
            # must not silently fall out of the "needs review" chip just because its
            # proc_status spells the outcome differently.
            where.append("m.proc_status IN ('review', 'partial')")
        elif state == "error":
            where.append("m.proc_status = 'error'")
        elif state == "processing":
            where.append("m.processing_at IS NOT NULL AND m.processed = false")
        elif state == "onfix":
            where.append("EXISTS (SELECT 1 FROM fix_requests f "
                         "WHERE f.message_id = m.message_id AND f.status = 'open')")
        if rev == "no":
            where.append("m.review_status IS NULL")
        elif rev in ("confirmed", "corrected"):
            where.append("m.review_status = %s")
            params.append(rev)
        if q:
            where.append(
                "(m.subject ILIKE %s OR m.from_addr ILIKE %s OR m.from_name ILIKE %s "
                "OR m.body_text ILIKE %s OR m.combined_text ILIKE %s "
                "OR EXISTS (SELECT 1 FROM attachments a WHERE a.message_id = m.message_id "
                "AND a.extracted_text ILIKE %s))")
            like = f"%{_escape_like(q)}%"
            params += [like, like, like, like, like, like]
        if dfrom:
            if not _valid_date(dfrom):
                abort(400)
            where.append("m.created_at >= %s::date")
            params.append(dfrom)
        if dto:
            if not _valid_date(dto):
                abort(400)
            where.append("m.created_at < (%s::date + 1)")   # inclusive of the whole day
            params.append(dto)
        wsql = ("WHERE " + " AND ".join(where)) if where else ""

        with deps.db() as c:
            total = c.execute(
                f"SELECT count(*) FROM messages m {wsql}", params).fetchone()[0]
            rows = c.execute(
                f"""SELECT m.id, m.sent_at, m.created_at, m.from_addr, m.from_name, m.subject,
                           m.category, m.original_category, m.review_status, m.processed,
                           m.has_attachments, m.proc_status, m.proc_stage, m.proc_outcome,
                           m.last_event_at, m.processing_at,
                           EXISTS (SELECT 1 FROM fix_requests f
                                   WHERE f.message_id = m.message_id AND f.status='open') AS on_fix
                    FROM messages m {wsql}
                    ORDER BY m.id DESC LIMIT %s OFFSET %s""",
                params + [limit, offset]).fetchall()
            cnt = c.execute(
                """SELECT count(*) AS total,
                          count(*) FILTER (WHERE processed) AS done,
                          count(*) FILTER (WHERE proc_status IN ('review', 'partial')) AS review,
                          count(*) FILTER (WHERE proc_status='error') AS error,
                          count(*) FILTER (WHERE processing_at IS NOT NULL AND NOT processed) AS proc
                   FROM messages""").fetchone()
            on_fix = c.execute(
                "SELECT count(DISTINCT message_id) FROM fix_requests WHERE status='open'").fetchone()[0]
            cat_counts = dict(c.execute(
                "SELECT COALESCE(category,'(none)'), count(*) FROM messages GROUP BY category").fetchall())

        items = [{
            "id": r[0], "sent_at": r[1],
            "created_at": r[2].isoformat() if r[2] else None,
            "from": r[3], "from_name": r[4], "subject": r[5], "category": r[6],
            "original_category": r[7], "review_status": r[8], "processed": r[9],
            "has_attachments": r[10], "proc_status": r[11], "proc_stage": r[12],
            "proc_outcome": r[13],
            "last_event_at": r[14].isoformat() if r[14] else None,
            "processing": (r[15] is not None) and not r[9], "on_fix": r[16],
        } for r in rows]
        return jsonify(
            total=total, offset=offset, limit=limit, items=items, categories=CATEGORIES,
            counts={"total": cnt[0], "done": cnt[1], "review": cnt[2],
                    "error": cnt[3], "processing": cnt[4], "on_fix": on_fix},
            category_counts=cat_counts)

    @app.get("/api/message/<int:mid>")
    def api_message(mid: int):
        with deps.db() as c:
            m = c.execute(
                """SELECT id, message_id, from_addr, from_name, to_addrs, cc_addrs, subject,
                          sent_at, created_at, body_text, combined_text, category,
                          original_category, needs_vision, processed, processing_at,
                          review_status, proc_status, proc_stage, proc_outcome, last_event_at,
                          attempts, edi_file, orion_path, odoo_url, forwarded_to, error, status
                   FROM messages WHERE id = %s""", (mid,)).fetchone()
            if not m:
                abort(404)
            atts = c.execute(
                """SELECT idx, filename, mime, size, method, ocr_conf, pages,
                          needs_vision, flag, left(extracted_text, 8000)
                   FROM attachments WHERE message_id = %s ORDER BY idx""", (m[1],)).fetchall()
            events = c.execute(
                """SELECT ts, workflow, stage, status, outcome, detail
                   FROM email_events WHERE message_id = %s ORDER BY ts, id""", (m[1],)).fetchall()
            fixes = c.execute(
                """SELECT id, problem_type, expected_category, description, status,
                          created_at, created_by, resolved_at, resolution
                   FROM fix_requests WHERE message_id = %s ORDER BY id DESC""", (m[1],)).fetchall()
        return jsonify(
            id=m[0], message_id=m[1], from_addr=m[2], from_name=m[3], to_addrs=m[4],
            cc_addrs=m[5], subject=m[6], sent_at=m[7],
            created_at=m[8].isoformat() if m[8] else None,
            body_text=m[9], combined_text=m[10], category=m[11], original_category=m[12],
            needs_vision=m[13], processed=m[14],
            processing=(m[15] is not None) and not m[14],
            review_status=m[16], proc_status=m[17], proc_stage=m[18], proc_outcome=m[19],
            last_event_at=m[20].isoformat() if m[20] else None, attempts=m[21],
            edi_file=m[22], orion_path=m[23], odoo_url=m[24], forwarded_to=m[25],
            error=m[26], status=m[27], categories=CATEGORIES,
            attachments=[{
                "idx": a[0], "filename": a[1], "mime": a[2], "size": a[3], "method": a[4],
                "ocr_conf": a[5], "pages": a[6], "needs_vision": a[7], "flag": a[8],
                "extracted_text": a[9],
            } for a in atts],
            events=[{
                "ts": e[0].isoformat() if e[0] else None, "workflow": e[1], "stage": e[2],
                "status": e[3], "outcome": e[4], "detail": e[5],
            } for e in events],
            fixes=[{
                "id": f[0], "problem_type": f[1], "expected_category": f[2], "description": f[3],
                "status": f[4], "created_at": f[5].isoformat() if f[5] else None,
                "created_by": f[6], "resolved_at": f[7].isoformat() if f[7] else None,
                "resolution": f[8],
            } for f in fixes])

    # ---- operator actions ----

    @app.post("/api/message/<int:mid>/reclassify")
    def api_reclassify(mid: int):
        body = request.get_json(force=True, silent=True) or {}
        cat = body.get("category")
        if cat not in CATEGORIES:
            abort(400)
        with deps.db() as c:
            m = c.execute("SELECT message_id, category FROM messages WHERE id=%s",
                          (mid,)).fetchone()
            if not m:
                abort(404)
            busy = _busy(mid, c)
            if busy:
                return busy
            c.execute(
                """UPDATE messages
                   SET original_category = COALESCE(original_category, category),
                       category = %s, human_reviewed = true, review_status = 'corrected',
                       corrected_at = now(), processed = false, processed_at = NULL,
                       processed_by = NULL, processing_at = NULL, error = NULL
                   WHERE id = %s""", (cat, mid))
            # rollup=False: a reclassify is an operator action, not a pipeline stage —
            # it must not overwrite proc_status (the real state set by processing).
            db.log_event(c, m[0], "dashboard", "reclassified", "ok",
                         outcome=f"preklasifikované {m[1]} → {cat}",
                         detail={"from": m[1], "to": cat}, rollup=False)
        log.info("reclassify #%s %s -> %s", mid, m[1], cat)
        return jsonify(ok=True, id=mid, category=cat)

    @app.post("/api/message/<int:mid>/reprocess")
    def api_reprocess(mid: int):
        with deps.db() as c:
            m = c.execute("SELECT message_id FROM messages WHERE id=%s", (mid,)).fetchone()
            if not m:
                abort(404)
            busy = _busy(mid, c)
            if busy:
                return busy
            c.execute(
                """UPDATE messages SET processed = false, processed_at = NULL,
                   processed_by = NULL, processing_at = NULL, error = NULL
                   WHERE id = %s""", (mid,))
            db.log_event(c, m[0], "dashboard", "requeued", "ok",
                         outcome="manuálne preposlané na spracovanie", rollup=False)
        log.info("reprocess #%s", mid)
        return jsonify(ok=True, id=mid)

    # #376: the "Zahodené AI (14 dní)" dashboard section + its one restore action.
    @app.get("/api/orders/discarded")
    def api_orders_discarded():
        """The mails the AI-not-order gate discarded to `no_processing` in the last 14 days —
        listed for the operator to eyeball and, if wrong, restore. Keyed on the STABLE
        `processed_by='ai-not-order'` marker (never a text LIKE), so a promo-filtered or
        hand-reclassified `no_processing` mail never shows up here."""
        with deps.db() as c:
            rows = c.execute(
                """SELECT id, from_addr, from_name, subject, proc_outcome, processed_at
                     FROM messages
                    WHERE processed_by = 'ai-not-order'
                      AND processed_at >= now() - interval '14 days'
                    ORDER BY processed_at DESC LIMIT 200""").fetchall()
        items = [{"id": r[0], "from": r[1], "from_name": r[2], "subject": r[3],
                  "reason": r[4],
                  "discarded_at": r[5].isoformat() if r[5] else None} for r in rows]
        return jsonify(total=len(items), items=items)

    @app.post("/api/message/<int:mid>/restore")
    def api_restore(mid: int):
        """Undo an AI-not-order discard: put the mail back to its original category and
        re-queue it. It re-extracts (empty again), and the discard gate's rule-6 loop guard —
        which reads THIS `stage='restore'` event via NOT EXISTS — refuses a second
        auto-discard, so the mail lands on the warehouse question exactly as today. Scoped to
        `category='no_processing'` so it can never disturb an already-live message."""
        with deps.db() as c:
            row = c.execute(
                """UPDATE messages
                      SET category = COALESCE(original_category, category),
                          processed = false, processed_at = NULL, processed_by = NULL,
                          processing_at = NULL, error = NULL
                    WHERE id = %s AND category = 'no_processing'
                    RETURNING id, message_id""", (mid,)).fetchone()
            if not row:
                abort(404)
            db.log_event(c, row[1], "dashboard", "restore", "ok",
                         outcome="Obnovené operátorom — daj na nástenku", rollup=True)
        log.info("restore #%s", mid)
        return jsonify(ok=True, id=mid)
