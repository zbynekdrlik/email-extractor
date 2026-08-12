"""Internal HTTP API + live dashboard.

Machine endpoints (token, used by n8n):
- /health, /version
- /files/<mid>/<idx>, /eml/<mid>            (originals for n8n AI-Vision / forwarding)

Warehouse link (no password — a signed, unguessable URL):
- /sklad/<key>                               (grants the questions surface only)
- /otazky                                    (answer a wording with one click)

Dashboard (session login):
- /                                          (single-page dashboard)
- /api/messages, /api/message/<id>           (list/search + detail + timeline)
- /api/message/<id>/reclassify|reprocess|fix (operator actions)
- /api/fix-queue, /api/fix/<id>/resolve      (the fix queue Claude works)
"""
from __future__ import annotations

import hmac
import logging
import re
import threading
import time
import unicodedata
from datetime import date, timedelta
from pathlib import Path

import psycopg
from flask import Flask, abort, jsonify, redirect, request, send_file, session
from psycopg.types.json import Json
from werkzeug.exceptions import HTTPException

from . import __version__, db, linkutil
from .db import MAX_UID_ATTEMPTS
from .orders import dl_snapshot, memory, snapshot
from .orders import teach as _teach
from .store import message_dir

CATEGORIES = ["ai_orders", "invoices", "reklamacie", "dodacie_listy",
              "static_orders", "human_processing", "no_processing"]
PROBLEM_TYPES = ["mis_sorted", "mis_processed", "other"]
FIX_STATUSES = ["open", "in_progress", "fixed", "wontfix"]

log = logging.getLogger("email_extractor.httpapi")

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


# #234: the exact same EAN normalization/validation `snapshot.normalize_ean` uses,
# duplicated here (not imported) so both HTTP entry points can return their own precise
# 400 body BEFORE ever calling into the DB layer.
_EAN_STRIP_RE = re.compile(r"[\s\-]")


def _fold(s: str) -> str:
    """Diacritics- and case-insensitive substring match for the /znalosti card/customer
    search — a warehouse worker types "rozok" and must still find "Rožok"."""
    return "".join(c for c in unicodedata.normalize("NFD", str(s or "").lower())
                   if unicodedata.category(c) != "Mn")


# The Flask session secret + the /sklad/<key> derivation both live in `linkutil` (#139) —
# the order worker's background thread mints the SAME link with no Flask request at all,
# so the derivation must not be duplicated here.
_persistent_secret = linkutil.persistent_secret
sklad_key = linkutil.sklad_key
dl_key = linkutil.dl_key

SKLAD_ROLE = "sklad"
# What the warehouse link may reach — the questions surface, nothing else. It is an
# UNAUTHENTICATED link, so this list is the whole security boundary: never widen it to
# anything that reads mails, files or spend. `/api/orders/held` (#93) is order metadata of
# the same shape as questions/taught (customer name, delivery date, question ids) — no mail
# body, no attachment, no spend — and IS meant to be sklad-visible: the `/otazky` panel
# fetches it so the warehouse sees what it is holding up, review finding on PR #116 (the
# panel silently 401'd and never rendered for the sklad role without this).
SKLAD_PATHS = ("/otazky", "/api/orders/questions", "/api/orders/taught", "/api/orders/held")
SKLAD_ACTION = re.compile(r"^/api/orders/question/\d+/(answer|undo)$")
# #104: the same warehouse link also reaches the knowledge-base page. Same boundary rule as
# SKLAD_PATHS above — wording/gtin/card metadata only, never a mail body or an attachment.
SKLAD_ZNALOSTI_PAGE = re.compile(r"^/znalosti(/[^/]+)?$")
# #235: narrowed to the ORDERS-only knowledge (global/catalog/customers/products/clients) —
# `dl-products`/`dl-suppliers` used to be alternatives here too (since #223's dashboard-
# editing rollout), which meant the orders SKLAD_ROLE already had a real, unintended write
# path into the DL supplier/catalog data — a pre-existing gap #235's own boundary
# requirement ("the orders role must equally not gain DL write access") closes. DL
# knowledge now has its own, separate allowlist below (SKLAD_DL_ZNALOSTI_API).
SKLAD_ZNALOSTI_API = re.compile(
    r"^/api/znalosti/(global(/\d+)?|catalog|customers|customer/[^/]+(/\d+)?"
    r"|products(/[^/]+)?|clients)$")
# #235: the DL nástenka's own API-only reach — deliberately NOT the `/znalosti` PAGE (that
# template also renders orders-domain boxes: catalog/customers/clients search+edit — giving
# SKLAD_DL_ROLE the page would either expose that dead-end UI or, if the API were widened to
# match, be a real widening of her role into the orders agenda). Only the two DL-specific
# endpoints her question card's new-entry form actually calls.
SKLAD_DL_ZNALOSTI_API = re.compile(r"^/api/znalosti/(dl-products(/[^/]+)?|dl-suppliers)$")

# #231: a SECOND, independent unauthenticated link — the delivery-notes-only nástenka.
# `order_questions.kind` is the ONE discriminator between the two agendas
# (`teach.KINDS`): ORDERS_KINDS are every kind the AI-orders pipeline raises, DL_KINDS are
# the two DL ones (#202). `/api/orders/questions`/`/api/orders/taught` are DELIBERATELY
# the SAME shared endpoints both roles use (and the full-admin dashboard, unrestricted) —
# `_role_kinds()` below decides what each role is actually allowed to see/touch, so the
# security boundary never depends on which URL a client happens to call.
ORDERS_KINDS = ("item", "customer", "mail", "date", "line")
DL_KINDS = ("dl_item", "dl_supplier")
SKLAD_DL_ROLE = "sklad_dl"
SKLAD_DL_PATHS = ("/otazky-dl", "/api/orders/questions", "/api/orders/taught",
                  "/api/orders/dl/stats")
# Review finding on the #231 PR: nothing enforced that these two tuples actually
# partition EVERY registered `teach.KINDS` entry. A future kind added to that registry
# but forgotten here would silently NEVER reach either unauthenticated nástenka link
# (fail-safe direction — full admin login still sees it — but nobody would notice why
# the warehouse never gets asked). Fail loudly at import time instead, mirroring
# `teach.py`'s own `KINDS` completeness assertion right after its dict definition.
assert set(ORDERS_KINDS) | set(DL_KINDS) == set(_teach.KINDS), (
    "every teach.KINDS entry must be routed to exactly one of ORDERS_KINDS/DL_KINDS")


def _role_kinds(role: str | None) -> tuple[str, ...] | None:
    """The `kind` values a session's role may see/answer/undo. `None` = unrestricted.

    A real dash_password login (`session["auth"]`) is ALWAYS unrestricted, regardless of
    whatever `role` the SAME session might also carry — `auth` and `role` are independent
    session keys, and a real browser can end up with BOTH set: the admin dashboard's own
    link panel (`showSkladLink()`) renders both nástenka links as clickable
    `target="_blank"` `<a>` tags specifically so the operator can preview/copy them, and
    opening either one in the same cookie jar sets `role` WITHOUT ever clearing `auth`.
    `_gate()` already treats `auth` as the overriding signal (checked first, before
    `role`, in the SAME `before_request` handler) — this function must use the identical
    precedence, or a logged-in admin who merely clicked their own dashboard's link would
    silently start seeing a role-filtered question list and getting 403s on answer/undo
    (review finding on the #231 PR — caught before merge, no live incident)."""
    if session.get("auth"):
        return None
    if role == SKLAD_DL_ROLE:
        return DL_KINDS
    if role == SKLAD_ROLE:
        return ORDERS_KINDS
    return None


