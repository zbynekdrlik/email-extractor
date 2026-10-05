"""#485: an invoice taken as a delivery note must never ship a SECOND delivery.

Live incident (Zeelandia 9.9.): the warehouse took the delivery in by hand into CODEX at 13:34
(typing the INVOICE number into the DL field); at 19:27 the DL engine uploaded a DESADV from
the same invoice (found in spam) — its dedup only knew `desadv_sent` by (supplier, DL number),
so neither the hand-entered receipt nor the differing numbers were visible. These tests drive
the REAL invoice-as-DL path (`dl_worker.tick` → `_tick_invoice` → `_claim_invoice` →
`_run_and_finish(invoice_mode=True)`) and pin every duplicate class the owner named: CODEX
receipt by date+total and by number, our own earlier DESADV (a DL scan of the same goods),
credit notes, newer versions, accounting-mailbox forwards, a stale CODEX snapshot
(fail-closed), the board-answer reprocess, and a flag-only supplier save.

Synthetic data only (made-up supplier, numbers, addresses) — this repo is public.
"""
import os
from datetime import UTC, datetime, timedelta

from app import store
from app.config import Config
from app.httpapi import create_app
from app.orders import dl_message, dl_questions, dl_snapshot, dl_worker

SUPPLIER_EAN = "2000000000991"
SUPPLIER_EMAIL = "fakturacia@dodavatel.test"
SUPPLIER_NAME = "Testovací dodávateľ s.r.o."
ITEM_GTIN = "8588000000001"
DL_NUMBER = "4400123456"          # the DL number printed on the invoice
INVOICE_NUMBER = "2400765432"     # the invoice's own number

DL_CATALOG_CSV = ("GTIN,Názov,doplnok,hmotnost,Sklad,Cena\n"
                  f"{ITEM_GTIN},Rožok 50g,,0.05,1,0.50\n")
OBJ_CATALOG_CSV = "GTIN,Sklad,Názov,doplnok\n"
SUPPLIERS_CSV = ("Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,"
                 "Číslo mobilu,E-mail\n"
                 f"{SUPPLIER_NAME},{SUPPLIER_EAN},Mesto,,,,\n")

YESTERDAY = datetime.now(UTC) - timedelta(days=1)
DELIVERY = YESTERDAY.strftime("%d.%m.%Y")
DELIVERY_ISO = YESTERDAY.date().isoformat()


def _cfg(tmp_path, **kw):
    base = dict(pg_dsn=os.environ.get("PG_TEST_DSN", ""), data_dir=str(tmp_path),
                delivery_notes_engine="python", delivery_notes_shadow=False,
                delivery_notes_channel_id=243, api_token="tok", dash_password="secret",
                secret_key="test-secret")
    base.update(kw)
    return Config(**base)


def _setup(pg):
    dl_snapshot.import_snapshot(pg, DL_CATALOG_CSV, OBJ_CATALOG_CSV, SUPPLIERS_CSV)
    pg.execute(
        """INSERT INTO dl_supplier_overrides (ean_edi, name, emails, city,
                                              invoice_is_delivery_note)
           VALUES (%s, %s, %s, 'Mesto', true)""",
        (SUPPLIER_EAN, SUPPLIER_NAME, [SUPPLIER_EMAIL]))
    dl_snapshot.dl_rebuild_from_overrides(pg)


def _push_receipts(tmp_path, receipts=None):
    """A FRESH CODEX receipts snapshot through the real machine endpoint. Without any
    receipt for our supplier it is only the 'fresh' precondition (the gate fails closed on a
    stale/missing snapshot)."""
    receipts = receipts or [{"receipt_number": "261000001", "supplier_ico": "11111111",
                             "supplier_ean": "2000000000001", "receipt_date": DELIVERY_ISO,
                             "dl_number": "123456789", "total": 1.0}]
    app = create_app(_cfg(tmp_path))
    r = app.test_client().post(
        "/api/codex/receipts", headers={"X-Token": "tok"},
        json={"source_as_of": datetime.now(UTC).isoformat(), "days": 60,
              "receipts": receipts})
    assert r.status_code == 200, r.get_data(as_text=True)


