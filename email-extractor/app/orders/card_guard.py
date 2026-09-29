"""The ONE gate for a brand-new card number typed on the nástenka (#467).

Two board paths create a DL catalog card from a number the sklad types — the Produkty sklad
„➕ Nová karta" (`board/services/catalog.upsert`, `new: true`) and the inline „➕ Nová karta" on
a dl_item question (`httpapi_orders_questions`). Both go through `guard_new_dl_card`, so they can
never disagree. A number is refused (`codex_cards.CardRefused`, the 409 JSON body in `.payload`)
when:

- it carries leading zeros — CODEX stores the EAN kód as a number, so „0" + a real code would
  pass the CODEX check yet become a SECOND card for one CODEX code (without sklad/cena);
- CODEX has no stock card with that EAN kód (`codex_cards.check_card_code`, fail-open on a
  missing/stale list) — the 3698 incident began exactly here;
- we already have a card with that number — upserting it would overwrite it with blank
  mass/sklad/cena (a kg-tracked card silently losing sklad=100): `taken()` names the card so the
  board offers it with one click;
- a card with that number sits in the Kôš (hidden by the loader's own rule) — a new card would
  overwrite it with blanks, and a later restore would bring back a wiped card.

`taken()` is also the orders catalog's „number already has a card" refusal — ONE shape for both.
Catalog rules live here, the CODEX list rules in `codex_cards`; this module only composes them.
"""
from __future__ import annotations

from . import codex_cards, dl_snapshot
from .codex_cards import CardRefused


def taken(card: dict) -> CardRefused:
    """The „this number already has a card" refusal — `existing` drives the board's one-click
    „use / find that card"."""
    return CardRefused({
        "error": (f"Číslo položky {card.get('gtin')} už má karta „{card.get('name', '')}“ — "
                  f"nová karta sa nezakladá, použi túto existujúcu."),
        "existing": {"gtin": str(card.get("gtin")), "name": card.get("name", "")}})


def guard_new_dl_card(conn, gtin, *texts: str, now=None) -> None:
    """Raise `CardRefused` (or its `codex_cards.CodexRefusal`) unless `gtin` may become a NEW
    DL card; `texts` (the name typed, the delivery-note wording) pick the similar CODEX cards
    the refusal offers."""
    code = codex_cards.normalize_code(gtin)
    if code and code != str(gtin).strip():
        raise CardRefused({"error": f"Číslo položky zadaj bez úvodných núl: {code} — tak ho "
                                    f"vedie CODEX (inak by vznikla druhá karta pre ten istý kód)."})
    catalog = dl_snapshot.dl_catalog_for_management(conn)
    codex_cards.check_card_code(conn, gtin, *texts, catalog=catalog, now=now)
    hit = next((r for r in catalog if str(r.get("gtin") or "") == str(gtin)), None)
    if hit:
        raise taken(hit)
    if dl_snapshot.deleted_dl_card(conn, gtin):
        raise CardRefused({
            "error": (f"Číslo položky {gtin} patrí zmazanej karte — obnov ju na záložke Kôš "
                      f"(nová karta by ju prepísala prázdnymi údajmi).")})
