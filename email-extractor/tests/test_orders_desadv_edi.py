"""DESADV EDI builder (#203, DL migration F4).

The low-level `generate()` is pinned **byte for byte** against
`fixtures/desadv_reference.json`. Those bytes were produced by running the PRODUCTION
n8n generator (the `ASSEMBLE AND GENERATE EDI [v1]` Code node, `sub3_edi_code.js` v27)
under node — the fixture is parity with what ORION receives today, not with a reading
of the design doc's prose summary (same technique `tests/test_orders_edi.py` already
uses for the orders-side `edi.py`).

`build()` (the orchestration layer: gating, docNumber fallback, filename, zero-qty/
NO_MATCH reporting) is control-flow the JS source doesn't expose separately from
`generateEDI()` — no node-generated fixture for it, just direct assertions.
"""
import json
from pathlib import Path

from app.orders import desadv_edi

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "desadv_reference.json").read_text(encoding="utf-8"))


def _case(name):
    return next(c for c in FIXTURE if c["name"] == name)


# --- byte parity with the production generator ------------------------------

def test_generate_matches_the_production_generator_byte_for_byte():
    for case in FIXTURE:
        got = desadv_edi.generate(**case["input"])
        assert got.content == case["expected"]["content"], case["name"]
        assert got.line_count == case["expected"]["lineCount"], case["name"]
        assert got.skipped == case["expected"]["skipped"], case["name"]
        assert got.substituted == case["expected"]["substituted"], case["name"]


def test_the_header_is_exactly_1157_characters():
    got = desadv_edi.generate(**_case("two_pieces_no_conversion")["input"])
    assert len(got.content.split("\r\n")[0]) == 1157


def test_a_lin_line_is_exactly_221_characters():
    got = desadv_edi.generate(**_case("two_pieces_no_conversion")["input"])
    lines = [ln for ln in got.content.split("\r\n") if ln.startswith("LIN")]
    assert len(lines) == 2
    assert all(len(ln) == 221 for ln in lines)


def test_lines_are_joined_with_crlf():
    got = desadv_edi.generate(**_case("two_pieces_no_conversion")["input"])
    assert "\r\n" in got.content
    assert "\n\n" not in got.content.replace("\r\n", "")


def test_diacritics_and_czech_e_caron_never_reach_the_file():
    got = desadv_edi.generate(**_case("diacritics_and_czech_e_caron")["input"])
    assert all(ord(c) < 128 for c in got.content), "ORION reads a fixed-width ASCII file"
    assert "Pekaren Novakova a spol." in got.content


def test_czech_e_caron_folds_via_the_win1250_table():
    """The fixture case above (named for Czech ě) actually only exercises Slovak
    diacritics — review finding on #203: the one thing the module's own docstring
    calls out as new coverage versus edi.py's table was untested. Direct test of the
    literal map entry (byte-exact port of the real JS `toWin1250`'s 'ě':'e'/'Ě':'E' —
    no fresh node fixture needed, the map itself IS the ground truth already sourced
    from the real JS)."""
    assert desadv_edi._to_win1250("měsíc") == "mesic"   # ě and í both fold
    assert desadv_edi._to_win1250("ě") == "e"
    assert desadv_edi._to_win1250("Ě") == "E"


# --- R84: quantity/unit conversion ladder ------------------------------------

def test_kg_tracked_sklad_100_converts_pieces_to_kg():
    case = _case("kg_tracked_piece_unit_converts")
    got = desadv_edi.generate(**case["input"])
    lin = [ln for ln in got.content.split("\r\n") if ln.startswith("LIN")][0]
    # unit price at offset 3+6+13+14+23+1+22=82, width 9; qty at offset 82+9+5=96, width 12
    assert lin[82:91] == "    4.000"
    assert lin[96:108] == "       7.500"


def test_kg_tracked_already_in_kg_unit_is_unchanged():
    case = _case("kg_tracked_already_kg_unit_unchanged")
    got = desadv_edi.generate(**case["input"])
    lin = [ln for ln in got.content.split("\r\n") if ln.startswith("LIN")][0]
    assert lin[82:91] == "    8.000"
    assert lin[96:108] == "       7.500"


def test_eggs_are_exempt_from_kg_conversion_despite_sklad_100():
    case = _case("kg_tracked_eggs_exempt_stays_pieces")
    got = desadv_edi.generate(**case["input"])
    lin = [ln for ln in got.content.split("\r\n") if ln.startswith("LIN")][0]
    assert lin[96:108] == "      30.000"        # unchanged piece qty, not x mass
    assert lin[108:111] == "ks "


