"""Catalog + customer snapshots (#59).

**#129: the Google Sheet is never read anymore — Postgres is the SOLE source of
truth.** The pipeline used to fetch the sheet's two tabs as CSV over its public export
URL every `catalog_refresh_minutes`; that network fetch (`fetch_csv`/`sheet_csv_url`/
`refresh`) has been removed entirely (#127/#128's `catalog_overrides`/
`customer_overrides` dashboard editing has been the maintained source since
2026-08-02, several days before this change). What remains here is a pure, network-free
CSV-text importer: `import_snapshot`/`import_files` freeze an already-obtained CSV
(a golden-corpus fixture file, or a hand-built string in a test) into Postgres under a
content hash, and every order run records the snapshot id it used. `worker.py`/
`dl_worker.py` no longer call anything in this module that touches the network — see
their own `refresh_due` docstrings.
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import re

log = logging.getLogger("orders.snapshot")

_EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}")


class SnapshotRefused(Exception):
    """The imported CSV is unusable (empty), so it must not replace a good snapshot."""


class InvalidCustomer(Exception):
    """A customer write with no usable EAN kód EDI (#234) — without it `edi.build` can
    never produce a real ORION document for that customer, so the write is refused rather
    than silently accepted and forgotten."""


_EAN_STRIP_RE = re.compile(r"[\s\-]")


def normalize_ean(value, *, entity: str = "zákazník", field: str = "EAN kód EDI") -> str:
    """Strip spaces/dashes and validate. `upsert_customer` calls this UNCONDITIONALLY —
    #234's whole point is that the EAN can never again be silently forgotten, so every
    write path funnels through here, not just the ones that remembered to check.

    #235: `entity`/`field` let a DIFFERENT write path (a DL supplier's EAN kód EDI, a DL
    catalog card's GTIN) reuse this exact helper — not a second copy of the regex/digit
    check — with its own precise Slovak wording. The defaults reproduce #234's original
    messages byte-for-byte, so every existing customer caller/test is unaffected."""
    code = _EAN_STRIP_RE.sub("", str(value or ""))
    if not code:
        raise InvalidCustomer(f"Bez {field} sa {entity} nedá uložiť.")
    if not code.isdigit():
        raise InvalidCustomer(f"{field} musí byť len číslice.")
    return code


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
    """Order-independent: two logically identical (catalog, customers) pairs must hash
    the same regardless of the order their rows arrived in. `import_snapshot` sees rows
    in raw sheet/CSV order; `rebuild_from_overrides` (#127/#128) sees them back out of
    Postgres sorted by gtin/id — reverting an override (e.g. retiring one right back to
    what the sheet already had) must be able to land on an EARLIER snapshot's exact
    hash for `_freeze`'s dedup to reuse it, or every retire would mint a needless new
    snapshot row even though nothing about the effective content actually changed."""
    cat_lines = sorted(f'C|{r["gtin"]}|{r["name"]}|{r["alias"]}' for r in catalog)
    cust_lines = sorted(
        f'S|{r["ean_edi"]}|{r["name"]}|{",".join(r["emails"])}|'
        f'{r["city"]}|{r["street"]}|{r["zip"]}' for r in customers)
    h = hashlib.sha256()
    for line in cat_lines + cust_lines:
        h.update(line.encode())
        h.update(b"\n")
    return h.hexdigest()


def _freeze(conn, catalog: list[dict], customers: list[dict]) -> int:
    """Content-address and persist one (catalog, customers) pair. Identical content
    reuses the existing snapshot, so re-freezing (an override edit's immediate
    `rebuild_from_overrides`, or a corpus re-import) never churns ids while nothing
    actually changed."""
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
    log.info("snapshot %s frozen: %d cards, %d customers", sid, len(catalog), len(customers))
    return int(sid)


def import_snapshot(conn, catalog_csv: str, customer_csv: str) -> int:
    """Freeze one (catalog, customers) pair parsed from the sheet, with any manual
    catalog/customer overrides (#127/#128) applied on top — an override always wins
    over whatever the sheet still says for the same identity. Returns the snapshot id.
    """
    catalog = parse_catalog(catalog_csv)
    customers = parse_customers(customer_csv)
    # A header-only CSV is what a revoked share or a Google error page looks like after
    # decoding. Accepting it would produce a snapshot that matches nothing and would
    # reject every order, so it is refused and the previous snapshot stays current.
    if not catalog or not customers:
        raise SnapshotRefused(
            f"import looks empty (catalog={len(catalog)}, customers={len(customers)}) "
            "— keeping the previous snapshot")
    catalog = _apply_catalog_overrides(conn, catalog)
    customers = _apply_customer_overrides(conn, customers)
    return _freeze(conn, catalog, customers)


def rebuild_from_overrides(conn) -> int | None:
    """Re-freeze a new snapshot from the current latest snapshot plus whatever catalog/
    customer overrides exist right now (#127/#128) — used right after a /znalosti edit
    so the change is visible immediately, with no network call and nothing else to wait
    for (#129: there is no periodic refresh anymore either). Idempotent: re-merging
    already-merged content just re-applies the same overrides, which is a no-op when
    nothing changed. Returns None when there is no snapshot yet to rebuild from."""
    sid = latest_snapshot_id(conn)
    if sid is None:
        return None
    catalog = _apply_catalog_overrides(conn, load_catalog(conn, sid))
    customers = _apply_customer_overrides(conn, load_customers(conn, sid))
    return _freeze(conn, catalog, customers)


def latest_snapshot_id(conn) -> int | None:
    """The CURRENT snapshot — not necessarily the highest id ever inserted. `_freeze`
    bumps `checked_at` on whichever row it decides represents the content right now,
    including when it dedup-reuses an OLDER id (content reverted to something an earlier
    snapshot already had, e.g. retiring a #127/#128 override) — `checked_at` is what
    actually tracks "current", `id` only tracks "when first seen"."""
    row = conn.execute(
        "SELECT id FROM order_snapshots ORDER BY checked_at DESC, id DESC LIMIT 1").fetchone()
    return int(row[0]) if row else None


def load_catalog(conn, snapshot_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT gtin, name, alias FROM catalog_snapshot WHERE snapshot_id = %s ORDER BY gtin",
        (snapshot_id,)).fetchall()
    return [{"gtin": r[0], "name": r[1], "alias": r[2] or ""} for r in rows]


def catalog_gtin_set(conn) -> set[str]:
    """The current catalog snapshot's GTIN set — the SAME source `/api/znalosti/catalog`
    (the warehouse's full-catalog search, #149) reads, so any card the search offers is
    guaranteed to be a valid `teach.answer()` target too. Empty when no snapshot exists yet
    (e.g. most unit tests), which keeps `teach.answer()`'s old candidates-only behaviour.

    Deliberately the RAW snapshot (`load_catalog`), not the override-merged
    `catalog_for_management` — same as `/api/znalosti/catalog` already reads (pre-existing
    behaviour, not introduced here). A retired card therefore stays searchable/teachable
    until the next snapshot refresh drops it; a manually-added override card is not
    searchable/teachable until it is. Don't assume override-awareness just because
    `catalog_for_management` exists next to this function — it serves a different page."""
    sid = latest_snapshot_id(conn)
    if not sid:
        return set()
    return {r["gtin"] for r in load_catalog(conn, sid)}


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


# --- #127: direct curation of product cards -------------------------------
#
# `catalog_overrides` is keyed by gtin — the card's own natural, stable identity
# everywhere else in this codebase — so there is no matching ambiguity: an edit or a
# retirement always targets exactly one row, sheet-derived or not. `alias`/`doplnok` is
# NOT part of this table on purpose (#127 design comment) — it stays owned by
# `global_item_memory` (#104's stage c).

def _load_catalog_overrides(conn) -> dict[str, dict]:
    rows = conn.execute("SELECT gtin, name, retired FROM catalog_overrides").fetchall()
    return {r[0]: {"name": r[1], "retired": r[2]} for r in rows}


def _merge_catalog(catalog: list[dict], overrides: dict[str, dict]) -> list[dict]:
    out, seen = [], set()
    for row in catalog:
        ov = overrides.get(row["gtin"])
        if ov:
            seen.add(row["gtin"])
            if ov["retired"]:
                continue
            out.append({"gtin": row["gtin"], "name": ov["name"], "alias": row.get("alias", "")})
        else:
            out.append(row)
    for gtin, ov in overrides.items():
        if gtin in seen or ov["retired"]:
            continue
        out.append({"gtin": gtin, "name": ov["name"], "alias": ""})
    return out


def _apply_catalog_overrides(conn, catalog: list[dict]) -> list[dict]:
    return _merge_catalog(catalog, _load_catalog_overrides(conn))


def catalog_for_management(conn) -> list[dict]:
    """The current effective catalog (sheet + overrides merged), each row flagged with
    whether it carries a manual override — what the /znalosti products UI lists. Loads
    the overrides table ONCE and reuses it for both the merge and the `overridden` flag
    (review finding: two separate loads for the same read)."""
    sid = latest_snapshot_id(conn)
    base = load_catalog(conn, sid) if sid else []
    overrides = _load_catalog_overrides(conn)
    merged = _merge_catalog(base, overrides)
    return [dict(r, overridden=r["gtin"] in overrides) for r in merged]


def upsert_catalog_card(conn, gtin: str, name: str) -> None:
    """Add a brand-new card, or edit an existing one (sheet-derived or already
    overridden) — same call either way, keyed by gtin."""
    conn.execute(
        """INSERT INTO catalog_overrides (gtin, name, retired, updated_at)
           VALUES (%s, %s, false, now())
           ON CONFLICT (gtin) DO UPDATE
              SET name = EXCLUDED.name, retired = false, updated_at = now()""",
        (gtin, name))


def retire_catalog_card(conn, gtin: str) -> bool:
    """True iff `gtin` was a real card in the CURRENT effective catalog (sheet or
    override) — retiring a gtin that never existed is refused rather than silently
    creating a phantom override row."""
    current = {r["gtin"] for r in catalog_for_management(conn)}
    if gtin not in current:
        return False
    conn.execute(
        """INSERT INTO catalog_overrides (gtin, name, retired, updated_at)
           VALUES (%s, '', true, now())
           ON CONFLICT (gtin) DO UPDATE SET retired = true, updated_at = now()""",
        (gtin,))
    return True


# --- #128: direct curation of customers ------------------------------------
#
# `customer_overrides` uses a SURROGATE id, not ean_edi — the sheet legitimately repeats
# an EAN across branches and can leave it empty (see the `customer_snapshot` schema
# comment), and #101 showed even an e-mail can belong to two customers at once, so
# neither is a safe override identity. `orig_ean_edi`/`orig_street` instead pin the
# ORIGINAL sheet row an override replaces (NULL orig_ean_edi = a brand-new customer, not
# an edit) — the partial unique index on those two columns makes a repeat edit of the
# same still-sheet-only row idempotent (update in place, never a duplicate override row).

def _load_customer_overrides(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT id, orig_ean_edi, orig_street, ean_edi, name, emails, city, street, zip,
                  retired
           FROM customer_overrides ORDER BY id""").fetchall()
    return [{"id": r[0], "orig_ean_edi": r[1], "orig_street": r[2], "ean_edi": r[3] or "",
             "name": r[4], "emails": list(r[5] or []), "city": r[6] or "",
             "street": r[7] or "", "zip": r[8] or "", "retired": r[9]} for r in rows]


def _merge_customers(base: list[dict], overrides: list[dict]) -> list[dict]:
    """base = sheet-parsed rows, or a previous snapshot's already-merged rows (merging
    twice is idempotent — see `rebuild_from_overrides`). Every output row carries
    `override_id` (None for an untouched sheet row) plus `orig_ean_edi`/`orig_street`:
    that row's OWN current identity for a still-sheet-only row (so the /znalosti edit
    form can send it back to create the FIRST override for it), or the override's fixed
    original identity otherwise (so a repeat edit keeps suppressing the same sheet row).

    Two things must be excluded from `base`, not just one: the override's ORIGINAL sheet
    identity (so the row it replaces disappears), AND the override's OWN CURRENT identity
    (so a row already baked into `base` by an EARLIER merge — e.g. `base` is itself a
    previously-rebuilt snapshot — is not counted twice once its own override row is
    appended below; this is what keeps re-merging idempotent for a brand-new customer
    too, which has no orig identity to exclude by).
    """
    excluded = set()
    for o in overrides:
        if o["orig_ean_edi"] is not None:
            excluded.add((o["orig_ean_edi"], o["orig_street"]))
        excluded.add((o["ean_edi"], o["street"]))
    out = []
    for row in base:
        key = (row.get("ean_edi") or "", row.get("street") or "")
        if key in excluded:
            continue
        out.append({**row, "override_id": None, "orig_ean_edi": key[0], "orig_street": key[1]})
    for o in overrides:
        if o["retired"]:
            continue
        out.append({"ean_edi": o["ean_edi"], "name": o["name"], "emails": o["emails"],
                     "city": o["city"], "street": o["street"], "zip": o["zip"],
                     "override_id": o["id"],
                     "orig_ean_edi": o["orig_ean_edi"], "orig_street": o["orig_street"]})
    return out


def _apply_customer_overrides(conn, customers: list[dict]) -> list[dict]:
    merged = _merge_customers(customers, _load_customer_overrides(conn))
    return [{"ean_edi": r["ean_edi"], "name": r["name"], "emails": r["emails"],
             "city": r["city"], "street": r["street"], "zip": r["zip"]} for r in merged]


def customers_for_management(conn) -> list[dict]:
    """The current effective customer list (sheet + overrides merged), each row carrying
    the override identity fields the /znalosti clients UI needs to edit/retire it."""
    sid = latest_snapshot_id(conn)
    base = load_customers(conn, sid) if sid else []
    return _merge_customers(base, _load_customer_overrides(conn))


def upsert_customer(conn, *, override_id: int | None, orig_ean_edi: str | None,
                     orig_street: str | None, ean_edi: str, name: str,
                     emails: list[str], city: str, street: str, zip_: str) -> int:
    """Add a brand-new customer (override_id=None, orig_ean_edi=None), or edit one —
    either an already-overridden row (pass its override_id) or a still-sheet-only row
    (pass its CURRENT orig_ean_edi/orig_street, from `customers_for_management`; the
    partial unique index makes a repeat call idempotent, no duplicate rows).

    #234: `ean_edi` is validated + normalized UNCONDITIONALLY (`normalize_ean` raises
    `InvalidCustomer` on blank/non-numeric) — every write funnels through here, so this is
    the single place the EAN can never again be silently forgotten."""
    ean_edi = normalize_ean(ean_edi)
    if override_id is not None:
        row = conn.execute(
            """UPDATE customer_overrides
                  SET ean_edi=%s, name=%s, emails=%s, city=%s, street=%s, zip=%s,
                      retired=false, updated_at=now()
                WHERE id=%s RETURNING id""",
            (ean_edi, name, emails, city, street, zip_, override_id)).fetchone()
        if not row:
            raise KeyError(f"no such override id {override_id}")
        return int(row[0])
    if orig_ean_edi is None:
        # #234 review finding: the unique index below only covers `orig_ean_edi IS NOT
        # NULL` (a still-sheet-only row being edited) — a genuinely BRAND-NEW customer has
        # no ON CONFLICT target at all, so a double-submit (a warehouse double-click, or
        # the auto-retry sweep re-adding the same sender) silently inserted TWO rows for
        # one real customer. `customer.resolve`'s exact_email rung then refuses ANY order
        # from that address once `len(owners) > 1` — the order gets stuck BECAUSE the
        # customer was added twice.
        #
        # Deep-review finding: the reclaim-or-insert decision below is itself a
        # check-then-act with NO db-level uniqueness behind it — two genuinely
        # concurrent callers for the SAME ean_edi (the server runs `threaded=True`) could
        # both pass the SELECT before either commits and each INSERT their own row. An
        # advisory TRANSACTION lock keyed on the EAN serializes that decision across
        # connections with zero schema change — the same class of fix this project
        # already uses for `edi_sent`'s two-phase claim (see orders-corpus.md). `conn.
        # transaction()` works whether `conn` already has an open transaction (this
        # nests as a SAVEPOINT; the lock is still held until the REAL enclosing
        # transaction commits, so it stays effective) or is autocommit (opens + commits
        # its own transaction here).
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (ean_edi,))
            existing = conn.execute(
                """SELECT id FROM customer_overrides
                    WHERE orig_ean_edi IS NULL AND ean_edi = %s AND street = %s
                      AND NOT retired""",
                (ean_edi, street)).fetchone()
            if existing:
                row = conn.execute(
                    """UPDATE customer_overrides
                          SET name=%s, emails=%s, city=%s, street=%s, zip=%s,
                              retired=false, updated_at=now()
                        WHERE id=%s RETURNING id""",
                    (name, emails, city, street, zip_, existing[0])).fetchone()
                return int(row[0])
            row = conn.execute(
                """INSERT INTO customer_overrides
                       (orig_ean_edi, orig_street, ean_edi, name, emails, city, street,
                        zip, retired, updated_at)
                   VALUES (NULL,%s,%s,%s,%s,%s,%s,%s,false,now())
                   RETURNING id""",
                (orig_street, ean_edi, name, emails, city, street, zip_)).fetchone()
            return int(row[0])
    row = conn.execute(
        """INSERT INTO customer_overrides
               (orig_ean_edi, orig_street, ean_edi, name, emails, city, street, zip,
                retired, updated_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,false,now())
           ON CONFLICT (orig_ean_edi, orig_street) WHERE orig_ean_edi IS NOT NULL
           DO UPDATE SET ean_edi=EXCLUDED.ean_edi, name=EXCLUDED.name, emails=EXCLUDED.emails,
                          city=EXCLUDED.city, street=EXCLUDED.street, zip=EXCLUDED.zip,
                          retired=false, updated_at=now()
           RETURNING id""",
        (orig_ean_edi, orig_street, ean_edi, name, emails, city, street, zip_)).fetchone()
    return int(row[0])


def retire_customer(conn, *, override_id: int | None, orig_ean_edi: str | None,
                     orig_street: str | None) -> bool:
    """Retire an already-overridden row by id, or a still-sheet-only row by its current
    (orig_ean_edi, orig_street) identity. False when neither identity is given, or the
    named override id does not exist."""
    if override_id is not None:
        row = conn.execute(
            "UPDATE customer_overrides SET retired=true, updated_at=now() "
            "WHERE id=%s RETURNING id", (override_id,)).fetchone()
        return row is not None
    if orig_ean_edi is None:
        return False
    # `ean_edi`/`street` here are the override's "current identity" that `_merge_customers`
    # ALSO excludes from `base` on every merge (needed so a previously-baked-in row of THIS
    # override does not survive re-merging) — for a fresh retirement marker that was NEVER
    # active with any other identity, that current identity must be the ORIGINAL one, never
    # a blank placeholder. A blank placeholder here would make `_merge_customers` exclude
    # ("", "") from EVERY future merge, silently dropping every OTHER customer that also
    # legitimately has both fields empty (both are optional per the sheet — #101/db.py's own
    # schema comment), not just the one actually being retired.
    conn.execute(
        """INSERT INTO customer_overrides
               (orig_ean_edi, orig_street, ean_edi, name, emails, city, street, zip,
                retired, updated_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,true,now())
           ON CONFLICT (orig_ean_edi, orig_street) WHERE orig_ean_edi IS NOT NULL
           DO UPDATE SET retired=true, updated_at=now()""",
        (orig_ean_edi, orig_street, orig_ean_edi, "", [], "", orig_street or "", ""))
    return True


# --- #159: remembering an answered "who is this customer?" address durably -----------

def remember_customer_email(conn, ean_edi: str, email: str) -> bool:
    """Append `email` to the chosen customer's e-mail list — the durable half of
    "answering teaches it forever": the next mail from this address then resolves via
    `customer.resolve`'s exact-address rule with no question asked at all. Layered on the
    SAME #128 override mechanism every /znalosti edit already uses, so it survives a sheet
    refresh (`_apply_customer_overrides` always wins over the sheet for the same identity)
    with no separate store needed — the caller still has to `rebuild_from_overrides` to
    make it effective immediately, exactly like every other override write in this module.

    No-op (returns False) when the address is already known for that customer, or
    `ean_edi` does not match any CURRENT customer (sheet-derived or already overridden).
    """
    email = (email or "").strip().lower()
    if not email or not ean_edi:
        return False
    rows = [r for r in customers_for_management(conn)
           if str(r.get("ean_edi") or "") == str(ean_edi)]
    if not rows:
        return False
    row = rows[0]
    emails = list(row.get("emails") or [])
    if email in [e.lower() for e in emails]:
        return False
    emails.append(email)
    upsert_customer(conn, override_id=row.get("override_id"),
                    orig_ean_edi=row.get("orig_ean_edi"), orig_street=row.get("orig_street"),
                    ean_edi=row.get("ean_edi") or "", name=row.get("name") or "",
                    emails=emails, city=row.get("city") or "", street=row.get("street") or "",
                    zip_=row.get("zip") or "")
    return True


def forget_customer_email(conn, ean_edi: str, email: str) -> bool:
    """The undo-half of `remember_customer_email` (#159, review finding on PR #161) —
    removes ONE address from the chosen customer's e-mail list, so a mis-taught sender no
    longer auto-resolves to the wrong customer on the NEXT order from that address. Same
    scope `teach.undo`'s product-wording undo already has: it protects FUTURE resolution
    only — it can never un-ship an order that already went out under the wrong customer.

    No-op (returns False) when the address is not currently listed for that customer, or
    `ean_edi` does not match any current customer.
    """
    email = (email or "").strip().lower()
    if not email or not ean_edi:
        return False
    rows = [r for r in customers_for_management(conn)
           if str(r.get("ean_edi") or "") == str(ean_edi)]
    if not rows:
        return False
    row = rows[0]
    current = list(row.get("emails") or [])
    emails = [e for e in current if e.lower() != email]
    if len(emails) == len(current):
        return False
    upsert_customer(conn, override_id=row.get("override_id"),
                    orig_ean_edi=row.get("orig_ean_edi"), orig_street=row.get("orig_street"),
                    ean_edi=row.get("ean_edi") or "", name=row.get("name") or "",
                    emails=emails, city=row.get("city") or "", street=row.get("street") or "",
                    zip_=row.get("zip") or "")
    return True
