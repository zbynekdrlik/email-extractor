"""Hold lifecycle — deadline check, decision (de)serialization, and the
read/create surface of `held_orders` (#424 split of hold.py). Re-exported by the
`hold` facade; every caller keeps reaching these via `hold.X`."""
from __future__ import annotations

import logging

from psycopg.types.json import Json

from . import edi

log = logging.getLogger("orders.hold")

# --- the deadline ----------------------------------------------------------

def is_past_deadline(delivery_date: str, today: str = "") -> bool:
    """True once there is no more time left to wait for an answer.

    An unparsable/blank delivery date behaves exactly like the code always did before
    holding existed: shipped immediately, never held — there is no date to wait for.
    """
    stamp = edi._format_date(delivery_date)
    if not stamp.strip():
        return True
    now_stamp = edi._format_date(today) if today else ""
    if not now_stamp.strip():
        from datetime import UTC, datetime
        now_stamp = datetime.now(UTC).strftime("%Y%m%d")
    return stamp <= now_stamp


# --- recording a hold --------------------------------------------------------

def _dump_decisions(decisions) -> list[dict]:
    return [{"item_name": d.item_name, "gtin": d.gtin, "card": d.card,
             "confidence": d.confidence, "rule": d.rule, "note": d.note,
             "review": d.review, "trace": d.trace, "quantity": d.quantity,
             "unit": d.unit} for d in decisions]


def _load_decisions(rows: list[dict]):
    from .match import Decision
    return [Decision(item_name=d.get("item_name", ""), gtin=d.get("gtin"),
                     card=d.get("card", ""), confidence=float(d.get("confidence") or 0.0),
                     rule=d.get("rule", ""), note=d.get("note", ""),
                     review=bool(d.get("review")), trace=d.get("trace") or {},
                     quantity=d.get("quantity"), unit=d.get("unit", "ks"))
            for d in rows]


def _apply_confirmed_quantities(conn, decisions: list, question_ids: list) -> None:
    """#360: apply the warehouse-confirmed quantity of EVERY answered `item` question of this
    held order to its matching stored decision, so a correction made on ANY of the order's
    questions ships — not only the LAST one answered (a held order can wait on several).

    The confirmed value lives on `order_questions.quantity` (persisted by `teach.answer`'s
    COALESCE). For an un-corrected question this equals the decision's own extracted quantity
    (both come from the same extracted item at ask time), so applying it is a no-op there —
    which makes reading it back uniform for single- AND multi-question holds. Only `item`
    kind carries the editable qty (customer/date/mail/line do not), so only those are read.

    Matched by `item_key` (the same normalizer `teach.ask` keyed the question on), so it
    survives spacing/typo differences between `Decision.item_name` and the question wording.
    Each confirmed quantity is applied to exactly ONE decision (first match, then consumed):
    two identically-worded lines share one question but stay separate decisions that
    `merge_same_card` later sums, so applying the value to BOTH would double it.

    Runs on the RAW loaded decisions BEFORE `_redecide` — `_redecide` copies `d.quantity`
    verbatim (it only re-derives the card), so the correction survives the redecide and ship.
    """
    from . import memory, teach
    by_key: dict[str, object] = {}
    for qid in question_ids or []:
        q = teach.get(conn, qid)
        if not q or q.get("kind", "item") != "item" or q.get("quantity") is None:
            continue
        key = memory.item_key(q.get("wording", ""))
        if key:
            by_key[key] = q["quantity"]   # last write wins for a shared key (rare)
    if not by_key:
        return
    for d in decisions:
        k = memory.item_key(d.item_name)
        if k in by_key:
            d.quantity = by_key.pop(k)    # apply to exactly ONE decision per key


