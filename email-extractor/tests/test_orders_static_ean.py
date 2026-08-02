"""EAN resolution for the n8n **Static auto orders** `generator` node (#49).

Before this module, `generator.js` matched a partner's item wording against two
hand-maintained JS maps only (`PRODUCT_EAN_BY_CODE`, `PRODUCT_EAN_BY_NAME`) — a new
own-code with a wording not yet in the list silently dropped the line from the EDI
(incident 2026-07-28, KARMEN order 020260851324, code P000534 = "Pečivo toskánske 60g
Granč" — the same product already listed under code 9098). This is a 1:1 port of the
catalog-lookup half of `generator.js`'s `getProductEAN()` — n8n's JS Code node cannot
run in this repo's CI, so this module is the pinned proof the algorithm is correct;
the n8n node is hand-kept in sync with it.
"""
from app.orders import static_ean

CATALOG = [
    {"gtin": "8588001805609", "name": "Chlieb pšenično-ražný 800gr", "alias": ""},
    {"gtin": "8588001805623", "name": "Chlieb pšenično-ražný 500gr", "alias": ""},
    {"gtin": "8588001805692", "name": "Toskánske pečivo 60 gr", "alias": ""},
    {"gtin": "8588001805722", "name": "Cereálna kocka syrová 60gr", "alias": "syrova kocka"},
    {"gtin": "8588001805630", "name": "Cereálna kocka 60 gr", "alias": ""},
    {"gtin": "8588001805777", "name": "Lupačka 75gr", "alias": ""},
    {"gtin": "9999900000001", "name": "Croissant vanilkový 90g", "alias": ""},
    {"gtin": "9999900000002", "name": "Croissant čokoládový 90g", "alias": ""},
    # Mirrors the REAL live catalog_snapshot (verified 2026-08-01): two weight
    # variants of the same single-word product, each reducing to ONE core token.
    {"gtin": "9999900000003", "name": "Lupačka 60g", "alias": ""},
]


# --- catalog_match: the core lookup --------------------------------------

def test_exact_catalog_name_match():
    hit = static_ean.catalog_match("Toskánske pečivo 60 gr", CATALOG)
    assert hit["gtin"] == "8588001805692"


def test_diacritics_and_case_are_ignored():
    hit = static_ean.catalog_match("toskanske pecivo 60 GR", CATALOG)
    assert hit["gtin"] == "8588001805692"


def test_alias_exact_match():
    hit = static_ean.catalog_match("syrova kocka", CATALOG)
    assert hit["gtin"] == "8588001805722"


def test_karmen_incident_new_own_code_new_wording_resolves_via_catalog():
    """The 2026-07-28 incident this ticket exists for: KARMEN's own code P000534 is not
    in any manual map, but the wording IS the catalog card (diacritics + case aside)."""
    hit = static_ean.catalog_match("PECIVO TOSKANSKE 60g GRANC", CATALOG)
    assert hit["gtin"] == "8588001805692"


def test_unambiguous_containment_match_with_agreeing_weight():
    hit = static_ean.catalog_match("Lupačka 75gr SLOVNORMAL", CATALOG)
    assert hit["gtin"] == "8588001805777"


def test_weight_mismatch_is_never_guessed():
    """A different gramáž is a different product — never resolved, even though the
    wording otherwise matches ('gramáž ako identita', match.py)."""
    hit = static_ean.catalog_match("Chlieb pšenično-ražný 900gr", CATALOG)
    assert hit is None


def test_ambiguous_wording_is_never_guessed():
    """'Croissant 90g' alone matches two different flavours — refuse rather than pick
    one at random."""
    hit = static_ean.catalog_match("Croissant 90g", CATALOG)
    assert hit is None


def test_unrelated_wording_has_no_match():
    hit = static_ean.catalog_match("Neexistujuci vyrobok XYZ", CATALOG)
    assert hit is None


def test_two_weight_variants_of_the_same_word_pick_the_agreeing_one():
    """The real catalog has this exact shape (Lupačka 60g vs 75g) — the weight, not
    the card's own token count, is what disambiguates."""
    assert static_ean.catalog_match("Lupačka 60g", CATALOG)["gtin"] == "9999900000003"
    assert static_ean.catalog_match("Lupačka 75gr", CATALOG)["gtin"] == "8588001805777"


def test_no_stated_weight_refuses_to_pick_between_the_two_variants():
    """No weight on the order -> weightsAgree() can't rule either card out ->
    ambiguous -> refuse, never guess which gramáž."""
    assert static_ean.catalog_match("Lupačka", CATALOG) is None


