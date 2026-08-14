"""The DL matching ladder (#202, DL migration F3) — supplier scoring/decision (R60-R61) and
the item candidate scoring + post-match gate ladder (R62-R76). Synthetic fixtures only, no
real customer/supplier data (this repo is public — see `.claude/rules/orders-corpus.md`).
"""
from app.orders import dl_match, dl_memory

CATALOG = [
    {"gtin": "G1", "name": "Múka hladká T512 25kg", "doplnok": "", "mass": 25.0,
     "sklad": "100", "cena": 12.5},
    {"gtin": "G2", "name": "Múka hladká T512 50kg", "doplnok": "", "mass": 50.0,
     "sklad": "100", "cena": 24.0},
    {"gtin": "G3", "name": "Cukor kryštálový 25kg", "doplnok": "cukor krupica", "mass": 25.0,
     "sklad": "100", "cena": 18.0},
    {"gtin": "G4", "name": "Olej repkový 10l", "doplnok": "", "mass": None,
     "sklad": "200", "cena": 15.0},
]

SUPPLIERS = [
    {"ean_edi": "S1", "name": "Mlyn Vrbovce s.r.o.", "emails": ["obchod@mlynvrbovce.sk"],
     "city": "Vrbovce"},
    {"ean_edi": "S2", "name": "Cukrovar Sereď a.s.", "emails": ["info@cukrovar-sered.sk"],
     "city": "Sereď"},
]


def _recall(gtin="", card="", strength=0, unanimous=False, last_day="", weight_override=False,
           human=False):
    return dl_memory.Recalled(gtin=gtin, card=card, strength=strength, unanimous=unanimous,
                              last_day=last_day, weight_override=weight_override, human=human)


# --- w_eq (R64) --------------------------------------------------------------------------

def test_w_eq_substring_match():
    assert dl_match.w_eq("muka", "pomuka") is True


def test_w_eq_common_stem_catches_slovak_declension():
    assert dl_match.w_eq("cukoru", "cukor") is True     # shared prefix >= max(4, len-2)
    assert dl_match.w_eq("cukrom", "cukor") is False    # internal stem change, known limit


def test_w_eq_short_words_need_the_four_char_floor():
    assert dl_match.w_eq("olej", "olejom") is True
    assert dl_match.w_eq("led", "les") is False         # both too short to share a real stem


def test_w_eq_empty_never_matches():
    assert dl_match.w_eq("", "muka") is False
    assert dl_match.w_eq("muka", "") is False


# --- R62 OCR fix --------------------------------------------------------------------------

def test_ocr_name_fix_corrects_the_known_scan_misread():
    assert dl_match.apply_ocr_fix("Cerealna kocka sypana") == "Cereálna kocka syrová 60g"


def test_ocr_name_fix_leaves_unrelated_names_untouched():
    assert dl_match.apply_ocr_fix("Múka hladká 25kg") == "Múka hladká 25kg"


# --- R63 normalization -----------------------------------------------------------------

def test_norm_strips_diacritics_case_and_weight():
    assert dl_match.mass_grams("Cukor 25kg") == 25000.0
    assert "25" not in dl_match._norm("Cukor kryštálový 25kg")


def test_norm_drops_brand_and_packaging_words_but_keeps_slash_as_two_words():
    n = dl_match._norm("Čučoriedka/PL Bertosi platky")
    assert "cucoriedka" in n and "pl" in n
    assert "bertosi" not in n and "platky" not in n


# --- item candidate scoring (R65) -------------------------------------------------------

def test_alias_contained_in_wording_scores_99():
    row = dl_match.candidates("cukor krupica biely", CATALOG)
    top = row[0]
    assert top["gtin"] == "G3" and top["score"] == 99.0


def test_exact_normalized_name_scores_98():
    row = dl_match.candidates("Múka hladká T512 25kg", CATALOG)
    top = row[0]
    assert top["gtin"] == "G1" and top["score"] == 98.0


def test_memory_gtin_is_floored_at_90_not_overridden():
    # G2 (50kg) scores well below 90 on its own merits against a 25kg wording (weight text
    # stripped by _norm, so it still shares "muka hladka t512" — substring hit, not the floor).
    row = dl_match.candidates("Múka hladká T512 25kg", CATALOG, memory_gtin="G4")
    g4 = next(c for c in row if c["gtin"] == "G4")
    assert g4["score"] == 90.0, "G4 has zero natural overlap with the wording — pure floor"


