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

#479 — the SAME CODEX list guards every ORDER file (the #467 gate covered only delivery notes;
the card 27 renumber broke orders too, „nebralo do objednávky"). `order_guard` (the live list,
fail-open exactly like DL) + `gate_order_line` (a line whose code CODEX lacks → a cardless
`codex_missing` line, so the orders engine's own item question + hold take over) +
`order_question_candidates` (a question offers only cards CODEX has) + `dead_codes` (the static
engine's check). Callers: `pipeline._run` / `_ship_one`, `hold_close._release_locked`,
`hold_redecide._ask_still_ambiguous`, `static_worker.run_live` — all LIVE only.
"""
from __future__ import annotations

import logging

from . import codex_cards, desadv_edi, dl_match, dl_snapshot, match, snapshot
from .codex_cards import CardRefused

log = logging.getLogger("orders.card_guard")

CODEX_ONLY = ("Nové karty sa pridávajú len výberom z CODEXu — pri otázke klikni „Vybrať kartu "
              "z CODEXu“ a vyber kartu zo zoznamu kariet CODEXu. Existujúcu kartu tu môžeš "
              "upraviť alebo zmazať.")
# #479: the History „Doučiť" teaches onto a card, it never creates one
TEACH_CARD_ONLY = ("Doučiť sa dá len na existujúcu kartu z katalógu — vyber ju vo vyhľadávaní. "
                   "Nová karta sa pridáva len výberom z CODEXu (pri otázke „Vybrať kartu z "
                   "CODEXu“).")
# #479: an orders line whose card code CODEX has no stock card for — no card, asked + held
CODEX_MISSING = "codex_missing"
# #160: at most this many card buttons on an orders question (`match.plausible_candidates`)
QUESTION_BUTTONS = 6
PICK_LIMIT = 30
# scope -> the CODEX sklady a pick may come from (None = every sklad of the pick stredisko),
# the longest code the scope's EDI can carry (None = no limit), the catalog override table, and
# how a refusal names the catalog. A DESADV line has a 13-char GTIN field: a longer CODEX code
# (the 14-digit #245 beverage cards on sklad 500) can never ship — `dl_match._gtin_edi_overflow`
# would re-hold the line and ask again on every reprocess.
_SCOPES: dict[str, dict] = {
    "orders": {"sklady": (1,), "max_code": None, "table": "catalog_overrides",
               "label": "objednávky (sklad 1)"},
    "dl": {"sklady": None, "max_code": desadv_edi.GTIN_FIELD_WIDTH,
           "table": "dl_catalog_overrides", "label": "dodacie listy"},
}


class CreateBlocked(CardRefused):
    """A typed new card — refused outright; a card comes only from `add_from_codex`."""

    status = 403


def blocked(error: str = CODEX_ONLY) -> CreateBlocked:
    return CreateBlocked({"error": error, "codex_only": True})


def refuse_typed_card(catalog: list[dict], gtin, *, error: str = CODEX_ONLY) -> None:
    """Raise `CreateBlocked` unless `gtin` is exactly the number of a card we already have —
    an edit passes, a typed NEW number never does. `error` words the refusal for the caller
    (#479: the History „Doučiť" teaches, it does not edit)."""
    if not is_card(catalog, gtin):
        log.warning("typed new card %s refused — cards come only from the CODEX pick (#477)",
                    gtin)
        raise blocked(error)


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
    """code -> the pickable CODEX card of `scope` (`codex_cards.pickable`, minus a code longer
    than the scope's EDI can carry)."""
    spec = _spec(scope)
    cards = codex_cards.pickable(conn, spec["sklady"])
    if spec["max_code"] is not None:
        cards = {c: e for c, e in cards.items() if len(c) <= spec["max_code"]}
    return cards


def _card(conn, scope: str, gtin: str) -> dict:
    return next((r for r in catalog(conn, scope) if str(r.get("gtin")) == gtin), {})


def index_ours(scope: str, rows: list[dict]) -> dict[str, dict]:
    """Our (live or Kôš) cards `rows` by CODEX code — only numbers the scope's EDI can carry: a
    legacy „0"+13-digit DL twin is 14 chars, which no DESADV line can ship, so it is never the
    card a pick selects or restores (the canonical CODEX card is added instead); the canonical
    number wins over a legacy twin (`codex_cards.index_by_code`). Pure — the CODEX card sync
    (#478) runs it over its simulated catalog."""
    limit = _spec(scope)["max_code"]
    if limit is not None:
        rows = [r for r in rows if len(str(r.get("gtin") or "")) <= limit]
    return codex_cards.index_by_code(rows)


def pick_target(scope: str, code: str, live: list[dict],
                binned: list[dict]) -> tuple[str, dict | None]:
    """What a „Vybrať kartu z CODEXu" pick of `code` does to `scope`'s catalog, given our live
    and Kôš cards — THE rule `add_from_codex` applies, and the one the CODEX card sync's texts
    describe (#478 review 17: re-deriving it in prose told the warehouse wrong things):
    ("select", our live card) — nothing is written; ("restore", our Kôš card) — restored as it
    was; ("new", None) — a new card with only the CODEX name (+ sklad for DL)."""
    card = index_ours(scope, live).get(code)
    if card is not None:
        return "select", card
    card = index_ours(scope, binned).get(code)
    return ("restore", card) if card is not None else ("new", None)


def _ours(conn, scope: str, *, deleted: bool = False) -> dict[str, dict]:
    """Our live (or Kôš) cards by CODEX code — `index_ours` over the stored catalog."""
    return index_ours(scope, _deleted(conn, scope) if deleted else catalog(conn, scope))


def _last_known_name(conn, scope: str, gtin: str) -> str:
    card = (snapshot.last_known_card(conn, gtin) if scope == "orders"
            else dl_snapshot.last_known_dl_card(conn, gtin))
    return card["name"] if card else ""


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
    ours, trash = _ours(conn, scope), _ours(conn, scope, deleted=True)
    items = []
    for e in hits[:limit]:
        card = ours.get(e["code"])
        item = dict(e, in_catalog=card is not None, in_trash=card is None and e["code"] in trash)
        if card is not None:
            item.update(catalog_gtin=str(card["gtin"]), catalog_name=card.get("name", ""))
        elif item["in_trash"]:
            # a bare retirement marker is blank — the name the restore brings back is the
            # snapshot's (`heal_blank_*`)
            binned = trash[e["code"]]
            item["trash_name"] = (binned.get("name")
                                  or _last_known_name(conn, scope, str(binned["gtin"])))
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
    if norm is None or entry is None:
        log.warning("CODEX pick %r refused — not an active CODEX card of the %s scope",
                    code, scope)
        raise CardRefused({"error": (
            f"Kód {code} nie je medzi aktívnymi kartami CODEXu pre {spec['label']} — vyber "
            f"kartu zo zoznamu „Vybrať kartu z CODEXu“.")})
    kind, ours = pick_target(scope, norm, catalog(conn, scope), _deleted(conn, scope))
    if kind == "select" and ours is not None:
        log.info("CODEX pick %s: already our card %s „%s“ — selected, nothing written",
                 norm, ours.get("gtin"), ours.get("name", ""))
        return {"gtin": str(ours["gtin"]), "name": ours.get("name", ""), "created": False}
    binned = ours if kind == "restore" else None
    from ..board.services import audit  # lazy: the audit leaf, like orders.teach does
    if binned is not None:
        gtin = str(binned["gtin"])
        # un-delete heals a bare retirement marker (a snapshot-only card) from the newest
        # snapshot that still has it — the same heal as the Kôš „Vrátiť"
        (snapshot.undelete_catalog_card if scope == "orders"
         else dl_snapshot.undelete_dl_catalog_card)(conn, gtin)
        if not str(_card(conn, scope, gtin).get("name") or "").strip():
            # a bare marker no snapshot remembers — nothing of ours to restore, and a NAMELESS
            # card is never acceptable: fill it from CODEX exactly like a new card
            _write(conn, scope, gtin, entry)
        _rebuild(conn, scope)
        card = _card(conn, scope, gtin)
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


# --- #479: the ORDER-file gate ---------------------------------------------------------------

def order_guard(conn, now=None) -> codex_cards.CodexCards | None:
    """The CODEX list the ORDER-file checks may trust — `codex_cards.live_guard`, so exactly the
    DL rule: None = checks OFF (never pushed / older than `STALE_HOURS` / empty; it logs its own
    warning, `codex_cards.stale_sweep` alerts ops). A stopped push must never hold every order."""
    return codex_cards.live_guard(conn, now)


def dead_codes(codex: codex_cards.CodexCards | None, gtins) -> list[str]:
    """The distinct codes among `gtins` no CODEX stock card has, in order — [] when `codex` is
    None (fail-open). A blank gtin is no card, nothing to check."""
    if codex is None:
        return []
    out: list[str] = []
    for g in gtins:
        code = str(g or "").strip()
        if code and not codex.has(code) and code not in out:
            out.append(code)
    return out


def codex_missing_note(card: str, gtin: str) -> str:
    """The warehouse-facing reason on a `codex_missing` order line / its board question."""
    return (f"Karta „{card or gtin}“ (kód {gtin}) v CODEXe neexistuje — žiadna skladová karta "
            "nemá tento EAN kód, takže CODEX by túto položku objednávky pri importe neprevzal. "
            "Objednávka čaká: vyber správnu kartu (ponúkame len karty, ktoré CODEX má) alebo ju "
            "pridaj cez „Vybrať kartu z CODEXu“; kartu s neplatným kódom potom zmaž v Produkty.")


def gate_order_line(decision, codex: codex_cards.CodexCards | None):
    """#479: an orders line whose card code CODEX has no stock card for → a cardless
    `CODEX_MISSING` line (the dead code + card + the rule it replaced kept in the note/trace), so
    the orders engine's own item question + hold take over — the orders twin of
    `dl_match.decide_item(codex=)`. Unchanged when `codex` is None (fail-open / shadow), the
    line has no card, or CODEX has the code."""
    gtin = str(decision.gtin or "").strip()
    if codex is None or not gtin or codex.has(gtin):
        return decision
    log.warning("order line %r: card %s „%s“ (%s) has a code no CODEX stock card has — "
                "left without a card, the order is asked + held (#479)", decision.item_name,
                gtin, decision.card, decision.rule)
    trace = dict(decision.trace or {}, rule=CODEX_MISSING,
                 codex_missing={"gtin": gtin, "card": decision.card, "rule": decision.rule})
    return match.Decision(item_name=decision.item_name, gtin=None, card="", confidence=0.0,
                          rule=CODEX_MISSING, note=codex_missing_note(decision.card, gtin),
                          review=True, trace=trace, quantity=decision.quantity,
                          unit=decision.unit)


def ask_codex_missing(conn, lines, *, message_id: str, customer_ean: str, customer_name: str,
                      delivery_date: str, codex: codex_cards.CodexCards | None,
                      on_new=None) -> list[int]:
    """#479: the board question for each `CODEX_MISSING` line the SHIP-TIME net found (a code
    that died while its order waited, reaching `pipeline._ship_one` via the deadline sweep): the
    order ships without the line (an item question is deadline-shippable), and this question
    names it + teaches the next order. Returns the qids (fresh or deduped onto an open one)."""
    from . import teach  # lazy: teach is the question leaf, imported where it is used
    sid = snapshot.latest_snapshot_id(conn)
    catalog = snapshot.load_catalog(conn, sid) if sid else []
    qids: list[int] = []
    for d in lines:
        cands = order_question_candidates(d.item_name, [], catalog, d, codex,
                                          customer_name=customer_name)
        qid = teach.ask(conn, message_id=message_id, customer_ean=customer_ean,
                        customer_name=customer_name, wording=d.item_name, quantity=d.quantity,
                        unit=d.unit,
                        candidates=[{"gtin": str(c.get("gtin")), "name": c.get("name", "")}
                                    for c in cands],
                        delivery_date=delivery_date, reason=d.note, on_new=on_new,
                        codex_missing=True)
        if qid:
            qids.append(qid)
    return qids


def order_question_candidates(item_name: str, item_cands: list[dict], catalog: list[dict],
                              decision, codex: codex_cards.CodexCards | None, *,
                              customer_name: str = "", memory_gtin: str = "") -> list[dict]:
    """The cards an orders item question offers (#147 re-heading + #160 plausibility). With a
    live CODEX list only cards CODEX has — a dead card is never a button. A `CODEX_MISSING` line
    has NO engine proposal (its proposed card IS the dead one), so it gets only the CODEX cards
    that clear the #160 relevance floor — never a forced first card that merely scored best (an
    unrelated card shown like a proposal is the #160 misclick class). An empty list is fine: the
    question text points to the search and „Vybrať kartu z CODEXu" (a renumbered card is often
    not in our catalog yet). `codex` None = unchanged."""
    if codex is None:
        return match.plausible_candidates(
            match.candidates_for_question(item_cands, catalog, decision), QUESTION_BUTTONS)
    valid = [c for c in catalog if codex.has(c.get("gtin"))]
    if decision.rule == CODEX_MISSING:
        return [c for c in match.candidates(item_name, valid, customer_name=customer_name,
                                             memory_gtin=memory_gtin)
                if float(c.get("score", 0) or 0) >= match.PLAUSIBLE_CANDIDATE_SCORE
                ][:QUESTION_BUTTONS]
    return match.plausible_candidates(match.candidates_for_question(
        [c for c in item_cands if codex.has(c.get("gtin"))], valid, decision), QUESTION_BUTTONS)
