"""#477 (replaces #426's „➕ Nová karta" on the item question): a card is never TYPED onto an
orders item question — it is PICKED from the CODEX stock-card list.

History: #426 let the warehouse type a brand-new číslo položky + name straight onto the
"ktorý výrobok to je?" (kind='item') question (a `new_product` body). Owner order 2026-09-30
(#477, after the #467 incident — a typed code the warehouse invented broke orders and DLs):
typed creation is gone everywhere. A `new_product` body is now refused 403 (nothing written,
the held order stays held); the replacement is a `codex_card` body — the picked CODEX card
(CODEX code + CODEX name, orders scope = CODEX stredisko 1 / sklad 1) lands in
`catalog_overrides` (audited), the question is answered by `sklad-codex-card` through the
normal `teach.answer` path, and the held order is released — still one click.

Flask test client + real Postgres, same pattern as test_httpapi_new_dl.py /
test_httpapi_new_customer.py. Synthetic codes/names only (public repo).
"""
import os
from datetime import UTC, datetime, timedelta

from psycopg.types.json import Json

from app.config import Config
from app.httpapi import create_app, sklad_key
from app.orders import codex_cards, memory, snapshot

PG_DSN = os.environ.get("PG_TEST_DSN")

# 3600 is a pre-existing base-snapshot card (a real číslo položky is numeric).
CATALOG_CSV = "GTIN,Názov,doplnok\n3600,Rožok štandart,\n"
CUSTOMER_CSV = (
    "Názov organizácie,EAN kód EDI,Obec,Ulica,E-mail\n"
    "Pekáreň Testovacia,2000000000864,Martin,Košútka 1,sklad@pekaren.sk\n"
)
CODEX = [
    {"code": "3650", "card_code": "3650", "stredisko": 1, "sklad": 1,
     "name": "Chlieb kváskový pražená cibuľka 500g"},
    {"code": "3600", "card_code": "3600", "stredisko": 1, "sklad": 1,
     "name": "Rožok štandart 40g"},
]


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


def _codex(pg):
    codex_cards.replace_cards(pg, CODEX, source_as_of=datetime.now(UTC) - timedelta(hours=1))


def _seed_item_held_order(pg, message_id="m426"):
    """One held order waiting on ONE item question whose line is unmatched — exactly what
    `pipeline._run` leaves when a wording has no catalog card yet."""
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


# --- #477: a typed card is refused ----------------------------------------------------

def test_a_typed_new_product_on_an_item_question_is_refused_codex_only(pg, monkeypatch):
    """The #426 `new_product` body (a typed číslo položky + názov) is refused 403 from BOTH the
    admin session and the warehouse link — nothing written, nothing shipped, still open."""
    qid = _seed_item_held_order(pg)
    _codex(pg)
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    admin = _client()
    _login(admin)
    for c in (admin, _sklad_client()):
        r = c.post(f"/api/orders/question/{qid}/answer", json={
            "new_product": {"gtin": "3650", "name": "Chlieb kváskový"}, "quantity": 5})
        assert r.status_code == 403
        body = r.get_json()
        assert body["codex_only"] is True
        assert "Nové karty sa pridávajú len výberom z CODEXu" in body["error"]
    assert _answered_row(pg, qid)[0] == "open"
    assert pg.execute("SELECT count(*) FROM catalog_overrides").fetchone()[0] == 0
    assert pg.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 0
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 0
    assert pg.execute(
        "SELECT status FROM held_orders WHERE message_id='m426'").fetchone() == ("held",)


# --- #477: the CODEX pick creates + answers + releases --------------------------------

def test_picking_a_codex_card_on_a_held_item_question_creates_it_answers_and_releases(
        pg, monkeypatch):
    """The whole flow from the warehouse link: the picked CODEX card lands as an override
    card (CODEX code + CODEX name, audited), the question is answered by `sklad-codex-card`
    (the confirmed quantity kept, the wording taught), and the held order ships once."""
    qid = _seed_item_held_order(pg)
    _codex(pg)
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    r = _sklad_client().post(f"/api/orders/question/{qid}/answer", json={
        "codex_card": {"code": "3650"}, "quantity": 5, "unit_price": "1,20"})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["ok"] is True
    assert pg.execute("SELECT gtin, name FROM catalog_overrides").fetchall() == [
        ("3650", "Chlieb kváskový pražená cibuľka 500g")]
    assert "3650" in snapshot.catalog_gtin_set(pg)
    audit = pg.execute("SELECT actor, table_name, row_id, action FROM audit_log "
                       "WHERE table_name='catalog_overrides'").fetchall()
    assert audit == [("sklad", "catalog_overrides", "3650", "create")]
    assert _answered_row(pg, qid) == ("answered", "3650", "sklad-codex-card")
    assert pg.execute("SELECT quantity FROM order_questions WHERE id=%s",
                      (qid,)).fetchone()[0] == 5
    assert memory.resolve(pg, "2000000000864", "chlieb").gtin == "3650"
    assert body["released"] and body["released"][0]["status"] == "ok"
    assert pg.execute(
        "SELECT status FROM held_orders WHERE message_id='m426'").fetchone() == ("released",)
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 1


def test_picking_a_codex_card_we_already_have_answers_with_our_card(pg, monkeypatch):
    """3600 is already our card (base snapshot) — the pick only SELECTS it: no override, no
    audit create, our name kept; the order ships with it."""
    qid = _seed_item_held_order(pg)
    _codex(pg)
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    c = _client()
    _login(c)
    r = c.post(f"/api/orders/question/{qid}/answer", json={
        "codex_card": {"code": "3600"}, "quantity": 5})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert pg.execute("SELECT count(*) FROM catalog_overrides").fetchone()[0] == 0
    assert pg.execute("SELECT count(*) FROM audit_log WHERE action='create'").fetchone()[0] == 0
    row = _answered_row(pg, qid)
    assert row[:2] == ("answered", "3600")
    assert pg.execute("SELECT answer_card FROM order_questions WHERE id=%s",
                      (qid,)).fetchone()[0] == "Rožok štandart"
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 1
