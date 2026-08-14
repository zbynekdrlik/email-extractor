"""Import CONFIRMATION (#151, revised #133 2026-08-05): uploading a file to `in/` is not
proof it ever reached ORION — but ORION import is a MANUAL morning click by the warehouse
(pani skladníčka), never an automatic sweep. A file sitting in `in/` overnight/over the
weekend is NORMAL; it only becomes alert-worthy as a CARRYOVER once it is left over from a
PRIOR day and the configured morning hour has arrived. Every alert-worthy condition
(carryover / failed / unknown) is grouped into ONE message per incident, never one message
per file — the real incident this rewrite fixes: the old ~60-minute timeout model posted 5
separate per-file alerts for one order sitting unaccepted since the afternoon, a false
alarm the user had deleted from the Odoo channel.

Live evidence still pinned by these tests (unchanged by this rewrite, see the #151 design
discussion): the `Z-` filename prefix is NOT the imported signal — presence in `archCodex`
is, with or without it.
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import Config
from app.orders import confirm

PG_DSN = os.environ.get("PG_TEST_DSN")
TZ = ZoneInfo("Europe/Bratislava")

# 2026-08-03 = Monday, 08-07 = Friday, 08-08 = Saturday, 08-09 = Sunday, 08-10 = Monday.
MON_MORNING = datetime(2026, 8, 3, 11, 0, tzinfo=TZ)
MON_EVENING = datetime(2026, 8, 3, 18, 0, tzinfo=TZ)
TUE_MORNING = datetime(2026, 8, 4, 11, 0, tzinfo=TZ)
FRI_EVENING = datetime(2026, 8, 7, 18, 0, tzinfo=TZ)
SAT_MORNING = datetime(2026, 8, 8, 11, 0, tzinfo=TZ)
SUN_MORNING = datetime(2026, 8, 9, 11, 0, tzinfo=TZ)
NEXT_MON_MORNING = datetime(2026, 8, 10, 11, 0, tzinfo=TZ)


class PostRecorder:
    def __init__(self, deliver=True):
        self.calls = []
        self.deliver = deliver

    def __call__(self, cfg, html, **kw):
        self.calls.append((html, kw.get("channel_id")))
        return {"id": 1} if self.deliver else None


def _cfg(**kw):
    base = dict(pg_dsn=PG_DSN, data_dir="/tmp", odoo_url="https://erp.example.sk",
               odoo_api_key="k", orders_channel_id=152, delivery_notes_channel_id=243,
               import_confirm_interval_minutes=5)
    base.update(kw)
    return Config(**base)


def _insert_at(pg, ean, filename, uploaded_at: datetime, checked_at: datetime | None = None):
    row = pg.execute(
        """INSERT INTO edi_sent (customer_ean, delivery_date, content_sha256, filename,
                                 uploaded_at, import_checked_at)
           VALUES (%s, '04.08.2026', %s, %s, %s, %s)
           RETURNING id""",
        (ean, "hash-" + filename, filename, uploaded_at, checked_at)).fetchone()
    return row[0]


def _insert(pg, ean, filename, uploaded_minutes_ago=0, checked_minutes_ago=None):
    row = pg.execute(
        """INSERT INTO edi_sent (customer_ean, delivery_date, content_sha256, filename,
                                 uploaded_at, import_checked_at)
           VALUES (%s, '04.08.2026', %s, %s,
                   now() - make_interval(mins => %s),
                   CASE WHEN %s::int IS NULL THEN NULL
                        ELSE now() - make_interval(mins => %s::int) END)
           RETURNING id""",
        (ean, "hash-" + filename, filename, uploaded_minutes_ago,
         checked_minutes_ago, checked_minutes_ago or 0)).fetchone()
    return row[0]


def _status(pg, rid):
    return pg.execute(
        "SELECT import_status FROM edi_sent WHERE id = %s", (rid,)).fetchone()[0]


def _insert_desadv_at(pg, supplier_ean, filename, uploaded_at: datetime,
                      doc_number: str = "D1"):
    row = pg.execute(
        """INSERT INTO desadv_sent (supplier_ean, doc_number, filename, uploaded_at)
           VALUES (%s, %s, %s, %s) RETURNING id""",
        (supplier_ean, doc_number, filename, uploaded_at)).fetchone()
    return row[0]


def _desadv_status(pg, rid):
    return pg.execute(
        "SELECT import_status FROM desadv_sent WHERE id = %s", (rid,)).fetchone()[0]


# --- imported: archCodex presence, WITH or WITHOUT the Z- prefix ---------------------

def test_a_file_present_in_archcodex_is_confirmed_imported_with_no_alert(pg):
    rid = _insert(pg, "1", "ORDER_a.txt", uploaded_minutes_ago=90)
    posts = PostRecorder()
    n = confirm.sweep(pg, _cfg(),
                      listdir=lambda: {"in": set(), "archCodex": {"ORDER_a.txt"},
                                       "unconfirmed": set()},
                      post=posts, now=MON_MORNING)
    assert n == 1
    assert posts.calls == [], "a clean import must never alert"
    assert _status(pg, rid) == "imported"


def test_a_z_prefixed_file_in_archcodex_still_counts_as_imported(pg):
    """The ticket's own original proposal (key on the Z- prefix) is WRONG — live evidence
    showed an already-imported file sitting 5+ hours with no Z- prefix yet. The prefix, if
    present, must not be REQUIRED."""
    rid = _insert(pg, "2", "ORDER_b.txt", uploaded_minutes_ago=90)
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: {"in": set(), "archCodex": {"Z-ORDER_b.txt"},
                                   "unconfirmed": set()},
                  post=posts, now=MON_MORNING)
    assert posts.calls == []
    assert _status(pg, rid) == "imported"


# --- normal: a file still in `in/` is NEVER alert-worthy the same day it arrived -------

def test_an_evening_upload_still_in_in_is_completely_silent_the_same_evening(pg):
    rid = _insert_at(pg, "3", "ORDER_c.txt", uploaded_at=MON_EVENING)
    posts = PostRecorder()
    n = confirm.sweep(pg, _cfg(),
                      listdir=lambda: {"in": {"ORDER_c.txt"}, "archCodex": set(),
                                       "unconfirmed": set()},
                      post=posts, now=MON_EVENING)
    assert n == 0
    assert posts.calls == [], "sitting in /in the SAME day it was uploaded is completely normal"
    row = pg.execute(
        "SELECT import_status, import_checked_at FROM edi_sent WHERE id = %s",
        (rid,)).fetchone()
    assert row[0] is None
    assert row[1] is not None, "the throttle timestamp must still advance"


def test_a_fresh_upload_this_same_morning_is_silent_even_past_the_morning_hour(pg):
    """Only a CARRYOVER (from a PRIOR day) is alert-worthy — a file uploaded THIS morning
    and still sitting in /in by 11:00 has simply not been accepted yet today, which is
    completely normal (she may not have gotten to it, or it arrived after her click)."""
    _insert_at(pg, "3b", "ORDER_c2.txt", uploaded_at=MON_MORNING)
    posts = PostRecorder()
    n = confirm.sweep(pg, _cfg(),
                      listdir=lambda: {"in": {"ORDER_c2.txt"}, "archCodex": set(),
                                       "unconfirmed": set()},
                      post=posts, now=MON_MORNING)
    assert n == 0
    assert posts.calls == []


# --- carryover: left over from a PRIOR day, past the morning hour ---------------------

def test_a_carryover_from_yesterday_alerts_once_grouped_next_morning(pg):
    """The reference incident (2026-08-05): 5 files from one order, uploaded in the
    afternoon, still unaccepted at 18:18 — under the OLD code this posted 5 separate
    messages. Under the new model, evening isn't even checked (see the test above); the
    NEXT morning's check must produce exactly ONE grouped message for all of them."""
    for i in range(5):
        _insert_at(pg, f"pno{i}", f"ORDER_pno_{i}.txt", uploaded_at=MON_EVENING)
    posts = PostRecorder()
    n = confirm.sweep(
        pg, _cfg(),
        listdir=lambda: {"in": {f"ORDER_pno_{i}.txt" for i in range(5)},
                         "archCodex": set(), "unconfirmed": set()},
        post=posts, now=TUE_MORNING)
    assert n == 0, "a carryover is never given a terminal status — it must self-heal later"
    assert len(posts.calls) == 1, "exactly ONE grouped message, never one per file"
    assert "5" in posts.calls[0][0]
    assert "objednávok" in posts.calls[0][0]
    assert "Codex" in posts.calls[0][0]
    for i in range(5):
        row = pg.execute(
            "SELECT import_status FROM edi_sent WHERE customer_ean = %s", (f"pno{i}",)
        ).fetchone()
        assert row[0] is None, "still pending, never a dead-end terminal status"


