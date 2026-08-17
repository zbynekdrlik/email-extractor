"""Durable alert outbox (#239, requirement 3: a lost alert is invisible one layer up).
"""
from __future__ import annotations

from app import db
from app.orders import dl_alerts


def test_enqueue_then_flush_delivers_and_marks_delivered(pg):
    dl_alerts.enqueue(pg, 243, "dl_upload_failed", "<p>zlyhalo</p>", message_id="m1")
    posted = []

    def _post(c, h, **kw):
        posted.append((h, kw.get("channel_id")))
        return {"id": 1}

    n = dl_alerts.flush_pending(pg, cfg=None, post=_post)
    assert n == 1
    assert posted == [("<p>zlyhalo</p>", 243)]
    row = pg.execute(
        "SELECT delivered_at IS NOT NULL FROM pending_alerts WHERE message_id='m1'"
    ).fetchone()
    assert row[0] is True


def test_a_failed_post_leaves_the_alert_pending_for_the_next_sweep(pg):
    """Odoo down (#253 live right now) must never silently eat the alert — it stays
    pending and is retried on the NEXT flush call."""
    dl_alerts.enqueue(pg, 243, "dl_upload_failed", "<p>zlyhalo</p>", message_id="m1")

    def _raises(c, h, **kw):
        raise Exception("Odoo unreachable")

    n = dl_alerts.flush_pending(pg, cfg=None, post=_raises)
    assert n == 0
    row = pg.execute(
        "SELECT delivered_at, attempts, last_error FROM pending_alerts "
        "WHERE message_id='m1'").fetchone()
    assert row[0] is None
    assert row[1] == 1
    assert row[2] == "post raised"

    # A later sweep, once Odoo recovers, delivers it — nothing was lost.
    posted = []

    def _ok(c, h, **kw):
        posted.append(h)
        return {"id": 2}

    n2 = dl_alerts.flush_pending(pg, cfg=None, post=_ok)
    assert n2 == 1
    assert len(posted) == 1


def test_post_returning_none_odoo_not_configured_also_stays_pending(pg):
    dl_alerts.enqueue(pg, 243, "dl_stuck_classified", "<p>zaseknuté</p>", message_id="m2")
    n = dl_alerts.flush_pending(pg, cfg=None, post=lambda c, h, **kw: None)
    assert n == 0
    row = pg.execute(
        "SELECT delivered_at, last_error FROM pending_alerts WHERE message_id='m2'"
    ).fetchone()
    assert row[0] is None
    assert row[1] == "Odoo not configured"


def test_flush_groups_multiple_pending_alerts_of_the_same_kind_into_one_post(pg):
    """The 2026-08-05 precedent: 5 separate Odoo messages for one incident got deleted
    by the user as noise. Two DIFFERENT messages hitting the SAME kind must produce
    exactly ONE Odoo post, not two."""
    dl_alerts.enqueue(pg, 243, "dl_stuck_classified", "<p>A</p>", message_id="a")
    dl_alerts.enqueue(pg, 243, "dl_stuck_classified", "<p>B</p>", message_id="b")
    posted = []

    def _post(c, h, **kw):
        posted.append((h, kw.get("channel_id")))
        return {"id": 3}

    n = dl_alerts.flush_pending(pg, cfg=None, post=_post)
    assert n == 2
    assert len(posted) == 1, "two pending alerts of the same kind must be ONE Odoo post"
    assert "<p>A</p>" in posted[0][0] and "<p>B</p>" in posted[0][0]


def test_flush_never_mixes_two_different_kinds_into_one_post(pg):
    dl_alerts.enqueue(pg, 243, "dl_upload_failed", "<p>upload</p>", message_id="a")
    dl_alerts.enqueue(pg, 243, "dl_stuck_classified", "<p>stuck</p>", message_id="b")
    posted = []

    def _post(c, h, **kw):
        posted.append(h)
        return {"id": 4}

    dl_alerts.flush_pending(pg, cfg=None, post=_post)
    assert len(posted) == 2


