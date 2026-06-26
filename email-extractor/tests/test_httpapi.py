"""App factory: auth gate + open endpoints + dashboard page (no DB needed).

The gate / token check runs before any DB access, so these paths are testable
without Postgres. /health and /version are intentionally open.
"""
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


def test_dashboard_page_served_when_no_auth_configured():
    r = _client(token="").get("/")
    assert r.status_code == 200
    assert b'data-testid="version"' in r.data   # version label present (mandatory rule)
    assert b"/api/messages" in r.data            # the SPA talks to the data API


def test_dashboard_redirects_without_auth():
    # token configured, no session, no token on the request -> not authorized
    assert _client(token="secret").get("/").status_code == 302


def test_files_and_eml_require_token():
    c = _client(token="secret")
    assert c.get("/files/x/0").status_code == 403
    assert c.get("/eml/x").status_code == 403


def test_files_ok_with_token_but_missing_is_404():
    # authorized via token -> the route runs and 404s on the missing file
    assert _client(token="secret").get("/files/nope/0?token=secret").status_code == 404


def test_api_requires_auth_401():
    assert _client(token="secret").get("/api/messages").status_code == 401


def test_login_disabled_without_dash_password():
    # dash_password unset -> login can never succeed (the dashboard stays closed)
    assert _client(token="secret", dash="").post(
        "/login", data={"password": "anything"}).status_code == 401


def test_login_succeeds_with_dash_password():
    c = _client(token="secret", dash="pw")
    assert c.post("/login", data={"password": "pw"}).status_code == 302
    assert c.get("/").status_code == 200          # now authorized via session