def test_a_carryover_never_gets_a_terminal_status_and_can_self_heal_to_imported(pg):
    """Requirement #133.3: a file that LATER shows up in archCodex must still flip to
    'imported' — the old bug marked it 'timeout' (terminal) and it would NEVER be
    re-checked again, even after she accepted it."""
    rid = _insert_at(pg, "heal1", "ORDER_heal.txt", uploaded_at=MON_EVENING)
    posts = PostRecorder()
    # Tuesday morning: still in /in, not yet accepted — one grouped alert, stays pending
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: {"in": {"ORDER_heal.txt"}, "archCodex": set(),
                                   "unconfirmed": set()},
                  post=posts, now=TUE_MORNING)
    assert _status(pg, rid) is None
    pg.execute("UPDATE edi_sent SET import_checked_at = now() - interval '10 minutes' "
              "WHERE id = %s", (rid,))
    # she accepts it later that same day
    n = confirm.sweep(pg, _cfg(),
                      listdir=lambda: {"in": set(), "archCodex": {"ORDER_heal.txt"},
                                       "unconfirmed": set()},
                      post=posts, now=TUE_MORNING)
    assert n == 1
    assert _status(pg, rid) == "imported"


def test_uploaded_before_today_is_the_only_carryover_signal_not_hours_elapsed(pg):
    """A file uploaded at 09:00 today and still pending at 11:00 today is NOT a carryover
    (see the "fresh upload this same morning" test) — but the SAME file, still pending
    the FOLLOWING morning, now genuinely is one. Confirms the classification keys on the
    calendar date crossing midnight, not a fixed hour-count."""
    _insert_at(pg, "date1", "ORDER_date1.txt", uploaded_at=MON_MORNING)
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: {"in": {"ORDER_date1.txt"}, "archCodex": set(),
                                   "unconfirmed": set()},
                  post=posts, now=MON_MORNING)
    assert posts.calls == []
    pg.execute("UPDATE edi_sent SET import_checked_at = now() - interval '10 minutes'")
    posts2 = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: {"in": {"ORDER_date1.txt"}, "archCodex": set(),
                                   "unconfirmed": set()},
                  post=posts2, now=TUE_MORNING)
    assert len(posts2.calls) == 1


# --- weekends: the warehouse doesn't work Saturday or Sunday --------------------------

def test_saturday_is_never_checked_by_default(pg):
    _insert_at(pg, "wk1", "ORDER_wk1.txt", uploaded_at=FRI_EVENING)
    posts = PostRecorder()
    n = confirm.sweep(pg, _cfg(),
                      listdir=lambda: {"in": {"ORDER_wk1.txt"}, "archCodex": set(),
                                       "unconfirmed": set()},
                      post=posts, now=SAT_MORNING)
    assert n == 0
    assert posts.calls == []


def test_sunday_is_never_checked_by_default(pg):
    _insert_at(pg, "wk2", "ORDER_wk2.txt", uploaded_at=FRI_EVENING)
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: {"in": {"ORDER_wk2.txt"}, "archCodex": set(),
                                   "unconfirmed": set()},
                  post=posts, now=SUN_MORNING)
    assert posts.calls == []


def test_a_friday_evening_upload_is_first_checked_the_following_monday(pg):
    _insert_at(pg, "wk3", "ORDER_wk3.txt", uploaded_at=FRI_EVENING)
    posts = PostRecorder()
    n = confirm.sweep(pg, _cfg(),
                      listdir=lambda: {"in": {"ORDER_wk3.txt"}, "archCodex": set(),
                                       "unconfirmed": set()},
                      post=posts, now=NEXT_MON_MORNING)
    assert n == 0
    assert len(posts.calls) == 1, "the weekend carryover surfaces on Monday's check"


def test_weekend_skip_days_are_individually_configurable(pg):
    _insert_at(pg, "wk4", "ORDER_wk4.txt", uploaded_at=FRI_EVENING)
    posts = PostRecorder()
    n = confirm.sweep(pg, _cfg(import_morning_check_skip_saturday=False),
                      listdir=lambda: {"in": {"ORDER_wk4.txt"}, "archCodex": set(),
                                       "unconfirmed": set()},
                      post=posts, now=SAT_MORNING)
    assert n == 0
    assert len(posts.calls) == 1, "Saturday checking can be explicitly re-enabled"


# --- the morning hour itself is configurable -------------------------------------------

