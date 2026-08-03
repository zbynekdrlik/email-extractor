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

from . import memory, snapshot

log = logging.getLogger("orders.teach")


class NotACandidate(Exception):
    """The answer is not one of the cards that were offered."""


class AlreadyAnswered(Exception):
    """This question has been settled; a second answer would silently rewrite history."""


def ask(conn, message_id: str, customer_ean: str, customer_name: str, wording: str,
        quantity, unit: str, candidates: list[dict], delivery_date: str = "",
        reason: str = "", on_new=None) -> int | None:
    """Raise ONE question for this (customer, wording). Returns its id.

    Returns the EXISTING id when it is already open, and None when the wording has already
    been taught — the engine will resolve it from memory, so asking would be noise.

    `on_new` (#102) fires ONLY when a genuinely NEW question was just inserted — never on the
    "already open" or "already taught" paths — so a duplicate order for the same still-open
    wording does not spam a second Odoo message. Called with the full question dict; any
    failure is logged and swallowed, exactly like the main order report's own post — a
    notification failure must never break order processing.
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
        qid = int(row[0])
        log.info("asking the warehouse about %r for %s", wording, customer_ean)
        if on_new:
            try:
                on_new(get(conn, qid))
            except Exception:
                log.exception("notifying about new question %s (%r) failed", qid, wording)
        return qid
    existing = conn.execute(
        "SELECT id FROM order_questions WHERE customer_ean = %s AND item_key = %s"
        " AND status = 'open'", (str(customer_ean), key)).fetchone()
    return int(existing[0]) if existing else None


_COLS = ("id, message_id, customer_ean, customer_name, wording, quantity, unit, "
        "candidates, delivery_date, reason, status, answer_gtin, answer_card, "
        "answered_by, answered_at, created_at, kind, context")


def get(conn, qid: int) -> dict | None:
    row = conn.execute(
        f"SELECT {_COLS} FROM order_questions WHERE id = %s", (qid,)).fetchone()
    return _row(row) if row else None


def open_questions(conn, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        f"""SELECT {_COLS} FROM order_questions WHERE status = 'open'
            ORDER BY created_at LIMIT %s""", (limit,)).fetchall()
    return [_row(r) for r in rows]


def recently_taught(conn, limit: int = 20) -> list[dict]:
    """The mappings a human settled, newest first — this is what makes `undo` reachable.

    An answered question leaves the open list, so without this the warehouse had nowhere to
    correct a mis-click, and an undo nobody can reach is not an undo (found while verifying
    0.9.5 on the live box).
    """
    rows = conn.execute(
        f"""SELECT {_COLS} FROM order_questions WHERE status = 'answered'
            ORDER BY answered_at DESC LIMIT %s""", (limit,)).fetchall()
    return [_row(r) for r in rows]


def answer(conn, qid: int, gtin: str, card: str, by: str = "") -> dict:
    """Settle a question and teach it. The card must be one that was offered, OR one that
    exists in the current catalog (#149 — the warehouse's full-catalog search on /otazky).

    Teaches on TWO layers (#102): the existing per-customer mapping (unchanged — it is what
    makes THIS customer's answer instant), and a GLOBAL one for every other customer who ever
    types the same wording. The global write is best-effort (`memory.remember_global`'s own
    `ON CONFLICT DO NOTHING`) — if this wording is already taught globally, the per-customer
    row still lands and, being checked first in the ladder, is exactly what lets a customer's
    OWN mapping override the global one.
    """
    q = get(conn, qid)
    if not q:
        raise NotACandidate(f"question {qid} does not exist")
    if q["status"] != "open":
        raise AlreadyAnswered(
            f"question {qid} was answered on {q['answered_at']} with {q['answer_gtin']}")
    offered = {str(c.get("gtin")) for c in q["candidates"]}
    if str(gtin) not in offered and str(gtin) not in snapshot.catalog_gtin_set(conn):
        raise NotACandidate(
            f"{gtin} was not offered for {q['wording']!r} and is not in the catalog")

    memory.remember(conn, q["customer_ean"], q["wording"], str(gtin), card or "",
                    _today(conn), source="human")
    memory.remember_global(conn, q["wording"], str(gtin), card or "", question_id=qid,
                           taught_by=by)
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

    Also retracts the GLOBAL mapping (#102) — but ONLY the row THIS question created
    (`memory.forget_global` checks `question_id`). A different customer's later, redundant
    answer to the same wording never owns the global row (it was already taught), so undoing
    THAT answer must not erase a mapping some OTHER question is responsible for.
    """
    q = get(conn, qid)
    if not q:
        raise NotACandidate(f"question {qid} does not exist")
    conn.execute(
        "DELETE FROM item_memory WHERE customer_ean = %s AND item_key = %s"
        " AND source = 'human'", (q["customer_ean"], memory.item_key(q["wording"])))
    memory.forget_global(conn, q["wording"], qid)
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
            "created_at": r[15], "kind": r[16] or "item", "context": r[17] or {}}


# --- #159: the customer-half of the SAME teach-once loop — "who is this customer?" ----
#
# Shares the exact same table/index/dashboard/undo machinery `ask`/`answer` above use for
# products — only `kind='customer'` differs. `customer_ean` is always '' for this kind
# (the customer is exactly what is unknown), so the dedupe key is `item_key`, built from
# the SENDER ADDRESS rather than a product wording: a second unresolved order from the
# same address, before the first is answered, reuses the SAME open question.

def ask_customer(conn, message_id: str, sender_email: str, candidates: list[dict],
                 delivery_date: str, context: dict, on_new=None) -> int | None:
    """Raise ONE 'who is this customer?' question. Returns its id — the EXISTING id when
    one is already open for this sender address, and None when there is no address to key
    on at all (nothing to dedupe against, nothing a human could even be shown)."""
    key = memory.item_key(f"neznamy zakaznik {sender_email}")
    if not (sender_email and key):
        return None
    row = conn.execute(
        """INSERT INTO order_questions
               (message_id, customer_ean, customer_name, wording, item_key, kind,
                candidates, delivery_date, reason, context)
           VALUES (%s, '', '', %s, %s, 'customer', %s, %s, %s, %s)
           ON CONFLICT (customer_ean, item_key) WHERE status = 'open' DO NOTHING
           RETURNING id""",
        (message_id, sender_email, key, Json(candidates or []), delivery_date or "",
         "Zákazník nebol nájdený v tabuľke zákazníkov", Json(context or {}))).fetchone()
    if row:
        qid = int(row[0])
        log.info("asking the warehouse who %r is", sender_email)
        if on_new:
            try:
                on_new(get(conn, qid))
            except Exception:
                log.exception("notifying about new customer question %s (%r) failed",
                              qid, sender_email)
        return qid
    existing = conn.execute(
        "SELECT id FROM order_questions WHERE customer_ean = '' AND item_key = %s"
        " AND status = 'open'", (key,)).fetchone()
    return int(existing[0]) if existing else None


def answer_customer(conn, qid: int, ean_edi: str, name: str, by: str = "") -> dict:
    """Settle a 'who is this customer?' question. `ean_edi=""` means the warehouse said
    "neviem, kto to je / nie je to ani jeden z nich" — the question is still marked
    answered (so it stops asking and the taught list shows it was handled), but the
    caller (`hold.release_unknown_customer`) is what turns the held order into a visible
    'review' outcome instead of shipping it; this function never ships anything itself.

    A real pick must be one of the offered candidates — unlike `answer()`'s product half
    there is no "or anything in the catalog" escape hatch, because the whole point of this
    question is that the customer is not yet reliably known at all.
    """
    q = get(conn, qid)
    if not q:
        raise NotACandidate(f"question {qid} does not exist")
    if q.get("kind") != "customer":
        raise NotACandidate(f"question {qid} is not a customer question")
    if q["status"] != "open":
        raise AlreadyAnswered(
            f"question {qid} was answered on {q['answered_at']} with {q['answer_gtin']}")
    if ean_edi:
        offered = {str(c.get("ean_edi")) for c in q["candidates"]}
        if str(ean_edi) not in offered:
            raise NotACandidate(f"{ean_edi} was not offered for question {qid}")
    conn.execute(
        """UPDATE order_questions
              SET status = 'answered', answer_gtin = %s, answer_card = %s,
                  answered_by = %s, answered_at = now()
            WHERE id = %s""", (str(ean_edi or ""), name or "", by or "", qid))
    log.info("customer question %s answered: %r (%s) by %s", qid, ean_edi, name, by)
    return get(conn, qid) or {}


def _today(conn):
    return conn.execute("SELECT current_date").fetchone()[0]
