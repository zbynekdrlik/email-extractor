"""The Kôš / História zmien tab (#444 lane 3, spec §4/§5).

Thin route layer over `board/services/audit.py` (route = parse input + call service + respond;
all logic + SQL live in the service, spec §3). Three routes on the shared `board` blueprint:

- GET  /nastenka/kos                     — the tab page (audit table + filter chips + search)
- GET  /api/board/audit                  — the change log as JSON (table/action/q filters, page)
- POST /api/board/audit/<id>/restore     — "Vrátiť": revert one recorded change

Guarded by `board.auth.board_gate()` (delegated from `httpapi._gate`) like every board path.
"""
from __future__ import annotations

from flask import jsonify, request

from . import render_board
from .auth import actor
from .services import audit


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def register_trash(bp, deps) -> None:
    @bp.get("/nastenka/kos")
    def board_kos():
        return render_board("kos", tab_template="board/trash.html")

    @bp.get("/api/board/audit")
    def board_audit_list():
        args = request.args
        with deps.db() as c:
            res = audit.list_audit(
                c,
                table=(args.get("table") or None),
                action=(args.get("action") or None),
                q=(args.get("q") or None),
                page=_int(args.get("page"), 0),
            )
        return jsonify(res)

    @bp.post("/api/board/audit/<int:audit_id>/restore")
    def board_audit_restore(audit_id: int):
        try:
            with deps.db() as c:
                audit.restore(c, audit_id, by=actor())
        except audit.RestoreError as e:
            return jsonify(error=e.message), e.status
        return jsonify(ok=True)
