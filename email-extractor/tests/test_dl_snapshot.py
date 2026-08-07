"""DL catalog + supplier snapshots (#200 F1). Mirrors tests/test_snapshot.py's shape
for the AI-orders snapshot — the two things under test that are genuinely NEW here:
R20's catalog UNION of two tabs, and the dedicated dl_snapshots versioning line.
"""
import pytest

from app.orders import dl_snapshot, snapshot

DL_CATALOG_CSV = (
    "GTIN,Názov,doplnok,hmotnost,Sklad,Cena\n"
    "8588001900001,Múka hladká T512 25kg,,25,100,\"12,50\"\n"
    "8588001900002,  Olej repkový 10l  ,\"olej repka\",10,200,\"9,90\"\n"
    ",Karta bez GTIN,alias,,,\n"
    "8588001900003,,alias bez nazvu,,,\n"
)

OBJEDNAVKY_CATALOG_CSV = (
    "GTIN,Sklad,Názov,doplnok\n"
    "8588001805889,1,Bábovka mini kakaová 200g,\n"
    "8588001800013,1,Rožok štandart 50g,rozok standard\n"
)

SUPPLIER_CSV = (
    "Názov organizácie,EAN kód EDI,Obec,Ulica,E-mail\n"
    "Signatus s.r.o.,8586010000001,Košice,Priemyselná 1,objednavky@signatus.sk\n"
    "Jackulík,8586010000002,Prešov,Hlavná 5,info@jackulik.sk\n"
    ",,,,nikto@nikde.sk\n"
)


# --- parsing ---------------------------------------------------------------------

def test_dl_catalog_rows_are_trimmed_and_incomplete_rows_dropped():
    rows = dl_snapshot.parse_dl_catalog(DL_CATALOG_CSV)
    assert [r["gtin"] for r in rows] == ["8588001900001", "8588001900002"]
    assert rows[1]["name"] == "Olej repkový 10l", "surrounding spaces must be stripped"


def test_dl_catalog_reads_the_dl_specific_fields_r20():
    rows = {r["gtin"]: r for r in dl_snapshot.parse_dl_catalog(DL_CATALOG_CSV)}
    row = rows["8588001900001"]
    assert row["mass"] == 25.0
    assert row["sklad"] == "100"
    assert row["cena"] == 12.5


def test_dl_catalog_price_and_mass_accept_comma_decimal_separator():
    """The sheet is Slovak-locale — '9,90' must parse as 9.90, not fail or truncate."""
    rows = {r["gtin"]: r for r in dl_snapshot.parse_dl_catalog(DL_CATALOG_CSV)}
    assert rows["8588001900002"]["cena"] == 9.90


def test_a_missing_mass_or_price_is_none_not_zero():
    """A genuinely absent value must stay distinguishable from an actual zero — R85's
    price fallback rule (a later phase) depends on telling 'no price' from '0 price' apart."""
    csv = "GTIN,Názov,doplnok,hmotnost,Sklad,Cena\nG1,Karta,,,,\n"
    row = dl_snapshot.parse_dl_catalog(csv)[0]
    assert row["mass"] is None
    assert row["cena"] is None


# --- R20: the union of two tabs, straight append (mirrors n8n Merge(append)) ------

def test_merge_catalog_is_a_straight_union_of_both_tabs():
    merged = dl_snapshot.merge_catalog(DL_CATALOG_CSV, OBJEDNAVKY_CATALOG_CSV)
    gtins = [r["gtin"] for r in merged]
    assert "8588001900001" in gtins and "8588001900002" in gtins
    assert "8588001805889" in gtins and "8588001800013" in gtins
    assert len(gtins) == 4, "no dedup — straight append, matching n8n's Merge(append)"


def test_merged_rows_from_the_objednavky_tab_carry_dl_fields_as_none():
    merged = dl_snapshot.merge_catalog(DL_CATALOG_CSV, OBJEDNAVKY_CATALOG_CSV)
    row = next(r for r in merged if r["gtin"] == "8588001805889")
    assert row["mass"] is None and row["cena"] is None
    assert row["doplnok"] == "", "the objednavky tab's own alias column maps to doplnok"


def test_objednavky_alias_maps_into_doplnok():
    merged = dl_snapshot.merge_catalog(DL_CATALOG_CSV, OBJEDNAVKY_CATALOG_CSV)
    row = next(r for r in merged if r["gtin"] == "8588001800013")
    assert row["doplnok"] == "rozok standard"


# --- R21: suppliers reuse the orders customer parser verbatim ---------------------

