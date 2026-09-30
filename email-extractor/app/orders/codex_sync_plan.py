"""The PLAN of the CODEX card sync (#478) — what the current CODEX stock-card list implies for
the cards WE ALREADY HAVE (both catalogs) and the code-keyed memories. `codex_sync` executes it,
or only reports it in dry-run. Nothing here writes; the new card bindings it finds are in
`Plan.seeds` (the executor stores them in both modes — they are identity, not catalog data).

**Identity = the CODEX card our card IS**, stored in `codex_card_bindings` ((scope, our gtin)
→ ACSKLP on stredisko 1 — `codex_cards.PICK_STREDISKO`, the #477 pick scope; ACSKLP is unique
only WITHIN a stredisko: live 2026-09-30 card 400448 is garlic on stredisko 1, crisps on 4).
A card is bound once, when its code has exactly ONE stredisko-1 carrier (or our name picks one
of several, or — the code gone already — the history's last carrier). From then on the sync
follows THAT card and never "whoever holds the code now": a code can be REUSED for another
product (the #478 review 🔴s: following the newest carrier renamed our rožok to a pagáč and
moved its memory). `codex_card_history` (per stredisko, card, code: first/last seen) gives the
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

A code with no stredisko-1 carrier and no history (the #467 "missing" cards) is never touched.
Nothing here ever adds a CODEX card we do not already have (#337).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

# the memory rules (which rows a move carries / holds) live in `codex_sync_memory` (review 12:
# the planner neared the size budget)
from . import card_guard, codex_cards, dl_snapshot, snapshot
from .codex_sync_memory import (
    CHECK_TAUGHT,
    SYNC_ACTOR,
    Split,
    held_clause,
    memory_split,
    taught_clause,
)

STREDISKO = codex_cards.PICK_STREDISKO
_NEVER = datetime.min.replace(tzinfo=UTC)
REASON_JOIN = " Tiež: "


@dataclass(frozen=True)
class Scope:
    name: str
    label: str
    table: str
    memory: tuple[str, ...]
    max_code: int | None


def _scope(name: str, label: str, memory: tuple[str, ...]) -> Scope:
    # the override table + the longest code the scope's EDI carries: ONE definition
    # (`card_guard`, the #477 pick scope — owned by the orders-gate lane, read only here)
    spec = card_guard._spec(name)
    return Scope(name, label, spec["table"], memory, spec["max_code"])


SCOPES = (
    _scope("orders", "objednávky", ("item_memory", "global_item_memory")),
    _scope("dl", "sklad", ("dl_item_memory",)),
)
BY_NAME = {s.name: s for s in SCOPES}


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

    def carriers(self, code: str) -> set[str]:
        return {r.card for r in self.by_code.get(code, [])}

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
            return _best_row(rows).name
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
        mine = self.first_seen.get((card, code), _NEVER)
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
        return self.prev_as_of is not None and (seen or _NEVER) < self.prev_as_of

    def gone_twice(self, card: str) -> bool:
        """`card` missing from stredisko 1 in this AND the previous synced CODEX snapshot."""
        return card not in self.by_card and self.absent_before(card)


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


@dataclass(frozen=True)
class Known:
    """What `_ScopePlanner._known` decided about one number of ours."""
    card: str | None           # the CODEX card it is, None = unknown
    picked: bool               # decided by a human #477 pick newer than the binding
    old: Binding | None        # the stored binding (may be retired / superseded)


def _named(name: str, rows: list[Row]) -> bool:
    """Our card name is one of these CODEX rows' names, cosmetics aside (#467 `name_key`)."""
    ours = codex_cards.name_key(name)
    return any(codex_cards.name_key(r.name) == ours for r in rows)


def _best_row(rows: list[Row]) -> Row:
    """The central row of a card's rows for a code (active first, the #477 `_name_order`)."""
    active = [r for r in rows if not r.inactive] or rows
    return min(active, key=lambda r: codex_cards._name_order(r.sklad == 1, r.changed_at, r.name))


