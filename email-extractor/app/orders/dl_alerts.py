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

**#239 reopened, finding 1 — the grouping above was defeated in production.**
`worker.run_forever` calls `flush_pending()` on almost every loop iteration, including
the tight `continue` cycle it runs while messages keep getting claimed (no `sleep`
between them) — so a burst of several back-to-back DL failures (an ORION outage
claiming one message after another) got flushed ONE ROW AT A TIME: round N enqueues an
alert, round N+1 flushes it before a second alert has a chance to land, producing the
exact per-file flood shape a real incident already got deleted as spam. `quiet_seconds`
(default 0, unchanged for every direct caller/test) makes a `(channel_id, kind)` group
wait until its NEWEST undelivered row is older than the window before it is ever
delivered — so however the caller happens to interleave `flush_pending()` calls with
the burst, the whole burst still lands as ONE grouped post once things go quiet.
`worker.run_forever` passes `FLUSH_QUIET_SECONDS`.

**#239 reopened, finding 3 — the dedup was "permanent by design", and that assumption
was wrong.** `already_pending()` used to check EVERY row ever inserted for a
`(kind, message_id)` pair, forever — on the theory that the two kinds using it
(`dl_upload_failed`, `dl_stuck_classified`) can never structurally re-enter their own
sweep's candidate set without an "unusual manual reset". The dashboard's one-click
`POST /api/message/<id>/reprocess` IS exactly that reset (re-queues the message with no
new `order_runs` row) — a message reprocessed and still not picked up 30+ minutes later
genuinely re-qualifies for `stuck_classified_sweep`'s candidate query, but the old
permanent dedup would silently suppress the fresh alert. Reworked to a bounded RECENCY
window (`DEDUP_WINDOW_HOURS`, 4h — the same value `confirm.py`'s own
`DEFAULT_REMINDER_HOURS` uses for its "still a problem, remind again" semantics, not a
second invented policy) — a genuinely new occurrence, whether reprocess-triggered or
just a message that stayed stuck past the window, gets alerted again.

**#239 reopened, finding 4 — unbounded growth.** No delivered row was ever removed
(the table only ever grows), and a permanently-broken Odoo config retried every single
worker tick forever with no ceiling. `prune_delivered()` removes delivered rows past a
retention window (the ONE state nothing needs kept indefinitely); `flush_pending()`
only selects rows under `MAX_FLUSH_ATTEMPTS` — a row past the cap simply stops being
actively retried (no more hammering a dead endpoint) but stays on record and still
counts in `pending_count()`/`dl_current_health`, so it is never silently dropped, only
no longer hammered.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from . import report

log = logging.getLogger("orders.dl_alerts")

# #239 finding 1 (reopened): production calls flush_pending() on almost every worker
# tick — this is the window a burst of same-kind alerts is given to accumulate before
# the group is ever posted. See the module docstring's "finding 1" section.
FLUSH_QUIET_SECONDS = 30

# #239 finding 3 (reopened): a dedup entry older than this no longer suppresses a fresh
# occurrence — mirrors confirm.py's own DEFAULT_REMINDER_HOURS. See the module
# docstring's "finding 3" section.
DEDUP_WINDOW_HOURS = 4

# #239 finding 4 (reopened): a row is no longer actively retried once it passes this
# many delivery attempts — it stays on record (still counted, never deleted) but stops
# hammering a durably broken Odoo config. See the module docstring's "finding 4" section.
MAX_FLUSH_ATTEMPTS = 200

# #239 finding 4 (reopened): how long a DELIVERED row is kept before prune_delivered()
# removes it. An undelivered row with a REAL channel is NEVER pruned, however old (a
# genuine delivery failure must never be silently dropped); a HELD channel-0 row has its
# own, separate retention below (#319).
DELIVERED_RETENTION_DAYS = 30

