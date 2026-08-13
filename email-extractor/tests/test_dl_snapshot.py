"""DL catalog + supplier snapshots (#200 F1). Mirrors tests/test_snapshot.py's shape
for the AI-orders snapshot — the two things under test that are genuinely NEW here:
R20's catalog UNION of two tabs, and the dedicated dl_snapshots versioning line.
"""
import os

import pytest

from app.orders import dl_snapshot, snapshot

PG_DSN = os.environ.get("PG_TEST_DSN")

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


# --- #129: the sheet is never read at all any more --------------------------------

def test_dl_snapshot_module_never_reads_the_sheet_over_the_network():
    """fetch_csv (re-exported from snapshot.py) and refresh() must both be gone —
    import_snapshot (pure CSV-text importer, no network) stays, still used by tests
    and by dl_eval_run.py's corpus import from frozen fixture files."""
    assert not hasattr(dl_snapshot, "refresh")
    assert not hasattr(dl_snapshot, "fetch_csv")


def test_parse_number_is_the_public_wrapper_the_dashboard_form_uses():
    """#221: the /znalosti dashboard's mass/cena text fields need the SAME tolerant
    parsing a sheet cell already got (comma decimal, currency symbol) — reuse _num
    via a public name instead of duplicating a parser."""
    assert dl_snapshot.parse_number("12,50 €") == 12.50
    assert dl_snapshot.parse_number("") is None
    assert dl_snapshot.parse_number(None) is None


# --- #221: direct curation overrides (mirror of #127/#128, DL's own dl_snapshots line) ---

def _dl_snap(pg):
    return dl_snapshot.import_snapshot(pg, DL_CATALOG_CSV, OBJEDNAVKY_CATALOG_CSV, SUPPLIER_CSV)


def test_dl_catalog_override_wins_over_the_snapshot_row_with_the_same_gtin(pg):
    dl_snapshot.upsert_dl_catalog_card(pg, "8588001900001", "Múka hladká T512 25kg OPRAVENÉ",
                                       doplnok="", mass=25.0, sklad="100", cena=13.0)
    sid = _dl_snap(pg)
    catalog = {r["gtin"]: r for r in dl_snapshot.load_catalog(pg, sid)}
    assert catalog["8588001900001"]["name"] == "Múka hladká T512 25kg OPRAVENÉ"
    assert catalog["8588001900001"]["cena"] == 13.0


def test_dl_catalog_override_can_add_a_brand_new_card_not_in_the_sheet(pg):
    dl_snapshot.upsert_dl_catalog_card(pg, "NEW1", "Nová DL karta", doplnok="nový",
                                       mass=1.5, sklad="50", cena=2.5)
    sid = _dl_snap(pg)
    catalog = {r["gtin"]: r for r in dl_snapshot.load_catalog(pg, sid)}
    assert catalog["NEW1"]["name"] == "Nová DL karta"
    assert catalog["NEW1"]["mass"] == 1.5


def test_dl_catalog_override_defaults_are_blank_not_required(pg):
    """mass/cena/doplnok/sklad are all optional — an override can add a card with just
    gtin+name, same as the sheet parser itself allows."""
    dl_snapshot.upsert_dl_catalog_card(pg, "NEW2", "Karta bez detailov")
    sid = _dl_snap(pg)
    catalog = {r["gtin"]: r for r in dl_snapshot.load_catalog(pg, sid)}
    assert catalog["NEW2"]["mass"] is None
    assert catalog["NEW2"]["cena"] is None
    assert catalog["NEW2"]["doplnok"] == ""


def test_retiring_a_dl_catalog_card_excludes_it_even_though_the_snapshot_still_has_it(pg):
    _dl_snap(pg)   # the card must exist first
    ok = dl_snapshot.retire_dl_catalog_card(pg, "8588001900001")
    assert ok is True
    sid = _dl_snap(pg)
    catalog = {r["gtin"] for r in dl_snapshot.load_catalog(pg, sid)}
    assert "8588001900001" not in catalog
    assert "8588001900002" in catalog, "an unrelated card must survive untouched"


def test_retire_dl_catalog_card_refuses_a_gtin_that_never_existed(pg):
    _dl_snap(pg)
    assert dl_snapshot.retire_dl_catalog_card(pg, "NOPE-NEVER-EXISTED") is False


