"""CODEX is the ONE source of truth for a card's code and name (#478).

Owner order 2026-09-30 (#478, after the #467 incident — card 27 carried code 3698 for a few
days, the warehouse saw the rožok code under a stale „bageta" name, invented a code and broke
orders AND delivery notes): a card's identity lived in three copies that never followed CODEX —
the orders catalog (`snapshot` + `catalog_overrides`), the DL catalog (`dl_snapshot` +
`dl_catalog_overrides`) and the learned mappings keyed by code (`item_memory`,
`global_item_memory`, `dl_item_memory`). This module runs after every ACCEPTED CODEX
stock-card push (`POST /api/codex/cards`, `httpapi_codex`) and mirrors CODEX onto the cards WE
ALREADY HAVE — never a bulk import of CODEX cards (#337): nothing here ever adds a card that
was not already in that catalog.

- **Name drift** (the #467 `CodexCards.name_status` "drift" — the Produkty sklad flag; a
  cosmetic „80 gr"/„80g" difference is not drift) → our card takes the CODEX name (the
  stredisko-1 name, `codex_cards._name_order` like the #477 pick), aliases / doplnok / mass /
  sklad / cena untouched, audited `update` (Kôš „Vrátiť" writes the old name back).
- **Renumber** — the same CODEX card (ACSKLP; unique only WITHIN a stredisko, so the identity
  is (stredisko 1, ACSKLP)) no longer carries our code X on stredisko 1 and carries Y instead
  (`codex_card_history` remembers who carried X — `codex_stock_cards` is a full replace and
  forgets it) → per catalog: our card becomes Y (created from X's fields, or X merged into a Y
  we already have, or our Y restored from the Kôš), X goes to the Kôš, every memory row of X is
  rewritten to Y (one audit row each; a row that would duplicate an existing Y row is
  soft-deleted instead — the tables are UNIQUE on the mapping) and ONE ops message says „karta
  N zmenila kód X → Y, upravené". Y must be a code the #477 pick would offer for that catalog
  (`card_guard` scope: orders = sklad 1, DL = any sklad but ≤ 13 chars, active rows); several
  candidates → the card's newest code, else a human decides.
- **Gone** — X is nowhere in CODEX any more and its card carries no stredisko-1 code → our card
  goes to the Kôš + the ops message. A code CODEX never had while we watched (no history — the
  #467 "missing" cards) is never touched here.
- **Stale / missing list** (`codex_cards.live_guard` None) → nothing is written (log.warning).
  **More than `MAX_CODE_CHANGES` codes changing in one push** → nothing is applied (a broken
  CODEX export that still passed the push's shrink guard must not strip our catalogs) + ops
  message.
- **Rollout switch `codex_sync_apply`** (default false = DRY-RUN): the plan is computed, logged
  and stored in `codex_sync_runs.report`, and nothing is written to a catalog, a memory table,
  the audit log or the ops outbox. The history is kept in both modes (so the first applied run
  still knows who carried what).

Every write goes through the same engine functions the nástenka uses (`snapshot` /
`dl_snapshot` upsert / retire / undelete, the audit leaf) — the Kôš reverts each change. One
sync at a time (`pg_advisory_xact_lock`), each sync in ONE transaction. Nothing here touches
CODEX, ORION or a shipped document.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import escape

from psycopg.types.json import Json

from . import card_guard, codex_cards, dl_alerts, dl_snapshot, report, snapshot

log = logging.getLogger("orders.codex_sync")

ACTOR = "codex-sync"
ALERT_KIND = "codex_card_sync"
# pending_alerts.message_id dedup key PREFIX (+ ":<run id>") — no mail behind this alert
ALERT_KEY = "codex-sync"
# More distinct codes renumbered/removed in ONE push than this → apply nothing, ask a human.
# A normal day renumbers 0-2 cards (card 27); a half-broken export shows up as dozens.
MAX_CODE_CHANGES = 10
MAX_ALERT_LINES = 40
STREDISKO = codex_cards.PICK_STREDISKO
_LOCK_KEY = 478_478_478
_NEVER = datetime.min.replace(tzinfo=UTC)
_GONE = "gone"


@dataclass(frozen=True)
class _Scope:
    name: str
    label: str
    table: str
    memory: tuple[str, ...]
    sklady: tuple[int, ...] | None
    max_code: int | None


def _scope(name: str, label: str, memory: tuple[str, ...]) -> _Scope:
    # the renumber target must be a code the #477 pick would offer for this catalog — ONE
    # definition of that scope (`card_guard`), never a second copy here
    spec = card_guard._spec(name)
    return _Scope(name, label, spec["table"], memory, spec["sklady"], spec["max_code"])


SCOPES = (
    _scope("orders", "objednávky", ("item_memory", "global_item_memory")),
    _scope("dl", "sklad", ("dl_item_memory",)),
)
_BY_NAME = {s.name: s for s in SCOPES}
# memory table -> the UNIQUE mapping columns besides `gtin` (a rewrite X→Y must not collide);
# trusted literals, the only table/column names ever interpolated below
_MEMORY_KEYS: dict[str, tuple[str, ...]] = {
    "item_memory": ("customer_ean", "item_key", "delivered_on"),
    "global_item_memory": (),
    "dl_item_memory": ("supplier_ean", "item_key", "delivered_on", "cnt"),
}


@dataclass(frozen=True)
class _Row:
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
    stored verbatim in `codex_sync_runs.report`."""
    renames: list[dict] = field(default_factory=list)
    renumbers: list[dict] = field(default_factory=list)
    removals: list[dict] = field(default_factory=list)
    review: list[dict] = field(default_factory=list)

    def code_changes(self) -> int:
        """Distinct CODEX codes renumbered or removed (one card in both catalogs = one)."""
        return len({i["code"] for i in self.renumbers + self.removals})

    def counts(self) -> dict:
        return {"renamed": len(self.renames), "renumbered": len(self.renumbers),
                "removed": len(self.removals), "review": len(self.review)}


