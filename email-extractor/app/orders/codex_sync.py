"""CODEX is the ONE source of truth for a card's code and name (#478).

Owner order 2026-09-30 (#478, after the #467 incident — card 27 carried code 3698 for a few
days, the warehouse saw the rožok code under a stale „bageta" name, invented a code and broke
orders AND delivery notes): a card's identity lived in three copies that never followed CODEX —
the orders catalog (`snapshot` + `catalog_overrides`), the DL catalog (`dl_snapshot` +
`dl_catalog_overrides`) and the learned mappings keyed by code (`item_memory`,
`global_item_memory`, `dl_item_memory`). `run` is called after every ACCEPTED CODEX stock-card
push (`POST /api/codex/cards`, `httpapi_codex`): it records the list in `codex_card_history`,
builds the plan (`codex_sync_plan` — which CODEX card each of our cards IS
(`codex_card_bindings`), renames, renumbers, removed codes, what a human must decide) and, with
`codex_sync_apply` on, executes it. It never adds a CODEX card we do not already have (the #337
bulk-import ban). An OLDER CODEX snapshot than one already synced is skipped (a re-sent old list
must not undo a renumber).

- **Every write** goes through the engine functions the nástenka uses (`snapshot` /
  `dl_snapshot` upsert / retire / undelete) + one `audit_log` row per change (actor
  `codex-sync`), so the Kôš reverts each one. A renumber rewrites every memory row of the old
  code (one audit row each); a row whose mapping already exists under the new code is
  soft-deleted instead (the tables are UNIQUE on the mapping), and a soft-deleted twin under
  the new code is revived rather than lost.
- **Safety**: a stale / missing CODEX list (`codex_cards.live_guard` None) → nothing is
  written (log.warning). More than `codex_sync_max_code_changes` codes or
  `codex_sync_max_renames` renames in one push → nothing is applied ("blocked", one ops
  message, reminded once per workday morning while the same plan stays blocked) — a broken
  CODEX export that still passed the push's shrink guard must not strip or rename our
  catalogs; raise the limit in the add-on options for a genuine mass change.
- **Rollout switch `codex_sync_apply`** (default false = DRY-RUN): the plan is computed,
  logged and stored in `codex_sync_runs.report` (with `would_block`), and nothing is written
  to a catalog, a memory table, the audit log or the ops outbox. The history is kept in both
  modes, so the first applied run still knows who carried what.
- ONE ops message per applied run with changes (`pending_alerts` → ops channel): „karta N
  zmenila kód X → Y, upravené", removed codes, renames, and review items not already reported
  by the previous applied run. One sync at a time (`pg_advisory_xact_lock`), each sync in ONE
  transaction; `run_safely` never lets a sync failure fail the push. Nothing here touches
  CODEX, ORION or a shipped document.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from html import escape

from psycopg.types.json import Json

from . import codex_cards, dl_alerts, dl_snapshot, report, snapshot
from . import codex_sync_plan as sp

log = logging.getLogger("orders.codex_sync")

ACTOR = "codex-sync"
ALERT_KIND = "codex_card_sync"
# pending_alerts.message_id dedup key PREFIX — no mail behind this alert
ALERT_KEY = "codex-sync"
# Defaults of the `codex_sync_max_*` options: a normal push renumbers 0-2 cards (card 27) and
# renames a few; the first applied run renames ~56 (the live drift of 2026-09-30); a garbled
# CODEX export touches hundreds.
MAX_CODE_CHANGES = 10
MAX_RENAMES = 80
MAX_ALERT_LINES = 40
_LOCK_KEY = 478_478_478


# --- executing the plan -----------------------------------------------------------------------

def _audit():
    from ..board.services import audit  # lazy: the audit leaf, like orders.teach does
    return audit


def _write_card(conn, scope: sp.Scope, gtin: str, card: dict, name: str) -> None:
    """`card`'s fields under `gtin` with `name` (a renumber carries every field over)."""
    if scope.name == "orders":
        snapshot.upsert_catalog_card(conn, gtin, name, alias=card.get("alias") or "")
    else:
        dl_snapshot.upsert_dl_catalog_card(
            conn, gtin, name, doplnok=card.get("doplnok") or "", mass=card.get("mass"),
            sklad=card.get("sklad") or "", cena=card.get("cena"))


