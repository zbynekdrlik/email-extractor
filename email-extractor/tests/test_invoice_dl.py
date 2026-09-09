"""#406: invoice-as-delivery-note dual routing tests.

Tests the per-supplier `invoice_is_delivery_note` flag, the independent `dl_invoice_runs`
ledger, prompt variant routing, and the guarantee that `messages.processed` is NEVER
touched by the DL invoice path.
"""
from __future__ import annotations

import types
import unittest.mock

from app.orders import dl_extract, dl_message, dl_snapshot

# --- fixtures (CSV strings, matching the `import_snapshot` API) ------------

DL_CATALOG_CSV = ("GTIN,Názov,doplnok,hmotnost,Sklad,Cena\n"
                  "5901234123457,Škoricový cukor,,,,6.39\n")
OBJ_CATALOG_CSV = "GTIN,Sklad,Názov,doplnok\n"
SUPPLIERS_CSV = ("Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,"
                 "Číslo mobilu,E-mail\n"
                 "Zeelandia s.r.o.,2000000000285,Rozhanovce,,,,\n")


# --- helpers ---------------------------------------------------------------

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
    """Seed a minimal DL catalog + supplier snapshot."""
    return dl_snapshot.import_snapshot(pg, DL_CATALOG_CSV, OBJ_CATALOG_CSV, SUPPLIERS_CSV)


def _insert_invoice_message(pg, message_id="inv-test-001",
                            from_addr="noreply@inforcloudsuite.com",
                            subject="Zeelandia faktura c. 526013012"):
    """Insert a category='invoices' message into the messages table."""
    pg.execute(
        """INSERT INTO messages (message_id, subject, from_addr, from_name, body_text,
                                  combined_text, category, has_attachments, status, processed)
           VALUES (%s, %s, %s, '', 'faktura text', 'faktura text', 'invoices', true,
                   'classified', false)""",
        (message_id, subject, from_addr))
    # Insert a dummy attachment
    pg.execute(
        """INSERT INTO attachments (message_id, idx, filename, mime, extracted_text, method,
                                     needs_vision)
           VALUES (%s, 0, 'PF_0_0.pdf', 'application/pdf', 'Dodací list 1149285\nŠkoricový cukor 15 KG\nZáklad dane: 191.70', 'pdf', false)""",
        (message_id,))


def _flag_supplier(pg, ean_edi="2000000000285", emails=None):
    """Create a dl_supplier_overrides row with invoice_is_delivery_note=true."""
    if emails is None:
        emails = ["noreply@inforcloudsuite.com"]
    pg.execute(
        """INSERT INTO dl_supplier_overrides
               (ean_edi, name, emails, city, invoice_is_delivery_note)
           VALUES (%s, 'Zeelandia s.r.o.', %s, 'Rozhanovce', true)""",
        (ean_edi, emails))


class FakeClient:
    """Minimal fake LLM client for testing extraction routing."""
    def __init__(self, documents=None):
        self.last_prompt_hash = ""
        self._documents = documents or []
        self.prompts_used = []

    def json_call(self, prompt, user, schema, name=""):
        self.last_prompt_hash = "fake-hash"
        self.prompts_used.append(prompt[:100])  # capture first 100 chars
        return {"documents": self._documents}

    def vision_call(self, prompt, **kw):
        return []


# --- test: _invoice_supplier_emails (F4: exact email, not domain) ----------

def test_invoice_supplier_emails_returns_flagged_only():
    suppliers = [
        {"ean_edi": "111", "name": "A", "emails": ["a@x.com"],
         "invoice_is_delivery_note": True},
        {"ean_edi": "222", "name": "B", "emails": ["b@y.com"],
         "invoice_is_delivery_note": False},
        {"ean_edi": "333", "name": "C", "emails": [],
         "invoice_is_delivery_note": True},
    ]
    result = dl_message._invoice_supplier_emails(suppliers)
    assert "a@x.com" in result
    assert result["a@x.com"]["ean_edi"] == "111"
    assert "b@y.com" not in result
    # C has no emails, so no entry


def test_invoice_supplier_emails_empty_when_no_flag():
    suppliers = [
        {"ean_edi": "111", "name": "A", "emails": ["a@x.com"],
         "invoice_is_delivery_note": False},
    ]
    result = dl_message._invoice_supplier_emails(suppliers)
    assert result == {}


# --- test: _claim_invoice --------------------------------------------------

def test_claim_invoice_claims_matching_message(pg):
    _snapshot(pg)
    _insert_invoice_message(pg)
    _flag_supplier(pg)
    suppliers = dl_snapshot.dl_suppliers_for_management(pg)
    msg = dl_message._claim_invoice(pg, suppliers, cfg=_cfg())
    assert msg is not None
    assert msg["message_id"] == "inv-test-001"
    assert msg["_invoice_supplier"]["ean_edi"] == "2000000000285"
    # Verify the dl_invoice_runs ledger row was created
    row = pg.execute("SELECT message_id, outcome FROM dl_invoice_runs "
                     "WHERE message_id = 'inv-test-001'").fetchone()
    assert row is not None
    assert row[0] == "inv-test-001"
    assert row[1] is None  # outcome not yet set


