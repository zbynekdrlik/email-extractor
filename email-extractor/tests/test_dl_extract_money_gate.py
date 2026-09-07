"""#393: money_gate review message must say WHAT is missing when line prices are zero.

Pre-fix: money_gate only reports the numeric mismatch ("0.00 vs 113.54") — the
warehouse cannot tell whether prices, items, or quantities are the problem.

Post-fix: when every item's totalPrice is 0 (or missing) but a real doc_total exists,
the message explicitly says "AI neprečítala ceny riadkov" so the warehouse knows
what to check manually.
"""
from app.orders import dl_extract


def test_money_gate_says_prices_missing_when_all_line_totals_zero():
    """Items exist with names/quantities but all totalPrice = 0, doc_total > 0
    -> the review message must mention missing prices, not just the numeric diff."""
    doc = {
        "documentTotalWithoutVAT": 113.54,
        "items": [
            {"name": "Mlieko 1L", "quantity": 10, "unit": "ks",
             "unitPrice": 0, "totalPrice": 0},
            {"name": "Maslo 250g", "quantity": 5, "unit": "ks",
             "unitPrice": 0, "totalPrice": 0},
        ],
    }
    reason = dl_extract.money_gate(doc)
    assert reason is not None
    # The message must tell the warehouse that LINE PRICES are missing
    assert "neprečítala ceny riadkov" in reason, (
        f"money_gate reason does not mention missing prices: {reason!r}")


def test_money_gate_normal_mismatch_unchanged():
    """A normal mismatch (some prices exist but don't add up) should NOT
    mention missing prices — only the numeric diff."""
    doc = {
        "documentTotalWithoutVAT": 100.00,
        "items": [
            {"name": "A", "quantity": 1, "unit": "ks",
             "unitPrice": 50.0, "totalPrice": 50.0},
        ],
    }
    reason = dl_extract.money_gate(doc)
    assert reason is not None
    # Normal mismatch message (50 vs 100) — should be the standard format
    assert "50.00" in reason and "100.00" in reason


def test_money_gate_passes_when_totals_match():
    """Sanity: money_gate returns None when line totals match doc total."""
    doc = {
        "documentTotalWithoutVAT": 10.00,
        "items": [
            {"name": "A", "quantity": 10, "unit": "ks",
             "unitPrice": 1.0, "totalPrice": 10.0},
        ],
    }
    assert dl_extract.money_gate(doc) is None


def test_validate_document_zero_items_with_doc_total_says_what_is_missing():
    """#393 review finding 1: when items=[] but doc_total > 0, the review message
    must mention that ITEMS (not just prices) could not be read."""
    doc = {
        "supplierName": "DOBROTA Orava",
        "docNumber": "126046192",
        "deliveryDate": "02.09.2026",
        "documentTotalWithoutVAT": 113.54,
        "items": [],
    }
    result = dl_extract.validate_document(doc)
    assert result["status"] == "needsReview"
    # The review message must mention what is missing
    assert "položky" in result["reviewReason"].lower() or "položk" in result["reviewReason"].lower(), (
        f"zero-items review reason does not mention missing items: {result['reviewReason']!r}")
    assert "113.54" in result["reviewReason"], (
        f"zero-items review reason does not mention the doc total: {result['reviewReason']!r}")