def test_suppliers_are_parsed_the_same_way_customers_are():
    suppliers = dl_snapshot.parse_suppliers(SUPPLIER_CSV)
    assert suppliers == snapshot.parse_customers(SUPPLIER_CSV)
    names = {r["name"] for r in suppliers}
    assert "Signatus s.r.o." in names and "Jackulík" in names
    assert "" not in names


# --- import + versioning (own dl_snapshots line, separate from order_snapshots) ---

def test_import_creates_a_dl_snapshot_that_can_be_read_back(pg):
    sid = dl_snapshot.import_snapshot(pg, DL_CATALOG_CSV, OBJEDNAVKY_CATALOG_CSV, SUPPLIER_CSV)
    assert sid > 0
    assert dl_snapshot.latest_snapshot_id(pg) == sid
    catalog = dl_snapshot.load_catalog(pg, sid)
    suppliers = dl_snapshot.load_suppliers(pg, sid)
    assert len(catalog) == 4
    assert len(suppliers) == 2


def test_reimporting_identical_content_does_not_create_a_new_snapshot(pg):
    first = dl_snapshot.import_snapshot(pg, DL_CATALOG_CSV, OBJEDNAVKY_CATALOG_CSV, SUPPLIER_CSV)
    second = dl_snapshot.import_snapshot(pg, DL_CATALOG_CSV, OBJEDNAVKY_CATALOG_CSV, SUPPLIER_CSV)
    assert second == first
    assert pg.execute("SELECT count(*) FROM dl_snapshots").fetchone()[0] == 1


def test_a_changed_dl_sheet_creates_a_new_snapshot_and_the_old_one_survives(pg):
    old = dl_snapshot.import_snapshot(pg, DL_CATALOG_CSV, OBJEDNAVKY_CATALOG_CSV, SUPPLIER_CSV)
    changed = DL_CATALOG_CSV + "8588001900099,Vianočka 400g,,0.4,1,4,20\n"
    new = dl_snapshot.import_snapshot(pg, changed, OBJEDNAVKY_CATALOG_CSV, SUPPLIER_CSV)
    assert new != old
    assert dl_snapshot.latest_snapshot_id(pg) == new
    assert len(dl_snapshot.load_catalog(pg, old)) == 4, "the old snapshot must not change"
    assert len(dl_snapshot.load_catalog(pg, new)) == 5


def test_an_empty_dl_catalog_is_refused_so_a_failed_fetch_cannot_wipe_it(pg):
    good = dl_snapshot.import_snapshot(pg, DL_CATALOG_CSV, OBJEDNAVKY_CATALOG_CSV, SUPPLIER_CSV)
    empty_objednavky = "GTIN,Sklad,Názov,doplnok\n"
    empty_dl = "GTIN,Názov,doplnok,hmotnost,Sklad,Cena\n"
    with pytest.raises(dl_snapshot.SnapshotRefused):
        dl_snapshot.import_snapshot(pg, empty_dl, empty_objednavky, SUPPLIER_CSV)
    assert dl_snapshot.latest_snapshot_id(pg) == good


def test_an_empty_supplier_sheet_is_also_refused(pg):
    good = dl_snapshot.import_snapshot(pg, DL_CATALOG_CSV, OBJEDNAVKY_CATALOG_CSV, SUPPLIER_CSV)
    with pytest.raises(dl_snapshot.SnapshotRefused):
        dl_snapshot.import_snapshot(pg, DL_CATALOG_CSV, OBJEDNAVKY_CATALOG_CSV,
                                    "Názov organizácie,EAN kód EDI,Obec,E-mail\n")
    assert dl_snapshot.latest_snapshot_id(pg) == good


def test_dl_and_orders_snapshots_are_independent_versioning_lines(pg):
    """A change to the AI-orders snapshot must never mint a new DL snapshot id, and
    vice versa — the whole reason dl_snapshots is a SEPARATE table."""
    dl_sid = dl_snapshot.import_snapshot(pg, DL_CATALOG_CSV, OBJEDNAVKY_CATALOG_CSV, SUPPLIER_CSV)
    orders_sid = snapshot.import_snapshot(pg, OBJEDNAVKY_CATALOG_CSV, SUPPLIER_CSV)
    assert pg.execute("SELECT count(*) FROM dl_snapshots").fetchone()[0] == 1
    assert pg.execute("SELECT count(*) FROM order_snapshots").fetchone()[0] == 1
    # re-freezing the orders side with unchanged content must not touch dl_snapshots
    snapshot.import_snapshot(pg, OBJEDNAVKY_CATALOG_CSV, SUPPLIER_CSV)
    assert dl_snapshot.latest_snapshot_id(pg) == dl_sid
    assert snapshot.latest_snapshot_id(pg) == orders_sid
