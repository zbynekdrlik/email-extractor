"""#494 — a delivery note the warehouse typed into CODEX BY HAND while our DESADV still waits
in ORION `in_DL` gets the terminal import status `manual`.

Live incident (13.10.): CODEX would not take four DESADVs of one supplier, the warehouse typed
the four DLs in by hand, and our files stayed in `in_DL`. `confirm.sweep` knew only archCodex =
imported / unconfirmed = failed / otherwise waiting, so the morning carryover reminder kept
telling the warehouse „treba ich prijať v CODEXe" — following it books every delivery TWICE.
Deleting the files was no way out: a file gone from every folder is the `unknown` alarm.

Pinned here:
- a still-queued DESADV whose supplier has a CODEX receipt (`codex_receipts`, #485) with the
  same DL number (digits only, leading zeros / prefixes dropped) → `manual`, out of every
  carryover alert, exactly ONE ops alert (never the warehouse channel), never repeated;
- no receipt / another supplier's receipt / a too-short number → unchanged (still waiting);
- a `manual` file that later vanishes from every folder is never the `unknown` alarm;
- a carryover incident whose members all end imported or manual closes with its all-clear;
- a `manual` mark is no evidence of an import pass (the #255 same-day check);
- a failing receipts lookup leaves the row waiting (fail-open, logged).

All fixtures SYNTHETIC (made-up EANs, numbers, names) — public repo.
"""
from __future__ import annotations

import pytest
from test_orders_confirm import (
    MON_EVENING,
    TUE_MORNING,
    TZ,
    PostRecorder,
    _cfg,
    _desadv_status,
    _insert,
    _insert_desadv_at,
)
from test_orders_confirm_carryover import SUP_A, SUP_A_NAME, SUP_B, _suppliers

from app.orders import codex_receipts, confirm, dl_alerts

OPS = 592
KIND = "dl_manual_in_codex"
DOC = "0100304144"                 # the DL number as the supplier prints it (leading zero)
CODEX_DL = "100304144"             # CODEX stores numbers as DOUBLE: no leading zero
RECEIPT = "261000071"


def _fname(doc: str = DOC, sup: str = SUP_A) -> str:
    return f"DESADV_{sup[-6:]}_{doc}_20261007_122216773.txt"


def _wire(doc: str = DOC, sup: str = SUP_A) -> str:
    return f"Z-{_fname(doc, sup)}"


def _dirs(in_dl=(), arch=(), in_=()):
    return {"in": set(in_), "in_DL": set(in_dl), "archCodex": set(arch),
            "unconfirmed": set()}


def _ocfg(**kw):
    return _cfg(ops_channel_id=OPS, dashboard_base_url="https://dash.example", **kw)


def _receipts(pg, *rows):
    """Push a CODEX receipts copy: each row = (receipt number, supplier EAN, DL numbers)."""
    codex_receipts.replace_receipts(
        pg, [{"receipt_number": number, "supplier_ico": f"ico-{ean}", "supplier_eans": [ean],
              "supplier_name": "Dodávateľ", "receipt_date": "2026-08-03", "dl_numbers": dls,
              "total": 10.0} for number, ean, dls in rows],
        source_as_of="2026-08-03T12:00:00+02:00", days=60)


def _alerts(pg):
    return pg.execute(
        "SELECT channel_id, kind, message_id, body_html FROM pending_alerts ORDER BY id"
    ).fetchall()


def _manual_alerts(pg):
    return [a for a in _alerts(pg) if a[1] == KIND]


def _make_due(pg):
    pg.execute("UPDATE desadv_sent SET import_checked_at = now() - interval '10 minutes'")
    pg.execute("UPDATE edi_sent SET import_checked_at = now() - interval '10 minutes'")


def _row(pg, rid):
    return pg.execute(
        "SELECT import_status, import_confirmed_at FROM desadv_sent WHERE id = %s",
        (rid,)).fetchone()


# --- the incident: a receipt with our DL number → manual + one ops alert --------------------

