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

from app.orders import dl_snapshot, dl_worker, teach

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
