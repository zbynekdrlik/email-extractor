"""Advise it once, and it is certain and free forever (#88).

Measured on the 30-email corpus (2026-07-31): 32 of 473 item lines would ever reach a human,
and behind them are only **15 distinct (customer, wording) pairs** — private nicknames
("Šiška", "jankove buchty"), sloppy abbreviations, and four variants that do not exist as
ordered ("Dánske pečivo s jahodami" against a čučoriedka card). Teach the 15 and the tail
closes; the four keep asking forever, and that is correct — shipping blueberry for strawberry
is worse than asking.

The user chose the channel (2026-07-31): **one click on the extractor dashboard**, linked from
the Odoo message. So the answer arrives as a card id, never as free text to be parsed.

What is pinned here:
  * a human answer is stored per CUSTOMER, so one customer's "Šiška" never leaks onto another
  * it decides the line with NO model call, and it OVERRIDES the weight guard — that is the
    whole point of a nickname whose weight nobody writes
  * it is not subject to the history's as-of window: a mapping is an instruction, not evidence
  * the same wording is never asked about twice
"""
import pytest

from app.orders import match, memory, teach

CATALOG = [
    {"gtin": "SLI50", "name": "Šiška džemová 50g", "alias": ""},
    {"gtin": "SLI90", "name": "Šiška džemová 90g", "alias": ""},
    {"gtin": "ROZ", "name": "Rožok štandart 50g", "alias": ""},
]
EAN, OTHER = "2000000000001", "2000000000002"


def _ask(pg, wording="Šiška", ean=EAN, **kw):
    return teach.ask(pg, message_id=kw.get("message_id", "m1"), customer_ean=ean,
                     customer_name=kw.get("customer_name", "Zákazník A"), wording=wording,
                     quantity=kw.get("quantity", 30), unit="ks",
                     candidates=kw.get("candidates", [{"gtin": "SLI50",
                                                       "name": "Šiška džemová 50g"},
                                                      {"gtin": "SLI90",
                                                       "name": "Šiška džemová 90g"}]),
                     delivery_date=kw.get("delivery_date", "04.08.2026"),
                     reason=kw.get("reason", "neznáme znenie"))


# --- asking --------------------------------------------------------------

def test_an_unknown_wording_becomes_one_question_with_its_candidates(pg):
    qid = _ask(pg)
    q = teach.get(pg, qid)
    assert q["wording"] == "Šiška" and q["status"] == "open"
    assert [c["gtin"] for c in q["candidates"]] == ["SLI50", "SLI90"]
    assert q["quantity"] == 30 and q["delivery_date"] == "04.08.2026"


def test_the_same_wording_is_never_asked_about_twice(pg):
    first = _ask(pg)
    again = _ask(pg, message_id="m2")
    assert again == first, "one open question per (customer, wording)"
    assert len(teach.open_questions(pg)) == 1


def test_two_customers_using_the_same_nickname_are_asked_separately(pg):
    _ask(pg, ean=EAN)
    _ask(pg, ean=OTHER, customer_name="Zákazník B")
    assert len(teach.open_questions(pg)) == 2, "the same word can mean different cards"


def test_a_wording_already_answered_is_not_asked_again(pg):
    qid = _ask(pg)
    teach.answer(pg, qid, gtin="SLI50", card="Šiška džemová 50g", by="sklad")
    assert _ask(pg, message_id="m3") is None, "it has been taught; asking again is noise"


# --- answering -----------------------------------------------------------

def test_an_answer_is_remembered_for_that_customer_only(pg):
    qid = _ask(pg)
    teach.answer(pg, qid, gtin="SLI50", card="Šiška džemová 50g", by="sklad")

    mine = memory.resolve(pg, EAN, "Šiška")
    assert mine is not None and mine.gtin == "SLI50" and mine.human is True
    assert memory.resolve(pg, OTHER, "Šiška") is None