def test_before_the_configured_morning_hour_nothing_is_checked_yet(pg):
    early = datetime(2026, 8, 4, 8, 0, tzinfo=TZ)   # Tuesday 08:00, before default 10:00
    _insert_at(pg, "hr1", "ORDER_hr1.txt", uploaded_at=MON_EVENING)
    posts = PostRecorder()
    n = confirm.sweep(pg, _cfg(),
                      listdir=lambda: {"in": {"ORDER_hr1.txt"}, "archCodex": set(),
                                       "unconfirmed": set()},
                      post=posts, now=early)
    assert n == 0
    assert posts.calls == []


# --- failed: unconfirmed, a genuine anomaly, grouped like everything else -------------

def test_files_in_unconfirmed_alert_grouped_regardless_of_age(pg):
    rid1 = _insert_at(pg, "f1", "ORDER_f1.txt", uploaded_at=MON_MORNING)
    rid2 = _insert_at(pg, "f2", "ORDER_f2.txt", uploaded_at=MON_MORNING)
    posts = PostRecorder()
    n = confirm.sweep(
        pg, _cfg(),
        listdir=lambda: {"in": set(), "archCodex": set(),
                         "unconfirmed": {"ORDER_f1.txt", "ORDER_f2.txt"}},
        post=posts, now=MON_MORNING)
    assert n == 2
    assert len(posts.calls) == 1, "two simultaneous failures = ONE grouped message"
    assert "2" in posts.calls[0][0]
    assert _status(pg, rid1) == "failed"
    assert _status(pg, rid2) == "failed"


# --- unknown: gone from all three, never silent ----------------------------------------

def test_files_gone_from_all_three_directories_alert_grouped_as_unknown(pg):
    rid = _insert_at(pg, "u1", "ORDER_u1.txt", uploaded_at=MON_MORNING)
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: {"in": set(), "archCodex": set(), "unconfirmed": set()},
                  post=posts, now=MON_MORNING)
    assert len(posts.calls) == 1
    assert "zmizlo" in posts.calls[0][0]
    assert _status(pg, rid) == "unknown"


def test_a_row_with_no_recorded_filename_alerts_as_unknown_immediately(pg):
    """A blank filename can never be looked up in any of the three folders — it must
    never be silently treated as pending forever, or as a clean import."""
    rid = _insert_at(pg, "blank1", "", uploaded_at=MON_MORNING)
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: {"in": set(), "archCodex": set(), "unconfirmed": set()},
                  post=posts, now=MON_MORNING)
    assert len(posts.calls) == 1
    assert _status(pg, rid) == "unknown"


# --- dedup: the SAME incident never posts a second message while it persists ----------

def test_dedup_no_repeat_message_while_the_carryover_incident_persists(pg):
    _insert_at(pg, "d1", "ORDER_d1.txt", uploaded_at=MON_EVENING)
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: {"in": {"ORDER_d1.txt"}, "archCodex": set(),
                                   "unconfirmed": set()},
                  post=posts, now=TUE_MORNING)
    assert len(posts.calls) == 1

    _insert_at(pg, "d2", "ORDER_d2.txt", uploaded_at=MON_EVENING)
    later_same_day = TUE_MORNING.replace(hour=13)   # +2h, well under the 4h reminder
    n = confirm.sweep(pg, _cfg(),
                      listdir=lambda: {"in": {"ORDER_d1.txt", "ORDER_d2.txt"},
                                       "archCodex": set(), "unconfirmed": set()},
                      post=posts, now=later_same_day)
    assert n == 0
    assert len(posts.calls) == 1, "a NEW carryover row folds into the SAME open incident, no new message"

    incident = confirm._open_incident(pg, 152, "carryover")
    assert incident["file_count"] == 2, "the incident's running count grows even though it's silent"


def test_an_incident_left_open_across_a_day_boundary_produces_the_reminder_not_a_new_alert(pg):
    """A genuinely still-unresolved incident spanning into a SECOND day is exactly the
    ~4h-reminder case, not a fresh "new incident" message — and once that one reminder
    has fired, a further same-day sweep must not repeat it again."""
    _insert_at(pg, "d3", "ORDER_d3.txt", uploaded_at=MON_EVENING)
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: {"in": {"ORDER_d3.txt"}, "archCodex": set(),
                                   "unconfirmed": set()},
                  post=posts, now=TUE_MORNING)
    assert len(posts.calls) == 1

    wed_morning = datetime(2026, 8, 5, 11, 0, tzinfo=TZ)   # +24h, well past the reminder
    pg.execute("UPDATE edi_sent SET import_checked_at = now() - interval '10 minutes'")
    n = confirm.sweep(pg, _cfg(),
                      listdir=lambda: {"in": {"ORDER_d3.txt"}, "archCodex": set(),
                                       "unconfirmed": set()},
                      post=posts, now=wed_morning)
    assert n == 0
    assert len(posts.calls) == 2, "one reminder, never a second brand-new incident message"

    pg.execute("UPDATE edi_sent SET import_checked_at = now() - interval '10 minutes'")
    n2 = confirm.sweep(pg, _cfg(),
                       listdir=lambda: {"in": {"ORDER_d3.txt"}, "archCodex": set(),
                                        "unconfirmed": set()},
                       post=posts, now=wed_morning.replace(hour=12))
    assert n2 == 0
    assert len(posts.calls) == 2, "the reminder itself must not repeat within its own window"


# --- reminder: at most one, after the configured threshold ----------------------------

def test_a_reminder_fires_once_after_the_threshold_and_updates_last_alert(pg):
    _insert_at(pg, "r1", "ORDER_r1.txt", uploaded_at=MON_EVENING)
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: {"in": {"ORDER_r1.txt"}, "archCodex": set(),
                                   "unconfirmed": set()},
                  post=posts, now=TUE_MORNING)
    assert len(posts.calls) == 1

    _insert_at(pg, "r2", "ORDER_r2.txt", uploaded_at=MON_EVENING)
    still_within_window = TUE_MORNING.replace(hour=13)   # +2h, under the 4h default
    pg.execute("UPDATE edi_sent SET import_checked_at = now() - interval '10 minutes'")
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: {"in": {"ORDER_r1.txt", "ORDER_r2.txt"},
                                   "archCodex": set(), "unconfirmed": set()},
                  post=posts, now=still_within_window)
    assert len(posts.calls) == 1, "still within the reminder window — no reminder yet"

    past_window = TUE_MORNING.replace(hour=16)   # +5h, past the 4h default
    pg.execute("UPDATE edi_sent SET import_checked_at = now() - interval '10 minutes'")
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: {"in": {"ORDER_r1.txt", "ORDER_r2.txt"},
                                   "archCodex": set(), "unconfirmed": set()},
                  post=posts, now=past_window)
    assert len(posts.calls) == 2, "exactly one reminder once the threshold passes"

    # and it doesn't fire AGAIN immediately after
    pg.execute("UPDATE edi_sent SET import_checked_at = now() - interval '10 minutes'")
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: {"in": {"ORDER_r1.txt", "ORDER_r2.txt"},
                                   "archCodex": set(), "unconfirmed": set()},
                  post=posts, now=past_window.replace(minute=5))
    assert len(posts.calls) == 2