def test_KNOWN_TRADEOFF_single_core_token_card_can_absorb_an_extra_qualifier_word():
    """Documented, accepted-risk boundary (review finding on #49, 2026-08-01).

    catalog_match's rung 3 requires 2+ core tokens on the ORDERED wording but NOT on
    the catalog card (`app.orders.match.unique_core_card` dropped its OWN >= 2 minimum
    on both sides, #140 — the two modules now agree that a single-core-token CARD is
    fine; they still differ on the WORDING side, where this module keeps its >= 2
    floor and match.py has none at all) — because the real catalog has legitimate
    single-word products distinguished only by weight (Lupačka 60g/75g above; see the
    #49 design comment on the issue). The consequence: an extra qualifier word that is
    not itself a distinguishing catalog word ("makova") does not block the match as long as the
    weight agrees — even though "Lupačka makova" is not actually a catalog product.
    Pinned here so any future change to this boundary is a deliberate, reviewed diff
    instead of silent drift.
    """
    hit = static_ean.catalog_match("Lupačka makova 75gr", CATALOG)
    assert hit is not None
    assert hit["gtin"] == "8588001805777"


def test_empty_description_has_no_match():
    assert static_ean.catalog_match("", CATALOG) is None
    assert static_ean.catalog_match(None, CATALOG) is None


def test_empty_catalog_has_no_match():
    assert static_ean.catalog_match("Toskánske pečivo 60 gr", []) is None


def test_whitespace_only_description_has_no_match():
    assert static_ean.catalog_match("   ", CATALOG) is None


def test_catalog_card_with_no_usable_core_tokens_is_skipped_not_matched():
    """A card whose name has no word >= 3 chars long (empty/blank/too-short) must
    never be treated as a hit — it is skipped, not silently matched."""
    weird_catalog = CATALOG + [{"gtin": "BLANK", "name": "  ", "alias": ""}]
    hit = static_ean.catalog_match("Lupačka makova 75gr", weird_catalog)
    assert hit["gtin"] == "8588001805777"


# --- small pure helpers: pin the edge branches directly ------------------

def test_weight_grams_ignores_multipack_notation():
    """"6x1l" states a TOTAL quantity, not a unit weight — must not be read as 6 or 1."""
    assert static_ean.weight_grams("Mlieko 6x1l") is None


def test_weights_agree_when_one_side_states_no_weight():
    """Missing weight information on either side is not a disagreement — the caller
    (catalog_match) relies on this to let a weightless catalog card or a weightless
    order wording through, rather than refusing on absence of data."""
    assert static_ean.weights_agree(None, 60.0) is True
    assert static_ean.weights_agree(60.0, None) is True
    assert static_ean.weights_agree(None, None) is True


# --- resolve_ean: the full precedence, as generator.js's getProductEAN ---

def test_ean_on_the_document_wins_over_everything():
    item = {"ean": "1234567890123", "code": "X", "description": "Toskánske pečivo 60 gr"}
    assert static_ean.resolve_ean(item, CATALOG) == "1234567890123"


def test_manual_code_map_is_an_override_checked_before_catalog():
    item = {"code": "9098", "description": "Neco ine"}
    code_map = {"9098": "OVERRIDE-EAN"}
    assert static_ean.resolve_ean(item, CATALOG, code_map=code_map) == "OVERRIDE-EAN"


def test_manual_name_map_is_an_override_checked_before_catalog():
    item = {"code": "P000534", "description": "PECIVO TOSKANSKE 60 GRANC"}
    name_map = {"PECIVO TOSKANSKE 60": "OVERRIDE-EAN"}
    assert static_ean.resolve_ean(item, CATALOG, name_map=name_map) == "OVERRIDE-EAN"


def test_falls_through_to_catalog_when_no_manual_entry_matches():
    """The regression this ticket fixes: an unmapped code with a mappable wording must
    resolve via the catalog instead of returning None."""
    item = {"code": "P000534", "description": "PECIVO TOSKANSKE 60g GRANC"}
    assert static_ean.resolve_ean(item, CATALOG, code_map={}, name_map={}) == "8588001805692"


def test_unresolvable_item_returns_none_never_guesses():
    item = {"code": "P999999", "description": "Uplne neznamy vyrobok"}
    assert static_ean.resolve_ean(item, CATALOG) is None
