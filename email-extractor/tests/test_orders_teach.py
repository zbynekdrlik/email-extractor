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
