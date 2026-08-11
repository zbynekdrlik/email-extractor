"""Durable alert outbox (#239, requirement 3: a lost alert is invisible one layer up).
"""
from __future__ import annotations

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
