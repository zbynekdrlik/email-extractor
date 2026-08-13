"""Catalog/customer snapshots (#59).

The n8n pipeline reads the catalog and the customer list LIVE from a Google Sheet on
every run, so the same email yields different results on different days and no
regression test can exist. These tests pin the replacement: parse the two sheet tabs,
freeze them into Postgres as an immutable, content-addressed snapshot, and let a run
reference the snapshot it used.
"""
import os

import pytest

from app.orders import snapshot

PG_DSN = os.environ.get("PG_TEST_DSN")

CATALOG_CSV = (
    "GTIN,Sklad,Názov,doplnok\n"
    "8588001805889,1,Bábovka mini kakaová 200g,\n"
    "8588001800013,1,  Rožok štandart 50g  ,\"rozok standard, žemľa 50g\"\n"
    ",1,Karta bez GTIN,alias\n"
    "8588001800020,1,,alias bez nazvu\n"
)

CUSTOMER_CSV = (
    "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
    '"TESCO STORES SR, a.s.",8589000020000,Bratislava,Cesta na Senec,,,gfr@tesco.com \n'
    "Potraviny nie otraviny Martin,2000000000864,Martin,Košútka 1,,,"
    "Marek Pavlovič <objednavky.pno.martin@gmail.com>\n"
    "Dva maily,2000000000871,Poprad,Hlavná 2,,,a@firma.sk; b@firma.sk\n"
    ",,,,,,nikto@nikde.sk\n"
)


# --- parsing -------------------------------------------------------------

def test_catalog_rows_are_trimmed_and_incomplete_rows_dropped():
    rows = snapshot.parse_catalog(CATALOG_CSV)
    assert [r["gtin"] for r in rows] == ["8588001805889", "8588001800013"]
    assert rows[1]["name"] == "Rožok štandart 50g", "surrounding spaces must be stripped"
    assert rows[1]["alias"] == "rozok standard, žemľa 50g"
    assert rows[0]["alias"] == "", "an empty alias is data, not a missing row"


def test_customer_emails_are_extracted_from_the_field_not_compared_whole():
    """The sheet writes 'Meno <adresa@dom>' and sometimes several addresses in one
    cell. Comparing the whole cell is what made the 30.07.2026 PNO Martin order fail:
    the customer WAS in the sheet, but the address was wrapped in a display name."""
    rows = snapshot.parse_customers(CUSTOMER_CSV)
    by_name = {r["name"]: r for r in rows}
    assert by_name["Potraviny nie otraviny Martin"]["emails"] == [
        "objednavky.pno.martin@gmail.com"]
    assert by_name["Dva maily"]["emails"] == ["a@firma.sk", "b@firma.sk"]
    assert by_name["TESCO STORES SR, a.s."]["emails"] == ["gfr@tesco.com"]
    assert "" not in by_name, "a row without a name is not a customer"


def test_customer_rows_keep_the_address_fields_used_for_matching():
    rows = snapshot.parse_customers(CUSTOMER_CSV)
    tesco = next(r for r in rows if r["ean_edi"] == "8589000020000")
    assert (tesco["city"], tesco["street"]) == ("Bratislava", "Cesta na Senec")


# --- import + versioning -------------------------------------------------

def test_import_creates_a_snapshot_that_can_be_read_back(pg):
    sid = snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    assert sid > 0
    assert snapshot.latest_snapshot_id(pg) == sid
    catalog = snapshot.load_catalog(pg, sid)
    customers = snapshot.load_customers(pg, sid)
    assert len(catalog) == 2
    assert len(customers) == 3
    assert catalog[0]["gtin"] and customers[0]["name"]


def test_reimporting_identical_content_does_not_create_a_new_snapshot(pg):
    """The importer runs hourly; unchanged sheets must not churn snapshot ids, or
    every run would reference a different id for identical data."""
    first = snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    second = snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    assert second == first
    assert pg.execute("SELECT count(*) FROM catalog_snapshot").fetchone()[0] == 2


def test_a_changed_sheet_creates_a_new_snapshot_and_the_old_one_survives(pg):
    """Reproducibility: a run that referenced the old snapshot must still be
    replayable after the sheet changes."""
    old = snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    changed = CATALOG_CSV + "8588001800037,1,Vianočka 400g,\n"
    new = snapshot.import_snapshot(pg, changed, CUSTOMER_CSV)
    assert new != old
    assert snapshot.latest_snapshot_id(pg) == new
    assert len(snapshot.load_catalog(pg, old)) == 2, "the old snapshot must not change"
    assert len(snapshot.load_catalog(pg, new)) == 3


