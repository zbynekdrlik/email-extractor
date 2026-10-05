"""#485: the CODEX supplier-receipts copy + the invoice-as-DL duplicate gate's rules.

`codex_receipts` (the pushed copy: atomic replace, freshness, fail-closed, the ops alerts),
`invoice_dedup` (the pure rules + the match against receipts and our own `desadv_sent`),
`dl_invoice` (credit notes, the invoice's sources, the stale hold), `desadv.record_facts` /
`desadv_edi.lin_quantities` (the facts our own shipments carry), `dl_snapshot.set_invoice_flag`
and the endpoint's guards. The end-to-end pipeline cases live in
`test_invoice_dedup_regression.py`.

Synthetic data only (made-up suppliers, numbers) — this repo is public.
"""
import logging
import os
from datetime import UTC, datetime, timedelta

import pytest

from app.config import Config
from app.httpapi import create_app
from app.orders import (
    codex_receipts,
    codex_snapshot,
    desadv,
    desadv_edi,
    dl_invoice,
    dl_message,
    dl_snapshot,
    invoice_dedup,
)

EAN = "2000000000991"
NOW = datetime.now(UTC)
TODAY = NOW.date()


def _cfg(**kw):
    base = dict(pg_dsn=os.environ.get("PG_TEST_DSN", ""), data_dir="/tmp", api_token="tok",
                dash_password="pw", secret_key="t", ops_channel_id=77)
    base.update(kw)
    return Config(**base)


def _receipt(number="261004409", ean=EAN, day=None, **kw):
    r = {"receipt_number": number, "supplier_ico": "12345678", "supplier_eans": [ean],
         "receipt_date": (day or TODAY).isoformat(), "dl_numbers": [], "total": 100.0}
    r.update(kw)
    return r


def _push(pg, receipts=None, as_of=None, force=False):
    return codex_receipts.replace_receipts(
        pg, receipts if receipts is not None else [_receipt()],
        source_as_of=(as_of or NOW).isoformat(), days=60, force=force)


def _flag(pg, ean=EAN):
    return pg.execute(
        "INSERT INTO dl_supplier_overrides (ean_edi, name, emails, city, "
        "invoice_is_delivery_note) VALUES (%s, 'Dodávateľ', '{}', 'Mesto', true) "
        "RETURNING id", (ean,)).fetchone()[0]


# --- the copy: atomic replace + guards ----------------------------------------------------

def test_a_push_replaces_the_whole_copy(pg):
    _push(pg, [_receipt("1"), _receipt("2")])
    assert _push(pg, [_receipt("2"), _receipt("3")]) == {"stored": 2}
    rows = pg.execute("SELECT receipt_number FROM codex_receipts ORDER BY 1").fetchall()
    assert rows == [("2",), ("3",)], "a receipt deleted in CODEX must stop blocking invoices"
    assert pg.execute("SELECT count(*) FROM codex_receipt_syncs").fetchone()[0] == 2


def test_rows_without_a_number_or_a_date_are_skipped_and_duplicates_collapse(pg):
    res = _push(pg, [_receipt("1", total=1.0), _receipt("1", total=2.0),
                     {"receipt_number": "", "receipt_date": TODAY.isoformat()},
                     {"receipt_number": "9", "receipt_date": "neviem"}, "junk"])
    assert res == {"stored": 1}
    assert pg.execute("SELECT total FROM codex_receipts").fetchone()[0] == 2


def test_list_fields_keep_every_ean_and_dl_number(pg):
    _push(pg, [_receipt(supplier_eans=["2000000000777", EAN, "", EAN],
                        dl_numbers=["526013012", "", "1144195"])])
    row = pg.execute("SELECT supplier_eans, dl_numbers FROM codex_receipts").fetchone()
    assert row == (["2000000000777", EAN], ["1144195", "526013012"])
    assert [r.receipt_number for r in
            codex_receipts.live(pg).for_supplier(pg, "2000000000777")] == ["261004409"]


