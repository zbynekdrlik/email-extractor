"""#485 review round 7: an answered board question re-queues EXACTLY the invoices it held.

Round 6 targeted the invoice re-queue by question id but still ran it only from the question
owner's path (after the owner had nothing left open) and only for the owner's sender — while
a question dedupes on its wording / card, not on the envelope. These tests drive the real
writers (the hold / ask review events record the `question_ids` they wait on) and the real
reader (`dl_questions._requeue_stuck_invoice_siblings`, run on every `dl_*` answer): an
invoice ships once every question its hold waits on is answered, in any order, whoever owns
the question — and no other review is ever re-run. Plus the round-7 date rule inside ONE
invoice mail (its invoice PDF and its DL PDF a day apart).

Synthetic data only (made-up supplier, numbers, addresses) — this repo is public.
"""
import functools
import json
from datetime import UTC, datetime, timedelta

from test_invoice_dedup_edges import NO_MATCH, _ago, _one
from test_invoice_dedup_regression import (
    ITEM_GTIN,
    ITEM_MATCHED,
    OBJ_CATALOG_CSV,
    SUPPLIER_EAN,
    SUPPLIER_EMAIL,
    SUPPLIER_NAME,
    SUPPLIERS_CSV,
    FakeClient,
    _cfg,
    _message,
    _push_receipts,
    _run_outcome,
    _setup,
    _tick,
)

from app.orders import dl_questions, dl_snapshot, dl_worker, question_alerts, teach

YESTERDAY = datetime.now(UTC) - timedelta(days=1)
TWO_DAYS_AGO = datetime.now(UTC) - timedelta(days=2)
SECOND_EMAIL = "objednavky@dodavatel.test"


def _lines(day, dl, invoice, names):
    doc = _one(day, dl, invoice, qty=10)
    base = doc["items"][0]
    doc["items"] = [dict(base, name=n) for n in names]
    doc["documentTotalWithoutVAT"] = round(sum(i["totalPrice"] for i in doc["items"]), 2)
    return {"documents": [doc]}


def _wire_release(monkeypatch, client, uploads, posts):
    """An answer's reprocess runs with the test's client / upload / post (as the worker's)."""
    real = dl_worker.release_for_question
    monkeypatch.setattr(dl_worker, "release_for_question", functools.partial(
        real, client=client,
        upload=lambda cfg, name, content, dir_override=None: uploads.append((name, content)),
        post=lambda cfg, html: posts.append(html),
        list_dirs=lambda cfg: {"in": [], "archCodex": [], "unconfirmed": [], "in_DL": []}))


def _answer(pg, tmp_path, wording, choice=ITEM_GTIN, kind="dl_item"):
    row = pg.execute("SELECT id, message_id, kind, wording, payload, candidates "
                     "FROM order_questions WHERE status = 'open' AND wording = %s",
                     (wording,)).fetchone()
    assert row, wording
    q = dict(zip(("id", "message_id", "kind", "wording", "payload", "candidates"), row,
                 strict=True))
    pg.execute("UPDATE order_questions SET status = 'answered', answer = %s::jsonb "
               "WHERE id = %s", (json.dumps({"choice": choice}), q["id"]))
    teach.KINDS[kind].apply(pg, _cfg(tmp_path), q, choice, "sklad")


def _ship_all(pg, tmp_path, client, uploads, posts, ticks=4):
    """The next CODEX push (a re-queued invoice waits for data newer than its re-queue, round
    8), then the worker's ticks."""
    _push_receipts(tmp_path)
    for _ in range(ticks):
        _tick(pg, tmp_path, client, uploads, posts)


def test_an_invoice_ships_whatever_order_the_questions_it_waits_on_are_answered(
        pg, tmp_path, monkeypatch):
    """The newer invoice (claimed first) holds on X and Y beside a known line; the older one
    holds only on X (its ask deduped onto the newer's X). Answered X, then Y: when X is
    answered the newer still waits on Y — the older must still ship, and it does once ITS
    questions are all answered (here: at once)."""
    _setup(pg)
    _message(pg, tmp_path, "inv-old", created_at=_ago(hours=3))
    _message(pg, tmp_path, "inv-new", created_at=_ago(hours=2))
    _push_receipts(tmp_path)
    new = _lines(YESTERDAY, "4400000002", "2400000002", ["Rožok 50g", "Kvasnice X",
                                                         "Kvasnice Y"])
    old = _lines(TWO_DAYS_AGO, "4400000001", "2400000001", ["Rožok 50g", "Kvasnice X"])
    client = FakeClient([new, old, new, old, new, old], runs=6)
    client._answers["dl_item"] = ([ITEM_MATCHED, NO_MATCH, NO_MATCH, ITEM_MATCHED, NO_MATCH]
                                  + [ITEM_MATCHED] * 20)
    uploads, posts = [], []
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert uploads == []
    held = pg.execute("SELECT message_id, detail->'question_ids' FROM email_events WHERE "
                      "stage = 'review' AND detail ? 'held' ORDER BY id").fetchall()
    assert [m for m, _ in held] == ["inv-new", "inv-old"], "the #365 hold records its asks"
    assert len(held[0][1]) == 2 and len(held[1][1]) == 1
    _wire_release(monkeypatch, client, uploads, posts)
    _answer(pg, tmp_path, "Kvasnice X")
    assert _run_outcome(pg, "inv-old") is None, "the older invoice waits only on X"
    assert _run_outcome(pg, "inv-new") == "review", "the newer still waits on Y"
    _answer(pg, tmp_path, "Kvasnice Y")
    _ship_all(pg, tmp_path, client, uploads, posts)
    assert len(uploads) == 2