# --- CODEX side: the current stredisko-1 list + the history ---------------------------------

def _rows(conn) -> list[_Row]:
    return [_Row(r[0], r[1], int(r[2]), r[3] or "", bool(r[4]), r[5]) for r in conn.execute(
        "SELECT code, card_code, sklad, name, inactive, changed_at FROM codex_stock_cards "
        "WHERE stredisko = %s", (STREDISKO,)).fetchall()]


def _update_history(conn, as_of: datetime) -> None:
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


def _history(conn) -> tuple[dict[str, list[str]], dict[tuple[str, str], datetime]]:
    """(code -> the stredisko-1 cards that carried it LAST, (card, code) -> first_seen)."""
    rows = conn.execute(
        "SELECT card_code, code, first_seen, last_seen FROM codex_card_history "
        "WHERE stredisko = %s", (STREDISKO,)).fetchall()
    latest: dict[str, datetime] = {}
    for _card, code, _first, last in rows:
        latest[code] = max(latest.get(code, _NEVER), last)
    owners: dict[str, list[str]] = {}
    first_seen: dict[tuple[str, str], datetime] = {}
    for card, code, first, last in rows:
        first_seen[(card, code)] = first
        if last == latest[code]:
            owners.setdefault(code, []).append(card)
    return owners, first_seen


def _successor(scope: _Scope, code: str, owners: list[str], by_card: dict[str, list[_Row]],
               first_seen: dict[tuple[str, str], datetime]) -> tuple[str | None, str | None]:
    """The ONE code the card(s) that carried `code` carry now in `scope` → (Y, None), or
    (None, reason): `_GONE` when no such card carries any stredisko-1 code any more, else a
    Slovak reason a human reads."""
    found: set[str] = set()
    for card in sorted(owners):
        rows = [r for r in by_card.get(card, []) if r.code != code]
        if not rows:
            continue
        fit = {r.code for r in rows if not r.inactive
               and (scope.sklady is None or r.sklad in scope.sklady)
               and (scope.max_code is None or len(r.code) <= scope.max_code)}
        if not fit:
            return None, (f"karta CODEX {card} nesie teraz iný kód, ale žiadny, ktorý by "
                          f"katalóg {scope.label} vedel použiť")
        if len(fit) > 1:
            newest = max(first_seen.get((card, c), _NEVER) for c in fit)
            fit = {c for c in fit if first_seen.get((card, c), _NEVER) == newest}
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


