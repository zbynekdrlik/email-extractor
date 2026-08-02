"""Order worker (#60): one tick picks at most one `ai_orders` message and runs it.

Three modes, chosen by add-on options, because n8n owns the live pipeline until the
very last step of this project:

- `ai_orders_engine=n8n`, `orders_shadow=false` (DEFAULT) — completely inert.
- `ai_orders_engine=n8n`, `orders_shadow=true` — runs the pipeline for comparison but
  **claims nothing and marks nothing**: the message must stay exactly as n8n expects to
  find it. Results land in `order_runs(shadow=true)` only.
- `ai_orders_engine=python` — claims with the same protocol the n8n dispatcher uses
  (`processing_at` + `attempts`, 30-minute stale window) and owns the message.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from psycopg.types.json import Json

from . import snapshot, spend

log = logging.getLogger("orders.worker")

CATEGORY = "ai_orders"
ENGINES = ("n8n", "python")
# Same window the n8n dispatcher re-claims on: a message claimed longer than this is
# assumed dead (container restart mid-run) and may be picked up again.
CLAIM_STALE_MINUTES = 30
MAX_ATTEMPTS = 5
# How far back shadow mode looks. Shadow calls the real model, so this is a cost bound
# as much as a scope one.
SHADOW_DAYS = 3


def resolve_engine(value: str | None) -> str:
    engine = (value or "n8n").strip() or "n8n"
    if engine not in ENGINES:
        raise ValueError(f"ai_orders_engine must be one of {ENGINES}, got {engine!r}")
    return engine


# --- message selection ---------------------------------------------------

def _claim(conn) -> dict | None:
    row = conn.execute(
        f"""UPDATE messages SET processing_at = now(), attempts = COALESCE(attempts, 0) + 1
             WHERE id = (SELECT id FROM messages
                          WHERE category = %s AND processed = false
                            AND COALESCE(attempts, 0) < %s
                            AND (processing_at IS NULL
                                 OR processing_at < now()
                                    - interval '{CLAIM_STALE_MINUTES} minutes')
                            -- #93: a message with a question still open for one of its
                            -- orders is WAITING, not stuck — re-running it would pay for
                            -- the LLM again for nothing. hold.release_for_question /
                            -- hold.release_due are what move it on, never a re-claim.
                            AND NOT EXISTS (
                                SELECT 1 FROM held_orders h
                                 WHERE h.message_id = messages.message_id
                                   AND h.status = 'held')
                          ORDER BY created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED)
         RETURNING message_id, subject, from_addr, from_name, combined_text, body_text""",
        (CATEGORY, MAX_ATTEMPTS)).fetchone()
    return _as_message(row)


def _peek_for_shadow(conn, days: int = SHADOW_DAYS) -> dict | None:
    """Pick a message the shadow run has not seen yet, WITHOUT touching its state.

    Bounded to the last `days` of mail on purpose: shadow calls the real model, so an
    unbounded shadow would replay the whole archive at top-tier reasoning cost. Shadow
    exists to compare against n8n on CURRENT mail; the historical corpus is the
    evaluation harness (#66), run deliberately.
    """
    row = conn.execute(
        """SELECT m.message_id, m.subject, m.from_addr, m.from_name,
                  m.combined_text, m.body_text
             FROM messages m
            WHERE m.category = %s
              AND m.created_at > now() - make_interval(days => %s)
              AND NOT EXISTS (SELECT 1 FROM order_runs r
                               WHERE r.message_id = m.message_id AND r.shadow)
            ORDER BY m.created_at DESC LIMIT 1""",
        (CATEGORY, max(1, int(days or SHADOW_DAYS)))).fetchone()
    return _as_message(row)


def _as_message(row) -> dict | None:
    if not row:
        return None
    # #117: memory.resolve's as_of date fence and hold.is_past_deadline both key off this
    # "today" — without it, a claimed message's date fence in production was always a
    # silent no-op (as_of=""). Both _claim (real engine=python path) and _peek_for_shadow
    # build their message dict through this one function, so this covers both.
    return {"message_id": row[0], "subject": row[1] or "", "from_addr": row[2] or "",
            "from_name": row[3] or "", "combined_text": row[4] or row[5] or "",
            "today": datetime.now(UTC).date().isoformat()}


# --- run bookkeeping -----------------------------------------------------

def _start_run(conn, message_id: str, snapshot_id: int, shadow: bool) -> int:
    return int(conn.execute(
        """INSERT INTO order_runs (message_id, snapshot_id, shadow, status)
           VALUES (%s, %s, %s, 'running') RETURNING id""",
        (message_id, snapshot_id, shadow)).fetchone()[0])


def _finish_run(conn, run_id: int, status: str, result: dict | None, error: str = "") -> None:
    conn.execute(
        """UPDATE order_runs
              SET status = %s, error = %s, result = %s, finished_at = now()
            WHERE id = %s""",
        (status, error or None, Json(result or {}), run_id))
    if (result or {}).get("spend"):
        spend.record(conn, run_id, result["spend"])
    for item in (result or {}).get("items", []):
        conn.execute(
            """INSERT INTO order_items
                   (run_id, name, quantity, unit, gtin, card, confidence, rule, trace)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (run_id, item.get("name", ""), item.get("quantity"), item.get("unit", ""),
             item.get("gtin"), item.get("card"), item.get("confidence"),
             item.get("rule"), Json(item.get("trace") or {})))