def test_reminder_threshold_is_configurable(pg):
    _insert_at(pg, "r3", "ORDER_r3.txt", uploaded_at=MON_EVENING)
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(import_alert_reminder_hours=1),
                  listdir=lambda: {"in": {"ORDER_r3.txt"}, "archCodex": set(),
                                   "unconfirmed": set()},
                  post=posts, now=TUE_MORNING)
    assert len(posts.calls) == 1

    _insert_at(pg, "r4", "ORDER_r4.txt", uploaded_at=MON_EVENING)
    pg.execute("UPDATE edi_sent SET import_checked_at = now() - interval '10 minutes'")
    confirm.sweep(pg, _cfg(import_alert_reminder_hours=1),
                  listdir=lambda: {"in": {"ORDER_r3.txt", "ORDER_r4.txt"},
                                   "archCodex": set(), "unconfirmed": set()},
                  post=posts, now=TUE_MORNING.replace(hour=13))   # +2h > 1h threshold
    assert len(posts.calls) == 2


# --- all-clear: at most one, only if an alert was previously sent ---------------------

def test_an_all_clear_fires_once_when_the_carryover_incident_resolves(pg):
    rid = _insert_at(pg, "c1", "ORDER_c1.txt", uploaded_at=MON_EVENING)
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: {"in": {"ORDER_c1.txt"}, "archCodex": set(),
                                   "unconfirmed": set()},
                  post=posts, now=TUE_MORNING)
    assert len(posts.calls) == 1

    # she accepts it later
    pg.execute("UPDATE edi_sent SET import_checked_at = now() - interval '10 minutes' "
              "WHERE id = %s", (rid,))
    later = TUE_MORNING.replace(hour=15)
    n = confirm.sweep(pg, _cfg(),
                      listdir=lambda: {"in": set(), "archCodex": {"ORDER_c1.txt"},
                                       "unconfirmed": set()},
                      post=posts, now=later)
    assert n == 1
    assert _status(pg, rid) == "imported"
    assert len(posts.calls) == 2, "one grouped alert + one all-clear"
    assert "poriadku" in posts.calls[1][0] or "prijaté" in posts.calls[1][0]

    open_incident = pg.execute(
        "SELECT count(*) FROM import_alert_incidents WHERE kind='carryover' "
        "AND closed_at IS NULL").fetchone()[0]
    assert open_incident == 0

    # a LATER sweep with nothing new must never re-send the all-clear
    n2 = confirm.sweep(pg, _cfg(), listdir=lambda: {"in": set(), "archCodex": set(),
                                                     "unconfirmed": set()},
                       post=posts, now=later.replace(hour=16))
    assert n2 == 0
    assert len(posts.calls) == 2, "the all-clear must never repeat"


def test_an_unrelated_import_never_falsely_clears_a_different_open_incident(pg):
    """Deep-review finding (PR #184): the first cut cleared an incident off a GLOBAL
    'something, somewhere, was imported' signal. A completely unrelated healthy order
    importing on a DIFFERENT channel must never falsely close a still-genuinely-stuck
    incident — the stuck files would then never get their reminder, having been wrongly
    marked resolved."""
    stuck_rid = _insert_at(pg, "iso1", "ORDER_iso1.txt", uploaded_at=MON_EVENING)
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: {"in": {"ORDER_iso1.txt"}, "archCodex": set(),
                                   "unconfirmed": set()},
                  post=posts, now=TUE_MORNING)
    assert len(posts.calls) == 1
    incident_before = pg.execute(
        "SELECT closed_at FROM import_alert_incidents WHERE kind='carryover'").fetchone()
    assert incident_before[0] is None

    # a totally unrelated, healthy order (different customer, uploaded fresh, never a
    # carryover) imports cleanly on the SAME sweep
    healthy_rid = _insert_at(pg, "iso2", "ORDER_iso2.txt", uploaded_at=TUE_MORNING)
    n = confirm.sweep(pg, _cfg(),
                      listdir=lambda: {"in": {"ORDER_iso1.txt"},
                                       "archCodex": {"ORDER_iso2.txt"},
                                       "unconfirmed": set()},
                      post=posts, now=TUE_MORNING.replace(minute=30))
    assert n == 1
    assert _status(pg, healthy_rid) == "imported"
    assert _status(pg, stuck_rid) is None, "the genuinely stuck file is untouched"

    incident_after = pg.execute(
        "SELECT closed_at FROM import_alert_incidents WHERE kind='carryover'").fetchone()
    assert incident_after[0] is None, \
        "an unrelated import must NEVER close a different, still-unresolved incident"
    assert len(posts.calls) == 1, "no spurious all-clear was ever sent"


def test_file_count_is_never_inflated_by_rediscovering_the_same_stuck_row(pg):
    """Deep-review finding (PR #184): the first cut incremented a counter column every
    time a group was handled — a SINGLE stuck carryover row, rediscovered on every
    throttle cycle while still unresolved, inflated its own incident's reported count
    without bound. Membership must be deduplicated per row, not blindly counted."""
    rid = _insert_at(pg, "cnt1", "ORDER_cnt1.txt", uploaded_at=MON_EVENING)
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: {"in": {"ORDER_cnt1.txt"}, "archCodex": set(),
                                   "unconfirmed": set()},
                  post=posts, now=TUE_MORNING)
    incident = confirm._open_incident(pg, 152, "carryover")
    assert incident["file_count"] == 1

    # the SAME still-unresolved row is rediscovered on several later throttle cycles
    for i in range(4):
        pg.execute("UPDATE edi_sent SET import_checked_at = now() - interval '10 minutes' "
                  "WHERE id = %s", (rid,))
        confirm.sweep(pg, _cfg(),
                      listdir=lambda: {"in": {"ORDER_cnt1.txt"}, "archCodex": set(),
                                       "unconfirmed": set()},
                      post=posts, now=TUE_MORNING.replace(minute=30 + i))

    incident_after = confirm._open_incident(pg, 152, "carryover")
    assert incident_after["file_count"] == 1, \
        "the SAME row rediscovered repeatedly must count once, not once per rediscovery"
    assert len(posts.calls) == 1, "still just the original opening alert — no reminder yet"


