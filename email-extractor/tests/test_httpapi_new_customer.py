"""#234: adding a genuinely NEW customer straight from the "who is this?" question card.

`teach.answer_customer` only accepts a pick from the question's own FROZEN candidate list,
built once at ask-time from `customer_snapshot` — a sender who does not exist in
`customer_snapshot` at all can therefore never be in that set, and the only reachable
answers today are "pick a wrong customer" or "Neviem, kto to je" (which parks the order for
manual retyping). This file pins the fix: a "new_customer" body on the existing answer
route, a live search over ALL current customers (not just the frozen candidates), and the
EAN-cannot-be-forgotten guarantee the ticket explicitly asked for.

Flask test client + real Postgres, same pattern as test_api.py / test_httpapi_znalosti.py.
"""
import os

from psycopg.types.json import Json

from app.config import Config
from app.httpapi import create_app, dl_key, sklad_key
from app.orders import snapshot, teach

PG_DSN = os.environ.get("PG_TEST_DSN")

CATALOG_CSV = "GTIN,Názov,doplnok\nG50,Rožok štandart 50g,\n"
CUSTOMER_CSV = (
    "Názov organizácie,EAN kód EDI,Obec,Ulica,E-mail\n"
    "Pekáreň Existujúca,2000000000001,Martin,Košútka 1,existujuca@pekaren.sk\n"
)


def _cfg():
    return Config(pg_dsn=PG_DSN, data_dir="/tmp", api_token="tok", dash_password="secret",
                 secret_key="test-secret", odoo_url="", odoo_api_key="",
                 orders_channel_id=0, orders_shadow=False)


def _client():
    app = create_app(_cfg())
    app.testing = True
    return app.test_client()


def _login(c):
    c.post("/login", data={"password": "secret"})


def _seed_held_order(pg, sender_email="novy@zakaznik.sk", message_id="m234", candidates=None):
    """One held order with no known customer at all — exactly what `pipeline._run` leaves
    behind when the sender is absent from `customer_snapshot` entirely (#234's dominant
    class, verified live: production `order_questions` id 29 / `held_orders` id 11)."""
    snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    pg.execute("INSERT INTO messages (message_id, category) VALUES (%s, 'ai_orders')",
              (message_id,))
    if candidates is None:
        candidates = [{"ean_edi": "2000000000001", "name": "Pekáreň Existujúca",
                      "city": "Martin", "street": "Košútka 1", "address_match": False}]
    qid = teach.ask_customer(
        pg, message_id=message_id, sender_email=sender_email, candidates=candidates,
        delivery_date="04.08.2026",
        context={"sender_email": sender_email, "sender_name": "Nový zákazník",
                "company_name": "Nová firma s.r.o.", "delivery_address_guess": ""})
    pg.execute(
        """INSERT INTO held_orders (message_id, customer_ean, customer_name, delivery_date,
                                    order_number, question_ids, order_json, extracted_json,
                                    decisions_json)
           VALUES (%s, '', '', '04.08.2026', '', %s, %s, %s, %s)""",
        (message_id, [qid], Json({"deliveryDate": "04.08.2026", "orderNumber": ""}),
         Json({"isChangeRequest": False, "unverified": [], "notes": ""}),
         Json([{"item_name": "rožok 50g", "gtin": "G50", "card": "Rožok štandart 50g",
                "confidence": 0.95, "rule": "catalog_direct", "note": "", "review": False,
                "trace": {}, "quantity": 120, "unit": "ks"}])))
    return qid


# --- the RED case: today's dead end ---------------------------------------------------

def test_a_held_order_from_an_unknown_customer_cannot_be_completed_today(pg, monkeypatch):
    """Proves the bug this ticket exists to fix: a brand-new customer, typed in from
    CODEX, must complete the held order immediately — one document, no second click."""
    qid = _seed_held_order(pg)
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    c = _client()
    _login(c)
    r = c.post(f"/api/orders/question/{qid}/answer", json={"new_customer": {
        "ean_edi": "7000000000099", "name": "Nová Pekáreň s.r.o.",
        "emails": "", "city": "Žilina", "street": "Vysokoškolákov 1", "zip": "01008"}})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["released"] and body["released"][0]["status"] == "ok"
    assert pg.execute(
        "SELECT status, customer_ean FROM held_orders WHERE message_id='m234'"
    ).fetchone() == ("released", "7000000000099")
    assert pg.execute(
        "SELECT count(*) FROM edi_sent WHERE customer_ean='7000000000099'").fetchone()[0] == 1
    row = pg.execute(
        "SELECT ean_edi, name FROM customer_overrides WHERE ean_edi='7000000000099'"
    ).fetchone()
    assert row == ("7000000000099", "Nová Pekáreň s.r.o.")
    assert teach.get(pg, qid)["status"] == "answered"