def test_an_invoice_from_another_address_of_the_supplier_is_requeued_too(
        pg, tmp_path, monkeypatch):
    """LESAFFRE invoices from two addresses on one card: the question dedupes on its wording,
    not on the envelope — the answer re-queues the other address's invoice as well."""
    dl_snapshot.import_snapshot(pg, "GTIN,Názov,doplnok,hmotnost,Sklad,Cena\n"
                                f"{ITEM_GTIN},Rožok 50g,,0.05,1,0.50\n", OBJ_CATALOG_CSV,
                                SUPPLIERS_CSV)
    pg.execute("INSERT INTO dl_supplier_overrides (ean_edi, name, emails, city, "
               "invoice_is_delivery_note) VALUES (%s, %s, %s, 'Mesto', true)",
               (SUPPLIER_EAN, SUPPLIER_NAME, [SUPPLIER_EMAIL, SECOND_EMAIL]))
    dl_snapshot.dl_rebuild_from_overrides(pg)
    _message(pg, tmp_path, "inv-1", created_at=_ago(hours=3), from_addr=SECOND_EMAIL)
    _message(pg, tmp_path, "inv-2", created_at=_ago(hours=2))
    _push_receipts(tmp_path)
    one = _lines(TWO_DAYS_AGO, "4400000001", "2400000001", ["Rožok 50g", "Kvasnice X"])
    two = _lines(YESTERDAY, "4400000002", "2400000002", ["Rožok 50g", "Kvasnice X"])
    client = FakeClient([two, one, two, one], runs=4)
    client._answers["dl_item"] = [ITEM_MATCHED, NO_MATCH, ITEM_MATCHED, NO_MATCH] + \
        [ITEM_MATCHED] * 10
    uploads, posts = [], []
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert pg.execute("SELECT count(*) FROM order_questions").fetchone()[0] == 1
    _wire_release(monkeypatch, client, uploads, posts)
    _answer(pg, tmp_path, "Kvasnice X")
    _ship_all(pg, tmp_path, client, uploads, posts)
    assert len(uploads) == 2, "the other address's invoice stays stranded"


def test_an_invoice_deduped_onto_a_dl_mails_question_is_requeued(pg, tmp_path, monkeypatch):
    """The DL scan (from the dispatch address) owns the question; the invoice's ask deduped
    onto it — the answer re-queues the invoice too."""
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "dl-1", category="dodacie_listy", subject="Dodací list",
             text="Dodací list", created_at=_ago(hours=3), from_addr="expedicia@sklad.test")
    _message(pg, tmp_path, "inv-1", created_at=_ago(hours=2))
    scan = _lines(TWO_DAYS_AGO, "7700000001", "", ["Rožok 50g", "Kvasnice X"])
    scan["documents"][0]["documentTotalWithoutVAT"] = 0
    invoice = _lines(YESTERDAY, "4400000002", "2400000002", ["Rožok 50g", "Kvasnice X"])
    for item in invoice["documents"][0]["items"]:      # other goods than the scan's delivery
        item.update(quantity=20, totalPrice=10.0)
    invoice["documents"][0]["documentTotalWithoutVAT"] = 20.0
    client = FakeClient([scan, invoice, scan, invoice], runs=4)
    client._answers["dl_item"] = [ITEM_MATCHED, NO_MATCH, ITEM_MATCHED, NO_MATCH] + \
        [ITEM_MATCHED] * 10
    uploads, posts = [], []
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    owner = pg.execute("SELECT message_id FROM order_questions").fetchall()
    assert owner == [("dl-1",)]
    _wire_release(monkeypatch, client, uploads, posts)
    _answer(pg, tmp_path, "Kvasnice X")
    _ship_all(pg, tmp_path, client, uploads, posts)
    assert len(uploads) == 2, "the invoice deduped onto the DL's question stays stranded"
    assert _run_outcome(pg, "inv-1") in ("ok", "partial")


