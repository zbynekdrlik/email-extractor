"""The ONE way a card enters our catalog: picked from the CODEX stock-card list (#477).

Owner order 2026-09-30 (#477, after the #467 incident — the sklad saw a stale name under a
code, typed a code of its own invention and broke orders AND delivery notes): CODEX is the only
source of truth for a card's code and name, so nobody TYPES a new card any more.

- `refuse_typed_card` / `blocked()` (`CreateBlocked`, HTTP 403) — every free-typed creation
  path refuses a number our catalog does not have: the board Produkty „Pridať", the inline
  „➕ Nová karta" on an item / dl_item question, the legacy `/api/znalosti/*` upserts. Editing an
  existing card (name, doplnok, mass/sklad/cena) and the soft delete stay.
- `codex_choices` — what „Vybrať kartu z CODEXu" on a question offers: the pushed CODEX cards
  (`codex_stock_cards`), sklad-scoped per catalog, searched by name / EAN kód / CODEX card no.
- `add_from_codex` — the ONE creation: exactly the picked code + its CODEX name go into the
  catalog override, audited as `create` (the Kôš can take it back). A code we already have is
  only SELECTED (never duplicated or overwritten — an upsert would wipe mass/sklad/cena); one
  sitting in the Kôš is refused (restore it there). One human pick = one card, never a bulk
  import (#337).

Scope of a pick (measured on prod 2026-09-30):
- orders: CODEX `stredisko 1 / sklad 1` (the finished goods) — 130/130 cards of the orders
  snapshot and every override sit there;
- dl: CODEX `stredisko 1`, every sklad (100 kg-tracked, 1, 200, 500, 600, 625, 650, 700) —
  never the other strediská (4, 40x-45x), the junk sklady #337 banned.
Only active rows (`inactive` = CODEX LNEAKTIVNY). A stale list still offers its last known cards
(the board warns); a list that never arrived offers nothing.

A new DL card takes its `sklad` from CODEX: 100 when the code has an active sklad-100 row (a
kg-tracked card must stay kg-tracked — with a blank mass the #462 hold then ASKS instead of
shipping a xN), else the lowest stredisko-1 sklad; this reproduces the `sklad` of all 465 of our
DL cards that carry one. mass/cena stay blank (CODEX does not push them).
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
STREDISKO = 1
KG_SKLAD = 100
# scope -> the CODEX sklady a pick may come from (None = every sklad of STREDISKO), the
# catalog override table, and how the refusal names the catalog.
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
    if not any(str(r.get("gtin") or "") == str(gtin) for r in catalog):
        log.warning("typed new card %s refused — cards come only from the CODEX pick (#477)",
                    gtin)
        raise blocked()


def same_code_card(catalog: list[dict], gtin) -> dict | None:
    """Our card holding `gtin`'s CODEX code — the exact number, or the same code written
    differently (a card created before #467 as „0"+code)."""
    code = codex_cards.normalize_code(gtin)
    return next((r for r in catalog if str(r.get("gtin") or "") == str(gtin)
                 or (code and codex_cards.normalize_code(r.get("gtin")) == code)), None)


def _spec(scope: str) -> dict:
    if scope not in _SCOPES:
        raise ValueError(f"neznámy scope {scope!r}")
    return _SCOPES[scope]


def _catalog(conn, scope: str) -> list[dict]:
    return (snapshot.catalog_for_management(conn) if scope == "orders"
            else dl_snapshot.dl_catalog_for_management(conn))


def _deleted(conn, scope: str) -> list[dict]:
    return (snapshot.deleted_catalog_cards(conn) if scope == "orders"
            else dl_snapshot.deleted_dl_cards(conn))


def _entries(conn, scope: str) -> dict[str, dict]:
    """code -> the ONE pickable entry {code, name, card_code, sklad} of this scope: the
    CODEX name the rest of the app shows (the sklad-1 row first, then the newest — the
    `codex_cards.load` order), the sklad a new DL card gets (see the module docstring)."""
    allowed = _spec(scope)["sklady"]
    rows = conn.execute(
        "SELECT code, card_code, sklad, name, changed_at FROM codex_stock_cards "
        "WHERE stredisko = %s AND NOT inactive AND name <> ''", (STREDISKO,)).fetchall()
    by_code: dict[str, list[tuple]] = {}
    for code, card_code, sklad, name, changed in rows:
        if allowed is None or sklad in allowed:
            by_code.setdefault(code, []).append((card_code, sklad, name, changed))
    out: dict[str, dict] = {}
    for code, rs in by_code.items():
        sklady = {r[1] for r in rs}
        rs.sort(key=lambda r: (r[1] != 1, -(r[3].timestamp() if r[3] else 0.0), r[2]))
        out[code] = {"code": code, "name": rs[0][2], "card_code": rs[0][0],
                     "sklad": KG_SKLAD if KG_SKLAD in sklady else min(sklady)}
    return out


def _by_code(cards: list[dict]) -> dict[str, dict]:
    return {c: r for r in cards if (c := codex_cards.normalize_code(r.get("gtin")))}


def codex_choices(conn, scope: str, q: str = "", *, limit: int = PICK_LIMIT) -> dict:
    """The picker list: CODEX cards of `scope` whose name / EAN kód / CODEX card number
    contain every word of `q` (diacritics folded), by name, at most `limit`. Each is marked
    `in_catalog` (+ OUR `catalog_gtin` / `catalog_name` — the exact number the answer keys
    on) or `in_trash`. An empty `q` lists nothing — only the list's freshness (`codex`).
    Raises `ValueError` for an unknown scope."""
    _spec(scope)
    meta = codex_cards.freshness(conn)
    words = dl_match.fold(q).split()
    if not words:
        return {"items": [], "total": 0, "codex": meta}
    hits = [e for e in _entries(conn, scope).values()
            if all(w in dl_match.fold(f"{e['name']} {e['code']} {e['card_code']}")
                   for w in words)]
    hits.sort(key=lambda e: (dl_match.fold(e["name"]), e["code"]))
    ours, trash = _by_code(_catalog(conn, scope)), _by_code(_deleted(conn, scope))
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
    {gtin, name, created}: OUR card (`created` False, nothing written) when we already have
    the code, else the new card (the CODEX code + name [+ sklad for DL], an audited `create`).
    Raises `CardRefused` (409) for a code the picker does not offer in this scope and for a
    code whose card sits in the Kôš, `ValueError` for an unknown scope."""
    spec = _spec(scope)
    norm = codex_cards.normalize_code(code)
    entry = _entries(conn, scope).get(norm) if norm else None
    if entry is None:
        log.warning("CODEX pick %r refused — not an active CODEX card of the %s scope",
                    code, scope)
        raise CardRefused({"error": (
            f"Kód {code} nie je medzi aktívnymi kartami CODEXu pre {spec['label']} — vyber "
            f"kartu zo zoznamu „Vybrať kartu z CODEXu“.")})
    ours = same_code_card(_catalog(conn, scope), norm)
    if ours is not None:
        log.info("CODEX pick %s: already our card %s „%s“ — selected, nothing written",
                 norm, ours.get("gtin"), ours.get("name", ""))
        return {"gtin": str(ours["gtin"]), "name": ours.get("name", ""), "created": False}
    if same_code_card(_deleted(conn, scope), norm):
        raise CardRefused({"error": (
            f"Karta s kódom {norm} je v Koši — obnov ju na záložke Kôš (nová karta by ju "
            f"prepísala prázdnymi údajmi).")})
    after = {"gtin": norm, "name": entry["name"], "source": "codex",
             "codex_card": entry["card_code"]}
    if scope == "orders":
        snapshot.upsert_catalog_card(conn, norm, entry["name"])
        snapshot.rebuild_from_overrides(conn)
    else:
        after["sklad"] = str(entry["sklad"])
        dl_snapshot.upsert_dl_catalog_card(conn, norm, entry["name"], sklad=after["sklad"])
        dl_snapshot.dl_rebuild_from_overrides(conn)
    from ..board.services import audit  # lazy: the audit leaf, like orders.teach does
    audit.record(conn, actor=actor, table=spec["table"], row_id=norm, action="create",
                 after=after, note="výber karty z CODEXu (#477)")
    log.info("CODEX card %s „%s“ (CODEX karta %s) added to the %s catalog by %s",
             norm, entry["name"], entry["card_code"], scope, actor)
    return {"gtin": norm, "name": entry["name"], "created": True}