def _check_spend_cap(conn, cfg, shadow: bool) -> None:
    """One message the first time a month passes the cap — never a hard stop.

    An unshipped order costs the business far more than the tokens, so this warns and keeps
    working. In shadow mode it only logs: shadow's guarantee is that nothing leaves the
    process, and money is not a reason to break it.
    """
    cap = float(getattr(cfg, "orders_spend_cap_eur", 30) or 0)
    if cap <= 0:
        return
    try:
        if not spend.cap_tripped(conn, cap_eur=cap):
            return
        text = spend.trip_message(conn, cap_eur=cap)
        log.warning("%s", text.replace("\n", " | "))
        if not shadow:
            from . import report
            report.post_from_config(cfg, "<p>" + text.replace("\n", "<br>") + "</p>")
    except Exception:
        # Reporting the bill must never take an order down with it.
        log.exception("the spend-cap check failed")


# --- one tick ------------------------------------------------------------

def tick(conn, cfg, pipeline=None) -> int:
    """Process at most one message. Returns the number of messages handled (0 or 1).

    `pipeline` is injected so the worker's claim/shadow guarantees can be tested
    without the extraction stack; production passes the real pipeline.
    """
    engine = resolve_engine(getattr(cfg, "ai_orders_engine", "n8n"))
    shadow = bool(getattr(cfg, "orders_shadow", False))
    if engine != "python" and not shadow:
        return 0

    snapshot_id = snapshot.latest_snapshot_id(conn)
    if not snapshot_id:
        # Matching against an absent catalog would reject every order with confident
        # nonsense. Refusing to run is the honest failure.
        log.warning("no catalog snapshot yet — order worker idle")
        return 0

    if pipeline is None:
        try:
            from .pipeline import run as pipeline
        except ImportError:
            # The extraction stack lands in #61. Until then, flipping the engine or the
            # shadow flag must fail loudly in the log and change nothing — never crash
            # the add-on's loop, and never look like a processed message.
            log.error("order pipeline not implemented yet (#61) — engine=%s shadow=%s "
                      "requested, doing nothing", engine, shadow)
            return 0

    message = (_claim(conn) if engine == "python"
               else _peek_for_shadow(conn, getattr(cfg, "orders_shadow_days", SHADOW_DAYS)))
    if not message:
        return 0

    run_id = _start_run(conn, message["message_id"], snapshot_id, shadow=engine != "python")
    try:
        result = pipeline(conn, cfg, message, snapshot_id) or {}
    except Exception as e:
        log.exception("order pipeline failed for %s", message["message_id"])
        _finish_run(conn, run_id, "error", None, error=repr(e))
        if engine == "python":
            # Release the claim: a crashed run must be retryable, and the message must
            # never look processed. attempts stays incremented, so a permanently broken
            # message stops after MAX_ATTEMPTS instead of spinning forever.
            conn.execute(
                "UPDATE messages SET processing_at = NULL WHERE message_id = %s",
                (message["message_id"],))
        return 0

    _finish_run(conn, run_id, result.get("status", "ok"), result)
    _check_spend_cap(conn, cfg, shadow=engine != "python")
    if engine == "python":
        from .hold import has_open
        # #93: a run that just HELD one of its orders must not be marked processed — the
        # message stays claimable-by-name-only (excluded from _claim above) until
        # hold.release_for_question / hold.release_due lets it go.
        if not has_open(conn, message["message_id"]):
            conn.execute(
                """UPDATE messages
                      SET processed = true, processed_at = now(), processed_by = %s,
                          processing_at = NULL
                    WHERE message_id = %s""",
                (CATEGORY, message["message_id"]))
    return 1