def test_memory_floor_never_lowers_an_already_higher_natural_score():
    row = dl_match.candidates("Múka hladká T512 25kg", CATALOG, memory_gtin="G1")
    g1 = next(c for c in row if c["gtin"] == "G1")
    assert g1["score"] == 98.0, "exact-name score (98) must not be dragged down to the floor (90)"


def test_candidates_limited_to_top_n():
    big_catalog = CATALOG * 5
    assert len(dl_match.candidates("cokolvek", big_catalog, limit=3)) == 3


def test_candidates_apply_ocr_fix_before_scoring():
    row = dl_match.candidates("Cerealna kocka sypana", [
        {"gtin": "G9", "name": "Cereálna kocka syrová 60g", "doplnok": ""}])
    assert row[0]["score"] == 98.0


def test_card_name_substring_of_wording_scores_70():
    row = dl_match.candidates("Múka hladká T512 25kg extra jemná",
                              [{"gtin": "GX", "name": "Múka hladká T512 25kg", "doplnok": ""}])
    assert row[0]["score"] == 70.0


def test_wording_substring_of_card_name_scores_65():
    row = dl_match.candidates("Cukor", [{"gtin": "GX", "name": "Cukor kryštálový biely 25kg",
                                        "doplnok": ""}])
    assert row[0]["score"] == 65.0


# --- #225: short filler words must never drive/dilute the word-overlap score -----------
#
# Root cause verified live against the real 491-row DL catalog (issue #225 comment): a
# Slovak preposition like "s" ("with") is its own token after _norm(); w_eq()'s cheap
# `a in b or b in a` substring check then treats a bare 1-2 char token as a "match" against
# ANY candidate word that happens to contain that single letter — inflating unrelated
# candidates and, via the shared hit-ratio denominator, diluting the real match's own
# score enough that it fell out of the top-15 shown to the model.

def test_short_filler_word_never_scores_a_spurious_substring_hit_fixes_225():
    catalog = [{"gtin": "NOISE", "name": "Syrova bageta", "doplnok": ""}]
    # the lone "s" token must not "match" "syrova" just because 's' is its first letter
    row = dl_match.candidates("Zavin s naplnou makovou", catalog)
    assert row[0]["score"] == 0.0


def test_short_filler_words_are_excluded_from_the_match_ratio_denominator_fixes_225():
    catalog = [{"gtin": "TARGET", "name": "Zavin makovy", "doplnok": ""}]
    row = dl_match.candidates("Zavin s naplnou makovou", catalog)
    # 2 real hits (zavin, makovou via stem) out of the 3 SCORABLE words ("s" excluded,
    # "naplnou" itself doesn't match) = 40.0 — not diluted to 30.0 by counting "s" too.
    assert row[0]["score"] == 40.0


def test_a_three_letter_word_still_counts_towards_a_match():
    # the filter must not be so aggressive it throws away genuinely meaningful short
    # Slovak words (e.g. "syr" = cheese, 3 letters) — only 1-2 char filler is excluded.
    catalog = [{"gtin": "GX", "name": "Bageta syr", "doplnok": ""}]
    row = dl_match.candidates("Bageta so syrom", catalog)
    assert row[0]["score"] > 0.0


# --- supplier scoring (R60) --------------------------------------------------------------

def test_supplier_exact_name_scores_100():
    row = dl_match.supplier_candidates("Mlyn Vrbovce s.r.o.", "", "", SUPPLIERS)
    assert row[0]["ean_edi"] == "S1" and row[0]["score"] == 100.0


def test_supplier_name_substring_scores_60():
    assert dl_match._score_supplier("Mlyn Vrbovce", "", "", SUPPLIERS[0]) == 60.0


def test_supplier_name_word_overlap_scores_between_20_and_60():
    score = dl_match._score_supplier("Vrbovce obchodná spoločnosť", "", "", SUPPLIERS[0])
    assert 20.0 <= score < 60.0


