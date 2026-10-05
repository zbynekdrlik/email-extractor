"""#485 review round 4: CODEX's own links, collective invoices, and what the gate cannot see.

Round 4 found: CODEX linking THIS invoice to the receipt of our earlier DL scan was ignored (a
second DESADV); CODEX's same-day import of our first delivery turned a possibly-new second
delivery with another invoice number into a silent duplicate; the second delivery note of a
collective invoice was dropped as a duplicate of the first; a flagged supplier whose EAN no
CODEX receipt carries was judged without any CODEX guard; the late check shipped when the copy
went stale meanwhile; a DL mail deduped onto an invoice's board question was stranded; a
DL-path conflict was re-run (and re-posted) by every sibling release; the claim could be held
inside a caller's transaction across the upload; a stale orphan claim was ignored.

Synthetic data only (made-up supplier, numbers, addresses) — this repo is public.
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_invoice_dedup_regression import (
    OTHER_EAN,
    SUPPLIER_EAN,
    SUPPLIER_EMAIL,
    SUPPLIER_NAME,
    FakeClient,
    _cfg,
    _message,
    _push_receipts,
    _run_outcome,
    _setup,
    _supplier,
    _tick,
)

from app.httpapi import create_app
from app.orders import codex_receipts, dl_invoice, dl_questions

YESTERDAY = datetime.now(UTC) - timedelta(days=1)
TWO_DAYS_AGO = datetime.now(UTC) - timedelta(days=2)


def _ago(**kw):
    return datetime.now(UTC) - timedelta(**kw)


def _one(day, doc_number, invoice_number, qty=100, unit_price=0.5, priced=True):
    item = {"name": "Rožok 50g", "quantity": qty, "unit": "ks", "vatRate": 10}
    if priced:
        item.update(unitPrice=unit_price, totalPrice=round(qty * unit_price, 2))
    return {"supplierName": SUPPLIER_NAME, "supplierCity": "Mesto",
            "supplierEmail": SUPPLIER_EMAIL, "docNumber": doc_number,
            "invoiceNumber": invoice_number, "deliveryDate": day.strftime("%d.%m.%Y"),
            "documentTotalWithoutVAT": round(qty * unit_price, 2) if priced else 0,
            "items": [item]}


def _inv(*a, **kw):
    return {"documents": [_one(*a, **kw)]}


def _receipt(number, day, dl_numbers, invoice_number="", total=50.0, ean=SUPPLIER_EAN, **kw):
    return {"receipt_number": number, "supplier_ico": "12345678", "supplier_eans": [ean],
            "receipt_date": day.date().isoformat(), "dl_numbers": dl_numbers,
            "invoice_number": invoice_number, "total": total, **kw}


# --- CODEX's own evidence -------------------------------------------------------------------

def test_codex_linking_this_invoice_to_the_receipt_of_our_dl_scan_blocks_it(pg, tmp_path):
    """Our priceless DL scan shipped; CODEX imported it and accounting linked invoice I to that
    receipt; invoice I arrives (another printed DL number, a day later). CODEX says it is
    received — the link counts even on CODEX's import of our own shipment."""
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "dl-1", category="dodacie_listy", subject="Dodací list",
             text="Dodací list", created_at=_ago(hours=5))
    uploads, posts = [], []
    client = FakeClient([_inv(TWO_DAYS_AGO, "7700000001", "", priced=False),
                         _inv(YESTERDAY, "4400000001", "2400000001")], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    _message(pg, tmp_path, "inv-1", created_at=_ago(hours=1))
    _push_receipts(tmp_path, [_receipt("261009001", TWO_DAYS_AGO, ["7700000001"],
                                       "2400000001")])
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1, "CODEX linked THIS invoice to a receipt, yet a 2nd DESADV went"
    assert _run_outcome(pg, "inv-1") == "duplicate"


def test_a_lesaffre_receipt_linked_to_the_invoice_under_another_dl_number_is_a_duplicate(
        pg, tmp_path):
    """Pin: LESAFFRE prints another DL number on the invoice than CODEX has — the receipt linked
    to THIS invoice with the same total is the same delivery."""
    _setup(pg)
    _message(pg, tmp_path, "inv-l")
    _push_receipts(tmp_path, [_receipt("261009004", YESTERDAY, ["5550001234"], "2400000001")])
    uploads, posts = [], []
    assert _tick(pg, tmp_path, FakeClient([_inv(YESTERDAY, "4400000001", "2400000001")]),
                 uploads, posts) == 1
    assert uploads == [] and posts == []
    assert _run_outcome(pg, "inv-l") == "duplicate"


def test_a_receipt_of_the_same_invoice_for_another_dl_and_sum_is_reviewed(pg, tmp_path):
    """A collective invoice: CODEX has D1's receipt linked to the invoice; the invoice's D2
    (another DL number, another sum) is no proof either way — a human decides."""
    _setup(pg)
    _message(pg, tmp_path, "inv-d2")
    _push_receipts(tmp_path, [_receipt("261009005", TWO_DAYS_AGO, ["7700000001"],
                                       "2400000001", total=50.0, invoice_total=70.0)])
    uploads, posts = [], []
    client = FakeClient([_inv(YESTERDAY, "7700000002", "2400000001", qty=40)])
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert uploads == []
    assert _run_outcome(pg, "inv-d2") == "review"
    assert len(posts) == 1 and "inému dodaciemu listu" in posts[0]


def test_a_second_same_day_delivery_with_another_invoice_after_codex_imported_the_first(
        pg, tmp_path):
    """CODEX's same-day import of our first delivery carries no more evidence than our own row
    — another invoice number stays a conflict, never a silent duplicate."""
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "inv-a", created_at=_ago(hours=3))
    uploads, posts = [], []
    client = FakeClient([_inv(YESTERDAY, "4400000001", "2400000001"),
                         _inv(YESTERDAY, "4400000002", "2400000002")], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    shipped_posts = len(posts)
    _message(pg, tmp_path, "inv-b", created_at=_ago(minutes=30))
    _push_receipts(tmp_path, [_receipt("261009001", YESTERDAY, ["4400000001"])])
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1
    assert _run_outcome(pg, "inv-b") == "review"
    assert len(posts) - shipped_posts == 1


# --- collective invoice -----------------------------------------------------------------------

def test_the_second_dl_of_a_collective_invoice_ships(pg, tmp_path):
    """ONE invoice mail listing TWO delivery notes (D1 day 1, D2 day 2, other goods), two
    documents sharing the invoice number: D2 is another delivery, not D1's duplicate."""
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "inv-coll", created_at=_ago(hours=1))
    uploads, posts = [], []
    client = FakeClient([{"documents": [_one(TWO_DAYS_AGO, "7700000001", "2400000001"),
                                        _one(YESTERDAY, "7700000002", "2400000001", qty=40)]}],
                        runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 2, "the second DL of a collective invoice was dropped"


def test_a_dl_scan_numbered_like_an_earlier_invoice_of_other_goods_ships(pg, tmp_path):
    """DL path: a scan whose number's digits equal an earlier invoice's number (another
    series), another day, other goods — another delivery, never a silent twin."""
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "inv-1", created_at=_ago(hours=3))
    uploads, posts = [], []
    client = FakeClient([_inv(TWO_DAYS_AGO, "4400000001", "2400000001"),
                         _inv(YESTERDAY, "DL2400000001", "", qty=30, priced=False)], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    _message(pg, tmp_path, "dl-2", category="dodacie_listy", subject="Dodací list",
             text="Dodací list", created_at=_ago(hours=1))
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 2


# --- what the gate cannot see ------------------------------------------------------------------

def test_a_flagged_supplier_no_codex_receipt_carries_waits(pg, tmp_path):
    """Its EAN differs from every CODEX `AEDIEAN`: the gate cannot see its receipts — its
    invoices wait (fail-closed), ops is told."""
    _setup(pg)
    _message(pg, tmp_path, "inv-u")
    app = create_app(_cfg(tmp_path))
    r = app.test_client().post(
        "/api/codex/receipts", headers={"X-Token": "tok"}, query_string={"force": "1"},
        json={"source_as_of": datetime.now(UTC).isoformat(), "days": 60, "receipts": [
            _receipt("261000001", YESTERDAY, ["123456789"], ean="2000000000001")]})
    assert r.status_code == 200
    uploads, posts = [], []
    client = FakeClient([_inv(YESTERDAY, "4400000001", "2400000001")])
    assert _tick(pg, tmp_path, client, uploads, posts) == 0
    assert uploads == [] and client.calls == []
    assert codex_receipts.missing_supplier_sweep(pg, _cfg(tmp_path)) == 1


def test_a_document_resolving_to_a_supplier_no_receipt_carries_is_reviewed(pg, tmp_path):
    _setup(pg)
    _message(pg, tmp_path, "inv-o")
    _push_receipts(tmp_path)
    uploads, posts = [], []
    client = FakeClient([_inv(YESTERDAY, "4400000001", "2400000001")],
                        supplier=_supplier(OTHER_EAN))
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert uploads == []
    assert _run_outcome(pg, "inv-o") == "review"
    assert len(posts) == 1 and "nedá overiť" in posts[0]


def _built(doc_number):
    lin = ("LIN" + "1".rjust(6) + "8588000000001".ljust(13)).ljust(96) + "100.000".rjust(12)
    return SimpleNamespace(doc_number=doc_number, filename=f"DESADV_{doc_number}.txt",
                           content=lin + "\r\n")


def test_the_late_check_never_ships_when_the_receipts_went_stale_meanwhile(pg, tmp_path):
    _setup(pg)
    _push_receipts(tmp_path)
    pg.execute("UPDATE codex_receipt_syncs SET source_as_of = now() - interval '31 hours', "
               "synced_at = now() - interval '31 hours'")
    twin = dl_invoice.twin_shipped(
        pg, {"message_id": "inv-s", "created_at": _ago(hours=1)},
        _one(YESTERDAY, "4400000001", "2400000001"), SUPPLIER_EAN, _built("4400000001"),
        invoice_only=False)
    assert twin is not None and twin.conflict and "nedá overiť" in twin.reason()


def test_the_claim_refuses_to_run_inside_a_callers_transaction(pg, tmp_path):
    """The claim must commit before the upload — inside a caller's transaction it would stay
    open (with the ship lock) across the upload."""
    _setup(pg)
    with pg.transaction(), pytest.raises(RuntimeError):
        dl_invoice.claim_unless_twin(
            pg, {"message_id": "inv-t"}, _one(YESTERDAY, "4400000001", ""), SUPPLIER_EAN,
            _built("4400000001"), invoice_only=True, facts={})


# --- the board -----------------------------------------------------------------------------

def test_a_dl_sibling_deduped_onto_an_invoices_question_is_released(pg, tmp_path):
    _setup(pg)
    _message(pg, tmp_path, "inv-1", created_at=_ago(hours=3))
    pg.execute("INSERT INTO dl_invoice_runs (message_id, outcome) VALUES ('inv-1', 'review')")
    pg.execute("INSERT INTO messages (message_id, category, subject, from_addr, combined_text, "
               "processed, proc_status, created_at) VALUES ('dl-1', 'dodacie_listy', "
               "'Dodací list', %s, 'x', true, 'review', now() - interval '2 hours')",
               (SUPPLIER_EMAIL,))
    qid = pg.execute(
        "INSERT INTO order_questions (message_id, kind, wording, status, customer_ean, "
        "item_key) VALUES ('inv-1', 'dl_item', 'Rožok 50g', 'answered', %s, 'rozok 50g') "
        "RETURNING id", (SUPPLIER_EAN,)).fetchone()[0]
    dl_questions.release_for_question(pg, _cfg(tmp_path), qid)
    processed = pg.execute("SELECT processed FROM messages WHERE message_id = 'dl-1'"
                           ).fetchone()[0]
    assert processed is False, "the deduped DL sibling stays held forever"


def test_a_dl_conflict_review_is_never_rerun_by_a_sibling_release(pg, tmp_path):
    """A DL that ended in a dedup conflict (posted for a human) has no question — every
    same-sender answer would re-run and RE-POST it."""
    _setup(pg)
    pg.execute("INSERT INTO messages (message_id, category, subject, from_addr, combined_text, "
               "processed, proc_status, created_at) VALUES ('dl-c', 'dodacie_listy', "
               "'Dodací list', %s, 'x', true, 'review', now() - interval '2 hours')",
               (SUPPLIER_EMAIL,))
    pg.execute("INSERT INTO email_events (message_id, workflow, stage, status, outcome, "
               "rollup) VALUES ('dl-c', 'delivery_notes', 'invoice_dedup', 'review', 'x', "
               "false)")
    dl_questions._release_stuck_siblings(pg, "another-mail", SUPPLIER_EMAIL)
    processed = pg.execute("SELECT processed FROM messages WHERE message_id = 'dl-c'"
                           ).fetchone()[0]
    assert processed is True
