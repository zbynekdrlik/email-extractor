"""App factory: auth gate + open endpoints + dashboard page (no DB needed).

The gate runs before any DB access, so these paths are testable without Postgres.
/health and /version are intentionally open. Secure-by-default: the dashboard is
session-only (login needs a configured dash_password); the file APIs are
session-or-token; nothing is open when unconfigured.
"""
import logging
import os

from app.config import Config
from app.httpapi import create_app


def _client(token="secret", dash=""):
    cfg = Config(api_token=token, dash_password=dash, secret_key="t",
                 pg_dsn="postgresql://unused", data_dir="/tmp")
    app = create_app(cfg)
    app.testing = True
    return app.test_client()


def test_health_open_and_ok():
    r = _client().get("/health")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_version_open():
    r = _client().get("/version")
    assert r.status_code == 200
    assert b"." in r.data


def test_favicon_no_404():
    # browsers auto-request /favicon.ico; serve 204 so the dashboard console stays clean
    assert _client().get("/favicon.ico").status_code == 204


def test_dashboard_closed_by_default():
    # nothing configured -> dashboard redirects to login (which cannot succeed)
    assert _client(token="", dash="").get("/").status_code == 302
    # token set but no session -> still redirected (token does NOT open the dashboard)
    assert _client(token="secret").get("/").status_code == 302


def test_api_requires_session_401():
    # token does NOT authorize the data API — it is session-only
    assert _client(token="secret").get("/api/messages?token=secret").status_code == 401
    assert _client(token="secret").get("/api/messages").status_code == 401


def test_orders_digest_requires_a_session():
    """#196: same admin-dashboard security boundary as /api/orders/spend — aggregate
    stats only, but not sklad-visible (see test_the_warehouse_link_opens_ONLY_the_
    questions_surface below, which pins that it stays out of SKLAD_PATHS)."""
    assert _client(token="secret").get("/api/orders/digest").status_code == 401


def test_dashboard_served_after_login():
    c = _client(token="secret", dash="pw")
    assert c.post("/login", data={"password": "pw"}).status_code == 302
    r = c.get("/")
    assert r.status_code == 200
    assert b'data-testid="version"' in r.data    # version label present (mandatory rule)
    assert b"/api/messages" in r.data             # the SPA talks to the data API


def test_files_and_eml_require_token_or_session():
    c = _client(token="secret")
    assert c.get("/files/x/0").status_code == 403
    assert c.get("/eml/x").status_code == 403


def test_files_ok_with_token_but_missing_is_404():
    # authorized via token -> the route runs and 404s on the missing file
    assert _client(token="secret").get("/files/nope/0?token=secret").status_code == 404


def test_login_disabled_without_dash_password():
    # dash_password unset -> login can never succeed (the dashboard stays closed)
    assert _client(token="secret", dash="").post(
        "/login", data={"password": "anything"}).status_code == 401


# ---- #28: the dashboard HTTP layer must log requests and error paths ----

def _app_client():
    from app.config import Config
    from app.httpapi import create_app
    cfg = Config(pg_dsn=os.environ.get("PG_TEST_DSN"), data_dir="/tmp", api_token="tok",
                 dash_password="secret", secret_key="s")
    return create_app(cfg)


def test_every_request_is_access_logged(pg, caplog):
    caplog.set_level(logging.INFO, logger="email_extractor.httpapi")
    c = _app_client().test_client()
    c.get("/health")
    lines = [r.message % r.args if r.args else r.message for r in caplog.records]
    assert any("GET /health -> 200" in ln for ln in lines), lines
    assert any("ms)" in ln for ln in lines), "the duration must be logged"


def test_the_access_log_never_contains_the_token(pg, caplog):
    caplog.set_level(logging.INFO, logger="email_extractor.httpapi")
    c = _app_client().test_client()
    c.get("/eml/whatever?token=tok")
    text = "\n".join((r.message % r.args if r.args else r.message) for r in caplog.records)
    assert "token" not in text, text


def test_a_failing_endpoint_is_logged_and_returns_a_clean_500(pg, caplog, monkeypatch):
    caplog.set_level(logging.ERROR, logger="email_extractor.httpapi")
    app = _app_client()
    c = app.test_client()
    c.post("/login", data={"password": "secret"})

    from app import httpapi

    def broken(*a, **kw):
        raise RuntimeError("relation does not exist")

    monkeypatch.setattr(httpapi.db, "list_uid_failures", broken)
    r = c.get("/api/imap-failures")
    assert r.status_code == 500
    assert "chyba" in r.get_json()["error"].lower(), r.get_json()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "/api/imap-failures failed" in text and "relation does not exist" in text


def test_client_errors_are_warned_not_swallowed(pg, caplog):
    caplog.set_level(logging.INFO, logger="email_extractor.httpapi")
    c = _app_client().test_client()
    c.get("/api/messages")            # 401, no session
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("-> 401" in (r.message % r.args if r.args else r.message) for r in warns)


# --- the warehouse link: answering must need no password (user's ask, 2026-07-31) ----