def update_history(conn, as_of: datetime) -> None:
    """Record every (stredisko, card, code) of the current list: a new one gets
    first_seen = last_seen = `as_of` (the CODEX data age), a known one advances last_seen — and
    takes the list's name only then: an OLDER re-sent list (recorded since review 12) never sets
    a newer name back (review 13 🟡: `same_product` then judged a recreated card another
    product and cleared its data)."""
    conn.execute(
        """INSERT INTO codex_card_history (stredisko, card_code, code, name, first_seen,
                                           last_seen)
           SELECT stredisko, card_code, code, max(name), %s, %s FROM codex_stock_cards
            GROUP BY stredisko, card_code, code
           ON CONFLICT (stredisko, card_code, code) DO UPDATE
              SET name = CASE WHEN EXCLUDED.last_seen >= codex_card_history.last_seen
                              THEN EXCLUDED.name ELSE codex_card_history.name END,
                  last_seen = GREATEST(codex_card_history.last_seen, EXCLUDED.last_seen)""",
        (as_of, as_of))


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


def load(conn, cards: codex_cards.CodexCards, as_of: datetime) -> Codex:
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
        latest[code] = max(latest.get(code, _NEVER), last)
        card_seen[card] = max(card_seen.get(card, _NEVER), last)
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
                 carried, {s.name: card_guard.pickable(conn, s.name) for s in SCOPES},
                 _prev_as_of(conn, as_of), bindings, _events(conn))


def _fields(card: dict) -> dict:
    return {k: v for k, v in card.items() if k != "overridden"}


def _ours(cards, max_code: int | None) -> dict[str, dict]:
    """Our cards by CODEX code over an in-memory list (the plan simulates the catalog) — the
    `card_guard._ours` rule: only numbers the scope's EDI can carry, the canonical number
    winning over a legacy twin (`codex_cards.index_by_code`, the ONE normalizer)."""
    return codex_cards.index_by_code(
        [c for c in cards if max_code is None or len(str(c.get("gtin") or "")) <= max_code])


def _groups(catalog: list[dict], max_code: int | None) -> dict[str, list[dict]]:
    """Our cards per CODEX code, the card `_ours` picks FIRST — a legacy „0"+code twin never
    supplies the data a renumber carries over."""
    groups: dict[str, list[dict]] = {}
    for c in catalog:
        code = codex_cards.normalize_code(c.get("gtin"))
        if code:
            groups.setdefault(code, []).append(c)
    for code, group in groups.items():
        primary = _ours(group, max_code).get(code) or group[0]
        group.sort(key=lambda c: c is not primary)
    return groups


# the curated fields a merge carries over onto a target that lacks them
_FILL_FIELDS = {"orders": ("alias",), "dl": ("doplnok", "mass", "sklad", "cena")}


def _fill(scope: Scope, target: dict, ours: dict) -> dict:
    """Our card's curated values for the fields the merge target has blank — never
    overwriting a value the target already has."""
    out = {}
    for k in _FILL_FIELDS[scope.name]:
        mine, theirs = ours.get(k), target.get(k)
        if (theirs is None or str(theirs).strip() == "") and mine not in (None, ""):
            out[k] = mine
    return out


