"""DL item-match history storage (#200 F1) — dl_item_memory, item_memory's sibling
for delivery notes. See tests/test_orders_memory.py for the AI-orders counterpart
these tests mirror; the one deliberate difference under test here is `cnt`.
"""
from app.orders import dl_memory


def _ship(pg, supplier, item, gtin, card, day, cnt=1, src="ship"):
    return dl_memory.remember(pg, supplier, item, gtin, card, delivered_on=day,
                              cnt=cnt, source=src)


def test_the_same_delivery_written_twice_is_stored_once(pg):
    assert _ship(pg, "111", "múka hladká", "G1", "Múka hladká 25kg", "2026-07-15") is True
    assert _ship(pg, "111", "múka hladká", "G1", "Múka hladká 25kg", "2026-07-15") is False
    assert pg.execute("SELECT count(*) FROM dl_item_memory").fetchone()[0] == 1


def test_key_is_diacritics_and_case_insensitive_but_keeps_the_weight(pg):
    """Same normalization as memory.item_key (R66: key = supplier EAN + EXACT
    normalized wording INCLUDING gramáž)."""
    _ship(pg, "111", "Múka hladká 25kg", "G25", "Múka hladká T512 25kg", "2026-07-15")
    assert dl_memory.remember(pg, "111", "muka hladka 25 kg", "G25",
                              "Múka hladká T512 25kg", delivered_on="2026-07-16") is True
    # a different weight is a different item_key, so a fresh row is genuinely new,
    # not a collision with the 25kg wording above:
    row = pg.execute(
        "SELECT item_key FROM dl_item_memory WHERE delivered_on = '2026-07-15'").fetchone()
    assert "25kg" in row[0]


# --- cnt: the one structural difference from item_memory (R66's weighted majority) --

def test_cnt_is_stored_verbatim_not_derived_from_distinct_days(pg):
    """Unlike item_memory.resolve() (which counts DISTINCT delivery days), R66's
    weighted-majority rule needs the raw n8n cnt preserved as-is."""
    _ship(pg, "222", "chlieb", "G2", "Chlieb kváskový", "2026-07-10", cnt=5)
    row = pg.execute(
        "SELECT cnt FROM dl_item_memory WHERE supplier_ean = '222'").fetchone()
    assert row[0] == 5


def test_a_zero_or_none_cnt_is_coerced_to_one(pg):
    """A falsy cnt (0, None) must never silently store a zero-weight row, which
    would make R66's later majority math divide by nothing for that record."""
    _ship(pg, "888", "maslo", "G8", "Maslo čerstvé", "2026-07-01", cnt=0)
    _ship(pg, "888", "maslo", "G8", "Maslo čerstvé", "2026-07-02", cnt=None)
    rows = pg.execute(
        "SELECT cnt FROM dl_item_memory WHERE supplier_ean = '888' ORDER BY delivered_on"
    ).fetchall()
    assert [r[0] for r in rows] == [1, 1]


def test_duplicate_rows_dedupe_by_gtin_day_cnt_identity(pg):
    """R66's own stated n8n dedup rule: two rows are the SAME underlying record when
    (gtin, day, cnt) match — this is what the UNIQUE constraint enforces directly."""
    assert _ship(pg, "333", "olej", "G3", "Olej repkový 10l", "2026-07-01", cnt=3) is True
    assert _ship(pg, "333", "olej", "G3", "Olej repkový 10l", "2026-07-01", cnt=3) is False
    # a DIFFERENT cnt for the same day+gtin is a genuinely different history record
    # (e.g. a corrected re-seed), not a duplicate:
    assert _ship(pg, "333", "olej", "G3", "Olej repkový 10l", "2026-07-01", cnt=4) is True
    assert pg.execute(
        "SELECT count(*) FROM dl_item_memory WHERE supplier_ean = '333'").fetchone()[0] == 2


def test_missing_required_fields_are_refused(pg):
    assert _ship(pg, "", "olej", "G3", "Olej", "2026-07-01") is False
    assert _ship(pg, "333", "", "G3", "Olej", "2026-07-01") is False
    assert _ship(pg, "333", "olej", "", "Olej", "2026-07-01") is False
    assert pg.execute("SELECT count(*) FROM dl_item_memory").fetchone()[0] == 0


# --- one-off n8n import ----------------------------------------------------------

def test_import_n8n_rows_carries_cnt_through(pg):
    rows = [
        {"cust": "444", "item": "vajcia M", "gtin": "G4", "card": "Vajcia M 30ks",
         "at": "2026-06-01T08:00:00Z", "src": "ship", "cnt": 7},
        {"cust": "444", "item": "vajcia M", "gtin": "G4", "card": "Vajcia M 30ks",
         "at": "2026-06-01T14:30:00Z", "src": "ship", "cnt": 7},   # same day, dupe
        {"cust": "444", "item": "vajcia M", "gtin": "G4", "card": "Vajcia M 30ks",
         "at": "2026-06-05T08:00:00Z", "src": "ship", "cnt": 2},
    ]
    stored = dl_memory.import_n8n_rows(pg, rows)
    assert stored == 2, "same day+gtin+cnt collapses; a different day is a new row"
    total = pg.execute(
        "SELECT count(*) FROM dl_item_memory WHERE supplier_ean = '444'").fetchone()[0]
    assert total == 2


def test_import_n8n_rows_skips_a_row_with_no_timestamp(pg):
    rows = [{"cust": "555", "item": "syr", "gtin": "G5", "card": "Syr", "src": "ship"}]
    assert dl_memory.import_n8n_rows(pg, rows) == 0
    assert pg.execute("SELECT count(*) FROM dl_item_memory").fetchone()[0] == 0


def test_import_n8n_rows_defaults_cnt_to_one_when_absent(pg):
    rows = [{"cust": "666", "item": "kvasnice", "gtin": "G6", "card": "Kvasnice čerstvé",
             "at": "2026-05-01T00:00:00Z", "src": "ship"}]
    assert dl_memory.import_n8n_rows(pg, rows) == 1
    row = pg.execute(
        "SELECT cnt FROM dl_item_memory WHERE supplier_ean = '666'").fetchone()
    assert row[0] == 1


def test_import_n8n_rows_is_idempotent_on_a_second_run(pg):
    rows = [{"cust": "777", "item": "cukor", "gtin": "G7", "card": "Cukor kryštálový",
             "at": "2026-05-10T00:00:00Z", "src": "ship", "cnt": 4}]
    assert dl_memory.import_n8n_rows(pg, rows) == 1
    assert dl_memory.import_n8n_rows(pg, rows) == 0, "re-running the import must store nothing new"
    assert pg.execute(
        "SELECT count(*) FROM dl_item_memory WHERE supplier_ean = '777'").fetchone()[0] == 1
