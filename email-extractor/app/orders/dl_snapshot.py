"""DL (dodacie listy) catalog + supplier snapshots (#200 F1).

**#129: the sheet is never fetched anymore — `refresh()` (and `snapshot.fetch_csv`,
which it used to call) has been removed.** The DL catalog/supplier snapshot frozen
2026-08-07 (491 catalog rows, 959 suppliers) is now permanent; `dl_worker.refresh_due`
just reports it (or a future one imported some other way — a dashboard editing UI is
tracked as a follow-up, #221). `import_snapshot` itself (pure CSV-text importer, no
network) is unchanged and still used by tests and `dl_eval_run.py`'s corpus import.

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

import psycopg

from . import snapshot
from .snapshot import SnapshotRefused  # re-exported for callers

log = logging.getLogger("orders.dl_snapshot")

_CURRENCY_RE = re.compile(r"[€$]")


def _cell(row: dict, *names: str) -> str:
    for n in names:
        if n in row and row[n] is not None:
            return str(row[n]).strip()
    return ""


def parse_number(value: str | None) -> float | None:
    """Public wrapper of `_num` (#221) — the /znalosti dashboard's mass/cena text fields
    need the exact same tolerant parsing a sheet cell already gets (comma decimal, NBSP
    thousands, trailing currency symbol); reused rather than re-implemented."""
    return _num(value)


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
    """Freeze one (catalog union, suppliers) pair, with any manual #221 catalog/supplier
    overrides applied on top — an override always wins over whatever the imported CSV
    still says for the same identity, same as snapshot.import_snapshot already does for
    the AI-orders side. Returns the snapshot id.

    A header-only / empty result for EITHER side is refused — same rule
    snapshot.import_snapshot applies, and the same reason: accepting it would freeze
    a snapshot that matches nothing and reject every DL, and it is what a revoked
    share or a Google error page looks like after decoding.
    """
    catalog = merge_catalog(dl_catalog_csv, objednavky_catalog_csv)
    suppliers = parse_suppliers(supplier_csv)
    if not catalog or not suppliers:
        raise SnapshotRefused(
            f"DL import looks empty (catalog={len(catalog)}, suppliers={len(suppliers)}) "
            "— keeping the previous snapshot")
    catalog = _apply_dl_catalog_overrides(conn, catalog)
    suppliers = _apply_dl_supplier_overrides(conn, suppliers)
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


# --- #221: direct curation of DL catalog cards, mirroring #127's catalog_overrides on the
# AI-orders side — see this module's own docstring + db.py's dl_catalog_overrides comment
# for why this is a SEPARATE table rather than a shared/widened one. Keyed by gtin, exactly
# like catalog_overrides, so an edit or a retirement always targets exactly one row.

def _load_dl_catalog_overrides(conn) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT gtin, name, doplnok, mass, sklad, cena, retired FROM dl_catalog_overrides"
    ).fetchall()
    return {r[0]: {"name": r[1], "doplnok": r[2] or "",
                   "mass": float(r[3]) if r[3] is not None else None, "sklad": r[4] or "",
                   "cena": float(r[5]) if r[5] is not None else None, "retired": r[6]}
            for r in rows}


def _merge_dl_catalog(catalog: list[dict], overrides: dict[str, dict]) -> list[dict]:
    out, seen = [], set()
    for row in catalog:
        ov = overrides.get(row["gtin"])
        if ov:
            seen.add(row["gtin"])
            if ov["retired"]:
                continue
            out.append({"gtin": row["gtin"], "name": ov["name"], "doplnok": ov["doplnok"],
                        "mass": ov["mass"], "sklad": ov["sklad"], "cena": ov["cena"]})
        else:
            out.append(row)
    for gtin, ov in overrides.items():
        if gtin in seen or ov["retired"]:
            continue
        out.append({"gtin": gtin, "name": ov["name"], "doplnok": ov["doplnok"],
                    "mass": ov["mass"], "sklad": ov["sklad"], "cena": ov["cena"]})
    return out


def _apply_dl_catalog_overrides(conn, catalog: list[dict]) -> list[dict]:
    return _merge_dl_catalog(catalog, _load_dl_catalog_overrides(conn))


def dl_catalog_for_management(conn) -> list[dict]:
    """The current effective DL catalog (frozen snapshot + overrides merged), each row
    flagged with whether it carries a manual override — what /znalosti's DL products box
    lists. Mirrors snapshot.catalog_for_management exactly."""
    sid = latest_snapshot_id(conn)
    base = load_catalog(conn, sid) if sid else []
    overrides = _load_dl_catalog_overrides(conn)
    merged = _merge_dl_catalog(base, overrides)
    return [dict(r, overridden=r["gtin"] in overrides) for r in merged]


def upsert_dl_catalog_card(conn, gtin: str, name: str, *, doplnok: str = "",
                           mass: float | None = None, sklad: str = "",
                           cena: float | None = None) -> None:
    """Add a brand-new DL card, or edit an existing one (snapshot-derived or already
    overridden) — same call either way, keyed by gtin."""
    conn.execute(
        """INSERT INTO dl_catalog_overrides (gtin, name, doplnok, mass, sklad, cena,
                                              retired, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, false, now())
           ON CONFLICT (gtin) DO UPDATE
              SET name = EXCLUDED.name, doplnok = EXCLUDED.doplnok, mass = EXCLUDED.mass,
                  sklad = EXCLUDED.sklad, cena = EXCLUDED.cena, retired = false,
                  updated_at = now()""",
        (gtin, name, doplnok, mass, sklad, cena))


def retire_dl_catalog_card(conn, gtin: str) -> bool:
    """True iff `gtin` was a real card in the CURRENT effective DL catalog (snapshot or
    override) — retiring a gtin that never existed is refused rather than silently
    creating a phantom override row."""
    current = {r["gtin"] for r in dl_catalog_for_management(conn)}
    if gtin not in current:
        return False
    conn.execute(
        """INSERT INTO dl_catalog_overrides (gtin, name, retired, updated_at)
           VALUES (%s, '', true, now())
           ON CONFLICT (gtin) DO UPDATE SET retired = true, updated_at = now()""",
        (gtin,))
    return True


# --- #221: direct curation of DL suppliers, mirroring #128's customer_overrides. Surrogate
# id, not ean_edi — a supplier row can legitimately have a blank EAN or share one across
# branches, same reasoning customer_overrides already documents. orig_ean_edi/orig_city pin
# the ORIGINAL snapshot row an override replaces (NULL orig_ean_edi = a brand-new supplier);
# city instead of street, because dl_supplier_snapshot/load_suppliers never persists
# street/zip at all (see this module's own R21 docstring above).

def _load_dl_supplier_overrides(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT id, orig_ean_edi, orig_city, ean_edi, name, emails, city, retired
           FROM dl_supplier_overrides ORDER BY id""").fetchall()
    return [{"id": r[0], "orig_ean_edi": r[1], "orig_city": r[2], "ean_edi": r[3] or "",
             "name": r[4], "emails": list(r[5] or []), "city": r[6] or "", "retired": r[7]}
            for r in rows]


