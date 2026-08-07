#!/usr/bin/env python3
"""One-off: import the n8n `dodacie_pamat_poloziek` Data Table export into
`dl_item_memory` (#200 F1).

Lives in scripts/, NOT app/orders/, deliberately: unlike app/orders/memory_import.py
(which ships inside the deployed add-on image and runs `python -m app.orders.
memory_import` from inside the container, per Dockerfile's `COPY app/ ./app/`),
this script is meant to run ONCE, from a checkout, at DL cutover time — not before.
`scripts/` is not copied into the Docker image, so it can never be run accidentally
by anything inside the running add-on.

Usage (from a checkout, e.g. dev1 — export PG_DSN first, same convention
.claude/rules/orders-corpus.md documents for running app/orders/eval_run.py outside
the container):

    export PG_DSN="postgresql://email:<pw>@<ha-host>:5432/email"
    python3 scripts/import_dl_item_memory.py /path/to/dodacie_pamat_poloziek_export.json

The JSON is the raw export of the n8n Data Table `MBCwHVhzsKjbQkVl`
(dodacie_pamat_poloziek) — rows with cust/item/gtin/card/at/src/cnt. Safe to re-run:
dl_memory.remember() is keyed on (supplier_ean, wording, gtin, day, cnt), so a second
run stores nothing new (see tests/test_dl_memory.py::
test_import_n8n_rows_is_idempotent_on_a_second_run).

DO NOT run this against the live add-on before the DL cutover phase — running it now
would only pre-populate history that nothing yet reads (dl_item_memory has no
resolve() rung wired into any pipeline in this foundation phase, #200).
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

# scripts/ is a sibling of app/, not a package under it — add the repo root (the
# parent of this file's directory) to sys.path so `from app import ...` resolves
# the same way it would inside the deployed container's WORKDIR /app.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db  # noqa: E402
from app.orders import dl_memory  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print(__doc__)
        return 2
    with open(args[0], encoding="utf-8") as fh:
        rows = json.load(fh)
    cfg = config.Config.load()
    if not cfg.pg_dsn:
        print("no Postgres DSN configured — export PG_DSN first (see this script's "
              "own docstring)", file=sys.stderr)
        return 2
    conn = db.connect(cfg.pg_dsn)
    db.init_schema(conn)
    stored = dl_memory.import_n8n_rows(conn, rows)
    total = conn.execute("SELECT count(*) FROM dl_item_memory").fetchone()[0]
    print(f"imported {stored} new row(s); dl_item_memory now holds {total}")
    return 0


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
