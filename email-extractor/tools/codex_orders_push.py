#!/usr/bin/env python3
"""Push CODEX order headers from the codex-bridge DuckDB to the add-on (#342).

Runs on the dev/ERP box (where `/var/lib/codex-bridge/codex.duckdb` lives) on its OWN
systemd timer, ~20-30 min after each codex-bridge ETL run (14:15 / 18:00 Prague). It reads
the DuckDB **read-only**, pulls the last ~7 days of order HEADERS, bridges each order's
customer number (NICO) to its EDI EAN via `raw.firma` (NICO → AEDIEAN), and POSTs a compact
JSON batch to `POST /api/codex/orders` (X-Token auth). The add-on stores it as EVIDENCE that
an order was entered manually in CODEX, so it can auto-resolve the "je toto objednávka?"
board question. Idempotent: the endpoint upserts by order number, so re-runs are harmless.

CI-testable WITHOUT duckdb/requests installed: both are lazy-imported INSIDE the functions
that need them, and `run()` takes a `query`/`poster` dependency-injection seam so tests feed
synthetic rows and capture the POST. `build_orders()` — the normalization core — is pure.

DuckDB columns (verified live 2026-08-18): `raw.sp002` order headers (ICDOBJEDNAV order
number, NICO customer number, DATVYST issue date ~97%, DATDODAV delivery date ~95%; never
DODTERMIN), line aggregate from `meta.sp003_dedup`, `raw.firma` for NICO→AEDIEAN + name.
The firma dedup keeps ONE EAN per NICO (`MAX`) — a rare multi-branch NICO simply may not
match a specific branch's card, which is a SAFE miss (the board question just stays open).

Config (env / EnvironmentFile, so the token is never committed):
  CODEX_PUSH_URL    e.g. http://<addon-host>:8099/api/codex/orders
  CODEX_PUSH_TOKEN  the add-on's api_token
  CODEX_PUSH_DAYS   lookback window (default 7)
  CODEX_DUCKDB_PATH default /var/lib/codex-bridge/codex.duckdb
"""
from __future__ import annotations

import argparse
import os
import sys

DEFAULT_DB_PATH = "/var/lib/codex-bridge/codex.duckdb"
DEFAULT_DAYS = 7
DEFAULT_CHUNK = 500
DEFAULT_TIMEOUT = 30

# One row per order number: dedup firma to one EAN+name per NICO (MAX/ANY_VALUE), collapse
# any duplicate sp002 header rows per order, and left-join the line count. Only orders whose
# NICO resolves to a non-empty EDI EAN are pushed (the sweep matches on that EAN). Parameter
# is the lookback window in days. NEVER DODTERMIN (only ~22% populated).
_SQL = """
WITH firm AS (
    SELECT NICO, MAX(AEDIEAN) AS ean, ANY_VALUE(ANAZORG) AS name
      FROM raw.firma
     WHERE AEDIEAN IS NOT NULL AND AEDIEAN <> ''
     GROUP BY NICO
),
hdr AS (
    SELECT ICDOBJEDNAV AS order_number, ANY_VALUE(NICO) AS nico,
           MAX(DATVYST) AS issue_date, MAX(DATDODAV) AS delivery_date
      FROM raw.sp002
     WHERE DATVYST >= (current_date - (CAST(? AS INTEGER) * INTERVAL 1 DAY))
       AND NICO IS NOT NULL AND NICO <> 0
     GROUP BY ICDOBJEDNAV
),
lines AS (
    SELECT ICDOBJEDNAV AS order_number, count(*) AS line_count
      FROM meta.sp003_dedup GROUP BY ICDOBJEDNAV
)
SELECT CAST(h.order_number AS BIGINT) AS order_number,
       CAST(h.nico AS BIGINT)         AS customer_nico,
       f.ean                          AS customer_ean,
       f.name                         AS customer_name,
       h.issue_date                   AS issue_date,
       h.delivery_date                AS delivery_date,
       COALESCE(l.line_count, 0)      AS line_count
  FROM hdr h
  JOIN firm f ON f.NICO = h.nico
  LEFT JOIN lines l ON l.order_number = h.order_number
 WHERE f.ean IS NOT NULL AND f.ean <> ''
 ORDER BY h.issue_date DESC
"""