def test_a_mass_hold_records_its_question_and_is_requeued(pg, tmp_path):
    """The #462 mass hold (a kg-tracked card with no per-piece mass, delivered in pieces)
    records its question too — answering it puts the invoice back on the queue."""
    dl_snapshot.import_snapshot(pg, "GTIN,Názov,doplnok,hmotnost,Sklad,Cena\n"
                                "8588000000002,Droždie,Rekord 10 kg,,100,1.00\n",
                                OBJ_CATALOG_CSV, SUPPLIERS_CSV)
    pg.execute("INSERT INTO dl_supplier_overrides (ean_edi, name, emails, city, "
               "invoice_is_delivery_note) VALUES (%s, %s, %s, 'Mesto', true)",
               (SUPPLIER_EAN, SUPPLIER_NAME, [SUPPLIER_EMAIL]))
    dl_snapshot.dl_rebuild_from_overrides(pg)
    _message(pg, tmp_path, "inv-m", created_at=_ago(hours=2))
    _push_receipts(tmp_path)
    doc = _one(YESTERDAY, "4400000001", "2400000001", qty=7, unit_price=10.0)
    doc["items"][0].update(name="Droždie Rekord 1 kg", unit="ks")
    matched = {"gtin": "8588000000002", "matchedCatalogName": "Droždie",
               "matchConfidence": 0.97, "matchReason": "presná zhoda"}
    client = FakeClient([{"documents": [doc]}])
    client._answers["dl_item"] = [matched]
    uploads, posts = [], []
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    qid = pg.execute("SELECT id FROM order_questions WHERE kind = 'dl_mass'").fetchone()
    assert qid, "no mass question"
    held = pg.execute("SELECT detail->'question_ids' FROM email_events WHERE message_id = "
                      "'inv-m' AND stage = 'review' AND detail ? 'held'").fetchone()
    assert held and held[0] == [qid[0]]
    _answer(pg, tmp_path, "Droždie Rekord 1 kg", choice="10", kind="dl_mass")
    assert _run_outcome(pg, "inv-m") is None, "the answered mass hold was not re-queued"


# --- one invoice mail: its invoice PDF and its DL PDF a day apart ---------------------------

def test_an_invoice_pdf_and_its_dl_pdf_a_day_apart_in_one_mail_are_reviewed(pg, tmp_path):
    """The DL PDF (no invoice number) carries the dispatch date, the invoice PDF the delivery
    date: ONE delivery, two documents of one mail — never two DESADVs; the warehouse text
    says what it is (a day apart), never "the same delivery date"."""
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "inv-2pdf", created_at=_ago(hours=1))
    invoice = _one(YESTERDAY, "4400000001", "2400000001")
    dl_pdf = _one(TWO_DAYS_AGO, "7700000001", "", priced=False)
    uploads, posts = [], []
    client = FakeClient([{"documents": [invoice, dl_pdf]}], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1, "one delivery shipped twice from one mail"
    conflict = [p for p in posts if "o deň inak" in p]
    assert len(conflict) == 1 and "rovnaký dátum" not in conflict[0]


# --- round 8: a re-queued invoice and CODEX's data -------------------------------------------

def test_a_requeued_invoice_waits_for_codex_data_newer_than_the_answer(
        pg, tmp_path, monkeypatch):
    """Held on the board; the warehouse types the delivery in by hand AND answers. The
    re-queued invoice must be judged against a copy newer than the answer — never the old one
    that cannot contain the hand receipt."""
    _setup(pg)
    _message(pg, tmp_path, "inv-1", created_at=_ago(hours=2))
    _push_receipts(tmp_path)
    doc = _lines(YESTERDAY, "4400000001", "2400000001", ["Rožok 50g", "Kvasnice X"])
    client = FakeClient([doc, doc], runs=2)
    client._answers["dl_item"] = [ITEM_MATCHED, NO_MATCH] + [ITEM_MATCHED] * 4
    uploads, posts = [], []
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    _wire_release(monkeypatch, client, uploads, posts)
    _answer(pg, tmp_path, "Kvasnice X")
    assert _tick(pg, tmp_path, client, uploads, posts) == 0, "judged against the old copy"
    _push_receipts(tmp_path, [{                       # the next ETL: the hand receipt
        "receipt_number": "261009009", "supplier_ico": "12345678",
        "supplier_eans": [SUPPLIER_EAN], "receipt_date": YESTERDAY.date().isoformat(),
        "dl_numbers": ["999000111"], "total": 10.0}])
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert uploads == [], "the delivery typed in by hand went to ORION a second time"
    assert _run_outcome(pg, "inv-1") == "duplicate"


# --- round 8: a scan in ks and its invoice in KAR a day apart --------------------------------

def _kar_invoice(day, dl, invoice):
    doc = _one(day, dl, invoice, qty=1, unit_price=50.0)
    doc["items"][0]["unit"] = "KAR"
    return {"documents": [doc]}


def test_a_ks_scan_then_its_kar_invoice_a_day_apart_never_ship_twice(pg, tmp_path):
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "dl-1", category="dodacie_listy", subject="Dodací list",
             text="Dodací list", created_at=_ago(hours=3))
    uploads, posts = [], []
    client = FakeClient([{"documents": [_one(TWO_DAYS_AGO, "7700000001", "", priced=False)]},
                         _kar_invoice(YESTERDAY, "4400000001", "2400000001")], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    _message(pg, tmp_path, "inv-1", created_at=_ago(hours=1))
    _push_receipts(tmp_path)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1, "the same delivery went to ORION twice"
    assert _run_outcome(pg, "inv-1") == "review"


def test_a_kar_invoice_then_its_ks_scan_a_day_apart_never_ship_twice(pg, tmp_path):
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "inv-1", created_at=_ago(hours=3))
    uploads, posts = [], []
    client = FakeClient([_kar_invoice(YESTERDAY, "4400000001", "2400000001"),
                         {"documents": [_one(TWO_DAYS_AGO, "7700000001", "", priced=False)]}],
                        runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    shipped_posts = len(posts)
    _message(pg, tmp_path, "dl-1", category="dodacie_listy", subject="Dodací list",
             text="Dodací list", created_at=_ago(hours=1))
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1, "the same delivery went to ORION twice (DL path)"
    assert len(posts) - shipped_posts == 1


def test_an_invoice_pdf_and_its_dl_pdf_both_read_with_the_invoice_number_are_reviewed(
        pg, tmp_path):
    """The invoice prompt asks for the invoice number on EVERY document — a DL PDF read with
    it must still count as the second source of the same mail."""
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "inv-2pdf", created_at=_ago(hours=1))
    invoice = _one(YESTERDAY, "4400000001", "2400000001")
    dl_pdf = _one(TWO_DAYS_AGO, "7700000001", "2400000001", priced=False)
    uploads, posts = [], []
    assert _tick(pg, tmp_path, FakeClient([{"documents": [invoice, dl_pdf]}], runs=2),
                 uploads, posts) == 1
    assert len(uploads) == 1, "one delivery shipped twice from one mail"