def _merge_dl_suppliers(base: list[dict], overrides: list[dict]) -> list[dict]:
    """Same shape as snapshot.py's `_merge_customers` — see that function's own docstring
    for why BOTH the override's original AND current identity must be excluded from `base`
    (idempotent re-merging, and a brand-new override with no prior "current" state uses its
    own original identity rather than a blank placeholder — see the #128 review finding
    mirrored in this module's own retire_dl_supplier below)."""
    excluded = set()
    for o in overrides:
        if o["orig_ean_edi"] is not None:
            excluded.add((o["orig_ean_edi"], o["orig_city"]))
        excluded.add((o["ean_edi"], o["city"]))
    out = []
    for row in base:
        key = (row.get("ean_edi") or "", row.get("city") or "")
        if key in excluded:
            continue
        out.append({**row, "override_id": None, "orig_ean_edi": key[0], "orig_city": key[1]})
    for o in overrides:
        if o["retired"]:
            continue
        out.append({"ean_edi": o["ean_edi"], "name": o["name"], "emails": o["emails"],
                    "city": o["city"], "override_id": o["id"],
                    "orig_ean_edi": o["orig_ean_edi"], "orig_city": o["orig_city"]})
    return out


def _apply_dl_supplier_overrides(conn, suppliers: list[dict]) -> list[dict]:
    merged = _merge_dl_suppliers(suppliers, _load_dl_supplier_overrides(conn))
    return [{"ean_edi": r["ean_edi"], "name": r["name"], "emails": r["emails"],
             "city": r["city"]} for r in merged]


