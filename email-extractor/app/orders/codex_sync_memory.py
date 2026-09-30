"""The learned-memory rules of the CODEX card sync (#478) — which mapping rows a renumber may
move, and which it must hold for a human. Pure SQL building blocks, no plan state: the planner
(`codex_sync_plan`) decides WHEN a move happens, the executor (`codex_sync`) runs it; both
read the rules from here so they can never disagree (a dry-run count is exactly what the apply
moves).

- `MEMORY_KEYS` — the three code-keyed memory tables and their UNIQUE mapping columns.
- `held_clause` — a row decided after the moment CODEX gave our code to another card (the
  reuse window, `codex_sync_plan.Codex.taken`): it may be that other product's, never moved.
- `taught_clause` / `TAUGHT_SOURCES` — a curated warehouse decision (answers, „Doučiť", the
  sheet import — `memory.CURATED_SOURCES`); anything else is delivery history.
- `memory_split` — what a move of some numbers carries vs what it holds (taught / shipped).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from . import dl_memory, memory

if TYPE_CHECKING:
    from .codex_sync_plan import Scope

# the audit actor of every sync write (`codex_sync.ACTOR`) — its own writes are never a human
# (re)entry of a card, nor a human re-pointing a mapping row
SYNC_ACTOR = "codex-sync"

# memory table -> its UNIQUE mapping columns besides `gtin` (a rewrite X→Y must not collide);
# trusted literals — the only table/column names ever interpolated into SQL here and in
# `codex_sync`
MEMORY_KEYS: dict[str, tuple[str, ...]] = {
    "item_memory": ("customer_ean", "item_key", "delivered_on"),
    "global_item_memory": (),
    "dl_item_memory": ("supplier_ean", "item_key", "delivered_on", "cnt"),
}

# a TAUGHT row = a curated warehouse decision (`memory.CURATED_SOURCES`, identical in
# `dl_memory`): human answers and História „Doučiť" (teachback) — the matcher's taught-first
# rung — plus the sheet import (Naučené lists it; it votes in the delivery-majority step).
# Review 12 🟡: counting teachback as delivery history held it without any review. Anything
# else (a shipped row, a NULL source) is delivery history.
assert memory.CURATED_SOURCES == dl_memory.CURATED_SOURCES
TAUGHT_SOURCES = memory.CURATED_SOURCES

# where a human fixes a TAUGHT row: Naučené lists the answers + the sheet import; a História
# „Doučiť" (teachback) row is undone in the Kôš and taught again (review 12)
CHECK_TAUGHT = ("skontroluj ich v Naučené (priradenie z „Doučiť“ v Histórii zruš v Koši a doúč "
                "znova)")


def held_clause(table: str) -> str:
    """SQL: a mapping row of `table` decided AFTER `%(hold)s` — created then (a NULL
    `created_at` is an old row), a DL row for a delivery then (`dl_item_memory.delivered_on` is
    the document's date; `item_memory.delivered_on` is an order's REQUESTED day, often ahead,
    so it never counts — review 11), or re-pointed then by a human (a non-sync audit row on it,
    e.g. a Naučené edit that keeps `created_at`). Used while CODEX gave our code to another
    card: such a row may be that other product's, so it is never moved (review 10 🟡). `table`
    is a trusted literal (`MEMORY_KEYS`)."""
    return ("(COALESCE(created_at, '-infinity') > %(hold)s::timestamptz"
            + (" OR delivered_on > %(hold)s::timestamptz::date"
               if table == "dl_item_memory" else "")
            + f" OR EXISTS (SELECT 1 FROM audit_log a WHERE a.table_name = '{table}'"
              f" AND a.row_id = {table}.id::text AND a.actor <> '{SYNC_ACTOR}'"
              " AND a.ts > %(hold)s::timestamptz))")


def taught_clause(table: str) -> str:
    """SQL: a TAUGHT row (all of global_item_memory; a NULL source is history — review 12)."""
    if table == "global_item_memory":
        return "TRUE"
    return ("COALESCE(source, '') IN (" + ", ".join(f"'{s}'" for s in TAUGHT_SOURCES) + ")")


@dataclass
class Split:
    """What a memory move of some numbers carries and what it holds (decided after `hold`)."""
    movable: dict[str, int]
    taught: int = 0            # held TAUGHT rows (a human checks them)
    shipped: int = 0           # held delivery history


def memory_split(conn, scope: Scope, gtins: list[str], hold: datetime | None) -> Split:
    split = Split({})
    for t in scope.memory:
        where = f"FROM {t} WHERE gtin = ANY(%(g)s) AND deleted_at IS NULL"
        n_all = int(conn.execute(f"SELECT count(*) {where}", {"g": gtins}).fetchone()[0])
        taught = shipped = 0
        if hold:
            taught, shipped = (int(v or 0) for v in conn.execute(
                f"SELECT count(*) FILTER (WHERE {taught_clause(t)}), "
                f"count(*) FILTER (WHERE NOT ({taught_clause(t)})) {where} AND "
                + held_clause(t), {"g": gtins, "hold": hold}).fetchone())
        split.movable[t] = n_all - taught - shipped
        split.taught += taught
        split.shipped += shipped
    return split
