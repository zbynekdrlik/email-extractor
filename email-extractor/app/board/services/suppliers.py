"""Dodávatelia (sklad) service for the unified nástenka lane 5 (#446, spec §4/§5).

Logic + SQL for the DL-supplier tab — a THIN pohľad over `orders.dl_snapshot`
(`dl_suppliers_for_management`/`upsert_dl_supplier`/`retire_dl_supplier`). Scanner/relay
addresses are stripped before ANY save via the SINGLE guard `dl_questions.is_scanner_sender`
(#407 — never store a scanner address as a supplier identity). Every create/update/delete
DELEGATES to the same engine the legacy `/znalosti` route uses, rebuilds the DL snapshot, and
writes an `audit_log` row (restorable from the Kôš — `dl_supplier_overrides` is in
`audit._SOFT_DELETE_TABLES`/`_SNAPSHOT_TABLES`).
"""
from __future__ import annotations

from ...httpapi_common import _fold, _parse_emails_field
from ...orders import dl_snapshot, snapshot
from . import audit
from .partners import PartnerError, matches, name_and_ean, row

PAGE = 50   # suppliers per page

_COLS = ("orig_ean_edi", "orig_city", "ean_edi", "name", "emails", "city",
         "retired", "invoice_is_delivery_note")


def _dl_counts(conn) -> dict[str, int]:
    rows = conn.execute("SELECT supplier_ean, count(*) FROM desadv_sent "
                        "WHERE supplier_ean IS NOT NULL GROUP BY supplier_ean").fetchall()
    return {r[0]: int(r[1]) for r in rows}


def list_suppliers(conn, *, q: str = "", page: int = 0) -> dict:
    rows = dl_snapshot.dl_suppliers_for_management(conn)
    if q and q.strip():
        needle = _fold(q)
        rows = [r for r in rows if matches(r, needle, extra_keys=())]
    counts = _dl_counts(conn)
    for r in rows:
        r["dls_shipped"] = counts.get(r.get("ean_edi") or "", 0)
    rows.sort(key=lambda r: _fold(r.get("name") or ""))
    page = max(0, int(page))
    return {"suppliers": rows[page * PAGE:(page + 1) * PAGE],
            "total": len(rows), "page": page, "page_size": PAGE}


def _before(conn, override_id, orig_ean_edi, orig_city) -> dict | None:
    if override_id is not None:
        return row(conn, "dl_supplier_overrides", _COLS, "id = %s", (override_id,))
    if orig_ean_edi is not None:
        return row(conn, "dl_supplier_overrides", _COLS,
                   "orig_ean_edi = %s AND orig_city IS NOT DISTINCT FROM %s",
                   (orig_ean_edi, orig_city))
    return None


def save_supplier(conn, cfg, actor: str, body: dict) -> dict:
    name, ean = name_and_ean(body, "dodávateľ")
    from ...orders.dl_questions import is_scanner_sender
    emails = [e for e in _parse_emails_field(body.get("emails"))
              if not is_scanner_sender(cfg, e)]   # #407 — never store a scanner address
    before = _before(conn, body.get("override_id"),
                     body.get("orig_ean_edi"), body.get("orig_city"))
    try:
        rid = dl_snapshot.upsert_dl_supplier(
            conn, override_id=body.get("override_id"), orig_ean_edi=body.get("orig_ean_edi"),
            orig_city=body.get("orig_city"), ean_edi=ean, name=name, emails=emails,
            city=str(body.get("city") or "").strip())
    except snapshot.DuplicateEan as e:
        raise PartnerError(409, f"EAN {ean} už má dodávateľ {e.existing.get('name', '')}.",
                           existing=e.existing) from e
    except snapshot.InvalidCustomer as e:
        raise PartnerError(400, str(e)) from e
    if "invoice_is_delivery_note" in body:
        conn.execute("UPDATE dl_supplier_overrides SET invoice_is_delivery_note = %s "
                     "WHERE id = %s", (bool(body["invoice_is_delivery_note"]), rid))
    dl_snapshot.dl_rebuild_from_overrides(conn)
    _release(conn, cfg, ean, name, emails)
    after = row(conn, "dl_supplier_overrides", _COLS, "id = %s", (rid,))
    action = "update" if before else "create"
    audit.record(conn, actor=actor, table="dl_supplier_overrides", row_id=rid, action=action,
                 before=before, after=after)
    return {"id": rid, "action": action}


def delete_supplier(conn, cfg, actor: str, body: dict) -> None:
    override_id = body.get("override_id")
    before = _before(conn, override_id, body.get("orig_ean_edi"), body.get("orig_city"))
    ok = dl_snapshot.retire_dl_supplier(conn, override_id=override_id,
                                        orig_ean_edi=body.get("orig_ean_edi"),
                                        orig_city=body.get("orig_city"))
    if not ok:
        raise PartnerError(404, "dodávateľ sa nenašiel")
    dl_snapshot.dl_rebuild_from_overrides(conn)
    rid = override_id if override_id is not None else _resolve_id(
        conn, body.get("orig_ean_edi"), body.get("orig_city"))
    audit.record(conn, actor=actor, table="dl_supplier_overrides", row_id=rid,
                 action="delete", before=before)


def _resolve_id(conn, orig_ean_edi, orig_city):
    r = conn.execute("SELECT id FROM dl_supplier_overrides WHERE orig_ean_edi = %s "
                     "AND orig_city IS NOT DISTINCT FROM %s",
                     (orig_ean_edi, orig_city)).fetchone()
    return r[0] if r else None


def _release(conn, cfg, ean, name, emails) -> None:
    """A saved DL supplier card may unstick a `dl_supplier` question (same call the /znalosti
    route makes). Best-effort — a reprocess failure never fails the save (#323)."""
    try:
        from ...orders import dl_worker
        dl_worker.release_for_supplier_card(conn, cfg, ean, name, emails)
    except Exception:
        audit.log.exception("release_for_supplier_card failed after a board save")
