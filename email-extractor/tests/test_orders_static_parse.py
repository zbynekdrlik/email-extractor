"""Faithful 1:1 Python port of the n8n "Static auto orders" workflow's `extractor` node
(#68, workflow O8IYhUESjaWmPMTI, node "Order Extractor v2 — NEW combined_text layout").

Same relationship as `static_ean.py` has to `generator`'s `getProductEAN()`: n8n's JS Code
node cannot run in this repo's CI, so this module is a hand-kept port and CI-tested proof
of correctness, NOT yet wired into the live pipeline. All fixtures below are SYNTHETIC —
constructed to match the documented template shapes, never real customer mail (this repo
is public).
"""
import pytest

from app.orders import static_parse as sp

# --- to_ascii / clean_invisible -----------------------------------------------------------

def test_to_ascii_folds_slovak_diacritics_and_punctuation():
    assert sp.to_ascii("Šiška žltá ďateľ ťava ňaň ôsmy ĺúbosť ŕeč") == \
        "Siska zlta datel tava nan osmy lubost rec"
    assert sp.to_ascii("“citát” – pomlčka…") == '"citat" - pomlcka...'


def test_to_ascii_drops_unmapped_non_ascii():
    assert sp.to_ascii("emoji 🎉 stays gone") == "emoji  stays gone"


def test_clean_invisible_replaces_zero_width_space_with_a_real_space():
    """The AGEL Levoca incident (#41): a zero-width space glued between two tokens made a
    plain \\s+ regex silently fail to match — cleaned once before any parser runs."""
    assert sp.clean_invisible("45​ks") == "45 ks"


# --- detect_partner --------------------------------------------------------------------

def test_detect_partner_priority_cash_and_carry_before_plain_karmen():
    assert sp.detect_partner("KARMEN CASH AND CARRY Zvolen") == "KARMEN_CASH"
    assert sp.detect_partner("Odberateľ: KARMEN 7") == "KARMEN"


def test_detect_partner_komfos_and_labas():
    assert sp.detect_partner("KOMFOS Trencin") == "KOMFOS"
    assert sp.detect_partner("LABAS s.r.o.") == "LABAS"
    assert sp.detect_partner("LABAŠ s.r.o.") == "LABAS"


def test_detect_partner_unknown():
    assert sp.detect_partner("nič z toho") == "UNKNOWN"


# --- parse_order_number ------------------------------------------------------------------

def test_parse_order_number_primary_pattern():
    d = sp.parse_order_number("Vyšlá objednávka č.: 12345/2026")
    assert d == {"fullOrderNumber": "12345/2026", "orderNumber": "12345", "orderYear": "2026"}


def test_parse_order_number_fallback_pattern():
    d = sp.parse_order_number("OBJEDNÁVKA č.: 202607010001")
    assert d["fullOrderNumber"] == "202607010001"
    assert d["orderNumber"] == "202607010001"
    assert d["orderYear"] == "2026"


def test_parse_order_number_missing():
    assert sp.parse_order_number("žiadne číslo tu") == \
        {"fullOrderNumber": None, "orderNumber": None, "orderYear": None}


# --- parse_delivery_date / parse_issue_date ----------------------------------------------

def test_parse_delivery_date_three_fallback_wordings():
    assert sp.parse_delivery_date("Termín dodávky: 03.08.2026") == "03.08.2026"
    assert sp.parse_delivery_date("Termín dodania: 04.08.2026") == "04.08.2026"
    assert sp.parse_delivery_date("Dátum dodania tovaru: 05.08.2026") == "05.08.2026"
    assert sp.parse_delivery_date("nič") is None


def test_parse_issue_date():
    assert sp.parse_issue_date("Dátum vystavenia: 01.08.2026") == "01.08.2026"
    assert sp.parse_issue_date("nič") is None


# --- parse_location ------------------------------------------------------------------------

def test_parse_location_karmen():
    loc = sp.parse_location("Prev.:7\nKARMEN 7, Hlavná 1, Banská Bystrica", "KARMEN")
    assert loc == {"prevNumber": "7", "deliveryLocationName": "KARMEN 7, Hlavná 1, Banská Bystrica"}


