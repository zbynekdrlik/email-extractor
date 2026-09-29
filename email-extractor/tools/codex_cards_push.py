#!/usr/bin/env python3
"""Push the CODEX stock-card list from the codex-bridge DuckDB to the add-on (#467).

Runs on dev2 (where `/var/lib/codex-bridge/codex.duckdb` lives) on its OWN systemd timer
(`tools/systemd/codex-cards-push.timer`: 14:42 / 18:27 Europe/Prague — the #342 orders push's
cadence + 2 min, ~27 min after each codex-bridge ETL). It reads `raw.sm002` **read-only** — every
stock-card row whose EAN kód (`NEANKOD`) is set — and POSTs the WHOLE list in ONE body to
`POST /api/codex/cards` (X-Token auth). The add-on REPLACES its copy atomically, so a code that
left CODEX leaves the add-on too (the 3698 incident: card 27 carried it only 24.-28.9.).

Why it matters: CODEX rejects a WHOLE delivery-note import when one DESADV line carries an EAN
kód no stock card has. The add-on uses this list to refuse such a card number on the nástenka,
hold a DL line matched to such a card, and flag card names that drifted from CODEX's.

CI-testable WITHOUT duckdb/requests installed: both are lazy-imported INSIDE the functions that
need them, and `run()` takes `query`/`as_of`/`poster` dependency-injection seams so tests feed
synthetic rows and capture the POST. `build_cards()` — the normalization core — is pure.

sm002 columns (verified live 2026-09-29): `NEANKOD` DOUBLE = exactly our DL catalog `gtin`
(480 of 483 cards match), `ACSKLP` card code, `AMATERNS` name, `SSTRED`/`SSKLAD`,
`LNEAKTIVNY` (NULL = active), `UDATUMAKT` last change (CODEX local time). Several rows per card
(one per stredisko/sklad, several SUS price tiers) — grouped here per (code, card, stredisko,
sklad). The snapshot time comes from `meta.etl_runs` (`table_name='sm002'`, naive UTC).

Config (the SAME EnvironmentFile as the orders push, so no new secret):
  CODEX_CARDS_PUSH_URL  optional; default = CODEX_PUSH_URL with its last path segment -> cards
  CODEX_PUSH_URL        e.g. https://email-pz.newlevel.media/api/codex/orders (the add-on
                        behind its Cloudflare tunnel, #470 — Cloudflare caps a body at
                        100 MB / a request at 100 s; this list is ~1 MB)
  CODEX_PUSH_TOKEN      the add-on's api_token
  CODEX_DUCKDB_PATH     default /var/lib/codex-bridge/codex.duckdb
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import sys

DEFAULT_DB_PATH = "/var/lib/codex-bridge/codex.duckdb"
DEFAULT_TIMEOUT = 60
LOCAL_TZ = "Europe/Bratislava"   # CODEX writes UDATUMAKT in local wall-clock time

# One row per (code, card, stredisko, sklad): SUS price tiers collapse. Only a positive, whole
# NEANKOD is a usable code (it is a DOUBLE in CODEX).
_SQL = """
SELECT CAST(CAST(NEANKOD AS BIGINT) AS VARCHAR) AS code,
       trim(CAST(ACSKLP AS VARCHAR))             AS card_code,
       COALESCE(SSTRED, 0)                       AS stredisko,
       COALESCE(SSKLAD, 0)                       AS sklad,
       MAX(trim(AMATERNS))                       AS name,
       bool_or(COALESCE(LNEAKTIVNY, false))      AS inactive,
       MAX(UDATUMAKT)                            AS changed_at
  FROM raw.sm002
 WHERE NEANKOD IS NOT NULL AND NEANKOD > 0 AND NEANKOD = floor(NEANKOD)
 GROUP BY 1, 2, 3, 4
 ORDER BY 1, 3, 4, 2