def test_liquid_multipack_takes_precedence_over_kg_conversion():
    """sklad=100 AND a `6x1l` supplier-name token — multipack must win (R84 order)."""
    case = _case("liquid_multipack_takes_precedence")
    got = desadv_edi.generate(**case["input"])
    lin = [ln for ln in got.content.split("\r\n") if ln.startswith("LIN")][0]
    assert lin[108:111] == "L  "
    assert lin[96:108] == "      12.000"
    assert lin[82:91] == "    2.000"


def test_multipack_detection_needs_the_original_supplier_wording_not_the_card_name():
    data = {
        "customerEanEdi": "2000000000099", "customerName": "X", "docNumber": "1",
        "orderNumber": "1", "deliveryDate": "01.08.2026",
        "items": [{"gtin": "1", "name": "Ocot 8% destilovany", "supplierName": "bez tokenu",
                  "quantity": 2, "mass": 0, "unit": "ks", "totalPrice": 10, "unitPrice": 5}],
    }
    got = desadv_edi.generate(data, {"1": "100"}, {})
    lin = got.content.split("\r\n")[1]
    # No multipack token in supplierName and no mass -> falls through to unchanged piece qty
    assert lin[108:111] == "ks "
    assert lin[96:108] == "       2.000"


def test_unit_column_keeps_original_text_except_when_multipack_forces_l():
    """W11 explicit contract."""
    non_multipack = _case("kg_tracked_piece_unit_converts")
    got = desadv_edi.generate(**non_multipack["input"])
    lin = got.content.split("\r\n")[1]
    assert lin[108:111] == "ks "        # original unit text preserved, not "kg"


def test_multipack_count_above_the_1000_sanity_cap_is_rejected():
    """Ported faithfully from the real JS guard (`count > 1000 -> return null`, review
    finding on #203) — an implausible count is treated as noise, not a real multipack,
    falling through to the unconverted piece branch rather than silently producing a
    huge quantity."""
    assert desadv_edi._detect_liquid_multipack("Voda / 1001x1l") is None
    assert desadv_edi._detect_liquid_multipack("Voda / 1000x1l") is not None


# --- R84.2 (#366): ton -> kg for a kg-tracked card ---------------------------

def _ton_data(gtin, quantity, unit, unit_price=0, total_price=0, name="Múka",
              supplier_name=""):
    return {
        "customerEanEdi": "2000000000099", "customerName": "X", "docNumber": "1",
        "orderNumber": "1", "deliveryDate": "26.08.2026",
        "items": [{"gtin": gtin, "name": name, "supplierName": supplier_name or name,
                  "quantity": quantity, "mass": 0, "unit": unit,
                  "totalPrice": total_price, "unitPrice": unit_price}],
    }


def test_kg_tracked_ton_unit_converts_quantity_to_kg():
    """#366 core: message 8700 shipped '2 ton' of a kg-tracked flour card as qty 2, so
    ORION imported 2 kg instead of 2000 kg (warehouse-reported '2000kg'). A tonne is
    1000 kg — the LIN quantity must be in kg and the unit label must become kg."""
    got = desadv_edi.generate(_ton_data("1557", 2, "ton", name="T 512 - hladná múka"),
                              {"1557": "100"}, {})
    lin = got.content.split("\r\n")[1]
    assert lin[96:108] == "    2000.000"   # 2 ton -> 2000 kg
    assert lin[108:111] == "kg "           # LIN unit label becomes kg, not "ton"


def test_kg_tracked_ton_with_no_price_fills_per_kg_and_labels_kg():
    """The EXACT message-8700 scenario end to end: '2 ton', no line price, catalog cena
    0.368 (€/kg). The quantity must convert to 2000 kg AND the price-substitution note
    must read '€/kg', never the misleading '€/ton' the bug produced."""
    got = desadv_edi.generate(
        _ton_data("1557", 2, "ton", name="T 512 - hladná múka"),
        {"1557": "100"}, {"1557": 0.368})
    lin = got.content.split("\r\n")[1]
    assert lin[96:108] == "    2000.000"
    assert lin[82:91] == "    0.368"        # catalog €/kg price, applied to the kg qty
    assert lin[108:111] == "kg "
    assert got.substituted == ["T 512 - hladná múka: bez ceny → 0.368 €/kg"]


