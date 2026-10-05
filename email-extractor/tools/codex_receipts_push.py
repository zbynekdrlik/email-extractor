#!/usr/bin/env python3
"""Push CODEX supplier-receipt headers (príjemky) from the codex-bridge DuckDB to the add-on (#485).

Runs on dev2 (where `/var/lib/codex-bridge/codex.duckdb` lives) on its OWN systemd timer
(`tools/systemd/codex-receipts-push.timer`: 14:50 / 18:35 Europe/Prague — after the codex-bridge
ETL has loaded `sp001`, which takes ~15 min from 14:15 / 18:00). It reads the last `--days` (60)
of supplier receipts **read-only** and POSTs them in ONE body to `POST /api/codex/receipts`
(X-Token auth); the add-on REPLACES its copy atomically.

Why it matters: the warehouse often enters a supplier delivery into CODEX BY HAND (and writes
the INVOICE number into the DL-number field). An invoice that the DL engine turns into a
DESADV (`invoice_is_delivery_note`, #406/#412) must never be shipped a second time when CODEX
already has that delivery — the add-on's invoice dedup gate (`invoice_dedup`) compares each
invoice with these receipts (same supplier AND a DL/invoice number match, OR the delivery date
±1 day AND the total within tolerance). Incident: Zeelandia 9.9. — received by hand 13:34,
uploaded from the invoice 19:27.

CI-testable WITHOUT duckdb/requests installed: both are lazy-imported INSIDE the functions that
need them, and `run()` takes `query`/`as_of`/`poster` dependency-injection seams so tests feed
synthetic rows and capture the POST. `build_receipts()` — the normalization core — is pure.

CODEX shape (verified live 2026-10-05):
- `raw.sp001` — one row per receipt LINE, line identity (NCD, ICPOL) (unique, no duplicate
  rows). SDPOH 10/12/13/14 = purchase (príjemka); NCD = the receipt number (series 26100… =
  stock receipts carrying the supplier's DL number, 26110… = the rest — transport lines,
  returns); NCDLIST = the supplier's DL number as typed by the warehouse (often the INVOICE
  number instead); DUCTOBD = receipt date; NSUMAP = line total EUR (reliable for SDPOH 10 —
  never NMNOZ, mis-decoded); NICO = supplier IČO (31697143 = SLOVNORMAL itself, internal);
  SDRUHFAKT/SROK/IPORCFAKT = the link to the booked supplier invoice.
- `raw.faktury` (SDRUHFAKT = 1, A/P) — ACFAKTDPH = the supplier's invoice number (AVSYMB the
  variable symbol, usually equal); NVYMZAK1..4 = VAT bases (their sum = the invoice total
  without VAT). Duplicate rows exist → grouped per (NICO, SROK, IPORCFAKT).
- `raw.firma` — AEDIEAN = the supplier's EDI EAN = the add-on's `supplier_ean`. NICO is not
  unique there (branches) → grouped per NICO, MAX(AEDIEAN).
The snapshot time is `meta.etl_runs` (`table_name='sp001'`, naive UTC).

Config (the SAME EnvironmentFile as the orders/cards pushes, so no new secret):
  CODEX_RECEIPTS_PUSH_URL optional; default = CODEX_PUSH_URL with its last path segment ->
                          receipts
  CODEX_PUSH_URL          e.g. https://email-pz.newlevel.media/api/codex/orders (the add-on
                          behind its Cloudflare tunnel, #470)
  CODEX_PUSH_TOKEN        the add-on's api_token
  CODEX_DUCKDB_PATH       default /var/lib/codex-bridge/codex.duckdb
  CODEX_RECEIPTS_DAYS     default 60
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
from urllib.parse import urlsplit

DEFAULT_DB_PATH = "/var/lib/codex-bridge/codex.duckdb"
DEFAULT_DAYS = 60
DEFAULT_TIMEOUT = 60
LOCAL_TZ = "Europe/Bratislava"   # CODEX writes UDATUMAKT in local wall-clock time
OWN_ICO = 31697143               # SLOVNORMAL s.r.o. — internal transfers, never a supplier

# One row per receipt (NCD + supplier). The faktúry/firma sides are deduplicated BEFORE the
# join (both carry duplicate rows — a naive join fans out, codex-orders.md). Parameterized
# lookback as `CAST(? AS INTEGER) * INTERVAL 1 DAY` (a bare `INTERVAL (?) DAY` bind fails).
_SQL = f"""
WITH lines AS (
  SELECT NCD, NICO, NCDLIST, DUCTOBD, NSUMAP, SDRUHFAKT, SROK, IPORCFAKT, UDATUMAKT, SDPOH
    FROM raw.sp001
   WHERE SDPOH IN (10, 12, 13, 14)
     AND DUCTOBD >= current_date - (CAST(? AS INTEGER) * INTERVAL 1 DAY)
     AND NCD IS NOT NULL AND NICO IS NOT NULL AND NICO <> {OWN_ICO}
),
hdr AS (
  SELECT CAST(CAST(NCD AS BIGINT) AS VARCHAR)            AS receipt_number,
         NICO                                            AS nico,
         min(DUCTOBD)                                    AS receipt_date,
         max(DUCTOBD)                                    AS receipt_date_to,
         list(DISTINCT TRY_CAST(NCDLIST AS BIGINT))
           FILTER (WHERE TRY_CAST(NCDLIST AS BIGINT) IS NOT NULL) AS dl_numbers,
         min(struct_pack(srok := SROK, iporc := IPORCFAKT))
           FILTER (WHERE SDRUHFAKT = 1 AND IPORCFAKT IS NOT NULL) AS fak_key,
         sum(NSUMAP)                                     AS total,
         count(*)                                        AS line_count,
         min(UDATUMAKT)                                  AS entered_at,
         min(SDPOH)                                      AS sdpoh
    FROM lines
   GROUP BY NCD, NICO
),
firm AS (
  SELECT NICO,
         list(DISTINCT trim(AEDIEAN))
           FILTER (WHERE AEDIEAN IS NOT NULL AND trim(AEDIEAN) <> '') AS eans,
         max(trim(ANAZORG)) AS name
    FROM raw.firma GROUP BY NICO
),
fak AS (
  SELECT NICO, SROK, IPORCFAKT,
         max(trim(ACFAKTDPH)) AS invoice_number, max(trim(AVSYMB)) AS invoice_vs,
         max(COALESCE(NVYMZAK1, 0) + COALESCE(NVYMZAK2, 0) + COALESCE(NVYMZAK3, 0)
             + COALESCE(NVYMZAK4, 0))   AS invoice_total
    FROM raw.faktury WHERE SDRUHFAKT = 1 GROUP BY NICO, SROK, IPORCFAKT
)
SELECT h.receipt_number, CAST(CAST(h.nico AS BIGINT) AS VARCHAR) AS supplier_ico,
       firm.eans AS supplier_eans, firm.name AS supplier_name,
       h.receipt_date, h.receipt_date_to, h.dl_numbers,
       fak.invoice_number, fak.invoice_vs, h.total, fak.invoice_total,
       h.line_count, h.entered_at, h.sdpoh
  FROM hdr h
  LEFT JOIN firm ON firm.NICO = h.nico
  LEFT JOIN fak ON fak.NICO = h.nico AND fak.SROK = h.fak_key.srok
                AND fak.IPORCFAKT = h.fak_key.iporc
 ORDER BY h.receipt_date, h.receipt_number
