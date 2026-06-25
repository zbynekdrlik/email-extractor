"""Tests for the review HTTP API (app factory + auth gate; no DB needed).

The token check runs before any DB access, so the 403 paths are testable without
Postgres. /health and /version are intentionally open.
"""
from app.config import Config
from app.httpapi import create_app


def _client(token="secret"):
    cfg = Config(api_token=token, pg_dsn="postgresql://unused", data_dir="/tmp")
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


def test_review_page_served():
    r = _client().get("/review")
    assert r.status_code == 200
    body = r.data.lower()
    assert b"/review/list" in r.data
    assert b"kontrol" in body  # the page is the human-review UI


def test_auth_gate_blocks_data_without_token():
    c = _client(token="secret")
    assert c.get("/review/list").status_code == 403
    assert c.get("/review/detail?id=1").status_code == 403
    assert c.post("/review/correct", json={"id": 1, "category": "invoices"}).status_code == 403
    assert c.post("/review/confirm", json={"id": 1}).status_code == 403


def test_auth_gate_open_when_no_token_configured():
    # with no api_token set, endpoints are not token-gated (the auth check is a no-op)
    c = _client(token="")
    assert c.get("/health").status_code == 200


def test_correct_rejects_bad_category_after_auth():
    c = _client(token="secret")
    # valid token, invalid category -> 400 (validation runs, no DB write)
    r = c.post("/review/correct", headers={"X-Token": "secret"},
               json={"id": 1, "category": "not_a_category"})
    assert r.status_code == 400