def test_kg_tracked_t_abbreviation_also_converts_to_kg():
    """The salt supplier uses 't' (not 'ton') for a 1-tonne delivery of a kg-tracked
    card (gtin 4003885181808) — the abbreviation must convert the same 1 t -> 1000 kg."""
    got = desadv_edi.generate(_ton_data("4003885181808", 1, "t", name="Soľ voľná"),
                              {"4003885181808": "100"}, {})
    lin = got.content.split("\r\n")[1]
    assert lin[96:108] == "    1000.000"
    assert lin[108:111] == "kg "


def test_kg_tracked_ton_line_with_a_per_ton_price_normalizes_to_per_kg():
    """A line that DOES carry a €/ton price (368 €/ton) is normalized to €/kg (0.368)
    when its quantity converts to kg — the per-line price divides by 1000 too, so the
    R85 price-fallback comparison stays apples-to-apples and does not fire."""
    got = desadv_edi.generate(
        _ton_data("1", 2, "ton", unit_price=368, total_price=736),
        {"1": "100"}, {"1": 0.368})
    lin = got.content.split("\r\n")[1]
    assert lin[96:108] == "    2000.000"
    assert lin[82:91] == "    0.368"        # 368/1000 = 0.368 €/kg; within 5x of cena
    assert got.substituted == []            # normalized price is fine -> no fallback


def test_ton_conversion_is_scoped_to_kg_tracked_cards_only():
    """A tonne unit only makes physical sense against a kg-tracked (sklad=100) card. A
    NON-kg-tracked card is left exactly as before — the rule is keyed on the card's
    unit, never applied blindly."""
    got = desadv_edi.generate(
        _ton_data("9", 2, "ton", unit_price=5, total_price=10, name="Kus"),
        {"9": ""}, {})
    lin = got.content.split("\r\n")[1]
    assert lin[96:108] == "       2.000"    # unconverted (out of scope)
    assert lin[108:111] == "ton"            # unit unchanged


def test_a_non_ton_unit_like_kt_is_never_mistaken_for_ton():
    """Exact-token match, never substring — 'kt' (kartón), 'ba', 'kus' must never be
    read as a tonne and multiplied by 1000."""
    got = desadv_edi.generate(_ton_data("1", 2, "kt", unit_price=5, total_price=10),
                              {"1": "100"}, {})
    lin = got.content.split("\r\n")[1]
    assert lin[96:108] == "       2.000"    # kt is NOT ton -> no x1000
    assert lin[108:111] == "kt "


def test_ton_beats_a_per_piece_mass_on_a_kg_tracked_card():
    """A tonne is 1000 kg regardless of any per-piece `mass` on the card, so the tonne
    branch MUST win over the mass rung — a "2 ton" line on a card carrying mass 25 must
    ship 2000 kg, never 2 x 25 = 50 kg. Pins the documented branch ordering (a refactor
    that moved the tonne rung below the mass rung would otherwise leave every other test
    green)."""
    data = {
        "customerEanEdi": "2000000000099", "customerName": "X", "docNumber": "1",
        "orderNumber": "1", "deliveryDate": "26.08.2026",
        "items": [{"gtin": "1", "name": "Múka 25 kg", "supplierName": "Múka 25 kg",
                  "quantity": 2, "mass": 25, "unit": "ton", "totalPrice": 0,
                  "unitPrice": 0}],
    }
    got = desadv_edi.generate(data, {"1": "100"}, {})
    lin = got.content.split("\r\n")[1]
    assert lin[96:108] == "    2000.000"   # 2 ton -> 2000 kg, NOT 2 x 25 = 50
    assert lin[108:111] == "kg "


def test_is_ton_unit_recognizes_tonne_forms_and_rejects_lookalikes():
    for u in ["ton", "TON", " ton ", "t", "T", "tona", "tony", "tonu", "tonne", "tón",
              "Tón", "ton.", "tuna", "tuny", "tunu", "tun", "TUNA"]:
        assert desadv_edi._is_ton_unit(u), f"expected ton: {u!r}"
    for u in ["kg", "ks", "kt", "ba", "kus", "balení", "g", "l", "", "to", None]:
        assert not desadv_edi._is_ton_unit(u), f"expected NOT ton: {u!r}"


# --- R85: price fallback -----------------------------------------------------

def test_price_fallback_fires_when_price_is_missing():
    case = _case("price_fallback_missing_price")
    got = desadv_edi.generate(**case["input"])
    assert got.substituted == ["Chlieb konzum: bez ceny → 1.500 €/ks"]
    lin = got.content.split("\r\n")[1]
    assert lin[82:91] == "    1.500"


