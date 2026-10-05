"""#485 review round 3: what the dedup cannot prove is never shipped AND never silent.

Round 2 narrowed the date match so a standing order's next delivery ships; round 3 found the
other side of that edge: an invoice and a DL scan of ONE delivery printed in different units
(1 KAR vs 100 ks, no price on the scan) shipped twice; CODEX's same-day import of our DL scan
was explained away; a corrected invoice with the same total but other items, the previous
day's hand-typed receipt and a „Re: dobropis" mail were judged silently; an invoice held on a
board question when the migration stamped its supplier never shipped after the answer; two
documents of one delivery processed at the same moment could both pass. These tests pin the
round-3 rules: provably the same → a silent duplicate; maybe the same → a review, never a
second DESADV; and the twin check + claim + facts serialised per supplier.

Synthetic data only (made-up supplier, numbers, addresses) — this repo is public.
"""
import os
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import psycopg
from test_invoice_dedup_regression import (
    SUPPLIER_EAN,
    SUPPLIER_EMAIL,
    SUPPLIER_NAME,
    FakeClient,
    _cfg,
    _doc,
    _message,
    _push_receipts,
    _run_outcome,
    _setup,
    _tick,
)

from app.orders import dl_invoice, dl_questions

PG_DSN = os.environ.get("PG_TEST_DSN")
YESTERDAY = datetime.now(UTC) - timedelta(days=1)
TWO_DAYS_AGO = datetime.now(UTC) - timedelta(days=2)
CODE = "8588000000001"


def _ago(**kw):
    return datetime.now(UTC) - timedelta(**kw)


def _priceless_dl(doc_number, qty=100):
    return {"documents": [{
        "supplierName": SUPPLIER_NAME, "supplierCity": "Mesto",
        "supplierEmail": SUPPLIER_EMAIL, "docNumber": doc_number,
        "deliveryDate": YESTERDAY.strftime("%d.%m.%Y"), "documentTotalWithoutVAT": 0,
        "items": [{"name": "Rožok 50g", "quantity": qty, "unit": "ks", "vatRate": 10}]}]}


def _invoice_in_cartons(doc_number, invoice_number):
    """The SAME delivery, invoiced as 1 KAR of 100 ks at 50 € — total 50 €."""
    return {"documents": [{
        "supplierName": SUPPLIER_NAME, "supplierCity": "Mesto",
        "supplierEmail": SUPPLIER_EMAIL, "docNumber": doc_number,
        "invoiceNumber": invoice_number, "deliveryDate": YESTERDAY.strftime("%d.%m.%Y"),
        "documentTotalWithoutVAT": 50.0,
        "items": [{"name": "Rožok 50g", "quantity": 1, "unit": "KAR", "vatRate": 10,
                   "unitPrice": 50.0, "totalPrice": 50.0}]}]}


def _priced(doc_number, invoice_number, qty, unit_price):
    return {"documents": [{
        "supplierName": SUPPLIER_NAME, "supplierCity": "Mesto",
        "supplierEmail": SUPPLIER_EMAIL, "docNumber": doc_number,
        "invoiceNumber": invoice_number, "deliveryDate": YESTERDAY.strftime("%d.%m.%Y"),
        "documentTotalWithoutVAT": round(qty * unit_price, 2),
        "items": [{"name": "Rožok 50g", "quantity": qty, "unit": "ks", "vatRate": 10,
                   "unitPrice": unit_price, "totalPrice": round(qty * unit_price, 2)}]}]}


def _dl_message(pg, tmp_path, mid, hours):
    _message(pg, tmp_path, mid, category="dodacie_listy", subject="Dodací list",
             text="Dodací list", created_at=_ago(hours=hours))


# --- one delivery, two documents in different units ---------------------------------------

