"""Route layer for the Dodávatelia tab of the unified nástenka (#446, spec §4).

Thin: parse input, call `services.partners`, respond. Mounted on the ONE board blueprint.
Create/update/delete DELEGATE to `dl_snapshot.upsert_dl_supplier`/`retire_dl_supplier` via
the service (never re-implemented); scanner addresses (#407) are stripped in the service
before any save, and every change writes an `audit_log` row (restorable from the Kôš).
"""
from __future__ import annotations

from flask import jsonify, request

from .auth import actor
from .services import suppliers
from .services.partners import PartnerError


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def register_suppliers(bp, deps) -> None:
    @bp.get("/api/board/suppliers")
    def board_suppliers_list():
        with deps.db() as c:
            res = suppliers.list_suppliers(
                c, q=(request.args.get("q") or ""), page=_int(request.args.get("page")))
        return jsonify(res)

    @bp.post("/api/board/suppliers")
    def board_suppliers_save():
        body = request.get_json(silent=True) or {}
        try:
            with deps.db() as c:
                res = suppliers.save_supplier(c, deps.cfg, actor(), body)
        except PartnerError as e:
            return jsonify(error=e.message, existing=e.existing), e.status
        return jsonify(ok=True, **res)

    @bp.delete("/api/board/suppliers")
    def board_suppliers_delete():
        body = request.get_json(silent=True) or {}
        try:
            with deps.db() as c:
                suppliers.delete_supplier(c, deps.cfg, actor(), body)
        except PartnerError as e:
            return jsonify(error=e.message), e.status
        return jsonify(ok=True)
