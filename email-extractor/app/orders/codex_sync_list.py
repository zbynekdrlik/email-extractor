"""The stredisko-1 CODEX stock-card list as the CODEX card sync (#478) reads it — the rows,
the history of who carried which code since when (`codex_card_history`), our card bindings
(`codex_card_bindings`) and the human #477 picks (`audit_log`). `codex_sync_plan` decides on
it; nothing here decides or writes except `update_history` (the history of an accepted list).
Split from the planner (review 23: the planner reached the size budget).
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from . import card_guard, codex_cards
from .codex_sync_memory import SYNC_ACTOR

log = logging.getLogger("orders.codex_sync")

STREDISKO = codex_cards.PICK_STREDISKO
NEVER = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Row:
    """One stredisko-1 row of the current CODEX list."""
    code: str
    card: str
    sklad: int
    name: str
    inactive: bool
    changed_at: datetime | None


@dataclass
class Codex:
    """The stredisko-1 CODEX list, its history and our card bindings, as the plan reads them."""
    cards: codex_cards.CodexCards
    by_card: dict[str, list[Row]]
    by_code: dict[str, list[Row]]
    first_seen: dict[tuple[str, str], datetime]
    last_seen: dict[tuple[str, str], datetime]
    names: dict[tuple[str, str], str]           # (card, code) -> its name while it carried it
    card_seen: dict[str, datetime]              # card -> last snapshot any of its rows was in
    owners: dict[str, list[str]]                # code -> the cards that carried it LAST
    carried: dict[str, dict[str, datetime]]     # code -> {card: last seen carrying it}
    pickable: dict[str, dict[str, dict]]        # scope -> the #477 pick's cards by code
    prev_as_of: datetime | None                 # the previous SUCCESSFULLY synced snapshot
    bindings: dict[tuple[str, str], Binding]
    # (override table, our gtin) -> the newest HUMAN (re)entry of the card into the catalog
    # (a #477 pick = audit `create` naming its CODEX card; a Kôš „Vrátiť" = `restore`)
    events: dict[tuple[str, str], Event]
    # when the history began (its oldest first sighting — the migration seed): a card on a code
    # since then was there before we watched; one arriving later on a code no card carried is
    # a carrier change (review 30)
    seeded_at: datetime | None = None

    def carriers(self, code: str) -> set[str]:
        return {r.card for r in self.by_code.get(code, [])}

    def since_seed(self, code: str) -> list[str]:
        """The cards seen carrying `code` in a list since the history began. A card seen on it
        only in a list OLDER than the beginning (recorded as first seen AT the beginning, so
        its reuse window opens — review 32) is no evidence of who our number is: it bound a
        #467 "missing" card with no name check, then removed / renumbered it (review 33)."""
        seed = self.seeded_at or NEVER
        return sorted(c for c, t in self.carried.get(code, {}).items() if t >= seed)

    def product(self, card: str, code: str) -> set[str]:
        """The product names (#467 `name_key`) of CODEX `card`: its current rows + its name
        while it carried `code` (the history keeps it after the card moved on)."""
        keys = {codex_cards.name_key(r.name) for r in self.by_card.get(card, [])}
        keys.add(codex_cards.name_key(self.names.get((card, code), "")))
        return {k for k in keys if k}

    def name_of(self, card: str, code: str) -> str:
        """CODEX `card`'s name for `code`: its central current NAMED row (the `_name_order` rule
        the renames use — review 15; a blank row is never a name, as the picker skips it —
        review 16), else the name it had while it carried the code."""
        rows = [r for r in self.rows(card, code) if r.name.strip()]
        if rows:
            return best_row(rows).name
        return (self.names.get((card, code))
                or next((r.name for r in self.by_card.get(card, [])), "") or "?")

    def taken(self, card: str, code: str) -> tuple[datetime, list[str]] | None:
        """CODEX gave `code` to another card after `card` last carried it (a REUSE) → (when
        the first such card was first SEEN on the code — nobody could pick it before a push
        listed it (review 11) — and those cards). A mapping decided on our number since then
        may be that other product's (review 10 🟡). None = no other card had it after `card`."""
        left = self.last_seen.get((card, code))
        if left is None:
            return None
        takers = sorted(d for d, t in self.carried.get(code, {}).items() if d != card and t > left)
        return (self._window(card, code, takers), takers) if takers else None

    def foreign(self, card: str, code: str) -> tuple[datetime, list[str]] | None:
        """Another card carried `code` since `card` first had it — e.g. while `card` was away
        on another code (a round trip X → Y → X, the #478 incident's shape) → (when the first
        of them was first seen on it, those cards); None otherwise (review 11 🟡)."""
        mine = self.first_seen.get((card, code))
        if mine is None:
            return None
        others = sorted(d for d, t in self.carried.get(code, {}).items() if d != card and t > mine)
        return (self._window(card, code, others), others) if others else None

    def _window(self, card: str, code: str, others: list[str]) -> datetime:
        mine = self.first_seen.get((card, code), NEVER)
        return min(max(self.first_seen.get((d, code), mine), mine) for d in others)

    def same_product(self, old: str, new: str, code: str) -> bool:
        """Two CODEX cards name the same product — the curated data taught for `old` fits
        `new` (a card recreated in CODEX), else it belongs to another product."""
        return bool(self.product(old, code) & self.product(new, code))

    def rows(self, card: str, code: str) -> list[Row]:
        return [r for r in self.by_card.get(card, []) if r.code == code]

    def absent_before(self, card: str, code: str | None = None) -> bool:
        """`card` (carrying `code`, when given) missing from stredisko 1 in the previous
        successfully synced snapshot too — never true without one (no guess from one push)."""
        seen = self.card_seen.get(card) if code is None else self.last_seen.get((card, code))
        return self.prev_as_of is not None and (seen or NEVER) < self.prev_as_of

    def gone_twice(self, card: str) -> bool:
        """`card` missing from stredisko 1 in this AND the previous synced CODEX snapshot."""
        return card not in self.by_card and self.absent_before(card)

    def glitched(self, card: str) -> bool:
        """`card` missing from THIS snapshot only — one list is no proof (an export glitch):
        nothing that depends on the card is decided on it."""
        return card not in self.by_card and not self.gone_twice(card)


