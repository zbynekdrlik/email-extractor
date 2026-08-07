"""DL (dodacie listy) catalog + supplier snapshots (#200 F1).

Same content-addressed pattern as app/orders/snapshot.py (the AI-orders catalog),
but a SEPARATE versioning line (`dl_snapshots`/`dl_catalog_snapshot`/
`dl_supplier_snapshot`, not the orders tables): the DL catalog's shape
(name/gtin/mass/doplnok/sklad/cena, R20) genuinely differs from the orders
catalog's (gtin/name/alias only), so sharing one snapshot table would mint a new
version whenever EITHER pipeline's own sheet changes, even when the other's data
is untouched.

**R20 — the catalog is a UNION of two tabs, never a replacement of one by the
other:** `produkty dodacie listy` (has mass/doplnok/sklad/cena — the DL-specific
fields) PLUS `produkty objednavky` (the existing AI-orders catalog tab). BOTH tabs
are parsed with the SAME `parse_dl_catalog` (not routed through the orders-only
`snapshot.parse_catalog`, which would silently drop `Sklad` — a real R84
kg-tracking signal the orders tab genuinely carries; review finding on #200's PR).
A field truly absent from a tab (that tab has no `hmotnost`/`Cena` header at all)
still comes back `None`, distinct from a present-but-empty cell.

**GTIN de-duplication — a deliberate DEVIATION from n8n's literal `Merge(append)`
(review finding on #200's PR).** n8n's node keeps both rows verbatim; here, the
DL-specific tab wins when the SAME gtin appears in both (`dl_catalog_snapshot`'s
own primary key is `(snapshot_id, gtin)`, matching `catalog_snapshot`'s shape — a
second row for the same gtin cannot coexist there either way, and letting the DB
silently swallow it via `ON CONFLICT DO NOTHING` would leave the CONTENT HASH
computed over rows that never actually get stored, exactly the churn
`snapshot.py`'s own `_content_hash` docstring exists to prevent). `merge_catalog`
now dedups explicitly, BEFORE hashing/counting, so what's hashed is what's stored.

**W14 — no static keep-only column allowlist that can silently drop a new column.**
The n8n `Edit Fields` node's fixed field list already did this once for real (the
Cena rollout, 2026-07-22 — a brand-new column sat in the sheet for a while with the
node silently never reading it). This parser reads every field R20 documents by
name with multiple header-spelling candidates (mirroring `snapshot.py`'s own `_cell`
helper); a genuinely NEW business field added to the sheet later needs a matching
addition HERE (never inferred automatically — Python's csv.DictReader has no way to
know a bare unlabelled column is meaningful), which is a visible, reviewable code
change instead of a silent drop.

**R21 — suppliers are the SAME `customers` sheet tab AI orders already reads**, just
interpreted the other way round (who ships TO us, not who we ship to) — reuses
`snapshot.parse_customers` verbatim rather than re-implementing an identical parser.
"""
from __future__ import annotations

import hashlib
import logging
import re

from . import snapshot
from .snapshot import SnapshotRefused, fetch_csv  # re-exported for callers

log = logging.getLogger("orders.dl_snapshot")

_CURRENCY_RE = re.compile(r"[€$]")


def _cell(row: dict, *names: str) -> str:
    for n in names:
        if n in row and row[n] is not None:
            return str(row[n]).strip()
    return ""


def _num(value: str) -> float | None:
    """Slovak Sheets exports commonly carry: a comma decimal separator ('9,90'), a
    NBSP/narrow-NBSP thousands separator from a formatted-number export ('1\xa0133,00'),
    a trailing currency symbol ('12,50 €'), or a dot thousands + comma decimal
    ('1.234,50'). Empty/unparsable → None, never 0 — a missing mass/price must stay
    distinguishable from a genuinely zero one (review finding on #200's PR: the
    original version only handled the plain comma case)."""
    v = str(value or "").strip()
    if not v:
        return None
    v = v.replace("\xa0", "").replace(" ", "").replace(" ", "")
    v = _CURRENCY_RE.sub("", v)
    if "," in v and "." in v:
        # '.' is a THOUSANDS separator here, not a decimal point — drop it before the
        # comma→dot swap below, or '1.234,50' would corrupt to '1.234.50'.
        v = v.replace(".", "")
    v = v.replace(",", ".")
    try:
        return float(v)
    except ValueError:
        return None