def test_dl_catalog_for_management_marks_overridden_cards(pg):
    _dl_snap(pg)
    rows = {r["gtin"]: r for r in dl_snapshot.dl_catalog_for_management(pg)}
    assert rows["8588001900001"]["overridden"] is False
    dl_snapshot.upsert_dl_catalog_card(pg, "8588001900001", "Múka - nový popis")
    rows2 = {r["gtin"]: r for r in dl_snapshot.dl_catalog_for_management(pg)}
    assert rows2["8588001900001"]["overridden"] is True
    assert rows2["8588001900001"]["name"] == "Múka - nový popis"


def test_dl_rebuild_from_overrides_reflects_a_new_override_without_reimporting(pg):
    sid = _dl_snap(pg)
    dl_snapshot.upsert_dl_catalog_card(pg, "8588001900002", "Olej - nový názov")
    new_sid = dl_snapshot.dl_rebuild_from_overrides(pg)
    assert new_sid != sid
    catalog = {r["gtin"]: r for r in dl_snapshot.load_catalog(pg, new_sid)}
    assert catalog["8588001900002"]["name"] == "Olej - nový názov"
    old_catalog = {r["gtin"]: r for r in dl_snapshot.load_catalog(pg, sid)}
    assert old_catalog["8588001900002"]["name"] == "Olej repkový 10l", \
        "the OLD snapshot must not change"


def test_dl_rebuild_from_overrides_is_a_noop_before_any_snapshot_exists(pg):
    assert dl_snapshot.dl_rebuild_from_overrides(pg) is None


def test_dl_supplier_override_replaces_the_snapshot_row_it_names(pg):
    _dl_snap(pg)
    rows = {r["name"]: r for r in dl_snapshot.dl_suppliers_for_management(pg)}
    row = rows["Signatus s.r.o."]
    dl_snapshot.upsert_dl_supplier(
        pg, override_id=None, orig_ean_edi=row["orig_ean_edi"], orig_city=row["orig_city"],
        ean_edi="8586010000001", name="Signatus s.r.o. OPRAVENÉ", emails=["nove@signatus.sk"],
        city="Košice")
    sid = dl_snapshot.dl_rebuild_from_overrides(pg)
    suppliers = {r["name"]: r for r in dl_snapshot.load_suppliers(pg, sid)}
    assert "Signatus s.r.o." not in suppliers
    assert suppliers["Signatus s.r.o. OPRAVENÉ"]["ean_edi"] == "8586010000001"


def test_dl_supplier_override_can_add_a_brand_new_supplier(pg):
    dl_snapshot.upsert_dl_supplier(
        pg, override_id=None, orig_ean_edi=None, orig_city=None,
        ean_edi="9999999999999", name="Nový dodávateľ", emails=["novy@x.sk"], city="Žilina")
    sid = _dl_snap(pg)
    suppliers = {r["name"]: r for r in dl_snapshot.load_suppliers(pg, sid)}
    assert suppliers["Nový dodávateľ"]["ean_edi"] == "9999999999999"


def test_two_concurrent_new_dl_supplier_adds_for_the_same_ean_with_different_city_produce_one_row(pg):
    """#248: the DL-supplier mirror of `test_two_concurrent_new_customer_adds_for_the_
    same_ean_with_different_street_produce_one_row` in tests/test_snapshot.py — see
    that test's docstring for the full race trace (`city` standing in for `street`
    here). Before the fix, `upsert_dl_supplier`'s reclaim SELECT was scoped
    `AND city = %s`, narrower than the advisory lock (keyed on `ean_edi` alone), so two
    genuinely simultaneous "new supplier" submissions for the same brand-new EAN under
    DIFFERENT cities both inserted — two rows sharing one EAN, `dl_match.py` then
    silently picking whichever row comes first, possibly a stale/wrong name. Exactly
    ONE row must survive, and the loser must be told about the conflict."""
    import threading

    import psycopg

    barrier = threading.Barrier(2)
    errors = []

    def add(name, city):
        conn = psycopg.connect(PG_DSN)
        try:
            barrier.wait(timeout=5)
            dl_snapshot.upsert_dl_supplier(
                conn, override_id=None, orig_ean_edi=None, orig_city=None,
                ean_edi="7000000000902", name=name, emails=["race@dl.sk"], city=city)
            conn.commit()
        except Exception as e:  # pragma: no cover - surfaced via `errors` below
            errors.append(e)
        finally:
            conn.close()

    t1 = threading.Thread(target=add, args=("Dodávateľ Košice", "Košice"))
    t2 = threading.Thread(target=add, args=("Dodávateľ Prešov", "Prešov"))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert pg.execute(
        "SELECT count(*) FROM dl_supplier_overrides WHERE ean_edi='7000000000902'"
    ).fetchone() == (1,), (
        "two concurrent new-dl-supplier adds with different cities must leave ONE row, "
        "not two duplicate suppliers sharing an EAN")
    assert len(errors) == 1, (
        f"exactly one of the two conflicting submissions must be refused, got: {errors}")
    assert isinstance(errors[0], snapshot.DuplicateEan), (
        f"the refused submission must raise DuplicateEan, not silently fail: {errors[0]!r}")


