"""#485: an invoice taken as a delivery note must never ship a SECOND delivery.

Live incident (Zeelandia 9.9.): the warehouse took the delivery in by hand into CODEX at 13:34
(typing the INVOICE number into the DL field); at 19:27 the DL engine uploaded a DESADV from
the same invoice (found in spam) — its dedup only knew `desadv_sent` by (supplier, DL number),
so neither the hand-entered receipt nor the differing numbers were visible. These tests drive
the REAL invoice-as-DL path (`dl_worker.tick` → `_tick_invoice` → `_claim_invoice` →
`_run_and_finish(invoice_mode=True)`) and pin every duplicate class the owner named: CODEX
receipt by date+total and by number, our own earlier DESADV (a DL scan of the same goods — also
a priceless one, and in the reverse order), credit notes, newer versions, accounting-mailbox
forwards, a stale / not-yet-covering CODEX snapshot (fail-closed), the board-answer reprocess,
turning the flag on (no backlog) and a flag-only supplier save.

Synthetic data only (made-up supplier, numbers, addresses) — this repo is public.
"""
import os
from datetime import UTC, datetime, timedelta

from app import store
from app.config import Config
from app.httpapi import create_app
from app.orders import dl_message, dl_questions, dl_snapshot, dl_worker

SUPPLIER_EAN = "2000000000991"
OTHER_EAN = "2000000000992"
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
                 f"{SUPPLIER_NAME},{SUPPLIER_EAN},Mesto,,,,\n"
                 f"Iný dodávateľ s.r.o.,{OTHER_EAN},Iné mesto,,,,\n")

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


# An old, unrelated receipt of OUR supplier: the copy must carry the supplier's EAN at all, or
# its invoices wait (round 4 — an EAN CODEX does not know is invisible to the gate).
COVERAGE_RECEIPT = {"receipt_number": "261000099", "supplier_ico": "12345678",
                    "supplier_eans": [SUPPLIER_EAN],
                    "receipt_date": (YESTERDAY - timedelta(days=40)).date().isoformat(),
                    "dl_numbers": ["990000099"], "total": 1.0}


def _push_receipts(tmp_path, receipts=None):
    """A FRESH CODEX receipts snapshot through the real machine endpoint, its data as of NOW
    (so it covers every mail inserted before it). Without any receipt for our supplier it is
    only the 'fresh' precondition (the gate fails closed on a stale/missing snapshot); an old
    unrelated receipt of our supplier rides along when none carries its EAN (coverage)."""
    receipts = list(receipts or [{"receipt_number": "261000001", "supplier_ico": "11111111",
                                  "supplier_eans": ["2000000000001"],
                                  "receipt_date": DELIVERY_ISO, "dl_numbers": ["123456789"],
                                  "total": 1.0}])
    if not any(SUPPLIER_EAN in r.get("supplier_eans", []) for r in receipts):
        receipts.append(COVERAGE_RECEIPT)
    app = create_app(_cfg(tmp_path))
    r = app.test_client().post(
        "/api/codex/receipts", headers={"X-Token": "tok"},
        json={"source_as_of": datetime.now(UTC).isoformat(), "days": 60,
              "receipts": receipts}, query_string={"force": "1"})
    assert r.status_code == 200, r.get_data(as_text=True)


def _message(pg, tmp_path, mid, *, category="invoices", subject="Faktúra",
             from_addr=SUPPLIER_EMAIL, text="Faktúra", created_at=None,
             attachments=(("doklad.pdf", None),)):
    """A mail received an hour ago (before any snapshot pushed in the test) by default."""
    created_at = created_at or datetime.now(UTC) - timedelta(hours=1)
    pg.execute(
        """INSERT INTO messages (message_id, category, subject, from_addr, combined_text,
                                 body_text, has_attachments, processed, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, true, false, %s)""",
        (mid, category, subject, from_addr, text, text, created_at))
    d = store.message_dir(str(tmp_path), mid)
    d.mkdir(parents=True, exist_ok=True)
    for idx, (filename, att_text) in enumerate(attachments):
        pg.execute(
            """INSERT INTO attachments (message_id, idx, filename, mime, extracted_text,
                                        method)
               VALUES (%s, %s, %s, 'application/pdf', %s, 'pdf')""",
            (mid, idx, filename, att_text if att_text is not None else text))
        (d / f"att{idx}__{filename}").write_bytes(b"%PDF-1.4 no embedded jpeg here\n")
    return mid


