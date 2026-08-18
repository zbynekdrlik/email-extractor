"""#342: the machine endpoint POST /api/codex/orders (X-Token auth + idempotent upsert)."""
import os

from app.config import Config
from app.httpapi import create_app

EAN = "2000000000001"


def _client(api_token="tok"):
    cfg = Config(pg_dsn=os.environ.get("PG_TEST_DSN"), data_dir="/tmp",
                 api_token=api_token, dash_password="pw", secret_key="t")
    return create_app(cfg).test_client()


_ORDERS = {"orders": [
    {"order_number": 900, "customer_ean": EAN, "customer_name": "A",
     "issue_date": "2026-08-15", "delivery_date": "2026-08-16", "line_count": 2}]}


def test_no_token_is_rejected(pg):
    r = _client().post("/api/codex/orders", json=_ORDERS)
    assert r.status_code == 403
    assert pg.execute("SELECT count(*) FROM codex_orders").fetchone()[0] == 0


def test_wrong_token_is_rejected(pg):
    r = _client().post("/api/codex/orders", json=_ORDERS, headers={"X-Token": "nope"})
    assert r.status_code == 403
    assert pg.execute("SELECT count(*) FROM codex_orders").fetchone()[0] == 0


def test_unconfigured_token_closes_the_endpoint(pg):
    """An add-on with no api_token set rejects even a blank token — never open-by-default."""
    r = _client(api_token="").post("/api/codex/orders", json=_ORDERS,
                                   headers={"X-Token": ""})
    assert r.status_code == 403


def test_correct_token_upserts(pg):
    r = _client().post("/api/codex/orders", json=_ORDERS, headers={"X-Token": "tok"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["upserted"] == 1 and body["received"] == 1
    row = pg.execute(
        "SELECT customer_ean, issue_date::text, line_count FROM codex_orders "
        "WHERE order_number = 900").fetchone()
    assert row == (EAN, "2026-08-15", 2)


def test_correct_token_is_idempotent_over_http(pg):
    c = _client()
    c.post("/api/codex/orders", json=_ORDERS, headers={"X-Token": "tok"})
    c.post("/api/codex/orders", json=_ORDERS, headers={"X-Token": "tok"})
    assert pg.execute("SELECT count(*) FROM codex_orders").fetchone()[0] == 1


def test_token_via_query_param_also_works(pg):
    r = _client().post("/api/codex/orders?token=tok", json=_ORDERS)
    assert r.status_code == 200


def test_bad_body_is_400(pg):
    c = _client()
    assert c.post("/api/codex/orders", json={"nope": 1},
                  headers={"X-Token": "tok"}).status_code == 400
    assert c.post("/api/codex/orders", data="not json",
                  headers={"X-Token": "tok"}).status_code == 400


def test_rows_missing_identity_are_dropped_not_upserted(pg):
    r = _client().post("/api/codex/orders", headers={"X-Token": "tok"}, json={"orders": [
        {"order_number": 901, "customer_ean": EAN, "issue_date": "2026-08-15"},
        {"order_number": None, "customer_ean": EAN},
        {"customer_ean": EAN},
        {"order_number": 902, "customer_ean": ""}]})
    assert r.status_code == 200
    body = r.get_json()
    assert body["upserted"] == 1 and body["received"] == 4
    assert pg.execute("SELECT count(*) FROM codex_orders").fetchone()[0] == 1