def dl_suppliers_for_management(conn) -> list[dict]:
    """The current effective DL supplier list (snapshot + overrides merged), each row
    carrying the override identity fields /znalosti needs to edit/retire it."""
    sid = latest_snapshot_id(conn)
    base = load_suppliers(conn, sid) if sid else []
    return _merge_dl_suppliers(base, _load_dl_supplier_overrides(conn))


def _active_dl_supplier_conflict(conn, ean_edi: str) -> dict | None:
    """#248 review finding: mirrors `snapshot._active_customer_conflict` — factored out
    so the reclaim check, the fresh-insert UniqueViolation backstop, and the
    override-id-edit UniqueViolation backstop all build the identical
    `DuplicateEan.existing` shape."""
    hit = conn.execute(
        """SELECT id, name, city FROM dl_supplier_overrides
            WHERE orig_ean_edi IS NULL AND ean_edi = %s AND NOT retired""",
        (ean_edi,)).fetchone()
    if not hit:
        return None
    hit_id, hit_name, hit_city = hit
    return {"ean_edi": ean_edi, "name": hit_name, "city": hit_city,
            "override_id": hit_id}


def upsert_dl_supplier(conn, *, override_id: int | None, orig_ean_edi: str | None,
                       orig_city: str | None, ean_edi: str, name: str,
                       emails: list[str], city: str) -> int:
    """Add a brand-new DL supplier (override_id=None, orig_ean_edi=None), or edit one —
    either an already-overridden row (pass its override_id) or a still-snapshot-only row
    (pass its CURRENT orig_ean_edi/orig_city, from `dl_suppliers_for_management`; the
    partial unique index makes a repeat call idempotent, no duplicate rows).

    #235: `ean_edi` is validated + normalized UNCONDITIONALLY (`snapshot.normalize_ean`,
    reused with `entity="dodávateľ"` rather than a second copy — mirrors #234's
    `upsert_customer`'s own "every write path funnels through here" guarantee for the
    EAN-EDI a DL delivery cannot be built without)."""
    ean_edi = snapshot.normalize_ean(ean_edi, entity="dodávateľ")
    if override_id is not None:
        # #248 review finding: mirrors `upsert_customer`'s own override-id-edit fix —
        # this branch (editing an already-overridden supplier row by id, e.g. from the
        # /znalosti admin dashboard) had no uniqueness check of its own; the new partial
        # unique index (db.py) now turns a collision into `UniqueViolation` instead of a
        # silent duplicate, caught here and raised as the same clean `DuplicateEan`.
        # This backstop only ever fires for a genuinely HAND-ADDED supplier row
        # (`orig_ean_edi IS NULL`) — the only partition the partial index actually
        # covers; see the #285 guard immediately below for the sibling SHEET-BOUND
        # case, which this backstop structurally cannot catch.
        #
        # #285: mirrors `snapshot.upsert_customer`'s own #285 fix byte-for-byte, `city`
        # standing in for `street` — a supplier row that FIRST became sheet-bound (this
        # same `id` was created by the fallthrough branch further down,
        # `orig_ean_edi IS NOT NULL`) and is now being edited a SECOND time via its own
        # `override_id` has the IDENTICAL gap #275 already fixed for the fallthrough
        # branch. See `upsert_customer`'s own #285 comment for the full reasoning
        # (why `orig_ean_edi` is safe to read outside the lock, why an empty string is
        # still treated as sheet-bound, the self-collision safety argument, and what
        # race direction this deliberately does NOT close).
        current = conn.execute(
            "SELECT orig_ean_edi FROM dl_supplier_overrides WHERE id=%s", (override_id,)
        ).fetchone()
        sheet_bound = current is not None and current[0] is not None
        try:
            with conn.transaction():
                if sheet_bound:
                    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (ean_edi,))
                    conflict = _active_dl_supplier_conflict(conn, ean_edi)
                    if conflict:
                        raise snapshot.DuplicateEan(ean_edi, conflict)
                row = conn.execute(
                    """UPDATE dl_supplier_overrides
                          SET ean_edi=%s, name=%s, emails=%s, city=%s, retired=false,
                              updated_at=now()
                        WHERE id=%s RETURNING id""",
                    (ean_edi, name, emails, city, override_id)).fetchone()
        except psycopg.errors.UniqueViolation:
            raise snapshot.DuplicateEan(
                ean_edi, _active_dl_supplier_conflict(conn, ean_edi) or
                {"ean_edi": ean_edi, "name": "", "city": "", "override_id": None}
            ) from None
        if not row:
            raise KeyError(f"no such override id {override_id}")
        return int(row[0])
    if orig_ean_edi is None:
        # #235: mirrors #234's own post-review fix for `upsert_customer` (see that
        # function's docstring for the full reasoning) — the unique index below only
        # covers `orig_ean_edi IS NOT NULL` (editing a still-snapshot-only row); a
        # genuinely BRAND-NEW supplier has no ON CONFLICT target at all, so answering
        # the SAME dl_supplier question twice (a double-click, or two independent
        # documents both raising "new supplier" for the same real sender) could insert
        # two rows sharing one EAN. An advisory transaction lock keyed on the EAN
        # serializes the reclaim-or-insert decision across connections (the server runs
        # `threaded=True`), zero schema change — same technique as `edi_sent`'s
        # two-phase claim.
        #
        # #248 fix: mirrors `upsert_customer`'s own #248 fix byte-for-byte, `city`
        # standing in for `street` — the reclaim SELECT below used to be scoped
        # `AND city = %s`, one notch narrower than the lock above it (keyed on `ean_edi`
        # alone), so two truly-simultaneous "new supplier" submissions for the same
        # brand-new EAN under DIFFERENT city values were serialized by the lock but each
        # one's own reclaim SELECT never saw the other's row (different city ⇒ no
        # match), and the second fell through to INSERT — a second row for one EAN. Now
        # widened to `ean_edi` alone, telling a genuine retry (same city ⇒ idempotent
        # reclaim) apart from a real second submission (different city ⇒ raises
        # `DuplicateEan`, same as `upsert_customer`). The `except
        # psycopg.errors.UniqueViolation` is the DB-level backstop for the partial
        # unique index #248's migration adds on `dl_supplier_overrides` — any future
        # write path that forgets this lock gets a clean `DuplicateEan` instead of a
        # raw constraint-violation crash.
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (ean_edi,))
            conflict = _active_dl_supplier_conflict(conn, ean_edi)
            if conflict:
                if conflict["city"] != city:
                    raise snapshot.DuplicateEan(ean_edi, conflict)
                row = conn.execute(
                    """UPDATE dl_supplier_overrides
                          SET name=%s, emails=%s, city=%s, retired=false, updated_at=now()
                        WHERE id=%s RETURNING id""",
                    (name, emails, city, conflict["override_id"])).fetchone()
                return int(row[0])
            try:
                with conn.transaction():
                    row = conn.execute(
                        """INSERT INTO dl_supplier_overrides
                               (orig_ean_edi, orig_city, ean_edi, name, emails, city,
                                retired, updated_at)
                           VALUES (NULL,%s,%s,%s,%s,%s,false,now())
                           RETURNING id""",
                        (orig_city, ean_edi, name, emails, city)).fetchone()
            except psycopg.errors.UniqueViolation:
                raise snapshot.DuplicateEan(
                    ean_edi, _active_dl_supplier_conflict(conn, ean_edi) or
                    {"ean_edi": ean_edi, "name": "", "city": "", "override_id": None}
                ) from None
            return int(row[0])
    # #275: mirrors `snapshot.upsert_customer`'s own #275 fix byte-for-byte, `city`
    # standing in for `street` — see that function's docstring for the full reasoning
    # (why the DB index can never cover this branch, why the SAME advisory lock is
    # taken here, and what race direction this deliberately does NOT close — a related
    # but distinct gap in the `override_id is not None` branch above is filed
    # separately as #285, out of this ticket's named scope).
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (ean_edi,))
        conflict = _active_dl_supplier_conflict(conn, ean_edi)
        if conflict:
            raise snapshot.DuplicateEan(ean_edi, conflict)
        row = conn.execute(
            """INSERT INTO dl_supplier_overrides
                   (orig_ean_edi, orig_city, ean_edi, name, emails, city, retired,
                    updated_at)
               VALUES (%s,%s,%s,%s,%s,%s,false,now())
               ON CONFLICT (orig_ean_edi, orig_city) WHERE orig_ean_edi IS NOT NULL
               DO UPDATE SET ean_edi=EXCLUDED.ean_edi, name=EXCLUDED.name,
                              emails=EXCLUDED.emails, city=EXCLUDED.city,
                              retired=false, updated_at=now()
               RETURNING id""",
            (orig_ean_edi, orig_city, ean_edi, name, emails, city)).fetchone()
    return int(row[0])


