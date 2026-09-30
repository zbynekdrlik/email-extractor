"""The ONE way a card enters our catalog: picked from the CODEX stock-card list (#477).

Owner order 2026-09-30 (#477, after the #467 incident — the sklad saw a stale name under a
code, typed a code of its own invention and broke orders AND delivery notes): CODEX is the only
source of truth for a card's code and name, so nobody TYPES a new card any more.

- `refuse_typed_card` / `blocked()` (`CreateBlocked`, HTTP 403) — every free-typed creation
  path refuses a number our catalog does not have: the board Produkty „Pridať", the inline
  „➕ Nová karta" on an item / dl_item question, the legacy `/api/znalosti/*` upserts. Editing an
  existing card (name, doplnok, mass/sklad/cena) and the soft delete stay.
- `codex_choices` — what „Vybrať kartu z CODEXu" on a question offers: the pickable CODEX cards
  (`codex_cards.pickable`), scoped per catalog, searched by name / EAN kód / CODEX card no.
- `add_from_codex` — the ONE creation: exactly the picked code + its CODEX name go into the
  catalog override, audited as `create` (the Kôš can take it back). A code we already have is
  only SELECTED (never duplicated or overwritten — an upsert would wipe mass/sklad/cena); a code
  whose card sits in the Kôš RESTORES that card exactly as it was (also an audited `create`, so
  pick → Kôš „Vrátiť" → pick again always works). One human pick = one card, never a bulk
  import (#337).

Scope of a pick (measured on prod 2026-09-30; the list rules live in `codex_cards.pickable`):
- orders: CODEX `stredisko 1 / sklad 1` (the finished goods) — 130/130 cards of the orders
  snapshot and every override sit there;
- dl: CODEX `stredisko 1`, every sklad (100 kg-tracked, 1, 200, 500, 600, 625, 650, 700) —
  never the other strediská (4, 40x-45x), the junk sklady #337 banned.
Only active rows. A stale list still offers its last known cards (the board warns); a list that
never arrived offers nothing. A new DL card takes its `sklad` from CODEX (100 preferred, else the
lowest — reproduces the `sklad` of all 465 of our DL cards that carry one); mass/cena stay blank
(CODEX does not push them — the #462 hold asks for a kg card's mass).
"""
from __future__ import annotations

import logging

from . import codex_cards, dl_match, dl_snapshot, snapshot
from .codex_cards import CardRefused

log = logging.getLogger("orders.card_guard")

CODEX_ONLY = ("Nové karty sa pridávajú len výberom z CODEXu — pri otázke klikni „Vybrať kartu "
              "z CODEXu“ a vyber kartu zo zoznamu kariet CODEXu. Existujúcu kartu tu môžeš "
              "upraviť alebo zmazať.")
PICK_LIMIT = 30
# scope -> the CODEX sklady a pick may come from (None = every sklad of the pick stredisko),
# the catalog override table, and how a refusal names the catalog.
_SCOPES: dict[str, dict] = {
    "orders": {"sklady": (1,), "table": "catalog_overrides", "label": "objednávky (sklad 1)"},
    "dl": {"sklady": None, "table": "dl_catalog_overrides", "label": "dodacie listy"},
}


class CreateBlocked(CardRefused):
    """A typed new card — refused outright; a card comes only from `add_from_codex`."""

    status = 403


def blocked() -> CreateBlocked:
    return CreateBlocked({"error": CODEX_ONLY, "codex_only": True})


def refuse_typed_card(catalog: list[dict], gtin) -> None:
    """Raise `CreateBlocked` unless `gtin` is exactly the number of a card we already have —
    an edit passes, a typed NEW number never does."""
    if not is_card(catalog, gtin):
        log.warning("typed new card %s refused — cards come only from the CODEX pick (#477)",
                    gtin)
        raise blocked()


def is_card(catalog: list[dict], gtin) -> bool:
    """`gtin` is exactly the number of a card in `catalog` (our effective catalog)."""
    return any(str(r.get("gtin") or "") == str(gtin) for r in catalog)


def _spec(scope: str) -> dict:
    if scope not in _SCOPES:
        raise ValueError(f"neznámy scope {scope!r}")
    return _SCOPES[scope]


def catalog(conn, scope: str) -> list[dict]:
    """`scope`'s effective catalog (orders / DL), as the board and the answer paths see it."""
    _spec(scope)
    return (snapshot.catalog_for_management(conn) if scope == "orders"
            else dl_snapshot.dl_catalog_for_management(conn))


def _deleted(conn, scope: str) -> list[dict]:
    return (snapshot.deleted_catalog_cards(conn) if scope == "orders"
            else dl_snapshot.deleted_dl_cards(conn))


def pickable(conn, scope: str) -> dict[str, dict]:
    """code -> the pickable CODEX card of `scope` (`codex_cards.pickable`)."""
    return codex_cards.pickable(conn, _spec(scope)["sklady"])


def mark_pickable(conn, scope: str, payload: dict) -> dict:
    """Mark each `codex.similar` card of a #467 refusal `pickable` — only those may be added
    with „Pridať kartu z CODEXu" (the refusal searches the WHOLE CODEX list, junk strediská and
    inactive cards included, which `add_from_codex` would refuse)."""
    similar = (payload.get("codex") or {}).get("similar") or []
    if similar:
        can = pickable(conn, scope)
        for s in similar:
            s["pickable"] = s.get("code") in can
    return payload