def test_price_fallback_fires_at_5x_too_high():
    case = _case("price_fallback_5x_too_high")
    got = desadv_edi.generate(**case["input"])
    assert got.substituted == ["Rohlik: 9.700 → 0.974 €/ks"]


def test_price_fallback_does_not_fire_on_normal_movement_under_5x():
    case = _case("price_normal_movement_not_substituted")
    got = desadv_edi.generate(**case["input"])
    assert got.substituted == []
    lin = got.content.split("\r\n")[1]
    assert lin[82:91] == "    1.200"        # the read price, not the catalog one


def test_price_fallback_fires_at_the_low_boundary_one_fifth():
    data = {
        "customerEanEdi": "2000000000099", "customerName": "X", "docNumber": "1",
        "orderNumber": "1", "deliveryDate": "01.08.2026",
        "items": [{"gtin": "1", "name": "Test", "supplierName": "test",
                  "quantity": 1, "mass": 0, "unit": "ks", "totalPrice": 1, "unitPrice": 1.0}],
    }
    got = desadv_edi.generate(data, {}, {"1": 5.0})   # 1.0 == 5.0 / 5 -> boundary fires
    assert got.substituted


def test_price_fallback_just_inside_the_boundary_does_not_fire():
    data = {
        "customerEanEdi": "2000000000099", "customerName": "X", "docNumber": "1",
        "orderNumber": "1", "deliveryDate": "01.08.2026",
        "items": [{"gtin": "1", "name": "Test", "supplierName": "test",
                  "quantity": 1, "mass": 0, "unit": "ks", "totalPrice": 1.01, "unitPrice": 1.01}],
    }
    got = desadv_edi.generate(data, {}, {"1": 5.0})   # 1.01 > 5.0/5 -> no fallback
    assert got.substituted == []


# --- NO_MATCH / zero-qty ------------------------------------------------------

def test_no_match_item_is_skipped_and_remaining_lines_renumber():
    case = _case("no_match_item_skipped_and_renumbered")
    got = desadv_edi.generate(**case["input"])
    assert got.skipped == ["Neznamy vyrobok"]
    lin = got.content.split("\r\n")[1]
    assert lin[3:9] == "     1"     # renumbered to 1, not 2
    assert "Neznamy" not in got.content


def test_missing_gtin_is_treated_the_same_as_no_match():
    data = {
        "customerEanEdi": "2000000000099", "customerName": "X", "docNumber": "1",
        "orderNumber": "1", "deliveryDate": "01.08.2026",
        "items": [{"gtin": None, "name": "Bez gtinu", "supplierName": "bez gtinu",
                  "quantity": 1, "mass": 0, "unit": "ks", "totalPrice": 1, "unitPrice": 1}],
    }
    got = desadv_edi.generate(data, {}, {})
    assert got.skipped == ["Bez gtinu"]
    assert got.line_count == 0


# --- delivery date / HDR ------------------------------------------------------

def test_missing_delivery_date_blanks_both_hdr_dates():
    case = _case("missing_delivery_date_blanks_hdr_dates")
    got = desadv_edi.generate(**case["input"])
    hdr = got.content.split("\r\n")[0]
    doc_date = hdr[3 + 15:3 + 15 + 8]
    deliv_date = hdr[3 + 15 + 8:3 + 15 + 8 + 8]
    assert doc_date == " " * 8
    assert deliv_date == " " * 8


def test_iso_delivery_date_is_accepted_as_a_defensive_fallback():
    """W12: the production node ONLY accepts DD.MM.YYYY and silently blanks an ISO
    date. Extraction always emits DD.MM.YYYY (R47) so this never fires on real input —
    it is a pure safety net, same reasoning edi.py's own _format_date already carries."""
    assert desadv_edi._format_date("2026-08-05") == "20260805"
    assert desadv_edi._format_date("05.08.2026") == "20260805"
    assert desadv_edi._format_date("") == " " * 8
    assert desadv_edi._format_date("garbage") == " " * 8


def test_two_digit_year_is_expanded_to_20xx():
    assert desadv_edi._format_date("05.08.26") == "20260805"


# --- catalog_lookups ----------------------------------------------------------

