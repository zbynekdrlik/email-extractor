"""#342: the machine endpoint POST /api/codex/orders (X-Token auth + idempotent upsert)."""
import os
from datetime import UTC, datetime

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


# --- #467: POST /api/codex/cards — the CODEX stock-card list (full replace) -------------

_CARDS = {"source_as_of": "2026-09-29T12:23:34+00:00", "cards": [
    {"code": "9990000000017", "card_code": "27", "stredisko": 1, "sklad": 1,
     "name": "Rožok so slaninou a syrom 70g", "inactive": False,
     "changed_at": "2026-09-28T09:00:48+02:00"},
    {"code": "9990000000031", "card_code": "40", "stredisko": 1, "sklad": 100,
     "name": "Múka pšeničná T650", "inactive": False, "changed_at": None},
    {"code": "9990000000048", "card_code": "41", "stredisko": 1, "sklad": 100,
     "name": "Múka ražná T930", "inactive": True, "changed_at": None}]}


def test_cards_endpoint_needs_the_token(pg):
    r = _client().post("/api/codex/cards", json=_CARDS)
    assert r.status_code == 403
    assert pg.execute("SELECT count(*) FROM codex_stock_cards").fetchone()[0] == 0


def test_cards_endpoint_is_closed_without_a_configured_token(pg):
    r = _client(api_token="").post("/api/codex/cards", json=_CARDS, headers={"X-Token": ""})
    assert r.status_code == 403


def test_cards_endpoint_replaces_the_list_and_records_the_sync(pg):
    c = _client()
    r = c.post("/api/codex/cards", json=_CARDS, headers={"X-Token": "tok"})
    assert r.status_code == 200
    body = r.get_json()
    assert {k: body[k] for k in ("rows", "codes", "received")} == {
        "rows": 3, "codes": 3, "received": 3}
    # #478: the accepted push also runs the CODEX card sync and answers its result (its
    # behaviour is pinned in test_codex_sync.py)
    assert set(body) == {"rows", "codes", "received", "sync"} and "mode" in body["sync"]
    # a second push without the first card REPLACES the list (the code leaves it)
    second = {"source_as_of": _CARDS["source_as_of"], "cards": _CARDS["cards"][1:]}
    r = c.post("/api/codex/cards", json=second, headers={"X-Token": "tok"})
    assert r.status_code == 200
    codes = sorted(x[0] for x in pg.execute("SELECT code FROM codex_stock_cards").fetchall())
    assert codes == ["9990000000031", "9990000000048"]
    syncs = pg.execute("SELECT row_count, code_count, source_as_of FROM codex_card_syncs "
                       "ORDER BY id").fetchall()
    assert [s[:2] for s in syncs] == [(3, 3), (2, 2)]
    assert syncs[0][2] == datetime(2026, 9, 29, 12, 23, 34, tzinfo=UTC)   # as pushed


def test_cards_endpoint_refuses_a_bad_body_or_an_empty_list(pg):
    c = _client()
    h = {"X-Token": "tok"}
    assert c.post("/api/codex/cards", json={"nope": 1}, headers=h).status_code == 400
    assert c.post("/api/codex/cards", data="not json", headers=h).status_code == 400
    assert c.post("/api/codex/cards", json={"cards": []}, headers=h).status_code == 400
    assert pg.execute("SELECT count(*) FROM codex_card_syncs").fetchone()[0] == 0


def test_cards_endpoint_refuses_a_drastic_shrink_unless_forced(pg):
    c = _client()
    h = {"X-Token": "tok"}
    c.post("/api/codex/cards", json=_CARDS, headers=h)
    shrunk = {"cards": _CARDS["cards"][:1]}
    r = c.post("/api/codex/cards", json=shrunk, headers=h)
    assert r.status_code == 409
    assert pg.execute("SELECT count(*) FROM codex_stock_cards").fetchone()[0] == 3
    assert c.post("/api/codex/cards?force=1", json=shrunk, headers=h).status_code == 200
    assert pg.execute("SELECT count(*) FROM codex_stock_cards").fetchone()[0] == 1


def test_cards_endpoint_takes_the_token_from_the_header_only(pg):
    """Review 🔵: a full-table replace must not accept the token from the URL (proxies and
    access logs keep query strings) — X-Token header only."""
    r = _client().post("/api/codex/cards?token=tok", json=_CARDS)
    assert r.status_code == 403
    assert pg.execute("SELECT count(*) FROM codex_stock_cards").fetchone()[0] == 0


def test_cards_endpoint_refuses_an_oversized_body(pg):
    big = {"cards": [dict(_CARDS["cards"][1], name="x" * 1000, card_code=str(i))
                     for i in range(20000)]}
    r = _client().post("/api/codex/cards", json=big, headers={"X-Token": "tok"})
    assert r.status_code == 413
    assert pg.execute("SELECT count(*) FROM codex_stock_cards").fetchone()[0] == 0
