"""Hold an order while its question is unanswered — but only until the delivery date (#93).

Shipping the matched part of an order now and the taught line later would write TWO ORION
documents for ONE delivery day — the exact defect fixed in #81.1 (40 and 10 delivered instead
of 50). So a pending question (#88's teach-once loop) holds its WHOLE order: nothing is built,
nothing uploaded, until either the question is answered or the delivery date arrives.

Two release paths, both funnelled through the SAME `pipeline._ship_one` / `edi.claim_send`
ledger the live pipeline already ships through, so a late answer arriving after the deadline
has already shipped can never double-upload:

- `release_for_question` — fires right after `teach.answer` settles a question. Only once
  EVERY question id a held order is waiting on has been answered does it re-check its stored
  decisions against fresh memory (no LLM call — `match.decide_without_model` again) and ship.
- `release_due` — the deadline backstop, a periodic sweep. Ships whatever matched using the
  ORIGINALLY stored decisions, unchanged — today's "ship what matched, name what's missing"
  behaviour, now gated on the delivery date instead of firing immediately.

`worker._claim` excludes any message with an open (`status='held'`) row here, so a held
message is never silently re-run through the LLM while it waits.
"""
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


def place(conn, message_id: str, matched, order: dict, decisions, extracted: dict,
          question_ids: list[int]) -> int:
    """Record a held order. Returns its id."""
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
    """True while any order of this message is still waiting — worker._claim's guard."""
    return conn.execute(
        "SELECT 1 FROM held_orders WHERE message_id = %s AND status = 'held' LIMIT 1",
        (message_id,)).fetchone() is not None


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
    return [_row(r) for r in rows]


# --- releasing ---------------------------------------------------------------

def _redecide(conn, customer_ean: str, decisions: list) -> list:
    """Give every stored line one more chance against FRESH memory (#93) — a wording just
    taught, per-customer or globally, must decide it NOW, without another LLM call.

    Safe to run over every decision, not only the ones that were pending: an item that
    already resolved before the hold (catalog, model, prior history) asks the same
    no-model question again and gets back the identical answer it already had, since
    `decide_without_model` only returns something MORE certain than what is already
    stored — never something weaker.
    """
    from . import memory
    from .match import decide_without_model, merge_same_card

    changed = False
    out = []
    for d in decisions:
        recalled = memory.resolve(conn, customer_ean, d.item_name)
        global_recalled = memory.resolve_global(conn, d.item_name)
        fresh = decide_without_model(d.item_name, [], recalled=recalled,
                                     global_recalled=global_recalled)
        if fresh is not None and (str(fresh.gtin) != str(d.gtin) or fresh.rule != d.rule):
            fresh.quantity, fresh.unit = d.quantity, d.unit
            out.append(fresh)
            changed = True
        else:
            out.append(d)
    return merge_same_card(out) if changed else decisions


def _do_release(conn, cfg, row: dict, release_reason: str, upload, post,
                redecide: bool) -> dict:
    from . import customer as customer_mod
    from . import report
    from . import upload as upload_mod
    from .pipeline import _ship_one  # lazy: pipeline imports this module at its own top

    upload = upload or (lambda c, name, content: upload_mod.put(c, name, content))
    post = post or (lambda c, html, **kw: report.post_from_config(c, html))

    matched = customer_mod.Matched(ean_edi=row["customer_ean"], name=row["customer_name"],
                                   confidence=1.0, rule="held_release", note="")
    decisions = _load_decisions(row["decisions"])
    if redecide:
        decisions = _redecide(conn, row["customer_ean"], decisions)

    status, preview = _ship_one(conn, cfg, {"message_id": row["message_id"]}, row["order"],
                                matched, decisions, row["extracted"], False, upload, post)
    conn.execute(
        """UPDATE held_orders SET status = 'released', release_reason = %s, released_at = now()
            WHERE id = %s""", (release_reason, row["id"]))
    log.info("released held order #%s (%s) for %s / %s -> %s", row["id"], release_reason,
             row["customer_ean"], row["delivery_date"], status)
    if not has_open(conn, row["message_id"]):
        # Every order this message produced has now shipped, been reviewed, or been
        # released — the message is finally done (#93: it stayed unprocessed while it held).
        conn.execute(
            """UPDATE messages
                  SET processed = true, processed_at = now(), processed_by = 'ai_orders',
                      processing_at = NULL
                WHERE message_id = %s""", (row["message_id"],))
    return {"id": row["id"], "status": status, "preview": preview}


def release_for_question(conn, cfg, qid: int, upload=None, post=None) -> list[dict]:
    """Release every held order whose LAST open question was just answered.

    A held order may be waiting on more than one wording; it ships only once every one of
    its question ids is answered. Releasing on the first answer would ship a still-guessed
    line the same way the immediate-partial-ship bug did (#81.1) — so a sibling still-open
    question keeps the whole order held.
    """
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM held_orders WHERE %s = ANY(question_ids) AND status = 'held'",
        (qid,)).fetchall()]
    released = []
    for hid in ids:
        row = get(conn, hid)
        if not row:
            continue
        remaining = conn.execute(
            "SELECT count(*) FROM order_questions WHERE id = ANY(%s) AND status <> 'answered'",
            (row["question_ids"],)).fetchone()[0]
        if remaining:
            continue
        released.append(_do_release(conn, cfg, row, "answered", upload, post, redecide=True))
    return released


def release_due(conn, cfg, upload=None, post=None, today: str = "") -> list[dict]:
    """The deadline backstop: whatever is still waiting when its delivery date arrives ships
    what matched — exactly like the pipeline always has, just no longer immediate."""
    today = today or str(_db_today(conn))
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM held_orders WHERE status = 'held'").fetchall()]
    released = []
    for hid in ids:
        row = get(conn, hid)
        if not row or not is_past_deadline(row["delivery_date"], today):
            continue
        released.append(_do_release(conn, cfg, row, "deadline", upload, post, redecide=False))
    return released


def _db_today(conn):
    return conn.execute("SELECT current_date").fetchone()[0]