def _message(pg, tmp_path, mid, *, category="invoices", subject="Faktúra",
             from_addr=SUPPLIER_EMAIL, text="Faktúra", created_at=None):
    pg.execute(
        """INSERT INTO messages (message_id, category, subject, from_addr, combined_text,
                                 body_text, has_attachments, processed, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, true, false, COALESCE(%s, now()))""",
        (mid, category, subject, from_addr, text, text, created_at))
    pg.execute(
        """INSERT INTO attachments (message_id, idx, filename, mime, extracted_text, method)
           VALUES (%s, 0, 'doklad.pdf', 'application/pdf', %s, 'pdf')""", (mid, text))
    d = store.message_dir(str(tmp_path), mid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "att0__doklad.pdf").write_bytes(b"%PDF-1.4 no embedded jpeg here\n")
    return mid


def _doc(doc_number=DL_NUMBER, invoice_number=INVOICE_NUMBER, total=50.0, quantity=100):
    return {"documents": [{
        "supplierName": SUPPLIER_NAME, "supplierCity": "Mesto",
        "supplierEmail": SUPPLIER_EMAIL, "docNumber": doc_number,
        "invoiceNumber": invoice_number, "deliveryDate": DELIVERY,
        "documentTotalWithoutVAT": total,
        "items": [{"name": "Rožok 50g", "quantity": quantity, "unit": "ks",
                   "unitPrice": 0.5, "totalPrice": round(quantity * 0.5, 2),
                   "vatRate": 10}]}]}


SUPPLIER_MATCHED = {"matched": True, "ean_edi": SUPPLIER_EAN, "name": SUPPLIER_NAME,
                    "matchConfidence": 0.95, "matchReason": "presná zhoda"}
ITEM_MATCHED = {"gtin": ITEM_GTIN, "matchedCatalogName": "Rožok 50g",
                "matchConfidence": 0.97, "matchReason": "presná zhoda", "mass": 0.05}


class FakeClient:
    """Scripted answers per `name=` (FIFO); records each call's name + schema."""

    def __init__(self, documents: list[dict], runs: int = 1):
        self._answers = {"dl_documents": list(documents),
                         "dl_supplier": [SUPPLIER_MATCHED] * runs,
                         "dl_item": [ITEM_MATCHED] * runs}
        self.calls: list[str] = []
        self.schemas: list[dict] = []
        self.last_prompt_hash = ""

    def json_call(self, system, user, schema, name="result"):
        self.calls.append(name)
        self.schemas.append(schema)
        self.last_prompt_hash = name
        queue = self._answers.get(name)
        if not queue:
            raise AssertionError(f"no scripted answer left for {name!r}")
        return queue.pop(0)

    def vision_call(self, *a, **kw):
        raise AssertionError("vision must not be called (machine text present)")


def _tick(pg, tmp_path, client, uploads, posts):
    return dl_worker.tick(pg, _cfg(tmp_path), client=client,
                          upload=lambda cfg, name, content, dir_override=None:
                          uploads.append((name, content)),
                          post=lambda cfg, html: posts.append(html),
                          list_dirs=lambda cfg: {"in": [], "archCodex": [], "unconfirmed": [],
                                                 "in_DL": []})


def _run_outcome(pg, mid):
    row = pg.execute("SELECT outcome FROM dl_invoice_runs WHERE message_id = %s",
                     (mid,)).fetchone()
    return row[0] if row else None


# --- duplicates against the CODEX receipts -----------------------------------------------

