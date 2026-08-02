"""One-off migration (#104): import the sheet's `doplnok` column into `global_item_memory`
before that column stops being read into the matching ladder at all.

`catalog_snapshot.alias` (the `doplnok` cell) is a property of the CARD, not of any one
customer — the same case `global_item_memory` (#102, "Twister") already exists to model, so
the import lands there, not in `item_memory` (which needs a real `customer_ean`). See the
#104 design comment on the issue for the full reasoning.

Idempotent: `memory.add_global_alias`'s `ON CONFLICT (item_key) DO NOTHING` means running
this twice, or after the sheet gains new aliases, only ever adds what is genuinely new — and
a wording a HUMAN already taught (#88/#102) is never overwritten by a bulk sheet import,
first-teach-wins exactly like two humans teaching the same wording.

Run manually after deploy (same pattern as `memory_import.py`), not on every worker start:

    python -m app.orders.alias_migration <PG_DSN>
"""
from __future__ import annotations

import logging
import re

from . import memory, snapshot

log = logging.getLogger("orders.alias_migration")

_SPLIT_RE = re.compile(r"[,;/]+")
MIN_LEN = 4   # same floor as match.alias_parts() — a shorter token never fires there anyway


def split_alias(raw: str) -> list[str]:
    """Split one card's alias cell into individual wordings.

    Same delimiter and length floor as `match.alias_parts()`, but applied to the RAW text
    (not diacritics-folded) so the wording stored as `item_raw` stays human-readable on the
    /znalosti page — `memory.item_key()` folds it again on write, so matching is unaffected.
    """
    return [p.strip() for p in _SPLIT_RE.split(str(raw or "")) if len(p.strip()) >= MIN_LEN]


def migrate(conn, snapshot_id: int | None = None) -> dict:
    """Import every catalog alias into `global_item_memory(taught_by='sheet-import')`.

    Returns `{"cards": N, "wordings": N, "imported": N}` — cards and wordings scanned,
    `imported` counts only genuinely new mappings (excludes ones already taught, by a human
    or by an earlier run of this same migration).
    """
    sid = snapshot_id if snapshot_id is not None else snapshot.latest_snapshot_id(conn)
    if sid is None:
        log.warning("alias migration: no catalog snapshot to import from")
        return {"cards": 0, "wordings": 0, "imported": 0}
    catalog = snapshot.load_catalog(conn, sid)
    cards = wordings = imported = 0
    for card in catalog:
        parts = split_alias(card.get("alias", ""))
        if not parts:
            continue
        cards += 1
        for wording in parts:
            wordings += 1
            if memory.add_global_alias(conn, wording, card["gtin"], card["name"],
                                       by="sheet-import") is not None:
                imported += 1
    log.info("alias migration: %d cards, %d wordings scanned, %d newly imported",
             cards, wordings, imported)
    return {"cards": cards, "wordings": wordings, "imported": imported}


def main() -> None:
    import os
    import sys

    import psycopg

    logging.basicConfig(level=logging.INFO)
    dsn = os.environ.get("PG_DSN") or (sys.argv[1] if len(sys.argv) > 1 else None)
    if not dsn:
        print("usage: python -m app.orders.alias_migration <PG_DSN>  (or set PG_DSN)")
        raise SystemExit(2)
    with psycopg.connect(dsn, autocommit=True) as conn:
        result = migrate(conn)
    print(result)


if __name__ == "__main__":
    main()