def test_one_failing_group_never_blocks_a_different_group_in_the_same_sweep(pg):
    """Deep-review finding on #239's own PR: `flush_pending` reads every undelivered
    row in ONE query spanning potentially several (channel_id, kind) groups — a group
    whose post() raises must not prevent a DIFFERENT group, later in the same dict
    iteration, from being attempted and delivered in this SAME sweep call."""
    dl_alerts.enqueue(pg, 243, "dl_upload_failed", "<p>fails</p>", message_id="a")
    dl_alerts.enqueue(pg, 243, "dl_stuck_classified", "<p>succeeds</p>", message_id="b")
    posted = []

    def _post(c, h, **kw):
        if "fails" in h:
            raise Exception("Odoo unreachable for this group")
        posted.append(h)
        return {"id": 1}

    n = dl_alerts.flush_pending(pg, cfg=None, post=_post)
    assert n == 1, "only the succeeding group's row counts as delivered"
    assert posted == ["<p>succeeds</p>"]

    delivered = {row[0]: row[1] for row in pg.execute(
        "SELECT message_id, delivered_at IS NOT NULL FROM pending_alerts").fetchall()}
    assert delivered == {"a": False, "b": True}


def test_already_pending_dedupes_by_kind_and_message_id(pg):
    assert dl_alerts.already_pending(pg, "dl_stuck_classified", "m1") is False
    dl_alerts.enqueue(pg, 243, "dl_stuck_classified", "<p>x</p>", message_id="m1")
    assert dl_alerts.already_pending(pg, "dl_stuck_classified", "m1") is True
    # A DIFFERENT kind for the SAME message is a different concern, not deduped.
    assert dl_alerts.already_pending(pg, "dl_upload_failed", "m1") is False
    # Delivery does not un-dedupe — a message alerted once stays alerted.
    dl_alerts.flush_pending(pg, cfg=None, post=lambda c, h, **kw: {"id": 5})
    assert dl_alerts.already_pending(pg, "dl_stuck_classified", "m1") is True


def test_pending_count_reflects_only_undelivered_rows(pg):
    assert dl_alerts.pending_count(pg) == 0
    dl_alerts.enqueue(pg, 243, "dl_upload_failed", "<p>x</p>", message_id="m1")
    dl_alerts.enqueue(pg, 243, "dl_stuck_classified", "<p>y</p>", message_id="m2")
    assert dl_alerts.pending_count(pg) == 2
    dl_alerts.flush_pending(pg, cfg=None, post=lambda c, h, **kw: {"id": 6})
    assert dl_alerts.pending_count(pg) == 0


# --- #239 reopened, finding 1: the burst must land as ONE grouped post ------

def test_flush_waits_for_a_quiet_period_before_delivering_a_burst(pg):
    """Production hits `flush_pending` on almost every worker tick — enqueuing rows one
    at a time as an ORION outage claims one DL message after another used to produce
    one Odoo post PER ROW, the exact 18:18 flood shape a real incident already got
    deleted as spam. `quiet_seconds>0` makes flush wait until NO NEW alert of that
    (channel, kind) has arrived recently, so a burst is always delivered as ONE grouped
    post — regardless of exactly when the caller happens to call flush relative to the
    burst."""
    dl_alerts.enqueue(pg, 243, "dl_upload_failed", "<p>A</p>", message_id="a")
    posted = []

    def _post(c, h, **kw):
        posted.append(h)
        return {"id": 1}

    n = dl_alerts.flush_pending(pg, cfg=None, post=_post, quiet_seconds=30)
    assert n == 0, "still within the quiet window — must wait for more of the burst"
    assert posted == []

    # A second failure lands 5s "later" — still inside the same quiet window (extends it).
    pg.execute("UPDATE pending_alerts SET created_at = created_at - interval '5 seconds' "
              "WHERE message_id = 'a'")
    dl_alerts.enqueue(pg, 243, "dl_upload_failed", "<p>B</p>", message_id="b")
    n = dl_alerts.flush_pending(pg, cfg=None, post=_post, quiet_seconds=30)
    assert n == 0, "b just arrived — the group is still not quiet"
    assert posted == []

    # No further arrivals; once BOTH rows are older than the quiet window, one post.
    pg.execute("UPDATE pending_alerts SET created_at = created_at - interval '31 seconds'")
    n = dl_alerts.flush_pending(pg, cfg=None, post=_post, quiet_seconds=30)
    assert n == 2
    assert len(posted) == 1
    assert "<p>A</p>" in posted[0] and "<p>B</p>" in posted[0]


