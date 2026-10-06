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
    """Undo of a „vždy vynechať" answer — for every rule row THIS answer wrote (its audit
    `create`/`update` rows), the rule stays only while ANOTHER decision that still stands backs
    it (`dl_memory.not_stock_backed`: a question still answered „vždy vynechať", or the admin
    seed); otherwise it goes (soft, audited, Kôš-restorable) — never a silently kept drop the
    sklad took back, whatever order its answers are undone in. A backed rule this answer only
    re-asserted last gets its previous `created_at` back (the Kôš's own sanctioned restore of
    that `update`), so the precedence it had before returns. With no audit row at all (the
    best-effort audit write failed) the wording's rule is forgotten — the SAFE direction (the
    line is held and asked again). The caller reopens the question."""
    qid = q.get("id")
    payload = q.get("payload") or {}
    rows = conn.execute(
        "SELECT DISTINCT ON (row_id) row_id, id, action FROM audit_log "
        "WHERE table_name = 'dl_item_memory' AND question_id = %s "
        "AND action IN ('create', 'update') ORDER BY row_id, id DESC", (qid,)).fetchall()
    if not rows:
        dl_memory.forget_not_stock(conn, payload.get("supplier_ean", ""), q.get("wording", ""),
                                   actor=by, question_id=qid, message_id=q.get("message_id"))
        return
    for row_id, audit_id, action in rows:
        if not dl_memory.not_stock_backed(conn, row_id, exclude_question=qid):
            dl_memory.forget_not_stock(conn, payload.get("supplier_ean", ""),
                                       q.get("wording", ""), actor=by, question_id=qid,
                                       message_id=q.get("message_id"), row_id=int(row_id))
            continue
        newest = conn.execute(
            "SELECT id FROM audit_log WHERE table_name = 'dl_item_memory' AND row_id = %s "
            "AND action IN ('create', 'update') ORDER BY id DESC LIMIT 1",
            (row_id,)).fetchone()
        if action == "update" and newest and newest[0] == audit_id:
            from ..board.services import audit  # lazy: a leaf module, no import cycle
            try:
                audit.restore(conn, int(audit_id), by=by)
            except audit.RestoreError as e:
                log.warning("undo of question %s: re-assert %s not reverted (%s)", qid,
                            audit_id, e.message)
        log.info("undo of question %s: rule row %s stays — another standing decision backs "
                 "it", qid, row_id)
