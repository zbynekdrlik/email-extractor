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