def place(conn, message_id: str, matched, order: dict, decisions, extracted: dict,
          question_ids: list[int]) -> int:
    """Record a held order. Returns its id."""
    # #431: a caller that cannot resolve the customer MUST hold on a placeholder
    # `customer.Matched(ean_edi="", …, rule="unmatched")`, never pass None. Passing None
    # used to raise an opaque `AttributeError: 'NoneType' … 'ean_edi'` deep in the INSERT
    # that a catch-all turned into a silent lost order (the 2026-09-14 incident). Refuse it
    # up front with a clear, diagnosable error so the combination can never slip through.
    if matched is None:
        raise ValueError(
            f"hold.place called with matched=None (message {message_id}) — the caller "
            "must resolve the customer or hold on a placeholder Matched(rule='unmatched') "
            "first (#431)")
    row = conn.execute(
        """INSERT INTO held_orders
               (message_id, customer_ean, customer_name, delivery_date, order_number,
                store, recipient_group, question_ids, order_json, extracted_json,
                decisions_json)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           RETURNING id""",
        (message_id, matched.ean_edi, matched.name, order.get("deliveryDate", ""),
         order.get("orderNumber", ""), order.get("store", ""),
         order.get("recipientGroup", ""), list(question_ids or []), Json(order),
         Json({"isChangeRequest": bool(extracted.get("isChangeRequest")),
               "unverified": extracted.get("unverified") or [],
               "notes": extracted.get("notes") or ""}),
         Json(_dump_decisions(decisions)))).fetchone()
    hid = int(row[0])
    log.info("holding order #%s for %s / %s (questions %s)", hid, matched.ean_edi,
             order.get("deliveryDate", ""), question_ids)
    return hid


def has_open(conn, message_id: str) -> bool:
    """True while any order of this message is still waiting — worker._claim's guard.

    #164: a `mail`-kind question ("is this even an order?") never gets a `held_orders`
    row (there is no order to hold — see `pipeline._run`'s "no orders" branch), so this
    now ALSO counts an open `mail`-kind question for the same message. Narrowed to
    `kind='mail'` deliberately: an open `item`/`customer`/`date`/`line` question already
    holding an order is covered by the `held_orders` check above, and widening this to
    EVERY open question kind could let a stray, unrelated open question on an already-
    shipped order's message block re-claiming something it has nothing to do with.
    """
    return conn.execute(
        """SELECT 1 FROM held_orders WHERE message_id = %s AND status = 'held'
           UNION ALL
           SELECT 1 FROM order_questions
            WHERE message_id = %s AND kind = 'mail' AND status = 'open'
           LIMIT 1""",
        (message_id, message_id)).fetchone() is not None


# --- reading ---------------------------------------------------------------

def _row(r) -> dict | None:
    if not r:
        return None
    return {"id": r[0], "message_id": r[1], "customer_ean": r[2], "customer_name": r[3] or "",
            "delivery_date": r[4] or "", "order_number": r[5] or "", "store": r[6] or "",
            "recipient_group": r[7] or "", "question_ids": list(r[8] or []),
            "order": r[9] or {}, "extracted": r[10] or {}, "decisions": r[11] or [],
            "status": r[12], "release_reason": r[13], "created_at": r[14],
            "released_at": r[15]}


_COLS = ("id, message_id, customer_ean, customer_name, delivery_date, order_number, "
         "store, recipient_group, question_ids, order_json, extracted_json, "
         "decisions_json, status, release_reason, created_at, released_at")


def get(conn, held_id: int) -> dict | None:
    return _row(conn.execute(
        f"SELECT {_COLS} FROM held_orders WHERE id = %s", (held_id,)).fetchone())


def list_held(conn, limit: int = 200) -> list[dict]:
    """Every order still waiting, oldest delivery date first — nothing waits invisibly."""
    rows = conn.execute(
        f"""SELECT {_COLS} FROM held_orders WHERE status = 'held'
            ORDER BY delivery_date, created_at LIMIT %s""", (limit,)).fetchall()
    return [d for r in rows if (d := _row(r)) is not None]


def _db_today(conn):
    return conn.execute("SELECT current_date").fetchone()[0]