def test_catalog_lookups_reads_sklad_and_cena_by_gtin():
    catalog = [
        {"gtin": "1", "sklad": "100", "cena": "1,50"},
        {"gtin": "2", "sklad": "", "cena": "0"},
        {"gtin": "", "sklad": "100", "cena": "9.99"},   # no gtin -> ignored
    ]
    sklad, cena = desadv_edi.catalog_lookups(catalog)
    assert sklad == {"1": "100", "2": ""}
    assert cena == {"1": 1.5}          # "2" has cena<=0, excluded


# --- filename / doc-number generation (R89, R83) ------------------------------

def test_filename_carries_ean_tail_docnumber_date_and_stamp():
    name = desadv_edi.filename("2000000000864", "04.08.2026", "P26036931",
                              stamp="123456789")
    assert name == "DESADV_000864_P26036931_20260804_123456789.txt"


def test_filename_truncates_doc_number_to_ten_alnum_chars():
    name = desadv_edi.filename("2000000000864", "04.08.2026", "PREFIX-VERY-LONG-12345",
                              stamp="000000000")
    # alnum-only, capped at 10: "PREFIXVERY"
    assert "_PREFIXVERY_" in name


def test_filename_omits_doc_part_when_doc_number_is_empty():
    name = desadv_edi.filename("2000000000864", "04.08.2026", "", stamp="000000000")
    assert name == "DESADV_000864_20260804_000000000.txt"


def test_filename_falls_back_to_short_ean_when_missing():
    name = desadv_edi.filename("", "04.08.2026", "1", stamp="000000000")
    assert name.startswith("DESADV_000000_")


def test_upload_name_adds_the_z_prefix():
    assert desadv_edi.upload_name("DESADV_x.txt") == "Z-DESADV_x.txt"


# --- #239 finding 6: stable identity + presence proof --------------------------

def test_stable_prefix_is_a_true_prefix_of_filename():
    prefix = desadv_edi.stable_prefix("2000000000864", "P26036931")
    name = desadv_edi.filename("2000000000864", "04.08.2026", "P26036931",
                               stamp="123456789")
    assert name.startswith(prefix)
    assert prefix == "DESADV_000864_P26036931_"


def test_stable_prefix_is_identical_across_retries_with_different_timestamps():
    """The whole point: two filenames built for the SAME document at different times
    must share the same stable prefix, even though the full filenames differ."""
    p1 = desadv_edi.stable_prefix("2000000000864", "P26036931")
    n1 = desadv_edi.filename("2000000000864", "04.08.2026", "P26036931", stamp="111111111")
    n2 = desadv_edi.filename("2000000000864", "05.08.2026", "P26036931", stamp="222222222")
    assert n1 != n2
    assert n1.startswith(p1) and n2.startswith(p1)


def test_already_landed_is_false_when_nothing_matches():
    dirs = {"in_DL": {"Z-DESADV_999999_OTHER_20260804_000000000.txt"},
           "archCodex": set(), "unconfirmed": set()}
    assert desadv_edi.already_landed(dirs, "2000000000864", "P26036931") is False


def test_already_landed_matches_the_wire_prefixed_name_in_in_dl():
    name = desadv_edi.upload_name(
        desadv_edi.filename("2000000000864", "04.08.2026", "P26036931",
                            stamp="123456789"))
    dirs = {"in_DL": {name}, "archCodex": set(), "unconfirmed": set()}
    assert desadv_edi.already_landed(dirs, "2000000000864", "P26036931") is True


def test_already_landed_matches_archcodex_even_with_a_different_attempt_timestamp():
    """This is the whole reason a filename check would be wrong — a RETRY builds a
    DIFFERENT filename, so only a stable-prefix match can prove the earlier attempt's
    document already landed."""
    earlier_attempt = desadv_edi.upload_name(
        desadv_edi.filename("2000000000864", "04.08.2026", "P26036931",
                            stamp="111111111"))
    dirs = {"in_DL": set(), "archCodex": {earlier_attempt}, "unconfirmed": set()}
    assert desadv_edi.already_landed(dirs, "2000000000864", "P26036931") is True


def test_already_landed_matches_archcodex_with_the_tolerant_extra_z_rename():
    """confirm.py's own `_decide()` tolerates an EXTRA Z- from Communicator's separate
    rename job — already_landed() must be equally tolerant."""
    name = desadv_edi.filename("2000000000864", "04.08.2026", "P26036931",
                               stamp="111111111")
    dirs = {"in_DL": set(), "archCodex": {f"Z-Z-{name}"}, "unconfirmed": set()}
    assert desadv_edi.already_landed(dirs, "2000000000864", "P26036931") is True


