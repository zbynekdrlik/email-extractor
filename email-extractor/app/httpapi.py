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
import threading
import time
from datetime import timedelta
from pathlib import Path

import psycopg
from flask import Flask, abort, jsonify, redirect, request, session
from psycopg.types.json import Json
from werkzeug.exceptions import HTTPException

from . import __version__, db, httpapi_files, httpapi_fixqueue, httpapi_reports, linkutil
from .httpapi_common import (
    _EAN_STRIP_RE,
    CATEGORIES,
    Deps,
    _escape_like,
    _fold,
    _parse_emails_field,
    _valid_date,
)
from .httpapi_security import (
    SKLAD_ACTION,
    SKLAD_DL_PATHS,
    SKLAD_DL_ROLE,
    SKLAD_DL_ZNALOSTI_API,
    SKLAD_PATHS,
    SKLAD_ROLE,
    SKLAD_ZNALOSTI_API,
    SKLAD_ZNALOSTI_PAGE,
    _role_kinds,
)
from .httpapi_templates import (
    ASK_DL_HTML,
    ASK_HTML,
    DASH_HTML,
    LOGIN_HTML,
    ZNALOSTI_HTML,
)
from .orders import dl_snapshot, memory, snapshot

log = logging.getLogger("email_extractor.httpapi")

# The Flask session secret + the /sklad/<key> derivation both live in `linkutil` (#139) —
# the order worker's background thread mints the SAME link with no Flask request at all,
# so the derivation must not be duplicated here.
_persistent_secret = linkutil.persistent_secret
sklad_key = linkutil.sklad_key
dl_key = linkutil.dl_key


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

    # #268 krok 5: what a split-out `register(app, deps)` route module may reach for —
    # never more. Built once here, passed to every split module unchanged.
    deps = Deps(cfg=cfg, db=_db, db_tx=_db_tx, data_dir=data_dir)

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

    # #268 krok 5: /files/<mid>/<idx>, /eml/<mid> + their _token_ok/_auth helpers —
    # moved verbatim into httpapi_files.py, registered here at the same spot they used
    # to sit at (route registration order is irrelevant to Flask; before_request hook
    # order, untouched by this move, is what actually matters — see the design comment).
    httpapi_files.register(app, deps)

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

    # #268 krok 7: the whole fix-queue feature (api_fix + api_fix_queue +
    # api_fix_resolve) — moved verbatim into httpapi_fixqueue.py and reunited into ONE
    # module (they used to be split across two regions ~850 lines apart in this file —
    # see the design comment on #268). Registered here, at api_fix's old position.
    httpapi_fixqueue.register(app, deps)

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

    # #268 krok 8: api_orders_spend / api_orders_digest / api_orders_dl_stats /
    # api_imap_failures — all four read-only aggregate/reporting endpoints, moved
    # verbatim into httpapi_reports.py, registered here at api_orders_spend's old
    # position (see the design comment on #268).
    httpapi_reports.register(app, deps)

    @app.get("/")
    def dashboard():
        # The address the operator is ON, never cfg.public_base_url — that one is the MACHINE
        # base (n8n fetches /files over the docker network) and is unopenable in a browser.
        base = request.host_url.rstrip("/")
        return (DASH_HTML.replace("__VERSION__", __version__)
                .replace("__SKLADLINK__", f"{base}/sklad/{key}")
                .replace("__DLSKLADLINK__", f"{base}/sklad-dl/{dl_link_key}"))

    return app


def start(cfg) -> None:
    app = create_app(cfg)
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=cfg.http_port, threaded=True),
        daemon=True,
    ).start()
