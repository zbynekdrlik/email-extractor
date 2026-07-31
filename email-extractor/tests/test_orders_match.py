"""The matching ladder (#62) — one test per rung, plus the precedence pairs.

In n8n these rules live as nested `if`s across three Code nodes, so their precedence is
an accident of statement order and nothing tests it. That is the direct cause of
"improve one order type, break five others". Here the ladder is an ordered list of rules
and the tests below are the contract: the rung that decided, and which rung wins when two
could fire.

Every case is a real incident (dates in the comments) — that is deliberate: this file is
the regression net for exactly the failures that already cost real orders.
"""
import json

from app.orders import match
from app.orders.memory import Recalled

CATALOG = [
    {"gtin": "G50", "name": "Rožok štandart 50g", "alias": "rozok standard, žemľa 50g"},
    {"gtin": "G70", "name": "Rožok kváskový 70g", "alias": ""},
    {"gtin": "CHL", "name": "Chlieb pšenično-ražný rezaný 1000 gr",
     "alias": "domovina, nemocnica agel, eurotrading"},
    {"gtin": "SLI", "name": "Slimák kakaový 90g", "alias": ""},
    {"gtin": "VIA", "name": "Vianočka 400g", "alias": ""},
    {"gtin": "DAN90", "name": "Dánske pečivo tvarohové 90g", "alias": ""},
    {"gtin": "DAN60", "name": "Dánske pečivo makové 60g", "alias": ""},
]


def _decide(item, llm=None, mem=None, customer="Pekáreň s.r.o.", catalog=None):
    return match.decide(
        item_name=item,
        llm=llm or {},
        catalog=catalog if catalog is not None else CATALOG,
        recalled=mem,
        customer_name=customer,
    )


def _mem(gtin, card, days=3, unanimous=True):
    return Recalled(gtin=gtin, card=card, strength=days, unanimous=unanimous,
                    last_day="2026-07-28",
                    weight_override=unanimous and days >= 3)


# --- rung 5: the model is sure ------------------------------------------

def test_a_confident_model_match_is_taken():
    d = _decide("Rožok kváskový 70g", llm={"gtin": "G70", "confidence": 0.93})
    assert (d.gtin, d.rule, d.review) == ("G70", "llm_sure", False)


def test_the_model_percentage_scale_is_normalised():
    """The model sometimes answers 0-100 instead of 0-1."""
    d = _decide("Rožok kváskový 70g", llm={"gtin": "G70", "confidence": 93})
    assert d.rule == "llm_sure" and d.confidence == 0.93


# --- rung 7: borderline, passes but flagged -----------------------------

def test_a_borderline_match_passes_but_is_flagged_for_review():
    """0.85 was rejecting correct answers (the warehouse confirmed the model was right on
    3 of 5 disputed items), so 0.70-0.85 passes WITH a visible note."""
    d = _decide("vianočka", llm={"gtin": "VIA", "confidence": 0.73})
    assert (d.gtin, d.rule, d.review) == ("VIA", "llm_borderline", True)
    assert "73" in d.note


def test_below_the_gate_there_is_no_match_and_the_reason_is_kept():
    d = _decide("úplne cudzí produkt", llm={"gtin": "VIA", "confidence": 0.41})
    assert d.gtin is None and d.rule == "unmatched"
    assert "41" in d.note


def test_a_gtin_with_no_confidence_at_all_is_not_trusted():
    """conf==0 with a filled GTIN must not slip through unchecked (hole found in the
    2026-07-28 review of the n8n code)."""
    d = _decide("vianočka", llm={"gtin": "VIA", "confidence": 0})
    assert d.gtin is None and d.rule == "unmatched"


# --- rung 3: the alias names the ordering customer ----------------------

def test_an_alias_naming_the_customer_beats_the_confidence_gate():
    """2026-07-27, Nemocnica AGEL Levoča: the model picked the right card but scored
    0.82, the 0.85 gate dropped it, and a year-old working order failed."""
    d = _decide("pšenično-ražneho krájaneho chleba",
                llm={"gtin": "CHL", "confidence": 0.82},
                customer="Nemocnica AGEL Levoča")
    assert (d.gtin, d.rule, d.review) == ("CHL", "alias_customer", False)
    assert "alias" in d.note.lower()


# --- rung 1: the alias IS the customer's wording, weight included -------

def test_an_alias_that_states_the_weight_overrides_the_weight_guard():
    """2026-07-27, Domovina: the customer writes 'žemľa 50g' while our card is 'žemľa
    45g'. The warehouse wrote the customer's exact wording into the alias — a deliberate
    decision that must beat the weight guard."""
    d = _decide("žemľa 50g", llm={"gtin": "G50", "confidence": 0.6})
    assert (d.gtin, d.rule) == ("G50", "alias_exact_weight")