def test_queued_desadv_with_a_codex_receipt_becomes_manual_with_one_ops_alert(pg):
    """The live incident: the file waits in in_DL, CODEX already has the receipt → `manual`,
    no carryover alert to the warehouse, exactly one ops alert naming the file to delete."""
    _suppliers(pg)
    rid = _insert_desadv_at(pg, SUP_A, _fname(), uploaded_at=MON_EVENING, doc_number=DOC)
    _receipts(pg, (RECEIPT, SUP_A, [CODEX_DL]))
    posts = PostRecorder()

    n = confirm.sweep(pg, _ocfg(), listdir=lambda: _dirs(in_dl=[_wire()]), post=posts,
                      now=TUE_MORNING)

    status, confirmed_at = _row(pg, rid)
    assert status == "manual"
    assert confirmed_at == TUE_MORNING
    assert n == 1, "manual is terminal — counted like any other resolved row"
    assert posts.calls == [], "nothing for the warehouse: the DL is already in CODEX"
    alerts = _manual_alerts(pg)
    assert len(alerts) == 1
    channel, _kind, key, body = alerts[0]
    assert channel == OPS, "an ops alert, never the delivery-notes channel 243"
    assert key == f"desadv-manual:{rid}"
    assert DOC in body and RECEIPT in body and _wire() in body and SUP_A_NAME in body
    assert confirm._open_incident(pg, 243, "carryover", confirm.DESADV_LEDGER) is None


def test_a_second_sweep_never_repeats_the_ops_alert(pg):
    _insert_desadv_at(pg, SUP_A, _fname(), uploaded_at=MON_EVENING, doc_number=DOC)
    _receipts(pg, (RECEIPT, SUP_A, [CODEX_DL]))
    posts = PostRecorder()
    confirm.sweep(pg, _ocfg(), listdir=lambda: _dirs(in_dl=[_wire()]), post=posts,
                  now=TUE_MORNING)
    other = _insert(pg, "7", "ORDER_x.txt", uploaded_minutes_ago=90)   # forces a listing
    for hour in (12, 16, 20):
        _make_due(pg)
        confirm.sweep(pg, _ocfg(), listdir=lambda: _dirs(in_dl=[_wire()], in_=["ORDER_x.txt"]),
                      post=posts, now=TUE_MORNING.replace(hour=hour))
    assert len(_manual_alerts(pg)) == 1, "one alert per file, ever"
    assert all(ch == 152 for _html, ch in posts.calls), (
        "only the unrelated ORDER carryover may reach a warehouse channel", posts.calls)
    assert other


def test_the_ops_alert_is_delivered_grouped_to_the_ops_channel_with_the_dashboard_link(pg):
    _suppliers(pg)
    docs = [DOC, "0100309226"]
    for doc in docs:
        _insert_desadv_at(pg, SUP_A, _fname(doc), uploaded_at=MON_EVENING, doc_number=doc)
    _receipts(pg, (RECEIPT, SUP_A, [CODEX_DL]), ("261000072", SUP_A, ["100309226"]))
    confirm.sweep(pg, _ocfg(), listdir=lambda: _dirs(in_dl=[_wire(d) for d in docs]),
                  post=PostRecorder(), now=TUE_MORNING)
    assert len(_manual_alerts(pg)) == 2, "one outbox row per file"

    delivered = PostRecorder()
    assert dl_alerts.flush_pending(pg, _ocfg(), post=delivered) == 2
    assert len(delivered.calls) == 1, "the two files land as ONE grouped ops message"
    html, channel = delivered.calls[0]
    assert channel == OPS
    assert "in_DL" in html and "druhýkrát" in html, html
    for doc in docs:
        assert doc in html and _wire(doc) in html
    assert "https://dash.example" in html, "ops kinds link the admin dashboard (#473)"


# --- unchanged: no receipt, another supplier, a short number --------------------------------