# --- round 8: the board ---------------------------------------------------------------------

def test_a_partial_run_whose_held_document_waits_on_another_mails_question_is_requeued(
        pg, tmp_path, monkeypatch):
    """One mail, two documents: A ships, B holds on a line whose ask deduped onto another
    mail's question — the run ends `partial`; the answer still re-queues it (A meets its own
    claim, B ships)."""
    _setup(pg)
    _message(pg, tmp_path, "inv-p", created_at=_ago(hours=3))
    _message(pg, tmp_path, "inv-own", created_at=_ago(hours=2))
    _push_receipts(tmp_path)
    own = _lines(YESTERDAY, "4400000009", "2400000009", ["Kvasnice X"])
    doc_a = _one(TWO_DAYS_AGO, "4400000001", "2400000001", qty=10)
    doc_b = _lines(YESTERDAY, "4400000002", "2400000002",
                   ["Rožok 50g", "Kvasnice X"])["documents"][0]
    doc_b["items"][0].update(quantity=20, totalPrice=10.0)
    doc_b["documentTotalWithoutVAT"] = 15.0
    pair = {"documents": [doc_a, doc_b]}
    client = FakeClient([own, pair, own, pair], runs=8)
    client._answers["dl_item"] = [NO_MATCH, ITEM_MATCHED, ITEM_MATCHED, NO_MATCH] + \
        [ITEM_MATCHED] * 20
    uploads, posts = [], []
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert _run_outcome(pg, "inv-p") == "partial" and len(uploads) == 1
    _wire_release(monkeypatch, client, uploads, posts)
    _answer(pg, tmp_path, "Kvasnice X")
    assert _run_outcome(pg, "inv-p") is None, "the partial run's held document is stranded"