def test_claim_invoice_skips_non_flagged_supplier(pg):
    _snapshot(pg)
    _insert_invoice_message(pg)
    # No _flag_supplier call — no flagged supplier exists
    suppliers = dl_snapshot.dl_suppliers_for_management(pg)
    msg = dl_message._claim_invoice(pg, suppliers, cfg=_cfg())
    assert msg is None


def test_claim_invoice_idempotent_second_claim(pg):
    _snapshot(pg)
    _insert_invoice_message(pg)
    _flag_supplier(pg)
    suppliers = dl_snapshot.dl_suppliers_for_management(pg)
    msg1 = dl_message._claim_invoice(pg, suppliers, cfg=_cfg())
    assert msg1 is not None
    # Second claim for the same message returns None
    msg2 = dl_message._claim_invoice(pg, suppliers, cfg=_cfg())
    assert msg2 is None


def test_claim_invoice_never_touches_messages_processed(pg):
    _snapshot(pg)
    _insert_invoice_message(pg)
    _flag_supplier(pg)
    suppliers = dl_snapshot.dl_suppliers_for_management(pg)
    dl_message._claim_invoice(pg, suppliers, cfg=_cfg())
    # messages.processed must still be false (untouched)
    row = pg.execute("SELECT processed, processing_at FROM messages "
                     "WHERE message_id = 'inv-test-001'").fetchone()
    assert row[0] is False
    assert row[1] is None


def test_claim_invoice_skips_scanner_sender(pg):
    _snapshot(pg)
    _insert_invoice_message(pg, from_addr="tlaciaren@slovnormal.sk")
    _flag_supplier(pg, emails=["tlaciaren@slovnormal.sk"])
    suppliers = dl_snapshot.dl_suppliers_for_management(pg)
    msg = dl_message._claim_invoice(
        pg, suppliers,
        cfg=_cfg(delivery_notes_scanner_senders="tlaciaren@slovnormal.sk"))
    assert msg is None


# --- test: invoice prompt variant ------------------------------------------

def test_extract_prompt_default_excludes_invoices():
    prompt = dl_extract.extract_prompt(invoice_mode=False)
    assert "Faktúra" in prompt
    assert "nikdy neextrahuj ako" in prompt.lower() or "účtovný doklad" in prompt


def test_extract_prompt_invoice_mode_lifts_exclusion():
    prompt = dl_extract.extract_prompt(invoice_mode=True)
    # The invoice prompt should mention deriving DL from invoice
    assert "faktúra" in prompt.lower()
    assert "dodací list" in prompt.lower() or "číslo dodacieho" in prompt.lower()
    # It should NOT contain the standard exclusion language
    assert "účtovný doklad za už dodaný" not in prompt


def test_extract_email_forwards_invoice_mode():
    """Verify that invoice_mode is threaded through to run_extraction."""
    prompts_seen = []
    original_run = dl_extract.run_extraction

    def spy_run(client, text, *, invoice_mode=False):
        prompts_seen.append(invoice_mode)
        return original_run(client, text, invoice_mode=invoice_mode)

    client = FakeClient(documents=[])
    atts = [{"idx": 0, "filename": "test.pdf", "pdf_bytes": b"",
             "machine_text": "some text"}]

    with unittest.mock.patch.object(dl_extract, "run_extraction", spy_run):
        dl_extract.extract_email(client, atts, invoice_mode=True)

    assert prompts_seen == [True]


# --- test: _finish_invoice_run ---------------------------------------------

def test_finish_invoice_run_records_outcome(pg):
    _snapshot(pg)
    _insert_invoice_message(pg)
    _flag_supplier(pg)
    suppliers = dl_snapshot.dl_suppliers_for_management(pg)
    dl_message._claim_invoice(pg, suppliers, cfg=_cfg())
    dl_message._finish_invoice_run(pg, "inv-test-001", "ok")
    row = pg.execute("SELECT outcome FROM dl_invoice_runs "
                     "WHERE message_id = 'inv-test-001'").fetchone()
    assert row[0] == "ok"


# --- test: migration r12 ---------------------------------------------------

def test_migration_r12_adds_column_and_table(pg):
    """Verify the migration added the invoice_is_delivery_note column and
    the dl_invoice_runs table."""
    # Column exists
    row = pg.execute(
        """SELECT column_name FROM information_schema.columns
           WHERE table_name = 'dl_supplier_overrides'
             AND column_name = 'invoice_is_delivery_note'""").fetchone()
    assert row is not None
    # Table exists
    row = pg.execute(
        """SELECT table_name FROM information_schema.tables
           WHERE table_name = 'dl_invoice_runs'""").fetchone()
    assert row is not None


# --- test: /znalosti dl-suppliers API exposes the flag ---------------------

def test_znalosti_dl_suppliers_returns_flag(pg):
    _snapshot(pg)
    _flag_supplier(pg)
    suppliers = dl_snapshot.dl_suppliers_for_management(pg)
    flagged = [s for s in suppliers if s.get("invoice_is_delivery_note")]
    assert len(flagged) >= 1
    assert flagged[0]["ean_edi"] == "2000000000285"
