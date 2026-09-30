"""The PLAN of the CODEX card sync (#478) — read-only: what the current CODEX stock-card list
implies for the cards WE ALREADY HAVE (both catalogs) and the code-keyed memories.
`codex_sync` executes it, or only reports it in dry-run. Nothing in this module writes.

**Identity = the CODEX card on stredisko 1** (`codex_cards.PICK_STREDISKO`, the #477 pick
scope): ACSKLP is unique only WITHIN a stredisko (live 2026-09-30: card 400448 is garlic on
stredisko 1 and crisps on stredisko 4). `codex_card_history` remembers which card carried which
code and when — `codex_stock_cards` is a full replace per push and forgets it.

Per catalog, per CODEX code X our cards carry (a legacy „0"+code twin is grouped with its
canonical card, the canonical one supplies the data — `codex_cards.index_by_code`):

- X left stredisko 1, its card now carries Y → **renumber** X → Y (Y created from our card /
  X merged into a Y we have / our Y restored from the Kôš). Y must be a code the #477 pick
  offers for that catalog (`card_guard.pickable`); several → the card's newest code, else a
  human decides.
- X still on stredisko 1 but on ANOTHER card (its old card dropped it — the code was reused):
  our card follows ITS card (X → the old card's successor) unless our name already is the new
  holder's; a swap / several old cards → a human decides. Never renamed to the new holder.
- X nowhere in CODEX and its card carries no code any more → **removal** (Kôš).
- A new code Y that itself used to belong to another card → a human decides (never merged).
- **Rename** a card whose name drifted (#467 `name_key` drift, cosmetics ignored) to the
  stredisko-1 CODEX name (#477 `_name_order`); a code on several cards → a human decides.
- Memory rows of a code no catalog card holds any more (written after an earlier renumber — a
  frozen question candidate, a held order) follow the same successor when it is our card.

A code CODEX never had while we watched (no history — the #467 "missing" cards) is never
touched. Nothing here ever adds a CODEX card we do not already have (#337).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import card_guard, codex_cards, dl_snapshot, snapshot

STREDISKO = codex_cards.PICK_STREDISKO
_NEVER = datetime.min.replace(tzinfo=UTC)
_GONE = "gone"


@dataclass(frozen=True)
class Scope:
    name: str
    label: str
    table: str
    memory: tuple[str, ...]
    max_code: int | None


def _scope(name: str, label: str, memory: tuple[str, ...]) -> Scope:
    # the override table + the longest code the scope's EDI carries — ONE definition
    # (`card_guard`, the #477 pick scope), never a second copy here
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

    def code_changes(self) -> int:
        """Distinct CODEX codes renumbered or removed (a card in both catalogs = one)."""
        return len({i["code"] for i in self.renumbers + self.removals})

    def counts(self) -> dict:
        return {"renamed": len(self.renames),
                "renumbered": sum(1 for r in self.renumbers if r["mode"] != "memory"),
                "memory_renumbered": sum(1 for r in self.renumbers if r["mode"] == "memory"),
                "removed": len(self.removals), "review": len(self.review)}

    def add_review(self, item: dict, reason: str) -> None:
        """One review entry per card (the first reason wins)."""
        if not any(r["scope"] == item["scope"] and r["gtin"] == item["gtin"]
                   for r in self.review):
            self.review.append(dict(item, reason=reason))


@dataclass
class Codex:
    """The stredisko-1 CODEX list + its history, as the plan reads them."""
    cards: codex_cards.CodexCards
    by_card: dict[str, list[Row]]
    by_code: dict[str, list[Row]]
    owners: dict[str, list[str]]                # code -> the cards that carried it LAST
    carried: dict[str, set[str]]                # code -> every card that ever carried it
    first_seen: dict[tuple[str, str], datetime]
    pickable: dict[str, set[str]]               # scope -> codes the #477 pick offers

    def former(self, code: str) -> set[str]:
        """Cards that carried `code` and dropped it while another card carries it now — the
        code was REUSED for a different CODEX card. Empty when nobody carries it now."""
        now = {r.card for r in self.by_code.get(code, [])}
        return (self.carried.get(code, set()) - now) if now else set()

    def cards_of(self, code: str) -> str:
        return ", ".join(sorted({r.card for r in self.by_code.get(code, [])}))


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


def load(conn, cards: codex_cards.CodexCards) -> Codex:
    """The current stredisko-1 list + the history (call `update_history` first, so a code new
    in this push is known with its first_seen)."""
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
    for _card, code, _first, last in hist:
        latest[code] = max(latest.get(code, _NEVER), last)
    owners: dict[str, list[str]] = {}
    carried: dict[str, set[str]] = {}
    first_seen: dict[tuple[str, str], datetime] = {}
    for card, code, first, last in hist:
        first_seen[(card, code)] = first
        carried.setdefault(code, set()).add(card)
        if last == latest[code]:
            owners.setdefault(code, []).append(card)
    return Codex(cards, by_card, by_code, owners, carried, first_seen,
                 {s.name: set(card_guard.pickable(conn, s.name)) for s in SCOPES})


def _successor(scope: Scope, code: str, owners: list[str],
               cx: Codex) -> tuple[str | None, str | None]:
    """The ONE code the card(s) that carried `code` carry now for `scope` → (Y, None), or
    (None, reason): `_GONE` when none of them carries a stredisko-1 code any more, else a
    Slovak reason a human reads."""
    found: set[str] = set()
    for card in sorted(owners):
        codes = {r.code for r in cx.by_card.get(card, []) if r.code != code}
        if not codes:
            continue
        fit = codes & cx.pickable[scope.name]
        if not fit:
            return None, (f"karta CODEX {card} nesie teraz kód {', '.join(sorted(codes))}, "
                          f"ale výber kariet ho pre katalóg {scope.label} neponúka")
        if len(fit) > 1:
            newest = max(cx.first_seen.get((card, c), _NEVER) for c in fit)
            fit = {c for c in fit if cx.first_seen.get((card, c), _NEVER) == newest}
        if len(fit) != 1:
            return None, (f"karta CODEX {card} nesie viac kódov naraz ("
                          f"{', '.join(sorted(fit))}) — nie je jasné, ktorý je nový")
        found |= fit
    if not found:
        return None, _GONE
    if len(found) > 1:
        return None, (f"kód mal v CODEXe viac kariet a tie nesú rôzne nové kódy "
                      f"({', '.join(sorted(found))})")
    return found.pop(), None


def _codex_name(code: str, cx: Codex) -> tuple[str | None, str | None]:
    """THE CODEX name of `code` for our catalogs: its stredisko-1 rows (active first), the
    #477 pick's `_name_order` (central sklad-1 row, then newest) → (name, None), or
    (None, reason) when the code sits on no stredisko-1 row or on several cards."""
    rows = cx.by_code.get(code, [])
    if not rows:
        return None, "kód je v CODEXe len v inom stredisku, nie v stredisku 1"
    active = [r for r in rows if not r.inactive] or rows
    if len({r.card for r in active}) > 1:
        return None, (f"kód nesie v CODEXe viac kariet "
                      f"({', '.join(sorted({r.card for r in active}))}) — ktorý názov?")
    best = min(active, key=lambda r: codex_cards._name_order(r.sklad == 1, r.changed_at,
                                                             r.name))
    return (best.name.strip() or None), None


def _fields(card: dict) -> dict:
    return {k: v for k, v in card.items() if k != "overridden"}


def _ours(cards, max_code: int | None) -> dict[str, dict]:
    """Our cards by CODEX code — only numbers the scope's EDI can carry (`card_guard._ours`'
    rule), the canonical number winning over a legacy twin (`codex_cards.index_by_code`)."""
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
    """One catalog's share of the plan, simulated on `live` (our effective catalog) so a
    later step sees an earlier one (a renumbered card is renamed under its new number)."""

    def __init__(self, conn, scope: Scope, cx: Codex, plan: Plan):
        self.conn, self.scope, self.cx, self.plan = conn, scope, cx, plan
        self.catalog = card_guard.catalog(conn, scope.name)
        self.live = {str(c["gtin"]): dict(c) for c in self.catalog}
        self.binned = [dict(c) for c in (snapshot.deleted_catalog_cards(conn)
                                         if scope.name == "orders"
                                         else dl_snapshot.deleted_dl_cards(conn))]

    def run(self) -> None:
        groups = _groups(self.catalog, self.scope.max_code)
        for code, group in groups.items():
            self._code(code, group)
        self._memory(set(groups))
        self._renames()

    def _owners(self, code: str, item: dict) -> list[str] | None:
        """The CODEX card(s) our card with `code` is — or None when nothing changed / it is
        not ours to judge (a review entry is added where a human must decide)."""
        cx = self.cx
        if code not in cx.by_code:
            return cx.owners.get(code)   # None: never seen on stredisko 1 while we watched
        former = cx.former(code)
        if not former or cx.cards.name_status(code, item["name"]) == "ok":
            return None   # the same card still carries it, or ours already is the new holder
        if len(former) > 1:
            self.plan.add_review(dict(item, codex_card=", ".join(sorted(former))), (
                f"kód {code} prešiel v CODEXe z kariet {', '.join(sorted(former))} na kartu "
                f"{cx.cards_of(code)} — ktorá z nich je naša karta?"))
            return None
        return sorted(former)

    def _code(self, code: str, group: list[dict]) -> None:
        cx, scope = self.cx, self.scope
        gtins = [str(c["gtin"]) for c in group]
        item = {"scope": scope.name, "gtin": gtins[0], "gtins": gtins, "code": code,
                "name": group[0].get("name", "")}
        owners = self._owners(code, item)
        if not owners:
            return
        item["codex_card"] = ", ".join(owners)
        succ, why = _successor(scope, code, owners, cx)
        if succ is None:
            if why == _GONE and code not in cx.by_code and not cx.cards.has(code):
                self.plan.removals.append(item)
                for g in gtins:
                    self.live.pop(g, None)
            elif why == _GONE:
                self.plan.add_review(item, (
                    "kód je v CODEXe už len v inom stredisku" if code not in cx.by_code else
                    f"kód {code} teraz nesie karta CODEX {cx.cards_of(code)} a pôvodná karta "
                    f"{item['codex_card']} už nemá žiadny kód"))
            else:
                self.plan.add_review(item, why or "")
            return
        if cx.former(succ):
            self.plan.add_review(item, (
                f"nový kód {succ} predtým patril karte CODEX "
                f"{', '.join(sorted(cx.former(succ)))} — prečíslovanie treba overiť ručne"))
            return
        self._renumber(item, group, succ)

    def _renumber(self, item: dict, group: list[dict], succ: str) -> None:
        target = _ours(self.live.values(), self.scope.max_code).get(succ)
        binned = _ours(self.binned, self.scope.max_code).get(succ)
        if target is not None:
            mode, to = "merge", str(target["gtin"])
        elif binned is not None:
            mode, to = "restore", str(binned["gtin"])
        else:
            mode, to = "create", succ
        old = sorted(set(item["gtins"]) | {item["code"]})
        self.plan.renumbers.append(dict(item, **{
            "from": item["gtin"], "to": to, "mode": mode, "old_gtins": old,
            "memory": _memory_count(self.conn, self.scope, old), "card": _fields(group[0])}))
        for g in item["gtins"]:
            self.live.pop(g, None)
        if mode != "merge":
            self.live[to] = dict(_fields(group[0]), gtin=to)
            self.binned = [b for b in self.binned if str(b["gtin"]) != to]

    def _memory(self, catalog_codes: set[str]) -> None:
        """Mapping rows of a code no card of ours holds (written after an earlier renumber)
        follow the same successor — only onto a card we have."""
        cx = self.cx
        per_code: dict[str, set[str]] = {}
        for table in self.scope.memory:
            for (gtin,) in self.conn.execute(
                    f"SELECT DISTINCT gtin FROM {table} WHERE deleted_at IS NULL").fetchall():
                code = codex_cards.normalize_code(gtin)
                if (code and code not in catalog_codes and code not in cx.by_code
                        and code in cx.owners):
                    per_code.setdefault(code, set()).add(str(gtin))
        ours = _ours(self.live.values(), self.scope.max_code)
        for code, gtins in sorted(per_code.items()):
            succ, _why = _successor(self.scope, code, cx.owners[code], cx)
            if succ is None or cx.former(succ) or succ not in ours:
                continue
            old = sorted(gtins | {code})
            self.plan.renumbers.append({
                "scope": self.scope.name, "gtin": code, "gtins": [], "code": code, "name": "",
                "codex_card": ", ".join(cx.owners[code]), "from": code,
                "to": str(ours[succ]["gtin"]), "mode": "memory", "old_gtins": old,
                "memory": _memory_count(self.conn, self.scope, old), "card": {}})

    def _renames(self) -> None:
        cx = self.cx
        for gtin, card in self.live.items():
            code = codex_cards.normalize_code(gtin)
            # a code reused by another card is never renamed — followed or reviewed above
            if not code or not cx.cards.has(code) or cx.former(code):
                continue
            if cx.cards.name_status(code, card.get("name", "")) != "drift":
                continue
            item = {"scope": self.scope.name, "gtin": gtin, "code": code,
                    "name": card.get("name", "")}
            new, why = _codex_name(code, cx)
            if new is None:
                self.plan.add_review(item, why or "")
            elif new != (card.get("name") or "").strip():
                self.plan.renames.append(dict(item, old=card.get("name", ""), new=new,
                                              card=_fields(card)))


def build_plan(conn, cx: Codex) -> Plan:
    """Everything the current CODEX list implies for our two catalogs + memories (no writes)."""
    plan = Plan()
    for scope in SCOPES:
        _ScopePlanner(conn, scope, cx, plan).run()
    return plan