def test_already_landed_true_for_unconfirmed_too_the_upload_still_happened():
    """A file in `unconfirmed` means CODEX rejected the IMPORT — but the UPLOAD itself
    still succeeded, so retrying now would still duplicate the upload."""
    name = desadv_edi.upload_name(
        desadv_edi.filename("2000000000864", "04.08.2026", "P26036931",
                            stamp="111111111"))
    dirs = {"in_DL": set(), "archCodex": set(), "unconfirmed": {name}}
    assert desadv_edi.already_landed(dirs, "2000000000864", "P26036931") is True


def test_already_landed_never_matches_a_different_documents_prefix():
    name = desadv_edi.upload_name(
        desadv_edi.filename("2000000000864", "04.08.2026", "P99999999", stamp="1"))
    dirs = {"in_DL": {name}, "archCodex": set(), "unconfirmed": set()}
    assert desadv_edi.already_landed(dirs, "2000000000864", "P26036931") is False


def test_already_landed_handles_a_missing_or_empty_dirs_dict():
    assert desadv_edi.already_landed({}, "2000000000864", "P26036931") is False
    assert desadv_edi.already_landed(None, "2000000000864", "P26036931") is False


def test_already_landed_never_tolerates_more_than_two_leading_z_prefixes():
    """Review finding: an earlier draft stripped an UNBOUNDED number of leading `Z-`,
    which is more permissive than confirm.py's own `_decide()` tolerance (exactly one
    extra `Z-` beyond R89's own wire prefix, never more) despite the docstring's claim
    of parity. A THIRD `Z-` must not match."""
    name = desadv_edi.filename("2000000000864", "04.08.2026", "P26036931", stamp="1")
    dirs = {"in_DL": set(), "archCodex": {f"Z-Z-Z-{name}"}, "unconfirmed": set()}
    assert desadv_edi.already_landed(dirs, "2000000000864", "P26036931") is False


def test_generate_doc_number_uses_first_word_of_supplier_ascii_folded():
    doc = desadv_edi._generate_doc_number("Čerešňový mlyn s.r.o.")
    assert doc.startswith("DL-CERESNOV-")          # 8-char cap: "Čerešňový" -> "CERESNOV"


def test_generate_doc_number_handles_empty_name():
    doc = desadv_edi._generate_doc_number("")
    assert doc.startswith("DL-UNKNOWN-")


# --- generate_stable_doc_number (#262) -----------------------------------------
#
# `_generate_doc_number()` above is wall-clock based (R83 byte parity with the
# production `generateDocNumber()`) and stays that way. This is a SEPARATE fallback
# for a document whose extraction found no doc number AT ALL and never will (an
# informal delivery announcement in mail body text, #262) — it must be STABLE across
# every reprocessing attempt of the SAME message (a stale-claim reclaim, an R17
# transient retry), or `desadv.claim_send_or_identify()`'s (supplier_ean, doc_number)
# dedup key changes on every retry and a numberless document can be uploaded to
# ORION twice under two different synthesized identities.

def test_generate_stable_doc_number_is_deterministic_for_the_same_message():
    a = desadv_edi.generate_stable_doc_number("eval-hkloan-1")
    b = desadv_edi.generate_stable_doc_number("eval-hkloan-1")
    assert a == b


def test_generate_stable_doc_number_differs_for_different_messages():
    a = desadv_edi.generate_stable_doc_number("eval-hkloan-1")
    b = desadv_edi.generate_stable_doc_number("eval-hkloan-2")
    assert a != b


def test_generate_stable_doc_number_carries_digits_for_the_edi_hdr_field():
    """R83's own comment: ORION/EDITEL parses the HDR docNumber field as a NUMBER, so
    `build()` strips the human-facing doc_number to digits-only for the EDI content
    (`re.sub(r"[^0-9]", "", doc_number) or doc_number`) — a synthesized identity with
    zero digit characters would fall back to the raw (non-numeric) string and crash
    the import, exactly the weak point `build()`'s own docstring already flags for a
    plain extraction miss. The stable synthesis must never produce that shape."""
    doc = desadv_edi.generate_stable_doc_number("eval-hkloan-1")
    assert any(c.isdigit() for c in doc)
    import re as _re
    assert _re.sub(r"[^0-9]", "", doc) != ""


def test_generate_stable_doc_number_never_crashes_on_an_empty_message_id():
    doc = desadv_edi.generate_stable_doc_number("")
    assert doc.startswith("AVIZO")