def test_quiet_seconds_zero_is_the_unchanged_immediate_behaviour(pg):
    """The default (0) must behave exactly like before this ticket — every existing
    caller/test relies on immediate delivery."""
    dl_alerts.enqueue(pg, 243, "dl_upload_failed", "<p>x</p>", message_id="m1")
    posted = []
    n = dl_alerts.flush_pending(pg, cfg=None, post=lambda c, h, **kw: posted.append(h) or
                                {"id": 1})
    assert n == 1
    assert posted == ["<p>x</p>"]


# --- #239 reopened, finding 3: the dedup must not be permanent -------------

def test_already_pending_a_delivered_row_expires_after_the_window_so_a_reprocess_can_realert(pg):
    """The old permanent dedup assumed a stuck-classified message can never
    structurally re-enter its own sweep's candidate set without an 'unusual manual
    reset' — the dashboard's one-click reprocess IS exactly that reset. Once the first
    alert has been DELIVERED, a dedup entry older than the window must stop suppressing
    a genuinely fresh occurrence.

    #327: this test (and the default-window one below) used to enqueue an UNDELIVERED row
    and assert it stops deduping past the window — that encoded the exact bug #327 fixes.
    The window now governs DELIVERED rows only, so both are re-scoped to deliver first;
    an UNDELIVERED row deduping regardless of age has its own dedicated test above."""
    dl_alerts.enqueue(pg, 243, "dl_stuck_classified", "<p>x</p>", message_id="m1")
    dl_alerts.flush_pending(pg, cfg=None, post=lambda c, h, **kw: {"id": 1})  # delivered
    assert dl_alerts.already_pending(pg, "dl_stuck_classified", "m1",
                                     window_hours=4) is True
    pg.execute("UPDATE pending_alerts SET created_at = created_at - interval '5 hours'")
    assert dl_alerts.already_pending(pg, "dl_stuck_classified", "m1",
                                     window_hours=4) is False


def test_already_pending_default_window_is_four_hours_for_a_delivered_row(pg):
    dl_alerts.enqueue(pg, 243, "dl_stuck_classified", "<p>x</p>", message_id="m1")
    dl_alerts.flush_pending(pg, cfg=None, post=lambda c, h, **kw: {"id": 1})  # delivered
    assert dl_alerts.already_pending(pg, "dl_stuck_classified", "m1") is True
    pg.execute("UPDATE pending_alerts SET created_at = created_at - interval '4 hours "
              "1 minute'")
    assert dl_alerts.already_pending(pg, "dl_stuck_classified", "m1") is False


def test_already_pending_a_held_then_delivered_row_still_dedupes_even_though_created_at_is_stale(pg):
    """#334: the live 2026-08-16 21:42 incident. A row HELD for days at channel_id=0 (the
    #310/#332 backlog, no ops channel configured yet) then finally delivered has `created_at`
    far outside the 4h window — but `flush_pending()` only ever sets `delivered_at`, it never
    touches `created_at`. The dedup window for a delivered row must anchor on `delivered_at`
    (the ACTUAL delivery), NOT `created_at`, or the just-delivered row immediately stops
    deduping and the #308 sweep re-enqueues the same `message_id` ~15s later — producing a
    duplicate grouped ops post to the channel, repeating every ~4h. Anchored on `created_at`
    (the bug) this returns False; anchored on `delivered_at` (the fix) it returns True."""
    pg.execute(
        "INSERT INTO pending_alerts (channel_id, kind, message_id, body_html, "
        "created_at, delivered_at) VALUES "
        "(243, 'human_processing_review', 'm1', '<p>held then delivered</p>', "
        "now() - interval '3 days', now())")
    assert dl_alerts.already_pending(pg, "human_processing_review", "m1") is True


