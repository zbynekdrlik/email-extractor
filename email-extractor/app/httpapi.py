"""Internal HTTP API + live dashboard — the entry point of a 9-module split (#268).

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

Architecture (#268, 2693 lines -> 290 in this file across an 11-step chain, see the
issue's own comment thread for the full history):

This module is the ONLY entry point (`create_app(cfg)`) and stays that way on purpose —
every other module below exposes a plain `register(app, deps)` function, called from
here, never a Flask Blueprint (this app has one `create_app()` and no other module uses
one — introducing Blueprints for a subset would be a second, inconsistent registration
mechanism for zero behavioural gain). What actually lives HERE, and why it never moved:
- `create_app`/`start` — the app factory + the process entry point.
- `_db`/`_db_tx` — the two connection-shape closures every split module reaches through
  `Deps` (`db`/`db_tx`, built once as `deps = Deps(cfg=cfg, db=_db, db_tx=_db_tx,
  data_dir=data_dir)` and passed unchanged to every `register()` call). `_db()` is a
  plain autocommit connection; `_db_tx()` is a real transaction (commits at exit, rolls
  back on error). **The two-connection rule**: any route that pairs a DB write with an
  external side effect that already happened (an ORION upload, an Odoo post) must run
  the CLAIM and the SIDE EFFECT on separate `_db()` autocommit connections, never inside
  one `_db_tx()` — a rollback after a real external action would revive a claim on
  something that cannot be undone. See `httpapi_orders_questions.py` (all four two-
  connection pairs behind `/api/orders/question/<id>/answer` and friends) for where this
  actually matters; `.claude/rules/orders-corpus.md`'s own "#93" entry has the original
  incident this rule comes from.
  After krok 10, `httpapi.py` itself has ZERO remaining `_db()`/`_db_tx()` CALLS — only
  the two `def`s survive, to build `Deps`.
- `_stamp`/`_access_log`/`_on_error`/`_gate` — request logging + the security boundary
  (see below). `before_request` REGISTRATION ORDER is load-bearing: `_stamp` before
  `_gate`, so a request `_gate` rejects still gets `request.environ["_t0"]` and
  `_access_log` never logs a bogus `-1 ms`. No split step has ever touched this order.
- The auth surface (`login_page`/`login_submit`/`sklad_link`/`dl_sklad_link_route`/
  `questions_page`/`dl_questions_page`/`logout`/`favicon`/`health`/`version`) — small,
  tightly bound to `create_app`'s own `app.secret_key`/`key`/`dl_link_key` scope, so it
  stayed here rather than in its own module (would just be another `Deps`-shaped object
  for one screenful of code).
- `/` (`dashboard()`) — needs the same `key`/`dl_link_key` closure variables.

The role/security boundary (#233/#235) is enforced in exactly TWO places, both meant to
stay auditable on their own:
- `_gate()` (this file, `before_request`) — matches `request.path` against the
  `SKLAD_ROLE`/`SKLAD_PATHS`/`SKLAD_ACTION`/`SKLAD_ZNALOSTI_PAGE`/`SKLAD_ZNALOSTI_API`
  and `SKLAD_DL_ROLE`/`SKLAD_DL_PATHS`/`SKLAD_DL_ZNALOSTI_API` constants — all DEFINED in
  `httpapi_security.py`, imported back here, never duplicated.
- `_role_kinds()` (`httpapi_security.py`) — the second, independent layer: filters WHICH
  question `kind`s a session may answer even when the path itself is allowed (stops a DL
  session from guessing an `item`-kind question id). Called from
  `httpapi_orders_questions.py`'s four public routes.

The nine split modules (all `register(app, deps)`, all leaf modules — none of them
imports `httpapi.py`, so there is no circular-import risk):
- `httpapi_common.py`    — `Deps`, string/date helpers with no Flask/DB dependency.
- `httpapi_security.py`  — the role/path constants + `_role_kinds()` (see above).
- `httpapi_templates.py` — `LOGIN_HTML`/`DASH_HTML`/`ASK_HTML`/`ASK_DL_HTML`/
                            `ZNALOSTI_HTML` (+ the internal `_ASK_HTML_TEMPLATE` they're
                            built from) — 1230 lines of HTML/CSS/JS as Python strings,
                            deliberately kept as ONE module (krok 4: splitting one HTML
                            document from the sibling constants it shares an origin with
                            gives nothing).
- `httpapi_files.py`          — `/files`, `/eml` (token-guarded originals for n8n).
- `httpapi_dashboard_data.py` — the dashboard's list/detail + operator actions.
- `httpapi_fixqueue.py`       — the fix queue (`api_fix`/`api_fix_queue`/
                                 `api_fix_resolve`, reunited here after living in two
                                 regions ~850 lines apart in the pre-split file).
- `httpapi_orders_questions.py` — the AI-orders question board; the RISKIEST split step
                                   (krok 10) because it carries all four two-connection
                                   pairs above, moved as ONE indivisible block on purpose.
- `httpapi_znalosti.py`   — the `/znalosti` knowledge-DB page + its 12 CRUD routes.
- `httpapi_reports.py`    — read-only aggregate/reporting endpoints (spend, digest,
                             dl-stats, imap-failures).

`tests/test_httpapi_characterization.py` (krok 1) pins the pre-split public contract —
the full route table, a checksum of the five rendered HTML constants, and a real
`/api/orders/digest` happy path — and has proven zero drift across all 11 steps.
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
from werkzeug.exceptions import HTTPException

# `db` is no longer CALLED from this module (krok 6 moved its only caller, `_busy`,
# into httpapi_dashboard_data.py) — but it stays imported below (noqa'd) so
# `httpapi.db` keeps resolving for two existing tests that monkeypatch it there
# (test_httpapi.py::test_a_failing_endpoint_is_logged_and_returns_a_clean_500,
# test_api.py::test_fix_request_and_its_event_commit_together). It is the SAME
# module object httpapi_reports.py/httpapi_fixqueue.py import and actually call, so
# the patch still reaches the real code either way — see krok 6's design comment.
from . import (
    __version__,
    db,  # noqa: F401
    httpapi_dashboard_data,
    httpapi_files,
    httpapi_fixqueue,
    httpapi_orders_questions,
    httpapi_reports,
    httpapi_znalosti,
    linkutil,
)
from .httpapi_common import Deps
from .httpapi_security import (
    SKLAD_ACTION,
    SKLAD_DL_PATHS,
    SKLAD_DL_ROLE,
    SKLAD_DL_ZNALOSTI_API,
    SKLAD_PATHS,
    SKLAD_ROLE,
    SKLAD_ZNALOSTI_API,
    SKLAD_ZNALOSTI_PAGE,
)
from .httpapi_templates import (
    ASK_DL_HTML,
    ASK_HTML,
    DASH_HTML,
    LOGIN_HTML,
    ZNALOSTI_HTML,  # noqa: F401 — no longer rendered here (krok 9 moved znalosti_page
    # into httpapi_znalosti.py), kept re-exported because
    # tests/test_httpapi_characterization.py imports it straight off `app.httpapi`
    # (krok 1's checksum test) — same shape as `db` above.
)

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
        # The role/path constants matched below (SKLAD_ROLE/SKLAD_PATHS/SKLAD_ACTION/
        # SKLAD_ZNALOSTI_PAGE/SKLAD_ZNALOSTI_API and their SKLAD_DL_* siblings) are
        # DEFINED in httpapi_security.py (#268 krok 2-3) — this function is the ONE
        # place that enforces the role boundary they describe; see this module's own
        # docstring for the full security-boundary map.
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

    # #268 krok 6: the dashboard's own main data API (api_messages/api_message)
    # plus the operator's manual actions (api_reclassify/api_reprocess) — moved
    # verbatim into httpapi_dashboard_data.py, including their shared `_busy`
    # helper. Registered here, at api_messages's old position (see the design
    # comment on #268).
    httpapi_dashboard_data.register(app, deps)

    # #268 krok 7: the whole fix-queue feature (api_fix + api_fix_queue +
    # api_fix_resolve) — moved verbatim into httpapi_fixqueue.py and reunited into ONE
    # module (they used to be split across two regions ~850 lines apart in this file —
    # see the design comment on #268). Registered here, at api_fix's old position.
    httpapi_fixqueue.register(app, deps)

    # #268 krok 10: the AI-orders question board — api_orders_questions,
    # _api_orders_answer_new_customer/_customer/_new_dl_supplier/_new_dl_item/
    # _generic, api_orders_answer, api_orders_held, api_orders_taught, api_orders_undo.
    # The RISKIEST step (see the design comment on #268): this block carries ALL FOUR
    # two-connection pairs against duplicate uploads and the _role_kinds id-guessing
    # boundary, moved verbatim as ONE indivisible unit into
    # httpapi_orders_questions.py, registered here at api_orders_questions's old
    # position.
    httpapi_orders_questions.register(app, deps)

    # #268 krok 9: znalosti_page + all 12 knowledge-DB CRUD routes (products/
    # clients/global/customer-alias/dl-products/dl-suppliers), moved verbatim into
    # httpapi_znalosti.py, registered here at znalosti_page's old position (see the
    # design comment on #268).
    httpapi_znalosti.register(app, deps)

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
