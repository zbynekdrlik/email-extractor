"""#426: adding a genuinely NEW catalog card straight from the "ktorý výrobok to je?"
(kind='item') question card on the warehouse orders board — the last "new thing right on
the question" action that was missing.

Root cause (see the design comment on #426): `api_orders_answer` handles `kind=='item'`
by falling through to a tail that requires an EXISTING card (`if not gtin: 400 "chýba
karta"`, `httpapi_orders_questions.py`). The "genuinely new" branch already exists for
`new_customer` (#234) and the two DL kinds `new_supplier`/`new_item` (#235) — item never
got one, so the warehouse had to leave for /znalosti when a card was missing (owner request
2026-09-14). This file pins the fix: a `new_product` body on the existing answer route
(mirrors #234/#235 exactly) — the card is created (catalog_overrides, like /znalosti), the
question is answered by `sklad-new-card`, and the held order is released, all one click.

Flask test client + real Postgres, same pattern as test_httpapi_new_dl.py /
test_httpapi_new_customer.py.
"""
import os

from psycopg.types.json import Json

from app.config import Config
from app.httpapi import create_app, sklad_key
from app.orders import snapshot, teach

PG_DSN = os.environ.get("PG_TEST_DSN")

# TOR is a pre-existing base-snapshot card → the duplicate-gtin test collides with it.
CATALOG_CSV = "GTIN,Názov,doplnok\nTOR,Torta čokoládová,\n"
CUSTOMER_CSV = (
    "Názov organizácie,EAN kód EDI,Obec,Ulica,E-mail\n"
    "Pekáreň Testovacia,2000000000864,Martin,Košútka 1,sklad@pekaren.sk\n"
)


def _cfg():
    return Config(pg_dsn=PG_DSN, data_dir="/tmp", api_token="tok", dash_password="secret",
                 secret_key="test-secret", odoo_url="", odoo_api_key="",
                 orders_channel_id=0, orders_shadow=False)


def _client():
    app = create_app(_cfg())
    app.testing = True
    return app.test_client()


def _sklad_client():
    """The unauthenticated warehouse orders link — role 'sklad', ORDERS_KINDS only."""
    c = _client()
    c.get("/sklad/" + sklad_key("test-secret"))
    return c


def _login(c):
    c.post("/login", data={"password": "secret"})


def _seed_item_held_order(pg, message_id="m426"):
    """One held order waiting on ONE item question whose line is unmatched — exactly what
    `pipeline._run` leaves when a wording has no catalog card yet. Answering with a
    brand-new card must create it, teach it, and ship the held order (one click)."""
    snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    pg.execute("INSERT INTO messages (message_id, category) VALUES (%s, 'ai_orders')",
              (message_id,))
    qid = pg.execute(
        """INSERT INTO order_questions (message_id, customer_ean, customer_name, wording,
                                        item_key, quantity, unit, candidates, delivery_date,
                                        reason)
           VALUES (%s, '2000000000864', 'Pekáreň Testovacia', 'chlieb', 'chlieb', 5, 'ks',
                   %s, '20.09.2026', 'test') RETURNING id""",
        (message_id, Json([]))).fetchone()[0]
    pg.execute(
        """INSERT INTO held_orders (message_id, customer_ean, customer_name, delivery_date,
                                    order_number, question_ids, order_json, extracted_json,
                                    decisions_json)
           VALUES (%s, '2000000000864', 'Pekáreň Testovacia', '20.09.2026', '', %s, %s, %s,
                   %s)""",
        (message_id, [qid], Json({"deliveryDate": "20.09.2026", "orderNumber": ""}),
         Json({"isChangeRequest": False, "unverified": [], "notes": ""}),
         Json([{"item_name": "chlieb", "gtin": None, "card": "", "confidence": 0.1,
                "rule": "unmatched", "note": "", "review": False, "trace": {},
                "quantity": 5, "unit": "ks"}])))
    return qid


def _answered_row(pg, qid):
    return pg.execute(
        "SELECT status, answer_gtin, answered_by FROM order_questions WHERE id=%s",
        (qid,)).fetchone()


# --- the fix: new_product answer path on an item question -----------------------------