def test_an_empty_sheet_is_refused_so_a_failed_fetch_cannot_wipe_the_catalog(pg):
    """A fetch that returns a header-only CSV (Google error page, revoked share)
    must NOT become a valid empty snapshot — the pipeline would then match nothing
    and reject every order."""
    good = snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    with pytest.raises(snapshot.SnapshotRefused):
        snapshot.import_snapshot(pg, "GTIN,Sklad,Názov,doplnok\n", CUSTOMER_CSV)
    assert snapshot.latest_snapshot_id(pg) == good


def test_snapshot_module_never_reads_the_sheet_over_the_network():
    """#129: fetch_csv/refresh/sheet_csv_url are gone entirely — Postgres
    (catalog_overrides/customer_overrides, #127/#128) is the sole source of truth.
    import_snapshot/import_files (pure CSV-text/frozen-file importers, no network) and
    every #127/#128 override function stay — only the network fetch path is removed."""
    assert not hasattr(snapshot, "fetch_csv")
    assert not hasattr(snapshot, "refresh")
    assert not hasattr(snapshot, "sheet_csv_url")


# --- the frozen snapshot the CI gate replays against ----------------------

# --- direct curation overrides (#127 catalog, #128 customers) -----------

def test_catalog_override_wins_over_the_sheet_row_with_the_same_gtin(pg):
    """#127: an edited name must beat whatever the sheet still says for that GTIN,
    on every future import — not just once."""
    snapshot.upsert_catalog_card(pg, "8588001805889", "Bábovka mini kakaová 200g OPRAVENÉ")
    sid = snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    catalog = {r["gtin"]: r for r in snapshot.load_catalog(pg, sid)}
    assert catalog["8588001805889"]["name"] == "Bábovka mini kakaová 200g OPRAVENÉ"


def test_catalog_override_can_add_a_brand_new_card_not_in_the_sheet(pg):
    snapshot.upsert_catalog_card(pg, "NEW1", "Nová karta")
    sid = snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    catalog = {r["gtin"]: r for r in snapshot.load_catalog(pg, sid)}
    assert catalog["NEW1"]["name"] == "Nová karta"


def test_retiring_a_catalog_card_excludes_it_even_though_the_sheet_still_has_it(pg):
    snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)   # the card must exist first
    ok = snapshot.retire_catalog_card(pg, "8588001805889")
    assert ok is True
    sid = snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    catalog = {r["gtin"] for r in snapshot.load_catalog(pg, sid)}
    assert "8588001805889" not in catalog
    assert "8588001800013" in catalog, "an unrelated card must survive untouched"


def test_retire_catalog_card_refuses_a_gtin_that_never_existed(pg):
    snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    assert snapshot.retire_catalog_card(pg, "NOPE-NEVER-EXISTED") is False


def test_rebuild_from_overrides_reflects_a_new_override_without_refetching_the_sheet(pg):
    """The whole point: the web edit must be visible on /znalosti immediately, not only
    after the next hourly sheet refresh."""
    sid = snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    snapshot.upsert_catalog_card(pg, "8588001800013", "Rožok - nový názov")
    new_sid = snapshot.rebuild_from_overrides(pg)
    assert new_sid != sid
    catalog = {r["gtin"]: r for r in snapshot.load_catalog(pg, new_sid)}
    assert catalog["8588001800013"]["name"] == "Rožok - nový názov"
    old_catalog = {r["gtin"]: r for r in snapshot.load_catalog(pg, sid)}
    assert old_catalog["8588001800013"]["name"] == "Rožok štandart 50g", \
        "the OLD snapshot must not change"


def test_rebuild_from_overrides_is_a_noop_before_any_snapshot_exists(pg):
    assert snapshot.rebuild_from_overrides(pg) is None