def create_app(cfg) -> Flask:
    app = Flask(__name__)
    data_dir = Path(cfg.data_dir)
    app.secret_key = cfg.secret_key or _persistent_secret(data_dir)
    # A year: the warehouse must never be asked to log in again, and neither must the
    # operator — Flask's default is a browser-session cookie that dies with the tab.
    app.permanent_session_lifetime = timedelta(days=365)
    key = sklad_key(app.secret_key)
    dl_link_key = dl_key(app.secret_key)
    if not cfg.dash_password:
        log.warning("dash_password is unset — the dashboard is CLOSED; "
                    "set dash_password to enable it")

    def _token_ok():
        tok = request.args.get("token") or request.headers.get("X-Token")
        return bool(cfg.api_token) and tok == cfg.api_token

    def _auth():
        # File APIs (/files, /eml): a logged-in human OR a valid machine token.
        # No open-by-default — if neither is configured the endpoint stays closed.
        if not (session.get("auth") or _token_ok()):
            abort(403)

    def _db():
        return psycopg.connect(cfg.pg_dsn, autocommit=True)

    def _db_tx():
        """One transaction: `with _db_tx() as c` commits at exit, rolls back on error.
        For routes that do several writes which must land together (#25)."""
        return psycopg.connect(cfg.pg_dsn)

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

    # ---- request + error logging (#28) ----

    @app.before_request
    def _stamp():
        request.environ["_t0"] = time.monotonic()

    @app.after_request
    def _access_log(resp):
        t0 = request.environ.get("_t0")
        ms = int((time.monotonic() - t0) * 1000) if t0 else -1
        # The path only, never the query string: /files carries ?token=<secret>.
        line = f"{request.method} {request.path} -> {resp.status_code} ({ms} ms)"
        (log.warning if resp.status_code >= 400 else log.info)(line)
        return resp

    @app.errorhandler(Exception)
    def _on_error(e):
        if isinstance(e, HTTPException):
            return e                      # 404/403/400/409 are answers, not failures
        # Without this a failing /api/* query 500s with nothing in the log at all.
        log.exception("%s %s failed: %s", request.method, request.path, e)
        return jsonify(error="Vnútorná chyba servera — podrobnosti sú v logu."), 500

    @app.before_request
    def _gate():
        p = request.path
        # Open, or self-guarded by their own in-route _auth() (the file APIs).
        if (p in ("/health", "/version", "/login", "/logout", "/favicon.ico")
                or p.startswith("/static")
                or p.startswith("/sklad/")          # the route verifies its own signature
                or p.startswith("/sklad-dl/")       # ditto, the DL nástenka link (#231)
                or p.startswith("/files") or p.startswith("/eml")):
            return None
        # Dashboard surface ("/", "/api/*"): session only — login requires a
        # configured dash_password, so an unconfigured add-on is closed, not open.
        if session.get("auth"):
            return None
        if session.get("role") == SKLAD_ROLE:
            if (p in SKLAD_PATHS or SKLAD_ACTION.match(p)
                    or SKLAD_ZNALOSTI_PAGE.match(p) or SKLAD_ZNALOSTI_API.match(p)):
                return None
            # Not an error: send the warehouse back to the one page it owns.
            if not p.startswith("/api/"):
                return redirect("/otazky")
        if session.get("role") == SKLAD_DL_ROLE:
            if (p in SKLAD_DL_PATHS or SKLAD_ACTION.match(p)
                    or SKLAD_DL_ZNALOSTI_API.match(p)):
                return None
            # Not an error: send the warehouse back to the ONE page IT owns — never
            # /otazky, which is the orders-only board (#231's whole point).
            if not p.startswith("/api/"):
                return redirect("/otazky-dl")
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
            session.permanent = True      # one login per device, not per browser session
            log.info("dashboard login OK from %s", request.remote_addr)
            return redirect("/")
        log.warning("dashboard login FAILED from %s", request.remote_addr)
        return LOGIN_HTML.replace("<!--ERR-->",
                                  '<div class="err">Nesprávne heslo</div>'), 401

    @app.get("/sklad/<k>")
    def sklad_link(k: str):
        """The warehouse's own way in: no password, one bookmarkable link (user's ask).

        The dashboard itself stays behind the password — this port is reachable from the
        open internet, so an open dashboard would publish every customer's orders and
        every original mail. The link grants ONLY the questions surface (SKLAD_PATHS).
        """
        if not hmac.compare_digest(str(k), key):
            log.warning("bad warehouse link from %s", request.remote_addr)
            abort(403)
        session["role"] = SKLAD_ROLE
        session.permanent = True
        return redirect("/otazky")

    @app.get("/otazky")
    def questions_page():
        return ASK_HTML.replace("__VERSION__", __version__)

    @app.get("/sklad-dl/<k>")
    def dl_sklad_link_route(k: str):
        """The DELIVERY-NOTES warehouse's own way in (#231) — a SEPARATE signed link
        from `/sklad/<k>` above, on its own key, so it cannot be reached by guessing or
        reusing the orders link. Grants ONLY the DL questions surface (SKLAD_DL_PATHS +
        `_role_kinds` on the shared endpoints) — never the AI-orders board.
        """
        if not hmac.compare_digest(str(k), dl_link_key):
            log.warning("bad DL warehouse link from %s", request.remote_addr)
            abort(403)
        session["role"] = SKLAD_DL_ROLE
        session.permanent = True
        return redirect("/otazky-dl")

    @app.get("/otazky-dl")
    def dl_questions_page():
        return ASK_DL_HTML.replace("__VERSION__", __version__)

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect("/login")

    @app.get("/favicon.ico")
    def favicon():
        return ("", 204)   # no icon — avoids a 404 console error on the dashboard

    @app.get("/health")
    def health():
        return jsonify(ok=True, version=__version__)

    @app.get("/version")
    def version():
        return __version__

    @app.get("/files/<mid>/<int:idx>")
    def get_file(mid: str, idx: int):
        _auth()
        matches = sorted(message_dir(str(data_dir), mid).glob(f"att{idx}__*"))
        if not matches:
            abort(404)
        return send_file(matches[0])

    @app.get("/eml/<mid>")
    def get_eml(mid: str):
        _auth()
        path = message_dir(str(data_dir), mid) / "raw.eml"
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
        with _db() as c:
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
        # One transaction: the fix row and its timeline event commit together, so a
        # failed second write cannot leave an orphan fix row that a client retry
        # then duplicates (#25).
        with _db_tx() as c:
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

    @app.get("/api/orders/questions")
    def api_orders_questions():
        """The wordings waiting for the warehouse (#88) — one per (customer, wording).

        #231: a SKLAD_ROLE/SKLAD_DL_ROLE session (the unauthenticated nástenka links)
        only ever sees ITS OWN kinds (`_role_kinds`) — a full dash_password login is
        unrestricted, unchanged from before this ticket.
        """
        from .orders import teach
        with _db() as c:
            return jsonify(items=teach.open_questions(c, kinds=_role_kinds(session.get("role"))))

    def _api_orders_answer_new_customer(qid: int, q: dict, nc: dict):
        """#234: the customer genuinely does not exist anywhere yet — the warehouse
        creates it in CODEX first (source of truth), then types the same few fields in
        here, prefilled from the mail. Same two-connection discipline as the branch below:
        the customer write, the `teach.add_candidate` audit trail, and `teach.
        answer_customer` commit together in ONE transaction; the release (a REAL external
        upload) runs afterward on its own autocommit connection, never inside the same
        rollback-able transaction — see `_api_orders_answer_customer`'s own docstring for
        why.
        """
        from .orders import hold, report, teach

        ean = _EAN_STRIP_RE.sub("", str(nc.get("ean_edi") or ""))
        if not ean:
            return jsonify(error="Bez EAN kódu EDI sa zákazník nedá uložiť — nájdeš ho v "
                                 "CODEXe pri odberateľovi."), 400
        if not ean.isdigit():
            return jsonify(error="EAN kód EDI musí byť len číslice."), 400
        name = str(nc.get("name") or "").strip()
        if not name:
            return jsonify(error="chýba názov"), 400

        ctx = q.get("context") or {}
        emails = _parse_emails_field(nc.get("emails"))
        ctx_email = str(ctx.get("sender_email") or "").strip().lower()
        if ctx_email and ctx_email not in [e.lower() for e in emails]:
            emails.append(ctx_email)
        city = str(nc.get("city") or "").strip()
        street = str(nc.get("street") or "").strip()
        zip_ = str(nc.get("zip") or "").strip()

        try:
            with _db_tx() as c:
                # #234 review finding: an unconditional 409 here (no "confirm and
                # proceed anyway" escape hatch) — the earlier draft had one
                # (`confirm_existing`), but it was reachable with no real caller and
                # would have bypassed the exact EAN-uniqueness guarantee this ticket
                # exists to add (`upsert_customer`'s own reclaim raises `DuplicateEan`
                # for a DIFFERENT street rather than silently inserting a second row —
                # #248 tightened this from a silent duplicate to a raised conflict; a
                # forced "confirm anyway" here would still bypass it either way). The
                # card's own reaction to a 409 is "Doplniť e-mail k <name>", which
                # re-posts through the EXISTING-customer path below — never a forced
                # re-submit of new_customer.
                existing = [r for r in snapshot.customers_for_management(c)
                           if str(r.get("ean_edi") or "") == ean]
                if existing:
                    hit = existing[0]
                    return jsonify(
                        error=f"EAN {ean} už má zákazník {hit.get('name', '')}.",
                        existing={"ean_edi": hit.get("ean_edi", ""),
                                 "name": hit.get("name", ""),
                                 "street": hit.get("street", ""),
                                 "override_id": hit.get("override_id")}), 409
                snapshot.upsert_customer(c, override_id=None, orig_ean_edi=None,
                                         orig_street=None, ean_edi=ean, name=name,
                                         emails=emails, city=city, street=street,
                                         zip_=zip_)
                snapshot.rebuild_from_overrides(c)
                teach.add_candidate(c, qid, {"ean_edi": ean, "name": name, "city": city,
                                             "street": street, "address_match": False,
                                             "source": "new"})
                answered = teach.answer_customer(c, qid, ean_edi=ean, name=name, by="sklad")
                report.log_event(
                    c, q["message_id"], stage="review", status="ok",
                    outcome=f"Sklad doplnil nového zákazníka {name} ({ean})",
                    detail={"question_id": qid, "ean_edi": ean}, rollup=False)
        except teach.AlreadyAnswered as e:
            return jsonify(error=str(e)), 409
        except teach.NotACandidate as e:
            return jsonify(error=str(e)), 400
        except snapshot.InvalidCustomer as e:
            return jsonify(error=str(e)), 400
        except snapshot.DuplicateEan as e:
            # #248: the pre-check above already returns this same shape for the
            # sequential case; this is the LOSER of a genuine race — `upsert_customer`
            # detected it INSIDE the advisory lock, after the pre-check above had
            # already passed for both requests.
            return jsonify(
                error=f"EAN {ean} už má zákazník {e.existing.get('name', '')}.",
                existing=e.existing), 409

        sender_email = ctx.get("sender_email", "")
        with _db() as c2:
            snapshot.remember_customer_email(c2, ean, sender_email)
            hold.set_customer(c2, qid, ean, name)
            released = hold.release_for_question(c2, cfg, qid)
        return jsonify(ok=True, question=answered, released=released,
                       customer={"ean_edi": ean, "name": name})

    def _api_orders_answer_customer(qid: int, q: dict, body: dict):
        """#159/#234: the customer-half of the same click — "this order belongs to THIS
        customer" (a frozen candidate button, OR a customer found via the live search box
        — #234), a brand-new customer typed in from CODEX (`new_customer`, #234), or
        "neviem, kto to je". A real pick durably remembers the sender address (#128's
        override mechanism) and releases through the SAME `_ship_one`/`edi.claim_send`
        ledger as the product half, now that the customer is known — `hold.set_customer`
        must land BEFORE `release_for_question`, which builds the `Matched` object
        straight from `held_orders.customer_ean`/`customer_name`. Both the remember-write
        and the release run on ONE autocommit connection, same reasoning as the product
        half's own docstring above (a real external upload must never share a
        rollback-able transaction with anything after it).
        """
        from .orders import hold, teach
        if isinstance(body.get("new_customer"), dict):
            return _api_orders_answer_new_customer(qid, q, body["new_customer"])
        unknown = bool(body.get("unknown"))
        ean_edi = "" if unknown else str(body.get("ean_edi") or "")
        name = "" if unknown else str(body.get("name") or "")
        if not unknown and not ean_edi:
            return jsonify(error="chýba zákazník"), 400
        try:
            with _db_tx() as c:
                # #234: a pick may come from the live search box over ALL current
                # customers, never just the frozen candidate set the question was asked
                # with — legitimise it server-side (never trust the client) before
                # answer_customer's own "must have been offered" check would refuse it.
                offered = {str(cd.get("ean_edi")) for cd in q["candidates"]}
                if not unknown and ean_edi not in offered:
                    hit = next((r for r in snapshot.customers_for_management(c)
                               if str(r.get("ean_edi") or "") == ean_edi
                               and r.get("ean_edi")), None)
                    if not hit:
                        return jsonify(error="Tento zákazník nie je v databáze."), 400
                    teach.add_candidate(c, qid, {
                        "ean_edi": hit["ean_edi"], "name": hit.get("name", ""),
                        "city": hit.get("city", ""), "street": hit.get("street", ""),
                        "address_match": False, "source": "search"})
                    name = name or hit.get("name", "")
                answered = teach.answer_customer(c, qid, ean_edi=ean_edi, name=name,
                                                 by="sklad")
        except teach.AlreadyAnswered as e:
            return jsonify(error=str(e)), 409
        except teach.NotACandidate as e:
            return jsonify(error=str(e)), 400
        if not ean_edi:
            with _db() as c2:
                released = hold.release_unknown_customer(c2, cfg, qid)
            return jsonify(ok=True, question=answered, released=released)
        sender_email = (q.get("context") or {}).get("sender_email", "")
        with _db() as c2:
            snapshot.remember_customer_email(c2, ean_edi, sender_email)
            snapshot.rebuild_from_overrides(c2)
            hold.set_customer(c2, qid, ean_edi, name)
            released = hold.release_for_question(c2, cfg, qid)
        return jsonify(ok=True, question=answered, released=released)

    def _api_orders_answer_new_dl_supplier(qid: int, q: dict, ns: dict):
        """#235: the DL-supplier half of the same "genuinely new, not just unoffered"
        card action #234 gave customers — HK LOAN (#236) is the concrete case. Validate
        the EAN-EDI (never forgettable — same helper #234 established, reused with
        `entity="dodávateľ"`), write the supplier, extend the question's OWN offered
        candidate set (`teach.add_candidate` — never bypass `_validate_dl_supplier`'s
        own check), then fall through to the SAME generic answer path every other
        dl_supplier pick already uses."""
        from .orders import teach
        ean = _EAN_STRIP_RE.sub("", str(ns.get("ean_edi") or ""))
        if not ean:
            return jsonify(error="Bez EAN kódu EDI sa dodávateľ nedá uložiť — nájdeš ho "
                                 "v CODEXe pri dodávateľovi."), 400
        if not ean.isdigit():
            return jsonify(error="EAN kód EDI musí byť len číslice."), 400
        name = str(ns.get("name") or "").strip()
        if not name:
            return jsonify(error="chýba názov"), 400
        emails = _parse_emails_field(ns.get("emails"))
        # `ask_dl_supplier`/`ask_generic` store the sender address in `payload`, not
        # `context` (that column is `customer`-kind-only, see `ask_customer`) — the
        # bug this fixes: reading `context` here always returns {} for a dl_supplier
        # question, so the sender's own address was silently never appended.
        ctx = q.get("payload") or {}
        ctx_email = str(ctx.get("sender_email") or "").strip().lower()
        if ctx_email and ctx_email not in [e.lower() for e in emails]:
            emails.append(ctx_email)
        city = str(ns.get("city") or "").strip()
        try:
            with _db_tx() as c:
                # Deep-review finding on #235 (mirrors #234's own new_customer collision
                # check above): `upsert_dl_supplier`'s advisory-lock reclaim only fires on
                # an EXACT (ean_edi, city) match against an un-overridden row — it checks
                # neither the frozen base snapshot nor an already-overridden supplier under
                # a DIFFERENT (or blank — city is optional in this quick form) city. Without
                # this check, entering an EAN that already belongs to a real supplier under
                # another city silently inserts a SECOND row sharing that ean_edi; both then
                # land in dl_suppliers_for_management and dl_match.py picks whichever comes
                # first, possibly a stale name. So: refuse up front, same shape as the
                # customer path, ignoring city on purpose (the collision is on the EAN).
                #
                # #248 (was "Residual, independent review, same PR" — CLOSED): this read
                # happens BEFORE `upsert_dl_supplier`'s own advisory lock is taken, so two
                # genuinely SIMULTANEOUS "new supplier" submissions for the SAME
                # never-before-seen EAN under DIFFERENT city values could both pass this
                # fast-path check before either commits. The race is no longer open,
                # though: `upsert_dl_supplier`'s own reclaim SELECT (inside the lock) is
                # now scoped by ean_edi alone, so it tells a genuine retry (same city)
                # apart from a real second submission (different city) and raises
                # `DuplicateEan` for the latter — caught below, same 409 shape this
                # fast-path check already returns. A DB-level partial unique index
                # (db.py's #248 migration) backstops both paths. See `upsert_dl_supplier`'s
                # own docstring for the full trace; `upsert_customer`'s #248 fix is the
                # identical shape, mirrored for `street` instead of `city`.
                existing = [r for r in dl_snapshot.dl_suppliers_for_management(c)
                           if str(r.get("ean_edi") or "") == ean]
                if existing:
                    hit = existing[0]
                    return jsonify(
                        error=f"EAN {ean} už má dodávateľ {hit.get('name', '')}.",
                        existing={"ean_edi": hit.get("ean_edi", ""),
                                 "name": hit.get("name", ""),
                                 "city": hit.get("city", ""),
                                 "override_id": hit.get("override_id")}), 409
                dl_snapshot.upsert_dl_supplier(
                    c, override_id=None, orig_ean_edi=None, orig_city=None,
                    ean_edi=ean, name=name, emails=emails, city=city)
                dl_snapshot.dl_rebuild_from_overrides(c)
                teach.add_candidate(c, qid, {"value": ean, "label": name})
        except snapshot.InvalidCustomer as e:
            return jsonify(error=str(e)), 400
        except snapshot.DuplicateEan as e:
            return jsonify(
                error=f"EAN {ean} už má dodávateľ {e.existing.get('name', '')}.",
                existing=e.existing), 409
        with _db() as c2:
            q2 = teach.get(c2, qid)
        return _api_orders_answer_generic(qid, q2, {"choice": ean, "by": "sklad"})

    def _api_orders_answer_new_dl_item(qid: int, q: dict, ni: dict):
        """#235: the DL-item half — a genuinely new catalog card with no GTIN in Codex
        yet (the #236 "Soľ jedlá..." case). Same shape as the supplier branch above."""
        from .orders import teach
        gtin = _EAN_STRIP_RE.sub("", str(ni.get("gtin") or ""))
        if not gtin:
            return jsonify(error="Bez GTIN sa karta nedá uložiť — nájdeš ho v CODEXe "
                                 "pri produkte."), 400
        if not gtin.isdigit():
            return jsonify(error="GTIN musí byť len číslice."), 400
        name = str(ni.get("name") or "").strip()
        if not name:
            return jsonify(error="chýba názov"), 400
        with _db_tx() as c:
            dl_snapshot.upsert_dl_catalog_card(c, gtin, name)
            dl_snapshot.dl_rebuild_from_overrides(c)
            teach.add_candidate(c, qid, {"value": gtin, "label": name})
        with _db() as c2:
            q2 = teach.get(c2, qid)
        return _api_orders_answer_generic(qid, q2, {"choice": gtin, "by": "sklad"})

    def _api_orders_answer_generic(qid: int, q: dict, body: dict):
        """#164: the SAME dispatch endpoint, generalized for kinds beyond item/customer
        (mail/date/line, and #235's dl_item/dl_supplier) — a UNIFIED `{"choice": ...,
        "by": ...}` body, routed through `teach.KINDS[q['kind']]`. `choice` blank/
        `"unknown"` is the universal escape hatch (constraint 5 of #164): the question
        stays OPEN and visible instead of being silently marked answered with nothing.

        #235: a `new_supplier`/`new_item` body (mirrors `customer`'s own `new_customer`
        branch) means the pick genuinely does not exist yet — dispatched BEFORE the
        open/kind checks below, same as `_api_orders_answer_customer` does for
        `new_customer`."""
        if q.get("kind") == "dl_supplier" and isinstance(body.get("new_supplier"), dict):
            return _api_orders_answer_new_dl_supplier(qid, q, body["new_supplier"])
        if q.get("kind") == "dl_item" and isinstance(body.get("new_item"), dict):
            return _api_orders_answer_new_dl_item(qid, q, body["new_item"])
        from .orders import teach
        kind = teach.KINDS.get(q.get("kind", ""))
        if not kind:
            return jsonify(error=f"neznámy druh otázky: {q.get('kind')!r}"), 400
        if q.get("status") != "open":
            return jsonify(error=f"otázka {qid} je už zodpovedaná"), 409
        raw = body.get("choice")
        choice = "" if raw in (None, "unknown") else str(raw)
        by = str(body.get("by") or "sklad")
        # Deep-review finding (independent review, same PR): dlSupplierSearchBox/
        # dlItemSearchBox (#235) search over the FULL current DL supplier/catalog list,
        # not just this question's frozen candidates — but `_validate_dl_supplier`/
        # `_validate_dl_item` only ever accept an OFFERED value, so a search hit (or the
        # new collision-reclaim button in newDlSupplierForm above) that was never in the
        # original candidate set was silently rejected with 400 "nebolo ponúknuté", even
        # though it IS a real, current supplier/card — the "live search over everything"
        # promise this ticket's own design comment describes was structurally unreachable.
        # Mirrors what `_api_orders_answer_customer` already does for its OWN search box
        # (legitimise server-side before validating) — scoped to the two DL kinds only;
        # mail/date/line have no search box and keep the strict offered-only check as-is.
        offered = {str(c.get("value")) for c in (q.get("candidates") or [])}
        if choice and choice not in offered and q.get("kind") in ("dl_supplier", "dl_item"):
            with _db() as clook:
                if q.get("kind") == "dl_supplier":
                    hit = next((r for r in dl_snapshot.dl_suppliers_for_management(clook)
                               if str(r.get("ean_edi") or "") == choice), None)
                else:
                    hit = next((r for r in dl_snapshot.dl_catalog_for_management(clook)
                               if str(r.get("gtin") or "") == choice), None)
                if hit:
                    cand = {"value": choice, "label": hit.get("name", "")}
                    teach.add_candidate(clook, qid, cand)
                    q = dict(q, candidates=[*(q.get("candidates") or []), cand])
        try:
            kind.validate(q, choice, by)
        except teach.NotACandidate as e:
            return jsonify(error=str(e)), 400
        if not choice:
            return jsonify(ok=True, question=q, released=[])
        # Same split as the item/customer branches above (review finding on PR #116,
        # reused here): the answer itself commits in its own transaction; `apply` (which
        # for `date` releases a held order — a REAL external upload) runs afterward on an
        # autocommit connection, so a later, unrelated failure can never roll back an
        # already-physically-uploaded document.
        #
        # Deep-review finding on #235: the `q.get("status") != "open"` check above is a
        # Python-level read from an EARLIER select (the `q` this function was called
        # with), not a WHERE-clause guard on this write — same class of race
        # `answer_customer` (teach.py) was already hardened against on #234's own review.
        # The new_supplier/new_item branches now route through here too, so two
        # concurrent answers to the same question could both pass the check above and
        # the second write would silently overwrite the first's `answered_by`/
        # `answered_at`. Guard the write itself and re-check on 0 rows affected.
        with _db_tx() as c:
            row = c.execute(
                """UPDATE order_questions
                      SET status = 'answered', answer = %s, answered_by = %s,
                          answered_at = now()
                    WHERE id = %s AND status = 'open'
                    RETURNING id""", (Json({"choice": choice}), by, qid)).fetchone()
        if not row:
            return jsonify(error=f"otázka {qid} je už zodpovedaná"), 409
        with _db() as c2:
            extra = kind.apply(c2, cfg, q, choice, by) or {}
        with _db() as c3:
            answered = teach.get(c3, qid)
        return jsonify(ok=True, question=answered, released=extra.get("released", []))

    @app.post("/api/orders/question/<int:qid>/answer")
    def api_orders_answer(qid: int):
        """One click: this wording IS this card. Taught for that customer, forever. Or,
        for a `kind='customer'` question (#159), this order belongs to THIS customer.

        If this was the LAST open question an order was held for (#93), the answer also
        releases it — the document is built and uploaded right here, once.

        The release runs on its OWN autocommit connection, deliberately NOT inside the
        `teach.answer` transaction above (review finding on PR #116): `hold.release_for_
        question` claims the `edi_sent` ledger row and then calls the real, external
        `upload()` — if that claim lived inside a rollback-able transaction and something
        AFTER the upload later failed (e.g. `report.log_event`), the whole transaction,
        INCLUDING the ledger claim, would roll back even though the document had already
        been physically delivered to ORION — a retry would then see no claim and upload a
        SECOND document, exactly the #81.1 defect this feature exists to prevent. Autocommit
        makes the claim durable the instant it is written, matching the same safe pattern
        `worker.tick` / `hold.release_due` already use (proven by
        `test_the_edi_ledger_itself_refuses_a_repeated_release_not_just_the_status_flag`).
        """
        from .orders import hold, teach
        body = request.get_json(silent=True) or {}
        with _db() as c0:
            q0 = teach.get(c0, qid)
        if not q0:
            return jsonify(error="otázka neexistuje"), 404
        # #231: a SKLAD_ROLE/SKLAD_DL_ROLE session may answer only ITS OWN kinds — the
        # id-based endpoint is otherwise shared, so this is the real boundary that keeps
        # the two nástenka links from reaching each other's agenda by guessing an id.
        allowed = _role_kinds(session.get("role"))
        if allowed is not None and q0.get("kind", "item") not in allowed:
            abort(403)
        if q0.get("kind") == "customer":
            return _api_orders_answer_customer(qid, q0, body)
        if q0.get("kind") in ("mail", "date", "line", "dl_item", "dl_supplier"):
            return _api_orders_answer_generic(qid, q0, body)

        gtin, card = str(body.get("gtin") or ""), str(body.get("card") or "")
        if not gtin:
            return jsonify(error="chýba karta"), 400
        try:
            with _db_tx() as c:
                q = teach.answer(c, qid, gtin=gtin, card=card, by="sklad")
        except teach.AlreadyAnswered as e:
            return jsonify(error=str(e)), 409
        except teach.NotACandidate as e:
            return jsonify(error=str(e)), 400
        with _db() as c2:
            released = hold.release_for_question(c2, cfg, qid)
        return jsonify(ok=True, question=q, released=released)

    @app.get("/api/orders/held")
    def api_orders_held():
        """Orders waiting on an answer, with their delivery date (#93) — so nothing waits
        invisibly: every one of these also has an open question on /otazky."""
        from .orders import hold
        with _db() as c:
            items = hold.list_held(c)
        return jsonify(items=[{
            "id": i["id"], "customer_name": i["customer_name"], "customer_ean": i["customer_ean"],
            "delivery_date": i["delivery_date"], "order_number": i["order_number"],
            "question_ids": i["question_ids"],
            "created_at": i["created_at"].isoformat() if i["created_at"] else None,
        } for i in items])

    @app.get("/api/orders/taught")
    def api_orders_taught():
        """What the warehouse has already taught — so a mis-click can be corrected.

        #231: role-scoped exactly like `/api/orders/questions` above.
        """
        from .orders import teach
        with _db() as c:
            return jsonify(items=teach.recently_taught(c, kinds=_role_kinds(session.get("role"))))

    @app.post("/api/orders/question/<int:qid>/undo")
    def api_orders_undo(qid: int):
        """Take a mistaken teaching back — it would otherwise decide that line forever.

        Routed through the SAME `teach.KINDS[kind].undo` every OTHER dispatch in this file
        already uses (#202 review pass — the previous `mail`-only special case silently left
        `dl_item`/`dl_supplier` on the bare `teach.undo` fallback, which never touches
        `dl_item_memory`/`dl_supplier_memory` at all: an undone DL teaching would reopen the
        question but keep the wrong mapping live). Behavior-preserving for every existing kind
        — `item`/`customer`/`date`/`line`'s own registered `undo` already delegates to the
        exact same `teach.undo(conn, qid)` call this replaces; `mail` is the one kind whose
        registered `undo` does more (retracts its own `mail_rules` row), and it already went
        through the registry before this change too.
        """
        from .orders import teach
        try:
            with _db_tx() as c:
                q0 = teach.get(c, qid)
                if not q0:
                    return jsonify(error="otázka neexistuje"), 404
                # #231: same role/kind boundary as the answer endpoint above.
                allowed = _role_kinds(session.get("role"))
                if allowed is not None and q0.get("kind", "item") not in allowed:
                    abort(403)
                kind = teach.KINDS.get(q0.get("kind", "item"))
                q = kind.undo(c, q0) if kind else teach.undo(c, qid)
        except teach.NotACandidate as e:
            return jsonify(error=str(e)), 404
        return jsonify(ok=True, question=q)

    # ---- /znalosti (#104): direct curation of wording->card knowledge, without waiting
    # for the pipeline to raise an order_questions row first (the ask/answer/undo flow
    # above only ever reacts to what the pipeline already asked about). ----

    @app.get("/znalosti")
    @app.get("/znalosti/<ean>")
    def znalosti_page(ean: str = ""):
        # #235: the DL product/supplier boxes call `/api/znalosti/dl-products`/
        # `dl-suppliers` — SKLAD_ROLE (the orders-only warehouse link) no longer has API
        # access to those (SKLAD_ZNALOSTI_API narrowed, see this ticket's own boundary
        # requirement). Rendering the boxes anyway would fire two 401s the instant the
        # page loads for that role (a real, dirty browser-console failure, caught by the
        # existing Playwright coverage) — so a non-admin session gets the page WITHOUT
        # them; a real dash_password login (`session["auth"]`) is unaffected.
        dl_boxes = ("    W.appendChild(dlProductsBox());\n"
                   "    W.appendChild(dlSuppliersBox());\n") if session.get("auth") else ""
        return (ZNALOSTI_HTML.replace("__VERSION__", __version__)
               .replace("__DL_BOXES__", dl_boxes))

    def _current_catalog(c):
        sid = snapshot.latest_snapshot_id(c)
        return snapshot.load_catalog(c, sid) if sid else []

    def _current_customers(c):
        sid = snapshot.latest_snapshot_id(c)
        return snapshot.load_customers(c, sid) if sid else []

    def _customer_name(c, ean: str) -> str:
        for row in _current_customers(c):
            if row["ean_edi"] == ean:
                return row["name"]
        return ""

    @app.get("/api/znalosti/catalog")
    def api_znalosti_catalog():
        q = _fold((request.args.get("q") or "").strip())
        with _db() as c:
            rows = _current_catalog(c)
        if q:
            rows = [r for r in rows if q in _fold(r["name"]) or q in _fold(r["gtin"])]
        return jsonify(items=[{"gtin": r["gtin"], "name": r["name"]} for r in rows[:30]])

    @app.get("/api/znalosti/customers")
    def api_znalosti_customers():
        q = _fold((request.args.get("q") or "").strip())
        with _db() as c:
            rows = _current_customers(c)
        if q:
            rows = [r for r in rows if q in _fold(r["name"]) or q in _fold(r["ean_edi"])]
        return jsonify(items=[{"ean_edi": r["ean_edi"], "name": r["name"]} for r in rows[:30]])

    @app.get("/api/znalosti/global")
    def api_znalosti_global():
        with _db() as c:
            return jsonify(items=memory.list_global_aliases(c))

    @app.post("/api/znalosti/global")
    def api_znalosti_global_add():
        body = request.get_json(silent=True) or {}
        wording, gtin = str(body.get("wording") or "").strip(), str(body.get("gtin") or "")
        if not (wording and gtin):
            return jsonify(error="chýba znenie alebo karta"), 400
        with _db() as c:
            rid = memory.add_global_alias(c, wording, gtin, str(body.get("card") or ""),
                                          by="sklad")
        if rid is None:
            return jsonify(error="toto znenie je už globálne priradené"), 409
        return jsonify(ok=True, id=rid)

    @app.delete("/api/znalosti/global/<int:rid>")
    def api_znalosti_global_delete(rid: int):
        with _db() as c:
            ok = memory.delete_global_row(c, rid)
        return jsonify(ok=True) if ok else (jsonify(error="nenájdené"), 404)

    @app.get("/api/znalosti/customer/<ean>")
    def api_znalosti_customer(ean: str):
        with _db() as c:
            # #128: the FIRST matching row when several share this EAN — same fallback
            # `_customer_name` above already accepts, and the /znalosti/<ean> edit form
            # only ever addresses one at a time from this page.
            record = next((r for r in snapshot.customers_for_management(c)
                          if r["ean_edi"] == ean), None)
            return jsonify(customer_name=_customer_name(c, ean), record=record,
                           items=memory.list_customer_aliases(c, ean))

    @app.post("/api/znalosti/customer/<ean>")
    def api_znalosti_customer_add(ean: str):
        body = request.get_json(silent=True) or {}
        wording, gtin = str(body.get("wording") or "").strip(), str(body.get("gtin") or "")
        if not (wording and gtin):
            return jsonify(error="chýba znenie alebo karta"), 400
        with _db() as c:
            rid = memory.add_customer_alias(c, ean, wording, gtin, str(body.get("card") or ""))
        if rid is None:
            return jsonify(error="toto znenie je už tomuto zákazníkovi priradené"), 409
        return jsonify(ok=True, id=rid)

    @app.delete("/api/znalosti/customer/<ean>/<int:rid>")
    def api_znalosti_customer_delete(ean: str, rid: int):
        with _db() as c:
            ok = memory.delete_item_memory_row(c, rid, ean)
        return jsonify(ok=True) if ok else (jsonify(error="nenájdené"), 404)

    # ---- /znalosti (#127/#128): direct add/edit/retire of the product cards and
    # customers themselves, layered as overrides ON TOP of the frozen base snapshot —
    # an override always wins, and is versioned the same way
    # (snapshot.rebuild_from_overrides freezes a new snapshot immediately, so the
    # change is visible on this same page right away — no network call, no periodic
    # refresh to wait for; the sheet itself is never read at all since #129). ----

    @app.get("/api/znalosti/products")
    def api_znalosti_products():
        q = _fold((request.args.get("q") or "").strip())
        with _db() as c:
            rows = snapshot.catalog_for_management(c)
        if q:
            rows = [r for r in rows if q in _fold(r["name"]) or q in _fold(r["gtin"])]
        rows.sort(key=lambda r: _fold(r["name"]))
        return jsonify(items=rows[:50])

    @app.post("/api/znalosti/products")
    def api_znalosti_products_upsert():
        body = request.get_json(silent=True) or {}
        gtin = str(body.get("gtin") or "").strip()
        name = str(body.get("name") or "").strip()
        if not (gtin and name):
            return jsonify(error="chýba GTIN alebo názov"), 400
        with _db() as c:
            snapshot.upsert_catalog_card(c, gtin, name)
            snapshot.rebuild_from_overrides(c)
        return jsonify(ok=True)

    @app.delete("/api/znalosti/products/<gtin>")
    def api_znalosti_products_retire(gtin: str):
        with _db() as c:
            ok = snapshot.retire_catalog_card(c, gtin)
            if ok:
                snapshot.rebuild_from_overrides(c)
        return jsonify(ok=True) if ok else (jsonify(error="nenájdené"), 404)

    @app.get("/api/znalosti/clients")
    def api_znalosti_clients():
        q = _fold((request.args.get("q") or "").strip())
        with _db() as c:
            rows = snapshot.customers_for_management(c)
        if q:
            rows = [r for r in rows if q in _fold(r["name"]) or q in _fold(r["ean_edi"])]
        rows.sort(key=lambda r: _fold(r["name"]))
        return jsonify(items=rows[:50])

    def _parse_emails_field(v) -> list[str]:
        if isinstance(v, list):
            return [str(e).strip() for e in v if str(e).strip()]
        return [e.strip() for e in str(v or "").split(",") if e.strip()]

    @app.post("/api/znalosti/clients")
    def api_znalosti_clients_upsert():
        body = request.get_json(silent=True) or {}
        name = str(body.get("name") or "").strip()
        if not name:
            return jsonify(error="chýba názov"), 400
        # #234: the identical EAN validation the question-card "new customer" flow uses —
        # the EAN must never be forgettable, whichever screen a customer is added from.
        ean = _EAN_STRIP_RE.sub("", str(body.get("ean_edi") or ""))
        if not ean:
            return jsonify(error="Bez EAN kódu EDI sa zákazník nedá uložiť — nájdeš ho v "
                                 "CODEXe pri odberateľovi."), 400
        if not ean.isdigit():
            return jsonify(error="EAN kód EDI musí byť len číslice."), 400
        from .orders import hold
        try:
            with _db() as c:
                rid = snapshot.upsert_customer(
                    c, override_id=body.get("override_id"),
                    orig_ean_edi=body.get("orig_ean_edi"),
                    orig_street=body.get("orig_street"),
                    ean_edi=ean, name=name,
                    emails=_parse_emails_field(body.get("emails")),
                    city=str(body.get("city") or "").strip(),
                    street=str(body.get("street") or "").strip(),
                    zip_=str(body.get("zip") or "").strip())
                snapshot.rebuild_from_overrides(c)
                # #234: this save may be exactly what an ALREADY-OPEN customer question
                # was waiting for (the customer was added on /znalosti instead of on the
                # card) — never leave that order stuck until the periodic worker sweep
                # catches up.
                hold.retry_unknown_customer_questions(c, cfg)
        except snapshot.DuplicateEan as e:
            # #248 review finding: this admin dashboard save funnels through the SAME
            # `upsert_customer` as the warehouse question-card flow, so it can raise the
            # same conflict — same 409 shape either way.
            return jsonify(
                error=f"EAN {ean} už má zákazník {e.existing.get('name', '')}.",
                existing=e.existing), 409
        return jsonify(ok=True, id=rid)

    @app.delete("/api/znalosti/clients")
    def api_znalosti_clients_retire():
        body = request.get_json(silent=True) or {}
        with _db() as c:
            ok = snapshot.retire_customer(
                c, override_id=body.get("override_id"),
                orig_ean_edi=body.get("orig_ean_edi"), orig_street=body.get("orig_street"))
            if ok:
                snapshot.rebuild_from_overrides(c)
        return jsonify(ok=True) if ok else (jsonify(error="nenájdené"), 404)

    # ---- /znalosti (#221): direct add/edit/retire of the DL catalog cards + suppliers,
    # mirroring the #127/#128 products/clients routes above 1:1 but on DL's own separate
    # dl_snapshots versioning line (see dl_snapshot.py's module docstring for why). ----

    @app.get("/api/znalosti/dl-products")
    def api_znalosti_dl_products():
        q = _fold((request.args.get("q") or "").strip())
        with _db() as c:
            rows = dl_snapshot.dl_catalog_for_management(c)
        if q:
            rows = [r for r in rows if q in _fold(r["name"]) or q in _fold(r["gtin"])]
        rows.sort(key=lambda r: _fold(r["name"]))
        return jsonify(items=rows[:50])

    @app.post("/api/znalosti/dl-products")
    def api_znalosti_dl_products_upsert():
        body = request.get_json(silent=True) or {}
        gtin = str(body.get("gtin") or "").strip()
        name = str(body.get("name") or "").strip()
        if not (gtin and name):
            return jsonify(error="chýba GTIN alebo názov"), 400
        with _db() as c:
            dl_snapshot.upsert_dl_catalog_card(
                c, gtin, name, doplnok=str(body.get("doplnok") or "").strip(),
                mass=dl_snapshot.parse_number(body.get("mass")),
                sklad=str(body.get("sklad") or "").strip(),
                cena=dl_snapshot.parse_number(body.get("cena")))
            dl_snapshot.dl_rebuild_from_overrides(c)
        return jsonify(ok=True)

    @app.delete("/api/znalosti/dl-products/<gtin>")
    def api_znalosti_dl_products_retire(gtin: str):
        with _db() as c:
            ok = dl_snapshot.retire_dl_catalog_card(c, gtin)
            if ok:
                dl_snapshot.dl_rebuild_from_overrides(c)
        return jsonify(ok=True) if ok else (jsonify(error="nenájdené"), 404)

    @app.get("/api/znalosti/dl-suppliers")
    def api_znalosti_dl_suppliers():
        q = _fold((request.args.get("q") or "").strip())
        with _db() as c:
            rows = dl_snapshot.dl_suppliers_for_management(c)
        if q:
            rows = [r for r in rows if q in _fold(r["name"]) or q in _fold(r["ean_edi"])]
        rows.sort(key=lambda r: _fold(r["name"]))
        return jsonify(items=rows[:50])

    @app.post("/api/znalosti/dl-suppliers")
    def api_znalosti_dl_suppliers_upsert():
        body = request.get_json(silent=True) or {}
        name = str(body.get("name") or "").strip()
        if not name:
            return jsonify(error="chýba názov"), 400
        # #235: the same EAN-cannot-be-forgotten guarantee #234 gave customers, reusing
        # the SAME `_EAN_STRIP_RE` constant (not a second copy) — an early, precise 400
        # before ever reaching the DB layer. `dl_snapshot.upsert_dl_supplier` ALSO
        # enforces this unconditionally (defense in depth, any future caller).
        ean = _EAN_STRIP_RE.sub("", str(body.get("ean_edi") or ""))
        if not ean:
            return jsonify(error="Bez EAN kódu EDI sa dodávateľ nedá uložiť — nájdeš ho "
                                 "v CODEXe pri dodávateľovi."), 400
        if not ean.isdigit():
            return jsonify(error="EAN kód EDI musí byť len číslice."), 400
        try:
            with _db() as c:
                rid = dl_snapshot.upsert_dl_supplier(
                    c, override_id=body.get("override_id"),
                    orig_ean_edi=body.get("orig_ean_edi"), orig_city=body.get("orig_city"),
                    ean_edi=ean, name=name,
                    emails=_parse_emails_field(body.get("emails")),
                    city=str(body.get("city") or "").strip())
                dl_snapshot.dl_rebuild_from_overrides(c)
        except snapshot.DuplicateEan as e:
            # #248 review finding: mirrors the customer endpoint's own fix above — this
            # admin dashboard save funnels through the SAME `upsert_dl_supplier` as the
            # warehouse question-card flow.
            return jsonify(
                error=f"EAN {ean} už má dodávateľ {e.existing.get('name', '')}.",
                existing=e.existing), 409
        return jsonify(ok=True, id=rid)

    @app.delete("/api/znalosti/dl-suppliers")
    def api_znalosti_dl_suppliers_retire():
        body = request.get_json(silent=True) or {}
        with _db() as c:
            ok = dl_snapshot.retire_dl_supplier(
                c, override_id=body.get("override_id"),
                orig_ean_edi=body.get("orig_ean_edi"), orig_city=body.get("orig_city"))
            if ok:
                dl_snapshot.dl_rebuild_from_overrides(c)
        return jsonify(ok=True) if ok else (jsonify(error="nenájdené"), 404)

    @app.get("/api/orders/spend")
    def api_orders_spend():
        """What the order engine costs this month, and how much of it needed no model (#89).

        The two numbers belong together: the deterministic share is supposed to RISE as the
        delivery history fills, so a falling share explains a rising bill.
        """
        from .orders import spend as spend_mod
        with _db() as c:
            mtd = spend_mod.month_to_date(c)
            share = spend_mod.deterministic_share(c)
            top = spend_mod.top_runs(c)
        return jsonify(month=mtd["month"], runs=mtd["runs"],
                       cost_eur=round(mtd["cost_eur"], 2),
                       cost_usd=round(mtd["cost_usd"], 2),
                       per_email_eur=round(mtd["cost_eur"] / mtd["runs"], 3)
                       if mtd["runs"] else 0.0,
                       calls=mtd["calls"], cached_calls=mtd["cached_calls"],
                       cap_eur=float(getattr(cfg, "orders_spend_cap_eur", 30) or 0),
                       free_pct=round(share["pct"], 1), free=share["free"],
                       decisions=share["total"], top_runs=top)

    @app.get("/api/orders/digest")
    def api_orders_digest():
        """#196: the same match-provenance stats + 'days since incident' the daily Odoo
        digest carries — the warehouse's measurable, live basis for trust, on the
        dashboard too, not only in the Odoo channel."""
        from .orders import reliability
        with _db() as c:
            today = reliability.provenance_stats_for_day(c)
            yesterday = reliability.provenance_stats_for_day(
                c, c.execute(
                    "SELECT to_char(now() - interval '1 day', 'YYYY-MM-DD')").fetchone()[0])
            since = reliability.days_since_incident(c)
        return jsonify(today=today, yesterday=yesterday, days_since_incident=since)

    @app.get("/api/orders/dl/stats")
    def api_orders_dl_stats():
        """#231: the "stavy" the DL nástenka asks for — today/yesterday's DL run counts
        (`reliability.dl_provenance_stats_for_day`, built for #204's daily digest — same
        aggregate-only shape: run/document counts, no mail body, no attachment). Reachable
        by BOTH the full admin login and the DL-only `sklad_dl` role (it is in
        `SKLAD_DL_PATHS`); the orders-only `sklad` role has no matching path and gets a
        plain 401, same as any other endpoint outside its own board."""
        from .orders import reliability
        with _db() as c:
            today = reliability.dl_provenance_stats_for_day(c)
            # #239 deep-review finding: the three current-health gauges are NOT
            # day-scoped — the JS badge only ever reads them off `today` (see
            # ASK_DL_HTML's loadStats()), so recomputing the identical three queries
            # for "yesterday" would be pure waste.
            yesterday = reliability.dl_provenance_stats_for_day(
                c, c.execute(
                    "SELECT to_char(now() - interval '1 day', 'YYYY-MM-DD')").fetchone()[0],
                include_current_health=False)
        return jsonify(today=today, yesterday=yesterday)

    @app.get("/api/imap-failures")
    def api_imap_failures():
        """Emails that could not be ingested at all (#20) — they have no messages row,
        so this is the ONLY place they are visible. Never let them be silent."""
        with _db() as c:
            items = db.list_uid_failures(c)
            pending, skipped = db.count_uid_failures(c)
        return jsonify(total=pending + skipped, items=items, shown=len(items),
                       max_attempts=MAX_UID_ATTEMPTS, pending=pending, skipped=skipped)

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
        log.info("fix #%s resolved -> %s", fid, status)
        return jsonify(ok=True, id=fid, status=status)

    @app.get("/")
    def dashboard():
        # The address the operator is ON, never cfg.public_base_url — that one is the MACHINE
        # base (n8n fetches /files over the docker network) and is unopenable in a browser.
        base = request.host_url.rstrip("/")
        return (DASH_HTML.replace("__VERSION__", __version__)
                .replace("__SKLADLINK__", f"{base}/sklad/{key}")
                .replace("__DLSKLADLINK__", f"{base}/sklad-dl/{dl_link_key}"))

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
  <span class="ver" id="spendBadge" data-testid="spend" title="náklady objednávkového automatu za tento mesiac"></span>
  <span class="ver" id="reliabilityBadge" data-testid="reliability" title="spoľahlivosť AI objednávok — dní od posledného potvrdeného incidentu, včerajší prehľad rozhodnutí"></span>
  <a class="ver" href="/logout">odhlásiť</a>