def test_retiring_a_dl_supplier_override_excludes_it(pg):
    _dl_snap(pg)
    rows = {r["name"]: r for r in dl_snapshot.dl_suppliers_for_management(pg)}
    row = rows["Jackulík"]
    ok = dl_snapshot.retire_dl_supplier(pg, override_id=None,
                                        orig_ean_edi=row["orig_ean_edi"], orig_city=row["orig_city"])
    assert ok is True
    sid = dl_snapshot.dl_rebuild_from_overrides(pg)
    names = {r["name"] for r in dl_snapshot.load_suppliers(pg, sid)}
    assert "Jackulík" not in names
    assert "Signatus s.r.o." in names, "an unrelated supplier must survive untouched"


def test_retire_dl_supplier_needs_some_identity(pg):
    assert dl_snapshot.retire_dl_supplier(pg, override_id=None,
                                          orig_ean_edi=None, orig_city=None) is False


def test_retiring_one_dl_supplier_does_not_drop_an_unrelated_blank_identity_supplier(pg):
    """Same #127/#128 review finding mirrored for DL: an override's own current identity
    must never default to a blank placeholder, or it would exclude EVERY blank-identity
    supplier from every future merge, not just the one being retired."""
    dl_catalog_csv = "GTIN,Názov,doplnok,hmotnost,Sklad,Cena\nG1,Múka,,,,\n"
    supplier_csv = (
        "Názov organizácie,EAN kód EDI,Obec,E-mail\n"
        "Dodávateľ na vyradenie,8586010000009,Košice,prvy@x.sk\n"
        "Nevinný okolostojaci bez EAN a obce,,,druhy@x.sk\n")
    dl_snapshot.import_snapshot(pg, dl_catalog_csv, "GTIN,Sklad,Názov,doplnok\n", supplier_csv)
    ok = dl_snapshot.retire_dl_supplier(pg, override_id=None,
                                        orig_ean_edi="8586010000009", orig_city="Košice")
    assert ok is True
    sid = dl_snapshot.dl_rebuild_from_overrides(pg)
    names = {r["name"] for r in dl_snapshot.load_suppliers(pg, sid)}
    assert "Dodávateľ na vyradenie" not in names
    assert "Nevinný okolostojaci bez EAN a obce" in names, \
        "an unrelated blank-identity supplier must survive"


def test_upsert_dl_supplier_by_orig_identity_is_idempotent_not_duplicated(pg):
    _dl_snap(pg)
    rows = {r["name"]: r for r in dl_snapshot.dl_suppliers_for_management(pg)}
    row = rows["Signatus s.r.o."]
    dl_snapshot.upsert_dl_supplier(
        pg, override_id=None, orig_ean_edi=row["orig_ean_edi"], orig_city=row["orig_city"],
        ean_edi="8586010000001", name="Prvá oprava", emails=[], city="Košice")
    dl_snapshot.upsert_dl_supplier(
        pg, override_id=None, orig_ean_edi=row["orig_ean_edi"], orig_city=row["orig_city"],
        ean_edi="8586010000001", name="Druhá oprava", emails=[], city="Košice")
    assert pg.execute("SELECT count(*) FROM dl_supplier_overrides").fetchone()[0] == 1
    rows2 = dl_snapshot.dl_suppliers_for_management(pg)
    names = [r["name"] for r in rows2 if r["orig_ean_edi"] == row["orig_ean_edi"]]
    assert names == ["Druhá oprava"]


