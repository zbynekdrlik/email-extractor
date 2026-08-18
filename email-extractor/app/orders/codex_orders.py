"""CODEX order evidence + the auto-resolve sweep (#342).

The warehouse enters every customer order into CODEX by hand each day. That manual entry
is PROOF the mail really was an order and is already handled — but the add-on never saw it,
so an open `mail`-kind board question ("je toto vôbec objednávka?") just nagged the nástenka
forever. `tools/codex_orders_push.py` (on the dev/ERP box) reads those order headers from
the codex-bridge DuckDB read-only and POSTs a thin slice here; `upsert_orders` stores it in
`codex_orders`; `resolve_mail_questions` (run in the worker tick) closes an open `mail`
question NEUTRALLY the moment the sender's own customer has a matching CODEX order.

Two hard safety rules, both from the ticket + #341's finding (comment 5325463114):

1. **Only the POSITIVE case auto-closes.** "Nothing in CODEX" is NEVER auto-closed here —
   an overlooked order would be a permanent lost future order; #341's 2-working-day
   expiry handles the negative case instead.
2. **The neutral close writes ZERO durable rules.** A `mail` question's two normal answers
   (`not_order`/`manual`) both teach a PERMANENT `mail_rules` row for the sender via
   `teach._apply_mail` — so this auto-close must NOT go through `teach.KINDS['mail'].apply`.
   It uses its own terminal path: a guarded `status='open'` UPDATE (a concurrent human
   answer always wins), `messages.processed=true` (or the question re-asks forever, #307),
   an honest `email_events` review event — and touches no `mail_rules`, no memory.

The sender→customer→EAN mapping is deliberately CONSERVATIVE: the sender address must be
written in EXACTLY ONE customer card (the exact-email rung `customer.resolve` already uses),
and that card must carry an `ean_edi`. Anything ambiguous → no match, the question stays
open. CODEX documents are EVIDENCE only — never a product/card import (the #337 boundary).
"""
from __future__ import annotations

import logging

from psycopg.types.json import Json

from . import customer, report, snapshot, teach

log = logging.getLogger("orders.codex_orders")

# One sweep pass never needs an unbounded scan — there are only ever a handful of open
# mail-kind questions on the board at once (they are single-shot per message).
_MAX_QUESTIONS = 500


def upsert_orders(conn, orders: list[dict]) -> int:
    """Idempotent upsert of CODEX order headers, keyed on the order number (PK).

    Each dict: order_number (int, required), customer_ean (str, required), plus optional
    customer_nico, customer_name, issue_date, delivery_date, line_count. Rows missing the
    two required fields are skipped (the endpoint already filters, this is belt-and-braces).
    Returns the number of rows actually written.
    """
    n = 0
    for o in orders or []:
        order_number = o.get("order_number")
        customer_ean = o.get("customer_ean")
        if order_number is None or not customer_ean:
            continue
        conn.execute(
            """INSERT INTO codex_orders
                   (order_number, customer_nico, customer_ean, customer_name,
                    issue_date, delivery_date, line_count, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, now())
               ON CONFLICT (order_number) DO UPDATE SET
                   customer_nico = EXCLUDED.customer_nico,
                   customer_ean  = EXCLUDED.customer_ean,
                   customer_name = EXCLUDED.customer_name,
                   issue_date    = EXCLUDED.issue_date,
                   delivery_date = EXCLUDED.delivery_date,
                   line_count    = EXCLUDED.line_count,
                   updated_at    = now()""",
            (int(order_number), o.get("customer_nico"), str(customer_ean),
             o.get("customer_name") or "", o.get("issue_date") or None,
             o.get("delivery_date") or None, o.get("line_count")))
        n += 1
    return n


def _owner_ean(customers: list[dict], sender_email: str) -> str | None:
    """The customer's EDI EAN when the sender address is written in EXACTLY ONE card that
    carries an EAN — otherwise None. Same ambiguity bar as `customer.resolve`'s exact-email
    rung (`len(owners) == 1`): a shared or unknown address never resolves, so a question is
    never closed for the wrong customer. Reuses `customer._addresses` so the email-field
    parsing is identical to the rest of the system (a list OR a comma string)."""
    addr = (customer._addresses(sender_email) or [""])[0]
    if not addr:
        return None
    eans = {str(c.get("ean_edi")) for c in customers
            if c.get("ean_edi") and addr in customer._addresses(c.get("emails"))}
    return next(iter(eans)) if len(eans) == 1 else None


