"""The Slovak texts of the CODEX card sync (#478) — every review reason and delivery-history note
the warehouse reads. Pure functions over FACTS the planner (`codex_sync_plan`) derives from the
same rules it applies: a text decides nothing and never asserts an outcome the planner did not
compute (reviews 13-16 — five rounds found prose claims that were false in reachable states:
what a „Vybrať kartu z CODEXu" pick binds, whether a rename rebind keeps the data, where held
rows sit, what the next list redoes). The ops message frame lives in `codex_sync`.
"""
from __future__ import annotations

from .codex_sync_memory import CHECK_TAUGHT


def pick_advice(code: str, candidates: list[str], offered: str | None, offered_name: str,
                same: bool, numbers: list[str]) -> str:
    """What deleting our card and picking `code` at a question would do: `offered` = the ONE
    card the picker offers for the code (None = not offered at all), `same` = the pick keeps the
    curated data (the same product / an unbound number), `numbers` = our live numbers of the
    code (the picker SELECTS any of them, so all must go to the Kôš first)."""
    if offered is None:
        return (f"výber kariet kód {code} neponúka — kartu {', '.join(candidates)} zaradí len "
                f"oprava v CODEXe")
    parts = []
    if offered in candidates:
        data = ("jej údaje ostanú (ten istý výrobok)" if same
                else "staré údaje (alias / doplnok / hmotnosť) sa vyčistia")
        ours = "našu kartu" if len(numbers) <= 1 else f"naše karty {', '.join(numbers)}"
        parts.append(f"ak je to výrobok karty CODEX {offered} („{offered_name}“), zmaž {ours} "
                     f"(Kôš) a pri otázke ju vyber cez „Vybrať kartu z CODEXu“ — priradí sa k "
                     f"nej, {data}")
    others = [c for c in candidates if c != offered]
    if others:
        parts.append(f"kartu CODEX {', '.join(others)} výber priradiť nevie (pre kód {code} "
                     f"ponúka len kartu {offered}) — tú zaradí len oprava v CODEXe")
    return "; ".join(parts)


def contest(gtin: str, name: str, code: str, card: str, was: str, *, retired: bool,
            named: list[str], card_alive: bool, pick: str | None, no_carrier: bool) -> str:
    """A number whose name says it is another product than its CODEX card (`_contested`)."""
    if retired and not named:
        parts = [f"číslo {gtin} bola karta CODEX {card} („{was}“) — synchronizácia ju "
                 f"zmazala, niekto ju vrátil z Koša a premenoval na „{name}“."]
    else:
        # our name = another carrier's: a human rename OR a CODEX-side rename of our card beside
        # a same-named duplicate — nothing tells which, so say only what we see (review 10)
        parts = [f"číslo {gtin} („{name}“) je karta CODEX {card} („{was}“), no rovnako ako "
                 f"naša karta sa volá karta CODEX {', '.join(named)}, ktorá kód {code} teraz "
                 f"nesie — nevieme, ktorá je naša."]
    if card_alive:
        act = (f"vráť karte názov „{was}“" if retired and not named
               else f"premenuj ju na „{was}“")
        parts.append(f"Ak je to „{was}“, {act} — ďalší zoznam kariet ju zaradí ku karte "
                     f"CODEX {card}.")
    if pick:
        parts.append(pick[:1].upper() + pick[1:] + ".")
    elif no_carrier:
        parts.append(f"Kód {code} v stredisku 1 CODEXu teraz nenesie žiadna karta — ak kartu "
                     f"nepotrebujete, zmažte ju (Kôš).")
    parts.append(f"Naučené priradenia k číslu {gtin}: {CHECK_TAUGHT}.")
    return " ".join(parts)


def multi_carrier(code: str, carriers: list[str], pick: str) -> str:
    return (f"kód {code} nesie v CODEXe viac kariet ({', '.join(carriers)}) — ktorá je naša? "
            f"Premenuj našu kartu (Produkty) na jej názov v CODEXe, pri ďalšom zozname kariet "
            f"sa priradí; ak majú v CODEXe rovnaký názov: {pick}.")


def last_owners(code: str, last: list[str]) -> str:
    return (f"kód {code} už v stredisku 1 CODEXu nie je a naposledy ho niesli karty "
            f"{', '.join(last)} — nevieme, ktorá je naša; ak kartu už nepotrebujete, zmažte ju "
            f"(Kôš).")


def gone(card: str, code: str, now: list[str], *, pick: str | None, same_one: bool) -> str:
    """Our CODEX card left stredisko 1 for good, the code lives on elsewhere."""
    head = f"karta CODEX {card} už v stredisku 1 nie je"
    if not now:
        return (f"{head} a kód {code} je v CODEXe už len na inom stredisku — výber kariet ho "
                f"neponúka, synchronizácia s touto kartou nič neurobí; ak kartu nepotrebujete, "
                f"zmažte ju (Kôš).")
    if len(now) > 1:
        return (f"{head} a kód {code} teraz nesie viac kariet ({', '.join(now)}): {pick}; ak to "
                f"nie je žiadna z nich, kartu zmaž (Kôš).")
    data = ("jej údaje ostanú (ten istý výrobok v CODEXe)" if same_one
            else "má v CODEXe iný názov, preto sa jej alias / doplnok / hmotnosť vyčistia — "
                 "skontroluj ich")
    return (f"{head} a kód {code} teraz nesie karta {now[0]} — ak je to ten istý výrobok, "
            f"premenuj našu kartu (Produkty) na jej názov v CODEXe, pri ďalšom zozname kariet sa "
            f"priradí ({data}); ak nie, kartu zmaž (Kôš).")


