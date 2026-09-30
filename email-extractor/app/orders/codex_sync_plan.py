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

from . import card_guard, codex_cards, dl_snapshot, snapshot

STREDISKO = codex_cards.PICK_STREDISKO
# the audit actor of every sync write (`codex_sync.ACTOR`) — its own writes are never a human
# (re)entry of a card
SYNC_ACTOR = "codex-sync"
_NEVER = datetime.min.replace(tzinfo=UTC)


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
# memory table -> its UNIQUE mapping columns besides `gtin` (a rewrite X→Y must not collide);
# trusted literals — the only table/column names ever interpolated into SQL here and in
# `codex_sync`
MEMORY_KEYS: dict[str, tuple[str, ...]] = {
    "item_memory": ("customer_ean", "item_key", "delivered_on"),
    "global_item_memory": (),
    "dl_item_memory": ("supplier_ean", "item_key", "delivered_on", "cnt"),
}


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
        """One review entry per card; a further reason is appended to it (never lost)."""
        for r in self.review:
            if r["scope"] == item["scope"] and r["gtin"] == item["gtin"]:
                if reason not in r["reason"]:
                    r["reason"] = f"{r['reason']} Tiež: {reason}"
                return
        self.review.append(dict(item, reason=reason))


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
        return (self.names.get((card, code))
                or next((r.name for r in self.by_card.get(card, [])), "") or "?")

    def names_card(self, card: str, code: str, name: str) -> bool:
        """Our card name `name` is CODEX `card`'s product (cosmetics aside)."""
        return codex_cards.name_key(name) in self.product(card, code)

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


