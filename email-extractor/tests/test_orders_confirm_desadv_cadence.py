"""#495 — „⚠️ Stále N dodacích listov neprevzatých v ORIONe": the WAREHOUSE delivery-notes
thread (DESADV, channel 243) is reminded of an open import incident at most ONCE per working
day after the morning hour — the cadence the DL board reminders already use
(`confirm.morning_check_active`) — never every `import_alert_reminder_hours` (4 h → live: 10,
14, 18 and 22 h, 47 posts in 14 days). The same-day evening check (#255) opens no DESADV alert:
the next morning's carryover check names the file. EDI orders (channel 152) are unchanged.
All fixtures SYNTHETIC (made-up numbers) — public repo.
"""
from __future__ import annotations

from datetime import datetime

from test_orders_confirm import (
    MON_EVENING,
    TZ,
    PostRecorder,
    _cfg,
    _insert_at,
    _insert_desadv_at,
)

from app.orders import confirm

# 2026-08-03 = Monday … 08-07 = Friday, 08-10 = Monday.
TUE = datetime(2026, 8, 4, 11, 0, tzinfo=TZ)
WED = datetime(2026, 8, 5, 10, 30, tzinfo=TZ)
FRI = datetime(2026, 8, 7, 11, 0, tzinfo=TZ)
SAT = datetime(2026, 8, 8, 11, 0, tzinfo=TZ)
MON2 = datetime(2026, 8, 10, 10, 30, tzinfo=TZ)
MON_EARLY = datetime(2026, 8, 3, 7, 30, tzinfo=TZ)


def _sweep(pg, posts, now, queued=(), imported=(), unconfirmed=()):
    pg.execute("UPDATE desadv_sent SET import_checked_at = now() - interval '10 minutes'")
    pg.execute("UPDATE edi_sent SET import_checked_at = now() - interval '10 minutes'")
    edi_q = {n for n in queued if n.startswith("ORDER_")}
    dl_q = {f"Z-{n}" for n in queued if n.startswith("DESADV_")}
    arch = {(f"Z-{n}" if n.startswith("DESADV_") else n) for n in imported}
    unc = {(f"Z-{n}" if n.startswith("DESADV_") else n) for n in unconfirmed}
    return confirm.sweep(pg, _cfg(), post=posts, now=now,
                         listdir=lambda: {"in": edi_q, "in_DL": dl_q, "archCodex": arch,
                                          "unconfirmed": unc},
                         read_files=lambda names: {})


def test_an_open_desadv_carryover_incident_is_reminded_once_per_working_day_morning(pg):
    _insert_desadv_at(pg, "9", "DESADV_c1.txt", uploaded_at=MON_EVENING, doc_number="12345601")
    posts = PostRecorder()
    _sweep(pg, posts, TUE, queued=["DESADV_c1.txt"])
    assert len(posts.calls) == 1, "the opening alert"
    for hour in (15, 19, 23):      # +4 h, +8 h, +12 h — the old 14/18/22 h repeats
        _sweep(pg, posts, TUE.replace(hour=hour), queued=["DESADV_c1.txt"])
    assert len(posts.calls) == 1, "no repeat the same day"
    _sweep(pg, posts, WED.replace(hour=9), queued=["DESADV_c1.txt"])
    assert len(posts.calls) == 1, "not before the morning hour"
    _sweep(pg, posts, WED, queued=["DESADV_c1.txt"])
    assert len(posts.calls) == 2, "one reminder the next working-day morning"
    assert "Stále 1 dodací list" in posts.calls[1][0]
    assert posts.calls[1][1] == 243
    _sweep(pg, posts, WED.replace(hour=15), queued=["DESADV_c1.txt"])
    assert len(posts.calls) == 2


def test_a_desadv_incident_is_never_reminded_on_a_weekend(pg):
    _insert_desadv_at(pg, "9", "DESADV_w1.txt", uploaded_at=MON_EVENING, doc_number="12345602")
    posts = PostRecorder()
    _sweep(pg, posts, FRI, queued=["DESADV_w1.txt"])
    assert len(posts.calls) == 1
    _sweep(pg, posts, SAT, queued=["DESADV_w1.txt"])
    _sweep(pg, posts, SAT.replace(day=9), queued=["DESADV_w1.txt"])
    assert len(posts.calls) == 1
    _sweep(pg, posts, MON2, queued=["DESADV_w1.txt"])
    assert len(posts.calls) == 2


