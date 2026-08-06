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
- `maybe_post_daily_digest` — posted through the SAME Odoo channel `report.py`'s other
  order notifications already use, claimed once per calendar day
  (`order_digest_sent`, the identical "claim, don't spam" pattern
  `spend.cap_tripped`/`order_spend_alerts` already use for the monthly cap) — called
  from `worker.tick()` on every tick, but only ever actually posts on the FIRST tick of
  a new day, summarizing the day that JUST finished (never "today", which is still
  incomplete while the day is in progress).
"""
from __future__ import annotations

import logging

from . import report
from .pipeline import ASK_THE_WAREHOUSE

log = logging.getLogger("orders.reliability")


def provenance_stats_for_day(conn, day: str = "") -> dict:
    """Per-day match provenance. `day` is `YYYY-MM-DD`; empty means today."""
    row = conn.execute(
        """SELECT count(*),
                  count(*) FILTER (WHERE i.rule = 'llm_sure'),
                  count(*) FILTER (WHERE i.rule = ANY(%s))
             FROM order_items i JOIN order_runs r ON r.id = i.run_id
            WHERE r.shadow = false
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
            WHERE shadow = false
              AND to_char(coalesce(finished_at, started_at), 'YYYY-MM-DD')
                  = coalesce(nullif(%s, ''), to_char(now(), 'YYYY-MM-DD'))""",
        (day,)).fetchone()
    runs_n, errors_n, orders_n = (int(x or 0) for x in run_row)

    return {"day": day or _today(conn), "runs": runs_n, "orders": orders_n,
           "errors": errors_n, "items": items_n, "deterministic": det_n,
           "llm": llm_n, "review": review_n}


def days_since_incident(conn) -> int | None:
    """Live-computed, never a constant. `None` (never a fake 0) when nothing has ever
    been recorded — honesty over false reassurance."""
    row = conn.execute("SELECT max(occurred_on) FROM match_incidents").fetchone()
    if not row or not row[0]:
        return None
    delta = conn.execute("SELECT (now()::date - %s::date)", (row[0],)).fetchone()
    return int(delta[0])


def maybe_post_daily_digest(conn, cfg, post=None, shadow: bool = False) -> bool:
    """Post the ONE digest for the day that JUST finished — claimed once per calendar
    day, safe to call on every tick. Never in shadow mode: shadow's whole guarantee is
    that nothing observable leaves the process. Never raises: a notification failure
    must never break the worker loop (mirrors `pipeline._post_summary`/
    `worker._check_spend_cap`)."""
    if shadow:
        return False
    post = post or (lambda c, html, **kw: report.post_from_config(c, html))
    yesterday = _yesterday(conn)
    claimed = conn.execute(
        "INSERT INTO order_digest_sent (day) VALUES (%s) ON CONFLICT (day) DO NOTHING "
        "RETURNING day", (yesterday,)).fetchone()
    if not claimed:
        return False
    stats = provenance_stats_for_day(conn, yesterday)
    html = report.build_daily_digest(stats, days_since_incident(conn),
                                     link=report.sklad_link(cfg))
    try:
        post(cfg, html)
    except Exception:
        log.exception("posting the daily provenance digest failed")
    return True


def _today(conn) -> str:
    return str(conn.execute("SELECT to_char(now(), 'YYYY-MM-DD')").fetchone()[0])


def _yesterday(conn) -> str:
    return str(conn.execute(
        "SELECT to_char(now() - interval '1 day', 'YYYY-MM-DD')").fetchone()[0])