def test_an_empty_push_is_refused_and_keeps_the_previous_copy(pg, caplog):
    _push(pg)
    with caplog.at_level(logging.WARNING, logger="orders.codex_receipts"):
        with pytest.raises(codex_receipts.ReplaceRefused) as e:
            _push(pg, [])
    assert e.value.status == 400
    assert pg.execute("SELECT count(*) FROM codex_receipts").fetchone()[0] == 1
    assert any("refused" in r.getMessage() for r in caplog.records)


def test_a_drastically_smaller_push_is_refused_unless_forced(pg):
    _push(pg, [_receipt(str(i)) for i in range(10)])
    with pytest.raises(codex_receipts.ReplaceRefused) as e:
        _push(pg, [_receipt("1"), _receipt("2")])
    assert e.value.status == 409
    assert pg.execute("SELECT count(*) FROM codex_receipts").fetchone()[0] == 10
    assert _push(pg, [_receipt("1"), _receipt("2")], force=True) == {"stored": 2}


def test_both_codex_snapshots_share_one_refusal_type():
    from app.orders import codex_cards
    assert codex_receipts.ReplaceRefused is codex_cards.ReplaceRefused \
        is codex_snapshot.ReplaceRefused
    assert codex_receipts.STALE_HOURS == codex_cards.STALE_HOURS == codex_snapshot.STALE_HOURS


# --- freshness: fail CLOSED -----------------------------------------------------------------

def test_never_pushed_or_stale_receipts_are_not_live(pg):
    assert codex_receipts.live(pg) is None
    _push(pg, as_of=NOW - timedelta(hours=codex_receipts.STALE_HOURS + 1))
    assert codex_receipts.live(pg) is None
    _push(pg, as_of=NOW - timedelta(hours=codex_receipts.STALE_HOURS - 1))
    live = codex_receipts.live(pg)
    assert live is not None and abs((live.as_of - (NOW - timedelta(
        hours=codex_receipts.STALE_HOURS - 1))).total_seconds()) < 1


def test_a_future_source_time_can_never_keep_the_receipts_fresh(pg):
    _push(pg, as_of=NOW + timedelta(days=30))
    pg.execute("UPDATE codex_receipt_syncs SET synced_at = now() - interval '40 hours'")
    assert codex_receipts.live(pg) is None


def test_the_hold_warning_is_rate_limited(pg, caplog):
    codex_receipts._last_warned.clear()
    with caplog.at_level(logging.WARNING, logger="orders.codex_receipts"):
        for _ in range(5):
            codex_receipts.live(pg)
    assert sum("never pushed" in r.getMessage() for r in caplog.records) == 1


def _alerts(pg, kind=codex_receipts.ALERT_KIND):
    return pg.execute("SELECT channel_id, body_html, message_id FROM pending_alerts "
                      "WHERE kind = %s", (kind,)).fetchall()


def test_stale_sweep_alerts_ops_once_per_episode_only_while_an_invoice_flag_is_on(pg):
    _push(pg, as_of=NOW - timedelta(hours=codex_receipts.STALE_HOURS + 2))
    assert codex_receipts.stale_sweep(pg, _cfg()) is False, "no flag → nothing is held"
    _flag(pg)
    assert codex_receipts.stale_sweep(pg, _cfg()) is True
    assert codex_receipts.stale_sweep(pg, _cfg()) is False, "deduped while undelivered"
    rows = _alerts(pg)
    assert len(rows) == 1 and rows[0][0] == 77
    assert "NEspracúvajú" in rows[0][1] and "codex-receipts-push.timer" in rows[0][1]


def test_stale_sweep_is_quiet_for_fresh_receipts_and_gives_a_new_install_grace(pg):
    _flag(pg)
    assert codex_receipts.stale_sweep(pg, _cfg()) is False, "grace right after the deploy"
    late = NOW + timedelta(hours=codex_receipts.STALE_HOURS + 1)
    assert codex_receipts.stale_sweep(pg, _cfg(), now=late) is True
    assert "nikdy" in _alerts(pg)[0][1]
    pg.execute("DELETE FROM pending_alerts")
    _push(pg)
    assert codex_receipts.stale_sweep(pg, _cfg()) is False


