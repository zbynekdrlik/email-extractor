"""Route layer for the Zákazníci tab of the unified nástenka (#446, spec §4).

Thin: parse input, call `services.partners`, respond. Mounted on the ONE board blueprint.
Customers are listed grouped into multi-site families (#435); create/update/delete DELEGATE
to `snapshot.upsert_customer`/`retire_customer` via the service (never re-implemented) and
every change writes an `audit_log` row, so it lands in the Kôš and is restorable.
"""
from __future__ import annotations

from flask import jsonify, request

from .auth import actor
from .services import customers
from .services.partners import PartnerError


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def register_customers(bp, deps) -> None:
    @bp.get("/api/board/customers")
    def board_customers_list():
        with deps.db() as c:
            res = customers.list_customers(
                c, q=(request.args.get("q") or ""), page=_int(request.args.get("page")))
        return jsonify(res)

    @bp.post("/api/board/customers")
    def board_customers_save():
        body = request.get_json(silent=True) or {}
        try:
            with deps.db() as c:
                res = customers.save_customer(c, deps.cfg, actor(), body)
        except PartnerError as e:
            return jsonify(error=e.message, existing=e.existing), e.status
        return jsonify(ok=True, **res)

    @bp.delete("/api/board/customers")
    def board_customers_delete():
        body = request.get_json(silent=True) or {}
        try:
            with deps.db() as c:
                customers.delete_customer(c, deps.cfg, actor(), body)
        except PartnerError as e:
            return jsonify(error=e.message), e.status
        return jsonify(ok=True)