def test_supplier_exact_email_stacks_domain_plus_exact_bonus():
    row = dl_match.supplier_candidates("", "obchod@mlynvrbovce.sk", "", SUPPLIERS)
    s1 = next(s for s in row if s["ean_edi"] == "S1")
    assert s1["score"] == 50.0, "domain (+30) AND exact address (+20) both apply"


def test_supplier_domain_only_scores_less_than_exact_email():
    row = dl_match.supplier_candidates("", "sklad@mlynvrbovce.sk", "", SUPPLIERS)
    s1 = next(s for s in row if s["ean_edi"] == "S1")
    assert s1["score"] == 30.0


def test_supplier_city_exact_beats_substring():
    exact = dl_match._score_supplier("", "", "Vrbovce", SUPPLIERS[0])
    substring = dl_match._score_supplier("", "", "Vrbo", SUPPLIERS[0])
    assert exact == 15.0
    assert substring == 10.0


def test_supplier_candidates_sorted_best_first():
    row = dl_match.supplier_candidates("Cukrovar Sereď a.s.", "", "", SUPPLIERS)
    assert row[0]["ean_edi"] == "S2"


# --- supplier decision (R61) --------------------------------------------------------------

def test_decide_supplier_matched_true_with_known_ean():
    d = dl_match.decide_supplier(
        {"matched": True, "ean_edi": "S1", "confidence": 0.9, "matchReason": "sedi"}, SUPPLIERS)
    assert d.matched and d.ean_edi == "S1" and d.name == "Mlyn Vrbovce s.r.o."


def test_decide_supplier_matched_false_goes_to_review():
    d = dl_match.decide_supplier(
        {"matched": False, "matchReason": "nenasiel som"}, SUPPLIERS)
    assert not d.matched and d.review and "nenasiel" in d.note


def test_decide_supplier_never_trusts_a_hallucinated_ean():
    d = dl_match.decide_supplier(
        {"matched": True, "ean_edi": "NOT-IN-TABLE", "confidence": 0.99}, SUPPLIERS)
    assert not d.matched and d.review


def test_decide_supplier_confidence_normalized_from_0_100_scale():
    d = dl_match.decide_supplier(
        {"matched": True, "ean_edi": "S1", "matchConfidence": 92}, SUPPLIERS)
    assert d.confidence == 0.92


# --- item decision ladder (R70-R76) -------------------------------------------------------

def test_llm_sure_ships_the_model_pick():
    d = dl_match.decide_item("Múka hladká 25kg", {"gtin": "G1", "confidence": 0.95}, CATALOG)
    assert d.rule == "llm_sure" and d.gtin == "G1" and d.mass == 25.0


def test_llm_borderline_is_flagged_for_review():
    d = dl_match.decide_item("Múka hladká 25kg", {"gtin": "G1", "confidence": 0.75}, CATALOG)
    assert d.rule == "llm_borderline" and d.review and d.gtin == "G1"


def test_below_gate_min_is_unmatched():
    d = dl_match.decide_item("Múka hladká 25kg", {"gtin": "G1", "confidence": 0.5}, CATALOG)
    assert d.rule == "unmatched" and d.gtin is None
    # R71: the rejected candidate's name is kept in the note, so the warehouse still sees
    # what the model guessed even though the answer was not trusted.
    assert "Múka hladká T512 25kg" in d.note


def test_model_no_match_string_is_unmatched():
    d = dl_match.decide_item("čosi neznáme", {"gtin": "NO_MATCH", "confidence": 0.4}, CATALOG)
    assert d.rule == "unmatched" and d.gtin is None


def test_confidence_scale_normalized_from_0_100():
    d = dl_match.decide_item("Múka hladká 25kg", {"gtin": "G1", "confidence": 95}, CATALOG)
    assert d.confidence == 0.95 and d.rule == "llm_sure"


def test_unknown_gtin_never_crashes_and_falls_through_as_no_match():
    d = dl_match.decide_item("čosi", {"gtin": "GHOST", "confidence": 0.99}, CATALOG)
    assert d.rule == "unmatched" and d.gtin is None


# --- #245: a catalog GTIN that overflows the DESADV LIN field (13 chars) must never ship --

