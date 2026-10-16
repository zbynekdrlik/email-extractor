#!/usr/bin/env python3
"""Push the CODEX customer cards from the codex-bridge DuckDB to the add-on (#493).

Runs on dev2 (where `/var/lib/codex-bridge/codex.duckdb` lives), started by the guarded push
round `codex_push_after_etl.py` after the other CODEX pushes, the moment the codex-bridge ETL
replaced that file (#485). It reads `raw.firma` **read-only** and POSTs every customer card that
carries an EDI EAN to `POST /api/codex/customers` (X-Token auth). The add-on REPLACES its copy
atomically and layers it under the board's own customer edits, so a shop the warehouse creates
in CODEX reaches the order engine with no second entry on the nástenka (the 2026-10-16 incident:
three of five shops existed only in CODEX and every order went to one card).

`raw.firma` (verified live 2026-10-16): several GENERATIONS per (NICO, IICODOPL) — `DDATVYPU` is
the extract date, the newest wins; one row per firm × branch (`IICODOPL`); `AEDIEAN` = the EDI
EAN, the same value our customer cards carry; `ANAZORG` name, `AOBCHODM` trade name, `AULICA`
street, `AOBEC` city, `APSC` zip, `AEMAIL` one or more addresses (`;` / `,`). An EAN CODEX has on
MORE THAN ONE firm/branch (a shop vs an unrelated firm — 4 of 980 on 2026-10-16) is never
imported: the push reports it (`ambiguous`) and the add-on raises one ops alert per change, so
the warehouse fixes it in CODEX — a guess would route orders to the wrong customer.

CI-testable WITHOUT duckdb/requests installed: both are lazy-imported INSIDE the functions that
need them, and `run()` takes `query`/`as_of`/`poster` dependency-injection seams so tests feed
synthetic rows and capture the POST. `build_customers()` — the normalization core — is pure.

Config (the SAME EnvironmentFile as the other CODEX pushes, so no new secret):
  CODEX_CUSTOMERS_PUSH_URL  optional; default = CODEX_PUSH_URL with its last path segment ->
                            customers
  CODEX_PUSH_URL            e.g. https://email-pz.newlevel.media/api/codex/orders (the add-on
                            behind its Cloudflare tunnel, #470; this list is ~250 kB)
  CODEX_PUSH_TOKEN          the add-on's api_token
  CODEX_DUCKDB_PATH         default /var/lib/codex-bridge/codex.duckdb
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
from urllib.parse import urlsplit

DEFAULT_DB_PATH = "/var/lib/codex-bridge/codex.duckdb"
DEFAULT_TIMEOUT = 60

# The newest generation per (firm, branch); only rows with an EDI EAN. Ties inside one extract
# date are broken by the row's own values, so the pick is deterministic.
_SQL = """
WITH d AS (
    SELECT CAST(CAST(NICO AS BIGINT) AS VARCHAR)  AS nico,
           CAST(IICODOPL AS VARCHAR)              AS branch,
           trim(CAST(AEDIEAN AS VARCHAR))         AS ean,
           trim(ANAZORG)                          AS name,
           trim(AOBCHODM)                         AS trade_name,
           trim(AULICA)                           AS street,
           trim(AOBEC)                            AS city,
           trim(CAST(APSC AS VARCHAR))            AS zip,
           AEMAIL                                 AS emails,
           ROW_NUMBER() OVER (PARTITION BY NICO, IICODOPL
                              ORDER BY DDATVYPU DESC NULLS LAST, AEDIEAN, ANAZORG) AS rn
      FROM raw.firma)
SELECT nico, branch, ean, name, trade_name, street, city, zip, emails
  FROM d
 WHERE rn = 1 AND ean IS NOT NULL AND ean <> ''
 ORDER BY ean, nico, branch
"""
_AS_OF_SQL = ("SELECT max(finished_at) FROM meta.etl_runs "
              "WHERE table_name = 'firma' AND status = 'ok'")
_EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}")
_EAN_STRIP_RE = re.compile(r"[\s\-]")


def query_duckdb(db_path: str) -> list[dict]:
    """The newest firm rows that carry an EDI EAN, read-only. Lazy duckdb import (CI has none)."""
    import duckdb  # noqa: PLC0415 - lazy on purpose (CI has no duckdb)

    con = duckdb.connect(db_path, read_only=True)
    try:
        cur = con.execute(_SQL)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
    finally:
        con.close()


def query_as_of(db_path: str):
    """When the codex-bridge ETL last loaded raw.firma (naive UTC), or None."""
    import duckdb  # noqa: PLC0415 - lazy on purpose (CI has no duckdb)

    con = duckdb.connect(db_path, read_only=True)
    try:
        row = con.execute(_AS_OF_SQL).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def _iso_utc(value) -> str | None:
    """meta.etl_runs.finished_at (naive UTC) -> an ISO string with +00:00."""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.UTC)
        return value.isoformat()
    return str(value)


def _text(value, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


def ean(value) -> str:
    """An EDI EAN: spaces/dashes stripped, digits only — else "" (the row is skipped)."""
    code = _EAN_STRIP_RE.sub("", str(value or ""))
    return code if code.isdigit() else ""


def emails(value) -> list[str]:
    """`AEMAIL` -> distinct lower-cased addresses (several per cell, `;` / `,` / spaces)."""
    out: list[str] = []
    for m in _EMAIL_RE.findall(str(value or "").lower()):
        if m not in out:
            out.append(m)
    return out


def build_customers(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Normalize raw DuckDB rows (pure, the testable core). Returns (customers, ambiguous):
    one customer per EDI EAN carried by exactly ONE firm/branch; every EAN carried by more
    than one is reported in `ambiguous` (with each firm's name + city) and imported for NONE
    of them. A row without a usable EAN or without any name is skipped."""
    by_ean: dict[str, list[dict]] = {}
    for r in rows:
        code = ean(r.get("ean"))
        name = _text(r.get("name")) or _text(r.get("trade_name"))
        if not code or not name:
            continue
        firm = {"ean_edi": code, "name": name, "trade_name": _text(r.get("trade_name")),
                "emails": emails(r.get("emails")), "city": _text(r.get("city"), 120),
                "street": _text(r.get("street"), 200), "zip": _text(r.get("zip"), 20),
                "codex_id": f"{_text(r.get('nico'), 20)}/{_text(r.get('branch'), 20)}"}
        firms = by_ean.setdefault(code, [])
        if all(f["codex_id"] != firm["codex_id"] for f in firms):
            firms.append(firm)
    customers, ambiguous = [], []
    for code in sorted(by_ean):
        firms = by_ean[code]
        if len(firms) == 1:
            customers.append(firms[0])
        else:
            ambiguous.append({"ean_edi": code,
                              "firms": [{"name": f["name"], "city": f["city"],
                                         "codex_id": f["codex_id"]} for f in firms]})
    return customers, ambiguous


