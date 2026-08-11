"""#235: adding a genuinely NEW DL supplier/product straight from the "ktorý dodávateľ?"/
"ktorá karta?" question card on the skladníčka's own /otazky-dl board.

Root cause (see the design comment on #235): the Google Sheet she has been (correctly, per
our own bot instructions) editing for weeks stopped being read at all (#129, commit
3330cb4) — every edit since has done nothing. The functional API to add a DL supplier/
product already exists (`dl_snapshot.upsert_dl_supplier`/`upsert_dl_catalog_card`, wired to
`/api/znalosti/dl-suppliers`/`dl-products`), but her role (`SKLAD_DL_ROLE`) cannot reach it,
and even the question card itself only offers pre-computed candidates + "Neviem"
(`_validate_dl_item`/`_validate_dl_supplier` reject anything else). This file pins the fix:
a `new_supplier`/`new_item` body on the existing answer route (mirrors #234's `new_customer`
exactly), the DL role gaining narrow API-only access to the two dl-* endpoints, and the
EAN/GTIN-cannot-be-forgotten guarantee #234 established, reused here.

Flask test client + real Postgres, same pattern as test_httpapi_new_customer.py.
"""
import os

from app.config import Config
from app.httpapi import create_app, dl_key, sklad_key
from app.orders import dl_snapshot, teach

PG_DSN = os.environ.get("PG_TEST_DSN")


def _cfg():
    return Config(pg_dsn=PG_DSN, data_dir="/tmp", api_token="tok", dash_password="secret",
                 secret_key="test-secret", odoo_url="", odoo_api_key="",
                 orders_channel_id=0, orders_shadow=False)


def _client():
    app = create_app(_cfg())
    app.testing = True
    return app.test_client()


def _dl_client():
    c = _client()
    c.get("/sklad-dl/" + dl_key("test-secret"))
    return c


def _sklad_client():
    c = _client()
    c.get("/sklad/" + sklad_key("test-secret"))
    return c


def _login(c):
    c.post("/login", data={"password": "secret"})


# --- the RED case: today's dead end -----------------------------------------------------

def test_a_new_dl_supplier_cannot_be_added_from_the_question_card_today(pg):
    """Proves the bug this ticket exists to fix: HK LOAN (#236) — a genuinely new DL
    supplier, EAN typed in from CODEX — must be addable right on the card. Today the
    unrecognized `new_supplier` body is silently treated as the generic-kind "unknown"
    escape (falls through to `choice=""`), so the POST reports success but does NOTHING:
    no supplier row, the question stays open, the data she typed just vanishes."""
    qid = teach.ask_dl_supplier(pg, message_id="m235a", sender_email="gnip@hkloan.eu",
                                candidates=[])
    c = _dl_client()
    r = c.post(f"/api/orders/question/{qid}/answer", json={"new_supplier": {
        "ean_edi": "2000000000900", "name": "HK LOAN s.r.o.", "emails": "gnip@hkloan.eu"}})
    assert r.status_code == 200
    assert teach.get(pg, qid)["status"] == "open"
    assert pg.execute(
        "SELECT count(*) FROM dl_supplier_overrides WHERE ean_edi='2000000000900'"
    ).fetchone()[0] == 0


def test_a_new_dl_product_cannot_be_added_from_the_question_card_today(pg):
    """Same dead end for a brand-new DL catalog card (the #236 "Soľ jedlá..." case: a
    genuinely new product with no GTIN in Codex yet cannot be entered from her card)."""
    qid = teach.ask_dl_item(
        pg, message_id="m235b", supplier_ean="S1", supplier_name="Mlyn s.r.o.",
        wording="Soľ jedlá kamenná jódovaná 0,7-0,16 mm", quantity=1000, unit="kg",
        candidates=[])
    c = _dl_client()
    r = c.post(f"/api/orders/question/{qid}/answer", json={"new_item": {
        "gtin": "4003885181808", "name": "Soľ jedlá kamenná jódovaná 0,7-0,16 mm"}})
    assert r.status_code == 200
    assert teach.get(pg, qid)["status"] == "open"
    assert pg.execute(
        "SELECT count(*) FROM dl_catalog_overrides WHERE gtin='4003885181808'"
    ).fetchone()[0] == 0