</header>
<div class="chips" id="chips"></div>
<div class="tabs">
  <button class="tab active" id="tabMails" onclick="setView('mails')">Maily</button>
  <button class="tab" id="tabFix" onclick="setView('fix')">Fix fronta</button>
  <button class="tab" id="tabImap" onclick="setView('imap')">Neprijaté <span id="imapBadge"></span></button>
  <button class="tab" id="tabAsk" onclick="setView('ask')">Otázky skladu <span id="askBadge"></span></button>
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
  if(!r.ok){let m='';try{m=(await r.json()).error||''}catch(e){}
    throw new Error(m||('chyba '+r.status))}
  return r.json()}
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
    const isRev=it.proc_status==='review'||it.proc_status==='partial';
    const st=it.processed?'done':(isRev?'review':it.proc_status==='error'?'error':it.processing?'processing':'');
    const out=it.on_fix?'<span class="out" style="color:#bf3989">🔧 na oprave</span>':
      (it.proc_outcome?'<span class="out '+(it.proc_status==='error'?'err':isRev?'rev':'ok')+'">'+E(it.proc_outcome)+'</span>':'');
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
  const badge=m.proc_status?('<span class="badge b-'+(m.proc_status==='ok'?'ok':(m.proc_status==='review'||m.proc_status==='partial')?'review':m.proc_status==='error'?'error':'none')+'">'+E(m.proc_status)+'</span>'):
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
async function doReclassify(id,cat){try{await api('/api/message/'+id+'/reclassify',{method:'POST',body:JSON.stringify({category:cat})});await loadList();await openDetail(id)}catch(e){alert(e.message||'chyba')}}
async function doReprocess(id){try{await api('/api/message/'+id+'/reprocess',{method:'POST'});await loadList();await openDetail(id)}catch(e){alert(e.message||'chyba')}}
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
async function loadImap(){const D=document.getElementById('detail'),L=document.getElementById('list');
  L.innerHTML='';let d;try{d=await api('/api/imap-failures')}catch(e){return}
  const b=document.getElementById('imapBadge');
  b.textContent=d.total?String(d.total):'';b.style.color='#f85149';
  if(!d.items.length){D.innerHTML='<div class="empty">Všetky maily sa podarilo prijať 🎉</div>';return}
  D.innerHTML='<div class="lbl">Maily, ktoré sa nepodarilo prijať ('+d.pending+' sa ešte skúša, '+d.skipped+' vzdané)</div>'+
    d.items.map(f=>'<div class="fixrow'+(f.skipped?'':' resolved')+'">'+
      '<div class="t" style="display:flex;justify-content:space-between"><b>'+(f.skipped?'⛔ vzdané':'🔄 skúša sa')+
      ' — '+E(f.folder)+' UID '+f.uid+'</b><span class="muted">'+f.attempts+'/'+d.max_attempts+' pokusov</span></div>'+
      '<div class="muted">prvýkrát '+tsShort(f.first_seen)+' · naposledy '+tsShort(f.last_seen)+'</div>'+
      '<div class="err">'+E(f.last_error||'')+'</div>'+
      (f.skipped?'<div class="muted">Tento mail v systéme NIE JE. Treba ho vytiahnuť ručne z mailu (schránka, UID '+f.uid+') alebo opraviť príčinu a znížiť watermark.</div>':'')+
      '</div>').join('')}