def _codex_name(code: str, by_code: dict[str, list[_Row]]) -> tuple[str | None, str | None]:
    """THE CODEX name of `code` for our catalogs: its stredisko-1 rows (active first), the
    #477 pick's `_name_order` (central sklad-1 row, then newest) → (name, None), or
    (None, reason) when the code sits on no stredisko-1 row or on several cards."""
    rows = by_code.get(code, [])
    if not rows:
        return None, "kód je v CODEXe len v inom stredisku, nie v stredisku 1"
    active = [r for r in rows if not r.inactive] or rows
    if len({r.card for r in active}) > 1:
        return None, (f"kód nesie v CODEXe viac kariet "
                      f"({', '.join(sorted({r.card for r in active}))}) — ktorý názov?")
    best = min(active, key=lambda r: codex_cards._name_order(r.sklad == 1, r.changed_at,
                                                             r.name))
    return (best.name.strip() or None), None


# --- the plan ----------------------------------------------------------------------------------

def _find(live: dict[str, dict], code: str, max_code: int | None) -> str | None:
    """Our exact gtin of the card holding CODEX `code` (the canonical number first), or None.
    A number longer than the scope's EDI can carry never counts (the `card_guard._ours` rule)."""
    hits = [g for g in live if codex_cards.normalize_code(g) == code
            and (max_code is None or len(g) <= max_code)]
    return code if code in hits else (hits[0] if hits else None)


def _memory_count(conn, scope: _Scope, gtins: list[str]) -> dict[str, int]:
    return {t: int(conn.execute(
        f"SELECT count(*) FROM {t} WHERE gtin = ANY(%s) AND deleted_at IS NULL",
        (gtins,)).fetchone()[0]) for t in scope.memory}


def _plan_scope(conn, scope: _Scope, cards: codex_cards.CodexCards, plan: Plan,
                by_card: dict[str, list[_Row]], by_code: dict[str, list[_Row]],
                owners: dict[str, list[str]], first_seen: dict) -> None:
    catalog = card_guard.catalog(conn, scope.name)
    live = {str(c["gtin"]): dict(c) for c in catalog}
    binned = {str(c["gtin"]): c for c in (snapshot.deleted_catalog_cards(conn)
                                          if scope.name == "orders"
                                          else dl_snapshot.deleted_dl_cards(conn))}
    for card in catalog:
        gtin = str(card["gtin"])
        code = codex_cards.normalize_code(gtin)
        if not code or code in by_code or code not in owners:
            continue   # still on stredisko 1, or never seen there (not ours to judge)
        succ, why = _successor(scope, code, owners[code], by_card, first_seen)
        item = {"scope": scope.name, "gtin": gtin, "code": code, "name": card.get("name", ""),
                "codex_card": ", ".join(sorted(owners[code]))}
        if succ is None and why == _GONE and not cards.has(code):
            plan.removals.append(item)
            live.pop(gtin, None)
            continue
        if succ is None:
            plan.review.append(dict(item, reason=(
                "kód je v CODEXe už len v inom stredisku" if why == _GONE else why)))
            continue
        old = [gtin] + ([code] if code != gtin and code not in live else [])
        target = _find(live, succ, scope.max_code)
        if target is not None:
            mode, to_gtin = "merge", target
        else:
            to_gtin = _find(binned, succ, scope.max_code) or succ
            mode = "restore" if to_gtin in binned else "create"
        plan.renumbers.append(dict(item, **{
            "from": gtin, "to": to_gtin, "mode": mode, "memory": _memory_count(conn, scope, old),
            "old_gtins": old, "card": {k: v for k, v in card.items() if k != "overridden"}}))
        moved = live.pop(gtin)
        if mode != "merge":
            live[to_gtin] = dict(moved, gtin=to_gtin)
            binned.pop(to_gtin, None)
    for gtin, card in live.items():
        code = codex_cards.normalize_code(gtin)
        if not code or not cards.has(code):
            continue
        if cards.name_status(code, card.get("name", "")) != "drift":
            continue
        new, why = _codex_name(code, by_code)
        item = {"scope": scope.name, "gtin": gtin, "code": code, "name": card.get("name", "")}
        if new is None:
            if why:
                plan.review.append(dict(item, reason=why))
            continue
        if new != (card.get("name") or "").strip():
            plan.renames.append(dict(item, old=card.get("name", ""), new=new,
                                     card={k: v for k, v in card.items()
                                           if k != "overridden"}))


