"""História objednávok / dodacích listov — the LIST service (#448 lane 7, spec §4/§5).

A THIN pohľad over machinery that already exists: `messages` (the document + its terminal
`proc_status`/`proc_outcome`), `order_runs.result` (the email-level partner + built doc
number/filename) and `order_items` (per-item search). Nothing here re-derives matching — it
only READS. The detail view lives in `history_detail.py`, the two safety-critical actions
(rerun / manual) in `history_actions.py`, and the teachback in `teachback.py`, so each module
stays ≤200 r. (spec §3).

Scope (orders|dl) is decided by the TAB (spec §6): `orders` = AI + static order mails,
`dl` = delivery-note mails. Both scopes read the SAME `order_runs`/`order_items` tables — the
DL engine reuses them UNMODIFIED (#200), so the detail machinery is uniform.
"""
from __future__ import annotations

from ...httpapi_common import _escape_like, _valid_date

PAGE_SIZE = 25

# scope -> the message categories that belong to that history tab.
_SCOPES: dict[str, tuple[str, ...]] = {
    "orders": ("ai_orders", "static_orders"),
    "dl": ("dodacie_listy",),
}

# proc_status -> plain-Slovak label (spec §4). Unknown/None falls back to the raw value.
_STATUS_LABEL: dict[str, str] = {
    "ok": "odišlo do ORIONu",
    "partial": "na kontrole",
    "held": "čaká na sklad",
    "review": "na kontrole",
    "error": "zlyhalo",
    "manual": "zadané ručne",
    "ignored": "ignorované",
    "not_warehouse": "netýka sa skladu",
    "sklad_unknown": "odložené (sklad nevie)",
}

# The status filter chips the tab offers -> the proc_status values each covers.
_STATUS_FILTER: dict[str, tuple[str, ...]] = {
    "sent": ("ok",),
    "review": ("review", "partial"),
    "held": ("held",),
    "error": ("error",),
    "manual": ("manual",),
    "ignored": ("ignored",),
}

# The chips rendered in the tab, in display order (value + label). "" = all.
STATUS_CHIPS: list[dict] = [{"value": "", "label": "Všetko"}] + [
    {"value": v, "label": _STATUS_LABEL[ps[0]]} for v, ps in _STATUS_FILTER.items()]


def status_label(proc_status: str | None) -> str:
    return _STATUS_LABEL.get(proc_status or "", proc_status or "neznáme")


def scope_categories(scope: str) -> tuple[str, ...]:
    if scope not in _SCOPES:
        raise ValueError(f"neznámy scope {scope!r}")
    return _SCOPES[scope]


def is_history_document(conn, message_id: str, scope: str | None = None) -> bool:
    """The scope guard for the file/eml preview: True only for a message whose category is a
    genuine order/DL document (never an arbitrary mail). When `scope` is given, restrict to
    that scope's categories; otherwise any history scope counts."""
    cats: tuple[str, ...] = (scope_categories(scope) if scope
                             else tuple(c for cc in _SCOPES.values() for c in cc))
    return conn.execute(
        "SELECT 1 FROM messages WHERE message_id = %s AND category = ANY(%s)",
        (message_id, list(cats))).fetchone() is not None


def orders_edi_names(result: dict | None, edi_file: str = "") -> list[str]:
    """Every candidate ORDERS EDI filename recoverable for a message — the message's own
    `edi_file` (set ONLY on a confirmed upload), the run's top-level `edi_filename`, AND every
    per-order `order_results[*].edi_filename` (which IS set the moment the EDI is BUILT, even
    if the subsequent upload FAILED — so `messages.edi_file` being NULL is NOT proof the
    bytes never reached ORION, #51/#239). Order-preserving, de-duplicated."""
    result = result or {}
    names: list[str] = []
    for n in [edi_file, result.get("edi_filename")]:
        if n and n not in names:
            names.append(str(n))
    for r in (result.get("order_results") or []):
        n = r.get("edi_filename") if isinstance(r, dict) else None
        if n and str(n) not in names:
            names.append(str(n))
    return names


