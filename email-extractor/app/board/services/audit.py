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


# The 4 override tables whose effective state lives in a frozen snapshot — a restore that
# changes their rows must re-freeze it, or the change is invisible to matching until the next
# unrelated edit. Value = which snapshot line to rebuild. The 5 memory/rule tables are read
# directly at match time (no snapshot), so they need no rebuild. Keys are a subset of the
# trusted `_SOFT_DELETE_TABLES` whitelist.
_SNAPSHOT_TABLES: dict[str, str] = {
    "catalog_overrides": "orders",
    "customer_overrides": "orders",
    "dl_catalog_overrides": "dl",
    "dl_supplier_overrides": "dl",
}

# Filter-chip groups the Kôš UI offers (spec §4): Zmazané / Zmeny / Odpovede. A bare exact
# action string is also accepted by list_audit().
_ACTION_GROUPS: dict[str, tuple[str, ...]] = {
    "zmazane": ("delete",),
    "zmeny": ("create", "update"),
    "odpovede": ("answer", "undo", "reopen"),
}


class RestoreError(Exception):
    """A restore that cannot proceed — carries the HTTP `status` the endpoint should return
    (404 unknown audit id, 409 already-reverted / not-in-the-right-state, 400 unsupported)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _rebuild_snapshot(conn, table: str) -> None:
    """Re-freeze the effective snapshot after a restore touched an override table. Lazy import
    keeps this module a leaf (spec §3 / board.md) — no `app.orders` import at module top."""
    kind = _SNAPSHOT_TABLES.get(table)
    if kind == "orders":
        from ...orders import snapshot
        snapshot.rebuild_from_overrides(conn)
    elif kind == "dl":
        from ...orders import dl_snapshot
        dl_snapshot.dl_rebuild_from_overrides(conn)


def _table_columns(conn, table: str) -> dict[str, str]:
    """{column_name: data_type} for `table` — used to validate an update-restore's `before`
    keys against the REAL schema (so only genuine columns are ever interpolated) and to know
    which columns are jsonb (wrapped in Json())."""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = %s",
        (table,)).fetchall()}


def list_audit(conn, *, table: str | None = None, action: str | None = None,
               q: str | None = None, page: int = 0, page_size: int = 50) -> dict:
    """The Kôš list: audit_log rows newest-first, optionally filtered by table, by action (an
    exact action OR a chip group name from `_ACTION_GROUPS`), and by a free-text search across
    actor / table / action / note / row_id / question_id / message_id. Returns
    {items, total, page, page_size}."""
    where: list[str] = []
    params: list = []
    if table:
        where.append("table_name = %s")
        params.append(table)
    if action:
        acts = list(_ACTION_GROUPS.get(action, (action,)))
        where.append("action = ANY(%s)")
        params.append(acts)
    if q and q.strip():
        like = f"%{q.strip()}%"
        where.append(
            "(actor ILIKE %s OR table_name ILIKE %s OR action ILIKE %s OR note ILIKE %s "
            "OR COALESCE(row_id, '') ILIKE %s OR COALESCE(question_id::text, '') ILIKE %s "
            "OR COALESCE(message_id, '') ILIKE %s)")
        params += [like] * 7
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute("SELECT count(*) FROM audit_log" + clause, params).fetchone()[0]
    page = max(0, int(page))
    page_size = max(1, min(int(page_size), 200))
    rows = conn.execute(
        "SELECT id, ts, actor, table_name, row_id, action, before, after, note, "
        "question_id, message_id FROM audit_log" + clause
        + " ORDER BY id DESC LIMIT %s OFFSET %s",
        params + [page_size, page * page_size]).fetchall()
    items = [{
        "id": int(r[0]),
        "ts": r[1].isoformat() if r[1] is not None else None,
        "actor": r[2], "table_name": r[3], "row_id": r[4], "action": r[5],
        "before": r[6], "after": r[7], "note": r[8] or "",
        "question_id": r[9], "message_id": r[10],
    } for r in rows]
    return {"items": items, "total": int(total), "page": page, "page_size": page_size}


def restore(conn, audit_id: int, by: str = "admin") -> bool:
    """Revert the change recorded by `audit_id` and append a NEW `restore` audit row (history
    is never mutated). Dispatches on the recorded action:

    - delete  → un-delete the soft-deleted row (+ rebuild snapshot for an override table)
    - create  → soft-delete the created row (+ rebuild)
    - update  → write the recorded `before` back over the row (+ rebuild)
    - answer  → `teach.undo` (removes the taught mapping, reopens the question)
    - undo    → re-apply the last prior `answer` via `teach.answer`
    - reopen  → re-expire the question (inverse of the Otázky-tab reopen)

    SAFE by construction: NOTHING here uploads to / touches an ORION ledger; it only reverts
    curated/override/memory rows and question state through the sanctioned engine functions.
    Raises `RestoreError` (404/409/400) when the restore cannot proceed; returns True on
    success (kept for the lane-1 callers that assert `is True`)."""
    row = conn.execute(
        "SELECT table_name, row_id, action, before, question_id "
        "FROM audit_log WHERE id = %s", (audit_id,)).fetchone()
    if not row:
        raise RestoreError(404, f"audit záznam #{audit_id} neexistuje")
    table_name, row_id, action, before, question_id = row
    qid = question_id if question_id is not None else row_id
    if action == "delete":
        _restore_soft_delete(conn, table_name, row_id, undelete=True)
    elif action == "create":
        _restore_soft_delete(conn, table_name, row_id, undelete=False)
    elif action == "update":
        _restore_update(conn, table_name, row_id, before)
    elif action == "answer":
        _restore_answer(conn, qid, by)
    elif action == "undo":
        _restore_undo(conn, qid, by)
    elif action == "reopen":
        _restore_reopen(conn, qid)
    else:
        raise RestoreError(400, f"akciu '{action}' nevieme vrátiť")
    record(conn, actor=by, table=table_name, row_id=row_id, action="restore",
           note=f"vrátené z audit #{audit_id}", question_id=question_id)
    return True


def _restore_soft_delete(conn, table_name, row_id, *, undelete: bool) -> None:
    """`undelete=True` clears deleted_at (+ retired) — revert a delete. `undelete=False`
    sets deleted_at=now() (+ retired) — revert a create. Both rebuild an override snapshot."""
    spec = _SOFT_DELETE_TABLES.get(table_name)
    if spec is None or row_id is None:
        raise RestoreError(400, f"tabuľku '{table_name}' nevieme vrátiť")
    pk_col, has_retired = spec
    if undelete:
        set_clause = "deleted_at = NULL" + (", retired = false" if has_retired else "")
        guard, msg = "deleted_at IS NOT NULL", "riadok už nie je zmazaný (už vrátené?)"
    else:
        set_clause = "deleted_at = now()" + (", retired = true" if has_retired else "")
        guard, msg = "deleted_at IS NULL", "riadok je už zmazaný (už vrátené?)"
    updated = conn.execute(
        f"UPDATE {table_name} SET {set_clause} "
        f"WHERE {pk_col}::text = %s AND {guard} RETURNING {pk_col}",
        (str(row_id),)).fetchone()
    if not updated:
        raise RestoreError(409, msg)
    _rebuild_snapshot(conn, table_name)


def _restore_update(conn, table_name, row_id, before) -> None:
    """Write the recorded `before` dict back over the row. Column names are validated against
    the real schema (never interpolate an unknown name); the pk is bound, never interpolated;
    jsonb columns are wrapped in Json()."""
    spec = _SOFT_DELETE_TABLES.get(table_name)
    if spec is None or row_id is None:
        raise RestoreError(400, f"tabuľku '{table_name}' nevieme vrátiť")
    if not isinstance(before, dict) or not before:
        raise RestoreError(409, "chýbajú pôvodné hodnoty (before)")
    pk_col, _ = spec
    cols = _table_columns(conn, table_name)
    sets, values = [], []
    for key, val in before.items():
        if key == pk_col or key not in cols:
            continue
        sets.append(f"{key} = %s")
        values.append(Json(val) if cols[key] == "jsonb" and val is not None else val)
    if not sets:
        raise RestoreError(409, "žiadne obnoviteľné stĺpce v 'before'")
    values.append(str(row_id))
    updated = conn.execute(
        f"UPDATE {table_name} SET {', '.join(sets)} WHERE {pk_col}::text = %s "
        f"RETURNING {pk_col}", values).fetchone()
    if not updated:
        raise RestoreError(409, "pôvodný riadok neexistuje")
    _rebuild_snapshot(conn, table_name)


def _restore_answer(conn, qid, by) -> None:
    """Revert a teach `answer`: run `teach.undo` (removes the mapping, reopens the question).
    Lazy import — audit is a leaf; `teach` imports `audit` lazily too, so no cycle."""
    if qid is None:
        raise RestoreError(400, "audit riadok nemá otázku")
    from ...orders import teach
    q = teach.get(conn, int(qid))
    if not q:
        raise RestoreError(404, "otázka neexistuje")
    if q.get("status") != "answered":
        raise RestoreError(409, "otázka nie je zodpovedaná (už vrátené?)")
    teach.undo(conn, int(qid))


def _restore_undo(conn, qid, by) -> None:
    """Revert a teach `undo`: re-apply the LAST prior `answer` for this question, via the same
    sanctioned `teach.answer` path (which re-teaches the mapping). Nothing ships."""
    if qid is None:
        raise RestoreError(400, "audit riadok nemá otázku")
    from ...orders import teach
    q = teach.get(conn, int(qid))
    if not q:
        raise RestoreError(404, "otázka neexistuje")
    if q.get("status") != "open":
        raise RestoreError(409, "otázka nie je otvorená")
    prior = conn.execute(
        "SELECT after FROM audit_log WHERE question_id = %s AND action = 'answer' "
        "AND after IS NOT NULL ORDER BY id DESC LIMIT 1", (int(qid),)).fetchone()
    if not prior or not prior[0]:
        raise RestoreError(409, "niet predošlej odpovede na obnovenie")
    after = prior[0]
    gtin = str(after.get("gtin") or "")
    card = str(after.get("card") or "")
    if not gtin:
        raise RestoreError(409, "predošlá odpoveď nemá kartu")
    try:
        teach.answer(conn, int(qid), gtin, card, by=by)
    except Exception as e:  # NotACandidate / AlreadyAnswered — cannot re-apply cleanly
        raise RestoreError(409, f"nedá sa znovu odpovedať: {e}") from e


def _restore_reopen(conn, qid) -> None:
    """Revert a question `reopen` (expired→open): set it back to `expired`. A pure state flip
    on `order_questions` — nothing is shipped, no ORION ledger touched."""
    if qid is None:
        raise RestoreError(400, "audit riadok nemá otázku")
    updated = conn.execute(
        "UPDATE order_questions SET status = 'expired' "
        "WHERE id = %s AND status <> 'expired' RETURNING id", (int(qid),)).fetchone()
    if not updated:
        raise RestoreError(409, "otázka už je expirovaná (už vrátené?)")
