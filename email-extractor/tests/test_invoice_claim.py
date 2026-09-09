"""#412: dl_invoice_runs — attempts + stale reclaim + max-attempts alert.

RED tests: these MUST fail before the implementation and pass after.
"""
from __future__ import annotations

import types

from app.orders import claim, dl_alerts, dl_message, dl_snapshot


# --- fixtures ---------------------------------------------------------------

DL_CATALOG_CSV = ("GTIN,Názov,doplnok,hmotnost,Sklad,Cena\n"
                  "5901234123457,Škoricový cukor,,,,6.39\n")
OBJ_CATALOG_CSV = "GTIN,Sklad,Názov,doplnok\n"
SUPPLIERS_CSV = ("Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,"
                 "Číslo mobilu,E-mail\n"
                 "Zeelandia s.r.o.,2000000000285,Rozhanovce,,,,\n")


def _cfg(**kw):
    base = {
        "delivery_notes_engine": "python",
        "delivery_notes_shadow": False,
        "delivery_notes_max_age_days": 14,
        "delivery_notes_channel_id": 243,
        "data_dir": "/data/store",
        "delivery_notes_scanner_senders": "",
    }
    base.update(kw)
    return types.SimpleNamespace(**base)


def _snapshot(pg):
    return dl_snapshot.import_snapshot(pg, DL_CATALOG_CSV, OBJ_CATALOG_CSV, SUPPLIERS_CSV)


def _insert_invoice_message(pg, message_id="inv-claim-001",
                            from_addr="noreply@inforcloudsuite.com",
                            subject="Zeelandia faktura c. 526013012"):
    pg.execute(
        """INSERT INTO messages (message_id, subject, from_addr, from_name, body_text,
                                  combined_text, category, has_attachments, status, processed)
           VALUES (%s, %s, %s, '', 'faktura text', 'faktura text', 'invoices', true,
                   'classified', false)""",
        (message_id, subject, from_addr))
    pg.execute(
        """INSERT INTO attachments (message_id, idx, filename, mime, extracted_text, method,
                                     needs_vision)
           VALUES (%s, 0, 'PF_0_0.pdf', 'application/pdf',
                   'Dodací list 1149285\nŠkoricový cukor 15 KG', 'pdf', false)""",
        (message_id,))


def _flag_supplier(pg, ean_edi="2000000000285", emails=None):
    if emails is None:
        emails = ["noreply@inforcloudsuite.com"]
    pg.execute(
        """INSERT INTO dl_supplier_overrides
               (ean_edi, name, emails, city, invoice_is_delivery_note)
           VALUES (%s, 'Zeelandia s.r.o.', %s, 'Rozhanovce', true)""",
        (ean_edi, emails))


# --- test: claim.ledger_claim -----------------------------------------------

def test_ledger_claim_fresh_insert(pg):
    """A fresh claim inserts a new row with attempts=1."""
    pg.execute(
        """CREATE TABLE IF NOT EXISTS _test_ledger (
               pk TEXT PRIMARY KEY,
               claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
               attempts INT NOT NULL DEFAULT 1,
               outcome TEXT,
               finished_at TIMESTAMPTZ)""")
    ok, attempts = claim.ledger_claim(
        pg, table="_test_ledger", pk_col="pk", pk_val="msg-1",
        stale_minutes=30, max_attempts=5)
    assert ok is True
    assert attempts == 1


def test_ledger_claim_refuses_fresh_row(pg):
    """A second claim on a freshly-claimed row is refused."""
    pg.execute(
        """CREATE TABLE IF NOT EXISTS _test_ledger (
               pk TEXT PRIMARY KEY,
               claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
               attempts INT NOT NULL DEFAULT 1,
               outcome TEXT,
               finished_at TIMESTAMPTZ)""")
    ok1, _ = claim.ledger_claim(
        pg, table="_test_ledger", pk_col="pk", pk_val="msg-1",
        stale_minutes=30, max_attempts=5)
    assert ok1 is True
    ok2, _ = claim.ledger_claim(
        pg, table="_test_ledger", pk_col="pk", pk_val="msg-1",
        stale_minutes=30, max_attempts=5)
    assert ok2 is False


