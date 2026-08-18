"""`POST /api/codex/orders` — the machine endpoint the codex-bridge push tool writes to (#342).

`tools/codex_orders_push.py` (on the dev/ERP box, next to the codex-bridge DuckDB) reads
order headers read-only and POSTs a compact JSON batch here on its own systemd timer. Auth
is the EXISTING static machine token (`cfg.api_token`, `X-Token` header) — the same pattern
`httpapi_files.py` already uses for n8n's file endpoints, extended to a POST (never a new
auth scheme). No open-by-default: an add-on with no `api_token` configured rejects every
request. Idempotent — the body's orders upsert by order number, so a re-run of the same
push is a harmless no-op.
"""
from __future__ import annotations

from flask import Flask, jsonify, request

from .httpapi_common import Deps
from .orders import codex_orders


def register(app: Flask, deps: Deps) -> None:
    def _token_ok() -> bool:
        tok = request.args.get("token") or request.headers.get("X-Token")
        return bool(deps.cfg.api_token) and tok == deps.cfg.api_token

    @app.post("/api/codex/orders")
    def codex_orders_upsert():
        # Machine-only: a valid token is required (no session fallback — the push tool is
        # never a logged-in browser). `before_request`'s _gate lets /api/codex/* through
        # so this in-route check is the sole guard, exactly like /files' own _auth().
        if not _token_ok():
            return jsonify(error="forbidden"), 403
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("orders"), list):
            return jsonify(error="body must be {\"orders\": [...]}"), 400
        clean = []
        for o in payload["orders"]:
            if not isinstance(o, dict):
                continue
            if o.get("order_number") is None or not o.get("customer_ean"):
                continue
            clean.append(o)
        with deps.db() as c:
            n = codex_orders.upsert_orders(c, clean)
        return jsonify(upserted=n, received=len(payload["orders"])), 200
