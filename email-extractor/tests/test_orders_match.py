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
    # A second Vianočka card, mirroring the LIVE catalog (#140 diagnosis, 2026-08-02:
    # "vianočka -> 2 karty"). Without it, "vianočka" would look falsely unique in this
    # fixture once unique_core_card() no longer requires 2+ core tokens, and would
    # silently hijack the borderline/no-confidence tests below (#140's own case).
    {"gtin": "VIA300", "name": "Vianočka 300gr", "alias": ""},
    {"gtin": "DAN90", "name": "Dánske pečivo tvarohové 90g", "alias": ""},
    {"gtin": "DAN60", "name": "Dánske pečivo makové 60g", "alias": ""},
]

# The Gazdovský trh catalog slice (#103): one card per kind, so the weight the customer
# states is the ONLY thing that differs — and one flavour we simply do not make.
GT_CATALOG = [
    {"gtin": "PIS", "name": "Croissant pistácia 130g", "alias": ""},
    {"gtin": "CUC", "name": "Dánske pečivo 100g čučoriedka", "alias": ""},
    {"gtin": "SPA", "name": "Špaldový kváskový chlieb 700g", "alias": ""},
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
    assert d.review is False, "one card of that kind is not a question (#103)"


# --- rung 6: the only card of its kind ---------------------------------

def test_the_only_card_of_its_kind_ships_without_asking():
    """30.07.2026, PNO Poprad/Martin: the customer states a weight we simply do not have
    ('Kakaový slimák 130g' vs our only 90g).

    User decision 2026-08-01, going through the disputed runs: "keď sú to produkty, ktoré
    majú u nás iba jednu gramáž, tak automaticky vyber ten". There is nothing to decide
    between, so asking the warehouse is noise — but the differing gramáž still belongs in
    the note, because that is what the Odoo report shows.
    """
    d = _decide("Kakaový slimák 130g", llm={"gtin": None, "confidence": 0.3})
    assert (d.gtin, d.rule, d.review) == ("SLI", "unique_card", False)
    assert "130" in d.note and "90" in d.note, "the overridden gramáž must stay in the note"


def test_a_borderline_model_pick_is_certain_when_it_is_the_only_card_of_its_kind():
    """Beh 26, Gazdovský trh: 'Croissant pistácia 120g' scored 0.70-0.85 against our only
    pistachio croissant (130g) and went out flagged, four times in one order.

    The model's doubt is about the weight, and the weight is not in dispute: there is no
    other pistachio croissant to confuse it with. Same user decision as above.
    """
    d = _decide("Croissant pistácia 120g", llm={"gtin": "PIS", "confidence": 0.78},
                catalog=GT_CATALOG)
    assert (d.gtin, d.rule, d.review) == ("PIS", "unique_card", False)


def test_a_flavour_we_do_not_make_is_never_shipped_as_the_only_card():
    """Beh 26: 'Dánske pečivo s jahodami' against our only 'Dánske pečivo 100g čučoriedka'.

    This is the line the whole rule has to get right. A different WEIGHT is a typo we can
    absorb; a different FLAVOUR is a different product, and shipping blueberry for
    strawberry is worse than asking. User, 2026-08-01: "keď oni chcú s jahodami a my taký
    produkt nemáme, tak to musí ísť notifikácia do Odoo a na nástenku".
    """
    d = _decide("Dánske pečivo s jahodami", llm={"gtin": "CUC", "confidence": 0.78},
                catalog=GT_CATALOG)
    assert d.rule != "unique_card", "a different flavour is not the same product"
    assert d.review is True, "it must reach a human"


def test_the_only_espaldovy_bread_is_taken_despite_the_stated_weight():
    """Beh 2, PNO Poprad: 'Chlieb kváskový špaldový 500g' vs our only špaldový (700g).
    Verified against the live catalog on 2026-08-01: 127 cards, exactly one špaldový."""
    d = _decide("Chlieb kváskový špaldový 500g", llm={"gtin": None, "confidence": 0.2},
                catalog=GT_CATALOG)
    assert (d.gtin, d.rule, d.review) == ("SPA", "unique_card", False)


def test_a_single_word_order_with_several_catalog_candidates_still_asks():
    """User decision 2026-08-02 (#140): the count of REAL catalog candidates decides, not
    the number of words in the wording — but 'rožok' alone still has TWO catalog variants
    (štandart 50g, kváskový 70g) distinguished only by weight, so it must still ask.
    Letting a single-word rule fire here unconditionally is exactly the Céder incident
    (2026-07-24: 'Rožok 70g' shipped as štandart 50g) — this pins that it cannot recur."""
    d = _decide("rožok", llm={"gtin": None, "confidence": 0.2})
    assert d.gtin is None and d.rule == "unmatched"


def test_a_single_word_order_passes_when_the_catalog_has_exactly_one_candidate():
    """User decision 2026-08-02 (#140), verbatim: "ak napisu babovka a my mame iba jednu
    babovku medzi produktami tak ma vybrat... to je jedno ze zakaznik nenapisal gramaz".
    'Slimák' alone has exactly ONE matching card in the catalog — nothing to ask about."""
    d = _decide("Slimák", llm={"gtin": None, "confidence": 0.2})
    assert (d.gtin, d.rule, d.review) == ("SLI", "unique_card", False)


def test_KNOWN_TRADEOFF_a_single_core_token_card_can_absorb_an_extra_unmatched_qualifier():
    """Documented, accepted-risk boundary (code review on #140, 2026-08-02) — the same
    shape `app.orders.static_ean`'s identically-named test already accepts for its own,
    independent matcher (see that test's docstring for the full reasoning).

    Before #140, unique_core_card() required >= 2 core tokens on the CARD side, so a
    single-word card like 'Bageta 250g' (core tokens: {'bageta'}) could never be reached
    by this rung at all — the guard made this scenario structurally impossible. Now that
    the floor is >= 1 token per side, a wording that ADDS an extra qualifier word the
    catalog does not distinguish ('Bageta cesnaková', when the only bageta card is the
    unqualified 'Bageta 250g') still resolves: the extra word is absorbed by the SAME
    superset-containment branch ("all(t in want for t in have)") that a 2+-token card
    could already use to absorb an extra qualifier BEFORE this change — this is not a
    new code path, only a card shape that could not reach it before. No weight is
    stated on either side, so UNIQUE_MAX_RATIO cannot rule it out.

    Accepted for the same reason static_ean.py accepts it: a real single-product card
    has no OTHER variant to confuse it with, so an unrecognised qualifier word is lower-
    value information than "there is exactly one card of this kind." Pinned here so any
    future tightening of this boundary is a deliberate, reviewed diff, not silent drift.
    """
    catalog = [{"gtin": "BAG", "name": "Bageta 250g", "alias": ""}]
    d = _decide("Bageta cesnaková", llm={"gtin": None, "confidence": 0.2}, catalog=catalog)
    assert (d.gtin, d.rule, d.review) == ("BAG", "unique_card", False)


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


# --- the stored question must offer the engine's own candidate (#147) --------

def test_proposed_gtin_reads_the_raw_llm_answer_for_a_rejected_unmatched_decision():
    """`unmatched` clears `decision.gtin` (nothing was confirmed), but the raw model
    answer that got rejected is still in the trace — the question's own note names it."""
    d = _decide("Rožok 70g", llm={"gtin": "G50", "confidence": 0.2})
    assert d.rule == "unmatched" and d.gtin is None
    assert match.proposed_gtin(d) == "G50"


def test_proposed_gtin_reads_decision_gtin_for_llm_borderline():
    # two Dánske pečivo cards match the generic wording -> not unique, borderline decides
    d = _decide("Dánske pečivo 88g", llm={"gtin": "DAN90", "confidence": 0.75})
    assert d.rule == "llm_borderline"
    assert match.proposed_gtin(d) == d.gtin == "DAN90"


def test_candidates_for_question_heads_the_list_with_the_proposed_candidate():
    """#147: a scoring quirk (here: SYNONYMS scores every 'Rožok' sibling above the model's
    actual pick) can push the model's own answer past the [:6] cutoff computed BEFORE the
    decision existed. The stored question must always put it first regardless."""
    d = _decide("Rožok 70g", llm={"gtin": "G50", "confidence": 0.2})
    truncated = [{"gtin": "G70", "name": "Rožok kváskový 70g"},
                {"gtin": "SLI", "name": "Slimák kakaový 90g"}]   # G50 already fell out
    out = match.candidates_for_question(truncated, CATALOG, d)
    assert [c["gtin"] for c in out] == ["G50", "G70", "SLI"]


def test_candidates_for_question_deduplicates_when_already_present():
    d = _decide("Dánske pečivo 88g", llm={"gtin": "DAN90", "confidence": 0.75})
    already_first = [{"gtin": "DAN90", "name": "Dánske pečivo tvarohové 90g"},
                     {"gtin": "SLI", "name": "Slimák kakaový 90g"}]
    out = match.candidates_for_question(already_first, CATALOG, d)
    assert [c["gtin"] for c in out] == ["DAN90", "SLI"]


def test_proposed_gtin_tolerates_a_trace_with_no_llm_key_at_all():
    """A `decide_without_model()` free rung sets `trace={"llm": None, ...}` — the key is
    PRESENT but its VALUE is None, which `dict.get(key, default)` does NOT fall back on
    (only a MISSING key does). `proposed_gtin` must not crash on it even though no current
    ASK_THE_WAREHOUSE decision actually reaches here with that shape."""
    d = match.Decision(item_name="x", gtin=None, card="", confidence=1.0, rule="catalog_name",
                       note="", trace={"rule": "catalog_name", "llm": None, "free": True})
    assert match.proposed_gtin(d) == ""


def test_candidates_for_question_is_a_no_op_when_nothing_was_proposed():
    d = _decide("Torta", llm={"gtin": None, "confidence": 0.0})
    assert d.gtin is None and match.proposed_gtin(d) == ""
    cands = [{"gtin": "SLI", "name": "Slimák kakaový 90g"}]
    assert match.candidates_for_question(cands, CATALOG, d) == cands


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
    card is no answer: the item goes to the warehouse named, and the run continues.

    Two 'rožok' variants (#140) so this stays a test of the unknown-GTIN path, not an
    accidental single-candidate unique_card match."""
    catalog = [{"gtin": "G50", "name": "Rožok štandart 50g", "alias": ""},
               {"gtin": "G70", "name": "Rožok kváskový 70g", "alias": ""}]
    d = match.decide(item_name="rožok", catalog=catalog,
                     llm={"gtin": "TOTALLY-MADE-UP", "confidence": 0.97}, recalled=None,
                     customer_name="Pekáreň s.r.o.")
    assert not d.gtin and d.rule == "unmatched"
    assert "TOTALLY-MADE-UP" in json.dumps(d.trace, ensure_ascii=False)


def test_a_borderline_answer_with_an_unknown_gtin_also_does_not_match():
    """Two 'rožok' variants (#140), same reason as the test above."""
    catalog = [{"gtin": "G50", "name": "Rožok štandart 50g", "alias": ""},
               {"gtin": "G70", "name": "Rožok kváskový 70g", "alias": ""}]
    d = match.decide(item_name="rožok", catalog=catalog,
                     llm={"gtin": "NOPE", "confidence": 0.75}, recalled=None,
                     customer_name="")
    assert not d.gtin and d.rule == "unmatched"


# --- #86: rungs that need no model call at all ---------------------------
#
# 89 % of the engine's model spend is one call per ordered line, made unconditionally —
# even when the wording IS a catalog card. Measured on the 30-email corpus (2026-07-31):
# 115 of 473 lines are answerable with no call, and in 115 of 115 the free answer was the
# SAME card the model-driven ladder shipped. So this is not a cheaper guess; it is the
# same answer without paying for it.

def test_the_wording_is_literally_a_catalog_card():
    d = match.decide_without_model("Rožok kváskový 70g", CATALOG)
    assert d is not None
    assert (d.gtin, d.rule, d.review, d.confidence) == ("G70", "catalog_name", False, 1.0)


def test_the_wording_is_literally_a_card_alias():
    """The warehouse wrote the customer's wording into the alias column — that is a human
    mapping already, and asking a model to confirm it is paid-for redundancy."""
    d = match.decide_without_model("žemľa 50g", CATALOG)
    assert d is not None
    assert (d.gtin, d.rule) == ("G50", "alias_exact")


def test_an_unanimous_history_of_three_days_needs_no_model():
    d = match.decide_without_model("šiška", CATALOG, recalled=_mem("SLI", "Slimák kakaový 90g"))
    assert d is not None
    assert (d.gtin, d.rule) == ("SLI", "history_sure")


def test_a_thin_history_is_not_enough_to_skip_the_model():
    """Two deliveries is a coincidence, not a mapping — the ladder's own bar is 3 days."""
    assert match.decide_without_model(
        "šiška", CATALOG, recalled=_mem("SLI", "Slimák kakaový 90g", days=2)) is None
    assert match.decide_without_model(
        "šiška", CATALOG, recalled=_mem("SLI", "Slimák kakaový 90g", unanimous=False)) is None


def test_an_ordinary_wording_still_goes_to_the_model():
    assert match.decide_without_model("nieco co v katalogu nie je", CATALOG) is None
    assert match.decide_without_model("Dánske pečivo", CATALOG) is None


def test_a_wording_matching_two_cards_is_never_decided_for_free():
    ambiguous = CATALOG + [{"gtin": "G70B", "name": "Rožok kváskový 70g", "alias": ""}]
    assert match.decide_without_model("Rožok kváskový 70g", ambiguous) is None


def test_a_disagreeing_weight_is_never_decided_for_free():
    """"Kakaový slimák 130g" against a 90 g card is exactly the line a human must rule on
    (measured corpus, 2026-07-31) — the free path must not swallow it."""
    aliased = [dict(CATALOG[3], alias="kakaovy slimak 130g")] + CATALOG[:3]
    assert match.decide_without_model("kakaovy slimak 130g", aliased) is None
    assert match.decide_without_model(
        "šiška 130g", CATALOG, recalled=_mem("SLI", "Slimák kakaový 90g")) is None


def test_the_free_decision_says_in_slovak_why_it_was_certain():
    for wording, recalled in (("Rožok kváskový 70g", None), ("žemľa 50g", None),
                              ("šiška", _mem("SLI", "Slimák kakaový 90g"))):
        d = match.decide_without_model(wording, CATALOG, recalled=recalled)
        assert d.note and d.note[0].isupper(), wording
        assert d.trace.get("rule") == d.rule
        assert d.trace.get("llm") is None, "no model was asked, so nothing to record"


# --- rung "global_taught" (#102): a wording taught for EVERY customer, not just one -------
#
# "Twister" is not one buyer's private nickname — it is a product name, so once the
# warehouse says what it means the answer must be free for every OTHER customer too, and
# with no model call. Below the customer's own signals (taught mapping, catalog-certain
# rungs, this customer's own unanimous history), above the model.

def _global(gtin, card):
    return Recalled(gtin=gtin, card=card, strength=1, unanimous=True, last_day="",
                    weight_override=True)


def test_a_globally_taught_wording_decides_with_no_model_call():
    d = match.decide_without_model("Twister", CATALOG,
                                   global_recalled=_global("VIA", "Vianočka 400g"))
    assert d is not None
    assert (d.gtin, d.rule, d.review) == ("VIA", "global_taught", False)
    assert "Twister" in d.note and "Vianočka 400g" in d.note


def test_a_globally_taught_wording_overrides_the_weight_guard():
    """Like a per-customer taught mapping, this IS a human decision — the guard exists to
    stop the MODEL guessing, not to overrule the warehouse."""
    d = match.decide_without_model("Twister 90g", CATALOG,
                                   global_recalled=_global("VIA", "Vianočka 400g"))
    assert d is not None and (d.gtin, d.rule) == ("VIA", "global_taught")


def test_the_customers_own_taught_mapping_beats_the_global_one():
    """The whole safety point of #102: a customer's OWN nickname is stronger evidence than
    a generic crowd answer and must win."""
    mine = Recalled(gtin="SLI", card="Slimák kakaový 90g", strength=1, unanimous=True,
                    last_day="2026-07-28", weight_override=True, human=True)
    d = match.decide_without_model("Twister", CATALOG, recalled=mine,
                                   global_recalled=_global("VIA", "Vianočka 400g"))
    assert (d.gtin, d.rule) == ("SLI", "human_taught")


def test_this_customers_own_unanimous_history_beats_the_global_one():
    """Real shipping evidence for THIS customer outranks a global crowd answer too — global
    is the LAST resort among the no-model rungs, not a shortcut past real evidence."""
    d = match.decide_without_model("Twister", CATALOG,
                                   recalled=_mem("SLI", "Slimák kakaový 90g"),
                                   global_recalled=_global("VIA", "Vianočka 400g"))
    assert (d.gtin, d.rule) == ("SLI", "history_sure")


def test_a_catalog_certain_match_beats_a_stale_global_answer():
    """If the catalog gains a real card for the wording later, catalog truth wins over
    whatever a human guessed globally before that card existed."""
    d = match.decide_without_model("Rožok kváskový 70g", CATALOG,
                                   global_recalled=_global("VIA", "Vianočka 400g"))
    assert (d.gtin, d.rule) == ("G70", "catalog_name")


def test_with_no_global_teaching_nothing_changes():
    assert match.decide_without_model("Twister", CATALOG) is None
    assert match.decide_without_model("Twister", CATALOG, global_recalled=None) is None