def parse_dl_catalog(text: str) -> list[dict]:
    """The 'produkty dodacie listy' tab — R20's DL-specific fields. A row without
    BOTH a GTIN and a name cannot be matched against, so it is dropped (same rule
    snapshot.parse_catalog already applies to the orders tab)."""
    out = []
    for row in snapshot._rows(text):
        gtin = _cell(row, "GTIN", "gtin")
        name = _cell(row, "Názov", "nazov", "name")
        if not gtin or not name:
            continue
        out.append({
            "gtin": gtin,
            "name": name,
            "doplnok": _cell(row, "doplnok", "alias"),
            "mass": _num(_cell(row, "hmotnost", "hmotnosť", "mass")),
            "sklad": _cell(row, "Sklad", "sklad"),
            "cena": _num(_cell(row, "Cena", "cena")),
        })
    return out


def merge_catalog(dl_catalog_csv: str, objednavky_catalog_csv: str) -> list[dict]:
    """The union R20 describes. Both tabs go through `parse_dl_catalog` — NOT
    `snapshot.parse_catalog` for the orders tab, which only returns gtin/name/alias
    and would silently drop that tab's `Sklad` column (a real R84 kg-tracking signal
    the orders sheet genuinely carries — review finding on #200's PR, the exact W14
    class of bug this module's docstring warns about).

    GTIN-deduped: when the SAME gtin appears in both tabs, the DL-specific tab's row
    wins (first-inserted-wins, matching `_freeze`'s own `ON CONFLICT DO NOTHING` on
    `dl_catalog_snapshot`'s `(snapshot_id, gtin)` primary key) — see the module
    docstring for why this deliberately deviates from n8n's literal `Merge(append)`.
    """
    seen: dict[str, dict] = {}
    for row in parse_dl_catalog(dl_catalog_csv):
        seen[row["gtin"]] = row
    for row in parse_dl_catalog(objednavky_catalog_csv):
        seen.setdefault(row["gtin"], row)
    return list(seen.values())


def parse_suppliers(text: str) -> list[dict]:
    """R21: suppliers = the SAME `customers` tab AI orders reads, same columns
    (Názov organizácie / EAN kód EDI / E-mail / Obec) — reused verbatim rather than
    re-implemented, since it is literally the same physical sheet tab.

    NOTE: the returned dicts also carry `street`/`zip` (inherited from
    `snapshot.parse_customers` — R21/R60 don't use them for DL). `load_suppliers`
    below does NOT persist those two fields (`dl_supplier_snapshot` has no such
    columns) — a later phase reading suppliers straight from THIS function (not from
    a frozen snapshot) sees a different shape than one reading `load_suppliers`'
    output. Review finding on #200's PR; not a bug today (nothing reads either yet).
    """
    return snapshot.parse_customers(text)


def _content_hash(catalog: list[dict], suppliers: list[dict]) -> str:
    """Order-independent, same reasoning as snapshot.py's own `_content_hash`
    docstring — two logically identical (catalog, suppliers) pairs must hash the
    same regardless of the order their rows arrived in."""
    cat_lines = sorted(
        f'C|{r["gtin"]}|{r["name"]}|{r["doplnok"]}|{r["mass"]}|{r["sklad"]}|{r["cena"]}'
        for r in catalog)
    sup_lines = sorted(
        f'S|{r["ean_edi"]}|{r["name"]}|{",".join(r["emails"])}|{r["city"]}'
        for r in suppliers)
    h = hashlib.sha256()
    for line in cat_lines + sup_lines:
        h.update(line.encode())
        h.update(b"\n")
    return h.hexdigest()