def test_gtin_longer_than_the_edi_field_is_unmatched_not_silently_truncated():
    """Live incident: catalog GTIN 18585037201518 ("Margarín stolný - Favorit", a valid
    GTIN-14 wholesale/bulk code) reached desadv_edi.generate()'s `_pad(str(gtin), 13)`,
    which silently truncated it to 1858503720151 — ORION then rejected the WHOLE document
    ("no stock card with EAN 1858503720151") because that truncated value matches no real
    card at all, and the document sat stuck in `in_DL`, unimportable, for days. A confident
    model match on such a card must never ship — it becomes a real `unmatched` Decision
    (same as any other unresolved item), which raises a visible warehouse question instead
    of corrupting the file."""
    catalog = [{"gtin": "18585037201518", "name": "Margarin stolny - Favorit", "doplnok": "",
               "mass": None, "sklad": "100", "cena": 1.824}]
    d = dl_match.decide_item("Margarin stolny - Favorit",
                             {"gtin": "18585037201518", "confidence": 0.95}, catalog)
    assert d.gtin is None
    assert d.rule == "unmatched"
    assert "13" in d.note


def test_gtin_exactly_at_the_edi_field_width_still_ships():
    """The guard must not be off-by-one — a genuine 13-char GTIN is the overwhelming normal
    case and must keep shipping exactly as before."""
    catalog = [{"gtin": "8436036682545", "name": "Cokolada tmava", "doplnok": "", "mass": None}]
    d = dl_match.decide_item("Cokolada tmava", {"gtin": "8436036682545", "confidence": 0.95},
                             catalog)
    assert d.rule == "llm_sure" and d.gtin == "8436036682545"


def test_memory_rescue_never_resurrects_an_edi_overflowing_gtin():
    """R73 must apply the SAME field-width guard — a stale/human `dl_item_memory` recall
    pointing at an overflowing catalog gtin must not resurrect it either."""
    catalog = [{"gtin": "18585037201518", "name": "Margarin stolny - Favorit", "doplnok": "",
               "mass": None}]
    recalled = _recall(gtin="18585037201518", card="Margarin stolny - Favorit", strength=3,
                       unanimous=True, last_day="2026-08-01")
    d = dl_match.decide_item("Margarin", {"gtin": "NO_MATCH", "confidence": 0.1}, catalog,
                             recalled=recalled)
    assert d.gtin is None
    assert d.rule == "unmatched"


# --- R72 ALIAS RESCUE ----------------------------------------------------------------------

def test_alias_rescue_overrides_low_confidence_when_alias_names_partner():
    catalog = [{"gtin": "G9", "name": "Vianočka 500g", "doplnok": "objednava Mlyn Vrbovce",
               "mass": 0.5}]
    d = dl_match.decide_item("Vianočka", {"gtin": "G9", "confidence": 0.2}, catalog,
                             partner_name="Mlyn Vrbovce s.r.o.")
    assert d.rule == "alias_rescue" and d.gtin == "G9" and d.confidence >= 0.95


def test_alias_rescue_overrides_even_a_weight_conflict():
    """R67: alias confirmation beats the SIZE rule too, unlike orders' equivalent rung."""
    catalog = [{"gtin": "G9", "name": "Chlieb 1kg", "doplnok": "objednava Pekaren Vrbovce",
               "mass": 1.0}]
    d = dl_match.decide_item("Chlieb 300g", {"gtin": "G9", "confidence": 0.3}, catalog,
                             partner_name="Pekaren Vrbovce s.r.o.")
    assert d.rule == "alias_rescue" and d.gtin == "G9"


def test_alias_naming_someone_else_does_not_rescue():
    catalog = [{"gtin": "G9", "name": "Vianočka 500g", "doplnok": "objednava Iny Zakaznik",
               "mass": 0.5}]
    d = dl_match.decide_item("Vianočka", {"gtin": "G9", "confidence": 0.2}, catalog,
                             partner_name="Mlyn Vrbovce s.r.o.")
    assert d.rule != "alias_rescue"


# --- R73 MEMORY RESCUE -----------------------------------------------------------------

