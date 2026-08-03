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


# --- notifying about a NEW question (#102) --------------------------------

def test_on_new_fires_for_a_genuinely_new_question(pg):
    seen = []
    qid = _ask(pg)
    # _ask() already fired on_new=None; call ask() directly to observe the callback on a
    # fresh (customer, wording) that has not been asked yet this test.
    qid2 = teach.ask(pg, message_id="m9", customer_ean=OTHER, customer_name="Zákazník B",
                     wording="Pletenka", quantity=5, unit="ks", candidates=[],
                     on_new=lambda q: seen.append(q))
    assert len(seen) == 1 and seen[0]["id"] == qid2 and seen[0]["wording"] == "Pletenka"
    assert qid != qid2


def test_on_new_does_not_fire_for_a_duplicate_of_an_open_question(pg):
    seen = []
    teach.ask(pg, message_id="m1", customer_ean=EAN, customer_name="Zákazník A",
             wording="Šiška", quantity=30, unit="ks", candidates=[],
             on_new=lambda q: seen.append(q))
    assert len(seen) == 1
    teach.ask(pg, message_id="m2", customer_ean=EAN, customer_name="Zákazník A",
             wording="Šiška", quantity=30, unit="ks", candidates=[],
             on_new=lambda q: seen.append(q))
    assert len(seen) == 1, "the same open question must not notify a second time"


def test_on_new_does_not_fire_once_the_wording_is_already_taught(pg):
    qid = _ask(pg)
    teach.answer(pg, qid, gtin="SLI50", card="Šiška džemová 50g", by="sklad")
    seen = []
    assert teach.ask(pg, message_id="m3", customer_ean=EAN, customer_name="Zákazník A",
                     wording="Šiška", quantity=30, unit="ks", candidates=[],
                     on_new=lambda q: seen.append(q)) is None
    assert seen == []


def test_a_failing_on_new_never_breaks_asking(pg):
    """A notification failure (e.g. Odoo unreachable) must never lose the question itself —
    the same log-and-continue discipline the main order report's own post already has."""
    def boom(q):
        raise RuntimeError("odoo is down")

    qid = teach.ask(pg, message_id="m1", customer_ean=EAN, customer_name="Zákazník A",
                    wording="Šiška", quantity=30, unit="ks", candidates=[], on_new=boom)
    assert qid is not None
    assert teach.get(pg, qid)["wording"] == "Šiška"


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


def test_an_answer_outside_the_candidates_but_in_the_full_catalog_is_accepted(pg):
    """#149: none of the 6 offered candidates may be the right card — the warehouse must be
    able to search the WHOLE catalog (here: 127 real cards) and answer with any of them. A
    gtin in the current catalog snapshot, even though `_ask()`'s two candidates never
    included it, must be accepted exactly like a candidate click — same teach, same release."""
    from app.orders import snapshot
    snapshot.import_snapshot(
        pg, "GTIN,Názov,doplnok\nSLI50,Šiška džemová 50g,\nSLI90,Šiška džemová 90g,\n"
            "ROZ,Rožok štandart 50g,\n",
        "Názov organizácie,EAN kód EDI,E-mail\nZákazník A,2000000000001,a@x.sk\n")
    qid = _ask(pg)   # candidates only offer SLI50/SLI90 — ROZ is the searched-for card
    q = teach.answer(pg, qid, gtin="ROZ", card="Rožok štandart 50g", by="sklad")
    assert q["status"] == "answered" and q["answer_gtin"] == "ROZ"
    assert memory.resolve(pg, EAN, "Šiška").gtin == "ROZ"


def test_an_answer_neither_a_candidate_nor_in_the_catalog_is_still_refused(pg):
    """The broadened check must not become "accept anything" — a gtin that is in neither the
    offered candidates NOR the current catalog snapshot stays refused."""
    from app.orders import snapshot
    snapshot.import_snapshot(
        pg, "GTIN,Názov,doplnok\nSLI50,Šiška džemová 50g,\nSLI90,Šiška džemová 90g,\n",
        "Názov organizácie,EAN kód EDI,E-mail\nZákazník A,2000000000001,a@x.sk\n")
    qid = _ask(pg)
    with pytest.raises(teach.NotACandidate):
        teach.answer(pg, qid, gtin="NOPE", card="Neexistujúca karta", by="sklad")