# #319: how long a HELD operator alert (channel_id = 0, still undelivered — the #310
# "no ops channel configured yet" mechanism) is kept before purge_held() removes it. These
# rows have no delivery target, so without retention they grow unbounded (~1-2/day).
# Conservative — a fresh held row survives, so if an ops channel is configured later,
# recent history still reaches it (flush_pending re-routes a still-held channel-0 row to
# the current ops channel). A REAL-channel undelivered row is never touched by this.
HELD_RETENTION_DAYS = 30


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


def already_pending(conn, kind: str, message_id: str,
                    window_hours: int = DEDUP_WINDOW_HOURS) -> bool:
    """True when THIS message already has an alert of this kind on record WITHIN THE
    LAST `window_hours` (delivered or not) — the dedup a persistently-stuck message
    needs so a sweep that keeps rediscovering it does not enqueue (and eventually post)
    a fresh copy every ~15s.

    **#239 reopened, finding 3: this dedup used to be PERMANENT (never expires) — that
    was wrong.** The original design assumed the two kinds this guards
    (`dl_upload_failed`/`dl_stuck_classified`) can never structurally re-enter their own
    sweep's candidate set without an "unusual manual reset" — but the dashboard's
    one-click `POST /api/message/<id>/reprocess` IS exactly that reset, and it needs no
    special-casing to make the permanent assumption wrong: any message that stays stuck
    (reprocessed or not) past a bounded window deserves a fresh alert, not permanent
    silence. Bounded to `window_hours` instead — same 4h value `confirm.py`'s own
    `DEFAULT_REMINDER_HOURS` uses for its own "still a problem, remind again" semantics."""
    if not message_id:
        return False
    row = conn.execute(
        "SELECT 1 FROM pending_alerts WHERE kind = %s AND message_id = %s "
        "AND created_at > now() - make_interval(hours => %s) LIMIT 1",
        (kind, message_id, max(1, int(window_hours)))).fetchone()
    return row is not None


def flush_pending(conn, cfg, post=None, limit: int = 50,
                  quiet_seconds: int = 0) -> int:
    """Deliver every undelivered alert, grouped by `(channel_id, kind)` into ONE Odoo
    message per group. Returns how many ROWS were delivered this pass (0 on a clean
    sweep with nothing pending, when every group's post attempt failed, or when every
    group is still within its `quiet_seconds` window — never raises, mirrors
    `confirm.sweep`'s own "a notification failure must never break the worker loop"
    discipline).

    `quiet_seconds` (#239 finding 1, reopened; default 0 — unchanged for every direct
    caller, including every existing test) makes a group wait until its NEWEST
    undelivered row is older than the window before it is delivered at all, so a burst
    of same-kind alerts always lands as ONE grouped post regardless of exactly when the
    caller happens to call this relative to the burst. See the module docstring.

    Only rows under `MAX_FLUSH_ATTEMPTS` are ever selected (#239 finding 4, reopened) —
    a row past the cap simply stops being actively retried; it stays on record and
    still counts in `pending_count()`."""
    post = post or (lambda c, html, **kw: report.post_from_config(
        c, html, channel_id=kw.get("channel_id")))
    rows = conn.execute(
        """SELECT id, channel_id, kind, body_html, created_at FROM pending_alerts
            WHERE delivered_at IS NULL AND attempts < %s
            ORDER BY id LIMIT %s""", (MAX_FLUSH_ATTEMPTS, limit)).fetchall()
    if not rows:
        return 0
    groups: dict[tuple[int, str], list[tuple[int, str, datetime]]] = {}
    for rid, channel_id, kind, body_html, created_at in rows:
        groups.setdefault((int(channel_id), kind), []).append(
            (int(rid), body_html, created_at))
    now = datetime.now(UTC)
    delivered = 0
    for (channel_id, kind), items in groups.items():
        # #310: a channel_id of 0 means "operator alert, but no ops channel configured
        # yet" (`report.ops_channel` returned 0 at enqueue time). #319: re-derive the
        # CURRENT ops channel here (`target`) instead of reading the frozen 0 off the row
        # and skipping it forever — so once `ops_channel_id` IS configured, an already-held
        # row finally DELIVERS to it (the "nothing foreclosed" half of #319's retention: a
        # held row exists precisely to reach the ops channel once one is set). While the
        # ops channel is still unset (`target` == 0) the row STAYS HELD — passing 0 to
        # `post_from_config` would fall back to `orders_channel_id` (152, the sales
        # channel), the exact misroute #310 exists to fix — undelivered and counted in
        # `pending_count()` (visible on the dashboard), attempts untouched. Only an
        # operator-kind alert ever enqueues channel 0; every warehouse caller passes a real
        # channel, so `target` == `channel_id` for those.
        target = channel_id or report.ops_channel(cfg)
        if not target:
            continue
        newest = max(created_at for _rid, _body, created_at in items)
        if quiet_seconds and (now - newest).total_seconds() < quiet_seconds:
            # Still receiving new alerts of this exact (channel, kind) — wait for the
            # burst to go quiet so it lands as ONE grouped post, never one per row.
            continue
        html = "".join(body for _rid, body, _created_at in items)
        ids = [rid for rid, _body, _created_at in items]
        try:
            result = post(cfg, html, channel_id=target)
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
                 target)
    return delivered