def test_no_all_clear_is_ever_sent_when_no_alert_was_sent_first(pg):
    """A row that quietly resolves without ever having been a carryover (e.g. accepted
    the same day) must never trigger a spurious all-clear — there is nothing to clear."""
    rid = _insert_at(pg, "c2", "ORDER_c2.txt", uploaded_at=MON_MORNING)
    posts = PostRecorder()
    n = confirm.sweep(pg, _cfg(),
                      listdir=lambda: {"in": set(), "archCodex": {"ORDER_c2.txt"},
                                       "unconfirmed": set()},
                      post=posts, now=MON_MORNING)
    assert n == 1
    assert _status(pg, rid) == "imported"
    assert posts.calls == []


# --- durability: no in-memory state anywhere, everything reads from the DB ------------

def test_incident_state_survives_a_simulated_restart(pg):
    """No Python module-level state anywhere in confirm.py — every decision is read fresh
    from the DB on every call, so a simulated 'restart' (a completely independent call
    sequence with nothing shared but the connection) must behave identically."""
    _insert_at(pg, "s1", "ORDER_s1.txt", uploaded_at=MON_EVENING)
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: {"in": {"ORDER_s1.txt"}, "archCodex": set(),
                                   "unconfirmed": set()},
                  post=posts, now=TUE_MORNING)
    assert len(posts.calls) == 1

    # "restart" — read the incident back with a brand-new, independent query
    incident = confirm._open_incident(pg, 152, "carryover")
    assert incident is not None
    assert incident["file_count"] == 1

    # a fresh sweep call (simulating the process having restarted) still dedups correctly
    _insert_at(pg, "s2", "ORDER_s2.txt", uploaded_at=MON_EVENING)
    posts2 = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: {"in": {"ORDER_s1.txt", "ORDER_s2.txt"},
                                   "archCodex": set(), "unconfirmed": set()},
                  post=posts2, now=TUE_MORNING.replace(hour=12))
    assert posts2.calls == [], "the restart must not forget the open incident"


# --- an alert must never be lost just because delivery failed once ---------------------

def test_a_failed_first_alert_delivery_is_never_lost_and_is_retried_next_sweep(pg):
    """Review-precedent finding (PR #179), preserved under the new grouped model: rows
    must stay pending (never terminal) until the group's FIRST alert genuinely delivers."""
    rid1 = _insert_at(pg, "fl1", "ORDER_fl1.txt", uploaded_at=MON_MORNING)
    rid2 = _insert_at(pg, "fl2", "ORDER_fl2.txt", uploaded_at=MON_MORNING)

    class FlakyPost:
        def __init__(self):
            self.calls = 0

        def __call__(self, cfg, html, **kw):
            self.calls += 1
            if self.calls == 1:
                raise OSError("Odoo API unreachable")
            return {"id": 1}

    post = FlakyPost()
    listdir = lambda: {"in": set(), "archCodex": set(),  # noqa: E731
                       "unconfirmed": {"ORDER_fl1.txt", "ORDER_fl2.txt"}}

    n1 = confirm.sweep(pg, _cfg(), listdir=listdir, post=post, now=MON_MORNING)
    assert n1 == 0, "an undelivered FIRST alert must not count as resolved"
    assert post.calls == 1
    assert _status(pg, rid1) is None
    assert _status(pg, rid2) is None

    pg.execute("UPDATE edi_sent SET import_checked_at = now() - interval '10 minutes'")
    n2 = confirm.sweep(pg, _cfg(), listdir=listdir, post=post, now=MON_MORNING)
    assert n2 == 2
    assert post.calls == 2, "the retry must genuinely re-attempt delivery"
    assert _status(pg, rid1) == "failed"
    assert _status(pg, rid2) == "failed"


def test_an_alert_odoo_never_delivers_keeps_retrying_not_silently_dropped(pg):
    """`report.post_from_config` returns None (not an exception) when Odoo is simply
    unconfigured — treated the same as a delivery failure, not as success."""
    rid = _insert_at(pg, "nd1", "ORDER_nd1.txt", uploaded_at=MON_MORNING)
    n = confirm.sweep(pg, _cfg(),
                      listdir=lambda: {"in": set(), "archCodex": set(),
                                       "unconfirmed": {"ORDER_nd1.txt"}},
                      post=PostRecorder(deliver=False), now=MON_MORNING)
    assert n == 0
    assert _status(pg, rid) is None


# --- SFTP is contacted only when there is real work ------------------------------------

def test_sweep_never_touches_sftp_when_nothing_is_pending(pg):
    def boom():
        raise AssertionError("SFTP must never be contacted with no pending rows")

    n = confirm.sweep(pg, _cfg(), listdir=boom, post=PostRecorder(), now=MON_MORNING)
    assert n == 0


def test_the_per_row_throttle_skips_a_recently_checked_row(pg):
    _insert(pg, "8", "ORDER_h.txt", uploaded_minutes_ago=90, checked_minutes_ago=1)

    def boom():
        raise AssertionError("a recently-checked row must not trigger a fresh listing")

    n = confirm.sweep(pg, _cfg(import_confirm_interval_minutes=5), listdir=boom,
                      post=PostRecorder(), now=MON_MORNING)
    assert n == 0


# --- a listdir failure backs off, it does not hammer ORION every tick ------------------

def test_a_listdir_failure_backs_off_by_the_normal_throttle_not_a_tight_retry_loop(pg):
    """Review finding on PR #179: without advancing import_checked_at here, a sustained
    ORION-side outage would retry the SFTP connection on every single worker tick
    (~15s) — this makes a listdir failure back off by the SAME interval a normal check
    already respects."""
    rid = _insert(pg, "14", "ORDER_k.txt", uploaded_minutes_ago=1)

    def boom():
        raise OSError("connection refused")

    n = confirm.sweep(pg, _cfg(import_confirm_interval_minutes=5), listdir=boom,
                      post=PostRecorder(), now=MON_MORNING)
    assert n == 0
    row = pg.execute(
        "SELECT import_status, import_checked_at FROM edi_sent WHERE id = %s",
        (rid,)).fetchone()
    assert row[0] is None
    assert row[1] is not None, "must back off — a bare listdir failure updates nothing"
    assert confirm.due_rows(pg, 5) == [], "must not be immediately due again"


# --- channel routing: ORDER_* vs DESADV_* -----------------------------------------------

def test_desadv_alert_routes_to_the_delivery_notes_channel(pg):
    _insert_at(pg, "9", "DESADV_a.txt", uploaded_at=MON_MORNING)
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: {"in": set(), "archCodex": set(),
                                   "unconfirmed": {"DESADV_a.txt"}},
                  post=posts, now=MON_MORNING)
    assert posts.calls[0][1] == 243