def test_an_answer_also_teaches_the_wording_globally(pg):
    """#102: "Šiška" is a product name, not one customer's private word — the SAME answer
    must resolve it for a DIFFERENT customer with no further asking."""
    qid = _ask(pg)
    teach.answer(pg, qid, gtin="SLI50", card="Šiška džemová 50g", by="sklad")
    glob = memory.resolve_global(pg, "Šiška")
    assert glob is not None and glob.gtin == "SLI50"


def test_a_different_customers_own_answer_still_beats_the_global_one(pg):
    """The safety point: a SECOND customer answering the same wording differently teaches
    THEIR OWN mapping (checked first by decide_without_model), never overwrites the global
    row a different question already owns."""
    first = _ask(pg, ean=EAN)
    teach.answer(pg, first, gtin="SLI50", card="Šiška džemová 50g", by="sklad")

    second = _ask(pg, ean=OTHER, customer_name="Zákazník B", message_id="m2")
    teach.answer(pg, second, gtin="SLI90", card="Šiška džemová 90g", by="sklad")

    assert memory.resolve(pg, OTHER, "Šiška").gtin == "SLI90", "their own answer wins for them"
    assert memory.resolve_global(pg, "Šiška").gtin == "SLI50", "first teach still owns global"


def test_answering_twice_is_refused_rather_than_silently_overwritten(pg):
    qid = _ask(pg)
    teach.answer(pg, qid, gtin="SLI50", card="Šiška džemová 50g", by="sklad")
    with pytest.raises(teach.AlreadyAnswered):
        teach.answer(pg, qid, gtin="SLI90", card="Šiška džemová 90g", by="sklad")


# --- #159: the customer-half of the same teach-once loop — "who is this?" -------

CUST_CANDS = [{"ean_edi": "2000000000861", "name": "Potraviny nie otraviny Žilina",
              "city": "Žilina", "street": "na bráne 4", "address_match": True},
             {"ean_edi": "2000000000864", "name": "Potraviny nie otraviny Martin",
              "city": "Martin", "street": "Košútka 1", "address_match": False}]
CUST_CONTEXT = {"sender_email": "zilina@farmeria.sk", "sender_name": "Sklad",
                "company_name": "", "delivery_address_guess": "Na bráne 4, 010 01 Žilina"}


def _ask_customer(pg, sender_email="zilina@farmeria.sk", **kw):
    return teach.ask_customer(
        pg, message_id=kw.get("message_id", "m1"), sender_email=sender_email,
        candidates=kw.get("candidates", CUST_CANDS),
        delivery_date=kw.get("delivery_date", "06.08.2026"),
        context=kw.get("context", CUST_CONTEXT))


def test_an_unrecognized_sender_becomes_one_kind_customer_question(pg):
    qid = _ask_customer(pg)
    q = teach.get(pg, qid)
    assert q["kind"] == "customer"
    assert q["customer_ean"] == "" and q["customer_name"] == ""
    assert q["status"] == "open"
    assert [c["ean_edi"] for c in q["candidates"]] == \
        ["2000000000861", "2000000000864"]
    assert q["context"]["sender_email"] == "zilina@farmeria.sk"
    assert q["context"]["delivery_address_guess"] == "Na bráne 4, 010 01 Žilina"


def test_the_same_unresolved_sender_is_never_asked_about_twice(pg):
    first = _ask_customer(pg)
    again = _ask_customer(pg, message_id="m2")
    assert again == first, "one open question per unresolved sender address"
    assert len([q for q in teach.open_questions(pg) if q["kind"] == "customer"]) == 1


def test_two_different_unresolved_senders_are_asked_separately(pg):
    _ask_customer(pg, sender_email="a@nikde.sk")
    _ask_customer(pg, sender_email="b@nikde.sk")
    assert len(teach.open_questions(pg)) == 2