def successor_not_offered(card: str, codes: list[str], label: str) -> str:
    return (f"karta CODEX {card} nesie teraz kód {', '.join(codes)}, ale výber kariet ho pre "
            f"katalóg {label} neponúka")


def successor_ambiguous(card: str, fit: list[str]) -> str:
    return (f"karta CODEX {card} nesie viac kódov naraz ({', '.join(fit)}) — nie je jasné, "
            f"ktorý je nový")


def successor_shared(succ: str, card: str, others: list[str]) -> str:
    return (f"nový kód {succ} karty CODEX {card} nesie aj karta {', '.join(others)} — "
            f"prečíslovanie treba overiť ručne")


def renumber_other_card(succ: str, other: str, card: str, card_name: str, live: bool) -> str:
    """Our number with the new code IS another CODEX card — never a silent merge."""
    return (f"náš kód {succ} je karta CODEX {other}, nie {card} — prečíslovanie čaká. Ak naše "
            f"číslo {succ} je teraz výrobok karty CODEX {card} („{card_name}“), "
            + ("zmaž ho (Kôš) a potom " if live else "")
            + f"vyber ho pri otázke cez „Vybrať kartu z CODEXu“ — priradí sa ku karte CODEX "
              f"{card} a prečíslovanie prebehne.")


def renumber_contested(succ: str, card: str, hit_name: str, was: str, live: bool) -> str:
    """Our number with the new code (live or its Kôš copy) a human renamed after the retire."""
    where = "" if live else "v Koši "
    back = "" if live else "vráť ju z Koša a "
    return (f"nový kód {succ} karty CODEX {card} je u nás karta {where}„{hit_name}“ — "
            f"synchronizácia ju zmazala ako „{was}“ a niekto ju potom premenoval, prečíslovanie "
            f"počká: ak je to stále „{was}“, {back}daj jej tento názov (potom sa prečísluje); "
            f"jej naučené priradenia: {CHECK_TAUGHT}")


def held(taught: int, shipped: int, at: str, code: str, takers: list[str], name: str,
         card: str, to: str) -> str:
    """Taught rows a renumber holds (decided after CODEX gave the code to another card)."""
    history = (f" ({shipped} záznamov o dodávkach z toho obdobia tiež ostáva pod {at} ako "
               f"história)" if shipped else "")
    return (f"{taught} naučených priradení k číslu {at} vzniklo (alebo ich niekto zmenil) "
            f"potom, čo sa kód {code} v CODEXe objavil pri karte CODEX {', '.join(takers)} — "
            f"nevieme, či patria „{name}“ (karta CODEX {card}), alebo jej: ostávajú pod číslom "
            f"{at}{history}; {CHECK_TAUGHT} a tie, čo patria „{name}“, preraď na {to}.")


def adopted(taught: int, target: str, succ: str, takers: list[str], card: str,
            name: str) -> str:
    """Taught rows a round trip adopts as they sit on the restore / merge target."""
    return (f"{taught} naučených priradení k číslu {target} vzniklo, kým kód {succ} v CODEXe "
            f"mala karta CODEX {', '.join(takers)} — prečíslovanie ich teraz pridá ku karte "
            f"CODEX {card} („{name}“); {CHECK_TAUGHT} a tie, čo patria tej druhej karte, zmaž "
            f"alebo preraď.")


def repick_fix(old: str, old_name: str, *, home: str | None, offered_code: str | None,
               old_alive: bool, label: str) -> str:
    """Where rows of an old product can go: its live number, a code the picker offers it
    under, or nowhere (not offered for this catalog / gone from CODEX)."""
    if home is not None:
        return f"preraď ich na {home}"
    if offered_code is not None:
        return (f"„{old_name}“ u nás karta nie je — ak treba, pri otázke vyber cez „Vybrať "
                f"kartu z CODEXu“ kód {offered_code} (karta CODEX {old}) a preraď ich naň")
    if old_alive:
        return (f"„{old_name}“ u nás karta nie je a výber kariet ju pre katalóg {label} "
                f"neponúka — tie priradenia zmaž, alebo pomôže oprava v CODEXe")
    return f"„{old_name}“ už v CODEXe nie je — tie priradenia zmaž"


def repick(gtin: str, old: str, old_name: str, *, moved: str | None, kept: int, taught: int,
           shipped: int, older: bool, fix: str) -> str:
    """Taught rows of a number that became another product than `old`."""
    if moved and kept:
        where = (f"číslo {gtin} (prečíslované na {moved}; {kept} z tých priradení ostalo pod "
                 f"{gtin} — vznikli, keď kód mala iná karta)")
    else:
        where = f"číslo {gtin}" + (f" (teraz prečíslované na {moved})" if moved else "")
    history = (f" ({shipped} záznamov o dodávkach z toho času tiež ostáva ako história)"
               if shipped else "")
    before = "je starších ako výber z CODEXu a " if older else ""
    return (f"{where} bolo karta CODEX {old} („{old_name}“) a {taught} naučených priradení k "
            f"nemu {before}môže patriť „{old_name}“{history}: {CHECK_TAUGHT}; ak patria "
            f"„{old_name}“, {fix}")


def why_taken(code: str) -> str:
    return f"kód {code} mala medzitým v CODEXe iná karta"


def why_repick(gtin: str, old: str, old_name: str) -> str:
    return f"číslo {gtin} bolo predtým karta CODEX {old} („{old_name}“)"
