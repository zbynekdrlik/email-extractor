"""EDI writer for the n8n **Static auto orders** workflow's `generator` node (#131).

`app/orders/edi.py` is a faithful port of a DIFFERENT n8n Code node — "ASSEMBLE AND
GENERATE EDI" in the "AI auto orders" workflow — not this one. Static orders' own
`generator` node (workflow `O8IYhUESjaWmPMTI`) has real, confirmed divergences from
that other writer: it dates the header from the order's own `issueDate` (not "today"),
front-truncates a long order number via its generic `pad()` (not `edi.py`'s special
last-15-chars truncation), keeps buyer name and delivery-location name as two DISTINCT
header fields (`edi.py` conflates them into one `store` param), computes its own store
EAN via `PARTNER_CONFIG`/`LABAS_STORES`/`KARMEN_CASH_STORES`, drops (not transliterates)
any non-ASCII character outside its own diacritics map, and uses an entirely different
filename convention. This module is the 1:1 port of THAT node.

`fixtures/static_edi_reference.json` was produced by running the PRODUCTION `generator`
node's VERBATIM JS source (fetched via `get_workflow_details`, not reimplemented) under
node — same "run the real node under node" technique `edi_reference.json` already uses
for the other writer, applied here with a mocked `$('Get Catalog Snapshot').all()`. The
real-corpus byte-parity proof (12 real processed static orders, pulled from this add-on's
own Postgres) lives outside git on dev2 per this repo's public/PII policy — see
`.claude/rules/orders-corpus.md` and the design comment on #131.
"""
import json
from pathlib import Path

from app.orders import static_edi

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "static_edi_reference.json").read_text(encoding="utf-8"))


def _build(case):
    return static_edi.build(case["input"]["order_data"], case["input"]["catalog"])


# --- byte parity with the production generator node -----------------------

def test_the_writer_matches_the_production_generator_byte_for_byte():
    for case in FIXTURE:
        got = _build(case)
        exp = case["expected"]
        assert got.content == exp["content"], case["name"]
        assert got.filename == exp["filename"], case["name"]
        assert got.store_ean == exp["storeEAN"], case["name"]
        assert got.buyer_name == exp["buyerName"], case["name"]
        assert got.items_with_ean == exp["itemsWithEAN"], case["name"]
        assert got.items_skipped == exp["itemsSkipped"], case["name"]
        assert got.skipped_items == exp["skippedItems"], case["name"]


def test_the_header_is_exactly_1157_characters():
    case = next(c for c in FIXTURE if c["name"] == "karmen_basic_two_items")
    got = _build(case)
    assert len(got.content.split("\r\n")[0]) == 1157


def test_lines_end_with_crlf_and_the_file_ends_with_sum():
    case = next(c for c in FIXTURE if c["name"] == "karmen_basic_two_items")
    got = _build(case)
    assert "\r\n" in got.content and got.content.split("\r\n")[-1] == "SUM"


# --- the confirmed divergences from edi.py (AI-orders' writer) -----------

def test_header_dates_from_issue_date_not_todays_wall_clock():
    """Field A is `issueDate` (the order's OWN date), never `datetime.now()` — the
    opposite of edi.py's `build(..., today=...)`."""
    case = next(c for c in FIXTURE if c["name"] == "karmen_basic_two_items")
    got = _build(case)
    # issueDate "01.08.2026" -> "20260801", appearing before the "   " + storeEAN block.
    assert "20260801" in got.content.split("\r\n")[0]


def test_a_long_order_number_is_truncated_from_the_front_not_the_tail():
    """edi.py special-cases order numbers to keep the LAST 15 chars. The real
    `generator` node has no such special case — its generic `pad()` keeps the FIRST 15
    chars, dropping the tail. `fullOrderNumber` here is 26 chars of A-Z."""
    case = next(c for c in FIXTURE
                if c["name"] == "long_order_number_truncated_from_front_not_tail")
    got = _build(case)
    hdr = got.content.split("\r\n")[0]
    assert "ABCDEFGHIJKLMNO" in hdr          # first 15 chars survive
    assert "PQRSTUVWXYZ" not in hdr          # the tail is gone
    assert got.filename == case["expected"]["filename"]


