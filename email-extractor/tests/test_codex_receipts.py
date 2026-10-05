"""#485: the CODEX supplier-receipts copy + the invoice-as-DL duplicate gate's rules.

`codex_receipts` (the pushed copy: atomic replace, freshness, fail-closed, the ops alert),
`invoice_dedup` (the pure rules + the match against receipts and our own `desadv_sent`),
`desadv.record_facts` (the facts our own shipments carry) and the endpoint's guards.
The end-to-end pipeline cases live in `test_invoice_dedup_regression.py`.

Synthetic data only (made-up suppliers, numbers) — this repo is public.
"""
import logging
import os
from datetime import UTC, date, datetime, timedelta

import pytest

from app.config import Config
from app.httpapi import create_app
from app.orders import codex_receipts, desadv, dl_message, invoice_dedup

EAN = "2000000000991"
NOW = datetime.now(UTC)
TODAY = NOW.date()


def _cfg(**kw):
    base = dict(pg_dsn=os.environ.get("PG_TEST_DSN", ""), data_dir="/tmp", api_token="tok",
                dash_password="pw", secret_key="t", ops_channel_id=77)
    base.update(kw)
    return Config(**base)


def _receipt(number="261004409", ean=EAN, day=None, **kw):
    r = {"receipt_number": number, "supplier_ico": "12345678", "supplier_ean": ean,
         "receipt_date": (day or TODAY).isoformat(), "dl_number": "", "total": 100.0}
    r.update(kw)
    return r


def _push(pg, receipts=None, as_of=None, force=False):
    return codex_receipts.replace_receipts(
        pg, receipts if receipts is not None else [_receipt()],
        source_as_of=(as_of or NOW).isoformat(), days=60, force=force)


def _flag(pg):
    pg.execute("INSERT INTO dl_supplier_overrides (ean_edi, name, emails, city, "
               "invoice_is_delivery_note) VALUES (%s, 'Dodávateľ', '{}', 'Mesto', true)",
               (EAN,))


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


# --- freshness: fail CLOSED -----------------------------------------------------------------

def test_never_pushed_or_stale_receipts_are_not_live(pg):
    assert codex_receipts.live(pg) is None
    _push(pg, as_of=NOW - timedelta(hours=codex_receipts.STALE_HOURS + 1))
    assert codex_receipts.live(pg) is None
    _push(pg, as_of=NOW - timedelta(hours=codex_receipts.STALE_HOURS - 1))
    assert codex_receipts.live(pg) is not None


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


def _alerts(pg):
    return pg.execute("SELECT channel_id, body_html FROM pending_alerts WHERE kind = %s",
                      (codex_receipts.ALERT_KIND,)).fetchall()


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
    assert invoice_dedup.numbers_of("", None, "526013012", "0526013012") == {"526013012"}


def test_dates_totals_and_tolerance():
    assert invoice_dedup.parse_day("09.09.2026") == date(2026, 9, 9)
    assert invoice_dedup.parse_day("2026-09-09") == date(2026, 9, 9)
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


def test_a_credit_note_is_recognised_from_an_attachment_name_alone():
    msg = {"subject": "Doklad", "combined_text": ""}
    assert dl_message.dl_invoice.credit_note_reason(
        msg, [{"filename": "Dobropis_123.pdf", "machine_text": ""}])
    assert dl_message.dl_invoice.credit_note_reason(
        msg, [{"filename": "Faktura_123.pdf", "machine_text": "Faktúra"}]) is None


# --- find_duplicate against the receipts and our own ledger -------------------------------

def _doc(doc_number="4400123456", invoice_number="2400765432", day=None, total=100.0):
    return {"docNumber": doc_number, "invoiceNumber": invoice_number,
            "deliveryDate": (day or TODAY).strftime("%d.%m.%Y"),
            "documentTotalWithoutVAT": total, "items": []}


def _dup(pg, doc, message_id="m1"):
    return invoice_dedup.find_duplicate(pg, codex_receipts.live(pg), EAN, doc, message_id)


def test_a_receipt_matches_by_any_number_field(pg):
    for field in ("dl_number", "invoice_number", "invoice_vs"):
        _push(pg, [_receipt(day=TODAY - timedelta(days=20), total=1.0,
                            **{field: "2400765432"})], force=True)
        dup = _dup(pg, _doc())
        assert dup and dup.source == "codex" and dup.match == "number", field
    _push(pg, [_receipt(day=TODAY - timedelta(days=20), total=1.0, dl_number="4400123456")],
          force=True)
    assert _dup(pg, _doc()).match == "number", "the DL number printed on the invoice too"


def test_a_receipt_matches_by_date_within_a_day_and_total_within_tolerance(pg):
    _push(pg, [_receipt(day=TODAY - timedelta(days=1), total=100.6)])
    dup = _dup(pg, _doc())
    assert dup and dup.match == "date_total" and dup.ref == "261004409"
    assert "Už prijaté v CODEXe" in dup.reason()
    _push(pg, [_receipt(day=TODAY - timedelta(days=2), total=100.0)], force=True)
    assert _dup(pg, _doc()) is None, "two days off is another delivery"
    _push(pg, [_receipt(day=TODAY, total=101.01)], force=True)
    assert _dup(pg, _doc()) is None, "over max(0.50 €, 1 %)"


