"""#369: the THIRD escape on a `customer` question — "Nie je to objednávka — takéto maily
ignoruj".

A supplier-eshop purchase confirmation (odbyt@karmen.sk) has the SHAPE of an order, so
extraction succeeds, but the sender is not a customer → `ask_customer` re-opens an
unanswerable `customer` question every week (a `mail` "is this even an order?" question is
never reached, because extraction DID find an order). This adds the customer-half of the
`mail` kind's own `not_order` escape: one click teaches a `mail_rules` ignore rule keyed on
the message's (sender, subject-shape), closes the question, releases the held order to
review WITHOUT shipping, and short-circuits every future mail of the same shape BEFORE
extraction.

Flask test client + real Postgres, same pattern as test_httpapi_new_customer.py.
"""
import os

from psycopg.types.json import Json

from app.config import Config
from app.httpapi import create_app
from app.orders import pipeline, snapshot, teach

PG_DSN = os.environ.get("PG_TEST_DSN")

CATALOG_CSV = "GTIN,Sklad,Názov,doplnok\nG50,1,Rožok štandart 50g,\n"
CUSTOMER_CSV = (
    "Názov organizácie,EAN kód EDI,Obec,Ulica,E-mail\n"
    "Pekáreň Existujúca,2000000000001,Martin,Košútka 1,existujuca@pekaren.sk\n"
)

# Karmen here is the SUPPLIER-side eshop sender (the MIX shop's own purchase confirmations),
# never a customer — synthetic subject text, no customer names/EANs/mail bodies.
SENDER = "odbyt@karmen.sk"
SUBJECT_1 = "KARMEN nová objednávka č. 4521 na 04.08.2026"
SUBJECT_2 = "KARMEN nová objednávka č. 9910 na 12.09.2027"   # same shape, different date/no.


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


def _seed_held_order(pg, sender_email=SENDER, subject=SUBJECT_1, message_id="m369"):
    """One held order tied to a `customer` question — exactly what `pipeline._run` leaves
    behind when the sender is absent from the customer table. Unlike the #234 helper, the
    `messages` row carries a real `from_addr`/`subject`, which is where
    `mark_customer_not_order` reads the mail_rules key from."""
    snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    pg.execute(
        "INSERT INTO messages (message_id, category, from_addr, subject) "
        "VALUES (%s, 'ai_orders', %s, %s)", (message_id, sender_email, subject))
    qid = teach.ask_customer(
        pg, message_id=message_id, sender_email=sender_email,
        candidates=[{"ean_edi": "2000000000001", "name": "Pekáreň Existujúca",
                    "city": "Martin", "street": "Košútka 1", "address_match": False}],
        delivery_date="04.08.2026",
        context={"sender_email": sender_email, "sender_name": "Karmen eshop",
                "company_name": "", "delivery_address_guess": ""})
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


# --- 1. the feature: teaches a rule, closes the question, NEVER ships -------------------