def build_plan(conn, cards: codex_cards.CodexCards) -> Plan:
    """Everything the current CODEX list implies for our two catalogs + memories (no writes).
    Reads the history — call `_update_history` first so a code new in this push is known."""
    rows = _rows(conn)
    by_card: dict[str, list[_Row]] = {}
    by_code: dict[str, list[_Row]] = {}
    for r in rows:
        by_card.setdefault(r.card, []).append(r)
        by_code.setdefault(r.code, []).append(r)
    owners, first_seen = _history(conn)
    plan = Plan()
    for scope in SCOPES:
        _plan_scope(conn, scope, cards, plan, by_card, by_code, owners, first_seen)
    return plan


# --- applying it (the same engine functions + audit the nástenka uses) ------------------------

def _write_card(conn, scope: _Scope, gtin: str, card: dict, name: str) -> None:
    if scope.name == "orders":
        snapshot.upsert_catalog_card(conn, gtin, name, alias=card.get("alias") or "")
    else:
        dl_snapshot.upsert_dl_catalog_card(
            conn, gtin, name, doplnok=card.get("doplnok") or "", mass=card.get("mass"),
            sklad=card.get("sklad") or "", cena=card.get("cena"))


def _retire(conn, scope: _Scope, gtin: str) -> bool:
    return (snapshot.retire_catalog_card(conn, gtin) if scope.name == "orders"
            else dl_snapshot.retire_dl_catalog_card(conn, gtin))


def _rewrite_memory(conn, table: str, old: list[str], new: str, note: str) -> int:
    """Every live mapping row of `old` → `new`, one audit row each. A row whose mapping already
    exists under `new` (UNIQUE) is soft-deleted instead of colliding. Returns rows touched."""
    from ..board.services import audit  # lazy: the audit leaf, like orders.teach does
    keys = _MEMORY_KEYS[table]
    cols = ", ".join(("id", "gtin") + keys)
    rows = conn.execute(f"SELECT {cols} FROM {table} WHERE gtin = ANY(%s) "
                        "AND deleted_at IS NULL ORDER BY id", (old,)).fetchall()
    clash_sql = (f"SELECT 1 FROM {table} WHERE gtin = %s AND "
                 + " AND ".join(f"{k} = %s" for k in keys) + " LIMIT 1") if keys else None
    for rid, gtin, *key in rows:
        if clash_sql and conn.execute(clash_sql, (new, *key)).fetchone():
            conn.execute(f"UPDATE {table} SET deleted_at = now() WHERE id = %s", (rid,))
            audit.record(conn, actor=ACTOR, table=table, row_id=rid, action="delete",
                         note=f"{note} — rovnaké priradenie už existuje pod {new}")
            continue
        conn.execute(f"UPDATE {table} SET gtin = %s WHERE id = %s", (new, rid))
        audit.record(conn, actor=ACTOR, table=table, row_id=rid, action="update",
                     before={"gtin": gtin}, after={"gtin": new}, note=note)
    return len(rows)


def _apply(conn, plan: Plan) -> None:
    from ..board.services import audit  # lazy: the audit leaf, like orders.teach does
    touched: set[str] = set()
    for r in plan.renumbers:
        scope = _BY_NAME[r["scope"]]
        note = f"karta CODEX {r['codex_card']} zmenila kód {r['from']} → {r['to']} (#478)"
        if r["mode"] != "merge":
            if r["mode"] == "restore":
                (snapshot.undelete_catalog_card if scope.name == "orders"
                 else dl_snapshot.undelete_dl_catalog_card)(conn, r["to"])
            _write_card(conn, scope, r["to"], r["card"], r["card"].get("name", ""))
            audit.record(conn, actor=ACTOR, table=scope.table, row_id=r["to"], action="create",
                         after={"gtin": r["to"], "name": r["card"].get("name", ""),
                                "source": "codex-sync", "renumbered_from": r["from"],
                                "codex_card": r["codex_card"],
                                "restored": r["mode"] == "restore"}, note=note)
        if _retire(conn, scope, r["from"]):
            audit.record(conn, actor=ACTOR, table=scope.table, row_id=r["from"],
                         action="delete", note=note)
        for table in scope.memory:
            _rewrite_memory(conn, table, r["old_gtins"], r["to"], note)
        touched.add(scope.name)
    for r in plan.removals:
        scope = _BY_NAME[r["scope"]]
        if _retire(conn, scope, r["gtin"]):
            audit.record(conn, actor=ACTOR, table=scope.table, row_id=r["gtin"],
                         action="delete",
                         note=(f"kód {r['code']} (karta CODEX {r['codex_card']}) z CODEXu "
                               f"zmizol bez náhrady (#478)"))
            touched.add(scope.name)
    for r in plan.renames:
        scope = _BY_NAME[r["scope"]]
        if scope.name == "orders":
            # alias=None: a name-only edit never touches the (tri-state, #383) alias
            snapshot.upsert_catalog_card(conn, r["gtin"], r["new"])
        else:
            _write_card(conn, scope, r["gtin"], r["card"], r["new"])
        audit.record(conn, actor=ACTOR, table=scope.table, row_id=r["gtin"], action="update",
                     before={"name": r["old"]}, after={"name": r["new"]},
                     note="názov podľa CODEXu (#478)")
        touched.add(scope.name)
    if "orders" in touched:
        snapshot.rebuild_from_overrides(conn)
    if "dl" in touched:
        dl_snapshot.dl_rebuild_from_overrides(conn)