def test_latest_snapshot_id_tracks_the_current_state_even_when_content_reverts(pg):
    """Retiring a card can bring the merged content back to EXACTLY what an OLDER
    snapshot already had — `_freeze` correctly reuses that old id (content-addressed,
    #59), but `latest_snapshot_id` must still report it as CURRENT, not silently point
    at a higher-id snapshot that is no longer what the pipeline should be matching
    against. This is the exact shape retiring a #127 override produces in practice."""
    original = snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    snapshot.upsert_catalog_card(pg, "TEMP1", "Dočasná karta")
    with_temp = snapshot.rebuild_from_overrides(pg)
    assert with_temp != original
    assert snapshot.latest_snapshot_id(pg) == with_temp

    snapshot.retire_catalog_card(pg, "TEMP1")
    reverted = snapshot.rebuild_from_overrides(pg)
    assert reverted == original, "content is back to exactly what it was — same hash, reused id"
    assert snapshot.latest_snapshot_id(pg) == original, \
        "latest must track checked_at, not just the highest id ever inserted"
    assert "TEMP1" not in {r["gtin"] for r in snapshot.load_catalog(pg, snapshot.latest_snapshot_id(pg))}


def test_customer_override_replaces_the_sheet_row_it_names(pg):
    """#128's own cited data bug: EAN 2000000000857 says GT1, header/street says GT2."""
    snapshot.upsert_customer(
        pg, override_id=None, orig_ean_edi="2000000000871", orig_street="Hlavná 2",
        ean_edi="2000000000871", name="Dva maily OPRAVENÉ", emails=["a@firma.sk", "b@firma.sk"],
        city="Poprad", street="Hlavná 2", zip_="")
    sid = snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    customers = {r["name"]: r for r in snapshot.load_customers(pg, sid)}
    assert "Dva maily" not in customers
    assert customers["Dva maily OPRAVENÉ"]["ean_edi"] == "2000000000871"


# --- #234: the EAN kód EDI can never be silently forgotten -----------------------------

def test_upsert_customer_refuses_a_blank_ean(pg):
    with pytest.raises(snapshot.InvalidCustomer):
        snapshot.upsert_customer(
            pg, override_id=None, orig_ean_edi=None, orig_street=None,
            ean_edi="", name="Bez EAN", emails=[], city="", street="", zip_="")


def test_upsert_customer_refuses_a_non_numeric_ean(pg):
    with pytest.raises(snapshot.InvalidCustomer):
        snapshot.upsert_customer(
            pg, override_id=None, orig_ean_edi=None, orig_street=None,
            ean_edi="SK123", name="Zlý EAN", emails=[], city="", street="", zip_="")


def test_adding_a_brand_new_customer_twice_updates_one_row(pg):
    """Review finding on #234's own design (§2.2): the unique index below only covers a
    still-sheet-only row being edited (`orig_ean_edi IS NOT NULL`) — a genuinely brand-new
    customer had NO conflict target at all, so a double submit (or the auto-retry sweep
    re-adding the same sender) silently inserted TWO rows for one real customer, and
    `customer.resolve`'s exact_email rung then refuses ANY order from that address once
    `len(owners) > 1` — the order gets stuck BECAUSE the customer was added twice."""
    for name in ("Nový Zákazník", "Nový Zákazník OPRAVA"):
        snapshot.upsert_customer(
            pg, override_id=None, orig_ean_edi=None, orig_street=None,
            ean_edi="7000000000001", name=name, emails=["novy@x.sk"],
            city="Košice", street="Nová 5", zip_="04001")
    assert pg.execute(
        "SELECT count(*) FROM customer_overrides WHERE ean_edi='7000000000001'"
    ).fetchone()[0] == 1
    rows = [r for r in snapshot.customers_for_management(pg)
           if r["ean_edi"] == "7000000000001"]
    assert len(rows) == 1
    assert rows[0]["name"] == "Nový Zákazník OPRAVA"


def test_two_concurrent_brand_new_customer_adds_for_the_same_ean_produce_one_row(pg):
    """Deep-review finding on #234: the reclaim-or-insert decision in `upsert_customer`
    is a check-then-act with no db-level uniqueness of its own for a genuinely
    brand-new customer. Proven with two REAL, separate connections racing on the actual
    advisory lock — not a mock — to add the IDENTICAL new customer (same EAN + street)
    at (as close as possible to) the same instant. Exactly ONE row must survive."""
    import threading

    import psycopg
    from _race import run_racers

    barrier = threading.Barrier(2)
    errors = []

    def add(name):
        conn = psycopg.connect(PG_DSN)
        try:
            barrier.wait(timeout=5)
            snapshot.upsert_customer(
                conn, override_id=None, orig_ean_edi=None, orig_street=None,
                ean_edi="7000000000900", name=name, emails=["race@x.sk"],
                city="Košice", street="Preteková 1", zip_="")
            conn.commit()
        except Exception as e:  # pragma: no cover - surfaced via `errors` below
            errors.append(e)
        finally:
            conn.close()

    t1 = threading.Thread(target=add, args=("Pretekár A",), name="add-a")
    t2 = threading.Thread(target=add, args=("Pretekár B",), name="add-b")
    # #291: bounded join() alone never kills a genuinely-stalled thread — run_racers
    # fails loudly + cleans up any stray backend instead of wedging later tests.
    run_racers(pg, [t1, t2], timeout=15, label="brand_new_customer")

    assert not errors, f"a racing upsert_customer call must never raise: {errors}"
    assert pg.execute(
        "SELECT count(*) FROM customer_overrides WHERE ean_edi='7000000000900'"
    ).fetchone() == (1,), "two concurrent brand-new-customer adds must leave ONE row"


