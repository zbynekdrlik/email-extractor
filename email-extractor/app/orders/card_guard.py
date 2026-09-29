"""The ONE gate for a brand-new card number typed on the nástenka (#467).

Two board paths create a DL catalog card from a number the sklad types — the Produkty sklad
„➕ Nová karta" (`board/services/catalog.upsert`, `new: true`) and the inline „➕ Nová karta" on
a dl_item question (`httpapi_orders_questions`). Both go through `guard_new_dl_card`, so they can
never disagree. A number is refused (`codex_cards.CardRefused`, the 409 JSON body in `.payload`)
when:

- it is written unlike CODEX writes the EAN kód (leading zeros, a „.0" suffix — CODEX stores it
  as a number): it would pass the CODEX check yet become a SECOND card for one CODEX code
  (without sklad/cena). `refuse_code_variant` — also applied by every OTHER write of a new DL
  number (legacy API, a board POST without `new`), so no path creates such a duplicate;
- CODEX has no stock card with that EAN kód (`codex_cards.check_card_code`, fail-open on a
  missing/stale list) — the 3698 incident began exactly here;
- we already have a card with that CODEX code (compared by the normalized code, so a card
  created before this gate as „0"+code takes the bare code too) — upserting it would overwrite
  it with blank mass/sklad/cena (a kg-tracked card silently losing sklad=100): `taken()` names
  the card so the board offers it with one click;
- a card with that code sits in the Kôš (hidden by the loader's own rule) — a new card would
  overwrite it with blanks, and a later restore would bring back a wiped card.

`taken()` is also the orders catalog's „number already has a card" refusal — ONE shape for both.
Catalog rules live here, the CODEX list rules in `codex_cards`; this module only composes them.
"""
from __future__ import annotations

from . import codex_cards, dl_snapshot
from .codex_cards import CardRefused


def _card(card: dict) -> dict:
    return {"gtin": str(card.get("gtin")), "name": card.get("name", "")}


def taken(card: dict) -> CardRefused:
    """The „this number already has a card" refusal — `existing` drives the board's one-click
    „use / find that card"."""
    return CardRefused({
        "error": (f"Číslo položky {card.get('gtin')} už má karta „{card.get('name', '')}“ — "
                  f"nová karta sa nezakladá, použi túto existujúcu."),
        "existing": _card(card)})


def same_code_card(catalog: list[dict], gtin) -> dict | None:
    """Our card holding `gtin`'s CODEX code — the exact number, or the same code written
    differently (a card created before this gate as „0"+code)."""
    code = codex_cards.normalize_code(gtin)
    return next((r for r in catalog if str(r.get("gtin") or "") == str(gtin)
                 or (code and codex_cards.normalize_code(r.get("gtin")) == code)), None)


def refuse_code_variant(gtin, catalog: list[dict]) -> None:
    """Refuse a NEW DL number written unlike CODEX writes the EAN kód (leading zeros, „.0");
    the refusal names our card with that code, if any. A canonical number passes."""
    code = codex_cards.normalize_code(gtin)
    if not code or code == str(gtin).strip():
        return
    payload: dict = {"error": (f"Číslo položky zadaj tak, ako ho vedie CODEX: {code} (bez "
                               f"úvodných núl a desatinnej časti) — inak by vznikla druhá karta "
                               f"pre ten istý kód.")}
    same = same_code_card(catalog, code)
    if same:
        payload["existing"] = _card(same)
    raise CardRefused(payload)


def guard_new_dl_card(conn, gtin, *texts: str, now=None) -> None:
    """Raise `CardRefused` (or its `codex_cards.CodexRefusal`) unless `gtin` may become a NEW
    DL card; `texts` (the name typed, the delivery-note wording) pick the similar CODEX cards
    the refusal offers."""
    catalog = dl_snapshot.dl_catalog_for_management(conn)
    refuse_code_variant(gtin, catalog)
    codex_cards.check_card_code(conn, gtin, *texts, catalog=catalog, now=now)
    hit = same_code_card(catalog, gtin)
    if hit:
        raise taken(hit)
    if same_code_card(dl_snapshot.deleted_dl_cards(conn), gtin):
        raise CardRefused({
            "error": (f"Číslo položky {gtin} patrí zmazanej karte — obnov ju na záložke Kôš "
                      f"(nová karta by ju prepísala prázdnymi údajmi).")})