def test_adding_a_customer_without_an_ean_is_refused(pg, monkeypatch):
    qid = _seed_held_order(pg)
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    c = _client()
    _login(c)
    r = c.post(f"/api/orders/question/{qid}/answer",
              json={"new_customer": {"name": "Bez EAN s.r.o."}})
    assert r.status_code == 400
    assert pg.execute("SELECT count(*) FROM customer_overrides").fetchone()[0] == 0
    assert teach.get(pg, qid)["status"] == "open"
    assert pg.execute(
        "SELECT status FROM held_orders WHERE message_id='m234'").fetchone() == ("held",)


def test_a_non_numeric_ean_is_refused(pg, monkeypatch):
    qid = _seed_held_order(pg)
    c = _client()
    _login(c)
    r = c.post(f"/api/orders/question/{qid}/answer",
              json={"new_customer": {"ean_edi": "SK12345", "name": "Zlý EAN s.r.o."}})
    assert r.status_code == 400
    assert pg.execute("SELECT count(*) FROM customer_overrides").fetchone()[0] == 0


def test_an_ean_that_already_belongs_to_someone_returns_409_with_that_customer(pg,
                                                                                monkeypatch):
    qid = _seed_held_order(pg)
    c = _client()
    _login(c)
    r = c.post(f"/api/orders/question/{qid}/answer", json={"new_customer": {
        "ean_edi": "2000000000001", "name": "Nejaké iné meno"}})
    assert r.status_code == 409
    body = r.get_json()
    assert body["existing"]["name"] == "Pekáreň Existujúca"
    assert teach.get(pg, qid)["status"] == "open"
    assert pg.execute(
        "SELECT status FROM held_orders WHERE message_id='m234'").fetchone() == ("held",)


def test_two_concurrent_new_customer_ean_collisions_leave_exactly_one_winner(pg, monkeypatch):
    """#248 review finding: httpapi.py's `except snapshot.DuplicateEan` in
    `_api_orders_answer_new_customer` had zero HTTP-level test coverage — the existing
    409 test above only exercises the SEQUENTIAL pre-check (an EAN that already belongs
    to someone BEFORE the request starts), never the race path this whole ticket is
    about. Proven with two real HTTP requests through the Flask test client racing the
    actual advisory lock — same shape as `test_two_concurrent_answers_to_the_same_dl_
    question_leave_exactly_one_winner` in test_httpapi_new_dl.py, one layer up at the
    HTTP boundary for #248's own race. Two DIFFERENT held orders/questions (a single
    question can only be answered once, which would hit AlreadyAnswered instead) both
    add a "new customer" with the SAME brand-new EAN but a DIFFERENT street."""
    import threading

    qid_a = _seed_held_order(pg, sender_email="preteka@x.sk", message_id="m248a")
    qid_b = _seed_held_order(pg, sender_email="pretekb@x.sk", message_id="m248b")
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    c1, c2 = _client(), _client()
    _login(c1)
    _login(c2)
    barrier = threading.Barrier(2)
    results: dict[str, int] = {}

    def answer(key, client, qid, street):
        barrier.wait(timeout=5)
        r = client.post(f"/api/orders/question/{qid}/answer", json={"new_customer": {
            "ean_edi": "7200000000001", "name": f"Pretekár {key}",
            "emails": "", "city": "Košice", "street": street, "zip": ""}})
        results[key] = r.status_code

    t1 = threading.Thread(target=answer, args=("A", c1, qid_a, "Ulica A"))
    t2 = threading.Thread(target=answer, args=("B", c2, qid_b, "Ulica B"))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    codes = sorted([results.get("A"), results.get("B")])
    assert codes == [200, 409], f"exactly one racing new-customer add may win, got {results}"
    assert pg.execute(
        "SELECT count(*) FROM customer_overrides WHERE ean_edi='7200000000001'"
    ).fetchone()[0] == 1


