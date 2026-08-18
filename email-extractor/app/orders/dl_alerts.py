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
just a message that stayed stuck past the window, gets alerted again. **#327 refined
this: the recency window applies ONLY to DELIVERED rows.** An UNDELIVERED alert — a HELD
channel-0 operator alert with no ops channel configured yet (#310) never resolves, so
the window expired and the #308 sweep re-enqueued the same message every ~4h, piling up
duplicate held rows (live #319: 65 held rows for 10 distinct messages) — now dedupes
regardless of age: there is nothing to remind about while the first alert has not even
gone out, and the held row itself still delivers once an ops channel is set. See
`already_pending`'s own docstring; `db.py` revision 3 cleaned up the pre-fix duplicates.

**#239 reopened, finding 4 — unbounded growth.** No delivered row was ever removed
(the table only ever grows), and a permanently-broken Odoo config retried every single
worker tick forever with no ceiling. `prune_delivered()` removes delivered rows past a
retention window; `purge_held()` (#319) does the same for the separate held channel-0
state (an undelivered row with a REAL channel is still never removed); `flush_pending()`
only selects rows under `MAX_FLUSH_ATTEMPTS` — a row past the cap simply stops being
actively retried (no more hammering a dead endpoint) but stays on record and still
counts in `pending_count()`/`dl_current_health`, so it is never silently dropped, only
no longer hammered.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from html import escape

from . import report

log = logging.getLogger("orders.dl_alerts")

# #336: the grouped OPERATOR-alert kinds whose rows are ONE-PER-ITEM short lines
# (`• odosielateľ — predmet (prijaté D.M.)`), enqueued across many worker ticks and
# grouped only at flush time. For each, `flush_pending` renders ONE readable message:
# a per-kind HEADER (count + one explanation, `{n}` filled at flush) + up to
# DISPLAY_ITEM_CAP short item lines + „…a N ďalších" + a dashboard action link — instead
# of the pre-#336 wall where flush just concatenated N full explanation sentences (live
# evidence: a 3177-char human_processing_review post). Mirrors question_alerts._group_html's
# header/cap/„…ďalších"/link convention. A kind NOT in this registry (question_reminder/
# question_escalation — already a fully-formatted single body from question_alerts;
# spend_cap — a one-off) is concatenated as before, untouched.
DISPLAY_ITEM_CAP = 10
GROUPED_ITEM_KINDS = {
    "human_processing_review":
        "&#128194; Nezaradené e-maily ({n}) &mdash; systém ich nevedel automaticky "
        "zaradiť; skontroluj ich na dashboarde a preklasifikuj / označ ako vybavené.",
    "dl_stuck_classified":
        "&#9888;&#65039; Zaseknuté dodacie listy ({n}) &mdash; zaradené ako dodací list, "
        "ale spracovanie sa vôbec nezačalo; over, či beží spracovanie dodacích listov.",
    "dl_upload_failed":
        "&#128230; Nenahraté dodacie listy ({n}) &mdash; nahranie do ORIONu zlyhalo; "
        "skús znova alebo nahlás administrátorovi.",
}

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


def item_line(sender: str, subject: str, received=None) -> str:
    """#336: ONE short per-item line for a grouped ops alert — `• odosielateľ — predmet
    (prijaté D.M.)`. The explanation sentence + the dashboard action link live ONCE in the
    group HEADER (`_format_grouped`, keyed on `GROUPED_ITEM_KINDS`), never repeated per
    item — that repetition was the pre-#336 wall. `received` may be a datetime/date (a
    `D.M.` suffix is added) or None (no suffix); never a microsecond timestamp."""
    when = ""
    if received is not None and hasattr(received, "day"):
        when = f" (prijaté {received.day}.{received.month}.)"
    return (f"<p>&#8226; {escape(sender or '-')} &mdash; "
            f"{escape(subject or '-')}{when}</p>")


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
    """True when THIS message already has an alert of this kind that should suppress a
    fresh enqueue — the dedup a persistently-stuck message needs so a sweep that keeps
    rediscovering it does not enqueue (and eventually post) a fresh copy every ~15s.

    Two states, deliberately asymmetric (#327):

    * an UNDELIVERED row (`delivered_at IS NULL`) dedupes REGARDLESS of age — while the
      very first alert has not even gone out there is nothing to remind about, and the
      already-held row will still deliver once it can (e.g. a held channel-0 operator
      alert delivers as soon as an ops channel is configured, per `flush_pending`);
    * a DELIVERED row dedupes only WITHIN `window_hours` **measured from `delivered_at`**
      (NOT `created_at`) — the recency protection against an immediate re-ask right after
      delivery. #334: `flush_pending()` sets only `delivered_at`, never `created_at`, so a
      row HELD for days (the #310/#332 channel-0 backlog) then finally delivered has a
      `created_at` far outside the window; anchoring the window on `created_at` made the
      dedup fail the instant such a held row delivered, and the #308 sweep re-enqueued the
      same message ~15s later — a duplicate grouped ops post, repeating every ~4h (mirrors
      `confirm.py`'s `last_alert_at` recency, which is measured from the last alert too).

    **#239 reopened, finding 3: this dedup used to be PERMANENT (never expires) — that
    was wrong.** The original design assumed the two kinds this guards
    (`dl_upload_failed`/`dl_stuck_classified`) can never structurally re-enter their own
    sweep's candidate set without an "unusual manual reset" — but the dashboard's
    one-click `POST /api/message/<id>/reprocess` IS exactly that reset: any message that
    stays stuck past a bounded window deserves a fresh alert, not permanent silence.

    **#327: bounding EVERY row by the window was ALSO wrong.** A HELD/undelivered alert
    (channel 0, no ops channel configured yet — the #310 hold) is never resolved, so the
    window expired and the #308 sweep re-enqueued the same message every ~4h, piling up
    duplicate held rows (live #319: 65 held rows for only 10 distinct messages). The
    window now applies ONLY to delivered rows; an undelivered row suppresses a re-enqueue
    forever (until it is finally delivered, after which the 4h `DEFAULT_REMINDER_HOURS`
    recency window — the same value `confirm.py` uses, and, per #334, measured from
    `delivered_at` — governs a genuine re-alert)."""
    if not message_id:
        return False
    row = conn.execute(
        "SELECT 1 FROM pending_alerts WHERE kind = %s AND message_id = %s "
        "AND (delivered_at IS NULL OR delivered_at > now() - make_interval(hours => %s)) "
        "LIMIT 1",
        (kind, message_id, max(1, int(window_hours)))).fetchone()
    return row is not None


def _format_grouped(kind: str, bodies: list[str], cfg) -> str:
    """#336: one readable grouped alert for a per-item wall kind — a single per-kind
    HEADER (count + one explanation, `{n}` filled) + up to `DISPLAY_ITEM_CAP` short item
    lines + „…a N ďalších" + a dashboard action link. Replaces the pre-#336 wall where
    `flush_pending` just `"".join`-ed N full explanation sentences. Mirrors
    `question_alerts._group_html`'s header/cap/„…ďalších"/link convention. `cfg` may be
    None (some tests) — `report.dashboard_link` then returns "" and the link line is
    simply omitted, exactly like an unset `dashboard_base_url`."""
    n = len(bodies)
    # `.replace`, not `.format`: a future header template with a stray literal `{`/`}` (a
    # CSS/JSON snippet, an emoji entity) must never raise and break the whole flush (this
    # runs OUTSIDE flush_pending's post() try/except) — `{n}` is the only placeholder.
    parts = [f"<p>{GROUPED_ITEM_KINDS[kind].replace('{n}', str(n))}</p>"]
    parts.extend(bodies[:DISPLAY_ITEM_CAP])
    if n > DISPLAY_ITEM_CAP:
        parts.append(f"<p>&#8230; a {n - DISPLAY_ITEM_CAP} ďalších.</p>")
    base = report.dashboard_link(cfg)
    if base:
        parts.append(f'<p>&#128203; Otvor dashboard: '
                     f'<a href="{escape(base)}">{escape(base)}</a></p>')
    return "".join(parts)


def reminder_suppressed(conn, cfg, kind: str, message_id: str, now=None) -> bool:
    """#336: the re-enqueue cadence for the grouped ops SWEEP kinds
    (`human_processing_review`, `dl_stuck_classified`) — replaces the old flat
    `already_pending` 4h window that re-swept (and re-posted) the SAME still-stuck message
    every ~4h, producing a repeated wall. Returns True when a fresh enqueue should be
    SUPPRESSED:

    - the FIRST alert for a message ALWAYS fires (surfaced promptly, any hour/day) — a
      newly-stuck message must never be delayed to the next morning;
    - a RE-reminder for a still-unresolved message fires at most ONCE per day, only after
      the configured morning hour, skipping Saturday/Sunday — mirrors `confirm.py`'s
      CARRYOVER cadence (`morning_check_active`, its own `import_morning_check_hour` knob).

    Anchored on `delivered_at`, NEVER `created_at` (#334): a row HELD undelivered for days
    (the #310 channel-0 hold) then finally delivered has a stale `created_at`, so the
    once-per-day gate must measure from the real delivery. While a row is still undelivered
    there is nothing to remind about — it suppresses regardless of age (#327), and the held
    row itself still delivers once it can."""
    if not message_id:
        return False
    now = now or datetime.now(UTC)
    if conn.execute(
            "SELECT 1 FROM pending_alerts WHERE kind = %s AND message_id = %s "
            "AND delivered_at IS NULL LIMIT 1", (kind, message_id)).fetchone():
        return True
    row = conn.execute(
        "SELECT max(delivered_at) FROM pending_alerts WHERE kind = %s AND message_id = %s",
        (kind, message_id)).fetchone()
    last = row[0] if row else None
    if last is None:
        return False   # never alerted → the first alert fires now, regardless of hour/day
    # A delivered alert already exists → re-remind at most once per morning, skip weekends.
    from . import confirm
    if not confirm.morning_check_active(cfg, now):
        return True    # not yet the morning window on a workday → hold the reminder
    return (last.astimezone(confirm.LOCAL_TZ).date()
            >= now.astimezone(confirm.LOCAL_TZ).date())   # already reminded today?


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
        bodies = [body for _rid, body, _created_at in items]
        # #336: a per-item wall kind gets ONE header + capped short lines + a dashboard
        # link; every other kind keeps the legacy concatenation (question_reminder/
        # escalation are already a single formatted body, spend_cap is a one-off).
        if kind in GROUPED_ITEM_KINDS:
            html = _format_grouped(kind, bodies, cfg)
        else:
            html = "".join(bodies)
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