def test_generate_stable_doc_number_is_never_confused_with_a_real_extracted_number():
    """The prefix must be obviously synthetic — never collide with a real printed doc
    number's shape (`docNumber`s in this corpus/production look like plain digits or
    an LT-prefixed code, never start with a supplier-unrelated word)."""
    doc = desadv_edi.generate_stable_doc_number("eval-hkloan-1")
    assert doc.startswith("AVIZO")


# --- build(): orchestration (R80-R83, R89) ------------------------------------

def _header(ean="2000000000099", name="Testovaci Dodavatel s.r.o."):
    return {"customerEanEdi": ean, "customerName": name}


def _extraction(doc_number="12345678", delivery_date="04.08.2026"):
    return {"docNumber": doc_number, "deliveryDate": delivery_date}


def _item(gtin="1", name="Rozok", matched="Rozok standard 50g", qty=10, unit="ks",
         unit_price=0.2, total_price=2.0, mass=0):
    return {"gtin": gtin, "name": name, "matchedCatalogName": matched, "quantity": qty,
            "unit": unit, "unitPrice": unit_price, "totalPrice": total_price, "mass": mass}


def test_build_creates_edi_for_a_fully_matched_delivery():
    result = desadv_edi.build(_header(), _extraction(), [_item()], [])
    assert result.can_create is True
    assert result.reject_reason == ""
    assert result.line_count == 1
    assert result.doc_number == "12345678"
    assert result.doc_number_auto_generated is False
    assert result.filename.startswith("DESADV_000099_12345678_20260804_")
    assert result.content.startswith("HDR")
    assert result.partial is False
    assert result.items_skipped_no_match == []
    assert result.items_skipped_zero_qty == []


def test_build_rejects_when_supplier_is_not_matched():
    result = desadv_edi.build(_header(ean=""), _extraction(), [_item()], [])
    assert result.can_create is False
    assert result.reject_reason == "Dodavatel nebol najdeny v databaze"
    assert result.filename == ""
    assert result.content == ""
    assert result.line_count == 0


def test_build_rejects_when_no_item_has_a_real_gtin():
    items = [_item(gtin="NO_MATCH", matched="")]
    result = desadv_edi.build(_header(), _extraction(), items, [])
    assert result.can_create is False
    assert result.reject_reason == "Ziadne polozky s GTIN: 0 z 1"


def test_build_rejects_when_every_item_has_zero_quantity():
    """Matches the production node's own reason-selection order exactly: `matchedItems.
    length===0` is checked BEFORE `items.length===0`, and an all-zero-qty input makes
    BOTH true at once (an empty `items` list can never contain a matched item) — so the
    'Ziadne polozky s GTIN' reason wins, and 'Ziadne polozky s nenulovym mnozstvom' is
    unreachable dead code in the original JS. Ported faithfully, not "fixed"."""
    items = [_item(qty=0)]
    result = desadv_edi.build(_header(), _extraction(), items, [])
    assert result.can_create is False
    assert result.reject_reason == "Ziadne polozky s GTIN: 0 z 0"


def test_build_reports_zero_quantity_items_instead_of_dropping_them_silently():
    """W10: the n8n version silently filters qty==0 items with zero trace. The Python
    build() must surface them so a later phase can put them in the Odoo message."""
    items = [_item(gtin="1", qty=10), _item(gtin="2", name="Free sample", qty=0)]
    result = desadv_edi.build(_header(), _extraction(), items, [])
    assert result.can_create is True
    assert result.items_skipped_zero_qty == ["Free sample"]
    assert "Free sample" not in result.content


def test_build_is_partial_edi_when_some_items_are_unmatched_but_others_ship():
    """R81: partial EDI is normal — unmatched items are surfaced, matched ones ship."""
    items = [_item(gtin="1", qty=10), _item(gtin="NO_MATCH", name="Cudzi vyrobok",
                                            matched="", qty=5)]
    result = desadv_edi.build(_header(), _extraction(), items, [])
    assert result.can_create is True
    assert result.partial is True
    assert result.items_skipped_no_match == ["Cudzi vyrobok"]
    assert result.line_count == 1
    assert "Cudzi" not in result.content


def test_build_auto_generates_doc_number_when_extraction_found_none():
    result = desadv_edi.build(_header(), _extraction(doc_number=""), [_item()], [])
    assert result.can_create is True
    assert result.doc_number_auto_generated is True
    assert result.doc_number.startswith("DL-TESTOVAC-")   # 8-char cap