def test_desadv_alert_falls_back_to_the_orders_channel_when_unset(pg):
    _insert_at(pg, "10", "DESADV_b.txt", uploaded_at=MON_MORNING)
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(delivery_notes_channel_id=0),
                  listdir=lambda: {"in": set(), "archCodex": set(),
                                   "unconfirmed": {"DESADV_b.txt"}},
                  post=posts, now=MON_MORNING)
    assert posts.calls[0][1] == 152


def test_orders_and_delivery_notes_incidents_are_tracked_independently(pg):
    """A simultaneous ORDER_* failure and DESADV_* failure are two DIFFERENT incidents
    (different channel/audience) — each gets its OWN grouped message, never merged."""
    _insert_at(pg, "ch1", "ORDER_ch1.txt", uploaded_at=MON_MORNING)
    _insert_at(pg, "ch2", "DESADV_ch2.txt", uploaded_at=MON_MORNING)
    posts = PostRecorder()
    confirm.sweep(
        pg, _cfg(),
        listdir=lambda: {"in": set(), "archCodex": set(),
                         "unconfirmed": {"ORDER_ch1.txt", "DESADV_ch2.txt"}},
        post=posts, now=MON_MORNING)
    assert len(posts.calls) == 2
    channels = {c for _h, c in posts.calls}
    assert channels == {152, 243}


# --- migration: never retroactively alert on pre-existing history --------------------

def test_pre_migration_rows_are_backfilled_as_already_imported_never_swept(pg, reapply_schema):
    """Every row confirmed-uploaded before this feature shipped predates import
    confirmation entirely — backfilling it straight to 'imported' (not NULL/pending)
    stops the first sweep after deploy from flooding Odoo with alerts for old, already-
    settled orders (see #151's design discussion)."""
    pg.execute("ALTER TABLE edi_sent DROP COLUMN import_status, "
              "DROP COLUMN import_confirmed_at, DROP COLUMN import_checked_at")
    pg.execute(
        """INSERT INTO edi_sent (customer_ean, delivery_date, content_sha256, filename,
                                 sent_at, uploaded_at)
           VALUES ('11', '04.08.2026', 'deadbeef', 'historical.txt',
                   now() - interval '30 days', now() - interval '30 days')""")
    reapply_schema()     # re-run the migration: columns come back, so does the backfill
    row = pg.execute(
        """SELECT import_status, import_confirmed_at, uploaded_at FROM edi_sent
            WHERE customer_ean = '11'""").fetchone()
    assert row[0] == "imported"
    assert row[1] is not None and row[1] == row[2]
    assert confirm.due_rows(pg, 5) == [], "a backfilled historical row must never be swept"


# --- #203: desadv_sent gets its own sweep coverage through in_DL ----------------------

def test_desadv_row_still_queued_in_in_dl_is_silent():
    """Normal waiting — DESADV rows check `in_DL`, not `in`, for their still-queued
    state (live-verified 2026-08-07: in_DL and in are siblings, not nested)."""
    row = {"id": 1, "filename": "DESADV_x.txt", "uploaded_at": None}
    dirs = {"in": set(), "in_DL": {"Z-DESADV_x.txt"}, "archCodex": set(),
           "unconfirmed": set()}
    assert confirm._decide(row, dirs, confirm.DESADV_LEDGER) is None


def test_desadv_row_in_archcodex_with_its_wire_z_prefix_is_imported():
    """R89: a DESADV upload's ON-WIRE name already carries Z- (unlike ORDER_ uploads,
    which get no prefix at write time) — the ledger's own filename column stays the
    human-facing base name."""
    row = {"id": 1, "filename": "DESADV_x.txt", "uploaded_at": None}
    dirs = {"in": set(), "in_DL": set(), "archCodex": {"Z-DESADV_x.txt"},
           "unconfirmed": set()}
    assert confirm._decide(row, dirs, confirm.DESADV_LEDGER) == "imported"


def test_desadv_row_in_archcodex_with_a_double_z_prefix_is_still_imported():
    """Defensive tolerance mirroring the ORDER_ side's own "with or without Z-" rule —
    Communicator's separate, uncontrolled rename job could in principle add a SECOND
    Z- on top of the one the upload itself already wrote."""
    row = {"id": 1, "filename": "DESADV_x.txt", "uploaded_at": None}
    dirs = {"in": set(), "in_DL": set(), "archCodex": {"Z-Z-DESADV_x.txt"},
           "unconfirmed": set()}
    assert confirm._decide(row, dirs, confirm.DESADV_LEDGER) == "imported"


def test_desadv_row_in_unconfirmed_checks_the_wire_name_not_the_base_name():
    row = {"id": 1, "filename": "DESADV_x.txt", "uploaded_at": None}
    dirs_bare = {"in": set(), "in_DL": set(), "archCodex": set(),
                "unconfirmed": {"DESADV_x.txt"}}
    # The bare (non-wire) name in unconfirmed does NOT match — only the actual on-wire
    # (Z-prefixed) name would ever legitimately land there.
    assert confirm._decide(row, dirs_bare, confirm.DESADV_LEDGER) == "unknown"
    dirs_wire = {"in": set(), "in_DL": set(), "archCodex": set(),
                "unconfirmed": {"Z-DESADV_x.txt"}}
    assert confirm._decide(row, dirs_wire, confirm.DESADV_LEDGER) == "failed"


def test_desadv_sweep_confirms_import_and_writes_desadv_sent_not_edi_sent(pg):
    rid = _insert_desadv_at(pg, "9", "DESADV_a.txt", uploaded_at=MON_MORNING)
    posts = PostRecorder()
    n = confirm.sweep(pg, _cfg(),
                      listdir=lambda: {"in": set(), "in_DL": set(),
                                       "archCodex": {"Z-DESADV_a.txt"},
                                       "unconfirmed": set()},
                      post=posts, now=MON_MORNING)
    assert n == 1
    assert posts.calls == []
    assert _desadv_status(pg, rid) == "imported"


def test_desadv_carryover_uses_dodaci_list_wording_not_objednavka(pg):
    _insert_desadv_at(pg, "9", "DESADV_c.txt", uploaded_at=MON_EVENING)
    posts = PostRecorder()
    confirm.sweep(pg, _cfg(),
                  listdir=lambda: {"in": set(), "in_DL": {"Z-DESADV_c.txt"},
                                   "archCodex": set(), "unconfirmed": set()},
                  post=posts, now=TUE_MORNING)
    assert len(posts.calls) == 1
    html = posts.calls[0][0]
    assert "dodací list" in html
    assert "objednávka" not in html and "objednávky" not in html


