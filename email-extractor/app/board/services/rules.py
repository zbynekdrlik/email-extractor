"""Rules service for the unified nástenka lane 6 (#447, spec §4).

The read side of „Naučené sklad / Naučené objednávky": a SINGLE unified row model over the
five „naučené" tables, so the warehouse can finally SEE, search and open the origin of every
rule the system learned. Everything here is a THIN pohľad — it only READs (list + origin
joins + search + paging); every WRITE (update/delete) lives in the sibling `rules_edit.py`
and DELEGATES to the engine write paths (`board.md`: call the engines, never copy them). The
per-kind editor field descriptor lives in the route modules (`rules_orders.py` /
`rules_dl.py`), merged into the list `meta` so `tab-rules.js` builds each kind from data.

Kinds by scope (spec §4):
  orders — mail (mail_rules) · alias (item_memory, curated) · global (global_item_memory)
  dl     — dl_alias (dl_item_memory, curated) · supplier (dl_supplier_memory)
"""
from __future__ import annotations

from ...httpapi_common import _fold

PAGE_SIZE = 50
_CURATED = ("human", "sheet-import")

# kind -> its physical table. TRUSTED literals (never user input) — `rules_edit` interpolates
# `table` into the audit row's table_name, and it must be a real, whitelisted table name.
_KIND_TABLE: dict[str, str] = {
    "mail": "mail_rules",
    "alias": "item_memory",
    "global": "global_item_memory",
    "dl_alias": "dl_item_memory",
    "supplier": "dl_supplier_memory",
}
SCOPE_KINDS: dict[str, tuple[str, ...]] = {
    "orders": ("mail", "alias", "global"),
    "dl": ("dl_alias", "supplier"),
}
# Slovak chip labels for the tab (kind -> label). Per-row labels add the customer/supplier.
KIND_LABELS: dict[str, str] = {
    "mail": "Ignorované maily",
    "alias": "Aliasy položiek (zákazník)",
    "global": "Globálne aliasy",
    "dl_alias": "Aliasy DL položiek (dodávateľ)",
    "supplier": "Pamäť dodávateľov",
}


def validate(scope: str, kind: str) -> None:
    """Raise ValueError (route → 400) for an unknown scope, unknown kind, or a kind that does
    not belong to the requested scope (a DL kind under orders, etc.)."""
    if scope not in SCOPE_KINDS:
        raise ValueError(f"neznámy scope {scope!r}")
    if kind not in SCOPE_KINDS[scope]:
        raise ValueError(f"neznámy kind {kind!r} pre scope {scope!r}")


def table_for(kind: str) -> str:
    return _KIND_TABLE[kind]


def scope_of(kind: str) -> str:
    """The scope a kind belongs to (each kind lives in exactly one). Raises ValueError (route
    → 400) for an unknown kind — so update/delete need only `<kind>`, never a client scope."""
    for scope, kinds in SCOPE_KINDS.items():
        if kind in kinds:
            return scope
    raise ValueError(f"neznámy kind {kind!r}")


def _iso(ts) -> str | None:
    return ts.isoformat() if ts is not None else None


def _rows_mail(conn) -> list[dict]:
    out = []
    for r in conn.execute(
            "SELECT mr.id, mr.sender_norm, mr.subject_key, mr.action, "
            "mr.sample_had_attachments, mr.created_at, mr.question_id, "
            "oq.message_id, oq.answered_by "
            "FROM mail_rules mr LEFT JOIN order_questions oq ON oq.id = mr.question_id "
            "WHERE mr.deleted_at IS NULL ORDER BY mr.created_at DESC").fetchall():
        label = "Ignorovaný mail" if r[3] == "ignore" else "Mailové pravidlo (ručne)"
        out.append({
            "kind": "mail", "id": int(r[0]), "label": label, "target": "",
            "key": {"sender": r[1] or "", "subject_key": r[2] or "", "action": r[3] or ""},
            "values": {"subject_key": r[2] or "", "action": r[3] or ""},
            "sample_had_attachments": r[4],
            "origin": {"question_id": r[6], "message_id": r[7], "by": r[8] or "",
                       "created_at": _iso(r[5]), "source": ""},
        })
    return out


