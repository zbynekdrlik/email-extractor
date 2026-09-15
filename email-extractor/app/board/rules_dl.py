"""DL-scope (Naučené sklad) editor-field descriptors for the unified nástenka (#447).

The DL „naučené" tab shows two kinds — `dl_alias` (a per-supplier `dl_item_memory` wording→
card mapping) and `supplier` (a `dl_supplier_memory` sender-email→EAN mapping). This is the
single source of the scope-specific EDITOR descriptor, consumed by `rules_orders.register`
(merged into the list response `meta.edit` so `tab-rules.js` builds each kind's inline editor
from data, not hardcode). A data module, no routes — mirrors `products_dl.py`/`questions_dl.py`.

The kind→table mapping + the list/update/delete logic itself live in `services/rules.py` +
`services/rules_edit.py` — never re-derived here.
"""
from __future__ import annotations

# The alias editor — shared by orders (alias/global) AND dl (dl_alias): a wording that points
# at a card (gtin + display name). Each `key` is BOTH the input id and the update-body key.
ALIAS_FIELDS: list[dict] = [
    {"key": "wording", "label": "Znenie (alias)"},
    {"key": "gtin", "label": "Číslo položky (GTIN)"},
    {"key": "card", "label": "Názov karty"},
]

# kind -> its inline editor fields.
DL_EDIT: dict[str, list[dict]] = {
    "dl_alias": ALIAS_FIELDS,
    "supplier": [
        {"key": "ean", "label": "EAN dodávateľa"},
        {"key": "name", "label": "Názov dodávateľa"},
    ],
}
