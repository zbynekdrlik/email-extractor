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
- no receipt / another supplier's receipt / a too-short number / a receipt dated before the
  delivery (a reused number, an invoice number typed into the DL field) / a row without a
  delivery date → unchanged (still waiting);
- a `manual` file that later vanishes from every folder is never the `unknown` alarm;
- a carryover incident whose members all end imported or manual closes with its all-clear;
- a `manual` mark is no evidence of an import pass (the #255 same-day check);
- a failing receipts lookup leaves the row waiting (fail-open, logged); the status and its
  alert land in one transaction; the alert escapes what it quotes.

All fixtures SYNTHETIC (made-up EANs, DL / receipt numbers, names) — public repo.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest
from test_orders_confirm import (
    MON_EVENING,
    TUE_MORNING,
    WED_MORNING,
    PostRecorder,
    _cfg,
    _desadv_status,
    _insert,
)
from test_orders_confirm_carryover import SUP_A, SUP_A_NAME, SUP_B, _suppliers

from app.orders import codex_receipts, confirm, confirm_manual, dl_alerts

OPS = 592
KIND = "dl_manual_in_codex"
DOC = "0100900001"                 # the DL number as the supplier prints it (leading zero)
CODEX_DL = "100900001"             # CODEX stores numbers as DOUBLE: no leading zero
DOC2, CODEX_DL2 = "0100900002", "100900002"
RECEIPT, RECEIPT2 = "261900001", "261900002"
DELIVERY = date(2026, 8, 3)        # = MON_EVENING's day; receipts default to it too


def _fname(doc: str = DOC, sup: str = SUP_A) -> str:
    return f"DESADV_{sup[-6:]}_{doc}_20260803_120000000.txt"


def _wire(doc: str = DOC, sup: str = SUP_A) -> str:
    return f"Z-{_fname(doc, sup)}"


def _desadv(pg, doc: str = DOC, *, uploaded_at: datetime = MON_EVENING,
            delivery: date | None = DELIVERY, sup: str = SUP_A, fname: str | None = None):
    """A confirmed-uploaded DESADV row with the delivery FACT `record_facts` writes (#485)."""
    return pg.execute(
        """INSERT INTO desadv_sent (supplier_ean, doc_number, filename, uploaded_at,
                                    delivery_date)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (sup, doc, fname or _fname(doc, sup), uploaded_at, delivery)).fetchone()[0]


def _dirs(in_dl=(), arch=(), in_=()):
    return {"in": set(in_), "in_DL": set(in_dl), "archCodex": set(arch),
            "unconfirmed": set()}


def _ocfg(**kw):
    return _cfg(ops_channel_id=OPS, dashboard_base_url="https://dash.example", **kw)


def _receipts(pg, *rows):
    """Push a CODEX receipts copy: each row = (receipt number, supplier EAN, DL numbers[,
    receipt day — default the delivery day[, last day of a receipt booked over several]])."""
    codex_receipts.replace_receipts(
        pg, [{"receipt_number": r[0], "supplier_ico": f"ico-{r[1]}", "supplier_eans": [r[1]],
              "supplier_name": "Dodávateľ", "dl_numbers": r[2], "total": 10.0,
              "receipt_date": (r[3] if len(r) > 3 else DELIVERY).isoformat(),
              "receipt_date_to": r[4].isoformat() if len(r) > 4 else None}
             for r in rows],
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


def _sweep(pg, posts, now=TUE_MORNING, **dirs):
    return confirm.sweep(pg, _ocfg(), listdir=lambda: _dirs(**dirs), post=posts, now=now)


# --- the incident: a receipt with our DL number → manual + one ops alert --------------------

def test_queued_desadv_with_a_codex_receipt_becomes_manual_with_one_ops_alert(pg):
    """The live incident: the file waits in in_DL, CODEX already has the receipt → `manual`,
    no carryover alert to the warehouse, exactly one ops alert naming the file to delete."""
    _suppliers(pg)
    rid = _desadv(pg)
    _receipts(pg, (RECEIPT, SUP_A, [CODEX_DL]))
    posts = PostRecorder()

    n = _sweep(pg, posts, in_dl=[_wire()])

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
    _desadv(pg)
    _receipts(pg, (RECEIPT, SUP_A, [CODEX_DL]))
    posts = PostRecorder()
    _sweep(pg, posts, in_dl=[_wire()])
    _insert(pg, "7", "ORDER_x.txt", uploaded_minutes_ago=90)        # forces a listing
    for hour in (12, 16, 20):
        _make_due(pg)
        _sweep(pg, posts, now=TUE_MORNING.replace(hour=hour), in_dl=[_wire()],
               in_=["ORDER_x.txt"])
    assert len(_manual_alerts(pg)) == 1, "one alert per file, ever"
    assert all(ch == 152 for _html, ch in posts.calls), (
        "only the unrelated ORDER carryover may reach a warehouse channel", posts.calls)


def test_the_ops_alert_is_delivered_grouped_to_the_ops_channel_with_the_dashboard_link(pg):
    _suppliers(pg)
    for doc in (DOC, DOC2):
        _desadv(pg, doc)
    _receipts(pg, (RECEIPT, SUP_A, [CODEX_DL]), (RECEIPT2, SUP_A, [CODEX_DL2]))
    _sweep(pg, PostRecorder(), in_dl=[_wire(DOC), _wire(DOC2)])
    assert len(_manual_alerts(pg)) == 2, "one outbox row per file"

    delivered = PostRecorder()
    assert dl_alerts.flush_pending(pg, _ocfg(), post=delivered) == 2
    assert len(delivered.calls) == 1, "the two files land as ONE grouped ops message"
    html, channel = delivered.calls[0]
    assert channel == OPS
    assert html.count("in_DL") == 1 and "druhýkrát" in html, "one header, the instruction"
    assert "príjemku" in html, "check the receipt in CODEX before deleting"
    for doc, receipt in ((DOC, RECEIPT), (DOC2, RECEIPT2)):
        assert doc in html and receipt in html and _wire(doc) in html
    assert "https://dash.example" in html, "ops kinds link the admin dashboard (#473)"


def test_every_file_of_a_large_batch_is_named_in_the_ops_post(pg):
    """Each line of this alert is an instruction (delete THIS file) and the row is terminal,
    so a „… a ešte N ďalších" cut would lose the deletion of the files behind it for good."""
    docs = [f"01009001{i:02d}" for i in range(12)]
    for doc in docs:
        _desadv(pg, doc)
    _receipts(pg, *[(f"2619001{i:02d}", SUP_A, [doc.lstrip("0")])
                    for i, doc in enumerate(docs)])
    _sweep(pg, PostRecorder(), in_dl=[_wire(d) for d in docs])
    assert len(_manual_alerts(pg)) == 12

    delivered = PostRecorder()
    assert dl_alerts.flush_pending(pg, _ocfg(), post=delivered) == 12
    html = "".join(h for h, _ch in delivered.calls)
    for doc in docs:
        assert _wire(doc) in html, f"{doc} must be named — its file must be deleted"
    assert "ďalš" not in html, "never a capped remainder for this kind"


def test_the_alert_escapes_what_it_quotes(pg):
    """The DL number comes from model extraction of supplier mail, the name from our cards."""
    doc = "0100900003<b>&"
    rid = _desadv(pg, doc, fname="DESADV_x_0100900003.txt")
    _receipts(pg, (RECEIPT, SUP_A, ["100900003"]))
    _sweep(pg, PostRecorder(), in_dl=["Z-DESADV_x_0100900003.txt"])
    assert _desadv_status(pg, rid) == "manual"
    body = _manual_alerts(pg)[0][3]
    assert "0100900003&lt;b&gt;&amp;" in body and "<b>" not in body, body


# --- unchanged: no receipt, another supplier, a short number, an older receipt --------------

def test_no_receipt_leaves_the_row_waiting_and_the_carryover_alert_as_today(pg):
    rid = _desadv(pg)
    _receipts(pg, (RECEIPT, SUP_A, ["100999999"]))
    posts = PostRecorder()
    _sweep(pg, posts, in_dl=[_wire()])
    assert _desadv_status(pg, rid) is None
    assert len(posts.calls) == 1 and posts.calls[0][1] == 243, "the carryover still goes out"
    assert _manual_alerts(pg) == []


def test_no_receipts_copy_at_all_leaves_the_row_waiting(pg):
    rid = _desadv(pg)
    posts = PostRecorder()
    _sweep(pg, posts, in_dl=[_wire()])
    assert _desadv_status(pg, rid) is None
    assert len(posts.calls) == 1
    assert _manual_alerts(pg) == []


def test_another_suppliers_receipt_with_the_same_number_changes_nothing(pg):
    rid = _desadv(pg)
    _receipts(pg, (RECEIPT, SUP_B, [CODEX_DL]))
    posts = PostRecorder()
    _sweep(pg, posts, in_dl=[_wire()])
    assert _desadv_status(pg, rid) is None
    assert len(posts.calls) == 1
    assert _manual_alerts(pg) == []


@pytest.mark.parametrize("doc,codex_dl", [
    ("0100900001", "100900001"),          # leading zero dropped by CODEX
    ("100900001", "0100900001"),          # …either side
    ("DL-0100900001", "100900001"),       # a non-digit prefix on our side
    ("0100900001", "LT 100900001"),       # …or on CODEX's
    ("AVIZO9000000001", "9000000001"),
])
def test_dl_numbers_are_compared_digits_only_without_leading_zeros(pg, doc, codex_dl):
    rid = _desadv(pg, doc, fname=_fname("X1"))
    _receipts(pg, (RECEIPT, SUP_A, ["999999999", codex_dl]))
    _sweep(pg, PostRecorder(), in_dl=[_wire("X1")])
    assert _desadv_status(pg, rid) == "manual"


def test_a_number_with_fewer_than_five_digits_never_matches(pg):
    """`invoice_dedup.digits`: a short counter (`1234`) must never meet another document."""
    rid = _desadv(pg, "1234", fname=_fname("X2"))
    _receipts(pg, (RECEIPT, SUP_A, ["1234"]))
    _sweep(pg, PostRecorder(), in_dl=[_wire("X2")])
    assert _desadv_status(pg, rid) is None
    assert _manual_alerts(pg) == []


def test_a_receipt_dated_before_the_delivery_is_another_document(pg):
    """A receipt cannot predate the delivery it books — an older one with the same digits is a
    reused counter or an invoice number typed into the DL field. Marking on it would drop a
    genuinely waiting delivery from the reminder and tell ops to delete its file."""
    rid = _desadv(pg)
    _receipts(pg, (RECEIPT, SUP_A, [CODEX_DL], date(2026, 8, 1)))
    posts = PostRecorder()
    _sweep(pg, posts, in_dl=[_wire()])
    assert _desadv_status(pg, rid) is None
    assert len(posts.calls) == 1, "the warehouse is still reminded"
    assert _manual_alerts(pg) == []


def test_a_receipt_one_day_before_the_delivery_still_counts(pg):
    """`invoice_dedup.DATE_WINDOW_DAYS`: a supplier may print the dispatch date (a day off)."""
    rid = _desadv(pg)
    _receipts(pg, (RECEIPT, SUP_A, [CODEX_DL], date(2026, 8, 2)))
    _sweep(pg, PostRecorder(), in_dl=[_wire()])
    assert _desadv_status(pg, rid) == "manual"


def test_a_receipt_booked_days_after_the_delivery_counts(pg):
    """CODEX books a receipt the day it is typed — a DL typed in by hand days after its
    delivery (the incident's were back-dated to the delivery day, also covered above)."""
    rid = _desadv(pg)
    _receipts(pg, (RECEIPT, SUP_A, [CODEX_DL], date(2026, 8, 13)))
    _sweep(pg, PostRecorder(), in_dl=[_wire()])
    assert _desadv_status(pg, rid) == "manual"


def test_a_receipt_booked_over_several_days_counts_by_its_last_day(pg):
    """`receipt_date_to`: a receipt opened before the delivery and closed after it is still
    a receipt of this delivery (CODEX books one receipt over several days)."""
    rid = _desadv(pg)
    _receipts(pg, (RECEIPT, SUP_A, [CODEX_DL], date(2026, 7, 30), date(2026, 8, 4)))
    _sweep(pg, PostRecorder(), in_dl=[_wire()])
    assert _desadv_status(pg, rid) == "manual"


def test_a_row_without_a_delivery_date_is_never_marked(pg):
    """Without the delivery fact (rows before #485) the receipt cannot be tied to THIS
    delivery — the row keeps waiting rather than risk a wrong `manual`."""
    rid = _desadv(pg, delivery=None)
    _receipts(pg, (RECEIPT, SUP_A, [CODEX_DL]))
    _sweep(pg, PostRecorder(), in_dl=[_wire()])
    assert _desadv_status(pg, rid) is None
    assert _manual_alerts(pg) == []


def test_an_imported_file_stays_imported_even_with_a_receipt(pg):
    """CODEX's own import of our file is ALSO a receipt with our DL number — archCodex wins."""
    rid = _desadv(pg)
    _receipts(pg, (RECEIPT, SUP_A, [CODEX_DL]))
    _sweep(pg, PostRecorder(), arch=[_wire()])
    assert _desadv_status(pg, rid) == "imported"
    assert _manual_alerts(pg) == []


# --- failures: fail-open lookup, one transaction, a guarded update --------------------------

def test_a_failing_receipts_lookup_leaves_the_row_waiting(pg, monkeypatch, caplog):
    rid = _desadv(pg)
    _receipts(pg, (RECEIPT, SUP_A, [CODEX_DL]))

    def boom(*_a, **_kw):
        raise RuntimeError("receipts lookup down")

    monkeypatch.setattr(codex_receipts, "for_supplier", boom)
    posts = PostRecorder()
    _sweep(pg, posts, in_dl=[_wire()])
    assert _desadv_status(pg, rid) is None, "fail-open: the row keeps waiting"
    assert len(posts.calls) == 1, "the carryover alert still goes out"
    assert _manual_alerts(pg) == []
    assert "receipts lookup down" in caplog.text


def test_unreadable_delivery_dates_leave_every_row_waiting_and_the_sweep_alive(pg):
    """A REAL failure (the column renamed away): the delivery-date read must never take the
    sweep — and with it every carryover reminder — down."""
    rid = _desadv(pg)
    _receipts(pg, (RECEIPT, SUP_A, [CODEX_DL]))
    posts = PostRecorder()
    pg.execute("ALTER TABLE desadv_sent RENAME COLUMN delivery_date TO delivery_date_494")
    try:
        _sweep(pg, posts, in_dl=[_wire()])
    finally:
        pg.execute("ALTER TABLE desadv_sent RENAME COLUMN delivery_date_494 TO delivery_date")
    assert _desadv_status(pg, rid) is None, "fail-open: the row keeps waiting"
    assert len(posts.calls) == 1, "the carryover alert still goes out"
    assert _manual_alerts(pg) == []


def test_unloadable_supplier_names_still_mark_and_name_the_file(pg):
    """A REAL failure of the names lookup (its table renamed away): the row is still marked
    and its alert still names the DL, the receipt and the file — just without the supplier."""
    _suppliers(pg)
    rid = _desadv(pg)
    _receipts(pg, (RECEIPT, SUP_A, [CODEX_DL]))
    pg.execute("ALTER TABLE dl_supplier_overrides RENAME TO dl_supplier_overrides_494")
    try:
        _sweep(pg, PostRecorder(), in_dl=[_wire()])
    finally:
        pg.execute("ALTER TABLE dl_supplier_overrides_494 RENAME TO dl_supplier_overrides")
    assert _desadv_status(pg, rid) == "manual"
    body = _manual_alerts(pg)[0][3]
    assert f"DL {DOC} &mdash;" in body and RECEIPT in body and _wire() in body, body
    assert SUP_A_NAME not in body


def test_status_and_alert_are_one_transaction_a_failed_alert_leaves_the_row_waiting(
        pg, monkeypatch):
    """`manual` without its ops alert would silently swallow the „delete the file" message —
    the status change and the outbox row land together or not at all."""
    rid = _desadv(pg)
    _receipts(pg, (RECEIPT, SUP_A, [CODEX_DL]))

    def boom(*_a, **_kw):
        raise RuntimeError("outbox down")

    monkeypatch.setattr(dl_alerts, "enqueue", boom)
    posts = PostRecorder()
    _sweep(pg, posts, in_dl=[_wire()])
    assert _row(pg, rid) == (None, None), "rolled back — still waiting, retried next sweep"
    assert len(posts.calls) == 1, "the carryover alert still goes out"

    monkeypatch.undo()
    _make_due(pg)
    _sweep(pg, posts, now=TUE_MORNING.replace(hour=12), in_dl=[_wire()])
    assert _desadv_status(pg, rid) == "manual"
    assert len(_manual_alerts(pg)) == 1


def test_mark_never_touches_a_row_resolved_meanwhile(pg):
    """The status change is guarded (`import_status IS NULL`): a row another sweep resolved
    between the listing and the mark keeps its status and gets no alert."""
    rid = _desadv(pg)
    _receipts(pg, (RECEIPT, SUP_A, [CODEX_DL]))
    row = {"id": rid, "supplier_ean": SUP_A, "doc_number": DOC, "filename": _fname(),
           "uploaded_at": MON_EVENING}
    pg.execute("UPDATE desadv_sent SET import_status = 'imported' WHERE id = %s", (rid,))
    marked = confirm_manual.mark(pg, _ocfg(), [row], wire_prefix="Z-", now=TUE_MORNING)
    assert marked == set()
    assert _desadv_status(pg, rid) == "imported"
    assert _manual_alerts(pg) == []


# --- after manual: the file vanishing is no alarm; the incident closes ----------------------

def test_a_manual_file_that_later_vanishes_raises_no_unknown_alarm(pg):
    """The ops alert asks a human to delete the file — that deletion must never come back as
    „zmizol zo všetkých priečinkov"."""
    rid = _desadv(pg)
    _receipts(pg, (RECEIPT, SUP_A, [CODEX_DL]))
    posts = PostRecorder()
    _sweep(pg, posts, in_dl=[_wire()])
    assert _desadv_status(pg, rid) == "manual"

    _insert(pg, "8", "ORDER_y.txt", uploaded_minutes_ago=90)        # forces a listing
    _make_due(pg)
    _sweep(pg, posts, now=TUE_MORNING.replace(hour=12), arch=["ORDER_y.txt"])
    assert _desadv_status(pg, rid) == "manual"
    assert posts.calls == [], posts.calls
    assert confirm._open_incident(pg, 243, "unknown", confirm.DESADV_LEDGER) is None


def test_a_carryover_incident_of_imported_and_manual_members_closes(pg):
    _suppliers(pg)
    a = _desadv(pg, DOC)
    b = _desadv(pg, DOC2)
    both = [_wire(DOC), _wire(DOC2)]
    posts = PostRecorder()
    _sweep(pg, posts, in_dl=both)
    assert len(posts.calls) == 1, "the carryover incident opens as today (no receipt yet)"
    assert confirm._open_incident(pg, 243, "carryover", confirm.DESADV_LEDGER) is not None

    # later the receipt for A arrives (typed by hand) and B is imported normally
    _receipts(pg, (RECEIPT, SUP_A, [CODEX_DL]))
    _make_due(pg)
    _sweep(pg, posts, now=TUE_MORNING.replace(hour=16), in_dl=[both[0]], arch=[both[1]])
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
    a = _desadv(pg, DOC)
    _desadv(pg, DOC2)
    both = [_wire(DOC), _wire(DOC2)]
    posts = PostRecorder()
    _sweep(pg, posts, in_dl=both)
    assert len(posts.calls) == 1

    _receipts(pg, (RECEIPT, SUP_A, [CODEX_DL]))
    _make_due(pg)
    pg.execute("UPDATE desadv_sent SET import_checked_at = now() WHERE id = %s", (a,))
    _sweep(pg, posts, now=WED_MORNING, in_dl=both)
    assert _desadv_status(pg, a) == "manual"
    assert len(posts.calls) == 2, "the next working-day morning reminder (#495) about the one file really waiting"
    html = posts.calls[1][0]
    assert "Stále 1 dodací list" in html, html
    assert DOC2 in html and DOC not in html


# --- a manual mark is not an import pass (#255) ----------------------------------------------

def test_a_manual_mark_is_no_evidence_of_an_import_pass_for_the_same_day_check(pg):
    """`_activity_today_at` reads `import_confirmed_at` as „an import ran today" — a manual
    mark writes it too, but nobody clicked „prijať"; another file uploaded earlier today and
    still queued must stay silent in the evening."""
    _desadv(pg, DOC, uploaded_at=MON_EVENING.replace(hour=9))
    waiting = _desadv(pg, DOC2, uploaded_at=MON_EVENING.replace(hour=10))
    _receipts(pg, (RECEIPT, SUP_A, [CODEX_DL]))
    posts = PostRecorder()
    evening = MON_EVENING.replace(hour=18, minute=30)
    _sweep(pg, posts, now=evening, in_dl=[_wire(DOC), _wire(DOC2)])
    assert _desadv_status(pg, waiting) is None
    assert confirm._activity_today_at(pg, evening) is None
    assert posts.calls == [], "no import ran today — the queued file is simply waiting"


def test_activity_today_counts_only_rows_really_imported(pg):
    evening = MON_EVENING.replace(hour=18, minute=30)
    rid = _desadv(pg, uploaded_at=MON_EVENING.replace(hour=9))
    pg.execute("UPDATE desadv_sent SET import_status = 'manual', import_confirmed_at = %s "
               "WHERE id = %s", (MON_EVENING.replace(hour=12), rid))
    assert confirm._activity_today_at(pg, evening) is None, "a manual mark is no import"
    pg.execute("UPDATE desadv_sent SET import_status = 'imported' WHERE id = %s", (rid,))
    assert confirm._activity_today_at(pg, evening) == MON_EVENING.replace(hour=12)
