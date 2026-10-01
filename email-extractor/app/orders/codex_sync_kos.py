"""The CODEX card sync's (#478) rules for OUR Kôš numbers a CODEX card takes over — split from
`codex_sync_plan` (its size budget, review 41), mixed into its `_ScopePlanner`.

When our card C gets a new code Y and OUR number Y sits in the Kôš without being known as C —
never identified (no binding, no pick: deleted before the deploy, its code freed in CODEX and
then REUSED for C), or a card that left CODEX for good — the renumber restores Y with OUR data
(CODEX's truth). When Y was not C's product, the rows it left are adopted as they sit, never
silently: taught rows → a human reviews them, delivery history → said in the report (the
review-11/12 rule; reviews 38-40 — a block held every order line of ours on a code CODEX no
longer has and protected nothing in orders, whose recall never reads the catalog). A #477
pick that RESTORED such a Kôš card as it was is judged the same way (review 40).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from . import codex_cards, dl_snapshot, snapshot
from . import codex_sync_texts as texts
from .card_guard import CURATED_FIELDS
from .codex_sync_list import NEVER
from .codex_sync_memory import SYNC_ACTOR, rows_on

if TYPE_CHECKING:
    from .codex_sync_list import Codex, Event
    from .codex_sync_plan import Known, Plan, Scope


@dataclass
class KosPick:
    """A judged #477 restore-pick, its review written once the whole plan is done — the rows
    sit where the same plan's renumber moves them (review 43)."""
    item: dict
    gtin: str
    card: str
    name: str
    drift: list[str]
    others: list[str]
    reset: bool
    since: datetime