def _retire(conn, scope: sp.Scope, gtin: str, note: str) -> None:
    done = (snapshot.retire_catalog_card(conn, gtin) if scope.name == "orders"
            else dl_snapshot.retire_dl_catalog_card(conn, gtin))
    if done:
        _audit().record(conn, actor=ACTOR, table=scope.table, row_id=gtin, action="delete",
                        note=note)


def _rewrite_memory(conn, table: str, old: list[str], new: str, note: str) -> int:
    """Every live mapping row of `old` → `new`, one audit row each. When the same mapping
    already exists under `new` (the tables are UNIQUE on it, soft-deleted rows included) the
    old row is soft-deleted instead — and a soft-deleted twin under `new` is revived, so the
    live mapping survives (an X → Y → X round trip loses nothing). Returns rows touched."""
    audit = _audit()
    keys = sp.MEMORY_KEYS[table]
    cols = ", ".join(("id", "gtin") + keys)
    rows = conn.execute(f"SELECT {cols} FROM {table} WHERE gtin = ANY(%s) "
                        "AND deleted_at IS NULL ORDER BY id", (old,)).fetchall()
    clash_sql = (f"SELECT id, deleted_at IS NOT NULL FROM {table} WHERE gtin = %s AND "
                 + " AND ".join(f"{k} = %s" for k in keys) + " LIMIT 1") if keys else None
    for rid, gtin, *key in rows:
        clash = conn.execute(clash_sql, (new, *key)).fetchone() if clash_sql else None
        if clash is None:
            conn.execute(f"UPDATE {table} SET gtin = %s WHERE id = %s", (new, rid))
            audit.record(conn, actor=ACTOR, table=table, row_id=rid, action="update",
                         before={"gtin": gtin}, after={"gtin": new}, note=note)
            continue
        twin, twin_deleted = clash
        if twin_deleted:
            conn.execute(f"UPDATE {table} SET deleted_at = NULL WHERE id = %s", (twin,))
            audit.record(conn, actor=ACTOR, table=table, row_id=twin, action="create",
                         note=f"{note} — rovnaké priradenie pod {new} obnovené z Koša")
        conn.execute(f"UPDATE {table} SET deleted_at = now() WHERE id = %s", (rid,))
        audit.record(conn, actor=ACTOR, table=table, row_id=rid, action="delete",
                     note=f"{note} — rovnaké priradenie už je pod {new}")
    return len(rows)


def _apply_renumber(conn, r: dict) -> None:
    scope = sp.BY_NAME[r["scope"]]
    note = f"karta CODEX {r['codex_card']} zmenila kód {r['code']} → {r['to']} (#478)"
    if r["mode"] in ("create", "restore"):
        # a restore brings our Kôš card back; for a create it is the guard that a hidden
        # override row under the new number never swallows the card (an upsert alone keeps
        # `deleted_at`, which hides it — review 4 🔴)
        (snapshot.undelete_catalog_card if scope.name == "orders"
         else dl_snapshot.undelete_dl_catalog_card)(conn, r["to"])
        _write_card(conn, scope, r["to"], r["card"], r["card"].get("name", ""))
        _audit().record(conn, actor=ACTOR, table=scope.table, row_id=r["to"], action="create",
                        after={"gtin": r["to"], "name": r["card"].get("name", ""),
                               "source": "codex-sync", "renumbered_from": r["from"],
                               "codex_card": r["codex_card"],
                               "restored": r["mode"] == "restore"}, note=note)
    elif r.get("fill"):
        # a merge keeps our curated data where the target card is blank (a fresh #477 pick
        # carries only the CODEX name [+ sklad]) — audited `update`, the Kôš writes it back
        target = r["target"]
        _write_card(conn, scope, r["to"], dict(target, **r["fill"]), target.get("name", ""))
        _audit().record(conn, actor=ACTOR, table=scope.table, row_id=r["to"], action="update",
                        before={k: target.get(k) for k in r["fill"]}, after=r["fill"],
                        note=f"{note} — doplnené údaje z karty {r['from']}")
    for gtin in r["gtins"]:
        _retire(conn, scope, gtin, note)
        _bind(conn, scope.name, gtin, r["codex_card"], active=False)
    if r["mode"] != "memory":
        _bind(conn, scope.name, r["to"], r["codex_card"], active=True)
    for table in scope.memory:
        _rewrite_memory(conn, table, r["old_gtins"], r["to"], note)