def test_ledger_claim_reclaims_stale(pg):
    """A stale row (claimed_at old, outcome IS NULL) is reclaimed with attempts+1."""
    pg.execute(
        """CREATE TABLE IF NOT EXISTS _test_ledger (
               pk TEXT PRIMARY KEY,
               claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
               attempts INT NOT NULL DEFAULT 1,
               outcome TEXT,
               finished_at TIMESTAMPTZ)""")
    # Insert and then age the claimed_at
    claim.ledger_claim(
        pg, table="_test_ledger", pk_col="pk", pk_val="msg-1",
        stale_minutes=30, max_attempts=5)
    pg.execute(
        "UPDATE _test_ledger SET claimed_at = now() - interval '31 minutes' "
        "WHERE pk = 'msg-1'")
    ok, attempts = claim.ledger_claim(
        pg, table="_test_ledger", pk_col="pk", pk_val="msg-1",
        stale_minutes=30, max_attempts=5)
    assert ok is True
    assert attempts == 2


def test_ledger_claim_refuses_exhausted(pg):
    """A row at max_attempts is never reclaimed, even when stale."""
    pg.execute(
        """CREATE TABLE IF NOT EXISTS _test_ledger (
               pk TEXT PRIMARY KEY,
               claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
               attempts INT NOT NULL DEFAULT 1,
               outcome TEXT,
               finished_at TIMESTAMPTZ)""")
    pg.execute(
        "INSERT INTO _test_ledger (pk, attempts, claimed_at) "
        "VALUES ('msg-1', 5, now() - interval '60 minutes')")
    ok, _ = claim.ledger_claim(
        pg, table="_test_ledger", pk_col="pk", pk_val="msg-1",
        stale_minutes=30, max_attempts=5)
    assert ok is False


def test_ledger_claim_refuses_finished(pg):
    """A row with a non-NULL outcome is never reclaimed."""
    pg.execute(
        """CREATE TABLE IF NOT EXISTS _test_ledger (
               pk TEXT PRIMARY KEY,
               claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
               attempts INT NOT NULL DEFAULT 1,
               outcome TEXT,
               finished_at TIMESTAMPTZ)""")
    pg.execute(
        "INSERT INTO _test_ledger (pk, attempts, outcome, finished_at, claimed_at) "
        "VALUES ('msg-1', 1, 'ok', now(), now() - interval '60 minutes')")
    ok, _ = claim.ledger_claim(
        pg, table="_test_ledger", pk_col="pk", pk_val="msg-1",
        stale_minutes=30, max_attempts=5)
    assert ok is False


def test_ledger_finish_sets_outcome_and_finished_at(pg):
    """ledger_finish records outcome + finished_at."""
    pg.execute(
        """CREATE TABLE IF NOT EXISTS _test_ledger (
               pk TEXT PRIMARY KEY,
               claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
               attempts INT NOT NULL DEFAULT 1,
               outcome TEXT,
               finished_at TIMESTAMPTZ)""")
    claim.ledger_claim(
        pg, table="_test_ledger", pk_col="pk", pk_val="msg-1",
        stale_minutes=30, max_attempts=5)
    claim.ledger_finish(
        pg, table="_test_ledger", pk_col="pk", pk_val="msg-1",
        outcome="ok")
    row = pg.execute(
        "SELECT outcome, finished_at FROM _test_ledger WHERE pk = 'msg-1'"
    ).fetchone()
    assert row[0] == "ok"
    assert row[1] is not None  # finished_at was set


# --- test: _claim_invoice with stale reclaim --------------------------------