def _freeze(conn, catalog: list[dict], suppliers: list[dict]) -> int:
    digest = _content_hash(catalog, suppliers)
    row = conn.execute(
        "SELECT id FROM dl_snapshots WHERE content_sha256 = %s ORDER BY id DESC LIMIT 1",
        (digest,)).fetchone()
    if row:
        conn.execute("UPDATE dl_snapshots SET checked_at = now() WHERE id = %s", (row[0],))
        return int(row[0])

    sid = conn.execute(
        """INSERT INTO dl_snapshots (content_sha256, catalog_rows, supplier_rows)
           VALUES (%s, %s, %s) RETURNING id""",
        (digest, len(catalog), len(suppliers))).fetchone()[0]
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO dl_catalog_snapshot
                   (snapshot_id, gtin, name, doplnok, mass, sklad, cena)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (snapshot_id, gtin) DO NOTHING""",
            [(sid, r["gtin"], r["name"], r["doplnok"], r["mass"], r["sklad"], r["cena"])
             for r in catalog])
        cur.executemany(
            """INSERT INTO dl_supplier_snapshot (snapshot_id, ean_edi, name, emails, city)
               VALUES (%s, %s, %s, %s, %s)""",
            [(sid, r["ean_edi"], r["name"], r["emails"], r["city"]) for r in suppliers])
    log.info("dl snapshot %s frozen: %d catalog rows, %d suppliers",
             sid, len(catalog), len(suppliers))
    return int(sid)


def import_snapshot(conn, dl_catalog_csv: str, objednavky_catalog_csv: str,
                    supplier_csv: str) -> int:
    """Freeze one (catalog union, suppliers) pair. Returns the snapshot id.

    A header-only / empty result for EITHER side is refused — same rule
    snapshot.import_snapshot applies, and the same reason: accepting it would freeze
    a snapshot that matches nothing and reject every DL, and it is what a revoked
    share or a Google error page looks like after decoding.
    """
    catalog = merge_catalog(dl_catalog_csv, objednavky_catalog_csv)
    suppliers = parse_suppliers(supplier_csv)
    if not catalog or not suppliers:
        raise SnapshotRefused(
            f"DL sheet fetch looks empty (catalog={len(catalog)}, suppliers={len(suppliers)}) "
            "— keeping the previous snapshot")
    return _freeze(conn, catalog, suppliers)


def latest_snapshot_id(conn) -> int | None:
    row = conn.execute(
        "SELECT id FROM dl_snapshots ORDER BY checked_at DESC, id DESC LIMIT 1").fetchone()
    return int(row[0]) if row else None


def load_catalog(conn, snapshot_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT gtin, name, doplnok, mass, sklad, cena
             FROM dl_catalog_snapshot WHERE snapshot_id = %s ORDER BY gtin""",
        (snapshot_id,)).fetchall()
    return [{"gtin": r[0], "name": r[1], "doplnok": r[2] or "",
             "mass": float(r[3]) if r[3] is not None else None, "sklad": r[4] or "",
             "cena": float(r[5]) if r[5] is not None else None} for r in rows]


def load_suppliers(conn, snapshot_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT ean_edi, name, emails, city
             FROM dl_supplier_snapshot WHERE snapshot_id = %s ORDER BY id""",
        (snapshot_id,)).fetchall()
    return [{"ean_edi": r[0], "name": r[1], "emails": list(r[2] or []), "city": r[3] or ""}
           for r in rows]


def refresh(conn, doc_id: str, dl_catalog_gid: int | str, objednavky_gid: int | str,
           supplier_gid: int | str) -> int | None:
    """Fetch all three tabs and import. Returns the current snapshot id, or None when
    the fetch failed — a failed refresh is logged and leaves the previous snapshot in
    place, mirroring snapshot.refresh's own contract exactly."""
    try:
        dl_csv = fetch_csv(doc_id, dl_catalog_gid)
        objednavky_csv = fetch_csv(doc_id, objednavky_gid)
        supplier_csv = fetch_csv(doc_id, supplier_gid)
        return import_snapshot(conn, dl_csv, objednavky_csv, supplier_csv)
    except Exception as e:
        log.error("dl snapshot refresh failed (%s) — keeping snapshot %s",
                  e, latest_snapshot_id(conn))
        return latest_snapshot_id(conn)