def test_asking_with_no_sender_address_at_all_falls_back_to_a_per_message_key(pg):
    """#164 row 4: a blank sender address used to make `ask_customer` refuse outright
    (`None`), which was `pipeline._run`'s "no sender address to even key a question on"
    dead end — the order shipped/rejected with NOBODY ever able to answer. Now it falls
    back to a synthetic per-message key (`cust:<message_id>`) so the warehouse can still
    be asked and the order can still be HELD for an answer."""
    qid = _ask_customer(pg, sender_email="")
    assert qid is not None
    q = teach.get(pg, qid)
    assert q["kind"] == "customer" and q["status"] == "open"
    # A second call for the SAME message reuses the same synthetic key (idempotent retry).
    assert _ask_customer(pg, sender_email="", message_id="m1") == qid
    # A DIFFERENT message with no address gets its OWN question, never collapsed together.
    other = _ask_customer(pg, sender_email="", message_id="m2")
    assert other != qid


def test_two_addresses_differing_only_by_punctuation_are_never_deduped_together(pg):
    """Adversarial review finding on PR #161: the dedupe key used to reuse
    `memory.item_key` — a FUZZY product-wording normalizer that folds '.', '-', '_', '@'
    all to the same blank separator. Two DIFFERENT real senders whose addresses differ
    only by that kind of punctuation must never collapse onto the SAME open question —
    answering it for one would silently ship the OTHER sender's order under the wrong
    customer's identity too."""
    a = _ask_customer(pg, sender_email="a.b@x.com")
    b = _ask_customer(pg, sender_email="a-b@x.com")
    c = _ask_customer(pg, sender_email="a_b@x.com")
    assert len({a, b, c}) == 3, "three genuinely different addresses, three questions"
    assert len(teach.open_questions(pg)) == 3


def test_an_item_question_and_a_customer_question_never_collide(pg):
    """A plain item question always carries a REAL customer_ean; a customer question is
    always keyed on customer_ean='' — but both dedupe on (customer_ean, item_key), so this
    pins that the two kinds genuinely cannot collide even by coincidence."""
    item_qid = _ask(pg, wording="zilina farmeria sk")   # deliberately close to the email key
    cust_qid = _ask_customer(pg, sender_email="zilina@farmeria.sk")
    assert item_qid != cust_qid
    assert len(teach.open_questions(pg)) == 2


def test_answering_a_customer_question_with_a_real_pick(pg):
    qid = _ask_customer(pg)
    q = teach.answer_customer(pg, qid, ean_edi="2000000000861",
                              name="Potraviny nie otraviny Žilina", by="sklad")
    assert q["status"] == "answered"
    assert q["answer_gtin"] == "2000000000861"
    assert q["answer_card"] == "Potraviny nie otraviny Žilina"
    assert teach.open_questions(pg) == []


def test_answering_with_unknown_is_a_blank_answer_never_a_candidate(pg):
    """'neviem, kto to je' (#159) — the question is settled with NO customer chosen."""
    qid = _ask_customer(pg)
    q = teach.answer_customer(pg, qid, ean_edi="", name="", by="sklad")
    assert q["status"] == "answered"
    assert q["answer_gtin"] == ""


def test_answering_a_customer_question_outside_the_offered_candidates_is_refused(pg):
    qid = _ask_customer(pg)
    with pytest.raises(teach.NotACandidate):
        teach.answer_customer(pg, qid, ean_edi="9999999999999", name="Vymyslený", by="sklad")


def test_answering_a_customer_question_twice_is_refused(pg):
    qid = _ask_customer(pg)
    teach.answer_customer(pg, qid, ean_edi="2000000000861", name="Žilina", by="sklad")
    with pytest.raises(teach.AlreadyAnswered):
        teach.answer_customer(pg, qid, ean_edi="2000000000864", name="Martin", by="sklad")


def test_answer_customer_refuses_an_item_kind_question(pg):
    """The wrong endpoint pointed at the wrong kind must fail loudly, not silently
    misinterpret a product gtin as a customer ean_edi."""
    qid = _ask(pg)
    with pytest.raises(teach.NotACandidate):
        teach.answer_customer(pg, qid, ean_edi="SLI50", name="x", by="sklad")