def test_buyer_name_and_delivery_location_are_distinct_header_fields():
    """`edi.py` writes the SAME `store` string into both header slots. The real
    generator writes the partner's fixed buyer name in one and the specific delivery
    location in the other — different text for a partner with several stores."""
    case = next(c for c in FIXTURE if c["name"] == "karmen_basic_two_items")
    got = _build(case)
    assert got.buyer_name == "KARMEN - velkoobchod potravin s.r.o."
    assert "KARMEN 19, Testovo" in got.content
    assert got.buyer_name not in "KARMEN 19, Testovo"


def test_store_ean_resolves_via_partner_config_and_prev_number():
    case = next(c for c in FIXTURE if c["name"] == "karmen_basic_two_items")
    got = _build(case)
    assert got.store_ean == "8589364472019"        # PARTNER_CONFIG['KARMEN'] + '019'


def test_labas_store_lookup_resolves_by_exact_name():
    case = next(c for c in FIXTURE if c["name"] == "labas_exact_store_lookup")
    got = _build(case)
    assert got.store_ean == case["expected"]["storeEAN"]


def test_karmen_cash_store_lookup_resolves_by_token_subset():
    case = next(c for c in FIXTURE
                if c["name"] == "karmen_cash_token_subset_store_lookup")
    got = _build(case)
    assert got.store_ean == case["expected"]["storeEAN"]


def test_unmapped_non_ascii_is_dropped_not_transliterated():
    """`edi.py`'s `_strip_diacritics` NFKD-folds an unmapped character (e.g. an en dash
    survives elsewhere as ASCII "-"-ish where possible). The real `generator`'s
    `toAscii` just DELETES anything not in its explicit map — an en dash vanishes
    entirely rather than becoming "-"."""
    case = next(c for c in FIXTURE
                if c["name"] == "unmapped_non_ascii_is_dropped_not_transliterated")
    got = _build(case)
    assert got.content.isascii()
    assert "–" not in got.content    # en dash
    assert "-" not in got.content.split("\r\n")[0][110:180]   # not transliterated either
    assert got.filename == case["expected"]["filename"]


def test_product_ean_by_code_override_wins_before_catalog():
    case = next(c for c in FIXTURE if c["name"] == "product_ean_by_code_override")
    got = _build(case)
    assert "8588001805609" in got.content   # PRODUCT_EAN_BY_CODE['9485']
    assert got.items_skipped == 0


def test_product_ean_by_name_override_matches_substring():
    case = next(c for c in FIXTURE if c["name"] == "product_ean_by_name_override")
    got = _build(case)
    assert "8588001805777" in got.content   # PRODUCT_EAN_BY_NAME['LUPACKA 75']
    assert got.items_skipped == 0


def test_an_unmatched_item_is_skipped_from_the_file_and_named():
    case = next(c for c in FIXTURE if c["name"] == "unmatched_item_is_skipped")
    got = _build(case)
    assert got.items_skipped == 1
    assert got.skipped_items == case["expected"]["skippedItems"]
    assert "Uplne neznamy produkt XYZ" not in got.content


def test_filename_convention_differs_from_ai_orders_writer():
    """`<PARTNER>_<orderNumber with / -> _>_<prevPart>.txt` — NOT edi.py's
    `ORDER_<ean tail>_<PO>_<delivery date>_<stamp>.txt`."""
    case = next(c for c in FIXTURE if c["name"] == "karmen_basic_two_items")
    got = _build(case)
    assert got.filename == "KARMEN_999_26_019.txt"


def test_missing_delivery_location_falls_back_to_partner_and_prev_number():
    case = next(c for c in FIXTURE
                if c["name"] == "missing_delivery_location_falls_back_to_partner_prevnumber")
    got = _build(case)
    assert "KARMEN 19" in got.content
