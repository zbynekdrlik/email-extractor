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