def test_a_ship_without_answer_ships_the_invoice_whose_ask_deduped_onto_it(
        pg, tmp_path, monkeypatch):
    """„Nemá kartu — pošli bez tejto položky" on a question of ANOTHER mail: the invoice that
    waited on it ships without the line — never a fresh question for it."""
    _setup(pg)
    _message(pg, tmp_path, "inv-b", created_at=_ago(hours=3))
    _message(pg, tmp_path, "inv-a", created_at=_ago(hours=2))
    _push_receipts(tmp_path)
    doc_a = _lines(YESTERDAY, "4400000002", "2400000002", ["Rožok 50g", "Kvasnice X"])
    doc_b = _lines(TWO_DAYS_AGO, "4400000001", "2400000001", ["Rožok 50g", "Kvasnice X"])
    doc_b["documents"][0]["items"][0].update(quantity=30, totalPrice=15.0)
    doc_b["documents"][0]["documentTotalWithoutVAT"] = 20.0
    client = FakeClient([doc_a, doc_b, doc_a, doc_b], runs=4)
    client._answers["dl_item"] = [ITEM_MATCHED, NO_MATCH, ITEM_MATCHED, NO_MATCH] + \
        [ITEM_MATCHED, NO_MATCH] * 4
    uploads, posts = [], []
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    _wire_release(monkeypatch, client, uploads, posts)
    _answer(pg, tmp_path, "Kvasnice X", choice=teach.DL_ITEM_SHIP_WITHOUT)
    _ship_all(pg, tmp_path, client, uploads, posts)
    assert len(uploads) == 2, "the invoice that waited on the shared question never shipped"
    assert pg.execute("SELECT count(*) FROM order_questions WHERE status = 'open'"
                      ).fetchone()[0] == 0, "the line was asked again"


def test_closing_the_owner_as_not_warehouse_requeues_the_invoice_waiting_on_it(
        pg, tmp_path):
    _setup(pg)
    _message(pg, tmp_path, "inv-b", created_at=_ago(hours=3))
    _message(pg, tmp_path, "inv-a", created_at=_ago(hours=2))
    _push_receipts(tmp_path)
    doc_a = _lines(YESTERDAY, "4400000002", "2400000002", ["Kvasnice X"])
    doc_b = _lines(TWO_DAYS_AGO, "4400000001", "2400000001", ["Rožok 50g", "Kvasnice X"])
    client = FakeClient([doc_a, doc_b], runs=2)
    client._answers["dl_item"] = [NO_MATCH, ITEM_MATCHED, NO_MATCH]
    uploads, posts = [], []
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    qid = pg.execute("SELECT id FROM order_questions").fetchone()[0]
    dl_questions.close_message_not_warehouse(pg, qid)
    assert _run_outcome(pg, "inv-b") is None, "the invoice waiting on the closed question"


def test_the_owner_waits_while_its_hold_still_waits_on_another_mails_question(
        pg, tmp_path, monkeypatch):
    """inv-b owns question Y and its X deduped onto inv-a's X: answering Y must not re-run
    inv-b (it would hold again and re-post); answering X then re-queues both."""
    _setup(pg)
    _message(pg, tmp_path, "inv-b", created_at=_ago(hours=3))
    _message(pg, tmp_path, "inv-a", created_at=_ago(hours=2))
    _push_receipts(tmp_path)
    doc_a = _lines(YESTERDAY, "4400000002", "2400000002", ["Rožok 50g", "Kvasnice X"])
    doc_b = _lines(TWO_DAYS_AGO, "4400000001", "2400000001",
                   ["Rožok 50g", "Kvasnice X", "Kvasnice Y"])
    client = FakeClient([doc_a, doc_b], runs=2)
    client._answers["dl_item"] = [ITEM_MATCHED, NO_MATCH, ITEM_MATCHED, NO_MATCH, NO_MATCH]
    uploads, posts = [], []
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    _wire_release(monkeypatch, client, uploads, posts)
    _answer(pg, tmp_path, "Kvasnice Y")
    assert _run_outcome(pg, "inv-b") == "review", "re-run while still waiting on X"
    _answer(pg, tmp_path, "Kvasnice X")
    assert _run_outcome(pg, "inv-b") is None and _run_outcome(pg, "inv-a") is None


def test_an_expired_question_never_writes_the_invoice_flows_state(pg, tmp_path):
    _setup(pg)
    _message(pg, tmp_path, "inv-e", created_at=_ago(days=8))
    pg.execute("INSERT INTO dl_invoice_runs (message_id, outcome) VALUES ('inv-e', 'review')")
    pg.execute(
        "INSERT INTO order_questions (message_id, kind, wording, status, customer_ean, "
        "item_key, created_at) VALUES ('inv-e', 'dl_item', 'Kvasnice X', 'open', %s, "
        "'kvasnice x', now() - interval '8 days')", (SUPPLIER_EAN,))
    assert question_alerts.expire_stale(pg, _cfg(tmp_path)) == 1
    assert pg.execute("SELECT processed, proc_status FROM messages WHERE message_id = 'inv-e'"
                      ).fetchone() == (False, None)


# --- round 9 --------------------------------------------------------------------------------