def test_parse_location_karmen_prev_number_falls_back_to_the_location_line():
    loc = sp.parse_location("KARMEN 9, Zvolenská 2", "KARMEN")
    assert loc["prevNumber"] == "9"


def test_parse_location_komfos():
    text = "Termín dodávky: 03.08.2026\nKOMFOS Predajňa Trenčín, Hlavná 5"
    loc = sp.parse_location(text, "KOMFOS")
    assert loc["deliveryLocationName"] == "KOMFOS Predajňa Trenčín, Hlavná 5"


def test_parse_location_karmen_cash():
    loc = sp.parse_location("prevádzka: KARMEN CASH AND CARRY Zvolen, Nám. SNP 10", "KARMEN_CASH")
    assert loc["deliveryLocationName"] == "KARMEN CASH AND CARRY, Zvolen, Nám. SNP 10"
    assert loc["prevNumber"] == "Zvolen, Nám. SNP 10"


def test_parse_location_labas():
    loc = sp.parse_location("LABAS s.r.o. KS/OC Predajňa Zvolen MOBIL 0900123456", "LABAS")
    assert loc == {"prevNumber": "Predajňa Zvolen",
                   "deliveryLocationName": "LABAS s.r.o. KS/OC Predajňa Zvolen"}


def test_parse_location_unknown_partner_is_empty():
    assert sp.parse_location("hocičo", "UNKNOWN") == \
        {"prevNumber": None, "deliveryLocationName": None}


# --- item parsers ----------------------------------------------------------------------

KARMEN_ORDER = """Vyšlá objednávka č.: 12345/2026
Dátum vystavenia: 01.08.2026
Termín dodávky: 03.08.2026
KARMEN 7, Hlavná 1, Banská Bystrica
Prev.:7

Množstvo
P100 8588001805647 Rožok štandart 50g   20,000 ks   0,45
Chlieb 1000g   5,000 ks
SLIMAK 80g VANILKOVY   3,000 ks   1,10
Nákupná cena spolu
"""

KARMEN_CASH_ORDER = """prevádzka: KARMEN CASH AND CARRY Zvolen, Námestie SNP 10
Termín dodávky: 03.08.2026
Dátum vystavenia: 01.08.2026
Int. kód a názov tovaru
Katalógové číslo a názov tovaru
1 K001 10,5 KS 8588001805647 1 2,50 26,25
Rožok štandart 50g
Poznámka:
"""

LABAS_ORDER = """LABAS s.r.o. KS/OC Predajňa Zvolen MOBIL 0900123456
Termín dodania: 03.08.2026
Dátum vystavenia: 01.08.2026
Celkom
1 45231 K002 Chlieb ražný 1000g 3 10,5 ks 1,20 12,60 132,30
Kusový EAN kód: 8588001805579
Celková hmotnosť: 10 kg
"""


def test_parse_vysla_items_reads_code_ean_description_qty_price():
    items = sp.parse_vysla_items(KARMEN_ORDER)
    assert items[0] == {"code": "P100", "ean": "8588001805647", "description": "Rožok štandart 50g",
                        "quantity": 20.0, "unit": "ks", "unitPrice": 0.45, "lineTotal": 9.0}


def test_parse_vysla_items_line_with_no_ean_falls_back_to_bare_description():
    items = sp.parse_vysla_items(KARMEN_ORDER)
    assert items[1] == {"code": None, "ean": None, "description": "Chlieb 1000g",
                        "quantity": 5.0, "unit": "ks", "unitPrice": None, "lineTotal": None}


def test_parse_vysla_items_excludes_the_hardcoded_product():
    items = sp.parse_vysla_items(KARMEN_ORDER)
    assert not any("SLIMAK" in (i["description"] or "") for i in items)
    assert len(items) == 2


def test_parse_karmen_cash_items_reads_two_physical_lines_per_item():
    items = sp.parse_karmen_cash_items(KARMEN_CASH_ORDER)
    assert items == [{"code": "K001", "ean": "8588001805647", "description": "Rožok štandart 50g",
                      "quantity": 10.5, "unit": "ks", "unitPrice": 2.50, "lineTotal": 26.25}]