def test_two_concurrent_new_customer_adds_for_the_same_ean_with_different_street_produce_one_row(pg):
    """#248: the actual bug — the test above races two IDENTICAL submissions (same
    street), which only proves the idempotent-retry path is safe. Here the two racing
    submissions carry the SAME brand-new EAN but a DIFFERENT street, i.e. two genuinely
    different customers colliding on one EAN — exactly what the ticket reports
    (`customer.resolve` sees len(owners) > 1 and the order gets stuck, or worse, a stale
    duplicate wins). Before the fix, `upsert_customer`'s reclaim SELECT was scoped
    `AND street = %s` — one notch narrower than the advisory lock above it (keyed on
    `ean_edi` alone) — so the two calls were serialized by the lock, but the SECOND
    caller's own reclaim SELECT never saw the FIRST caller's already-committed row
    (different street ⇒ no match) and fell through to INSERT: two rows sharing one EAN,
    with no error raised at all. Exactly ONE row must survive, and the loser must be
    told about the conflict (not silently succeed)."""
    import threading

    import psycopg
    from _race import run_racers

    barrier = threading.Barrier(2)
    errors = []

    def add(name, street):
        conn = psycopg.connect(PG_DSN)
        try:
            barrier.wait(timeout=5)
            snapshot.upsert_customer(
                conn, override_id=None, orig_ean_edi=None, orig_street=None,
                ean_edi="7000000000901", name=name, emails=["race2@x.sk"],
                city="Košice", street=street, zip_="")
            conn.commit()
        except Exception as e:  # pragma: no cover - surfaced via `errors` below
            errors.append(e)
        finally:
            conn.close()

    t1 = threading.Thread(target=add, args=("Pretekár Košice", "Ulica A 1"), name="add-a")
    t2 = threading.Thread(target=add, args=("Pretekár Prešov", "Ulica B 2"), name="add-b")
    # #291: bounded join() alone never kills a genuinely-stalled thread — run_racers
    # fails loudly + cleans up any stray backend instead of wedging later tests.
    run_racers(pg, [t1, t2], timeout=15, label="different_street")

    assert pg.execute(
        "SELECT count(*) FROM customer_overrides WHERE ean_edi='7000000000901'"
    ).fetchone() == (1,), (
        "two concurrent new-customer adds with different streets must leave ONE row, "
        "not two duplicate customers sharing an EAN")
    assert len(errors) == 1, (
        f"exactly one of the two conflicting submissions must be refused, got: {errors}")
    assert isinstance(errors[0], snapshot.DuplicateEan), (
        f"the refused submission must raise DuplicateEan, not silently fail: {errors[0]!r}")


def test_editing_an_already_overridden_customer_by_override_id_into_a_colliding_ean_raises(pg):
    """#248 review finding: the `override_id is not None` branch (editing an
    already-overridden row by its own id — the /znalosti admin dashboard's second edit
    of the same row) had NO uniqueness check of its own at all before this fix; it
    would have silently retargeted the row's EAN onto another active customer's EAN,
    creating the exact same #248 duplicate-EAN bug from a different screen. Now the
    partial unique index (db.py) turns that into `DuplicateEan`, not a duplicate row
    and not a raw crash."""
    a_id = snapshot.upsert_customer(
        pg, override_id=None, orig_ean_edi=None, orig_street=None,
        ean_edi="7100000000001", name="Zákazník A", emails=["a@x.sk"],
        city="Košice", street="Ulica A", zip_="")
    b_id = snapshot.upsert_customer(
        pg, override_id=None, orig_ean_edi=None, orig_street=None,
        ean_edi="7100000000002", name="Zákazník B", emails=["b@x.sk"],
        city="Prešov", street="Ulica B", zip_="")

    with pytest.raises(snapshot.DuplicateEan) as exc_info:
        snapshot.upsert_customer(
            pg, override_id=b_id, orig_ean_edi=None, orig_street=None,
            ean_edi="7100000000001",  # A's EAN, but B's own (different) street
            name="Zákazník B premenovaný", emails=["b2@x.sk"],
            city="Prešov", street="Ulica B", zip_="")
    assert exc_info.value.existing["override_id"] == a_id

    # Neither row was corrupted by the failed edit.
    rows = {r["ean_edi"]: r["name"] for r in snapshot.customers_for_management(pg)}
    assert rows["7100000000001"] == "Zákazník A"
    assert rows["7100000000002"] == "Zákazník B"