def _rows_alias(conn) -> list[dict]:
    out = []
    for r in conn.execute(
            "SELECT id, customer_ean, item_raw, gtin, card, source, created_at "
            "FROM item_memory WHERE deleted_at IS NULL AND source = ANY(%s) "
            "ORDER BY created_at DESC", (list(_CURATED),)).fetchall():
        out.append({
            "kind": "alias", "id": int(r[0]),
            "label": f"Alias položky (zákazník {r[1] or '—'})", "target": r[4] or "",
            "key": {"wording": r[2] or "", "ean": r[1] or "", "gtin": r[3] or ""},
            "values": {"wording": r[2] or "", "gtin": r[3] or "", "card": r[4] or ""},
            "origin": {"question_id": None, "message_id": None, "by": "",
                       "created_at": _iso(r[6]), "source": r[5] or ""},
        })
    return out


def _rows_global(conn) -> list[dict]:
    out = []
    for r in conn.execute(
            "SELECT g.id, g.item_raw, g.gtin, g.card, g.taught_by, g.created_at, "
            "g.question_id, oq.message_id, oq.answered_by "
            "FROM global_item_memory g LEFT JOIN order_questions oq ON oq.id = g.question_id "
            "WHERE g.deleted_at IS NULL ORDER BY g.created_at DESC").fetchall():
        out.append({
            "kind": "global", "id": int(r[0]), "label": "Globálny alias", "target": r[3] or "",
            "key": {"wording": r[1] or "", "gtin": r[2] or ""},
            "values": {"wording": r[1] or "", "gtin": r[2] or "", "card": r[3] or ""},
            "origin": {"question_id": r[6], "message_id": r[7],
                       "by": r[4] or (r[8] or ""), "created_at": _iso(r[5]), "source": ""},
        })
    return out


def _rows_dl_alias(conn) -> list[dict]:
    out = []
    for r in conn.execute(
            "SELECT id, supplier_ean, item_raw, gtin, card, source, created_at "
            "FROM dl_item_memory WHERE deleted_at IS NULL AND source = ANY(%s) "
            "ORDER BY created_at DESC", (list(_CURATED),)).fetchall():
        out.append({
            "kind": "dl_alias", "id": int(r[0]),
            "label": f"Alias DL položky (dodávateľ {r[1] or '—'})", "target": r[4] or "",
            "key": {"wording": r[2] or "", "ean": r[1] or "", "gtin": r[3] or ""},
            "values": {"wording": r[2] or "", "gtin": r[3] or "", "card": r[4] or ""},
            "origin": {"question_id": None, "message_id": None, "by": "",
                       "created_at": _iso(r[6]), "source": r[5] or ""},
        })
    return out


def _rows_supplier(conn) -> list[dict]:
    out = []
    for r in conn.execute(
            "SELECT id, sender_email, ean_edi, name, created_at "
            "FROM dl_supplier_memory WHERE deleted_at IS NULL "
            "ORDER BY created_at DESC").fetchall():
        out.append({
            "kind": "supplier", "id": int(r[0]), "label": "Pamäť dodávateľa",
            "target": r[3] or "",
            "key": {"email": r[1] or "", "ean": r[2] or ""},
            "values": {"ean": r[2] or "", "name": r[3] or ""},
            "origin": {"question_id": None, "message_id": None, "by": "",
                       "created_at": _iso(r[4]), "source": ""},
        })
    return out


_FETCH = {
    "mail": _rows_mail, "alias": _rows_alias, "global": _rows_global,
    "dl_alias": _rows_dl_alias, "supplier": _rows_supplier,
}


def _haystack(row: dict) -> str:
    parts = [str(v) for v in row["key"].values()]
    o = row.get("origin") or {}
    parts += [row.get("target") or "", str(o.get("message_id") or ""), str(o.get("by") or ""),
              str(o.get("source") or "")]
    return _fold(" ".join(parts))


def list_rules(conn, *, scope: str, kind: str, q: str = "", page: int = 0) -> dict:
    """One kind's rules, search-filtered over every text field, and paged. Raises ValueError
    (route → 400) for an unknown scope/kind."""
    validate(scope, kind)
    rows = _FETCH[kind](conn)
    if q:
        needle = _fold(q)
        rows = [r for r in rows if needle in _haystack(r)]
    total = len(rows)
    page = max(0, int(page))
    start = page * PAGE_SIZE
    return {"items": rows[start:start + PAGE_SIZE], "page": page, "page_size": PAGE_SIZE,
            "total": total, "has_more": start + PAGE_SIZE < total}