def test_a_desadv_failed_incident_is_reminded_daily_too(pg):
    _insert_desadv_at(pg, "9", "DESADV_f1.txt", uploaded_at=MON_EVENING, doc_number="12345603")
    posts = PostRecorder()
    _sweep(pg, posts, TUE, unconfirmed=["DESADV_f1.txt"])
    assert len(posts.calls) == 1
    # the failed row is terminal now; a later file of the same kind folds into the incident
    _insert_desadv_at(pg, "9", "DESADV_f2.txt", uploaded_at=MON_EVENING, doc_number="12345604")
    _sweep(pg, posts, TUE.replace(hour=16), unconfirmed=["DESADV_f1.txt", "DESADV_f2.txt"])
    assert len(posts.calls) == 1, "no 4-hourly repeat in the warehouse thread"
    _insert_desadv_at(pg, "9", "DESADV_f3.txt", uploaded_at=MON_EVENING, doc_number="12345605")
    _sweep(pg, posts, WED, unconfirmed=["DESADV_f1.txt", "DESADV_f2.txt", "DESADV_f3.txt"])
    assert len(posts.calls) == 2


def test_an_edi_carryover_incident_keeps_its_4h_reminder(pg):
    _insert_at(pg, "8580000000019", "ORDER_c1.txt", uploaded_at=MON_EVENING)
    posts = PostRecorder()
    _sweep(pg, posts, TUE, queued=["ORDER_c1.txt"])
    _sweep(pg, posts, TUE.replace(hour=13), queued=["ORDER_c1.txt"])
    assert len(posts.calls) == 1
    _sweep(pg, posts, TUE.replace(hour=16), queued=["ORDER_c1.txt"])
    assert len(posts.calls) == 2, "orders (channel 152) keep the 4-hour reminder"
    assert posts.calls[1][1] == 152


def test_the_evening_check_opens_no_desadv_alert_the_morning_one_does(pg):
    # today's import pass (the #255 activity signal) took another file at 07:30 …
    _insert_desadv_at(pg, "9", "DESADV_a1.txt", uploaded_at=MON_EARLY.replace(hour=6),
                      doc_number="12345606")
    posts = PostRecorder()
    _sweep(pg, posts, MON_EARLY, imported=["DESADV_a1.txt"])
    assert posts.calls == []
    # … but left ours (uploaded 07:00, before it) behind
    _insert_desadv_at(pg, "9", "DESADV_s1.txt", uploaded_at=MON_EARLY.replace(minute=0),
                      doc_number="12345607")
    _sweep(pg, posts, MON_EVENING, queued=["DESADV_s1.txt"])
    assert posts.calls == [], "the evening check opens no DESADV alert"
    _sweep(pg, posts, TUE, queued=["DESADV_s1.txt"])
    assert len(posts.calls) == 1, "the next morning's carryover check catches it"
    assert posts.calls[0][1] == 243


def test_the_evening_check_still_alerts_an_edi_order(pg):
    _insert_at(pg, "8580000000019", "ORDER_a1.txt", uploaded_at=MON_EARLY.replace(hour=6))
    posts = PostRecorder()
    _sweep(pg, posts, MON_EARLY, imported=["ORDER_a1.txt"])
    _insert_at(pg, "8580000000019", "ORDER_s1.txt", uploaded_at=MON_EARLY.replace(minute=0))
    _sweep(pg, posts, MON_EVENING, queued=["ORDER_s1.txt"])
    assert len(posts.calls) == 1 and posts.calls[0][1] == 152


def test_reminder_due_cadence_by_source():
    cfg = _cfg()
    tue_10 = TUE.replace(hour=10, minute=30)
    last = TUE.replace(hour=9)
    desadv, edi = confirm.DESADV_LEDGER, confirm.EDI_LEDGER
    assert confirm._reminder_due(cfg, desadv, last, tue_10.replace(hour=18), 4) is False
    assert confirm._reminder_due(cfg, desadv, last, WED, 4) is True
    assert confirm._reminder_due(cfg, edi, last, tue_10.replace(hour=13), 4) is True
    assert confirm._reminder_due(cfg, edi, last, tue_10, 4) is False