# --- the ops message + the run log -------------------------------------------------------------

def _labels(items: list[dict]) -> str:
    return ", ".join(_BY_NAME[s].label for s in ("orders", "dl")
                     if any(i["scope"] == s for i in items))


def _alert_lines(plan: Plan, new_review: list[dict]) -> list[str]:
    lines: list[str] = []
    groups: dict[tuple, list[dict]] = {}
    for r in plan.renumbers:
        groups.setdefault((r["code"], codex_cards.normalize_code(r["to"]), r["codex_card"]),
                          []).append(r)
    for (code, to, card), items in groups.items():
        rows = sum(sum(i["memory"].values()) for i in items)
        lines.append(f"karta CODEX {escape(card)} „{escape(items[0]['name'])}“ zmenila kód "
                     f"{escape(code)} → {escape(str(to))} — upravené ({_labels(items)}), "
                     f"pamäť: {rows} priradení")
    gone: dict[str, list[dict]] = {}
    for r in plan.removals:
        gone.setdefault(r["code"], []).append(r)
    for code, items in gone.items():
        lines.append(f"kód {escape(code)} („{escape(items[0]['name'])}“, karta CODEX "
                     f"{escape(items[0]['codex_card'])}) z CODEXu zmizol bez náhrady — karta "
                     f"presunutá do Koša ({_labels(items)})")
    for r in plan.renames:
        lines.append(f"názov podľa CODEXu ({_BY_NAME[r['scope']].label}) {escape(r['gtin'])}: "
                     f"„{escape(r['old'])}“ → „{escape(r['new'])}“")
    for r in new_review:
        lines.append(f"treba skontrolovať ({_BY_NAME[r['scope']].label}) {escape(r['gtin'])} "
                     f"„{escape(r['name'])}“: {escape(r['reason'])}")
    return lines


def _alert_html(plan: Plan, mode: str, new_review: list[dict]) -> str | None:
    if mode == "blocked":
        head = (f"&#9888;&#65039; Synchronizácia kariet s CODEXom (#478) sa ZASTAVILA: zoznam z "
                f"CODEXu by naraz zmenil kódy {plan.code_changes()} kariet (limit "
                f"{MAX_CODE_CHANGES}) — vyzerá to na neúplný export, nič sa nezmenilo. "
                f"Skontroluj codex-bridge ETL a codex-cards-push na dev2.")
        lines = _alert_lines(Plan(renumbers=plan.renumbers, removals=plan.removals), [])
    else:
        head = "&#128260; Karty podľa CODEXu (#478) — zmeny z posledného zoznamu kariet:"
        lines = _alert_lines(plan, new_review)
        if not lines:
            return None
    more = len(lines) - MAX_ALERT_LINES
    shown = lines[:MAX_ALERT_LINES] + ([f"… a ďalších {more}"] if more > 0 else [])
    return (f"<p>{head}</p><ul>" + "".join(f"<li>{line}</li>" for line in shown)
            + "</ul><p>Každá zmena je v nástenke → Kôš a dá sa vrátiť.</p>")


def _review_key(r: dict) -> tuple:
    return (r.get("scope"), r.get("gtin"), r.get("reason"))