def test_a_multi_day_receipt_and_an_invoice_split_over_two_receipts_still_match(pg):
    """EKVIA shape: the stock lines and the transport line sit in two receipts of the SAME
    invoice — neither total alone matches, the invoice's total (or their sum) does."""
    _push(pg, [_receipt("261004462", total=95.4, invoice_number="777777777"),
               _receipt("261101190", total=4.6, invoice_number="777777777")])
    assert _dup(pg, _doc()).match == "date_total"
    _push(pg, [_receipt("261004463", total=50.0, invoice_total=100.0)], force=True)
    assert _dup(pg, _doc()).match == "date_total"
    _push(pg, [_receipt(day=TODAY - timedelta(days=3), receipt_date_to=(
        TODAY - timedelta(days=1)).isoformat())], force=True)
    assert _dup(pg, _doc()).match == "date_total"


def test_another_suppliers_receipt_never_matches(pg):
    _push(pg, [_receipt(ean="2000000000555", dl_number="2400765432")])
    assert _dup(pg, _doc()) is None


def _sent(pg, doc_number, message_id, delivery=None, total=None, invoice=None):
    pg.execute("INSERT INTO desadv_sent (supplier_ean, doc_number, filename, message_id, "
               "uploaded_at, delivery_date, total_amount, invoice_number) "
               "VALUES (%s, %s, 'f.txt', %s, now(), %s, %s, %s)",
               (EAN, doc_number, message_id, delivery, total, invoice))


def test_our_own_earlier_shipment_matches_by_number_or_by_date_and_total(pg):
    _push(pg, [_receipt(ean="2000000000555")])
    _sent(pg, "9990001111", "dl-scan", invoice="2400765432")
    dup = _dup(pg, _doc())
    assert dup.source == "desadv" and dup.match == "number"
    assert "Už odoslané do ORIONu" in dup.reason()
    pg.execute("DELETE FROM desadv_sent")
    _sent(pg, "9990001111", "dl-scan", delivery=TODAY, total=100.3)
    assert _dup(pg, _doc()).match == "date_total"


def test_this_messages_own_earlier_claim_is_left_to_the_claim(pg):
    """A retry of THIS message after a partial ship (or a stale orphan claim it must be able
    to reclaim) is the claim's business (`already_shipped_this_run`), never a dedup skip."""
    _push(pg, [_receipt(ean="2000000000555")])
    _sent(pg, "4400123456", "m1", delivery=TODAY, total=100.0, invoice="2400765432")
    assert _dup(pg, _doc(), message_id="m1") is None
    assert _dup(pg, _doc(), message_id="m2").source == "desadv"


def test_an_old_ledger_row_without_facts_matches_by_number_only(pg):
    _push(pg, [_receipt(ean="2000000000555")])
    _sent(pg, "9990001111", "dl-old")
    assert _dup(pg, _doc()) is None
    assert _dup(pg, _doc(doc_number="9990001111")).match == "number"


# --- record_facts --------------------------------------------------------------------------

def test_record_facts_writes_only_the_claimants_row(pg):
    pg.execute("INSERT INTO desadv_sent (supplier_ean, doc_number, filename, message_id) "
               "VALUES (%s, '4400123456', 'f.txt', 'm1')", (EAN,))
    assert desadv.record_facts(pg, EAN, "4400123456", message_id="m2",
                               delivery_date=TODAY, total_amount=1.0) is False
    assert desadv.record_facts(pg, EAN, "4400123456", message_id="m1",
                               delivery_date=TODAY, total_amount=100.0,
                               invoice_number="2400765432") is True
    row = pg.execute("SELECT delivery_date, total_amount, invoice_number FROM desadv_sent"
                     ).fetchone()
    assert row == (TODAY, 100.0, "2400765432")
    assert desadv.record_facts(pg, "", "x") is False


def test_aggregate_status_of_the_invoice_skips():
    agg = dl_message._aggregate_status
    assert agg([{"outcome": "credit_note"}]) == "credit_note"
    assert agg([{"outcome": "superseded"}]) == "superseded"
    assert agg([{"outcome": "credit_note"}, {"outcome": "duplicate"}]) == "duplicate"
    assert agg([{"outcome": "credit_note"}, {"outcome": "review"}]) == "review"
    assert agg([{"outcome": "ok"}, {"outcome": "superseded"}]) == "ok"
    assert agg([{"outcome": "not_warehouse"}, {"outcome": "credit_note"}]) == "not_warehouse"


def test_the_accounting_mailbox_is_ignored_by_default():
    assert dl_message.ignored_invoice_senders(None) == {"ucto@slovnormal.sk"}
    assert dl_message.ignored_invoice_senders(_cfg()) == {"ucto@slovnormal.sk"}
    cfg = _cfg(delivery_notes_invoice_ignored_senders=" A@x.sk, b@y.sk ")
    assert dl_message.ignored_invoice_senders(cfg) == {"a@x.sk", "b@y.sk"}