@dataclass(frozen=True)
class Binding:
    card: str
    active: bool
    bound_at: datetime
    retired_name: str | None = None     # our card's name when the sync retired the number


@dataclass(frozen=True)
class Event:
    at: datetime
    card: str | None     # the CODEX card a #477 pick named; None for a legacy creation


def is_named(name: str, rows: list[Row]) -> bool:
    """Our card name is one of these CODEX rows' names, cosmetics aside (#467 `name_key`)."""
    ours = codex_cards.name_key(name)
    return any(codex_cards.name_key(r.name) == ours for r in rows)


def best_row(rows: list[Row]) -> Row:
    """The central row of a card's rows for a code (active first, the #477 `_name_order`)."""
    active = [r for r in rows if not r.inactive] or rows
    return min(active, key=lambda r: codex_cards._name_order(r.sklad == 1, r.changed_at, r.name))


def update_history(conn, as_of: datetime) -> None:
    """Record every (stredisko, card, code) of the current list: a new one gets
    first_seen = last_seen = `as_of` (the CODEX data age), a known one advances last_seen — and
    takes the list's name only then: an OLDER re-sent list (recorded since review 12) never sets
    a newer name back (review 13 🟡: `same_product` then judged a recreated card another
    product and cleared its data). Every ACCEPTED list is recorded — it is pickable the moment
    it is accepted, so its sightings open reuse windows (round 12; review 32: skipping an old
    one moved a wording taught from it with the wrong card) — but a pair first seen in a list
    OLDER than the history's beginning is recorded as first seen AT that beginning — its
    stredisko's own, the one `Codex.seeded_at` reads for stredisko 1 (review 33): the beginning
    never moves back, else every card on a code since then would count as "arrived" (review 31).
    Only first_seen is clamped — last_seen stays the list's age (the name rule above). Such a
    pair is no identity evidence (`Codex.since_seed`, review 33)."""
    begun = conn.execute("SELECT min(first_seen) FROM codex_card_history").fetchone()
    if begun and begun[0] is not None and as_of < begun[0]:
        log.warning("CODEX card history: the list (CODEX data as of %s) is older than the "
                    "history's beginning (%s) — its new sightings are recorded at the beginning "
                    "(#478)", as_of, begun[0])
    # GREATEST ignores NULL: a stredisko with no history yet records the list's own age
    conn.execute(
        """INSERT INTO codex_card_history (stredisko, card_code, code, name, first_seen,
                                           last_seen)
           SELECT c.stredisko, c.card_code, c.code, max(c.name),
                  GREATEST(%(as_of)s, (SELECT min(h.first_seen) FROM codex_card_history h
                                        WHERE h.stredisko = c.stredisko)),
                  %(as_of)s
             FROM codex_stock_cards c
            GROUP BY c.stredisko, c.card_code, c.code
           ON CONFLICT (stredisko, card_code, code) DO UPDATE
              SET name = CASE WHEN EXCLUDED.last_seen >= codex_card_history.last_seen
                              THEN EXCLUDED.name ELSE codex_card_history.name END,
                  last_seen = GREATEST(codex_card_history.last_seen, EXCLUDED.last_seen)""",
        {"as_of": as_of})


