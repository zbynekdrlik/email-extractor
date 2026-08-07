"""DL item-match history (#200 F1) — item_memory's sibling for delivery notes.

Same shape as app/orders/memory.py's `item_memory` (db.py:397-410), keyed by SUPPLIER
instead of customer, with one structural difference: the `cnt` column.

R66 (the delivery-notes matching rule this table exists to eventually serve) resolves
a mixed history by taking the newest card's GTIN only when it carries >= 60% of ALL
deliveries, WEIGHTED BY the n8n table's own per-row `cnt` field — a raw delivery count
that must be preserved verbatim. That is a genuinely different semantics from
`item_memory.resolve()`, which counts DISTINCT DELIVERY DAYS instead (a deliberate fix
for a DIFFERENT n8n bug — see that module's own docstring: a seed row's raw `cnt` was
once misread as "18 deliveries" there). Conflating the two by bolting a nullable `cnt`
onto `item_memory` would risk reintroducing exactly the bug `item_memory` was built to
fix, so this is a dedicated table.

The n8n Data Table this replaces ("dodacie_pamat_poloziek", MBCwHVhzsKjbQkVl) has no
unique key either — R66 documents its own dedup rule for that: duplicate rows are the
same underlying record when (gtin, day, cnt) match. This table's UNIQUE constraint
enforces that identity directly, so no JS-style re-dedup is ever needed on read.

Resolution (R66's actual matching ladder — unanimous/majority/silent) is NOT
implemented here. This is the foundation phase (#200); only storage and the one-shot
n8n import exist yet. A later phase adds the `resolve()` counterpart, mirroring
`memory.resolve()`.
"""
from __future__ import annotations

import logging

from .memory import item_key  # same normalization — R66 keys on EXACT wording incl. gramáž

log = logging.getLogger("orders.dl_memory")


def remember(conn, supplier_ean: str, item: str, gtin: str, card: str,
             delivered_on, cnt: int = 1, source: str = "ship") -> bool:
    """Record one delivery (or one imported n8n history row). Returns False when this
    exact (supplier, wording, gtin, day, cnt) is already known.
    """
    key = item_key(item)
    if not (supplier_ean and key and gtin):
        return False
    row = conn.execute(
        """INSERT INTO dl_item_memory
               (supplier_ean, item_key, item_raw, gtin, card, delivered_on, cnt, source)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (supplier_ean, item_key, gtin, delivered_on, cnt) DO NOTHING
           RETURNING id""",
        (str(supplier_ean), key, str(item), str(gtin), card or "", delivered_on,
         int(cnt or 1), source),
    ).fetchone()
    return row is not None


def import_n8n_rows(conn, rows: list[dict]) -> int:
    """One-off import of the n8n Data Table `dodacie_pamat_poloziek` (MBCwHVhzsKjbQkVl)
    export — rows with cust/item/gtin/card/at/src/cnt. Mirrors
    `memory.import_n8n_rows` exactly, plus carrying `cnt` through instead of
    discarding it (that field is the whole reason this table exists separately —
    see the module docstring). Returns the number of rows actually stored.

    The source table's `at` is a full timestamp; several rows of one shipment differ
    only by time and collapse into one delivery day here, same as `memory.py`'s import.
    """
    stored = 0
    for r in rows:
        day = str(r.get("at") or "")[:10]
        if not day:
            continue
        if remember(conn, str(r.get("cust") or ""), str(r.get("item") or ""),
                    str(r.get("gtin") or ""), str(r.get("card") or ""),
                    delivered_on=day, cnt=int(r.get("cnt") or 1),
                    source=str(r.get("src") or "n8n")):
            stored += 1
    log.info("dl item memory: imported %d of %d n8n rows", stored, len(rows))
    return stored