class _ScopePlanner:
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

    def run(self) -> None:
        groups = _groups(self.catalog, self.scope.max_code)
        for code, group in groups.items():
            self._code(code, group)
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
        if card not in cx.by_card and not cx.gone_twice(card):
            return False
        if ours in cx.product(card, code):
            return False
        if any(_named(name, cx.rows(d, code)) for d in cx.carriers(code) - {card}):
            return True
        return (old is not None and not old.active and old.retired_name is not None
                and ours != codex_cards.name_key(old.retired_name))

    def _contest_reason(self, gtin: str, name: str, code: str, known: Known,
                        numbers: list[str]) -> str:
        cx, card, old = self.cx, str(known.card), known.old
        retired = old is not None and not old.active
        was = ((old.retired_name if retired and old is not None else None)
               or cx.name_of(card, code))
        now = sorted(cx.carriers(code) - {card})
        named = [d for d in now if _named(name, cx.rows(d, code))]
        if retired and not named:
            parts = [f"číslo {gtin} bola karta CODEX {card} („{was}“) — synchronizácia ju "
                     f"zmazala, niekto ju vrátil z Koša a premenoval na „{name}“."]
        else:
            # our name = another carrier's: a human rename OR a CODEX-side rename of our card
            # beside a same-named duplicate — nothing tells which, so say only what we see
            # (review 10 🔵: „niekto ju premenoval" was false after a CODEX rename)
            parts = [f"číslo {gtin} („{name}“) je karta CODEX {card} („{was}“), no rovnako ako "
                     f"naša karta sa volá karta CODEX {', '.join(named)}, ktorá kód {code} teraz "
                     f"nesie — nevieme, ktorá je naša."]
        if card in cx.by_card:
            act = (f"vráť karte názov „{was}“" if retired and not named
                   else f"premenuj ju na „{was}“")
            parts.append(f"Ak je to „{was}“, {act} — ďalší zoznam kariet ju zaradí ku karte "
                         f"CODEX {card}.")
        if now:
            advice = self._pick_advice(code, now, card, numbers)
            parts.append(advice[:1].upper() + advice[1:] + ".")
        elif not cx.carriers(code):
            # only when NO card carries it — our own card may (a round trip — review 16)
            parts.append(f"Kód {code} v stredisku 1 CODEXu teraz nenesie žiadna karta — ak kartu "
                         f"nepotrebujete, zmažte ju (Kôš).")
        parts.append(f"Naučené priradenia k číslu {gtin}: {CHECK_TAUGHT}.")
        return " ".join(parts)

    def _pick_advice(self, code: str, candidates: list[str], current: str | None,
                     numbers: list[str]) -> str:
        """What deleting our card and picking `code` at a question („Vybrať kartu z CODEXu")
        would REALLY do — derived from the rules the sync applies, never asserted (review 15):
        the picker offers ONE card per code (`card_guard.pickable`, under its own name), it
        SELECTS a live number of ours that normalizes to the code (so every such number must
        go to the Kôš first — review 16), and the pick resets the curated data only for another
        product (`Codex.same_product`, as in `_identify`)."""
        entry = self.cx.pickable[self.scope.name].get(code)
        if entry is None:
            return (f"výber kariet kód {code} neponúka — kartu {', '.join(candidates)} zaradí len "
                    f"oprava v CODEXe")
        offered = str(entry["card_code"])
        parts = []
        if offered in candidates:
            # a pick of a number bound to nothing resets nothing (`_identify`: no old binding)
            same = current is None or self.cx.same_product(current, offered, code)
            data = ("jej údaje ostanú (ten istý výrobok)" if same
                    else "staré údaje (alias / doplnok / hmotnosť) sa vyčistia")
            ours = ("našu kartu" if len(numbers) <= 1
                    else f"naše karty {', '.join(numbers)}")
            label = str(entry.get("name") or "") or self.cx.name_of(offered, code)
            parts.append(f"ak je to výrobok karty CODEX {offered} („{label}“), zmaž {ours} (Kôš) "
                         f"a pri otázke ju vyber cez „Vybrať kartu z CODEXu“ — priradí sa k nej, "
                         f"{data}")
        others = [c for c in candidates if c != offered]
        if others:
            parts.append(f"kartu CODEX {', '.join(others)} výber priradiť nevie (pre kód {code} "
                         f"ponúka len kartu {offered}) — tú zaradí len oprava v CODEXe")
        return "; ".join(parts)

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
        """The CODEX card our card with `code` IS (`_known`); a card not known yet is bound now
        to the code's only stredisko-1 carrier when no other card carried it in the previous
        snapshot (one push is no proof — an export glitch), the one our name picks among
        several, or the history's last carrier when the code is gone already. A card whose
        CODEX card left for good while exactly one card carries our code under OUR name is
        re-bound to it (recreated in CODEX). None = cannot tell yet (a human decides)."""
        cx, name, gtin = self.cx, item["name"], item["gtin"]
        known = self._known(gtin)
        if known.card is not None:
            if known.picked:
                old = known.old
                replaces = old is not None and old.card != known.card
                self._seed(item, known.card, replaces=replaces)
                if old is not None and replaces and not cx.same_product(old.card, known.card,
                                                                        code):
                    # our number used to be ANOTHER product: the pick restored its old Kôš
                    # card „as it was" — its curated data belongs to that product (review 4).
                    # Products compared, never OUR name: a human rename before the delete +
                    # pick would hide it (review 7)
                    self._reset_from(item, old.card, cx.events[(self.scope.table, gtin)].at)
                return known.card
            others = cx.carriers(code) - {known.card}
            if (cx.gone_twice(known.card) and len(others) == 1
                    and _named(name, cx.rows(next(iter(others)), code))):
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
                self.plan.add_review(item, self._contest_reason(gtin, name, code, known,
                                                                    item["gtins"]))
                return None
            # a group member that joined later (a legacy twin back from the Kôš) is bound to
            # the group's card too (review 10 🔵: left alone it was identified from the list)
            unbound = [g for g in item["gtins"] if (self.scope.name, g) not in cx.bindings]
            if unbound:
                self._seed(dict(item, gtins=unbound), known.card)
            return known.card
        carriers = cx.carriers(code)
        if len(carriers) > 1:
            named = [c for c in carriers if _named(name, cx.rows(c, code))]
            if len(named) != 1:
                self.plan.add_review(item, (
                    f"kód {code} nesie v CODEXe viac kariet ({', '.join(sorted(carriers))}) "
                    f"— ktorá je naša? Premenuj našu kartu (Produkty) na jej názov v CODEXe, "
                    f"pri ďalšom zozname kariet sa priradí; ak majú v CODEXe rovnaký názov: "
                    f"{self._pick_advice(code, sorted(carriers), None, item['gtins'])}."))
                return None
            card = named[0]
        elif carriers:
            card = next(iter(carriers))
            if any(not cx.absent_before(c, code) for c in cx.carried.get(code, {}) if c != card):
                return None                    # another card carried it a snapshot ago
        else:
            last = cx.owners.get(code, [])
            if len(last) > 1:
                self.plan.add_review(item, (
                    f"kód {code} už v stredisku 1 CODEXu nie je a naposledy ho niesli karty "
                    f"{', '.join(sorted(last))} — nevieme, ktorá je naša; ak kartu už "
                    f"nepotrebujete, zmažte ju (Kôš)."))
            if len(last) != 1:
                return None                    # no stredisko-1 history, or ambiguous
            card = last[0]
        self._seed(item, card)
        return card

    def _code(self, code: str, group: list[dict]) -> None:
        cx = self.cx
        gtins = [str(c["gtin"]) for c in group]
        item = {"scope": self.scope.name, "gtin": gtins[0], "gtins": gtins, "code": code,
                "name": group[0].get("name", ""),
                # each number's name as the plan found it — what a retire stores (review 8)
                "names": {str(c["gtin"]): str(c.get("name") or "") for c in group}}
        card = self._identify(code, item)
        if card is None:
            return
        item["codex_card"] = card
        if item.pop("reset_from", None) is not None:
            # every number of the group — a legacy twin keeps no old-product data either
            for g in gtins:
                self._reset(dict(item, gtin=g), self.live[g])
            # a renumber / fill later in this plan carries the RESET data, never the old
            # product's (review 5 🟡)
            group = [self.live[g] for g in gtins]
        if cx.rows(card, code):              # our CODEX card still carries our code
            for g in gtins:
                self.identity[g] = (card, code)
            return
        if card not in cx.by_card:           # our CODEX card left stredisko 1
            if not cx.gone_twice(card):
                return                       # one missing snapshot is no proof (a glitch)
            if not cx.cards.has(code):
                self.plan.removals.append(item)
                self._vacate(group)
            else:
                self.plan.add_review(item, self._gone_reason(card, code, gtins))
            return
        succ, why = self._successor(card, code)
        if succ is None:
            self.plan.add_review(item, why)
            return
        self._renumber(item, group, card, succ)

    def _gone_reason(self, card: str, code: str, numbers: list[str]) -> str:
        """Our CODEX card left stredisko 1 for good, the code lives on elsewhere — the way out
        per case, never a promise the sync cannot keep (review 14 🔵)."""
        now = sorted(self.cx.carriers(code))
        head = f"karta CODEX {card} už v stredisku 1 nie je"
        if not now:
            return (f"{head} a kód {code} je v CODEXe už len na inom stredisku — výber kariet ho "
                    f"neponúka, synchronizácia s touto kartou nič neurobí; ak kartu nepotrebujete, "
                    f"zmažte ju (Kôš).")
        if len(now) > 1:
            return (f"{head} a kód {code} teraz nesie viac kariet ({', '.join(now)}): "
                    f"{self._pick_advice(code, now, card, numbers)}; ak to nie je žiadna z nich, "
                    f"kartu zmaž (Kôš).")
        # the rename rebind resets the data exactly when `_identify` says so (review 15)
        data = ("jej údaje ostanú (ten istý výrobok v CODEXe)"
                if self.cx.same_product(card, now[0], code)
                else "má v CODEXe iný názov, preto sa jej alias / doplnok / hmotnosť vyčistia — "
                     "skontroluj ich")
        return (f"{head} a kód {code} teraz nesie karta {now[0]} — ak je to ten istý výrobok, "
                f"premenuj našu kartu (Produkty) na jej názov v CODEXe, pri ďalšom zozname kariet "
                f"sa priradí ({data}); ak nie, kartu zmaž (Kôš).")

    def _successor(self, card: str, code: str) -> tuple[str | None, str]:
        """The ONE new code our CODEX `card` carries for this catalog → (Y, ""), or
        (None, a Slovak reason a human reads)."""
        cx, scope = self.cx, self.scope
        codes = {r.code for r in cx.by_card.get(card, [])} - {code}
        fit = codes & cx.pickable[scope.name].keys()
        if not fit:
            return None, (f"karta CODEX {card} nesie teraz kód {', '.join(sorted(codes))}, ale "
                          f"výber kariet ho pre katalóg {scope.label} neponúka")
        if len(fit) > 1:
            newest = max(cx.first_seen.get((card, c), _NEVER) for c in fit)
            fit = {c for c in fit if cx.first_seen.get((card, c), _NEVER) == newest}
        if len(fit) != 1:
            return None, (f"karta CODEX {card} nesie viac kódov naraz ("
                          f"{', '.join(sorted(fit))}) — nie je jasné, ktorý je nový")
        succ = fit.pop()
        others = cx.carriers(succ) - {card}
        if others:
            return None, (f"nový kód {succ} karty CODEX {card} nesie aj karta "
                          f"{', '.join(sorted(others))} — prečíslovanie treba overiť ručne")
        return succ, ""

    def _renumber(self, item: dict, group: list[dict], card: str, succ: str) -> None:
        target = _ours(self.live.values(), self.scope.max_code).get(succ)
        binned = _ours(self.binned, self.scope.max_code).get(succ)
        hit = target if target is not None else binned
        hit_known = self._known(str(hit["gtin"])) if hit is not None else None
        other = hit_known.card if hit_known is not None else None
        if other not in (None, card):
            # our card with the new code is ANOTHER CODEX card (e.g. a #477 pick of the
            # product that held the code before) — never a silent merge of two products
            self.plan.add_review(item, (
                f"náš kód {succ} je karta CODEX {other}, nie {card} — prečíslovanie čaká. Ak "
                f"naše číslo {succ} je teraz výrobok karty CODEX {card} („"
                f"{self.cx.name_of(card, succ)}“), "
                + ("zmaž ho (Kôš) a potom " if target is not None else "")
                + "vyber ho pri otázke cez „Vybrať kartu z CODEXu“ — priradí sa ku karte "
                f"CODEX {card} a prečíslovanie prebehne."))
            return
        hit_name = str((hit or {}).get("name") or "")
        if hit_known is not None and self._contested(hit_known, hit_name, succ):
            # our card with the new code — live, or its Kôš copy (review 8) — was renamed by a
            # human after the sync retired it: never merged into / restored over / renamed, a
            # human settles it first (review 7)
            was = ((hit_known.old.retired_name if hit_known.old else None)
                   or self.cx.name_of(card, succ))
            where = "" if target is not None else "v Koši "
            back = "" if target is not None else "vráť ju z Koša a "
            self.plan.add_review(item, (
                f"nový kód {succ} karty CODEX {card} je u nás karta {where}„{hit_name}“ — "
                f"synchronizácia ju zmazala ako „{was}“ a niekto ju potom premenoval, "
                f"prečíslovanie počká: ak je to stále „{was}“, {back}daj jej tento názov (potom "
                f"sa prečísluje); jej naučené priradenia: {CHECK_TAUGHT}"))
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
            "card": _fields(group[0])})
        if taken and split.taught:
            self.plan.add_review(item, self._held_reason(item["gtin"], code, card, to, split,
                                                         taken[1]))
        if hit is not None:
            self._adopted_review(item, str(hit["gtin"]), card, succ)
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

    def _held_reason(self, gtin: str, code: str, card: str, to: str, split: Split,
                     takers: list[str]) -> str:
        """Only TAUGHT held rows are the warehouse's to check; held delivery history just
        stays (review 11) — and says where each kind of taught row is fixed (review 12)."""
        name = self.cx.name_of(card, code)
        at = split.at(gtin)                    # the numbers they really sit on (review 15)
        history = (f" ({split.shipped} záznamov o dodávkach z toho obdobia tiež ostáva pod "
                   f"{at} ako história)" if split.shipped else "")
        return (f"{split.taught} naučených priradení k číslu {at} vzniklo (alebo ich niekto "
                f"zmenil) potom, čo sa kód {code} v CODEXe objavil pri karte CODEX "
                f"{', '.join(takers)} — nevieme, či patria „{name}“ (karta CODEX {card}), alebo "
                f"jej: ostávajú pod číslom {at}{history}; {CHECK_TAUGHT} a tie, čo patria "
                f"„{name}“, preraď na {to}.")

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
        name = self.cx.name_of(card, succ)
        self.plan.add_review(item, (
            f"{split.taught} naučených priradení k číslu {target} vzniklo, kým kód {succ} v "
            f"CODEXe mala karta CODEX {', '.join(foreign[1])} — prečíslovanie ich teraz pridá ku "
            f"karte CODEX {card} („{name}“); {CHECK_TAUGHT} a tie, čo patria tej druhej karte, "
            f"zmaž alebo preraď."))

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
        (orders alias; DL doplnok / mass / cena, and the sklad of the picked CODEX card) go
        back to a fresh pick's — audited, restorable."""
        if self.scope.name == "orders":
            new: dict = {"alias": ""}
        else:
            entry = self.cx.pickable[self.scope.name].get(item["code"]) or {}
            new = {"doplnok": "", "mass": None, "cena": None,
                   "sklad": str(entry.get("sklad") or card.get("sklad") or "")}
        changed = {k: v for k, v in new.items()
                   if (card.get(k) or None) != (v if v != "" else None)}
        if not changed:
            return
        self.plan.resets.append(dict(item, before={k: card.get(k) for k in changed},
                                     after=changed, card=_fields(card)))
        self.live[item["gtin"]] = dict(card, **changed)

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
        ours = _ours(self.live.values(), self.scope.max_code)
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
                self._hold_note(item, split.at(code), split.shipped,
                                f"kód {code} mala medzitým v CODEXe iná karta")
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
        if moved and kept:
            where = (f"číslo {gtin} (prečíslované na {moved}; {kept} z tých priradení ostalo pod "
                     f"{gtin} — vznikli, keď kód mala iná karta)")
        else:
            where = f"číslo {gtin}" + (f" (teraz prečíslované na {moved})" if moved else "")
        old_name = self.cx.name_of(old, code)
        name = next((str(c.get("name") or "") for c in self.catalog if str(c["gtin"]) == gtin),
                    "")
        item = {"scope": self.scope.name, "gtin": gtin, "code": code, "name": name}
        if not taught:
            # delivery history only — nothing for a human to fix in Naučené (review 12 🔵)
            if shipped:
                self._hold_note(item, moved or gtin, shipped,
                                f"číslo {gtin} bolo predtým karta CODEX {old} („{old_name}“)",
                                moved=moved is not None)
            return
        history = (f" ({shipped} záznamov o dodávkach z toho času tiež ostáva ako história)"
                   if shipped else "")
        older = "je starších ako výber z CODEXu a " if since is not None else ""
        # where they can go: the old product's live number, or nowhere when it left CODEX
        # (review 10 🔵: never „preraď" to a card gone from CODEX)
        home = next((g for g, (c, _code) in self.identity.items() if c == old and g in self.live),
                    None)
        # the codes under which the picker offers the old card (review 16: never advise a pick
        # the picker cannot do — it offers one card per code, for this catalog only)
        offered = sorted(c for c, e in self.cx.pickable[self.scope.name].items()
                         if str(e["card_code"]) == old)
        if home is not None:
            fix = f"preraď ich na {home}"
        elif offered:
            fix = (f"„{old_name}“ u nás karta nie je — ak treba, pri otázke vyber cez „Vybrať "
                   f"kartu z CODEXu“ kód {offered[0]} (karta CODEX {old}) a preraď ich naň")
        elif old in self.cx.by_card:
            fix = (f"„{old_name}“ u nás karta nie je a výber kariet ju pre katalóg "
                   f"{self.scope.label} neponúka — tie priradenia zmaž, alebo pomôže oprava v "
                   f"CODEXe")
        else:
            fix = f"„{old_name}“ už v CODEXe nie je — tie priradenia zmaž"
        self.plan.add_review(item, (
            f"{where} bolo karta CODEX {old} („{old_name}“) a {taught} naučených priradení k "
            f"nemu {older}môže patriť „{old_name}“{history}: {CHECK_TAUGHT}; ak patria "
            f"„{old_name}“, {fix}"))

    def _hold_note(self, item: dict, at: str, shipped: int, why: str, *,
                   moved: bool = False) -> None:
        """Delivery history (shipped rows) with nothing for a human to fix — a report + ops
        note, never silent (review 12 🔵); `at` = the number(s) the rows sit on after this plan,
        `moved` = a same-push renumber carried them there (reviews 13-14 🔵)."""
        self.plan.holds.append(dict(item, held={"taught": 0, "shipped": shipped}, at=at,
                                    moved=moved, why=why))

    def _renames(self) -> None:
        for gtin, card in self.live.items():
            if gtin not in self.identity:
                continue
            codex_card, code = self.identity[gtin]
            rows = self.cx.rows(codex_card, code)
            name = card.get("name") or ""
            if not rows or _named(name, rows):
                continue
            new = _best_row(rows).name.strip()
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