def test_parse_labas_items_reads_the_trailing_ean_line():
    items = sp.parse_labas_items(LABAS_ORDER)
    assert items == [{"code": "45231", "ean": "8588001805579", "description": "Chlieb ražný 1000g",
                      "quantity": 10.5, "unit": "ks", "unitPrice": 12.60, "lineTotal": 132.3}]


def test_parse_labas_items_without_an_ean_line_leaves_ean_none():
    text = LABAS_ORDER.replace("Kusový EAN kód: 8588001805579\n", "")
    items = sp.parse_labas_items(text)
    assert items[0]["ean"] is None


# --- extract_order_data (the full orchestration) ------------------------------------------

def test_extract_order_data_karmen_end_to_end():
    d = sp.extract_order_data(KARMEN_ORDER)
    assert d["partner"] == "KARMEN"
    assert d["fullOrderNumber"] == "12345/2026"
    assert d["deliveryDate"] == "03.08.2026"
    assert d["issueDate"] == "01.08.2026"
    assert d["prevNumber"] == "7"
    assert d["itemCount"] == 2
    assert d["totalQuantity"] == 25.0
    assert d.get("skip") is not True


def test_extract_order_data_karmen_cash_dispatches_to_the_right_parser():
    d = sp.extract_order_data(KARMEN_CASH_ORDER)
    assert d["partner"] == "KARMEN_CASH"
    assert d["itemCount"] == 1
    assert d["items"][0]["code"] == "K001"


def test_extract_order_data_labas_dispatches_to_the_right_parser():
    d = sp.extract_order_data(LABAS_ORDER)
    assert d["partner"] == "LABAS"
    assert d["itemCount"] == 1
    assert d["items"][0]["ean"] == "8588001805579"


def test_extract_order_data_with_zero_items_is_a_skip_not_an_error():
    """KARMEN and others occasionally send a PDF order with a valid header but zero lines
    ("BEZ OBJEDNAVKY") — not a real order, and the workflow marks it skipped, not errored."""
    text = "Vyšlá objednávka č.: 1/2026\nDátum vystavenia: 01.08.2026\nTermín dodávky: 02.08.2026\n" \
           "Množstvo\nNákupná cena spolu\n"
    d = sp.extract_order_data(text)
    assert d["skip"] is True and d["skipReason"] == "empty_order"
    assert d["items"] == []


def test_extract_order_data_missing_delivery_date_raises():
    with pytest.raises(sp.OrderExtractionError, match="dátum dodania"):
        sp.extract_order_data("Dátum vystavenia: 01.08.2026\nMnožstvo\nNákupná cena spolu\n")


def test_extract_order_data_missing_issue_date_raises():
    with pytest.raises(sp.OrderExtractionError, match="dátum vystavenia"):
        sp.extract_order_data("Termín dodávky: 01.08.2026\nMnožstvo\nNákupná cena spolu\n")


# --- parse_static_order (the n8n MAIN wrapper: invisible cleaning + photo guard) -----------

def test_parse_static_order_end_to_end():
    d = sp.parse_static_order(KARMEN_ORDER, has_attachments=False)
    assert d["partner"] == "KARMEN"
    assert d["itemCount"] == 2


def test_parse_static_order_rejects_empty_input():
    with pytest.raises(sp.MissingInputText):
        sp.parse_static_order("", has_attachments=False)


def test_parse_static_order_photo_only_body_needs_vision():
    """komfos.67 sends orders as PNG photos with an empty combined_text body (#47) — the
    deterministic parser has nothing to read and must say so, not guess."""
    with pytest.raises(sp.PhotoOrderNeedsVision):
        sp.parse_static_order("Subject: objednavka\nFrom: komfos@example.sk\nBody:",
                              has_attachments=True)


def test_parse_static_order_short_body_with_no_attachment_is_not_a_photo_guard():
    """The photo guard only fires when an attachment exists — a short body alone (e.g. the
    empty-order case) must still reach the real parser and its own error/skip handling."""
    with pytest.raises(sp.OrderExtractionError):
        sp.parse_static_order("Subject: x\nFrom: y\nBody:", has_attachments=False)