def test_undo_on_a_customer_question_also_reverts_the_remembered_email(pg):
    """Adversarial review finding on PR #161: `undo` only ever cleared `item_memory` — a
    customer-kind question's real pick is remembered entirely OUTSIDE `teach.answer_
    customer` (httpapi.py's `snapshot.remember_customer_email`), so undo left the wrong
    sender-address binding live forever: every future order from that address would keep
    silently auto-resolving to the WRONG customer via `customer.resolve`'s `exact_email`
    rule at confidence 0.99, with no further review."""
    from app.orders import snapshot

    snapshot.import_snapshot(
        pg, "GTIN,Názov,doplnok\nG1,Rožok,\n",
        "Názov organizácie,EAN kód EDI,Obec,Ulica,E-mail\n"
        "Potraviny nie otraviny Žilina,2000000000861,Žilina,na bráne 4,eva@x.sk\n")
    qid = _ask_customer(pg)
    teach.answer_customer(pg, qid, ean_edi="2000000000861",
                          name="Potraviny nie otraviny Žilina", by="sklad")
    # exactly what httpapi.py's answer flow does right after teach.answer_customer
    snapshot.remember_customer_email(pg, "2000000000861", "zilina@farmeria.sk")
    snapshot.rebuild_from_overrides(pg)
    before = next(r for r in snapshot.customers_for_management(pg)
                 if r["ean_edi"] == "2000000000861")
    assert "zilina@farmeria.sk" in before["emails"]

    teach.undo(pg, qid)
    assert teach.get(pg, qid)["status"] == "open"
    after = next(r for r in snapshot.customers_for_management(pg)
                if r["ean_edi"] == "2000000000861")
    assert "zilina@farmeria.sk" not in after["emails"], \
        "undo must revert the remembered address, or every future order keeps mis-resolving"
    assert "eva@x.sk" in after["emails"], "the customer's ORIGINAL address must survive"


def test_undo_on_an_unknown_customer_answer_is_a_harmless_reopen(pg):
    """'neviem' never remembered anything — undoing it must not error, just reopen."""
    qid = _ask_customer(pg)
    teach.answer_customer(pg, qid, ean_edi="", name="", by="sklad")
    q = teach.undo(pg, qid)
    assert q["status"] == "open"


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


def test_a_new_question_also_reaches_odoo(pg):
    """#102 point 1: the warehouse reads Odoo, not always the dashboard, so a genuinely NEW
    question must ALSO be counted into the Odoo summary — not just written to
    order_questions. #139: the WORDING itself no longer appears in Odoo (that item-level
    detail moved to the linked /otazky page) — only the fact that a new question exists,
    plus the link to go answer it."""
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

    posted = []
    cfg = Config(pg_dsn="", data_dir="/tmp", orders_shadow=False, odoo_url="",
                 orion_host="")
    pipeline.run(pg, cfg, mail, sid, client=Client(), upload=lambda *a, **k: None,
                 post=lambda c, html, **kw: posted.append(html))
    assert len(posted) == 1, "one processed e-mail must post exactly one Odoo message (#139)"
    assert "jankove buchty" not in posted[0], \
        "the wording itself is item-level detail — it belongs on /otazky, not in Odoo"
    assert "1" in posted[0], "the new question must still be COUNTED in the summary"