def test_customer_override_can_add_a_brand_new_customer(pg):
    snapshot.upsert_customer(
        pg, override_id=None, orig_ean_edi=None, orig_street=None,
        ean_edi="9999999999999", name="Nový odberateľ", emails=["novy@x.sk"],
        city="Košice", street="Nová 1", zip_="04001")
    sid = snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    customers = {r["name"]: r for r in snapshot.load_customers(pg, sid)}
    assert customers["Nový odberateľ"]["ean_edi"] == "9999999999999"


def test_retiring_a_customer_override_excludes_it(pg):
    ok = snapshot.retire_customer(pg, override_id=None,
                                   orig_ean_edi="2000000000864", orig_street="Košútka 1")
    assert ok is True
    sid = snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    customers = {r["ean_edi"] for r in snapshot.load_customers(pg, sid)}
    assert "2000000000864" not in customers
    assert "8589000020000" in customers, "an unrelated customer must survive untouched"


def test_retiring_one_customer_does_not_drop_an_unrelated_blank_identity_customer(pg):
    """Review finding (code review of #127/#128): `retire_customer`'s orig-identity path
    used to hardcode ean_edi/street="" as the override's OWN "current identity", which made
    `_merge_customers` unconditionally exclude EVERY customer with a blank ean_edi AND a
    blank street from every future merge — not just the one actually being retired. Both
    fields are legitimately optional (db.py's own `customer_snapshot` comment), so a real
    customer known only by name/email would silently vanish from matching."""
    catalog_csv = "GTIN,Názov,doplnok\nG1,Rožok,\n"
    customer_csv = (
        "Názov organizácie,EAN kód EDI,Obec,Ulica,E-mail\n"
        "Zákazník na vyradenie,2000000000864,Martin,Košútka 1,prvy@x.sk\n"
        "Nevinný okolostojaci bez EAN a ulice,,,,druhy@x.sk\n")
    snapshot.import_snapshot(pg, catalog_csv, customer_csv)
    ok = snapshot.retire_customer(pg, override_id=None,
                                   orig_ean_edi="2000000000864", orig_street="Košútka 1")
    assert ok is True
    sid = snapshot.import_snapshot(pg, catalog_csv, customer_csv)
    names = {r["name"] for r in snapshot.load_customers(pg, sid)}
    assert "Zákazník na vyradenie" not in names
    assert "Nevinný okolostojaci bez EAN a ulice" in names, \
        "an unrelated blank-identity customer must survive"


def test_retire_customer_needs_some_identity(pg):
    assert snapshot.retire_customer(pg, override_id=None,
                                     orig_ean_edi=None, orig_street=None) is False


def test_editing_a_customer_email_via_override_changes_what_customer_resolve_matches(pg):
    """#128's own regression requirement: an override is not just a stored row, it must
    actually change matching — the same bar #104's alias PR held itself to."""
    from app.orders import customer as customer_mod

    sid = snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    customers = snapshot.load_customers(pg, sid)
    assert customer_mod.resolve(customers, "nova@adresa.sk", "", "") is None

    snapshot.upsert_customer(
        pg, override_id=None, orig_ean_edi="2000000000864", orig_street="Košútka 1",
        ean_edi="2000000000864", name="Potraviny nie otraviny Martin",
        emails=["nova@adresa.sk"], city="Martin", street="Košútka 1", zip_="")
    new_sid = snapshot.rebuild_from_overrides(pg)
    new_customers = snapshot.load_customers(pg, new_sid)
    hit = customer_mod.resolve(new_customers, "nova@adresa.sk", "", "")
    assert hit is not None and hit.ean_edi == "2000000000864"


