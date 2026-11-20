"""Shared partner-service helpers for the unified nástenka lane 5 (#446, spec §4/§5).

The Zákazníci + Dodávatelia tabs each get their own focused service module
(`services/customers.py`, `services/suppliers.py`) — both ≤200 lines (spec §3) — and share
the small, entity-agnostic pieces here: the `PartnerError` HTTP-status carrier, the #234
name+EAN validation, a generic single-row read, and the folded free-text match. Every
partner write DELEGATES to the existing engines (`orders.snapshot` / `orders.dl_snapshot`)
and audits via the leaf `board.services.audit`; the entity modules hold that orchestration.
"""
from __future__ import annotations

from ...httpapi_common import _EAN_STRIP_RE, _fold


class PartnerError(Exception):
    """A partner write that cannot proceed — carries the HTTP `status` the route returns
    and (for a 409 EAN collision) the `existing` row the warehouse can act on."""

    def __init__(self, status: int, message: str, existing: dict | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.existing = existing


def name_and_ean(body: dict, entity: str) -> tuple[str, str]:
    """The #234 validation, identical to the /znalosti rule: a non-blank name and a
    digits-only EAN kód EDI (stripped of spaces/dashes). Raises `PartnerError(400)`."""
    name = str(body.get("name") or "").strip()
    if not name:
        raise PartnerError(400, "chýba názov")
    ean = _EAN_STRIP_RE.sub("", str(body.get("ean_edi") or ""))
    if not ean:
        raise PartnerError(400, f"Bez EAN kódu EDI sa {entity} nedá uložiť — nájdeš ho v "
                                "CODEXe pri odberateľovi.")
    if not ean.isdigit():
        raise PartnerError(400, "EAN kód EDI musí byť len číslice.")
    return name, ean


def row(conn, table: str, cols: tuple[str, ...], where: str, params) -> dict | None:
    """One row of `table` as a {col: value} dict (or None). `table`/`cols`/`where` are
    TRUSTED module literals (never user input); values are always bound params."""
    r = conn.execute(f"SELECT {', '.join(cols)} FROM {table} WHERE {where}", params).fetchone()
    return dict(zip(cols, r, strict=True)) if r else None


def matches(record: dict, needle: str, extra_keys=("street",)) -> bool:
    """Folded free-text match over name/EAN/city (+ street for customers) + every e-mail."""
    parts = [str(record.get(k) or "") for k in ("name", "ean_edi", "city", *extra_keys)]
    parts += [str(e) for e in (record.get("emails") or [])]
    if needle in _fold(" ".join(parts)):
        return True
    # #504: an EAN typed with spaces/dashes („2000 0000 00730") still finds the card
    digits = _EAN_STRIP_RE.sub("", needle)
    return digits.isdigit() and digits in str(record.get("ean_edi") or "")


# --- list sorting + paging (#504) -------------------------------------------------------

PAGE_SIZES = (50, 100, 200, 0)   # 0 = all on one page
DEFAULT_PAGE_SIZE = 100


def page_size(raw) -> int:
    """The requested rows-per-page, only one of `PAGE_SIZES` (0 = all); anything else is
    the default — never an unbounded or negative slice from a typed URL."""
    try:
        size = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE
    return size if size in PAGE_SIZES else DEFAULT_PAGE_SIZE


def sort_key(sort: str, count_key: str):
    """Key for one partner row. A blank value sorts LAST in both directions (the caller
    splits blanks off before reversing), so an EAN-less card never tops the list."""
    if sort == "ean":
        def key(r):
            ean = str(r.get("ean_edi") or "")
            return (len(ean), ean)   # numeric order for digit strings of mixed length
    elif sort == "city":
        def key(r):
            return (_fold(r.get("city") or ""), _fold(r.get("name") or ""))
    elif sort == "count":
        def key(r):
            return int(r.get(count_key) or 0)
    else:
        def key(r):
            return _fold(r.get("name") or "")
    return key


def is_blank(r: dict, sort: str) -> bool:
    return (sort == "ean" and not r.get("ean_edi")) or (sort == "city" and not r.get("city"))


def sorted_rows(rows: list[dict], sort: str, desc: bool, count_key: str) -> list[dict]:
    key = sort_key(sort, count_key)
    filled = sorted((r for r in rows if not is_blank(r, sort)), key=key, reverse=desc)
    return filled + [r for r in rows if is_blank(r, sort)]


def page_slice(items: list, page: int, size: int) -> tuple[list, int]:
    """(the items of `page`, the effective page) — size 0 = everything on page 0."""
    if size == 0:
        return items, 0
    page = max(0, int(page))
    return items[page * size:(page + 1) * size], page