def test_a_taught_wording_resolves_for_a_different_customer_with_no_model_call(pg):
    """#102's core acceptance: teach it once, a DIFFERENT customer's order resolves it for
    free — and asserts the stronger claim that it needed NO model call at all for that line
    (the product schema would raise if the engine ever asked)."""
    from app.config import Config
    from app.orders import pipeline, snapshot

    sid = snapshot.import_snapshot(
        pg, "GTIN,Sklad,Názov,doplnok\nSLI50,1,Šiška džemová 50g,\nVIA,1,Vianočka 400g,\n",
        "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
        f"Zákazník A,{EAN},Martin,U 1,,,sklad@a.sk\n"
        f"Zákazník B,{OTHER},Poprad,U 2,,,sklad@b.sk\n")
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('mq', 'ai_orders')")

    qid = _ask(pg, wording="Twister", candidates=[{"gtin": "VIA", "name": "Vianočka 400g"}])
    teach.answer(pg, qid, gtin="VIA", card="Vianočka 400g", by="sklad")

    pg.execute("INSERT INTO messages (message_id, category) VALUES ('mq2', 'ai_orders')")
    mail = {"message_id": "mq2", "subject": "Objednávka", "from_addr": "sklad@b.sk",
            "from_name": "B", "combined_text": "na 04.08.2026 prosím 5x Twister",
            "today": "2026-07-31"}

    class StrictClient:
        """Raises if ever asked to match the product line — decide_without_model must
        resolve it entirely from the global teaching."""
        last_prompt_hash = "p"

        def json_call(self, system, user, schema, name="result"):
            if name == "orders":
                return {"senderName": "B", "senderEmail": "sklad@b.sk", "companyName": "",
                        "isChangeRequest": False, "notes": "",
                        "orders": [{"orderNumber": "", "deliveryDate": "04.08.2026",
                                    "recipientGroup": "",
                                    "items": [{"name": "Twister", "quantity": 5, "unit": "ks",
                                               "sourceQuote": "5x Twister"}]}]}
            if name == "customer":
                return {"ean_edi": OTHER, "confidence": 0.99}
            raise AssertionError("the product line must resolve without a model call")

    cfg = Config(pg_dsn="", data_dir="/tmp", orders_shadow=False, odoo_url="",
                 orion_host="")
    result = pipeline.run(pg, cfg, mail, sid, client=StrictClient(),
                          upload=lambda *a, **k: None, post=lambda *a, **k: None)

    assert result["status"] == "ok"
    assert [i["gtin"] for i in result["items"]] == ["VIA"]
    assert [i["rule"] for i in result["items"]] == ["global_taught"]
    assert teach.open_questions(pg) == [], "no second question for the taught wording"


def test_a_customers_own_taught_nickname_overrides_the_global_one_end_to_end(pg):
    """The safety point: this customer answered the same wording differently for THEMSELVES,
    and their own answer must win over what everyone else gets."""
    from app.config import Config
    from app.orders import pipeline, snapshot

    sid = snapshot.import_snapshot(
        pg, "GTIN,Sklad,Názov,doplnok\nSLI50,1,Šiška džemová 50g,\nVIA,1,Vianočka 400g,\n",
        "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
        f"Zákazník A,{EAN},Martin,U 1,,,sklad@a.sk\n"
        f"Zákazník B,{OTHER},Poprad,U 2,,,sklad@b.sk\n")

    # customer A teaches the wording globally, VIA
    global_q = _ask(pg, ean=EAN, wording="Twister",
                    candidates=[{"gtin": "VIA", "name": "Vianočka 400g"}])
    teach.answer(pg, global_q, gtin="VIA", card="Vianočka 400g", by="sklad")
    # customer B has since taught THEIR OWN wording, SLI50 — their own nickname
    own_q = _ask(pg, ean=OTHER, customer_name="Zákazník B", message_id="mB",
                wording="Twister", candidates=[{"gtin": "SLI50", "name": "Šiška džemová 50g"}])
    teach.answer(pg, own_q, gtin="SLI50", card="Šiška džemová 50g", by="sklad")

    pg.execute("INSERT INTO messages (message_id, category) VALUES ('mq3', 'ai_orders')")
    mail = {"message_id": "mq3", "subject": "Objednávka", "from_addr": "sklad@b.sk",
            "from_name": "B", "combined_text": "na 04.08.2026 prosím 5x Twister",
            "today": "2026-07-31"}

    class Client:
        last_prompt_hash = "p"

        def json_call(self, system, user, schema, name="result"):
            if name == "orders":
                return {"senderName": "B", "senderEmail": "sklad@b.sk", "companyName": "",
                        "isChangeRequest": False, "notes": "",
                        "orders": [{"orderNumber": "", "deliveryDate": "04.08.2026",
                                    "recipientGroup": "",
                                    "items": [{"name": "Twister", "quantity": 5, "unit": "ks",
                                               "sourceQuote": "5x Twister"}]}]}
            if name == "customer":
                return {"ean_edi": OTHER, "confidence": 0.99}
            raise AssertionError("the product line must resolve without a model call")

    cfg = Config(pg_dsn="", data_dir="/tmp", orders_shadow=False, odoo_url="",
                 orion_host="")
    result = pipeline.run(pg, cfg, mail, sid, client=Client(), upload=lambda *a, **k: None,
                          post=lambda *a, **k: None)
    assert [i["gtin"] for i in result["items"]] == ["SLI50"]
    assert [i["rule"] for i in result["items"]] == ["human_taught"]


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


