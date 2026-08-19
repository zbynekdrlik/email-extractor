"""Match-quality provenance + a live, honest 'days since incident' signal (#196).

The warehouse checks every AI-matched order by hand because it has no measurable basis
to stop ("skladníčka zatiaľ kontroluje každú objednávku, lebo sa na to nedá spoľahnúť").
Two pieces, both readable live from this app's OWN Postgres (never a hand-maintained
constant, never a cross-machine read of the dev2 eval corpus — see the design comment
on #196 for why both alternatives were rejected):

- `provenance_stats_for_day` — per-day counts of processed order LINES, bucketed by how
  they were decided: deterministic (no model call, or a rule the model only confirmed —
  `catalog_name`/`alias_exact`/`history_sure`/`global_taught`/`alias_exact_weight`/
  `alias_customer`/`history`/`unique_card`/`sibling`), AI rung (`llm_sure` — the model
  chose the card and nothing flagged it), held for review
  (`pipeline.ASK_THE_WAREHOUSE` — unmatched/borderline/weight-conflict/alias-conflict/
  the new #195 lexical-gap guard), plus per-RUN counts (processed e-mails, orders,
  outright errors). `shadow=false` only: the live add-on has run
  `ai_orders_engine=python`/`orders_shadow=false` since 2026-08-02, so those rows are
  real, already-shipped decisions — a shadow comparison run must never inflate a trust
  metric nobody is actually relying on yet.
- `days_since_incident` — `now() - max(occurred_on)` over `match_incidents`, a small
  append-only table seeded with the two real incidents this batch (#195) already found.
  Returns `None` (never a fake 0) when nothing has ever been recorded.

These stats are read live by the dashboard (`httpapi_reports.py` → `/api/orders/digest`).
The daily Odoo "Denný prehľad" digest that once posted them was removed in #347 (spam,
no consumer); the stat functions themselves stay — the dashboard reads them.

**#204 (DL migration F5) extends this file with a DL-scoped counterpart, never mixes the
two.** F1 (#200)'s design decision reuses `order_runs`/`order_items` UNMODIFIED for DL
runs (`result->>'kind' = 'dl'` is the only discriminator, no new column) — so
`provenance_stats_for_day` now explicitly EXCLUDES `kind='dl'` rows (`IS DISTINCT FROM`,
which also matches every pre-#204 row that has no `kind` key at all — backward
compatible), or a DL run would silently inflate the AI-orders "runs"/"objednávky" counts,
and DL's OWN rule vocabulary (`llm_sure`/`unmatched`/`llm_borderline`/
`llm_sure_lexical_gap`, `dl_match.py`) would leak into orders' `llm`/`review` buckets —
`llm_sure` is a real match rule name BOTH engines happen to share, so without the filter a
DL document decided by the model would silently count as an ORDER decided by the model.
`dl_provenance_stats_for_day` is the DL-scoped mirror (its own review-rule set, `email_events`
counts for W7's duplicate-skip visibility and the spec §4 announced-vs-attached mismatch —
neither of those two has an `order_items` row at all, they are message/document-level
findings, not item decisions). Both stat sets are surfaced only on the dashboard now
(`/api/orders/dl/stats` for DL); the daily digest that combined them was removed in #347.
"""
from __future__ import annotations

import logging

from . import dl_alerts, dl_worker
from .pipeline import ASK_THE_WAREHOUSE

log = logging.getLogger("orders.reliability")