def test_new_product_on_an_item_question_creates_the_card_answers_and_releases(
        pg, monkeypatch):
    """Proves the whole #426 flow: a brand-new card (číslo položky from CODEX, never in the
    catalog) typed straight onto the item question creates the override card, answers the
    question by `sklad-new-card`, and ships the held order — one document, one click."""
    qid = _seed_item_held_order(pg)
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    c = _client()
    _login(c)
    r = c.post(f"/api/orders/question/{qid}/answer", json={
        "new_product": {"gtin": "3650", "name": "Chlieb kváskový pražená cibuľka"},
        "quantity": 5, "unit_price": "1,20"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    # the card is in the effective catalog + a real override row landed (like /znalosti)
    assert any(x["gtin"] == "3650" for x in snapshot.catalog_for_management(pg))
    assert pg.execute(
        "SELECT name FROM catalog_overrides WHERE gtin='3650'"
    ).fetchone() == ("Chlieb kváskový pražená cibuľka",)
    # question answered, attributed to the new-card path
    assert _answered_row(pg, qid) == ("answered", "3650", "sklad-new-card")
    # held order released, exactly one EDI shipped
    assert body["released"] and body["released"][0]["status"] == "ok"
    assert pg.execute(
        "SELECT status FROM held_orders WHERE message_id='m426'").fetchone() == ("released",)
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 1


def test_new_product_with_a_gtin_that_already_has_a_live_card_is_refused(pg, monkeypatch):
    """A číslo položky that already belongs to a live card → 409 + `existing`, so the client
    can offer „Použiť existujúcu kartu" instead of silently creating a duplicate override.
    The question stays open and nothing ships."""
    qid = _seed_item_held_order(pg)
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    c = _client()
    _login(c)
    r = c.post(f"/api/orders/question/{qid}/answer", json={
        "new_product": {"gtin": "TOR", "name": "Iný názov"}, "quantity": 5})
    assert r.status_code == 409
    body = r.get_json()
    assert body["existing"]["gtin"] == "TOR"
    assert "Torta" in body["existing"]["name"]
    assert _answered_row(pg, qid)[0] == "open"
    assert pg.execute("SELECT count(*) FROM catalog_overrides").fetchone()[0] == 0
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 0


def test_new_product_without_a_gtin_is_refused(pg):
    qid = _seed_item_held_order(pg)
    c = _client()
    _login(c)
    r = c.post(f"/api/orders/question/{qid}/answer",
              json={"new_product": {"name": "Bez čísla položky"}})
    assert r.status_code == 400
    assert pg.execute("SELECT count(*) FROM catalog_overrides").fetchone()[0] == 0
    assert _answered_row(pg, qid)[0] == "open"


def test_new_product_with_a_non_numeric_gtin_is_refused(pg):
    qid = _seed_item_held_order(pg)
    c = _client()
    _login(c)
    r = c.post(f"/api/orders/question/{qid}/answer",
              json={"new_product": {"gtin": "36X0", "name": "X"}})
    assert r.status_code == 400
    assert pg.execute("SELECT count(*) FROM catalog_overrides").fetchone()[0] == 0
    assert _answered_row(pg, qid)[0] == "open"


def test_new_product_from_the_unauthenticated_sklad_link_is_allowed(pg, monkeypatch):
    """The warehouse's own /otazky link (role 'sklad', ORDERS_KINDS) may create+answer an
    item question — SKLAD_ACTION already covers the answer endpoint, no security change."""
    qid = _seed_item_held_order(pg)
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    c = _sklad_client()
    r = c.post(f"/api/orders/question/{qid}/answer", json={
        "new_product": {"gtin": "3650", "name": "Chlieb kváskový"}, "quantity": 5})
    assert r.status_code == 200
    assert any(x["gtin"] == "3650" for x in snapshot.catalog_for_management(pg))
    assert _answered_row(pg, qid) == ("answered", "3650", "sklad-new-card")


def test_new_product_with_a_doplnok_stores_the_alias(pg, monkeypatch):
    """The optional doplnok is stored as the card's alias (a real match.py alias_exact
    signal), exactly like /api/znalosti/products does."""
    qid = _seed_item_held_order(pg)
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    c = _client()
    _login(c)
    r = c.post(f"/api/orders/question/{qid}/answer", json={
        "new_product": {"gtin": "3650", "name": "Chlieb kváskový",
                        "doplnok": "pražená cibuľka"}, "quantity": 5})
    assert r.status_code == 200
    assert pg.execute(
        "SELECT alias FROM catalog_overrides WHERE gtin='3650'"
    ).fetchone() == ("pražená cibuľka",)