def test_an_alias_without_a_weight_does_not_switch_off_the_weight_guard():
    """Found by self-review of the n8n code: an alias like 'rozok standard' would
    otherwise disable the weight check for the whole group and bring back the Céder
    incident (Rožok 70g shipped as štandart 50g)."""
    d = _decide("rozok standard 70g", llm={"gtin": "G50", "confidence": 0.9})
    assert d.gtin is None and d.rule == "unmatched"
    assert "gram" in d.note.lower()


# --- rung 2 + 4: history ------------------------------------------------

def test_history_rescues_an_item_the_model_was_unsure_about():
    """2026-07-28, Savoneria 'rožok' / Céder 'Škoricový uzol': generic wording, several
    catalog variants, the model gives 0.64-0.81 and the gate drops an order that had
    been shipping for weeks. The answer is not in the email — it is in what we shipped."""
    d = _decide("dánske pečivo", llm={"gtin": "DAN60", "confidence": 0.64},
                mem=_mem("DAN90", "Dánske pečivo tvarohové 90g"))
    assert (d.gtin, d.rule) == ("DAN90", "history")
    assert "hist" in d.note.lower()


def test_history_also_rescues_an_item_the_model_did_not_match_at_all():
    d = _decide("dánske pečivo", llm={"gtin": None, "confidence": 0},
                mem=_mem("DAN90", "Dánske pečivo tvarohové 90g"))
    assert (d.gtin, d.rule) == ("DAN90", "history")


def test_a_unanimous_three_day_history_may_override_the_weight_guard():
    """User decision 2026-07-29: an unambiguous history with 3+ deliveries may ship a
    different gramáž — and it must always be written into the report."""
    d = _decide("Slimák kakaový 130g", llm={"gtin": "SLI", "confidence": 0.8},
                mem=_mem("SLI", "Slimák kakaový 90g", days=4))
    assert (d.gtin, d.rule, d.review) == ("SLI", "history_weight", True)
    assert "gram" in d.note.lower()


def test_a_weak_history_does_not_override_the_weight_guard():
    d = _decide("Slimák kakaový 130g", llm={"gtin": "SLI", "confidence": 0.8},
                mem=_mem("SLI", "Slimák kakaový 90g", days=2))
    assert d.rule == "unique_card", "no weight override, but it is the only card of its kind"


# --- rung 6: the only card of its kind ---------------------------------

def test_the_only_card_of_its_kind_ships_with_a_warning():
    """30.07.2026, PNO Poprad/Martin: the customer states a weight we simply do not have
    ('Kakaový slimák 130g' vs our only 90g). There is nothing to decide between — but the
    warehouse must see that the gramáž differs."""
    d = _decide("Kakaový slimák 130g", llm={"gtin": None, "confidence": 0.3})
    assert (d.gtin, d.rule, d.review) == ("SLI", "unique_card", True)
    assert "gram" in d.note.lower()


def test_a_single_word_order_never_uses_the_only_card_rule():
    """'rožok' alone has several catalog variants distinguished by weight; letting the
    rule fire there returns the Céder incident."""
    d = _decide("rožok", llm={"gtin": None, "confidence": 0.2})
    assert d.gtin is None and d.rule == "unmatched"


def test_a_wildly_different_weight_is_not_a_typo_and_is_refused():
    """'Chlieb 5 kg' against a 1000 g card is not a mistyped spec, it is the TOTAL
    quantity (the customer wants 5 loaves) — the warehouse must see it."""
    d = _decide("Chlieb pšenično-ražný rezaný 5 kg", llm={"gtin": None, "confidence": 0.2})
    assert d.gtin is None and d.rule == "unmatched"


# --- the weight guard itself -------------------------------------------

def test_a_mismatched_weight_is_refused_even_when_the_model_is_sure():
    """2026-07-24, Céder: 'Rožok 70g' was shipped as štandart 50g. Weight is identity."""
    d = _decide("Rožok 70g", llm={"gtin": "G50", "confidence": 0.95})
    assert d.gtin is None and d.rule == "unmatched"
    assert "70" in d.note and "50" in d.note


def test_a_multipack_weight_is_not_compared():
    catalog = CATALOG + [{"gtin": "MLK", "name": "Mlieko 6x1l", "alias": ""}]
    d = _decide("Mlieko 6x1l", llm={"gtin": "MLK", "confidence": 0.9}, catalog=catalog)
    assert d.gtin == "MLK"


def test_kilograms_and_grams_are_compared_in_the_same_unit():
    d = _decide("Chlieb pšenično-ražný rezaný 1 kg", llm={"gtin": "CHL", "confidence": 0.9})
    assert d.gtin == "CHL", "1 kg == 1000 gr"


# --- precedence pairs (the tests that catch 'fixed X, broke Y') ---------