def provenance_stats_for_day(conn, day: str = "") -> dict:
    """Per-day match provenance. `day` is `YYYY-MM-DD`; empty means today.

    Orders-only (#204): every query below excludes `result->>'kind' = 'dl'` rows — see
    the module docstring's #204 section for why a DL run must never be counted here.
    """
    row = conn.execute(
        """SELECT count(*),
                  count(*) FILTER (WHERE i.rule = 'llm_sure'),
                  count(*) FILTER (WHERE i.rule = ANY(%s))
             FROM order_items i JOIN order_runs r ON r.id = i.run_id
            WHERE r.shadow = false AND (r.result->>'kind') IS DISTINCT FROM 'dl'
              AND to_char(coalesce(r.finished_at, r.started_at), 'YYYY-MM-DD')
                  = coalesce(nullif(%s, ''), to_char(now(), 'YYYY-MM-DD'))""",
        (list(ASK_THE_WAREHOUSE), day)).fetchone()
    items_n, llm_n, review_n = (int(x or 0) for x in row)
    # Deterministic is "everything else" — every no-model / model-confirmed-only rung,
    # computed by subtraction so a future new rung is deterministic-by-default instead
    # of silently invisible (unlike the AI/review buckets, which name their rules
    # explicitly and must be updated whenever the ladder changes).
    det_n = max(0, items_n - llm_n - review_n)

    run_row = conn.execute(
        """SELECT count(*), count(*) FILTER (WHERE status = 'error'),
                  coalesce(sum((result->>'orders')::int), 0)
             FROM order_runs
            WHERE shadow = false AND (result->>'kind') IS DISTINCT FROM 'dl'
              AND to_char(coalesce(finished_at, started_at), 'YYYY-MM-DD')
                  = coalesce(nullif(%s, ''), to_char(now(), 'YYYY-MM-DD'))""",
        (day,)).fetchone()
    runs_n, errors_n, orders_n = (int(x or 0) for x in run_row)

    return {"day": day or _today(conn), "runs": runs_n, "orders": orders_n,
           "errors": errors_n, "items": items_n, "deterministic": det_n,
           "llm": llm_n, "review": review_n}


# --- #204: the DL-scoped counterpart ----------------------------------------

# dl_match.py's own review-worthy rule names (mirrors pipeline.ASK_THE_WAREHOUSE, but a
# DIFFERENT rule vocabulary — DL's `llm_sure` would otherwise collide with orders' own
# rule of the same name, see the module docstring's #204 section).
DL_ASK_THE_WAREHOUSE = ("unmatched", "llm_borderline", "llm_sure_lexical_gap")


def dl_provenance_stats_for_day(conn, day: str = "",
                               include_current_health: bool = True) -> dict:
    """The DL mirror of `provenance_stats_for_day` — `kind='dl'` runs/items ONLY, plus
    two message/document-level `email_events` counts `order_items` has no row for at
    all: W7's duplicate-skip visibility and the spec §4 announced-vs-attached mismatch
    (both `dl_report.log_duplicate`/`log_announced_mismatch`, #204).

    `include_current_health` (#239, deep-review finding on this ticket's own PR):
    `dl_current_health`'s three gauges are current-STATE, not day-scoped — calling this
    function once for "today" and once for "yesterday" (as `/api/orders/dl/stats` does)
    would otherwise run the SAME three queries twice for the identical answer. Set
    `False` for a caller that already has (or doesn't need) that section — the "today"
    call keeps the default so every pre-existing caller/test is unaffected."""
    row = conn.execute(
        """SELECT count(*),
                  count(*) FILTER (WHERE i.rule = 'llm_sure'),
                  count(*) FILTER (WHERE i.rule = ANY(%s))
             FROM order_items i JOIN order_runs r ON r.id = i.run_id
            WHERE r.shadow = false AND r.result->>'kind' = 'dl'
              AND to_char(coalesce(r.finished_at, r.started_at), 'YYYY-MM-DD')
                  = coalesce(nullif(%s, ''), to_char(now(), 'YYYY-MM-DD'))""",
        (list(DL_ASK_THE_WAREHOUSE), day)).fetchone()
    items_n, llm_n, review_n = (int(x or 0) for x in row)
    det_n = max(0, items_n - llm_n - review_n)

    run_row = conn.execute(
        """SELECT count(*), count(*) FILTER (WHERE status = 'error')
             FROM order_runs
            WHERE shadow = false AND result->>'kind' = 'dl'
              AND to_char(coalesce(finished_at, started_at), 'YYYY-MM-DD')
                  = coalesce(nullif(%s, ''), to_char(now(), 'YYYY-MM-DD'))""",
        (day,)).fetchone()
    runs_n, errors_n = (int(x or 0) for x in run_row)

    events_row = conn.execute(
        """SELECT count(*) FILTER (WHERE stage = 'duplicate_skip'),
                  count(*) FILTER (WHERE stage = 'announced_mismatch'),
                  count(*) FILTER (WHERE stage = 'sklad_unknown')
             FROM email_events
            WHERE workflow = 'delivery_notes'
              AND to_char(ts, 'YYYY-MM-DD')
                  = coalesce(nullif(%s, ''), to_char(now(), 'YYYY-MM-DD'))""",
        (day,)).fetchone()
    dup_n, mismatch_n, sklad_unknown_n = (int(x or 0) for x in events_row)

    # #305: each „Neviem"-deferred DL logs one rollup `stage='sklad_unknown'` event, so
    # this is the count of delivery notes the warehouse set aside that day for Marek's
    # manual resolution — rendered as its own line in the warehouse-facing digest.
    stats = {"day": day or _today(conn), "runs": runs_n, "errors": errors_n,
            "items": items_n, "deterministic": det_n, "llm": llm_n, "review": review_n,
            "duplicates": dup_n, "announced_mismatch": mismatch_n,
            "sklad_unknown": sklad_unknown_n}
    if include_current_health:
        stats.update(dl_current_health(conn))
    return stats