def test_dl_suppliers_for_management_marks_override_identity_for_editing(pg):
    _dl_snap(pg)
    rows = {r["name"]: r for r in dl_snapshot.dl_suppliers_for_management(pg)}
    assert rows["Signatus s.r.o."]["override_id"] is None
    assert rows["Signatus s.r.o."]["orig_ean_edi"] == "8586010000001"

    rid = dl_snapshot.upsert_dl_supplier(
        pg, override_id=None, orig_ean_edi="8586010000001", orig_city="Košice",
        ean_edi="8586010000001", name="Signatus v2", emails=[], city="Košice")
    rows2 = {r["name"]: r for r in dl_snapshot.dl_suppliers_for_management(pg)}
    assert rows2["Signatus v2"]["override_id"] == rid


def test_editing_an_already_overridden_dl_supplier_by_its_override_id(pg):
    """The second edit through /znalosti sends the REAL override_id it got back from the
    first edit — a different code path than the still-snapshot-only orig-identity one."""
    _dl_snap(pg)
    rows = {r["name"]: r for r in dl_snapshot.dl_suppliers_for_management(pg)}
    row = rows["Signatus s.r.o."]
    first_rid = dl_snapshot.upsert_dl_supplier(
        pg, override_id=None, orig_ean_edi=row["orig_ean_edi"], orig_city=row["orig_city"],
        ean_edi="8586010000001", name="Prvá verzia", emails=[], city="Košice")
    second_rid = dl_snapshot.upsert_dl_supplier(
        pg, override_id=first_rid, orig_ean_edi=None, orig_city=None,
        ean_edi="8586010000001", name="Druhá verzia", emails=["nove@x.sk"], city="Košice")
    assert second_rid == first_rid
    rows2 = dl_snapshot.dl_suppliers_for_management(pg)
    names = [r["name"] for r in rows2 if r["orig_ean_edi"] == row["orig_ean_edi"]]
    assert names == ["Druhá verzia"]


def test_upsert_dl_supplier_by_override_id_refuses_a_nonexistent_id(pg):
    with pytest.raises(KeyError):
        dl_snapshot.upsert_dl_supplier(
            pg, override_id=999999, orig_ean_edi=None, orig_city=None,
            ean_edi="1", name="x", emails=[], city="")


def test_editing_an_already_overridden_dl_supplier_by_override_id_into_a_colliding_ean_raises(pg):
    """#248 review finding: mirrors the identical customer-side test in
    tests/test_snapshot.py — the `override_id is not None` branch had no uniqueness
    check of its own; now the partial unique index turns a collision into
    `DuplicateEan`, not a silent duplicate."""
    a_id = dl_snapshot.upsert_dl_supplier(
        pg, override_id=None, orig_ean_edi=None, orig_city=None,
        ean_edi="7100000000003", name="Dodávateľ A", emails=["a@dl.sk"], city="Košice")
    b_id = dl_snapshot.upsert_dl_supplier(
        pg, override_id=None, orig_ean_edi=None, orig_city=None,
        ean_edi="7100000000004", name="Dodávateľ B", emails=["b@dl.sk"], city="Prešov")

    with pytest.raises(snapshot.DuplicateEan) as exc_info:
        dl_snapshot.upsert_dl_supplier(
            pg, override_id=b_id, orig_ean_edi=None, orig_city=None,
            ean_edi="7100000000003",  # A's EAN, but B's own (different) city
            name="Dodávateľ B premenovaný", emails=["b2@dl.sk"], city="Prešov")
    assert exc_info.value.existing["override_id"] == a_id

    rows = {r["ean_edi"]: r["name"] for r in dl_snapshot.dl_suppliers_for_management(pg)}
    assert rows["7100000000003"] == "Dodávateľ A"
    assert rows["7100000000004"] == "Dodávateľ B"


# --- #275: sheet-bound EDIT branch vs an active hand-added supplier's EAN -------------