def test_not_order_teaches_ignore_rule_closes_question_and_never_ships(pg, monkeypatch):
    qid = _seed_held_order(pg)
    # if anything tried to ship, this would record it — proving nothing does.
    uploads = []
    monkeypatch.setattr("app.orders.upload.put",
                        lambda cfg, name, content: uploads.append(name) or True)
    c = _client()
    _login(c)
    r = c.post(f"/api/orders/question/{qid}/answer", json={"not_order": True})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["not_order"] is True
    # the held order was released to REVIEW, not shipped
    assert body["released"] and body["released"][0]["status"] == "review"
    assert uploads == []
    assert pg.execute(
        "SELECT count(*) FROM edi_sent").fetchone()[0] == 0
    # a mail_rules ignore row keyed on THIS message's sender + subject-shape, traced to qid
    rule = pg.execute(
        "SELECT sender_norm, subject_key, action, question_id FROM mail_rules").fetchone()
    assert rule == (teach._sender_norm(SENDER), teach.subject_key(SUBJECT_1), "ignore", qid)
    # the question is closed with the distinguishable not_order marker
    q = teach.get(pg, qid)
    assert q["status"] == "answered" and q["answer_card"] == "not_order"
    assert q["answer_gtin"] == ""
    # the message is finished (processed). `processed_by` ends as 'ai_orders' here because
    # releasing the held order runs `_mark_message_done_if_clear` afterward (the SAME
    # finalization every "Neviem"/released-to-review order gets); the `mark_customer_not_
    # order` function's own 'ai_orders_mail_rule' marker is asserted directly below in
    # test_mark_customer_not_order_writes_the_rule_and_marks_processed (the no-held-order
    # case, where nothing re-marks it).
    assert pg.execute(
        "SELECT processed FROM messages WHERE message_id='m369'").fetchone() == (True,)
    # held order left the 'held' state
    assert pg.execute(
        "SELECT status FROM held_orders WHERE message_id='m369'").fetchone() == ("released",)


# --- 2. a following mail of the same shape short-circuits BEFORE extraction -------------

def test_a_following_mail_of_the_same_shape_is_ignored_before_extraction(pg, monkeypatch):
    qid = _seed_held_order(pg)
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    c = _client()
    _login(c)
    assert c.post(f"/api/orders/question/{qid}/answer",
                  json={"not_order": True}).status_code == 200
    # A NEW mail from the SAME sender with the SAME subject SHAPE (different date/number)
    # must be short-circuited by `pipeline._mail_rule` before `extract.run` is ever called —
    # a ScriptedClient with ZERO answers proves the model was never invoked at all.
    sid = pg.execute("SELECT max(id) FROM order_snapshots").fetchone()[0]
    posts = []
    mail2 = {"message_id": "m369b", "subject": SUBJECT_2, "from_addr": SENDER,
             "from_name": "Karmen eshop", "combined_text": "na 12.09.2027 čokoľvek",
             "today": "2026-09-01"}
    pg.execute("INSERT INTO messages (message_id, category, from_addr, subject) "
               "VALUES ('m369b', 'ai_orders', %s, %s)", (SENDER, SUBJECT_2))

    class _Empty:
        def json_call(self, *a, **k):
            raise AssertionError("extraction ran — the ignore rule did not short-circuit")

    result = pipeline.run(pg, _cfg(), mail2, sid, client=_Empty(),
                          upload=lambda c, n, ct: True,
                          post=lambda c, html, **k: posts.append(html) or {"id": 1})
    assert result["status"] == "ok"
    assert len(posts) == 1 and "ignorované" in posts[0].lower()


# --- 3. undo removes the rule and reopens the question ---------------------------------

def test_undo_removes_the_ignore_rule_and_reopens_the_question(pg, monkeypatch):
    qid = _seed_held_order(pg)
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    c = _client()
    _login(c)
    assert c.post(f"/api/orders/question/{qid}/answer",
                  json={"not_order": True}).status_code == 200
    assert pg.execute("SELECT count(*) FROM mail_rules").fetchone()[0] == 1

    r = c.post(f"/api/orders/question/{qid}/undo")
    assert r.status_code == 200
    # the rule THIS question created is gone
    assert pg.execute(
        "SELECT count(*) FROM mail_rules WHERE question_id=%s", (qid,)).fetchone()[0] == 0
    # the question is open again, marker cleared
    q = teach.get(pg, qid)
    assert q["status"] == "open"
    assert q["answer_card"] in (None, "") and q["answer_gtin"] in (None, "")


# --- 4. a concurrent, already-answered question is never overwritten -------------------