def _bind(conn, scope: str, gtin: str, card: str, *, active: bool) -> None:
    """Our card `gtin` IS CODEX card `card` (`active=False`: a number the sync retired — a
    later memory row of it still follows the card). Identity, not catalog data."""
    conn.execute(
        """INSERT INTO codex_card_bindings (scope, gtin, card_code, active)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (scope, gtin) DO UPDATE
              SET card_code = EXCLUDED.card_code, active = EXCLUDED.active, bound_at = now()""",
        (scope, gtin, card, active))


def _apply(conn, plan: sp.Plan) -> None:
    # resets FIRST: a card the same plan then renumbers is retired AFTER its reset, so the
    # retire leaves `retired` and `deleted_at` set together (#442) — review 5 🟡
    for r in plan.resets:
        scope = sp.BY_NAME[r["scope"]]
        _write_card(conn, scope, r["gtin"], dict(r["card"], **r["after"]),
                    r["card"].get("name", ""))
        _audit().record(conn, actor=ACTOR, table=scope.table, row_id=r["gtin"],
                        action="update", before=r["before"], after=r["after"],
                        note=(f"kód {r['code']} bol predtým iný výrobok — vybraný znova ako "
                              f"karta CODEX {r['codex_card']}, staré údaje vyčistené (#478)"))
    for r in plan.renumbers:
        _apply_renumber(conn, r)
    for r in plan.removals:
        for gtin in r["gtins"]:
            _retire(conn, sp.BY_NAME[r["scope"]], gtin,
                    f"kód {r['code']} (karta CODEX {r['codex_card']}) z CODEXu zmizol bez "
                    f"náhrady (#478)")
            _bind(conn, r["scope"], gtin, r["codex_card"], active=False)
    for r in plan.renames:
        scope = sp.BY_NAME[r["scope"]]
        if scope.name == "orders":
            # alias=None: a name-only edit never touches the (tri-state, #383) alias
            snapshot.upsert_catalog_card(conn, r["gtin"], r["new"])
        else:
            _write_card(conn, scope, r["gtin"], r["card"], r["new"])
        _audit().record(conn, actor=ACTOR, table=scope.table, row_id=r["gtin"],
                        action="update", before={"name": r["old"]}, after={"name": r["new"]},
                        note="názov podľa CODEXu (#478)")
    touched = {r["scope"] for r in plan.renumbers + plan.removals + plan.resets + plan.renames
               if r.get("mode") != "memory"}
    if "orders" in touched:
        snapshot.rebuild_from_overrides(conn)
    if "dl" in touched:
        dl_snapshot.dl_rebuild_from_overrides(conn)


# --- the ops message ---------------------------------------------------------------------------

def _labels(items: list[dict]) -> str:
    return ", ".join(sp.BY_NAME[s].label for s in ("orders", "dl")
                     if any(i["scope"] == s for i in items))