def pending_count(conn) -> int:
    """Current-state gauge for the dashboard/digest (#239 requirement 1: every one of
    the five classes must be queryable/visible there, not just Odoo-alerted). Counts
    EVERY undelivered row, including one past `MAX_FLUSH_ATTEMPTS` (#239 finding 4,
    reopened) — a row that stopped being actively retried must never disappear from
    this gauge, or it becomes silently invisible again, the exact failure this whole
    table exists to prevent."""
    row = conn.execute(
        "SELECT count(*) FROM pending_alerts WHERE delivered_at IS NULL").fetchone()
    return int(row[0] or 0)


def prune_delivered(conn, older_than_days: int = DELIVERED_RETENTION_DAYS) -> int:
    """#239 finding 4 (reopened): delete DELIVERED rows past the retention window — the
    ONLY state `pending_alerts` otherwise keeps forever. NEVER touches an undelivered
    row, however old or however many attempts it has — deleting one of those would be
    silent loss, exactly what this table exists to prevent. Returns how many rows were
    removed."""
    rows = conn.execute(
        "DELETE FROM pending_alerts WHERE delivered_at IS NOT NULL "
        "AND delivered_at < now() - make_interval(days => %s) RETURNING id",
        (max(1, int(older_than_days)),)).fetchall()
    return len(rows)


def purge_held(conn, older_than_days: int = HELD_RETENTION_DAYS) -> int:
    """#319: delete HELD operator alerts (channel_id = 0, still undelivered — the #310
    "no ops channel configured yet" mechanism) past the retention window. These rows
    have no delivery target, so without retention they grow unbounded (~1-2/day, live
    verified). Deliberately NARROW, unlike a "delete every old undelivered row" sweep:

    - `channel_id = 0` only — a REAL-channel undelivered row (a genuine Odoo delivery
      failure) is NEVER touched, however old; deleting one would be exactly the silent
      loss this whole outbox exists to prevent (`prune_delivered`'s docstring makes the
      same promise for its own scope).
    - `delivered_at IS NULL` only — a delivered channel-0 row belongs to
      `prune_delivered()`, not here.

    Conservative window (`HELD_RETENTION_DAYS`): a fresh held row survives, so if an ops
    channel is configured later, `flush_pending()` re-routes recent history into it
    before retention would ever remove it. Returns how many rows were removed."""
    rows = conn.execute(
        "DELETE FROM pending_alerts WHERE channel_id = 0 AND delivered_at IS NULL "
        "AND created_at < now() - make_interval(days => %s) RETURNING id",
        (max(1, int(older_than_days)),)).fetchall()
    return len(rows)