# --- #327: an UNDELIVERED row dedupes regardless of age (the 4h window is delivered-only) --

def test_already_pending_dedupes_an_undelivered_alert_regardless_of_age(pg):
    """#327: a HELD/undelivered alert older than DEDUP_WINDOW_HOURS must STILL dedupe —
    there is nothing to remind about while the very first alert has not even gone out.
    Before this fix the window expired and the #308 sweep re-enqueued the same message
    every ~4h, accumulating ~6 duplicate held channel-0 rows/day per message (live #319
    finding: 65 held rows for only 10 distinct messages). The 4h window survives ONLY for
    DELIVERED rows (see the delivered-row tests above)."""
    dl_alerts.enqueue(pg, 0, "human_processing_review", "<p>held</p>", message_id="m1")
    # age it well past the dedup window; STILL undelivered (channel-0 hold, no ops channel)
    pg.execute("UPDATE pending_alerts SET created_at = now() - interval '9 hours' "
               "WHERE message_id = 'm1'")
    assert dl_alerts.already_pending(pg, "human_processing_review", "m1") is True


# --- #239 reopened, finding 4: bounded growth -------------------------------

def test_flush_stops_retrying_a_row_past_the_attempt_cap(pg, monkeypatch):
    """A durably-broken Odoo config must not be hammered forever — a row past
    MAX_FLUSH_ATTEMPTS stops being actively retried, but stays counted (never silently
    dropped, only no longer hammered)."""
    monkeypatch.setattr(dl_alerts, "MAX_FLUSH_ATTEMPTS", 3)
    dl_alerts.enqueue(pg, 243, "dl_upload_failed", "<p>x</p>", message_id="m1")
    pg.execute("UPDATE pending_alerts SET attempts = 3 WHERE message_id = 'm1'")
    posted = []
    n = dl_alerts.flush_pending(pg, cfg=None, post=lambda c, h, **kw: posted.append(h) or
                                {"id": 1})
    assert n == 0
    assert posted == [], "a row at/over the cap must never be retried again"
    assert dl_alerts.pending_count(pg) == 1, "still visible — never silently dropped"


def test_prune_delivered_removes_only_old_delivered_rows(pg):
    dl_alerts.enqueue(pg, 243, "dl_upload_failed", "<p>old</p>", message_id="old")
    dl_alerts.enqueue(pg, 243, "dl_upload_failed", "<p>recent</p>", message_id="recent")
    dl_alerts.enqueue(pg, 243, "dl_upload_failed", "<p>pending</p>", message_id="pending")
    dl_alerts.flush_pending(pg, cfg=None, post=lambda c, h, **kw: {"id": 1})
    # the "pending" row's post below fails, so it stays undelivered
    pg.execute("UPDATE pending_alerts SET delivered_at = NULL, channel_id = 999 "
              "WHERE message_id = 'pending'")
    pg.execute("UPDATE pending_alerts SET delivered_at = now() - interval '31 days' "
              "WHERE message_id = 'old'")
    pg.execute("UPDATE pending_alerts SET delivered_at = now() - interval '1 day' "
              "WHERE message_id = 'recent'")
    n = dl_alerts.prune_delivered(pg, older_than_days=30)
    assert n == 1
    remaining = {row[0] for row in pg.execute(
        "SELECT message_id FROM pending_alerts").fetchall()}
    assert remaining == {"recent", "pending"}