def test_upsert_customer_by_orig_identity_is_idempotent_not_duplicated(pg):
    """Editing the SAME still-sheet-only row twice (without knowing its override id yet,
    which is exactly what the /znalosti edit form does) must update in place."""
    snapshot.upsert_customer(
        pg, override_id=None, orig_ean_edi="2000000000871", orig_street="Hlavná 2",
        ean_edi="2000000000871", name="Prvá oprava", emails=[], city="Poprad",
        street="Hlavná 2", zip_="")
    snapshot.upsert_customer(
        pg, override_id=None, orig_ean_edi="2000000000871", orig_street="Hlavná 2",
        ean_edi="2000000000871", name="Druhá oprava", emails=[], city="Poprad",
        street="Hlavná 2", zip_="")
    assert pg.execute("SELECT count(*) FROM customer_overrides").fetchone()[0] == 1
    rows = snapshot.customers_for_management(pg)
    names = [r["name"] for r in rows if r["orig_ean_edi"] == "2000000000871"]
    assert names == ["Druhá oprava"]


def test_customers_for_management_marks_override_identity_for_editing(pg):
    """The /znalosti edit form needs override_id (None for a still-sheet-only row) plus
    orig_ean_edi/orig_street, so a repeat edit targets the right override row."""
    snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    rows = {r["name"]: r for r in snapshot.customers_for_management(pg)}
    assert rows["Dva maily"]["override_id"] is None
    assert rows["Dva maily"]["orig_ean_edi"] == "2000000000871"

    rid = snapshot.upsert_customer(
        pg, override_id=None, orig_ean_edi="2000000000871", orig_street="Hlavná 2",
        ean_edi="2000000000871", name="Dva maily v2", emails=[], city="Poprad",
        street="Hlavná 2", zip_="")
    rows2 = {r["name"]: r for r in snapshot.customers_for_management(pg)}
    assert rows2["Dva maily v2"]["override_id"] == rid


def test_a_snapshot_can_be_imported_from_frozen_files(pg, tmp_path):
    """The golden corpus pins the catalog it was written against. Re-fetching the live
    sheet in CI would silently invalidate every expected GTIN, so the gate imports FILES."""
    cat = tmp_path / "catalog.csv"
    cat.write_text("GTIN,Sklad,Názov,doplnok\n8588001800013,1,Rožok štandart 50g,\n",
                   encoding="utf-8")
    cust = tmp_path / "customers.csv"
    cust.write_text(
        "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
        "Pekáreň s.r.o.,2000000000864,Martin,Košútka 1,,,sklad@pekaren.sk\n",
        encoding="utf-8")

    sid = snapshot.import_files(pg, str(cat), str(cust))
    assert sid == snapshot.latest_snapshot_id(pg)
    assert pg.execute("SELECT count(*) FROM catalog_snapshot WHERE snapshot_id = %s",
                      (sid,)).fetchone()[0] == 1
    assert pg.execute("SELECT count(*) FROM customer_snapshot WHERE snapshot_id = %s",
                      (sid,)).fetchone()[0] == 1
    # unchanged content reuses the snapshot instead of piling up copies
    assert snapshot.import_files(pg, str(cat), str(cust)) == sid


# --- #159: remembering an answered "who is this customer?" address durably --------

def test_remember_customer_email_appends_to_a_still_sheet_only_row(pg):
    """#159's own regression bar: an answered customer question must remember the sender
    address the SAME way #128's own regression test above proves — a real change to what
    `customer.resolve` matches next time, surviving a snapshot rebuild."""
    from app.orders import customer as customer_mod

    snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    sid = snapshot.latest_snapshot_id(pg)
    before = snapshot.load_customers(pg, sid)
    assert customer_mod.resolve(before, "zilina@farmeria.sk", "", "") is None

    ok = snapshot.remember_customer_email(pg, "2000000000864", "zilina@farmeria.sk")
    assert ok is True
    new_sid = snapshot.rebuild_from_overrides(pg)
    after = snapshot.load_customers(pg, new_sid)
    hit = customer_mod.resolve(after, "zilina@farmeria.sk", "", "")
    assert hit is not None and hit.ean_edi == "2000000000864"
    # the ORIGINAL address is still there too — this APPENDS, never replaces
    row = next(r for r in after if r["ean_edi"] == "2000000000864")
    assert "objednavky.pno.martin@gmail.com" in row["emails"]
    assert "zilina@farmeria.sk" in row["emails"]


