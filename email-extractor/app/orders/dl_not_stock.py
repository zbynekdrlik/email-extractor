"""#488 — the learned LINE-level rule „Nie je skladová položka — vždy vynechať".

A recurring service line on a delivery note or an invoice taken as one (EKVIA „PREPRAVNÉ" =
transport; a deposit, packaging) has no stock card. Its only answer used to be „pošli bez tejto
položky" (`teach.DL_ITEM_SHIP_WITHOUT`), which is read back per MAIL
(`dl_document._skip_answered_item_keys`) — so the line held every weekly invoice on a fresh
`dl_item` question. Here the sklad decides it ONCE per (supplier, wording):

- **The rule** is a `dl_item_memory` row whose `gtin` is the sentinel `NOT_STOCK` — never a card
  (`dl_memory.resolve` filters it out of every rung, so the memory-conflict verdict, the CODEX
  guard and the ask pre-check never see it) — `source='human'`, `card=LABEL`, keyed by the same
  (supplier_ean, item_key) the `dl_item` question uses. `remember` writes it with an audited
  `create` (the Kôš lists it, „Vrátiť" soft-deletes it), Naučené sklad lists it, `forget`
  soft-deletes it with an audited `delete` (restorable).
- **The engine** reads `keys(conn, supplier_ean)` once per document: the wordings whose NEWEST live
  curated row (`source IN human/teachback`, the taught-first rung of `dl_memory.resolve`) is the
  rule — a card taught LATER for the same wording wins again. `dl_document` leaves such a line off
  the EDI before matching (no model call, no question, no hold) and keeps it in the run's items
  (`history_item`, rule `not_stock`) so História shows why.
- **The question halves** (`apply_answer` / `undo_answer`) live here, `teach` only dispatches —
  kept out of the already-oversized `teach.py`, the precedent `dl_item_conflict` set.

Supplier-scoped on purpose: the same wording from another supplier is still asked (one human
decision per supplier + wording, visible and reversible — the owner's decision on the ticket).
"""
from __future__ import annotations

import logging

from .memory import item_key

log = logging.getLogger("orders.dl_not_stock")

# the answer choice on a dl_item question AND the stored `dl_item_memory.gtin` of the rule —
# never a GTIN (letters), so it can never collide with a catalog card
NOT_STOCK = "not_stock"
LABEL = "Nie je skladová položka — vždy vynechať"
# the curated decisions `dl_memory.resolve`'s taught-first rung reads — the newest one wins
_DECISIONS = ("human", "teachback")


def remember(conn, supplier_ean: str, wording: str, *, actor: str, question_id=None,
             message_id=None) -> int | None:
    """Learn the rule for (supplier, wording); returns its `dl_item_memory` id (None when a
    field is missing). A live rule is re-asserted as the newest decision (`created_at`), never
    duplicated; one soft-deleted the same day (an undo / a Kôš removal) is revived — the UNIQUE
    identity is not partial. A new or revived rule gets a `create` audit row tied to the
    question, which is what the Kôš lists and reverts."""
    key = item_key(wording)
    if not (supplier_ean and key):
        return None
    live = conn.execute(
        "UPDATE dl_item_memory SET created_at = now() WHERE supplier_ean = %s AND item_key = %s"
        " AND gtin = %s AND deleted_at IS NULL RETURNING id",
        (str(supplier_ean), key, NOT_STOCK)).fetchone()
    if live:
        log.info("not-stock rule for %r (%s) already live (row %s) — re-asserted as newest",
                 wording, supplier_ean, live[0])
        return int(live[0])
    rid = int(conn.execute(
        """INSERT INTO dl_item_memory
               (supplier_ean, item_key, item_raw, gtin, card, delivered_on, cnt, source)
           VALUES (%s, %s, %s, %s, %s, current_date, 1, 'human')
           ON CONFLICT (supplier_ean, item_key, gtin, delivered_on, cnt) DO UPDATE
              SET deleted_at = NULL, created_at = now(), source = 'human',
                  item_raw = EXCLUDED.item_raw, card = EXCLUDED.card
           RETURNING id""",
        (str(supplier_ean), key, str(wording), NOT_STOCK, LABEL)).fetchone()[0])
    _audit(conn, actor=actor, row_id=rid, action="create", question_id=question_id,
           message_id=message_id,
           after={"supplier_ean": str(supplier_ean), "item_raw": str(wording),
                  "item_key": key, "gtin": NOT_STOCK, "card": LABEL, "source": "human"},
           note="#488 nie je skladová položka — vždy vynechať")
    log.warning("not-stock rule learned: %r of supplier %s is never a stock line (row %s, "
                "question %s, by %s)", wording, supplier_ean, rid, question_id, actor)
    return rid