def test_codex_importing_our_same_day_dl_scan_still_catches_its_invoice(pg, tmp_path):
    """The DL scan (priceless, in ks) ships; CODEX imports it (receipt 50 €, its DL number =
    ours); the invoice of the SAME delivery (other numbers, 1 KAR, 50 €) arrives: CODEX's
    same-day receipt with the same total proves it — a plain duplicate."""
    _setup(pg)
    _push_receipts(tmp_path)
    _dl_message(pg, tmp_path, "dl-1", 3)
    uploads, posts = [], []
    client = FakeClient([_priceless_dl("7700000001"),
                         _invoice_in_cartons("4400000001", "2400000001")], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    _message(pg, tmp_path, "inv-1", created_at=_ago(hours=1))
    _push_receipts(tmp_path, [{
        "receipt_number": "261009001", "supplier_ico": "12345678",
        "supplier_eans": [SUPPLIER_EAN], "receipt_date": YESTERDAY.date().isoformat(),
        "dl_numbers": ["7700000001"], "total": 50.0}])
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1, "a SECOND DESADV of the same delivery reached ORION"
    assert _run_outcome(pg, "inv-1") == "duplicate"


def test_an_invoice_after_a_same_day_dl_scan_in_other_units_is_reviewed(pg, tmp_path):
    """CODEX has not imported the scan yet (the warehouse imports in the morning): the scan has
    no price, the invoice other units — not provable either way: a review, never a 2nd DESADV."""
    _setup(pg)
    _push_receipts(tmp_path)
    _dl_message(pg, tmp_path, "dl-1", 3)
    uploads, posts = [], []
    client = FakeClient([_priceless_dl("7700000001"),
                         _invoice_in_cartons("4400000001", "2400000001")], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    shipped_posts = len(posts)
    _message(pg, tmp_path, "inv-1", created_at=_ago(hours=1))
    _push_receipts(tmp_path)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1, "a SECOND DESADV of the same delivery reached ORION"
    assert _run_outcome(pg, "inv-1") == "review"
    new = posts[shipped_posts:]
    assert len(new) == 1 and "nie je však isté" in new[0] and "7700000001" in new[0]


def test_a_same_day_dl_scan_in_other_units_after_the_invoice_is_reviewed(pg, tmp_path):
    """The reverse order on the DL path: the invoice (1 KAR, its own numbers) shipped, the
    priceless scan of the same delivery (100 ks, the DL's own number) arrives."""
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "inv-1", created_at=_ago(hours=3))
    uploads, posts = [], []
    client = FakeClient([_invoice_in_cartons("4400000001", "2400000001"),
                         _priceless_dl("7700000001")], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    shipped_posts = len(posts)
    _dl_message(pg, tmp_path, "dl-1", 1)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1, "a SECOND DESADV of the same delivery reached ORION (DL path)"
    new = posts[shipped_posts:]
    assert len(new) == 1 and "nie je však isté" in new[0]


def test_a_dl_scan_of_what_the_invoice_shipped_by_number_is_a_plain_duplicate(pg, tmp_path):
    """The invoice printed the DL number and shipped; the scan of that very DL (other units,
    no price) is the same document by number — a duplicate, no „corrected version" review."""
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "inv-1", created_at=_ago(hours=3))
    uploads, posts = [], []
    client = FakeClient([_invoice_in_cartons("7700000001", "2400000001"),
                         _priceless_dl("7700000001")], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    shipped_posts = len(posts)
    _dl_message(pg, tmp_path, "dl-1", 1)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1
    assert posts[shipped_posts:] == []
    assert pg.execute("SELECT count(*) FROM email_events WHERE message_id = 'dl-1' AND "
                      "stage = 'duplicate_skip'").fetchone()[0] == 1


# --- versions and neighbours --------------------------------------------------------------

def test_a_corrected_invoice_with_the_same_total_but_other_items_is_reviewed(pg, tmp_path):
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "inv-v1", created_at=_ago(hours=3))
    uploads, posts = [], []
    client = FakeClient([_priced("4400000001", "2400000001", 100, 0.5),
                         _priced("4400000001", "2400000001", 50, 1.0)], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    shipped_posts = len(posts)
    _message(pg, tmp_path, "inv-v2", subject="Opravená faktúra", created_at=_ago(minutes=30))
    _push_receipts(tmp_path)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1
    assert _run_outcome(pg, "inv-v2") == "review", "corrected items were skipped silently"
    new = posts[shipped_posts:]
    assert len(new) == 1 and "iné položky" in new[0]


def test_the_previous_days_hand_receipt_with_the_same_total_is_reviewed_not_silent(
        pg, tmp_path):
    """Monday's delivery typed by hand (not linked to an invoice yet), Tuesday's invoice of
    the standing order with the same total: a neighbouring day proves nothing — never a second
    DESADV on a guess, never a silent skip either."""
    _setup(pg)
    _message(pg, tmp_path, "inv-tue", created_at=_ago(hours=1))
    _push_receipts(tmp_path, [{
        "receipt_number": "261009003", "supplier_ico": "12345678",
        "supplier_eans": [SUPPLIER_EAN], "receipt_date": TWO_DAYS_AGO.date().isoformat(),
        "dl_numbers": ["4400000001"], "invoice_number": "", "total": 50.0}])
    uploads, posts = [], []
    client = FakeClient([_priced("4400000002", "2400000002", 100, 0.5)])
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert uploads == []
    assert _run_outcome(pg, "inv-tue") == "review"
    assert len(posts) == 1 and "261009003" in posts[0] and "príjemka je z" in posts[0]
    assert "dl_item" not in client.calls, "decided before item matching (no content needed)"


def test_a_mail_whose_only_hint_of_a_credit_note_is_its_words_is_posted(pg, tmp_path):
    """„Re: Dobropis …" on a single-PDF mail whose PDF says nothing about a credit note: not
    extracted (the words decide), but a human is told — a real invoice must never vanish."""
    _setup(pg)
    _message(pg, tmp_path, "inv-re", subject="Re: Dobropis k faktúre 2400000001",
             attachments=(("faktura.pdf", "Faktúra 2400000002 Rožok 50g 100 ks"),))
    _push_receipts(tmp_path)
    uploads, posts = [], []
    client = FakeClient([])
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert uploads == [] and client.calls == []
    assert _run_outcome(pg, "inv-re") == "credit_note"
    assert len(posts) == 1 and "dobropise" in posts[0] and "ručne" in posts[0]


# --- the clock and the board --------------------------------------------------------------

def test_an_invoice_held_on_the_board_when_its_supplier_was_stamped_ships_after_the_answer(
        pg, tmp_path):
    """Held on a board question when revision 19 stamps every flagged card (or the flag is
    toggled): the answer re-queues it — a mail that already has a run is no backlog."""
    _setup(pg)
    _message(pg, tmp_path, "inv-held", created_at=_ago(hours=5))
    pg.execute("INSERT INTO dl_invoice_runs (message_id, outcome) VALUES ('inv-held', "
               "'review')")
    qid = pg.execute(
        "INSERT INTO order_questions (message_id, kind, wording, status, customer_ean, "
        "item_key) VALUES ('inv-held', 'dl_item', 'Rožok 50g', 'answered', %s, 'rozok 50g') "
        "RETURNING id", (SUPPLIER_EAN,)).fetchone()[0]
    pg.execute("UPDATE dl_supplier_overrides SET invoice_dl_since = now()")
    _push_receipts(tmp_path)
    dl_questions.release_for_question(pg, _cfg(tmp_path), qid)
    uploads, posts = [], []
    assert _tick(pg, tmp_path, FakeClient([_doc()]), uploads, posts) == 1
    assert len(uploads) == 1, "the answered, held invoice never ships (stranded)"


def test_a_requeued_invoice_the_claim_cannot_take_raises_a_durable_alert(pg, tmp_path):
    """Its supplier no longer takes invoices (flag switched off since): the warehouse answered
    and nothing will happen — an alert says so (once), never a silent wait."""
    _setup(pg)
    _message(pg, tmp_path, "inv-off", created_at=_ago(hours=5))
    pg.execute("INSERT INTO dl_invoice_runs (message_id, outcome) VALUES ('inv-off', "
               "'review')")
    qid = pg.execute(
        "INSERT INTO order_questions (message_id, kind, wording, status, customer_ean, "
        "item_key) VALUES ('inv-off', 'dl_item', 'Rožok 50g', 'answered', %s, 'rozok 50g') "
        "RETURNING id", (SUPPLIER_EAN,)).fetchone()[0]
    pg.execute("UPDATE dl_supplier_overrides SET invoice_is_delivery_note = false")
    dl_questions.release_for_question(pg, _cfg(tmp_path), qid)
    dl_questions.release_for_question(pg, _cfg(tmp_path), qid)
    rows = pg.execute("SELECT body_html FROM pending_alerts WHERE kind = "
                      "'dl_invoice_stranded'").fetchall()
    assert len(rows) == 1 and "NEspracuje" in rows[0][0]


# --- two documents of one delivery at the same moment ---------------------------------------

def _built(doc_number):
    lin = "LIN" + "1".rjust(6) + CODE.ljust(13)
    lin = lin.ljust(96) + "100.000".rjust(12)
    return SimpleNamespace(doc_number=doc_number, filename=f"DESADV_{doc_number}.txt",
                           content=lin + "\r\n")


def test_two_documents_of_one_delivery_at_the_same_moment_claim_once(pg, tmp_path,
                                                                     monkeypatch):
    """The worker's invoice and a board-answer reprocess of its DL scan, side by side: the
    twin check, the claim and the facts run under the supplier's ship lock — the second sees
    the first one's row and does not claim. (A widened race window: each twin check sleeps.)"""
    import time

    from _race import run_racers
    _setup(pg)
    _push_receipts(tmp_path)        # the invoice path's late check needs a fresh, covering copy
    real = dl_invoice.twin_shipped

    def slow_twin(*a, **kw):
        out = real(*a, **kw)
        time.sleep(0.4)
        return out

    monkeypatch.setattr(dl_invoice, "twin_shipped", slow_twin)
    day = YESTERDAY.strftime("%d.%m.%Y")
    results, errors = {}, []

    def ship(mid, number):
        conn = psycopg.connect(PG_DSN, autocommit=True)
        try:
            doc = {"docNumber": number, "invoiceNumber": "", "deliveryDate": day,
                   "documentTotalWithoutVAT": 50.0, "items": []}
            results[mid] = dl_invoice.claim_unless_twin(
                conn, {"message_id": mid, "created_at": _ago(hours=1)}, doc, SUPPLIER_EAN,
                _built(number), invoice_only=False,
                facts={"delivery_date": YESTERDAY.date(), "total_amount": 50.0,
                       "items": [[CODE, 100.0]]})
        except Exception as e:  # pragma: no cover - surfaced below
            errors.append(e)
        finally:
            conn.close()

    run_racers(pg, [threading.Thread(target=ship, args=("m-a", "4400000001"), name="a"),
                    threading.Thread(target=ship, args=("m-b", "7700000001"), name="b")],
               timeout=15, label="claim_unless_twin")
    assert errors == []
    claimed = [mid for mid, (twin, ok, _h) in results.items() if ok]
    twins = [mid for mid, (twin, ok, _h) in results.items() if twin is not None]
    assert len(claimed) == 1 and len(twins) == 1, results
    assert pg.execute("SELECT count(*) FROM desadv_sent").fetchone()[0] == 1
