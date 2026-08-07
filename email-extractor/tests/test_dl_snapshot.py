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


# --- _num: real Sheets export noise (review finding on #200's PR) ----------------

def test_num_strips_nbsp_thousands_separator_from_a_formatted_export():
    assert dl_snapshot._num("1\xa0133,00") == 1133.0


def test_num_strips_a_trailing_currency_symbol():
    assert dl_snapshot._num("12,50 €") == 12.5


def test_num_handles_dot_thousands_plus_comma_decimal():
    assert dl_snapshot._num("1.234,50") == 1234.5


def test_num_returns_none_for_garbage():
    assert dl_snapshot._num("not a number") is None


# --- R20: the union of two tabs — GTIN-deduped, DL tab wins on overlap -----------
# (a deliberate deviation from n8n's literal Merge(append), which keeps both rows —
# see dl_snapshot.merge_catalog's own docstring; review finding on #200's PR)

def test_merge_catalog_unions_both_tabs():
    merged = dl_snapshot.merge_catalog(DL_CATALOG_CSV, OBJEDNAVKY_CATALOG_CSV)
    gtins = [r["gtin"] for r in merged]
    assert "8588001900001" in gtins and "8588001900002" in gtins
    assert "8588001805889" in gtins and "8588001800013" in gtins
    assert len(gtins) == len(set(gtins)) == 4, "no duplicate gtins in the merged result"


def test_merged_rows_from_the_objednavky_tab_still_carry_sklad():
    """The original version routed this tab through the orders-only parser, which
    silently dropped Sklad — a real R84 kg-tracking signal (review finding on #200's
    PR, the exact W14 class of bug this module claims to have eliminated). Only
    fields truly absent from that tab's header (mass, cena) stay None."""
    merged = dl_snapshot.merge_catalog(DL_CATALOG_CSV, OBJEDNAVKY_CATALOG_CSV)
    row = next(r for r in merged if r["gtin"] == "8588001805889")
    assert row["sklad"] == "1"
    assert row["mass"] is None and row["cena"] is None


def test_objednavky_alias_maps_into_doplnok():
    merged = dl_snapshot.merge_catalog(DL_CATALOG_CSV, OBJEDNAVKY_CATALOG_CSV)
    row = next(r for r in merged if r["gtin"] == "8588001800013")
    assert row["doplnok"] == "rozok standard"


def test_a_gtin_shared_by_both_tabs_dedupes_with_the_dl_tab_winning():
    """Review finding on #200's PR: without this, the DB's own primary key
    (snapshot_id, gtin) would silently swallow the second row anyway, but the
    content hash would still be computed over BOTH — churning a new snapshot id
    for content that was never actually going to be stored."""
    dl_csv = DL_CATALOG_CSV + "8588001805889,Bábovka DL-strana,alias-dl,0.2,1,\"3,50\"\n"
    merged = dl_snapshot.merge_catalog(dl_csv, OBJEDNAVKY_CATALOG_CSV)
    rows = [r for r in merged if r["gtin"] == "8588001805889"]
    assert len(rows) == 1
    assert rows[0]["name"] == "Bábovka DL-strana", "the DL-specific tab must win"
    assert rows[0]["mass"] == 0.2


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
    changed = DL_CATALOG_CSV + "8588001900099,Vianočka 400g,,0.4,1,\"4,20\"\n"
    new = dl_snapshot.import_snapshot(pg, changed, OBJEDNAVKY_CATALOG_CSV, SUPPLIER_CSV)
    assert new != old
    assert dl_snapshot.latest_snapshot_id(pg) == new
    assert len(dl_snapshot.load_catalog(pg, old)) == 4, "the old snapshot must not change"
    new_rows = {r["gtin"]: r for r in dl_snapshot.load_catalog(pg, new)}
    assert len(new_rows) == 5
    assert new_rows["8588001900099"]["cena"] == 4.20, \
        "the price must parse correctly now that it's properly quoted (review finding)"


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