def test_a_flagged_supplier_codex_has_no_receipt_for_is_reported_once(pg):
    """Its EDI EAN on our card matches no raw.firma AEDIEAN → the gate is blind to its CODEX
    receipts; ops is told once (not a hold — our ledger + numbers still guard)."""
    _flag(pg, EAN)
    _flag(pg, "2000000000555")
    _push(pg, [_receipt(ean="2000000000555")])
    assert codex_receipts.missing_supplier_sweep(pg, _cfg()) == 1
    assert codex_receipts.missing_supplier_sweep(pg, _cfg()) == 0, "deduped"
    rows = _alerts(pg, codex_receipts.MISSING_KIND)
    assert len(rows) == 1 and EAN in rows[0][1] and rows[0][2].endswith(EAN)


def test_the_missing_supplier_sweep_is_silent_without_a_fresh_copy(pg):
    _flag(pg)
    assert codex_receipts.missing_supplier_sweep(pg, _cfg()) == 0


# --- the endpoint -------------------------------------------------------------------------

def _post(body, headers=None, query=""):
    c = create_app(_cfg()).test_client()
    return c.post("/api/codex/receipts" + query, json=body, headers=headers or {})


def test_the_endpoint_needs_the_header_token(pg):
    body = {"receipts": [_receipt()]}
    assert _post(body).status_code == 403
    assert _post(body, query="?token=tok").status_code == 403, "never a URL token"
    assert _post(body, {"X-Token": "nope"}).status_code == 403
    assert pg.execute("SELECT count(*) FROM codex_receipts").fetchone()[0] == 0
    r = _post({**body, "source_as_of": NOW.isoformat(), "days": 60}, {"X-Token": "tok"})
    assert r.status_code == 200 and r.get_json() == {"stored": 1, "received": 1}
    assert pg.execute("SELECT days FROM codex_receipt_syncs").fetchone()[0] == 60


def test_the_endpoint_maps_refusals_and_bad_bodies(pg):
    assert _post({"nope": []}, {"X-Token": "tok"}).status_code == 400
    assert _post({"receipts": []}, {"X-Token": "tok"}).status_code == 400
    _post({"receipts": [_receipt(str(i)) for i in range(10)]}, {"X-Token": "tok"})
    assert _post({"receipts": [_receipt("1")]}, {"X-Token": "tok"}).status_code == 409
    assert _post({"receipts": [_receipt("1")]}, {"X-Token": "tok"},
                 query="?force=1").status_code == 200


# --- the pure rules -------------------------------------------------------------------------

def test_numbers_compare_on_digits_without_prefixes_or_leading_zeros():
    assert invoice_dedup.digits("AVIZO9572455748") == "9572455748"
    assert invoice_dedup.digits("0100237291") == "100237291"
    assert invoice_dedup.digits("FV 2026/00123") == "202600123"
    assert invoice_dedup.digits("1234") == "", "too short to identify a document"
    assert invoice_dedup.numbers_of("", None, ["526013012", "0526013012"]) == {"526013012"}


def test_dates_totals_and_tolerance():
    assert invoice_dedup.parse_day("09.09.2026") == TODAY.replace(year=2026, month=9, day=9)
    assert invoice_dedup.parse_day("2026-09-09").isoformat() == "2026-09-09"
    assert invoice_dedup.parse_day("31.02.2026") is None
    assert invoice_dedup.doc_total({"documentTotalWithoutVAT": 191.7}) == 191.7
    assert invoice_dedup.doc_total({"documentTotalWithoutVAT": 0,
                                    "items": [{"totalPrice": 1.5}, {"totalPrice": 2}]}) == 3.5
    assert invoice_dedup.doc_total({"items": [{"name": "x"}]}) is None
    assert invoice_dedup.tolerance(10.0) == 0.50
    assert invoice_dedup.tolerance(500.0) == 5.0


