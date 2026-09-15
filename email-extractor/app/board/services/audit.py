"""The audit-log writer + restore skeleton (#442 lane 1, spec §5).

LEAF module — imports ONLY stdlib + psycopg, never `app.board` (the blueprint) or any
`app.orders` module. That is deliberate: `orders.teach.answer/undo` import `record` here
LAZILY (inside the function body) to write their own audit rows, and a leaf with no board/
orders imports at module top cannot create an import cycle when it does.

`record()` appends one immutable row for every change made through the nástenka (and the
teach paths). `restore()` is the lane-1 skeleton for "Vrátiť": it only undoes a soft DELETE
(clears `deleted_at`, and `retired` where that column exists) — a full before/after restore
of an update lands with the Kôš tab (lane 3). Both are best-effort from the teach hook's
point of view: a failing audit write must never break the underlying operation, so callers
that need that guarantee wrap the call (see `orders.teach`).
"""
from __future__ import annotations

import logging

from psycopg.types.json import Json

log = logging.getLogger("board.audit")

# table_name -> (primary-key column, whether the table also carries a legacy `retired` flag).
# Every value here is a TRUSTED literal from this module — NEVER user input — so it is safe to
# interpolate the pk column / table name into the restore UPDATE below. `row_id` (the audited
# row's key) is always bound as a parameter, never interpolated.
_SOFT_DELETE_TABLES: dict[str, tuple[str, bool]] = {
    "catalog_overrides": ("gtin", True),
    "dl_catalog_overrides": ("gtin", True),
    "customer_overrides": ("id", True),
    "dl_supplier_overrides": ("id", True),
    "mail_rules": ("id", False),
    "item_memory": ("id", False),
    "global_item_memory": ("id", False),
    "dl_item_memory": ("id", False),
    "dl_supplier_memory": ("id", False),
}


def record(conn, *, actor: str, table: str, row_id, action: str,
           before=None, after=None, note: str = "",
           question_id=None, message_id=None) -> int | None:
    """Append one audit_log row. Returns its id (or None if the write is skipped/fails).

    `row_id` is stored as text (the audited tables key on mixed pk types). `before`/`after`
    are JSONB when given. Never raises for a JSON-serialisation or DB error — the audit trail
    is a record OF an operation, it must not be able to fail that operation.
    """
    try:
        r = conn.execute(
            """INSERT INTO audit_log
                   (actor, table_name, row_id, action, before, after, note,
                    question_id, message_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (str(actor), str(table),
             (str(row_id) if row_id is not None else None), str(action),
             Json(before) if before is not None else None,
             Json(after) if after is not None else None,
             note or "", question_id, message_id),
        ).fetchone()
        return int(r[0]) if r else None
    except Exception:
        log.exception("audit_log write failed (table=%s action=%s row_id=%s)",
                      table, action, row_id)
        return None


def restore(conn, audit_id: int, by: str = "admin") -> bool:
    """Undo a soft DELETE recorded by `audit_id`: clear `deleted_at` (+ `retired`), and append
    a `restore` audit row. Skeleton for lane 1 — only `action='delete'` rows are restorable,
    only for the known soft-delete tables. Returns True when a row was actually un-deleted."""
    row = conn.execute(
        "SELECT table_name, row_id, action FROM audit_log WHERE id = %s", (audit_id,)
    ).fetchone()
    if not row:
        return False
    table_name, row_id, action = row
    if action != "delete" or row_id is None:
        return False
    spec = _SOFT_DELETE_TABLES.get(table_name)
    if spec is None:
        return False
    pk_col, has_retired = spec
    set_clause = "deleted_at = NULL" + (", retired = false" if has_retired else "")
    updated = conn.execute(
        f"UPDATE {table_name} SET {set_clause} "
        f"WHERE {pk_col}::text = %s AND deleted_at IS NOT NULL "
        f"RETURNING {pk_col}",
        (str(row_id),),
    ).fetchone()
    if not updated:
        return False
    record(conn, actor=by, table=table_name, row_id=row_id, action="restore",
           note=f"restored from audit #{audit_id}")
    return True