def test_desadv_and_edi_incidents_on_the_same_channel_stay_independent(pg):
    """A DESADV failure and an ORDER_ failure both routing to the SAME channel
    (delivery_notes_channel_id unset -> falls back to orders_channel_id) must still open
    TWO separate incidents (source-scoped), never merge into one grouped message."""
    _insert_at(pg, "e1", "ORDER_e1.txt", uploaded_at=MON_MORNING)
    _insert_desadv_at(pg, "d1", "DESADV_d1.txt", uploaded_at=MON_MORNING)
    posts = PostRecorder()
    confirm.sweep(
        pg, _cfg(delivery_notes_channel_id=0),
        listdir=lambda: {"in": set(), "in_DL": set(), "archCodex": set(),
                         "unconfirmed": {"ORDER_e1.txt", "Z-DESADV_d1.txt"}},
        post=posts, now=MON_MORNING)
    assert len(posts.calls) == 2, "one grouped message per SOURCE, not merged"
    edi_incident = confirm._open_incident(pg, 152, "failed", confirm.EDI_LEDGER)
    desadv_incident = confirm._open_incident(pg, 152, "failed", confirm.DESADV_LEDGER)
    assert edi_incident is not None and edi_incident["file_count"] == 1
    assert desadv_incident is not None and desadv_incident["file_count"] == 1


def test_one_listdir_call_serves_both_ledgers_in_one_sweep(pg):
    _insert(pg, "e2", "ORDER_e2.txt", uploaded_minutes_ago=90)
    _insert_desadv_at(pg, "d2", "DESADV_d2.txt", uploaded_at=MON_MORNING)
    calls = []

    def listdir():
        calls.append(1)
        return {"in": {"ORDER_e2.txt"}, "in_DL": {"Z-DESADV_d2.txt"},
               "archCodex": set(), "unconfirmed": set()}

    confirm.sweep(pg, _cfg(), listdir=listdir, post=PostRecorder(), now=MON_MORNING)
    assert len(calls) == 1


def test_plural_uses_delivery_note_nouns_for_desadv_source():
    assert confirm._plural(1, "desadv") == "dodací list"
    assert confirm._plural(2, "desadv") == "dodacie listy"
    assert confirm._plural(5, "desadv") == "dodacích listov"
    assert confirm._plural(1, "edi") == "objednávka"


def test_desadv_pre_migration_rows_are_backfilled_as_already_imported(pg, reapply_schema):
    """Same backfill contract desadv_sent's own #203 migration carries — mirrors the
    edi_sent test above."""
    pg.execute("ALTER TABLE desadv_sent DROP COLUMN import_status, "
              "DROP COLUMN import_confirmed_at, DROP COLUMN import_checked_at")
    pg.execute(
        """INSERT INTO desadv_sent (supplier_ean, doc_number, filename, sent_at,
                                    uploaded_at)
           VALUES ('12', 'HIST1', 'DESADV_historical.txt',
                   now() - interval '30 days', now() - interval '30 days')""")
    reapply_schema()
    row = pg.execute(
        """SELECT import_status, import_confirmed_at, uploaded_at FROM desadv_sent
            WHERE supplier_ean = '12'""").fetchone()
    assert row[0] == "imported"
    assert row[1] is not None and row[1] == row[2]
    assert confirm.due_rows(pg, 5, confirm.DESADV_LEDGER) == [], \
        "a backfilled historical DESADV row must never be swept"


# --- #255: a SAME-DAY rejection, detected via an evening activity-signal check --------
#
# ROZHODNUTÉ (owner, 2026-08-13): alert same-day ONLY when (a) import activity
# demonstrably happened that day (some OTHER file moved into archCodex that day) AND
# (b) our own file, uploaded BEFORE that activity, is still sitting in in/in_DL. No
# activity that day -> stay silent; the morning carryover check is unaffected.

MON_EARLY = datetime(2026, 8, 3, 7, 30, tzinfo=TZ)   # the activity-creating sweep
SAT_EVENING = datetime(2026, 8, 8, 18, 0, tzinfo=TZ)  # Saturday, past the default hour


def test_same_day_stuck_file_alerts_once_grouped_in_the_evening_given_real_activity(pg):
    """The reference scenario from the ticket: sklad accepts SOME files this morning
    (the activity signal) but silently leaves ONE of ours behind — must be caught the
    SAME evening, grouped exactly like a carryover alert, never one message per file."""
    a_id = _insert_at(pg, "255a", "ORDER_255a.txt", uploaded_at=MON_EARLY.replace(hour=6, minute=0))
    posts0 = PostRecorder()
    n0 = confirm.sweep(
        pg, _cfg(),
        listdir=lambda: {"in": set(), "archCodex": {"ORDER_255a.txt"}, "unconfirmed": set()},
        post=posts0, now=MON_EARLY)
    assert n0 == 1
    assert posts0.calls == [], "the activity-creating import itself must never alert"
    assert _status(pg, a_id) == "imported"

    # our own file, uploaded at 07:00 -- BEFORE the 07:30 activity above -- silently left
    # behind (still sitting in /in, never moved to unconfirmed either)
    b_id = _insert_at(pg, "255b", "ORDER_255b.txt", uploaded_at=MON_EARLY.replace(hour=7, minute=0))
    posts = PostRecorder()
    n = confirm.sweep(
        pg, _cfg(),
        listdir=lambda: {"in": {"ORDER_255b.txt"}, "archCodex": set(), "unconfirmed": set()},
        post=posts, now=MON_EVENING)
    assert n == 0, "a same-day-stuck row is never given a terminal status either"
    assert len(posts.calls) == 1, "exactly ONE grouped message, never one per file"
    assert "Codex" in posts.calls[0][0]
    assert _status(pg, b_id) is None, "still pending, so it self-heals if accepted later"


def test_no_activity_today_stays_silent_even_past_the_evening_hour(pg):
    """No file, ours or anyone else's, was ever confirmed imported today -- there is
    nothing to compare against, so the evening check has no basis to alert, exactly the
    same way the whole existing carryover check already stays silent on a normal day."""
    rid = _insert_at(pg, "255c", "ORDER_255c.txt", uploaded_at=MON_EVENING.replace(hour=9))
    posts = PostRecorder()
    n = confirm.sweep(
        pg, _cfg(),
        listdir=lambda: {"in": {"ORDER_255c.txt"}, "archCodex": set(), "unconfirmed": set()},
        post=posts, now=MON_EVENING)
    assert n == 0
    assert posts.calls == []
    assert _status(pg, rid) is None


