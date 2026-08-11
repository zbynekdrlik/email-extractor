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


# --- the fix: new_supplier/new_item answer path, and the DL role's widened reach -------
#
# (An earlier draft of this file pinned the pre-fix dead end directly — asserting the
# question STAYS open and NOTHING gets written when posting new_supplier/new_item, and
# that the DL role gets 401 on /api/znalosti/dl-*. Those assertions describe the OLD
# buggy behavior, so they PASSED before this fix and would FAIL after it — the inverse
# of a real regression test. Removed once verified: the tests below assert the CORRECT,
# desired behavior instead, exactly mirroring #234's own `test_a_held_order_from_an_
# unknown_customer_cannot_be_completed_today` pattern — they fail without the fix
# (confirmed on the [red] commit) and pass with it, which is what stays committed.)

def test_adding_a_new_dl_supplier_from_the_card_teaches_it_and_answers(pg):
    """Proves the fix for the bug #235 exists to close: HK LOAN (#236) — a genuinely new
    DL supplier, EAN typed in from CODEX — completes right on the card, one click."""
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


def test_a_new_dl_suppliers_own_sender_address_is_remembered_even_when_left_blank(pg):
    """Regression: `ask_dl_supplier`/`ask_generic` store the sender address in the
    question's `payload` column, not `context` (that column is customer-kind-only) — an
    early draft of `_api_orders_answer_new_dl_supplier` read `context` and always got {}
    for a dl_supplier question, so the sender's own address never got auto-appended when
    she left the "e-maily" field blank in the form."""
    qid = teach.ask_dl_supplier(pg, message_id="m235k", sender_email="gnip@hkloan.eu",
                                candidates=[])
    c = _dl_client()
    r = c.post(f"/api/orders/question/{qid}/answer", json={"new_supplier": {
        "ean_edi": "2000000000900", "name": "HK LOAN s.r.o."}})
    assert r.status_code == 200
    row = pg.execute(
        "SELECT emails FROM dl_supplier_overrides WHERE ean_edi='2000000000900'"
    ).fetchone()
    assert row and "gnip@hkloan.eu" in row[0]


def test_a_new_dl_supplier_ean_already_belonging_to_another_supplier_is_refused(pg):
    """Deep-review finding on #235: `upsert_dl_supplier`'s advisory-lock reclaim only
    matches an EXACT (ean_edi, city) pair against an un-overridden row — it checks
    neither the frozen base snapshot nor an already-overridden supplier under a
    DIFFERENT (or, as here, BLANK — city is optional in this quick form) city. Without
    this check, typing in an EAN that already belongs to a real supplier would silently
    create a SECOND row sharing that ean_edi; both then land in
    dl_suppliers_for_management and dl_match.py would pick whichever comes first,
    possibly a stale name. Mirrors #234's own new_customer collision test."""
    dl_snapshot.upsert_dl_supplier(pg, override_id=None, orig_ean_edi=None, orig_city=None,
                                   ean_edi="2000000000950", name="Existujúci s.r.o.",
                                   emails=[], city="Bratislava")
    qid = teach.ask_dl_supplier(pg, message_id="m235z", sender_email="iny@x.sk",
                                candidates=[])
    c = _dl_client()
    r = c.post(f"/api/orders/question/{qid}/answer", json={"new_supplier": {
        "ean_edi": "2000000000950", "name": "Iný názov s.r.o."}})
    assert r.status_code == 409
    body = r.get_json()
    assert "Existujúci s.r.o." in body["error"]
    assert body["existing"]["ean_edi"] == "2000000000950"
    assert pg.execute(
        "SELECT count(*) FROM dl_supplier_overrides WHERE ean_edi='2000000000950'"
    ).fetchone()[0] == 1
    assert teach.get(pg, qid)["status"] == "open"


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


def test_two_concurrent_answers_to_the_same_dl_question_leave_exactly_one_winner(pg):
    """Deep-review finding on #235: `_api_orders_answer_generic`'s UPDATE had no
    `WHERE status='open'` guard — the `q.get('status') != 'open'` check above it is a
    Python-level read from the EARLIER select this route already did (`q0` in
    `api_orders_answer`), not a write-time guard on the UPDATE itself. Two genuinely
    racing answers to the SAME open question could both pass that check and the second
    write would silently overwrite the first's `answered_by`/`answered_at` — the
    new_supplier/new_item branches route through this same generic path (#235), so the
    race is reachable from the warehouse's own board, not just the pre-existing
    mail/date/line kinds. Proven with real threads + real HTTP requests through the
    Flask test client (fresh psycopg connection per request, `_db_tx()` in httpapi.py) —
    same shape as `test_two_concurrent_answers_to_the_same_customer_question_leave_
    exactly_one_winner` in test_orders_teach.py, one layer up at the HTTP boundary."""
    import threading

    qid = teach.ask_dl_supplier(
        pg, message_id="m235race", sender_email="race@x.sk",
        candidates=[{"ean_edi": "2000000000961", "name": "Pretekár s.r.o."}])
    c1, c2 = _dl_client(), _dl_client()
    barrier = threading.Barrier(2)
    results: dict[str, int] = {}

    def answer(key, client):
        barrier.wait(timeout=5)
        r = client.post(f"/api/orders/question/{qid}/answer",
                        json={"choice": "2000000000961", "by": "sklad"})
        results[key] = r.status_code

    t1 = threading.Thread(target=answer, args=("a", c1))
    t2 = threading.Thread(target=answer, args=("b", c2))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    codes = sorted([results.get("a"), results.get("b")])
    assert codes == [200, 409], f"exactly one racing answer may win, got {results}"
    assert teach.get(pg, qid)["status"] == "answered"


# --- role boundary: neither side widens beyond its own agenda ------------------------

def test_the_dl_role_can_now_reach_the_dl_znalosti_api(pg):
    dl_snapshot.upsert_dl_supplier(pg, override_id=None, orig_ean_edi=None, orig_city=None,
                                   ean_edi="2000000000111", name="Testovací dodávateľ",
                                   emails=[], city="")
    c = _dl_client()
    assert c.get("/api/znalosti/dl-suppliers").status_code == 200
    assert c.get("/api/znalosti/dl-products").status_code == 200


def test_the_dl_role_znalosti_api_regex_does_not_match_a_mere_prefix(pg):
    """SKLAD_DL_ZNALOSTI_API's alternatives are anchored with ^/$ — an unrelated path
    sharing the dl-products/dl-suppliers prefix must not be accidentally granted."""
    c = _dl_client()
    assert c.get("/api/znalosti/dl-productsX").status_code == 401
    assert c.get("/api/znalosti/dl-suppliersX").status_code == 401


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
