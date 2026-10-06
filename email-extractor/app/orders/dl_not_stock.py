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
- **The engine** (`dl_document` reads `dl_memory.not_stock_keys` once per document; `leave_off`
  per line) takes the ruled lines out before matching — no model call, no question, no hold —
  and records them in the run items in place (rule `not_stock`) so História shows why they are
  not on the EDI.
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


def leave_off(rule_keys: set[str], item: dict, all_items: list[dict], message_id: str,
              supplier_ean: str) -> bool:
    """Is this line ruled „not a stock line" for its supplier (`rule_keys` =
    `dl_memory.not_stock_keys`, read once per document)? A ruled line is logged and recorded in
    `all_items` IN ITS PLACE (the run's `order_items`, rule `not_stock` — História keeps the
    document's line order) and is never matched, never asked, never on the EDI."""
    if not rule_keys or item_key(item.get("name", "")) not in rule_keys:
        return False
    log.info("DL message %s: %r is not a stock line for supplier %s (#488 rule) — left off "
             "the EDI, nothing asked", message_id, item.get("name", ""), supplier_ean)
    all_items.append({"name": item.get("name", ""), "quantity": item.get("quantity"),
                      "unit": item.get("unit"), "gtin": None, "card": LABEL,
                      "confidence": 1.0, "rule": NOT_STOCK, "trace": {"not_stock": True}})
    return True


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
    """Undo of a „vždy vynechať" answer ALWAYS takes the rule back — every live rule row of the
    question's (supplier, wording), soft + audited (`delete` rows tied to the question, so the
    Kôš „Vrátiť" brings it back), whoever else also decided it (another answer, the admin seed).
    The SAFE direction by construction: the line is held and asked again — never a silently kept
    drop the sklad just took back, and nothing to infer from the audit history. The caller
    reopens the question."""
    payload = q.get("payload") or {}
    dl_memory.forget_not_stock(conn, payload.get("supplier_ean", ""), q.get("wording", ""),
                               actor=by, question_id=q.get("id"),
                               message_id=q.get("message_id"))