def _one(doc_number=DL_NUMBER, invoice_number=INVOICE_NUMBER, total=50.0, quantity=100,
         priced=True):
    item = {"name": "Rožok 50g", "quantity": quantity, "unit": "ks", "vatRate": 10}
    if priced:
        item.update(unitPrice=0.5, totalPrice=round(quantity * 0.5, 2))
    return {"supplierName": SUPPLIER_NAME, "supplierCity": "Mesto",
            "supplierEmail": SUPPLIER_EMAIL, "docNumber": doc_number,
            "invoiceNumber": invoice_number, "deliveryDate": DELIVERY,
            "documentTotalWithoutVAT": total if priced else 0, "items": [item]}


def _doc(**kw):
    return {"documents": [_one(**kw)]}


def _supplier(ean=SUPPLIER_EAN):
    return {"matched": True, "ean_edi": ean, "name": SUPPLIER_NAME,
            "matchConfidence": 0.95, "matchReason": "presná zhoda"}


ITEM_MATCHED = {"gtin": ITEM_GTIN, "matchedCatalogName": "Rožok 50g",
                "matchConfidence": 0.97, "matchReason": "presná zhoda", "mass": 0.05}


class FakeClient:
    """Scripted answers per `name=` (FIFO); records each call's name + schema. A queued
    answer that is an Exception is raised instead (a model failure)."""

    def __init__(self, documents: list, runs: int = 1, supplier=None):
        self._answers = {"dl_documents": list(documents),
                         "dl_supplier": [supplier or _supplier()] * runs,
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
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def vision_call(self, *a, **kw):
        raise AssertionError("vision must not be called (machine text present)")


def _tick(pg, tmp_path, client, uploads, posts, **cfg_kw):
    return dl_worker.tick(pg, _cfg(tmp_path, **cfg_kw), client=client,
                          upload=lambda cfg, name, content, dir_override=None:
                          uploads.append((name, content)),
                          post=lambda cfg, html: posts.append(html),
                          list_dirs=lambda cfg: {"in": [], "archCodex": [], "unconfirmed": [],
                                                 "in_DL": []})


def _run_outcome(pg, mid):
    row = pg.execute("SELECT outcome FROM dl_invoice_runs WHERE message_id = %s",
                     (mid,)).fetchone()
    return row[0] if row else None


def _dedup_event(pg, mid):
    row = pg.execute("SELECT outcome FROM email_events WHERE message_id = %s "
                     "AND stage = 'invoice_dedup' ORDER BY id DESC LIMIT 1", (mid,)).fetchone()
    return row[0] if row else None


# --- duplicates against the CODEX receipts -----------------------------------------------

def test_an_invoice_the_warehouse_already_took_in_by_hand_is_not_shipped_date_total(
        pg, tmp_path):
    """The incident shape: the hand-entered receipt carries NO number we know (the warehouse
    typed something else), but same supplier, same day, same total → no second delivery."""
    _setup(pg)
    _message(pg, tmp_path, "inv-1")
    _push_receipts(tmp_path, [{
        "receipt_number": "261004409", "supplier_ico": "12345678",
        "supplier_eans": [SUPPLIER_EAN], "receipt_date": DELIVERY_ISO,
        "dl_numbers": ["999000111"], "invoice_number": "", "total": 50.20}])
    uploads, posts = [], []
    client = FakeClient([_doc()])
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert uploads == [], "a duplicate DESADV reached ORION"
    assert _run_outcome(pg, "inv-1") == "duplicate"
    assert pg.execute("SELECT count(*) FROM order_questions").fetchone()[0] == 0
    assert pg.execute("SELECT count(*) FROM desadv_sent").fetchone()[0] == 0
    assert "dl_item" not in client.calls, "a duplicate must not even be matched/asked"
    assert posts == [], "a duplicate is no news for the warehouse channel"
    ev = _dedup_event(pg, "inv-1")
    assert ev and "CODEX" in ev and "261004409" in ev


def test_an_invoice_whose_number_the_warehouse_typed_into_codex_is_not_shipped(pg, tmp_path):
    """Number match in either field: the warehouse wrote the INVOICE number into the DL field
    of a receipt dated a week off with another total — still the same invoice. The receipt's
    supplier carries several EDI EANs in CODEX; ours is one of them."""
    _setup(pg)
    _message(pg, tmp_path, "inv-2")
    _push_receipts(tmp_path, [{
        "receipt_number": "261004500", "supplier_ico": "12345678",
        "supplier_eans": ["2000000000777", SUPPLIER_EAN],
        "receipt_date": (YESTERDAY - timedelta(days=7)).date().isoformat(),
        "dl_numbers": [INVOICE_NUMBER], "total": 999.0}])
    uploads, posts = [], []
    assert _tick(pg, tmp_path, FakeClient([_doc()]), uploads, posts) == 1
    assert uploads == []
    assert _run_outcome(pg, "inv-2") == "duplicate"


def test_a_receipt_of_another_supplier_never_blocks_an_invoice(pg, tmp_path):
    """Same day + same total + same number — but another supplier's receipt: ships."""
    _setup(pg)
    _message(pg, tmp_path, "inv-3")
    _push_receipts(tmp_path, [{
        "receipt_number": "261004600", "supplier_ico": "87654321",
        "supplier_eans": ["2000000000555"], "receipt_date": DELIVERY_ISO,
        "dl_numbers": [INVOICE_NUMBER], "total": 50.0}])
    uploads, posts = [], []
    assert _tick(pg, tmp_path, FakeClient([_doc()]), uploads, posts) == 1
    assert len(uploads) == 1
    row = pg.execute("SELECT doc_number, delivery_date::text, total_amount, invoice_number, "
                     "items FROM desadv_sent").fetchone()
    assert row == (DL_NUMBER, DELIVERY_ISO, 50.0, INVOICE_NUMBER,
                   [[ITEM_GTIN, 100.0, "ks"]]), \
        "the shipped document's facts are kept for the next document's dedup"


def test_the_supplier_the_document_resolves_to_is_checked_too(pg, tmp_path):
    """The claimed supplier's receipts show nothing, but the document resolves to ANOTHER
    supplier whose receipt has this invoice — the second gate after the supplier match."""
    _setup(pg)
    _message(pg, tmp_path, "inv-4")
    _push_receipts(tmp_path, [{
        "receipt_number": "261004700", "supplier_ico": "22222222",
        "supplier_eans": [OTHER_EAN], "receipt_date": DELIVERY_ISO,
        "dl_numbers": [INVOICE_NUMBER], "total": 50.0}])
    uploads, posts = [], []
    client = FakeClient([_doc()], supplier=_supplier(OTHER_EAN))
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert uploads == []
    assert _run_outcome(pg, "inv-4") == "duplicate"
    assert "dl_supplier" in client.calls and "dl_item" not in client.calls


# --- duplicates against our OWN shipments ------------------------------------------------

def test_an_invoice_of_goods_we_already_shipped_from_a_dl_scan_is_not_shipped(pg, tmp_path):
    """A DL scan of the delivery shipped first (its own DL number); the invoice of the same
    goods prints another number — same supplier + day + total → never a second DESADV."""
    _setup(pg)
    _message(pg, tmp_path, "dl-scan", category="dodacie_listy", subject="Dodací list",
             text="Dodací list 7700112233")
    _message(pg, tmp_path, "inv-5")
    _push_receipts(tmp_path)
    uploads, posts = [], []
    client = FakeClient([_doc(doc_number="7700112233", invoice_number=""), _doc()], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1      # the DL path ships the scan
    assert len(uploads) == 1
    assert _tick(pg, tmp_path, client, uploads, posts) == 1      # the invoice path
    assert len(uploads) == 1, "the invoice re-shipped goods the DL scan already delivered"
    assert _run_outcome(pg, "inv-5") == "duplicate"
    assert "7700112233" in _dedup_event(pg, "inv-5")


def test_an_invoice_after_a_priceless_dl_scan_of_the_same_goods_is_not_shipped(pg, tmp_path):
    """A DL scan carries no prices (no total to compare) and another number — the invoice is
    still recognised: same day + the SAME [card, quantity] content of the generated EDI."""
    _setup(pg)
    _message(pg, tmp_path, "dl-scan", category="dodacie_listy", subject="Dodací list",
             text="Dodací list 7700112233")
    _message(pg, tmp_path, "inv-6")
    _push_receipts(tmp_path)
    uploads, posts = [], []
    client = FakeClient([_doc(doc_number="7700112233", invoice_number="", priced=False),
                         _doc()], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1
    assert _run_outcome(pg, "inv-6") == "duplicate"
    assert "rovnaké položky" in _dedup_event(pg, "inv-6")


def test_a_dl_scan_after_the_invoice_already_shipped_is_not_shipped(pg, tmp_path):
    """The reverse order (LESAFFRE sends both): the invoice shipped first, then the DL scan of
    the same goods arrives on the DL path with another number and no prices."""
    _setup(pg)
    _message(pg, tmp_path, "inv-7")
    _push_receipts(tmp_path)
    uploads, posts = [], []
    client = FakeClient([_doc(), _doc(doc_number="7700112233", invoice_number="",
                                      priced=False)], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1
    _message(pg, tmp_path, "dl-scan", category="dodacie_listy", subject="Dodací list",
             text="Dodací list 7700112233")
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1, "the DL scan re-shipped goods the invoice already delivered"
    ev = pg.execute("SELECT outcome, detail->'twin'->>'ref' FROM email_events WHERE "
                    "message_id='dl-scan' AND stage='duplicate_skip'").fetchone()
    assert ev and ev[1] == DL_NUMBER and "z faktúry" in ev[0]


def test_an_invoice_pdf_and_the_dl_pdf_in_one_mail_are_one_delivery(pg, tmp_path):
    """One mail, two documents of the same goods (the invoice + its DL, different numbers):
    the second must meet the first one's row — rows of the same mail are not all excluded,
    only the very document's own."""
    _setup(pg)
    _message(pg, tmp_path, "inv-8")
    _push_receipts(tmp_path)
    uploads, posts = [], []
    client = FakeClient([{"documents": [
        _one(), _one(doc_number="7700112233", invoice_number="")]}], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1
    assert pg.execute("SELECT count(*) FROM desadv_sent").fetchone()[0] == 1
    assert client.calls.count("dl_supplier") == 1, "the second document stopped at the gate"


# --- credit notes, versions, forwards, attachments ---------------------------------------

def test_a_credit_note_is_never_shipped_and_costs_no_model_call(pg, tmp_path):
    _setup(pg)
    _message(pg, tmp_path, "inv-9", subject="Dobropis č. 2500111222",
             text="Opravný daňový doklad")
    _push_receipts(tmp_path)
    uploads, posts = [], []
    client = FakeClient([_doc(total=-50.0, quantity=-100)])
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert uploads == []
    assert _run_outcome(pg, "inv-9") == "credit_note"
    assert client.calls == [], "a credit note is recognised before any extraction"


def test_a_credit_note_with_only_a_negative_total_is_never_shipped(pg, tmp_path):
    _setup(pg)
    _message(pg, tmp_path, "inv-10", subject="Doklad", text="Opravný doklad")
    _push_receipts(tmp_path)
    uploads, posts = [], []
    assert _tick(pg, tmp_path, FakeClient([_doc(total=-50.0, quantity=-100)]),
                 uploads, posts) == 1
    assert uploads == []
    assert _run_outcome(pg, "inv-10") == "credit_note"


def test_a_credit_note_attachment_is_dropped_but_the_invoice_beside_it_ships(pg, tmp_path):
    """A mixed mail (invoice + dobropis PDFs): only the credit-note attachment (its header says
    so) is dropped; a later mention of credit notes deep in the invoice text kills nothing."""
    _setup(pg)
    footer = "Faktúra 2400765432\n" + "x" * 400 + "\nReklamácie riešime dobropisom."
    _message(pg, tmp_path, "inv-11", subject="Doklady",
             attachments=(("faktura.pdf", footer),
                          ("1326100113.pdf", "Faktúra - dobropis 1/1 Číslo 1326100113")))
    _push_receipts(tmp_path)
    uploads, posts = [], []
    client = FakeClient([_doc()])
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1
    assert client.calls.count("dl_documents") == 1, "the credit-note PDF was not extracted"


def test_the_newest_version_of_an_invoice_wins_and_ships_exactly_once(pg, tmp_path):
    """Two versions of the same invoice waiting together: the NEWEST is taken first and ships;
    the older then meets it by number — never two DESADVs, never the stale content."""
    _setup(pg)
    _message(pg, tmp_path, "inv-old", text=f"Faktúra {INVOICE_NUMBER}",
             created_at=datetime.now(UTC) - timedelta(hours=3))
    _message(pg, tmp_path, "inv-new", subject=f"Opravená faktúra {INVOICE_NUMBER}",
             text=f"Faktúra {INVOICE_NUMBER} (oprava)")
    _push_receipts(tmp_path)
    uploads, posts = [], []
    client = FakeClient([_doc(quantity=90, total=45.0), _doc(quantity=100)], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1
    shipped = pg.execute("SELECT message_id, total_amount FROM desadv_sent").fetchall()
    assert shipped == [("inv-new", 45.0)], "the NEWEST version must be the one shipped"
    assert _run_outcome(pg, "inv-old") == "duplicate"


def test_a_newer_mail_that_ships_nothing_never_blocks_the_older_invoice(pg, tmp_path):
    """A newer credit note citing the invoice (or a reminder) is taken first and ships
    nothing — the older invoice still ships."""
    _setup(pg)
    _message(pg, tmp_path, "inv-12", text=f"Faktúra {INVOICE_NUMBER}",
             created_at=datetime.now(UTC) - timedelta(hours=2))
    _message(pg, tmp_path, "cn-12", subject="Dobropis č. 2500111223",
             text=f"Dobropis k faktúre {INVOICE_NUMBER}")
    _push_receipts(tmp_path)
    uploads, posts = [], []
    client = FakeClient([_doc()])
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert _run_outcome(pg, "cn-12") == "credit_note"
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1, "the invoice itself still ships"
    assert _run_outcome(pg, "inv-12") == "ok"


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


def test_an_invoice_mails_banner_image_costs_no_vision_call_and_no_review(pg, tmp_path):
    """EKVIA mails every invoice PDF (text) with a 43 kB marketing banner JPEG that ingest
    flags `needs_vision`. Read as a DL source it cost a vision call and — no delivery note in
    a banner — raised a "this attachment has no DL" review on the warehouse channel for EVERY
    invoice. In invoice mode the text document IS the invoice; image-only extras are dropped."""
    _setup(pg)
    _message(pg, tmp_path, "inv-13")
    pg.execute(
        """INSERT INTO attachments (message_id, idx, filename, mime, extracted_text, method,
                                    needs_vision)
           VALUES ('inv-13', 1, 'banner.jpg', 'image/jpeg',
                   '[needs AI Vision: banner.jpg]', 'image-ocr', true)""")
    (store.message_dir(str(tmp_path), "inv-13") / "att1__banner.jpg").write_bytes(
        b"\xff\xd8\xff\xe0 not a real jpeg \xff\xd9")
    _push_receipts(tmp_path)
    uploads, posts = [], []
    assert _tick(pg, tmp_path, FakeClient([_doc()]), uploads, posts) == 1
    assert len(uploads) == 1
    assert _run_outcome(pg, "inv-13") == "ok"
    assert not any("banner.jpg" in p for p in posts), "a banner raised a warehouse review"


def test_an_invoice_that_arrived_only_as_a_scan_still_reads_the_scan(pg, tmp_path):
    """The banner rule never drops the only document: a mail whose attachments all need
    vision keeps them (the scan IS the invoice)."""
    _setup(pg)
    pg.execute(
        """INSERT INTO messages (message_id, category, subject, from_addr, combined_text,
                                 body_text, has_attachments, processed, created_at)
           VALUES ('inv-14', 'invoices', 'Faktúra', %s, '', '', true, false,
                   now() - interval '1 hour')""", (SUPPLIER_EMAIL,))
    pg.execute(
        """INSERT INTO attachments (message_id, idx, filename, mime, extracted_text, method,
                                    needs_vision)
           VALUES ('inv-14', 0, 'scan.jpg', 'image/jpeg', '[needs AI Vision: scan.jpg]',
                   'image-ocr', true)""")
    d = store.message_dir(str(tmp_path), "inv-14")
    d.mkdir(parents=True, exist_ok=True)
    (d / "att0__scan.jpg").write_bytes(b"\xff\xd8\xff\xe0 scan \xff\xd9")
    _push_receipts(tmp_path)

    class VisionClient(FakeClient):
        vision = 0

        def vision_call(self, *a, **kw):
            self.vision += 1
            return ["Faktúra 2400765432 Rožok 50g 100 ks 50,00"]

    client = VisionClient([_doc()])
    uploads, posts = [], []
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert client.vision >= 1, "the only document (a scan) must still be read"
    assert len(uploads) == 1


# --- fail-closed: no fresh CODEX snapshot that covers the invoice -------------------------

def test_without_a_fresh_codex_receipts_snapshot_no_invoice_is_claimed(pg, tmp_path):
    _setup(pg)
    _message(pg, tmp_path, "inv-15")
    uploads, posts = [], []
    client = FakeClient([_doc()])
    assert _tick(pg, tmp_path, client, uploads, posts) == 0
    assert uploads == [] and client.calls == []
    assert pg.execute("SELECT count(*) FROM dl_invoice_runs").fetchone()[0] == 0, \
        "the invoice waits unclaimed (no attempt spent) until a fresh snapshot arrives"


def test_a_stale_codex_receipts_snapshot_holds_invoices_too(pg, tmp_path):
    _setup(pg)
    _message(pg, tmp_path, "inv-16")
    _push_receipts(tmp_path)
    pg.execute("UPDATE codex_receipt_syncs SET source_as_of = now() - interval '31 hours', "
               "synced_at = now() - interval '31 hours'")
    uploads, posts = [], []
    assert _tick(pg, tmp_path, FakeClient([_doc()]), uploads, posts) == 0
    assert uploads == []


def test_an_invoice_newer_than_the_codex_data_waits_for_the_next_push(pg, tmp_path):
    """A receipt the warehouse types by hand this morning is in CODEX only after the next ETL
    — an invoice that arrived after the last snapshot is judged only once one covers it."""
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "inv-17", created_at=datetime.now(UTC) + timedelta(seconds=1))
    uploads, posts = [], []
    client = FakeClient([_doc()])
    assert _tick(pg, tmp_path, client, uploads, posts) == 0
    assert client.calls == [] and _run_outcome(pg, "inv-17") is None
    pg.execute("UPDATE messages SET created_at = now() - interval '1 minute' "
               "WHERE message_id = 'inv-17'")
    _push_receipts(tmp_path)                    # the next push covers it
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1


def test_the_wait_for_codex_rule_can_be_switched_off(pg, tmp_path):
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "inv-18", created_at=datetime.now(UTC) + timedelta(seconds=1))
    uploads, posts = [], []
    assert _tick(pg, tmp_path, FakeClient([_doc()]), uploads, posts,
                 delivery_notes_invoice_wait_for_codex=False) == 1
    assert len(uploads) == 1


# --- turning the flag on, the board answer, a flag-only save -------------------------------

def test_turning_the_flag_on_never_ships_the_suppliers_backlog(pg, tmp_path):
    """Only invoices received since the flag went on are taken — the days before are goods the
    warehouse already entered by hand (the 2-workday horizon)."""
    dl_snapshot.import_snapshot(pg, DL_CATALOG_CSV, OBJ_CATALOG_CSV, SUPPLIERS_CSV)
    rid = pg.execute(
        "INSERT INTO dl_supplier_overrides (ean_edi, name, emails, city) "
        "VALUES (%s, %s, %s, 'Mesto') RETURNING id",
        (SUPPLIER_EAN, SUPPLIER_NAME, [SUPPLIER_EMAIL])).fetchone()[0]
    dl_snapshot.dl_rebuild_from_overrides(pg)
    _message(pg, tmp_path, "inv-old", created_at=datetime.now(UTC) - timedelta(days=3))
    dl_snapshot.set_invoice_flag(pg, rid, True)
    _message(pg, tmp_path, "inv-new", created_at=datetime.now(UTC))
    _push_receipts(tmp_path)
    uploads, posts = [], []
    client = FakeClient([_doc()], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert _tick(pg, tmp_path, client, uploads, posts) == 0
    assert len(uploads) == 1
    assert _run_outcome(pg, "inv-new") == "ok" and _run_outcome(pg, "inv-old") is None


def test_a_board_answer_requeues_an_invoice_as_an_invoice(pg, tmp_path):
    """`release_for_question` used to re-run an invoice-as-DL mail through the PLAIN DL path:
    DL prompt, `messages.processed` (owned by the n8n invoice flow) set, and no dedup gate.
    Now it goes back to the invoice queue — the next tick runs it with every hold and the
    gate; a transient model failure there is retried, never stranded."""
    _setup(pg)
    _message(pg, tmp_path, "inv-19")
    _push_receipts(tmp_path)
    pg.execute("INSERT INTO dl_invoice_runs (message_id, outcome) VALUES ('inv-19', 'review')")
    qid = pg.execute(
        "INSERT INTO order_questions (message_id, kind, wording, status, customer_ean, "
        "item_key) VALUES ('inv-19', 'dl_item', 'Rožok 50g', 'answered', %s, 'rozok 50g') "
        "RETURNING id", (SUPPLIER_EAN,)).fetchone()[0]
    uploads, posts = [], []
    client = FakeClient([Exception("Rate limit reached for gpt"), _doc()])
    assert dl_questions.release_for_question(pg, _cfg(tmp_path), qid, client=client) == []
    assert uploads == [] and client.calls == [], "nothing runs inline any more"
    _push_receipts(tmp_path)          # round 8: a re-queued invoice waits for the next copy
    assert _tick(pg, tmp_path, client, uploads, posts) == 0          # transient → retry later
    assert _run_outcome(pg, "inv-19") is None
    pg.execute("UPDATE dl_invoice_runs SET claimed_at = now() - interval '31 minutes'")
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1
    assert any("invoiceNumber" in str(sc) for sc in client.schemas), \
        "the invoice extraction was not used"
    processed = pg.execute("SELECT processed FROM messages WHERE message_id='inv-19'"
                           ).fetchone()[0]
    assert processed is False, "the n8n invoice flow's processed flag was written"
    assert _run_outcome(pg, "inv-19") == "ok"


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
    flag = pg.execute("SELECT invoice_is_delivery_note, invoice_dl_since FROM "
                      "dl_supplier_overrides WHERE id = %s", (card["override_id"],)).fetchone()
    assert flag == (False, None)
