"""Zákazníci (spoločné) service for the unified nástenka lane 5 (#446, spec §4/§5).

Logic + SQL for the customer tab — a THIN pohľad over machinery that already exists. It
CALLS the existing engine (`orders.snapshot.customers_for_management`/`upsert_customer`/
`retire_customer`) and groups cards into multi-site FAMILIES by their distinctive name stem
(`orders.customer.name_stem`, #435) — never copies that logic. Every create/update/delete
DELEGATES to the same `upsert_customer`/`retire_customer` the legacy `/znalosti` route uses,
rebuilds the snapshot, and writes an `audit_log` row via the leaf `board.services.audit` — so
the change lands in the Kôš and is restorable by the existing lane-3 restore logic
(`customer_overrides` is already in `audit._SOFT_DELETE_TABLES`/`_SNAPSHOT_TABLES`).
"""
from __future__ import annotations

from ...httpapi_common import _fold, _parse_emails_field
from ...orders import customer, snapshot
from . import audit
from .partners import PartnerError, matches, name_and_ean, row

PAGE = 25   # families per page

# Business columns captured into the audit before/after (all real, restorable columns —
# the pk `id` and timestamps are excluded; `_restore_update` re-validates each name).
_COLS = ("orig_ean_edi", "orig_street", "ean_edi", "name", "emails",
         "city", "street", "zip", "retired")


def _order_counts(conn) -> dict[str, int]:
    rows = conn.execute("SELECT customer_ean, count(*) FROM edi_sent "
                        "WHERE customer_ean IS NOT NULL GROUP BY customer_ean").fetchall()
    return {r[0]: int(r[1]) for r in rows}


def _group_families(rows: list[dict]) -> list[dict]:
    """Group customer cards into multi-site families by their distinctive name stem
    (`customer.name_stem`, #435). An empty stem (all-generic name) NEVER groups — it is its
    own singleton family, so two unrelated all-generic orgs are never welded together."""
    groups: dict[str, list[dict]] = {}
    families: list[dict] = []
    for r in rows:
        stem = customer.name_stem(r.get("name") or "")
        if stem:
            groups.setdefault(stem, []).append(r)
        else:
            families.append({"stem": "", "label": r.get("name") or "", "size": 1,
                             "sites": [dict(r, site_index=1, site_total=1)]})
    for stem, sites in groups.items():
        sites.sort(key=lambda s: (_fold(s.get("city") or ""), _fold(s.get("name") or "")))
        total = len(sites)
        annotated = [dict(s, site_index=i, site_total=total) for i, s in enumerate(sites, 1)]
        families.append({"stem": stem, "label": annotated[0].get("name") or "",
                         "size": total, "sites": annotated})
    families.sort(key=lambda f: _fold(f["label"]))
    return families


def list_customers(conn, *, q: str = "", page: int = 0) -> dict:
    rows = snapshot.customers_for_management(conn)
    if q and q.strip():
        needle = _fold(q)
        rows = [r for r in rows if matches(r, needle)]
    counts = _order_counts(conn)
    for r in rows:
        r["orders_shipped"] = counts.get(r.get("ean_edi") or "", 0)
    families = _group_families(rows)
    page = max(0, int(page))
    return {"families": families[page * PAGE:(page + 1) * PAGE],
            "total": len(families), "page": page, "page_size": PAGE}


def _before(conn, override_id, orig_ean_edi, orig_street) -> dict | None:
    if override_id is not None:
        return row(conn, "customer_overrides", _COLS, "id = %s", (override_id,))
    if orig_ean_edi is not None:
        return row(conn, "customer_overrides", _COLS,
                   "orig_ean_edi = %s AND orig_street IS NOT DISTINCT FROM %s",
                   (orig_ean_edi, orig_street))
    return None


def save_customer(conn, cfg, actor: str, body: dict) -> dict:
    name, ean = name_and_ean(body, "zákazník")
    before = _before(conn, body.get("override_id"),
                     body.get("orig_ean_edi"), body.get("orig_street"))
    try:
        rid = snapshot.upsert_customer(
            conn, override_id=body.get("override_id"), orig_ean_edi=body.get("orig_ean_edi"),
            orig_street=body.get("orig_street"), ean_edi=ean, name=name,
            emails=_parse_emails_field(body.get("emails")),
            city=str(body.get("city") or "").strip(),
            street=str(body.get("street") or "").strip(),
            zip_=str(body.get("zip") or "").strip())
    except snapshot.DuplicateEan as e:
        raise PartnerError(409, f"EAN {ean} už má zákazník {e.existing.get('name', '')}.",
                           existing=e.existing) from e
    except snapshot.InvalidCustomer as e:
        raise PartnerError(400, str(e)) from e
    snapshot.rebuild_from_overrides(conn)
    _retry_unknown(conn, cfg)
    after = row(conn, "customer_overrides", _COLS, "id = %s", (rid,))
    action = "update" if before else "create"
    audit.record(conn, actor=actor, table="customer_overrides", row_id=rid, action=action,
                 before=before, after=after)
    return {"id": rid, "action": action}


def delete_customer(conn, cfg, actor: str, body: dict) -> None:
    override_id = body.get("override_id")
    before = _before(conn, override_id, body.get("orig_ean_edi"), body.get("orig_street"))
    ok = snapshot.retire_customer(conn, override_id=override_id,
                                  orig_ean_edi=body.get("orig_ean_edi"),
                                  orig_street=body.get("orig_street"))
    if not ok:
        raise PartnerError(404, "zákazník sa nenašiel")
    snapshot.rebuild_from_overrides(conn)
    rid = override_id if override_id is not None else _resolve_id(
        conn, body.get("orig_ean_edi"), body.get("orig_street"))
    audit.record(conn, actor=actor, table="customer_overrides", row_id=rid, action="delete",
                 before=before)


def _resolve_id(conn, orig_ean_edi, orig_street):
    r = conn.execute("SELECT id FROM customer_overrides WHERE orig_ean_edi = %s "
                     "AND orig_street IS NOT DISTINCT FROM %s",
                     (orig_ean_edi, orig_street)).fetchone()
    return r[0] if r else None


def _retry_unknown(conn, cfg) -> None:
    """A saved customer may be exactly what an open `customer` question was waiting for —
    unstick it now (same call the /znalosti route makes). Best-effort: a failure here never
    fails the save (the periodic worker sweep is the backstop)."""
    try:
        from ...orders import hold
        hold.retry_unknown_customer_questions(conn, cfg)
    except Exception:
        audit.log.exception("retry_unknown_customer_questions failed after a board save")