async function resolveFix(fid,status){const res=status==='fixed'?(prompt('Poznámka k oprave (voliteľné):')||''):'';
  try{await api('/api/fix/'+fid+'/resolve',{method:'POST',body:JSON.stringify({status,resolution:res})});await loadFix()}catch(e){alert('chyba')}}
function setView(v){view=v;document.getElementById('tabMails').classList.toggle('active',v==='mails');
  document.getElementById('tabFix').classList.toggle('active',v==='fix');
  document.getElementById('tabImap').classList.toggle('active',v==='imap');
  document.getElementById('tabAsk').classList.toggle('active',v==='ask');
  if(v==='fix'){loadFix()}else if(v==='imap'){loadImap()}
  else if(v==='ask'){showSkladLink();loadAsk()}
  else{document.getElementById('detail').innerHTML='<div class="empty">Vyber mail vľavo.</div>';loadList()}}
function tick(){if(live&&document.getElementById('ov').style.display!=='flex'){
  if(view==='mails')loadList();else if(view==='imap')loadImap();
  else if(view==='ask')loadAsk();else loadFix()}}
const SKLAD_LINK="__SKLADLINK__";
const DL_SKLAD_LINK="__DLSKLADLINK__";
function skladLinkRow(label,url){const w=document.createElement('div');w.className='row';
  const h=document.createElement('div');h.className='sub';h.textContent=label;
  const a=document.createElement('a');a.href=url;a.textContent=url;
  a.target='_blank';a.rel='noopener';a.style.wordBreak='break-all';
  w.appendChild(h);w.appendChild(a);return w}
function showSkladLink(){const D=document.getElementById('detail');D.textContent='';
  D.appendChild(skladLinkRow(
    'Odkaz pre predaj (objednávky) — otvorí sa bez hesla, dá sa dať do Odoo aj do záložiek:',
    SKLAD_LINK));
  D.appendChild(skladLinkRow(
    'Odkaz pre sklad (dodacie listy) — samostatná nástenka, len dodacie listy:',
    DL_SKLAD_LINK))}
