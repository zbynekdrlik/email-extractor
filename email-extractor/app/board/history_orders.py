"""Route layer for the two history tabs of the unified nástenka (#448, spec §4/§5).

Thin: parse input, call a `services.history*` function, respond. Mounted on the ONE board
blueprint. The scope (orders|dl) is a `?scope=` query-param on every route — the TAB decides
scope, never the key (spec §6), and `board_gate` already authorized the session, so the orders
`sklad` key reaches the DL history too. Column LABELS differ per scope: ORDERS_LABELS here,
`history_dl.DL_LABELS` for DL, merged into the list `meta` so `tab-history.js` builds each
scope's headers from data. No business logic is re-implemented — list/detail/rerun/manual/teach
delegate to the services (which delegate to the real engines), the card picker reuses the lane-4
`/api/board/products` search, and the original preview is board-gated + history-scoped.
"""
from __future__ import annotations

from flask import jsonify, request, send_file

from . import history_dl
from .auth import actor
from .services import history, history_detail, teachback
from .services.history_actions import HistoryActionError
from .services.teachback import TeachbackError

ORDERS_LABELS: dict[str, str] = {
    "partner": "Zákazník",
    "doc": "Objednávka / EDI",
    "empty": "Žiadne objednávky v tomto filtri.",
}
_LABELS = {"orders": ORDERS_LABELS, "dl": history_dl.DL_LABELS}


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def register(bp, deps) -> None:
    @bp.get("/api/board/history")
    def board_history_list():
        scope = request.args.get("scope", "orders")
        try:
            with deps.db() as c:
                res = history.list_documents(
                    c, scope=scope, q=request.args.get("q", "").strip(),
                    status=request.args.get("status", "").strip(),
                    dfrom=request.args.get("from", "").strip(),
                    dto=request.args.get("to", "").strip(),
                    page=_int(request.args.get("page")))
        except ValueError as e:
            return jsonify(error=str(e)), 400
        res["meta"]["labels"] = _LABELS.get(scope, ORDERS_LABELS)
        return jsonify(**res)

    @bp.get("/api/board/history/<message_id>")
    def board_history_detail(message_id: str):
        scope = request.args.get("scope", "orders")
        try:
            with deps.db() as c:
                d = history_detail.document_detail(c, deps.data_dir, scope, message_id)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        if d is None:
            return jsonify(error="doklad neexistuje"), 404
        d["labels"] = _LABELS.get(scope, ORDERS_LABELS)
        return jsonify(d)

    @bp.post("/api/board/history/<message_id>/rerun")
    def board_history_rerun(message_id: str):
        scope = request.args.get("scope", "orders")
        try:
            with deps.db() as c:
                from .services import history_actions
                res = history_actions.rerun(c, deps.cfg, scope, message_id, actor())
        except ValueError as e:
            return jsonify(error=str(e)), 400
        except HistoryActionError as e:
            return jsonify(error=e.message), e.status
        return jsonify(**res)

    @bp.post("/api/board/history/<message_id>/manual")
    def board_history_manual(message_id: str):
        scope = request.args.get("scope", "orders")
        try:
            with deps.db() as c:
                from .services import history_actions
                res = history_actions.manual(c, deps.cfg, scope, message_id, actor())
        except ValueError as e:
            return jsonify(error=str(e)), 400
        except HistoryActionError as e:
            return jsonify(error=e.message), e.status
        return jsonify(**res)

    @bp.post("/api/board/history/<message_id>/teach")
    def board_history_teach(message_id: str):
        scope = request.args.get("scope", "orders")
        body = request.get_json(silent=True) or {}
        try:
            with deps.db() as c:
                res = teachback.teach_item(
                    c, scope, message_id, actor(),
                    name=body.get("name", ""), gtin=body.get("gtin", ""),
                    card=body.get("card", ""))
        except ValueError as e:
            return jsonify(error=str(e)), 400
        except TeachbackError as e:
            return jsonify(error=e.message), e.status
        return jsonify(**res)

    @bp.get("/api/board/history/<message_id>/files/<int:idx>")
    def board_history_file(message_id: str, idx: int):
        scope = request.args.get("scope", "orders")
        with deps.db() as c:
            path = history_detail.file_path(c, deps.data_dir, scope, message_id, idx)
        if path is None:
            return jsonify(error="súbor nie je dostupný"), 404
        return send_file(path)

    @bp.get("/api/board/history/<message_id>/eml")
    def board_history_eml(message_id: str):
        scope = request.args.get("scope", "orders")
        with deps.db() as c:
            path = history_detail.eml_path(c, deps.data_dir, scope, message_id)
        if path is None:
            return jsonify(error="e-mail nie je dostupný"), 404
        return send_file(path, mimetype="message/rfc822")
