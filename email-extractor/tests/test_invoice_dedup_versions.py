"""#485 review round 2: the invoice dedup must tell a DUPLICATE from a SECOND DELIVERY.

Round 1 matched by date within ±1 day + total / content against our own shipments — a standing
order (the same goods every day) then lost every second day: Tuesday's invoice met Monday's
DESADV (or CODEX's import of it), on the invoice path AND the DL path. These tests pin: two
deliveries on consecutive days both ship; an ambiguous same-day match with another invoice
number and a corrected version arriving after the first shipped are never shipped silently (a
review tells the warehouse); the Kôš / a revived card restart the flag's clock (no backlog);
a CODEX copy of unknown age holds invoices; a terminal board click on an invoice mail never
writes the n8n invoice flow's state; the day's stats count the gate's verdicts.

Synthetic data only (made-up supplier, numbers, addresses) — this repo is public.
"""
import logging
from datetime import UTC, datetime, timedelta

from test_invoice_dedup_regression import (
    ITEM_MATCHED,
    SUPPLIER_EAN,
    SUPPLIER_EMAIL,
    SUPPLIER_NAME,
    FakeClient,
    _cfg,
    _message,
    _push_receipts,
    _run_outcome,
    _setup,
    _tick,
)

from app.board.services import audit, suppliers
from app.httpapi import create_app
from app.orders import dl_questions, reliability

TWO_DAYS_AGO = datetime.now(UTC) - timedelta(days=2)
YESTERDAY = datetime.now(UTC) - timedelta(days=1)
NO_MATCH = {"gtin": "NO_MATCH", "matchConfidence": 0.0, "matchReason": "žiadna zhoda"}


def _doc_on(day, doc_number, invoice_number, *, total=50.0, quantity=100, priced=True,
            extra_items=()):
    item = {"name": "Rožok 50g", "quantity": quantity, "unit": "ks", "vatRate": 10}
    if priced:
        item.update(unitPrice=0.5, totalPrice=round(quantity * 0.5, 2))
    return {"documents": [{
        "supplierName": SUPPLIER_NAME, "supplierCity": "Mesto",
        "supplierEmail": SUPPLIER_EMAIL, "docNumber": doc_number,
        "invoiceNumber": invoice_number, "deliveryDate": day.strftime("%d.%m.%Y"),
        "documentTotalWithoutVAT": total if priced else 0,
        "items": [item, *extra_items]}]}


def _ago(**kw):
    return datetime.now(UTC) - timedelta(**kw)


def _card_id(pg):
    return pg.execute("SELECT id FROM dl_supplier_overrides WHERE ean_edi = %s",
                      (SUPPLIER_EAN,)).fetchone()[0]


def _since(pg):
    return pg.execute("SELECT invoice_dl_since FROM dl_supplier_overrides WHERE id = %s",
                      (_card_id(pg),)).fetchone()[0]


# --- a standing order: the same goods on two days are two deliveries ----------------------