def test_the_next_mail_from_the_same_address_needs_no_question(pg, monkeypatch):
    """The teaching is durable (#234's whole point) — the sender address gets appended to
    the new customer's e-mail list, so `customer.resolve` finds it via `exact_email` with
    no board question needed for the NEXT order."""
    from app.orders import customer

    qid = _seed_held_order(pg, sender_email="druha@zakaznik.sk")
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    c = _client()
    _login(c)
    r = c.post(f"/api/orders/question/{qid}/answer", json={"new_customer": {
        "ean_edi": "7000000000050", "name": "Druhá Nová s.r.o."}})
    assert r.status_code == 200
    customers = snapshot.customers_for_management(pg)
    matched = customer.resolve(customers, "druha@zakaznik.sk", "", "")
    assert matched is not None
    assert matched.ean_edi == "7000000000050"
    assert matched.rule == "exact_email"


def test_adding_the_same_new_customer_twice_leaves_one_customer(pg, monkeypatch):
    """§2.2 of the design: a brand-new customer had NO ON CONFLICT target at all (the
    unique index only covers a still-sheet-only row), so re-adding the identical customer
    (e.g. once from the card, once again via /znalosti) silently inserted a SECOND row —
    and `customer.resolve`'s exact_email rung then refuses ANY order from that address
    once len(owners) > 1, i.e. the order gets stuck BECAUSE the customer was added twice."""
    qid = _seed_held_order(pg, sender_email="dvojita@zakaznik.sk")
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    c = _client()
    _login(c)
    r1 = c.post(f"/api/orders/question/{qid}/answer", json={"new_customer": {
        "ean_edi": "7000000000077", "name": "Dvojitá s.r.o.", "street": "Ulica 9"}})
    assert r1.status_code == 200
    r2 = c.post("/api/znalosti/clients", json={
        "ean_edi": "7000000000077", "name": "Dvojitá s.r.o.", "street": "Ulica 9"})
    assert r2.status_code == 200
    rows = [r for r in c.get("/api/znalosti/clients?q=dvojit").get_json()["items"]
           if r["ean_edi"] == "7000000000077"]
    assert len(rows) == 1


def test_picking_an_existing_customer_found_by_search_completes_the_order(pg, monkeypatch):
    """The unmapped-address class (#234's second inventory row): the customer already
    exists, just not under this sender address, and it was never offered as a frozen
    candidate. A pick from the live search box must be legitimised server-side and complete
    the order, and the address gets remembered for next time."""
    qid = _seed_held_order(pg, sender_email="neznama@adresa.sk", candidates=[])
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    c = _client()
    _login(c)
    r = c.post(f"/api/orders/question/{qid}/answer",
              json={"ean_edi": "2000000000001", "name": "Pekáreň Existujúca"})
    assert r.status_code == 200
    assert r.get_json()["released"][0]["status"] == "ok"
    row = pg.execute(
        "SELECT emails FROM customer_overrides WHERE ean_edi='2000000000001'").fetchone()
    assert row and "neznama@adresa.sk" in row[0]


def test_the_dl_role_cannot_add_a_customer(pg, monkeypatch):
    qid = _seed_held_order(pg)
    c = _client()
    c.get("/sklad-dl/" + dl_key("test-secret"))
    r = c.post(f"/api/orders/question/{qid}/answer", json={"new_customer": {
        "ean_edi": "7000000000099", "name": "Nová Pekáreň s.r.o."}})
    assert r.status_code == 403


def test_the_sklad_role_can_add_a_customer(pg, monkeypatch):
    qid = _seed_held_order(pg)
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    c = _client()
    c.get("/sklad/" + sklad_key("test-secret"))
    r = c.post(f"/api/orders/question/{qid}/answer", json={"new_customer": {
        "ean_edi": "7000000000099", "name": "Nová Pekáreň s.r.o."}})
    assert r.status_code == 200