# --- review findings: a mis-click must be undoable ------------------------

def test_a_mistaken_answer_can_be_taken_back(pg):
    """Review finding: a taught wording is never asked about again, so a mis-click used to be
    permanent AND invisible — it would decide that customer's line forever, with no model call
    to second-guess it. Undo removes the mapping and reopens the question."""
    qid = _ask(pg)
    teach.answer(pg, qid, gtin="SLI90", card="Šiška džemová 90g", by="sklad")
    assert memory.resolve(pg, EAN, "Šiška").gtin == "SLI90"

    teach.undo(pg, qid)
    assert memory.resolve(pg, EAN, "Šiška") is None, "the wrong mapping is gone"
    assert [q["id"] for q in teach.open_questions(pg)] == [qid], "and it is asked again"

    teach.answer(pg, qid, gtin="SLI50", card="Šiška džemová 50g", by="sklad")
    assert memory.resolve(pg, EAN, "Šiška").gtin == "SLI50"


def test_undo_only_removes_what_a_human_taught(pg):
    """Deliveries the engine actually shipped are evidence and must survive an undo."""
    memory.remember(pg, EAN, "Šiška", "SLI90", "Šiška džemová 90g", "2026-07-01",
                    source="ship")
    qid = _ask(pg)
    teach.answer(pg, qid, gtin="SLI50", card="Šiška džemová 50g", by="sklad")
    teach.undo(pg, qid)
    rows = pg.execute("SELECT source FROM item_memory WHERE customer_ean = %s",
                      (EAN,)).fetchall()
    assert [r[0] for r in rows] == ["ship"]


def test_undo_also_removes_the_global_mapping_it_created(pg):
    """#102's acceptance bar: 'vrátiť' zmaže globálny zápis."""
    qid = _ask(pg)
    teach.answer(pg, qid, gtin="SLI50", card="Šiška džemová 50g", by="sklad")
    assert memory.resolve_global(pg, "Šiška") is not None

    teach.undo(pg, qid)
    assert memory.resolve_global(pg, "Šiška") is None, "undo must reach the global entry too"


def test_undo_never_removes_a_global_mapping_a_different_question_owns(pg):
    """Undoing customer B's redundant answer must not erase the global mapping that customer
    A's (still-standing) answer created — only ITS OWN question may retract it."""
    first = _ask(pg, ean=EAN)
    teach.answer(pg, first, gtin="SLI50", card="Šiška džemová 50g", by="sklad")

    second = _ask(pg, ean=OTHER, customer_name="Zákazník B", message_id="m2")
    teach.answer(pg, second, gtin="SLI90", card="Šiška džemová 90g", by="sklad")

    teach.undo(pg, second)
    assert memory.resolve_global(pg, "Šiška").gtin == "SLI50", \
        "the global row belongs to the FIRST question and must survive"


def test_the_dashboard_can_take_an_answer_back(pg):
    qid = _ask(pg)
    c = _dash(pg)
    c.post(f"/api/orders/question/{qid}/answer",
           json={"gtin": "SLI90", "card": "Šiška džemová 90g"})
    r = c.post(f"/api/orders/question/{qid}/undo")
    assert r.status_code == 200
    assert [q["id"] for q in c.get("/api/orders/questions").get_json()["items"]] == [qid]