def update_history(conn, as_of: datetime) -> None:
    """Record every (stredisko, card, code) of the current list: a new one gets
    first_seen = last_seen = `as_of` (the CODEX data age), a known one advances last_seen."""
    conn.execute(
        """INSERT INTO codex_card_history (stredisko, card_code, code, name, first_seen,
                                           last_seen)
           SELECT stredisko, card_code, code, max(name), %s, %s FROM codex_stock_cards
            GROUP BY stredisko, card_code, code
           ON CONFLICT (stredisko, card_code, code) DO UPDATE
              SET name = EXCLUDED.name,
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
    bindings = {(s, g): Binding(c, bool(a), at) for s, g, c, a, at in conn.execute(
        "SELECT scope, gtin, card_code, active, bound_at FROM codex_card_bindings").fetchall()}
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


def _memory_count(conn, scope: Scope, gtins: list[str]) -> dict[str, int]:
    return {t: int(conn.execute(
        f"SELECT count(*) FROM {t} WHERE gtin = ANY(%s) AND deleted_at IS NULL",
        (gtins,)).fetchone()[0]) for t in scope.memory}


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
        # our number re-picked (#477) as ANOTHER product -> (its old CODEX card, pick time)
        self.repicked: dict[str, tuple[str, datetime]] = {}

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
        renamed it to another product since, `_disputed` hands it to a human); a legacy
        creation without a named card newer than the binding = unknown (identify it from the
        list)."""
        b = self.cx.bindings.get((self.scope.name, gtin))
        ev = self.cx.events.get((self.scope.table, gtin))
        if ev is not None and (b is None or ev.at > b.bound_at):
            return Known(ev.card, picked=ev.card is not None, old=b)
        return Known(b.card if b else None, picked=False, old=b)

    def _card_of(self, gtin: str) -> str | None:
        return self._known(gtin).card

    def _disputed(self, known: Known, name: str, code: str) -> bool:
        """A number the sync RETIRED, brought back from the Kôš by a human under a name that is
        no longer its CODEX card's product (e.g. the Produkty drift button offered the name of
        the card that carries the code NOW, as if cosmetic): still the old product, or now the
        new one? Nothing in our data tells — a human decides (review 7 🟡: re-binding it by the
        name kept the old product's data; merging it moved the new product's wordings)."""
        return (not known.picked and known.old is not None and not known.old.active
                and known.card is not None and not self.cx.names_card(known.card, code, name))

    def _dispute_reason(self, gtin: str, name: str, code: str, old: str) -> str:
        old_name = self.cx.name_of(old, code)
        now = sorted(self.cx.carriers(code))
        holder = (f"kód {code} teraz nesie karta CODEX {', '.join(now)}" if now
                  else f"kód {code} teraz v stredisku 1 CODEXu nenesie žiadna karta")
        return (f"číslo {gtin} bola karta CODEX {old} („{old_name}“) — synchronizácia ju "
                f"zmazala, niekto ju vrátil z Koša a premenoval na „{name}“ ({holder}). Ak je to "
                f"stále „{old_name}“, vráť karte tento názov (ďalší zoznam kariet ju zlúči s "
                f"kartou {old}). Ak je to iný výrobok, zmaž ju (Kôš) a pri otázke ju vyber cez "
                f"„Vybrať kartu z CODEXu“ — staré údaje (alias / doplnok / hmotnosť) sa vtedy "
                f"vyčistia; naučené priradenia k číslu {gtin} skontroluj v Naučené.")

    def _seed(self, gtin: str, card: str, *, replaces: bool = False) -> None:
        """A binding found this run. `replaces` = it overwrites an existing binding with another
        card: stored only by an APPLIED run, so a pick seen during a dry-run / blocked run still
        triggers its reset when the apply finally comes (review 5 🟡)."""
        self.plan.seeds.append({"scope": self.scope.name, "gtin": gtin, "card": card,
                                "replaces": replaces})

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
                self._seed(gtin, known.card, replaces=replaces)
                if old is not None and replaces and not cx.same_product(old.card, known.card,
                                                                        code):
                    # our number used to be ANOTHER product: the pick restored its old Kôš
                    # card „as it was" — its curated data belongs to that product (review 4).
                    # Products compared, never OUR name: a human rename before the delete +
                    # pick would hide it (review 7)
                    item["reset_from"] = old.card
                    self.repicked[gtin] = (old.card, self.cx.events[(self.scope.table, gtin)].at)
                return known.card
            if self._disputed(known, name, code):
                self.plan.add_review(item, self._dispute_reason(gtin, name, code, known.card))
                return None
            others = cx.carriers(code) - {known.card}
            if (cx.gone_twice(known.card) and len(others) == 1
                    and _named(name, cx.rows(next(iter(others)), code))):
                # our card was recreated in CODEX under the same name — durable evidence (the
                # list itself), so stored in every mode (review 6 🟡: a dry-run that skipped
                # it later REMOVED the card)
                card = others.pop()
                self._seed(gtin, card)
                return card
            return known.card
        carriers = cx.carriers(code)
        if len(carriers) > 1:
            named = [c for c in carriers if _named(name, cx.rows(c, code))]
            if len(named) != 1:
                self.plan.add_review(item, (
                    f"kód {code} nesie v CODEXe viac kariet ({', '.join(sorted(carriers))}) "
                    f"— ktorá je naša? Premenuj našu kartu (Produkty) na jej názov v CODEXe, "
                    f"pri ďalšom zozname kariet sa priradí."))
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
        self._seed(gtin, card)
        return card

    def _code(self, code: str, group: list[dict]) -> None:
        cx = self.cx
        gtins = [str(c["gtin"]) for c in group]
        item = {"scope": self.scope.name, "gtin": gtins[0], "gtins": gtins, "code": code,
                "name": group[0].get("name", "")}
        card = self._identify(code, item)
        if card is None:
            return
        item["codex_card"] = card
        if item.pop("reset_from", None) is not None:
            self._reset(item, self.live[item["gtin"]])
            # a renumber / fill later in this plan carries the RESET data, never the old
            # product's (review 5 🟡)
            group = [self.live[item["gtin"]], *group[1:]]
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
                self.plan.add_review(item, (
                    f"karta CODEX {card} už v stredisku 1 nie je a kód {code} teraz nesie iná "
                    f"karta ({', '.join(sorted(cx.carriers(code))) or 'iné stredisko'}) — ak je "
                    f"to ten istý výrobok, premenuj našu kartu (Produkty) na jej názov v CODEXe, "
                    f"pri ďalšom zozname kariet sa priradí; ak nie, kartu zmaž (Kôš)."))
            return
        succ, why = self._successor(card, code)
        if succ is None:
            self.plan.add_review(item, why)
            return
        self._renumber(item, group, card, succ)

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
                f"náš kód {succ} je karta CODEX {other}, nie {card} — prečíslovanie treba "
                f"overiť ručne"))
            return
        target_name = str((target or {}).get("name") or "")
        if (target is not None and hit_known is not None
                and self._disputed(hit_known, target_name, succ)):
            # our LIVE card with the new code is one a human must settle first (review 7) —
            # never merged into / renamed over. (A Kôš card is restored with OUR card's data,
            # its own — often blank — name never counts.)
            self.plan.add_review(item, (
                f"nový kód {succ} karty CODEX {card} je u nás karta „{target_name}“, ktorú "
                f"treba najprv skontrolovať — prečíslovanie počká"))
            return
        mode = "merge" if target is not None else "restore" if binned is not None else "create"
        to = str(hit["gtin"]) if hit is not None else succ
        old = sorted(set(item["gtins"]) | {item["code"]})
        entry = dict(item, **{
            "from": item["gtin"], "to": to, "mode": mode, "old_gtins": old,
            "memory": _memory_count(self.conn, self.scope, old), "card": _fields(group[0])})
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
        renumber, from a frozen question or a held order) follow its card's live number."""
        retired = {g: b.card for (s, g), b in self.cx.bindings.items()
                   if s == self.scope.name and not b.active}
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
                key = str(gtin) if str(gtin) in retired else norm
                owner = retired.get(key) if key else None
                # the ONE identity rule decides (review 6 🟡: a newer pick of the retired number
                # makes it another product — its rows are that product's, never moved)
                if (norm and owner and norm not in catalog_codes and owner in number
                        and self._card_of(key or "") == owner):
                    per.setdefault((norm, owner), set()).add(str(gtin))
        for (code, card), gtins in sorted(per.items()):
            old = sorted(gtins | {code})
            self.plan.renumbers.append({
                "scope": self.scope.name, "gtin": code, "gtins": [], "code": code, "name": "",
                "codex_card": card, "from": code, "to": number[card], "mode": "memory",
                "old_gtins": old, "memory": _memory_count(self.conn, self.scope, old),
                "card": {}})
        for gtin, (card, picked_at) in sorted(self.repicked.items()):
            self._repicked_review(gtin, card, picked_at)

    def _repicked_review(self, gtin: str, old: str, picked_at: datetime) -> None:
        """Mapping rows of a number re-picked as another product that are OLDER than the pick
        may be the old product's (a frozen question answered with the retired number — review
        6) — or the new one's (a Naučené edit / a revive re-points a row and keeps its
        `created_at` — review 7): nothing tells, so they stay where they are and a human checks
        them. Where they are = the picked card's number after this plan (a same-push renumber
        carries them — review 7 F2)."""
        code = codex_cards.normalize_code(gtin) or gtin
        total = sum(int(self.conn.execute(
            f"SELECT count(*) FROM {t} WHERE gtin = %s AND deleted_at IS NULL "
            "AND created_at < %s", (gtin, picked_at)).fetchone()[0]) for t in self.scope.memory)
        if not total:
            return
        moved = next((r["to"] for r in self.plan.renumbers
                      if r["scope"] == self.scope.name and gtin in r["gtins"]), None)
        where = f"číslo {gtin}" + (f" (teraz prečíslované na {moved})" if moved else "")
        old_name = self.cx.name_of(old, code)
        name = next((str(c.get("name") or "") for c in self.catalog if str(c["gtin"]) == gtin),
                    "")
        self.plan.add_review(
            {"scope": self.scope.name, "gtin": gtin, "code": code, "name": name},
            f"{where} bolo pred výberom z CODEXu karta CODEX {old} "
            f"(„{old_name}“) a {total} naučených priradení k nemu je starších ako výber — môžu "
            f"patriť „{old_name}“: skontroluj ich v Naučené a ak áno, preraď ich na jeho kartu")

    def _renames(self) -> None:
        for gtin, card in self.live.items():
            if gtin not in self.identity:
                continue
            codex_card, code = self.identity[gtin]
            rows = self.cx.rows(codex_card, code)
            name = card.get("name") or ""
            if not rows or _named(name, rows):
                continue
            active = [r for r in rows if not r.inactive] or rows
            best = min(active, key=lambda r: codex_cards._name_order(r.sklad == 1, r.changed_at,
                                                                     r.name))
            new = best.name.strip()
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