def query_duckdb(db_path: str, days: int) -> list[dict]:
    """Read the order headers from the codex-bridge DuckDB, read-only. Lazy-imports duckdb
    so the module imports (and its pure functions test) without it. Returns list of dicts."""
    import duckdb  # noqa: PLC0415 - lazy on purpose (CI has no duckdb)

    con = duckdb.connect(db_path, read_only=True)
    try:
        cur = con.execute(_SQL, [int(days)])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
    finally:
        con.close()


def _iso(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def build_orders(rows: list[dict]) -> list[dict]:
    """Normalize raw DuckDB rows into JSON-safe payload dicts (pure, the testable core).

    Skips any row missing the two identity fields the endpoint requires (order_number,
    customer_ean) — never sends a row the add-on would drop anyway. Dates become ISO
    strings; numbers become plain ints (DuckDB hands back numpy/date types otherwise)."""
    out = []
    for r in rows:
        order_number = r.get("order_number")
        customer_ean = r.get("customer_ean")
        if order_number is None or not str(customer_ean or "").strip():
            continue
        nico = r.get("customer_nico")
        line_count = r.get("line_count")
        out.append({
            "order_number": int(order_number),
            "customer_nico": int(nico) if nico is not None else None,
            "customer_ean": str(customer_ean).strip(),
            "customer_name": str(r.get("customer_name") or "")[:200],
            "issue_date": _iso(r.get("issue_date")),
            "delivery_date": _iso(r.get("delivery_date")),
            "line_count": int(line_count) if line_count is not None else None,
        })
    return out


def _requests_post(url: str, headers: dict, body: dict) -> dict:
    import requests  # noqa: PLC0415 - lazy on purpose (CI has no requests need here)

    resp = requests.post(url, headers=headers, json=body, timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def post_orders(url: str, token: str, orders: list[dict], poster=None,
                chunk: int = DEFAULT_CHUNK) -> int:
    """POST the orders to the add-on in bounded chunks. `poster(url, headers, body)` is the
    DI seam (tests capture it); the default uses requests. Returns total rows the add-on
    reported upserted."""
    poster = poster or _requests_post
    headers = {"X-Token": token, "Content-Type": "application/json"}
    total = 0
    for i in range(0, len(orders), max(1, chunk)):
        batch = orders[i:i + chunk]
        resp = poster(url, headers, {"orders": batch}) or {}
        total += int(resp.get("upserted", 0) or 0)
    return total


def run(url: str, token: str, days: int = DEFAULT_DAYS, db_path: str = DEFAULT_DB_PATH,
        query=None, poster=None) -> dict:
    """Fetch → normalize → push. `query()`/`poster(...)` are injectable for tests."""
    query = query or (lambda: query_duckdb(db_path, days))
    rows = query()
    orders = build_orders(rows)
    upserted = post_orders(url, token, orders, poster=poster)
    return {"fetched": len(rows), "orders": len(orders), "upserted": upserted}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Push CODEX order headers to the add-on (#342)")
    ap.add_argument("--url", default=os.environ.get("CODEX_PUSH_URL", ""))
    ap.add_argument("--token", default=os.environ.get("CODEX_PUSH_TOKEN", ""))
    ap.add_argument("--days", type=int,
                    default=int(os.environ.get("CODEX_PUSH_DAYS", DEFAULT_DAYS)))
    ap.add_argument("--db", default=os.environ.get("CODEX_DUCKDB_PATH", DEFAULT_DB_PATH))
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch + normalize, print counts, POST nothing")
    args = ap.parse_args(argv)

    if args.dry_run:
        orders = build_orders(query_duckdb(args.db, args.days))
        print(f"dry-run: orders={len(orders)} (window={args.days}d, db={args.db})")
        return 0
    if not args.url or not args.token:
        print("error: CODEX_PUSH_URL and CODEX_PUSH_TOKEN (or --url/--token) are required",
              file=sys.stderr)
        return 2
    res = run(args.url, args.token, days=args.days, db_path=args.db)
    print(f"pushed: fetched={res['fetched']} orders={res['orders']} "
          f"upserted={res['upserted']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