def test_no_receipt_leaves_the_row_waiting_and_the_carryover_alert_as_today(pg):
    rid = _insert_desadv_at(pg, SUP_A, _fname(), uploaded_at=MON_EVENING, doc_number=DOC)
    _receipts(pg, (RECEIPT, SUP_A, ["100399999"]))
    posts = PostRecorder()
    confirm.sweep(pg, _ocfg(), listdir=lambda: _dirs(in_dl=[_wire()]), post=posts,
                  now=TUE_MORNING)
    assert _desadv_status(pg, rid) is None
    assert len(posts.calls) == 1 and posts.calls[0][1] == 243, "the carryover still goes out"
    assert _manual_alerts(pg) == []


def test_no_receipts_copy_at_all_leaves_the_row_waiting(pg):
    rid = _insert_desadv_at(pg, SUP_A, _fname(), uploaded_at=MON_EVENING, doc_number=DOC)
    posts = PostRecorder()
    confirm.sweep(pg, _ocfg(), listdir=lambda: _dirs(in_dl=[_wire()]), post=posts,
                  now=TUE_MORNING)
    assert _desadv_status(pg, rid) is None
    assert len(posts.calls) == 1
    assert _manual_alerts(pg) == []


def test_another_suppliers_receipt_with_the_same_number_changes_nothing(pg):
    rid = _insert_desadv_at(pg, SUP_A, _fname(), uploaded_at=MON_EVENING, doc_number=DOC)
    _receipts(pg, (RECEIPT, SUP_B, [CODEX_DL]))
    posts = PostRecorder()
    confirm.sweep(pg, _ocfg(), listdir=lambda: _dirs(in_dl=[_wire()]), post=posts,
                  now=TUE_MORNING)
    assert _desadv_status(pg, rid) is None
    assert len(posts.calls) == 1
    assert _manual_alerts(pg) == []


@pytest.mark.parametrize("doc,codex_dl", [
    ("0100304144", "100304144"),          # leading zero dropped by CODEX
    ("100304144", "0100304144"),          # …either side
    ("DL-0100304144", "100304144"),       # a non-digit prefix on our side
    ("0100304144", "LT 100304144"),       # …or on CODEX's
    ("AVIZO5336710511", "5336710511"),
])
def test_dl_numbers_are_compared_digits_only_without_leading_zeros(pg, doc, codex_dl):
    rid = _insert_desadv_at(pg, SUP_A, _fname("X1"), uploaded_at=MON_EVENING, doc_number=doc)
    _receipts(pg, (RECEIPT, SUP_A, ["999999999", codex_dl]))
    confirm.sweep(pg, _ocfg(), listdir=lambda: _dirs(in_dl=[_wire("X1")]),
                  post=PostRecorder(), now=TUE_MORNING)
    assert _desadv_status(pg, rid) == "manual"


def test_a_number_with_fewer_than_five_digits_never_matches(pg):
    """`invoice_dedup.digits`: a short counter (`1234`) must never meet another document."""
    rid = _insert_desadv_at(pg, SUP_A, _fname("X2"), uploaded_at=MON_EVENING,
                            doc_number="1234")
    _receipts(pg, (RECEIPT, SUP_A, ["1234"]))
    confirm.sweep(pg, _ocfg(), listdir=lambda: _dirs(in_dl=[_wire("X2")]),
                  post=PostRecorder(), now=TUE_MORNING)
    assert _desadv_status(pg, rid) is None
    assert _manual_alerts(pg) == []


def test_an_imported_file_stays_imported_even_with_a_receipt(pg):
    """CODEX's own import of our file is ALSO a receipt with our DL number — archCodex wins."""
    rid = _insert_desadv_at(pg, SUP_A, _fname(), uploaded_at=MON_EVENING, doc_number=DOC)
    _receipts(pg, (RECEIPT, SUP_A, [CODEX_DL]))
    confirm.sweep(pg, _ocfg(), listdir=lambda: _dirs(arch=[_wire()]), post=PostRecorder(),
                  now=TUE_MORNING)
    assert _desadv_status(pg, rid) == "imported"
    assert _manual_alerts(pg) == []


