"""One-off: import the n8n item-memory rows into `item_memory` (#63).

Usage inside the add-on container:

    python -m app.orders.memory_import /data/item_memory_n8n.json

The JSON is the raw export of the n8n Data Table `mX1EIECccDfTa9ab`
(rows with cust/item/gtin/card/at/src). Safe to re-run: `remember()` is keyed on
(customer, wording, card, day), so a second run stores nothing.
"""
from __future__ import annotations

import json
import logging
import sys

from .. import config, db
from . import memory


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print(__doc__)
        return 2
    with open(args[0], encoding="utf-8") as fh:
        rows = json.load(fh)
    cfg = config.Config.load()
    conn = db.connect(cfg.pg_dsn)
    db.init_schema(conn)
    stored = memory.import_n8n_rows(conn, rows)
    total = conn.execute("SELECT count(*) FROM item_memory").fetchone()[0]
    print(f"imported {stored} new row(s); item_memory now holds {total}")
    return 0


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