def refresh_due(conn, cfg) -> int | None:
    """Import a fresh snapshot when the newest one is older than the refresh interval.

    Returns the current snapshot id. Needs `catalog_sheet_id` + both tab ids; without
    them the pipeline stays idle rather than matching against a stale or absent catalog.
    """
    if not (cfg.catalog_sheet_id and cfg.catalog_gid and cfg.customer_gid):
        return snapshot.latest_snapshot_id(conn)
    row = conn.execute(
        "SELECT max(checked_at) FROM order_snapshots").fetchone()
    minutes = max(1, int(getattr(cfg, "catalog_refresh_minutes", 60) or 60))
    if row and row[0]:
        age = conn.execute(
            "SELECT (now() - %s) > make_interval(mins => %s)", (row[0], minutes)).fetchone()
        if not age[0]:
            return snapshot.latest_snapshot_id(conn)
    return snapshot.refresh(conn, cfg.catalog_sheet_id, cfg.catalog_gid, cfg.customer_gid)


def run_forever(conn, cfg, stop=None, sleep=None, pipeline=None) -> None:  # pragma: no cover
    """Worker loop, started as a thread by the add-on entrypoint.

    Also drives the static-orders SHADOW worker (#132) on the SAME connection/thread — a
    static shadow tick is a cheap, non-blocking regex parse (no LLM call), so it does not
    warrant its own thread, and reusing this loop means it inherits the same reconnect/
    error handling for free. With the default options (static_orders_shadow off) that call
    returns 0 immediately, same "provably inert by default" contract as the ai_orders tick.
    """
    import time as _time

    from . import static_worker
    sleep = sleep or _time.sleep
    log.info("order worker started (engine=%s shadow=%s static_shadow=%s)",
             getattr(cfg, "ai_orders_engine", "n8n"), getattr(cfg, "orders_shadow", False),
             getattr(cfg, "static_orders_shadow", False))
    while not (stop and stop.is_set()):
        try:
            refresh_due(conn, cfg)
            if resolve_engine(getattr(cfg, "ai_orders_engine", "n8n")) == "python":
                # #93: the deadline backstop — ship whatever is still held once its
                # delivery date arrives. Shadow/n8n modes never hold an order, so there is
                # never anything for this to release there.
                from .hold import release_due
                release_due(conn, cfg)
            handled = tick(conn, cfg, pipeline=pipeline)
            handled = static_worker.tick(conn, cfg) or handled
            if handled:
                continue          # more may be waiting; do not sleep between messages
        except Exception:
            log.exception("order worker tick failed")
        sleep(15)
