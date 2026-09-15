"""DL-scope (Produkty sklad = dodacie listy) card presentation for the unified nástenka (#445).

The DL product card carries MORE fields than an orders card (doplnok + hmotnosť/mass +
sklad + cena, R20 of `dl_snapshot`), and its aliases are per-SUPPLIER (`dl_item_memory`),
never global/per-customer. This is the single source of that scope-specific descriptor —
consumed by `products_orders.register` (merged into the list response `meta` so
`tab-products.js` builds each scope's card editor + alias form from data, not hardcode).
Mirrors `questions_dl.py`'s DL_CARD_ACTIONS structure exactly (a data module, no routes).

The scope→machinery mapping itself lives in `services/catalog.py` — never re-derived here.
"""
from __future__ import annotations

# Editor fields beyond the readonly „číslo položky" (gtin). `key` is the JSON body key +
# the catalog row key; `label` is the input label rendered by tab-products.js.
DL_FIELDS: list[dict] = [
    {"key": "name", "label": "Názov", "required": True},
    {"key": "doplnok", "label": "Doplnok / aliasy (oddelené čiarkou)"},
    {"key": "mass", "label": "Hmotnosť ks (kg)"},
    {"key": "sklad", "label": "Sklad (100 = sledované na kg)"},
    {"key": "cena", "label": "Cena (€/kg)"},
]

# DL memory is always per-supplier — the alias form REQUIRES an EAN (no global aliases here).
DL_ALIAS = {"per_customer": True, "ean_required": True, "ean_label": "EAN dodávateľa"}