def _sklad_client(secret="t", base=""):
    cfg = Config(api_token="secret", dash_password="pw", secret_key=secret,
                 pg_dsn="postgresql://unused", data_dir="/tmp", public_base_url=base)
    app = create_app(cfg)
    app.testing = True
    return app, app.test_client()


def test_the_signed_warehouse_link_opens_the_questions_page_with_no_password():
    from app import httpapi
    _, c = _sklad_client()
    # #442: the signed link lands on the unified nástenka (both keys valid). #449 lane 8:
    # the old /otazky board is RETIRED — it now 302s to the board's orders questions tab.
    r0 = c.get("/sklad/" + httpapi.sklad_key("t"))
    assert r0.status_code == 302 and "/nastenka" in r0.headers["Location"]
    r = c.get("/otazky")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/nastenka/otazky-objednavky"), r.headers["Location"]


def test_a_wrong_warehouse_link_is_refused():
    _, c = _sklad_client()
    assert c.get("/sklad/" + "0" * 32).status_code == 403
    # #449 lane 8: /otazky is now an open redirect to the board for everyone (the board
    # itself gates the target tab) — nothing about the mail archive is granted here.
    r = c.get("/otazky")
    assert r.status_code == 302 and r.headers["Location"].endswith("/nastenka/otazky-objednavky")


def test_the_key_differs_per_install():
    from app import httpapi
    assert httpapi.sklad_key("t") != httpapi.sklad_key("other")
    _, c = _sklad_client(secret="other")
    assert c.get("/sklad/" + httpapi.sklad_key("t")).status_code == 403


def test_the_warehouse_link_opens_ONLY_the_questions_surface():
    """It is an unauthenticated link: it must not become a way into the mail archive."""
    from app import httpapi
    _, c = _sklad_client()
    c.get("/sklad/" + httpapi.sklad_key("t"))
    assert c.get("/api/messages").status_code == 401
    assert c.get("/api/orders/spend").status_code == 401
    assert c.get("/api/orders/digest").status_code == 401
    assert c.get("/api/fix-queue").status_code == 401
    assert c.get("/eml/e1").status_code == 403
    r = c.get("/")
    # #449 lane 8: the sklad role is bounced from the admin dashboard to the board.
    assert r.status_code == 302 and "/nastenka" in r.headers["Location"]


def test_the_warehouse_link_can_also_see_held_orders():
    """#93 review finding on PR #116: /api/orders/held was missing from SKLAD_PATHS, so
    the sklad role got a silent 401 and the /otazky held-orders panel never rendered for
    the warehouse users it was built for. It must at least reach the route (a DB error
    against the fake DSN here is fine — the point is the auth gate, not the query)."""
    from app import httpapi
    _, c = _sklad_client()
    c.get("/sklad/" + httpapi.sklad_key("t"))
    assert c.get("/api/orders/held").status_code != 401


def test_a_login_is_remembered_so_nobody_retypes_the_password():
    app, c = _sklad_client()
    assert app.permanent_session_lifetime.days >= 365
    c.post("/login", data={"password": "pw"})
    assert c.get("/api/messages").status_code != 401   # the session carries (DB error is fine)


# --- #231: a SECOND, independent unauthenticated link for the DL-only nástenka ----------

def test_the_signed_dl_warehouse_link_opens_the_dl_questions_page_with_no_password():
    from app import httpapi
    _, c = _sklad_client()
    # #442: the DL link also lands on the unified nástenka (DL key must NOT lose access).
    # #449 lane 8: the old /otazky-dl board is RETIRED — it now 302s to the board's
    # „Otázky sklad" (DL questions) tab.
    r0 = c.get("/sklad-dl/" + httpapi.dl_key("t"))
    assert r0.status_code == 302 and "/nastenka" in r0.headers["Location"]
    r = c.get("/otazky-dl")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/nastenka/otazky-sklad"), r.headers["Location"]


def test_a_wrong_dl_warehouse_link_is_refused():
    _, c = _sklad_client()
    assert c.get("/sklad-dl/" + "0" * 32).status_code == 403
    # #449 lane 8: /otazky-dl is now an open redirect to the board for everyone.
    r = c.get("/otazky-dl")
    assert r.status_code == 302 and r.headers["Location"].endswith("/nastenka/otazky-sklad")


def test_the_orders_link_does_not_open_the_dl_key_and_vice_versa():
    """The two links are genuinely independent secrets (#231) — the orders sklad_key must
    not open the DL route, and dl_key must not open the orders route."""
    from app import httpapi
    _, c = _sklad_client()
    assert c.get("/sklad-dl/" + httpapi.sklad_key("t")).status_code == 403
    assert c.get("/sklad/" + httpapi.dl_key("t")).status_code == 403