def test_remember_customer_email_appends_to_an_already_overridden_row(pg):
    snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    snapshot.upsert_customer(
        pg, override_id=None, orig_ean_edi="2000000000864", orig_street="Košútka 1",
        ean_edi="2000000000864", name="Potraviny nie otraviny Martin",
        emails=["objednavky.pno.martin@gmail.com"], city="Martin", street="Košútka 1",
        zip_="")
    ok = snapshot.remember_customer_email(pg, "2000000000864", "novy@x.sk")
    assert ok is True
    rows = snapshot.customers_for_management(pg)
    row = next(r for r in rows if r["ean_edi"] == "2000000000864")
    assert "novy@x.sk" in row["emails"]
    assert "objednavky.pno.martin@gmail.com" in row["emails"]
    # still exactly ONE override row for this customer — an edit, not a duplicate
    assert pg.execute(
        "SELECT count(*) FROM customer_overrides WHERE ean_edi='2000000000864'"
    ).fetchone()[0] == 1


def test_remember_customer_email_is_a_noop_when_already_known(pg):
    snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    assert snapshot.remember_customer_email(
        pg, "2000000000864", "OBJEDNAVKY.PNO.MARTIN@GMAIL.COM") is False
    assert pg.execute("SELECT count(*) FROM customer_overrides").fetchone()[0] == 0


def test_remember_customer_email_is_a_noop_for_an_unknown_ean(pg):
    snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    assert snapshot.remember_customer_email(pg, "9999999999999", "x@x.sk") is False


# --- #275: sheet-bound EDIT branch vs an active hand-added row's EAN -------------------

def test_editing_a_sheet_bound_customer_into_a_colliding_active_hand_added_ean_raises(pg):
    """#275: the fallthrough branch (`orig_ean_edi IS NOT NULL`, editing a still-sheet-
    only row) has NO uniqueness check of its own — the #248 partial index deliberately
    excludes it (`WHERE orig_ean_edi IS NULL`), since the sheet legitimately repeats/
    blanks EAN across branches. An edit that would retarget `ean_edi` onto a value an
    ACTIVE hand-added row already holds must be refused with the same clean
    `DuplicateEan` every other collision path raises."""
    snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    hand_added_id = snapshot.upsert_customer(
        pg, override_id=None, orig_ean_edi=None, orig_street=None,
        ean_edi="7200000000001", name="Ručne pridaný zákazník", emails=["rucny@x.sk"],
        city="Košice", street="Ručná 1", zip_="")

    with pytest.raises(snapshot.DuplicateEan) as exc_info:
        snapshot.upsert_customer(
            pg, override_id=None, orig_ean_edi="2000000000871", orig_street="Hlavná 2",
            ean_edi="7200000000001",  # collides with the hand-added row's EAN
            name="Dva maily prepísané", emails=["a@firma.sk", "b@firma.sk"],
            city="Poprad", street="Hlavná 2", zip_="")
    assert exc_info.value.existing["override_id"] == hand_added_id

    # neither row was corrupted by the refused edit
    rows = {r["ean_edi"]: r["name"] for r in snapshot.customers_for_management(pg)}
    assert rows["7200000000001"] == "Ručne pridaný zákazník"
    assert "Dva maily" in {r["name"] for r in snapshot.customers_for_management(pg)}


def test_editing_a_sheet_bound_customer_into_a_retired_hand_added_eans_ean_is_allowed(pg):
    """The new guard reuses `_active_customer_conflict`, which already filters `NOT
    retired` — a hand-added row that was retired must never block a sheet-bound edit."""
    snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    hand_added_id = snapshot.upsert_customer(
        pg, override_id=None, orig_ean_edi=None, orig_street=None,
        ean_edi="7200000000002", name="Zrušený zákazník", emails=["x@x.sk"],
        city="Košice", street="Ručná 2", zip_="")
    assert snapshot.retire_customer(pg, override_id=hand_added_id,
                                    orig_ean_edi=None, orig_street=None) is True

    rid = snapshot.upsert_customer(
        pg, override_id=None, orig_ean_edi="2000000000871", orig_street="Hlavná 2",
        ean_edi="7200000000002",  # same EAN as the now-RETIRED hand-added row
        name="Dva maily prepísané", emails=["a@firma.sk", "b@firma.sk"],
        city="Poprad", street="Hlavná 2", zip_="")
    assert rid is not None
    rows = {r["ean_edi"]: r["name"] for r in snapshot.customers_for_management(pg)}
    assert rows["7200000000002"] == "Dva maily prepísané"