def retire_dl_supplier(conn, *, override_id: int | None, orig_ean_edi: str | None,
                       orig_city: str | None) -> bool:
    """Retire an already-overridden row by id, or a still-snapshot-only row by its current
    (orig_ean_edi, orig_city) identity. False when neither identity is given, or the named
    override id does not exist."""
    if override_id is not None:
        row = conn.execute(
            "UPDATE dl_supplier_overrides SET retired=true, updated_at=now() "
            "WHERE id=%s RETURNING id", (override_id,)).fetchone()
        return row is not None
    if orig_ean_edi is None:
        return False
    # A fresh retirement marker's "current identity" must be its ORIGINAL one, never a blank
    # placeholder — same #127/#128 review finding this module's docstring already cites:
    # a blank ("", "") current identity would make _merge_dl_suppliers exclude EVERY
    # supplier that legitimately has both ean_edi and city blank, not just the one retired.
    conn.execute(
        """INSERT INTO dl_supplier_overrides
               (orig_ean_edi, orig_city, ean_edi, name, emails, city, retired, updated_at)
           VALUES (%s,%s,%s,%s,%s,%s,true,now())
           ON CONFLICT (orig_ean_edi, orig_city) WHERE orig_ean_edi IS NOT NULL
           DO UPDATE SET retired=true, updated_at=now()""",
        (orig_ean_edi, orig_city, orig_ean_edi, "", [], orig_city or ""))
    return True


def dl_rebuild_from_overrides(conn) -> int | None:
    """Re-freeze a new DL snapshot from the current latest one plus whatever catalog/
    supplier overrides exist right now (#221) — used right after a /znalosti DL edit so the
    change is visible immediately, with no network call and nothing else to wait for (#129:
    there is no periodic refresh anymore either). Mirrors snapshot.rebuild_from_overrides.
    Returns None when there is no DL snapshot yet to rebuild from."""
    sid = latest_snapshot_id(conn)
    if sid is None:
        return None
    catalog = _apply_dl_catalog_overrides(conn, load_catalog(conn, sid))
    suppliers = _apply_dl_supplier_overrides(conn, load_suppliers(conn, sid))
    return _freeze(conn, catalog, suppliers)


