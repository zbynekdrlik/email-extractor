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
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_invoice_dedup_regression import (
    ITEM_GTIN,
    ITEM_MATCHED,
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
    """A DL that ended in a REAL dedup conflict (posted for a human: the priceless scan of a
    same-day delivery in other units after its invoice shipped) has no question — every
    same-sender answer would re-run and RE-POST it. Writer and reader proven together."""
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "inv-1", created_at=_ago(hours=3))
    uploads, posts = [], []
    kar = _one(YESTERDAY, "4400000001", "2400000001", qty=1, unit_price=50.0)
    kar["items"][0]["unit"] = "KAR"
    client = FakeClient([{"documents": [kar]},
                         _inv(YESTERDAY, "7700000001", "", priced=False)], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    _message(pg, tmp_path, "dl-c", category="dodacie_listy", subject="Dodací list",
             text="Dodací list", created_at=_ago(hours=1))
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert pg.execute("SELECT processed, proc_status FROM messages WHERE message_id = 'dl-c'"
                      ).fetchone() == (True, "review")
    assert dl_questions._release_stuck_siblings(pg, "another-mail", SUPPLIER_EMAIL) == 0
    processed = pg.execute("SELECT processed FROM messages WHERE message_id = 'dl-c'"
                           ).fetchone()[0]
    assert processed is True


# --- round 5: versions whose DL reference the model read differently ----------------------

def test_a_resend_whose_dl_reference_drifted_is_still_a_duplicate(pg, tmp_path):
    """A plain resend of the shipped invoice; the model read its DL reference as the invoice
    number this time (and another day). The shared invoice number from ANOTHER mail stays a
    number match — the same total and goods: a duplicate, never a second DESADV."""
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "inv-1", created_at=_ago(hours=3))
    uploads, posts = [], []
    client = FakeClient([_inv(YESTERDAY, "4400000001", "2400000001"),
                         _inv(datetime.now(UTC), "2400000001", "2400000001")], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    _message(pg, tmp_path, "inv-1-resend", created_at=_ago(hours=1))
    _push_receipts(tmp_path)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1, "a resend of the same invoice shipped a second DESADV"
    assert _run_outcome(pg, "inv-1-resend") == "duplicate"


def test_a_corrected_version_with_a_drifted_dl_reference_is_reviewed(pg, tmp_path):
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "inv-v1", created_at=_ago(hours=3))
    uploads, posts = [], []
    client = FakeClient([_inv(YESTERDAY, "4400000001", "2400000001"),
                         _inv(YESTERDAY, "2400000001", "2400000001", qty=90)], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    _message(pg, tmp_path, "inv-v2", subject="Opravená faktúra 2400000001",
             created_at=_ago(minutes=30))
    _push_receipts(tmp_path)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1, "a corrected version of the SAME invoice shipped a 2nd DESADV"
    assert _run_outcome(pg, "inv-v2") == "review"


def test_the_older_version_with_a_drifted_dl_reference_never_ships_after_the_newer(
        pg, tmp_path):
    _setup(pg)
    _message(pg, tmp_path, "inv-v1", created_at=_ago(hours=3))
    _message(pg, tmp_path, "inv-v2", subject="Opravená faktúra", created_at=_ago(hours=1))
    _push_receipts(tmp_path)
    uploads, posts = [], []
    client = FakeClient([_inv(YESTERDAY, "4400000001", "2400000001", qty=90),
                         _inv(YESTERDAY, "2400000001", "2400000001")], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1, "the superseded version shipped with its old content"
    assert _run_outcome(pg, "inv-v1") == "duplicate"


# --- round 5: CODEX links and old shipments -------------------------------------------------

def test_a_receipt_of_the_same_invoice_for_another_dl_days_apart_is_reviewed(pg, tmp_path):
    """The same sum but D1's receipt is days before D2: a collective invoice's other delivery,
    not provably D1 — a human decides, never silent."""
    _setup(pg)
    _message(pg, tmp_path, "inv-d2")
    _push_receipts(tmp_path, [_receipt("261009005", datetime.now(UTC) - timedelta(days=5),
                                       ["7700000001"], "2400000001")])
    uploads, posts = [], []
    assert _tick(pg, tmp_path, FakeClient([_inv(YESTERDAY, "7700000002", "2400000001")]),
                 uploads, posts) == 1
    assert uploads == []
    assert _run_outcome(pg, "inv-d2") == "review"
    assert len(posts) == 1 and "inému dodaciemu listu" in posts[0]


def test_codex_importing_an_old_shipment_without_facts_is_reviewed_not_silent(pg, tmp_path):
    """A shipment of ours from before #485 (no facts) that CODEX imported: the same total on
    that receipt's day proves nothing about today's other invoice — a review."""
    _setup(pg)
    _message(pg, tmp_path, "inv-old", created_at=_ago(hours=30))
    pg.execute("INSERT INTO desadv_sent (supplier_ean, doc_number, filename, message_id, "
               "sent_at, uploaded_at) VALUES (%s, '4400000001', 'x.txt', 'inv-old', "
               "now() - interval '30 hours', now() - interval '30 hours')", (SUPPLIER_EAN,))
    pg.execute("INSERT INTO dl_invoice_runs (message_id, outcome) VALUES ('inv-old', 'ok')")
    _message(pg, tmp_path, "inv-new", created_at=_ago(hours=1))
    _push_receipts(tmp_path, [_receipt("261009001", YESTERDAY, ["4400000001"])])
    uploads, posts = [], []
    client = FakeClient([_inv(YESTERDAY, "4400000002", "2400000002")])
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert uploads == []
    assert _run_outcome(pg, "inv-new") == "review"
    assert len(posts) == 1 and "bez údajov" in posts[0]


# --- round 5: an invoice deduped onto another invoice's board question ---------------------

NO_MATCH = {"gtin": "NO_MATCH", "matchConfidence": 0.0, "matchReason": "žiadna zhoda"}


def _yeast(day, dl, invoice, qty):
    doc = _one(day, dl, invoice, qty=qty)
    doc["items"][0]["name"] = "Kvasnice Rekord 1kg"
    return {"documents": [doc]}


def test_an_invoice_deduped_onto_another_invoices_question_ships_after_the_answer(
        pg, tmp_path):
    """End to end (writer + reader): two invoices of one supplier with the same unknown line —
    the second one's ask dedupes onto the first one's question and both are held; the answer
    re-queues BOTH and both ship."""
    from app.orders import teach
    _setup(pg)
    _message(pg, tmp_path, "inv-1", created_at=_ago(hours=3))
    _message(pg, tmp_path, "inv-2", created_at=_ago(hours=2))
    _push_receipts(tmp_path)
    client = FakeClient([_yeast(YESTERDAY, "4400000002", "2400000002", 40),
                         _yeast(TWO_DAYS_AGO, "4400000001", "2400000001", 100),
                         _yeast(YESTERDAY, "4400000002", "2400000002", 40),
                         _yeast(TWO_DAYS_AGO, "4400000001", "2400000001", 100)], runs=4)
    client._answers["dl_item"] = [NO_MATCH, NO_MATCH] + [ITEM_MATCHED] * 4
    uploads, posts = [], []
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    questions = pg.execute("SELECT id, message_id, kind, wording, payload, candidates "
                           "FROM order_questions").fetchall()
    assert uploads == [] and len(questions) == 1, "the second ask did not dedupe"
    q = dict(zip(("id", "message_id", "kind", "wording", "payload", "candidates"),
                 questions[0], strict=True))
    pg.execute("UPDATE order_questions SET status = 'answered', answer = %s::jsonb "
               "WHERE id = %s", (json.dumps({"choice": ITEM_GTIN}), q["id"]))
    teach.KINDS["dl_item"].apply(pg, _cfg(tmp_path), q, ITEM_GTIN, "sklad")
    for _ in range(3):
        _tick(pg, tmp_path, client, uploads, posts)
    assert len(uploads) == 2, "the invoice deduped onto the answered question stays stranded"


def test_only_the_invoices_waiting_on_the_answered_question_are_requeued(pg, tmp_path):
    """Other same-sender reviews without a question of their own (a money-gate breach, a
    stale-copy hold, a dedup conflict, one waiting on ANOTHER question) are a human's — never
    re-run nor re-posted by an answer."""
    _setup(pg)
    for mid in ("inv-a", "inv-b", "inv-c", "inv-d", "inv-e"):
        _message(pg, tmp_path, mid, created_at=_ago(hours=2))
        pg.execute("INSERT INTO dl_invoice_runs (message_id, outcome, finished_at) "
                   "VALUES (%s, 'review', now())", (mid,))
    qid = pg.execute(
        "INSERT INTO order_questions (message_id, kind, wording, status, customer_ean, "
        "item_key) VALUES ('inv-a', 'dl_item', 'Rožok 50g', 'answered', %s, 'rozok 50g') "
        "RETURNING id", (SUPPLIER_EAN,)).fetchone()[0]
    events = (("inv-b", "review", "review", {"held": True, "question_ids": [qid]}),
              ("inv-c", "review", "review", {"reason": "money gate"}),
              ("inv-d", dl_invoice.STAGE, "review", {"question_ids": [qid]}),
              ("inv-e", "review", "review", {"held": True, "question_ids": [qid + 1000]}))
    for mid, stage, status, detail in events:
        pg.execute("INSERT INTO email_events (message_id, workflow, stage, status, outcome, "
                   "detail, rollup) VALUES (%s, 'delivery_notes', %s, %s, 'x', %s::jsonb, "
                   "false)", (mid, stage, status, json.dumps(detail)))
    dl_questions.release_for_question(pg, _cfg(tmp_path), qid)
    runs = dict(pg.execute("SELECT message_id, outcome FROM dl_invoice_runs").fetchall())
    assert runs == {"inv-a": None, "inv-b": None, "inv-c": "review", "inv-d": "review",
                    "inv-e": "review"}


# --- round 6: one delivery as a scan and an invoice a day apart ------------------------------

def test_a_scan_and_its_invoice_dated_a_day_apart_never_ship_twice(pg, tmp_path):
    """LESAFFRE shape: the priceless scan (number Y, dated D) shipped; the invoice of the same
    goods prints another DL number and the date D+1; CODEX imported the scan. Not provably
    the same delivery — a review, never a second DESADV."""
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "dl-1", category="dodacie_listy", subject="Dodací list",
             text="Dodací list", created_at=_ago(hours=30))
    uploads, posts = [], []
    client = FakeClient([_inv(TWO_DAYS_AGO, "7700000001", "", priced=False),
                         _inv(YESTERDAY, "4400000001", "2400000001")], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    shipped_posts = len(posts)
    _message(pg, tmp_path, "inv-1", created_at=_ago(hours=1))
    _push_receipts(tmp_path, [_receipt("261009001", TWO_DAYS_AGO, ["7700000001"])])
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1, "the same goods went to ORION twice"
    assert _run_outcome(pg, "inv-1") == "review"
    assert len(posts) - shipped_posts == 1


def test_codex_importing_an_old_scan_with_another_total_is_reviewed(pg, tmp_path):
    """Transition: a scan shipped before #485 (no facts); CODEX's import of it carries OUR
    catalog-price total (not the invoice's): within ±1 day it is a conflict whatever the
    total — never a silent second DESADV."""
    _setup(pg)
    _message(pg, tmp_path, "dl-old", category="dodacie_listy", subject="Dodací list",
             text="Dodací list", created_at=_ago(hours=40))
    pg.execute("UPDATE messages SET processed = true WHERE message_id = 'dl-old'")
    pg.execute("INSERT INTO desadv_sent (supplier_ean, doc_number, filename, message_id, "
               "sent_at, uploaded_at) VALUES (%s, '7700000001', 'x.txt', 'dl-old', "
               "now() - interval '40 hours', now() - interval '40 hours')", (SUPPLIER_EAN,))
    _message(pg, tmp_path, "inv-1", created_at=_ago(hours=1))
    _push_receipts(tmp_path, [_receipt("261009001", YESTERDAY, ["7700000001"], total=41.0)])
    uploads, posts = [], []
    assert _tick(pg, tmp_path, FakeClient([_inv(YESTERDAY, "4400000001", "2400000001")]),
                 uploads, posts) == 1
    assert uploads == []
    assert _run_outcome(pg, "inv-1") == "review"


def test_a_resent_collective_invoice_meets_each_dls_own_twin(pg, tmp_path):
    """The collective invoice (DL1 + DL2) shipped; its resend: each DL meets its OWN row, never
    DL1's through the shared invoice number (a plain duplicate each, no false review)."""
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "inv-coll", created_at=_ago(hours=3))
    docs = {"documents": [_one(TWO_DAYS_AGO, "7700000001", "2400000001"),
                          _one(YESTERDAY, "7700000002", "2400000001", qty=40)]}
    uploads, posts = [], []
    client = FakeClient([docs, docs], runs=4)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 2
    shipped_posts = len(posts)
    _message(pg, tmp_path, "inv-coll-2", created_at=_ago(hours=1))
    _push_receipts(tmp_path)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 2
    assert posts[shipped_posts:] == []
    assert _run_outcome(pg, "inv-coll-2") == "duplicate"