def test_an_invoice_the_warehouse_already_took_in_by_hand_is_not_shipped_date_total(
        pg, tmp_path):
    """The incident shape: the hand-entered receipt carries NO number we know (the warehouse
    typed something else), but same supplier, same day, same total → no second delivery."""
    _setup(pg)
    _push_receipts(tmp_path, [{
        "receipt_number": "261004409", "supplier_ico": "12345678",
        "supplier_ean": SUPPLIER_EAN, "receipt_date": DELIVERY_ISO,
        "dl_number": "999000111", "invoice_number": "", "total": 50.20}])
    _message(pg, tmp_path, "inv-1")
    uploads, posts = [], []
    client = FakeClient([_doc()])
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert uploads == [], "a duplicate DESADV reached ORION"
    assert _run_outcome(pg, "inv-1") == "duplicate"
    assert pg.execute("SELECT count(*) FROM order_questions").fetchone()[0] == 0
    assert pg.execute("SELECT count(*) FROM desadv_sent").fetchone()[0] == 0
    assert "dl_item" not in client.calls, "a duplicate must not even be matched/asked"
    assert posts == [], "a duplicate is no news for the warehouse channel"
    ev = pg.execute("SELECT outcome FROM email_events WHERE message_id='inv-1' "
                    "AND stage='invoice_dedup'").fetchone()
    assert ev and "CODEX" in ev[0] and "261004409" in ev[0]


def test_an_invoice_whose_number_the_warehouse_typed_into_codex_is_not_shipped(pg, tmp_path):
    """Number match in either field: the warehouse wrote the INVOICE number into the DL field
    of a receipt dated a week off with another total — still the same invoice."""
    _setup(pg)
    _push_receipts(tmp_path, [{
        "receipt_number": "261004500", "supplier_ico": "12345678",
        "supplier_ean": SUPPLIER_EAN,
        "receipt_date": (YESTERDAY - timedelta(days=7)).date().isoformat(),
        "dl_number": INVOICE_NUMBER, "total": 999.0}])
    _message(pg, tmp_path, "inv-2")
    uploads, posts = [], []
    assert _tick(pg, tmp_path, FakeClient([_doc()]), uploads, posts) == 1
    assert uploads == []
    assert _run_outcome(pg, "inv-2") == "duplicate"


def test_a_receipt_of_another_supplier_never_blocks_an_invoice(pg, tmp_path):
    """Same day + same total + same number — but another supplier's receipt: ships."""
    _setup(pg)
    _push_receipts(tmp_path, [{
        "receipt_number": "261004600", "supplier_ico": "87654321",
        "supplier_ean": "2000000000555", "receipt_date": DELIVERY_ISO,
        "dl_number": INVOICE_NUMBER, "total": 50.0}])
    _message(pg, tmp_path, "inv-3")
    uploads, posts = [], []
    assert _tick(pg, tmp_path, FakeClient([_doc()]), uploads, posts) == 1
    assert len(uploads) == 1
    row = pg.execute("SELECT doc_number, delivery_date::text, total_amount, invoice_number "
                     "FROM desadv_sent").fetchone()
    assert row == (DL_NUMBER, DELIVERY_ISO, 50.0, INVOICE_NUMBER), \
        "the shipped document's facts are kept for the next invoice's dedup"


# --- duplicates against our OWN earlier shipment -------------------------------------------

def test_an_invoice_of_goods_we_already_shipped_from_a_dl_scan_is_not_shipped(pg, tmp_path):
    """A DL scan of the delivery shipped first (its own DL number); the invoice of the same
    goods prints another number — same supplier + day + total → never a second DESADV."""
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "dl-scan", category="dodacie_listy", subject="Dodací list",
             text="Dodací list 7700112233")
    _message(pg, tmp_path, "inv-4")
    uploads, posts = [], []
    client = FakeClient([_doc(doc_number="7700112233", invoice_number=""), _doc()], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1      # the DL path ships the scan
    assert len(uploads) == 1
    assert _tick(pg, tmp_path, client, uploads, posts) == 1      # the invoice path
    assert len(uploads) == 1, "the invoice re-shipped goods the DL scan already delivered"
    assert _run_outcome(pg, "inv-4") == "duplicate"
    ev = pg.execute("SELECT outcome FROM email_events WHERE message_id='inv-4' "
                    "AND stage='invoice_dedup'").fetchone()
    assert ev and "7700112233" in ev[0]


# --- credit notes, versions, forwards ----------------------------------------------------

def test_a_credit_note_is_never_shipped_and_costs_no_model_call(pg, tmp_path):
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "inv-5", subject="Dobropis č. 2500111222",
             text="Opravný daňový doklad")
    uploads, posts = [], []
    client = FakeClient([_doc(total=-50.0, quantity=-100)])
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert uploads == []
    assert _run_outcome(pg, "inv-5") == "credit_note"
    assert client.calls == [], "a credit note is recognised before any extraction"