def test_prune_delivered_never_touches_an_undelivered_row_however_old(pg):
    dl_alerts.enqueue(pg, 243, "dl_upload_failed", "<p>x</p>", message_id="m1")
    pg.execute("UPDATE pending_alerts SET created_at = created_at - interval '90 days'")
    n = dl_alerts.prune_delivered(pg, older_than_days=30)
    assert n == 0
    assert dl_alerts.pending_count(pg) == 1


# --- #319: retention of HELD (channel_id=0) operator alerts + re-route on config ------

def test_purge_held_removes_only_old_held_channel_0_undelivered_rows(pg):
    """#319: a held operator alert (channel_id=0, #310 hold mechanism) older than the
    retention window is purged; a fresh held row survives (so a later-configured ops
    channel still gets recent history); a REAL-channel undelivered row (a genuine Odoo
    delivery failure) is NEVER purged, however old; a DELIVERED row (prune_delivered's
    job) is left alone."""
    # (a) old held channel-0 row → purged
    dl_alerts.enqueue(pg, 0, "spend_cap", "<p>old held</p>", message_id="old_held")
    pg.execute("UPDATE pending_alerts SET created_at = now() - interval '31 days' "
               "WHERE message_id = 'old_held'")
    # (b) fresh held channel-0 row (5 days) → survives
    dl_alerts.enqueue(pg, 0, "spend_cap", "<p>fresh held</p>", message_id="fresh_held")
    pg.execute("UPDATE pending_alerts SET created_at = now() - interval '5 days' "
               "WHERE message_id = 'fresh_held'")
    # (c) old UNDELIVERED real-channel row → never purged (genuine delivery failure)
    dl_alerts.enqueue(pg, 243, "dl_upload_failed", "<p>real fail</p>", message_id="real_fail")
    pg.execute("UPDATE pending_alerts SET created_at = now() - interval '90 days' "
               "WHERE message_id = 'real_fail'")
    # (d) old DELIVERED channel-0 row → prune_delivered owns it, purge_held leaves it
    dl_alerts.enqueue(pg, 0, "spend_cap", "<p>delivered held</p>", message_id="delivered_held")
    pg.execute("UPDATE pending_alerts SET created_at = now() - interval '40 days', "
               "delivered_at = now() - interval '39 days' WHERE message_id = 'delivered_held'")

    n = dl_alerts.purge_held(pg, older_than_days=30)
    assert n == 1
    survivors = {row[0] for row in pg.execute(
        "SELECT message_id FROM pending_alerts").fetchall()}
    assert survivors == {"fresh_held", "real_fail", "delivered_held"}


def test_purge_held_default_window_is_thirty_days(pg):
    assert dl_alerts.HELD_RETENTION_DAYS == 30
    dl_alerts.enqueue(pg, 0, "spend_cap", "<p>x</p>", message_id="m1")
    pg.execute("UPDATE pending_alerts SET created_at = now() - interval '31 days'")
    assert dl_alerts.purge_held(pg) == 1  # default older_than_days = HELD_RETENTION_DAYS


def test_a_held_channel_0_row_delivers_to_the_ops_channel_once_it_is_configured(pg):
    """#319: the other half of 'nothing foreclosed'. A row held on channel 0 (no ops
    channel configured yet) must actually reach the ops channel once `ops_channel_id`
    is set — flush_pending re-derives the CURRENT ops channel for a channel-0 group,
    rather than reading the frozen 0 off the row and skipping it forever."""
    import types
    dl_alerts.enqueue(pg, 0, "spend_cap", "<p>ops alert</p>", message_id="held1")
    posted = []

    def _post(c, h, **kw):
        posted.append(kw.get("channel_id"))
        return {"id": 1}

    # ops channel still unset → held, never delivered (unchanged pre-#319 behaviour)
    cfg_unset = types.SimpleNamespace(ops_channel_id=0)
    assert dl_alerts.flush_pending(pg, cfg=cfg_unset, post=_post) == 0
    assert posted == []
    assert pg.execute(
        "SELECT delivered_at IS NULL FROM pending_alerts WHERE message_id='held1'"
    ).fetchone()[0] is True

    # ops channel configured later → the ALREADY-held row delivers to it
    cfg_set = types.SimpleNamespace(ops_channel_id=555)
    assert dl_alerts.flush_pending(pg, cfg=cfg_set, post=_post) == 1
    assert posted == [555]
    assert pg.execute(
        "SELECT delivered_at IS NOT NULL FROM pending_alerts WHERE message_id='held1'"
    ).fetchone()[0] is True


