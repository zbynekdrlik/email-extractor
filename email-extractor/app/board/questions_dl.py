"""DL-scope (Otázky sklad = dodacie listy) presentation for the unified nástenka (#443).

The DL question tab shows `dl_item`/`dl_supplier` questions, whose card affordances differ
from the orders kinds: a DL item can be shipped WITHOUT a card (#365 ship_without) or the
whole mail marked „Netýka sa skladu" (#307), and either DL kind can be deferred with
„Neviem" (#305). This is the single source of that scope-specific button set — consumed by
`questions_orders.register` (merged into the list response so `tab-questions.js` renders
each kind's buttons from data) and mirrors the ORDERS_CARD_ACTIONS structure exactly.

The scope→kind partition itself (`DL_KINDS`) is owned by `httpapi_security` and re-used via
`services.questions.SCOPE_KINDS` — never re-derived here.
"""
from __future__ import annotations

# `op` is what `tab-questions.js` maps to an answer body; `label` is the button text.
# Offered candidates + the free "iné číslo položky" input are rendered generically by the JS.
DL_CARD_ACTIONS: dict[str, list[dict]] = {
    "dl_item": [{"op": "new_item", "label": "➕ Nová karta"},
                {"op": "ship_without", "label": "Nemá kartu — pošli bez tejto položky"},
                {"op": "not_warehouse", "label": "Netýka sa skladu"},
                {"op": "dl_unknown", "label": "Neviem"}],
    "dl_supplier": [{"op": "new_supplier", "label": "➕ Nový dodávateľ"},
                    {"op": "not_warehouse", "label": "Netýka sa skladu"},
                    {"op": "dl_unknown", "label": "Neviem"}],
}