def test_credit_notes_by_word_and_by_sign():
    assert invoice_dedup.is_credit_note_text("Zaslanie dobropisu")
    assert invoice_dedup.is_credit_note_text("", "DOBROPIS č. 25260025")
    assert not invoice_dedup.is_credit_note_text("Faktúra č. 10261641", "")
    assert invoice_dedup.is_credit_note_doc({"documentTotalWithoutVAT": -225.0})
    assert not invoice_dedup.is_credit_note_doc({"documentTotalWithoutVAT": 225.0})


def test_credit_notes_are_split_off_by_subject_body_name_or_header_only():
    inv = {"filename": "Faktura_123.pdf", "machine_text": "Faktúra 123\n" + "x" * 400
           + "\nReklamácie riešime dobropisom."}
    cn = {"filename": "1326100113.pdf", "machine_text": "Faktúra - dobropis 1/1"}
    named = {"filename": "Dobropis_77.pdf", "machine_text": "Opravný doklad"}
    msg = {"subject": "Doklady", "combined_text": "Subject: Doklady\n\nBody: v prílohe"}
    assert dl_invoice.split_credit_notes(msg, [inv, cn, named]) == ([inv], None)
    assert dl_invoice.split_credit_notes(msg, [cn]) == ([], dl_invoice.CREDIT_REASON)
    # only the mail's words say it (round 3): not extracted, but the reason the caller POSTS
    whole = {"subject": "Dobropis č. 25260025", "combined_text": ""}
    assert dl_invoice.split_credit_notes(whole, [inv]) == ([], dl_invoice.MAIL_CREDIT_REASON)
    body = {"subject": "Doklad", "combined_text": "Body: Zasielame dobropis\n\nAttachments:\n"}
    assert dl_invoice.split_credit_notes(body, [inv])[1] == dl_invoice.MAIL_CREDIT_REASON
    assert dl_invoice.split_credit_notes(whole, [cn]) == ([], dl_invoice.CREDIT_REASON), \
        "the attachment's own header says so — a silent skip"
    in_attachment_text_only = {"subject": "Faktúra", "combined_text":
                               "Body: dobrý deň\n\nAttachments:\n===== f.pdf =====\n"
                               "... riešime dobropisom ..."}
    assert dl_invoice.split_credit_notes(in_attachment_text_only, [inv]) == ([inv], None)


def test_an_invoice_is_read_from_its_text_documents_and_a_scan_only_mail_keeps_its_scans():
    pdf = {"filename": "f.pdf", "machine_text": "Faktúra 123", "needs_vision": False}
    banner = {"filename": "b.jpg", "machine_text": "[needs AI Vision: b.jpg]",
              "needs_vision": True}
    blank = {"filename": "c.jpg", "machine_text": "", "needs_vision": False}
    assert dl_invoice.invoice_sources([banner, pdf, blank]) == [pdf]
    assert dl_invoice.invoice_sources([banner, blank]) == [banner, blank]


def test_the_content_signature_reads_card_and_quantity_back_from_the_edi():
    built = desadv_edi.build(
        {"customerName": "X", "customerEanEdi": EAN},
        {"docNumber": "4400123456", "deliveryDate": "05.10.2026"},
        [{"gtin": "8588000000002", "name": "B", "quantity": 7, "unit": "ks",
          "unitPrice": 1.0, "totalPrice": 7.0, "mass": 0.1},
         {"gtin": "8588000000001", "name": "A", "quantity": 100, "unit": "ks",
          "unitPrice": 0.5, "totalPrice": 50.0, "mass": 0.05}],
        [{"gtin": "8588000000001", "name": "A", "sklad": "1", "cena": "0.5"},
         {"gtin": "8588000000002", "name": "B", "sklad": "1", "cena": "1"}])
    assert desadv_edi.lin_quantities(built.content) == [
        ("8588000000002", "7.000"), ("8588000000001", "100.000")]
    assert invoice_dedup.signature(built.content) == [
        ["8588000000001", 100.0], ["8588000000002", 7.0]]
    assert invoice_dedup.signature("") == []


# --- find_duplicate against the receipts and our own ledger -------------------------------