def test_a_standing_order_invoiced_on_two_days_ships_both_days(pg, tmp_path):
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "inv-mon", created_at=_ago(hours=3))
    uploads, posts = [], []
    client = FakeClient([_doc_on(TWO_DAYS_AGO, "4400000001", "2400000001"),
                         _doc_on(YESTERDAY, "4400000002", "2400000002")], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    _message(pg, tmp_path, "inv-tue", created_at=_ago(hours=1))
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 2, "Tuesday's delivery was taken for Monday's"
    assert _run_outcome(pg, "inv-tue") == "ok"


def test_a_dl_scan_of_the_next_days_standing_order_ships_after_the_invoice(pg, tmp_path):
    """The DL path checks our invoice-derived rows — by the SAME day only: a priceless scan of
    Tuesday's identical goods is no twin of Monday's invoice."""
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "inv-mon", created_at=_ago(hours=3))
    uploads, posts = [], []
    client = FakeClient([_doc_on(TWO_DAYS_AGO, "4400000001", "2400000001"),
                         _doc_on(YESTERDAY, "7700000002", "", priced=False)], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    _message(pg, tmp_path, "dl-tue", category="dodacie_listy", subject="Dodací list",
             text="Dodací list 7700000002", created_at=_ago(hours=1))
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 2, "the DL path skipped Tuesday's delivery as Monday's twin"


def test_codex_importing_our_monday_desadv_never_blocks_tuesdays_invoice(pg, tmp_path):
    """CODEX books our own Monday DESADV as a receipt (its DL number = our document number). A
    receipt that IS one of our shipments is explained by it — it must not match Tuesday's
    invoice by date ±1 + the same total."""
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "inv-mon", created_at=_ago(hours=3))
    uploads, posts = [], []
    client = FakeClient([_doc_on(TWO_DAYS_AGO, "4400000001", "2400000001"),
                         _doc_on(YESTERDAY, "4400000002", "2400000002")], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    _message(pg, tmp_path, "inv-tue", created_at=_ago(hours=1))
    _push_receipts(tmp_path, [{
        "receipt_number": "261009001", "supplier_ico": "12345678",
        "supplier_eans": [SUPPLIER_EAN], "receipt_date": TWO_DAYS_AGO.date().isoformat(),
        "dl_numbers": ["4400000001"], "total": 50.0}])
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 2, "CODEX's copy of Monday's DESADV blocked Tuesday's invoice"


def test_a_codex_receipt_linked_to_another_invoice_never_matches_by_date(pg, tmp_path):
    """A hand-entered receipt from the day before, already linked in CODEX to ANOTHER invoice
    than ours, is another delivery — same total or not."""
    _setup(pg)
    _message(pg, tmp_path, "inv-tue")
    _push_receipts(tmp_path, [{
        "receipt_number": "261009002", "supplier_ico": "12345678",
        "supplier_eans": [SUPPLIER_EAN], "receipt_date": TWO_DAYS_AGO.date().isoformat(),
        "dl_numbers": ["999000222"], "invoice_number": "2400000001", "total": 50.0}])
    uploads, posts = [], []
    client = FakeClient([_doc_on(YESTERDAY, "4400000002", "2400000002")])
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1


def test_a_same_day_match_with_another_invoice_number_is_asked_never_shipped_or_silent(
        pg, tmp_path):
    """Same day + same total + same goods, but another invoice number: a reissued invoice of
    the same delivery OR a second delivery that day — not provable either way. Never a second
    DESADV (the harm #485 exists for), never a silent skip either: the warehouse is told."""
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "inv-a", created_at=_ago(hours=3))
    uploads, posts = [], []
    client = FakeClient([_doc_on(YESTERDAY, "4400000001", "2400000001"),
                         _doc_on(YESTERDAY, "4400000002", "2400000002")], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    shipped_posts = len(posts)
    _message(pg, tmp_path, "inv-b", created_at=_ago(hours=1))
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1
    assert _run_outcome(pg, "inv-b") == "review"
    new = posts[shipped_posts:]
    assert len(new) == 1 and "2400000002" in new[0] and "2400000001" in new[0]
    assert "NEposiela" in new[0]


# --- versions: a corrected invoice after the first one shipped ----------------------------

def test_a_corrected_invoice_after_the_first_version_shipped_is_reviewed_not_silent(
        pg, tmp_path):
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "inv-v1", created_at=_ago(hours=3))
    uploads, posts = [], []
    client = FakeClient([_doc_on(YESTERDAY, "4400000001", "2400000001"),
                         _doc_on(YESTERDAY, "4400000001", "2400000001", quantity=90,
                                 total=45.0)], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    shipped_posts = len(posts)
    _message(pg, tmp_path, "inv-v2", subject="Opravená faktúra 2400000001",
             created_at=_ago(minutes=30))
    _push_receipts(tmp_path)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1, "a corrected version must never ship a second DESADV"
    assert _run_outcome(pg, "inv-v2") == "review"
    new = posts[shipped_posts:]
    assert len(new) == 1 and "opravená verzia" in new[0] and "45.00" in new[0]


def test_an_older_version_met_after_the_newer_shipped_stays_a_silent_duplicate(pg, tmp_path):
    _setup(pg)
    _message(pg, tmp_path, "inv-old", created_at=_ago(hours=3))
    _message(pg, tmp_path, "inv-new", created_at=_ago(hours=1))
    _push_receipts(tmp_path)
    uploads, posts = [], []
    client = FakeClient([_doc_on(YESTERDAY, "4400000001", "2400000001", quantity=90,
                                 total=45.0),
                         _doc_on(YESTERDAY, "4400000001", "2400000001")], runs=2)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    shipped_posts = len(posts)
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1
    assert _run_outcome(pg, "inv-old") == "duplicate"
    assert posts[shipped_posts:] == [], "the right content already went — nothing to check"


def test_a_dl_scan_twin_is_decided_before_any_board_question(pg, tmp_path):
    """The DL scan of goods the invoice already shipped carries a line the catalog does not
    know: the twin check runs BEFORE the hold — no board question for goods that already went
    (the unknown line never reaches the EDI, so the scan's content IS the shipment's)."""
    _setup(pg)
    _push_receipts(tmp_path)
    _message(pg, tmp_path, "inv-1", created_at=_ago(hours=3))
    uploads, posts = [], []
    extra = {"name": "Neznámy tovar", "quantity": 5, "unit": "ks", "vatRate": 10}
    client = FakeClient([_doc_on(YESTERDAY, "4400000001", "2400000001"),
                         _doc_on(YESTERDAY, "4400000001", "", priced=False,
                                 extra_items=(extra,))], runs=2)
    client._answers["dl_item"] = [ITEM_MATCHED, ITEM_MATCHED, NO_MATCH]
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    shipped_posts = len(posts)
    _message(pg, tmp_path, "dl-1", category="dodacie_listy", subject="Dodací list",
             text="Dodací list 4400000001", created_at=_ago(hours=1))
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1
    assert pg.execute("SELECT count(*) FROM order_questions").fetchone()[0] == 0, \
        "the warehouse was asked about a line of goods that already went to ORION"
    assert posts[shipped_posts:] == []
    assert pg.execute("SELECT count(*) FROM email_events WHERE message_id = 'dl-1' "
                      "AND stage = 'duplicate_skip'").fetchone()[0] == 1


# --- credit notes: the mail's words decide only for a single document ---------------------

def test_a_mail_saying_invoice_and_credit_note_still_ships_the_invoice(pg, tmp_path):
    _setup(pg)
    _message(pg, tmp_path, "inv-mix", subject="Faktúra a dobropis",
             attachments=(("faktura.pdf", "Faktúra 2400000001 Rožok 50g 100 ks"),
                          ("priloha.pdf", "Dodacie podmienky a kontakty")))
    _push_receipts(tmp_path)
    uploads, posts = [], []
    client = FakeClient([_doc_on(YESTERDAY, "4400000001", "2400000001"),
                         {"documents": []}])
    assert _tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 1, "the mail's subject killed the invoice beside the credit note"


# --- the flag's clock: Kôš restore and a revived card --------------------------------------

def test_restoring_a_deleted_flagged_card_from_the_trash_restarts_its_clock(pg, tmp_path):
    _setup(pg)
    pg.execute("UPDATE dl_supplier_overrides SET invoice_dl_since = now() - interval '10 days'")
    suppliers.delete_supplier(pg, _cfg(tmp_path), "admin", {"override_id": _card_id(pg)})
    aid = pg.execute("SELECT max(id) FROM audit_log WHERE action = 'delete'").fetchone()[0]
    assert audit.restore(pg, aid) is True
    assert _since(pg) > datetime.now(UTC) - timedelta(minutes=5), \
        "the restored card would take its 10-day invoice backlog as delivery notes"


def test_restoring_a_flag_switch_off_from_the_trash_restarts_the_clock(pg, tmp_path):
    _setup(pg)
    pg.execute("UPDATE dl_supplier_overrides SET invoice_dl_since = now() - interval '10 days'")
    suppliers.save_supplier(pg, _cfg(tmp_path), "admin", {
        "override_id": _card_id(pg), "ean_edi": SUPPLIER_EAN, "name": SUPPLIER_NAME,
        "emails": SUPPLIER_EMAIL, "city": "Mesto", "invoice_is_delivery_note": False})
    aid = pg.execute("SELECT max(id) FROM audit_log WHERE action = 'update'").fetchone()[0]
    assert audit.restore(pg, aid) is True
    flag = pg.execute("SELECT invoice_is_delivery_note FROM dl_supplier_overrides "
                      "WHERE id = %s", (_card_id(pg),)).fetchone()[0]
    assert flag is True
    assert _since(pg) > datetime.now(UTC) - timedelta(minutes=5)


def test_saving_a_retired_flagged_card_back_to_life_restarts_its_clock(pg, tmp_path):
    _setup(pg)
    pg.execute("UPDATE dl_supplier_overrides SET invoice_dl_since = now() - interval '10 days'")
    rid = _card_id(pg)
    suppliers.delete_supplier(pg, _cfg(tmp_path), "admin", {"override_id": rid})
    suppliers.save_supplier(pg, _cfg(tmp_path), "admin", {
        "override_id": rid, "ean_edi": SUPPLIER_EAN, "name": SUPPLIER_NAME,
        "emails": SUPPLIER_EMAIL, "city": "Mesto", "invoice_is_delivery_note": True})
    assert _since(pg) > datetime.now(UTC) - timedelta(minutes=5)


def test_a_save_that_brings_a_retired_card_back_is_a_change_of_who_it_is(pg, tmp_path):
    """Pin: un-retiring a card can unstick mail waiting on it (the release still fires) — only
    a pure flag toggle of a LIVE card skips it."""
    _setup(pg)
    rid = _card_id(pg)
    pg.execute("UPDATE dl_supplier_overrides SET retired = true WHERE id = %s", (rid,))
    pg.execute(
        """INSERT INTO messages (message_id, category, subject, from_addr, combined_text,
                                 processed, proc_status)
           VALUES ('stuck-2', 'dodacie_listy', 'Dodací list', %s, 'text', true, 'review')""",
        (SUPPLIER_EMAIL,))
    suppliers.save_supplier(pg, _cfg(tmp_path), "admin", {
        "override_id": rid, "ean_edi": SUPPLIER_EAN, "name": SUPPLIER_NAME,
        "emails": SUPPLIER_EMAIL, "city": "Mesto", "invoice_is_delivery_note": True})
    row = pg.execute("SELECT processed FROM messages WHERE message_id='stuck-2'").fetchone()
    assert row[0] is False, "re-activating the card did not release the mail waiting on it"


def test_the_migration_stamps_a_card_flagged_before_it(pg, reapply_schema):
    """Pin: a card already taking invoices when #485 installs gets `invoice_dl_since` = the
    install time — never NULL (= no lower bound, its whole backlog)."""
    pg.execute("INSERT INTO dl_supplier_overrides (ean_edi, name, emails, city, "
               "invoice_is_delivery_note) VALUES (%s, %s, %s, 'Mesto', true)",
               (SUPPLIER_EAN, SUPPLIER_NAME, [SUPPLIER_EMAIL]))
    assert _since(pg) is None
    reapply_schema()
    assert _since(pg) is not None


# --- a CODEX copy of unknown age ------------------------------------------------------------

def test_a_codex_receipts_push_without_an_etl_time_holds_invoices(pg, tmp_path):
    _setup(pg)
    _message(pg, tmp_path, "inv-1")
    app = create_app(_cfg(tmp_path))
    r = app.test_client().post(
        "/api/codex/receipts", headers={"X-Token": "tok"}, query_string={"force": "1"},
        json={"days": 60, "receipts": [{
            "receipt_number": "261000001", "supplier_ico": "11111111",
            "supplier_eans": ["2000000000001"], "receipt_date": YESTERDAY.date().isoformat(),
            "dl_numbers": ["123456789"], "total": 1.0}]})
    assert r.status_code == 200
    uploads, posts = [], []
    client = FakeClient([_doc_on(YESTERDAY, "4400000001", "2400000001")])
    assert _tick(pg, tmp_path, client, uploads, posts) == 0
    assert uploads == [] and client.calls == []


# --- a terminal board click on an invoice mail ----------------------------------------------

def _invoice_question(pg, tmp_path, mid):
    _setup(pg)
    _message(pg, tmp_path, mid)
    pg.execute("INSERT INTO dl_invoice_runs (message_id, outcome) VALUES (%s, 'review')",
               (mid,))
    return pg.execute(
        "INSERT INTO order_questions (message_id, kind, wording, status, customer_ean, "
        "item_key) VALUES (%s, 'dl_item', 'Rožok 50g', 'open', %s, 'rozok 50g') "
        "RETURNING id", (mid, SUPPLIER_EAN)).fetchone()[0]


def _n8n_state(pg, mid):
    return pg.execute("SELECT processed, proc_status FROM messages WHERE message_id = %s",
                      (mid,)).fetchone()


def test_not_warehouse_on_an_invoice_mail_never_writes_the_invoice_flows_state(pg, tmp_path):
    qid = _invoice_question(pg, tmp_path, "inv-nw")
    dl_questions.close_message_not_warehouse(pg, qid)
    assert _n8n_state(pg, "inv-nw") == (False, None), \
        "messages.processed / proc_status belong to the n8n invoice-forward flow"
    assert _run_outcome(pg, "inv-nw") == "not_warehouse"


def test_sklad_unknown_on_an_invoice_mail_never_writes_the_invoice_flows_state(pg, tmp_path):
    qid = _invoice_question(pg, tmp_path, "inv-su")
    dl_questions.close_message_sklad_unknown(pg, qid)
    assert _n8n_state(pg, "inv-su") == (False, None)
    assert _run_outcome(pg, "inv-su") == "sklad_unknown"


def test_a_board_answer_on_an_invoice_outside_the_claim_window_is_alerted(pg, tmp_path,
                                                                          caplog):
    _setup(pg)
    _message(pg, tmp_path, "inv-old", created_at=_ago(days=20))
    pg.execute("INSERT INTO dl_invoice_runs (message_id, outcome) VALUES ('inv-old', "
               "'review')")
    qid = pg.execute(
        "INSERT INTO order_questions (message_id, kind, wording, status, customer_ean, "
        "item_key) VALUES ('inv-old', 'dl_item', 'Rožok 50g', 'answered', %s, 'rozok 50g') "
        "RETURNING id", (SUPPLIER_EAN,)).fetchone()[0]
    with caplog.at_level(logging.WARNING, logger="orders.dl_worker"):
        dl_questions.release_for_question(pg, _cfg(tmp_path), qid)
    assert any("will never take it" in r.getMessage() for r in caplog.records)
    alert = pg.execute("SELECT body_html FROM pending_alerts WHERE kind = "
                       "'dl_invoice_stranded'").fetchone()
    assert alert and "staršia ako 14 dní" in alert[0], "the warehouse is never told"


# --- the day's stats ------------------------------------------------------------------------

def test_the_days_dl_stats_count_the_invoice_gates_verdicts(pg, tmp_path):
    _setup(pg)
    _message(pg, tmp_path, "inv-dup")
    _push_receipts(tmp_path, [{
        "receipt_number": "261004409", "supplier_ico": "12345678",
        "supplier_eans": [SUPPLIER_EAN], "receipt_date": YESTERDAY.date().isoformat(),
        "dl_numbers": ["2400000001"], "total": 50.0}])
    uploads, posts = [], []
    assert _tick(pg, tmp_path, FakeClient([_doc_on(YESTERDAY, "4400000001", "2400000001")]),
                 uploads, posts) == 1
    _message(pg, tmp_path, "cn-1", subject="Dobropis č. 2500000001")
    assert _tick(pg, tmp_path, FakeClient([]), uploads, posts) == 1
    stats = reliability.dl_provenance_stats_for_day(pg, include_current_health=False)
    assert stats["invoice_duplicates"] == 1
    assert stats["invoice_credit_notes"] == 1
    assert stats["invoice_conflicts"] == 0