def test_build_strips_doc_number_to_digits_only_in_edi_content_but_keeps_it_human_facing():
    """R83: ORION/EDITEL parses the HDR doc-number field as a number — a letter prefix
    crashes the import. The human-facing doc_number (Odoo, registry, filename) keeps
    the original."""
    result = desadv_edi.build(_header(), _extraction(doc_number="P26036931"), [_item()], [])
    assert result.can_create is True
    assert result.doc_number == "P26036931"          # human-facing keeps the letter
    hdr = result.content.split("\r\n")[0]
    doc_number_field = hdr[3:18]
    assert doc_number_field.strip() == "26036931"    # EDI content is digits-only


def test_doc_number_with_zero_digits_falls_back_to_the_raw_value_matching_production():
    """A genuine INHERITED weak point (review finding on #203, verified against the
    real sub3_edi_code.js source): the production node's own digits-only strip is
    `.replace(/[^0-9]/g, '') || String(docNumber)` — when NOTHING in docNumber is a
    digit, it falls back to the un-stripped original, not an empty string. This test
    PINS that this Python port matches production byte-for-byte here too, deliberately
    NOT "fixed" to diverge from it (that would break the byte-parity guarantee this
    module exists for)."""
    result = desadv_edi.build(_header(), _extraction(doc_number="ABC"), [_item()], [])
    assert result.can_create is True
    assert result.doc_number == "ABC"
    hdr = result.content.split("\r\n")[0]
    doc_number_field = hdr[3:18]
    assert doc_number_field.strip() == "ABC"          # NOT stripped — matches production


def test_build_uses_catalog_price_fallback_and_sklad_conversion_end_to_end():
    catalog = [{"gtin": "1", "sklad": "100", "cena": "2.0", "name": "x", "mass": 1,
               "doplnok": ""}]
    items = [_item(gtin="1", qty=3, unit="kg", unit_price=0, total_price=0, mass=1)]
    result = desadv_edi.build(_header(), _extraction(), items, catalog)
    assert result.can_create is True
    assert result.price_substitutions
    assert "2.000" in result.content


def test_build_computes_items_total_from_non_zero_quantity_items_only():
    items = [_item(gtin="1", qty=10), _item(gtin="2", qty=0)]
    result = desadv_edi.build(_header(), _extraction(), items, [])
    assert result.items_total == 1


# --- #246: GTIN-14 -> GTIN-13 sibling (GS1 check-digit recompute) for the DL overflow hint

def test_gtin14_to_gtin13_recomputes_the_nested_gtin13_check_digit():
    """A valid GTIN-14's nested GTIN-13 is its middle 12 digits + a RECOMPUTED check digit —
    never the GTIN-14's own check, never a naive strip. Synthetic code (repo is public)."""
    assert desadv_edi.gtin14_to_gtin13("20001112223336") == "0001112223332"
    sib = desadv_edi.gtin14_to_gtin13("20001112223336")
    assert desadv_edi._gs1_check_digit(sib[:-1]) == sib[-1]  # result is a valid GTIN-13


def test_gtin14_to_gtin13_returns_none_for_a_14char_code_that_is_not_a_valid_gtin14():
    """The Korenie class (#246): 88590000123455 is 14 chars but its own GS1 check digit does
    NOT validate — an internal identifier, not a real GTIN-14. Its naive prefix-strip
    (8590000123455) happens to be a valid GTIN-13, but the warehouse confirmed no such shorter
    CODEX card exists, so it is a FALSE code. The helper must refuse it (None), never hand back
    the plausible-but-false naive strip."""
    assert desadv_edi.gtin14_to_gtin13("88590000123455") is None
    naive = "88590000123455"[1:]  # the trap: the naive strip really does validate
    assert desadv_edi._gs1_check_digit(naive[:-1]) == naive[-1]


def test_gtin14_to_gtin13_returns_none_for_a_13char_or_non_numeric_input():
    assert desadv_edi.gtin14_to_gtin13("2000111222333") is None   # already 13 chars (synthetic)
    assert desadv_edi.gtin14_to_gtin13("") is None
    assert desadv_edi.gtin14_to_gtin13(None) is None
    assert desadv_edi.gtin14_to_gtin13("1234567890ABCD") is None  # non-numeric
    assert desadv_edi.gtin14_to_gtin13("²²²²²²²²²²²²²²") is None   # Unicode digit look-alikes

