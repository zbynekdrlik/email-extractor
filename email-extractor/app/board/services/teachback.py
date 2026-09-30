"""„Doučiť z histórie: toto má byť iné" (#448 lane 7, spec §5).

For one item row of a history document the warehouse picks the RIGHT card; teachback writes
the SAME memory a question answer would (`item_memory` for orders / `dl_item_memory` for DL),
tagged `source='teachback'` (a distinct provenance from a real answer/shipment) — both are
honoured by the matcher's taught-first rung (`resolve()` reads `source IN ('human',
'teachback')`), so future matching of that wording uses the corrected card. It writes ONLY the
memory + a `teach` audit row (restorable from the Kôš); it NEVER modifies the shipped document,
`edi_sent`/`desadv_sent` or ORION. The card picker itself reuses the existing
`/api/board/products?scope=&q=` catalog search — no new endpoint.

#479 (owner order „zablokuj pridávanie produktov"): the API used to accept ANY typed number —
a code no card of ours has became authoritative taught memory. It now teaches ONLY onto an
existing card of the scope's effective catalog (`card_guard.refuse_typed_card`, the #477 403
`codex_only` refusal); a new card enters only via the CODEX pick on a question.
"""
from __future__ import annotations

from ...orders import card_guard, codex_cards, dl_memory, memory
from . import audit, history


class TeachbackError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _partner_ean(conn, scope: str, message_id: str) -> str:
    r = conn.execute(
        "SELECT result FROM order_runs WHERE message_id = %s AND shadow = false "
        "ORDER BY id DESC LIMIT 1", (message_id,)).fetchone()
    result = (r[0] or {}) if r else {}
    _name, ean, _docs = history.partner_and_docs(scope, result, "")
    return ean


def teach_item(conn, scope: str, message_id: str, actor: str, *,
               name: str, gtin: str, card: str = "") -> dict:
    """Teach `name → (gtin, card)` for this document's partner. Raises `TeachbackError`
    (400/404/409) on bad input / unknown document / unknown partner / already-taught, and
    `card_guard.CreateBlocked` (403, #479) when `gtin` is no card of the scope's catalog, and
    `codex_cards.CodexRefusal` (409) when it is our card but CODEX has no stock card for its
    code (a fresh CODEX list only)."""
    history.scope_categories(scope)  # raises ValueError (→400) for an unknown scope
    name, gtin, card = (name or "").strip(), (gtin or "").strip(), (card or "").strip()
    if not name or not gtin:
        raise TeachbackError(400, "chýba položka alebo číslo položky (karta)")
    if not history.is_history_document(conn, message_id, scope):
        raise TeachbackError(404, "doklad neexistuje")
    catalog = card_guard.catalog(conn, scope)
    card_guard.refuse_typed_card(catalog, gtin, error=card_guard.TEACH_CARD_ONLY)
    # a card of ours whose code CODEX lacks would teach a mapping CODEX refuses (409, #467/#479
    # — the same refusal as a question pick); a missing/stale list passes (fail-open)
    codex_cards.check_card_code(conn, gtin, name, card, catalog=catalog,
                                doc=codex_cards.DOC_ORDER if scope == "orders"
                                else codex_cards.DOC_DL)
    ean = _partner_ean(conn, scope, message_id)
    if not ean:
        raise TeachbackError(409, "nepodarilo sa zistiť partnera dokladu — doučenie sa nedá uložiť")
    note = f"doučené z histórie (doklad {message_id}): {name!r} → {card or gtin}"
    if scope == "dl":
        rid = dl_memory.add_dl_alias(conn, ean, name, gtin, card, source="teachback")
        table = "dl_item_memory"
    else:
        rid = memory.add_customer_alias(conn, ean, name, gtin, card, source="teachback")
        table = "item_memory"
    if rid is None:
        raise TeachbackError(409, "toto doučenie už existuje (rovnaká položka a karta)")
    audit.record(conn, actor=actor, table=table, row_id=rid, action="teach",
                 message_id=message_id, note=note,
                 after={"gtin": gtin, "card": card, "item": name,
                        "partner_ean": ean, "source": "teachback"})
    return {"ok": True, "id": rid, "table": table}