def test_closing_the_owner_as_neviem_requeues_the_invoice_waiting_on_it(pg, tmp_path):
    _setup(pg)
    _message(pg, tmp_path, "inv-b", created_at=_ago(hours=3))
    _message(pg, tmp_path, "inv-a", created_at=_ago(hours=2))
    _push_receipts(tmp_path)
    doc_a = _lines(YESTERDAY, "4400000002", "2400000002", ["Kvasnice X"])
    doc_b = _lines(TWO_DAYS_AGO, "4400000001", "2400000001", ["Rožok 50g", "Kvasnice X"])
    client = FakeClient([doc_a, doc_b], runs=2)
    client._answers["dl_item"] = [NO_MATCH, ITEM_MATCHED, NO_MATCH]
    uploads, posts = [], []
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    qid = pg.execute("SELECT id FROM order_questions").fetchone()[0]
    dl_questions.close_message_sklad_unknown(pg, qid)
    assert _run_outcome(pg, "inv-b") is None, "the invoice waiting on the closed question"


def test_a_requeued_invoice_waits_for_fresh_codex_data_even_without_wait_for_codex(
        pg, tmp_path, monkeypatch):
    """`delivery_notes_invoice_wait_for_codex=false` drops the wait for a NEW mail — never the
    wait of a re-queued one (the warehouse may have typed it in by hand while it was held)."""
    _setup(pg)
    _message(pg, tmp_path, "inv-1", created_at=_ago(hours=2))
    _push_receipts(tmp_path)
    doc = _lines(YESTERDAY, "4400000001", "2400000001", ["Rožok 50g", "Kvasnice X"])
    client = FakeClient([doc, doc], runs=2)
    client._answers["dl_item"] = [ITEM_MATCHED, NO_MATCH] + [ITEM_MATCHED] * 4
    uploads, posts = [], []
    assert _tick(pg, tmp_path, client, uploads, posts,
                 delivery_notes_invoice_wait_for_codex=False) == 1
    _wire_release(monkeypatch, client, uploads, posts)
    _answer(pg, tmp_path, "Kvasnice X")
    assert _tick(pg, tmp_path, client, uploads, posts,
                 delivery_notes_invoice_wait_for_codex=False) == 0
    assert uploads == []


def test_an_invoice_waits_while_another_document_of_its_mail_still_waits_on_a_question(
        pg, tmp_path, monkeypatch):
    """One invoice mail, two documents: A held on its OWN open question Y, B on another mail's
    X. Answering X must not re-run the mail (A would hold again and re-post); answering Y
    then re-queues it."""
    _setup(pg)
    _message(pg, tmp_path, "inv-x", created_at=_ago(hours=3))
    _message(pg, tmp_path, "inv-own", created_at=_ago(hours=2))
    _push_receipts(tmp_path)
    own = _lines(YESTERDAY, "4400000009", "2400000009", ["Kvasnice X"])
    doc_a = _lines(TWO_DAYS_AGO, "4400000001", "2400000001",
                   ["Rožok 50g", "Kvasnice Y"])["documents"][0]
    doc_b = _lines(YESTERDAY, "4400000002", "2400000002",
                   ["Rožok 50g", "Kvasnice X"])["documents"][0]
    doc_b["items"][0].update(quantity=20, totalPrice=10.0)
    doc_b["documentTotalWithoutVAT"] = 15.0
    client = FakeClient([own, {"documents": [doc_a, doc_b]}], runs=4)
    client._answers["dl_item"] = [NO_MATCH, ITEM_MATCHED, NO_MATCH, ITEM_MATCHED, NO_MATCH]
    uploads, posts = [], []
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    _wire_release(monkeypatch, client, uploads, posts)
    _answer(pg, tmp_path, "Kvasnice X")
    assert _run_outcome(pg, "inv-x") == "review", "re-run while document A still waits on Y"
    _answer(pg, tmp_path, "Kvasnice Y")
    assert _run_outcome(pg, "inv-x") is None


