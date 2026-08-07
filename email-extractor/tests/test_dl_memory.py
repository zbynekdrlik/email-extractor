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


def test_a_negative_cnt_is_clamped_to_one_not_stored_as_negative(pg):
    """A negative weight would poison R66's future weighted-majority math (review
    finding on #200's PR) — the original int(cnt or 1) coercion let -3 through as-is."""
    _ship(pg, "889", "maslo", "G8", "Maslo čerstvé", "2026-07-01", cnt=-3)
    row = pg.execute(
        "SELECT cnt FROM dl_item_memory WHERE supplier_ean = '889'").fetchone()
    assert row[0] == 1


def test_a_non_numeric_cnt_defaults_to_one_instead_of_raising(pg):
    """A garbled cnt value must never crash the caller (review finding on #200's PR) —
    the whole point is that a single bad row in a batch import never aborts the rest."""
    assert _ship(pg, "890", "maslo", "G8", "Maslo čerstvé", "2026-07-01", cnt="not-a-number") is True
    row = pg.execute(
        "SELECT cnt FROM dl_item_memory WHERE supplier_ean = '890'").fetchone()
    assert row[0] == 1


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


def test_import_n8n_rows_a_garbled_cnt_does_not_abort_the_rest_of_the_batch(pg):
    """Review finding on #200's PR: the original int(r.get('cnt') or 1) raised
    ValueError straight out of the loop for a non-numeric cnt, losing every row after
    the bad one silently (the caller sees a partial import with no indication why)."""
    rows = [
        {"cust": "556", "item": "syr eidam", "gtin": "G56", "card": "Syr eidam",
         "at": "2026-05-02T00:00:00Z", "src": "ship", "cnt": "corrupted"},
        {"cust": "556", "item": "syr eidam", "gtin": "G56", "card": "Syr eidam",
         "at": "2026-05-03T00:00:00Z", "src": "ship", "cnt": 4},
    ]
    stored = dl_memory.import_n8n_rows(pg, rows)
    assert stored == 2, "both rows must land — the bad cnt defaults to 1, never aborts the batch"
    rows_out = pg.execute(
        "SELECT delivered_on, cnt FROM dl_item_memory WHERE supplier_ean = '556' "
        "ORDER BY delivered_on").fetchall()
    assert [r[1] for r in rows_out] == [1, 4]


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


# --- resolve() (#202, R66's weighted-majority read) ---------------------------------------

def test_resolve_unanimous_single_gtin(pg):
    _ship(pg, "S1", "múka hladká 25kg", "G1", "Múka hladká 25kg", "2026-07-01")
    _ship(pg, "S1", "múka hladká 25kg", "G1", "Múka hladká 25kg", "2026-07-08")
    r = dl_memory.resolve(pg, "S1", "múka hladká 25kg")
    assert r is not None and r.gtin == "G1" and r.unanimous is True and r.strength == 2


def test_resolve_none_when_no_history_at_all(pg):
    assert dl_memory.resolve(pg, "S9", "nič") is None


def test_resolve_mixed_history_newest_card_wins_at_or_above_60_percent(pg):
    # G1: 2 deliveries (weight 2), G2: 3 deliveries (weight 3), newest is G2 -> 3/5 = 60%.
    _ship(pg, "S2", "cukor", "G1", "Cukor A", "2026-06-01")
    _ship(pg, "S2", "cukor", "G1", "Cukor A", "2026-06-02")
    _ship(pg, "S2", "cukor", "G2", "Cukor B", "2026-06-10")
    _ship(pg, "S2", "cukor", "G2", "Cukor B", "2026-06-11")
    _ship(pg, "S2", "cukor", "G2", "Cukor B", "2026-06-12")
    r = dl_memory.resolve(pg, "S2", "cukor")
    assert r is not None and r.gtin == "G2" and r.unanimous is False and r.strength == 3


def test_resolve_silent_when_newest_card_is_below_60_percent(pg):
    # G1: 2 deliveries, G2: 2 deliveries -> newest (G2) is only 2/4 = 50%, below MAJORITY.
    _ship(pg, "S3", "olej", "G1", "Olej A", "2026-06-01")
    _ship(pg, "S3", "olej", "G1", "Olej A", "2026-06-02")
    _ship(pg, "S3", "olej", "G2", "Olej B", "2026-06-10")
    _ship(pg, "S3", "olej", "G2", "Olej B", "2026-06-11")
    assert dl_memory.resolve(pg, "S3", "olej") is None


