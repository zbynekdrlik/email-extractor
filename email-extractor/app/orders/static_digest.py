"""Grouped Odoo digest for cleanly-uploaded static orders (#133 "DOPLNENIE ROZHODNUTIA
— skupinové Odoo správy", 2026-08-05): static-order volume (~32/day) is high enough that
one Odoo message per shipped order was explicitly rejected by the user. A clean, fresh,
non-actionable upload is queued DURABLY — the `static_order_digest` table, never
in-memory state — and flushed as ONE grouped message either once the batch fills or once
the queue has sat idle past a timeout. Every function here reads its state straight from
the DB on every call, so the queue survives an add-on restart by construction: there is
nothing to lose.

Only genuine exceptions keep their own IMMEDIATE, individual Odoo message and never enter
this queue at all: an order with an actionable extra-content note (`static_extra.py`), a
genuine upload error, or a duplicate/"already sent" skip (not a fresh upload — counting
it would misreport how many orders actually shipped this batch). Scope is static orders
only; AI-orders reporting is untouched.
"""
from __future__ import annotations

import logging

from . import report

log = logging.getLogger("orders.static_digest")

BATCH_SIZE = 30
IDLE_MINUTES = 60


def _plural_objednavok(n: int) -> str:
    if n == 1:
        return "objednávka"
    if 2 <= n <= 4:
        return "objednávky"
    return "objednávok"


def queue(conn, message_id: str, filename: str) -> None:
    """Record ONE cleanly-uploaded, non-actionable static order. Call this right after
    the upload is confirmed — never before, never for a duplicate/empty/error outcome."""
    conn.execute(
        "INSERT INTO static_order_digest (message_id, filename) VALUES (%s, %s)",
        (message_id, filename))


def _pending_count(conn) -> int:
    return int(conn.execute(
        "SELECT count(*) FROM static_order_digest WHERE flushed_at IS NULL").fetchone()[0])


def _flush(conn, cfg, post=None) -> int:
    post = post or (lambda c, html: report.post_from_config(c, html))
    n = _pending_count(conn)
    if not n:
        return 0
    html = (f"<p>&#9989; {n} {_plural_objednavok(n)} nahraných do ORIONu, "
           "žiadna nemá doplňujúcu správu.</p>")
    try:
        result = post(cfg, html)
    except Exception:
        log.exception("posting the static-order digest failed — will retry next flush")
        return 0
    if result is None:
        log.warning("static-order digest was not delivered (Odoo not configured?) — "
                   "will retry next flush")
        return 0
    conn.execute(
        "UPDATE static_order_digest SET flushed_at = now() WHERE flushed_at IS NULL")
    log.info("flushed static-order digest: %d %s", n, _plural_objednavok(n))
    return n


def maybe_flush_batch(conn, cfg, batch_size: int | None = None, post=None) -> int:
    """Call right after `queue()` — flushes immediately once the pending count reaches
    the batch size. Returns how many orders were just flushed (0 if not yet due)."""
    limit = int(batch_size if batch_size is not None
               else getattr(cfg, "static_digest_batch_size", BATCH_SIZE) or BATCH_SIZE)
    if _pending_count(conn) >= max(1, limit):
        return _flush(conn, cfg, post=post)
    return 0


def flush_idle(conn, cfg, idle_minutes: int | None = None, post=None) -> int:
    """Call periodically (every worker tick, regardless of engine) — flushes whatever is
    pending once it has sat unflushed for longer than the idle timeout, measured from the
    MOST RECENTLY queued pending row (a steady trickle of new orders keeps resetting the
    clock; only real quiet triggers this). Returns how many orders were just flushed."""
    minutes = int(idle_minutes if idle_minutes is not None
                 else getattr(cfg, "static_digest_idle_minutes", IDLE_MINUTES) or IDLE_MINUTES)
    row = conn.execute(
        "SELECT count(*), max(created_at) FROM static_order_digest "
        "WHERE flushed_at IS NULL").fetchone()
    n, newest = int(row[0] or 0), row[1]
    if not n or newest is None:
        return 0
    stale = conn.execute(
        "SELECT %s < now() - make_interval(mins => %s)",
        (newest, max(1, minutes))).fetchone()[0]
    if not stale:
        return 0
    return _flush(conn, cfg, post=post)
