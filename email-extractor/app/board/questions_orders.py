"""Route layer for the two question tabs of the unified nástenka (#443, spec §4).

Thin: parse input, call `services.questions`, respond. Mounted on the ONE board blueprint
(never a second app). The scope-INDEPENDENT operations (answer, undo, reopen, preview, and
the scope-guarded original file/eml serving) live here ONCE — a `qid`/`mid` carries no
scope, so a per-scope copy of them would be exactly the duplication `board.md` forbids;
`scope` is a query-param on the LIST only. `questions_dl.py` carries the DL card-affordance
descriptor (the genuinely scope-specific part), merged with this module's ORDERS one into
the list response so `tab-questions.js` renders each kind's buttons from data, not hardcode.

Answer/undo DELEGATE to `questions_api` — the exact `_answer_dispatch`/`_undo_dispatch`
callables `httpapi_orders_questions.register` returned — passing `allowed_kinds=None`
(unrestricted): `board_gate` already authorized the session and spec §6 makes the TAB, not
the key, decide scope. No business logic is re-implemented here.
"""
from __future__ import annotations

from flask import jsonify, request, send_file

from . import questions_dl
from .auth import actor
from .services import questions

# ORDERS-scope card affordances (beyond the offered candidates), by kind. `op` is what
# `tab-questions.js` maps to an answer body; `label` is the button text. Candidates + the
# free "iné číslo položky" input are rendered generically by the JS.
ORDERS_CARD_ACTIONS: dict[str, list[dict]] = {
    "item": [{"op": "new_product", "label": "➕ Nová karta"},
             {"op": "manual", "label": "Vyriešené ručne"}],
    "customer": [{"op": "new_customer", "label": "➕ Nový zákazník"},
                 {"op": "unknown_customer", "label": "Neviem, kto to je"},
                 {"op": "not_order", "label": "Toto nie je objednávka"}],
    "mail": [{"op": "mail_not_order", "label": "Toto nie je objednávka"},
             {"op": "mail_manual", "label": "Je to objednávka — vybavím ručne"}],
    "date": [],
    "line": [{"op": "line_keep", "label": "Patrí do objednávky"},
             {"op": "line_drop", "label": "Nepatrí do objednávky"}],
}

_CARD_ACTIONS: dict[str, dict[str, list[dict]]] = {
    "orders": ORDERS_CARD_ACTIONS,
    "dl": questions_dl.DL_CARD_ACTIONS,
}


def register(bp, deps, questions_api) -> None:
    # `questions_api` is the {"answer","undo"} dispatch that httpapi_orders_questions.register
    # returned — required (the board answer/undo tabs DELEGATE to it, never reimplement it).
    if not questions_api:
        raise RuntimeError("board questions tab needs the orders questions dispatch API")
    answer_dispatch = questions_api["answer"]
    undo_dispatch = questions_api["undo"]

    @bp.get("/api/board/questions")
    def board_questions():
        scope = request.args.get("scope", "orders")
        status = request.args.get("status", "open")
        q = request.args.get("q", "").strip()
        try:
            with deps.db() as c:
                items = questions.list_questions(c, scope=scope, status=status, q=q)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(items=items, meta={
            "scope": scope, "status": status,
            "card_actions": _CARD_ACTIONS.get(scope, {}),
        })

    @bp.post("/api/board/questions/<int:qid>/answer")
    def board_answer(qid: int):
        # Delegate to the SAME dispatch the legacy /api/orders endpoint uses; unrestricted
        # kinds because the board's own gate already authorized the session (§6).
        return answer_dispatch(qid, allowed_kinds=None)

    @bp.post("/api/board/questions/<int:qid>/undo")
    def board_undo(qid: int):
        return undo_dispatch(qid, allowed_kinds=None)

    @bp.post("/api/board/questions/<int:qid>/reopen")
    def board_reopen(qid: int):
        with deps.db() as c:
            res = questions.reopen(c, deps.cfg, qid, actor())
        if res is None:
            return jsonify(error="otázka neexistuje"), 404
        if res.get("error") == "not_expired":
            return jsonify(error="znovu otvoriť možno iba expirovanú otázku"), 409
        return jsonify(ok=True, question=res["question"])

    @bp.get("/api/board/questions/<int:qid>/preview")
    def board_preview(qid: int):
        with deps.db() as c:
            pv = questions.preview(c, deps.data_dir, qid)
        if pv is None:
            return jsonify(error="otázka neexistuje"), 404
        return jsonify(pv)

    @bp.get("/api/board/files/<mid>/<int:idx>")
    def board_file(mid: str, idx: int):
        with deps.db() as c:
            path = questions.file_path(c, deps.data_dir, mid, idx)
        if path is None:
            return jsonify(error="súbor nie je dostupný"), 404
        return send_file(path)

    @bp.get("/api/board/eml/<mid>")
    def board_eml(mid: str):
        with deps.db() as c:
            path = questions.eml_path(c, deps.data_dir, mid)
        if path is None:
            return jsonify(error="e-mail nie je dostupný"), 404
        return send_file(path, mimetype="message/rfc822")