def test_claim_invoice_reclaims_stale_row(pg):
    """A stale dl_invoice_runs row is reclaimed with attempts incremented."""
    _snapshot(pg)
    _insert_invoice_message(pg)
    _flag_supplier(pg)
    suppliers = dl_snapshot.dl_suppliers_for_management(pg)

    # First claim
    msg1 = dl_message._claim_invoice(pg, suppliers, cfg=_cfg())
    assert msg1 is not None
    assert msg1.get("attempts", 0) == 1

    # Age the claim
    pg.execute(
        "UPDATE dl_invoice_runs SET claimed_at = now() - interval '31 minutes' "
        "WHERE message_id = 'inv-claim-001'")

    # Second claim should succeed (stale reclaim)
    msg2 = dl_message._claim_invoice(pg, suppliers, cfg=_cfg())
    assert msg2 is not None
    assert msg2["message_id"] == "inv-claim-001"
    assert msg2.get("attempts", 0) == 2


def test_claim_invoice_max_attempts_blocks_reclaim(pg):
    """At MAX_ATTEMPTS, even a stale row is not reclaimed."""
    _snapshot(pg)
    _insert_invoice_message(pg)
    _flag_supplier(pg)
    suppliers = dl_snapshot.dl_suppliers_for_management(pg)

    # Simulate a row at MAX_ATTEMPTS
    pg.execute(
        "INSERT INTO dl_invoice_runs (message_id, attempts, claimed_at) "
        "VALUES ('inv-claim-001', %s, now() - interval '60 minutes')",
        (dl_message.MAX_ATTEMPTS,))

    msg = dl_message._claim_invoice(pg, suppliers, cfg=_cfg())
    assert msg is None


def test_claim_invoice_finished_row_never_reclaimed(pg):
    """A finished (outcome IS NOT NULL) row is never reclaimed."""
    _snapshot(pg)
    _insert_invoice_message(pg)
    _flag_supplier(pg)
    suppliers = dl_snapshot.dl_suppliers_for_management(pg)

    pg.execute(
        "INSERT INTO dl_invoice_runs (message_id, outcome, finished_at) "
        "VALUES ('inv-claim-001', 'ok', now())")

    msg = dl_message._claim_invoice(pg, suppliers, cfg=_cfg())
    assert msg is None


def test_finish_invoice_run_sets_finished_at(pg):
    """_finish_invoice_run now sets finished_at alongside outcome."""
    _snapshot(pg)
    _insert_invoice_message(pg)
    _flag_supplier(pg)
    suppliers = dl_snapshot.dl_suppliers_for_management(pg)
    dl_message._claim_invoice(pg, suppliers, cfg=_cfg())
    dl_message._finish_invoice_run(pg, "inv-claim-001", "ok")
    row = pg.execute(
        "SELECT outcome, finished_at FROM dl_invoice_runs "
        "WHERE message_id = 'inv-claim-001'").fetchone()
    assert row[0] == "ok"
    assert row[1] is not None


# --- test: max-attempts ops alert -------------------------------------------

def test_invoice_max_attempts_enqueues_alert(pg):
    """When a message hits MAX_ATTEMPTS, an ops alert is enqueued."""
    _snapshot(pg)
    _insert_invoice_message(pg)
    _flag_supplier(pg)

    # Simulate a row at MAX_ATTEMPTS with no outcome (exhausted)
    pg.execute(
        "INSERT INTO dl_invoice_runs (message_id, attempts, claimed_at) "
        "VALUES ('inv-claim-001', %s, now())",
        (dl_message.MAX_ATTEMPTS,))

    # The park function should enqueue an alert
    dl_message._park_exhausted_invoice(pg, "inv-claim-001",
                                       channel_id=243)
    row = pg.execute(
        "SELECT kind, message_id FROM pending_alerts "
        "WHERE message_id = 'inv-claim-001'").fetchone()
    assert row is not None
    assert row[0] == "dl_invoice_exhausted"

    # And the dl_invoice_runs row should be finished with 'exhausted'
    row = pg.execute(
        "SELECT outcome, finished_at FROM dl_invoice_runs "
        "WHERE message_id = 'inv-claim-001'").fetchone()
    assert row[0] == "exhausted"
    assert row[1] is not None