def test_a_credit_note_with_only_a_negative_total_is_never_shipped(pg, tmp_path):
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "inv-6", subject="Doklad", text="Opravný doklad")
    uploads, posts = [], []
    assert _tick(pg, tmp_path, FakeClient([_doc(total=-50.0, quantity=-100)]),
                 uploads, posts) == 1
    assert uploads == []
    assert _run_outcome(pg, "inv-6") == "credit_note"


def test_the_newest_version_of_an_invoice_wins_and_ships_exactly_once(pg, tmp_path):
    """Two versions of the same invoice waiting together: the older is skipped as superseded,
    the newer ships — never two DESADVs, never the stale content."""
    _setup(pg)
    _push_receipts(tmp_path)
    older = datetime.now(UTC) - timedelta(hours=3)
    _message(pg, tmp_path, "inv-old", text=f"Faktúra {INVOICE_NUMBER}", created_at=older)
    _message(pg, tmp_path, "inv-new", subject=f"Opravená faktúra {INVOICE_NUMBER}",
             text=f"Faktúra {INVOICE_NUMBER} (oprava)")
    uploads, posts = [], []
    client = FakeClient([_doc(quantity=100), _doc(quantity=90, total=45.0)], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert _run_outcome(pg, "inv-old") == "superseded"
    assert uploads == []
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1
    shipped = pg.execute("SELECT message_id, total_amount FROM desadv_sent").fetchall()
    assert shipped == [("inv-new", 45.0)], "the NEWEST version must be the one shipped"


def test_a_newer_credit_note_citing_the_invoice_never_supersedes_it(pg, tmp_path):
    _setup(pg)
    _push_receipts(tmp_path)
    older = datetime.now(UTC) - timedelta(hours=1)
    _message(pg, tmp_path, "inv-7", text=f"Faktúra {INVOICE_NUMBER}", created_at=older)
    _message(pg, tmp_path, "cn-7", subject="Dobropis č. 2500111223",
             text=f"Dobropis k faktúre {INVOICE_NUMBER}")
    uploads, posts = [], []
    assert _tick(pg, tmp_path, FakeClient([_doc()]), uploads, posts) == 1
    assert len(uploads) == 1, "the invoice itself still ships"
    assert _run_outcome(pg, "inv-7") == "ok"


def test_a_forward_from_our_accounting_mailbox_is_never_claimed(pg, tmp_path):
    """ucto@ forwards supplier invoices (FW: …) — never a new delivery, even when someone put
    the address on the supplier card."""
    dl_snapshot.import_snapshot(pg, DL_CATALOG_CSV, OBJ_CATALOG_CSV, SUPPLIERS_CSV)
    pg.execute(
        """INSERT INTO dl_supplier_overrides (ean_edi, name, emails, city,
                                              invoice_is_delivery_note)
           VALUES (%s, %s, %s, 'Mesto', true)""",
        (SUPPLIER_EAN, SUPPLIER_NAME, [SUPPLIER_EMAIL, "ucto@slovnormal.sk"]))
    dl_snapshot.dl_rebuild_from_overrides(pg)
    _message(pg, tmp_path, "fw-1", subject=f"FW: Faktúra {INVOICE_NUMBER}",
             from_addr="ucto@slovnormal.sk")
    msg = dl_message._claim_invoice(pg, dl_snapshot.dl_suppliers_for_management(pg),
                                    cfg=_cfg(tmp_path))
    assert msg is None
    assert pg.execute("SELECT count(*) FROM dl_invoice_runs").fetchone()[0] == 0


# --- fail-closed on a stale / missing CODEX snapshot --------------------------------------

def test_without_a_fresh_codex_receipts_snapshot_no_invoice_is_claimed(pg, tmp_path):
    _setup(pg)
    _message(pg, tmp_path, "inv-8")
    uploads, posts = [], []
    client = FakeClient([_doc()])
    assert _tick(pg, tmp_path, client, uploads, posts) == 0
    assert uploads == [] and client.calls == []
    assert pg.execute("SELECT count(*) FROM dl_invoice_runs").fetchone()[0] == 0, \
        "the invoice waits unclaimed (no attempt spent) until a fresh snapshot arrives"


def test_a_stale_codex_receipts_snapshot_holds_invoices_too(pg, tmp_path):
    _setup(pg)
    _push_receipts(tmp_path)
    pg.execute("UPDATE codex_receipt_syncs SET source_as_of = now() - interval '31 hours', "
               "synced_at = now() - interval '31 hours'")
    _message(pg, tmp_path, "inv-9")
    uploads, posts = [], []
    assert _tick(pg, tmp_path, FakeClient([_doc()]), uploads, posts) == 0
    assert uploads == []


# --- the board-answer reprocess + a flag-only supplier save --------------------------------

def test_a_board_answer_reprocesses_an_invoice_as_an_invoice(pg, tmp_path):
    """`release_for_question` used to re-run an invoice-as-DL mail through the PLAIN DL path:
    DL prompt, `messages.processed` (owned by the n8n invoice flow) set, and no dedup gate."""
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "inv-10")
    pg.execute("INSERT INTO dl_invoice_runs (message_id, outcome) VALUES ('inv-10', 'review')")
    qid = pg.execute(
        "INSERT INTO order_questions (message_id, kind, wording, status, customer_ean, "
        "item_key) VALUES ('inv-10', 'dl_item', 'Rožok 50g', 'answered', %s, 'rozok 50g') "
        "RETURNING id", (SUPPLIER_EAN,)).fetchone()[0]
    uploads, posts = [], []
    client = FakeClient([_doc()])
    dl_questions.release_for_question(
        pg, _cfg(tmp_path), qid, client=client,
        upload=lambda cfg, name, content, dir_override=None: uploads.append(name),
        post=lambda cfg, html: posts.append(html),
        list_dirs=lambda cfg: {"in": [], "archCodex": [], "unconfirmed": [], "in_DL": []})
    processed = pg.execute("SELECT processed FROM messages WHERE message_id='inv-10'"
                           ).fetchone()[0]
    assert processed is False, "the reprocess wrote the n8n invoice flow's processed flag"
    assert "invoiceNumber" in str(client.schemas[0]), "the invoice extraction was not used"
    assert len(uploads) == 1


