"""#407: scanner/relay sender addresses must NEVER be learned as supplier identity.

RED tests — demonstrate that the current code (0.9.142) has NO guard against learning
a scanner sender as a supplier identity. Every test here FAILS before the fix.
"""
from __future__ import annotations

import types
from unittest.mock import MagicMock

from app.orders import dl_match, dl_matching, dl_supplier_memory


SCANNER = "tlaciaren@slovnormal.sk"
SUPPLIER_EAN = "2000000000869"
OTHER_EAN = "2000000001234"


# --- (a) remember() learns a scanner address — the bug -------------------------

def test_remember_should_refuse_scanner_sender(pg):
    """BUG: remember() unconditionally stores the scanner address as a supplier
    identity. After the fix, it must return False and store nothing."""
    # Current (broken) behavior: remember() succeeds for a scanner address
    result = dl_supplier_memory.remember(pg, SCANNER, SUPPLIER_EAN, "Dobrota")
    # This SHOULD be False (scanner addresses are not supplier identities),
    # but currently returns True — RED.
    assert result is False, (
        "remember() should refuse to store a scanner address as supplier identity")


# --- (b) _match_supplier uses poisoned memory for scanner sender — the bug -----

def test_match_supplier_should_ignore_memory_for_scanner_sender(pg):
    """BUG: when dl_supplier_memory has a (poisoned) row for the scanner address,
    `_match_supplier` uses it instead of calling the model. After the fix, the memory
    rung must be skipped for scanner senders."""
    # Pre-seed a poisoned memory row
    pg.execute(
        "INSERT INTO dl_supplier_memory (sender_email, ean_edi, name) "
        "VALUES (%s, %s, %s)",
        (SCANNER, SUPPLIER_EAN, "Dobrota"))

    suppliers = [{"ean_edi": SUPPLIER_EAN, "name": "Dobrota", "city": "Dolný Kubín",
                  "emails": []},
                 {"ean_edi": OTHER_EAN, "name": "Lesaffre", "city": "Bratislava",
                  "emails": []}]
    doc = {"supplierName": "Lesaffre CZ", "supplierEmail": "",
           "supplierCity": "Bratislava"}

    # Mock the LLM client — should be called if memory is skipped
    client = MagicMock()
    client.json_call.return_value = {
        "matched": True, "ean_edi": OTHER_EAN, "name": "Lesaffre",
        "matchConfidence": 0.9, "matchReason": "name match"}

    result = dl_matching._match_supplier(pg, client, doc, suppliers,
                                          sender_email=SCANNER)
    # Currently (broken): returns Dobrota from the poisoned memory, model never called.
    # After fix: model is called, returns Lesaffre.
    assert client.json_call.called, (
        "model should be called when sender is a scanner (memory rung skipped)")
    assert result.ean_edi == OTHER_EAN, (
        "should match Lesaffre via model, not Dobrota via poisoned memory")


# --- (c) resolve_supplier_from_cards rung 2 matches scanner email — the bug ----

def test_rung2_should_exclude_scanner_email():
    """BUG: rung 2 of resolve_supplier_from_cards matches the scanner address
    against a card's emails list. When the document's printed name does NOT match
    any card's name (no contradiction guard fires), rung 2 wrongly resolves to the
    card that has the scanner email. After the fix, scanner emails must be excluded."""
    cards = [{"ean_edi": SUPPLIER_EAN, "name": "Dobrota Orava",
              "city": "Dolný Kubín", "emails": [SCANNER]}]
    # Document with a supplier name that does NOT match any card — no contradiction
    # guard fires, so rung 2's email match is the only thing that fires.
    doc = {"supplierName": "Neznámy dodávateľ", "supplierEmail": "",
           "supplierCity": ""}

    # Current (broken): rung 2 matches Dobrota via the scanner email in sender_email,
    # because the name "Neznámy dodávateľ" has no card match to trigger the contradiction.
    # After fix: scanner emails excluded from rung 2, returns None (no match).
    result = dl_match.resolve_supplier_from_cards(doc, cards, sender_email=SCANNER)
    assert result is None, (
        "should NOT match any card via a scanner email — scanner is not an identity")