def test_the_dl_role_cannot_reach_the_dl_znalosti_api_today(pg):
    """Root cause half 2: her role's whole path allowlist has no /znalosti reach at all."""
    c = _dl_client()
    assert c.get("/api/znalosti/dl-suppliers").status_code == 401
    assert c.get("/api/znalosti/dl-products").status_code == 401


# --- GREEN: the new_supplier/new_item answer path -----------------------------------

def test_adding_a_new_dl_supplier_from_the_card_teaches_it_and_answers(pg):
    qid = teach.ask_dl_supplier(pg, message_id="m235c", sender_email="gnip@hkloan.eu",
                                candidates=[])
    c = _dl_client()
    r = c.post(f"/api/orders/question/{qid}/answer", json={"new_supplier": {
        "ean_edi": "2000000000900", "name": "HK LOAN s.r.o.", "emails": "gnip@hkloan.eu"}})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    row = pg.execute(
        "SELECT ean_edi, name FROM dl_supplier_overrides WHERE ean_edi='2000000000900'"
    ).fetchone()
    assert row == ("2000000000900", "HK LOAN s.r.o.")
    assert teach.get(pg, qid)["status"] == "answered"
    from app.orders import dl_supplier_memory
    assert dl_supplier_memory.resolve(pg, "gnip@hkloan.eu") == {
        "ean_edi": "2000000000900", "name": "HK LOAN s.r.o."}


def test_adding_a_new_dl_product_from_the_card_teaches_it_and_answers(pg):
    qid = teach.ask_dl_item(
        pg, message_id="m235d", supplier_ean="S1", supplier_name="Mlyn s.r.o.",
        wording="Soľ jedlá kamenná jódovaná 0,7-0,16 mm", quantity=1000, unit="kg",
        candidates=[])
    c = _dl_client()
    r = c.post(f"/api/orders/question/{qid}/answer", json={"new_item": {
        "gtin": "4003885181808", "name": "Soľ jedlá kamenná jódovaná 0,7-0,16 mm"}})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    row = pg.execute(
        "SELECT gtin, name FROM dl_catalog_overrides WHERE gtin='4003885181808'"
    ).fetchone()
    assert row == ("4003885181808", "Soľ jedlá kamenná jódovaná 0,7-0,16 mm")
    assert teach.get(pg, qid)["status"] == "answered"
    from app.orders import dl_memory
    assert dl_memory.resolve(pg, "S1", "Soľ jedlá kamenná jódovaná 0,7-0,16 mm").gtin == (
        "4003885181808")


def test_a_new_dl_supplier_without_an_ean_is_refused(pg):
    qid = teach.ask_dl_supplier(pg, message_id="m235e", sender_email="x@y.sk",
                                candidates=[])
    c = _dl_client()
    r = c.post(f"/api/orders/question/{qid}/answer",
              json={"new_supplier": {"name": "Bez EAN s.r.o."}})
    assert r.status_code == 400
    assert pg.execute("SELECT count(*) FROM dl_supplier_overrides").fetchone()[0] == 0
    assert teach.get(pg, qid)["status"] == "open"


def test_a_new_dl_supplier_with_a_non_numeric_ean_is_refused(pg):
    qid = teach.ask_dl_supplier(pg, message_id="m235f", sender_email="x@y.sk",
                                candidates=[])
    c = _dl_client()
    r = c.post(f"/api/orders/question/{qid}/answer",
              json={"new_supplier": {"ean_edi": "SK123", "name": "Zlý EAN s.r.o."}})
    assert r.status_code == 400
    assert pg.execute("SELECT count(*) FROM dl_supplier_overrides").fetchone()[0] == 0