def _doc(doc_number="4400123456", invoice_number="2400765432", day=None, total=100.0):
    return {"docNumber": doc_number, "invoiceNumber": invoice_number,
            "deliveryDate": (day or TODAY).strftime("%d.%m.%Y"),
            "documentTotalWithoutVAT": total, "items": []}


def _dup(pg, doc, message_id="m1", content=None, invoice_only=False, receipts=True):
    return invoice_dedup.find_duplicate(
        pg, codex_receipts.live(pg) if receipts else None, EAN, doc, message_id,
        doc_number=doc.get("docNumber") or "AVIZO1", content=content,
        invoice_only=invoice_only)


def test_a_receipt_matches_by_any_number_field(pg):
    for fields in ({"dl_numbers": ["2400765432"]}, {"invoice_number": "2400765432"},
                   {"invoice_vs": "2400765432"}, {"dl_numbers": ["1", "4400123456"]}):
        _push(pg, [_receipt(day=TODAY - timedelta(days=20), total=1.0, **fields)], force=True)
        dup = _dup(pg, _doc())
        assert dup and dup.source == "codex" and dup.match == "number", fields


def test_a_receipt_matches_by_date_within_a_day_and_total_within_tolerance(pg):
    _push(pg, [_receipt(day=TODAY - timedelta(days=1), total=100.6)])
    dup = _dup(pg, _doc())
    assert dup and dup.match == "date_total" and dup.ref == "261004409"
    assert "Už prijaté v CODEXe" in dup.reason()
    assert dup.conflict, "a neighbouring day's receipt proves nothing — a human looks"
    _push(pg, [_receipt(day=TODAY, total=100.6)], force=True)
    assert not _dup(pg, _doc()).conflict, "the same day + the same total: the incident"
    _push(pg, [_receipt(day=TODAY - timedelta(days=2), total=100.0)], force=True)
    assert _dup(pg, _doc()) is None, "two days off is another delivery"
    _push(pg, [_receipt(day=TODAY, total=101.01)], force=True)
    assert _dup(pg, _doc()) is None, "over max(0.50 €, 1 %)"


def test_a_multi_day_receipt_and_an_invoice_split_over_two_receipts_still_match(pg):
    """EKVIA shape: the stock lines and the transport line sit in two receipts of the SAME
    invoice — neither total alone matches, the invoice's total (or their sum) does. (A
    document printing no invoice number of its own: one that does would match the link by
    number, and one printing ANOTHER number is another delivery — round 2.)"""
    doc = _doc(invoice_number="")
    _push(pg, [_receipt("261004462", total=95.4, invoice_number="777777777"),
               _receipt("261101190", total=4.6, invoice_number="777777777")])
    assert _dup(pg, doc).match == "date_total"
    assert _dup(pg, _doc(invoice_number="777777777")).match == "number"
    assert _dup(pg, _doc(invoice_number="888888888")) is None, \
        "receipts linked to another invoice are another delivery"
    _push(pg, [_receipt("261004463", total=50.0, invoice_total=100.0)], force=True)
    assert _dup(pg, doc).match == "date_total"
    _push(pg, [_receipt(day=TODAY - timedelta(days=3), receipt_date_to=(
        TODAY - timedelta(days=1)).isoformat())], force=True)
    assert _dup(pg, doc).match == "date_total"


def test_another_suppliers_receipt_never_matches(pg):
    _push(pg, [_receipt(ean="2000000000555", dl_numbers=["2400765432"])])
    assert _dup(pg, _doc()) is None


def _sent(pg, doc_number, message_id, delivery=None, total=None, invoice=None, items=None):
    from psycopg.types.json import Json
    pg.execute("INSERT INTO desadv_sent (supplier_ean, doc_number, filename, message_id, "
               "uploaded_at, delivery_date, total_amount, invoice_number, items) "
               "VALUES (%s, %s, 'f.txt', %s, now(), %s, %s, %s, %s)",
               (EAN, doc_number, message_id, delivery, total, invoice,
                Json(items) if items else None))