def test_alias_weight_beats_the_only_card_rule():
    """Rungs 1 vs 6: both can fire on a weight mismatch; the hand-curated alias wins, so
    the reported reason is the alias, not the catalog coincidence."""
    catalog = [{"gtin": "ZEM45", "name": "Žemľa 45g", "alias": "žemľa 50g"}]
    d = _decide("žemľa 50g", llm={"gtin": "ZEM45", "confidence": 0.5}, catalog=catalog)
    assert d.rule == "alias_exact_weight"


def test_history_weight_beats_a_confident_model_pick_of_another_card():
    """Rungs 2 vs 5: the model is sure about a card whose weight disagrees; an unanimous
    history decides, and the item is flagged."""
    d = _decide("Slimák kakaový 130g", llm={"gtin": "G50", "confidence": 0.95},
                mem=_mem("SLI", "Slimák kakaový 90g", days=5))
    assert (d.gtin, d.rule) == ("SLI", "history_weight")


def test_the_customer_alias_beats_a_borderline_model_score():
    """Rungs 3 vs 7: both would pass the item, but only one of them is trustworthy — the
    alias, so the item must NOT be flagged for review."""
    d = _decide("krájaný chlieb pšenično-ražný", llm={"gtin": "CHL", "confidence": 0.78},
                customer="Nemocnica AGEL Levoča")
    assert (d.rule, d.review) == ("alias_customer", False)


# --- rung 8: the same wording resolved elsewhere in the same email ------

def test_the_same_wording_in_one_email_resolves_identically():
    """2026-07-30, CDR Lipová 6 + ČSB: the same wording and the same card scored 0.88
    (accepted) and 0.84/0.83 (rejected) in ONE email, and since an order needs all its
    items, the whole thing fell over. Not a discount on the gate — removing a coin flip
    on identical input."""
    accepted = _decide("rožok", llm={"gtin": "G50", "confidence": 0.88})
    rejected = _decide("rožok", llm={"gtin": "G50", "confidence": 0.4})
    assert rejected.rule == "unmatched", "the second line really did fall below the gate"
    out = match.apply_siblings([accepted, rejected])
    assert out[1].gtin == "G50" and out[1].rule == "sibling"
    assert out[1].review is True


def test_siblings_never_invent_a_match_for_a_different_wording():
    a = _decide("Chlieb 1000 g rezaný", llm={"gtin": "CHL", "confidence": 0.9})
    b = _decide("Vianočka 400g", llm={"gtin": None, "confidence": 0.1})
    out = match.apply_siblings([a, b])
    assert out[1].gtin is None


# --- candidates handed to the model ------------------------------------

def test_candidates_are_ranked_and_the_history_card_is_always_included():
    """The model can only pick from what it is shown, so a card confirmed by history
    must never fall out of the candidate list."""
    cands = match.candidates("dánske pečivo", CATALOG, customer_name="Pekáreň s.r.o.",
                             memory_gtin="DAN90", limit=3)
    assert [c["gtin"] for c in cands][:1] == ["DAN90"]
    assert len(cands) == 3


def test_candidates_prefer_a_card_whose_alias_names_the_customer():
    cands = match.candidates("chlieb pšenično-ražný", CATALOG,
                             customer_name="Nemocnica AGEL Levoča", limit=3)
    assert cands[0]["gtin"] == "CHL"


def test_the_decision_trace_names_the_rule_and_its_inputs():
    d = _decide("Rožok 70g", llm={"gtin": "G50", "confidence": 0.95})
    assert d.trace["rule"] == "unmatched"
    assert d.trace["llm"] == {"gtin": "G50", "confidence": 0.95,
                              "unknown_gtin": False}
    assert d.trace["weight"] == {"ordered": 70.0, "card": 50.0}


# --- a GTIN the catalog does not have (#81) --------------------------------

def test_a_gtin_that_is_not_in_the_catalog_is_not_a_match():
    """The model returned a code that exists nowhere in the catalog and the whole corpus run
    died with `'NoneType' object is not subscriptable`. An answer we cannot resolve to a real
    card is no answer: the item goes to the warehouse named, and the run continues."""
    catalog = [{"gtin": "G50", "name": "Rožok štandart 50g", "alias": ""}]
    d = match.decide(item_name="rožok", catalog=catalog,
                     llm={"gtin": "TOTALLY-MADE-UP", "confidence": 0.97}, recalled=None,
                     customer_name="Pekáreň s.r.o.")
    assert not d.gtin and d.rule == "unmatched"
    assert "TOTALLY-MADE-UP" in json.dumps(d.trace, ensure_ascii=False)


def test_a_borderline_answer_with_an_unknown_gtin_also_does_not_match():
    catalog = [{"gtin": "G50", "name": "Rožok štandart 50g", "alias": ""}]
    d = match.decide(item_name="rožok", catalog=catalog,
                     llm={"gtin": "NOPE", "confidence": 0.75}, recalled=None,
                     customer_name="")
    assert not d.gtin and d.rule == "unmatched"
