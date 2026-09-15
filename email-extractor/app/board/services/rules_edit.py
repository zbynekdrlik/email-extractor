"""Write side of the nástenka „Naučené" tabs (#447, spec §4/§5).

Split out of `rules.py` to keep each service ≤200 r. (spec §3), the same way lane 4 split
`catalog.py` + `catalog_aliases.py`. Every update/delete DELEGATES to the canonical engine
write path (`board.md`: never copy the engines, never raw memory SQL in the board) and records
an `audit_log` row via the leaf `audit.record`:

  * DELETE is always SOFT — the engine helpers set `deleted_at` (spec §5, recoverable from the
    Kôš); the matching readers (`pipeline._mail_rule`, `memory.resolve`, `dl_memory.resolve`,
    `dl_supplier_memory.resolve`) already filter `deleted_at IS NULL`, so a delete stops the
    rule by construction.
  * UPDATE edits in place and returns the PRE-edit `before` dict, recorded so the Kôš
    update-restore can write it back.

There is NO create path here — a rule is only ever born by ANSWERING a question (the engines),
so the scanner-address guard (#407) cannot be bypassed from this tab.
"""
from __future__ import annotations

import psycopg

from ...orders import dl_memory, dl_supplier_memory, memory, teach
from . import audit, rules


class RuleCollision(Exception):
    """An update whose new key collides with an existing rule (route → 409)."""


def _alias_fields(body: dict) -> dict:
    """The three helper kwargs (item_raw/gtin/card) for an alias update. Raises ValueError
    (route → 400) when a required field is blank."""
    fields = {"item_raw": str(body.get("wording") or "").strip(),
              "gtin": str(body.get("gtin") or "").strip(),
              "card": str(body.get("card") or "").strip()}
    if not (fields["item_raw"] and fields["gtin"]):
        raise ValueError("chýba znenie alebo číslo položky")
    return fields


def _ean(conn, table: str, rid: int, col: str) -> str | None:
    """Read one row's owning EAN (customer/supplier) — the delete helpers are EAN-scoped for
    safety. `table`/`col` are TRUSTED literals from the kind config, `rid` is bound."""
    row = conn.execute(
        f"SELECT {col} FROM {table} WHERE id = %s AND deleted_at IS NULL", (rid,)).fetchone()
    return row[0] if row else None


def _alias_update(fn, conn, rid, body):
    """Run one alias-kind update helper + build its audit `after` (incl. the recomputed
    item_key the stored row actually carries). Returns (before, after)."""
    f = _alias_fields(body)
    before = fn(conn, rid, **f)
    after = dict(f, item_key=memory.item_key(f["item_raw"]))
    return before, after


def update(conn, scope: str, kind: str, rid: int, body: dict, actor: str) -> bool:
    """Edit one rule in place via the engine path + an `update` audit row. Returns False when
    the rule does not exist (route → 404); raises ValueError (route → 400) for a bad field,
    or RuleCollision (route → 409) when the new key duplicates an existing rule."""
    rules.validate(scope, kind)
    try:
        if kind == "mail":
            before = teach.update_mail_rule(conn, rid, subject=body.get("subject_key", ""),
                                            action=body.get("action", ""))
            after = {"subject_key": teach.subject_key(str(body.get("subject_key") or "")),
                     "action": str(body.get("action") or "").strip()}
        elif kind == "global":
            before, after = _alias_update(memory.update_global_row, conn, rid, body)
        elif kind == "alias":
            before, after = _alias_update(memory.update_item_memory_row, conn, rid, body)
        elif kind == "dl_alias":
            before, after = _alias_update(dl_memory.update_dl_item_memory_row, conn, rid, body)
        else:  # supplier
            before = dl_supplier_memory.update_by_id(conn, rid, ean_edi=body.get("ean", ""),
                                                     name=body.get("name", ""))
            after = {"ean_edi": str(body.get("ean") or "").strip(),
                     "name": str(body.get("name") or "").strip()}
    except psycopg.errors.UniqueViolation as e:
        # the edited key collides with another live rule — a clean 409, not a 500. autocommit
        # means the failed statement is its own aborted tx; the connection stays usable.
        raise RuleCollision("toto pravidlo už existuje") from e
    if before is None:
        return False
    audit.record(conn, actor=actor, table=rules.table_for(kind), row_id=rid, action="update",
                 before=before, after=after)
    return True


def delete(conn, scope: str, kind: str, rid: int, actor: str) -> bool:
    """Soft-delete one rule via the engine path + a `delete` audit row. Returns False when the
    rule does not exist / is already deleted (route → 404)."""
    rules.validate(scope, kind)
    if kind == "mail":
        ok = teach.soft_delete_mail_rule(conn, rid) is not None
    elif kind == "global":
        ok = memory.delete_global_row(conn, rid)
    elif kind == "alias":
        ean = _ean(conn, "item_memory", rid, "customer_ean")
        ok = ean is not None and memory.delete_item_memory_row(conn, rid, ean)
    elif kind == "dl_alias":
        ean = _ean(conn, "dl_item_memory", rid, "supplier_ean")
        ok = ean is not None and dl_memory.delete_dl_item_memory_row(conn, rid, ean)
    else:  # supplier
        ok = dl_supplier_memory.soft_delete(conn, rid) is not None
    if not ok:
        return False
    audit.record(conn, actor=actor, table=rules.table_for(kind), row_id=rid, action="delete")
    return True
