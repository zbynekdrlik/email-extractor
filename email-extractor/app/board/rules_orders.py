"""Route layer for the two „Naučené" tabs of the unified nástenka (#447, spec §4).

Thin: parse input, call `services.rules` (read) / `services.rules_edit` (write), respond.
Mounted on the ONE board blueprint (never a second app). Both tabs share these routes; the
KIND (`?kind=`) selects which „naučené" table, and each kind belongs to exactly one scope, so
update/delete derive the scope from the kind server-side (`rules.scope_of`). This module holds
the ORDERS editor descriptor; `rules_dl.py` holds the DL one — merged into the list response
`meta.edit` so `tab-rules.js` builds each kind's inline editor from data. No business logic is
re-implemented here (update/delete delegate to the engine write paths via the service).
"""
from __future__ import annotations

from flask import jsonify, request

from . import rules_dl
from .auth import actor
from .services import rules, rules_edit

# ORDERS editor fields per kind. Aliases (global) reuse `rules_dl.ALIAS_FIELDS`.
ORDERS_EDIT: dict[str, list[dict]] = {
    "mail": [
        {"key": "subject_key", "label": "Predmet (kľúč)"},
        {"key": "action", "label": "Akcia", "options": [
            {"value": "ignore", "label": "Ignorovať (nie je objednávka)"},
            {"value": "manual", "label": "Objednávka — vybaviť ručne"},
        ]},
    ],
    "alias": rules_dl.ALIAS_FIELDS,
    "global": rules_dl.ALIAS_FIELDS,
}
_EDIT: dict[str, list[dict]] = {**ORDERS_EDIT, **rules_dl.DL_EDIT}


def _kinds_meta(scope: str) -> list[dict]:
    return [{"slug": k, "label": rules.KIND_LABELS[k]} for k in rules.SCOPE_KINDS.get(scope, ())]


def register(bp, deps) -> None:
    @bp.get("/api/board/rules")
    def board_rules():
        scope = request.args.get("scope", "orders")
        kind = request.args.get("kind", "")
        q = request.args.get("q", "").strip()
        try:
            page = int(request.args.get("page", 0))
        except (TypeError, ValueError):
            page = 0
        try:
            with deps.db() as c:
                res = rules.list_rules(c, scope=scope, kind=kind, q=q, page=page)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        res["meta"] = {"scope": scope, "kind": kind, "page": res.pop("page"),
                       "page_size": res.pop("page_size"), "total": res.pop("total"),
                       "has_more": res.pop("has_more"),
                       "kinds": _kinds_meta(scope), "edit": _EDIT.get(kind, [])}
        return jsonify(**res)

    @bp.post("/api/board/rules/<kind>/<int:rid>")
    def board_rule_update(kind: str, rid: int):
        body = request.get_json(silent=True) or {}
        try:
            scope = rules.scope_of(kind)
            with deps.db() as c:
                ok = rules_edit.update(c, scope, kind, rid, body, actor())
        except ValueError as e:
            return jsonify(error=str(e)), 400
        except rules_edit.RuleCollision as e:
            return jsonify(error=str(e)), 409
        return jsonify(ok=True) if ok else (jsonify(error="pravidlo neexistuje"), 404)

    @bp.delete("/api/board/rules/<kind>/<int:rid>")
    def board_rule_delete(kind: str, rid: int):
        try:
            scope = rules.scope_of(kind)
            with deps.db() as c:
                ok = rules_edit.delete(c, scope, kind, rid, actor())
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True) if ok else (jsonify(error="pravidlo neexistuje"), 404)
