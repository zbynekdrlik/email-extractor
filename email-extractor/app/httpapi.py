"""Internal HTTP API + live dashboard.

Machine endpoints (token, used by n8n):
- /health, /version
- /files/<mid>/<idx>, /eml/<mid>            (originals for n8n AI-Vision / forwarding)

Dashboard (session login):
- /                                          (single-page dashboard)
- /api/messages, /api/message/<id>           (list/search + detail + timeline)
- /api/message/<id>/reclassify|reprocess|fix (operator actions)
- /api/fix-queue, /api/fix/<id>/resolve      (the fix queue Claude works)
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
        # Used by the file APIs (/files, /eml) — exempt from the gate, self-guard here.
        if not _authorized():
            abort(403)

    def _db():
        return psycopg.connect(cfg.pg_dsn, autocommit=True)

    @app.before_request
    def _gate():
        p = request.path
        # Open, or self-guarded by their own in-route _auth() (the file APIs).
        if (p in ("/health", "/version", "/login", "/logout")
                or p.startswith("/static")
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
        return DASH_HTML.replace("__VERSION__", __version__)

    return app


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


DASH_HTML = r"""<!doctype html><html lang="sk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Email dashboard</title>
<style>
 *{box-sizing:border-box}
 body{font:13px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:0;
      background:#f6f8fa;color:#1f2328;height:100vh;display:flex;flex-direction:column;overflow:hidden}
 a{color:#0969da}
 header{background:#0d1117;color:#fff;padding:8px 14px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
 header b{font-size:14px;white-space:nowrap}
 header input,header select{font:inherit;padding:5px 8px;border:1px solid #30363d;border-radius:6px;
      background:#161b22;color:#e6edf3}
 #q{min-width:220px;flex:1}
 .live{display:flex;align-items:center;gap:5px;font-size:12px;color:#3fb950;cursor:pointer;white-space:nowrap}
 .ver{color:#6e7681;font-size:11px;white-space:nowrap}
 .chips{display:flex;gap:6px;padding:7px 14px;background:#fff;border-bottom:1px solid #d0d7de;flex-wrap:wrap}
 .chip{border:0;border-radius:11px;padding:3px 10px;font:inherit;font-size:11px;cursor:pointer}
 .chip.active{outline:2px solid #0969da}
 .c-total{background:#ddf4ff;color:#0969da}.c-done{background:#dafbe1;color:#1a7f37}
 .c-review{background:#fff8c5;color:#7d4e00}.c-error{background:#ffebe9;color:#cf222e}
 .c-processing{background:#eaeef2;color:#57606a}.c-onfix{background:#ffe3f1;color:#bf3989}
 .tabs{display:flex;gap:4px;padding:6px 14px 0;background:#fff;border-bottom:1px solid #d0d7de}
 .tab{border:1px solid #d0d7de;border-bottom:0;border-radius:7px 7px 0 0;background:#f6f8fa;
      padding:5px 12px;cursor:pointer;font:inherit}
 .tab.active{background:#fff;font-weight:600}
 main{flex:1;display:flex;min-height:0}
 #list{width:42%;max-width:560px;border-right:1px solid #d0d7de;overflow:auto;background:#fff}
 .row{padding:7px 11px;border-bottom:1px solid #eaeef2;border-left:3px solid transparent;cursor:pointer}
 .row:hover{background:#f0f6ff}.row.sel{background:#eef4ff;border-left-color:#1f6feb}
 .row.s-done{border-left-color:#1a7f37}.row.s-review{border-left-color:#7d4e00}
 .row.s-error{border-left-color:#cf222e}.row.s-processing{border-left-color:#57606a}
 .row .t{display:flex;justify-content:space-between;gap:8px}
 .row .f{font-weight:600}.row .when{color:#57606a;font-size:11px;white-space:nowrap}
 .row .sub{color:#1f2328;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .pill{border-radius:9px;padding:1px 7px;font-size:11px;background:#ddf4ff;color:#0969da}
 .out{font-size:11px}.ok{color:#1a7f37}.rev{color:#7d4e00}.err{color:#cf222e}
 #detail{flex:1;overflow:auto;padding:14px 16px}
 .muted{color:#57606a}.lbl{font-size:11px;color:#57606a;text-transform:uppercase;letter-spacing:.04em;margin:14px 0 6px}
 .badge{border-radius:11px;padding:2px 9px;font-size:11px}
 .b-ok{background:#dafbe1;color:#1a7f37}.b-review{background:#fff8c5;color:#7d4e00}
 .b-error{background:#ffebe9;color:#cf222e}.b-none{background:#eaeef2;color:#57606a}
 .tl{border-left:2px solid #d0d7de;padding-left:13px;margin-left:4px}
 .tl .ev{margin-bottom:9px;position:relative}
 .tl .dot{position:absolute;left:-18px;top:2px;width:9px;height:9px;border-radius:50%;background:#57606a}
 .tl .d-ok{background:#1a7f37}.tl .d-review{background:#7d4e00}.tl .d-error{background:#cf222e}
 .att{background:#fff;border:1px solid #d0d7de;border-radius:7px;padding:6px 9px;margin:5px 0;font-size:12px}
 pre{background:#f6f8fa;border:1px solid #eaeef2;border-radius:6px;padding:9px;white-space:pre-wrap;
     word-break:break-word;max-height:280px;overflow:auto;font-size:12px;margin:0}
 .actions{display:flex;gap:7px;flex-wrap:wrap;margin:14px 0;align-items:center}
 button,select.act{font:inherit;padding:6px 11px;border:1px solid #d0d7de;border-radius:6px;background:#fff;cursor:pointer}
 .btn-blue{background:#0969da;color:#fff;border-color:#0969da;font-weight:600}
 .btn-red{background:#cf222e;color:#fff;border-color:#cf222e;font-weight:600}
 .fixrow{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:9px 11px;margin:8px 14px}
 .fixrow.resolved{opacity:.6}
 #ov{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:30;align-items:center;justify-content:center}
 #modal{background:#fff;border-radius:10px;width:440px;max-width:92vw;padding:16px}
 #modal h3{margin:0 0 10px}#modal label{display:block;margin:8px 0 3px;font-size:12px;color:#57606a}
 #modal select,#modal textarea{width:100%;font:inherit;padding:7px;border:1px solid #d0d7de;border-radius:6px}
 .empty{color:#57606a;padding:30px;text-align:center}
</style></head><body>
<header>
  <b>📬 Email dashboard</b>
  <input id="q" placeholder="hľadať: odosielateľ, predmet, telo, príloha…">
  <select id="fcat"><option value="">kategória</option></select>
  <select id="fstate"><option value="">stav</option>
    <option value="done">hotové</option><option value="review">review</option>
    <option value="error">chyba</option><option value="processing">spracúva</option>
    <option value="onfix">na oprave</option></select>
  <input id="ffrom" type="date" title="od">
  <input id="fto" type="date" title="do">
  <span class="live" id="livetog">● <span id="livelbl">LIVE</span></span>
  <span class="ver" data-testid="version">v__VERSION__</span>
  <a class="ver" href="/logout">odhlásiť</a>
</header>
<div class="chips" id="chips"></div>
<div class="tabs">
  <button class="tab active" id="tabMails" onclick="setView('mails')">Maily</button>
  <button class="tab" id="tabFix" onclick="setView('fix')">Fix fronta</button>
</div>
<main>
  <div id="list"></div>
  <div id="detail"><div class="empty">Vyber mail vľavo.</div></div>
</main>
<div id="ov" onclick="if(event.target.id=='ov')closeModal()"><div id="modal"></div></div>
<script>
const E=s=>(s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let CATS=[],sel=null,view='mails',timer=null,live=true,counts={};
async function api(path,opts){const r=await fetch(path,Object.assign({headers:{'Content-Type':'application/json'}},opts));
  if(r.status===401){location.href='/login';throw new Error('auth')}
  if(!r.ok)throw new Error(r.status);return r.json()}
function tsShort(s){if(!s)return '';return s.replace('T',' ').slice(5,16)}
function params(){const p=new URLSearchParams();
  if(q.value.trim())p.set('q',q.value.trim());
  if(fcat.value)p.set('category',fcat.value);
  if(fstate.value)p.set('state',fstate.value);
  if(ffrom.value)p.set('from',ffrom.value);
  if(fto.value)p.set('to',fto.value);
  return p}
async function loadList(){
  let d;try{d=await api('/api/messages?'+params())}catch(e){return}
  if(!CATS.length){CATS=d.categories;for(const c of CATS){const o=document.createElement('option');o.value=o.textContent=c;fcat.appendChild(o)}}
  counts=d.counts;renderChips();
  const L=document.getElementById('list');
  if(view!=='mails')return;
  if(!d.items.length){L.innerHTML='<div class="empty">Žiadne maily pre tento filter.</div>';return}
  L.innerHTML=d.items.map(it=>{
    const st=it.processed?'done':(it.proc_status==='review'?'review':it.proc_status==='error'?'error':it.processing?'processing':'');
    const out=it.on_fix?'<span class="out" style="color:#bf3989">🔧 na oprave</span>':
      (it.proc_outcome?'<span class="out '+(it.proc_status==='error'?'err':it.proc_status==='review'?'rev':'ok')+'">'+E(it.proc_outcome)+'</span>':'');
    return '<div class="row s-'+st+(sel===it.id?' sel':'')+'" onclick="openDetail('+it.id+')">'+
      '<div class="t"><span class="f">#'+it.id+' '+E(it.from||'')+'</span><span class="when">'+tsShort(it.last_event_at||it.created_at)+'</span></div>'+
      '<div class="sub">'+(it.has_attachments?'📎 ':'')+E(it.subject||'(bez predmetu)')+'</div>'+
      '<div><span class="pill">'+E(it.category||'—')+'</span> '+out+'</div></div>'}).join('')}
function renderChips(){const c=counts;const C=document.getElementById('chips');
  const def=[['','c-total','spolu',c.total],['done','c-done','✓ hotové',c.done],['review','c-review','⚠ review',c.review],
    ['error','c-error','✗ chyba',c.error],['processing','c-processing','… spracúva',c.processing],['onfix','c-onfix','🔧 na oprave',c.on_fix]];
  C.innerHTML=def.map(([v,cl,lbl,n])=>'<button class="chip '+cl+(fstate.value===v?' active':'')+'" onclick="setState(\''+v+'\')">'+lbl+' '+(n||0)+'</button>').join('')}
function setState(v){fstate.value=v;loadList()}
async function openDetail(id){
  sel=id;document.querySelectorAll('.row').forEach(r=>r.classList.toggle('sel',r.getAttribute('onclick').includes('('+id+')')));
  const D=document.getElementById('detail');D.innerHTML='<div class="empty">načítavam…</div>';
  let m;try{m=await api('/api/message/'+id)}catch(e){D.innerHTML='<div class="empty">chyba</div>';return}
  const badge=m.proc_status?('<span class="badge b-'+(m.proc_status==='ok'?'ok':m.proc_status==='review'?'review':m.proc_status==='error'?'error':'none')+'">'+E(m.proc_status)+'</span>'):
    (m.processed?'<span class="badge b-ok">hotové</span>':'<span class="badge b-none">nové</span>');
  const fb='/files/'+encodeURIComponent(m.message_id);
  const evs=(m.events||[]).map(e=>'<div class="ev"><span class="dot d-'+(e.status==='ok'?'ok':e.status==='review'?'review':e.status==='error'?'error':'')+'"></span>'+
    '<b>'+E(e.stage)+'</b> <span class="muted">'+tsShort(e.ts)+(e.workflow?' · '+E(e.workflow):'')+'</span>'+(e.outcome?'<br>'+E(e.outcome):'')+'</div>').join('')
    ||'<div class="muted">žiadne udalosti zatiaľ</div>';
  const atts=(m.attachments||[]).map(a=>'<div class="att"><b>'+E(a.filename)+'</b> <span class="muted">'+E(a.mime)+' · '+Math.round((a.size||0)/1024)+' KB · '+E(a.method||'')+(a.ocr_conf!=null?' · OCR '+a.ocr_conf+'%':'')+'</span>'+
    (a.needs_vision?' <span class="pill" style="background:#ffe3f1;color:#bf3989">VISION</span>':'')+
    ' <a target=_blank href="'+fb+'/'+a.idx+'">otvoriť</a></div>').join('')||'<div class="muted">žiadne prílohy</div>';
  const fixes=(m.fixes||[]).filter(f=>f.status==='open'||f.status==='in_progress').map(f=>'<div class="att" style="border-color:#bf3989">🔧 <b>'+E(f.problem_type)+'</b>'+(f.expected_category?' → '+E(f.expected_category):'')+(f.description?' — '+E(f.description):'')+' <span class="muted">('+E(f.status)+')</span></div>').join('');
  const opts=CATS.map(c=>'<option'+(c===m.category?' selected':'')+'>'+c+'</option>').join('');
  D.innerHTML='<div class="t" style="display:flex;justify-content:space-between;align-items:flex-start">'+
      '<div><b style="font-size:15px">#'+m.id+' — '+E(m.subject||'(bez predmetu)')+'</b>'+
      '<div class="muted">'+E(m.from_name||'')+' &lt;'+E(m.from_addr||'')+'&gt; · '+E(m.sent_at||'')+'</div></div>'+badge+'</div>'+
    '<div class="actions">'+
      '<label class="muted">kategória: <select class="act" onchange="doReclassify('+m.id+',this.value)">'+opts+'</select></label>'+
      '<button onclick="doReprocess('+m.id+')">⟳ spustiť znova</button>'+
      '<a class="ver" style="color:#0969da" target=_blank href="/eml/'+encodeURIComponent(m.message_id)+'">📄 .eml</a>'+
      '<button class="btn-red" onclick="openFix('+m.id+')">🔧 dať na opravu</button></div>'+
    (fixes?'<div>'+fixes+'</div>':'')+
    '<div class="lbl">Časová os spracovania</div><div class="tl">'+evs+'</div>'+
    '<div class="lbl">Prílohy ('+(m.attachments||[]).length+')</div>'+atts+
    '<div class="lbl">Telo</div><pre>'+E(m.body_text||'(prázdne)')+'</pre>'+
    '<div class="lbl">combined_text (čo videla AI)</div><pre>'+E(m.combined_text||'')+'</pre>'}
async function doReclassify(id,cat){try{await api('/api/message/'+id+'/reclassify',{method:'POST',body:JSON.stringify({category:cat})});await loadList();await openDetail(id)}catch(e){alert('chyba')}}
async function doReprocess(id){try{await api('/api/message/'+id+'/reprocess',{method:'POST'});await loadList();await openDetail(id)}catch(e){alert('chyba')}}
function openFix(id){
  const opts=CATS.map(c=>'<option value="'+c+'">'+c+'</option>').join('');
  document.getElementById('modal').innerHTML='<h3>🔧 Dať na opravu — #'+id+'</h3>'+
    '<label>Čo je zle?</label><select id="fxtype" onchange="document.getElementById(\'fxcatwrap\').style.display=this.value===\'mis_sorted\'?\'block\':\'none\'">'+
      '<option value="mis_processed">zle spracované</option><option value="mis_sorted">zle zaradené (sortnuté)</option><option value="other">iné</option></select>'+
    '<div id="fxcatwrap" style="display:none"><label>Správna kategória</label><select id="fxcat">'+opts+'</select></div>'+
    '<label>Poznámka pre Clauda</label><textarea id="fxdesc" rows="3" placeholder="čo presne je zle / aké by malo byť správne"></textarea>'+
    '<div class="actions"><button class="btn-red" onclick="submitFix('+id+')">Odoslať na opravu</button><button onclick="closeModal()">zrušiť</button></div>';
  document.getElementById('ov').style.display='flex'}
async function submitFix(id){
  const t=document.getElementById('fxtype').value;
  const body={problem_type:t,description:document.getElementById('fxdesc').value};
  if(t==='mis_sorted')body.expected_category=document.getElementById('fxcat').value;
  try{await api('/api/message/'+id+'/fix',{method:'POST',body:JSON.stringify(body)});closeModal();await loadList();await openDetail(id)}catch(e){alert('chyba')}}
function closeModal(){document.getElementById('ov').style.display='none'}
async function loadFix(){const D=document.getElementById('detail'),L=document.getElementById('list');
  L.innerHTML='';let d;try{d=await api('/api/fix-queue')}catch(e){return}
  if(!d.items.length){D.innerHTML='<div class="empty">Fix fronta je prázdna 🎉</div>';return}
  D.innerHTML='<div class="lbl">Fix fronta ('+d.total+')</div>'+d.items.map(f=>{
    const open=f.status==='open'||f.status==='in_progress';
    return '<div class="fixrow'+(open?'':' resolved')+'">'+
      '<div class="t" style="display:flex;justify-content:space-between"><b>🔧 #'+f.id+' — '+E(f.problem_type)+(f.expected_category?' → '+E(f.expected_category):'')+'</b><span class="muted">'+E(f.status)+'</span></div>'+
      '<div class="muted">mail #'+(f.msg_id||'?')+' · '+E(f.from||'')+' · '+E(f.subject||'')+'</div>'+
      (f.description?'<div>'+E(f.description)+'</div>':'')+
      (f.resolution?'<div class="ok">→ '+E(f.resolution)+'</div>':'')+
      (open?'<div class="actions"><button onclick="openDetail('+(f.msg_id||'null')+');setView(\'mails\')">otvoriť mail</button>'+
        '<button class="btn-blue" onclick="resolveFix('+f.id+',\'fixed\')">označiť opravené</button>'+
        '<button onclick="resolveFix('+f.id+',\'wontfix\')">neopravím</button></div>':'')+'</div>'}).join('')}
async function resolveFix(fid,status){const res=status==='fixed'?(prompt('Poznámka k oprave (voliteľné):')||''):'';
  try{await api('/api/fix/'+fid+'/resolve',{method:'POST',body:JSON.stringify({status,resolution:res})});await loadFix()}catch(e){alert('chyba')}}
function setView(v){view=v;document.getElementById('tabMails').classList.toggle('active',v==='mails');
  document.getElementById('tabFix').classList.toggle('active',v==='fix');
  if(v==='fix'){loadFix()}else{document.getElementById('detail').innerHTML='<div class="empty">Vyber mail vľavo.</div>';loadList()}}
function tick(){if(live&&document.getElementById('ov').style.display!=='flex'){if(view==='mails')loadList();else loadFix()}}
document.getElementById('livetog').onclick=()=>{live=!live;document.getElementById('livetog').style.color=live?'#3fb950':'#6e7681';document.getElementById('livelbl').textContent=live?'LIVE':'pauza'};
let deb;q.oninput=()=>{clearTimeout(deb);deb=setTimeout(loadList,350)};
for(const el of [fcat,fstate,ffrom,fto])el.onchange=loadList;
loadList();timer=setInterval(tick,5000);
</script></body></html>"""


def start(cfg) -> None:
    app = create_app(cfg)
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=cfg.http_port, threaded=True),
        daemon=True,
    ).start()