# --- #327 revision 3: one-time cleanup of the accumulated duplicate held alerts -------

def test_dedup_held_alerts_migration_keeps_oldest_and_deletes_newer_held_duplicates(pg):
    """`db.DEDUP_HELD_ALERTS` (revision 3): the one-time cleanup of the duplicate held
    channel-0 rows #327's pre-fix bug accumulated (live #319: 65 rows for 10 messages).
    Keeps the OLDEST row (min id) per (kind, message_id); deletes the newer held
    duplicates. NARROW like purge_held (#319): a real-channel undelivered row, a delivered
    row, and a NULL-message_id row (never deduped in the first place) are all untouched."""
    # (a) three held channel-0 duplicates of the SAME message → keep the oldest, delete 2
    dl_alerts.enqueue(pg, 0, "human_processing_review", "<p>dup1</p>", message_id="m1")
    dl_alerts.enqueue(pg, 0, "human_processing_review", "<p>dup2</p>", message_id="m1")
    dl_alerts.enqueue(pg, 0, "human_processing_review", "<p>dup3</p>", message_id="m1")
    # (b) a held channel-0 row of a DIFFERENT message → its own group, survives
    dl_alerts.enqueue(pg, 0, "human_processing_review", "<p>other</p>", message_id="m2")
    # (c) two NULL-message_id held rows (e.g. spend_cap) → never deduped, untouched
    dl_alerts.enqueue(pg, 0, "spend_cap", "<p>spend1</p>")
    dl_alerts.enqueue(pg, 0, "spend_cap", "<p>spend2</p>")
    # (d) two REAL-channel undelivered rows (genuine delivery failures) → never touched
    dl_alerts.enqueue(pg, 243, "dl_upload_failed", "<p>real1</p>", message_id="m1")
    dl_alerts.enqueue(pg, 243, "dl_upload_failed", "<p>real2</p>", message_id="m1")
    # (e) two DELIVERED channel-0 duplicates → prune_delivered's domain, not this cleanup's
    dl_alerts.enqueue(pg, 0, "human_processing_review", "<p>del1</p>", message_id="m3")
    dl_alerts.enqueue(pg, 0, "human_processing_review", "<p>del2</p>", message_id="m3")
    pg.execute("UPDATE pending_alerts SET delivered_at = now() WHERE message_id = 'm3'")

    before = pg.execute("SELECT count(*) FROM pending_alerts").fetchone()[0]
    for stmt in db.DEDUP_HELD_ALERTS:
        pg.execute(stmt)
    kept = {r[0] for r in pg.execute(
        "SELECT body_html FROM pending_alerts ORDER BY id").fetchall()}
    after = pg.execute("SELECT count(*) FROM pending_alerts").fetchone()[0]

    assert "<p>dup1</p>" in kept                                   # oldest held m1 kept
    assert "<p>dup2</p>" not in kept and "<p>dup3</p>" not in kept  # newer held m1 duplicates gone
    assert "<p>other</p>" in kept                                  # different message survives
    assert "<p>spend1</p>" in kept and "<p>spend2</p>" in kept     # NULL message_id untouched
    assert "<p>real1</p>" in kept and "<p>real2</p>" in kept       # real-channel undelivered untouched
    assert "<p>del1</p>" in kept and "<p>del2</p>" in kept         # delivered rows untouched
    assert before - after == 2, "exactly the 2 newer held m1 duplicates removed"
