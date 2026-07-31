"""The teach-once loop: the warehouse advises a wording once, forever (#88).

Measured on the 30-email corpus (2026-07-31): only **15 distinct (customer, wording) pairs**
are behind every line a human would ever see — private nicknames ("Šiška", "Pletenka",
"jankove buchty"), sloppy abbreviations, and four variants that do not exist as ordered
("Dánske pečivo s jahodami" against a čučoriedka card). Teach the 15 and the tail closes.
The four keep asking, forever, and that is correct: shipping blueberry for strawberry is
worse than asking.

The channel is the user's decision (2026-07-31): **one click on the extractor dashboard**,
linked from the Odoo message the warehouse already reads. So an answer arrives as a card id
out of the offered candidates — never as free text to be parsed, never as a typo.

The answer lands in `item_memory(source='human')`, per CUSTOMER, and outranks every model
rung (`match.decide_without_model` → `human_taught`). It also overrides the weight guard: the
guard exists to stop the MODEL guessing, not to overrule the warehouse.
"""
from __future__ import annotations

import logging

from psycopg.types.json import Json

from . import memory

log = logging.getLogger("orders.teach")


class NotACandidate(Exception):
    """The answer is not one of the cards that were offered."""


class AlreadyAnswered(Exception):
    """This question has been settled; a second answer would silently rewrite history."""


def ask(conn, message_id: str, customer_ean: str, customer_name: str, wording: str,
        quantity, unit: str, candidates: list[dict], delivery_date: str = "",
        reason: str = "") -> int | None:
    """Raise ONE question for this (customer, wording). Returns its id.

    Returns the EXISTING id when it is already open, and None when the wording has already
    been taught — the engine will resolve it from memory, so asking would be noise.
    """
    key = memory.item_key(wording)
    if not (customer_ean and key):
        return None
    # Skip only when a HUMAN has already settled this wording. A thin machine-learned history
    # is NOT a reason to stay silent: it is below the ladder's 3-day bar, so the line can still
    # end up unmatched — and then nobody could ever teach it.
    recalled = memory.resolve(conn, customer_ean, wording)
    if recalled is not None and recalled.human:
        return None
    row = conn.execute(
        """INSERT INTO order_questions
               (message_id, customer_ean, customer_name, wording, item_key, quantity, unit,
                candidates, delivery_date, reason)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (customer_ean, item_key) WHERE status = 'open' DO NOTHING
           RETURNING id""",
        (message_id, str(customer_ean), customer_name or "", str(wording), key, quantity,
         unit or "ks", Json(candidates or []), delivery_date or "", reason or ""),
    ).fetchone()
    if row:
        log.info("asking the warehouse about %r for %s", wording, customer_ean)
        return int(row[0])
    existing = conn.execute(
        "SELECT id FROM order_questions WHERE customer_ean = %s AND item_key = %s"
        " AND status = 'open'", (str(customer_ean), key)).fetchone()
    return int(existing[0]) if existing else None


def get(conn, qid: int) -> dict | None:
    row = conn.execute(
        """SELECT id, message_id, customer_ean, customer_name, wording, quantity, unit,
                  candidates, delivery_date, reason, status, answer_gtin, answer_card,
                  answered_by, answered_at, created_at
             FROM order_questions WHERE id = %s""", (qid,)).fetchone()
    return _row(row) if row else None


def open_questions(conn, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        """SELECT id, message_id, customer_ean, customer_name, wording, quantity, unit,
                  candidates, delivery_date, reason, status, answer_gtin, answer_card,
                  answered_by, answered_at, created_at
             FROM order_questions WHERE status = 'open'
            ORDER BY created_at LIMIT %s""", (limit,)).fetchall()
    return [_row(r) for r in rows]


def recently_taught(conn, limit: int = 20) -> list[dict]:
    """The mappings a human settled, newest first — this is what makes `undo` reachable.

    An answered question leaves the open list, so without this the warehouse had nowhere to
    correct a mis-click, and an undo nobody can reach is not an undo (found while verifying
    0.9.5 on the live box).
    """
    rows = conn.execute(
        """SELECT id, message_id, customer_ean, customer_name, wording, quantity, unit,
                  candidates, delivery_date, reason, status, answer_gtin, answer_card,
                  answered_by, answered_at, created_at
             FROM order_questions WHERE status = 'answered'
            ORDER BY answered_at DESC LIMIT %s""", (limit,)).fetchall()
    return [_row(r) for r in rows]


def answer(conn, qid: int, gtin: str, card: str, by: str = "") -> dict:
    """Settle a question and teach it. The card must be one that was offered."""
    q = get(conn, qid)
    if not q:
        raise NotACandidate(f"question {qid} does not exist")
    if q["status"] != "open":
        raise AlreadyAnswered(
            f"question {qid} was answered on {q['answered_at']} with {q['answer_gtin']}")
    if str(gtin) not in [str(c.get("gtin")) for c in q["candidates"]]:
        raise NotACandidate(f"{gtin} was not offered for {q['wording']!r}")

    memory.remember(conn, q["customer_ean"], q["wording"], str(gtin), card or "",
                    _today(conn), source="human")
    conn.execute(
        """UPDATE order_questions
              SET status = 'answered', answer_gtin = %s, answer_card = %s,
                  answered_by = %s, answered_at = now()
            WHERE id = %s""", (str(gtin), card or "", by or "", qid))
    log.info("taught %r -> %s for %s (by %s)", q["wording"], gtin, q["customer_ean"], by)
    return get(conn, qid) or {}


def undo(conn, qid: int) -> dict:
    """Take a mistaken answer back: drop the mapping and ask again.

    Without this a mis-click was permanent AND invisible — a taught wording is never asked
    about again and decides the line with no model call, so nothing would ever contradict it.
    Only what a HUMAN taught is removed; real deliveries are evidence and stay.
    """
    q = get(conn, qid)
    if not q:
        raise NotACandidate(f"question {qid} does not exist")
    conn.execute(
        "DELETE FROM item_memory WHERE customer_ean = %s AND item_key = %s"
        " AND source = 'human'", (q["customer_ean"], memory.item_key(q["wording"])))
    conn.execute(
        """UPDATE order_questions
              SET status = 'open', answer_gtin = NULL, answer_card = NULL,
                  answered_by = NULL, answered_at = NULL
            WHERE id = %s""", (qid,))
    log.warning("teaching taken back for %r (%s)", q["wording"], q["customer_ean"])
    return get(conn, qid) or {}


def _row(r) -> dict:
    return {"id": int(r[0]), "message_id": r[1], "customer_ean": r[2],
            "customer_name": r[3] or "", "wording": r[4], "quantity": r[5],
            "unit": r[6] or "", "candidates": r[7] or [], "delivery_date": r[8] or "",
            "reason": r[9] or "", "status": r[10], "answer_gtin": r[11],
            "answer_card": r[12], "answered_by": r[13], "answered_at": r[14],
            "created_at": r[15]}


def _today(conn):
    return conn.execute("SELECT current_date").fetchone()[0]