def test_a_card_name_with_a_quote_does_not_break_the_page(pg):
    """The candidate list comes from the catalog sheet, so a name may contain a quote. The
    buttons must be built as DOM nodes, never spliced into an HTML string."""
    _ask(pg, wording='Chlieb "special"',
         candidates=[{"gtin": "SLI50", "name": 'Chlieb "special" 500g'}])
    d = _dash(pg).get("/api/orders/questions").get_json()
    assert d["items"][0]["candidates"][0]["name"] == 'Chlieb "special" 500g'


# --- the undo must be REACHABLE, not just implemented --------------------

def test_recently_taught_mappings_are_listed_so_the_undo_can_be_reached(pg):
    """Found while verifying 0.9.5 on the live box: an answered question leaves the open list,
    so the warehouse had nowhere to click 'vrátiť'. An undo nobody can reach is not an undo."""
    qid = _ask(pg)
    teach.answer(pg, qid, gtin="SLI90", card="Šiška džemová 90g", by="sklad")
    taught = teach.recently_taught(pg)
    assert [t["id"] for t in taught] == [qid]
    assert taught[0]["answer_card"] == "Šiška džemová 90g"
    assert taught[0]["wording"] == "Šiška"


def test_an_undone_mapping_leaves_the_taught_list(pg):
    qid = _ask(pg)
    teach.answer(pg, qid, gtin="SLI90", card="Šiška džemová 90g", by="sklad")
    teach.undo(pg, qid)
    assert teach.recently_taught(pg) == []


def test_the_dashboard_serves_the_taught_list(pg):
    qid = _ask(pg)
    c = _dash(pg)
    c.post(f"/api/orders/question/{qid}/answer",
           json={"gtin": "SLI50", "card": "Šiška džemová 50g"})
    d = c.get("/api/orders/taught").get_json()
    assert [t["wording"] for t in d["items"]] == ["Šiška"]


def _sklad(pg):
    """The warehouse's client: the signed link only, never a password."""
    import os

    from app.config import Config
    from app.httpapi import create_app, sklad_key
    cfg = Config(pg_dsn=os.environ["PG_TEST_DSN"], data_dir="/tmp", dash_password="pw",
                 secret_key="t")
    app = create_app(cfg)
    app.testing = True
    c = app.test_client()
    assert c.get("/sklad/" + sklad_key("t")).status_code == 302
    return c


def test_the_warehouse_teaches_through_the_link_without_logging_in(pg):
    """The user's ask (2026-07-31): answering must need no password. What must NOT follow
    from that is access to the mails — the port is on the open internet."""
    qid = _ask(pg)
    c = _sklad(pg)
    assert [q["wording"] for q in c.get("/api/orders/questions").get_json()["items"]] == ["Šiška"]

    r = c.post(f"/api/orders/question/{qid}/answer",
               json={"gtin": "SLI50", "card": "Šiška džemová 50g"})
    assert r.status_code == 200
    assert memory.resolve(pg, EAN, "Šiška").gtin == "SLI50"

    # a mis-click is still correctable from the same link
    assert c.get("/api/orders/taught").get_json()["items"][0]["answer_gtin"] == "SLI50"
    assert c.post(f"/api/orders/question/{qid}/undo").status_code == 200
    assert memory.resolve(pg, EAN, "Šiška") is None

    # and the link reaches nothing else
    assert c.get("/api/messages").status_code == 401
    assert c.get("/api/orders/spend").status_code == 401


def test_the_only_card_of_its_kind_is_no_longer_a_question():
    """User decision 2026-08-01 (#103): a product we make in exactly ONE gramáž is decided,
    not asked. Beh 2 raised a question for 'Kakaový slimák 130g' against our only 90g card —
    there was nothing to choose between, so the warehouse got noise instead of a question.

    What stays in the ask list is what a human can actually settle: a wording we could not
    place at all, a genuinely borderline model pick, and a weight overridden by history.
    """
    from app.orders.pipeline import ASK_THE_WAREHOUSE

    assert "unique_card" not in ASK_THE_WAREHOUSE
    assert {"unmatched", "llm_borderline", "history_weight"} <= set(ASK_THE_WAREHOUSE)