def test_our_own_earlier_shipment_matches_by_number_date_total_or_content(pg):
    _push(pg, [_receipt(ean="2000000000555")])
    _sent(pg, "9990001111", "inv-a", invoice="2400765432")
    dup = _dup(pg, _doc(doc_number=""))
    assert dup.source == "desadv" and dup.match == "number"
    assert "Už odoslané do ORIONu" in dup.reason()
    # round 4: only the invoice number shared while both print different DL numbers — a
    # collective invoice's other delivery note, judged by the date rules (none here)
    assert _dup(pg, _doc()) is None
    assert _dup(pg, _doc(doc_number="9990001111")).match == "number"
    pg.execute("DELETE FROM desadv_sent")
    _sent(pg, "9990001111", "dl-scan", delivery=TODAY, total=100.3)
    assert _dup(pg, _doc()).match == "date_total"
    pg.execute("DELETE FROM desadv_sent")
    _sent(pg, "9990001111", "dl-scan", delivery=TODAY, items=[["8588000000001", 100.0]])
    assert _dup(pg, _doc()) is None, "a priceless row needs the content"
    dup = _dup(pg, _doc(), content=[["8588000000001", 100.0]])
    assert dup.match == "date_content" and "rovnaké položky" in dup.reason()
    assert not dup.conflict
    # round 3: the sums cannot be compared (a priceless row) and the content differs (other
    # units) — maybe the same delivery: never shipped, never silent
    dup = _dup(pg, _doc(), content=[["8588000000001", 90.0]])
    assert dup.match == "date" and dup.conflict and "nie je však isté" in dup.reason()
    pg.execute("DELETE FROM desadv_sent")
    _sent(pg, "9990001111", "dl-scan", delivery=TODAY, total=60.0,
          items=[["8588000000001", 90.0]])
    assert _dup(pg, _doc(), content=[["8588000000001", 100.0]]) is None, \
        "two known different sums and other goods are another delivery"


def test_a_stale_orphan_claim_is_a_conflict_not_a_shipment(pg):
    """A claim never confirmed and older than the claim's stale window: a crash between the
    claim and the upload (not in ORION) or between the upload and the confirmation (in ORION)
    — not provable either way (round 4): never a silent skip, never a blind second DESADV. A
    FRESH unconfirmed claim (mid-upload) is a plain twin."""
    _push(pg, [_receipt(ean="2000000000555")])
    pg.execute("INSERT INTO desadv_sent (supplier_ean, doc_number, filename, message_id, "
               "sent_at, delivery_date, total_amount) VALUES (%s, '9990001111', 'f.txt', "
               "'dl-x', now() - interval '1 hour', %s, 100.0)", (EAN, TODAY))
    dup = _dup(pg, _doc())
    assert dup.match == "date_total" and "nebolo potvrdené" in dup.conflict
    pg.execute("UPDATE desadv_sent SET sent_at = now()")
    dup = _dup(pg, _doc())
    assert dup.match == "date_total" and not dup.conflict


def test_only_this_very_documents_own_row_is_left_to_the_claim(pg):
    """A retry of THIS document (or its stale orphan claim) is the claim's business; another
    document of the SAME mail is a twin like any other (invoice + DL PDF in one mail)."""
    _push(pg, [_receipt(ean="2000000000555")])
    _sent(pg, "4400123456", "m1", delivery=TODAY, total=100.0, invoice="2400765432")
    assert _dup(pg, _doc(), message_id="m1") is None
    assert _dup(pg, _doc(), message_id="m2").source == "desadv"
    other_doc_same_mail = _doc(doc_number="7700112233", invoice_number="")
    assert _dup(pg, other_doc_same_mail, message_id="m1").match == "date_total"


def test_the_dl_path_check_sees_only_invoice_derived_rows(pg):
    """`invoice_only` = rows shipped from an INVOICE mail (its category) — not "a row with an
    invoice number": an invoice may print none, and a DL's row may carry one."""
    for mid, category in (("dl-a", "dodacie_listy"), ("inv-a", "invoices")):
        pg.execute("INSERT INTO messages (message_id, category, subject, from_addr) "
                   "VALUES (%s, %s, 's', 'x@y.sk')", (mid, category))
    _sent(pg, "9990001111", "dl-a", delivery=TODAY, total=100.0, invoice="2400765432")
    assert _dup(pg, _doc(), invoice_only=True, receipts=False) is None
    _sent(pg, "9990002222", "inv-a", delivery=TODAY, total=100.0)
    assert _dup(pg, _doc(doc_number="5550001111", invoice_number=""),
                invoice_only=True, receipts=False).ref == "9990002222"