let askRender=0;
async function loadAsk(){const L=document.getElementById('list');
  // Every render gets a number. A fetch that comes back after a newer render started must not
  // append to it, or the list doubles (seen live on 0.9.7).
  const mine=++askRender;
  L.innerHTML='';let d;try{d=await api('/api/orders/questions')}catch(e){return}
  if(mine!==askRender)return;
  if(!d.items.length){const e0=document.createElement('div');e0.className='empty';
    e0.textContent='Nič nečaká \u2014 automat si vie poradiť sám.';L.appendChild(e0);
    await loadHeld(mine);return loadTaught(mine)}   // nothing waiting is the NORMAL state: the undo must still be here
  for(const q of d.items){const el=document.createElement('div');el.className='row';
    const head=document.createElement('div');const b=document.createElement('b');
    // #159: a 'customer' question asks WHO placed the order, not WHICH card a wording is
    if(q.kind==='customer'){const ctx=q.context||{};
      b.textContent='Nezn\u00e1my z\u00e1kazn\u00edk: '+(ctx.sender_email||q.wording);head.appendChild(b);
      const who=document.createElement('div');who.className='sub';
      who.textContent=[ctx.sender_name,ctx.company_name,ctx.delivery_address_guess]
        .filter(Boolean).join(' \u00b7 ')+' \u00b7 dodanie '+(q.delivery_date||'?');
      const why=document.createElement('div');why.className='sub';why.textContent=q.reason||'';
      const acts=document.createElement('div');acts.className='acts';
      for(const c of (q.candidates||[])){const bt=document.createElement('button');bt.className='btn';
        const addr=[c.street,c.city].filter(Boolean).join(', ');
        bt.textContent=(c.name||c.ean_edi)+(addr?' ('+addr+')':'')+(c.address_match?' \u2713':'');
        bt.onclick=()=>answerCustomerIt(q.id,c.ean_edi,c.name||'');acts.appendChild(bt)}
      const ub=document.createElement('button');ub.className='btn';ub.textContent='Neviem, kto to je';
      ub.onclick=()=>answerCustomerIt(q.id,'','',true);acts.appendChild(ub);
      head.appendChild(who);head.appendChild(why);head.appendChild(acts);
      el.appendChild(head);L.appendChild(el);continue}
    // #164/#202: ONE generic renderer for every OTHER new kind (mail/date/line, and DL's
    // own dl_item/dl_supplier) — the candidates carry their own {value,label}; a universal
    // "Neviem" escape posts {"choice":"unknown"} through the same dispatch endpoint (stays
    // open, never silent).
    if(q.kind==='mail'||q.kind==='date'||q.kind==='line'||q.kind==='dl_item'||q.kind==='dl_supplier'){
      const titles={mail:'Je to vôbec objednávka?',date:'Ktorý deň dodávky platí?',
        line:'Platí ešte táto položka?',dl_item:'Ktorá karta je táto DL položka?',
        dl_supplier:'Ktorý dodávateľ?'};
      b.textContent=titles[q.kind]||q.kind;head.appendChild(b);
      const who=document.createElement('div');who.className='sub';who.textContent=q.wording||'';
      const why=document.createElement('div');why.className='sub';why.textContent=q.reason||'';
      const acts=document.createElement('div');acts.className='acts';
      for(const c of (q.candidates||[])){const bt=document.createElement('button');bt.className='btn';
        bt.textContent=c.label||c.value;bt.onclick=()=>answerGenericIt(q.id,c.value);acts.appendChild(bt)}
      const ub=document.createElement('button');ub.className='btn';ub.textContent='Neviem';
      ub.onclick=()=>answerGenericIt(q.id,'unknown');acts.appendChild(ub);
      head.appendChild(who);head.appendChild(why);head.appendChild(acts);
      el.appendChild(head);L.appendChild(el);continue}
    b.textContent=q.wording;head.appendChild(b);
    head.appendChild(document.createTextNode(' \u00b7 '+(q.quantity||'')+' '+(q.unit||'')));
    const who=document.createElement('div');who.className='sub';
    who.textContent=(q.customer_name||q.customer_ean)+' \u00b7 dodanie '+(q.delivery_date||'?');
    const why=document.createElement('div');why.className='sub';why.textContent=q.reason||'';
    const acts=document.createElement('div');acts.className='acts';
    for(const c of q.candidates){const bt=document.createElement('button');bt.className='btn';
      bt.textContent=c.name||c.gtin;            // textContent: a name may contain quotes
      bt.onclick=()=>teachIt(q.id,c.gtin,c.name||'');acts.appendChild(bt)}
    head.appendChild(who);head.appendChild(why);head.appendChild(acts);
    el.appendChild(head);L.appendChild(el)}
  await loadHeld(mine);loadTaught(mine)}
async function loadHeld(token){const L=document.getElementById('list');let d;
  // #93: orders waiting on an answer, so nothing waits invisibly \u2014 each one names its
  // own delivery date, the deadline this project promises it will ship by regardless.
  try{d=await api('/api/orders/held')}catch(e){return}
  if(token!==askRender||!d.items.length)return;
  const h=document.createElement('div');h.className='sub';h.style.padding='8px 10px';
  h.textContent='Objednávky čakajúce na odpoveď \u2014 odošlú sa po odpovedi, najneskôr v deň dodania:';
  L.appendChild(h);
  for(const o of d.items){const el=document.createElement('div');el.className='row';
    const head=document.createElement('div');const b=document.createElement('b');
    b.textContent=o.customer_name||o.customer_ean||'(neznámy zákazník)';head.appendChild(b);
    const who=document.createElement('div');who.className='sub';
    who.textContent='dodanie '+(o.delivery_date||'?')+(o.order_number?' \u00b7 obj. '+o.order_number:'')
      +' \u00b7 '+o.question_ids.length+' \u00d7 otázka';
    head.appendChild(who);el.appendChild(head);L.appendChild(el)}}
async function loadTaught(token){const L=document.getElementById('list');let d;
  try{d=await api('/api/orders/taught')}catch(e){return}
  if(token!==askRender)return;              // a newer render owns the list now
  if(!d.items.length)return;
  const h=document.createElement('div');h.className='sub';h.style.padding='8px 10px';
  h.textContent='Naposledy naučené \u2014 keby bol klik omylom, dá sa vrátiť:';L.appendChild(h);
  for(const t of d.items){const el=document.createElement('div');el.className='row';
    const w=document.createElement('div');const b=document.createElement('b');
    b.textContent=t.wording;w.appendChild(b);
    w.appendChild(document.createTextNode(' \u2192 '+(t.answer_card||t.answer_gtin)));
    const who=document.createElement('div');who.className='sub';
    who.textContent=(t.customer_name||t.customer_ean);
    const acts=document.createElement('div');acts.className='acts';
    const bt=document.createElement('button');bt.className='btn';bt.textContent='vrátiť';
    bt.onclick=()=>undoIt(t.id);acts.appendChild(bt);
    w.appendChild(who);w.appendChild(acts);el.appendChild(w);L.appendChild(el)}}
async function undoIt(qid){try{await api('/api/orders/question/'+qid+'/undo',{method:'POST'});
  await loadAsk();await askBadgeRefresh()}catch(e){alert(e.message||'chyba')}}
async function teachIt(qid,gtin,card){try{await api('/api/orders/question/'+qid+'/answer',
  {method:'POST',body:JSON.stringify({gtin:gtin,card:card})});await loadAsk();await askBadgeRefresh()}
  catch(e){alert(e.message||'chyba')}}
async function answerCustomerIt(qid,ean_edi,name,unknown){try{await api('/api/orders/question/'+qid+'/answer',
  {method:'POST',body:JSON.stringify(unknown?{unknown:true}:{ean_edi:ean_edi,name:name})});
  await loadAsk();await askBadgeRefresh()}catch(e){alert(e.message||'chyba')}}
async function answerGenericIt(qid,choice){try{await api('/api/orders/question/'+qid+'/answer',
  {method:'POST',body:JSON.stringify({choice:choice})});await loadAsk();await askBadgeRefresh()}
  catch(e){alert(e.message||'chyba')}}
async function askBadgeRefresh(){try{const d=await api('/api/orders/questions');
  const b=document.getElementById('askBadge');b.textContent=d.items.length?String(d.items.length):'';
  b.style.color='#d29922'}catch(e){}}
async function spendBadgeRefresh(){try{const d=await api('/api/orders/spend');
  const b=document.getElementById('spendBadge');
  b.textContent=d.cost_eur.toFixed(2)+' \u20ac / '+d.cap_eur.toFixed(0)+' \u20ac \u00b7 bez modelu '+d.free_pct+' %';
  b.style.color=(d.cap_eur&&d.cost_eur>d.cap_eur)?'#f85149':'#6e7681'}catch(e){}}
async function imapBadgeRefresh(){try{const d=await api('/api/imap-failures');
  const b=document.getElementById('imapBadge');b.textContent=d.total?String(d.total):'';b.style.color='#f85149'}catch(e){}}
async function reliabilityBadgeRefresh(){try{const d=await api('/api/orders/digest');
  const b=document.getElementById('reliabilityBadge');
  const since=d.days_since_incident;
  const y=d.yesterday||{};
  const sinceTxt=(since==null)?'bez záznamu incidentu':(since+' '+(since===1?'deň':(since>=2&&since<=4?'dni':'dní'))+' bez incidentu');
  b.textContent=sinceTxt+(y.items?(' · včera '+y.deterministic+'/'+y.llm+'/'+y.review+' (isté/AI/kontrola)'):'');
  b.style.color=(since!=null&&since<3)?'#f85149':'#6e7681'}catch(e){}}
