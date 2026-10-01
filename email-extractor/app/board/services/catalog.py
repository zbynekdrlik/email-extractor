"""Catalog service for the unified nástenka lane 4 (#445, spec §4).

Logic + SQL for the two product tabs (Produkty sklad = DL, Produkty objednávky = orders).
Everything here is a THIN pohľad over machinery that already exists — it CALLS
`orders.snapshot`/`dl_snapshot` (upsert/retire/rebuild, the exact functions the old
`/znalosti` endpoints call), never copies their logic (`board.md`: "call the existing
engines, never copy them"). What IS genuinely new: one board read over BOTH catalog scopes
(reachable by the `sklad` role, DL was admin-only before), searchable + paged, and soft
delete + audit. The per-card ALIAS manager lives in the sibling `catalog_aliases.py` (kept
separate to hold each module ≤200 r., spec §3). Every change writes an `audit_log` row via
the leaf `audit.record` (spec §5). #477: the tabs only EDIT and delete — a new card enters the
catalog solely through the CODEX pick on a question (`orders.card_guard.add_from_codex`).
"""
from __future__ import annotations

from ...httpapi_common import _fold
from ...orders import card_guard, codex_cards, dl_snapshot, snapshot
from . import audit, catalog_aliases

PAGE_SIZE = 50
# #467: `?codex=issues` on Produkty sklad lists only the cards whose name drifted from CODEX's
# or whose number CODEX has no stock card for.
_CODEX_ISSUES = ("drift", "missing")


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
        "data_fields": ("alias",),
    },
    "dl": {
        "for_management": dl_snapshot.dl_catalog_for_management,
        "search_extra": "doplnok",
        "upsert": _dl_upsert,
        "retire": _dl_retire,
        "override_table": "dl_catalog_overrides",
        "alias_tables": ("dl_item_memory",),
        "data_fields": ("doplnok", "mass", "sklad", "cena"),
    },
}


def _scope(scope: str) -> dict:
    if scope not in _SCOPES:
        raise ValueError(f"neznámy scope {scope!r}")
    return _SCOPES[scope]


def list_products(conn, *, scope: str, q: str = "", page: int = 0, codex: str = "") -> dict:
    """One scope's effective catalog, search-filtered (číslo položky / názov / doplnok /
    aliasy) and paged. Raises `ValueError` for an unknown scope (the route → 400).

    #467: the DL scope annotates every card with its CODEX status (ok / drift + the CODEX name /
    missing) and returns the list's freshness as `codex`; `codex="issues"` keeps only drift and
    missing cards."""
    cfg = _scope(scope)
    rows = cfg["for_management"](conn)
    codex_meta = None
    if scope == "dl":
        cards = codex_cards.load(conn)
        rows = codex_cards.annotate(rows, cards)
        codex_meta = codex_cards.meta_for(cards)
        if codex == "issues":
            rows = [r for r in rows if r["codex"]["status"] in _CODEX_ISSUES]
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
            "has_more": start + PAGE_SIZE < total, "codex": codex_meta}


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
    """EDIT a card via the SAME snapshot machinery /znalosti uses, + an audit row. Raises
    `ValueError` (→ 400) when gtin/name are missing, `card_guard.CreateBlocked` (→ 403) for a
    number we do not have or a `new: true` POST (#477: a card enters the catalog only by the
    CODEX pick, `card_guard.add_from_codex` — never typed here), and `codex_cards.CardRefused`
    (→ 409) for a DL card whose number CODEX lacks (#467, fail-open)."""
    cfg = _scope(scope)
    gtin = str(body.get("gtin") or "").strip()
    name = str(body.get("name") or "").strip()
    if not (gtin and name):
        raise ValueError("chýba číslo položky alebo názov")
    rows = cfg["for_management"](conn)
    if body.get("new"):
        raise card_guard.blocked()
    card_guard.refuse_typed_card(rows, gtin)
    if scope == "dl":
        codex_cards.check_card_code(conn, gtin, name, catalog=rows)
    before: dict = next((r for r in rows if r["gtin"] == gtin), {})
    cfg["upsert"](conn, gtin, name, body)
    # the curated data fields the save CHANGED are audited too (#478 review 42: the CODEX sync
    # tells a human's fix of the data from a name-only save, e.g. „Prevziať názov z CODEXu")
    now: dict = next((r for r in cfg["for_management"](conn) if r["gtin"] == gtin), {})
    changed = {k: now.get(k) for k in cfg["data_fields"] if now.get(k) != before.get(k)}
    audit.record(conn, actor=actor, table=cfg["override_table"], row_id=gtin, action="update",
                 after={"gtin": gtin, "name": name, **changed})
    return {"action": "update"}


def delete(conn, scope: str, gtin: str, actor: str) -> bool:
    """Soft-delete a card (retire sets retired+deleted_at, then rebuild) + an audit row.
    False when the card does not exist (→ 404). Never a hard DELETE (spec §5)."""
    cfg = _scope(scope)
    if not cfg["retire"](conn, gtin):
        return False
    audit.record(conn, actor=actor, table=cfg["override_table"], row_id=gtin, action="delete")
    return True