"""
_AS_OF_SQL = ("SELECT max(finished_at) FROM meta.etl_runs "
              "WHERE table_name = 'sp001' AND status = 'ok'")
_DIGITS_RE = re.compile(r"(\d+)(?:\.0+)?")


def query_duckdb(db_path: str, days: int) -> list[dict]:
    """The receipt headers, read-only. Lazy duckdb import (CI has none)."""
    import duckdb  # noqa: PLC0415 - lazy on purpose (CI has no duckdb)

    con = duckdb.connect(db_path, read_only=True)
    try:
        cur = con.execute(_SQL, [int(days)])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
    finally:
        con.close()


def query_as_of(db_path: str):
    """When the codex-bridge ETL last loaded sp001 (naive UTC), or None."""
    import duckdb  # noqa: PLC0415 - lazy on purpose (CI has no duckdb)

    con = duckdb.connect(db_path, read_only=True)
    try:
        row = con.execute(_AS_OF_SQL).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def _number(value) -> str:
    """A CODEX number (DOUBLE / BIGINT / text) -> its text without a trailing `.0`, '' when
    missing. Text numbers (ACFAKTDPH) are kept as typed — the add-on compares digits only."""
    if value is None or isinstance(value, bool):
        return ""
    s = str(value).strip()
    m = _DIGITS_RE.fullmatch(s)
    return m.group(1) if m else s


def _numbers(value) -> list[str]:
    """A DuckDB LIST (or None / a scalar) of numbers → their distinct texts, sorted."""
    items = value if isinstance(value, (list, tuple)) else [value]
    return sorted({n for n in (_number(v) for v in items) if n})


def _money(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return round(f, 2) if f == f else None


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _date(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    return str(value)[:10] or None


def _iso_local(value) -> str | None:
    """UDATUMAKT (a naive CODEX local timestamp) -> an ISO string with the local offset."""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            from zoneinfo import ZoneInfo  # noqa: PLC0415 - stdlib, kept local
            value = value.replace(tzinfo=ZoneInfo(LOCAL_TZ))
        return value.isoformat()
    return str(value)


def _iso_utc(value) -> str | None:
    """meta.etl_runs.finished_at (naive UTC) -> an ISO string with +00:00."""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.UTC)
        return value.isoformat()
    return str(value)


def build_receipts(rows: list[dict]) -> list[dict]:
    """Normalize raw DuckDB rows into JSON-safe payload dicts (pure, the testable core). A row
    without a receipt number or a receipt date is skipped — it can never match anything."""
    out = []
    for r in rows:
        number = _number(r.get("receipt_number"))
        receipt_date = _date(r.get("receipt_date"))
        if not number or not receipt_date:
            continue
        out.append({
            "receipt_number": number,
            "supplier_ico": _number(r.get("supplier_ico")),
            "supplier_eans": sorted({str(e).strip() for e in (r.get("supplier_eans") or [])
                                     if str(e or "").strip()}),
            "supplier_name": str(r.get("supplier_name") or "").strip()[:300],
            "receipt_date": receipt_date,
            "receipt_date_to": _date(r.get("receipt_date_to")) or receipt_date,
            "dl_numbers": _numbers(r.get("dl_numbers")),
            "invoice_number": _number(r.get("invoice_number")),
            "invoice_vs": _number(r.get("invoice_vs")),
            "total": _money(r.get("total")),
            "invoice_total": _money(r.get("invoice_total")),
            "line_count": _int(r.get("line_count")),
            "entered_at": _iso_local(r.get("entered_at")),
            "sdpoh": _int(r.get("sdpoh")),
        })
    return out


def receipts_url(orders_url: str) -> str:
    """The receipts endpoint next to the orders push URL (…/api/codex/orders -> …/receipts)."""
    base = orders_url.rstrip("/")
    return base.rsplit("/", 1)[0] + "/receipts" if "/" in base else base


def _requests_post(url: str, headers: dict, body: dict) -> dict:
    import requests  # noqa: PLC0415 - lazy on purpose

    resp = requests.post(url, headers=headers, json=body, timeout=DEFAULT_TIMEOUT)
    if resp.status_code >= 400:
        raise RuntimeError(f"add-on refused the push: HTTP {resp.status_code} {resp.text[:300]}")
    return resp.json() if resp.content else {}


def run(url: str, token: str, db_path: str = DEFAULT_DB_PATH, days: int = DEFAULT_DAYS,
        query=None, as_of=None, poster=None) -> dict:
    """Fetch -> normalize -> ONE POST (the add-on replaces its copy atomically). An empty list
    is never posted (it would be refused, and it means the source is broken — CODEX always has
    receipts in a 60-day window). `query()`/`as_of()`/`poster(...)` are injectable for tests."""
    query = query or (lambda: query_duckdb(db_path, days))
    as_of = as_of or (lambda: query_as_of(db_path))
    poster = poster or _requests_post
    rows = query()
    receipts = build_receipts(rows)
    if not receipts:
        return {"fetched": len(rows), "receipts": 0, "stored": 0,
                "error": "no usable receipt rows — nothing posted"}
    body = {"source_as_of": _iso_utc(as_of()), "days": int(days), "receipts": receipts}
    resp = poster(url, {"X-Token": token, "Content-Type": "application/json"}, body) or {}
    return {"fetched": len(rows), "receipts": len(receipts),
            "stored": int(resp.get("stored", 0) or 0)}


def pushed_line(res: dict, url: str) -> str:
    """The journal line of a successful push. Ends with the TARGET (scheme + host[:port] only
    — never the path, a query token or userinfo) so journalctl proves which address the push
    reached (the #470 convention of the cards push)."""
    parts = urlsplit(url)
    target = f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}"
    return (f"pushed: fetched={res['fetched']} receipts={res['receipts']} "
            f"stored={res['stored']} to={target}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Push CODEX supplier receipts to the add-on (#485)")
    orders_url = os.environ.get("CODEX_PUSH_URL", "")
    ap.add_argument("--url", default=os.environ.get("CODEX_RECEIPTS_PUSH_URL", "")
                    or (receipts_url(orders_url) if orders_url else ""))
    ap.add_argument("--token", default=os.environ.get("CODEX_PUSH_TOKEN", ""))
    ap.add_argument("--db", default=os.environ.get("CODEX_DUCKDB_PATH", DEFAULT_DB_PATH))
    ap.add_argument("--days", type=int,
                    default=int(os.environ.get("CODEX_RECEIPTS_DAYS", DEFAULT_DAYS) or DEFAULT_DAYS))
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch + normalize, print counts, POST nothing")
    args = ap.parse_args(argv)

    if args.dry_run:
        receipts = build_receipts(query_duckdb(args.db, args.days))
        newest = max((r["receipt_date"] for r in receipts), default=None)
        print(f"dry-run: receipts={len(receipts)} newest={newest} days={args.days} "
              f"as_of={_iso_utc(query_as_of(args.db))} (db={args.db})")
        return 0
    if not args.url or not args.token:
        print("error: CODEX_PUSH_URL (or CODEX_RECEIPTS_PUSH_URL) and CODEX_PUSH_TOKEN "
              "(or --url/--token) are required", file=sys.stderr)
        return 2
    res = run(args.url, args.token, db_path=args.db, days=args.days)
    if res.get("error"):
        print(f"error: {res['error']} (fetched={res['fetched']})", file=sys.stderr)
        return 1
    print(pushed_line(res, args.url))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
