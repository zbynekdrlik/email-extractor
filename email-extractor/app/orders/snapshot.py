"""Catalog + customer snapshots (#59).

The pipeline must never read the Google Sheet directly: a live read makes every run
unreproducible, which is why the n8n version cannot be regression-tested at all. The
sheet is fetched here, frozen into Postgres under a content hash, and every order run
records the snapshot id it used.

Both tabs are fetched as CSV over the document's public export URL (verified
2026-07-30: both tabs return 200 without credentials), so the add-on needs no Google
credential. The document id lives in the add-on options, never in git.
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
import urllib.request

log = logging.getLogger("orders.snapshot")

EXPORT_URL = "https://docs.google.com/spreadsheets/d/{doc}/export?format=csv&gid={gid}"
FETCH_TIMEOUT = 60

_EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}")


class SnapshotRefused(Exception):
    """The fetched sheet is unusable (empty), so it must not replace a good snapshot."""


def sheet_csv_url(doc_id: str, gid: int | str) -> str:
    return EXPORT_URL.format(doc=doc_id, gid=gid)


def fetch_csv(doc_id: str, gid: int | str, timeout: int = FETCH_TIMEOUT) -> str:
    url = sheet_csv_url(doc_id, gid)
    with urllib.request.urlopen(url, timeout=timeout) as resp:   # noqa: S310 (fixed https host)
        return resp.read().decode("utf-8", errors="replace")


def _rows(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))


def _cell(row: dict, *names: str) -> str:
    for n in names:
        if n in row and row[n] is not None:
            return str(row[n]).strip()
    return ""


def parse_catalog(text: str) -> list[dict]:
    """Product cards. A row without BOTH a GTIN and a name cannot be ordered, so it is
    dropped; an empty alias is kept (it is data — 'no alias curated yet')."""
    out = []
    for row in _rows(text):
        gtin = _cell(row, "GTIN", "gtin")
        name = _cell(row, "Názov", "nazov", "name")
        if not gtin or not name:
            continue
        out.append({"gtin": gtin, "name": name,
                    "alias": _cell(row, "doplnok", "alias")})
    return out


def extract_emails(value: str) -> list[str]:
    """Addresses are pulled OUT of the cell rather than compared as a whole.

    The sheet writes them as 'Meno <adresa@dom>' and sometimes several per cell. The
    30.07.2026 order from objednavky.pno.martin@gmail.com failed because the whole-cell
    comparison missed exactly this shape — the customer was in the sheet all along.
    """
    seen, out = set(), []
    for m in _EMAIL_RE.findall(str(value or "").lower()):
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def parse_customers(text: str) -> list[dict]:
    out = []
    for row in _rows(text):
        name = _cell(row, "Názov organizácie", "Názov", "name")
        if not name:
            continue
        out.append({
            "name": name,
            "ean_edi": _cell(row, "EAN kód EDI", "ean_edi"),
            "emails": extract_emails(_cell(row, "E-mail", "email")),
            "city": _cell(row, "Obec", "city"),
            "street": _cell(row, "Ulica", "street"),
            "zip": _cell(row, "PSČ", "zip"),
        })
    return out


def _content_hash(catalog: list[dict], customers: list[dict]) -> str:
    h = hashlib.sha256()
    for r in catalog:
        h.update(f'C|{r["gtin"]}|{r["name"]}|{r["alias"]}\n'.encode())
    for r in customers:
        h.update(f'S|{r["ean_edi"]}|{r["name"]}|{",".join(r["emails"])}|'
                 f'{r["city"]}|{r["street"]}|{r["zip"]}\n'.encode())
    return h.hexdigest()


def import_snapshot(conn, catalog_csv: str, customer_csv: str) -> int:
    """Freeze one (catalog, customers) pair. Returns the snapshot id.

    Identical content reuses the existing snapshot, so the hourly refresh does not
    churn ids while the sheets are unchanged.
    """
    catalog = parse_catalog(catalog_csv)
    customers = parse_customers(customer_csv)
    # A header-only CSV is what a revoked share or a Google error page looks like after
    # decoding. Accepting it would produce a snapshot that matches nothing and would
    # reject every order, so it is refused and the previous snapshot stays current.
    if not catalog or not customers:
        raise SnapshotRefused(
            f"sheet fetch looks empty (catalog={len(catalog)}, customers={len(customers)}) "
            "— keeping the previous snapshot")

    digest = _content_hash(catalog, customers)
    row = conn.execute(
        "SELECT id FROM order_snapshots WHERE content_sha256 = %s ORDER BY id DESC LIMIT 1",
        (digest,)).fetchone()
    if row:
        conn.execute("UPDATE order_snapshots SET checked_at = now() WHERE id = %s", (row[0],))
        return int(row[0])

    sid = conn.execute(
        """INSERT INTO order_snapshots (content_sha256, catalog_rows, customer_rows)
           VALUES (%s, %s, %s) RETURNING id""",
        (digest, len(catalog), len(customers))).fetchone()[0]
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO catalog_snapshot (snapshot_id, gtin, name, alias)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (snapshot_id, gtin) DO NOTHING""",
            [(sid, r["gtin"], r["name"], r["alias"]) for r in catalog])
        cur.executemany(
            """INSERT INTO customer_snapshot
                   (snapshot_id, ean_edi, name, emails, city, street, zip)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            [(sid, r["ean_edi"], r["name"], r["emails"], r["city"], r["street"], r["zip"])
             for r in customers])
    log.info("snapshot %s imported: %d cards, %d customers", sid, len(catalog), len(customers))
    return int(sid)


def latest_snapshot_id(conn) -> int | None:
    row = conn.execute("SELECT id FROM order_snapshots ORDER BY id DESC LIMIT 1").fetchone()
    return int(row[0]) if row else None


def load_catalog(conn, snapshot_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT gtin, name, alias FROM catalog_snapshot WHERE snapshot_id = %s ORDER BY gtin",
        (snapshot_id,)).fetchall()
    return [{"gtin": r[0], "name": r[1], "alias": r[2] or ""} for r in rows]


def load_customers(conn, snapshot_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT ean_edi, name, emails, city, street, zip
           FROM customer_snapshot WHERE snapshot_id = %s ORDER BY id""",
        (snapshot_id,)).fetchall()
    return [{"ean_edi": r[0], "name": r[1], "emails": list(r[2] or []),
             "city": r[3] or "", "street": r[4] or "", "zip": r[5] or ""} for r in rows]


def import_files(conn, catalog_path: str, customers_path: str) -> int:
    """Import a snapshot from two frozen CSV files.

    The golden corpus pins the catalog its expected GTINs were written against. Fetching the
    live sheet in the CI gate would silently invalidate the whole corpus the next time
    somebody edits a product, so the gate imports these files instead (#79).
    """
    from pathlib import Path
    return import_snapshot(conn,
                           Path(catalog_path).read_text(encoding="utf-8"),
                           Path(customers_path).read_text(encoding="utf-8"))


def refresh(conn, doc_id: str, catalog_gid: int | str, customer_gid: int | str) -> int | None:
    """Fetch both tabs and import. Returns the current snapshot id, or None when the
    fetch failed — a failed refresh is logged and leaves the previous snapshot in place,
    it never raises into the worker loop."""
    try:
        catalog_csv = fetch_csv(doc_id, catalog_gid)
        customer_csv = fetch_csv(doc_id, customer_gid)
        return import_snapshot(conn, catalog_csv, customer_csv)
    except Exception as e:
        log.error("snapshot refresh failed (%s) — keeping snapshot %s",
                  e, latest_snapshot_id(conn))
        return latest_snapshot_id(conn)