def test_resolve_duplicate_rows_for_the_same_day_take_max_cnt_not_sum(pg):
    """R66's own stated dedup rule + this module's own #200 review-finding guidance: two
    rows for the SAME (gtin, day) with different cnt are the same underlying record —
    max(cnt), never sum(cnt), or a re-import would double-count one delivery."""
    _ship(pg, "S4", "vajcia", "G1", "Vajcia M", "2026-06-01", cnt=3)
    _ship(pg, "S4", "vajcia", "G1", "Vajcia M", "2026-06-01", cnt=5)   # same day, re-seeded
    r = dl_memory.resolve(pg, "S4", "vajcia")
    assert r is not None and r.strength == 5, "must be max(3,5)=5, never 3+5=8"


def test_resolve_weight_override_needs_unanimous_and_at_least_3(pg):
    _ship(pg, "S5", "chlieb", "G1", "Chlieb 1kg", "2026-06-01", cnt=1)
    _ship(pg, "S5", "chlieb", "G1", "Chlieb 1kg", "2026-06-02", cnt=1)
    r = dl_memory.resolve(pg, "S5", "chlieb")
    assert r.unanimous and r.strength == 2 and r.weight_override is False
    _ship(pg, "S5", "chlieb", "G1", "Chlieb 1kg", "2026-06-03", cnt=1)
    r = dl_memory.resolve(pg, "S5", "chlieb")
    assert r.strength == 3 and r.weight_override is True


def test_resolve_weight_override_false_when_history_is_mixed_even_at_3_plus(pg):
    # G1: weight 1 (older), G2: weight 3 (newer) -> newest share 3/4 = 75%, clears MAJORITY,
    # and its own strength (3) alone would clear WEIGHT_OVERRIDE_MIN — but the history is
    # NOT unanimous (a genuinely different card was also shipped), so override must stay off.
    _ship(pg, "S6", "syr", "G1", "Syr A", "2026-06-01", cnt=1)
    _ship(pg, "S6", "syr", "G2", "Syr B", "2026-06-10", cnt=3)
    r = dl_memory.resolve(pg, "S6", "syr")
    assert r is not None and r.gtin == "G2" and r.unanimous is False
    assert r.weight_override is False, "R66: weightOverride only when unanimous"


def test_resolve_human_taught_row_wins_unconditionally(pg):
    _ship(pg, "S7", "roztok", "G1", "Roztok A", "2026-06-01", cnt=9)  # heavy ship history
    _ship(pg, "S7", "roztok", "G2", "Roztok B", "2026-07-01", src="human")
    r = dl_memory.resolve(pg, "S7", "roztok")
    assert r.gtin == "G2" and r.human is True and r.weight_override is True


def test_resolve_catalog_invalidation_drops_a_retired_gtin(pg):
    """R66: 'catalog-card disappearance invalidates the memory.'"""
    _ship(pg, "S8", "maslo", "G1", "Maslo A", "2026-06-01")
    _ship(pg, "S8", "maslo", "G1", "Maslo A", "2026-06-02")
    assert dl_memory.resolve(pg, "S8", "maslo", catalog_gtins={"G1"}) is not None
    assert dl_memory.resolve(pg, "S8", "maslo", catalog_gtins={"G9"}) is None


def test_resolve_catalog_invalidation_also_applies_to_a_human_taught_row(pg):
    _ship(pg, "S8b", "maslo extra", "G1", "Maslo A", "2026-06-01", src="human")
    assert dl_memory.resolve(pg, "S8b", "maslo extra", catalog_gtins={"G9"}) is None


def test_resolve_as_of_excludes_deliveries_on_or_after_the_given_day(pg):
    _ship(pg, "S9x", "kvasnice", "G1", "Kvasnice", "2026-06-01")
    _ship(pg, "S9x", "kvasnice", "G1", "Kvasnice", "2026-06-15")
    r = dl_memory.resolve(pg, "S9x", "kvasnice", as_of="2026-06-10")
    assert r is not None and r.strength == 1, "only the 06-01 delivery is before as_of"


def test_resolve_missing_supplier_or_item_returns_none(pg):
    assert dl_memory.resolve(pg, "", "co") is None
    assert dl_memory.resolve(pg, "S1", "") is None