def test_a_failing_receipts_lookup_leaves_the_row_waiting(pg, monkeypatch, caplog):
    rid = _insert_desadv_at(pg, SUP_A, _fname(), uploaded_at=MON_EVENING, doc_number=DOC)
    _receipts(pg, (RECEIPT, SUP_A, [CODEX_DL]))

    def boom(*_a, **_kw):
        raise RuntimeError("receipts lookup down")

    monkeypatch.setattr(codex_receipts, "for_supplier", boom, raising=False)
    posts = PostRecorder()
    confirm.sweep(pg, _ocfg(), listdir=lambda: _dirs(in_dl=[_wire()]), post=posts,
                  now=TUE_MORNING)
    assert _desadv_status(pg, rid) is None, "fail-open: the row keeps waiting"
    assert len(posts.calls) == 1, "the carryover alert still goes out"
    assert _manual_alerts(pg) == []
    assert "receipts lookup down" in caplog.text


# --- after manual: the file vanishing is no alarm; the incident closes ----------------------

def test_a_manual_file_that_later_vanishes_raises_no_unknown_alarm(pg):
    """The ops alert asks a human to delete the file — that deletion must never come back as
    „zmizol zo všetkých priečinkov"."""
    rid = _insert_desadv_at(pg, SUP_A, _fname(), uploaded_at=MON_EVENING, doc_number=DOC)
    _receipts(pg, (RECEIPT, SUP_A, [CODEX_DL]))
    posts = PostRecorder()
    confirm.sweep(pg, _ocfg(), listdir=lambda: _dirs(in_dl=[_wire()]), post=posts,
                  now=TUE_MORNING)
    assert _desadv_status(pg, rid) == "manual"

    _insert(pg, "8", "ORDER_y.txt", uploaded_minutes_ago=90)        # forces a listing
    _make_due(pg)
    confirm.sweep(pg, _ocfg(), listdir=lambda: _dirs(arch=["ORDER_y.txt"]), post=posts,
                  now=TUE_MORNING.replace(hour=12))
    assert _desadv_status(pg, rid) == "manual"
    assert posts.calls == [], posts.calls
    assert confirm._open_incident(pg, 243, "unknown", confirm.DESADV_LEDGER) is None


def test_a_carryover_incident_of_imported_and_manual_members_closes(pg):
    _suppliers(pg)
    a = _insert_desadv_at(pg, SUP_A, _fname(), uploaded_at=MON_EVENING, doc_number=DOC)
    b = _insert_desadv_at(pg, SUP_A, _fname("0100309226"), uploaded_at=MON_EVENING,
                          doc_number="0100309226")
    both = [_wire(), _wire("0100309226")]
    posts = PostRecorder()
    confirm.sweep(pg, _ocfg(), listdir=lambda: _dirs(in_dl=both), post=posts,
                  now=TUE_MORNING)
    assert len(posts.calls) == 1, "the carryover incident opens as today (no receipt yet)"
    assert confirm._open_incident(pg, 243, "carryover", confirm.DESADV_LEDGER) is not None

    # later the receipt for A arrives (typed by hand) and B is imported normally
    _receipts(pg, (RECEIPT, SUP_A, [CODEX_DL]))
    _make_due(pg)
    confirm.sweep(pg, _ocfg(), listdir=lambda: _dirs(in_dl=[both[0]], arch=[both[1]]),
                  post=posts, now=TUE_MORNING.replace(hour=16))
    assert _desadv_status(pg, a) == "manual"
    assert _desadv_status(pg, b) == "imported"
    assert len(posts.calls) == 2, posts.calls
    assert "boli prijaté v CODEXe" in posts.calls[1][0], "the all-clear, not a reminder"
    assert confirm._open_incident(pg, 243, "carryover", confirm.DESADV_LEDGER) is None
    assert len(_manual_alerts(pg)) == 1