def test_the_dl_warehouse_link_opens_ONLY_the_dl_questions_surface():
    """Same unauthenticated-link boundary discipline as the orders link — must never
    reach the mail archive, and must never redirect to the ORDERS board (#231's point)."""
    from app import httpapi
    _, c = _sklad_client()
    c.get("/sklad-dl/" + httpapi.dl_key("t"))
    assert c.get("/api/messages").status_code == 401
    assert c.get("/api/orders/spend").status_code == 401
    assert c.get("/api/orders/digest").status_code == 401
    assert c.get("/api/orders/held").status_code == 401, \
        "held orders are an AI-orders concept — outside the DL role's own path allowlist"
    assert c.get("/eml/e1").status_code == 403
    r = c.get("/")
    # #449 lane 8: the DL sklad role is bounced from the admin dashboard to the board.
    assert r.status_code == 302 and "/nastenka" in r.headers["Location"]


def test_the_dl_role_can_reach_dl_stats_but_the_orders_role_cannot():
    from app import httpapi
    _, dl_c = _sklad_client()
    dl_c.get("/sklad-dl/" + httpapi.dl_key("t"))
    assert dl_c.get("/api/orders/dl/stats").status_code != 401

    _, orders_c = _sklad_client()
    orders_c.get("/sklad/" + httpapi.sklad_key("t"))
    assert orders_c.get("/api/orders/dl/stats").status_code == 401


def test_the_dashboard_shows_the_warehouse_link_to_copy():
    """Built from the address the OPERATOR is actually on — never from public_base_url.

    That option is the MACHINE base (n8n fetches /files over the docker network), so on the
    live box it is "http://e0ac7775-email-extractor:8099": a link no browser can open. Found
    by verifying 0.9.9 on the live dashboard.
    """
    from app import httpapi
    _, c = _sklad_client(base="http://e0ac7775-email-extractor:8099")
    host = "http://46.224.130.35:8099"           # the operator's own address
    c.post("/login", data={"password": "pw"}, base_url=host)
    body = c.get("/", base_url=host).data.decode()
    assert "http://46.224.130.35:8099/sklad/" + httpapi.sklad_key("t") in body
    assert "e0ac7775-email-extractor:8099/sklad/" not in body
    # #231: the DL-only nástenka link is shown alongside it, same operator-host rule
    assert "http://46.224.130.35:8099/sklad-dl/" + httpapi.dl_key("t") in body
    assert "e0ac7775-email-extractor:8099/sklad-dl/" not in body


# --- lane 8 (#449): the old warehouse pages are RETIRED -> they 302 to the board ------
# /otazky, /otazky-dl, /znalosti, /znalosti/<ean> used to render ASK_HTML/ASK_DL_HTML/
# ZNALOSTI_HTML; the unified nastenka (epic #441, lane 1-7) now owns every one of those
# surfaces, so the old routes stay registered but return a redirect to the matching tab,
# for EVERY role (the signed-key cookies stay valid). See the design comment on #449.

def _role_client(secret="t", role=None):
    """A test client, optionally pre-seeded with a warehouse role cookie (no signed-key
    round-trip needed - we set the session directly)."""
    cfg = Config(api_token="secret", dash_password="pw", secret_key=secret,
                 pg_dsn="postgresql://unused", data_dir="/tmp")
    app = create_app(cfg)
    app.testing = True
    c = app.test_client()
    if role:
        with c.session_transaction() as s:
            s["role"] = role
    return app, c


def test_the_old_otazky_pages_redirect_to_the_board_for_every_role():
    from app.httpapi_security import SKLAD_DL_ROLE, SKLAD_ROLE
    for role in (None, SKLAD_ROLE, SKLAD_DL_ROLE):
        _, c = _role_client(role=role)
        r = c.get("/otazky")
        assert r.status_code == 302, role
        assert r.headers["Location"].endswith("/nastenka/otazky-objednavky"), r.headers["Location"]
        r = c.get("/otazky-dl")
        assert r.status_code == 302, role
        assert r.headers["Location"].endswith("/nastenka/otazky-sklad"), r.headers["Location"]
    _, c = _role_client()
    c.post("/login", data={"password": "pw"})
    assert c.get("/otazky").status_code == 302
    assert c.get("/otazky-dl").status_code == 302


def test_the_old_znalosti_pages_redirect_to_the_board_for_every_role():
    from app.httpapi_security import SKLAD_DL_ROLE, SKLAD_ROLE
    for role in (None, SKLAD_ROLE, SKLAD_DL_ROLE):
        _, c = _role_client(role=role)
        r = c.get("/znalosti")
        assert r.status_code == 302, role
        assert r.headers["Location"].endswith("/nastenka/produkty-objednavky"), r.headers["Location"]
        r = c.get("/znalosti/2000000000777")
        assert r.status_code == 302, role
        loc = r.headers["Location"]
        assert "/nastenka/zakaznici" in loc and "q=2000000000777" in loc, loc


def test_a_retired_page_ultimately_lands_on_login_for_an_anon_session():
    """No-cookie -> the old page redirects to the board tab, and the board gate then
    sends an unauthenticated visitor to /login (the whole redirect chain)."""
    _, c = _role_client(role=None)
    r = c.get("/otazky", follow_redirects=True)
    assert r.request.path == "/login", r.request.path
    r = c.get("/znalosti", follow_redirects=True)
    assert r.request.path == "/login", r.request.path