def test_editing_a_sheet_bound_customer_whose_original_ean_was_blank_is_allowed(pg):
    """The sheet legitimately leaves EAN blank (this module's own documented intent) —
    `orig_ean_edi` for such a row is the empty string `""`, not `None`, and still enters
    this branch (`"" is not None`). The new guard must not treat that specially; a
    non-colliding edit must succeed exactly like editing any other sheet-bound row."""
    customer_csv = (
        "Názov organizácie,EAN kód EDI,Obec,Ulica,E-mail\n"
        "Zákazník bez EAN v hárku,,Žilina,Bez EAN 1,ziadny@x.sk\n")
    catalog_csv = "GTIN,Názov,doplnok\nG1,Rožok,\n"
    snapshot.import_snapshot(pg, catalog_csv, customer_csv)
    rid = snapshot.upsert_customer(
        pg, override_id=None, orig_ean_edi="", orig_street="Bez EAN 1",
        ean_edi="7200000000003", name="Zákazník bez EAN v hárku", emails=["ziadny@x.sk"],
        city="Žilina", street="Bez EAN 1", zip_="")
    assert rid is not None
    rows = {r["name"]: r for r in snapshot.customers_for_management(pg)}
    assert rows["Zákazník bez EAN v hárku"]["ean_edi"] == "7200000000003"


def test_editing_two_different_sheet_bound_customers_to_share_the_same_ean_is_allowed(pg):
    """The sheet legitimately repeats an EAN across branches (this module's own
    documented intent, unchanged by #275) — the new guard only ever checks against
    ACTIVE hand-added rows, never against other sheet-bound rows, so two sheet-bound
    edits sharing a target EAN must both succeed."""
    snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    snapshot.upsert_customer(
        pg, override_id=None, orig_ean_edi="2000000000864", orig_street="Košútka 1",
        ean_edi="7200000000004", name="Pobočka A", emails=[], city="Martin",
        street="Košútka 1", zip_="")
    snapshot.upsert_customer(
        pg, override_id=None, orig_ean_edi="2000000000871", orig_street="Hlavná 2",
        ean_edi="7200000000004", name="Pobočka B", emails=[], city="Poprad",
        street="Hlavná 2", zip_="")
    names = {r["name"] for r in snapshot.customers_for_management(pg)
             if r["ean_edi"] == "7200000000004"}
    assert names == {"Pobočka A", "Pobočka B"}


def test_two_concurrent_sheet_bound_edits_sharing_a_target_ean_do_not_deadlock(pg):
    """#275: the new guard takes the SAME advisory lock (keyed on `ean_edi`) the
    brand-new-customer branch already takes — this proves that lock does not turn the
    LEGITIMATE branch-sharing case above into a deadlock or spurious failure under real
    concurrency (two different sheet-bound rows, two real connections/threads, same
    target EAN, no hand-added row involved at all)."""
    import threading

    import psycopg
    from _race import run_racers

    snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    barrier = threading.Barrier(2)
    errors = []

    def edit(orig_ean_edi, orig_street, name, city, street):
        conn = psycopg.connect(PG_DSN)
        try:
            barrier.wait(timeout=5)
            snapshot.upsert_customer(
                conn, override_id=None, orig_ean_edi=orig_ean_edi, orig_street=orig_street,
                ean_edi="7200000000005", name=name, emails=[], city=city, street=street,
                zip_="")
            conn.commit()
        except Exception as e:  # pragma: no cover - surfaced via `errors` below
            errors.append(e)
        finally:
            conn.close()

    t1 = threading.Thread(target=edit, args=(
        "2000000000864", "Košútka 1", "Pobočka A súbežne", "Martin", "Košútka 1"),
        name="edit-a")
    t2 = threading.Thread(target=edit, args=(
        "2000000000871", "Hlavná 2", "Pobočka B súbežne", "Poprad", "Hlavná 2"),
        name="edit-b")
    # #291: bounded join() alone never kills a genuinely-stalled thread — run_racers
    # fails loudly + cleans up any stray backend instead of wedging later tests.
    run_racers(pg, [t1, t2], timeout=15, label="sheet_bound_no_deadlock")

    assert not errors, f"two legitimate branch-sharing edits must never raise: {errors}"
    names = {r["name"] for r in snapshot.customers_for_management(pg)
             if r["ean_edi"] == "7200000000005"}
    assert names == {"Pobočka A súbežne", "Pobočka B súbežne"}
