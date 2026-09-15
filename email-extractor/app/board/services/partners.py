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
    return needle in _fold(" ".join(parts))