def _change_lines(plan: sp.Plan) -> list[str]:
    lines: list[str] = []
    groups: dict[tuple, list[dict]] = {}
    for r in plan.renumbers:
        key = (r["code"], codex_cards.normalize_code(r["to"]) or r["to"], r["codex_card"])
        groups.setdefault(key, []).append(r)
    for (code, to, card), items in groups.items():
        rows = sum(sum(i["memory"].values()) for i in items)
        cards = [i for i in items if i["mode"] != "memory"]
        if cards:
            lines.append(f"karta CODEX {escape(card)} „{escape(cards[0]['name'])}“ zmenila kód "
                         f"{escape(code)} → {escape(to)} — upravené ({_labels(cards)}), pamäť: "
                         f"{rows} priradení")
        else:
            lines.append(f"pamäť: {rows} priradení starého kódu {escape(code)} (karta CODEX "
                         f"{escape(card)}) presunutých na {escape(to)} ({_labels(items)})")
    gone: dict[str, list[dict]] = {}
    for r in plan.removals:
        gone.setdefault(r["code"], []).append(r)
    for code, items in gone.items():
        lines.append(f"kód {escape(code)} („{escape(items[0]['name'])}“, karta CODEX "
                     f"{escape(items[0]['codex_card'])}) z CODEXu zmizol bez náhrady — karta "
                     f"presunutá do Koša ({_labels(items)})")
    for r in plan.resets:
        lines.append(f"kód {escape(r['gtin'])} ({sp.BY_NAME[r['scope']].label}) bol predtým iný "
                     f"výrobok, teraz karta CODEX {escape(r['codex_card'])} — staré údaje "
                     f"({escape(', '.join(r['after']))}) vyčistené")
    for r in plan.renames:
        lines.append(f"názov podľa CODEXu ({sp.BY_NAME[r['scope']].label}) "
                     f"{escape(r['gtin'])}: „{escape(r['old'])}“ → „{escape(r['new'])}“")
    return lines


def _review_lines(review: list[dict]) -> list[str]:
    return [f"treba skontrolovať ({sp.BY_NAME[r['scope']].label}) {escape(r['gtin'])} "
            f"„{escape(r['name'])}“: {escape(r['reason'])}" for r in review]


def _html(head: str, lines: list[str]) -> str:
    more = len(lines) - MAX_ALERT_LINES
    shown = lines[:MAX_ALERT_LINES] + ([f"… a ďalších {more}"] if more > 0 else [])
    return (f"<p>{head}</p><ul>" + "".join(f"<li>{line}</li>" for line in shown)
            + "</ul><p>Každá zmena je v nástenke → Kôš. Pozor: kým CODEX ostane rovnaký, "
              "ďalší zoznam kariet ju urobí znova — natrvalo ju zmení len oprava v CODEXe "
              "(alebo vypnutie codex_sync_apply v nastaveniach add-onu). Prečíslovanie sú DVE "
              "zmeny (nový kód vytvorený + starý zmazaný) — vracaj vždy obe, inak karta v "
              "katalógu chýba.</p>")


def _review_key(r: dict) -> tuple:
    return (r.get("scope"), r.get("gtin"), r.get("reason"))