def test_marking_not_order_on_an_already_answered_question_is_refused(pg):
    """The guarded `UPDATE ... WHERE status='open'` must lose to a human answer that already
    landed — no rule is written and the earlier answer is untouched."""
    import pytest
    qid = _seed_held_order(pg)
    # simulate the race winner: a real "Neviem" answer already settled the question
    teach.answer_customer(pg, qid, ean_edi="", name="", by="human")
    before = teach.get(pg, qid)
    with pytest.raises(teach.AlreadyAnswered):
        teach.mark_customer_not_order(pg, qid, by="sklad")
    # nothing taught, the winner's answer stands
    assert pg.execute("SELECT count(*) FROM mail_rules").fetchone()[0] == 0
    after = teach.get(pg, qid)
    assert after["status"] == "answered"
    assert after["answered_by"] == before["answered_by"] == "human"
    assert after["answer_card"] != "not_order"


def test_mark_customer_not_order_writes_the_rule_and_marks_processed(pg):
    """The function itself (no held order to release) writes the ignore rule keyed on the
    message's own sender+subject, marks the message processed by 'ai_orders_mail_rule', and
    logs an honest review event — never the OK/EDI logger."""
    pg.execute("INSERT INTO messages (message_id, category, from_addr, subject) "
               "VALUES ('mc369', 'ai_orders', %s, %s)", (SENDER, SUBJECT_1))
    qid = teach.ask_customer(
        pg, message_id="mc369", sender_email=SENDER,
        candidates=[{"ean_edi": "2000000000001", "name": "X", "city": "", "street": "",
                    "address_match": False}],
        delivery_date="04.08.2026",
        context={"sender_email": SENDER, "sender_name": "", "company_name": "",
                "delivery_address_guess": ""})
    q = teach.mark_customer_not_order(pg, qid, by="sklad")
    assert q["status"] == "answered" and q["answer_card"] == "not_order"
    assert pg.execute(
        "SELECT sender_norm, subject_key, action, question_id FROM mail_rules").fetchone() \
        == (teach._sender_norm(SENDER), teach.subject_key(SUBJECT_1), "ignore", qid)
    assert pg.execute(
        "SELECT processed, processed_by FROM messages WHERE message_id='mc369'"
    ).fetchone() == (True, "ai_orders_mail_rule")
    # honest review/skip event — never an OK/EDI 'uploaded' outcome
    ev = pg.execute(
        "SELECT stage, status, outcome FROM email_events WHERE message_id='mc369' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert ev[0] == "review" and ev[1] == "ok"
    assert "nie je objednávka" in ev[2].lower()


def test_not_order_with_a_blank_header_message_teaches_no_rule_but_still_closes(pg):
    """Defensive (review #369): a message with no sender AND no subject must NOT teach a
    degenerate ('', '', 'ignore') rule (it would ignore any future truly-blank mail) — the
    question is still closed and the message still marked processed, only the rule is
    skipped."""
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('mblank', 'ai_orders')")
    qid = teach.ask_customer(
        pg, message_id="mblank", sender_email="",
        candidates=[{"ean_edi": "2000000000001", "name": "X", "city": "", "street": "",
                    "address_match": False}],
        delivery_date="04.08.2026",
        context={"sender_email": "", "sender_name": "", "company_name": "",
                "delivery_address_guess": ""})
    q = teach.mark_customer_not_order(pg, qid, by="sklad")
    assert q["status"] == "answered" and q["answer_card"] == "not_order"
    assert pg.execute("SELECT count(*) FROM mail_rules").fetchone()[0] == 0
    assert pg.execute(
        "SELECT processed FROM messages WHERE message_id='mblank'").fetchone() == (True,)


def test_mark_customer_not_order_refuses_a_non_customer_question(pg):
    """Defensive: the helper is customer-kind only (mirrors `answer_customer`'s own guard)."""
    import pytest
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('mk', 'ai_orders')")
    mq = teach.ask_mail(pg, message_id="mk", sender_email="x@y.sk", subject="Faktúra")
    with pytest.raises(teach.NotACandidate):
        teach.mark_customer_not_order(pg, mq, by="sklad")
