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

A code with no stredisko-1 carrier and no history (the #467 "missing" cards) is never touched.
Nothing here ever adds a CODEX card we do not already have (#337).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import card_guard, codex_cards, dl_snapshot, snapshot

STREDISKO = codex_cards.PICK_STREDISKO
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

    def code_changes(self) -> int:
        """Distinct CODEX codes renumbered or removed (a card in both catalogs = one)."""
        return len({i["code"] for i in self.renumbers + self.removals})

    def counts(self) -> dict:
        return {"renamed": len(self.renames),
                "renumbered": sum(1 for r in self.renumbers if r["mode"] != "memory"),
                "memory_renumbered": sum(1 for r in self.renumbers if r["mode"] == "memory"),
                "removed": len(self.removals), "review": len(self.review),
                "bound": len(self.seeds)}

    def add_review(self, item: dict, reason: str) -> None:
        """One review entry per card (the first reason wins)."""
        if not any(r["scope"] == item["scope"] and r["gtin"] == item["gtin"]
                   for r in self.review):
            self.review.append(dict(item, reason=reason))


@dataclass
class Codex:
    """The stredisko-1 CODEX list, its history and our card bindings, as the plan reads them."""
    cards: codex_cards.CodexCards
    by_card: dict[str, list[Row]]
    by_code: dict[str, list[Row]]
    first_seen: dict[tuple[str, str], datetime]
    card_seen: dict[str, datetime]              # card -> last snapshot any of its rows was in
    owners: dict[str, list[str]]                # code -> the cards that carried it LAST
    pickable: dict[str, set[str]]               # scope -> codes the #477 pick offers
    prev_as_of: datetime | None                 # the previous distinct CODEX snapshot
    bindings: dict[tuple[str, str], tuple[str, bool]]   # (scope, gtin) -> (card, active)

    def carriers(self, code: str) -> set[str]:
        return {r.card for r in self.by_code.get(code, [])}

    def rows(self, card: str, code: str) -> list[Row]:
        return [r for r in self.by_card.get(card, []) if r.code == code]

    def gone_twice(self, card: str) -> bool:
        """`card` missing from stredisko 1 in this AND the previous distinct CODEX snapshot."""
        return (self.prev_as_of is not None and card not in self.by_card
                and self.card_seen.get(card, _NEVER) < self.prev_as_of)


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
    row = conn.execute(
        "SELECT max(a) FROM (SELECT LEAST(COALESCE(source_as_of, synced_at), synced_at) AS a "
        "FROM codex_card_syncs) s WHERE a < %s", (as_of,)).fetchone()
    return row[0] if row else None


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
    hist = conn.execute("SELECT card_code, code, first_seen, last_seen FROM codex_card_history "
                        "WHERE stredisko = %s", (STREDISKO,)).fetchall()
    latest: dict[str, datetime] = {}
    card_seen: dict[str, datetime] = {}
    first_seen: dict[tuple[str, str], datetime] = {}
    for card, code, first, last in hist:
        latest[code] = max(latest.get(code, _NEVER), last)
        card_seen[card] = max(card_seen.get(card, _NEVER), last)
        first_seen[(card, code)] = first
    owners: dict[str, list[str]] = {}
    for card, code, _first, last in hist:
        if last == latest[code]:
            owners.setdefault(code, []).append(card)
    bindings = {(s, g): (c, bool(a)) for s, g, c, a in conn.execute(
        "SELECT scope, gtin, card_code, active FROM codex_card_bindings").fetchall()}
    return Codex(cards, by_card, by_code, first_seen, card_seen, owners,
                 {s.name: set(card_guard.pickable(conn, s.name)) for s in SCOPES},
                 _prev_as_of(conn, as_of), bindings)


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

    def run(self) -> None:
        groups = _groups(self.catalog, self.scope.max_code)
        for code, group in groups.items():
            self._code(code, group)
        self._memory(set(groups))
        self._renames()

    def _bound(self, gtin: str) -> str | None:
        b = self.cx.bindings.get((self.scope.name, gtin))
        return b[0] if b else None

    def _seed(self, gtin: str, card: str) -> None:
        self.plan.seeds.append({"scope": self.scope.name, "gtin": gtin, "card": card})

    def _identify(self, code: str, item: dict) -> str | None:
        """The CODEX card our card with `code` IS — its binding, else (seeded now) the code's
        only stredisko-1 carrier / the one our name picks / the history's last carrier when
        the code is gone already. None = cannot tell (a human decides when it matters)."""
        cx, name = self.cx, item["name"]
        card = self._bound(item["gtin"])
        if card is not None:
            carriers = cx.carriers(code) - {card}
            if (cx.gone_twice(card) and len(carriers) == 1
                    and _named(name, cx.rows(next(iter(carriers)), code))):
                card = carriers.pop()          # our card was recreated in CODEX: same name
                self._seed(item["gtin"], card)
            return card
        carriers = cx.carriers(code)
        if len(carriers) > 1:
            named = [c for c in carriers if _named(name, cx.rows(c, code))]
            if len(named) != 1:
                self.plan.add_review(item, (
                    f"kód {code} nesie v CODEXe viac kariet ({', '.join(sorted(carriers))}) "
                    f"— ktorá je naša? (premenuj našu kartu na jej názov v CODEXe)"))
                return None
            carriers = set(named)
        elif not carriers:
            last = cx.owners.get(code, [])
            if len(last) != 1:
                return None               # never seen on stredisko 1 while we watched
            carriers = set(last)
        card = carriers.pop()
        self._seed(item["gtin"], card)
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
        if cx.rows(card, code):              # our CODEX card still carries our code
            for g in gtins:
                self.identity[g] = (card, code)
            return
        if card not in cx.by_card:           # our CODEX card left stredisko 1
            if not cx.gone_twice(card):
                return                       # one missing snapshot is no proof (a glitch)
            if not cx.cards.has(code):
                self.plan.removals.append(item)
                for g in gtins:
                    self.live.pop(g, None)
            else:
                self.plan.add_review(item, (
                    f"karta CODEX {card} už v stredisku 1 nie je a kód {code} teraz nesie iná "
                    f"karta ({', '.join(sorted(cx.carriers(code))) or 'iné stredisko'}) — je "
                    f"to ten istý výrobok?"))
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
        fit = codes & cx.pickable[scope.name]
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
        other = self._bound(str(hit["gtin"])) if hit is not None else None
        if other not in (None, card):
            self.plan.add_review(item, (
                f"náš kód {succ} patrí karte CODEX {other}, nie {card} — prečíslovanie treba "
                f"overiť ručne"))
            return
        mode = "merge" if target is not None else "restore" if binned is not None else "create"
        to = str(hit["gtin"]) if hit is not None else succ
        old = sorted(set(item["gtins"]) | {item["code"]})
        self.plan.renumbers.append(dict(item, **{
            "from": item["gtin"], "to": to, "mode": mode, "old_gtins": old,
            "memory": _memory_count(self.conn, self.scope, old), "card": _fields(group[0])}))
        for g in item["gtins"]:
            self.live.pop(g, None)
        if mode != "merge":
            self.live[to] = dict(_fields(group[0]), gtin=to)
            self.binned = [b for b in self.binned if str(b["gtin"]) != to]
        self.identity[to] = (card, succ)

    def _memory(self, catalog_codes: set[str]) -> None:
        """Mapping rows of a number the sync RETIRED (an inactive binding — written after the
        renumber, from a frozen question or a held order) follow its card's live number."""
        retired = {g: c for (s, g), (c, active) in self.cx.bindings.items()
                   if s == self.scope.name and not active}
        live_of: dict[str, list[str]] = {}
        for gtin, (card, _code) in self.identity.items():
            if gtin in self.live:
                live_of.setdefault(card, []).append(gtin)
        per: dict[tuple[str, str], set[str]] = {}
        for table in self.scope.memory:
            for (gtin,) in self.conn.execute(
                    f"SELECT DISTINCT gtin FROM {table} WHERE deleted_at IS NULL").fetchall():
                code = codex_cards.normalize_code(gtin)
                owner = retired.get(str(gtin)) or (retired.get(code) if code else None)
                if (code and owner and code not in catalog_codes
                        and len(live_of.get(owner, [])) == 1):
                    per.setdefault((code, owner), set()).add(str(gtin))
        for (code, card), gtins in sorted(per.items()):
            old = sorted(gtins | {code})
            self.plan.renumbers.append({
                "scope": self.scope.name, "gtin": code, "gtins": [], "code": code, "name": "",
                "codex_card": card, "from": code, "to": live_of[card][0], "mode": "memory",
                "old_gtins": old, "memory": _memory_count(self.conn, self.scope, old),
                "card": {}})

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