def test_editing_a_sheet_bound_dl_supplier_into_a_colliding_active_hand_added_ean_raises(pg):
    """#275: mirrors the identical customer-side test in tests/test_snapshot.py, `city`
    standing in for `street` — the fallthrough branch (editing a still-snapshot-only DL
    supplier row) had no uniqueness check of its own; the #248 partial index deliberately
    excludes it (`WHERE orig_ean_edi IS NULL`)."""
    _dl_snap(pg)
    hand_added_id = dl_snapshot.upsert_dl_supplier(
        pg, override_id=None, orig_ean_edi=None, orig_city=None,
        ean_edi="7200000000010", name="Ručne pridaný dodávateľ", emails=["rucny@dl.sk"],
        city="Trnava")

    with pytest.raises(snapshot.DuplicateEan) as exc_info:
        dl_snapshot.upsert_dl_supplier(
            pg, override_id=None, orig_ean_edi="8586010000001", orig_city="Košice",
            ean_edi="7200000000010",  # collides with the hand-added supplier's EAN
            name="Signatus prepísaný", emails=["nove@signatus.sk"], city="Košice")
    assert exc_info.value.existing["override_id"] == hand_added_id

    rows = {r["ean_edi"]: r["name"] for r in dl_snapshot.dl_suppliers_for_management(pg)}
    assert rows["7200000000010"] == "Ručne pridaný dodávateľ"
    assert "Signatus s.r.o." in {r["name"] for r in dl_snapshot.dl_suppliers_for_management(pg)}


def test_editing_a_sheet_bound_dl_supplier_into_a_retired_hand_added_eans_ean_is_allowed(pg):
    _dl_snap(pg)
    hand_added_id = dl_snapshot.upsert_dl_supplier(
        pg, override_id=None, orig_ean_edi=None, orig_city=None,
        ean_edi="7200000000011", name="Zrušený dodávateľ", emails=["x@dl.sk"], city="Trnava")
    assert dl_snapshot.retire_dl_supplier(
        pg, override_id=hand_added_id, orig_ean_edi=None, orig_city=None) is True

    rid = dl_snapshot.upsert_dl_supplier(
        pg, override_id=None, orig_ean_edi="8586010000001", orig_city="Košice",
        ean_edi="7200000000011",  # same EAN as the now-RETIRED hand-added supplier
        name="Signatus prepísaný", emails=["nove@signatus.sk"], city="Košice")
    assert rid is not None
    rows = {r["ean_edi"]: r["name"] for r in dl_snapshot.dl_suppliers_for_management(pg)}
    assert rows["7200000000011"] == "Signatus prepísaný"


def test_editing_a_sheet_bound_dl_supplier_whose_original_ean_was_blank_is_allowed(pg):
    """`orig_ean_edi` for a still-snapshot-only row whose ORIGINAL EAN was blank is `""`,
    not `None` — still enters this branch (`"" is not None`); the new guard must not
    treat that specially."""
    dl_catalog_csv = "GTIN,Názov,doplnok,hmotnost,Sklad,Cena\nG1,Múka,,,,\n"
    supplier_csv = (
        "Názov organizácie,EAN kód EDI,Obec,E-mail\n"
        "Dodávateľ bez EAN,,Žilina,ziadny@dl.sk\n")
    dl_snapshot.import_snapshot(pg, dl_catalog_csv, "GTIN,Sklad,Názov,doplnok\n", supplier_csv)
    rid = dl_snapshot.upsert_dl_supplier(
        pg, override_id=None, orig_ean_edi="", orig_city="Žilina",
        ean_edi="7200000000012", name="Dodávateľ bez EAN", emails=["ziadny@dl.sk"],
        city="Žilina")
    assert rid is not None
    rows = {r["name"]: r for r in dl_snapshot.dl_suppliers_for_management(pg)}
    assert rows["Dodávateľ bez EAN"]["ean_edi"] == "7200000000012"


def test_editing_two_different_sheet_bound_dl_suppliers_to_share_the_same_ean_is_allowed(pg):
    """The snapshot legitimately repeats an EAN across cities (same intent as
    snapshot.py's customer sheet) — the new guard only ever checks active hand-added
    rows, never other sheet-bound rows, so two sheet-bound edits sharing a target EAN
    must both succeed."""
    _dl_snap(pg)
    dl_snapshot.upsert_dl_supplier(
        pg, override_id=None, orig_ean_edi="8586010000001", orig_city="Košice",
        ean_edi="7200000000013", name="Pobočka Signatus A", emails=[], city="Košice")
    dl_snapshot.upsert_dl_supplier(
        pg, override_id=None, orig_ean_edi="8586010000002", orig_city="Prešov",
        ean_edi="7200000000013", name="Pobočka Jackulík B", emails=[], city="Prešov")
    names = {r["name"] for r in dl_snapshot.dl_suppliers_for_management(pg)
             if r["ean_edi"] == "7200000000013"}
    assert names == {"Pobočka Signatus A", "Pobočka Jackulík B"}