def test_a_human_answer_is_not_limited_by_the_history_window(pg):
    """`resolve(as_of=...)` keeps deliveries strictly BEFORE the email's day, so the corpus
    stays non-circular. A human mapping is an instruction, not evidence — it applies to the
    email that triggered the question, including one that arrives the same day."""
    qid = _ask(pg)
    teach.answer(pg, qid, gtin="SLI50", card="Šiška džemová 50g", by="sklad")
    assert memory.resolve(pg, EAN, "Šiška", as_of="2026-07-31") is not None


def test_answering_closes_the_question_and_records_who(pg):
    qid = _ask(pg)
    teach.answer(pg, qid, gtin="SLI50", card="Šiška džemová 50g", by="sklad")
    q = teach.get(pg, qid)
    assert q["status"] == "answered" and q["answer_gtin"] == "SLI50"
    assert q["answered_by"] == "sklad" and q["answered_at"]
    assert teach.open_questions(pg) == []


def test_an_answer_outside_the_offered_candidates_is_refused(pg):
    """The click UI only offers candidates; anything else is a bug or a tampered request."""
    qid = _ask(pg)
    with pytest.raises(teach.NotACandidate):
        teach.answer(pg, qid, gtin="ROZ", card="Rožok štandart 50g", by="sklad")


def test_answering_twice_is_refused_rather_than_silently_overwritten(pg):
    qid = _ask(pg)
    teach.answer(pg, qid, gtin="SLI50", card="Šiška džemová 50g", by="sklad")
    with pytest.raises(teach.AlreadyAnswered):
        teach.answer(pg, qid, gtin="SLI90", card="Šiška džemová 90g", by="sklad")


# --- what the engine does with it ----------------------------------------

def test_a_taught_wording_decides_with_no_model_call(pg):
    qid = _ask(pg)
    teach.answer(pg, qid, gtin="SLI50", card="Šiška džemová 50g", by="sklad")
    recalled = memory.resolve(pg, EAN, "Šiška")
    d = match.decide_without_model("Šiška", CATALOG, recalled=recalled)
    assert d is not None
    assert (d.gtin, d.rule, d.review) == ("SLI50", "human_taught", False)


def test_a_taught_wording_overrides_the_weight_guard(pg):
    """"Šiška 90g" taught onto a 50 g card is exactly the case a human must be allowed to
    settle — the guard exists to stop the MODEL guessing, not to overrule the warehouse."""
    qid = _ask(pg, wording="Šiška 90g")
    teach.answer(pg, qid, gtin="SLI50", card="Šiška džemová 50g", by="sklad")
    recalled = memory.resolve(pg, EAN, "Šiška 90g")
    d = match.decide_without_model("Šiška 90g", CATALOG, recalled=recalled)
    assert d is not None and (d.gtin, d.rule) == ("SLI50", "human_taught")


def test_a_thin_ordinary_history_still_does_not_skip_the_model(pg):
    """The human rung must not accidentally lower the bar for machine-learned history."""
    memory.remember(pg, EAN, "Pletenka", "SLI50", "Šiška džemová 50g", "2026-07-01",
                    source="ship")
    recalled = memory.resolve(pg, EAN, "Pletenka")
    assert match.decide_without_model("Pletenka", CATALOG, recalled=recalled) is None


# --- the pipeline asks, the dashboard answers ----------------------------

def _dash(pg):
    import os

    from app.config import Config
    from app.httpapi import create_app
    cfg = Config(pg_dsn=os.environ["PG_TEST_DSN"], data_dir="/tmp", dash_password="pw",
                 secret_key="t")
    app = create_app(cfg)
    app.testing = True
    c = app.test_client()
    c.post("/login", data={"password": "pw"})
    return c