def test_the_plain_dl_path_keeps_asking_a_deduped_line_after_a_ship_without(pg, tmp_path):
    """A non-flagged supplier's DL whose ask deduped onto another DL's question answered „pošli
    bez": the DL path behaves as before #485 — the line is asked again for that DL."""
    from psycopg.types.json import Json
    from test_dl_worker import SUPPLIER_MATCHED, _attach, _bev_doc, _msg, _snapshot
    from test_dl_worker import FakeClient as DLClient
    from test_dl_worker import _cfg as dl_cfg
    _snapshot(pg)
    cfg = dl_cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    items = [{"name": "Rožok 50g", "quantity": 10, "unit": "ks", "unitPrice": 0.5,
              "totalPrice": 5.0, "vatRate": 10},
             {"name": "Neznámy nápoj XYZ", "quantity": 6, "unit": "ks", "unitPrice": 2.0,
              "totalPrice": 12.0, "vatRate": 10}]

    def client(number):
        return DLClient({"dl_documents": [_bev_doc(doc_number=number, items=items)],
                         "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED, NO_MATCH]})

    def upload(cfg, name, content, dir_override=None):
        uploads.append(name)

    uploads: list = []
    _msg(pg, mid="dlA")
    _attach(pg, tmp_path, "dlA")
    dl_worker.tick(pg, cfg, client=client("0100000060"), upload=upload)
    _msg(pg, mid="dlB")
    _attach(pg, tmp_path, "dlB")
    dl_worker.tick(pg, cfg, client=client("0100000061"), upload=upload)
    qid = pg.execute("SELECT id FROM order_questions WHERE message_id = 'dlA'").fetchone()[0]
    pg.execute("UPDATE order_questions SET status = 'answered', answer = %s WHERE id = %s",
               (Json({"choice": teach.DL_ITEM_SHIP_WITHOUT}), qid))
    dl_worker.release_for_question(pg, cfg, qid, client=client("0100000060"), upload=upload)
    dl_worker.tick(pg, cfg, client=client("0100000061"), upload=upload)
    asked = pg.execute("SELECT count(*) FROM order_questions WHERE message_id = 'dlB' "
                       "AND status = 'open'").fetchone()[0]
    assert asked == 1, "the plain DL path changed: dlB shipped without being asked"


# --- round 10 -------------------------------------------------------------------------------

def test_an_invoice_of_a_shipped_dl_scans_number_is_a_duplicate_whatever_its_sum(
        pg, tmp_path):
    """The DL scan (priced) shipped at 50 €; its invoice prints the same DL number and adds a
    transport line (54.60 €) — the same document by number, as on the DL path in the other
    order: a duplicate, no „corrected version" review."""
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "dl-1", category="dodacie_listy", subject="Dodací list",
             text="Dodací list", created_at=_ago(hours=3))
    invoice = _one(YESTERDAY, "7700000001", "2400000001")
    invoice["items"].append({"name": "PREPRAVNÉ", "quantity": 1, "unit": "ks",
                             "vatRate": 20, "unitPrice": 4.6, "totalPrice": 4.6})
    invoice["documentTotalWithoutVAT"] = 54.6
    uploads, posts = [], []
    client = FakeClient([{"documents": [_one(YESTERDAY, "7700000001", "")]},
                         {"documents": [invoice]}], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    shipped_posts = len(posts)
    _message(pg, tmp_path, "inv-1", created_at=_ago(hours=1))
    _push_receipts(tmp_path)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1
    assert _run_outcome(pg, "inv-1") == "duplicate"
    assert posts[shipped_posts:] == []


def test_an_invoice_with_no_document_never_writes_the_invoice_flows_state(pg, tmp_path):
    """No delivery note recognised in the invoice: a review for the warehouse, never the n8n
    invoice flow's proc_status / outcome (its rollup)."""
    _setup(pg)
    _message(pg, tmp_path, "inv-0")
    pg.execute("UPDATE messages SET proc_status = 'ok', proc_outcome = 'preposlané' "
               "WHERE message_id = 'inv-0'")
    _push_receipts(tmp_path)
    uploads, posts = [], []
    assert _tick(pg, tmp_path, FakeClient([{"documents": []}]), uploads, posts) == 1
    assert pg.execute("SELECT proc_status, proc_outcome FROM messages WHERE message_id = "
                      "'inv-0'").fetchone() == ("ok", "preposlané")


def test_a_receipt_booked_over_many_days_is_no_same_day_proof(pg, tmp_path):
    """Zeelandia books a receipt over three weeks (receipt_date … receipt_date_to): an invoice
    inside that span with the same sum is not provably that receipt — a review, never a
    silent drop."""
    _setup(pg)
    _message(pg, tmp_path, "inv-span")
    _push_receipts(tmp_path, [{
        "receipt_number": "261004158", "supplier_ico": "12345678",
        "supplier_eans": [SUPPLIER_EAN],
        "receipt_date": (YESTERDAY - timedelta(days=10)).date().isoformat(),
        "receipt_date_to": (YESTERDAY + timedelta(days=5)).date().isoformat(),
        "dl_numbers": ["990000123"], "total": 50.0}])
    uploads, posts = [], []
    assert _tick(pg, tmp_path, FakeClient([{"documents": [
        _one(YESTERDAY, "4400000001", "2400000001")]}]), uploads, posts) == 1
    assert uploads == []
    assert _run_outcome(pg, "inv-span") == "review"
    assert len(posts) == 1 and "261004158" in posts[0]


# --- round 11: a DL scan and an invoice sharing a number on another day ----------------------

def _scan_then_invoice(pg, tmp_path, scan_doc, invoice_doc):
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "dl-1", category="dodacie_listy", subject="Dodací list",
             text="Dodací list", created_at=_ago(hours=5))
    uploads, posts = [], []
    client = FakeClient([{"documents": [scan_doc]}, {"documents": [invoice_doc]}], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1
    shipped_posts = len(posts)
    _message(pg, tmp_path, "inv-1", created_at=_ago(hours=1))
    _push_receipts(tmp_path)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    return uploads, posts[shipped_posts:]


def test_an_invoice_whose_own_number_is_an_older_scans_dl_number_is_reviewed(pg, tmp_path):
    """Another number series: the invoice's OWN number has the digits of an older scan's DL
    number — another day, other goods: a genuinely different delivery is never dropped
    silently (the early gate defers to the built EDI, which sees other goods)."""
    uploads, new_posts = _scan_then_invoice(
        pg, tmp_path, _one(TWO_DAYS_AGO, "2400000001", "", qty=100),
        _one(YESTERDAY, "4400000001", "2400000001", qty=30))
    assert len(uploads) == 1
    assert _run_outcome(pg, "inv-1") == "review" and len(new_posts) == 1


def test_an_invoice_printing_a_scans_dl_number_another_day_other_goods_is_reviewed(
        pg, tmp_path):
    uploads, new_posts = _scan_then_invoice(
        pg, tmp_path, _one(TWO_DAYS_AGO, "7700000001", "", qty=100),
        _one(YESTERDAY, "7700000001", "2400000001", qty=30))
    assert len(uploads) == 1
    assert _run_outcome(pg, "inv-1") == "review" and len(new_posts) == 1


def test_the_same_with_a_priceless_scan_is_reviewed(pg, tmp_path):
    uploads, new_posts = _scan_then_invoice(
        pg, tmp_path, _one(TWO_DAYS_AGO, "7700000001", "", qty=100, priced=False),
        _one(YESTERDAY, "7700000001", "2400000001", qty=30))
    assert len(uploads) == 1
    assert _run_outcome(pg, "inv-1") == "review" and len(new_posts) == 1


def test_an_invoice_printing_a_scans_dl_number_a_day_later_with_its_goods_is_a_duplicate(
        pg, tmp_path):
    """Pin: the LESAFFRE dispatch-date pair — the scan and its invoice share the DL number,
    a day apart, the SAME goods: a silent duplicate (no review, no second DESADV)."""
    uploads, new_posts = _scan_then_invoice(
        pg, tmp_path, _one(TWO_DAYS_AGO, "7700000001", "", qty=100, priced=False),
        _one(YESTERDAY, "7700000001", "2400000001", qty=100))
    assert len(uploads) == 1 and new_posts == []
    assert _run_outcome(pg, "inv-1") == "duplicate"


def test_a_dl_scan_printing_an_invoice_rows_dl_number_another_day_other_goods_is_reviewed(
        pg, tmp_path):
    """The DL path: the scan of ANOTHER delivery printing a shipped invoice's DL number."""
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "inv-1", created_at=_ago(hours=5))
    uploads, posts = [], []
    client = FakeClient([{"documents": [_one(TWO_DAYS_AGO, "7700000001", "2400000001")]},
                         {"documents": [_one(YESTERDAY, "7700000001", "", qty=30,
                                             priced=False)]}], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    shipped_posts = len(posts)
    _message(pg, tmp_path, "dl-1", category="dodacie_listy", subject="Dodací list",
             text="Dodací list", created_at=_ago(hours=1))
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1 and len(posts) - shipped_posts == 1


def _invoice_flow_state(pg, mid):
    return pg.execute("SELECT proc_status, proc_outcome FROM messages WHERE message_id = %s",
                      (mid,)).fetchone()


def test_an_empty_invoice_mail_never_writes_the_invoice_flows_state(pg, tmp_path):
    _setup(pg)
    _message(pg, tmp_path, "inv-n", text="", attachments=())
    pg.execute("UPDATE messages SET proc_status = 'ok', proc_outcome = 'preposlané' "
               "WHERE message_id = 'inv-n'")
    _push_receipts(tmp_path)
    uploads, posts = [], []
    assert _tick(pg, tmp_path, FakeClient([]), uploads, posts) == 1
    assert _invoice_flow_state(pg, "inv-n") == ("ok", "preposlané")


def test_a_correction_invoice_mail_never_writes_the_invoice_flows_state(pg, tmp_path):
    _setup(pg)
    _message(pg, tmp_path, "inv-c", subject="OPRAVA HMOTNOSTI", attachments=(),
             text="Rožok 50g = 90 ks (nie 100 ks), zvyšok bez zmien")
    pg.execute("UPDATE messages SET proc_status = 'ok', proc_outcome = 'preposlané' "
               "WHERE message_id = 'inv-c'")
    _push_receipts(tmp_path)
    uploads, posts = [], []
    assert _tick(pg, tmp_path, FakeClient([]), uploads, posts) == 1
    assert uploads == []
    assert _invoice_flow_state(pg, "inv-c") == ("ok", "preposlané")
