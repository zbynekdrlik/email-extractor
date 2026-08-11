"""Durable alert outbox for DL processing-health failures (#239).

Requirement 3 of #239: an alert that cannot be delivered (Odoo down/misconfigured, a
transient HTTP failure) must be RECORDED and RETRIED, never silently dropped — the
exact one-layer-up version of the failure this whole ticket exists to fix. `_post()`
in `dl_worker.py` is fire-and-forget: it tries once, logs an exception on failure, and
never tries again — fine for a routine review notice (the document sitting in review
is itself visible), but NOT ACCEPTABLE for the two classes #239 adds callers for here,
which have NO other durable trace at all if the alert itself is lost:

- `dl_upload_failed` — an ORION upload failed and the retry window was exhausted or the
  failure was non-transient (`dl_worker._process_document`).
- `dl_stuck_classified` — a message was classified `dodacie_listy` and never even got a
  first processing attempt within a generous threshold
  (`dl_worker.stuck_classified_sweep`).

`enqueue()` writes the alert FIRST, durably, before any delivery attempt is ever made —
so even a crash between enqueue and the next flush loses nothing (the row is already on
disk). `flush_pending()` runs on the SAME ~15s worker tick `confirm.sweep` already runs
on (`worker.run_forever`): it groups every undelivered row by `(channel_id, kind)` into
ONE combined Odoo message — never one message per item, the precedent being the
2026-08-05 flood of 5 separate "stuck" alerts the user deleted (`.claude/rules/
n8n-workflow-edits.md`) — and marks the WHOLE group delivered only once the post
genuinely succeeds. `post(...)` returning `None` (Odoo not configured) is treated
exactly like a raised exception: not delivered, retried next sweep. Mirrors
`confirm.py`'s own `_handle_group`/`sweep()` shape, simplified: these are one-shot event
alerts (an upload failed, a message is stuck), not "still open until resolved"
carryover-style incidents, so there is no open/close state machine here — just
"delivered" or "still pending".
"""
from __future__ import annotations

import logging

from . import report

log = logging.getLogger("orders.dl_alerts")


def enqueue(conn, channel_id: int, kind: str, body_html: str,
           message_id: str = "") -> None:
    """Durably record an alert BEFORE any delivery is attempted. Never raises on a
    normal INSERT failure path other than a genuine DB error (there is nothing safer to
    fall back to — if this write fails the caller's own except/log around it is what
    surfaces the problem, same as any other DB write in this codebase)."""
    conn.execute(
        """INSERT INTO pending_alerts (channel_id, kind, message_id, body_html)
           VALUES (%s, %s, %s, %s)""",
        (int(channel_id), kind, str(message_id or "") or None, body_html))


def already_pending(conn, kind: str, message_id: str) -> bool:
    """True when THIS message already has an alert of this kind on record (delivered or
    not) — the dedup a persistently-stuck message needs so a sweep that keeps
    rediscovering it does not enqueue (and eventually post) a fresh copy every ~15s.
    Deliberately checks ALL rows, not just undelivered ones: once alerted, a message
    should not be re-alerted for the SAME condition even after delivery succeeds.

    Deep-review finding on this ticket's own PR: this dedup is PERMANENT, by design —
    once a `(kind, message_id)` pair has ever been alerted, it never fires again for
    that pair, even if the condition genuinely recurs much later (e.g. a message stuck
    once, later fixed, then somehow stuck again). Accepted tradeoff for the two kinds
    this currently guards (`dl_upload_failed`/`dl_stuck_classified`, both keyed to a
    SINGLE message that structurally cannot re-enter its own sweep's candidate set
    without an unusual manual reset) — a future kind whose condition genuinely CAN
    recur for the same key should either dedupe on something that changes per
    occurrence (e.g. include a document/run id in the key) or add an age bound here."""
    if not message_id:
        return False
    row = conn.execute(
        "SELECT 1 FROM pending_alerts WHERE kind = %s AND message_id = %s LIMIT 1",
        (kind, message_id)).fetchone()
    return row is not None


def flush_pending(conn, cfg, post=None, limit: int = 50) -> int:
    """Deliver every undelivered alert, grouped by `(channel_id, kind)` into ONE Odoo
    message per group. Returns how many ROWS were delivered this pass (0 on a clean
    sweep with nothing pending, or when every group's post attempt failed — never
    raises, mirrors `confirm.sweep`'s own "a notification failure must never break the
    worker loop" discipline)."""
    post = post or (lambda c, html, **kw: report.post_from_config(
        c, html, channel_id=kw.get("channel_id")))
    rows = conn.execute(
        """SELECT id, channel_id, kind, body_html FROM pending_alerts
            WHERE delivered_at IS NULL ORDER BY id LIMIT %s""", (limit,)).fetchall()
    if not rows:
        return 0
    groups: dict[tuple[int, str], list[tuple[int, str]]] = {}
    for rid, channel_id, kind, body_html in rows:
        groups.setdefault((int(channel_id), kind), []).append((int(rid), body_html))
    delivered = 0
    for (channel_id, kind), items in groups.items():
        html = "".join(body for _rid, body in items)
        ids = [rid for rid, _ in items]
        try:
            result = post(cfg, html, channel_id=channel_id)
        except Exception:
            log.exception("delivering %d pending %s alert(s) failed — will retry next "
                          "sweep", len(items), kind)
            conn.execute(
                "UPDATE pending_alerts SET attempts = attempts + 1, "
                "last_error = %s WHERE id = ANY(%s)", ("post raised", ids))
            continue
        if result is None:
            log.warning("%d pending %s alert(s) not delivered (Odoo not configured?) "
                       "— will retry next sweep", len(items), kind)
            conn.execute(
                "UPDATE pending_alerts SET attempts = attempts + 1, "
                "last_error = %s WHERE id = ANY(%s)", ("Odoo not configured", ids))
            continue
        conn.execute(
            "UPDATE pending_alerts SET delivered_at = now() WHERE id = ANY(%s)", (ids,))
        delivered += len(items)
        log.info("delivered %d pending %s alert(s) to channel %s", len(items), kind,
                 channel_id)
    return delivered


def pending_count(conn) -> int:
    """Current-state gauge for the dashboard/digest (#239 requirement 1: every one of
    the five classes must be queryable/visible there, not just Odoo-alerted)."""
    row = conn.execute(
        "SELECT count(*) FROM pending_alerts WHERE delivered_at IS NULL").fetchone()
    return int(row[0] or 0)
