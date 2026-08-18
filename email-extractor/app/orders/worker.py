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
# #271: must equal the SAME hardcoded value in the n8n workflow "AI auto orders"
# (id `wlORIhkVZISCdZNmBTM4Z`, node "Get AI Orders" — `interval '30 minutes'`,
# verified live via the n8n MCP `get_workflow_details`, 2026-08-13). That workflow
# is `active: false` today (the Python engine fully owns `ai_orders` dispatch — see
# this module's own docstring), so the two numbers cannot currently RACE each other
# — but the convention is what a rollback to n8n would depend on, and CI has no n8n
# API access to verify it live (no secret configured in ci.yml), so
# `tests/test_orders_worker.py::test_claim_stale_minutes_matches_n8n_ai_orders_window`
# PINS this value rather than comparing it live. If you change this constant, you
# MUST also update (or knowingly diverge from) that n8n node — the test only catches
# an accidental drift on THIS side, never a change made on the n8n side.
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
                            -- #164: a 'mail' question ("is this even an order?") has no
                            -- held_orders row (there is no order to hold) — same
                            -- reasoning as the held_orders guard above, narrowed to
                            -- kind='mail' the same way hold.has_open is.
                            AND NOT EXISTS (
                                SELECT 1 FROM order_questions q
                                 WHERE q.message_id = messages.message_id
                                   AND q.kind = 'mail' AND q.status = 'open')
                          ORDER BY created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED)
         RETURNING message_id, subject, from_addr, from_name, combined_text, body_text,
                   attempts, list_unsubscribe""",
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
    msg = {"message_id": row[0], "subject": row[1] or "", "from_addr": row[2] or "",
           "from_name": row[3] or "", "combined_text": row[4] or row[5] or "",
           # #342: default present on every path (shadow's 6-col row too) so the pipeline's
           # promo gate can read it unconditionally; only `_claim` actually fills it.
           "list_unsubscribe": "", "today": datetime.now(UTC).date().isoformat()}
    # #330: only `_claim` (engine=python) selects `attempts` — `_peek_for_shadow` (shadow
    # mode) never increments/claims and has no need for it, so its row stays 6 columns.
    # `tick`'s crash handler below only reads this for the python engine.
    if len(row) > 6:
        msg["attempts"] = int(row[6] or 0)
    # #342: `_claim` adds list_unsubscribe as the 8th column (index 7); `_peek_for_shadow`
    # never selects it (shadow never routes a promo mail — that write is not-shadow only).
    if len(row) > 7:
        msg["list_unsubscribe"] = row[7] or ""
    return msg


# --- run bookkeeping -----------------------------------------------------

def _start_run(conn, message_id: str, snapshot_id: int | None, shadow: bool) -> int:
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
    if result and result.get("spend"):
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
        # #310: the spend-cap tripwire is OPERATOR/diagnostic (only a system operator can
        # act on the monthly LLM bill). Route it through the durable dl_alerts outbox to
        # the OPS channel — NEVER a direct post_from_config, which with an unset ops
        # channel (0) would fall back to the sales channel 152 (the misroute #310 fixes).
        # When ops is unset the channel_id=0 row is HELD by flush_pending (never
        # delivered, still counted on the dashboard) — a durable trace on top of this
        # WARNING log, and never on 152/243.
        log.warning("%s", text.replace("\n", " | "))
        if not shadow:
            from . import dl_alerts, report
            dl_alerts.enqueue(conn, report.ops_channel(cfg), "spend_cap",
                              "<p>" + text.replace("\n", "<br>") + "</p>")
    except Exception:
        # Reporting the bill must never take an order down with it.
        log.exception("the spend-cap check failed")


def _check_daily_digest(conn, cfg, shadow: bool) -> None:
    """#196: the daily match-provenance digest. `reliability.maybe_post_daily_digest`
    already claims once-per-day and never raises — this wrapper only exists so a
    genuinely unexpected failure (e.g. the claim insert itself) still cannot take an
    order down with it, the same guarantee `_check_spend_cap` gives its own check."""
    try:
        from . import reliability
        reliability.maybe_post_daily_digest(conn, cfg, shadow=shadow)
    except Exception:
        log.exception("the daily digest check failed")


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
            # #330: `_claim` already incremented `attempts`; once it reaches MAX_ATTEMPTS
            # the `_claim` guard (`attempts < MAX_ATTEMPTS`) stops reprocessing this
            # message, so it would otherwise vanish with NO human-visible diagnostic —
            # exactly the undiagnosable "Chyba: neznáma [line 150]" class the n8n engine
            # produced. On that FINAL attempt, surface a REAL error (rollup=True ->
            # proc_status='error') carrying the exception type + message + stage. The
            # first MAX_ATTEMPTS-1 crashes stay silent-and-retryable (a transient crash
            # must still auto-recover), so this fires at most once per message — never a
            # per-tick email_events flood on a deterministic crash. Mirrors
            # static_worker.tick's identical #330 fix.
            if int(message.get("attempts") or 0) >= MAX_ATTEMPTS:
                from . import report
                report.log_event(
                    conn, message["message_id"], stage="error", status="error",
                    outcome=report.crash_outcome(e, "run_live"),
                    detail={"error": repr(e), "stage": "run_live",
                            "attempts": int(message.get("attempts") or 0)})
        return 0

    _finish_run(conn, run_id, result.get("status", "ok"), result)
    _check_spend_cap(conn, cfg, shadow=engine != "python")
    _check_daily_digest(conn, cfg, shadow=engine != "python")
    if engine == "python":
        from .hold import has_open
        # #93: a run that just HELD one of its orders must not be marked processed — the
        # message stays claimable-by-name-only (excluded from _claim above) until
        # hold.release_for_question / hold.release_due lets it go.
        if not has_open(conn, message["message_id"]):
            # #342: `AND processed = false` so a message the pipeline ALREADY terminalized
            # (the promo → no_processing branch marks processed=true, processed_by=
            # 'promo-filter') is not re-stamped here as processed_by=CATEGORY — that would
            # silently overwrite the promo attribution. In every normal flow the message is
            # still processed=false at this point, so this is unchanged there.
            conn.execute(
                """UPDATE messages
                      SET processed = true, processed_at = now(), processed_by = %s,
                          processing_at = NULL
                    WHERE message_id = %s AND processed = false""",
                (CATEGORY, message["message_id"]))
    return 1


def refresh_due(conn, cfg) -> int | None:
    """#129: the Google Sheet is never read anymore — Postgres (`catalog_overrides`/
    `customer_overrides`, #127/#128) is the sole source of truth for the catalog and
    customer list. This just reports whichever snapshot is currently frozen; there is
    no fetch, no interval, and no config gate left to check. `cfg` stays in the
    signature only so `run_forever`'s call site (and every caller) needs no change.
    """
    return snapshot.latest_snapshot_id(conn)


def run_forever(conn, cfg, stop=None, sleep=None, pipeline=None) -> None:  # pragma: no cover
    """Worker loop, started as a thread by the add-on entrypoint.

    Also drives the static-orders SHADOW worker (#132) on the SAME connection/thread — a
    static shadow tick is a cheap, non-blocking regex parse (no LLM call), so it does not
    warrant its own thread, and reusing this loop means it inherits the same reconnect/
    error handling for free. With the default options (static_orders_shadow off) that call
    returns 0 immediately, same "provably inert by default" contract as the ai_orders tick.
    """
    import time as _time

    from . import dl_worker, static_worker
    sleep = sleep or _time.sleep
    log.info("order worker started (engine=%s shadow=%s static_shadow=%s dl_shadow=%s)",
             getattr(cfg, "ai_orders_engine", "n8n"), getattr(cfg, "orders_shadow", False),
             getattr(cfg, "static_orders_shadow", False),
             getattr(cfg, "delivery_notes_shadow", False))
    while not (stop and stop.is_set()):
        try:
            refresh_due(conn, cfg)
            dl_worker.refresh_due(conn, cfg)
            orders_python = resolve_engine(getattr(cfg, "ai_orders_engine", "n8n")) == "python"
            # #204: delivery_notes_engine gets the SAME "n8n"/"python" contract as
            # ai_orders_engine (F1's own design comment) — this is the gate both
            # confirm.sweep (desadv_sent, #203) and dl_worker.tick (#204) key on.
            dl_python = resolve_engine(getattr(cfg, "delivery_notes_engine", "n8n")) == "python"
            if orders_python:
                # #234: a customer added on /znalosti (rather than answered straight on
                # the question card) must also unstick any order still waiting for it —
                # runs BEFORE the deadline backstop so a genuinely resolvable order never
                # has to wait for its delivery date to arrive first.
                from .hold import release_due, retry_unknown_customer_questions
                retry_unknown_customer_questions(conn, cfg)
                # #342: auto-resolve an open `mail`-kind board question ("je toto vôbec
                # objednávka?") when the sender's customer already has a matching order the
                # warehouse entered by hand in CODEX (pushed into `codex_orders`). Neutral
                # close only, ZERO durable rules; the negative case is never auto-closed
                # (#341's expiry handles it). Never raises — one bad question can't stop the
                # tick. Gated on orders_python like the other order sweeps.
                from . import codex_orders
                codex_orders.resolve_mail_questions(conn, cfg)
                # #93: the deadline backstop — ship whatever is still held once its
                # delivery date arrives. Shadow/n8n modes never hold an order, so there is
                # never anything for this to release there.
                release_due(conn, cfg)
            if orders_python or dl_python:
                # #151/#203: has Communicator actually taken what we uploaded? Covers
                # BOTH edi_sent (orders_python) and desadv_sent (dl_python) in one
                # sweep — shadow/n8n modes never upload through either engine, so
                # neither ledger ever gets a row for confirm.sweep to find there.
                from . import confirm
                confirm.sweep(conn, cfg)
                # #237: a board question (order_questions, either audience — item/
                # customer/mail/date/line from the orders_python path, dl_item/
                # dl_supplier from the dl_python path) that has sat open too long gets
                # reminded/escalated here, grouped, via the SAME durable pending_alerts
                # outbox flushed below. Gated the same way confirm.sweep is: shadow/n8n
                # modes never write an order_questions row through either engine, so
                # there is never anything for this to find there either.
                from . import question_alerts
                question_alerts.sweep(conn, cfg)
                # #308: no message may sit SILENTLY in the terminal `human_processing`
                # pit (a scan the classifier could not place, needs_vision, no processor
                # watching it). Vision rescues the near-empty-scan case into its real
                # category; anything unrescued raises a warehouse-actionable alert through
                # the SAME durable outbox flushed just below. Runs on the live-engine
                # gate like the sweeps above — the app is "live" whenever an engine owns
                # its category, and a rescue can route to either the DL or orders side.
                from . import human_processing
                human_processing.sweep(conn, cfg)
                # #239 finding 4 / #237: deliver every durably-queued alert (DL
                # processing-health AND, since #237, stale-question reminders) —
                # `quiet_seconds=FLUSH_QUIET_SECONDS` (#239 reopened, finding 1) makes a
                # burst of same-kind alerts land as ONE grouped Odoo post instead of one
                # per row — see dl_alerts.py's own module docstring. `prune_delivered`
                # (finding 4) bounds the table's growth; it is cheap and idempotent, so
                # running it on the same tick as everything else needs no extra timer.
                # Moved OUT of the dl_python-only gate (#237): a plain orders_python-only
                # install (no DL engine) can now also have pending question_reminder/
                # question_escalation rows that need flushing.
                from . import dl_alerts
                dl_alerts.flush_pending(conn, cfg, quiet_seconds=dl_alerts.FLUSH_QUIET_SECONDS)
                dl_alerts.prune_delivered(conn)
                # #319: bound the growth of HELD operator alerts (channel_id=0,
                # undelivered — the #310 "no ops channel configured yet" rows), which
                # `prune_delivered` deliberately leaves alone. Same maintenance tick,
                # same cheap/idempotent discipline — no parallel timer or path.
                dl_alerts.purge_held(conn)
            if dl_python:
                # #239 classes 2/3: a message classified `dodacie_listy` that never got
                # a first attempt at all — gated on the live DL python engine, same
                # discipline confirm.sweep above already uses (shadow/n8n modes never
                # write to messages the way this sweep's own query expects).
                dl_worker.stuck_classified_sweep(conn, cfg)
            handled = tick(conn, cfg, pipeline=pipeline)
            handled = static_worker.tick(conn, cfg) or handled
            # #204: shadow ALSO needs a tick (it never claims, but it does need to be
            # driven) — dl_worker.tick's own engine/shadow gate is the SAME "inert by
            # default" contract every other engine here already has.
            handled = dl_worker.tick(conn, cfg) or handled
            if handled:
                continue          # more may be waiting; do not sleep between messages
        except Exception:
            log.exception("order worker tick failed")
        sleep(15)