def dl_current_health(conn) -> dict:
    """#239: three CURRENT-STATE gauges (deliberately NOT day-scoped — a stuck backlog
    matters regardless of which day it originated on) so every one of #239's five
    invisible-failure classes is queryable/visible on the DL nástenka's own stats strip
    (`/api/orders/dl/stats`) and the daily Odoo digest, not just alerted into the
    channel:

    - `quarantined` (class 1) — DL messages that exhausted all 5 claim attempts and are
      never picked up again. The hourly n8n "Stuck message watchdog" already alerts
      this class into the Odoo channel (attempts>=3) — this is the dashboard half of
      requirement 1, which that channel-only alert does not cover on its own.
    - `pending_alerts` (classes 2/3) — rows `dl_alerts` has enqueued but not yet
      delivered (an upload failure, or a stuck-classified finding). A nonzero count
      here for more than a sweep or two means Odoo delivery itself is struggling, which
      is exactly the "alert-delivery failure must itself be visible" requirement.
    - `open_import_incidents` (classes 4/5) — currently-open `import_alert_incidents`
      for the DESADV ledger (`confirm.py`), the same ground truth that module's own
      sweep already uses to decide whether to keep reminding.

    Deep-review finding on this ticket's own PR: the quarantine threshold (5 attempts)
    used to be a bare literal duplicated across THREE places (this query,
    `dl_worker.MAX_ATTEMPTS`, and the digest's own Slovak wording) with nothing keeping
    them in sync. Reading `dl_worker.MAX_ATTEMPTS`/`dl_worker.CATEGORY` directly (no
    import cycle — `dl_worker` never imports `reliability`, confirmed by tracing its
    whole dependency chain) collapses it to ONE source of truth; `quarantine_threshold`
    is carried in the returned dict so the dashboard (`/api/orders/dl/stats`) can show the
    real number instead of hardcoding it a second time.
    """
    quarantined_row = conn.execute(
        """SELECT count(*) FROM messages
            WHERE category = %s AND processed = false
              AND coalesce(attempts,0) >= %s""",
        (dl_worker.CATEGORY, dl_worker.MAX_ATTEMPTS)).fetchone()
    incidents_row = conn.execute(
        """SELECT count(*) FROM import_alert_incidents
            WHERE closed_at IS NULL AND source = 'desadv'""").fetchone()
    return {"quarantined": int(quarantined_row[0] or 0),
           "quarantine_threshold": dl_worker.MAX_ATTEMPTS,
           "pending_alerts": dl_alerts.pending_count(conn),
           "open_import_incidents": int(incidents_row[0] or 0)}


def days_since_incident(conn) -> int | None:
    """Live-computed, never a constant. `None` (never a fake 0) when nothing has ever
    been recorded — honesty over false reassurance."""
    row = conn.execute("SELECT max(occurred_on) FROM match_incidents").fetchone()
    if not row or not row[0]:
        return None
    delta = conn.execute("SELECT (now()::date - %s::date)", (row[0],)).fetchone()
    return int(delta[0])


def _today(conn) -> str:
    return str(conn.execute("SELECT to_char(now(), 'YYYY-MM-DD')").fetchone()[0])


def _yesterday(conn) -> str:
    return str(conn.execute(
        "SELECT to_char(now() - interval '1 day', 'YYYY-MM-DD')").fetchone()[0])
