"""Internal HTTP API + human review UI.

- /health, /version
- /files/<mid>/<idx>, /eml/<mid>            (originals for n8n AI-Vision / forwarding)
- /review                                   (human review web page)
- /review/list, /review/detail, /review/correct, /review/confirm, /review/processed

Human actions:
- confirm  -> review_status='confirmed', category unchanged (this one is right)
- correct  -> review_status='corrected', category := new, original_category kept,
              processed reset so the terminal workflow re-handles it
Both set human_reviewed=true. Confirmed + corrected = labelled set to score/tune the classifier.
"""
from __future__ import annotations

import os
import threading
from datetime import date
from pathlib import Path

import psycopg
from flask import Flask, abort, jsonify, redirect, request, send_file, session
from psycopg.types.json import Json

from . import __version__, db
from .store import safe_id

CATEGORIES = ["ai_orders", "invoices", "reklamacie", "dodacie_listy",
              "static_orders", "human_processing", "no_processing"]
PROBLEM_TYPES = ["mis_sorted", "mis_processed", "other"]
FIX_STATUSES = ["open", "in_progress", "fixed", "wontfix"]

def _valid_date(s: str) -> bool:
    """True iff s is a real ISO date (YYYY-MM-DD); rejects bad months/days."""
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


def _escape_like(s: str) -> str:
    """Escape LIKE/ILIKE metacharacters so user input is a literal substring."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _persistent_secret(data_dir: Path) -> bytes:
    """Stable Flask session key when secret_key is unset: persist one on the
    data volume so sessions survive restarts (instead of a per-process random
    key that logs everyone out on every restart)."""
    f = data_dir / ".session_secret"
    try:
        if f.exists():
            return f.read_bytes()
        data_dir.mkdir(parents=True, exist_ok=True)
        s = os.urandom(32)
        f.write_bytes(s)
        return s
    except OSError:
        return os.urandom(32)   # read-only fs fallback: ephemeral key


def create_app(cfg) -> Flask:
    app = Flask(__name__)
    data_dir = Path(cfg.data_dir)
    app.secret_key = cfg.secret_key or _persistent_secret(data_dir)

    def _token_ok():
        tok = request.args.get("token") or request.headers.get("X-Token")
        return bool(cfg.api_token) and tok == cfg.api_token

    def _authorized():
        # A logged-in human OR a valid machine token; OR — only when NO auth at
        # all is configured — open (pure dev mode).
        if session.get("auth") or _token_ok():
            return True
        return not cfg.api_token and not cfg.dash_password

    def _auth():
        # Used by the file APIs + legacy /review/* routes (they self-guard).
        if not _authorized():
            abort(403)

    def _db():
        return psycopg.connect(cfg.pg_dsn, autocommit=True)

    @app.before_request
    def _gate():
        p = request.path
        # Open, or self-guarded by their own in-route _auth() (file APIs, legacy
        # /review/*). The /review page itself is a public HTML shell; its data
        # endpoints enforce _auth().
        if (p in ("/health", "/version", "/login", "/logout")
                or p.startswith("/static") or p.startswith("/review")
                or p.startswith("/files") or p.startswith("/eml")):
            return None
        # New dashboard surface ("/", "/api/*"): require a session or token.
        if _authorized():
            return None
        if p.startswith("/api/"):
            return jsonify(error="auth required"), 401
        return redirect("/login")

    @app.get("/login")
    def login_page():
        return LOGIN_HTML

    @app.post("/login")
    def login_submit():
        body = request.form or (request.get_json(silent=True) or {})
        pw = body.get("password", "")
        if cfg.dash_password and pw == cfg.dash_password:
            session["auth"] = True
            return redirect("/")
        return LOGIN_HTML.replace("<!--ERR-->",
                                  '<div class="err">Nesprávne heslo</div>'), 401

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect("/login")

    @app.get("/health")
    def health():
        return jsonify(ok=True, version=__version__)

    @app.get("/version")
    def version():
        return __version__

    @app.get("/files/<mid>/<int:idx>")
    def get_file(mid: str, idx: int):
        _auth()
        matches = sorted((data_dir / safe_id(mid)).glob(f"att{idx}__*"))
        if not matches:
            abort(404)
        return send_file(matches[0])

    @app.get("/eml/<mid>")
    def get_eml(mid: str):
        _auth()
        path = data_dir / safe_id(mid) / "raw.eml"
        if not path.exists():
            abort(404)
        return send_file(path, mimetype="message/rfc822")

    @app.get("/review/list")
    def review_list():
        _auth()
        cat = request.args.get("category", "")
        proc = request.args.get("processed", "")
        rev = request.args.get("reviewed", "")   # '', 'no', 'confirmed', 'corrected'
        q = (request.args.get("q", "") or "").strip()
        try:
            offset = max(0, int(request.args.get("offset", 0)))
        except ValueError:
            offset = 0
        where, params = [], []
        if cat:
            where.append("category = %s")
            params.append(cat)
        if proc in ("true", "false"):
            where.append("processed = %s")
            params.append(proc == "true")
        if rev == "no":
            where.append("review_status IS NULL")
        elif rev in ("confirmed", "corrected"):
            where.append("review_status = %s")
            params.append(rev)
        if q:
            where.append("(from_addr ILIKE %s OR subject ILIKE %s)")
            like = f"%{_escape_like(q)}%"
            params += [like, like]
        wsql = ("WHERE " + " AND ".join(where)) if where else ""
        with _db() as c:
            counts = dict(c.execute(
                "SELECT COALESCE(category,'(none)'), count(*) FROM messages GROUP BY category").fetchall())
            grand = c.execute("SELECT count(*) FROM messages").fetchone()[0]
            reviewed = c.execute("SELECT count(*) FROM messages WHERE review_status IS NOT NULL").fetchone()[0]
            total = c.execute(f"SELECT count(*) FROM messages {wsql}", params).fetchone()[0]
            rows = c.execute(
                f"""SELECT id, sent_at, from_addr, subject, category, original_category,
                           review_status, processed, has_attachments
                    FROM messages {wsql}
                    ORDER BY id DESC LIMIT 50 OFFSET %s""", params + [offset]).fetchall()
        items = [{
            "id": r[0], "sent_at": r[1], "from": r[2], "subject": r[3],
            "category": r[4], "original_category": r[5], "review_status": r[6],
            "processed": r[7], "has_attachments": r[8],
        } for r in rows]
        return jsonify(total=total, offset=offset, counts=counts, grand=grand,
                       reviewed=reviewed, categories=CATEGORIES, items=items)

    @app.get("/review/detail")
    def review_detail():
        _auth()
        try:
            mid = int(request.args.get("id"))
        except (TypeError, ValueError):
            abort(400)
        with _db() as c:
            m = c.execute(
                """SELECT id, message_id, from_addr, from_name, to_addrs, cc_addrs,
                          subject, sent_at, body_text, combined_text, category,
                          original_category, needs_vision, processed, review_status
                   FROM messages WHERE id = %s""", (mid,)).fetchone()
            if not m:
                abort(404)
            atts = c.execute(
                """SELECT idx, filename, mime, size, method, ocr_conf, pages,
                          needs_vision, flag, left(extracted_text, 6000)
                   FROM attachments WHERE message_id = %s ORDER BY idx""", (m[1],)).fetchall()
        return jsonify(
            id=m[0], message_id=m[1], from_addr=m[2], from_name=m[3], to_addrs=m[4],
            cc_addrs=m[5], subject=m[6], sent_at=m[7], body_text=m[8], combined_text=m[9],
            category=m[10], original_category=m[11], needs_vision=m[12], processed=m[13],
            review_status=m[14], categories=CATEGORIES,
            attachments=[{
                "idx": a[0], "filename": a[1], "mime": a[2], "size": a[3], "method": a[4],
                "ocr_conf": a[5], "pages": a[6], "needs_vision": a[7], "flag": a[8],
                "extracted_text": a[9],
            } for a in atts])

    @app.post("/review/confirm")
    def review_confirm():
        _auth()
        body = request.get_json(force=True, silent=True) or {}
        mid = body.get("id")
        if not isinstance(mid, int):
            abort(400)
        with _db() as c:
            c.execute(
                "UPDATE messages SET human_reviewed = true, review_status = 'confirmed', corrected_at = now() WHERE id = %s",
                (mid,))
        return jsonify(ok=True, id=mid, review_status="confirmed")

    @app.post("/review/correct")
    def review_correct():
        _auth()
        body = request.get_json(force=True, silent=True) or {}
        mid, cat = body.get("id"), body.get("category")
        if not isinstance(mid, int) or cat not in CATEGORIES:
            abort(400)
        with _db() as c:
            c.execute(
                """UPDATE messages
                   SET original_category = COALESCE(original_category, category),
                       category = %s, human_reviewed = true, review_status = 'corrected',
                       corrected_at = now(), processed = false, processed_at = NULL, processed_by = NULL
                   WHERE id = %s""", (cat, mid))
        return jsonify(ok=True, id=mid, category=cat, review_status="corrected")

    @app.post("/review/processed")
    def review_processed():
        _auth()
        body = request.get_json(force=True, silent=True) or {}
        mid = body.get("id")
        by = body.get("by") or "workflow"
        if not isinstance(mid, int):
            abort(400)
        with _db() as c:
            c.execute(
                "UPDATE messages SET processed = true, processed_at = now(), processed_by = %s WHERE id = %s",
                (by, mid))
        return jsonify(ok=True, id=mid)

    @app.get("/review")
    def review_page():
        return REVIEW_HTML

    # ---- dashboard data API (session-gated via _gate) ----

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
            where.append("m.proc_status = 'review'")
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

        with _db() as c:
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
                          count(*) FILTER (WHERE proc_status='review') AS review,
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
        with _db() as c:
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
        with _db() as c:
            m = c.execute("SELECT message_id, category FROM messages WHERE id=%s",
                          (mid,)).fetchone()
            if not m:
                abort(404)
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
        return jsonify(ok=True, id=mid, category=cat)

    @app.post("/api/message/<int:mid>/reprocess")
    def api_reprocess(mid: int):
        with _db() as c:
            m = c.execute("SELECT message_id FROM messages WHERE id=%s", (mid,)).fetchone()
            if not m:
                abort(404)
            c.execute(
                """UPDATE messages SET processed = false, processed_at = NULL,
                   processed_by = NULL, processing_at = NULL, error = NULL
                   WHERE id = %s""", (mid,))
            db.log_event(c, m[0], "dashboard", "requeued", "ok",
                         outcome="manuálne preposlané na spracovanie", rollup=False)
        return jsonify(ok=True, id=mid)

    # ---- fix queue ----

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
        with _db() as c:
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
        with _db() as c:
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
        with _db() as c:
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
        return jsonify(ok=True, id=fid, status=status)

    @app.get("/")
    def dashboard():
        # Placeholder landing; the full single-page dashboard is built in #16.
        return DASH_PLACEHOLDER_HTML.replace("__VERSION__", __version__)

    return app


REVIEW_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Email triedenie — kontrola</title>
<style>
 body{font:14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f6f8fa;color:#1f2328}
 header{background:#24292f;color:#fff;padding:10px 16px;position:sticky;top:0;z-index:5}
 .wrap{padding:14px 16px}
 .bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
 select,input,button{font:inherit;padding:6px 8px;border:1px solid #d0d7de;border-radius:6px;background:#fff}
 button{cursor:pointer}
 .chips span{display:inline-block;background:#eaeef2;border-radius:12px;padding:2px 8px;margin:2px;font-size:12px}
 table{border-collapse:collapse;width:100%;background:#fff;border:1px solid #d0d7de;border-radius:8px;overflow:hidden}
 th,td{padding:7px 10px;border-bottom:1px solid #eaeef2;text-align:left;vertical-align:middle;font-size:13px}
 th{background:#f6f8fa;position:sticky;top:44px}
 tr.confirmed{background:#e6ffec}tr.corrected{background:#fff8c5}
 tr.row:hover{background:#f0f6ff}
 .muted{color:#57606a;font-size:12px}
 .subj{max-width:380px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer}
 .badge{font-size:11px;padding:1px 6px;border-radius:10px}
 .p1{background:#1a7f37;color:#fff}.p0{background:#d0d7de}.nv{background:#bf3989;color:#fff;margin-left:4px}
 .catsel{min-width:150px}
 .ok{background:#1a7f37;color:#fff;border-color:#1a7f37;font-weight:600}
 .stbadge{font-size:11px;padding:1px 6px;border-radius:10px}.sc{background:#1a7f37;color:#fff}.sx{background:#9a6700;color:#fff}
 #ov{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:20}
 #modal{background:#fff;max-width:900px;margin:24px auto;border-radius:10px;max-height:90vh;overflow:auto}
 #modal .mh{position:sticky;top:0;background:#24292f;color:#fff;padding:12px 16px;display:flex;justify-content:space-between;align-items:center}
 #modal .mb{padding:16px}#modal h3{margin:14px 0 6px;font-size:14px}
 #modal pre{background:#f6f8fa;border:1px solid #eaeef2;border-radius:6px;padding:10px;white-space:pre-wrap;word-break:break-word;max-height:320px;overflow:auto;font-size:12px}
 .kv{font-size:13px;margin:2px 0}.kv b{display:inline-block;min-width:70px;color:#57606a}
 .att{border:1px solid #d0d7de;border-radius:8px;padding:10px;margin:8px 0}
 .x{cursor:pointer;font-size:20px;background:none;border:none;color:#fff}
 a.btn{display:inline-block;text-decoration:none;background:#0969da;color:#fff;padding:4px 8px;border-radius:6px;font-size:12px;margin-right:6px}
</style></head><body>
<header><b>Email triedenie — ľudská kontrola</b> &nbsp;<span id="stats" class="muted"></span></header>
<div class="wrap">
 <div class="bar">
   <label>Kategória: <select id="fcat"><option value="">— všetky —</option></select></label>
   <label>Kontrola: <select id="frev"><option value="">— všetky —</option><option value="no">neskontrolované</option><option value="confirmed">potvrdené</option><option value="corrected">opravené</option></select></label>
   <label>Stav: <select id="fproc"><option value="">— všetky —</option><option value="false">nespracované</option><option value="true">spracované</option></select></label>
   <input id="fq" placeholder="hľadať odosielateľ/predmet" size="20">
   <button onclick="load(0)">Filtrovať</button>
   <span id="pager"></span>
 </div>
 <div class="chips" id="chips"></div>
 <table><thead><tr><th>kontrola</th><th>id</th><th>od</th><th>predmet (klik = detail)</th><th>kategória</th><th>spr.</th></tr></thead>
 <tbody id="rows"></tbody></table>
</div>
<div id="ov" onclick="if(event.target.id=='ov')closeM()"><div id="modal">
  <div class="mh"><span id="mtitle">Detail</span><button class="x" onclick="closeM()">×</button></div>
  <div class="mb" id="mbody"></div>
</div></div>
<script>
const token=new URLSearchParams(location.search).get('token')||'';
const H={'Content-Type':'application/json','X-Token':token};
const CATS=["ai_orders","invoices","reklamacie","dodacie_listy","static_orders","human_processing","no_processing"];
let offset=0;
function esc(s){return (s||'').toString().replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function catSelect(id,cur,big){return '<select class=catsel onchange="correct('+id+',this.value,this)"'+(big?' style="font-size:14px"':'')+'>'+CATS.map(c=>'<option'+(c===cur?' selected':'')+'>'+c+'</option>').join('')+'</select>'}
function stbadge(s){return s==='confirmed'?'<span class="stbadge sc">✓</span>':s==='corrected'?'<span class="stbadge sx">✎</span>':''}
async function load(off){
  offset=off||0;
  const p=new URLSearchParams({category:fcat.value,reviewed:frev.value,processed:fproc.value,q:fq.value,offset});
  const r=await fetch('/review/list?'+p+'&token='+encodeURIComponent(token));
  if(!r.ok){rows.innerHTML='<tr><td colspan=6>chyba '+r.status+' (token?)</td></tr>';return}
  const d=await r.json();
  if(fcat.options.length<=1){CATS.forEach(c=>{const o=document.createElement('option');o.value=o.textContent=c;fcat.appendChild(o)})}
  stats.textContent='skontrolované '+d.reviewed+' / '+d.grand+'  ·  výber: '+d.total;
  chips.innerHTML=Object.entries(d.counts).map(([k,v])=>'<span>'+esc(k)+': '+v+'</span>').join('');
  pager.innerHTML=(offset>0?'<button onclick="load('+(offset-50)+')">‹</button> ':'')+'<span class=muted>'+(offset+1)+'–'+(offset+d.items.length)+'</span>'+(d.items.length===50?' <button onclick="load('+(offset+50)+')">›</button>':'');
  rows.innerHTML=d.items.map(it=>'<tr class="row'+(it.review_status==='confirmed'?' confirmed':it.review_status==='corrected'?' corrected':'')+'" id=r'+it.id+'>'+
    '<td><button class="ok'+(it.review_status==='confirmed'?'':'')+'" onclick="confirm('+it.id+',this)">✓ OK</button></td>'+
    '<td onclick="detail('+it.id+')">'+it.id+' '+stbadge(it.review_status)+'</td>'+
    '<td onclick="detail('+it.id+')">'+esc(it.from)+'</td>'+
    '<td class=subj onclick="detail('+it.id+')">'+(it.has_attachments?'📎 ':'')+esc(it.subject)+'</td>'+
    '<td>'+catSelect(it.id,it.category)+(it.original_category&&it.original_category!==it.category?' <span class=muted>(pôv. '+esc(it.original_category)+')</span>':'')+'</td>'+
    '<td><span class="badge '+(it.processed?'p1':'p0')+'">'+(it.processed?'OK':'—')+'</span></td></tr>').join('');
}
async function confirm(id,el){
  el.disabled=true;
  const r=await fetch('/review/confirm?token='+encodeURIComponent(token),{method:'POST',headers:H,body:JSON.stringify({id})});
  el.disabled=false;
  if(r.ok){const tr=document.getElementById('r'+id);if(tr){tr.className='row confirmed';if(frev.value==='no'){tr.remove()}}}
  else alert('chyba '+r.status);
}
async function correct(id,category,el){
  el.disabled=true;
  const r=await fetch('/review/correct?token='+encodeURIComponent(token),{method:'POST',headers:H,body:JSON.stringify({id,category})});
  el.disabled=false;
  if(r.ok){const tr=document.getElementById('r'+id);if(tr){tr.className='row corrected';const b=tr.querySelector('.badge');if(b){b.className='badge p0';b.textContent='—';}if(frev.value==='no'){tr.remove()}}}
  else alert('chyba '+r.status);
}
async function detail(id){
  mbody.innerHTML='načítavam…';ov.style.display='block';
  const r=await fetch('/review/detail?id='+id+'&token='+encodeURIComponent(token));
  if(!r.ok){mbody.innerHTML='chyba '+r.status;return}
  const d=await r.json();
  mtitle.textContent='#'+d.id+' — '+(d.subject||'(bez predmetu)');
  const fb='/files/'+encodeURIComponent(d.message_id);
  const atts=(d.attachments||[]).map(a=>'<div class=att><div><b>'+esc(a.filename)+'</b> <span class=muted>'+esc(a.mime)+' · '+Math.round((a.size||0)/1024)+' KB · '+esc(a.method)+(a.ocr_conf!=null?' · OCR '+a.ocr_conf+'%':'')+'</span>'+(a.needs_vision?' <span class="badge nv">AI VISION</span>':'')+'</div>'+
    '<div style="margin:6px 0"><a class=btn target=_blank href="'+fb+'/'+a.idx+'?token='+encodeURIComponent(token)+'">Otvoriť súbor</a></div>'+
    '<pre>'+esc(a.extracted_text||'(žiadny text)')+'</pre></div>').join('')||'<div class=muted>žiadne prílohy</div>';
  mbody.innerHTML=
    '<div style="margin-bottom:8px"><button class=ok onclick="confirm('+d.id+',this);closeM()">✓ Správne</button> &nbsp; '+catSelect(d.id,d.category,true)+(d.original_category&&d.original_category!==d.category?' <span class=muted>(pôv. '+esc(d.original_category)+')</span>':'')+'</div>'+
    '<div class=kv><b>Od:</b> '+esc(d.from_name)+' &lt;'+esc(d.from_addr)+'&gt;</div>'+
    '<div class=kv><b>Komu:</b> '+esc((d.to_addrs||[]).join(', '))+'</div>'+
    ((d.cc_addrs||[]).length?'<div class=kv><b>Kópia:</b> '+esc(d.cc_addrs.join(', '))+'</div>':'')+
    '<div class=kv><b>Dátum:</b> '+esc(d.sent_at)+' &nbsp; <a class=btn target=_blank href="/eml/'+encodeURIComponent(d.message_id)+'?token='+encodeURIComponent(token)+'">Originál .eml</a></div>'+
    '<h3>Telo</h3><pre>'+esc(d.body_text||'(prázdne)')+'</pre>'+
    '<h3>Prílohy ('+(d.attachments||[]).length+')</h3>'+atts+
    '<h3>combined_text (čo videla AI)</h3><pre>'+esc(d.combined_text)+'</pre>';
}
function closeM(){ov.style.display='none'}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeM()});
load(0);
</script></body></html>"""


