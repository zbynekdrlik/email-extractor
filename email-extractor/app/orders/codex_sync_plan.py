"""The PLAN of the CODEX card sync (#478) — what the current CODEX stock-card list implies for
the cards WE ALREADY HAVE (both catalogs) and the code-keyed memories. `codex_sync` executes it,
or only reports it in dry-run. Nothing here writes; the new card bindings it finds are in
`Plan.seeds` (the executor stores them in both modes — they are identity, not catalog data).

**Identity = the CODEX card our card IS**, stored in `codex_card_bindings` ((scope, our gtin)
→ ACSKLP on stredisko 1 — `codex_cards.PICK_STREDISKO`, the #477 pick scope; ACSKLP is unique
only WITHIN a stredisko: live 2026-09-30 card 400448 is garlic on stredisko 1, crisps on 4).
A card is bound once (`_by_history`): to the ONE card on its code since the history began, or
— the code changed carrier, or a card arrived on it later — to the card its NAME is among every
card that ever carried it (never one that took the code over from another product: a human
then; one list is no proof: it waits). From
then on the sync follows THAT card and never "whoever holds the code now": a code can be REUSED
for another product (the #478 review 🔴s: following the newest carrier renamed our rožok to a
pagáč and moved its memory). `codex_card_history` (per stredisko, card, code: first/last seen) gives the
newest code of a card and whether a card has really left CODEX.

Per catalog, per CODEX code X our cards carry (a legacy „0"+code twin is grouped with its
canonical card, which supplies the data — `codex_cards.index_by_code`), with our card bound to
CODEX card C:

- C still carries X → **rename** our card when its name drifted from C's stredisko-1 name for
  X (#467 `name_key`, cosmetics ignored; #477 `_name_order` picks the name).
- C carries a new code Y instead → **renumber** X → Y in the catalog (Y created from our card /
  merged into our Y bound to C / our Y restored from the Kôš) and in every memory row. Y must be
  a code the #477 pick offers for that catalog (`card_guard.pickable`); several → C's newest;
  a Y that another card ALSO carries, or our Y bound to another card → a human decides.
- C is gone from stredisko 1 in TWO consecutive CODEX snapshots (one missing push is an export
  glitch) and X is nowhere in CODEX → **removal** (Kôš); X still elsewhere → a human decides,
  unless exactly one card now carries X under OUR name (the card was recreated) → rebind.
- Memory rows of a number the sync already retired follow its card's current number.
- What the sync cannot tell goes to a human (review), never a guess: a retired number brought
  back from the Kôš under a name that is no longer its CODEX card's, and the mapping rows
  older than a #477 re-pick of our number as another product (review 7).

A code with no stredisko-1 carrier and no history (the #467 "missing" cards) is never touched —
a card seen on it only in a list older than the history's beginning counts as none (review 33).
Nothing here ever adds a CODEX card we do not already have (#337).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# the memory rules (which rows a move carries / holds) live in `codex_sync_memory` (review 12:
# the planner neared the size budget), the Slovak texts the warehouse reads in
# `codex_sync_texts` (review 16: pure functions over the facts the planner derives), the CODEX
# list + history + bindings as read in `codex_sync_list` (review 23), the rules for our Kôš
# numbers a card takes over in `codex_sync_kos` (review 41)
from . import card_guard, codex_cards, codex_sync_list, dl_snapshot, snapshot
from . import codex_sync_texts as texts
from .codex_sync_kos import KosRules
from .codex_sync_list import (
    NEVER,
    Binding,
    Codex,
    best_row,
    is_named,
)
from .codex_sync_memory import (
    Split,
    held_clause,
    memory_split,
    taught_clause,
)

REASON_JOIN = " Tiež: "


@dataclass(frozen=True)
class Scope:
    name: str
    label: str
    table: str
    memory: tuple[str, ...]


def _scope(name: str, label: str, memory: tuple[str, ...]) -> Scope:
    # the override table: ONE definition (`card_guard`, the #477 pick scope; which numbers a
    # pick selects / restores is `card_guard.index_ours` / `pick_target` too)
    return Scope(name, label, card_guard._spec(name)["table"], memory)


SCOPES = (
    _scope("orders", "objednávky", ("item_memory", "global_item_memory")),
    _scope("dl", "sklad", ("dl_item_memory",)),
)
BY_NAME = {s.name: s for s in SCOPES}


@dataclass
class Plan:
    """What the sync changes (apply) or would change (dry-run). Items are JSON-safe dicts —
    stored in `codex_sync_runs.report`."""
    renames: list[dict] = field(default_factory=list)
    renumbers: list[dict] = field(default_factory=list)
    removals: list[dict] = field(default_factory=list)
    review: list[dict] = field(default_factory=list)
    seeds: list[dict] = field(default_factory=list)
    resets: list[dict] = field(default_factory=list)
    # delivery history (shipped rows) held on a number with nothing to move and nothing for a
    # human to fix — reported, never silently (review 12 🔵)
    holds: list[dict] = field(default_factory=list)
    # our numbers left alone on THIS list because a card they depend on is missing from it once
    # (a glitch) — reported + logged, never an all-zero plan that reads "nothing to do" (review 23)
    waits: list[dict] = field(default_factory=list)

    def wait(self, item: dict, why: str) -> None:
        self.waits.append({"scope": item["scope"], "gtin": item["gtin"],
                           "code": item["code"], "why": why})

    def code_changes(self) -> int:
        """Distinct CODEX codes renumbered or removed (a card in both catalogs = one)."""
        return len({i["code"] for i in self.renumbers + self.removals})

    def counts(self) -> dict:
        return {"renamed": len(self.renames),
                "renumbered": sum(1 for r in self.renumbers if r["mode"] != "memory"),
                "memory_renumbered": sum(1 for r in self.renumbers if r["mode"] == "memory"),
                "removed": len(self.removals), "review": len(self.review),
                "reset": len(self.resets),
                # stored in every mode / a replacing binding only by an applied run
                "bound": sum(1 for s in self.seeds if not s["replaces"]),
                "rebound": sum(1 for s in self.seeds if s["replaces"])}

    def add_review(self, item: dict, reason: str) -> None:
        """One review entry per card; every distinct reason is kept (`reasons` — the ops
        alert dedups PER reason, review 8) and `reason` is them joined, for reading."""
        for r in self.review:
            if r["scope"] == item["scope"] and r["gtin"] == item["gtin"]:
                if reason not in r["reasons"]:
                    r["reasons"].append(reason)
                    r["reason"] = REASON_JOIN.join(r["reasons"])
                return
        self.review.append(dict(item, reason=reason, reasons=[reason]))


@dataclass(frozen=True)
class Known:
    """What `_ScopePlanner._known` decided about one number of ours."""
    card: str | None           # the CODEX card it is, None = unknown
    picked: bool               # decided by a human #477 pick newer than the binding
    old: Binding | None        # the stored binding (may be retired / superseded)


def load(conn, cards: codex_cards.CodexCards, as_of: datetime) -> Codex:
    """The current stredisko-1 list + its history + our bindings, with the #477 pick's
    cards of both catalogs (call `update_history` first)."""
    return codex_sync_list.load(conn, cards, as_of, [s.name for s in SCOPES])


def _fields(card: dict) -> dict:
    return {k: v for k, v in card.items() if k != "overridden"}


def _ours(cards, scope: str) -> dict[str, dict]:
    """Our cards by CODEX code over an in-memory list (the plan simulates the catalog) — the
    picker's own rule (`card_guard.index_ours`): only numbers the scope's EDI can carry, the
    canonical number winning over a legacy twin."""
    return card_guard.index_ours(scope, list(cards))


def _groups(catalog: list[dict], scope: str) -> dict[str, list[dict]]:
    """Our cards per CODEX code, the card `_ours` picks FIRST — a legacy „0"+code twin never
    supplies the data a renumber carries over."""
    groups: dict[str, list[dict]] = {}
    for c in catalog:
        code = codex_cards.normalize_code(c.get("gtin"))
        if code:
            groups.setdefault(code, []).append(c)
    for code, group in groups.items():
        primary = _ours(group, scope).get(code) or group[0]
        group.sort(key=lambda c: c is not primary)
    return groups


# the curated fields a merge carries over onto a target that lacks them
_FILL_FIELDS = card_guard.CURATED_FIELDS


def _fill(scope: Scope, target: dict, ours: dict) -> dict:
    """Our card's curated values for the fields the merge target has blank — never
    overwriting a value the target already has."""
    out = {}
    for k in _FILL_FIELDS[scope.name]:
        mine, theirs = ours.get(k), target.get(k)
        if (theirs is None or str(theirs).strip() == "") and mine not in (None, ""):
            out[k] = mine
    return out


class _ScopePlanner(KosRules):
    """One catalog's share of the plan, simulated on `live` (our effective catalog) so a later
    step sees an earlier one (a renumbered card is renamed under its new number)."""

    def __init__(self, conn, scope: Scope, cx: Codex, plan: Plan):
        self.conn, self.scope, self.cx, self.plan = conn, scope, cx, plan
        self.catalog = card_guard.catalog(conn, scope.name)
        self.live = {str(c["gtin"]): dict(c) for c in self.catalog}
        self.binned = [dict(c) for c in (snapshot.deleted_catalog_cards(conn)
                                         if scope.name == "orders"
                                         else dl_snapshot.deleted_dl_cards(conn))]
        self.identity: dict[str, tuple[str, str]] = {}   # our live gtin -> (card, code)
        # our number now ANOTHER product -> (its old CODEX card, the #477 pick time; None for
        # a rename rebind — `_reset_from`)
        self.repicked: dict[str, tuple[str, datetime | None]] = {}
        # our numbers left waiting this list ("pick": a #477 pick waits out a glitch; "carrier":
        # an unbound number on one-list evidence — a card left its code, a card arrived on a
        # code no card carried, a card it depends on missing once) — nothing may land on them
        # (review 22 🟡: a merge's binding superseded the waiting pick; review 25 🟡: a merge
        # renamed the waiting number to another product)
        self.waiting: dict[str, str] = {}
        # pass 1's verdict per number of ours: the CODEX card it IS in this plan (a binding
        # this plan stores counts — `_known` reads only the stored ones), or not decided
        self.settled: dict[str, str] = {}
        self.unsettled: set[str] = set()
        # renumber reviews whose way out depends on the catalog as the WHOLE plan leaves it
        self.other_card: list[tuple[dict, str, str, str]] = []
        self.kos_picks = []                  # judged restore-picks (`KosRules`)

    def run(self) -> None:
        groups = _groups(self.catalog, self.scope.name)
        # two passes: which CODEX card every group IS and every reset that implies, BEFORE any
        # renumber — a merge onto a number reset in the same plan must read the reset card
        # (review 18 🟡: in one pass the group order decided whether our data or the old
        # product's kg sklad + mass survived the merge)
        settled = [s for s in (self._settle(code, group) for code, group in groups.items())
                   if s is not None]
        for item, group in settled:
            self._follow(item, group)
        for args in self.other_card:          # the way out read from the catalog as the WHOLE
            self._other_card_review(*args)    # plan leaves it, whatever the group order
        self._kos_pick_reviews()              # so are the restore-picks' (review 43)
        self._memory(set(groups))
        self._renames()

    def _known(self, gtin: str) -> Known:
        """THE identity rule — the one place that decides which CODEX card a number of ours
        (live or in the Kôš) is known to be: a human #477 pick newer than its binding names it
        exactly (a pick is a NEW card — review 3 🔴); else its binding, active OR retired (a
        Kôš „Vrátiť" of a number the sync retired is still that CODEX card — review 5 🟡: taking
        it for whoever carries the code now re-bound our rožok to the pagáč — and when a human
        renamed it to another product since, `_contested` hands it to a human); a legacy
        creation without a named card newer than the binding = unknown (identify it from the
        list)."""
        b = self.cx.bindings.get((self.scope.name, gtin))
        ev = self.cx.events.get((self.scope.table, gtin))
        if ev is not None and (b is None or ev.at > b.bound_at):
            return Known(ev.card, picked=ev.card is not None, old=b)
        return Known(b.card if b else None, picked=False, old=b)

    def _card_of(self, gtin: str) -> str | None:
        return self._known(gtin).card

    def _contested(self, known: Known, name: str, code: str) -> bool:
        """THE rule for "our number's name says it is another product than its CODEX card",
        checked wherever the sync would mutate a number (identify, a renumber onto it, a memory
        move from its Kôš copy). Our name (a live card or a Kôš copy) against CODEX card C:
        - it IS C's product (C's current rows or C's name while it carried the code) → not
          contested (review 9: a rename to C's NEW CODEX name is the same product);
        - it names ANOTHER card that carries the code now → contested: typically the #467
          drift button offered the reusing card's name, as if cosmetic. Renumbering it into C
          moved the other product's wordings onto C (review 9 🟡), re-binding it by the name
          kept C's data (review 7 🟡) — a human decides;
        - a number the sync RETIRED, renamed since (≠ `retired_name`, the name the sync retired
          it under) → contested (review 8: keyed on the retire-time name, so a plain Kôš undo of
          a card whose name had drifted is no rename).
        Never contested: a blank name (a Kôš retirement marker), a pick (`_known` names the card
        exactly), C missing from ONE snapshot (a glitch — nothing happens to the number then)."""
        card, old, cx = known.card, known.old, self.cx
        ours = codex_cards.name_key(name)
        if known.picked or card is None or not ours:
            return False
        if cx.glitched(card):
            return False
        if ours in cx.product(card, code):
            return False
        if any(is_named(name, cx.rows(d, code)) for d in cx.carriers(code) - {card}):
            return True
        return (old is not None and not old.active and old.retired_name is not None
                and ours != codex_cards.name_key(old.retired_name))

    def _contest_reason(self, gtin: str, name: str, code: str, known: Known) -> str:
        cx, card, old = self.cx, str(known.card), known.old
        retired = old is not None and not old.active
        was = ((old.retired_name if retired and old is not None else None)
               or cx.name_of(card, code))
        now = sorted(cx.carriers(code) - {card})
        return texts.contest(
            gtin, name, code, card, was, retired=retired,
            named=[d for d in now if is_named(name, cx.rows(d, code))],
            card_alive=card in cx.by_card,
            pick=self._pick_advice(code, now) if now else None,
            # „no card carries it" only when NONE does — our own may (a round trip, review 16)
            no_carrier=not cx.carriers(code))

    def _pick_advice(self, code: str, candidates: list[str]) -> str:
        """What deleting our cards and picking `code` at a question („Vybrať kartu z CODEXu")
        would REALLY do — the picker's own rules, never asserted (reviews 15-17): it offers ONE
        card per code (`card_guard.pickable`, under its own name), it SELECTS a live number of
        ours it can carry (so every number of the code goes to the Kôš first — review 16), then
        restores our Kôš card or adds a new one (`_pick` — review 17: a DL legacy twin is never
        restored), and a restored card keeps its data only when bound to nothing or to the same
        product (`_pick` / `_bound`)."""
        entry = self.cx.pickable[self.scope.name].get(code)
        offered = str(entry["card_code"]) if entry is not None else None
        label = ((str(entry.get("name") or "") if entry is not None else "")
                 or (self.cx.name_of(offered, code) if offered is not None else ""))
        delete = self._numbers(code)
        pick = self._pick(code, delete, offered) if offered is not None else None
        return texts.pick_advice(self.scope.name, code, candidates, offered, label, delete, pick)

    def _numbers(self, code: str) -> list[str]:
        """Our live numbers of CODEX `code` as this plan leaves them (a legacy twin included)."""
        return sorted(g for g in self.live if codex_cards.normalize_code(g) == code)

    def _pick(self, code: str, delete: list[str], offered: str) -> texts.Pick:
        """What a pick of `code` (CODEX card `offered`) does once our numbers `delete` went to
        the Kôš — `card_guard.pick_target`, the rule `add_from_codex` applies, over the plan's
        simulated catalog (review 17: the texts re-derived it in prose and got it wrong)."""
        gone = set(delete)
        kind, card = card_guard.pick_target(
            self.scope.name, code, [c for g, c in self.live.items() if g not in gone],
            self.binned + [self.live[g] for g in delete if g in self.live])
        gtin = str(card["gtin"]) if card is not None else None
        bound = self._bound(gtin) if kind == "restore" and gtin is not None else None
        same = bound is not None and (bound == offered
                                      or self.cx.same_product(bound, offered, code))
        # `_identify`'s reset rule: a restored number keeps its curated data when it is bound to
        # nothing (nothing to reset) or to the same product (review 26: an unbound number's
        # text claimed „the same product")
        return texts.Pick(kind, gtin, kind == "restore" and (bound is None or same), same)

    def _bound(self, gtin: str) -> str | None:
        """The CODEX card our `gtin` is bound to once this plan is applied — a seed of this
        plan, else the stored binding — what `_identify` compares a later pick with."""
        seed = next((s["card"] for s in reversed(self.plan.seeds)
                     if s["scope"] == self.scope.name and s["gtin"] == gtin), None)
        b = self.cx.bindings.get((self.scope.name, gtin))
        return seed if seed is not None else b.card if b is not None else None

    def _seed(self, item: dict, card: str, *, replaces: bool = False) -> None:
        """Bindings found this run — for EVERY number of the group (a legacy „0"+code twin too:
        left alone later it must still follow its card, review 9 🔵). `replaces` = it
        overwrites an existing binding with another card: stored only by an APPLIED run, so a
        pick seen during a dry-run / blocked run still triggers its reset when the apply finally
        comes (review 5 🟡); a twin already bound to another card is such a replacement too."""
        for g in item["gtins"]:
            b = self.cx.bindings.get((self.scope.name, g))
            self.plan.seeds.append({
                "scope": self.scope.name, "gtin": g, "card": card,
                "replaces": replaces or (g != item["gtin"] and b is not None and b.card != card)})

    def _reset_from(self, item: dict, old: str, since: datetime | None) -> None:
        """Our number becomes ANOTHER product than CODEX card `old` it was: `old`'s curated data
        is reset (`_reset`) and the mapping rows of the number (older than `since`, the pick —
        all of them when None) go to a human (`_repicked_review`)."""
        item["reset_from"] = old
        for g in item["gtins"]:              # every number of the group (review 10 🔵)
            self.repicked[g] = (old, since)

    def _identify(self, code: str, item: dict) -> str | None:
        """The CODEX card our card with `code` IS (`_known`); a card not known yet is identified
        from the code's whole history (`_by_history` — the one rule for an unbound number). A
        card whose CODEX card left for good while exactly one card carries our code under OUR
        name is re-bound to it (recreated in CODEX). None = cannot tell yet (it waits, or a
        human decides)."""
        cx, name, gtin = self.cx, item["name"], item["gtin"]
        known = self._known(gtin)
        if known.card is not None:
            if known.picked:
                old = known.old
                picked_gone = cx.glitched(known.card)
                if picked_gone or (old is not None and old.card != known.card
                                   and cx.glitched(old.card)):
                    # the picked card — or the one it replaces — missing from ONE list is a
                    # glitch: the pick waits (no seed, no reset, no renumber onto it — review
                    # 22), the next list settles it (review 20 🟡: the reset kept the old
                    # product's sklad; review 21 🔵: the rows review called the replaced product
                    # gone from CODEX — both final once the binding is stored)
                    self.waiting.update({g: "pick" for g in item["gtins"]})
                    missing = known.card if picked_gone or old is None else old.card
                    self.plan.wait(item, texts.why_pick_waits(known.card, missing))
                    return None
                replaces = old is not None and old.card != known.card
                ev = cx.events[(self.scope.table, gtin)]
                # the pick restored our NEVER-identified Kôš card as it was (reviews 40-42): when
                # judged (not the card's product — reset or a human told) its binding is stored
                # by an applied run only (review 5's rule — review 41: a dry-run's binding hid
                # the pick from the apply; review 42: and the one-time review never reached ops)
                judged = (old is None and ev.restored
                          and self._restored_pick(item, gtin, code, known.card, ev))
                self._seed(item, known.card, replaces=replaces or judged)
                if old is not None and replaces and not cx.same_product(old.card, known.card,
                                                                        code):
                    # our number used to be ANOTHER product: the pick restored its old Kôš
                    # card „as it was" — its curated data belongs to that product (review 4).
                    # Products compared, never OUR name: a human rename before the delete +
                    # pick would hide it (review 7)
                    self._reset_from(item, old.card, ev.at)
                return known.card
            others = cx.carriers(code) - {known.card}
            if (cx.gone_twice(known.card) and len(others) == 1
                    and is_named(name, cx.rows(next(iter(others)), code))):
                # our CODEX card left for good and exactly one card carries our code under OUR
                # name: it is that card now (recreated in CODEX — review 6; or a number a human
                # renamed to it, as the review asks — review 8). The SAME product keeps its data
                # (durable, stored in every mode — review 6 🟡); another product is a pick in
                # all but name: reset + its rows to a human, stored by an applied run only
                # (review 9 🟡: it kept the old product's alias / doplnok)
                card = others.pop()
                same = cx.same_product(known.card, card, code)
                self._seed(item, card, replaces=not same)
                if not same:
                    self._reset_from(item, known.card, None)
                return card
            if self._contested(known, name, code):
                self.plan.add_review(item, self._contest_reason(gtin, name, code, known))
                return None
            # a group member that joined later (a legacy twin back from the Kôš) is bound to
            # the group's card too (review 10 🔵: left alone it was identified from the list)
            unbound = [g for g in item["gtins"] if (self.scope.name, g) not in cx.bindings]
            if unbound:
                self._seed(dict(item, gtins=unbound), known.card)
            return known.card
        found = self._by_history(code, item, cx.carriers(code))
        if found is None:
            return None
        self._seed(item, found)
        return found

    def _by_history(self, code: str, item: dict, carriers: set[str]) -> str | None:
        """THE rule for an UNBOUND number (every number on the first post-deploy lists): which
        CODEX card it IS, from every card that carried its code since the history began
        (`Codex.since_seed`, each counted from its first list since then — `Codex.seen_from`:
        a sighting in a list older than the beginning is no evidence, reviews 33-34) — the
        carriers now (none, one or several) and before — never "who holds the code now / held
        it last" (reviews 25 / 28 / 29: a reuser — also one that moved on, or one beside a
        duplicate carrier — dragged our rožok along, round 1's 🔴).
        - One list is no proof: a card that left the code since the last list, a card arriving
          on a code no card carried before (review 30: a #467 "missing" card bound to it and
          then removed), or (no carrier now) a last carrier missing from this list only → it
          waits (protected, `waiting`).
        - The ONE card on the code since the history began (`Codex.seeded_at`) → that card —
          only when NO other card ever carried it: one seen there only before we watched is
          never a candidate, but still another carrier (review 35: round 33 dropped it from the
          count too, and the pagáč that reused our rožok's code got our number).
        - Else its NAME: the one carrier now named so (its rows), else the one card ever named
          so (`Codex.product` — its history name too) — unless that card took the code over
          from ANOTHER product (`_took_over`: the #467 drift button offers the code's holder,
          round 9's rule → a human, reviews 26-29; one of them missing once → it waits).
        - Else a human decides, with ways out that work (`_carrier_way_out`); the review names
          the cards seen on the code only before we watched (`unwatched`)."""
        cx, name = self.cx, item["name"]
        now = sorted(carriers)
        hist = cx.since_seed(code)               # a pre-history-only sighting: no evidence
        if not hist:
            return None                          # no stredisko-1 history: never touched
        unwatched = [c for c in sorted(cx.carried.get(code, {})) if c not in hist]
        earlier = [c for c in hist if c not in carriers]
        seed = cx.seeded_at or NEVER

        def since(c: str) -> datetime:
            return cx.seen_from(c, code)

        recent = [c for c in earlier if not cx.absent_before(c, code)] if now else []
        arrived = bool(now) and not earlier and all(
            since(c) > seed and (cx.prev_as_of is None or since(c) > cx.prev_as_of) for c in now)
        missing = [] if now else [c for c in cx.owners.get(code, []) if cx.glitched(c)]
        if recent or arrived or missing:
            self._wait_carrier(item, texts.why_new_carrier(code, now, recent) if recent
                               else texts.why_arrived(code, now, unwatched) if arrived
                               else texts.why_glitch(", ".join(missing)))
            return None
        if len(hist) == 1 and not unwatched and since(hist[0]) <= seed:
            return hist[0]
        ours = codex_cards.name_key(name)
        named = ([c for c in now if is_named(name, cx.rows(c, code))]
                 or [c for c in hist if ours and ours in cx.product(c, code)])
        if len(named) != 1:
            self.plan.add_review(item, texts.carrier_changed(
                self.scope.name, item["gtin"], code, [(c, cx.name_of(c, code)) for c in now],
                earlier, named, self._carrier_way_out(code, ours, now, hist),
                last=sorted(cx.owners.get(code, [])),
                unwatched=[(c, cx.name_of(c, code)) for c in unwatched],
                named_unwatched=[c for c in unwatched if ours and ours in cx.product(c, code)]))
            return None
        card = named[0]
        took = self._took_over(code, card)
        # a card never seen on the code since the beginning is never "missing from this list
        # only" — no previous synced list made it look so (review 37)
        missing = [d for d in took if cx.glitched(d) and d not in unwatched]
        if missing:
            self._wait_carrier(item, texts.why_glitch(", ".join(missing)))
            return None
        if took:
            self.plan.add_review(item, texts.carrier_named_now(
                self.scope.name, item["gtin"], name, code, card, took, now,
                self._carrier_way_out(code, ours, now, hist)))
            return None
        return card

    def _took_over(self, code: str, card: str) -> list[str]:
        """The cards of ANOTHER product that carried `code` before `card` first did — `card`
        took the code over from them (a reuse). A number named like `card` may have that name
        from the #467 drift button, which offers the code's holder — while its data is theirs.
        `card` counts from its first list since the history began (`Codex.seen_from` — a
        sighting before we watched hid a take-over, review 34); another card seen on the code
        before we watched (`Codex.before_watch`) carried it before ANY card first seen since —
        never "seeded together" with one at the beginning (review 36: its clamped first
        sighting tied, our rožok drift-renamed to the reusing pagáč was bound to it and later
        removed); else its first sighting."""
        cx = self.cx
        first = cx.seen_from(card, code)
        return [d for d in sorted(cx.carried.get(code, {}))
                if d != card and not cx.same_product(d, card, code)
                and (cx.before_watch(d, code) or cx.first_seen.get((d, code), NEVER) < first)]

    def _wait_carrier(self, item: dict, why: str) -> None:
        """One list is no proof: the number waits — reported, and protected from a renumber
        landing on it (`waiting`, review 25)."""
        self.plan.wait(item, why)
        self.waiting.update({g: "carrier" for g in item["gtins"]})

    def _carrier_way_out(self, code: str, ours: str, now: list[str], hist: list[str]) -> str:
        """The ways out of a carrier-change review, each what the next list then decides
        (`_by_history`): rename our card to a name only ONE card that ever carried the code
        bears (never our own name, never a card gone from CODEX — review 10's rule — never one
        that took the code over from another product: a human again, reviews 26-29) → bound to
        that card; the pick of the code among the carriers now (`_pick_advice`) → bound to the
        picked card; delete."""
        cx = self.cx
        renames = []
        for c in hist:
            label = cx.name_of(c, code)
            key = codex_cards.name_key(label)
            if (key and key != ours and not cx.gone_twice(c) and not self._took_over(code, c)
                    and sum(key in cx.product(e, code) for e in hist) == 1):
                renames.append((c, label))
        return texts.carrier_way_out(renames, self._pick_advice(code, now) if now else None)

    def _settle(self, code: str, group: list[dict]) -> tuple[dict, list[dict]] | None:
        """Pass 1 for one group: which CODEX card it IS (`_identify`) and the reset a pick /
        rebind of another product implies — None when a human decides first."""
        gtins = [str(c["gtin"]) for c in group]
        item = {"scope": self.scope.name, "gtin": gtins[0], "gtins": gtins, "code": code,
                "name": group[0].get("name", ""),
                # each number's name as the plan found it — what a retire stores (review 8)
                "names": {str(c["gtin"]): str(c.get("name") or "") for c in group}}
        card = self._identify(code, item)
        if card is None:
            self.unsettled.update(gtins)
            return None
        self.settled.update({g: card for g in gtins})
        item["codex_card"] = card
        reset = item.pop("reset_from", None) is not None
        if item.pop("reset_kos", False) or reset:
            # every number of the group — a legacy twin keeps no old-product data either
            for g in gtins:
                self._reset(dict(item, gtin=g), self.live[g])
        return item, group

    def _follow(self, item: dict, group: list[dict]) -> None:
        """Pass 2 for one settled group: follow its CODEX card — stays, leaves, renumbers. The
        group is read from the simulated catalog, so a renumber / fill carries the RESET data,
        never the old product's (review 5 🟡), and a merge target reset by ANOTHER group is
        read after its reset (review 18 🟡)."""
        cx, code, card, gtins = self.cx, item["code"], item["codex_card"], item["gtins"]
        group = [self.live.get(str(c["gtin"]), c) for c in group]
        if cx.rows(card, code):              # our CODEX card still carries our code
            for g in gtins:
                self.identity[g] = (card, code)
            return
        if card not in cx.by_card:           # our CODEX card left stredisko 1
            if not cx.gone_twice(card):      # one missing snapshot is no proof (a glitch)
                self.plan.wait(item, texts.why_glitch(card))
                return
            if not cx.cards.has(code):
                self.plan.removals.append(item)
                self._vacate(group)
            else:
                self.plan.add_review(item, self._gone_reason(card, code))
            return
        succ, why = self._successor(card, code)
        if succ is None:
            self.plan.add_review(item, why)
            return
        self._renumber(item, group, card, succ)

    def _gone_reason(self, card: str, code: str) -> str:
        """Our CODEX card left stredisko 1 for good, the code lives on elsewhere — the way out
        per case, never a promise the sync cannot keep (review 14 🔵); the rename rebind resets
        the data exactly when `_identify` says so (`same_product` — review 15)."""
        now = sorted(self.cx.carriers(code))
        return texts.gone(
            self.scope.name, card, code, now,
            pick=self._pick_advice(code, now) if len(now) > 1 else None,
            same_one=len(now) == 1 and self.cx.same_product(card, now[0], code))

    def _successor(self, card: str, code: str) -> tuple[str | None, str]:
        """The ONE new code our CODEX `card` carries for this catalog → (Y, ""), or
        (None, a Slovak reason a human reads)."""
        cx, scope = self.cx, self.scope
        codes = {r.code for r in cx.by_card.get(card, [])} - {code}
        fit = codes & cx.pickable[scope.name].keys()
        if not fit:
            return None, texts.successor_not_offered(card, sorted(codes), scope.label)
        if len(fit) > 1:
            newest = max(cx.first_seen.get((card, c), NEVER) for c in fit)
            fit = {c for c in fit if cx.first_seen.get((card, c), NEVER) == newest}
        if len(fit) != 1:
            return None, texts.successor_ambiguous(card, sorted(fit))
        succ = fit.pop()
        others = cx.carriers(succ) - {card}
        if others:
            return None, texts.successor_shared(succ, card, sorted(others))
        return succ, ""

    def _renumber(self, item: dict, group: list[dict], card: str, succ: str) -> None:
        target = _ours(self.live.values(), self.scope.name).get(succ)
        binned = _ours(self.binned, self.scope.name).get(succ)
        hit = target if target is not None else binned
        at = str(hit["gtin"]) if hit is not None else ""
        hit_known = self._known(at) if hit is not None else None
        unknown = hit_known is not None and hit_known.card is None
        if at in self.waiting or (unknown and at in self.unsettled):
            # what our number IS settles first — never a merge onto it (reviews 22 / 25)
            self.plan.wait(item, texts.why_renumber_waits(succ, at, self.waiting.get(at)))
            return
        # what the number IS: its stored identity, else this plan's (a binding this plan stores
        # — review 25: an unbound number settled as another card was merged into)
        other = (hit_known.card if hit_known is not None and hit_known.card is not None
                 else self.settled.get(at))
        # our Kôš number whose card left CODEX for good: no two live products — overwritten like
        # an unknown one, `_kos_review` decides (review 40: blocked, every line of ours on the
        # dead code was held; review 41: `_contested` blocked it too, with an untrue text)
        gone_kos = (target is None and other not in (None, card)
                    and self.cx.gone_twice(str(other)))
        if other not in (None, card) and not gone_kos:
            # our card with the new code is ANOTHER CODEX card (e.g. a #477 pick of the
            # product that held the code before) — never a silent merge of two products. The
            # way out is read once the whole plan is done (`_other_card_review`)
            self.other_card.append((item, succ, str(other), card))
            return
        hit_name = str((hit or {}).get("name") or "")
        if hit_known is not None and not gone_kos and self._contested(hit_known, hit_name, succ):
            # our card with the new code — live, or its Kôš copy (review 8) — was renamed by a
            # human after the sync retired it: never merged into / restored over / renamed, a
            # human settles it first (review 7)
            was = ((hit_known.old.retired_name if hit_known.old else None)
                   or self.cx.name_of(card, succ))
            self.plan.add_review(item, texts.renumber_contested(
                succ, card, hit_name, was, target is not None))
            return
        mode = "merge" if target is not None else "restore" if binned is not None else "create"
        to = str(hit["gtin"]) if hit is not None else succ
        # OUR numbers of this group, plus the canonical number when it is not among them (a
        # human deleted it) but still IS this CODEX card — never an unchecked union (review 9
        # 🔵 / review 10 🔵)
        code = item["code"]
        extra = ({code} if code not in item["gtins"] and self._card_of(code) == card
                 else set())
        old = sorted(set(item["gtins"]) | extra)
        taken = self.cx.taken(card, code)
        hold = taken[0] if taken else None
        split = memory_split(self.conn, self.scope, old, hold)
        entry = dict(item, **{
            "from": item["gtin"], "to": to, "mode": mode, "old_gtins": old,
            # the CODEX card's product, for the ops line (review 14: our pre-plan name may be
            # another product's — a pick restores a Kôš card „as it was")
            "codex_name": self.cx.name_of(card, succ),
            "memory": split.movable, "hold": hold.isoformat() if hold else None,
            "held": {"taught": split.taught, "shipped": split.shipped}, "held_at": split.held_at,
            # the old code left CODEX: a DL number retired with it never comes back from the
            # Kôš (#467) — the ops footer says so (review 30)
            "old_dead": not self.cx.cards.has(code),
            "card": _fields(group[0])})
        if taken and split.taught:
            self.plan.add_review(item, self._held_reason(item["gtin"], code, card, to, split,
                                                         taken[1]))
        if hit is not None:
            self._adopted_review(item, str(hit["gtin"]), card, succ)
            if target is None:
                self._kos_hit_review(item, hit, hit_known, card, succ, other)
        if target is not None:
            fill = _fill(self.scope, target, group[0])
            if fill:
                # review 3 🟡: a merge keeps OUR curated data where the target is blank (a
                # fresh #477 pick carries only the CODEX name [+ sklad])
                entry.update(fill=fill, target=_fields(target))
                self.live[to] = dict(target, **fill)
        self.plan.renumbers.append(entry)
        self._vacate(group)
        if mode != "merge":
            self.live[to] = dict(_fields(group[0]), gtin=to)
            self.binned = [b for b in self.binned if str(b["gtin"]) != to]
        self.identity[to] = (card, succ)

    def _other_card_review(self, item: dict, succ: str, other: str, card: str) -> None:
        """The review of a renumber whose target is ANOTHER CODEX card's number — its way out
        names every live number the picker would select (review 17: a live twin) and what the
        pick then does, read from the catalog as the WHOLE plan leaves it (review 25: a number
        another group vacates later in the same plan is in the Kôš by then)."""
        delete = self._numbers(succ)
        self.plan.add_review(item, texts.renumber_other_card(
            self.scope.name, succ, other, card, self.cx.name_of(card, succ), delete,
            self._pick(succ, delete, card)))

    def _held_reason(self, gtin: str, code: str, card: str, to: str, split: Split,
                     takers: list[str]) -> str:
        """Only TAUGHT held rows are the warehouse's to check; held delivery history just
        stays (review 11) — under the numbers they really sit on (review 15)."""
        return texts.held(split.taught, split.shipped, split.at(gtin), code, takers,
                          self.cx.name_of(card, code), card, to)

    def _adopted_review(self, item: dict, target: str, card: str, succ: str) -> None:
        """Our number `target` becomes card `card`'s again — but another card carried its code
        in between (a round trip X → Y → X): its TAUGHT rows decided since then may be that
        other product's and are adopted as they sit; never silently (review 11 🟡)."""
        foreign = self.cx.foreign(card, succ)
        if foreign is None:
            return
        split = memory_split(self.conn, self.scope, [target], foreign[0])
        if not split.taught:
            return
        self.plan.add_review(item, texts.adopted(split.taught, target, succ, foreign[1], card,
                                                 self.cx.name_of(card, succ)))

    def _vacate(self, group: list[dict]) -> None:
        """Our numbers this plan retires go to the (simulated) Kôš — a later step in the SAME
        push that lands on one of them sees it there, as its old card, never an empty number
        to create over (review 4 🔴: a chain 024 → NEW, 055 → 024 re-created the koláč onto
        the row being retired and it vanished)."""
        for c in group:
            self.live.pop(str(c["gtin"]), None)
            self.binned.append(dict(c))

    def _reset(self, item: dict, card: dict) -> None:
        """A #477 pick restored a number that used to be ANOTHER product: its curated fields
        (orders alias; DL doplnok / mass / cena, and the sklad of the CODEX card the number IS
        now — `_sklad_of`) go back to a fresh pick's — audited, restorable."""
        # every curated field (`card_guard.CURATED_FIELDS`, the board's audit reads the same
        # list — review 43): text cleared, numbers to None, the sklad the picker would give
        new: dict = {k: "" if k in ("alias", "doplnok") else None
                     for k in card_guard.CURATED_FIELDS[self.scope.name]}
        if "sklad" in new:
            sklad = self._sklad_of(item["codex_card"], item["code"])
            new["sklad"] = str(sklad if sklad is not None else card.get("sklad") or "")
        changed = {k: v for k, v in new.items()
                   if (card.get(k) or None) != (v if v != "" else None)}
        if not changed:
            return
        self.plan.resets.append(dict(item, before={k: card.get(k) for k in changed},
                                     after=changed, card=_fields(card)))
        self.live[item["gtin"]] = dict(card, **changed)

    def _sklad_of(self, card: str, code: str) -> int | None:
        """The sklad a DL number bound to CODEX card `card` gets — exactly what a fresh pick
        writes (review 20 🔵): the picker's sklad for the code the card carries now — `code`
        itself, or the code it is renumbered to (`_successor`) — never the sklad of whoever
        carries `code` after the card moved on (review 19 🟡: a kg card went piece-tracked).
        The picker's entry counts only when the card's OWN active named row is among the rows
        it is built from (review 21 🟡: an inactive row left an entry that is purely another
        card's); otherwise the pick's rule (`codex_cards.pick_sklad`) over the card's own rows
        on that code, then over all its rows; None = the card has no stredisko-1 row (the
        number keeps its sklad)."""
        cx, offered = self.cx, self.cx.pickable[self.scope.name]
        rows = cx.by_card.get(card, [])
        now = code if any(r.code == code for r in rows) else (
            self._successor(card, code)[0] if rows else None)
        own = [r for r in rows if r.code == now]
        if now in offered and any(not r.inactive and r.name.strip() for r in own):
            return int(offered[str(now)]["sklad"])
        for pool in (own, rows):
            live = [r for r in pool if not r.inactive and r.name.strip()] or pool
            if live:
                return codex_cards.pick_sklad(r.sklad for r in live)
        return None

    def _memory(self, catalog_codes: set[str]) -> None:
        """Mapping rows of a number the sync RETIRED (an inactive binding — written after the
        renumber, from a frozen question or a held order) follow its card's live number —
        never onto themselves (review 8 🔴: a card back on its old number „moved" X → X and
        every row found itself as the duplicate), never from a `_contested` Kôš copy (review 8
        🟡: a human renamed it — its wordings are that other product's), never a number whose
        own identity is another card (only the rows that qualified themselves — review 9 🔵)."""
        kos_names = {str(c["gtin"]): str(c.get("name") or "") for c in self.binned}
        retired: dict[str, str] = {}
        disputed: set[str] = set()
        for (s, g), b in self.cx.bindings.items():
            if s != self.scope.name or b.active:
                continue
            if self._contested(self._known(g), kos_names.get(g, ""),
                               codex_cards.normalize_code(g) or g):
                disputed.add(g)
            else:
                retired[g] = b.card
        codes_of: dict[str, set[str]] = {}
        for gtin, (card, code) in self.identity.items():
            if gtin in self.live:
                codes_of.setdefault(card, set()).add(code)
        # the ONE live number of each CODEX card (its canonical number when a legacy twin sits
        # next to it — review 3 🔵); a card holding two different codes of ours stays ambiguous
        ours = _ours(self.live.values(), self.scope.name)
        number = {card: str(ours[next(iter(codes))]["gtin"])
                  for card, codes in codes_of.items()
                  if len(codes) == 1 and next(iter(codes)) in ours}
        per: dict[tuple[str, str], set[str]] = {}
        for table in self.scope.memory:
            for (gtin,) in self.conn.execute(
                    f"SELECT DISTINCT gtin FROM {table} WHERE deleted_at IS NULL").fetchall():
                norm = codex_cards.normalize_code(gtin)
                if str(gtin) in disputed or norm in disputed:
                    continue
                key = str(gtin) if str(gtin) in retired else norm
                owner = retired.get(key) if key else None
                # the ONE identity rule decides (review 6 🟡: a newer pick of the retired number
                # makes it another product — its rows are that product's, never moved)
                if (norm and owner and norm not in catalog_codes and owner in number
                        and self._card_of(key or "") == owner):
                    per.setdefault((norm, owner), set()).add(str(gtin))
        for (code, card), gtins in sorted(per.items()):
            to = number[card]
            if not gtins - {to}:
                continue                   # the rows already sit on the card's live number
            old = sorted(gtins - {to})
            # rows decided while CODEX gave the code to another card stay for a human (review
            # 10 🟡) — the same rule as a card's renumber
            taken = self.cx.taken(card, code)
            hold = taken[0] if taken else None
            split = memory_split(self.conn, self.scope, old, hold)
            item = {"scope": self.scope.name, "gtin": code, "gtins": [], "code": code, "name": ""}
            if taken and split.taught:
                self.plan.add_review(item, self._held_reason(code, code, card, to, split,
                                                             taken[1]))
            if sum(split.movable.values()):
                self.plan.renumbers.append(dict(item, **{
                    "codex_card": card, "from": code, "to": to, "mode": "memory",
                    "old_gtins": old, "memory": split.movable,
                    "hold": hold.isoformat() if hold else None,
                    "held": {"taught": split.taught, "shipped": split.shipped},
                    "held_at": split.held_at, "card": {}}))
            elif split.shipped and not split.taught:
                # the numbers the rows really sit on (a legacy twin too — review 14 🔵)
                self._hold_note(item, split.at(code), split.shipped, texts.why_taken(code))
        for gtin, (card, since) in sorted(self.repicked.items()):
            self._repicked_review(gtin, card, since)

    def _repicked_review(self, gtin: str, old: str, since: datetime | None) -> None:
        """Mapping rows of a number that became another product than CODEX card `old` — OLDER
        than the pick (`since`; every row for a rename rebind, `since` None) — may be the old
        product's (a frozen question answered with the retired number — review 6) or the new
        one's (a Naučené edit / a revive re-points a row and keeps its `created_at` — review 7):
        nothing tells, so they stay where they are and a human checks them. Where they are is
        read from the same-push renumber of the number, never guessed (review 14 🔵): the rows
        it carries are under its new number, the rows it HOLDS (reuse window) stay. Taught rows
        are all listed with the old product's home (review 15 🔵: leaving the held ones out lost
        that pointer); held delivery history is left to the renumber's own `held` line."""
        code = codex_cards.normalize_code(gtin) or gtin
        entry = next((r for r in self.plan.renumbers
                      if r["scope"] == self.scope.name and gtin in r["gtins"]), None)
        moved = entry["to"] if entry is not None else None
        hold = entry.get("hold") if entry is not None else None
        carried = kept = shipped = 0
        for t in self.scope.memory:
            tc, held = taught_clause(t), (held_clause(t) if hold else "FALSE")
            a, b, c = self.conn.execute(
                f"SELECT count(*) FILTER (WHERE {tc} AND NOT {held}), "
                f"count(*) FILTER (WHERE {tc} AND {held}), "
                f"count(*) FILTER (WHERE NOT ({tc}) AND NOT {held}) "
                f"FROM {t} WHERE gtin = %(g)s AND deleted_at IS NULL AND (%(s)s::timestamptz "
                f"IS NULL OR COALESCE(created_at, '-infinity') < %(s)s::timestamptz)",
                {"g": gtin, "s": since, "hold": hold}).fetchone()
            carried += int(a or 0)
            kept += int(b or 0)
            shipped += int(c or 0)
        taught = carried + kept
        old_name = self.cx.name_of(old, code)
        name = next((str(c.get("name") or "") for c in self.catalog if str(c["gtin"]) == gtin),
                    "")
        item = {"scope": self.scope.name, "gtin": gtin, "code": code, "name": name}
        if not taught:
            # delivery history only — nothing for a human to fix in Naučené (review 12 🔵)
            if shipped:
                self._hold_note(item, moved or gtin, shipped,
                                texts.why_repick(gtin, old, old_name), moved=moved is not None)
            return
        # where they can go: the old product's live number, or nowhere when it left CODEX
        # (review 10 🔵: never „preraď" to a card gone from CODEX); a pick is advised only under
        # a code the picker offers the old card for, in this catalog (review 16)
        home = next((g for g, (c, _code) in self.identity.items() if c == old and g in self.live),
                    None)
        offered = sorted(c for c, e in self.cx.pickable[self.scope.name].items()
                         if str(e["card_code"]) == old)
        # a code whose pick would SELECT another number of ours is never advised (review 17)
        picks = {c: self._pick(c, [], old) for c in offered}
        usable = [c for c in offered if picks[c].kind != "select"]
        code_to = usable[0] if usable else offered[0] if offered else None
        fix = texts.repick_fix(
            old, old_name, home=home, offered_code=code_to,
            selected=picks[code_to].gtin if code_to and picks[code_to].kind == "select" else None,
            old_alive=old in self.cx.by_card, label=self.scope.label)
        self.plan.add_review(item, texts.repick(
            gtin, old, old_name, moved=moved, kept=kept, taught=taught, shipped=shipped,
            older=since is not None, fix=fix))

    def _renames(self) -> None:
        for gtin, card in self.live.items():
            if gtin not in self.identity:
                continue
            codex_card, code = self.identity[gtin]
            rows = self.cx.rows(codex_card, code)
            name = card.get("name") or ""
            if not rows or is_named(name, rows):
                continue
            new = best_row(rows).name.strip()
            if new and new != name.strip():
                self.plan.renames.append({
                    "scope": self.scope.name, "gtin": gtin, "code": code, "name": name,
                    "codex_card": codex_card, "old": name, "new": new, "card": _fields(card)})


def build_plan(conn, cx: Codex) -> Plan:
    """Everything the current CODEX list implies for our two catalogs + memories (no writes)."""
    plan = Plan()
    for scope in SCOPES:
        _ScopePlanner(conn, scope, cx, plan).run()
    return plan