def _last_applied_review(conn) -> set[tuple]:
    row = conn.execute("SELECT report FROM codex_sync_runs WHERE status = 'apply' "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    return {_review_key(r) for r in ((row[0] or {}).get("review") or [])} if row else set()


def _record(conn, sync: dict | None, status: str, report_json: dict) -> int:
    row = conn.execute(
        "INSERT INTO codex_sync_runs (sync_id, applied, status, report) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (sync["id"] if sync else None, status == "apply", status, Json(report_json))
    ).fetchone()
    return int(row[0])


def _report(plan: Plan, mode: str, as_of: datetime | None) -> dict:
    strip = ("card", "old_gtins")
    return {"mode": mode, "as_of": as_of.isoformat() if as_of else None,
            "counts": plan.counts(),
            "renames": [{k: v for k, v in r.items() if k not in strip} for r in plan.renames],
            "renumbers": [{k: v for k, v in r.items() if k not in strip}
                          for r in plan.renumbers],
            "removals": plan.removals, "review": plan.review}


def _log(plan: Plan, mode: str) -> None:
    verb = "would" if mode != "apply" else "did"
    for r in plan.renumbers:
        log.info("codex sync (%s) %s renumber %s card %s -> %s (CODEX card %s, %s)", mode, verb,
                 r["scope"], r["from"], r["to"], r["codex_card"], r["mode"])
    for r in plan.removals:
        log.info("codex sync (%s) %s remove %s card %s — code left CODEX (card %s)", mode,
                 verb, r["scope"], r["gtin"], r["codex_card"])
    for r in plan.renames:
        log.info("codex sync (%s) %s rename %s card %s: %r -> %r", mode, verb, r["scope"],
                 r["gtin"], r["old"], r["new"])
    for r in plan.review:
        log.warning("codex sync (%s): %s card %s needs a human — %s", mode, r["scope"],
                    r["gtin"], r["reason"])
    log.info("codex sync (%s): %s", mode, plan.counts())


# --- the entry point ---------------------------------------------------------------------------

def _summary(mode: str, run_id: int | None, plan: Plan) -> dict:
    return dict(plan.counts(), mode=mode, run_id=run_id)


def run(conn, cfg, now: datetime | None = None) -> dict:
    """Sync our catalogs + memories to the CURRENT CODEX list (after an accepted push). Returns
    {mode: apply|dry-run|blocked|skipped, run_id, renamed, renumbered, removed, review}."""
    cards = codex_cards.live_guard(conn, now)
    sync = codex_cards.latest_sync(conn)
    if cards is None or sync is None:
        log.warning("CODEX card sync skipped: the CODEX stock-card list is stale or missing — "
                    "nothing is renamed, renumbered or removed (#478)")
        return _summary("skipped", _record(conn, sync, "skipped", {"reason": "stale"}), Plan())
    as_of = codex_cards._data_as_of(sync)
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        _update_history(conn, as_of)
        plan = build_plan(conn, cards)
        mode = "apply" if getattr(cfg, "codex_sync_apply", False) else "dry-run"
        if mode == "apply" and plan.code_changes() > MAX_CODE_CHANGES:
            log.warning("CODEX card sync BLOCKED: %d codes would change at once (limit %d) — "
                        "nothing applied (#478)", plan.code_changes(), MAX_CODE_CHANGES)
            mode = "blocked"
        known = _last_applied_review(conn)
        if mode == "apply":
            _apply(conn, plan)
        run_id = _record(conn, sync, mode, _report(plan, mode, as_of))
        if mode in ("apply", "blocked"):
            html = _alert_html(plan, mode, [r for r in plan.review
                                            if _review_key(r) not in known])
            if html:
                dl_alerts.enqueue(conn, report.ops_channel(cfg), ALERT_KIND, html,
                                  message_id=f"{ALERT_KEY}:{run_id}")
    _log(plan, mode)
    return _summary(mode, run_id, plan)


def run_safely(conn, cfg) -> dict:
    """`run` for the push endpoint: the CODEX list is already replaced, so a sync failure must
    never fail the push — it is logged (+ an `error` run row) and retried by the next push."""
    try:
        return run(conn, cfg)
    except Exception as e:
        log.exception("CODEX card sync failed (#478) — the pushed list stays, nothing synced")
        try:
            run_id = _record(conn, codex_cards.latest_sync(conn), "error",
                             {"error": str(e)[:500]})
        except Exception:
            log.exception("CODEX card sync: recording the failed run failed too")
            run_id = None
        return {"mode": "error", "run_id": run_id, "error": str(e)[:200]}