LOGIN_HTML = r"""<!doctype html><html lang="sk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Prihlásenie</title>
<style>
 body{font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:#0d1117;color:#e6edf3;
      display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
 form{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:28px 26px;width:300px}
 h1{font-size:17px;margin:0 0 16px}
 input{width:100%;box-sizing:border-box;padding:9px 11px;border:1px solid #30363d;border-radius:7px;
       background:#0d1117;color:#e6edf3;font:inherit;margin-bottom:12px}
 button{width:100%;padding:9px;border:0;border-radius:7px;background:#1f6feb;color:#fff;font:inherit;
        font-weight:600;cursor:pointer}
 .err{background:#3d1418;border:1px solid #cf222e;color:#ffb3ba;border-radius:7px;padding:7px 10px;
      margin-bottom:12px;font-size:13px}
</style></head><body>
<form method="post" action="/login">
  <h1>📬 Email dashboard</h1>
  <!--ERR-->
  <input type="password" name="password" placeholder="heslo" autofocus autocomplete="current-password">
  <button type="submit">Prihlásiť sa</button>
</form></body></html>"""


DASH_PLACEHOLDER_HTML = r"""<!doctype html><html lang="sk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Email dashboard</title>
<style>body{font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:#0d1117;color:#e6edf3;
 display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;margin:0;gap:10px}
 .v{color:#6e7681;font-size:12px}a{color:#58a6ff}</style></head><body>
<h1>📬 Email dashboard</h1>
<p>Dátové API beží (<a href="/api/messages">/api/messages</a>). Plné UI sa dorába (#16).</p>
<p class="v">v__VERSION__ · <a href="/logout">odhlásiť</a></p>
</body></html>"""


def start(cfg) -> None:
    app = create_app(cfg)
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=cfg.http_port, threaded=True),
        daemon=True,
    ).start()