def test_upload_after_todays_activity_stays_silent_the_normal_next_morning_case(pg):
    """The exact race the ROZHODNUTÉ names: sklad imports at 07:30, we upload at 07:31 --
    that upload is simply waiting for TOMORROW's click, not evidence of a rejection, and
    must stay silent even once the evening check is active."""
    a_id = _insert_at(pg, "255d", "ORDER_255d.txt", uploaded_at=MON_EARLY.replace(hour=6, minute=0))
    posts0 = PostRecorder()
    confirm.sweep(
        pg, _cfg(),
        listdir=lambda: {"in": set(), "archCodex": {"ORDER_255d.txt"}, "unconfirmed": set()},
        post=posts0, now=MON_EARLY)
    assert _status(pg, a_id) == "imported"

    # uploaded ONE MINUTE after the 07:30 activity above
    c_id = _insert_at(pg, "255e", "ORDER_255e.txt", uploaded_at=MON_EARLY.replace(minute=31))
    posts = PostRecorder()
    n = confirm.sweep(
        pg, _cfg(),
        listdir=lambda: {"in": {"ORDER_255e.txt"}, "archCodex": set(), "unconfirmed": set()},
        post=posts, now=MON_EVENING)
    assert n == 0
    assert posts.calls == [], "uploaded AFTER today's only import pass -- normal, silent"
    assert _status(pg, c_id) is None


def test_evening_check_stays_silent_on_saturday_even_with_a_genuine_activity_signal(pg):
    """Belt-and-braces, mirrors the existing Saturday/Sunday morning-check tests: the
    warehouse doesn't work weekends, so the evening check must stay off even if an
    activity signal technically exists that day."""
    a_id = _insert_at(pg, "255f", "ORDER_255f.txt", uploaded_at=SAT_EVENING.replace(hour=6))
    posts0 = PostRecorder()
    confirm.sweep(
        pg, _cfg(),
        listdir=lambda: {"in": set(), "archCodex": {"ORDER_255f.txt"}, "unconfirmed": set()},
        post=posts0, now=SAT_EVENING.replace(hour=7))
    assert _status(pg, a_id) == "imported"

    b_id = _insert_at(pg, "255g", "ORDER_255g.txt",
                      uploaded_at=SAT_EVENING.replace(hour=6, minute=30))
    posts = PostRecorder()
    n = confirm.sweep(
        pg, _cfg(),
        listdir=lambda: {"in": {"ORDER_255g.txt"}, "archCodex": set(), "unconfirmed": set()},
        post=posts, now=SAT_EVENING)
    assert n == 0
    assert posts.calls == []
    assert _status(pg, b_id) is None


def test_same_day_incident_does_not_repost_within_the_same_evening(pg):
    """A second evening sweep discovering the SAME condition folds into the already-open
    incident -- no second message, same dedup the morning carryover incident already
    gets (see test_dedup_no_repeat_message_while_the_carryover_incident_persists)."""
    a_id = _insert_at(pg, "255h", "ORDER_255h.txt", uploaded_at=MON_EARLY.replace(hour=6, minute=0))
    posts0 = PostRecorder()
    confirm.sweep(
        pg, _cfg(),
        listdir=lambda: {"in": set(), "archCodex": {"ORDER_255h.txt"}, "unconfirmed": set()},
        post=posts0, now=MON_EARLY)
    assert _status(pg, a_id) == "imported"

    b_id = _insert_at(pg, "255i", "ORDER_255i.txt", uploaded_at=MON_EARLY.replace(hour=7, minute=0))
    posts = PostRecorder()
    n = confirm.sweep(
        pg, _cfg(),
        listdir=lambda: {"in": {"ORDER_255i.txt"}, "archCodex": set(), "unconfirmed": set()},
        post=posts, now=MON_EVENING)
    assert n == 0
    assert len(posts.calls) == 1

    # a second sweep, one hour later, same evening, well under the 4h default reminder —
    # the row is still stuck, but must not repost
    pg.execute("UPDATE edi_sent SET import_checked_at = now() - interval '10 minutes' "
              "WHERE id = %s", (b_id,))
    later_same_evening = MON_EVENING.replace(hour=19)
    n2 = confirm.sweep(
        pg, _cfg(),
        listdir=lambda: {"in": {"ORDER_255i.txt"}, "archCodex": set(), "unconfirmed": set()},
        post=posts, now=later_same_evening)
    assert n2 == 0
    assert len(posts.calls) == 1, "folds into the SAME open incident, no repeat message"

    incident = confirm._open_incident(pg, 152, "carryover")
    assert incident["file_count"] == 1


def test_evening_check_hour_is_configurable(pg):
    """Mirrors the existing morning-hour test: before the configured evening hour,
    nothing is checked yet -- even with a genuine activity signal already present."""
    a_id = _insert_at(pg, "255j", "ORDER_255j.txt", uploaded_at=MON_EARLY.replace(hour=6, minute=0))
    posts0 = PostRecorder()
    confirm.sweep(
        pg, _cfg(),
        listdir=lambda: {"in": set(), "archCodex": {"ORDER_255j.txt"}, "unconfirmed": set()},
        post=posts0, now=MON_EARLY)
    assert _status(pg, a_id) == "imported"

    b_id = _insert_at(pg, "255k", "ORDER_255k.txt", uploaded_at=MON_EARLY.replace(hour=7, minute=0))
    before_default_hour = MON_EARLY.replace(hour=15, minute=0)  # 15:00, before the default 18:00
    posts = PostRecorder()
    n = confirm.sweep(
        pg, _cfg(),
        listdir=lambda: {"in": {"ORDER_255k.txt"}, "archCodex": set(), "unconfirmed": set()},
        post=posts, now=before_default_hour)
    assert n == 0
    assert posts.calls == [], "before the configured evening hour, nothing is checked yet"
    assert _status(pg, b_id) is None

    pg.execute("UPDATE edi_sent SET import_checked_at = now() - interval '10 minutes' "
              "WHERE id = %s", (b_id,))
    # `import_evening_check_hour` is read via getattr(cfg, ..., DEFAULT) in confirm.py
    # (not a declared Config dataclass field, deliberately -- see the #255 design
    # comment) -- setattr on the already-built Config instance is the override.
    cfg2 = _cfg()
    cfg2.import_evening_check_hour = 15
    n2 = confirm.sweep(
        pg, cfg2,
        listdir=lambda: {"in": {"ORDER_255k.txt"}, "archCodex": set(), "unconfirmed": set()},
        post=posts, now=before_default_hour)
    assert n2 == 0
    assert len(posts.calls) == 1, "the evening hour can be explicitly lowered"