def test_a_new_dl_product_without_a_gtin_is_refused(pg):
    qid = teach.ask_dl_item(pg, message_id="m235g", supplier_ean="S1",
                            supplier_name="Mlyn", wording="Neznáme", quantity=1, unit="ks",
                            candidates=[])
    c = _dl_client()
    r = c.post(f"/api/orders/question/{qid}/answer",
              json={"new_item": {"name": "Neznáme"}})
    assert r.status_code == 400
    assert pg.execute("SELECT count(*) FROM dl_catalog_overrides").fetchone()[0] == 0


def test_a_new_dl_product_with_a_non_numeric_gtin_is_refused(pg):
    qid = teach.ask_dl_item(pg, message_id="m235h", supplier_ean="S1",
                            supplier_name="Mlyn", wording="Neznáme", quantity=1, unit="ks",
                            candidates=[])
    c = _dl_client()
    r = c.post(f"/api/orders/question/{qid}/answer",
              json={"new_item": {"gtin": "ABC", "name": "Neznáme"}})
    assert r.status_code == 400
    assert pg.execute("SELECT count(*) FROM dl_catalog_overrides").fetchone()[0] == 0


# --- role boundary: neither side widens beyond its own agenda ------------------------

def test_the_dl_role_can_now_reach_the_dl_znalosti_api(pg):
    dl_snapshot.upsert_dl_supplier(pg, override_id=None, orig_ean_edi=None, orig_city=None,
                                   ean_edi="2000000000111", name="Testovací dodávateľ",
                                   emails=[], city="")
    c = _dl_client()
    assert c.get("/api/znalosti/dl-suppliers").status_code == 200
    assert c.get("/api/znalosti/dl-products").status_code == 200


def test_the_orders_role_cannot_reach_the_dl_znalosti_api(pg):
    """Requirement #3 of #235: widening the DL role must NOT also leave (or grant) the
    orders role write access to DL knowledge — verified against a real request, not just
    regex reading (the orders role's `SKLAD_ZNALOSTI_API` regex already covered dl-* before
    this fix, a pre-existing gap this ticket's own boundary requirement closes)."""
    c = _sklad_client()
    assert c.get("/api/znalosti/dl-suppliers").status_code == 401
    assert c.get("/api/znalosti/dl-products").status_code == 401


def test_the_dl_role_cannot_reach_orders_znalosti_endpoints(pg):
    c = _dl_client()
    assert c.get("/api/znalosti/clients").status_code == 401
    assert c.get("/api/znalosti/products").status_code == 401
    assert c.get("/api/znalosti/customers").status_code == 401


def test_the_dl_role_still_cannot_reach_the_znalosti_page(pg):
    c = _dl_client()
    r = c.get("/znalosti")
    assert r.status_code == 302 and "/otazky-dl" in r.headers["Location"]


def test_the_dl_role_cannot_add_a_dl_supplier_via_the_orders_new_customer_route(pg):
    """The id-based answer endpoint is shared — a DL-role session must not be able to
    answer an ORDERS-kind question by guessing its id (mirrors #234's own
    test_the_dl_role_cannot_add_a_customer)."""
    from app.orders import snapshot
    snapshot.import_snapshot(pg, "GTIN,Názov,doplnok\nG1,Rožok,\n",
                             "Názov organizácie,EAN kód EDI,E-mail\nX,1,a@b.sk\n")
    from app.orders import teach as t
    qid = t.ask_customer(pg, message_id="m235i", sender_email="nova@firma.sk",
                         candidates=[], delivery_date="04.08.2026",
                         context={"sender_email": "nova@firma.sk"})
    c = _dl_client()
    r = c.post(f"/api/orders/question/{qid}/answer", json={"new_customer": {
        "ean_edi": "7000000000001", "name": "Nová s.r.o."}})
    assert r.status_code == 403


def test_the_sklad_role_cannot_add_a_dl_supplier(pg):
    """And the reverse: the orders role must not be able to answer a DL-kind question."""
    qid = teach.ask_dl_supplier(pg, message_id="m235j", sender_email="x@y.sk",
                                candidates=[])
    c = _sklad_client()
    r = c.post(f"/api/orders/question/{qid}/answer", json={"new_supplier": {
        "ean_edi": "2000000000901", "name": "X s.r.o."}})
    assert r.status_code == 403