def test_a_reminder_never_lists_a_manual_file_whose_own_recheck_is_not_due(pg):
    """Each row has its own 5-minute re-check, but the reminder names every waiting file of
    the listing (#476) — a file already typed into CODEX must be resolved before ANY alert of
    that sweep is built, not only when its own re-check comes round."""
    _suppliers(pg)
    a = _insert_desadv_at(pg, SUP_A, _fname(), uploaded_at=MON_EVENING, doc_number=DOC)
    _insert_desadv_at(pg, SUP_A, _fname("0100309226"), uploaded_at=MON_EVENING,
                      doc_number="0100309226")
    both = [_wire(), _wire("0100309226")]
    posts = PostRecorder()
    confirm.sweep(pg, _ocfg(), listdir=lambda: _dirs(in_dl=both), post=posts,
                  now=TUE_MORNING)
    assert len(posts.calls) == 1

    _receipts(pg, (RECEIPT, SUP_A, [CODEX_DL]))
    _make_due(pg)
    pg.execute("UPDATE desadv_sent SET import_checked_at = now() WHERE id = %s", (a,))
    confirm.sweep(pg, _ocfg(), listdir=lambda: _dirs(in_dl=both), post=posts,
                  now=TUE_MORNING.replace(hour=16))
    assert _desadv_status(pg, a) == "manual"
    assert len(posts.calls) == 2, "the 4h reminder about the one file really waiting"
    html = posts.calls[1][0]
    assert "Stále 1 dodací list" in html, html
    assert "0100309226" in html and DOC not in html


# --- a manual mark is not an import pass (#255) ----------------------------------------------

def test_a_manual_mark_is_no_evidence_of_an_import_pass_for_the_same_day_check(pg):
    """`_activity_today_at` reads `import_confirmed_at` as „an import ran today" — a manual
    mark writes it too, but nobody clicked „prijať"; another file uploaded earlier today and
    still queued must stay silent in the evening."""
    morning = MON_EVENING.replace(hour=9)
    _insert_desadv_at(pg, SUP_A, _fname(), uploaded_at=morning, doc_number=DOC)
    waiting = _insert_desadv_at(pg, SUP_A, _fname("0100309226"),
                                uploaded_at=MON_EVENING.replace(hour=10),
                                doc_number="0100309226")
    _receipts(pg, (RECEIPT, SUP_A, [CODEX_DL]))
    posts = PostRecorder()
    evening = MON_EVENING.replace(hour=18, minute=30)
    confirm.sweep(pg, _ocfg(),
                  listdir=lambda: _dirs(in_dl=[_wire(), _wire("0100309226")]),
                  post=posts, now=evening)
    assert _desadv_status(pg, waiting) is None
    assert confirm._activity_today_at(pg, evening) is None
    assert posts.calls == [], "no import ran today — the queued file is simply waiting"


def test_activity_today_counts_only_rows_really_imported(pg):
    evening = MON_EVENING.replace(hour=18, minute=30)
    rid = _insert_desadv_at(pg, SUP_A, _fname(), uploaded_at=MON_EVENING.replace(hour=9),
                            doc_number=DOC)
    pg.execute("UPDATE desadv_sent SET import_status = 'manual', import_confirmed_at = %s "
               "WHERE id = %s", (MON_EVENING.replace(hour=12), rid))
    assert confirm._activity_today_at(pg, evening) is None, "a manual mark is no import"
    pg.execute("UPDATE desadv_sent SET import_status = 'imported' WHERE id = %s", (rid,))
    assert confirm._activity_today_at(pg, evening) == MON_EVENING.replace(hour=12)


def test_an_edi_row_never_consults_the_receipts(pg):
    """CODEX receipts are SUPPLIER deliveries; an ORDER file to a customer is never `manual`."""
    rid = _insert(pg, SUP_A, "ORDER_z.txt", uploaded_minutes_ago=90)
    _receipts(pg, (RECEIPT, SUP_A, ["04082026"]))
    confirm.sweep(pg, _ocfg(), listdir=lambda: _dirs(in_=["ORDER_z.txt"]),
                  post=PostRecorder(), now=TUE_MORNING.replace(tzinfo=TZ))
    assert pg.execute("SELECT import_status FROM edi_sent WHERE id = %s",
                      (rid,)).fetchone()[0] is None