def partner_and_docs(scope: str, result: dict | None, from_name: str) -> tuple[str, str, list[str]]:
    """(partner_name, partner_ean, doc_numbers) derived from a run's `result` for one scope.
    Falls back to the envelope `from_name` when the run carries no partner name."""
    result = result or {}
    if scope == "dl":
        docs = [d for d in (result.get("documents") or []) if isinstance(d, dict)]
        name = next((d.get("supplier_name") for d in docs if d.get("supplier_name")), "")
        ean = next((d.get("supplier_ean") for d in docs if d.get("supplier_ean")), "")
        numbers = [str(d.get("doc_number")) for d in docs if d.get("doc_number")]
        if not name:
            name = result.get("supplier_name") or ""
        if not ean:
            ean = result.get("supplier_ean") or ""
    else:
        name = result.get("customer_name") or ""
        ean = result.get("customer_ean") or ""
        numbers = orders_edi_names(result)
    return (name or from_name or ""), (ean or ""), numbers


def _latest_results(conn, message_ids: list[str]) -> dict[str, dict]:
    """The latest NON-shadow run's `result` per message (batched, one query)."""
    if not message_ids:
        return {}
    rows = conn.execute(
        "SELECT DISTINCT ON (message_id) message_id, result FROM order_runs "
        "WHERE message_id = ANY(%s) AND shadow = false ORDER BY message_id, id DESC",
        (message_ids,)).fetchall()
    return {r[0]: (r[1] or {}) for r in rows}


def list_documents(conn, *, scope: str, q: str = "", status: str = "",
                   dfrom: str = "", dto: str = "", page: int = 0) -> dict:
    """One scope's history page: newest first, optionally filtered by status chip, date range
    and a free-text search over subject / partner / doc number / item wording. Raises
    `ValueError` (→ 400) for an unknown scope."""
    cats = list(scope_categories(scope))
    where = ["m.category = ANY(%s)"]
    params: list = [cats]
    if status:
        where.append("m.proc_status = ANY(%s)")
        params.append(list(_STATUS_FILTER.get(status, (status,))))
    if dfrom:
        if not _valid_date(dfrom):
            raise ValueError("zlý dátum od")
        where.append("m.created_at >= %s::date")
        params.append(dfrom)
    if dto:
        if not _valid_date(dto):
            raise ValueError("zlý dátum do")
        where.append("m.created_at < (%s::date + 1)")
        params.append(dto)
    if q and q.strip():
        like = f"%{_escape_like(q.strip())}%"
        where.append(
            "(m.subject ILIKE %s OR m.from_addr ILIKE %s OR m.from_name ILIKE %s "
            "OR COALESCE(m.proc_outcome,'') ILIKE %s "
            "OR EXISTS (SELECT 1 FROM order_runs r WHERE r.message_id = m.message_id "
            "  AND r.shadow = false AND r.result::text ILIKE %s) "
            "OR EXISTS (SELECT 1 FROM order_runs r JOIN order_items i ON i.run_id = r.id "
            "  WHERE r.message_id = m.message_id AND i.name ILIKE %s))")
        params += [like] * 6
    clause = " WHERE " + " AND ".join(where)
    total = conn.execute("SELECT count(*) FROM messages m" + clause, params).fetchone()[0]
    page = max(0, int(page))
    rows = conn.execute(
        "SELECT m.id, m.message_id, m.created_at, m.sent_at, m.from_name, m.subject, "
        "m.proc_status, m.proc_outcome, m.edi_file FROM messages m" + clause
        + " ORDER BY m.id DESC LIMIT %s OFFSET %s",
        params + [PAGE_SIZE, page * PAGE_SIZE]).fetchall()
    results = _latest_results(conn, [r[1] for r in rows])
    items = []
    for r in rows:
        name, ean, numbers = partner_and_docs(scope, results.get(r[1]), r[4] or "")
        items.append({
            "id": r[0], "message_id": r[1],
            "date": r[2].isoformat() if r[2] else None, "sent_at": r[3] or "",
            "partner": name, "partner_ean": ean, "subject": r[5] or "",
            "proc_status": r[6], "status_label": status_label(r[6]),
            "outcome": r[7] or "", "doc_numbers": numbers,
            "doc_number": ", ".join(numbers), "edi_file": r[8] or "",
        })
    return {"items": items, "meta": {
        "scope": scope, "page": page, "page_size": PAGE_SIZE, "total": int(total),
        "has_more": (page + 1) * PAGE_SIZE < int(total), "statuses": STATUS_CHIPS}}