def customers_url(orders_url: str) -> str:
    """The customers endpoint next to the orders push URL (…/api/codex/orders -> …/customers)."""
    base = orders_url.rstrip("/")
    return base.rsplit("/", 1)[0] + "/customers" if "/" in base else base


def _requests_post(url: str, headers: dict, body: dict) -> dict:
    import requests  # noqa: PLC0415 - lazy on purpose

    resp = requests.post(url, headers=headers, json=body, timeout=DEFAULT_TIMEOUT)
    if resp.status_code >= 400:
        raise RuntimeError(f"add-on refused the push: HTTP {resp.status_code} {resp.text[:300]}")
    return resp.json() if resp.content else {}


def run(url: str, token: str, db_path: str = DEFAULT_DB_PATH, query=None, as_of=None,
        poster=None) -> dict:
    """Fetch -> normalize -> ONE POST (the add-on replaces its copy atomically). An empty list
    is never posted (it would be refused, and it means the source is broken, not that CODEX
    has no customers). `query()`/`as_of()`/`poster(...)` are injectable for tests."""
    query = query or (lambda: query_duckdb(db_path))
    as_of = as_of or (lambda: query_as_of(db_path))
    poster = poster or _requests_post
    # #485: the ETL time FIRST — if the ETL replaces the file between the two reads, the time
    # sent is older than the rows (safe), never newer
    source_as_of = _iso_utc(as_of())
    rows = query()
    customers, ambiguous = build_customers(rows)
    if not customers:
        return {"fetched": len(rows), "customers": 0, "ambiguous": len(ambiguous),
                "stored": 0, "error": "no usable customer rows — nothing posted"}
    body = {"source_as_of": source_as_of, "customers": customers, "ambiguous": ambiguous}
    resp = poster(url, {"X-Token": token, "Content-Type": "application/json"}, body) or {}
    return {"fetched": len(rows), "customers": len(customers), "ambiguous": len(ambiguous),
            "stored": int(resp.get("stored", 0) or 0), "source_as_of": source_as_of,
            "snapshot_id": resp.get("snapshot_id")}


def pushed_line(res: dict, url: str) -> str:
    """The journal line of a successful push — ends with the TARGET (scheme + host[:port] only,
    never the path / a token) so journalctl proves which address it reached (#470)."""
    parts = urlsplit(url)
    target = f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}"
    as_of = f"source_as_of={res['source_as_of']} " if res.get("source_as_of") else ""
    return (f"pushed: fetched={res['fetched']} customers={res['customers']} "
            f"stored={res['stored']} ambiguous={res['ambiguous']} "
            f"snapshot={res.get('snapshot_id')} {as_of}to={target}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Push the CODEX customer cards to the add-on (#493)")
    orders_url = os.environ.get("CODEX_PUSH_URL", "")
    ap.add_argument("--url", default=os.environ.get("CODEX_CUSTOMERS_PUSH_URL", "")
                    or (customers_url(orders_url) if orders_url else ""))
    ap.add_argument("--token", default=os.environ.get("CODEX_PUSH_TOKEN", ""))
    ap.add_argument("--db", default=os.environ.get("CODEX_DUCKDB_PATH", DEFAULT_DB_PATH))
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch + normalize, print counts, POST nothing")
    args = ap.parse_args(argv)

    if args.dry_run:
        customers, ambiguous = build_customers(query_duckdb(args.db))
        print(f"dry-run: customers={len(customers)} ambiguous={len(ambiguous)} "
              f"({', '.join(a['ean_edi'] for a in ambiguous)}) "
              f"as_of={_iso_utc(query_as_of(args.db))} (db={args.db})")
        return 0
    if not args.url or not args.token:
        print("error: CODEX_PUSH_URL (or CODEX_CUSTOMERS_PUSH_URL) and CODEX_PUSH_TOKEN "
              "(or --url/--token) are required", file=sys.stderr)
        return 2
    res = run(args.url, args.token, db_path=args.db)
    if res.get("error"):
        print(f"error: {res['error']} (fetched={res['fetched']})", file=sys.stderr)
        return 1
    print(pushed_line(res, args.url))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