def forget(conn, supplier_ean: str, wording: str, *, actor: str, question_id=None,
           message_id=None) -> list[int]:
    """Soft-delete every live rule for (supplier, wording), each with a `delete` audit row (the
    Kôš „Vrátiť" brings it back). Returns the removed ids."""
    rows = conn.execute(
        "UPDATE dl_item_memory SET deleted_at = now() WHERE supplier_ean = %s AND item_key = %s"
        " AND gtin = %s AND deleted_at IS NULL RETURNING id",
        (str(supplier_ean), item_key(wording), NOT_STOCK)).fetchall()
    ids = [int(r[0]) for r in rows]
    for rid in ids:
        _audit(conn, actor=actor, row_id=rid, action="delete", question_id=question_id,
               message_id=message_id, note="#488 pravidlo „vždy vynechať“ vrátené")
    log.warning("not-stock rule for %r (%s) removed: rows %s", wording, supplier_ean, ids)
    return ids


def keys(conn, supplier_ean: str) -> set[str]:
    """The `item_key`s of this supplier's wordings whose NEWEST live curated decision is the
    rule — the lines `dl_document` leaves off the EDI without asking."""
    if not supplier_ean:
        return set()
    rows = conn.execute(
        """SELECT item_key FROM (
               SELECT DISTINCT ON (item_key) item_key, gtin FROM dl_item_memory
                WHERE supplier_ean = %s AND source = ANY(%s) AND deleted_at IS NULL
                ORDER BY item_key, created_at DESC NULLS LAST, id DESC) newest
            WHERE gtin = %s""",
        (str(supplier_ean), list(_DECISIONS), NOT_STOCK)).fetchall()
    return {r[0] for r in rows}


def excludes(rule_keys: set[str], wording: str) -> bool:
    """Is this line ruled „not a stock line" for its supplier (`rule_keys` = `keys(...)`)?"""
    return bool(rule_keys) and item_key(wording) in rule_keys


def history_item(item: dict) -> dict:
    """The run-items entry (`order_items`, História) for a ruled line — never on the EDI."""
    return {"name": item.get("name", ""), "quantity": item.get("quantity"),
            "unit": item.get("unit"), "gtin": None, "card": LABEL, "confidence": 1.0,
            "rule": NOT_STOCK, "trace": {"not_stock": True}}


def apply_answer(conn, cfg, q: dict, by: str) -> dict:
    """The sklad answered a `dl_item` question „vždy vynechať": learn the rule for the
    question's (supplier, wording), then give the mail its second chance — its reprocess leaves
    the line off (the rule, and this answer as the mail's own ship-without). Lazy `dl_worker`
    import, the same reason `teach._apply_dl_item` gives."""
    payload = q.get("payload") or {}
    remember(conn, payload.get("supplier_ean", ""), q.get("wording", ""),
             actor=by or "auto:teach", question_id=q.get("id"), message_id=q.get("message_id"))
    from . import dl_worker
    released = dl_worker.release_for_question(conn, cfg, q["id"])
    return {"released": released, "not_stock": True}


def undo_answer(conn, q: dict) -> None:
    """Undo of a „vždy vynechať" answer removes its rule (soft, audited); the caller reopens
    the question."""
    payload = q.get("payload") or {}
    forget(conn, payload.get("supplier_ean", ""), q.get("wording", ""), actor="auto:teach",
           question_id=q.get("id"), message_id=q.get("message_id"))


def _audit(conn, **kw) -> None:
    """Best-effort `dl_item_memory` audit row (lazy import of the leaf audit module, the
    `dl_memory._audit` shape); an audit failure never breaks the rule write it records."""
    try:
        from ..board.services import audit
        audit.record(conn, table="dl_item_memory", **kw)
    except Exception:
        log.exception("audit of dl_item_memory row %s failed", kw.get("row_id"))