# --- record_facts, the stale hold, the flag writer ---------------------------------------

def test_record_facts_writes_only_the_claimants_row(pg):
    pg.execute("INSERT INTO desadv_sent (supplier_ean, doc_number, filename, message_id) "
               "VALUES (%s, '4400123456', 'f.txt', 'm1')", (EAN,))
    assert desadv.record_facts(pg, EAN, "4400123456", message_id="m2",
                               delivery_date=TODAY, total_amount=1.0) is False
    assert desadv.record_facts(pg, EAN, "4400123456", message_id="m1",
                               delivery_date=TODAY, total_amount=100.0,
                               invoice_number="2400765432",
                               items=[["8588000000001", 100.0]]) is True
    row = pg.execute("SELECT delivery_date, total_amount, invoice_number, items "
                     "FROM desadv_sent").fetchone()
    assert row == (TODAY, 100.0, "2400765432", [["8588000000001", 100.0]])
    assert desadv.record_facts(pg, "", "x") is False


def test_a_document_reaching_the_gate_with_stale_receipts_is_held_for_a_human(pg):
    _push(pg, as_of=NOW - timedelta(hours=codex_receipts.STALE_HOURS + 1))
    posts = []
    out = dl_invoice.gate(pg, _cfg(), {"message_id": "m1", "subject": "Faktúra",
                                       "from_addr": "x@y.sk"}, _doc(), EAN,
                          post=lambda cfg, html: posts.append(html))
    assert out["outcome"] == "review" and out["held"] is True
    assert len(posts) == 1 and "nie sú aktuálne" in posts[0]


def test_the_flag_writer_stamps_since_once_and_clears_it(pg):
    rid = pg.execute("INSERT INTO dl_supplier_overrides (ean_edi, name, emails, city) "
                     "VALUES (%s, 'D', '{}', 'M') RETURNING id", (EAN,)).fetchone()[0]

    def state():
        return pg.execute("SELECT invoice_is_delivery_note, invoice_dl_since FROM "
                          "dl_supplier_overrides WHERE id = %s", (rid,)).fetchone()

    dl_snapshot.set_invoice_flag(pg, rid, True)
    on, since = state()
    assert on is True and since is not None
    pg.execute("UPDATE dl_supplier_overrides SET invoice_dl_since = invoice_dl_since "
               "- interval '1 day' WHERE id = %s", (rid,))
    kept = state()[1]
    dl_snapshot.set_invoice_flag(pg, rid, True)
    assert state()[1] == kept, "a save that keeps the flag on keeps its start"
    dl_snapshot.set_invoice_flag(pg, rid, False)
    assert state() == (False, None)


def test_aggregate_status_of_the_invoice_skips():
    agg = dl_message._aggregate_status
    assert agg([{"outcome": "credit_note"}]) == "credit_note"
    assert agg([{"outcome": "credit_note"}, {"outcome": "duplicate"}]) == "duplicate"
    assert agg([{"outcome": "credit_note"}, {"outcome": "review"}]) == "review"
    assert agg([{"outcome": "ok"}, {"outcome": "credit_note"}]) == "ok"
    assert agg([{"outcome": "not_warehouse"}, {"outcome": "credit_note"}]) == "not_warehouse"


def test_the_accounting_mailbox_is_ignored_by_default():
    assert dl_message.ignored_invoice_senders(None) == {"ucto@slovnormal.sk"}
    assert dl_message.ignored_invoice_senders(_cfg()) == {"ucto@slovnormal.sk"}
    cfg = _cfg(delivery_notes_invoice_ignored_senders=" A@x.sk, b@y.sk ")
    assert dl_message.ignored_invoice_senders(cfg) == {"a@x.sk", "b@y.sk"}