def test_memory_rescue_fires_below_the_sure_gate():
    recalled = _recall(gtin="G1", card="Múka hladká T512 25kg", strength=5, unanimous=True,
                       last_day="2026-07-01", weight_override=True)
    d = dl_match.decide_item("Muka", {"gtin": "G2", "confidence": 0.3}, CATALOG,
                             recalled=recalled)
    assert d.rule == "memory_rescue" and d.gtin == "G1" and d.confidence == 0.95


def test_memory_rescue_fires_on_model_no_match():
    recalled = _recall(gtin="G3", card="Cukor kryštálový 25kg", strength=2, unanimous=True,
                       last_day="2026-07-01")
    d = dl_match.decide_item("cukor", {"gtin": "NO_MATCH", "confidence": 0.1}, CATALOG,
                             recalled=recalled)
    assert d.rule == "memory_rescue" and d.gtin == "G3"


def test_memory_never_overrides_a_sure_model_match():
    """R73's own explicit invariant."""
    recalled = _recall(gtin="G3", card="Cukor kryštálový 25kg", strength=9, unanimous=True,
                       last_day="2026-07-01", weight_override=True)
    d = dl_match.decide_item("Múka hladká 25kg", {"gtin": "G1", "confidence": 0.9}, CATALOG,
                             recalled=recalled)
    assert d.rule == "llm_sure" and d.gtin == "G1"


def test_memory_rescue_skipped_when_recalled_gtin_left_the_catalog():
    """R66: catalog-card disappearance invalidates the memory — dl_memory.resolve() is
    responsible for the actual filtering, but decide_item must also not crash or fabricate
    a card when handed a stale recall pointing at a gtin no longer in `catalog`."""
    recalled = _recall(gtin="GONE", card="Retired card", strength=5, unanimous=True,
                       last_day="2026-07-01")
    d = dl_match.decide_item("Muka", {"gtin": "NO_MATCH", "confidence": 0.1}, CATALOG,
                             recalled=recalled)
    assert d.rule == "unmatched"


# --- R74 WEIGHT-CONFLICT guard -----------------------------------------------------------

def test_weight_conflict_rejects_a_mismatched_gramaz():
    d = dl_match.decide_item("Múka hladká 5kg", {"gtin": "G1", "confidence": 0.95}, CATALOG)
    assert d.rule == "unmatched" and d.gtin is None
    assert "gramáž" in d.note


def test_weight_conflict_within_tolerance_is_accepted():
    # 25kg ordered vs a hypothetical 23kg card is within the 0.9-1.1 ratio band.
    catalog = [{"gtin": "GX", "name": "Múka hladká 23kg", "doplnok": "", "mass": 23.0}]
    d = dl_match.decide_item("Múka hladká 25kg", {"gtin": "GX", "confidence": 0.95}, catalog)
    assert d.rule == "llm_sure"


def test_weight_override_accepts_despite_conflict_when_history_confirms():
    recalled = _recall(gtin="G1", card="Múka hladká T512 25kg", strength=4, unanimous=True,
                       last_day="2026-07-01", weight_override=True)
    d = dl_match.decide_item("Múka hladká 5kg", {"gtin": "G1", "confidence": 0.9}, CATALOG,
                             recalled=recalled)
    assert d.rule == "weight_override" and d.gtin == "G1" and d.review


def test_liquid_multipack_is_exempt_from_the_weight_guard():
    catalog = [{"gtin": "GL", "name": "Olej 6x1l", "doplnok": "", "mass": None}]
    d = dl_match.decide_item("Olej 12x1l", {"gtin": "GL", "confidence": 0.9}, catalog)
    assert d.rule == "llm_sure", "multipack names are exempt (R74) — never a weight conflict"


def test_weightless_names_never_conflict():
    catalog = [{"gtin": "GN", "name": "Chlieb tradičný", "doplnok": "", "mass": None}]
    d = dl_match.decide_item("Chlieb tradičný", {"gtin": "GN", "confidence": 0.9}, catalog)
    assert d.rule == "llm_sure"


# --- lexical tripwire (mirrors match.py's #195 pattern) -----------------------------------

def test_lexical_gap_blocks_a_sure_but_unrelated_answer():
    catalog = [{"gtin": "GZ", "name": "Chlieb tradičný pšenično ražný 700gr", "doplnok": ""}]
    d = dl_match.decide_item("Kváskový slimák s pizzovou plnkou",
                             {"gtin": "GZ", "confidence": 0.96}, catalog)
    assert d.rule == "llm_sure_lexical_gap" and d.review and d.gtin is None