def test_a_flag_only_supplier_save_does_not_rerun_old_stuck_mail(pg, tmp_path):
    """Turning `invoice_is_delivery_note` on changes nothing about WHO the supplier is — it must
    not re-queue (and re-post to the warehouse) the supplier's old stuck review mails."""
    _setup(pg)
    pg.execute(
        """INSERT INTO messages (message_id, category, subject, from_addr, combined_text,
                                 processed, proc_status)
           VALUES ('stuck-1', 'dodacie_listy', 'Re: objednávka', %s, 'text', true,
                   'review')""", (SUPPLIER_EMAIL,))
    card = next(s for s in dl_snapshot.dl_suppliers_for_management(pg)
                if s["ean_edi"] == SUPPLIER_EAN)
    app = create_app(_cfg(tmp_path))
    app.testing = True
    c = app.test_client()
    c.post("/login", data={"password": "secret"})
    r = c.post("/api/board/suppliers", json={
        "override_id": card["override_id"], "ean_edi": SUPPLIER_EAN, "name": SUPPLIER_NAME,
        "emails": SUPPLIER_EMAIL, "city": "Mesto", "invoice_is_delivery_note": False})
    assert r.status_code == 200, r.get_data(as_text=True)
    row = pg.execute("SELECT processed FROM messages WHERE message_id='stuck-1'").fetchone()
    assert row[0] is True, "a flag-only save re-queued an old stuck review mail"
    flag = pg.execute("SELECT invoice_is_delivery_note FROM dl_supplier_overrides "
                      "WHERE id = %s", (card["override_id"],)).fetchone()[0]
    assert flag is False
