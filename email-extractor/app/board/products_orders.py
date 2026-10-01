"""Route layer for the two product tabs of the unified nástenka (#445, spec §4).

Thin: parse input, call `services.catalog`, respond. Mounted on the ONE board blueprint
(never a second app). The scope (orders|dl) is a `?scope=` query-param on every route — the
TAB decides scope, never the key (spec §6), and `board_gate` already authorized the session,
so the orders `sklad` key reaches the DL products too (the whole point of lane 4). The card
FIELD descriptor differs per scope: this module holds ORDERS_FIELDS, `products_dl.py` holds
the DL one, merged into the list response `meta` so `tab-products.js` builds each scope's
editor from data. No business logic is re-implemented here — update/delete delegate to the
snapshot machinery, alias add/remove to the memory write paths (via the service). #477: the
tabs never CREATE a card (403); the CODEX picker list `/api/board/codex-cards` lives here too
(`card_guard.codex_choices`), the pick itself is a question answer.
"""
from __future__ import annotations

from flask import jsonify, request

from ..orders import card_guard, codex_cards
from . import products_dl
from .auth import actor
from .services import catalog, catalog_aliases

# ORDERS editor fields beyond the readonly „číslo položky" (gtin).
ORDERS_FIELDS: list[dict] = [
    {"key": "name", "label": "Názov", "required": True},
    # keyed as an orders card carries it (`alias`): keyed `doplnok`, the editor opened empty
    # and every save — a rename included — wiped the card's alias (#478 review 43)
    {"key": "alias", "label": "Doplnok / aliasy (oddelené čiarkou)"},
]
# Orders aliases can be global (no EAN) or per-customer (EAN present) — EAN optional.
ORDERS_ALIAS = {"per_customer": True, "ean_required": False, "ean_label": "EAN zákazníka"}

_FIELDS = {"orders": ORDERS_FIELDS, "dl": products_dl.DL_FIELDS}
_ALIAS = {"orders": ORDERS_ALIAS, "dl": products_dl.DL_ALIAS}


def register(bp, deps) -> None:
    @bp.get("/api/board/products")
    def board_products():
        scope = request.args.get("scope", "orders")
        q = request.args.get("q", "").strip()
        try:
            page = int(request.args.get("page", 0))
        except (TypeError, ValueError):
            page = 0
        try:
            with deps.db() as c:
                res = catalog.list_products(c, scope=scope, q=q, page=page,
                                            codex=request.args.get("codex", ""))
        except ValueError as e:
            return jsonify(error=str(e)), 400
        res["meta"] = {"scope": scope, "page": res.pop("page"),
                       "page_size": res.pop("page_size"), "total": res.pop("total"),
                       "has_more": res.pop("has_more"), "codex": res.pop("codex"),
                       "fields": _FIELDS.get(scope, []), "alias": _ALIAS.get(scope, {})}
        return jsonify(**res)

    @bp.get("/api/board/products/<gtin>")
    def board_product_detail(gtin: str):
        scope = request.args.get("scope", "orders")
        try:
            with deps.db() as c:
                d = catalog.card_detail(c, scope, gtin)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        if d is None:
            return jsonify(error="karta neexistuje"), 404
        return jsonify(d)

    @bp.post("/api/board/products")
    def board_product_upsert():
        scope = request.args.get("scope", "orders")
        body = request.get_json(silent=True) or {}
        try:
            with deps.db() as c:
                res = catalog.upsert(c, scope, body, actor())
        except ValueError as e:
            return jsonify(error=str(e)), 400
        except codex_cards.CardRefused as e:   # #477: a typed new card (403); #467: CODEX (409)
            return jsonify(**e.payload), e.status
        return jsonify(ok=True, **res)

    @bp.get("/api/board/codex-cards")
    def board_codex_cards():
        # #477: the „Vybrať kartu z CODEXu" picker list — the ONLY way a card enters the
        # catalog is picking one of these on a question (`codex_card` answer body).
        try:
            with deps.db() as c:
                res = card_guard.codex_choices(c, request.args.get("scope", "dl"),
                                               request.args.get("q", ""))
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(**res)

    @bp.delete("/api/board/products/<gtin>")
    def board_product_delete(gtin: str):
        scope = request.args.get("scope", "orders")
        try:
            with deps.db() as c:
                ok = catalog.delete(c, scope, gtin, actor())
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True) if ok else (jsonify(error="karta neexistuje"), 404)

    @bp.post("/api/board/products/<gtin>/aliases")
    def board_product_add_alias(gtin: str):
        scope = request.args.get("scope", "orders")
        body = request.get_json(silent=True) or {}
        try:
            with deps.db() as c:
                res = catalog_aliases.add_alias(c, scope, gtin, body.get("wording", ""),
                                                body.get("ean", ""), actor())
        except ValueError as e:
            return jsonify(error=str(e)), 400
        if "error" in res:
            code = 409 if "priradené" in res["error"] else 400
            return jsonify(error=res["error"]), code
        return jsonify(ok=True, id=res["id"])

    @bp.delete("/api/board/products/<gtin>/aliases")
    def board_product_remove_alias(gtin: str):
        scope = request.args.get("scope", "orders")
        body = request.get_json(silent=True) or {}
        try:
            with deps.db() as c:
                ok = catalog_aliases.remove_alias(c, scope, str(body.get("alias_scope") or ""),
                                                  int(body.get("id") or 0), body.get("ean", ""),
                                                  actor())
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True) if ok else (jsonify(error="alias neexistuje"), 404)
