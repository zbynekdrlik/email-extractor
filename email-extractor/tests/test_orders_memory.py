"""Item memory: what we really shipped this customer for this wording (#63).

Replaces the n8n Data Table `mX1EIECccDfTa9ab`, which has no unique key — one delivery
writes duplicate rows there and the n8n code has to dedup them in JavaScript. That
workaround already misfired once (a seed row was read as "18 deliveries" and released
the weight override too early), so the key is enforced in the schema here.
"""
from app.orders import memory


def _ship(pg, cust, item, gtin, card, day, src="ship"):
    return memory.remember(pg, cust, item, gtin, card, delivered_on=day, source=src)


# --- storage -------------------------------------------------------------

def test_the_same_delivery_written_twice_is_stored_once(pg):
    assert _ship(pg, "111", "rožok", "G1", "Rožok štandart 50g", "2026-07-15") is True
    assert _ship(pg, "111", "rožok", "G1", "Rožok štandart 50g", "2026-07-15") is False
    assert pg.execute("SELECT count(*) FROM item_memory").fetchone()[0] == 1


def test_key_is_diacritics_and_case_insensitive_but_keeps_the_weight(pg):
    _ship(pg, "111", "Rožok 70g", "G70", "Rožok kváskový 70g", "2026-07-15")
    assert memory.resolve(pg, "111", "rozok 70 g").gtin == "G70"
    assert memory.resolve(pg, "111", "rožok") is None, \
        "a weight in the wording is part of the identity — 'rožok' is a different item"


# --- resolution ----------------------------------------------------------

def test_unanimous_history_resolves_and_counts_distinct_days(pg):
    for day in ("2026-07-15", "2026-07-17", "2026-07-21"):
        _ship(pg, "111", "rožok", "G1", "Rožok štandart 50g", day)
    hit = memory.resolve(pg, "111", "rožok")
    assert (hit.gtin, hit.unanimous, hit.strength) == ("G1", True, 3)
    assert hit.card == "Rožok štandart 50g"
    assert "3x" in hit.note and "2026-07-21" in hit.note


def test_duplicate_rows_on_one_day_do_not_inflate_the_strength(pg):
    """The n8n table writes the batch once per item, so one shipment can appear several
    times. Strength must count DAYS, or the weight override fires far too early."""
    _ship(pg, "111", "rožok", "G1", "Rožok štandart 50g", "2026-07-15")
    _ship(pg, "111", "rožok", "G1", "Rožok štandart 50g", "2026-07-15", src="ship-again")
    hit = memory.resolve(pg, "111", "rožok")
    assert hit.strength == 1
    assert hit.weight_override is False


def test_a_wobbly_history_resolves_to_the_newest_card_only_with_a_clear_majority(pg):
    _ship(pg, "111", "šiška", "OLD", "Šiška džemová 50g", "2026-07-01")
    _ship(pg, "111", "šiška", "OLD", "Šiška džemová 50g", "2026-07-02")
    _ship(pg, "111", "šiška", "NEW", "Šiška lesné ovocie 100g", "2026-07-20")
    assert memory.resolve(pg, "111", "šiška") is None, "1 of 3 is not a majority"

    for day in ("2026-07-21", "2026-07-22", "2026-07-23"):
        _ship(pg, "111", "šiška", "NEW", "Šiška lesné ovocie 100g", day)
    hit = memory.resolve(pg, "111", "šiška")
    assert hit.gtin == "NEW" and hit.unanimous is False


def test_the_weight_override_needs_a_unanimous_history_of_three_days(pg):
    """Overriding the weight guard is the most dangerous thing memory may do (it ships a
    different gramáž than the customer wrote), so it needs an unambiguous history."""
    _ship(pg, "111", "dánske pečivo", "G1", "Dánske pečivo tvarohové 90g", "2026-07-15")
    _ship(pg, "111", "dánske pečivo", "G1", "Dánske pečivo tvarohové 90g", "2026-07-16")
    assert memory.resolve(pg, "111", "dánske pečivo").weight_override is False
    _ship(pg, "111", "dánske pečivo", "G1", "Dánske pečivo tvarohové 90g", "2026-07-17")
    assert memory.resolve(pg, "111", "dánske pečivo").weight_override is True


def test_memory_is_per_customer(pg):
    _ship(pg, "111", "rožok", "G1", "Rožok štandart 50g", "2026-07-15")
    assert memory.resolve(pg, "222", "rožok") is None


def test_unknown_wording_resolves_to_nothing(pg):
    _ship(pg, "111", "rožok", "G1", "Rožok štandart 50g", "2026-07-15")
    assert memory.resolve(pg, "111", "vianočka") is None


# --- import of the existing n8n rows -------------------------------------

def test_importing_the_n8n_rows_is_idempotent(pg):
    rows = [
        {"cust": "2000000000865", "item": "rožok", "gtin": "8588001805647",
         "card": "Rožok štandart 50g", "at": "2026-07-15T06:51:00.000Z", "src": "seed"},
        {"cust": "2000000000865", "item": "rožok", "gtin": "8588001805647",
         "card": "Rožok štandart 50g", "at": "2026-07-15T09:10:00.000Z", "src": "ship"},
        {"cust": "", "item": "bez zákazníka", "gtin": "X", "card": "X",
         "at": "2026-07-15T06:51:00.000Z", "src": "ship"},
    ]
    first = memory.import_n8n_rows(pg, rows)
    assert first == 1, \
        "same customer+item+card+DAY is one delivery; a row without a customer is unusable"
    assert memory.import_n8n_rows(pg, rows) == 0
    assert memory.resolve(pg, "2000000000865", "rožok").gtin == "8588001805647"


# --- history is AS OF the email's date (#81.2) -----------------------------

def test_history_only_counts_deliveries_before_the_email(pg):
    """Two reasons. In production, a delivery recorded for a LATER date must not decide an
    order written today. And in the golden corpus, history seeded from the archive would
    otherwise contain the very order being scored — the test would know its own answer."""
    for day in ("2026-07-01", "2026-07-08", "2026-07-22"):
        memory.remember(pg, "200", "chlieb psenicno razny", "OLD", "Chlieb 1000 gr",
                        delivered_on=day, source="ship")
    memory.remember(pg, "200", "chlieb psenicno razny", "NEW", "Chlieb 700 gr",
                    delivered_on="2026-07-30", source="ship")

    as_of = memory.resolve(pg, "200", "chlieb pšenično ražný", as_of="2026-07-24")
    assert as_of and as_of.gtin == "OLD", "only what was delivered BEFORE 24.07. may decide"
    assert as_of.strength == 3

    # Unfiltered, the later contradicting delivery makes the history ambiguous — which is
    # exactly why an order must be judged against the history as it stood at the time.
    assert memory.resolve(pg, "200", "chlieb pšenično ražný") is None


def test_a_history_with_nothing_before_the_email_decides_nothing(pg):
    memory.remember(pg, "200", "siska", "G", "Šiška čokoláda 100g",
                    delivered_on="2026-07-30", source="ship")
    assert memory.resolve(pg, "200", "šiška", as_of="2026-07-01") is None