def _mail_date(conn, message_id: str):
    """The day this mail was received (its `messages.created_at::date`). The comparison the
    sweep makes is "was a CODEX order ISSUED on/after the mail arrived" — the ingestion day
    is the reliable, always-present proxy for the mail's own day (`sent_at` is a raw,
    sometimes-malformed RFC2822 header string). None when the message row is gone."""
    row = conn.execute(
        "SELECT created_at::date FROM messages WHERE message_id = %s", (message_id,)).fetchone()
    return row[0] if row else None


def _find_codex_order(conn, customer_ean: str, mail_date):
    """The newest CODEX order for this customer issued on/after the mail's date, or None."""
    row = conn.execute(
        """SELECT order_number, issue_date FROM codex_orders
            WHERE customer_ean = %s AND issue_date IS NOT NULL AND issue_date >= %s
            ORDER BY issue_date DESC, order_number DESC LIMIT 1""",
        (str(customer_ean), mail_date)).fetchone()
    return {"order_number": row[0], "issue_date": row[1]} if row else None


def _close_mail_question(conn, q: dict, order: dict) -> bool:
    """Close ONE open `mail` question neutrally as "handled manually in CODEX". Writes
    NOTHING to mail_rules or any teach memory. Returns True iff this call actually closed it
    (False when a concurrent human answer already did — the guarded `status='open'` UPDATE).
    Runs on the worker's autocommit connection: the answer commits before the message/event
    writes, exactly like the #323 human-answer path."""
    order_number, issue_date = order["order_number"], order["issue_date"]
    won = conn.execute(
        """UPDATE order_questions
              SET status = 'answered', answer = %s, answered_by = 'codex-auto',
                  answered_at = now()
            WHERE id = %s AND status = 'open'
            RETURNING id""",
        (Json({"kind": "codex_handled", "order_number": order_number,
               "issue_date": str(issue_date)}), q["id"])).fetchone()
    if not won:
        return False
    # #307: a terminal-but-still-processed=false message is re-claimed and re-asks the same
    # question (whack-a-mole) — mark it processed so this stays closed for good.
    conn.execute(
        """UPDATE messages
              SET processed = true, processed_at = now(),
                  processed_by = 'codex-auto', processing_at = NULL
            WHERE message_id = %s""", (q["message_id"],))
    # Honest rollup event: status='review' (not 'ok'/'uploaded' — nothing shipped through
    # US; the order was entered by hand in CODEX), so the dashboard/digest show it as
    # handled-manually, never as a delivery we made.
    report.log_event(
        conn, q["message_id"], stage="review", status="review",
        outcome=f"Vybavené ručne v CODEXe — objednávka č. {order_number} zadaná "
                f"{issue_date}",
        detail={"question_id": q["id"], "codex_order": order_number, "auto": "codex"})
    log.info("mail question %s auto-closed from CODEX order %s (%s)",
             q["id"], order_number, issue_date)
    return True


def resolve_mail_questions(conn, cfg) -> int:
    """Sweep every OPEN `mail`-kind question and neutrally close the ones whose sender's
    customer already has a matching CODEX order (issued on/after the mail's day). Returns
    the number closed this pass. Never raises for a single question — one bad row must not
    stop the sweep (mirrors the worker's other sweeps)."""
    open_qs = teach.open_questions(conn, limit=_MAX_QUESTIONS, kinds=("mail",))
    if not open_qs:
        return 0
    snapshot_id = snapshot.latest_snapshot_id(conn)
    if not snapshot_id:
        return 0
    customers = snapshot.load_customers(conn, snapshot_id)
    closed = 0
    for q in open_qs:
        try:
            sender = (q.get("payload") or {}).get("sender_email", "")
            ean = _owner_ean(customers, sender)
            if not ean:
                continue
            mail_date = _mail_date(conn, q["message_id"])
            if mail_date is None:
                continue
            order = _find_codex_order(conn, ean, mail_date)
            if order and _close_mail_question(conn, q, order):
                closed += 1
        except Exception:
            log.exception("codex auto-resolve failed for question %s", q.get("id"))
    if closed:
        log.info("codex sweep auto-closed %d mail question(s)", closed)
    return closed