def test_the_dashboard_lists_open_questions_and_teaches_on_one_click(pg):
    qid = _ask(pg)
    c = _dash(pg)
    d = c.get("/api/orders/questions").get_json()
    assert [q["wording"] for q in d["items"]] == ["Šiška"]

    r = c.post(f"/api/orders/question/{qid}/answer",
               json={"gtin": "SLI50", "card": "Šiška džemová 50g"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert c.get("/api/orders/questions").get_json()["items"] == []
    assert memory.resolve(pg, EAN, "Šiška").gtin == "SLI50"


def test_the_dashboard_refuses_a_card_that_was_not_offered(pg):
    qid = _ask(pg)
    r = _dash(pg).post(f"/api/orders/question/{qid}/answer",
                       json={"gtin": "ROZ", "card": "Rožok štandart 50g"})
    assert r.status_code == 400


def test_an_unmatched_line_becomes_a_question_for_the_warehouse(pg):
    """The measured tail: a nickname the catalog cannot resolve. It must not vanish into a
    review message nobody can act on — it becomes one answerable question."""
    from app.config import Config
    from app.orders import pipeline, snapshot

    sid = snapshot.import_snapshot(
        pg, "GTIN,Sklad,Názov,doplnok\nSLI50,1,Šiška džemová 50g,\n",
        "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
        f"Zákazník A,{EAN},Martin,U 1,,,sklad@a.sk\n")
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('mq', 'ai_orders')")
    mail = {"message_id": "mq", "subject": "Objednávka", "from_addr": "sklad@a.sk",
            "from_name": "A", "combined_text": "na 04.08.2026 prosím 30x jankove buchty",
            "today": "2026-07-31"}

    class Client:
        last_prompt_hash = "p"

        def json_call(self, system, user, schema, name="result"):
            if name == "orders":
                return {"senderName": "A", "senderEmail": "sklad@a.sk", "companyName": "",
                        "isChangeRequest": False, "notes": "",
                        "orders": [{"orderNumber": "", "deliveryDate": "04.08.2026",
                                    "recipientGroup": "",
                                    "items": [{"name": "jankove buchty", "quantity": 30,
                                               "unit": "ks",
                                               "sourceQuote": "30x jankove buchty"}]}]}
            if name == "customer":
                return {"ean_edi": EAN, "confidence": 0.99}
            return {"gtin": "", "confidence": 0.2, "matchedCatalogName": "", "reason": "?"}

    cfg = Config(pg_dsn="", data_dir="/tmp", orders_shadow=False, odoo_url="",
                 orion_host="")
    pipeline.run(pg, cfg, mail, sid, client=Client(),
                 upload=lambda *a, **k: None, post=lambda *a, **k: None)

    qs = teach.open_questions(pg)
    assert [q["wording"] for q in qs] == ["jankove buchty"]
    assert qs[0]["customer_ean"] == EAN
    assert any(c["gtin"] == "SLI50" for c in qs[0]["candidates"]), "candidates are offered"


def test_shadow_mode_asks_nobody(pg):
    """Shadow's contract is that nothing leaves the process — and a question to a human
    leaves it. n8n is still handling these emails."""
    from app.config import Config
    from app.orders import pipeline, snapshot

    sid = snapshot.import_snapshot(
        pg, "GTIN,Sklad,Názov,doplnok\nSLI50,1,Šiška džemová 50g,\n",
        "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
        f"Zákazník A,{EAN},Martin,U 1,,,sklad@a.sk\n")
    mail = {"message_id": "ms", "subject": "Objednávka", "from_addr": "sklad@a.sk",
            "from_name": "A", "combined_text": "na 04.08.2026 prosím 30x jankove buchty",
            "today": "2026-07-31"}

    class Client:
        last_prompt_hash = "p"

        def json_call(self, system, user, schema, name="result"):
            if name == "orders":
                return {"senderName": "A", "senderEmail": "sklad@a.sk", "companyName": "",
                        "isChangeRequest": False, "notes": "",
                        "orders": [{"orderNumber": "", "deliveryDate": "04.08.2026",
                                    "recipientGroup": "",
                                    "items": [{"name": "jankove buchty", "quantity": 30,
                                               "unit": "ks",
                                               "sourceQuote": "30x jankove buchty"}]}]}
            if name == "customer":
                return {"ean_edi": EAN, "confidence": 0.99}
            return {"gtin": "", "confidence": 0.2, "matchedCatalogName": "", "reason": "?"}

    pipeline.run(pg, Config(pg_dsn="", data_dir="/tmp", orders_shadow=True), mail, sid,
                 client=Client(), upload=lambda *a, **k: None, post=lambda *a, **k: None)
    assert teach.open_questions(pg) == []