"""
_AS_OF_SQL = ("SELECT max(finished_at) FROM meta.etl_runs "
              "WHERE table_name = 'sm002' AND status = 'ok'")
_CODE_RE = re.compile(r"(\d+)(?:\.0+)?")


def query_duckdb(db_path: str) -> list[dict]:
    """The stock-card rows, read-only. Lazy duckdb import (CI has none)."""
    import duckdb  # noqa: PLC0415 - lazy on purpose (CI has no duckdb)

    con = duckdb.connect(db_path, read_only=True)
    try:
        cur = con.execute(_SQL)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
    finally:
        con.close()


def query_as_of(db_path: str):
    """When the codex-bridge ETL last loaded sm002 (naive UTC), or None."""
    import duckdb  # noqa: PLC0415 - lazy on purpose (CI has no duckdb)

    con = duckdb.connect(db_path, read_only=True)
    try:
        row = con.execute(_AS_OF_SQL).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def _code(value) -> str | None:
    """A NEANKOD (DOUBLE / int / text) -> canonical integer text, or None. Mirrors the add-on's
    `codex_cards.normalize_code` (this file runs standalone on dev2, so it cannot import it)."""
    if value is None or isinstance(value, bool):
        return None
    m = _CODE_RE.fullmatch(str(value).strip())
    if not m:
        return None
    digits = m.group(1).lstrip("0")
    return digits or None


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


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


def build_cards(rows: list[dict]) -> list[dict]:
    """Normalize raw DuckDB rows into JSON-safe payload dicts (pure, the testable core). A row
    without a usable code is skipped — the add-on would drop it anyway."""
    out = []
    for r in rows:
        code = _code(r.get("code"))
        if not code:
            continue
        out.append({
            "code": code,
            "card_code": str(r.get("card_code") or "").strip(),
            "stredisko": _int(r.get("stredisko")),
            "sklad": _int(r.get("sklad")),
            "name": str(r.get("name") or "").strip()[:300],
            "inactive": bool(r.get("inactive")),
            "changed_at": _iso_local(r.get("changed_at")),
        })
    return out


def cards_url(orders_url: str) -> str:
    """The cards endpoint next to the orders push URL (…/api/codex/orders -> …/cards)."""
    base = orders_url.rstrip("/")
    return base.rsplit("/", 1)[0] + "/cards" if "/" in base else base


def _requests_post(url: str, headers: dict, body: dict) -> dict:
    import requests  # noqa: PLC0415 - lazy on purpose

    resp = requests.post(url, headers=headers, json=body, timeout=DEFAULT_TIMEOUT)
    if resp.status_code >= 400:
        raise RuntimeError(f"add-on refused the push: HTTP {resp.status_code} {resp.text[:300]}")
    return resp.json() if resp.content else {}


def run(url: str, token: str, db_path: str = DEFAULT_DB_PATH, query=None, as_of=None,
        poster=None) -> dict:
    """Fetch -> normalize -> ONE POST (the add-on replaces its list atomically). An empty list is
    never posted (it would be refused, and it means the source is broken, not that CODEX has no
    cards). `query()`/`as_of()`/`poster(...)` are injectable for tests."""
    query = query or (lambda: query_duckdb(db_path))
    as_of = as_of or (lambda: query_as_of(db_path))
    poster = poster or _requests_post
    rows = query()
    cards = build_cards(rows)
    if not cards:
        return {"fetched": len(rows), "cards": 0, "rows": 0, "codes": 0,
                "error": "no usable stock-card rows — nothing posted"}
    body = {"source_as_of": _iso_utc(as_of()), "cards": cards}
    resp = poster(url, {"X-Token": token, "Content-Type": "application/json"}, body) or {}
    return {"fetched": len(rows), "cards": len(cards), "rows": int(resp.get("rows", 0) or 0),
            "codes": int(resp.get("codes", 0) or 0)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Push the CODEX stock-card list to the add-on (#467)")
    orders_url = os.environ.get("CODEX_PUSH_URL", "")
    ap.add_argument("--url", default=os.environ.get("CODEX_CARDS_PUSH_URL", "")
                    or (cards_url(orders_url) if orders_url else ""))
    ap.add_argument("--token", default=os.environ.get("CODEX_PUSH_TOKEN", ""))
    ap.add_argument("--db", default=os.environ.get("CODEX_DUCKDB_PATH", DEFAULT_DB_PATH))
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch + normalize, print counts, POST nothing")
    args = ap.parse_args(argv)

    if args.dry_run:
        cards = build_cards(query_duckdb(args.db))
        print(f"dry-run: cards={len(cards)} codes={len({c['code'] for c in cards})} "
              f"as_of={_iso_utc(query_as_of(args.db))} (db={args.db})")
        return 0
    if not args.url or not args.token:
        print("error: CODEX_PUSH_URL (or CODEX_CARDS_PUSH_URL) and CODEX_PUSH_TOKEN "
              "(or --url/--token) are required", file=sys.stderr)
        return 2
    res = run(args.url, args.token, db_path=args.db)
    if res.get("error"):
        print(f"error: {res['error']} (fetched={res['fetched']})", file=sys.stderr)
        return 1
    print(f"pushed: fetched={res['fetched']} cards={res['cards']} rows={res['rows']} "
          f"codes={res['codes']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
