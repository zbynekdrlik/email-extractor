"""#407: scanner/relay sender addresses must NEVER be learned as supplier identity.

Tests that the fix guards all five poisoning paths:
(a) remember() with cfg refuses a scanner address
(b) _match_supplier skips memory + rung 2 for scanner senders
(c) resolve_supplier_from_cards excludes scanner emails from rung 2
(d) migration removes seeded scanner rows + overrides emails
(e) is_scanner_sender helper works correctly
"""
from __future__ import annotations

import types
from unittest.mock import MagicMock

from app.orders import dl_match, dl_matching, dl_supplier_memory
from app.orders.dl_questions import is_scanner_sender

SCANNER = "tlaciaren@slovnormal.sk"
SUPPLIER_EAN = "2000000000869"
OTHER_EAN = "2000000001234"


# --- (a) remember() with cfg refuses a scanner address -------------------------

def test_remember_scanner_sender_is_noop_with_cfg(pg):
    """With cfg provided, remember() must refuse to store a scanner address."""
    cfg = types.SimpleNamespace(delivery_notes_scanner_senders=SCANNER)
    result = dl_supplier_memory.remember(pg, SCANNER, SUPPLIER_EAN, "Dobrota",
                                         cfg=cfg)
    assert result is False, "remember() should return False for a scanner address"
    assert dl_supplier_memory.resolve(pg, SCANNER) is None, (
        "no row should exist for a scanner address")


def test_remember_non_scanner_still_works(pg):
    """Non-scanner addresses must still be learned normally."""
    cfg = types.SimpleNamespace(delivery_notes_scanner_senders=SCANNER)
    result = dl_supplier_memory.remember(pg, "real@supplier.sk", SUPPLIER_EAN,
                                         "Dobrota", cfg=cfg)
    assert result is True
    assert dl_supplier_memory.resolve(pg, "real@supplier.sk") is not None


def test_remember_without_cfg_still_works(pg):
    """Backward compat: without cfg, remember() still works (no scanner guard)."""
    result = dl_supplier_memory.remember(pg, SCANNER, SUPPLIER_EAN, "Dobrota")
    assert result is True, "without cfg, remember() should still store anything"


# --- (b) _match_supplier skips memory + rung 2 for scanner sender -------------

def test_match_supplier_ignores_memory_for_scanner_sender(pg):
    """With cfg, _match_supplier skips the memory rung for scanner senders."""
    # Pre-seed a poisoned memory row
    pg.execute(
        "INSERT INTO dl_supplier_memory (sender_email, ean_edi, name) "
        "VALUES (%s, %s, %s)",
        (SCANNER, SUPPLIER_EAN, "Dobrota"))

    cfg = types.SimpleNamespace(delivery_notes_scanner_senders=SCANNER)
    suppliers = [{"ean_edi": SUPPLIER_EAN, "name": "Dobrota", "city": "Dolny Kubin",
                  "emails": []},
                 {"ean_edi": OTHER_EAN, "name": "Lesaffre", "city": "Bratislava",
                  "emails": []}]
    doc = {"supplierName": "Lesaffre CZ", "supplierEmail": "",
           "supplierCity": "Bratislava"}

    client = MagicMock()
    client.json_call.return_value = {
        "matched": True, "ean_edi": OTHER_EAN, "name": "Lesaffre",
        "matchConfidence": 0.9, "matchReason": "name match"}

    result = dl_matching._match_supplier(pg, client, doc, suppliers,
                                          sender_email=SCANNER, cfg=cfg)
    assert client.json_call.called, (
        "model should be called when sender is a scanner (memory rung skipped)")
    assert result.ean_edi == OTHER_EAN, (
        "should match Lesaffre via model, not Dobrota via poisoned memory")


# --- (c) resolve_supplier_from_cards excludes scanner emails from rung 2 ------

def test_rung2_excludes_scanner_email():
    """Scanner emails excluded from rung 2's wanted set, so it falls through."""
    cards = [{"ean_edi": SUPPLIER_EAN, "name": "Dobrota Orava",
              "city": "Dolny Kubin", "emails": [SCANNER]}]
    doc = {"supplierName": "Neznamy dodavatel", "supplierEmail": "",
           "supplierCity": ""}

    result = dl_match.resolve_supplier_from_cards(
        doc, cards, sender_email=SCANNER,
        exclude_emails=frozenset({SCANNER}))
    assert result is None, (
        "should NOT match any card via a scanner email")


