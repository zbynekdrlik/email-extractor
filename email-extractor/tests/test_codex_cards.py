"""#467 — the CODEX stock-card list (`app/orders/codex_cards.py`).

CODEX rejects a WHOLE delivery-note import when one DESADV line carries an EAN kód no stock
card has. The add-on keeps a pushed copy of the CODEX list (full replace, never a merge — a code
that left CODEX must leave here too) and uses it to (a) refuse a new/edited card with a code
CODEX does not know, (b) hold a DL line matched to such a card, (c) show name drift. A missing or
stale list FAILS OPEN (never blocks every DL) with a warning + one ops alert.

All fixtures are SYNTHETIC (made-up codes/names) — this repo is public.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from app.config import Config
from app.orders import codex_cards

NOW = datetime.now(UTC)

# CODEX side: code -> stock-card name. 9990000000017 is the "renamed" card (our catalog still
# calls it by an old name), 9990000000024 exists on two sklad rows with the same name.
CARDS = [
    {"code": "9990000000017", "card_code": "27", "stredisko": 1, "sklad": 1,
     "name": "Rožok so slaninou a syrom 70g", "inactive": False,
     "changed_at": "2026-09-28T09:00:48+02:00"},
    {"code": "9990000000024", "card_code": "31", "stredisko": 1, "sklad": 1,
     "name": "Chlieb pšeničný 1000g REZANÝ", "inactive": False, "changed_at": None},
    {"code": "9990000000024", "card_code": "31", "stredisko": 1, "sklad": 100,
     "name": "Chlieb pšeničný 1000g REZANÝ", "inactive": False, "changed_at": None},
    {"code": "9990000000031", "card_code": "40", "stredisko": 1, "sklad": 100,
     "name": "Múka pšeničná T650", "inactive": False, "changed_at": None},
    {"code": "9990000000048", "card_code": "41", "stredisko": 1, "sklad": 100,
     "name": "Múka ražná T930", "inactive": True, "changed_at": None},
]


def _push(pg, cards=None, as_of=None, force=False):
    # replace_cards owns its own transaction (atomic replace) — called on the plain
    # autocommit test connection, exactly like the endpoint does.
    return codex_cards.replace_cards(pg, CARDS if cards is None else cards,
                                     source_as_of=as_of or NOW, force=force)


def _cfg(**kw):
    base = dict(pg_dsn="", data_dir="/tmp", ops_channel_id=77)
    base.update(kw)
    return Config(**base)


# --- replace_cards: the full-snapshot contract ------------------------------------------

def test_replace_stores_every_row_and_records_the_sync(pg):
    res = _push(pg)
    assert res == {"rows": 5, "codes": 4}
    assert pg.execute("SELECT count(*) FROM codex_stock_cards").fetchone()[0] == 5
    sync = codex_cards.latest_sync(pg)
    assert sync["row_count"] == 5 and sync["code_count"] == 4
    assert abs((sync["source_as_of"] - NOW).total_seconds()) < 1


def test_a_code_that_left_codex_leaves_the_list_too(pg):
    """The incident class: a code CODEX carried for a few days and then dropped (3698 on card
    27, 24.-28.9.) must NOT stay 'valid' here — the push is a replace, never a merge."""
    _push(pg)
    _push(pg, cards=CARDS[1:])
    codes = {r[0] for r in pg.execute("SELECT code FROM codex_stock_cards").fetchall()}
    assert "9990000000017" not in codes
    assert codex_cards.load(pg).has("9990000000017") is False


def test_an_empty_push_is_refused_and_keeps_the_current_list(pg):
    _push(pg)
    with pytest.raises(codex_cards.ReplaceRefused) as ei:
        _push(pg, cards=[])
    assert ei.value.status == 400
    assert pg.execute("SELECT count(*) FROM codex_stock_cards").fetchone()[0] == 5


def test_a_drastically_smaller_push_is_refused_unless_forced(pg):
    """A half-loaded ETL snapshot must not turn most real codes 'missing' and hold every DL."""
    _push(pg)
    with pytest.raises(codex_cards.ReplaceRefused) as ei:
        _push(pg, cards=CARDS[:1])
    assert ei.value.status == 409
    assert pg.execute("SELECT count(*) FROM codex_stock_cards").fetchone()[0] == 5
    assert _push(pg, cards=CARDS[:1], force=True) == {"rows": 1, "codes": 1}


def test_malformed_rows_are_skipped_never_stored(pg):
    res = _push(pg, cards=CARDS + [{"code": "ABC", "name": "x"}, {"code": "", "name": "y"},
                                   {"name": "no code"}, "not a dict"])
    assert res == {"rows": 5, "codes": 4}


def test_normalize_code_turns_a_neankod_number_into_our_gtin_text():
    assert codex_cards.normalize_code(9990000000017.0) == "9990000000017"
    assert codex_cards.normalize_code(" 3698 ") == "3698"
    assert codex_cards.normalize_code("3698.0") == "3698"
    assert codex_cards.normalize_code(0) is None
    assert codex_cards.normalize_code("12a") is None
    assert codex_cards.normalize_code(None) is None


# --- freshness: fail OPEN -----------------------------------------------------------------

def test_a_fresh_list_is_the_live_guard(pg):
    _push(pg)
    guard = codex_cards.live_guard(pg)
    assert guard is not None
    assert guard.has("9990000000031") and not guard.has("3698")
    assert guard.has("9990000000048"), "an inactive card still exists in CODEX"


def test_never_pushed_fails_open_with_a_warning(pg, caplog):
    with caplog.at_level(logging.WARNING, logger="orders.codex_cards"):
        assert codex_cards.live_guard(pg) is None
    assert any("CODEX" in r.getMessage() for r in caplog.records)


def test_a_stale_list_fails_open_with_a_warning(pg, caplog):
    _push(pg, as_of=NOW - timedelta(hours=codex_cards.STALE_HOURS + 1))
    with caplog.at_level(logging.WARNING, logger="orders.codex_cards"):
        assert codex_cards.live_guard(pg) is None
    assert any("stale" in r.getMessage().lower() for r in caplog.records)
    assert codex_cards.load(pg).stale is True


def test_a_list_just_inside_the_threshold_is_still_live(pg):
    _push(pg, as_of=NOW - timedelta(hours=codex_cards.STALE_HOURS - 1))
    assert codex_cards.live_guard(pg) is not None


# --- name drift ---------------------------------------------------------------------------

def test_name_status_ignores_cosmetics_but_flags_a_real_rename(pg):
    _push(pg)
    guard = codex_cards.load(pg)
    # cosmetic: gr vs g, word order, diacritics, case
    assert guard.name_status("9990000000024", "Chlieb pšeničný rezaný 1000 gr") == "ok"
    # the incident: our card still carries an old, unrelated name
    assert guard.name_status("9990000000017", "Bagetka s kečupom a syrom 80 gr") == "drift"
    assert guard.name_status("9990000000017", "Rožok so slaninou a syrom 70g") == "ok"
    assert guard.name_status("3698", "Rožok so slaninou a syrom 70g") == "missing"
    assert guard.name_for("9990000000017") == "Rožok so slaninou a syrom 70g"


def test_similar_lists_codex_cards_by_name_with_their_codes(pg):
    _push(pg)
    sim = codex_cards.load(pg).similar("Rožok so slaninou a syrom 70g")
    assert sim[0] == {"code": "9990000000017", "name": "Rožok so slaninou a syrom 70g"}
    assert all(s["code"] != "9990000000031" for s in sim), "an unrelated card is not 'similar'"


# --- the board/new-card refusal -----------------------------------------------------------

def test_check_card_code_passes_a_code_codex_has(pg):
    _push(pg)
    codex_cards.check_card_code(pg, "9990000000031", "Múka pšeničná T650")


def test_check_card_code_refuses_a_missing_code_with_similar_codex_cards(pg):
    _push(pg)
    catalog = [{"gtin": "9990000000017", "name": "Bagetka s kečupom a syrom 80 gr"}]
    with pytest.raises(codex_cards.CodexRefusal) as ei:
        codex_cards.check_card_code(pg, "3698", "Rožok so slaninou a syrom 70g",
                                    catalog=catalog)
    payload = ei.value.payload
    assert "3698" in payload["error"] and "CODEX" in payload["error"]
    assert payload["codex"]["code"] == "3698" and payload["codex"]["missing"] is True
    top = payload["codex"]["similar"][0]
    assert top == {"code": "9990000000017", "name": "Rožok so slaninou a syrom 70g",
                   "in_catalog": True, "catalog_name": "Bagetka s kečupom a syrom 80 gr"}


def test_check_card_code_fails_open_on_a_stale_list(pg):
    _push(pg, as_of=NOW - timedelta(hours=codex_cards.STALE_HOURS + 5))
    codex_cards.check_card_code(pg, "3698", "Čokoľvek")   # no raise


def test_check_card_code_fails_open_when_never_pushed(pg):
    codex_cards.check_card_code(pg, "3698", "Čokoľvek")   # no raise


# --- the DL board question's candidates -------------------------------------------------

def test_question_candidates_drop_cards_codex_lacks_and_rank_by_the_codex_name(pg):
    """The held line's question must never offer a card CODEX would reject, and the right card
    must surface even when OUR name for it is stale (the incident: CODEX „Rožok so slaninou…"
    vs our „Bagetka s kečupom…")."""
    _push(pg)
    guard = codex_cards.load(pg)
    catalog = [
        {"gtin": "3698", "name": "Rožok so slaninou a syrom 70g", "doplnok": ""},
        {"gtin": "9990000000017", "name": "Bagetka s kečupom a syrom 80 gr", "doplnok": ""},
        {"gtin": "9990000000031", "name": "Múka pšeničná T650", "doplnok": "hladka"},
    ]
    cands = codex_cards.question_candidates("Rožok so slaninou a syrom 70g", catalog, guard)
    assert [c["gtin"] for c in cands][:1] == ["9990000000017"]
    assert all(c["gtin"] != "3698" for c in cands)
    assert cands[0]["codex_name"] == "Rožok so slaninou a syrom 70g"
    assert cands[0]["name"] == "Bagetka s kečupom a syrom 80 gr", "our own card, not a copy"
    muka = next(c for c in cands if c["gtin"] == "9990000000031")
    assert "codex_name" not in muka and muka["doplnok"] == "hladka"


# --- Produkty sklad annotation ------------------------------------------------------------

def test_annotate_marks_ok_drift_and_missing(pg):
    _push(pg)
    rows = codex_cards.annotate([
        {"gtin": "9990000000031", "name": "Múka pšeničná T650"},
        {"gtin": "9990000000017", "name": "Bagetka s kečupom a syrom 80 gr"},
        {"gtin": "3698", "name": "Rožok so slaninou a syrom 70g"},
    ], codex_cards.load(pg))
    assert [r["codex"]["status"] for r in rows] == ["ok", "drift", "missing"]
    assert rows[1]["codex"]["name"] == "Rožok so slaninou a syrom 70g"


def test_annotate_without_a_list_marks_unknown(pg):
    rows = codex_cards.annotate([{"gtin": "1", "name": "x"}], None)
    assert rows[0]["codex"] == {"status": "unknown"}


# --- the ops alert for a missing / stale list --------------------------------------------

def _alerts(pg):
    return pg.execute("SELECT channel_id, kind, body_html FROM pending_alerts "
                      "WHERE kind = %s", (codex_cards.ALERT_KIND,)).fetchall()


def test_stale_sweep_raises_one_ops_alert_for_a_stale_list(pg):
    _push(pg, as_of=NOW - timedelta(hours=codex_cards.STALE_HOURS + 2))
    assert codex_cards.stale_sweep(pg, _cfg()) is True
    assert codex_cards.stale_sweep(pg, _cfg()) is False, "deduped while undelivered"
    rows = _alerts(pg)
    assert len(rows) == 1 and rows[0][0] == 77
    assert "CODEX" in rows[0][2]


def test_stale_sweep_is_quiet_for_a_fresh_list(pg):
    _push(pg)
    assert codex_cards.stale_sweep(pg, _cfg()) is False
    assert _alerts(pg) == []


def test_stale_sweep_gives_a_never_pushed_list_the_same_grace_from_install(pg):
    """Right after the deploy the first push has not run yet — no alert. A list that STILL has
    never arrived a threshold after the feature went live is alerted."""
    assert codex_cards.stale_sweep(pg, _cfg()) is False
    late = NOW + timedelta(hours=codex_cards.STALE_HOURS + 1)
    assert codex_cards.stale_sweep(pg, _cfg(), now=late) is True
    assert "nikdy" in _alerts(pg)[0][2]