def test_lexical_overlap_present_ships_normally():
    d = dl_match.decide_item("Múka hladká 25kg", {"gtin": "G1", "confidence": 0.96}, CATALOG)
    assert d.rule == "llm_sure"


# --- mass resolution (R67) -----------------------------------------------------------------

def test_mass_from_matched_catalog_entry_wins_over_parsed_name():
    d = dl_match.decide_item("Múka hladká 25kg", {"gtin": "G1", "confidence": 0.95}, CATALOG)
    assert d.mass == 25.0


def test_mass_parsed_from_name_when_catalog_entry_has_none():
    catalog = [{"gtin": "GM", "name": "Olej repkový 10l", "doplnok": "", "mass": None}]
    d = dl_match.decide_item("Olej repkový 5l", {"gtin": "GM", "confidence": 0.2}, catalog)
    # weight conflict (5l vs 10l ratio 0.5, outside 0.9-1.1) -> unmatched, mass falls back to 0
    assert d.mass == 0.0


def test_mass_parsed_from_wording_when_matched_card_has_no_weight_conflict():
    """The catalog entry itself carries no `mass`, and its NAME states no weight either
    (so the WEIGHT-CONFLICT guard has nothing to disagree with) — R67's own fallback,
    'else parsed from name', must resolve the kg value from the ORDERED wording."""
    catalog = [{"gtin": "GW", "name": "Maslo čerstvé", "doplnok": "", "mass": None}]
    d = dl_match.decide_item("Maslo čerstvé 2kg", {"gtin": "GW", "confidence": 0.9}, catalog)
    assert d.rule == "llm_sure" and d.mass == 2.0


def test_mass_zero_when_nothing_available():
    catalog = [{"gtin": "GZ2", "name": "Neznáma položka bez gramáže", "doplnok": "",
               "mass": None}]
    d = dl_match.decide_item("Neznáma položka", {"gtin": "NO_MATCH", "confidence": 0.1},
                             catalog)
    assert d.mass == 0.0


# --- #246: the DL overflow review note surfaces a SAFE computed 13-digit sibling hint ------

def test_gtin14_overflow_note_surfaces_the_computed_13digit_sibling_as_a_verify_hint():
    """A valid GTIN-14 catalog card still cannot ship (13-char DESADV field, #245), but the
    review note now hands the warehouse the computed 13-digit sibling to verify in CODEX — the
    same manual step the owner did for #246's 9 confirmed products. The card NEVER ships.
    Synthetic GTIN-14 (repo is public)."""
    catalog = [{"gtin": "20001112223336", "name": "Synteticky napoj 330ml", "doplnok": "",
               "mass": None, "sklad": "100", "cena": 1.0}]
    d = dl_match.decide_item("Synteticky napoj 330ml",
                             {"gtin": "20001112223336", "confidence": 0.96}, catalog)
    assert d.gtin is None and d.rule == "unmatched"   # still never ships
    assert "0001112223332" in d.note                  # computed 13-digit sibling surfaced
    assert "over" in d.note.lower()                    # framed as a verify-in-CODEX hint


def test_gtin14_overflow_note_shows_no_false_sibling_for_an_invalid_gtin14_korenie_class():
    """The Korenie class (#246, warehouse-confirmed 14-digit-only): a 14-char code whose own
    check digit does not validate. Its naive prefix-strip validates but is a FALSE code — the
    note must NEVER surface it, only the generic 'fix the card in CODEX' wording. Synthetic."""
    catalog = [{"gtin": "88590000123455", "name": "Korenie synteticke", "doplnok": "",
               "mass": None, "sklad": "100", "cena": 5.0}]
    d = dl_match.decide_item("Korenie synteticke",
                             {"gtin": "88590000123455", "confidence": 0.97}, catalog)
    assert d.gtin is None and d.rule == "unmatched"   # still never ships
    assert "8590000123455" not in d.note              # the false naive strip is NEVER shown
    assert str(dl_match.GTIN_FIELD_WIDTH) in d.note    # generic overflow wording preserved
