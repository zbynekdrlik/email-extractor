"""Static-orders SHADOW-mode worker (#132): wires `static_parse` + `static_edi` (#131) into
the worker loop for `category='static_orders'`, for comparison against n8n's own "Static auto
orders" workflow (`O8IYhUESjaWmPMTI`). Same shape `worker.py` already uses for `ai_orders`
shadow mode — see that module's docstring — with ONE deliberate difference: static orders are
1287/month (8x the AI-orders volume) on live production, so a direct cutover is out of scope
here (#133 decides that later). This module can therefore ONLY ever shadow:

- `static_orders_engine=n8n`, `static_orders_shadow=false` (DEFAULT) — completely inert.
- `static_orders_engine=n8n`, `static_orders_shadow=true` — parses with `static_parse` and
  builds the EDI with `static_edi` for comparison only: **claims nothing and marks nothing**,
  exactly like `worker.py`'s `_peek_for_shadow` — the message must stay exactly as n8n expects
  to find it. Results land in `order_runs(shadow=true)` / `order_items` only.
- `static_orders_engine=python` — NOT implemented. #133 (the actual cutover) is a separate,
  not-yet-decided ticket; flipping this option must fail loudly in the log and change nothing,
  never crash the add-on's loop and never look like a processed message (same contract
  `worker.tick` used while ITS pipeline was pending, #61).

No LLM is involved (static parsing is pure regex + a deterministic EDI writer), so unlike
`ai_orders` shadow this costs nothing per run — the day bound below exists anyway, for the same
reason `worker._peek_for_shadow`'s exists: comparison against n8n on CURRENT mail, never a
replay of the whole archive on every tick.
"""
from __future__ import annotations

import logging

from . import snapshot, static_ean, static_edi, static_parse, worker

log = logging.getLogger("orders.static_worker")

CATEGORY = "static_orders"
SHADOW_DAYS = 3

# Reused as-is (worker.py's engine validation has no ai_orders-specific coupling): "n8n" and
# "python" are the only recognized values, anything else raises.
resolve_engine = worker.resolve_engine

# The extractor's three "cannot proceed" exceptions all mean the same thing for shadow
# purposes: report a reviewable run, never crash the tick.
_PARSE_ERRORS = (static_parse.MissingInputText, static_parse.PhotoOrderNeedsVision,
                 static_parse.OrderExtractionError)


def _peek_for_shadow(conn, days: int = SHADOW_DAYS) -> dict | None:
    """Pick a `static_orders` message the shadow run has not seen yet, WITHOUT touching its
    state — same shape as `worker._peek_for_shadow`, plus `has_attachments` (`static_parse`
    needs it for the photo-only guard, which `worker._as_message` never had to carry)."""
    row = conn.execute(
        """SELECT m.message_id, m.subject, m.from_addr, m.from_name,
                  m.combined_text, m.body_text, m.has_attachments
             FROM messages m
            WHERE m.category = %s
              AND m.created_at > now() - make_interval(days => %s)
              AND NOT EXISTS (SELECT 1 FROM order_runs r
                               WHERE r.message_id = m.message_id AND r.shadow)
            ORDER BY m.created_at DESC LIMIT 1""",
        (CATEGORY, max(1, int(days or SHADOW_DAYS)))).fetchone()
    if not row:
        return None
    return {"message_id": row[0], "subject": row[1] or "", "from_addr": row[2] or "",
            "from_name": row[3] or "", "combined_text": row[4] or row[5] or "",
            "has_attachments": bool(row[6])}


def _items_with_ean(items: list[dict], catalog: list[dict]) -> list[dict]:
    out = []
    for it in items:
        gtin = static_ean.resolve_ean(it, catalog, code_map=static_edi.PRODUCT_EAN_BY_CODE,
                                      name_map=static_edi.PRODUCT_EAN_BY_NAME)
        out.append({"name": it.get("description", ""), "quantity": it.get("quantity"),
                    "unit": it.get("unit", "ks"), "gtin": gtin})
    return out


def run_shadow(conn, message: dict, snapshot_id: int) -> dict:
    """Parse + build for ONE message. Never uploads, never posts, never writes message
    state — the caller (`tick`) only ever records the result into `order_runs`/`order_items`.
    """
    text = message.get("combined_text", "")
    try:
        parsed = static_parse.parse_static_order(
            text, has_attachments=bool(message.get("has_attachments")))
    except _PARSE_ERRORS as e:
        return {"status": "review", "items": [], "reject_reason": str(e)}

    catalog = snapshot.load_catalog(conn, snapshot_id)

    if parsed.get("skip"):
        return {"status": "review", "items": [], "reject_reason": parsed.get("skipReason", ""),
                "partner": parsed.get("partner", "")}

    items = _items_with_ean(parsed.get("items") or [], catalog)

    try:
        built = static_edi.build(parsed, catalog)
    except ValueError as e:
        return {"status": "review", "items": items, "reject_reason": str(e),
                "partner": parsed.get("partner", "")}

    status = "ok" if built.items_skipped == 0 else "partial"
    return {"status": status, "items": items, "partner": parsed.get("partner", ""),
            "delivery_date": parsed.get("deliveryDate", ""),
            "order_number": parsed.get("fullOrderNumber", ""),
            "edi_filename": built.filename, "edi_preview": built.content,
            "items_with_ean": built.items_with_ean, "items_skipped": built.items_skipped}


def tick(conn, cfg) -> int:
    """Process at most one `static_orders` message, SHADOW ONLY. Returns 0 or 1."""
    engine = resolve_engine(getattr(cfg, "static_orders_engine", "n8n"))
    shadow = bool(getattr(cfg, "static_orders_shadow", False))
    if not shadow:
        return 0
    if engine == "python":
        # #133 (the real cutover) is not built yet — fail loudly in the log, do nothing,
        # never crash the add-on's loop. Same contract worker.tick used pre-#61.
        log.error("static_orders_engine=python is not implemented yet (#133) — "
                  "shadow=%s requested, doing nothing", shadow)
        return 0

    snapshot_id = snapshot.latest_snapshot_id(conn)
    if not snapshot_id:
        log.warning("no catalog snapshot yet — static shadow worker idle")
        return 0

    message = _peek_for_shadow(conn, getattr(cfg, "static_orders_shadow_days", SHADOW_DAYS))
    if not message:
        return 0

    run_id = worker._start_run(conn, message["message_id"], snapshot_id, shadow=True)
    result = run_shadow(conn, message, snapshot_id)
    worker._finish_run(conn, run_id, result.get("status", "ok"), result)
    return 1