def test_rung2_still_works_for_non_scanner_emails():
    """Non-scanner emails in rung 2 must still match normally."""
    real_email = "obchod@dobrota.sk"
    cards = [{"ean_edi": SUPPLIER_EAN, "name": "Dobrota Orava",
              "city": "Dolny Kubin", "emails": [real_email]}]
    doc = {"supplierName": "Neznamy dodavatel", "supplierEmail": "",
           "supplierCity": ""}

    result = dl_match.resolve_supplier_from_cards(
        doc, cards, sender_email=real_email,
        exclude_emails=frozenset({SCANNER}))
    assert result is not None
    assert result.ean_edi == SUPPLIER_EAN, (
        "non-scanner email should still match via rung 2")


# --- (d) migration removes seeded scanner rows --------------------------------

def test_migration_removes_scanner_memory_rows(pg):
    """Revision 11 deletes dl_supplier_memory rows for scanner senders."""
    pg.execute(
        "INSERT INTO dl_supplier_memory (sender_email, ean_edi, name) "
        "VALUES (%s, %s, %s)",
        (SCANNER, SUPPLIER_EAN, "Dobrota"))
    assert pg.execute(
        "SELECT count(*) FROM dl_supplier_memory WHERE sender_email = %s",
        (SCANNER,)).fetchone()[0] == 1

    from app.db import REVISIONS
    rev11 = next((r for r in REVISIONS if r.revision == 11), None)
    assert rev11 is not None, "revision 11 must exist"
    for stmt in rev11.statements:
        pg.execute(stmt)

    assert pg.execute(
        "SELECT count(*) FROM dl_supplier_memory WHERE sender_email = %s",
        (SCANNER,)).fetchone()[0] == 0, (
        "migration must delete scanner sender memory rows")


def test_migration_removes_scanner_email_from_overrides(pg):
    """Revision 11 removes scanner addresses from dl_supplier_overrides.emails."""
    pg.execute(
        "INSERT INTO dl_supplier_overrides (ean_edi, name, city, emails) "
        "VALUES (%s, %s, %s, %s)",
        (SUPPLIER_EAN, "Dobrota", "Dolny Kubin",
         "{tlaciaren@slovnormal.sk,real@dobrota.sk}"))
    row = pg.execute(
        "SELECT emails FROM dl_supplier_overrides WHERE ean_edi = %s",
        (SUPPLIER_EAN,)).fetchone()
    assert SCANNER in row[0]

    from app.db import REVISIONS
    rev11 = next((r for r in REVISIONS if r.revision == 11), None)
    assert rev11 is not None, "revision 11 must exist"
    for stmt in rev11.statements:
        pg.execute(stmt)

    row = pg.execute(
        "SELECT emails FROM dl_supplier_overrides WHERE ean_edi = %s",
        (SUPPLIER_EAN,)).fetchone()
    assert SCANNER not in (row[0] if row else []), (
        "migration must remove scanner email from overrides")
    # The other email must survive
    assert "real@dobrota.sk" in row[0], (
        "non-scanner emails must be preserved")


# --- (e) is_scanner_sender helper works correctly -----------------------------

def test_is_scanner_sender_positive():
    cfg = types.SimpleNamespace(delivery_notes_scanner_senders="tlaciaren@slovnormal.sk")
    assert is_scanner_sender(cfg, "tlaciaren@slovnormal.sk") is True
    assert is_scanner_sender(cfg, "Tlaciaren@slovnormal.sk") is True
    assert is_scanner_sender(cfg, " tlaciaren@slovnormal.sk ") is True


def test_is_scanner_sender_negative():
    cfg = types.SimpleNamespace(delivery_notes_scanner_senders="tlaciaren@slovnormal.sk")
    assert is_scanner_sender(cfg, "real@supplier.sk") is False
    assert is_scanner_sender(cfg, "") is False
    assert is_scanner_sender(cfg, None) is False


def test_is_scanner_sender_empty_config():
    cfg = types.SimpleNamespace(delivery_notes_scanner_senders="")
    assert is_scanner_sender(cfg, "tlaciaren@slovnormal.sk") is False
