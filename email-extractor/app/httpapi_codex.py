"""`POST /api/codex/orders` (#342) + `POST /api/codex/cards` (#467) — the machine endpoints the
codex-bridge push tools write to.

`tools/codex_orders_push.py` (on the dev/ERP box, next to the codex-bridge DuckDB) reads
order headers read-only and POSTs a compact JSON batch here on its own systemd timer. Auth
is the EXISTING static machine token (`cfg.api_token`, `X-Token` header) — the same pattern
`httpapi_files.py` already uses for n8n's file endpoints, extended to a POST (never a new
auth scheme). No open-by-default: an add-on with no `api_token` configured rejects every
request. Idempotent — the body's orders upsert by order number, so a re-run of the same
push is a harmless no-op.
"""
from __future__ import annotations

import hmac

from flask import Flask, jsonify, request

from .httpapi_common import Deps
from .orders import codex_cards, codex_orders

# #467: the cards push is one ~1 MB body (~6k rows); anything past this is not a CODEX list.
MAX_CARDS_BODY = 16 * 1024 * 1024


def register(app: Flask, deps: Deps) -> None:
    def _token_ok(allow_query: bool = True) -> bool:
        # constant-time compare; the cards replace takes the header only (a URL token ends up
        # in proxy/access logs, and that endpoint swaps a whole table)
        tok = request.headers.get("X-Token") or (
            request.args.get("token") if allow_query else None) or ""
        want = deps.cfg.api_token or ""
        return bool(want) and hmac.compare_digest(tok.encode(), want.encode())

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

    @app.post("/api/codex/cards")
    def codex_cards_replace():
        # #467: the FULL CODEX stock-card list (`tools/codex_cards_push.py`). Same machine-token
        # guard as /api/codex/orders. The body REPLACES the list atomically — a code that left
        # CODEX must leave here too; an empty or drastically smaller push is refused (a
        # half-loaded ETL snapshot would otherwise hold every delivery note), `?force=1`
        # overrides the shrink guard for a genuine mass removal.
        if not _token_ok(allow_query=False):
            return jsonify(error="forbidden"), 403
        if (request.content_length or 0) > MAX_CARDS_BODY:
            return jsonify(error="body too large"), 413
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("cards"), list):
            return jsonify(error="body must be {\"cards\": [...]}"), 400
        force = request.args.get("force") in ("1", "true", "yes")
        try:
            with deps.db() as c:
                res = codex_cards.replace_cards(c, payload["cards"],
                                                source_as_of=payload.get("source_as_of"),
                                                force=force)
        except codex_cards.ReplaceRefused as e:
            return jsonify(error=str(e)), e.status
        return jsonify(rows=res["rows"], codes=res["codes"],
                       received=len(payload["cards"])), 200