def _last_applied_review(conn) -> set[tuple]:
    row = conn.execute("SELECT report FROM codex_sync_runs WHERE status = 'apply' "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    return {_review_key(r) for r in ((row[0] or {}).get("review") or [])} if row else set()


def _plan_digest(plan: sp.Plan) -> str:
    """Stable identity of a plan's changes — one blocked episode per distinct plan."""
    keys = sorted([(r["scope"], r["code"], r["to"]) for r in plan.renumbers]
                  + [(r["scope"], r["code"], "") for r in plan.removals]
                  + [(r["scope"], r["gtin"], r["new"]) for r in plan.renames])
    return hashlib.sha256(json.dumps(keys, ensure_ascii=False).encode()).hexdigest()[:16]


def _alert(conn, cfg, plan: sp.Plan, mode: str, run_id: int, known: set[tuple],
           limits: tuple[int, int]) -> None:
    channel = report.ops_channel(cfg)
    if mode == "blocked":
        key = f"{ALERT_KEY}:blocked:{_plan_digest(plan)}"
        if dl_alerts.reminder_suppressed(conn, cfg, ALERT_KIND, key):
            return
        head = (f"&#9888;&#65039; Synchronizácia kariet s CODEXom (#478) sa ZASTAVILA: zoznam z "
                f"CODEXu by naraz zmenil kódy {plan.code_changes()} kariet (limit {limits[0]}) "
                f"a premenoval {len(plan.renames)} kariet (limit {limits[1]}) — vyzerá to na "
                f"neúplný alebo pokazený export, nič sa nezmenilo. Skontroluj codex-bridge ETL "
                f"a codex-cards-push na dev2. Ak je to zámer (hromadná zmena v CODEXe), zvýš v "
                f"nastaveniach add-onu codex_sync_max_code_changes / codex_sync_max_renames — "
                f"zmeny sa použijú pri ďalšom zozname kariet.")
        dl_alerts.enqueue(conn, channel, ALERT_KIND, _html(head, _change_lines(plan)),
                          message_id=key)
        return
    lines = _change_lines(plan) + _review_lines(
        [r for r in plan.review if _review_key(r) not in known])
    if lines:
        dl_alerts.enqueue(conn, channel, ALERT_KIND, _html(
            "&#128260; Karty podľa CODEXu (#478) — zmeny z posledného zoznamu kariet:", lines),
            message_id=f"{ALERT_KEY}:{run_id}")


# --- the run log -------------------------------------------------------------------------------

def _record(conn, sync: dict | None, status: str, report_json: dict) -> int:
    row = conn.execute(
        "INSERT INTO codex_sync_runs (sync_id, applied, status, report) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (sync["id"] if sync else None, status == "apply", status, Json(report_json))
    ).fetchone()
    return int(row[0])


def _report(plan: sp.Plan, mode: str, as_of: datetime, would_block: bool,
            limits: tuple[int, int]) -> dict:
    strip = ("card", "old_gtins")
    return {"mode": mode, "as_of": as_of.isoformat(), "would_block": would_block,
            "limits": {"code_changes": limits[0], "renames": limits[1]},
            "counts": dict(plan.counts(), codes=plan.code_changes()),
            "renames": [{k: v for k, v in r.items() if k not in strip} for r in plan.renames],
            "renumbers": [{k: v for k, v in r.items() if k not in strip}
                          for r in plan.renumbers],
            "removals": plan.removals, "review": plan.review,
            "resets": [{k: v for k, v in r.items() if k not in strip} for r in plan.resets]}


def _log(plan: sp.Plan, mode: str) -> None:
    verb = "did" if mode == "apply" else "would"
    for r in plan.renumbers:
        log.info("codex sync (%s) %s renumber %s %s -> %s (CODEX card %s, %s)", mode, verb,
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

def _limits(cfg) -> tuple[int, int]:
    return (int(getattr(cfg, "codex_sync_max_code_changes", 0) or MAX_CODE_CHANGES),
            int(getattr(cfg, "codex_sync_max_renames", 0) or MAX_RENAMES))


def _summary(mode: str, run_id: int | None, plan: sp.Plan, would_block: bool = False) -> dict:
    return dict(plan.counts(), mode=mode, run_id=run_id, codes=plan.code_changes(),
                would_block=would_block)


def run(conn, cfg, now: datetime | None = None) -> dict:
    """Sync our catalogs + memories to the CURRENT CODEX list (after an accepted push). Returns
    {mode: apply|dry-run|blocked|skipped, run_id, renamed, renumbered, memory_renumbered,
    removed, review, codes, would_block}."""
    limits = _limits(cfg)
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        # the list must not change under the sync: SHARE conflicts with the push's EXCLUSIVE
        # replace (`codex_cards.replace_cards`), plain readers are never blocked (review 5)
        conn.execute("LOCK TABLE codex_stock_cards IN SHARE MODE")
        cards = codex_cards.live_guard(conn, now)
        sync = codex_cards.latest_sync(conn)
        if cards is None or sync is None:
            log.warning("CODEX card sync skipped: the CODEX stock-card list is stale or missing"
                        " — nothing is renamed, renumbered or removed (#478)")
            return _summary("skipped", _record(conn, sync, "skipped", {"reason": "stale"}),
                            sp.Plan())
        as_of = codex_cards._data_as_of(sync)
        newest = sp.newest_seen(conn)
        if newest is not None and as_of < newest:
            # an OLDER CODEX snapshot than one already synced (a re-sent old list) must never
            # undo a renumber / removal the newer one made
            log.warning("CODEX card sync skipped: the pushed list (CODEX data as of %s) is older "
                        "than one already synced (%s) — nothing changes (#478)", as_of, newest)
            return _summary("skipped", _record(conn, sync, "skipped", {"reason": "older"}),
                            sp.Plan())
        sp.update_history(conn, as_of)
        plan = sp.build_plan(conn, sp.load(conn, cards, as_of))
        too_many = plan.code_changes() > limits[0] or len(plan.renames) > limits[1]
        if not getattr(cfg, "codex_sync_apply", False):
            mode = "dry-run"
        elif too_many:
            log.warning("CODEX card sync BLOCKED: %d codes / %d renames at once (limits %d / %d)"
                        " — nothing applied (#478)", plan.code_changes(), len(plan.renames),
                        *limits)
            mode = "blocked"
        else:
            mode = "apply"
        for s in plan.seeds:
            # identity (which CODEX card our card is) is stored in every mode — except a
            # binding that REPLACES another card's: only an applied run stores it, so a pick
            # seen during a dry-run / blocked run still gets its reset when the apply comes
            if mode == "apply" or not s["replaces"]:
                _bind(conn, s["scope"], s["gtin"], s["card"], active=True)
        known = _last_applied_review(conn)
        if mode == "apply":
            _apply(conn, plan)
        run_id = _record(conn, sync, mode, _report(plan, mode, as_of, too_many, limits))
        if mode != "dry-run":
            _alert(conn, cfg, plan, mode, run_id, known, limits)
    _log(plan, mode)
    return _summary(mode, run_id, plan, too_many)


def run_safely(conn, cfg) -> dict:
    """`run` for the push endpoint: the CODEX list is already replaced, so a sync failure must
    never fail the push — the sync's transaction rolls back whole, the failure is logged
    (+ an `error` run row) and the next push retries it."""
    try:
        return run(conn, cfg)
    except Exception as e:
        log.exception("CODEX card sync failed (#478) — the pushed list stays, nothing synced")
        try:
            run_id: int | None = _record(conn, codex_cards.latest_sync(conn), "error",
                                         {"error": str(e)[:500]})
            _error_alert(conn, cfg, e)
        except Exception:
            log.exception("CODEX card sync: recording the failed run failed too")
            run_id = None
        return {"mode": "error", "run_id": run_id, "error": str(e)[:200]}


def _error_alert(conn, cfg, e: Exception) -> None:
    """ONE ops alert while the sync keeps failing (then at most a workday-morning reminder) —
    a sync that fails on every push must not stay a log line nobody reads."""
    key = f"{ALERT_KEY}:error"
    if dl_alerts.reminder_suppressed(conn, cfg, ALERT_KIND, key):
        return
    dl_alerts.enqueue(conn, report.ops_channel(cfg), ALERT_KIND, (
        "<p>&#9888;&#65039; Synchronizácia kariet s CODEXom (#478) zlyhala — názvy a kódy kariet "
        f"sa neaktualizujú, zoznam kariet z CODEXu je ale prijatý. Chyba: {escape(str(e)[:300])}"
        "</p>"), message_id=key)