def newest_seen(conn) -> datetime | None:
    """The newest CODEX snapshot the history already holds (an older push must not undo it)."""
    row = conn.execute("SELECT max(last_seen) FROM codex_card_history").fetchone()
    return row[0] if row else None


def _prev_as_of(conn, as_of: datetime) -> datetime | None:
    """The newest CODEX snapshot older than `as_of` that a sync actually PROCESSED (a push
    whose sync failed or was skipped never counts as "seen" — review 3)."""
    row = conn.execute(
        "SELECT max((report->>'as_of')::timestamptz) FROM codex_sync_runs "
        "WHERE status IN ('apply', 'dry-run', 'blocked') "
        "AND (report->>'as_of')::timestamptz < %s", (as_of,)).fetchone()
    return row[0] if row else None


def _events(conn) -> dict[tuple[str, str], Event]:
    """The newest HUMAN creation of each catalog card — a #477 pick („Vybrať kartu z CODEXu"
    writes `create` with the picked ACSKLP in `after.codex_card`, a Kôš card restored by a
    pick included) — never the sync's own writes. A Kôš „Vrátiť" (`restore`) is NOT a new card:
    it reverts one change of the same card (review 4 🟡: counting it reset a valid binding and
    re-bound our rožok to the pagáč that reused its code); a number the sync retired that comes
    back is re-identified through its inactive binding anyway."""
    rows = conn.execute(
        """SELECT DISTINCT ON (table_name, row_id) table_name, row_id, ts, after->>'codex_card'
             FROM audit_log
            WHERE table_name IN ('catalog_overrides', 'dl_catalog_overrides')
              AND action = 'create' AND actor <> %s
            ORDER BY table_name, row_id, id DESC""", (SYNC_ACTOR,)).fetchall()
    return {(t, str(g)): Event(ts, card or None) for t, g, ts, card in rows}


def load(conn, cards: codex_cards.CodexCards, as_of: datetime,
         scopes: Iterable[str]) -> Codex:
    """The current stredisko-1 list + the history + our bindings (call `update_history`
    first, so a code new in this push is known with its first_seen)."""
    by_card: dict[str, list[Row]] = {}
    by_code: dict[str, list[Row]] = {}
    for r in conn.execute(
            "SELECT code, card_code, sklad, name, inactive, changed_at FROM codex_stock_cards "
            "WHERE stredisko = %s", (STREDISKO,)).fetchall():
        row = Row(r[0], r[1], int(r[2]), r[3] or "", bool(r[4]), r[5])
        by_card.setdefault(row.card, []).append(row)
        by_code.setdefault(row.code, []).append(row)
    hist = conn.execute("SELECT card_code, code, first_seen, last_seen, name "
                        "FROM codex_card_history WHERE stredisko = %s", (STREDISKO,)).fetchall()
    latest: dict[str, datetime] = {}
    card_seen: dict[str, datetime] = {}
    first_seen: dict[tuple[str, str], datetime] = {}
    last_seen: dict[tuple[str, str], datetime] = {}
    carried: dict[str, dict[str, datetime]] = {}
    names: dict[tuple[str, str], str] = {}
    for card, code, first, last, name in hist:
        latest[code] = max(latest.get(code, NEVER), last)
        card_seen[card] = max(card_seen.get(card, NEVER), last)
        first_seen[(card, code)] = first
        last_seen[(card, code)] = last
        names[(card, code)] = name or ""
        carried.setdefault(code, {})[card] = last
    owners: dict[str, list[str]] = {}
    for card, code, _first, last, _name in hist:
        if last == latest[code]:
            owners.setdefault(code, []).append(card)
    bindings = {(s, g): Binding(c, bool(a), at, rn) for s, g, c, a, at, rn in conn.execute(
        "SELECT scope, gtin, card_code, active, bound_at, retired_name FROM codex_card_bindings"
    ).fetchall()}
    return Codex(cards, by_card, by_code, first_seen, last_seen, names, card_seen, owners,
                 carried, {s: card_guard.pickable(conn, s) for s in scopes},
                 _prev_as_of(conn, as_of), bindings, _events(conn),
                 min(first_seen.values(), default=None))
