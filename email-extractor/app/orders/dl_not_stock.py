"""#488 — the learned LINE-level rule „Nie je skladová položka — vždy vynechať": the question
halves and the engine helpers.

A recurring service line on a delivery note or an invoice taken as one (EKVIA „PREPRAVNÉ" =
transport; a deposit, packaging) has no stock card. Its only answer used to be „pošli bez tejto
položky" (`teach.DL_ITEM_SHIP_WITHOUT`), which is read back per MAIL
(`dl_document._skip_answered_item_keys`) — so the line held every weekly invoice on a fresh
`dl_item` question. Here the sklad decides it ONCE per (supplier, wording):

- **The rule** lives in `dl_item_memory` and every write of it is in `dl_memory` (the ONE module
  that writes that table): `remember_not_stock` / `forget_not_stock` / `not_stock_keys`, the
  sentinel `NOT_STOCK` (never a card — `dl_memory.resolve` skips it on every rung), audited, in
  Naučené sklad, Kôš-restorable. The newest live taught decision for the wording wins, so a card
  taught later sends the line back to the matcher.
- **The engine** (`partition`, called once per document by `dl_document`) takes the ruled lines
  out before matching — no model call, no question, no hold — and records them in the run items
  (rule `not_stock`) so História shows why they are not on the EDI.
- **The question halves** (`apply_answer` / `undo_answer`); `teach` only dispatches — kept out of
  the already-oversized `teach.py`, the precedent `dl_item_conflict` set.

Supplier-scoped on purpose: the same wording from another supplier is still asked (one human
decision per supplier + wording, visible and reversible — the owner's decision on the ticket).
"""
from __future__ import annotations

import logging

from . import dl_memory
from .memory import item_key

log = logging.getLogger("orders.dl_not_stock")

NOT_STOCK = dl_memory.NOT_STOCK
LABEL = dl_memory.NOT_STOCK_LABEL


def partition(conn, supplier_ean: str, items: list[dict], all_items: list[dict],
              message_id: str = "") -> tuple[list[dict], int]:
    """Split a document's lines into the ones to match and the ones this supplier's rule leaves
    off: returns `(stock_lines, ruled_count)`. Each ruled line is logged and appended to
    `all_items` (the run's `order_items`, rule `not_stock`) — never matched, never asked, never
    on the EDI."""
    keys = dl_memory.not_stock_keys(conn, supplier_ean)
    if not keys:
        return list(items), 0
    stock: list[dict] = []
    ruled = 0
    for item in items:
        if item_key(item.get("name", "")) not in keys:
            stock.append(item)
            continue
        ruled += 1
        log.info("DL message %s: %r is not a stock line for supplier %s (#488 rule) — left "
                 "off the EDI, nothing asked", message_id, item.get("name", ""), supplier_ean)
        all_items.append({"name": item.get("name", ""), "quantity": item.get("quantity"),
                          "unit": item.get("unit"), "gtin": None, "card": LABEL,
                          "confidence": 1.0, "rule": NOT_STOCK, "trace": {"not_stock": True}})
    return stock, ruled


def apply_answer(conn, cfg, q: dict, by: str) -> dict:
    """The sklad answered a `dl_item` question „vždy vynechať": learn the rule for the
    question's (supplier, wording), then give the mail its second chance — its reprocess leaves
    the line off (the rule, and this answer as the mail's own ship-without). Lazy `dl_worker`
    import, the same reason `teach._apply_dl_item` gives."""
    payload = q.get("payload") or {}
    rid = dl_memory.remember_not_stock(
        conn, payload.get("supplier_ean", ""), q.get("wording", ""), actor=by or "auto:teach",
        question_id=q.get("id"), message_id=q.get("message_id"))
    if rid is None:
        # no supplier / wording on the question: nothing to key a rule by — the answer still
        # ships THIS mail without the line (`_skip_answered_item_keys`), loudly
        log.warning("dl_item question %s answered „vždy vynechať“ but no rule could be keyed "
                    "(supplier %r, wording %r) — only this mail skips the line", q.get("id"),
                    payload.get("supplier_ean", ""), q.get("wording", ""))
    from . import dl_worker
    released = dl_worker.release_for_question(conn, cfg, q["id"])
    return {"released": released, "not_stock": True}


def undo_answer(conn, q: dict, by: str = "auto:teach") -> None:
    """Undo of a „vždy vynechať" answer takes back exactly what IT did: a rule it created goes
    (soft, audited); a rule it only re-asserted gets its previous `created_at` back through the
    Kôš's own sanctioned restore of that `update` row — only on the ROW this answer wrote, and
    only while no LATER answer that still stands (answered „vždy vynechať", not undone) has
    written that row since: that answer owns the rule now, so the undo leaves it and logs why.
    Nothing else counts as ownership (a Naučené edit of a rule row is refused; an undone answer
    no longer stands). With no audit row at all (the best-effort audit write failed) the rule is
    forgotten: the SAFE direction (the line is held and asked again), never a silently kept drop
    the sklad just took back. The caller reopens the question."""
    row = conn.execute(
        "SELECT id, action, row_id FROM audit_log WHERE table_name = 'dl_item_memory' "
        "AND question_id = %s AND action IN ('create', 'update') ORDER BY id DESC LIMIT 1",
        (q.get("id"),)).fetchone()
    if row and conn.execute(
            "SELECT 1 FROM audit_log a JOIN order_questions oq ON oq.id = a.question_id "
            "WHERE a.table_name = 'dl_item_memory' AND a.row_id = %s "
            "AND a.action IN ('create', 'update') AND a.id > %s AND oq.status = 'answered' "
            "AND oq.answer->>'choice' = %s LIMIT 1",
            (row[2], row[0], NOT_STOCK)).fetchone():
        log.warning("undo of question %s: rule row %s was written again by a later answer — "
                    "left as it is", q.get("id"), row[2])
        return
    if row and row[1] == "update":
        from ..board.services import audit  # lazy: a leaf module, no import cycle
        try:
            audit.restore(conn, int(row[0]), by=by)
        except audit.RestoreError as e:
            log.warning("undo of question %s: re-assert %s not reverted (%s)", q.get("id"),
                        row[0], e.message)
        return
    payload = q.get("payload") or {}
    dl_memory.forget_not_stock(conn, payload.get("supplier_ean", ""), q.get("wording", ""),
                               actor=by, question_id=q.get("id"),
                               message_id=q.get("message_id"),
                               row_id=int(row[2]) if row else None)