def codex_choices(conn, scope: str, q: str = "", *, limit: int = PICK_LIMIT) -> dict:
    """The picker list: pickable CODEX cards of `scope` whose name / EAN kód / CODEX card
    number contain every word of `q` (diacritics folded), by name, at most `limit`. Each is
    marked `in_catalog` (+ OUR `catalog_gtin` / `catalog_name` — the exact number the answer
    keys on) or `in_trash` (a pick restores it). An empty `q` lists nothing — only the list's
    freshness (`codex`). Raises `ValueError` for an unknown scope."""
    _spec(scope)
    meta = codex_cards.freshness(conn)
    words = dl_match.fold(q).split()
    if not words:
        return {"items": [], "total": 0, "codex": meta}
    hits = [e for e in pickable(conn, scope).values()
            if all(w in dl_match.fold(f"{e['name']} {e['code']} {e['card_code']}")
                   for w in words)]
    hits.sort(key=lambda e: (dl_match.fold(e["name"]), e["code"]))
    ours = codex_cards.index_by_code(catalog(conn, scope))
    trash = codex_cards.index_by_code(_deleted(conn, scope))
    items = []
    for e in hits[:limit]:
        card = ours.get(e["code"])
        item = dict(e, in_catalog=card is not None, in_trash=card is None and e["code"] in trash)
        if card is not None:
            item.update(catalog_gtin=str(card["gtin"]), catalog_name=card.get("name", ""))
        items.append(item)
    return {"items": items, "total": len(hits), "codex": meta}


def add_from_codex(conn, scope: str, code, *, actor: str) -> dict:
    """Put the picked CODEX card into `scope`'s catalog — the ONE creation path. Returns
    {gtin, name, created}: OUR card (`created` False, nothing written) when we already have the
    code; our Kôš card RESTORED as it was (`created` True, audited `create` + `restored`); else
    the new card (the CODEX code + name [+ sklad for DL], audited `create`). Raises
    `CardRefused` (409) for a code the picker does not offer in this scope, `ValueError` for an
    unknown scope. The caller commits/rolls back (the answer path runs it in its `db_tx`)."""
    spec = _spec(scope)
    norm = codex_cards.normalize_code(code)
    entry = pickable(conn, scope).get(norm) if norm else None
    if entry is None:
        log.warning("CODEX pick %r refused — not an active CODEX card of the %s scope",
                    code, scope)
        raise CardRefused({"error": (
            f"Kód {code} nie je medzi aktívnymi kartami CODEXu pre {spec['label']} — vyber "
            f"kartu zo zoznamu „Vybrať kartu z CODEXu“.")})
    ours = codex_cards.index_by_code(catalog(conn, scope)).get(norm)
    if ours is not None:
        log.info("CODEX pick %s: already our card %s „%s“ — selected, nothing written",
                 norm, ours.get("gtin"), ours.get("name", ""))
        return {"gtin": str(ours["gtin"]), "name": ours.get("name", ""), "created": False}
    binned = codex_cards.index_by_code(_deleted(conn, scope)).get(norm)
    from ..board.services import audit  # lazy: the audit leaf, like orders.teach does
    if binned is not None:
        gtin = str(binned["gtin"])
        (snapshot.undelete_catalog_card if scope == "orders"
         else dl_snapshot.undelete_dl_catalog_card)(conn, gtin)
        if not str(binned.get("name") or "").strip():
            # a bare retirement marker of a card that lived only in the snapshot (`retire_*`
            # writes name '' + blank fields, the next snapshot dropped the card) — nothing of
            # ours to keep, and un-deleting it alone would make a NAMELESS card: fill it from
            # CODEX exactly like a new card
            _write(conn, scope, gtin, entry)
        _rebuild(conn, scope)
        card = next((r for r in catalog(conn, scope) if str(r.get("gtin")) == gtin), {})
        audit.record(conn, actor=actor, table=spec["table"], row_id=gtin, action="create",
                     after={"gtin": gtin, "name": card.get("name", ""), "source": "codex",
                            "restored": True, "codex_card": entry["card_code"]},
                     note="výber karty z CODEXu (#477) — karta obnovená z Koša")
        log.info("CODEX pick %s: our card %s restored from the Kôš by %s", norm, gtin, actor)
        return {"gtin": gtin, "name": card.get("name", ""), "created": True}
    after = {"gtin": norm, "name": entry["name"], "source": "codex",
             "codex_card": entry["card_code"]}
    if scope == "dl":
        after["sklad"] = str(entry["sklad"])
    _write(conn, scope, norm, entry)
    _rebuild(conn, scope)
    audit.record(conn, actor=actor, table=spec["table"], row_id=norm, action="create",
                 after=after, note="výber karty z CODEXu (#477)")
    log.info("CODEX card %s „%s“ (CODEX karta %s) added to the %s catalog by %s",
             norm, entry["name"], entry["card_code"], scope, actor)
    return {"gtin": norm, "name": entry["name"], "created": True}


def _write(conn, scope: str, gtin: str, entry: dict) -> None:
    """The CODEX card's name (+ its sklad for DL) into `scope`'s override row for `gtin`."""
    if scope == "orders":
        snapshot.upsert_catalog_card(conn, gtin, entry["name"])
    else:
        dl_snapshot.upsert_dl_catalog_card(conn, gtin, entry["name"], sklad=str(entry["sklad"]))


def _rebuild(conn, scope: str) -> None:
    if scope == "orders":
        snapshot.rebuild_from_overrides(conn)
    else:
        dl_snapshot.dl_rebuild_from_overrides(conn)
