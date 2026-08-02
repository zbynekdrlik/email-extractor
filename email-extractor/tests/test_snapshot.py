"""Catalog/customer snapshots (#59).

The n8n pipeline reads the catalog and the customer list LIVE from a Google Sheet on
every run, so the same email yields different results on different days and no
regression test can exist. These tests pin the replacement: parse the two sheet tabs,
freeze them into Postgres as an immutable, content-addressed snapshot, and let a run
reference the snapshot it used.
"""
import pytest

from app.orders import snapshot

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


def test_sheet_csv_url_targets_the_configured_document_and_tab():
    url = snapshot.sheet_csv_url("DOC123", 957145124)
    assert url == ("https://docs.google.com/spreadsheets/d/DOC123/export"
                   "?format=csv&gid=957145124")


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
    assert snapshot.load_catalog(pg, sid)[1]["name"] == "Rožok štandart 50g", \
        "the OLD snapshot must not change"


def test_rebuild_from_overrides_is_a_noop_before_any_snapshot_exists(pg):
    assert snapshot.rebuild_from_overrides(pg) is None


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
