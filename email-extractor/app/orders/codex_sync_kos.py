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

from typing import TYPE_CHECKING, Any

from . import codex_cards, dl_snapshot, snapshot
from . import codex_sync_texts as texts
from .codex_sync_memory import rows_on

if TYPE_CHECKING:
    from .codex_sync_list import Codex, Event
    from .codex_sync_plan import Plan, Scope


class KosRules:
    """Mixin of `codex_sync_plan._ScopePlanner` (its state: `conn`, `scope`, `cx`, `plan`)."""
    conn: Any
    scope: Scope
    cx: Codex
    plan: Plan

    def _took_over(self, code: str, card: str) -> list[str]:
        raise NotImplementedError       # the planner's identity rule

    def _kos_review(self, item: dict, binned: dict, card: str, succ: str,
                    known: str | None) -> None:
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
            drift: list[str] = []
        else:
            ours, drift = self._kos_verdict(name, card, succ)
            if ours:
                return
        self._kos_rows(item, str(binned["gtin"]), card, name, drift, picked=False, succ=succ)

    def _restored_pick(self, item: dict, gtin: str, code: str, card: str, ev: Event) -> None:
        """A #477 pick of `card` that restored our NEVER-identified Kôš card `gtin` as it was: by
        its name then another product (`_kos_verdict`) → reset like a re-pick of another product,
        its taught rows to a human (review 40: kept silently, a merge filled only blanks — the
        bageta's mass / cena on our rožok)."""
        ours, drift = self._kos_verdict(ev.name, card, code)
        if not ours:
            item["reset_kos"] = True
            self._kos_rows(item, gtin, card, ev.name, drift, picked=True)

    def _kos_verdict(self, name: str, card: str, code: str) -> tuple[bool, list[str]]:
        """Is our never-identified Kôš card named `name` CODEX card `card`'s product on `code`?
        By name — never a name the #467 drift button may have lent it: `card` took the code over
        from another product (`_took_over`, review 39) → (ours, drift = those earlier carriers
        when only the name says it is ours)."""
        key = codex_cards.name_key(name)
        took = self._took_over(code, card)
        named_like = bool(key) and key in self.cx.product(card, code)
        return named_like and not took, (took if named_like else [])

    def _kos_rows(self, item: dict, at: str, card: str, name: str, drift: list[str], *,
                  picked: bool, succ: str = "") -> None:
        """Another product's rows under our number `at` (a Kôš card our card takes over — a
        renumber onto it, or a #477 pick that restored it): taught → a human checks them;
        delivery history → said in the report (review 12)."""
        taught, shipped = rows_on(self.conn, self.scope, at)
        code = codex_cards.normalize_code(at) or at
        if taught:
            card_name = self.cx.name_of(card, code)
            self.plan.add_review(item, texts.kos_picked(
                at, code, card, card_name, name, taught, drift) if picked else texts.kos_adopted(
                item["gtin"], succ, card, card_name, name, taught, drift))
        if shipped:
            self._hold_note(dict(item, gtin=at, name=name), at, shipped,
                            texts.why_kos_other(at, name, card, drift))

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