class KosRules:
    """Mixin of `codex_sync_plan._ScopePlanner` (its state: `conn`, `scope`, `cx`, `plan`,
    `kos_picks`)."""
    conn: Any
    scope: Scope
    cx: Codex
    plan: Plan
    kos_picks: list[KosPick]

    def _took_over(self, code: str, card: str) -> list[str]:
        raise NotImplementedError       # the planner's identity rule

    def _kos_hit_review(self, item: dict, binned: dict, hit_known: Known | None, card: str,
                        succ: str, other: str | None) -> None:
        """Our Kôš number `binned` a renumber of `card` onto `succ` restores: not known as
        `card` (`other`: unknown, or a card that left CODEX) → `_kos_review`. Known as `card` only
        through a RESTORE-pick never judged (undone in the Kôš / deleted again before any applied
        run): what it was BEFORE the pick decides — never identified (review 43), or the card its
        binding names (review 44: a re-pick over another card's binding hid its rows) — counting
        only its rows from before the pick. A FRESH pick made it `card` (review 44: no review)."""
        if other != card:
            self._kos_review(item, binned, card, succ, other)
            return
        if hit_known is None or not hit_known.picked:
            return
        ev = self.cx.events.get((self.scope.table, str(binned["gtin"])))
        old = hit_known.old
        if ev is None or not ev.restored or (old is not None and old.card == card):
            return
        self._kos_review(item, binned, card, succ, old.card if old is not None else None,
                         since=ev.at)

    def _kos_review(self, item: dict, binned: dict, card: str, succ: str,
                    known: str | None, *, since: datetime | None = None) -> None:
        """Our Kôš number under the new code that was never identified (no binding, no pick —
        e.g. deleted before the deploy, its code freed in CODEX and then REUSED for this card),
        or is a card that left CODEX for good (`known`, review 40), is restored as ours: CODEX's
        truth, OUR data over the dead card's. When it is not this card's product (`known`:
        products compared; else `_kos_verdict`) its taught rows, adopted as they sit, go to a
        human (review 38: silently, the bageta's wording recalled the rožok; the review-11 rule
        for adopted rows — review 39: a BLOCK held every order line of ours on a code CODEX no
        longer has, protected nothing in orders, whose recall never reads the catalog, and led
        the warehouse to a pick restoring the bageta's data) — `_kos_rows`."""
        name = self._kos_name(binned)
        if known is not None:
            if self.cx.same_product(known, card, succ):
                return
            key = codex_cards.name_key(name)
            named_like = bool(key) and key in self.cx.product(card, succ)
            # named like this card (the #467 drift button) → the card it really was named so
            drift, others = ([known], []) if named_like else ([], [known])
        else:
            ours, drift, others = self._kos_verdict(name, card, succ)
            if ours:
                return
        self._kos_rows(item, str(binned["gtin"]), card, name, drift, others, picked=False,
                       since=since, succ=succ)

    def _restored_pick(self, item: dict, gtin: str, code: str, card: str, ev: Event) -> bool:
        """A #477 pick of `card` that restored our NEVER-identified Kôš card `gtin` as it was,
        by its name then not `card`'s product (`_kos_verdict`). Reset like a re-pick of another
        product only on EVIDENCE — its name is another card's that carried the code — for a pick
        since the history began, never over a human's edit since the pick (review 41: a missing
        name match wiped our own croissant restored under a drifted name, and a warehouse fix);
        else its data stays and a human is told. True = judged (reset or told — its binding: an
        applied run only, review 42). Review 40: kept silently, a merge filled only blanks — the
        bageta's mass / cena on our rožok."""
        ours, drift, others = self._kos_verdict(ev.name, card, code)
        if ours:
            return False
        reset = (bool(others) and ev.at >= (self.cx.seeded_at or NEVER)
                 and not self._edited_since(gtin, ev.at))
        # its review is written once the whole plan is done (`_kos_pick_reviews`)
        self.kos_picks.append(KosPick(dict(item), gtin, card, ev.name, drift, others, reset,
                                      ev.at))
        if reset:
            item["reset_kos"] = True
        return True

    def _kos_pick_reviews(self) -> None:
        """The judged restore-picks' reviews, read from the plan as the WHOLE plan leaves it:
        the rows sit where the same plan's renumber moves them (review 43: the text said they
        stay under the old number while the apply moved them — `_repicked_review`'s rule)."""
        for p in self.kos_picks:
            moved = next((r["to"] for r in self.plan.renumbers
                          if r["scope"] == self.scope.name and p.gtin in r["gtins"]), None)
            self._kos_rows(p.item, p.gtin, p.card, p.name, p.drift, p.others, picked=True,
                           reset=p.reset, since=p.since, where=moved)

    def _kos_verdict(self, name: str, card: str, code: str
                     ) -> tuple[bool, list[str], list[str]]:
        """Is our never-identified Kôš card named `name` CODEX card `card`'s product on `code`?
        By name — never a name the #467 drift button may have lent it: `card` took the code over
        from another product (`_took_over`, review 39) → (ours, drift = those earlier carriers
        when only the name says it is ours, others = the cards of another product that carried
        the code under that name — the evidence it is not ours, review 41)."""
        key = codex_cards.name_key(name)
        took = self._took_over(code, card)
        named_like = bool(key) and key in self.cx.product(card, code)
        others = ([d for d in sorted(self.cx.carried.get(code, {}))
                   if d != card and not self.cx.same_product(d, card, code)
                   and key in self.cx.product(d, code)] if key else [])
        return named_like and not took, (took if named_like else []), others

    def _edited_since(self, gtin: str, at: datetime) -> bool:
        """A human edited our card `gtin`'s DATA since `at` — a Produkty save that changed a
        field the reset would clear (the board audits them, `board.services.catalog.upsert`);
        a name-only save (e.g. „Prevziať názov z CODEXu", invited right after the pick) is no
        fix of the data (review 42)."""
        return self.conn.execute(
            "SELECT 1 FROM audit_log WHERE table_name = %s AND row_id = %s AND action = 'update' "
            "AND actor <> %s AND ts > %s AND after ?| %s::text[] LIMIT 1",
            (self.scope.table, gtin, SYNC_ACTOR, at,
             list(CURATED_FIELDS[self.scope.name]))).fetchone() is not None

    def _kos_rows(self, item: dict, at: str, card: str, name: str, drift: list[str],
                  others: list[str], *, picked: bool, reset: bool = False,
                  since: datetime | None = None, succ: str = "",
                  where: str | None = None) -> None:
        """Another product's rows under our number `at` (a Kôš card our card takes over — a
        renumber onto it, or a #477 pick that restored it — only its rows from before the pick:
        the warehouse's own answer at that question is never the other product's, review 41):
        taught → a human checks them (a pick whose data is not reset: always — its data too);
        delivery history → said in the report (review 12). `where` = the number this plan
        moves them to (a pick's renumber in the same plan, review 43)."""
        taught, shipped = rows_on(self.conn, self.scope, at, before=since)
        code = codex_cards.normalize_code(at) or at
        card_name = self.cx.name_of(card, code)
        if picked and (taught or not reset):
            self.plan.add_review(item, texts.kos_picked(
                at, code, card, card_name, name, taught, drift, others, reset=reset,
                where=where or at))
        elif not picked and taught:
            self.plan.add_review(item, texts.kos_adopted(
                item["gtin"], succ, card, card_name, name, taught, drift, others))
        if shipped:
            self._hold_note(dict(item, gtin=at, name=name), where or at, shipped,
                            texts.why_kos_other(at, name, card, drift, others),
                            moved=where is not None)

    def _kos_name(self, card: dict) -> str:
        """A Kôš card's name — a bare retirement marker's from the newest snapshot that had it."""
        name = str(card.get("name") or "").strip()
        if name:
            return name
        last = (snapshot.last_known_card if self.scope.name == "orders"
                else dl_snapshot.last_known_dl_card)(self.conn, str(card["gtin"]))
        return str(last["name"]) if last else ""

    def _hold_note(self, item: dict, at: str, shipped: int, why: str, *,
                   moved: bool = False) -> None:
        """Delivery history (shipped rows) with nothing for a human to fix — a report + ops
        note, never silent (review 12 🔵); `at` = the number(s) the rows sit on after this plan,
        `moved` = a same-push renumber carried them there (reviews 13-14 🔵)."""
        self.plan.holds.append(dict(item, held={"taught": 0, "shipped": shipped}, at=at,
                                    moved=moved, why=why))