document.getElementById('livetog').onclick=()=>{live=!live;document.getElementById('livetog').style.color=live?'#3fb950':'#6e7681';document.getElementById('livelbl').textContent=live?'LIVE':'pauza'};
let deb;q.oninput=()=>{clearTimeout(deb);deb=setTimeout(loadList,350)};
for(const el of [fcat,fstate,ffrom,fto])el.onchange=loadList;
loadList();imapBadgeRefresh();spendBadgeRefresh();askBadgeRefresh();reliabilityBadgeRefresh();setInterval(askBadgeRefresh,30000);timer=setInterval(tick,5000);setInterval(imapBadgeRefresh,30000);setInterval(spendBadgeRefresh,60000);setInterval(reliabilityBadgeRefresh,60000);
</script></body></html>"""


# The warehouse's own page: reachable from the signed /sklad/<key> link with NO password,
# so it fetches ONLY the two question endpoints — nothing here may reach a mail, a file or
# the spend. Phone-sized buttons: it is answered from the floor, not from a desk.
# #231: ONE shared template for BOTH unauthenticated question boards (the orders-only
# `/otazky` and the NEW delivery-notes-only `/otazky-dl`) — they need the same rendering
# for every generic-kind card (`genericQuestionCard`, `dl_item`/`dl_supplier` included
# since #202) and the same "naposledy naučené" history, and the server already sends
# each role only its OWN kinds (`_role_kinds` in httpapi.py) — so the two pages differ
# only in title/heading and an optional "stavy" (states) strip the DL board asks for
# (ticket #231: "review fronta, história, stavy"). Building both from ONE literal string
# via `.replace()` keeps them from silently drifting apart the way two hand-maintained
# ~150-line copies inevitably would.
_ASK_HTML_TEMPLATE = r"""<!doctype html><html lang="sk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
 *{box-sizing:border-box}
 body{font:15px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:0;
      background:#f6f8fa;color:#1f2328}
 header{background:#161b22;color:#e6edf3;padding:12px 16px;display:flex;justify-content:space-between;
        align-items:center;position:sticky;top:0}
 h1{font-size:16px;margin:0}
 .ver{font-size:12px;color:#8b949e}
 .dl-alert-banner{background:#fff3cd;border-bottom:2px solid #d4a72c;color:#5c4813;
   padding:10px 14px;font-size:14px;font-weight:600;position:sticky;top:44px}
 .dl-alert-banner div{margin:3px 0}
 main{padding:14px 12px;max-width:760px;margin:0 auto}
 .q{background:#fff;border:1px solid #d0d7de;border-radius:12px;padding:14px;margin-bottom:14px}
 .who{font-size:13px;color:#57606a}
 .w{font-size:18px;font-weight:700;margin:4px 0 2px}
 .why{font-size:13px;color:#57606a;margin-bottom:10px}
 button{display:block;width:100%;text-align:left;padding:12px 14px;margin-top:8px;font:inherit;
        border:1px solid #1f6feb;border-radius:10px;background:#ddf4ff;color:#0969da;cursor:pointer}
 .t{background:#fff;border:1px solid #d0d7de;border-radius:10px;padding:10px 12px;margin-bottom:8px;
    display:flex;justify-content:space-between;align-items:center;gap:10px;font-size:14px}
 .t button{width:auto;margin:0;border-color:#d0d7de;background:#f6f8fa;color:#57606a;padding:7px 12px}
 h2{font-size:14px;color:#57606a;margin:22px 0 8px}
 .empty{color:#57606a;padding:14px 2px}
 .kb{display:block;font-size:13px;color:#57606a;margin-top:8px;text-decoration:none}
 .kb:hover{text-decoration:underline}
 .slabel{font-size:12px;color:#57606a;margin-top:12px}
 .search{width:100%;box-sizing:border-box;padding:9px 10px;margin-top:6px;border:1px solid #d0d7de;
         border-radius:8px;font:inherit}
 .sres-wrap{margin-top:4px}
 .sres{padding:10px 12px;border:1px solid #d0d7de;border-radius:8px;margin-top:4px;cursor:pointer;
       font-size:14px;background:#fff}
 .sres:hover{background:#ddf4ff}
 .sres.none{cursor:default;color:#57606a;background:transparent;border-style:dashed}
 .sres.none:hover{background:transparent}
 input{width:100%;padding:9px 10px;margin-top:6px;border:1px solid #d0d7de;border-radius:8px;font:inherit}
</style></head><body>
<header><h1>__HEADING__</h1><span class="ver" data-testid="version">v__VERSION__</span>__STATS_HEADER__</header>
__ALERT_BANNER__
<main id="wrap"><div class="empty">Nahrávam&hellip;</div></main>
<script>
async function api(u,o){const r=await fetch(u,Object.assign({headers:{'Content-Type':'application/json'}},o||{}));
  if(!r.ok){const b=await r.json().catch(()=>({}));const e=new Error(b.error||('HTTP '+r.status));e.body=b;throw e}
  return r.json()}
let render=0;
// #149: what the warehouse has typed into each open question's catalog search, keyed by
// question id — the list auto-refreshes every 5s (see setInterval below), and without this
// the whole card gets rebuilt from scratch mid-typing and wipes what was just typed.
const searchState={};
function el(t,cls,txt){const e=document.createElement(t);if(cls)e.className=cls;
  if(txt!==undefined)e.textContent=txt;return e}
function searchBox(q){
  const wrap=el('div');
  const inp=el('input','search');inp.placeholder='hľadaj v celom katalógu (názov karty)…';
  inp.value=searchState[q.id]||'';
  const res=el('div','sres-wrap');
  wrap.appendChild(inp);wrap.appendChild(res);
  // Same stale-response guard as load()'s `mine=++render`: a slower response for an
  // earlier keystroke must not overwrite a faster response for a later one.
  let seq=0;
  async function run(v){
    const mine=++seq;
    if(v.length<2){res.textContent='';return}
    let d;try{d=await api('/api/znalosti/catalog?q='+encodeURIComponent(v))}catch(e){return}
    if(mine!==seq)return;      // a later keystroke's response already landed — drop this one
    res.textContent='';
    if(!d.items.length){res.appendChild(el('div','sres none','žiadna zhoda'));return}
    for(const it of d.items){const b=el('div','sres',it.name+'  ('+it.gtin+')');
      b.onclick=()=>teach(q.id,it.gtin,it.name);res.appendChild(b)}
  }
  let t=null;
  inp.oninput=()=>{searchState[q.id]=inp.value;clearTimeout(t);
    t=setTimeout(()=>run(inp.value.trim()),200)};
  if(inp.value.trim().length>=2)run(inp.value.trim());
  return wrap;
}
// #164/#202: ONE generic card for every kind BEYOND item/customer (mail/date/line, and
// DL's own dl_item/dl_supplier) — each candidate button posts {"choice": <value>} through
// the SAME dispatch endpoint, plus a universal "Neviem" escape that posts
// {"choice":"unknown"} (stays open, never silent).
const GENERIC_TITLE={mail:'Je to vôbec objednávka?',date:'Ktorý deň dodávky platí?',
  line:'Platí ešte táto položka?',dl_item:'Ktorá karta je táto DL položka?',
  dl_supplier:'Ktorý dodávateľ?'};
function genericQuestionCard(q){
  const c=el('div','q');
  c.appendChild(el('div','who',GENERIC_TITLE[q.kind]||q.kind));
  c.appendChild(el('div','w',q.wording||''));
  c.appendChild(el('div','why',q.reason||''));
  for(const opt of (q.candidates||[])){
    const b=el('button',null,opt.label||opt.value);
    b.onclick=()=>answerGeneric(q.id,opt.value);c.appendChild(b)}
  const nb=el('button',null,'Neviem');
  nb.style.borderColor='#d0d7de';nb.style.background='#f6f8fa';nb.style.color='#57606a';
  nb.onclick=()=>answerGeneric(q.id,'unknown');c.appendChild(nb);
  return c}
async function answerGeneric(qid,choice){try{await api('/api/orders/question/'+qid+'/answer',
  {method:'POST',body:JSON.stringify({choice:choice})});await load()}
  catch(e){alert(e.message||'chyba')}}
// #235: dl_supplier/dl_item get their OWN card (mirrors #234's customerQuestionCard
// below) — a live search over the CURRENT DL suppliers/catalog (not just the frozen
// candidates the question was asked with), plus a collapsed "this is genuinely new"
// form. mail/date/line stay on the plain genericQuestionCard above, unchanged.
function dlSupplierSearchBox(q){
  const wrap=el('div');
  const inp=el('input','search');inp.placeholder='hľadaj dodávateľa (názov alebo EAN)…';
  const res=el('div','sres-wrap');
  wrap.appendChild(inp);wrap.appendChild(res);
  let seq=0;
  async function run(v){
    const mine=++seq;
    if(v.length<2){res.textContent='';return}
    let d;try{d=await api('/api/znalosti/dl-suppliers?q='+encodeURIComponent(v))}catch(e){return}
    if(mine!==seq)return;
    res.textContent='';
    const hits=d.items.filter(it=>it.ean_edi);
    if(!hits.length){res.appendChild(el('div','sres none','žiadna zhoda'));return}
    for(const it of hits){
      const b=el('div','sres',it.name+(it.city?'  ('+it.city+')':'')+'  ('+it.ean_edi+')');
      b.onclick=()=>answerGeneric(q.id,it.ean_edi);res.appendChild(b)}
  }
  let t=null;
  inp.oninput=()=>{clearTimeout(t);t=setTimeout(()=>run(inp.value.trim()),200)};
  return wrap}
function dlItemSearchBox(q){
  const wrap=el('div');
  const inp=el('input','search');inp.placeholder='hľadaj v DL katalógu (názov karty)…';
  const res=el('div','sres-wrap');
  wrap.appendChild(inp);wrap.appendChild(res);
  let seq=0;
  async function run(v){
    const mine=++seq;
    if(v.length<2){res.textContent='';return}
    let d;try{d=await api('/api/znalosti/dl-products?q='+encodeURIComponent(v))}catch(e){return}
    if(mine!==seq)return;
    res.textContent='';
    if(!d.items.length){res.appendChild(el('div','sres none','žiadna zhoda'));return}
    for(const it of d.items){
      const b=el('div','sres',it.name+'  ('+it.gtin+')');
      b.onclick=()=>answerGeneric(q.id,it.gtin);res.appendChild(b)}
  }
  let t=null;
  inp.oninput=()=>{clearTimeout(t);t=setTimeout(()=>run(inp.value.trim()),200)};
  return wrap}
function newDlSupplierForm(q){
  const ctx=q.payload||q.context||{};
  const box=el('div');box.style.marginTop='12px';
  const toggle=el('button',null,'➕ Nový dodávateľ (najprv EAN kód na karte v Codexe)');
  toggle.style.borderColor='#d0d7de';toggle.style.background='#f6f8fa';toggle.style.color='#57606a';
  const form=el('div');form.style.display='none';
  const ean=el('input');ean.placeholder='EAN kód EDI *';
  const name=el('input');name.placeholder='názov firmy *';
  const emails=el('input');emails.placeholder='e-maily';emails.value=ctx.sender_email||'';
  const city=el('input');city.placeholder='obec';
  for(const i of [ean,name,emails,city])form.appendChild(i);
  const status=el('div','slabel','');form.appendChild(status);
  const extra=el('div');form.appendChild(extra);
  const save=el('button',null,'Uložiť nového dodávateľa');
  save.style.borderColor='#1f6feb';save.style.background='#ddf4ff';save.style.color='#0969da';
  save.onclick=async()=>{
    const e=ean.value.replace(/[\s-]/g,'');
    if(!e){alert('Bez EAN kódu EDI sa dodávateľ nedá uložiť — nájdeš ho v CODEXe pri dodávateľovi.');return}
    if(!/^\d+$/.test(e)){alert('EAN kód EDI musí byť len číslice.');return}
    if(!name.value.trim()){alert('vyplň názov firmy');return}
    status.textContent='';extra.textContent='';save.disabled=true;
    try{
      await api('/api/orders/question/'+q.id+'/answer',{method:'POST',body:JSON.stringify({
        new_supplier:{ean_edi:e,name:name.value.trim(),emails:emails.value.trim(),
          city:city.value.trim()}})});
      await load()
    }catch(err){
      save.disabled=false;
      status.textContent=err.message||'chyba';
      // Deep-review finding (independent review, same PR): mirror newCustomerForm's own
      // one-click reclaim button — a 409 collision (see the httpapi.py collision check
      // this form posts to) already carries err.body.existing; render it instead of
      // leaving her to re-type the same EAN into the search box above.
      if(err.body&&err.body.existing){
        const b=el('button',null,'Použiť existujúceho '+err.body.existing.name);
        b.onclick=()=>answerGeneric(q.id,err.body.existing.ean_edi);
        extra.appendChild(b)}
    }
  };
  form.appendChild(save);
  toggle.onclick=()=>{form.style.display=form.style.display==='none'?'block':'none'};
  box.appendChild(toggle);box.appendChild(form);
  return box}
function newDlProductForm(q){
  const box=el('div');box.style.marginTop='12px';
  const toggle=el('button',null,'➕ Nový produkt (najprv EAN kód na karte v Codexe)');
  toggle.style.borderColor='#d0d7de';toggle.style.background='#f6f8fa';toggle.style.color='#57606a';
  const form=el('div');form.style.display='none';
  const gtin=el('input');gtin.placeholder='GTIN (EAN kód) *';
  const name=el('input');name.placeholder='názov produktu *';name.value=q.wording||'';
  for(const i of [gtin,name])form.appendChild(i);
  const status=el('div','slabel','');form.appendChild(status);
  const save=el('button',null,'Uložiť nový produkt');
  save.style.borderColor='#1f6feb';save.style.background='#ddf4ff';save.style.color='#0969da';
  save.onclick=async()=>{
    const g=gtin.value.replace(/[\s-]/g,'');
    if(!g){alert('Bez GTIN sa karta nedá uložiť — nájdeš ho v CODEXe pri produkte.');return}
    if(!/^\d+$/.test(g)){alert('GTIN musí byť len číslice.');return}
    if(!name.value.trim()){alert('vyplň názov produktu');return}
    status.textContent='';save.disabled=true;
    try{
      await api('/api/orders/question/'+q.id+'/answer',{method:'POST',body:JSON.stringify({
        new_item:{gtin:g,name:name.value.trim()}})});
      await load()
    }catch(err){save.disabled=false;status.textContent=err.message||'chyba'}
  };
  form.appendChild(save);
  toggle.onclick=()=>{form.style.display=form.style.display==='none'?'block':'none'};
  box.appendChild(toggle);box.appendChild(form);
  return box}
function dlSupplierQuestionCard(q){
  const c=el('div','q');
  c.appendChild(el('div','who',GENERIC_TITLE.dl_supplier));
  c.appendChild(el('div','w',q.wording||''));
  c.appendChild(el('div','why',q.reason||''));
  for(const opt of (q.candidates||[])){
    const b=el('button',null,opt.label||opt.value);
    b.onclick=()=>answerGeneric(q.id,opt.value);c.appendChild(b)}
  c.appendChild(el('div','slabel','alebo nájdi v celej databáze dodávateľov:'));
  c.appendChild(dlSupplierSearchBox(q));
  c.appendChild(newDlSupplierForm(q));
  const nb=el('button',null,'Neviem');
  nb.style.borderColor='#d0d7de';nb.style.background='#f6f8fa';nb.style.color='#57606a';
  nb.onclick=()=>answerGeneric(q.id,'unknown');c.appendChild(nb);
  return c}
function dlItemQuestionCard(q){
  const c=el('div','q');
  c.appendChild(el('div','who',GENERIC_TITLE.dl_item));
  c.appendChild(el('div','w',q.wording||''));
  c.appendChild(el('div','why',q.reason||''));
  for(const opt of (q.candidates||[])){
    const b=el('button',null,opt.label||opt.value);
    b.onclick=()=>answerGeneric(q.id,opt.value);c.appendChild(b)}
  c.appendChild(el('div','slabel','alebo nájdi v celom DL katalógu:'));
  c.appendChild(dlItemSearchBox(q));
  c.appendChild(newDlProductForm(q));
  const nb=el('button',null,'Neviem');
  nb.style.borderColor='#d0d7de';nb.style.background='#f6f8fa';nb.style.color='#57606a';
  nb.onclick=()=>answerGeneric(q.id,'unknown');c.appendChild(nb);
  return c}
// #234: a live search over ALL current customers — not just the frozen candidates the
// question was asked with. Mirrors searchBox() above, one input, debounced.
function customerSearchBox(q){
  const wrap=el('div');
  const inp=el('input','search');inp.placeholder='hľadaj zákazníka (názov alebo EAN)…';
  const res=el('div','sres-wrap');
  wrap.appendChild(inp);wrap.appendChild(res);
  let seq=0;
  async function run(v){
    const mine=++seq;
    if(v.length<2){res.textContent='';return}
    let d;try{d=await api('/api/znalosti/clients?q='+encodeURIComponent(v))}catch(e){return}
    if(mine!==seq)return;
    res.textContent='';
    const hits=d.items.filter(it=>it.ean_edi);   // an EAN-less row cannot be picked either
    if(!hits.length){res.appendChild(el('div','sres none','žiadna zhoda'));return}
    for(const it of hits){
      const addr=[it.street,it.city].filter(Boolean).join(', ');
      const b=el('div','sres',it.name+(addr?'  ('+addr+')':'')+'  ('+it.ean_edi+')');
      b.onclick=()=>answerCustomer(q.id,it.ean_edi,it.name||'');res.appendChild(b)}
  }
  let t=null;
  inp.oninput=()=>{clearTimeout(t);t=setTimeout(()=>run(inp.value.trim()),200)};
  return wrap}
// #234: the customer genuinely does not exist anywhere yet — create it right on the card,
// prefilled from the mail. Collapsed by default so the common (candidate/search) path
// stays uncluttered.
function newCustomerForm(q){
  const ctx=q.context||{};
  const box=el('div');box.style.marginTop='12px';
  const toggle=el('button',null,'➕ Nový zákazník (najprv ho vytvor v CODEXe)');
  toggle.style.borderColor='#d0d7de';toggle.style.background='#f6f8fa';toggle.style.color='#57606a';
  const form=el('div');form.style.display='none';
  const ean=el('input');ean.placeholder='EAN kód EDI *';
  const name=el('input');name.placeholder='názov firmy *';
  name.value=ctx.company_name||ctx.sender_name||'';
  const emails=el('input');emails.placeholder='e-maily';emails.value=ctx.sender_email||'';
  const city=el('input');city.placeholder='obec';
  const street=el('input');street.placeholder='ulica';street.value=ctx.delivery_address_guess||'';
  const zip=el('input');zip.placeholder='PSČ';
  for(const i of [ean,name,emails,city,street,zip])form.appendChild(i);
  const status=el('div','slabel','');form.appendChild(status);
  const extra=el('div');form.appendChild(extra);
  const save=el('button',null,'Uložiť nového zákazníka');
  save.style.borderColor='#1f6feb';save.style.background='#ddf4ff';save.style.color='#0969da';
  save.onclick=async()=>{
    const e=ean.value.replace(/[\s-]/g,'');
    if(!e){alert('Bez EAN kódu EDI sa zákazník nedá uložiť — nájdeš ho v CODEXe pri odberateľovi.');return}
    if(!/^\d+$/.test(e)){alert('EAN kód EDI musí byť len číslice.');return}
    if(!name.value.trim()){alert('vyplň názov firmy');return}
    if(e.length!==13&&!confirm('EAN kód EDI má obvykle 13 číslic, zadal si '+e.length+'. Naozaj uložiť?'))return;
    status.textContent='';extra.textContent='';
    // #234 review finding: a fast double-click sent two overlapping POSTs — the server
    // is now race-safe (advisory lock) either way, but this closes off the easy trigger.
    save.disabled=true;
    try{
      await api('/api/orders/question/'+q.id+'/answer',{method:'POST',body:JSON.stringify({
        new_customer:{ean_edi:e,name:name.value.trim(),emails:emails.value.trim(),
          city:city.value.trim(),street:street.value.trim(),zip:zip.value.trim()}})});
      await load()
    }catch(err){
      save.disabled=false;
      status.textContent=err.message||'chyba';
      if(err.body&&err.body.existing){
        const b=el('button',null,'Doplniť e-mail k '+err.body.existing.name);
        b.onclick=()=>answerCustomer(q.id,err.body.existing.ean_edi,err.body.existing.name);
        extra.appendChild(b)}
    }
  };
  form.appendChild(save);
  toggle.onclick=()=>{form.style.display=form.style.display==='none'?'block':'none'};
  box.appendChild(toggle);box.appendChild(form);
  return box}
// #159: "who is this customer?" candidates render as name + address (+ a ✓ badge when
// the ranking already found the address in the mail), plus a "neviem, kto to je" escape.
// #234: a candidate with no EAN renders disabled (would just 400), plus a live search over
// every current customer and a "add a brand-new one" form, right on the same card.
function customerQuestionCard(q){
  const ctx=q.context||{};const c=el('div','q');
  c.appendChild(el('div','who','Neznámy zákazník'+(q.delivery_date?' · na '+q.delivery_date:'')));
  c.appendChild(el('div','w',ctx.sender_email||q.wording));
  const bits=[];
  if(ctx.sender_name)bits.push('meno: '+ctx.sender_name);
  if(ctx.company_name)bits.push('firma: '+ctx.company_name);
  if(ctx.delivery_address_guess)bits.push('adresa v maile: '+ctx.delivery_address_guess);
  c.appendChild(el('div','why',(q.reason||'Kto to objednal?')+(bits.length?' — '+bits.join(' · '):'')));
  for(const cand of (q.candidates||[])){
    const addr=[cand.street,cand.city].filter(Boolean).join(', ');
    if(!cand.ean_edi){
      const b=el('button',null,(cand.name||'(bez mena)')+(addr?'  ('+addr+')':'')+
        '  — bez EAN, doplň v databáze znalostí');
      b.disabled=true;b.style.opacity='0.55';b.style.cursor='default';b.style.borderColor='#d0d7de';
      b.style.background='#f6f8fa';b.style.color='#57606a';c.appendChild(b);continue}
    const label=(cand.name||cand.ean_edi)+(addr?'  ('+addr+')':'')+(cand.address_match?'  ✓ adresa sedí':'');
    const b=el('button',null,label);
    b.onclick=()=>answerCustomer(q.id,cand.ean_edi,cand.name||'');c.appendChild(b)}
  c.appendChild(el('div','slabel','alebo nájdi v celej databáze zákazníkov:'));
  c.appendChild(customerSearchBox(q));
  c.appendChild(newCustomerForm(q));
  const nb=el('button',null,'Neviem, kto to je');
  nb.style.borderColor='#d0d7de';nb.style.background='#f6f8fa';nb.style.color='#57606a';
  nb.onclick=()=>answerCustomer(q.id,'','',true);c.appendChild(nb);
  return c}
async function answerCustomer(qid,ean_edi,name,unknown){try{await api('/api/orders/question/'+qid+'/answer',
  {method:'POST',body:JSON.stringify(unknown?{unknown:true}:{ean_edi:ean_edi,name:name})});await load()}
  catch(e){alert(e.message||'chyba')}}
async function load(){const mine=++render;let d,t;
  try{d=await api('/api/orders/questions');t=await api('/api/orders/taught')}catch(e){return}
  if(mine!==render)return;
  const W=document.getElementById('wrap');W.textContent='';
  if(!d.items.length)W.appendChild(el('div','empty','Nič nečaká. Ďakujem!'));
  for(const q of d.items){
    if(q.kind==='customer'){W.appendChild(customerQuestionCard(q));continue}
    if(q.kind==='dl_supplier'){W.appendChild(dlSupplierQuestionCard(q));continue}
    if(q.kind==='dl_item'){W.appendChild(dlItemQuestionCard(q));continue}
    if(q.kind==='mail'||q.kind==='date'||q.kind==='line'){
      W.appendChild(genericQuestionCard(q));continue}
    const c=el('div','q');
    c.appendChild(el('div','who',(q.customer_name||q.customer_ean)+(q.delivery_date?' · na '+q.delivery_date:'')));
    c.appendChild(el('div','w',q.wording+(q.quantity?'  —  '+q.quantity+' '+(q.unit||'ks'):'')));
    c.appendChild(el('div','why',q.reason||'Ktorý výrobok to je?'));
    for(const cand of (q.candidates||[])){const b=el('button',null,cand.name||cand.gtin);
      b.onclick=()=>teach(q.id,cand.gtin,cand.name||'');c.appendChild(b)}
    c.appendChild(el('div','slabel','alebo vyhľadaj v celom katalógu:'));
    c.appendChild(searchBox(q));
    const kb=document.createElement('a');kb.className='kb';kb.textContent='📚 databáza znalostí';
    kb.href='/znalosti/'+encodeURIComponent(q.customer_ean)+'?wording='+encodeURIComponent(q.wording);
    c.appendChild(kb);
    W.appendChild(c)}
  if(t.items.length){W.appendChild(el('h2',null,'Naposledy naučené'));
    for(const x of t.items){const r=el('div','t');
      r.appendChild(el('span',null,x.wording+' → '+(x.answer_card||x.answer_gtin)));
      const b=el('button',null,'vrátiť');b.onclick=()=>undo(x.id);r.appendChild(b);W.appendChild(r)}}}
async function teach(qid,gtin,card){try{await api('/api/orders/question/'+qid+'/answer',
  {method:'POST',body:JSON.stringify({gtin:gtin,card:card})});delete searchState[qid];await load()}
  catch(e){alert(e.message||'chyba')}}
async function undo(qid){try{await api('/api/orders/question/'+qid+'/undo',{method:'POST'});
  await load()}catch(e){alert(e.message||'chyba')}}
__STATS_SCRIPT__
load();setInterval(load,5000);
</script></body></html>"""

ASK_HTML = (_ASK_HTML_TEMPLATE
           .replace("__TITLE__", "Otázky skladu")
           .replace("__HEADING__", "&#128230; Otázky skladu")
           .replace("__STATS_HEADER__", "")
           .replace("__ALERT_BANNER__", "")
           .replace("__STATS_SCRIPT__", ""))

# #231: the DL nástenka additionally shows today/yesterday's DL run "stavy" (states) —
# a small text strip in the header, fed by `/api/orders/dl/stats` (role-scoped, see
# httpapi.py's `api_orders_dl_stats`). The orders board has no equivalent (out of scope
# for this ticket) — the placeholders above are replaced with "" for it, so nothing is
# fetched or rendered there.
#
# #239 reopened, finding 5: the three current-state gauges used to be three words
# silently appended to this SAME small header strip, shown only when non-zero — visually
# indistinguishable from ordinary text, easy to miss entirely (verified live: on a quiet
# day nothing at all was rendered, so the warehouse never learned the feature existed).
# A separate, visually prominent banner (`__ALERT_BANNER__`, hidden when quiet) now
# carries them instead, each on its own line with plain wording explaining what happened
# and what to do — the header strip itself stays the plain today/yesterday summary only.
ASK_DL_HTML = (_ASK_HTML_TEMPLATE
              .replace("__TITLE__", "Dodacie listy — sklad")
              .replace("__HEADING__", "&#128666; Dodacie listy")
              .replace("__STATS_HEADER__", '<span class="ver" id="dlStats"></span>')
              .replace("__ALERT_BANNER__",
                      '<div id="dlAlertBanner" class="dl-alert-banner" '
                      'style="display:none"></div>')
              .replace("__STATS_SCRIPT__", r"""
async function loadStats(){try{const d=await api('/api/orders/dl/stats');
  const t=d.today||{},y=d.yesterday||{};
  let s='dnes: '+(t.runs||0)+' spracovaných, '+(t.duplicates||0)+' duplicít, '+
    (t.announced_mismatch||0)+' nezhôd · včera: '+(y.runs||0)+' spracovaných';
  document.getElementById('dlStats').textContent=s;
  // #239 reopened, finding 5: each nonzero class gets its OWN plain-Slovak line in a
  // prominent banner, explaining what happened and what to do — never just a number
  // silently glued onto the header strip above.
  const banner=document.getElementById('dlAlertBanner');
  const lines=[];
  if(t.quarantined) lines.push('&#128683; '+t.quarantined+' dodací(ch) list(ov) sa po '+
    (t.quarantine_threshold||5)+' pokusoch vzdalo spracovania &mdash; skontroluj v '+
    'dashboarde.');
  if(t.pending_alerts) lines.push('&#128276; '+t.pending_alerts+
    ' upozornenie/upozornení stále čaká na odoslanie do Odoo.');
  if(t.open_import_incidents) lines.push('&#128230; '+t.open_import_incidents+
    ' otvorený problém s importom do ORIONu.');
  if(lines.length){
    banner.innerHTML=lines.map(l=>'<div>'+l+'</div>').join('');
    banner.style.display='block';
  }else{
    banner.style.display='none';
    banner.innerHTML='';
  }}
  catch(e){}}
loadStats();setInterval(loadStats,30000);"""))


# #104: direct curation of wording->card knowledge (no order_questions row required).
# Same page for /znalosti (global only, + a customer search to jump to one) and
# /znalosti/<ean> (that customer's own aliases + the global section underneath) — the JS
# below reads the ean out of location.pathname, exactly like ASK_HTML reads none at all.
ZNALOSTI_HTML = r"""<!doctype html><html lang="sk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Databáza znalostí</title>
<style>
 *{box-sizing:border-box}
 body{font:15px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:0;
      background:#f6f8fa;color:#1f2328}
 header{background:#161b22;color:#e6edf3;padding:12px 16px;display:flex;justify-content:space-between;
        align-items:center;position:sticky;top:0}
 h1{font-size:16px;margin:0}
 .ver{font-size:12px;color:#8b949e}
 main{padding:14px 12px;max-width:760px;margin:0 auto}
 h2{font-size:14px;color:#57606a;margin:22px 0 8px}
 .box{background:#fff;border:1px solid #d0d7de;border-radius:12px;padding:14px;margin-bottom:14px}
 .row{background:#fff;border:1px solid #d0d7de;border-radius:10px;padding:10px 12px;margin-bottom:8px;
      display:flex;justify-content:space-between;align-items:center;gap:10px;font-size:14px}
 .row .meta{font-size:12px;color:#57606a}
 .row button{width:auto;margin:0;border-color:#d0d7de;background:#f6f8fa;color:#57606a;padding:7px 12px;
             border-radius:8px;font:inherit;cursor:pointer}
 input{width:100%;padding:9px 10px;margin-top:6px;border:1px solid #d0d7de;border-radius:8px;font:inherit}
 .cands{margin-top:4px}
 .cand{padding:8px 10px;border:1px solid #d0d7de;border-radius:8px;margin-top:4px;cursor:pointer;font-size:14px}
 .cand:hover{background:#ddf4ff}
 .picked{font-size:13px;color:#1a7f37;margin-top:6px}
 button.add{display:block;width:100%;text-align:center;padding:11px;margin-top:10px;font:inherit;
        border:1px solid #1f6feb;border-radius:10px;background:#ddf4ff;color:#0969da;cursor:pointer}
 .box>button:not(.add){display:block;width:100%;text-align:center;padding:9px;margin-top:6px;
        font:inherit;border:1px solid #d0d7de;border-radius:10px;background:#f6f8fa;
        color:#57606a;cursor:pointer}
 .empty{color:#57606a;padding:6px 2px}
 .who{font-size:13px;color:#57606a;margin-bottom:6px}
</style></head><body>
<header><h1>&#128218; Databáza znalostí</h1><span class="ver" data-testid="version">v__VERSION__</span></header>
<main id="wrap"><div class="empty">Nahrávam&hellip;</div></main>
<script>
async function api(u,o){const r=await fetch(u,Object.assign({headers:{'Content-Type':'application/json'}},o||{}));
  if(!r.ok){throw new Error((await r.json().catch(()=>({}))).error||('HTTP '+r.status))}return r.json()}
function el(t,cls,txt){const e=document.createElement(t);if(cls)e.className=cls;
  if(txt!==undefined)e.textContent=txt;return e}
const parts=location.pathname.split('/').filter(Boolean);
const EAN=parts.length>1?decodeURIComponent(parts[1]):'';
const params=new URLSearchParams(location.search);
const PREFILL=params.get('wording')||'';
let picked=null;

function pickerBox(inputId,candId,onPick){
  const wrap=el('div');
  const inp=el('input');inp.id=inputId;inp.placeholder='hľadaj kartu (názov alebo GTIN)…';
  const cands=el('div','cands');cands.id=candId;
  wrap.appendChild(inp);wrap.appendChild(cands);
  let t=null;
  inp.oninput=()=>{clearTimeout(t);t=setTimeout(async()=>{
    const q=inp.value.trim();cands.textContent='';
    if(q.length<2)return;
    const d=await api('/api/znalosti/catalog?q='+encodeURIComponent(q));
    for(const it of d.items){const c=el('div','cand',it.name+'  ('+it.gtin+')');
      c.onclick=()=>{onPick(it);inp.value=it.name;cands.textContent=''};cands.appendChild(c)}
  },200)};
  return wrap;
}

function addForm(onSubmit,wordingPrefill){
  const box=el('div','box');
  box.appendChild(el('h2',null,'Pridať priradenie'));
  const w=el('input');w.placeholder='znenie (ako to zákazník píše)';w.value=wordingPrefill||'';
  box.appendChild(w);
  let chosen=null;
  const status=el('div','picked','');
  box.appendChild(pickerBox('','',(it)=>{chosen=it;status.textContent='vybraná karta: '+it.name}));
  box.appendChild(status);
  const b=el('button','add','Uložiť');
  b.onclick=async()=>{
    if(!w.value.trim()||!chosen){alert('vyplň znenie a vyber kartu zo zoznamu');return}
    try{await onSubmit(w.value.trim(),chosen.gtin,chosen.name);w.value='';chosen=null;
      status.textContent='';await load()}catch(e){alert(e.message||'chyba')}
  };
  box.appendChild(b);
  return box;
}

function aliasRow(item,onDelete){
  const r=el('div','row');
  const left=el('div');
  left.appendChild(el('div',null,item.item_raw+' → '+(item.card||item.gtin)));
  left.appendChild(el('div','meta',(item.source||item.taught_by||'')+' · '+
    String(item.created_at||'').slice(0,10)));
  r.appendChild(left);
  const curated=(item.source===undefined)||item.source==='human'||item.source==='sheet-import';
  if(curated){const b=el('button',null,'zmazať');b.onclick=onDelete;r.appendChild(b)}
  return r;
}

// #127: direct add/edit/retire of product cards, keyed by GTIN — one form doubles as
// add (unknown GTIN) and edit (known GTIN); a click on a search result loads it in.
function productsBox(){
  const box=el('div','box');
  box.appendChild(el('h2',null,'Karty výrobkov'));
  const gtin=el('input');gtin.placeholder='GTIN';
  const name=el('input');name.placeholder='názov karty';
  box.appendChild(gtin);box.appendChild(name);
  const status=el('div','picked','');box.appendChild(status);
  const list=el('div');
  async function refresh(q){
    list.textContent='';
    const d=await api('/api/znalosti/products'+(q?('?q='+encodeURIComponent(q)):''));
    if(!d.items.length){list.appendChild(el('div','empty','Zatiaľ nič.'));return}
    for(const it of d.items){
      const r=el('div','row');
      r.appendChild(el('div',null,it.name+'  ('+it.gtin+')'+(it.overridden?' · upravené':'')));
      const b=el('button',null,'upraviť');
      b.onclick=()=>{gtin.value=it.gtin;name.value=it.name;status.textContent=''};
      r.appendChild(b);list.appendChild(r)
    }
  }
  const save=el('button','add','Uložiť (nový GTIN = pridá, existujúci = upraví)');
  save.onclick=async()=>{
    if(!gtin.value.trim()||!name.value.trim()){alert('vyplň GTIN aj názov');return}
    try{await api('/api/znalosti/products',{method:'POST',
      body:JSON.stringify({gtin:gtin.value.trim(),name:name.value.trim()})});
      status.textContent='uložené';await refresh(search.value.trim())}
    catch(e){alert(e.message||'chyba')}
  };
  box.appendChild(save);
  const retire=el('button',null,'Vyradiť kartu s GTIN vyššie');
  retire.onclick=async()=>{
    const g=gtin.value.trim();if(!g)return;
    if(!confirm('Vyradiť kartu '+g+'?'))return;
    try{await api('/api/znalosti/products/'+encodeURIComponent(g),{method:'DELETE'});
      gtin.value='';name.value='';status.textContent='vyradené';await refresh(search.value.trim())}
    catch(e){alert(e.message||'chyba')}
  };
  box.appendChild(retire);
  var search=el('input');search.placeholder='hľadaj kartu (názov alebo GTIN)…';
  box.appendChild(search);box.appendChild(list);
  let t=null;search.oninput=()=>{clearTimeout(t);t=setTimeout(()=>refresh(search.value.trim()),200)};
  refresh('');
  return box;
}

// #128: direct add/edit/retire of customers. `editing` tracks the identity the SAVE
// button targets (null = a brand-new customer); picking a search result fills the form
// AND the identity, exactly like productsBox does with a bare gtin.
function clientsBox(){
  const box=el('div','box');
  box.appendChild(el('h2',null,'Odberatelia'));
  const ean=el('input');ean.placeholder='EAN kód EDI *';
  const name=el('input');name.placeholder='názov firmy';
  const emails=el('input');emails.placeholder='e-maily (čiarkou oddelené)';
  const city=el('input');city.placeholder='obec';
  const street=el('input');street.placeholder='ulica';
  const zip=el('input');zip.placeholder='PSČ';
  for(const i of [ean,name,emails,city,street,zip])box.appendChild(i);
  const status=el('div','picked','');box.appendChild(status);
  let editing=null;
  function clearForm(){ean.value=name.value=emails.value=city.value=street.value=zip.value='';editing=null}
  const list=el('div');
  async function refresh(q){
    list.textContent='';
    const d=await api('/api/znalosti/clients'+(q?('?q='+encodeURIComponent(q)):''));
    if(!d.items.length){list.appendChild(el('div','empty','Zatiaľ nič.'));return}
    for(const it of d.items){
      const r=el('div','row');
      // #234: a legacy blank-EAN row must be VISIBLE as needing attention, not silent.
      r.appendChild(el('div',null,it.name+'  ('+(it.ean_edi||'bez EAN — doplň')+')'+
        (it.street?(' · '+it.street):'')));
      const b=el('button',null,'upraviť');
      b.onclick=()=>{
        ean.value=it.ean_edi||'';name.value=it.name||'';emails.value=(it.emails||[]).join(', ');
        city.value=it.city||'';street.value=it.street||'';zip.value=it.zip||'';
        editing={override_id:it.override_id,orig_ean_edi:it.orig_ean_edi,orig_street:it.orig_street};
        status.textContent=''
      };
      r.appendChild(b);list.appendChild(r)
    }
  }
  const save=el('button','add','Uložiť');
  save.onclick=async()=>{
    if(!name.value.trim()){alert('vyplň názov');return}
    const cleaned=ean.value.trim().replace(/[\s-]/g,'');
    if(!cleaned){alert('Bez EAN kódu EDI sa zákazník nedá uložiť — nájdeš ho v CODEXe pri odberateľovi.');return}
    if(!/^\d+$/.test(cleaned)){alert('EAN kód EDI musí byť len číslice.');return}
    const body={ean_edi:cleaned,name:name.value.trim(),emails:emails.value.trim(),
      city:city.value.trim(),street:street.value.trim(),zip:zip.value.trim()};
    if(editing)Object.assign(body,editing);
    try{await api('/api/znalosti/clients',{method:'POST',body:JSON.stringify(body)});
      status.textContent='uložené';await refresh(search.value.trim())}
    catch(e){alert(e.message||'chyba')}
  };
  box.appendChild(save);
  const retire=el('button',null,'Vyradiť tohto odberateľa');
  retire.onclick=async()=>{
    if(!editing){alert('najprv vyber existujúceho odberateľa zo zoznamu');return}
    if(!confirm('Vyradiť '+(name.value||'tohto odberateľa')+'?'))return;
    try{await api('/api/znalosti/clients',{method:'DELETE',body:JSON.stringify(editing)});
      clearForm();status.textContent='vyradené';await refresh(search.value.trim())}
    catch(e){alert(e.message||'chyba')}
  };
  box.appendChild(retire);
  var search=el('input');search.placeholder='hľadaj odberateľa (názov alebo EAN)…';
  box.appendChild(search);box.appendChild(list);
  let t=null;search.oninput=()=>{clearTimeout(t);t=setTimeout(()=>refresh(search.value.trim()),200)};
  refresh('');
  return box;
}

// #221: direct add/edit/retire of DL catalog cards (mirror of #127's productsBox, with the
// DL-specific fields doplnok/mass/sklad/cena added — see dl_snapshot.py).
function dlProductsBox(){
  const box=el('div','box');
  box.appendChild(el('h2',null,'DL katalóg (dodacie listy)'));
  const gtin=el('input');gtin.placeholder='GTIN';
  const name=el('input');name.placeholder='názov karty';
  const doplnok=el('input');doplnok.placeholder='doplnok';
  const mass=el('input');mass.placeholder='hmotnosť (kg)';
  const sklad=el('input');sklad.placeholder='sklad';
  const cena=el('input');cena.placeholder='cena';
  for(const i of [gtin,name,doplnok,mass,sklad,cena])box.appendChild(i);
  const status=el('div','picked','');box.appendChild(status);
  const list=el('div');
  async function refresh(q){
    list.textContent='';
    const d=await api('/api/znalosti/dl-products'+(q?('?q='+encodeURIComponent(q)):''));
    if(!d.items.length){list.appendChild(el('div','empty','Zatiaľ nič.'));return}
    for(const it of d.items){
      const r=el('div','row');
      r.appendChild(el('div',null,it.name+'  ('+it.gtin+')'+(it.overridden?' · upravené':'')));
      const b=el('button',null,'upraviť');
      b.onclick=()=>{gtin.value=it.gtin;name.value=it.name;doplnok.value=it.doplnok||'';
        mass.value=it.mass==null?'':it.mass;sklad.value=it.sklad||'';
        cena.value=it.cena==null?'':it.cena;status.textContent=''};
      r.appendChild(b);list.appendChild(r)
    }
  }
  const save=el('button','add','Uložiť (nový GTIN = pridá, existujúci = upraví)');
  save.onclick=async()=>{
    if(!gtin.value.trim()||!name.value.trim()){alert('vyplň GTIN aj názov');return}
    try{await api('/api/znalosti/dl-products',{method:'POST',
      body:JSON.stringify({gtin:gtin.value.trim(),name:name.value.trim(),
        doplnok:doplnok.value.trim(),mass:mass.value.trim(),sklad:sklad.value.trim(),
        cena:cena.value.trim()})});
      status.textContent='uložené';await refresh(search.value.trim())}
    catch(e){alert(e.message||'chyba')}
  };
  box.appendChild(save);
  const retire=el('button',null,'Vyradiť kartu s GTIN vyššie');
  retire.onclick=async()=>{
    const g=gtin.value.trim();if(!g)return;
    if(!confirm('Vyradiť kartu '+g+'?'))return;
    try{await api('/api/znalosti/dl-products/'+encodeURIComponent(g),{method:'DELETE'});
      gtin.value='';name.value='';doplnok.value='';mass.value='';sklad.value='';cena.value='';
      status.textContent='vyradené';await refresh(search.value.trim())}
    catch(e){alert(e.message||'chyba')}
  };
  box.appendChild(retire);
  var search=el('input');search.placeholder='hľadaj kartu (názov alebo GTIN)…';
  box.appendChild(search);box.appendChild(list);
  let t=null;search.oninput=()=>{clearTimeout(t);t=setTimeout(()=>refresh(search.value.trim()),200)};
  refresh('');
  return box;
}

// #221: direct add/edit/retire of DL suppliers (mirror of #128's clientsBox). Identity is
// city-only (no street/zip) — dl_supplier_snapshot never persists those, see dl_snapshot.py.
function dlSuppliersBox(){
  const box=el('div','box');
  box.appendChild(el('h2',null,'Dodávatelia (dodacie listy)'));
  const ean=el('input');ean.placeholder='EAN kód EDI';
  const name=el('input');name.placeholder='názov firmy';
  const emails=el('input');emails.placeholder='e-maily (čiarkou oddelené)';
  const city=el('input');city.placeholder='obec';
  for(const i of [ean,name,emails,city])box.appendChild(i);
  const status=el('div','picked','');box.appendChild(status);
  let editing=null;
  function clearForm(){ean.value=name.value=emails.value=city.value='';editing=null}
  const list=el('div');
  async function refresh(q){
    list.textContent='';
    const d=await api('/api/znalosti/dl-suppliers'+(q?('?q='+encodeURIComponent(q)):''));
    if(!d.items.length){list.appendChild(el('div','empty','Zatiaľ nič.'));return}
    for(const it of d.items){
      const r=el('div','row');
      r.appendChild(el('div',null,it.name+'  ('+(it.ean_edi||'bez EAN')+')'+
        (it.city?(' · '+it.city):'')));
      const b=el('button',null,'upraviť');
      b.onclick=()=>{
        ean.value=it.ean_edi||'';name.value=it.name||'';emails.value=(it.emails||[]).join(', ');
        city.value=it.city||'';
        editing={override_id:it.override_id,orig_ean_edi:it.orig_ean_edi,orig_city:it.orig_city};
        status.textContent=''
      };
      r.appendChild(b);list.appendChild(r)
    }
  }
  const save=el('button','add','Uložiť');
  save.onclick=async()=>{
    if(!name.value.trim()){alert('vyplň názov');return}
    const body={ean_edi:ean.value.trim(),name:name.value.trim(),emails:emails.value.trim(),
      city:city.value.trim()};
    if(editing)Object.assign(body,editing);
    try{await api('/api/znalosti/dl-suppliers',{method:'POST',body:JSON.stringify(body)});
      status.textContent='uložené';await refresh(search.value.trim())}
    catch(e){alert(e.message||'chyba')}
  };
  box.appendChild(save);
  const retire=el('button',null,'Vyradiť tohto dodávateľa');
  retire.onclick=async()=>{
    if(!editing){alert('najprv vyber existujúceho dodávateľa zo zoznamu');return}
    if(!confirm('Vyradiť '+(name.value||'tohto dodávateľa')+'?'))return;
    try{await api('/api/znalosti/dl-suppliers',{method:'DELETE',body:JSON.stringify(editing)});
      clearForm();status.textContent='vyradené';await refresh(search.value.trim())}
    catch(e){alert(e.message||'chyba')}
  };
  box.appendChild(retire);
  var search=el('input');search.placeholder='hľadaj dodávateľa (názov alebo EAN)…';
  box.appendChild(search);box.appendChild(list);
  let t=null;search.oninput=()=>{clearTimeout(t);t=setTimeout(()=>refresh(search.value.trim()),200)};
  refresh('');
  return box;
}

// #128: on the /znalosti/<ean> page, edit THIS customer directly (no search needed —
// the page already fixes which one). `record` is null only if the ean matches nobody.
function customerEditBox(record, fallbackName){
  const box=el('div','box');
  box.appendChild(el('h2',null,'Upraviť údaje zákazníka'));
  const ean=el('input');ean.placeholder='EAN kód EDI';ean.value=(record&&record.ean_edi)||EAN;
  const name=el('input');name.placeholder='názov firmy';
  name.value=(record&&record.name)||fallbackName||'';
  const emails=el('input');emails.placeholder='e-maily (čiarkou oddelené)';
  emails.value=record?(record.emails||[]).join(', '):'';
  const city=el('input');city.placeholder='obec';city.value=(record&&record.city)||'';
  const street=el('input');street.placeholder='ulica';street.value=(record&&record.street)||'';
  const zip=el('input');zip.placeholder='PSČ';zip.value=(record&&record.zip)||'';
  for(const i of [ean,name,emails,city,street,zip])box.appendChild(i);
  const b=el('button','add','Uložiť zmeny');
  b.onclick=async()=>{
    if(!name.value.trim()){alert('vyplň názov');return}
    const body={ean_edi:ean.value.trim(),name:name.value.trim(),emails:emails.value.trim(),
      city:city.value.trim(),street:street.value.trim(),zip:zip.value.trim()};
    if(record){body.override_id=record.override_id;body.orig_ean_edi=record.orig_ean_edi;
      body.orig_street=record.orig_street}
    try{await api('/api/znalosti/clients',{method:'POST',body:JSON.stringify(body)});
      alert('uložené');location.reload()}
    catch(e){alert(e.message||'chyba')}
  };
  box.appendChild(b);
  return box;
}

async function load(){
  const W=document.getElementById('wrap');W.textContent='';
  if(EAN){
    const d=await api('/api/znalosti/customer/'+encodeURIComponent(EAN));
    W.appendChild(el('div','who',(d.customer_name||EAN)+'  ('+EAN+')'));
    W.appendChild(customerEditBox(d.record,d.customer_name));
    W.appendChild(addForm((wording,gtin,card)=>
      api('/api/znalosti/customer/'+encodeURIComponent(EAN),
         {method:'POST',body:JSON.stringify({wording:wording,gtin:gtin,card:card})}),
      PREFILL));
    W.appendChild(el('h2',null,'Priradenia tohto zákazníka'));
    if(!d.items.length)W.appendChild(el('div','empty','Zatiaľ nič.'));
    for(const it of d.items){W.appendChild(aliasRow(it,async()=>{
      try{await api('/api/znalosti/customer/'+encodeURIComponent(EAN)+'/'+it.id,{method:'DELETE'});
        await load()}catch(e){alert(e.message||'chyba')}}))}
  } else {
    const box=el('div','box');
    box.appendChild(el('h2',null,'Nájsť zákazníka'));
    const inp=el('input');inp.placeholder='hľadaj zákazníka (názov alebo EAN)…';
    const cands=el('div','cands');
    let t=null;
    inp.oninput=()=>{clearTimeout(t);t=setTimeout(async()=>{
      const q=inp.value.trim();cands.textContent='';if(q.length<2)return;
      const d=await api('/api/znalosti/customers?q='+encodeURIComponent(q));
      for(const c of d.items){const e=el('div','cand',c.name+'  ('+c.ean_edi+')');
        e.onclick=()=>{location.href='/znalosti/'+encodeURIComponent(c.ean_edi)};cands.appendChild(e)}
    },200)};
    box.appendChild(inp);box.appendChild(cands);
    W.appendChild(box);
    W.appendChild(productsBox());
    W.appendChild(clientsBox());
__DL_BOXES__  }
  W.appendChild(el('h2',null,'Globálne priradenia (platia pre každého zákazníka)'));
  W.appendChild(addForm((wording,gtin,card)=>
    api('/api/znalosti/global',{method:'POST',body:JSON.stringify({wording:wording,gtin:gtin,card:card})})));
  const g=await api('/api/znalosti/global');
  if(!g.items.length)W.appendChild(el('div','empty','Zatiaľ nič.'));
  for(const it of g.items){W.appendChild(aliasRow(it,async()=>{
    try{await api('/api/znalosti/global/'+it.id,{method:'DELETE'});await load()}
    catch(e){alert(e.message||'chyba')}}))}
}
load();
</script></body></html>"""


def start(cfg) -> None:
    app = create_app(cfg)
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=cfg.http_port, threaded=True),
        daemon=True,
    ).start()
