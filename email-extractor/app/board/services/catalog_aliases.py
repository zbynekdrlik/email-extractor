"""Per-card alias manager for the nástenka product tabs (#445, spec §4).

Split out of `catalog.py` to keep each service module ≤200 r. (spec §3). Handles the „aliasy
položky na karte" affordance: listing the wording→card memory rows that point AT a card
(per-customer + global for orders; per-supplier for DL), searching cards BY those wordings,
and adding/removing them. Every write DELEGATES to the existing `orders.memory` /
`orders.dl_memory` write paths (`board.md`: never copy the engines) and records an
`audit_log` row via the leaf `audit.record`. Removal is a SOFT delete (spec §5).
"""
from __future__ import annotations

from ...httpapi_common import _fold
from ...orders import dl_memory, memory
from . import audit

_CURATED = ("human", "sheet-import")


def alias_gtins(conn, tables: tuple[str, ...], needle: str) -> set[str]:
    """The gtins of cards whose learned wording matches `needle` (fold-compared like every
    other board search). Bounded to DISTINCT (gtin, wording) per table — this includes ship
    history, not only curated aliases, which is desirable for SEARCH (find a card by any
    wording ever seen for it). The per-card alias LIST (`card_aliases`) is the one that
    restricts to `_CURATED`."""
    hits: set[str] = set()
    for t in tables:
        for gtin, raw in conn.execute(
                f"SELECT DISTINCT gtin, item_raw FROM {t} WHERE deleted_at IS NULL").fetchall():
            if raw and needle in _fold(raw):
                hits.add(gtin)
    return hits


def card_aliases(conn, scope: str, gtin: str) -> list[dict]:
    """Every curated alias pointing at this card, newest first. Orders: global + per-customer;
    DL: per-supplier. Only curated sources (human/sheet-import) — raw ship history is not a
    removable alias and would be noise."""
    out: list[dict] = []
    if scope == "orders":
        for r in conn.execute(
                "SELECT id, item_raw, gtin, card, taught_by, created_at "
                "FROM global_item_memory WHERE gtin = %s AND deleted_at IS NULL "
                "ORDER BY created_at DESC", (gtin,)).fetchall():
            out.append({"id": int(r[0]), "scope": "global", "wording": r[1] or "",
                        "gtin": r[2], "card": r[3] or "", "ean": "", "source": r[4] or "",
                        "created_at": r[5].isoformat() if r[5] else None})
        for r in conn.execute(
                "SELECT id, customer_ean, item_raw, gtin, card, source, created_at "
                "FROM item_memory WHERE gtin = %s AND deleted_at IS NULL "
                "AND source = ANY(%s) ORDER BY created_at DESC",
                (gtin, list(_CURATED))).fetchall():
            out.append({"id": int(r[0]), "scope": "customer", "wording": r[2] or "",
                        "gtin": r[3], "card": r[4] or "", "ean": r[1] or "",
                        "source": r[5] or "", "created_at": r[6].isoformat() if r[6] else None})
    else:
        for r in conn.execute(
                "SELECT id, supplier_ean, item_raw, gtin, card, source, created_at "
                "FROM dl_item_memory WHERE gtin = %s AND deleted_at IS NULL "
                "AND source = ANY(%s) ORDER BY created_at DESC",
                (gtin, list(_CURATED))).fetchall():
            out.append({"id": int(r[0]), "scope": "dl", "wording": r[2] or "",
                        "gtin": r[3], "card": r[4] or "", "ean": r[1] or "",
                        "source": r[5] or "", "created_at": r[6].isoformat() if r[6] else None})
    return out


def add_alias(conn, scope: str, gtin: str, wording: str, ean: str, actor: str) -> dict:
    """Add a card alias via the existing memory write paths + an audit row. Returns
    `{"id": id}`; `{"error": ...}` when it is a duplicate (→ 409) or a field is missing (→ 400)."""
    if scope not in ("orders", "dl"):
        return {"error": f"neznámy scope {scope!r}"}
    wording = (wording or "").strip()
    ean = (ean or "").strip()
    if not wording:
        return {"error": "chýba znenie"}
    if scope == "orders":
        if ean:
            rid, table = memory.add_customer_alias(conn, ean, wording, gtin, ""), "item_memory"
        else:
            rid = memory.add_global_alias(conn, wording, gtin, "", by=actor)
            table = "global_item_memory"
    else:
        if not ean:
            return {"error": "pri sklade treba EAN dodávateľa"}
        rid, table = dl_memory.add_dl_alias(conn, ean, wording, gtin, ""), "dl_item_memory"
    if rid is None:
        return {"error": "toto znenie je už priradené"}
    audit.record(conn, actor=actor, table=table, row_id=rid, action="create",
                 after={"wording": wording, "gtin": gtin, "ean": ean})
    return {"id": rid}


def remove_alias(conn, scope: str, alias_scope: str, row_id: int, ean: str, actor: str) -> bool:
    """Soft-delete a card alias via the existing memory delete paths + an audit row. False
    when nothing matched (→ 404)."""
    if alias_scope == "global":
        ok, table = memory.delete_global_row(conn, row_id), "global_item_memory"
    elif alias_scope == "customer":
        ok, table = memory.delete_item_memory_row(conn, row_id, ean), "item_memory"
    elif alias_scope == "dl":
        ok, table = dl_memory.delete_dl_item_memory_row(conn, row_id, ean), "dl_item_memory"
    else:
        return False
    if ok:
        audit.record(conn, actor=actor, table=table, row_id=row_id, action="delete")
    return ok
