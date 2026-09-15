"""DL-scope (História dodacích listov) presentation descriptor for the unified nástenka (#448).

The history tabs share ONE route module (`history_orders.py`, `?scope=`) and ONE JS module —
only the column LABELS differ per scope (partner = supplier vs customer, doc = DL number vs
order/EDI). This is the single source of the DL labels, merged into the list response `meta`
so `tab-history.js` renders each scope's headers from data (mirrors `questions_dl.py`/
`products_dl.py`). The scope→category partition itself is owned by `services.history`.
"""
from __future__ import annotations

DL_LABELS: dict[str, str] = {
    "partner": "Dodávateľ",
    "doc": "Číslo dodacieho listu",
    "empty": "Žiadne dodacie listy v tomto filtri.",
}
