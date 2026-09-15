"""Catalog service for the unified nástenka lane 4 (#445, spec §4).

Logic + SQL for the two product tabs (Produkty sklad = DL, Produkty objednávky = orders).
Everything here is a THIN pohľad over machinery that already exists — it CALLS
`orders.snapshot`/`dl_snapshot` (upsert/retire/rebuild, the exact functions the old
`/znalosti` endpoints call), never copies their logic (`board.md`: "call the existing
engines, never copy them"). What IS genuinely new: one board read over BOTH catalog scopes
(reachable by the `sklad` role, DL was admin-only before), searchable + paged, and soft
delete + audit. The per-card ALIAS manager lives in the sibling `catalog_aliases.py` (kept
separate to hold each module ≤200 r., spec §3). Every change writes an `audit_log` row via
the leaf `audit.record` (spec §5).
"""
from __future__ import annotations

from ...httpapi_common import _fold
from ...orders import dl_snapshot, snapshot
from . import audit, catalog_aliases

PAGE_SIZE = 50


def _orders_upsert(conn, gtin, name, body):
    # #383 alias tri-state: a `doplnok`/`alias` key present (even "") sets it; absent → None
    # (don't touch — a name-only edit never wipes an existing alias).
    if "doplnok" in body:
        alias: str | None = str(body.get("doplnok") or "").strip()
    elif "alias" in body:
        alias = str(body.get("alias") or "").strip()
    else:
        alias = None
    snapshot.upsert_catalog_card(conn, gtin, name, alias=alias)
    snapshot.rebuild_from_overrides(conn)


def _dl_upsert(conn, gtin, name, body):
    # keep any field the editor did not send at its current value (never silently wipe it).
    cur = next((r for r in dl_snapshot.dl_catalog_for_management(conn) if r["gtin"] == gtin), {})

    def _val(key, parse=False):
        if key in body:
            return dl_snapshot.parse_number(body.get(key)) if parse else str(body.get(key) or "").strip()
        return cur.get(key) if parse else (cur.get(key) or "")

    dl_snapshot.upsert_dl_catalog_card(
        conn, gtin, name, doplnok=_val("doplnok"), mass=_val("mass", parse=True),
        sklad=_val("sklad"), cena=_val("cena", parse=True))
    dl_snapshot.dl_rebuild_from_overrides(conn)


def _orders_retire(conn, gtin):
    if not snapshot.retire_catalog_card(conn, gtin):
        return False
    snapshot.rebuild_from_overrides(conn)
    return True


def _dl_retire(conn, gtin):
    if not dl_snapshot.retire_dl_catalog_card(conn, gtin):
        return False
    dl_snapshot.dl_rebuild_from_overrides(conn)
    return True


_SCOPES = {
    "orders": {
        "for_management": snapshot.catalog_for_management,
        "search_extra": "alias",
        "upsert": _orders_upsert,
        "retire": _orders_retire,
        "override_table": "catalog_overrides",
        "alias_tables": ("global_item_memory", "item_memory"),
    },
    "dl": {
        "for_management": dl_snapshot.dl_catalog_for_management,
        "search_extra": "doplnok",
        "upsert": _dl_upsert,
        "retire": _dl_retire,
        "override_table": "dl_catalog_overrides",
        "alias_tables": ("dl_item_memory",),
    },
}


def _scope(scope: str) -> dict:
    if scope not in _SCOPES:
        raise ValueError(f"neznámy scope {scope!r}")
    return _SCOPES[scope]


def list_products(conn, *, scope: str, q: str = "", page: int = 0) -> dict:
    """One scope's effective catalog, search-filtered (číslo položky / názov / doplnok /
    aliasy) and paged. Raises `ValueError` for an unknown scope (the route → 400)."""
    cfg = _scope(scope)
    rows = cfg["for_management"](conn)
    if q:
        needle = _fold(q)
        alias_gtins = catalog_aliases.alias_gtins(conn, cfg["alias_tables"], needle)
        extra = cfg["search_extra"]
        rows = [r for r in rows
                if needle in _fold(r["name"]) or needle in _fold(r["gtin"])
                or needle in _fold(r.get(extra) or "") or r["gtin"] in alias_gtins]
    rows.sort(key=lambda r: _fold(r["name"]))
    total = len(rows)
    page = max(0, int(page))
    start = page * PAGE_SIZE
    window = rows[start:start + PAGE_SIZE]
    return {"items": window, "page": page, "page_size": PAGE_SIZE, "total": total,
            "has_more": start + PAGE_SIZE < total}


def card_detail(conn, scope: str, gtin: str) -> dict | None:
    """One card's fields + its aliases (per-customer + global for orders; per-supplier for
    DL) + the used-by count. `None` when the card is not in the effective catalog (→ 404)."""
    cfg = _scope(scope)
    card = next((r for r in cfg["for_management"](conn) if r["gtin"] == gtin), None)
    if card is None:
        return None
    aliases = catalog_aliases.card_aliases(conn, scope, gtin)
    return {"card": card, "aliases": aliases, "counts": {"aliases": len(aliases)}}


def upsert(conn, scope: str, body: dict, actor: str) -> dict:
    """Create or edit a card via the SAME snapshot machinery /znalosti uses, + an audit row.
    Raises `ValueError` (→ 400) when gtin/name are missing."""
    cfg = _scope(scope)
    gtin = str(body.get("gtin") or "").strip()
    name = str(body.get("name") or "").strip()
    if not (gtin and name):
        raise ValueError("chýba číslo položky alebo názov")
    existed = any(r["gtin"] == gtin for r in cfg["for_management"](conn))
    cfg["upsert"](conn, gtin, name, body)
    action = "update" if existed else "create"
    audit.record(conn, actor=actor, table=cfg["override_table"], row_id=gtin, action=action,
                 after={"gtin": gtin, "name": name})
    return {"action": action}


def delete(conn, scope: str, gtin: str, actor: str) -> bool:
    """Soft-delete a card (retire sets retired+deleted_at, then rebuild) + an audit row.
    False when the card does not exist (→ 404). Never a hard DELETE (spec §5)."""
    cfg = _scope(scope)
    if not cfg["retire"](conn, gtin):
        return False
    audit.record(conn, actor=actor, table=cfg["override_table"], row_id=gtin, action="delete")
    return True
