"""#478 — CODEX is the ONE source of truth for a card's code and name (`app/orders/codex_sync.py`).

After every accepted CODEX stock-card push the sync mirrors CODEX onto the cards WE already
have, in both catalogs (objednávky + sklad/DL) and the learned memories:

- a drifted NAME (the #467 `name_key` drift) → our card takes the CODEX name (aliases / doplnok
  untouched), audited, Kôš-restorable;
- a RENUMBER (the same CODEX card ACSKLP now carries code Y instead of X — card 27's incident)
  → X → Y in both catalogs AND in item_memory / global_item_memory / dl_item_memory, one audit
  row per changed row, ONE ops message;
- a code that left CODEX with no successor → our card goes to the Kôš + ops message;
- a stale list → nothing is touched (log.warning);
- `codex_sync_apply` false (the default) = DRY-RUN: the plan is computed, logged and stored in
  `codex_sync_runs`, and no catalog / memory / audit / alert row is written;
- the sync never ADDS a CODEX card we do not already have (the #337 bulk-import ban).

All codes/names are SYNTHETIC — this repo is public.
"""
from __future__ import annotations

import logging
import os
from datetime import UTC, date, datetime, timedelta

import psycopg

from app import db
from app.board.services import audit
from app.config import Config
from app.httpapi import create_app
from app.orders import (
    card_guard,
    codex_cards,
    codex_sync,
    codex_sync_list,
    dl_memory,
    dl_snapshot,
    memory,
    snapshot,
)

PG_DSN = os.environ.get("PG_TEST_DSN")
NOW = datetime.now(UTC)
# a mapping taught long before any synthetic CODEX push of a test — the pushes are hours old,
# a row inserted „now" is AFTER them (review 10: rows decided during a reuse are held)
_BEFORE = NOW - timedelta(hours=12)

ROZOK = "9990000000017"        # CODEX card 27 — the incident card, renumbered below
ROZOK_NEW = "9990000000093"    # card 27's new code
CHLIEB = "9990000000024"       # CODEX card 31 — renamed below
MUKA = "9990000000031"         # CODEX card 40 — a kg-tracked DL-only card
KOLAC = "9990000000055"        # CODEX card 55 — leaves CODEX entirely
CUDZIA = "9990000000079"       # CODEX card 79 — a CODEX card we do NOT have


def _row(code, card, name, sklad=1, stredisko=1, inactive=False):
    return {"code": code, "card_code": card, "stredisko": stredisko, "sklad": sklad,
            "name": name, "inactive": inactive, "changed_at": None}


V1 = [
    _row(ROZOK, "27", "Rožok so slaninou 70g"),
    _row(CHLIEB, "31", "Chlieb pšeničný 1000g"),
    _row(CHLIEB, "31", "Chlieb pšeničný 1000g", sklad=100),
    _row(MUKA, "40", "Múka pšeničná T650", sklad=100),
    _row(KOLAC, "55", "Koláč makový 80g"),
    _row(CUDZIA, "79", "Pagáč syrový 60g"),
]


def _cfg(apply=True, **kw):
    return Config(pg_dsn="", data_dir="/tmp", ops_channel_id=77, codex_sync_apply=apply, **kw)


def _push(pg, cards, hours_old):
    codex_cards.replace_cards(pg, cards, source_as_of=NOW - timedelta(hours=hours_old))


def _seed_catalogs(pg):
    """Orders: ROZOK (with an alias), CHLIEB, KOLAC. DL: ROZOK, MUKA (kg card), KOLAC."""
    snapshot._freeze(pg, [
        {"gtin": ROZOK, "name": "Rožok so slaninou 70g", "alias": "rozok slanina"},
        {"gtin": CHLIEB, "name": "Chlieb pšeničný 1000g", "alias": ""},
        {"gtin": KOLAC, "name": "Koláč makový 80g", "alias": ""},
    ], [])
    dl_snapshot._freeze(pg, [
        {"gtin": ROZOK, "name": "Rožok so slaninou 70g", "doplnok": "rožok slanina",
         "mass": 0.07, "sklad": "1", "cena": 0.35},
        {"gtin": MUKA, "name": "Múka pšeničná T650", "doplnok": "hladká", "mass": None,
         "sklad": "100", "cena": 0.37},
        {"gtin": KOLAC, "name": "Koláč makový 80g", "doplnok": "", "mass": None,
         "sklad": "1", "cena": None},
    ], [])


def _seed_memory(pg, gtin=ROZOK):
    """The card's learned history — taught / shipped long BEFORE the test's CODEX pushes."""
    pg.execute(
        "INSERT INTO item_memory (customer_ean, item_key, item_raw, gtin, card, delivered_on, "
        "source, created_at) VALUES ('C1', 'rozok slanina', 'rožok slanina', %s, 'Rožok', %s, "
        "'ship', %s), ('C2', 'rozky so slaninou', 'rožky so slaninou', %s, 'Rožok', %s, "
        "'human', %s)", (gtin, date(2026, 9, 1), _BEFORE, gtin, date(2026, 9, 2), _BEFORE))
    pg.execute(
        "INSERT INTO global_item_memory (item_key, item_raw, gtin, card, taught_by, created_at) "
        "VALUES ('slaninovy rozok', 'slaninový rožok', %s, 'Rožok', 'sklad', %s)",
        (gtin, _BEFORE))
    pg.execute(
        "INSERT INTO dl_item_memory (supplier_ean, item_key, item_raw, gtin, card, "
        "delivered_on, cnt, source, created_at) VALUES ('S1', 'rozok slanina 70', "
        "'Rožok slanina 70g', %s, 'Rožok', %s, 1, 'ship', %s)", (gtin, date(2026, 9, 3), _BEFORE))


def _gtins(pg, table):
    return sorted(r[0] for r in pg.execute(
        f"SELECT gtin FROM {table} WHERE deleted_at IS NULL").fetchall())


def _orders(pg):
    return {r["gtin"]: r for r in snapshot.catalog_for_management(pg)}


def _dl(pg):
    return {r["gtin"]: r for r in dl_snapshot.dl_catalog_for_management(pg)}


def _counts(pg):
    return {t: pg.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("audit_log", "pending_alerts", "catalog_overrides",
                      "dl_catalog_overrides")}


def _baseline(pg):
    """v1 pushed + one sync over it (seeds the history, no change: our names == CODEX's)."""
    _seed_catalogs(pg)
    _push(pg, V1, hours_old=5)
    res = codex_sync.run(pg, _cfg())
    assert res["mode"] == "apply"
    assert (res["renamed"], res["renumbered"], res["removed"]) == (0, 0, 0)


# --- (a) name drift -------------------------------------------------------------------------

def test_a_drifted_name_takes_the_codex_name_in_both_catalogs_alias_untouched(pg):
    _baseline(pg)
    v2 = [dict(r, name="Chlieb pšeničný voľný 1000g") if r["code"] == CHLIEB else r
          for r in V1]
    _push(pg, v2, hours_old=1)
    res = codex_sync.run(pg, _cfg())
    assert res["renamed"] == 1
    orders = _orders(pg)
    assert orders[CHLIEB]["name"] == "Chlieb pšeničný voľný 1000g"
    assert orders[ROZOK]["alias"] == "rozok slanina", "another card's alias untouched"
    row = pg.execute("SELECT actor, action, before, after FROM audit_log "
                     "WHERE table_name = 'catalog_overrides' AND row_id = %s",
                     (CHLIEB,)).fetchone()
    assert row == ("codex-sync", "update", {"name": "Chlieb pšeničný 1000g"},
                   {"name": "Chlieb pšeničný voľný 1000g"})


def test_a_rename_keeps_the_dl_cards_doplnok_mass_sklad_and_cena(pg):
    _baseline(pg)
    v2 = [dict(r, name="Múka pšeničná hladká T650 00") if r["code"] == MUKA else r for r in V1]
    _push(pg, v2, hours_old=1)
    assert codex_sync.run(pg, _cfg())["renamed"] == 1
    card = _dl(pg)[MUKA]
    assert card["name"] == "Múka pšeničná hladká T650 00"
    assert (card["doplnok"], card["sklad"], card["cena"]) == ("hladká", "100", 0.37)


def test_a_cosmetic_name_difference_is_not_drift(pg):
    """„1000 gr" vs „1000g", case, word order — the #467 name_key: not a rename."""
    _baseline(pg)
    v2 = [dict(r, name="CHLIEB 1000 gr pšeničný") if r["code"] == CHLIEB else r for r in V1]
    _push(pg, v2, hours_old=1)
    assert codex_sync.run(pg, _cfg())["renamed"] == 0
    assert _orders(pg)[CHLIEB]["name"] == "Chlieb pšeničný 1000g"


def test_a_rename_is_restorable_from_the_kos(pg):
    _baseline(pg)
    v2 = [dict(r, name="Chlieb pšeničný voľný 1000g") if r["code"] == CHLIEB else r
          for r in V1]
    _push(pg, v2, hours_old=1)
    codex_sync.run(pg, _cfg())
    aid = pg.execute("SELECT id FROM audit_log WHERE table_name = 'catalog_overrides' "
                     "AND row_id = %s AND action = 'update'", (CHLIEB,)).fetchone()[0]
    assert audit.restore(pg, aid) is True
    assert _orders(pg)[CHLIEB]["name"] == "Chlieb pšeničný 1000g"


# --- (b) renumber ---------------------------------------------------------------------------

def _v2_renumbered():
    return [dict(r, code=ROZOK_NEW) if r["code"] == ROZOK else r for r in V1]


def test_a_renumber_rewrites_the_code_in_both_catalogs(pg):
    _baseline(pg)
    _push(pg, _v2_renumbered(), hours_old=1)
    res = codex_sync.run(pg, _cfg())
    assert res["renumbered"] == 2, "one per catalog holding the card"
    orders, dl = _orders(pg), _dl(pg)
    assert ROZOK not in orders and ROZOK not in dl
    assert orders[ROZOK_NEW]["name"] == "Rožok so slaninou 70g"
    assert orders[ROZOK_NEW]["alias"] == "rozok slanina", "the alias moves with the card"
    new = dl[ROZOK_NEW]
    assert (new["doplnok"], new["mass"], new["sklad"], new["cena"]) == (
        "rožok slanina", 0.07, "1", 0.35)
    # the old number sits in the Kôš (soft delete), never hard-deleted
    for table in ("catalog_overrides", "dl_catalog_overrides"):
        retired, deleted = pg.execute(
            f"SELECT retired, deleted_at FROM {table} WHERE gtin = %s", (ROZOK,)).fetchone()
        assert retired is True and deleted is not None


def test_a_renumber_rewrites_every_memory_table_audited_per_row(pg):
    _baseline(pg)
    _seed_memory(pg)
    _push(pg, _v2_renumbered(), hours_old=1)
    codex_sync.run(pg, _cfg())
    for table in ("item_memory", "global_item_memory", "dl_item_memory"):
        assert set(_gtins(pg, table)) == {ROZOK_NEW}, table
    rows = pg.execute("SELECT table_name, before, after FROM audit_log WHERE action = 'update' "
                      "AND table_name LIKE '%%memory' ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["item_memory", "item_memory", "global_item_memory",
                                    "dl_item_memory"]
    assert all(r[1] == {"gtin": ROZOK} and r[2] == {"gtin": ROZOK_NEW} for r in rows)


def test_a_renumber_sends_one_ops_message(pg):
    _baseline(pg)
    _seed_memory(pg)
    _push(pg, _v2_renumbered(), hours_old=1)
    codex_sync.run(pg, _cfg())
    alerts = pg.execute("SELECT channel_id, kind, body_html FROM pending_alerts").fetchall()
    assert len(alerts) == 1
    channel, kind, body = alerts[0]
    assert channel == 77 and kind == codex_sync.ALERT_KIND
    assert f"{ROZOK} → {ROZOK_NEW}" in body and "27" in body and "upravené" in body


def test_a_memory_row_that_would_duplicate_an_existing_one_is_soft_deleted(pg):
    """item_memory is UNIQUE (customer, item_key, gtin, delivered_on): when the same mapping
    already exists under the new code, the old row goes to the Kôš instead of colliding."""
    _baseline(pg)
    _seed_memory(pg)
    pg.execute(
        "INSERT INTO item_memory (customer_ean, item_key, item_raw, gtin, card, delivered_on, "
        "source) VALUES ('C1', 'rozok slanina', 'rožok slanina', %s, 'Rožok', %s, 'ship')",
        (ROZOK_NEW, date(2026, 9, 1)))
    _push(pg, _v2_renumbered(), hours_old=1)
    codex_sync.run(pg, _cfg())
    assert _gtins(pg, "item_memory") == [ROZOK_NEW, ROZOK_NEW]
    old = pg.execute("SELECT gtin, deleted_at FROM item_memory WHERE customer_ean = 'C1' "
                     "AND gtin = %s", (ROZOK,)).fetchone()
    assert old is not None and old[1] is not None


def test_a_memory_rewrite_is_restorable_from_the_kos(pg):
    _baseline(pg)
    _seed_memory(pg)
    _push(pg, _v2_renumbered(), hours_old=1)
    codex_sync.run(pg, _cfg())
    aid = pg.execute("SELECT id FROM audit_log WHERE table_name = 'global_item_memory' "
                     "AND action = 'update'").fetchone()[0]
    assert audit.restore(pg, aid) is True
    assert _gtins(pg, "global_item_memory") == [ROZOK]


def test_the_incident_round_trip_x_to_y_and_back(pg):
    """Card 27: X → Y (24.9.) → X again (28.9.). Each change is followed, no card lost."""
    _baseline(pg)
    _seed_memory(pg)
    _push(pg, _v2_renumbered(), hours_old=3)
    codex_sync.run(pg, _cfg())
    _push(pg, V1, hours_old=1)
    res = codex_sync.run(pg, _cfg())
    assert res["renumbered"] == 2
    assert ROZOK in _orders(pg) and ROZOK_NEW not in _orders(pg)
    assert ROZOK in _dl(pg) and ROZOK_NEW not in _dl(pg)
    assert _orders(pg)[ROZOK]["alias"] == "rozok slanina"
    for table in ("item_memory", "global_item_memory", "dl_item_memory"):
        assert set(_gtins(pg, table)) == {ROZOK}, table


def test_a_renumber_onto_a_code_we_already_have_merges_into_it(pg):
    """Review 30 (contract changed): our ROZOK_NEW is the #477 pick of card 27's new code (the
    pick names its CODEX card). A hand-typed, unbound ROZOK_NEW named otherwise is no longer
    taken for card 27 on the one list where 27 first shows up on its code."""
    _baseline(pg)
    _seed_memory(pg)
    _push(pg, _v2_renumbered(), hours_old=1)
    card_guard.add_from_codex(pg, "orders", ROZOK_NEW, actor="sklad")
    codex_sync.run(pg, _cfg())
    orders = _orders(pg)
    assert ROZOK not in orders
    assert orders[ROZOK_NEW]["name"] == "Rožok so slaninou 70g", "renamed to CODEX's name"
    assert set(_gtins(pg, "item_memory")) == {ROZOK_NEW}


def test_an_ambiguous_successor_is_left_for_a_human(pg):
    """Card 27 now carries TWO new codes on stredisko 1 — nothing is guessed."""
    _baseline(pg)
    _seed_memory(pg)
    v2 = _v2_renumbered() + [_row("9990000000109", "27", "Rožok so slaninou 70g")]
    _push(pg, v2, hours_old=1)
    res = codex_sync.run(pg, _cfg())
    assert res["renumbered"] == 0 and res["review"] >= 1
    assert ROZOK in _orders(pg) and ROZOK in _dl(pg)
    assert set(_gtins(pg, "item_memory")) == {ROZOK}
    body = pg.execute("SELECT body_html FROM pending_alerts").fetchone()[0]
    assert ROZOK in body and "skontrolovať" in body


def test_a_second_run_over_the_same_list_changes_nothing(pg):
    _baseline(pg)
    _seed_memory(pg)
    _push(pg, _v2_renumbered(), hours_old=1)
    codex_sync.run(pg, _cfg())
    before = _counts(pg)
    res = codex_sync.run(pg, _cfg())
    assert (res["renamed"], res["renumbered"], res["removed"]) == (0, 0, 0)
    assert _counts(pg) == before


# --- (c) a code gone with no successor -------------------------------------------------------

def test_a_code_that_left_codex_without_a_successor_goes_to_the_kos(pg):
    """Only after the card is missing from TWO consecutive CODEX snapshots (review 2 🔵: one
    missing push is an export glitch, and a removal never comes back by itself)."""
    _baseline(pg)
    _push(pg, [r for r in V1 if r["code"] != KOLAC], hours_old=2)
    assert codex_sync.run(pg, _cfg())["removed"] == 0
    assert KOLAC in _orders(pg) and KOLAC in _dl(pg)
    _push(pg, [r for r in V1 if r["code"] != KOLAC], hours_old=1)
    res = codex_sync.run(pg, _cfg())
    assert res["removed"] == 2
    assert KOLAC not in _orders(pg) and KOLAC not in _dl(pg)
    assert pg.execute("SELECT count(*) FROM audit_log WHERE action = 'delete' AND row_id = %s",
                      (KOLAC,)).fetchone()[0] == 2
    body = pg.execute("SELECT body_html FROM pending_alerts").fetchone()[0]
    assert KOLAC in body and "Koš" in body


def test_a_code_without_any_codex_history_is_never_touched(pg):
    """A card CODEX never had while we watched (the #467 'missing' cards) is not ours to
    delete — the board's missing filter covers it."""
    _seed_catalogs(pg)
    snapshot.upsert_catalog_card(pg, "9990000000116", "Karta mimo CODEX")
    snapshot.rebuild_from_overrides(pg)
    _push(pg, V1, hours_old=5)
    codex_sync.run(pg, _cfg())
    _push(pg, V1, hours_old=1)
    res = codex_sync.run(pg, _cfg())
    assert res["removed"] == 0
    assert "9990000000116" in _orders(pg)


# --- (d) stale list, dry-run, never adds -----------------------------------------------------

def test_a_stale_list_writes_nothing_and_warns(pg, caplog):
    _baseline(pg)
    _seed_memory(pg)
    before = _counts(pg)
    _push(pg, _v2_renumbered(), hours_old=40)
    with caplog.at_level(logging.WARNING, logger="orders.codex_sync"):
        res = codex_sync.run(pg, _cfg())
    assert res["mode"] == "skipped"
    assert _counts(pg) == before
    assert ROZOK in _orders(pg) and set(_gtins(pg, "item_memory")) == {ROZOK}
    assert any(r.name == "orders.codex_sync" and "stale" in r.getMessage()
               for r in caplog.records)


def test_dry_run_computes_and_reports_but_writes_no_catalog_memory_audit_or_alert(pg):
    _seed_catalogs(pg)
    _seed_memory(pg)
    _push(pg, V1, hours_old=5)
    codex_sync.run(pg, _cfg(apply=False))
    v2 = [dict(r, name="Chlieb pšeničný voľný 1000g") if r["code"] == CHLIEB else r
          for r in _v2_renumbered() if r["code"] != KOLAC]
    before = _counts(pg)
    _push(pg, v2, hours_old=2)
    codex_sync.run(pg, _cfg(apply=False))
    _push(pg, v2, hours_old=1)
    res = codex_sync.run(pg, _cfg(apply=False))
    assert res["mode"] == "dry-run"
    assert (res["renamed"], res["renumbered"], res["removed"]) == (1, 2, 2)
    assert _counts(pg) == before
    assert ROZOK in _orders(pg) and KOLAC in _dl(pg)
    assert _orders(pg)[CHLIEB]["name"] == "Chlieb pšeničný 1000g"
    assert set(_gtins(pg, "item_memory")) == {ROZOK}
    applied, status, report = pg.execute(
        "SELECT applied, status, report FROM codex_sync_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert applied is False and status == "dry-run"
    assert {(r["scope"], r["from"], r["to"]) for r in report["renumbers"]} == {
        ("orders", ROZOK, ROZOK_NEW), ("dl", ROZOK, ROZOK_NEW)}
    memory = {r["scope"]: r["memory"] for r in report["renumbers"]}
    assert memory == {"orders": {"item_memory": 2, "global_item_memory": 1},
                      "dl": {"dl_item_memory": 1}}
    assert {(r["scope"], r["gtin"]) for r in report["removals"]} == {
        ("orders", KOLAC), ("dl", KOLAC)}
    assert [(r["gtin"], r["new"]) for r in report["renames"]] == [
        (CHLIEB, "Chlieb pšeničný voľný 1000g")]


def test_the_default_config_is_dry_run():
    assert Config(pg_dsn="", data_dir="/tmp").codex_sync_apply is False


def test_the_sync_never_adds_a_codex_card_we_do_not_have(pg):
    """A CODEX card absent from a catalog stays absent — even when it is renumbered or its
    name changes (MUKA is a DL-only card; CUDZIA is in no catalog)."""
    _baseline(pg)
    v2 = [dict(r, code="9990000000123") if r["code"] == MUKA
          else dict(r, name="Pagáč syrový veľký 90g") if r["code"] == CUDZIA else r
          for r in V1]
    _push(pg, v2, hours_old=1)
    codex_sync.run(pg, _cfg())
    orders = _orders(pg)
    assert "9990000000123" not in orders and CUDZIA not in orders and MUKA not in orders
    assert CUDZIA not in _dl(pg)
    assert "9990000000123" in _dl(pg), "the DL card itself was renumbered"


CHLIEB_NEW = "9990000000130"


def _v2_two_renumbers():
    """Card 27 ROZOK → ROZOK_NEW and card 31 CHLIEB → CHLIEB_NEW: two code changes."""
    return [dict(r, code=CHLIEB_NEW) if r["code"] == CHLIEB else r for r in _v2_renumbered()]


def test_too_many_code_changes_at_once_apply_nothing(pg):
    """A half-broken CODEX export that still passes the push's shrink guard must not strip
    our catalogs: over the limit (the `codex_sync_max_code_changes` option) the code changes
    wait for a human."""
    _baseline(pg)
    _push(pg, _v2_two_renumbers(), hours_old=1)
    res = codex_sync.run(pg, _cfg(codex_sync_max_code_changes=1))
    assert res["mode"] == "blocked" and res["codes"] == 2
    assert ROZOK in _orders(pg) and CHLIEB in _orders(pg)
    body = pg.execute("SELECT body_html FROM pending_alerts").fetchone()[0]
    assert "naraz" in body and "codex_sync_max_code_changes" in body


def test_a_blocked_plan_alerts_once_not_on_every_push(pg):
    """Review 🟡: the same blocked plan pushed again twice a day must not re-post the same
    ZASTAVILA message every time (once, then at most a morning reminder)."""
    _baseline(pg)
    _push(pg, _v2_two_renumbers(), hours_old=3)
    assert codex_sync.run(pg, _cfg(codex_sync_max_code_changes=1))["mode"] == "blocked"
    _push(pg, _v2_two_renumbers(), hours_old=1)
    assert codex_sync.run(pg, _cfg(codex_sync_max_code_changes=1))["mode"] == "blocked"
    assert pg.execute("SELECT count(*) FROM pending_alerts").fetchone()[0] == 1


def test_a_garbled_export_renaming_everything_is_blocked(pg):
    """Review 🟡: a mojibake CODEX export makes every card drift — over
    `codex_sync_max_renames` nothing is renamed."""
    _baseline(pg)
    garbled = [dict(r, name=r["name"].replace("ž", "Å¾").replace("á", "Ã¡") + " ??")
               for r in V1]
    _push(pg, garbled, hours_old=1)
    res = codex_sync.run(pg, _cfg(codex_sync_max_renames=2))
    assert res["mode"] == "blocked" and res["renamed"] >= 3
    assert _orders(pg)[ROZOK]["name"] == "Rožok so slaninou 70g"
    assert "ZASTAVILA" in pg.execute("SELECT body_html FROM pending_alerts").fetchone()[0]


def test_dry_run_reports_that_an_apply_would_be_blocked(pg):
    _seed_catalogs(pg)
    _push(pg, V1, hours_old=5)
    codex_sync.run(pg, _cfg(apply=False))
    _push(pg, _v2_two_renumbers(), hours_old=1)
    res = codex_sync.run(pg, _cfg(apply=False, codex_sync_max_code_changes=1))
    assert res["mode"] == "dry-run" and res["would_block"] is True
    report = pg.execute("SELECT report FROM codex_sync_runs ORDER BY id DESC LIMIT 1"
                        ).fetchone()[0]
    assert report["would_block"] is True


# --- a code REUSED by another CODEX card (review 🔴) ---------------------------------------

def test_a_code_reused_by_another_card_is_followed_never_renamed_to_it(pg):
    """Card 27 moves ROZOK → ROZOK_NEW and card 79 (a pagáč) takes ROZOK: our rožok card
    follows ITS card to ROZOK_NEW — it is never renamed to „Pagáč"."""
    _baseline(pg)
    _seed_memory(pg)
    v2 = [dict(r, code=ROZOK_NEW) if r["code"] == ROZOK
          else dict(r, code=ROZOK) if r["code"] == CUDZIA else r for r in V1]
    _push(pg, v2, hours_old=1)
    res = codex_sync.run(pg, _cfg())
    assert res["renumbered"] == 2 and res["renamed"] == 0
    orders = _orders(pg)
    assert ROZOK not in orders
    assert orders[ROZOK_NEW]["name"] == "Rožok so slaninou 70g"
    assert orders[ROZOK_NEW]["alias"] == "rozok slanina"
    assert set(_gtins(pg, "item_memory")) == {ROZOK_NEW}


def test_two_of_our_cards_swapping_codes_are_left_for_a_human(pg):
    """Card 27 takes CHLIEB's code and card 31 takes ROZOK's. Orders has BOTH cards: nothing
    is merged, nothing renamed to the other product — both go to review. DL has only the
    rožok: it follows ITS card (27) to its new code (identity, not the code)."""
    _baseline(pg)
    _seed_memory(pg)
    v2 = [dict(r, code=CHLIEB) if r["code"] == ROZOK
          else dict(r, code=ROZOK) if r["code"] == CHLIEB else r for r in V1]
    _push(pg, v2, hours_old=1)
    res = codex_sync.run(pg, _cfg())
    assert (res["renumbered"], res["renamed"], res["review"]) == (1, 0, 2)
    orders = _orders(pg)
    assert orders[ROZOK]["name"] == "Rožok so slaninou 70g"
    assert orders[CHLIEB]["name"] == "Chlieb pšeničný 1000g"
    assert set(_gtins(pg, "item_memory")) == {ROZOK}
    assert _dl(pg)[CHLIEB]["name"] == "Rožok so slaninou 70g"
    assert set(_gtins(pg, "dl_item_memory")) == {CHLIEB}


PAGAC_W = "9990000000147"   # card 79's NEXT code in the reuse chains below


def _reused(v, x=ROZOK, to=ROZOK_NEW, pagac_code=ROZOK):
    """Card 27 moves `x` → `to`, card 79 (the pagáč) now carries `pagac_code`."""
    return [dict(r, code=to) if r["card_code"] == "27"
            else dict(r, code=pagac_code) if r["card_code"] == "79" else r for r in v]


def test_a_reuse_chain_seen_only_in_dry_run_is_still_followed_by_card(pg):
    """Review 2 🔴: during the dry-run, card 27 moves ROZOK → ROZOK_NEW and the pagáč card
    takes ROZOK, then moves on to W. When apply goes on, our rožok follows card 27 — never the
    pagáč card that held the code last."""
    _seed_catalogs(pg)
    _seed_memory(pg)
    _push(pg, V1, hours_old=5)
    codex_sync.run(pg, _cfg(apply=False))
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg(apply=False))
    _push(pg, _reused(V1, pagac_code=PAGAC_W), hours_old=3)
    res = codex_sync.run(pg, _cfg())
    assert res["renumbered"] == 2 and res["renamed"] == 0
    orders = _orders(pg)
    assert ROZOK not in orders and PAGAC_W not in orders
    assert orders[ROZOK_NEW]["name"] == "Rožok so slaninou 70g"
    assert set(_gtins(pg, "item_memory")) == {ROZOK_NEW}


def test_our_card_whose_codex_card_vanished_never_follows_the_code_to_another_card(pg):
    """Review 2 🔴: card 27 disappears and the pagáč card takes ROZOK — our rožok waits one
    snapshot, then goes to review (never renamed); when the pagáč moves on, our card leaves
    with its own card (Kôš) — its memory never lands on the pagáč's new code."""
    _baseline(pg)
    _seed_memory(pg)
    gone = [r for r in _reused(V1, to=ROZOK) if r["card_code"] != "27"]
    _push(pg, gone, hours_old=4)
    res = codex_sync.run(pg, _cfg())
    assert (res["renumbered"], res["renamed"], res["removed"], res["review"]) == (0, 0, 0, 0)
    _push(pg, gone, hours_old=3)
    res = codex_sync.run(pg, _cfg())
    assert (res["renamed"], res["review"]) == (0, 2)
    assert _orders(pg)[ROZOK]["name"] == "Rožok so slaninou 70g"
    moved_on = [dict(r, code=PAGAC_W) if r["card_code"] == "79" else r for r in gone]
    _push(pg, moved_on, hours_old=2)
    res = codex_sync.run(pg, _cfg())
    assert res["removed"] == 2 and res["renumbered"] == 0
    assert ROZOK not in _orders(pg) and PAGAC_W not in _orders(pg)
    assert set(_gtins(pg, "item_memory")) == {ROZOK}, "memory never moved to the pagáč"


def test_a_new_code_another_card_still_carries_is_never_merged(pg):
    """Review 2 🔴: card 27 takes KOLAC's code while card 55 keeps it — no silent merge of the
    rožok into the koláč."""
    _baseline(pg)
    _seed_memory(pg)
    _push(pg, [dict(r, code=KOLAC) if r["card_code"] == "27" else r for r in V1],
          hours_old=1)
    res = codex_sync.run(pg, _cfg())
    assert res["renumbered"] == 0 and res["review"] >= 2
    assert ROZOK in _orders(pg) and ROZOK in _dl(pg)
    assert set(_gtins(pg, "item_memory")) == {ROZOK}


def test_two_cards_moving_to_the_same_new_code_are_both_reviewed(pg):
    _baseline(pg)
    _push(pg, [dict(r, code=ROZOK_NEW) if r["card_code"] in ("27", "55") else r for r in V1],
          hours_old=1)
    res = codex_sync.run(pg, _cfg())
    assert res["renumbered"] == 0
    assert ROZOK in _orders(pg) and KOLAC in _orders(pg) and ROZOK_NEW not in _orders(pg)


def test_a_name_on_another_stredisko_does_not_hide_a_reused_code(pg):
    """Review 2 🟡: a stredisko-4 row still carrying ROZOK under the rožok's name changes
    nothing — our card follows card 27 on stredisko 1."""
    _baseline(pg)
    v2 = _reused(V1) + [_row(ROZOK, "27", "Rožok so slaninou 70g", sklad=4, stredisko=4)]
    _push(pg, v2, hours_old=1)
    assert codex_sync.run(pg, _cfg())["renumbered"] == 2
    assert ROZOK_NEW in _orders(pg) and ROZOK not in _orders(pg)


def test_a_one_time_duplicate_carrier_does_not_switch_renames_off(pg):
    """Review 2 🟡: ROZOK sat on card 28 too for one push — a later CODEX rename of card 27
    still reaches our card."""
    _baseline(pg)
    _push(pg, V1 + [_row(ROZOK, "28", "Rožok so slaninou 70g")], hours_old=3)
    codex_sync.run(pg, _cfg())
    _push(pg, V1, hours_old=2)
    codex_sync.run(pg, _cfg())
    _push(pg, [dict(r, name="Rožok so slaninou a syrom 70g") if r["code"] == ROZOK else r
               for r in V1], hours_old=1)
    assert codex_sync.run(pg, _cfg())["renamed"] == 2
    assert _orders(pg)[ROZOK]["name"] == "Rožok so slaninou a syrom 70g"


def test_our_card_recreated_in_codex_under_the_same_name_is_rebound(pg):
    """Review 2 🟡: card 27 recreated as 127 with the same code + name → the binding moves
    silently (no review) and a later rename of 127 reaches our card."""
    _baseline(pg)
    recreated = [dict(r, card_code="127") if r["card_code"] == "27" else r for r in V1]
    _push(pg, recreated, hours_old=4)
    codex_sync.run(pg, _cfg())
    _push(pg, recreated, hours_old=3)
    res = codex_sync.run(pg, _cfg())
    assert (res["review"], res["removed"], res["renamed"]) == (0, 0, 0)
    assert pg.execute("SELECT card_code FROM codex_card_bindings WHERE scope = 'orders' "
                      "AND gtin = %s", (ROZOK,)).fetchone()[0] == "127"
    _push(pg, [dict(r, name="Rožok slaninový 70g") if r["code"] == ROZOK else r
               for r in recreated], hours_old=2)
    assert codex_sync.run(pg, _cfg())["renamed"] == 2


def test_an_older_codex_snapshot_never_undoes_a_renumber(pg):
    """Review 2 🔵: a re-sent OLDER list is skipped."""
    _baseline(pg)
    _push(pg, _v2_renumbered(), hours_old=1)
    codex_sync.run(pg, _cfg())
    _push(pg, V1, hours_old=3)
    res = codex_sync.run(pg, _cfg())
    assert res["mode"] == "skipped"
    assert ROZOK_NEW in _orders(pg) and ROZOK not in _orders(pg)


def test_memory_of_a_code_that_was_never_our_card_is_never_moved(pg):
    """Review 2 🔴 (memory path): a mapping row with a code no card of ours was ever bound to
    never follows that code's card anywhere."""
    _baseline(pg)
    pg.execute("INSERT INTO item_memory (customer_ean, item_key, item_raw, gtin, card, "
               "delivered_on, source) VALUES ('C5', 'pagac', 'pagáč', %s, 'Pagáč', %s, "
               "'ship')", (CUDZIA, date(2026, 9, 2)))
    _push(pg, [dict(r, code=PAGAC_W) if r["card_code"] == "79" else r for r in V1],
          hours_old=1)
    res = codex_sync.run(pg, _cfg())
    assert res["memory_renumbered"] == 0
    assert _gtins(pg, "item_memory") == [CUDZIA]


# --- binding lifecycle: a human re-adds / picks a card (review 3) ------------------------------

def _teach(pg, customer, key, gtin, at=None, delivered=date(2026, 9, 20)):
    """A human-taught mapping — `at` = when it was taught (default: now, i.e. after every
    synthetic CODEX snapshot of a test; a mapping from BEFORE a code change passes an `at`
    older than the pushes)."""
    pg.execute("INSERT INTO item_memory (customer_ean, item_key, item_raw, gtin, card, "
               "delivered_on, source, created_at) VALUES (%s, %s, %s, %s, 'x', %s, 'human', "
               "COALESCE(%s, now()))", (customer, key, key, gtin, delivered, at))


def test_a_repicked_old_number_is_the_picked_card_not_the_old_binding(pg):
    """Review 3 🔴: the sync moved our rožok ROZOK → ROZOK_NEW, the pagáč card took ROZOK and
    the warehouse picked it (#477) and taught a pagáč wording — the next sync must NOT merge
    the pagáč into the rožok nor move its memory; it is card 79 now."""
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=3)
    codex_sync.run(pg, _cfg())
    card_guard.add_from_codex(pg, "orders", ROZOK, actor="sklad")
    _teach(pg, "C7", "pagac syrovy", ROZOK)
    _push(pg, _reused(V1), hours_old=2)
    res = codex_sync.run(pg, _cfg())
    assert res["renumbered"] == 0
    orders = _orders(pg)
    assert orders[ROZOK]["name"] == "Pagáč syrový 60g", "the picked card takes ITS CODEX name"
    assert orders[ROZOK_NEW]["name"] == "Rožok so slaninou 70g"
    assert pg.execute("SELECT gtin FROM item_memory WHERE customer_ean = 'C7'"
                      ).fetchone()[0] == ROZOK
    assert pg.execute("SELECT card_code FROM codex_card_bindings WHERE scope = 'orders' "
                      "AND gtin = %s", (ROZOK,)).fetchone()[0] == "79"


def test_a_card_deleted_and_repicked_between_two_pushes_is_the_picked_card(pg):
    """Review 3 🔴: the warehouse deletes our rožok and re-picks ROZOK — now the pagáč — before
    the next sync ran; the stale binding (card 27) must not drag the pagáč along."""
    _baseline(pg)
    snapshot.retire_catalog_card(pg, ROZOK)
    snapshot.rebuild_from_overrides(pg)
    audit.record(pg, actor="sklad", table="catalog_overrides", row_id=ROZOK, action="delete")
    _push(pg, _reused(V1), hours_old=3)
    card_guard.add_from_codex(pg, "orders", ROZOK, actor="sklad")
    codex_sync.run(pg, _cfg())
    orders = _orders(pg)
    assert orders[ROZOK]["name"] == "Pagáč syrový 60g"
    assert ROZOK_NEW not in orders, "our orders rožok was deleted by a human — nothing follows"
    assert ROZOK_NEW in _dl(pg), "the DL rožok still follows its card"


def test_a_merge_keeps_our_curated_data_the_picked_target_lacks(pg):
    """Review 3 🟡: during the dry-run the warehouse picked card 27's new code (#477: only the
    CODEX name [+ sklad]); the applied merge keeps our alias / doplnok / mass / cena."""
    _seed_catalogs(pg)
    _push(pg, V1, hours_old=5)
    codex_sync.run(pg, _cfg(apply=False))
    _push(pg, _v2_renumbered(), hours_old=3)
    codex_sync.run(pg, _cfg(apply=False))
    card_guard.add_from_codex(pg, "orders", ROZOK_NEW, actor="sklad")
    card_guard.add_from_codex(pg, "dl", ROZOK_NEW, actor="sklad")
    _push(pg, _v2_renumbered(), hours_old=2)
    res = codex_sync.run(pg, _cfg())
    assert res["renumbered"] == 2
    assert _orders(pg)[ROZOK_NEW]["alias"] == "rozok slanina"
    new = _dl(pg)[ROZOK_NEW]
    assert (new["doplnok"], new["mass"], new["cena"]) == ("rožok slanina", 0.07, 0.35)
    assert ROZOK not in _orders(pg) and ROZOK not in _dl(pg)


def test_a_one_push_sole_carrier_is_never_bound(pg):
    """Review 3 🟡: ROZOK sits on cards 27 and 28 and our name matches neither (review); one
    export glitch without card 27 must not bind — and rename — our card to card 28 for good."""
    _seed_catalogs(pg)
    snapshot.upsert_catalog_card(pg, ROZOK, "Rožok starý názov")
    snapshot.rebuild_from_overrides(pg)
    shared = V1 + [_row(ROZOK, "28", "Bageta šunková 120g")]
    _push(pg, shared, hours_old=5)
    codex_sync.run(pg, _cfg())
    _push(pg, [r for r in shared if r["card_code"] != "27"], hours_old=3)
    res = codex_sync.run(pg, _cfg())
    assert res["renamed"] == 0
    assert _orders(pg)[ROZOK]["name"] == "Rožok starý názov"
    assert pg.execute("SELECT count(*) FROM codex_card_bindings WHERE scope = 'orders' "
                      "AND gtin = %s", (ROZOK,)).fetchone()[0] == 0


def test_a_failed_sync_does_not_count_as_a_seen_snapshot(pg, monkeypatch):
    """Review 3 🔵: a push whose sync FAILED is no snapshot the removal rule may count."""
    _baseline(pg)
    _push(pg, V1, hours_old=4)

    def boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(codex_sync.sp, "build_plan", boom)
    assert codex_sync.run_safely(pg, _cfg())["mode"] == "error"
    monkeypatch.undo()
    _push(pg, [r for r in V1 if r["code"] != KOLAC], hours_old=3)
    assert codex_sync.run(pg, _cfg())["removed"] == 0
    assert KOLAC in _orders(pg)


def test_a_legacy_twin_does_not_stop_a_retired_numbers_memory(pg):
    """Review 3 🔵: with the canonical ROZOK_NEW and a legacy „0"+ROZOK_NEW twin in DL, a
    memory row written later with the retired ROZOK still follows (to the canonical card)."""
    _baseline(pg)
    _push(pg, _v2_renumbered(), hours_old=3)
    codex_sync.run(pg, _cfg())
    dl_snapshot.upsert_dl_catalog_card(pg, "0" + ROZOK_NEW, "Rožok so slaninou 70g", sklad="1")
    dl_snapshot.dl_rebuild_from_overrides(pg)
    pg.execute("INSERT INTO dl_item_memory (supplier_ean, item_key, item_raw, gtin, card, "
               "delivered_on, cnt, source) VALUES ('S2', 'rozok', 'rožok', %s, 'R', %s, 1, "
               "'ship')", (ROZOK, date(2026, 9, 25)))
    _push(pg, _v2_renumbered(), hours_old=2)
    assert codex_sync.run(pg, _cfg())["memory_renumbered"] == 1
    assert _gtins(pg, "dl_item_memory") == [ROZOK_NEW]


# --- review 4: chains in one push, Kôš undo, re-picked reused number ---------------------------

def test_a_number_vacated_in_the_same_push_is_never_recreated_under_another_card(pg):
    """Review 4 🔴: card 31 moves CHLIEB → CHLIEB_NEW while card 55 takes CHLIEB, in ONE push.
    Orders holds both cards: the chlieb follows its card; the koláč must NOT be created onto
    the number being retired (the upsert kept `deleted_at` and the koláč vanished) — review."""
    _baseline(pg)
    v2 = [dict(r, code=CHLIEB_NEW) if r["card_code"] == "31"
          else dict(r, code=CHLIEB) if r["card_code"] == "55" else r for r in V1]
    _push(pg, v2, hours_old=1)
    codex_sync.run(pg, _cfg())
    orders = _orders(pg)
    assert orders[KOLAC]["name"] == "Koláč makový 80g", "the koláč never vanishes"
    assert orders[CHLIEB_NEW]["name"] == "Chlieb pšeničný 1000g"
    assert CHLIEB not in orders
    report = pg.execute("SELECT report FROM codex_sync_runs ORDER BY id DESC LIMIT 1"
                        ).fetchone()[0]
    assert ("orders", KOLAC) in {(r["scope"], r["gtin"]) for r in report["review"]}
    assert _dl(pg)[CHLIEB]["name"] == "Koláč makový 80g", "DL has no chlieb: it follows 55"


def test_the_same_chain_in_the_other_catalog_order_ends_the_same(pg):
    """Review 4 🔴 (order independence): the card taking the vacated number sorts FIRST."""
    _baseline(pg)
    v2 = [dict(r, code=CHLIEB_NEW) if r["card_code"] == "31"
          else dict(r, code=CHLIEB) if r["card_code"] == "27" else r for r in V1]
    _push(pg, v2, hours_old=1)
    codex_sync.run(pg, _cfg())
    orders = _orders(pg)
    assert orders[ROZOK]["name"] == "Rožok so slaninou 70g"
    assert orders[CHLIEB_NEW]["name"] == "Chlieb pšeničný 1000g" and CHLIEB not in orders


def test_a_kos_undo_of_a_sync_change_keeps_the_binding(pg):
    """Review 4 🟡: reverting the sync's rename in the Kôš is no new card — the binding stays,
    so when CODEX later reuses our code for the pagáč, our rožok is never re-bound to it."""
    _baseline(pg)
    _push(pg, [dict(r, name="Rožok slaninový 70g") if r["code"] == ROZOK else r for r in V1],
          hours_old=4)
    codex_sync.run(pg, _cfg())
    aid = pg.execute("SELECT id FROM audit_log WHERE table_name = 'catalog_overrides' "
                     "AND row_id = %s AND action = 'update'", (ROZOK,)).fetchone()[0]
    audit.restore(pg, aid, by="sklad")
    v2 = [dict(r, code=ROZOK_NEW, sklad=600) if r["card_code"] == "27"
          else dict(r, code=ROZOK) if r["card_code"] == "79" else r for r in V1]
    _push(pg, v2, hours_old=3)
    codex_sync.run(pg, _cfg())
    _push(pg, v2, hours_old=2)
    codex_sync.run(pg, _cfg())
    assert _orders(pg)[ROZOK]["name"] != "Pagáč syrový 60g"
    assert pg.execute("SELECT card_code FROM codex_card_bindings WHERE scope = 'orders' "
                      "AND gtin = %s", (ROZOK,)).fetchone()[0] == "27"


def test_a_repicked_reused_number_drops_the_old_products_data(pg):
    """Review 4 🟡: the #477 pick restores our Kôš card „as it was" — the rožok's alias /
    doplnok / mass / cena on what is now the pagáč; the sync resets them (audited)."""
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=3)
    codex_sync.run(pg, _cfg())
    card_guard.add_from_codex(pg, "orders", ROZOK, actor="sklad")
    card_guard.add_from_codex(pg, "dl", ROZOK, actor="sklad")
    _push(pg, _reused(V1), hours_old=2)
    res = codex_sync.run(pg, _cfg())
    assert res["reset"] == 2
    orders, dl = _orders(pg), _dl(pg)
    assert (orders[ROZOK]["name"], orders[ROZOK]["alias"]) == ("Pagáč syrový 60g", "")
    card = dl[ROZOK]
    assert (card["name"], card["doplnok"], card["mass"], card["cena"], card["sklad"]) == (
        "Pagáč syrový 60g", "", None, None, "1")
    assert orders[ROZOK_NEW]["alias"] == "rozok slanina", "the real rožok keeps its data"


def test_a_code_gone_whose_last_carriers_are_several_is_reviewed_not_silently_stuck(pg):
    """Review 4 🔵: an unbound card whose code left stredisko 1 while two cards carried it."""
    _seed_catalogs(pg)
    snapshot.upsert_catalog_card(pg, ROZOK, "Rožok starý názov")
    snapshot.rebuild_from_overrides(pg)
    _push(pg, V1 + [_row(ROZOK, "28", "Bageta šunková 120g")], hours_old=5)
    codex_sync.run(pg, _cfg())
    # review 28 (contract changed): the two cards missing from ONE list is no proof — the number
    # waits one list, then a human decides
    for hours in (3, 2):
        _push(pg, [r for r in V1 if r["code"] != ROZOK], hours_old=hours)
        codex_sync.run(pg, _cfg())
    report = pg.execute("SELECT report FROM codex_sync_runs ORDER BY id DESC LIMIT 1"
                        ).fetchone()[0]
    reasons = [r["reason"] for r in report["review"]
               if r["scope"] == "orders" and r["gtin"] == ROZOK]
    assert reasons and "naposledy" in reasons[0]


# --- review 5: one identity rule, resets that survive dry-run / blocked -----------------------

def _pagac_names(pg):
    return ([c["gtin"] for c in _orders(pg).values() if c["name"] == "Pagáč syrový 60g"],
            [c["gtin"] for c in _dl(pg).values() if c["name"] == "Pagáč syrový 60g"])


def test_a_kos_undo_of_a_retired_number_stays_our_card(pg):
    """Review 5 🟡: the sync retired ROZOK (card 27 moved to ROZOK_NEW), the pagáč took ROZOK,
    the warehouse „Vrátiť"-ed the sync's delete. It is still card 27's number: the next push
    merges it back into ROZOK_NEW — it never becomes the pagáč with the rožok's data."""
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=3)
    codex_sync.run(pg, _cfg())
    for table in ("catalog_overrides", "dl_catalog_overrides"):
        aid = pg.execute("SELECT id FROM audit_log WHERE actor = 'codex-sync' AND action = "
                         "'delete' AND table_name = %s AND row_id = %s", (table, ROZOK)
                         ).fetchone()[0]
        audit.restore(pg, aid, by="sklad")
    assert ROZOK in _orders(pg) and ROZOK in _dl(pg)
    _push(pg, _reused(V1), hours_old=2)
    codex_sync.run(pg, _cfg())
    assert _pagac_names(pg) == ([], [])
    assert ROZOK not in _orders(pg) and ROZOK not in _dl(pg)
    assert _orders(pg)[ROZOK_NEW]["alias"] == "rozok slanina"


def _repick_reused(pg):
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    card_guard.add_from_codex(pg, "orders", ROZOK, actor="sklad")
    card_guard.add_from_codex(pg, "dl", ROZOK, actor="sklad")


def _assert_reset(pg, gtin=ROZOK):
    assert _orders(pg)[gtin]["alias"] == ""
    card = _dl(pg)[gtin]
    assert (card["doplnok"], card["mass"], card["cena"]) == ("", None, None)


def test_a_pick_seen_by_a_blocked_run_still_resets_when_the_apply_comes(pg):
    """Review 5 🟡: the blocked run must not store the pick's binding — else the apply that
    follows sees no newer pick and the rožok's data stays on the pagáč for good."""
    _repick_reused(pg)
    _push(pg, _reused(V1), hours_old=3)
    assert codex_sync.run(pg, _cfg(codex_sync_max_renames=1))["mode"] == "blocked"
    _push(pg, _reused(V1), hours_old=2)
    assert codex_sync.run(pg, _cfg())["reset"] == 2
    _assert_reset(pg)


def test_a_pick_seen_by_a_dry_run_still_resets_when_the_apply_comes(pg):
    _repick_reused(pg)
    _push(pg, _reused(V1), hours_old=3)
    assert codex_sync.run(pg, _cfg(apply=False))["reset"] == 2
    _push(pg, _reused(V1), hours_old=2)
    assert codex_sync.run(pg, _cfg())["reset"] == 2
    _assert_reset(pg)


def test_a_picked_card_renumbered_in_the_same_push_carries_the_reset_data(pg):
    """Review 5 🟡: the picked pagáč moves ROZOK → W in the very next push — W is created from
    the RESET card, and the retired ROZOK row keeps both soft-delete flags together (#442)."""
    _repick_reused(pg)
    _push(pg, _reused(V1, pagac_code=PAGAC_W), hours_old=3)
    codex_sync.run(pg, _cfg())
    assert _orders(pg)[PAGAC_W]["name"] == "Pagáč syrový 60g"
    _assert_reset(pg, PAGAC_W)
    for table in ("catalog_overrides", "dl_catalog_overrides"):
        assert pg.execute(f"SELECT retired, deleted_at IS NOT NULL FROM {table} "
                          f"WHERE gtin = %s", (ROZOK,)).fetchone() == (True, True)


def test_a_repick_of_the_same_product_under_another_codex_card_keeps_its_data(pg):
    """Review 5 🔵: the code sits on two CODEX cards with the SAME product name; a delete +
    re-pick lands on the other card — same product, its doplnok / mass / cena stay."""
    _baseline(pg)
    dup = V1 + [dict(_row(ROZOK, "28", "Rožok so slaninou 70g"),
                     changed_at="2026-09-29T10:00:00+02:00")]
    _push(pg, dup, hours_old=3)
    codex_sync.run(pg, _cfg())
    dl_snapshot.retire_dl_catalog_card(pg, ROZOK)
    dl_snapshot.dl_rebuild_from_overrides(pg)
    audit.record(pg, actor="sklad", table="dl_catalog_overrides", row_id=ROZOK,
                 action="delete")
    card_guard.add_from_codex(pg, "dl", ROZOK, actor="sklad")
    # review 6 🔵: the pick really landed on the OTHER CODEX card (else this test is vacuous)
    assert pg.execute("SELECT after->>'codex_card' FROM audit_log WHERE actor = 'sklad' "
                      "AND action = 'create' AND table_name = 'dl_catalog_overrides' "
                      "ORDER BY id DESC LIMIT 1").fetchone()[0] == "28"
    _push(pg, dup, hours_old=2)
    assert codex_sync.run(pg, _cfg())["reset"] == 0
    card = _dl(pg)[ROZOK]
    assert (card["doplnok"], card["mass"], card["cena"]) == ("rožok slanina", 0.07, 0.35)


# --- review 6: durable rebind, memory through the one resolver, human rename -------------------

def test_a_recreated_card_rebound_during_dry_run_is_followed_by_the_apply(pg):
    """Review 6 🟡: card 27 recreated as 127 during the dry-run, then 127 renumbers — the
    first apply follows 127 (renumber), never removes our card."""
    _seed_catalogs(pg)
    _push(pg, V1, hours_old=6)
    codex_sync.run(pg, _cfg(apply=False))
    recreated = [dict(r, card_code="127") if r["card_code"] == "27" else r for r in V1]
    _push(pg, recreated, hours_old=5)
    codex_sync.run(pg, _cfg(apply=False))
    _push(pg, recreated, hours_old=4)
    codex_sync.run(pg, _cfg(apply=False))
    moved = [dict(r, code=ROZOK_NEW) if r["card_code"] == "127" else r for r in recreated]
    _push(pg, moved, hours_old=3)
    res = codex_sync.run(pg, _cfg())
    assert (res["removed"], res["renumbered"]) == (0, 2)
    assert ROZOK_NEW in _orders(pg) and ROZOK_NEW in _dl(pg)


def test_memory_of_a_retired_number_picked_as_another_product_is_never_moved(pg):
    """Review 6 🟡: the retired ROZOK was picked (#477) as the pagáč, a pagáč wording taught
    onto it, the card deleted again — that row is the pagáč's, never moved to the rožok."""
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    card_guard.add_from_codex(pg, "orders", ROZOK, actor="sklad")
    _teach(pg, "C8", "pagac syrovy", ROZOK)
    snapshot.retire_catalog_card(pg, ROZOK)
    snapshot.rebuild_from_overrides(pg)
    audit.record(pg, actor="sklad", table="catalog_overrides", row_id=ROZOK, action="delete")
    _push(pg, _reused(V1), hours_old=3)
    codex_sync.run(pg, _cfg())
    assert pg.execute("SELECT gtin FROM item_memory WHERE customer_ean = 'C8'"
                      ).fetchone()[0] == ROZOK


def _restore_and_rename(pg, name="Pagáč syrový 60g"):
    """Kôš „Vrátiť" of the sync's delete of the orders ROZOK, then a human rename (e.g. the
    Produkty drift button „Prevziať názov z CODEXu" — the code is the pagáč's in CODEX now)."""
    aid = pg.execute("SELECT id FROM audit_log WHERE actor = 'codex-sync' AND action = 'delete' "
                     "AND table_name = 'catalog_overrides' AND row_id = %s ORDER BY id DESC "
                     "LIMIT 1", (ROZOK,)).fetchone()[0]
    audit.restore(pg, aid, by="sklad")
    snapshot.upsert_catalog_card(pg, ROZOK, name)
    snapshot.rebuild_from_overrides(pg)


def _review_reason(pg, scope, gtin):
    report = pg.execute("SELECT report FROM codex_sync_runs ORDER BY id DESC LIMIT 1"
                        ).fetchone()[0]
    return " ".join(r["reason"] for r in report["review"]
                    if r["scope"] == scope and r["gtin"] == gtin)


def _binding(pg, gtin, scope="orders"):
    return pg.execute("SELECT card_code, active FROM codex_card_bindings WHERE scope = %s "
                      "AND gtin = %s", (scope, gtin)).fetchone()


def test_a_restored_retired_number_renamed_by_a_human_is_reviewed_never_merged_or_rebound(pg):
    """Review 6 🟡 → review 7 🟡 (contract changed): Kôš „Vrátiť" of the sync's delete of ROZOK,
    then a rename to the pagáč and a pagáč wording taught. Whether it is still the rožok (27) or
    now the pagáč (79) cannot be told from a name the drift button offers as cosmetic — a human
    decides (review with both cards): never merged into the rožok, never re-bound on the name
    alone (the rebind kept the rožok's alias / doplnok on the pagáč — review 7 F1b)."""
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    _restore_and_rename(pg)
    _teach(pg, "C9", "pagac syrovy", ROZOK)
    _push(pg, _reused(V1), hours_old=3)
    codex_sync.run(pg, _cfg())
    orders = _orders(pg)
    assert orders[ROZOK]["name"] == "Pagáč syrový 60g"
    assert (orders[ROZOK_NEW]["name"], orders[ROZOK_NEW]["alias"]) == (
        "Rožok so slaninou 70g", "rozok slanina")
    assert pg.execute("SELECT gtin FROM item_memory WHERE customer_ean = 'C9'"
                      ).fetchone()[0] == ROZOK
    assert _binding(pg, ROZOK) == ("27", False), "never re-bound on a guess"
    reason = _review_reason(pg, "orders", ROZOK)
    assert "27" in reason and "79" in reason and "Vybrať kartu z CODEXu" in reason


def test_rows_written_to_a_retired_number_before_its_repick_are_reviewed_never_moved(pg):
    """Review 6 🔵 → review 7 🔵 F3 (contract changed): a row's `created_at` cannot tell whose
    it is (a Naučené edit / revive re-points a row without touching it) — the rows older than
    the pick of ROZOK as the pagáč are left where they are and listed for a human."""
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    _teach(pg, "C10", "rozok slaninovy", ROZOK)
    card_guard.add_from_codex(pg, "orders", ROZOK, actor="sklad")
    _teach(pg, "C11", "pagac syrovy", ROZOK)
    _push(pg, _reused(V1), hours_old=3)
    codex_sync.run(pg, _cfg())
    got = dict(pg.execute("SELECT customer_ean, gtin FROM item_memory WHERE customer_ean IN "
                          "('C10', 'C11') AND deleted_at IS NULL").fetchall())
    assert got == {"C10": ROZOK, "C11": ROZOK}
    reason = _review_reason(pg, "orders", ROZOK)
    assert "Naučené" in reason and "1 naučen" in reason and "27" in reason


# --- review 7: a guess the sync cannot make is a human's, one product predicate -----------------

def test_a_restored_renamed_number_whose_new_holder_moved_on_is_never_merged(pg):
    """Review 7 🟡 F1a: restored + renamed to the pagáč, and the pagáč moved ROZOK → W before
    the next push — no carrier names the card any more, yet it must never fall back into the
    rožok (its pagáč wording would move onto the rožok)."""
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    _restore_and_rename(pg)
    _teach(pg, "C9", "pagac syrovy", ROZOK)
    _push(pg, _reused(V1, pagac_code=PAGAC_W), hours_old=3)
    codex_sync.run(pg, _cfg())
    orders = _orders(pg)
    assert orders[ROZOK]["name"] == "Pagáč syrový 60g"
    assert pg.execute("SELECT gtin FROM item_memory WHERE customer_ean = 'C9'"
                      ).fetchone()[0] == ROZOK
    assert "27" in _review_reason(pg, "orders", ROZOK)


def test_a_card_renumbering_onto_a_disputed_number_of_ours_is_reviewed(pg):
    """Review 7 (same rule): card 27 moves BACK onto ROZOK while our ROZOK is the restored card
    a human renamed to the pagáč — never merged into it, never renamed over the human's name."""
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    _restore_and_rename(pg)
    _push(pg, _reused(V1, to=ROZOK, pagac_code=PAGAC_W), hours_old=3)
    codex_sync.run(pg, _cfg())
    orders = _orders(pg)
    assert orders[ROZOK]["name"] == "Pagáč syrový 60g"
    assert orders[ROZOK_NEW]["alias"] == "rozok slanina"
    assert _review_reason(pg, "orders", ROZOK_NEW)


def test_a_kos_card_renamed_then_repicked_as_another_product_still_drops_the_old_data(pg):
    """Review 7 (the F1 review's own advice): restored + renamed to the pagáč, deleted, then
    picked (#477) as the pagáč — the pick restores it „as it was", the rožok's alias with it.
    The reset asks whether the picked product is the one the data was taught for (card 27's
    name vs card 79's), never whether OUR name matches — a human rename would hide it."""
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    _restore_and_rename(pg)
    snapshot.retire_catalog_card(pg, ROZOK)
    snapshot.rebuild_from_overrides(pg)
    audit.record(pg, actor="sklad", table="catalog_overrides", row_id=ROZOK, action="delete")
    card_guard.add_from_codex(pg, "orders", ROZOK, actor="sklad")
    assert _orders(pg)[ROZOK]["alias"] == "rozok slanina", "restored as it was"
    _push(pg, _reused(V1), hours_old=3)
    codex_sync.run(pg, _cfg())
    assert _orders(pg)[ROZOK]["alias"] == ""


def test_a_repicked_number_renumbered_in_the_same_push_never_misreports_its_rows(pg):
    """Review 7 🔵 F2: the pick of ROZOK as the pagáč and the pagáč's move ROZOK → W in the SAME
    push — the rows older than the pick go with the card; nothing may claim they moved to the
    rožok's ROZOK_NEW, and the human is told where they are now."""
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    _teach(pg, "C10", "rozok slaninovy", ROZOK)
    card_guard.add_from_codex(pg, "orders", ROZOK, actor="sklad")
    _push(pg, _reused(V1, pagac_code=PAGAC_W), hours_old=3)
    codex_sync.run(pg, _cfg())
    report = pg.execute("SELECT report FROM codex_sync_runs ORDER BY id DESC LIMIT 1"
                        ).fetchone()[0]
    assert not [r for r in report["renumbers"] if r["mode"] == "memory"]
    assert pg.execute("SELECT gtin FROM item_memory WHERE customer_ean = 'C10'"
                      ).fetchone()[0] == PAGAC_W
    reason = _review_reason(pg, "orders", ROZOK)
    assert "Naučené" in reason and PAGAC_W in reason
    body = pg.execute("SELECT body_html FROM pending_alerts ORDER BY id DESC LIMIT 1"
                      ).fetchone()[0]
    assert f"presunutých na {ROZOK_NEW}" not in body


def test_a_row_repointed_to_a_repicked_number_after_the_pick_is_never_moved(pg):
    """Review 7 🔵 F3: an OLDER row re-pointed onto the re-picked ROZOK after the pick (a Naučené
    edit keeps `created_at`) is the pagáč's — never moved to the rožok."""
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    _teach(pg, "C12", "pagac syrovy", CHLIEB)
    card_guard.add_from_codex(pg, "orders", ROZOK, actor="sklad")
    pg.execute("UPDATE item_memory SET gtin = %s WHERE customer_ean = 'C12'", (ROZOK,))
    _push(pg, _reused(V1), hours_old=3)
    codex_sync.run(pg, _cfg())
    assert pg.execute("SELECT gtin FROM item_memory WHERE customer_ean = 'C12'"
                      ).fetchone()[0] == ROZOK


# --- review 8: no memory is ever lost; the retire-time name tells a human rename ---------------

_MEMORY = ("item_memory", "global_item_memory", "dl_item_memory")


def _live_rows(pg):
    return {t: pg.execute(f"SELECT count(*) FROM {t} WHERE deleted_at IS NULL").fetchone()[0]
            for t in _MEMORY}


def _kos_delete(pg, gtin=ROZOK):
    """The warehouse deletes our orders card (Produkty → Kôš)."""
    snapshot.retire_catalog_card(pg, gtin)
    snapshot.rebuild_from_overrides(pg)
    audit.record(pg, actor="sklad", table="catalog_overrides", row_id=gtin, action="delete")


def _gtin_of(pg, customer):
    return pg.execute("SELECT gtin FROM item_memory WHERE customer_ean = %s AND deleted_at IS NULL",
                      (customer,)).fetchone()[0]


def test_a_round_trip_with_late_rows_on_the_retired_number_keeps_every_mapping(pg):
    """Review 8 🔴: card 27 ROZOK → ROZOK_NEW (applied), a frozen question / held order writes a
    late row onto the retired ROZOK (orders + DL), then card 27 goes BACK to ROZOK (the #478
    incident itself was such a round trip). The plan used to move ROZOK's rows onto ROZOK —
    each row found ITSELF as the duplicate and was soft-deleted: the card lost every mapping.
    The invariant: an applied sync never lowers the live mapping rows (no duplicates here)."""
    _baseline(pg)
    _seed_memory(pg)
    _push(pg, _v2_renumbered(), hours_old=4)
    codex_sync.run(pg, _cfg())
    before = _live_rows(pg)
    _teach(pg, "C70", "rozok neskoro", ROZOK)
    pg.execute("INSERT INTO dl_item_memory (supplier_ean, item_key, item_raw, gtin, card, "
               "delivered_on, cnt, source) VALUES ('S9', 'rozok neskoro', 'x', %s, 'x', %s, 1, "
               "'ship')", (ROZOK, date(2026, 9, 21)))
    _push(pg, V1, hours_old=3)
    codex_sync.run(pg, _cfg())
    after = _live_rows(pg)
    assert after == dict(before, item_memory=before["item_memory"] + 1,
                         dl_item_memory=before["dl_item_memory"] + 1)
    assert set(_gtins(pg, "item_memory")) == {ROZOK}
    assert set(_gtins(pg, "dl_item_memory")) == {ROZOK}


def test_the_ops_message_tells_merged_duplicates_from_moved_rows(pg):
    """Review 8 🔵: a row soft-deleted because the same mapping already lives under the new
    code was reported as „presunutých" — the message says how many moved and how many merged."""
    _baseline(pg)
    _seed_memory(pg)
    pg.execute(
        "INSERT INTO item_memory (customer_ean, item_key, item_raw, gtin, card, delivered_on, "
        "source) VALUES ('C1', 'rozok slanina', 'rožok slanina', %s, 'Rožok', %s, 'ship')",
        (ROZOK_NEW, date(2026, 9, 1)))
    _push(pg, _v2_renumbered(), hours_old=1)
    codex_sync.run(pg, _cfg())
    body = pg.execute("SELECT body_html FROM pending_alerts").fetchone()[0]
    assert "zlúčen" in body
    report = pg.execute("SELECT report FROM codex_sync_runs ORDER BY id DESC LIMIT 1"
                        ).fetchone()[0]
    orders = [r for r in report["renumbers"] if r["scope"] == "orders"][0]
    assert orders["merged"] == {"item_memory": 1, "global_item_memory": 0}


def test_a_disputed_number_deleted_as_advised_never_hands_its_wording_to_the_old_card(pg):
    """Review 8 🟡: the dispute review says „iný výrobok → zmaž ju (Kôš) a pri otázke ju vyber".
    The warehouse deletes it; a push arrives before any question — the pagáč wording taught
    during the dispute must not move to the rožok (its orders would match the rožok silently)."""
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=5)
    codex_sync.run(pg, _cfg())
    _restore_and_rename(pg)
    _teach(pg, "C30", "pagac syrovy", ROZOK)
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    assert _review_reason(pg, "orders", ROZOK)
    _kos_delete(pg)
    _push(pg, _reused(V1), hours_old=3)
    codex_sync.run(pg, _cfg())
    assert _gtin_of(pg, "C30") == ROZOK


def test_a_renumber_back_onto_a_deleted_disputed_number_waits_for_a_human(pg):
    """Review 8 🟡 (same rule, Kôš copy): card 27 moves BACK onto ROZOK after the warehouse
    deleted the disputed (pagáč-named) ROZOK — the Kôš card is never restored as the rožok with
    the pagáč wording on it; a human decides."""
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=5)
    codex_sync.run(pg, _cfg())
    _restore_and_rename(pg)
    _teach(pg, "C32", "pagac syrovy", ROZOK)
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    _kos_delete(pg)
    _push(pg, _reused(V1, to=ROZOK, pagac_code=PAGAC_W), hours_old=3)
    codex_sync.run(pg, _cfg())
    assert ROZOK not in _orders(pg) and ROZOK_NEW in _orders(pg)
    assert _gtin_of(pg, "C32") == ROZOK
    assert "Koši" in _review_reason(pg, "orders", ROZOK_NEW)


def test_following_the_recreated_card_advice_on_a_restored_removed_card_binds_it(pg):
    """Review 8 🟡: KOLAC removed (its card 55 left CODEX), CODEX recreated the product as card
    155 carrying KOLAC under a new name, the warehouse restored KOLAC. The review says
    „premenuj našu kartu na jej názov v CODEXe, pri ďalšom zozname sa priradí" — doing exactly
    that binds it to 155 (no second review sending the human back)."""
    _baseline(pg)
    gone = [r for r in V1 if r["card_code"] != "55"]
    _push(pg, gone, hours_old=4)
    codex_sync.run(pg, _cfg())
    _push(pg, gone, hours_old=3.5)
    assert codex_sync.run(pg, _cfg())["removed"] >= 1
    back = gone + [_row(KOLAC, "155", "Koláč s makovou náplňou 80g")]
    _push(pg, back, hours_old=3)
    codex_sync.run(pg, _cfg())
    aid = pg.execute("SELECT id FROM audit_log WHERE actor = 'codex-sync' AND action = 'delete' "
                     "AND table_name = 'catalog_overrides' AND row_id = %s ORDER BY id DESC "
                     "LIMIT 1", (KOLAC,)).fetchone()[0]
    audit.restore(pg, aid, by="sklad")
    _push(pg, back, hours_old=2.5)
    codex_sync.run(pg, _cfg())
    assert "premenuj" in _review_reason(pg, "orders", KOLAC)
    snapshot.upsert_catalog_card(pg, KOLAC, "Koláč s makovou náplňou 80g")
    snapshot.rebuild_from_overrides(pg)
    _push(pg, back, hours_old=2)
    codex_sync.run(pg, _cfg())
    assert _binding(pg, KOLAC)[:2] == ("155", True)
    assert not _review_reason(pg, "orders", KOLAC)


def test_a_kos_undo_of_a_drifted_cards_renumber_is_merged_back_not_disputed(pg):
    """Review 8 🔵: the first applied run renumbers a card whose name had drifted from CODEX
    (the live 56-card drift) and renames its new number; a plain „Vrátiť" of the delete is the
    round-5 Kôš undo — merged back, never a dispute claiming a human renamed it. The dispute
    keys on the name the sync RETIRED the number under, not on today's CODEX name."""
    _seed_catalogs(pg)
    snapshot.upsert_catalog_card(pg, ROZOK, "Rožok slaninový malý", alias="rozok slanina")
    snapshot.rebuild_from_overrides(pg)
    _push(pg, V1, hours_old=6)
    codex_sync.run(pg, _cfg(apply=False))
    _push(pg, V1, hours_old=5)
    codex_sync.run(pg, _cfg(apply=False))
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    aid = pg.execute("SELECT id FROM audit_log WHERE actor = 'codex-sync' AND action = 'delete' "
                     "AND table_name = 'catalog_overrides' AND row_id = %s ORDER BY id DESC "
                     "LIMIT 1", (ROZOK,)).fetchone()[0]
    audit.restore(pg, aid, by="sklad")
    _push(pg, _reused(V1), hours_old=3)
    codex_sync.run(pg, _cfg())
    assert ROZOK not in _orders(pg)
    assert not _review_reason(pg, "orders", ROZOK)


def test_a_dispute_of_a_code_gone_from_codex_never_promises_a_merge(pg):
    """Review 8 🔵: KOLAC left CODEX (card 55 gone), restored + renamed — renaming it back would
    only get it removed again; the review must not promise a merge with card 55."""
    _baseline(pg)
    gone = [r for r in V1 if r["card_code"] != "55"]
    _push(pg, gone, hours_old=4)
    codex_sync.run(pg, _cfg())
    _push(pg, gone, hours_old=3.5)
    codex_sync.run(pg, _cfg())
    aid = pg.execute("SELECT id FROM audit_log WHERE actor = 'codex-sync' AND action = 'delete' "
                     "AND table_name = 'catalog_overrides' AND row_id = %s ORDER BY id DESC "
                     "LIMIT 1", (KOLAC,)).fetchone()[0]
    audit.restore(pg, aid, by="sklad")
    snapshot.upsert_catalog_card(pg, KOLAC, "Koláč tvarohový 80g")
    snapshot.rebuild_from_overrides(pg)
    _push(pg, gone, hours_old=3)
    codex_sync.run(pg, _cfg())
    reason = _review_reason(pg, "orders", KOLAC)
    assert reason and "zlúči" not in reason


def test_a_persistent_review_is_alerted_once_even_when_another_reason_came_and_went(pg):
    """Review 8 🔵: a lasting review A on the picked ROZOK plus the one-shot re-pick review B in
    the same run („A Tiež: B"); the next run has only A — A must not be alerted again."""
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=5)
    codex_sync.run(pg, _cfg())
    _teach(pg, "C60", "rozok q", ROZOK)
    card_guard.add_from_codex(pg, "orders", ROZOK, actor="sklad")
    v = _reused(V1, pagac_code=PAGAC_W) + [_row(PAGAC_W, "88", "Iný výrobok 50g")]
    for hours in (4, 3, 2):
        _push(pg, v, hours_old=hours)
        codex_sync.run(pg, _cfg())
    bodies = [b for (b,) in pg.execute("SELECT body_html FROM pending_alerts WHERE kind = %s "
                                       "ORDER BY id", (codex_sync.ALERT_KIND,)).fetchall()]
    assert len([b for b in bodies if "nesie aj karta" in b]) == 1


# --- review 9: ONE contest rule wherever the sync would mutate a number ------------------------

def _drift_click(pg, gtin, name, table="catalog_overrides"):
    """Produkty „Prevziať názov z CODEXu" + Uložiť — a plain, audited name edit (the #467 drift
    button offers the name CODEX has for the code NOW, i.e. a reusing card's)."""
    if table == "catalog_overrides":
        before = _orders(pg)[gtin]["name"]
        snapshot.upsert_catalog_card(pg, gtin, name)
        snapshot.rebuild_from_overrides(pg)
    else:
        card = _dl(pg)[gtin]
        before = card["name"]
        dl_snapshot.upsert_dl_catalog_card(pg, gtin, name, doplnok=card["doplnok"] or "",
                                           mass=card["mass"], sklad=card["sklad"] or "",
                                           cena=card["cena"])
        dl_snapshot.dl_rebuild_from_overrides(pg)
    audit.record(pg, actor="sklad", table=table, row_id=gtin, action="update",
                 before={"name": before}, after={"name": name})


def _restore_sync_delete(pg, table, gtin):
    aid = pg.execute("SELECT id FROM audit_log WHERE actor = 'codex-sync' AND action = 'delete' "
                     "AND table_name = %s AND row_id = %s ORDER BY id DESC LIMIT 1",
                     (table, gtin)).fetchone()[0]
    audit.restore(pg, aid, by="sklad")


def test_a_drift_rename_to_the_code_reusers_name_holds_the_renumber_for_a_human(pg):
    """Review 9 🟡 F1: during the dry-run card 27 moved ROZOK → ROZOK_NEW and the pagáč took
    ROZOK; the warehouse clicked the drift button (ROZOK „Pagáč") and taught a pagáč wording.
    Whether our ROZOK is still the rožok or now the pagáč is a human's call — the apply never
    renumbers it into the rožok (the pagáč wording would move onto the rožok)."""
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=4.9)
    codex_sync.run(pg, _cfg(apply=False))
    _drift_click(pg, ROZOK, "Pagáč syrový 60g")
    _teach(pg, "C40", "pagac syrovy", ROZOK)
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    assert _orders(pg)[ROZOK]["name"] == "Pagáč syrový 60g"
    assert _gtin_of(pg, "C40") == ROZOK
    assert _binding(pg, ROZOK) == ("27", True)
    reason = _review_reason(pg, "orders", ROZOK)
    assert "27" in reason and "79" in reason


def test_a_rename_rebind_to_another_product_drops_the_old_products_data(pg):
    """Review 9 🟡 F2: code KOLAC left CODEX with card 55 (removed), then CODEX gave it to a
    DIFFERENT product (card 155); the warehouse restored KOLAC and clicked the drift button.
    The recreated-card rebind binds it to 155 — but a different product never keeps the koláč's
    DL doplnok / mass / cena, and the koláč's wordings go to a human."""
    _seed_catalogs(pg)
    dl_snapshot.upsert_dl_catalog_card(pg, KOLAC, "Koláč makový 80g", doplnok="makový koláč",
                                       mass=0.08, sklad="1", cena=0.31)
    dl_snapshot.dl_rebuild_from_overrides(pg)
    _push(pg, V1, hours_old=6)
    codex_sync.run(pg, _cfg())
    _teach(pg, "C50", "kolac makovy", KOLAC)
    gone = [r for r in V1 if r["card_code"] != "55"]
    _push(pg, gone, hours_old=5)
    codex_sync.run(pg, _cfg())
    _push(pg, gone, hours_old=4.5)
    assert codex_sync.run(pg, _cfg())["removed"] == 2
    back = gone + [_row(KOLAC, "155", "Pizza štangľa 90g")]
    _push(pg, back, hours_old=4)
    codex_sync.run(pg, _cfg())
    _restore_sync_delete(pg, "catalog_overrides", KOLAC)
    _restore_sync_delete(pg, "dl_catalog_overrides", KOLAC)
    _drift_click(pg, KOLAC, "Pizza štangľa 90g")
    _drift_click(pg, KOLAC, "Pizza štangľa 90g", table="dl_catalog_overrides")
    _push(pg, back, hours_old=3)
    codex_sync.run(pg, _cfg())
    card = _dl(pg)[KOLAC]
    assert (card["doplnok"], card["mass"], card["cena"]) == ("", None, None)
    assert _binding(pg, KOLAC, "dl") == ("155", True)
    assert "Naučené" in _review_reason(pg, "orders", KOLAC)


def test_a_pending_dispute_resolved_by_the_old_card_leaving_codex_drops_its_data(pg):
    """Review 9 🟡 F2: a pending dispute (restored ROZOK drift-renamed to the pagáč while card 27
    lived), then card 27 leaves CODEX for good — the rebind to the pagáč never keeps the
    rožok's alias."""
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=4.9)
    codex_sync.run(pg, _cfg())
    _restore_sync_delete(pg, "catalog_overrides", ROZOK)
    _drift_click(pg, ROZOK, "Pagáč syrový 60g")
    _push(pg, _reused(V1), hours_old=4.7)
    codex_sync.run(pg, _cfg())
    assert _review_reason(pg, "orders", ROZOK), "disputed while card 27 lives"
    no27 = [r for r in _reused(V1) if r["card_code"] != "27"]
    for hours in (4.5, 4.3):
        _push(pg, no27, hours_old=hours)
        codex_sync.run(pg, _cfg())
    assert _binding(pg, ROZOK)[0] == "79"
    assert _orders(pg)[ROZOK]["alias"] == ""


def test_a_restored_number_renamed_to_its_own_cards_new_codex_name_is_merged_back(pg):
    """Review 9 🔵 F5: CODEX renamed card 27 after the sync retired ROZOK; the warehouse
    restored ROZOK and took card 27's NEW name — the same product: merged back, no dispute."""
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=4.9)
    codex_sync.run(pg, _cfg())
    renamed = [dict(r, name="Rožok slaninový 70g") if r["card_code"] == "27" else r
               for r in _reused(V1)]
    _push(pg, renamed, hours_old=4)
    codex_sync.run(pg, _cfg())
    _restore_sync_delete(pg, "catalog_overrides", ROZOK)
    _drift_click(pg, ROZOK, "Rožok slaninový 70g")
    _push(pg, renamed, hours_old=3)
    codex_sync.run(pg, _cfg())
    assert ROZOK not in _orders(pg)
    assert not _review_reason(pg, "orders", ROZOK)


def test_a_twins_late_row_never_drags_a_repicked_numbers_rows(pg):
    """Review 9 🔵 F3: ROZOK and its legacy „0"+code twin retired; ROZOK re-picked as the pagáč
    (a pagáč wording), deleted again; a late row lands on the twin — the pagáč's row on ROZOK
    never follows the rožok. Review 10 (contract changed): the twin's late row was decided
    AFTER CODEX gave ROZOK to the pagáč, so it is held for a human too, never moved."""
    _seed_catalogs(pg)
    snapshot.upsert_catalog_card(pg, "0" + ROZOK, "Rožok so slaninou 70g")
    snapshot.rebuild_from_overrides(pg)
    _push(pg, V1, hours_old=6)
    codex_sync.run(pg, _cfg())
    _push(pg, _reused(V1), hours_old=5)
    codex_sync.run(pg, _cfg())
    card_guard.add_from_codex(pg, "orders", ROZOK, actor="sklad")
    _teach(pg, "C8", "pagac syrovy", ROZOK)
    _kos_delete(pg)
    _teach(pg, "C9", "rozok neskoro", "0" + ROZOK)
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    assert _gtin_of(pg, "C8") == ROZOK
    assert _gtin_of(pg, "C9") == "0" + ROZOK
    assert "Naučené" in _review_reason(pg, "orders", ROZOK)


def test_a_lone_legacy_twin_keeps_its_cards_binding(pg):
    """Review 9 🔵 F4: the whole group (canonical + legacy twin) is bound — after the warehouse
    deleted the canonical card and the code was reused, the lone twin still follows card 27,
    never identified from the list as the pagáč (the round-1 🔴 pattern through a twin)."""
    _seed_catalogs(pg)
    snapshot.upsert_catalog_card(pg, "0" + ROZOK, "Rožok so slaninou 70g")
    snapshot.rebuild_from_overrides(pg)
    _push(pg, V1, hours_old=6)
    codex_sync.run(pg, _cfg())
    assert _binding(pg, "0" + ROZOK) == ("27", True)
    _kos_delete(pg)
    for hours in (5, 4):
        _push(pg, _reused(V1), hours_old=hours)
        codex_sync.run(pg, _cfg())
    assert _pagac_names(pg)[0] == []
    assert _binding(pg, "0" + ROZOK)[0] == "27"


def test_a_rename_rebind_to_another_product_resets_the_legacy_twin_too(pg):
    """Round 9 follow-through: the reset of a number that became another product covers every
    number of its group — a legacy „0"+code twin never keeps the old product's alias."""
    _seed_catalogs(pg)
    snapshot.upsert_catalog_card(pg, "0" + ROZOK, "Rožok so slaninou 70g", alias="rozok twin")
    snapshot.rebuild_from_overrides(pg)
    _push(pg, V1, hours_old=6)
    codex_sync.run(pg, _cfg())
    reused = [dict(r, code=ROZOK) if r["card_code"] == "79" else r
              for r in V1 if r["card_code"] != "27"]
    _push(pg, reused, hours_old=5)
    codex_sync.run(pg, _cfg())
    _drift_click(pg, ROZOK, "Pagáč syrový 60g")
    _push(pg, reused, hours_old=4)
    codex_sync.run(pg, _cfg())
    orders = _orders(pg)
    assert (orders[ROZOK]["alias"], orders["0" + ROZOK]["alias"]) == ("", "")
    assert _binding(pg, "0" + ROZOK) == ("79", True)


def test_a_blocked_run_never_says_rows_were_moved(pg):
    """Review 9 🔵 F6: the blocked alert says nothing changed — its memory counts are what
    WOULD move, never „presunutých"."""
    _baseline(pg)
    _seed_memory(pg)
    _push(pg, _v2_two_renumbers(), hours_old=3)
    assert codex_sync.run(pg, _cfg(codex_sync_max_code_changes=1))["mode"] == "blocked"
    body = pg.execute("SELECT body_html FROM pending_alerts ORDER BY id DESC LIMIT 1"
                      ).fetchone()[0]
    assert "presunutých" not in body and "priradení" in body


# --- review 10: rows decided during a reuse are held; the group follows through ----------------


def test_a_wording_taught_while_codex_gave_our_code_to_another_card_is_held(pg):
    """Review 10 🟡 P2: card 27 left ROZOK, the pagáč took it; in the dry-run window the
    warehouse picked the pagáč at a question — #477 SELECTS our existing ROZOK — and the answer
    taught the pagáč wording onto ROZOK. No rename happened. The apply renumbers ROZOK → the
    rožok's ROZOK_NEW, but the row decided after CODEX gave ROZOK away stays (a human decides);
    the rožok's older rows move."""
    _baseline(pg)
    _teach(pg, "C39", "rozok slaninovy", ROZOK, at=_BEFORE)
    _push(pg, _reused(V1), hours_old=4.9)
    codex_sync.run(pg, _cfg(apply=False))
    picked = card_guard.add_from_codex(pg, "orders", ROZOK, actor="sklad")
    _teach(pg, "C40", "pagac syrovy", picked["gtin"], delivered=date.today())
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    assert _gtin_of(pg, "C39") == ROZOK_NEW
    assert _gtin_of(pg, "C40") == ROZOK
    reason = _review_reason(pg, "orders", ROZOK)
    assert "79" in reason and "Naučené" in reason and ROZOK_NEW in reason


def test_a_row_repointed_onto_our_number_during_the_reuse_is_held(pg):
    """Review 10 🟡 P2: an OLD row re-pointed onto ROZOK (a Naučené edit, audited) after CODEX
    gave ROZOK to the pagáč keeps its `created_at` — the audit says when it was decided."""
    _baseline(pg)
    _teach(pg, "C43", "pagac syrovy", CHLIEB, at=_BEFORE)
    _push(pg, _reused(V1), hours_old=4.9)
    codex_sync.run(pg, _cfg(apply=False))
    rid = pg.execute("SELECT id FROM item_memory WHERE customer_ean = 'C43'").fetchone()[0]
    pg.execute("UPDATE item_memory SET gtin = %s WHERE id = %s", (ROZOK, rid))
    audit.record(pg, actor="sklad", table="item_memory", row_id=rid, action="update",
                 before={"gtin": CHLIEB}, after={"gtin": ROZOK})
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    assert _gtin_of(pg, "C43") == ROZOK


def test_a_codex_rename_beside_a_same_named_duplicate_is_never_called_a_human_rename(pg):
    """Review 10 🔵 P1: our ROZOK is card 27; card 28 also carries ROZOK under our name; CODEX
    renames card 27. Nobody touched our card — the review may hold it, but never claims
    „niekto ju premenoval" nor asks to „vrátiť" a name it never had."""
    _baseline(pg)
    dup = V1 + [_row(ROZOK, "28", "Rožok so slaninou 70g")]
    _push(pg, dup, hours_old=4)
    codex_sync.run(pg, _cfg())
    renamed = [dict(r, name="Rožok slaninový 70g") if r["card_code"] == "27" else r for r in dup]
    _push(pg, renamed, hours_old=3)
    codex_sync.run(pg, _cfg())
    reason = _review_reason(pg, "orders", ROZOK)
    assert "premenoval" not in reason and "vráť" not in reason


def test_a_rename_rebind_lists_the_twins_rows_too(pg):
    """Review 10 🔵 P4: a rename rebind to another product lists EVERY number's rows for a
    human — the legacy twin's too."""
    _seed_catalogs(pg)
    snapshot.upsert_catalog_card(pg, "0" + ROZOK, "Rožok so slaninou 70g", alias="rozok twin")
    snapshot.rebuild_from_overrides(pg)
    _push(pg, V1, hours_old=6)
    codex_sync.run(pg, _cfg())
    _teach(pg, "C70", "rozok twin wording", "0" + ROZOK, at=_BEFORE)
    reused = [dict(r, code=ROZOK) if r["card_code"] == "79" else r
              for r in V1 if r["card_code"] != "27"]
    _push(pg, reused, hours_old=5)
    codex_sync.run(pg, _cfg())
    _drift_click(pg, ROZOK, "Pagáč syrový 60g")
    _push(pg, reused, hours_old=4)
    codex_sync.run(pg, _cfg())
    assert "Naučené" in _review_reason(pg, "orders", "0" + ROZOK)


def test_a_twin_joining_an_already_bound_group_is_bound(pg):
    """Review 10 🔵 P13: a legacy twin that comes back after the group was bound (a Kôš undo) is
    bound too — left alone later it follows card 27, never captured by the pagáč."""
    _baseline(pg)
    snapshot.upsert_catalog_card(pg, "0" + ROZOK, "Rožok so slaninou 70g")
    snapshot.rebuild_from_overrides(pg)
    _push(pg, V1, hours_old=4.5)
    codex_sync.run(pg, _cfg())
    assert _binding(pg, "0" + ROZOK) == ("27", True)


def test_rows_on_a_deleted_canonical_number_follow_the_twins_renumber(pg):
    """Review 10 🔵 P11: the warehouse deleted the canonical ROZOK (still card 27's — its binding
    is active) and kept the legacy twin; card 27 moves to ROZOK_NEW — the rožok's older row on
    ROZOK follows the twin's renumber (checked: only a number that IS card 27)."""
    _seed_catalogs(pg)
    snapshot.upsert_catalog_card(pg, "0" + ROZOK, "Rožok so slaninou 70g")
    snapshot.rebuild_from_overrides(pg)
    _push(pg, V1, hours_old=6)
    codex_sync.run(pg, _cfg())
    _teach(pg, "C80", "rozok x", ROZOK, at=_BEFORE)
    _kos_delete(pg)
    for hours in (5, 4):
        _push(pg, _reused(V1), hours_old=hours)
        codex_sync.run(pg, _cfg())
    assert _gtin_of(pg, "C80") == ROZOK_NEW


def test_a_renumber_onto_another_cards_number_names_the_way_out(pg):
    """Review 10 🔵 P3: „náš kód … je karta CODEX 31, nie 55" repeated on every push without a
    way out — it names the pick that settles it."""
    _baseline(pg)
    v2 = [dict(r, code=CHLIEB_NEW) if r["card_code"] == "31"
          else dict(r, code=CHLIEB) if r["card_code"] == "55" else r for r in V1]
    _push(pg, v2, hours_old=3)
    codex_sync.run(pg, _cfg())
    assert "Vybrať kartu z CODEXu" in _review_reason(pg, "orders", KOLAC)


def test_same_named_duplicate_carriers_name_the_way_out(pg):
    """Review 10 🔵 P12: two CODEX cards carry our code under the SAME name — renaming cannot
    tell them apart; the review names the pick."""
    _seed_catalogs(pg)
    dup = V1 + [_row(ROZOK, "28", "Rožok so slaninou 70g")]
    _push(pg, dup, hours_old=3)
    codex_sync.run(pg, _cfg())
    assert "Vybrať kartu z CODEXu" in _review_reason(pg, "orders", ROZOK)


def test_a_rename_rebind_never_calls_itself_a_pick_or_sends_rows_to_a_gone_card(pg):
    """Review 10 🔵: the rename-rebind reset is audited without „vybraný znova" (nothing was
    picked), and its rows review never asks to move them onto a card gone from CODEX."""
    _seed_catalogs(pg)
    _push(pg, V1, hours_old=6)
    codex_sync.run(pg, _cfg())
    _teach(pg, "C90", "rozok y", ROZOK, at=_BEFORE)
    reused = [dict(r, code=ROZOK) if r["card_code"] == "79" else r
              for r in V1 if r["card_code"] != "27"]
    _push(pg, reused, hours_old=5)
    codex_sync.run(pg, _cfg())
    _drift_click(pg, ROZOK, "Pagáč syrový 60g")
    _push(pg, reused, hours_old=4)
    codex_sync.run(pg, _cfg())
    notes = [n for (n,) in pg.execute("SELECT note FROM audit_log WHERE actor = 'codex-sync' "
                                      "AND action = 'update' AND table_name = "
                                      "'catalog_overrides'").fetchall()]
    assert notes and not any("vybraný znova" in n for n in notes)
    reason = _review_reason(pg, "orders", ROZOK)
    assert "Naučené" in reason and "preraď ich na jeho kartu" not in reason


# --- review 11: the reuse window opens when the other card APPEARS; the round trip ------------

def test_a_row_decided_before_the_other_card_appeared_on_our_code_follows_the_card(pg):
    """Review 11 🟡: the window opens when the pagáč was first SEEN on ROZOK (nobody could pick
    it before a push listed it), never when card 27 was last seen there — a row decided in
    between is card 27's and moves with it."""
    _baseline(pg)                                   # card 27 on ROZOK, as of 5 h ago
    _teach(pg, "C45", "rozok slaninovy", ROZOK, at=NOW - timedelta(hours=4.5))
    _push(pg, _reused(V1), hours_old=4)             # the pagáč first seen on ROZOK
    codex_sync.run(pg, _cfg())
    assert _gtin_of(pg, "C45") == ROZOK_NEW


def test_an_order_for_a_later_delivery_is_not_held_by_its_delivery_date(pg):
    """Review 11 🟡: item_memory.delivered_on is the order's REQUESTED delivery day (often
    ahead) — an order taught long before the reuse for a later delivery is card 27's."""
    _baseline(pg)
    _teach(pg, "C46", "rozok slaninovy", ROZOK, at=_BEFORE, delivered=date.today()
           + timedelta(days=2))
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    assert _gtin_of(pg, "C46") == ROZOK_NEW


def test_held_delivery_history_stays_without_sending_anyone_to_naucene(pg):
    """Review 11 🟡: a SHIPPED row decided during the reuse is delivery history — it stays on
    the old number, and the review never tells the warehouse to fix it in Naučené (it is not
    listed there); only taught rows are theirs to check."""
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=4.9)
    codex_sync.run(pg, _cfg(apply=False))
    pg.execute("INSERT INTO item_memory (customer_ean, item_key, item_raw, gtin, card, "
               "delivered_on, source) VALUES ('C47', 'pagac', 'pagáč', %s, 'x', %s, 'ship')",
               (ROZOK, date.today()))
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    assert _gtin_of(pg, "C47") == ROZOK
    assert "Naučené" not in _review_reason(pg, "orders", ROZOK)
    # review 12: not trivially — the report and the ops message say the history stayed
    held = [r["held"] for r in _last_report(pg)["renumbers"] if r["scope"] == "orders"]
    assert held == [{"taught": 0, "shipped": 1}]
    body = pg.execute("SELECT body_html FROM pending_alerts ORDER BY id DESC LIMIT 1"
                      ).fetchone()[0]
    assert "ostalo pod" in body


def test_a_row_held_during_a_reuse_is_flagged_when_the_card_comes_back_to_the_code(pg):
    """Review 11 🟡: the held pagáč wording on ROZOK, then card 27 goes BACK to ROZOK and the
    pagáč moves on — the restore adopts the rows sitting on ROZOK; that must not happen
    silently (the #478 incident itself was such a round trip)."""
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=4.9)
    codex_sync.run(pg, _cfg(apply=False))
    picked = card_guard.add_from_codex(pg, "orders", ROZOK, actor="sklad")
    _teach(pg, "C48", "pagac syrovy", picked["gtin"], delivered=date.today())
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    assert _gtin_of(pg, "C48") == ROZOK
    _push(pg, _reused(V1, to=ROZOK, pagac_code=PAGAC_W), hours_old=3)
    codex_sync.run(pg, _cfg())
    reason = _review_reason(pg, "orders", ROZOK_NEW)
    assert "79" in reason and "Naučené" in reason


def test_a_row_without_created_at_is_planned_and_moved_alike(pg):
    """Review 11 🔵: a NULL `created_at` (the column allows it) is an old row — moved, and the
    dry-run count equals what the apply really moves."""
    _baseline(pg)
    pg.execute("INSERT INTO item_memory (customer_ean, item_key, item_raw, gtin, card, "
               "delivered_on, source, created_at) VALUES ('C49', 'rozok', 'rožok', %s, 'x', "
               "%s, 'human', NULL)", (ROZOK, date(2026, 9, 1)))
    _push(pg, _reused(V1), hours_old=4.9)
    codex_sync.run(pg, _cfg(apply=False))
    planned = pg.execute("SELECT report FROM codex_sync_runs ORDER BY id DESC LIMIT 1"
                         ).fetchone()[0]
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    applied = pg.execute("SELECT report FROM codex_sync_runs ORDER BY id DESC LIMIT 1"
                         ).fetchone()[0]
    assert _gtin_of(pg, "C49") == ROZOK_NEW

    def rows(report):
        return [r["memory"] for r in report["renumbers"] if r["scope"] == "orders"]
    assert rows(planned) == rows(applied) == [{"item_memory": 1, "global_item_memory": 0}]


# --- review 12: the history of every accepted push; "taught" = what the matcher trusts --------

def _last_report(pg):
    return pg.execute("SELECT report FROM codex_sync_runs ORDER BY id DESC LIMIT 1").fetchone()[0]


def test_a_push_whose_sync_failed_still_opens_the_reuse_window(pg, monkeypatch):
    """Review 12 🟡: the pushed list is live (pickable) the moment it is accepted — even when
    its sync fails. The pagáč's first sighting on ROZOK is that push's, so a pagáč wording
    taught right after it is held, never moved with card 27."""
    _baseline(pg)

    def boom(conn, cx):
        raise RuntimeError("boom")

    monkeypatch.setattr(codex_sync.sp, "build_plan", boom)
    _push(pg, _reused(V1), hours_old=4.9)
    assert codex_sync.run_safely(pg, _cfg())["mode"] == "error"
    monkeypatch.undo()
    _teach(pg, "C60", "pagac syrovy", ROZOK, at=NOW - timedelta(hours=4.5))
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    assert _gtin_of(pg, "C60") == ROZOK


def test_a_doucit_row_decided_during_a_reuse_is_held_and_reviewed(pg):
    """Review 12 🟡: a História „Doučiť" row (`source='teachback'`) is a taught mapping for the
    matcher — held during a reuse AND reviewed like one (with its own way out), never counted
    as delivery history."""
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=4.9)
    codex_sync.run(pg, _cfg(apply=False))
    pg.execute("INSERT INTO item_memory (customer_ean, item_key, item_raw, gtin, card, "
               "delivered_on, source) VALUES ('C61', 'pagac syrovy', 'pagáč', %s, 'x', %s, "
               "'teachback')", (ROZOK, date.today()))
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    assert _gtin_of(pg, "C61") == ROZOK
    reason = _review_reason(pg, "orders", ROZOK)
    assert "1 naučen" in reason and "Koš" in reason
    held = [r["held"] for r in _last_report(pg)["renumbers"] if r["scope"] == "orders"]
    assert held == [{"taught": 1, "shipped": 0}]


def test_a_row_without_source_is_planned_and_held_alike(pg):
    """Review 12 🔵: a NULL `source` counts as delivery history — the dry-run plan and the
    apply agree on it."""
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=4.9)
    codex_sync.run(pg, _cfg(apply=False))
    pg.execute("INSERT INTO item_memory (customer_ean, item_key, item_raw, gtin, card, "
               "delivered_on, source) VALUES ('C62', 'pagac', 'pagáč', %s, 'x', %s, NULL)",
               (ROZOK, date.today()))
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg(apply=False))
    planned = [(r["memory"], r["held"]) for r in _last_report(pg)["renumbers"]
               if r["scope"] == "orders"]
    _push(pg, _reused(V1), hours_old=3)
    codex_sync.run(pg, _cfg())
    applied = [(r["memory"], r["held"]) for r in _last_report(pg)["renumbers"]
               if r["scope"] == "orders"]
    assert planned == applied == [({"item_memory": 0, "global_item_memory": 0},
                                   {"taught": 0, "shipped": 1})]
    assert _gtin_of(pg, "C62") == ROZOK


def test_a_repick_review_never_sends_delivery_history_to_naucene(pg):
    """Review 12 🔵: rows older than a re-pick that are SHIPPED history are not listed in
    Naučené — the re-pick review never asks to fix them there."""
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    pg.execute("INSERT INTO item_memory (customer_ean, item_key, item_raw, gtin, card, "
               "delivered_on, source) VALUES ('C63', 'rozok', 'rožok', %s, 'x', %s, 'ship')",
               (ROZOK, date(2026, 9, 1)))
    card_guard.add_from_codex(pg, "orders", ROZOK, actor="sklad")
    _push(pg, _reused(V1), hours_old=3)
    codex_sync.run(pg, _cfg())
    assert "Naučené" not in _review_reason(pg, "orders", ROZOK)


# --- review 13: an older list never regresses a name; holds said once, where they are ----------

def _history_name(pg, card, code):
    return pg.execute("SELECT name FROM codex_card_history WHERE stredisko = 1 AND "
                      "card_code = %s AND code = %s", (card, code)).fetchone()[0]


def test_an_older_repush_never_rewrites_the_history_name(pg):
    """Review 13 🟡: every accepted push's history is recorded (round 12) — an OLDER re-sent
    list must never set a card's name back (last_seen does not advance, so the name must not
    either): the recreated SAME product keeps its alias / doplnok."""
    renamed = [dict(r, name="Rožok slaninový 70g") if r["card_code"] == "27" else r for r in V1]
    recreated = ([r for r in V1 if r["card_code"] != "27"]
                 + [_row(ROZOK, "28", "Rožok slaninový 70g")])
    _seed_catalogs(pg)
    _push(pg, V1, hours_old=6)
    codex_sync.run(pg, _cfg())
    _push(pg, renamed, hours_old=5)
    codex_sync.run(pg, _cfg())
    _push(pg, V1, hours_old=6)                       # the OLD list re-sent
    assert codex_sync.run(pg, _cfg())["mode"] == "skipped"
    assert _history_name(pg, "27", ROZOK) == "Rožok slaninový 70g"
    for hours in (4, 3):                             # card 27 recreated as 28 → rename rebind
        _push(pg, recreated, hours_old=hours)
        codex_sync.run(pg, _cfg())
    assert _orders(pg)[ROZOK]["alias"] == "rozok slanina"
    assert _dl(pg)[ROZOK]["doplnok"] == "rožok slanina"


def test_an_older_list_skip_never_claims_a_newer_list_was_synced(pg, caplog):
    """Review 13 🔵: the newer list may only have been RECORDED (its sync failed / skipped) —
    the skip says so."""
    _baseline(pg)
    _push(pg, V1, hours_old=7)
    with caplog.at_level(logging.WARNING, logger="orders.codex_sync"):
        assert codex_sync.run(pg, _cfg())["mode"] == "skipped"
    assert any("already recorded" in r.getMessage() for r in caplog.records)


def test_a_repick_hold_note_says_where_the_rows_went(pg):
    """Review 13 🔵: the picked pagáč moves ROZOK → W in the same push — its older delivery
    history moved with it; the note never says it „stayed" under ROZOK."""
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    pg.execute("INSERT INTO item_memory (customer_ean, item_key, item_raw, gtin, card, "
               "delivered_on, source) VALUES ('C80', 'rozok', 'rožok', %s, 'x', %s, 'ship')",
               (ROZOK, date(2026, 9, 1)))
    card_guard.add_from_codex(pg, "orders", ROZOK, actor="sklad")
    _push(pg, _reused(V1, pagac_code=PAGAC_W), hours_old=3)
    codex_sync.run(pg, _cfg())
    assert _gtin_of(pg, "C80") == PAGAC_W
    body = pg.execute("SELECT body_html FROM pending_alerts ORDER BY id DESC LIMIT 1"
                      ).fetchone()[0]
    assert f"ostalo pod {ROZOK}" not in body
    assert all(h.get("at") == PAGAC_W for h in _last_report(pg)["holds"]
               if h["scope"] == "orders")


def test_held_delivery_history_is_alerted_once(pg):
    """Review 13 🔵: the renumber's line already said the history stayed — the next push's
    retired-number note about the same rows is not a second alert."""
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=4.9)
    codex_sync.run(pg, _cfg(apply=False))
    pg.execute("INSERT INTO item_memory (customer_ean, item_key, item_raw, gtin, card, "
               "delivered_on, source) VALUES ('C90', 'pagac', 'pagáč', %s, 'x', %s, 'ship')",
               (ROZOK, date.today()))
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    before = pg.execute("SELECT count(*) FROM pending_alerts").fetchone()[0]
    _push(pg, _reused(V1), hours_old=3)
    codex_sync.run(pg, _cfg())
    assert pg.execute("SELECT count(*) FROM pending_alerts").fetchone()[0] == before


def test_a_contest_review_names_the_doucit_way_out(pg):
    """Review 13 🔵: every review that sends the warehouse to its taught rows says where a
    História „Doučiť" row is fixed (the Kôš) — the contest texts too."""
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    _restore_and_rename(pg)
    _push(pg, _reused(V1), hours_old=3)
    codex_sync.run(pg, _cfg())
    assert "Doučiť" in _review_reason(pg, "orders", ROZOK)


# --- review 14: every line says what really happened ------------------------------------------

def test_a_repick_hold_note_never_moves_rows_the_renumber_kept(pg):
    """Review 14 🔵: 79 is picked for ROZOK while card 80 ALSO carries ROZOK; next push 79 moves
    ROZOK → W and 80 stays — the renumber keeps the rows decided since 80 appeared (reuse
    window). The history row stays on ROZOK; nothing may claim it „je teraz pod" W."""
    _baseline(pg)
    both = _reused(V1) + [_row(ROZOK, "80", "Zemiaková placka 90g")]
    _push(pg, both, hours_old=4)
    codex_sync.run(pg, _cfg())
    pg.execute("INSERT INTO item_memory (customer_ean, item_key, item_raw, gtin, card, "
               "delivered_on, source) VALUES ('C81', 'rozok', 'rožok', %s, 'x', %s, 'ship')",
               (ROZOK, date(2026, 9, 25)))
    card_guard.add_from_codex(pg, "orders", ROZOK, actor="sklad")
    moved = [dict(r, code=PAGAC_W) if r["card_code"] == "79" else r for r in both]
    _push(pg, moved, hours_old=3)
    codex_sync.run(pg, _cfg())
    assert _gtin_of(pg, "C81") == ROZOK
    body = pg.execute("SELECT body_html FROM pending_alerts ORDER BY id DESC LIMIT 1"
                      ).fetchone()[0]
    assert f"je teraz pod {PAGAC_W}" not in body
    assert all(h.get("at") == _gtin_of(pg, "C81") for h in _last_report(pg)["holds"]
               if h["scope"] == "orders" and h["gtin"] == ROZOK)


def test_a_card_gone_with_its_code_only_elsewhere_never_promises_a_binding(pg):
    """Review 14 🔵: card 27 left stredisko 1 and ROZOK lives on only on another stredisko —
    no rename can bind it (nothing there is pickable); the review says so."""
    _baseline(pg)
    v = [r for r in V1 if r["card_code"] != "27"] + [
        _row(ROZOK, "400", "Rožok cestovný 70g", stredisko=4)]
    for hours in (4, 3):
        _push(pg, v, hours_old=hours)
        codex_sync.run(pg, _cfg())
    reason = _review_reason(pg, "orders", ROZOK)
    assert reason and "priradí" not in reason and "Kôš" in reason


def test_a_card_gone_with_its_code_on_two_cards_names_the_pick(pg):
    """Review 14 🔵: two stredisko-1 cards carry ROZOK now — a rename cannot pick one; the
    review names the pick."""
    _baseline(pg)
    v = [r for r in V1 if r["card_code"] != "27"] + [
        _row(ROZOK, "80", "Rožok cestovný 70g"), _row(ROZOK, "81", "Bageta 70g")]
    for hours in (4, 3):
        _push(pg, v, hours_old=hours)
        codex_sync.run(pg, _cfg())
    assert "Vybrať kartu z CODEXu" in _review_reason(pg, "orders", ROZOK)


def test_a_renumber_line_names_the_codex_cards_product(pg):
    """Review 14 🔵: the picked pagáč (restored „as it was", still named like the rožok until
    the rename) renumbers — the line names card 79's CODEX product, not our old name."""
    _repick_reused(pg)
    _push(pg, _reused(V1, pagac_code=PAGAC_W), hours_old=3)
    codex_sync.run(pg, _cfg())
    body = pg.execute("SELECT body_html FROM pending_alerts ORDER BY id DESC LIMIT 1"
                      ).fetchone()[0]
    assert "karta CODEX 79 „Pagáč syrový 60g“ zmenila kód" in body


# --- review 15: the texts say what a pick / rename WILL do, derived from the same rules --------

def _pick_card(pg, code=ROZOK, scope="orders"):
    return str(card_guard.pickable(pg, scope)[code]["card_code"])


def test_a_gone_card_review_names_the_one_card_the_pick_binds(pg):
    """Review 15 🔵: the picker offers ONE card per code — with two carriers the review names
    that card and says the other one cannot be picked."""
    _baseline(pg)
    v = [r for r in V1 if r["card_code"] != "27"] + [
        _row(ROZOK, "80", "Rožok cestovný 70g"), _row(ROZOK, "81", "Bageta 70g")]
    for hours in (4, 3):
        _push(pg, v, hours_old=hours)
        codex_sync.run(pg, _cfg())
    reason = _review_reason(pg, "orders", ROZOK)
    offered = _pick_card(pg)
    other = ({"80", "81"} - {offered}).pop()
    assert f"karty CODEX {offered}" in reason and "priradiť nevie" in reason
    assert other in reason.split("priradiť nevie")[0].rsplit(";", 1)[-1]


def test_a_contest_review_never_advises_a_pick_that_binds_another_card(pg):
    """Review 15 🔵: CODEX renamed card 27 beside a same-named duplicate 80 — the contest review
    must not advise picking card 80: the picker offers card 27 for this code."""
    _baseline(pg)
    v = [dict(r, name="Rožok so slaninou a syrom 70g", changed_at="2026-09-29T10:00:00+02:00")
         if r["card_code"] == "27" else r for r in V1] + [_row(ROZOK, "80", "Rožok so slaninou 70g")]
    _push(pg, v, hours_old=4)
    codex_sync.run(pg, _cfg())
    reason = _review_reason(pg, "orders", ROZOK)
    assert _pick_card(pg) == "27"
    assert "Ak je to výrobok karty CODEX 80" not in reason and "priradiť nevie" in reason


def test_a_rename_advice_says_the_same_products_data_stays(pg):
    """Review 15 🔵: card 27 recreated as 127 under 27's OWN name while our name had drifted —
    renaming to it keeps the data (same product); the review must not promise a reset."""
    _baseline(pg)
    snapshot.upsert_catalog_card(pg, ROZOK, "Rožok XXL tmavý 90g")
    snapshot.rebuild_from_overrides(pg)
    v = [dict(r, card_code="127") if r["card_code"] == "27" else r for r in V1]
    for hours in (4, 3):
        _push(pg, v, hours_old=hours)
        codex_sync.run(pg, _cfg())
    reason = _review_reason(pg, "orders", ROZOK)
    assert "ostanú" in reason and "vyčistia" not in reason


def test_a_held_taught_row_older_than_a_repick_still_points_at_the_old_product(pg):
    """Review 15 🔵: a frozen rožok answer on the retired ROZOK, older than the pick of 79 and
    inside card 80's reuse window — some review still names the rožok's number as its home."""
    _baseline(pg)
    both = _reused(V1) + [_row(ROZOK, "80", "Zemiaková placka 90g")]
    _push(pg, both, hours_old=4)
    codex_sync.run(pg, _cfg())
    _teach(pg, "C83", "rozok slanina frozen", ROZOK)
    card_guard.add_from_codex(pg, "orders", ROZOK, actor="sklad")
    moved = [dict(r, code=PAGAC_W) if r["card_code"] == "79" else r for r in both]
    _push(pg, moved, hours_old=3)
    codex_sync.run(pg, _cfg())
    assert _gtin_of(pg, "C83") == ROZOK
    assert ROZOK_NEW in _review_reason(pg, "orders", ROZOK)


def test_held_rows_on_a_retired_twin_are_named_where_they_sit(pg):
    """Review 15 🔵: the late rožok row sits on the legacy twin „0"+ROZOK — the held review
    names that number, and no review line shows an empty „“ name."""
    _seed_catalogs(pg)
    snapshot.upsert_catalog_card(pg, "0" + ROZOK, "Rožok so slaninou 70g")
    snapshot.rebuild_from_overrides(pg)
    _push(pg, V1, hours_old=6)
    codex_sync.run(pg, _cfg())
    _push(pg, _reused(V1), hours_old=5)
    codex_sync.run(pg, _cfg())
    card_guard.add_from_codex(pg, "orders", ROZOK, actor="sklad")
    _teach(pg, "C8", "pagac syrovy", ROZOK)
    _kos_delete(pg)
    _teach(pg, "C9", "rozok neskoro", "0" + ROZOK)
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    assert _gtin_of(pg, "C9") == "0" + ROZOK
    held = [r for r in _review_reason(pg, "orders", ROZOK).split(" Tiež: ") if "vzniklo" in r]
    assert held and "0" + ROZOK in held[0]
    body = pg.execute("SELECT body_html FROM pending_alerts ORDER BY id DESC LIMIT 1"
                      ).fetchone()[0]
    assert "„“" not in body


def test_held_rows_on_a_live_twin_are_named_where_they_sit(pg):
    """Review 15 🔵: a row taught onto the live legacy twin after the pagáč appeared is held by
    the renumber — the review and the ops line name the twin, not the canonical number."""
    _seed_catalogs(pg)
    snapshot.upsert_catalog_card(pg, "0" + ROZOK, "Rožok so slaninou 70g")
    snapshot.rebuild_from_overrides(pg)
    _push(pg, V1, hours_old=6)
    codex_sync.run(pg, _cfg())
    _push(pg, _reused(V1), hours_old=5)
    codex_sync.run(pg, _cfg(apply=False))
    _teach(pg, "C10", "rozok twin neskoro", "0" + ROZOK)
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    assert _gtin_of(pg, "C10") == "0" + ROZOK
    assert "0" + ROZOK in _review_reason(pg, "orders", ROZOK)
    body = pg.execute("SELECT body_html FROM pending_alerts ORDER BY id DESC LIMIT 1"
                      ).fetchone()[0]
    assert f"pod 0{ROZOK}" in body


def test_the_ops_footer_never_promises_a_reset_is_redone(pg):
    """Review 15 🔵: a Kôš undo of a reset is NOT redone by the next list — the footer says only
    what is (renames / renumbers), and the reset line speaks Slovak field names."""
    _repick_reused(pg)
    _push(pg, _reused(V1), hours_old=3)
    assert codex_sync.run(pg, _cfg())["reset"] == 2
    body = pg.execute("SELECT body_html FROM pending_alerts ORDER BY id DESC LIMIT 1"
                      ).fetchone()[0]
    assert "ju urobí znova" not in body
    assert "mass" not in body and "hmotnosť" in body


# --- review 16: the remaining prose claims, derived too --------------------------------------

def test_a_repick_review_never_advises_picking_a_card_the_picker_cannot_offer(pg):
    """Review 16 🔵: card 27 moved to a sklad-100-only code (the orders picker offers sklad 1),
    the pagáč took ROZOK and was picked — the rows review never tells the warehouse to pick
    card 27 at a question: the picker cannot offer it."""
    _baseline(pg)
    _seed_memory(pg)
    moved = [dict(r, code=ROZOK_NEW, sklad=100) if r["card_code"] == "27"
             else dict(r, code=ROZOK) if r["card_code"] == "79" else r for r in V1]
    _push(pg, moved, hours_old=4)
    _kos_delete(pg)
    card_guard.add_from_codex(pg, "orders", ROZOK, actor="sklad")
    codex_sync.run(pg, _cfg())
    reason = _review_reason(pg, "orders", ROZOK)
    assert "u nás karta nie je — ak treba, vyber" not in reason
    assert "neponúka" in reason


def test_a_pick_advice_names_every_number_of_ours_to_delete(pg):
    """Review 16 🔵: a live legacy twin „0"+ROZOK sits next to our ROZOK — the picker would
    SELECT the twin and write nothing, so the advice says to delete both numbers."""
    _seed_catalogs(pg)
    snapshot.upsert_catalog_card(pg, "0" + ROZOK, "Rožok so slaninou 70g")
    snapshot.rebuild_from_overrides(pg)
    _push(pg, V1, hours_old=6)
    codex_sync.run(pg, _cfg())
    v = [r for r in V1 if r["card_code"] != "27"] + [
        _row(ROZOK, "80", "Rožok cestovný 70g"), _row(ROZOK, "81", "Zemiaková placka 90g")]
    for hours in (5, 4):
        _push(pg, v, hours_old=hours)
        codex_sync.run(pg, _cfg())
    reason = _review_reason(pg, "orders", ROZOK)
    assert "Vybrať kartu z CODEXu" in reason and f"0{ROZOK}" in reason


def test_the_ops_footer_says_a_removal_undone_is_redone(pg):
    """Review 16 🔵: a Kôš undo of a removal IS redone by the next list — the footer names it."""
    _baseline(pg)
    gone = [r for r in V1 if r["card_code"] != "55"]
    for hours in (4, 3):
        _push(pg, gone, hours_old=hours)
        codex_sync.run(pg, _cfg())
    body = pg.execute("SELECT body_html FROM pending_alerts ORDER BY id DESC LIMIT 1"
                      ).fetchone()[0]
    assert "presun do Koša" in body.split("<p>Každá")[-1]


def test_a_review_never_quotes_an_empty_codex_name(pg):
    """Review 16 🔵: card 80's sklad-1 row has a BLANK name (never pickable, the picker offers its
    named row) — no review quotes „“."""
    _baseline(pg)
    v = [r for r in V1 if r["card_code"] != "27"] + [
        _row(ROZOK, "80", ""), _row(ROZOK, "80", "Rožok cestovný 70g", sklad=100),
        _row(ROZOK, "81", "Zemiaková placka 90g", sklad=100)]
    for hours in (4, 3):
        _push(pg, v, hours_old=hours)
        codex_sync.run(pg, _cfg())
    reason = _review_reason(pg, "dl", ROZOK)
    body = pg.execute("SELECT body_html FROM pending_alerts ORDER BY id DESC LIMIT 1"
                      ).fetchone()[0]
    assert reason and "„“" not in reason and "„“" not in body


def test_a_contest_never_says_no_card_carries_the_code_our_card_carries(pg):
    """Review 16 🔵: the round trip — card 27 back on ROZOK, our restored retired ROZOK renamed
    by a human: the contest review never says no card carries ROZOK (card 27 does)."""
    _baseline(pg)
    _push(pg, _v2_renumbered(), hours_old=4)
    codex_sync.run(pg, _cfg())
    _restore_sync_delete(pg, "catalog_overrides", ROZOK)
    snapshot.upsert_catalog_card(pg, ROZOK, "Rožok XXL tmavý 90g")
    snapshot.rebuild_from_overrides(pg)
    _push(pg, V1, hours_old=3)
    codex_sync.run(pg, _cfg())
    reason = _review_reason(pg, "orders", ROZOK)
    assert reason and "nenesie žiadna karta" not in reason


def test_held_delivery_history_on_a_retired_number_is_reported(pg):
    """Review 12 🔵: on the retired-number path a held row that is only delivery history makes
    no move and no review — the report and the ops message still say it stayed."""
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=4.9)
    codex_sync.run(pg, _cfg())                          # ROZOK retired, the pagáč took it
    pg.execute("INSERT INTO item_memory (customer_ean, item_key, item_raw, gtin, card, "
               "delivered_on, source) VALUES ('C64', 'pagac', 'pagáč', %s, 'x', %s, 'ship')",
               (ROZOK, date.today()))
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    assert _gtin_of(pg, "C64") == ROZOK
    holds = [h for h in _last_report(pg)["holds"] if h["scope"] == "orders"]
    assert holds and holds[0]["held"] == {"taught": 0, "shipped": 1}
    body = pg.execute("SELECT body_html FROM pending_alerts ORDER BY id DESC LIMIT 1"
                      ).fetchone()[0]
    assert "ostalo pod" in body


def test_a_create_onto_a_hidden_override_row_undeletes_it(pg):
    """Review 5 🔵: the executor's guard — a renumber's `create` onto a number whose override
    row is soft-deleted brings the card back visible (an upsert alone keeps `deleted_at`)."""
    _seed_catalogs(pg)
    hidden = "9990000000161"
    snapshot.upsert_catalog_card(pg, hidden, "Starý záznam")
    snapshot.retire_catalog_card(pg, hidden)
    snapshot.rebuild_from_overrides(pg)
    with pg.transaction():
        codex_sync._apply_renumber(pg, {
            "scope": "orders", "codex_card": "27", "code": ROZOK, "from": ROZOK,
            "to": hidden, "mode": "create", "gtins": [ROZOK], "old_gtins": [ROZOK],
            "card": {"gtin": ROZOK, "name": "Rožok so slaninou 70g",
                     "alias": "rozok slanina"}})
    snapshot.rebuild_from_overrides(pg)
    assert _orders(pg)[hidden]["name"] == "Rožok so slaninou 70g"


def test_a_code_moved_to_another_stredisko_is_reviewed_once(pg):
    """Review 🔵: one card, one review entry — never two with different reasons."""
    _baseline(pg)
    v2 = [dict(r, stredisko=4, name="Koláč tvarohový 90g") if r["code"] == KOLAC else r
          for r in V1]
    _push(pg, v2, hours_old=2)
    codex_sync.run(pg, _cfg())
    _push(pg, v2, hours_old=1)
    codex_sync.run(pg, _cfg())
    report = pg.execute("SELECT report FROM codex_sync_runs ORDER BY id DESC LIMIT 1"
                        ).fetchone()[0]
    assert [(r["scope"], r["gtin"]) for r in report["review"]] == [
        ("orders", KOLAC), ("dl", KOLAC)]
    assert _orders(pg)[KOLAC]["name"] == "Koláč makový 80g"


def test_a_review_item_is_alerted_once_not_on_every_run(pg):
    _baseline(pg)
    v2 = _v2_renumbered() + [_row("9990000000109", "27", "Rožok so slaninou 70g")]
    _push(pg, v2, hours_old=3)
    codex_sync.run(pg, _cfg())
    _push(pg, v2, hours_old=1)
    codex_sync.run(pg, _cfg())
    assert pg.execute("SELECT count(*) FROM pending_alerts").fetchone()[0] == 1


# --- legacy twin, memory edge cases (review 🟡) ------------------------------------------------

def test_a_renumber_carries_the_canonical_cards_data_not_a_legacy_twins(pg):
    _seed_catalogs(pg)
    dl_snapshot.upsert_dl_catalog_card(pg, "0" + ROZOK, "Rožok so slaninou 70g",
                                       doplnok="stary", mass=0.5, sklad="1", cena=9.99)
    dl_snapshot.dl_rebuild_from_overrides(pg)
    _push(pg, V1, hours_old=5)
    codex_sync.run(pg, _cfg())
    _push(pg, _v2_renumbered(), hours_old=1)
    codex_sync.run(pg, _cfg())
    dl = _dl(pg)
    new = dl[ROZOK_NEW]
    assert (new["doplnok"], new["mass"], new["cena"]) == ("rožok slanina", 0.07, 0.35)
    assert ROZOK not in dl and "0" + ROZOK not in dl, "both old numbers go to the Kôš"


def test_a_memory_row_written_after_a_renumber_follows_on_the_next_push(pg):
    """A frozen question candidate / held order answered after the renumber writes the OLD
    code — the next sync moves it to the card's new code."""
    _baseline(pg)
    _push(pg, _v2_renumbered(), hours_old=3)
    codex_sync.run(pg, _cfg())
    pg.execute("INSERT INTO item_memory (customer_ean, item_key, item_raw, gtin, card, "
               "delivered_on, source) VALUES ('C9', 'rozok', 'rožok', %s, 'Rožok', %s, "
               "'human')", (ROZOK, date(2026, 9, 29)))
    _push(pg, _v2_renumbered(), hours_old=1)
    res = codex_sync.run(pg, _cfg())
    assert res["memory_renumbered"] == 1
    assert _gtins(pg, "item_memory") == [ROZOK_NEW]


def test_a_round_trip_with_a_duplicate_mapping_keeps_one_live_row(pg):
    """Review 🟡: A(C1,k,X,d) + B(C1,k,Y,d). X→Y soft-deletes A (B has the mapping); Y→X
    must revive A, never soft-delete B against an already soft-deleted A."""
    _baseline(pg)
    for g in (ROZOK, ROZOK_NEW):
        pg.execute("INSERT INTO item_memory (customer_ean, item_key, item_raw, gtin, card, "
                   "delivered_on, source) VALUES ('C1', 'rozok slanina', 'r', %s, 'R', %s, "
                   "'human')", (g, date(2026, 9, 1)))
    _push(pg, _v2_renumbered(), hours_old=3)
    codex_sync.run(pg, _cfg())
    _push(pg, V1, hours_old=1)
    codex_sync.run(pg, _cfg())
    assert _gtins(pg, "item_memory") == [ROZOK]


def test_a_kos_restore_that_would_duplicate_a_mapping_is_a_clean_409(pg):
    _baseline(pg)
    _seed_memory(pg)
    _push(pg, _v2_renumbered(), hours_old=1)
    codex_sync.run(pg, _cfg())
    aid, rid = pg.execute("SELECT id, row_id FROM audit_log WHERE table_name = 'item_memory' "
                          "AND action = 'update' ORDER BY id LIMIT 1").fetchone()
    key = pg.execute("SELECT customer_ean, item_key, delivered_on FROM item_memory "
                     "WHERE id = %s", (int(rid),)).fetchone()
    pg.execute("INSERT INTO item_memory (customer_ean, item_key, item_raw, gtin, card, "
               "delivered_on, source) VALUES (%s, %s, 'x', %s, 'Rožok', %s, 'ship')",
               (key[0], key[1], ROZOK, key[2]))
    try:
        audit.restore(pg, aid)
    except audit.RestoreError as e:
        assert e.status == 409
    else:
        raise AssertionError("the duplicate restore must be refused")
    assert pg.execute("SELECT 1").fetchone() == (1,), "the connection stays usable"


def test_a_failing_sync_rolls_back_whole_and_never_fails_the_push(pg, monkeypatch):
    _baseline(pg)
    _seed_memory(pg)
    _push(pg, _v2_renumbered(), hours_old=1)
    history = pg.execute("SELECT count(*) FROM codex_card_history").fetchone()[0]
    real, calls = codex_sync._rewrite_memory, []

    def boom(conn, table, old, new, note, hold=None):
        calls.append(table)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return real(conn, table, old, new, note, hold=hold)

    monkeypatch.setattr(codex_sync, "_rewrite_memory", boom)
    res = codex_sync.run_safely(pg, _cfg())
    assert res["mode"] == "error" and "boom" in res["error"]
    # the failure really happened MID-rewrite: the first table was rewritten, then rolled back
    assert calls == ["item_memory", "global_item_memory"]
    assert ROZOK in _orders(pg) and ROZOK_NEW not in _orders(pg)
    assert set(_gtins(pg, "item_memory")) == {ROZOK}
    assert pg.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 0
    # review 12 (contract changed): the pushed list was live the moment it was accepted — its
    # HISTORY is kept even though the sync failed (card 27's new code is recorded)
    assert pg.execute("SELECT count(*) FROM codex_card_history").fetchone()[0] == history + 1
    assert pg.execute("SELECT count(*) FROM codex_card_history WHERE card_code = '27' AND "
                      "code = %s", (ROZOK_NEW,)).fetchone()[0] == 1
    assert pg.execute("SELECT status FROM codex_sync_runs ORDER BY id DESC LIMIT 1"
                      ).fetchone()[0] == "error"
    # review 2 🔵: a failing sync is not only a log line — ops hears about it once
    alerts = pg.execute("SELECT kind, body_html FROM pending_alerts").fetchall()
    assert len(alerts) == 1 and alerts[0][0] == codex_sync.ALERT_KIND
    assert "zlyhala" in alerts[0][1]


# --- history + the migration seed ------------------------------------------------------------

def test_the_history_keeps_first_seen_and_advances_last_seen(pg):
    _baseline(pg)
    first = pg.execute("SELECT first_seen, last_seen FROM codex_card_history "
                       "WHERE card_code = '27' AND code = %s", (ROZOK,)).fetchone()
    _push(pg, V1, hours_old=1)
    codex_sync.run(pg, _cfg())
    again = pg.execute("SELECT first_seen, last_seen FROM codex_card_history "
                       "WHERE card_code = '27' AND code = %s", (ROZOK,)).fetchone()
    assert again[0] == first[0] and again[1] > first[1]


def test_the_migration_seeds_the_history_from_the_stored_list(pg):
    _push(pg, V1, hours_old=5)
    rev = next(r.revision for r in db.REVISIONS if r.name == "add_codex_card_history")
    pg.execute("DROP TABLE codex_card_history")
    pg.execute("DROP TABLE codex_sync_runs")
    pg.execute("DELETE FROM schema_version WHERE revision = %s", (rev,))
    db.init_schema(pg)
    n = pg.execute("SELECT count(*) FROM codex_card_history").fetchone()[0]
    assert n == len({(r["stredisko"], r["card_code"], r["code"]) for r in V1})
    seen = pg.execute("SELECT DISTINCT first_seen FROM codex_card_history").fetchall()
    assert len(seen) == 1
    assert abs((seen[0][0] - (NOW - timedelta(hours=5))).total_seconds()) < 2
    # review 34: the seed IS the beginning — every seeded pair is seen since it
    assert pg.execute("SELECT count(*) FROM codex_card_history "
                      "WHERE seen_since IS DISTINCT FROM first_seen").fetchone()[0] == 0


def test_the_migration_seeded_history_identifies_our_numbers(pg):
    """Review 34: who our number IS reads `seen_since` — a migration seed without it would
    leave every number "never touched" after the deploy. A drifted card on the first
    post-deploy list follows its seed card."""
    _seed_catalogs(pg)
    _push(pg, V1, hours_old=6)
    rev = next(r.revision for r in db.REVISIONS if r.name == "add_codex_card_history")
    pg.execute("DROP TABLE codex_card_history")
    pg.execute("DROP TABLE codex_sync_runs")
    pg.execute("DELETE FROM schema_version WHERE revision = %s", (rev,))
    db.init_schema(pg)
    drift = [dict(r, name="Rožok slaninový 70g") if r["card_code"] == "27" else r for r in V1]
    _push(pg, drift, hours_old=5)
    codex_sync.run(pg, _cfg())
    assert _binding(pg, ROZOK)[0] == "27"
    assert _orders(pg)[ROZOK]["name"] == "Rožok slaninový 70g"


# --- the push endpoint runs the sync ----------------------------------------------------------

def test_the_cards_push_endpoint_runs_the_sync_and_reports_it(pg):
    _seed_catalogs(pg)
    app = create_app(Config(pg_dsn=PG_DSN, data_dir="/tmp", api_token="tok",
                            secret_key="s", ops_channel_id=77))
    app.testing = True
    c = app.test_client()
    v1_at = (NOW - timedelta(hours=5)).isoformat()
    r = c.post("/api/codex/cards", headers={"X-Token": "tok"},
               json={"source_as_of": v1_at, "cards": V1})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["sync"]["mode"] == "dry-run"
    v2 = [dict(r, name="Chlieb pšeničný voľný 1000g") if r["code"] == CHLIEB else r
          for r in V1]
    r = c.post("/api/codex/cards", headers={"X-Token": "tok"},
               json={"source_as_of": (NOW - timedelta(hours=1)).isoformat(), "cards": v2})
    sync = r.get_json()["sync"]
    assert sync["mode"] == "dry-run" and sync["renamed"] == 1
    assert _orders(pg)[CHLIEB]["name"] == "Chlieb pšeničný 1000g", "dry-run by default"
    assert pg.execute("SELECT count(*) FROM codex_sync_runs").fetchone()[0] == 2


# --- review 17: what a pick does is the picker's own rule (`card_guard.pick_target`) --------

def test_a_renumber_waiting_on_another_card_names_every_number_the_pick_would_select(pg):
    """Review 17 🔵: our CHLIEB (card 31) + its live legacy twin 0CHLIEB; card 31 is missing from
    ONE snapshot and card 55 (the koláč) takes code CHLIEB. The koláč's renumber waits on our
    CHLIEB — the review names BOTH numbers to delete (the picker SELECTS the twin otherwise), and
    following it the pick restores CHLIEB as card 55 and the renumber goes through."""
    _seed_catalogs(pg)
    snapshot.upsert_catalog_card(pg, "0" + CHLIEB, "Chlieb pšeničný 1000g")
    snapshot.rebuild_from_overrides(pg)
    _push(pg, V1, hours_old=6)
    codex_sync.run(pg, _cfg())
    v2 = [dict(r, code=CHLIEB) if r["card_code"] == "55" else r
          for r in V1 if r["card_code"] != "31"]
    _push(pg, v2, hours_old=5)
    codex_sync.run(pg, _cfg())
    reason = _review_reason(pg, "orders", KOLAC)
    assert "nie 55" in reason and f"0{CHLIEB}" in reason
    for g in (CHLIEB, "0" + CHLIEB):
        snapshot.retire_catalog_card(pg, g)
    snapshot.rebuild_from_overrides(pg)
    res = card_guard.add_from_codex(pg, "orders", CHLIEB, actor="sklad")
    assert (res["gtin"], res["created"]) == (CHLIEB, True)
    _push(pg, v2, hours_old=4)
    codex_sync.run(pg, _cfg())
    assert "nie 55" not in _review_reason(pg, "orders", KOLAC)
    orders = _orders(pg)
    assert CHLIEB in orders and KOLAC not in orders, "the koláč was renumbered onto CHLIEB"


def test_a_pick_advice_for_a_dl_twin_says_the_pick_adds_a_new_card(pg):
    """Review 17 🔵: the DL catalog has only the legacy 14-char twin 0ROZOK (curated doplnok /
    mass / cena) and two CODEX cards carry ROZOK under the same name. The DL picker never
    selects or restores a 14-char number — the advice says the pick ADDS a new card with only
    the CODEX name (+ sklad), never that the data stays; and that is what the pick does."""
    dl_snapshot._freeze(pg, [
        {"gtin": "0" + ROZOK, "name": "Rožok so slaninou 70g", "doplnok": "rožok slanina",
         "mass": 0.07, "sklad": "1", "cena": 0.35}], [])
    dup = V1 + [_row(ROZOK, "28", "Rožok so slaninou 70g")]
    _push(pg, dup, hours_old=3)
    codex_sync.run(pg, _cfg())
    reason = _review_reason(pg, "dl", "0" + ROZOK)
    assert "Vybrať kartu z CODEXu" in reason
    assert "údaje ostanú" not in reason and "nová karta" in reason
    dl_snapshot.retire_dl_catalog_card(pg, "0" + ROZOK)
    dl_snapshot.dl_rebuild_from_overrides(pg)
    res = card_guard.add_from_codex(pg, "dl", ROZOK, actor="sklad")
    assert (res["gtin"], res["created"]) == (ROZOK, True)
    assert not _dl(pg)[ROZOK].get("doplnok") and _dl(pg)[ROZOK].get("cena") is None


def test_a_repick_review_never_advises_a_pick_that_selects_another_number_of_ours(pg):
    """Review 17 (speculative, made concrete): the warehouse re-picked our ROZOK as the pagáč
    (card 79) while card 27 moved to ROZOK_NEW — a code our number ROZOK_NEW (card 81) carries
    too. The picker offers card 27 for ROZOK_NEW, but a pick of it SELECTS our ROZOK_NEW (card
    81): the review never advises that pick for the rožok's rows."""
    _seed_catalogs(pg)
    snapshot.upsert_catalog_card(pg, ROZOK_NEW, "Zemiaková placka 90g")
    snapshot.rebuild_from_overrides(pg)
    _seed_memory(pg)
    placka = _row(ROZOK_NEW, "81", "Zemiaková placka 90g")
    _push(pg, V1 + [placka], hours_old=6)
    codex_sync.run(pg, _cfg())
    assert _binding(pg, ROZOK_NEW)[0] == "81"
    v2 = _reused(V1) + [placka]
    _push(pg, v2, hours_old=5)
    codex_sync.run(pg, _cfg())
    snapshot.retire_catalog_card(pg, ROZOK)
    snapshot.rebuild_from_overrides(pg)
    card_guard.add_from_codex(pg, "orders", ROZOK, actor="sklad")
    _push(pg, v2, hours_old=4)
    codex_sync.run(pg, _cfg())
    reason = _review_reason(pg, "orders", ROZOK)
    assert "môže patriť" in reason, "the rožok's rows older than the pick go to a human"
    assert f"kód {ROZOK_NEW} (karta CODEX 27)" not in reason and ROZOK_NEW in reason


def test_a_failing_dry_run_writes_no_ops_alert(pg, monkeypatch):
    """Review 17 🔵: the dry-run writes nothing to the ops outbox — a failing sync included
    (its `error` run row is the record; the alert said names „are not updated", which a dry-run
    never does anyway)."""
    _seed_catalogs(pg)
    _push(pg, V1, hours_old=3)

    def boom(conn, cx):
        raise RuntimeError("boom")

    monkeypatch.setattr(codex_sync.sp, "build_plan", boom)
    res = codex_sync.run_safely(pg, _cfg(apply=False))
    assert res["mode"] == "error"
    assert pg.execute("SELECT status FROM codex_sync_runs ORDER BY id DESC LIMIT 1"
                      ).fetchone()[0] == "error"
    assert pg.execute("SELECT count(*) FROM pending_alerts").fetchone()[0] == 0


# --- review 18: a reset settles before any merge; a restored card is restored as it was -----

def test_a_merge_onto_a_picked_target_carries_our_data_whatever_the_group_order(pg):
    """Review 18 🟡: card 31 (the chlieb, kg sklad 100, mass 1.0) leaves, card 27 (the rožok)
    moves to the chlieb's code; following the review, the warehouse deletes the chlieb number
    and picks the code (restored as card 27 → reset). The next plan resets the chlieb number
    AND merges the rožok into it — ROZOK sorts BEFORE CHLIEB, so the merge used to read the
    target before its reset: our data was lost (or the chlieb's kg data written back onto the
    rožok, the #462 ×N class)."""
    snapshot._freeze(pg, [
        {"gtin": ROZOK, "name": "Rožok so slaninou 70g", "alias": "rozok slanina"},
        {"gtin": CHLIEB, "name": "Chlieb pšeničný 1000g", "alias": "chlieb velky"}], [])
    dl_snapshot._freeze(pg, [
        {"gtin": ROZOK, "name": "Rožok so slaninou 70g", "doplnok": "rožok slanina",
         "mass": 0.07, "sklad": "1", "cena": 0.35},
        {"gtin": CHLIEB, "name": "Chlieb pšeničný 1000g", "doplnok": "chlieb veľký",
         "mass": 1.0, "sklad": "100", "cena": None}], [])
    _push(pg, [_row(ROZOK, "27", "Rožok so slaninou 70g"),
               _row(CHLIEB, "31", "Chlieb pšeničný 1000g")], hours_old=6)
    codex_sync.run(pg, _cfg())
    v2 = [_row(CHLIEB, "27", "Rožok so slaninou 70g")]
    _push(pg, v2, hours_old=5)
    codex_sync.run(pg, _cfg())
    assert "Vybrať kartu z CODEXu" in _review_reason(pg, "dl", ROZOK)
    snapshot.retire_catalog_card(pg, CHLIEB)
    snapshot.rebuild_from_overrides(pg)
    dl_snapshot.retire_dl_catalog_card(pg, CHLIEB)
    dl_snapshot.dl_rebuild_from_overrides(pg)
    for scope in ("orders", "dl"):
        card_guard.add_from_codex(pg, scope, CHLIEB, actor="sklad")
    _push(pg, v2, hours_old=4)
    codex_sync.run(pg, _cfg())
    card = _dl(pg)[CHLIEB]
    assert (card["doplnok"], card["mass"], card["cena"]) == ("rožok slanina", 0.07, 0.35)
    assert card["sklad"] == "1", "never the chlieb's kg sklad on the rožok"
    assert _orders(pg)[CHLIEB]["alias"] == "rozok slanina"
    assert ROZOK not in _orders(pg) and ROZOK not in _dl(pg)


def _sync_renamed_rozok(pg):
    """Our orders rožok renamed BY THE SYNC (a name-only override, alias inherited from the
    snapshot row)."""
    snapshot._freeze(pg, [{"gtin": ROZOK, "name": "Rožok so slaninou 70g",
                           "alias": "rozok slanina"}], [])
    _push(pg, [_row(ROZOK, "27", "Rožok slaninový 70g")], hours_old=8)
    assert codex_sync.run(pg, _cfg())["renamed"] == 1
    assert _orders(pg)[ROZOK]["alias"] == "rozok slanina"


def test_a_picked_sync_renamed_card_keeps_its_alias_as_promised(pg):
    """Review 18 🟡: card 27 leaves, two same-named cards carry the code — the review promises
    delete + pick keeps the data. The sync's rename left the alias only in the snapshot row;
    the delete's rebuild dropped it and the pick restored the card with NO alias."""
    _sync_renamed_rozok(pg)
    two = [_row(ROZOK, "80", "Rožok slaninový 70g"), _row(ROZOK, "81", "Rožok slaninový 70g")]
    for hours in (7, 6):
        _push(pg, two, hours_old=hours)
        codex_sync.run(pg, _cfg())
    assert "jej údaje ostanú" in _review_reason(pg, "orders", ROZOK)
    snapshot.retire_catalog_card(pg, ROZOK)
    snapshot.rebuild_from_overrides(pg)
    card_guard.add_from_codex(pg, "orders", ROZOK, actor="sklad")
    assert _orders(pg)[ROZOK]["alias"] == "rozok slanina"
    _push(pg, two, hours_old=5)
    codex_sync.run(pg, _cfg())
    assert _orders(pg)[ROZOK]["alias"] == "rozok slanina"


def test_a_kos_undo_of_a_deleted_sync_renamed_card_keeps_its_alias(pg):
    """Review 18 🟡: the plain Kôš „Vrátiť" of a delete of that card lost the alias too."""
    _sync_renamed_rozok(pg)
    snapshot.retire_catalog_card(pg, ROZOK)
    snapshot.rebuild_from_overrides(pg)
    audit.record(pg, actor="sklad", table="catalog_overrides", row_id=ROZOK, action="delete",
                 note="Kôš")
    aid = pg.execute("SELECT max(id) FROM audit_log WHERE action = 'delete'").fetchone()[0]
    audit.restore(pg, aid)
    assert _orders(pg)[ROZOK]["alias"] == "rozok slanina"


def test_a_dl_reset_text_says_the_sklad_follows_codex(pg):
    """Review 18 🔵: a DL reset also sets the sklad to the picked CODEX card's (kg-tracking may
    switch) — the text says so; the orders one names the alias."""
    _baseline(pg)
    v = [dict(r, card_code="80", name="Pagáč syrový 60g") if r["card_code"] == "27" else r
         for r in V1]
    for hours in (4, 3):
        _push(pg, v, hours_old=hours)
        codex_sync.run(pg, _cfg())
    dl, orders = _review_reason(pg, "dl", ROZOK), _review_reason(pg, "orders", ROZOK)
    assert "iný názov" in dl and "sklad" in dl
    assert "iný názov" in orders and "alias" in orders


# --- review 19: a reset takes the sklad of the card the number IS, never the code's holder ---

def _picked_as_the_muka(pg, old_sklad):
    """Our DL chlieb CHLIEB (card 31, sklad `old_sklad`); card 31 leaves and card 40 (the múka,
    kg sklad 100) carries CHLIEB; the warehouse deletes CHLIEB and picks it (restored, card 40)."""
    dl_snapshot._freeze(pg, [{"gtin": CHLIEB, "name": "Chlieb pšeničný 1000g",
                              "doplnok": "chlieb", "mass": 1.0, "sklad": old_sklad,
                              "cena": 0.9}], [])
    _push(pg, [_row(CHLIEB, "31", "Chlieb pšeničný 1000g", sklad=int(old_sklad))], hours_old=8)
    codex_sync.run(pg, _cfg())
    for hours in (7, 6):
        _push(pg, [_row(CHLIEB, "40", "Múka pšeničná T650", sklad=100)], hours_old=hours)
        codex_sync.run(pg, _cfg())
    dl_snapshot.retire_dl_catalog_card(pg, CHLIEB)
    dl_snapshot.dl_rebuild_from_overrides(pg)
    card_guard.add_from_codex(pg, "dl", CHLIEB, actor="sklad")


def test_a_reset_takes_the_sklad_of_the_picked_card_after_it_moved(pg):
    """Review 19 🟡: the picked múka (card 40) moves CHLIEB → ROZOK_NEW before the sync that
    resets our number: the reset read the sklad of whoever carries CHLIEB NOW (nobody) and kept
    the chlieb's piece sklad — a kg card piece-tracked (the #462 ×N class)."""
    _picked_as_the_muka(pg, "1")
    _push(pg, [_row(ROZOK_NEW, "40", "Múka pšeničná T650", sklad=100)], hours_old=5)
    codex_sync.run(pg, _cfg())
    assert _dl(pg)[ROZOK_NEW]["sklad"] == "100"


def test_a_reset_never_takes_the_sklad_of_the_card_that_took_the_code_since(pg):
    """Review 19 🟡: … and card 55 (the koláč, sklad 1) took CHLIEB meanwhile — the reset took
    the koláč's sklad for our múka."""
    _picked_as_the_muka(pg, "100")
    _push(pg, [_row(ROZOK_NEW, "40", "Múka pšeničná T650", sklad=100),
               _row(CHLIEB, "55", "Koláč makový 80g")], hours_old=5)
    codex_sync.run(pg, _cfg())
    assert _dl(pg)[ROZOK_NEW]["sklad"] == "100"


def test_a_reset_deferred_by_a_dry_run_takes_the_picked_cards_sklad(pg):
    """Review 19 🟡: the pick seen by dry-run syncs (its reset waits for the first apply), the
    múka moves meanwhile — the first apply still resets to the múka's kg sklad."""
    _picked_as_the_muka(pg, "1")
    _push(pg, [_row(CHLIEB, "40", "Múka pšeničná T650", sklad=100)], hours_old=4)
    codex_sync.run(pg, _cfg(apply=False))
    for hours in (3, 2):
        _push(pg, [_row(ROZOK_NEW, "40", "Múka pšeničná T650", sklad=100)], hours_old=hours)
        codex_sync.run(pg, _cfg(apply=hours == 2))
    assert _dl(pg)[ROZOK_NEW]["sklad"] == "100"


# --- review 20: a pick waits out a glitch; a reset's sklad is exactly a fresh pick's ----------

def test_a_pick_settled_while_its_card_is_missing_once_waits_for_the_card(pg):
    """Review 20 🟡: the picked múka (card 40) is missing from ONE list exactly at the sync
    that settles the pick — the reset ran with no sklad to take and stored the binding, so the
    chlieb's piece sklad stayed for good. A card missing from one snapshot is a glitch: the
    pick waits, the next list with the card settles it."""
    _picked_as_the_muka(pg, "1")
    _push(pg, [_row(KOLAC, "55", "Koláč makový 80g")], hours_old=5)
    codex_sync.run(pg, _cfg())
    assert _dl(pg)[CHLIEB]["doplnok"] == "chlieb", "nothing happens on a glitch"
    for hours in (4, 3):
        _push(pg, [_row(CHLIEB, "40", "Múka pšeničná T650", sklad=100),
                   _row(KOLAC, "55", "Koláč makový 80g")], hours_old=hours)
        codex_sync.run(pg, _cfg())
    card = _dl(pg)[CHLIEB]
    assert (card["sklad"], card["doplnok"]) == ("100", "")


def test_a_first_apply_on_a_glitched_list_still_resets_to_the_picked_cards_sklad(pg):
    """Review 20 🟡: the pick seen by a dry-run; the first APPLY lands on a list missing the
    picked card once — the next list with the card still resets to its kg sklad."""
    _picked_as_the_muka(pg, "1")
    _push(pg, [_row(CHLIEB, "40", "Múka pšeničná T650", sklad=100)], hours_old=5)
    codex_sync.run(pg, _cfg(apply=False))
    _push(pg, [_row(KOLAC, "55", "Koláč makový 80g")], hours_old=4)
    codex_sync.run(pg, _cfg())
    _push(pg, [_row(CHLIEB, "40", "Múka pšeničná T650", sklad=100),
               _row(KOLAC, "55", "Koláč makový 80g")], hours_old=3)
    codex_sync.run(pg, _cfg())
    assert _dl(pg)[CHLIEB]["sklad"] == "100"


def test_a_reset_of_a_moved_card_takes_the_sklad_a_pick_of_its_new_code_gives(pg):
    """Review 20 🔵: the picked card 40 moves — its sklad-1 row now carries the NEW code
    ROZOK_NEW (the renumber target), its sklad-100 row an older code. A pick of ROZOK_NEW gives
    sklad 1; the reset took the kg sklad of ALL the card's rows (a piece card kg-tracked with a
    blank mass → a #462 hold on every piece delivery)."""
    older = "9990000000086"
    _picked_as_the_muka(pg, "1")
    _push(pg, [_row(older, "40", "Múka pšeničná T650", sklad=100),
               _row(CHLIEB, "40", "Múka pšeničná T650")], hours_old=5)
    codex_sync.run(pg, _cfg(apply=False))
    _push(pg, [_row(older, "40", "Múka pšeničná T650", sklad=100),
               _row(ROZOK_NEW, "40", "Múka pšeničná T650")], hours_old=4)
    codex_sync.run(pg, _cfg())
    assert _dl(pg)[ROZOK_NEW]["sklad"] == str(card_guard.pickable(pg, "dl")[ROZOK_NEW]["sklad"])
    assert _dl(pg)[ROZOK_NEW]["sklad"] == "1"


def test_a_reset_on_a_code_two_cards_carry_takes_the_pickers_sklad(pg):
    """Review 20 🔵: code CHLIEB carried by card 40 (the central sklad-1 row — the one offered)
    and card 41 (a kg sklad-100 row): a pick restoring our Kôš chlieb as card 40 is reset to
    exactly what a fresh pick of the code writes (the picker's sklad rule over the code)."""
    dl_snapshot._freeze(pg, [{"gtin": CHLIEB, "name": "Chlieb pšeničný 1000g",
                              "doplnok": "chlieb", "mass": 1.0, "sklad": "1", "cena": 0.9}], [])
    _push(pg, [_row(CHLIEB, "31", "Chlieb pšeničný 1000g")], hours_old=8)
    codex_sync.run(pg, _cfg())
    both = [_row(CHLIEB, "40", "Múka pšeničná T650"),
            _row(CHLIEB, "41", "Múka pšeničná T650 kg", sklad=100)]
    for hours in (7, 6):
        _push(pg, both, hours_old=hours)
        codex_sync.run(pg, _cfg())
    dl_snapshot.retire_dl_catalog_card(pg, CHLIEB)
    dl_snapshot.dl_rebuild_from_overrides(pg)
    card_guard.add_from_codex(pg, "dl", CHLIEB, actor="sklad")
    _push(pg, both, hours_old=5)
    codex_sync.run(pg, _cfg())
    picker = card_guard.pickable(pg, "dl")[CHLIEB]
    assert picker["card_code"] == "40"
    assert _dl(pg)[CHLIEB]["sklad"] == str(picker["sklad"]) == "100"


# --- review 21: the picker's sklad only for the card's OWN offered row; both cards of a pick
# --- wait out a glitch --------------------------------------------------------------------

def test_a_reset_never_takes_the_sklad_of_another_card_offered_for_an_inactive_row(pg):
    """Review 21 🟡: the picked múka's (card 40) row on CHLIEB went INACTIVE before the
    resetting sync while the koláč (card 55, sklad 1) carries CHLIEB actively — the picker's
    entry for CHLIEB is purely the koláč's; the reset must take the múka's own kg sklad."""
    _picked_as_the_muka(pg, "1")
    _push(pg, [_row(CHLIEB, "40", "Múka pšeničná T650", sklad=100, inactive=True),
               _row(CHLIEB, "55", "Koláč makový 80g")], hours_old=5)
    codex_sync.run(pg, _cfg())
    assert _dl(pg)[CHLIEB]["sklad"] == "100"


def test_a_dry_run_deferred_reset_after_the_row_went_inactive_keeps_the_cards_sklad(pg):
    """Review 21 🟡: the same after the pick was seen by a dry-run (the reset deferred)."""
    _picked_as_the_muka(pg, "1")
    _push(pg, [_row(CHLIEB, "40", "Múka pšeničná T650", sklad=100)], hours_old=5)
    codex_sync.run(pg, _cfg(apply=False))
    _push(pg, [_row(CHLIEB, "40", "Múka pšeničná T650", sklad=100, inactive=True),
               _row(CHLIEB, "55", "Koláč makový 80g")], hours_old=4)
    codex_sync.run(pg, _cfg())
    assert _dl(pg)[CHLIEB]["sklad"] == "100"


def test_a_pick_waits_while_the_card_it_replaces_is_missing_once(pg):
    """Review 21 🔵: card 31 (our chlieb, taught rows) moves to ROZOK_NEW, the múka (card 40)
    takes CHLIEB, the warehouse picks CHLIEB during the dry-run. The first apply lands on a list
    missing card 31 ONCE: the rows review told the warehouse the chlieb is gone from CODEX —
    delete its rows — and the binding made it final. The pick waits; the next list gives the
    true way out (pick card 31 under its new code)."""
    dl_snapshot._freeze(pg, [{"gtin": CHLIEB, "name": "Chlieb pšeničný 1000g",
                              "doplnok": "chlieb", "mass": 1.0, "sklad": "1", "cena": 0.9}], [])
    _push(pg, [_row(CHLIEB, "31", "Chlieb pšeničný 1000g")], hours_old=8)
    codex_sync.run(pg, _cfg())
    pg.execute("INSERT INTO dl_item_memory (supplier_ean, item_key, item_raw, gtin, card, "
               "delivered_on, cnt, source, created_at) VALUES ('S1', 'chlieb psen', "
               "'Chlieb pšen.', %s, 'Chlieb', %s, 1, 'human', %s)",
               (CHLIEB, date(2026, 9, 3), _BEFORE))
    both = [_row(ROZOK_NEW, "31", "Chlieb pšeničný 1000g"),
            _row(CHLIEB, "40", "Múka pšeničná T650", sklad=100)]
    _push(pg, both, hours_old=7)
    codex_sync.run(pg, _cfg(apply=False))
    dl_snapshot.retire_dl_catalog_card(pg, CHLIEB)
    dl_snapshot.dl_rebuild_from_overrides(pg)
    card_guard.add_from_codex(pg, "dl", CHLIEB, actor="sklad")
    _push(pg, [_row(CHLIEB, "40", "Múka pšeničná T650", sklad=100)], hours_old=6)
    codex_sync.run(pg, _cfg())
    assert "už v CODEXe nie je" not in _review_reason(pg, "dl", CHLIEB)
    assert _dl(pg)[CHLIEB]["doplnok"] == "chlieb", "nothing happens on a glitch"
    # review 24 🔵: the report names the REPLACED card as the missing one
    assert "karta CODEX 31 v tomto zozname chýba" in _waits(pg)[("dl", CHLIEB)]
    _push(pg, both, hours_old=5)
    codex_sync.run(pg, _cfg())
    assert f"kód {ROZOK_NEW} (karta CODEX 31)" in _review_reason(pg, "dl", CHLIEB)


# --- review 22: no renumber merges onto a number whose pick is waiting -----------------------

def test_a_renumber_never_merges_onto_a_pick_waiting_out_a_glitch(pg):
    """Review 22 🟡: card 31 (the chlieb) moves CHLIEB → ROZOK_NEW (our CHLIEB follows, the
    number goes to the Kôš) and card 27 (the rožok) ALSO carries CHLIEB; the warehouse picks
    CHLIEB — the Kôš chlieb is restored as card 27. The next list misses card 31 once (the pick
    waits) and card 27 leaves ROZOK: our ROZOK renumbered onto the waiting CHLIEB as a merge,
    the binding it stored superseded the pick — the chlieb's alias / doplnok stayed on the
    rožok for good, never reset, never reviewed."""
    rest = [_row(KOLAC, "55", "Koláč makový 80g"), _row(CUDZIA, "79", "Pagáč syrový 60g")]
    snapshot._freeze(pg, [
        {"gtin": ROZOK, "name": "Rožok so slaninou 70g", "alias": "rozok slanina"},
        {"gtin": CHLIEB, "name": "Chlieb pšeničný 1000g", "alias": "chlieb velky"}], [])
    dl_snapshot._freeze(pg, [
        {"gtin": ROZOK, "name": "Rožok so slaninou 70g", "doplnok": "rožok slanina",
         "mass": 0.07, "sklad": "1", "cena": 0.35},
        {"gtin": CHLIEB, "name": "Chlieb pšeničný 1000g", "doplnok": "chlieb veľký",
         "mass": 1.0, "sklad": "1", "cena": 0.9}], [])
    _push(pg, rest + [_row(ROZOK, "27", "Rožok so slaninou 70g"),
                      _row(CHLIEB, "31", "Chlieb pšeničný 1000g")], hours_old=9)
    codex_sync.run(pg, _cfg())
    _push(pg, rest + [_row(ROZOK, "27", "Rožok so slaninou 70g"),
                      _row(CHLIEB, "27", "Rožok so slaninou 70g"),
                      _row(ROZOK_NEW, "31", "Chlieb pšeničný 1000g")], hours_old=8)
    codex_sync.run(pg, _cfg())
    for scope in ("orders", "dl"):
        card_guard.add_from_codex(pg, scope, CHLIEB, actor="sklad")
    _push(pg, rest + [_row(CHLIEB, "27", "Rožok so slaninou 70g")], hours_old=7)
    codex_sync.run(pg, _cfg())
    # review 24 🔵: both waits are reported — the pick (card 31 missing) and the renumber onto it
    waits = _waits(pg)
    assert "karta CODEX 31 v tomto zozname chýba" in waits[("dl", CHLIEB)]
    assert "prečíslovanie" in waits[("dl", ROZOK)] and CHLIEB in waits[("dl", ROZOK)]
    steady = rest + [_row(CHLIEB, "27", "Rožok so slaninou 70g"),
                     _row(ROZOK_NEW, "31", "Chlieb pšeničný 1000g")]
    for hours in (6, 5):
        _push(pg, steady, hours_old=hours)
        codex_sync.run(pg, _cfg())
    assert _dl(pg)[CHLIEB]["doplnok"] == "rožok slanina"
    assert _orders(pg)[CHLIEB]["alias"] == "rozok slanina"


# --- review 23: every glitch wait is reported + logged; the run log keeps a bounded window ---

def _waits(pg):
    return {(w["scope"], w["gtin"]): w["why"] for w in _last_report(pg).get("waits", [])}


def test_a_card_missing_from_one_list_is_reported_as_waiting(pg, caplog):
    """Review 23 🔵: a card missing from ONE list leaves its numbers alone (a glitch) — and the
    report / log say so, never an all-zero plan indistinguishable from "nothing to do"."""
    _baseline(pg)
    _push(pg, [r for r in V1 if r["card_code"] != "27"], hours_old=4)
    with caplog.at_level(logging.INFO, logger="orders.codex_sync"):
        codex_sync.run(pg, _cfg())
    waits = _waits(pg)
    assert ("orders", ROZOK) in waits and ("dl", ROZOK) in waits
    assert "27" in waits[("orders", ROZOK)]
    assert any("waits" in r.getMessage() and ROZOK in r.getMessage() for r in caplog.records)


def test_an_unbound_card_whose_code_changed_carrier_in_one_list_is_reported_as_waiting(pg):
    """Review 24 🔵: ROZOK sat on cards 27 and 28 (our name matches neither — reviewed); one
    list without card 27 leaves 28 the only carrier — one push is no proof, nothing is bound —
    and the report says the number waits (its open review vanished with no trace before)."""
    _seed_catalogs(pg)
    snapshot.upsert_catalog_card(pg, ROZOK, "Rožok starý názov")
    snapshot.rebuild_from_overrides(pg)
    shared = V1 + [_row(ROZOK, "28", "Bageta šunková 120g")]
    _push(pg, shared, hours_old=5)
    codex_sync.run(pg, _cfg(apply=False))
    _push(pg, [r for r in shared if r["card_code"] != "27"], hours_old=3)
    codex_sync.run(pg, _cfg(apply=False))
    why = _waits(pg).get(("orders", ROZOK), "")
    assert "28" in why and "27" in why


def test_a_pick_waiting_out_a_glitch_is_reported(pg):
    """Review 23 🔵: a pick whose picked card is missing from one list waits — reported."""
    _picked_as_the_muka(pg, "1")
    _push(pg, [_row(KOLAC, "55", "Koláč makový 80g")], hours_old=5)
    codex_sync.run(pg, _cfg())
    assert "40" in _waits(pg).get(("dl", CHLIEB), "")


def test_the_run_log_keeps_a_window_and_the_newest_run_of_each_status(pg):
    """Review 23 🔵: `codex_sync_runs` (the whole plan as JSON per push) never grew bounded —
    runs older than `RUNS_KEEP_DAYS` are pruned, the newest run of each status is always kept
    (the last applied run's review dedup, the previous synced snapshot)."""
    _baseline(pg)
    old = NOW - timedelta(days=codex_sync.RUNS_KEEP_DAYS + 10)
    for status in ("dry-run", "error", "error", "apply"):
        pg.execute("INSERT INTO codex_sync_runs (ran_at, status, applied, report) "
                   "VALUES (%s, %s, %s, '{}')", (old, status, status == "apply"))
    _push(pg, V1, hours_old=1)
    codex_sync.run(pg, _cfg())
    rows = pg.execute("SELECT status FROM codex_sync_runs WHERE ran_at < %s ORDER BY id",
                      (NOW - timedelta(days=codex_sync.RUNS_KEEP_DAYS),)).fetchall()
    assert [r[0] for r in rows] == ["dry-run", "error"], "the newest old run of each status"
    assert pg.execute("SELECT count(*) FROM codex_sync_runs WHERE ran_at >= %s",
                      (NOW - timedelta(days=1),)).fetchone()[0] == 2


# --- review 25: an UNBOUND number whose code changed carrier — our name decides, else a human -

def _first_post_deploy(pg, v2, *, apply=True, runs=2):
    """The rollout shape: the history holds only the pre-deploy list (the migration seed — no
    synced run yet, every number unbound), then `runs` pushes of `v2`."""
    _seed_catalogs(pg)
    _push(pg, V1, hours_old=6)
    codex_sync._record_history(pg)
    for hours in range(5, 5 - runs, -1):
        _push(pg, v2, hours_old=hours)
        codex_sync.run(pg, _cfg(apply=apply))


def test_an_unbound_number_follows_the_card_its_name_is_not_the_reuser(pg):
    """Review 25 🟡: card 27 (our rožok) moved ROZOK → ROZOK_NEW and the pagáč (79) took ROZOK
    — on the first post-deploy lists every number is unbound; after the one-list wait our rožok
    was bound to the pagáč and renamed (round 1's 🔴). Our NAME is card 27's product: it follows
    card 27 to ROZOK_NEW."""
    _first_post_deploy(pg, _reused(V1))
    orders = _orders(pg)
    assert ROZOK not in orders and orders[ROZOK_NEW]["name"] == "Rožok so slaninou 70g"
    assert _binding(pg, ROZOK_NEW)[0] == "27"
    assert pg.execute("SELECT count(*) FROM codex_card_bindings WHERE card_code = '79'"
                      ).fetchone()[0] == 0


def test_a_dry_run_never_stores_the_reusers_binding_for_an_unbound_number(pg):
    """Review 25 🟡: the same under the real rollout — dry-runs store identity in every mode,
    and stored the pagáč's binding; the first apply then renamed both catalogs' rožok."""
    _first_post_deploy(pg, _reused(V1), apply=False)
    assert _binding(pg, ROZOK)[0] == "27" and _binding(pg, ROZOK, "dl")[0] == "27"
    _push(pg, _reused(V1), hours_old=2)
    codex_sync.run(pg, _cfg())
    assert _orders(pg)[ROZOK_NEW]["name"] == "Rožok so slaninou 70g"


def test_an_unbound_number_named_like_neither_carrier_goes_to_a_human(pg):
    """Review 25 🟡: ROZOK sat on cards 27 and 28 and our orders name matches neither
    (reviewed); card 27 moves to ROZOK_NEW — after the one-list wait our ROZOK was bound to the
    bageta (28) and renamed. Neither name is ours: a human decides."""
    _seed_catalogs(pg)
    snapshot.upsert_catalog_card(pg, ROZOK, "Rožok starý názov")
    snapshot.rebuild_from_overrides(pg)
    shared = V1 + [_row(ROZOK, "28", "Bageta šunková 120g")]
    _push(pg, shared, hours_old=6)
    codex_sync.run(pg, _cfg())
    moved = [dict(r, code=ROZOK_NEW) if r["card_code"] == "27" else r for r in shared]
    for hours in (5, 4):
        _push(pg, moved, hours_old=hours)
        codex_sync.run(pg, _cfg())
    assert _orders(pg)[ROZOK]["name"] == "Rožok starý názov"
    assert _binding(pg, ROZOK) is None
    reason = _review_reason(pg, "orders", ROZOK)
    assert "28" in reason and "27" in reason


def test_an_unbound_number_named_like_the_new_carrier_is_bound_to_it(pg):
    """Review 25 🔵: the rožok recreated in CODEX as card 127 under 27's own name, card 27 gone
    — our unbound rožok is the same product: bound to 127, no rename. Review 26 🔵 (test
    fixed): decided on the SECOND list, the one after the wait — with a third list it also
    converged through the rename rebind, so it pinned nothing."""
    _first_post_deploy(pg, [dict(r, card_code="127") if r["card_code"] == "27" else r
                            for r in V1], runs=2)
    assert _binding(pg, ROZOK)[0] == "127"
    assert _orders(pg)[ROZOK]["name"] == "Rožok so slaninou 70g"


def test_no_renumber_merges_onto_an_unbound_number_waiting_for_its_carrier(pg):
    """Review 25 🟡: card 27 (our rožok) moved ROZOK → ROZOK_NEW and card 31 (our chlieb)
    moved CHLIEB → ROZOK — our unbound ROZOK waits (new carrier), and our chlieb's renumber
    merged into it in the same plan (the rožok renamed to the chlieb, bound to 31). It never
    lands on a number another group of this plan identified as another card either."""
    v2 = [dict(r, code=ROZOK_NEW) if r["card_code"] == "27"
          else dict(r, code=ROZOK) if r["card_code"] == "31" else r for r in V1]
    _first_post_deploy(pg, v2, runs=1)
    assert _orders(pg)[ROZOK]["name"] == "Rožok so slaninou 70g"
    assert ("orders", ROZOK) in _waits(pg)
    _push(pg, v2, hours_old=3)
    codex_sync.run(pg, _cfg())
    orders = _orders(pg)
    assert orders[ROZOK_NEW]["name"] == "Rožok so slaninou 70g"
    assert _binding(pg, ROZOK_NEW)[0] == "27"
    # the chlieb's renumber onto ROZOK — the number this plan identified as card 27 — is a
    # human's call (a chain in one push), never a merge
    assert ROZOK not in orders and orders[CHLIEB]["name"] == "Chlieb pšeničný 1000g"
    assert "nie 31" in _review_reason(pg, "orders", CHLIEB)


# --- review 26: the carrier-change reviews say what is true; the gates are pinned -------------

BAGETA = "9990000000215"        # synthetic: card 88's code, then card 27's
BAGETA_ELSE = "9990000000222"   # card 88's next code


def _bageta_taken_by_the_rozok(pg, runs):
    """Our unbound orders BAGETA („Bageta stará" — named like no card) on card 88's code;
    card 27 (our rožok) moves ROZOK → BAGETA, card 88 moves on to BAGETA_ELSE."""
    _seed_catalogs(pg)
    snapshot.upsert_catalog_card(pg, BAGETA, "Bageta stará")
    snapshot.rebuild_from_overrides(pg)
    v1 = V1 + [_row(BAGETA, "88", "Bageta šunková 120g")]
    _push(pg, v1, hours_old=6)
    codex_sync._record_history(pg)
    v2 = [dict(r, code=BAGETA) if r["card_code"] == "27"
          else dict(r, code=BAGETA_ELSE) if r["card_code"] == "88" else r for r in v1]
    for hours in range(5, 5 - runs, -1):
        _push(pg, v2, hours_old=hours)
        codex_sync.run(pg, _cfg())
    return v2


def test_a_renumber_waiting_on_a_new_carrier_wait_promises_nothing(pg):
    """Review 26 🔵: the renumber onto a number waiting for its new carrier said the number
    „will be assigned to a CODEX card with the next list" — it may get a review instead."""
    _bageta_taken_by_the_rozok(pg, runs=1)
    why = _waits(pg)[("orders", ROZOK)]
    assert BAGETA in why and "priradí" not in why
    assert "má nového nositeľa" in why, "the carrier wait, not a review the number lacks"


def test_no_renumber_merges_onto_an_unbound_number_a_human_must_decide(pg):
    """Review 26 🔵 (pins round 25's gate): our BAGETA is named like neither card — reviewed;
    our rožok's renumber onto it never merges (it waits, pointing at the number's review)."""
    _bageta_taken_by_the_rozok(pg, runs=2)
    orders = _orders(pg)
    assert orders[BAGETA]["name"] == "Bageta stará"
    assert orders[ROZOK]["name"] == "Rožok so slaninou 70g"
    assert _binding(pg, BAGETA) is None
    assert "kontrol" in _waits(pg)[("orders", ROZOK)]


def test_a_carrier_change_review_names_a_way_out_that_works(pg):
    """Review 26 🔵: the review advised „rename it to the name of the card that is ours" — for
    the CURRENT carrier that is the drift-button shape (a human again); the way out for it is
    the pick, and following it settles the number."""
    v2 = _bageta_taken_by_the_rozok(pg, runs=2)
    reason = _review_reason(pg, "orders", BAGETA)
    assert "Vybrať kartu z CODEXu" in reason and "„Bageta šunková 120g“" in reason
    # review 27 🔵: an UNBOUND number restored by the pick keeps its data — with no claim it is
    # „the same product" — and the taught rows are pointed at
    assert "jej údaje ostanú" in reason and "ten istý" not in reason
    assert "Naučené" in reason
    snapshot.retire_catalog_card(pg, BAGETA)
    snapshot.rebuild_from_overrides(pg)
    card_guard.add_from_codex(pg, "orders", BAGETA, actor="sklad")
    _push(pg, v2, hours_old=3)
    codex_sync.run(pg, _cfg())
    assert _binding(pg, BAGETA)[0] == "27"


def test_a_carrier_change_with_two_same_named_earlier_carriers_says_so(pg):
    """Review 26 🔵: same-named cards 27 and 127 carried ROZOK; both leave it, the pagáč takes
    it — the review said our name „matches none of them" while it is the name of both."""
    _seed_catalogs(pg)
    _push(pg, V1 + [_row(ROZOK, "127", "Rožok so slaninou 70g")], hours_old=6)
    codex_sync._record_history(pg)
    v2 = _reused(V1) + [_row("9990000000208", "127", "Rožok so slaninou 70g")]
    for hours in (5, 4):
        _push(pg, v2, hours_old=hours)
        codex_sync.run(pg, _cfg(apply=False))
    reason = _review_reason(pg, "orders", ROZOK)
    assert "nesedí so žiadnou" not in reason and "27" in reason and "127" in reason
    # review 27 🔵: a name several earlier carriers bear binds none of them — never offered
    assert "premenuj" not in reason


def test_an_unbound_number_renamed_to_the_reusers_name_goes_to_a_human(pg):
    """Review 26 🔵 (round 9's rule for an unbound number): the pagáč (79) took ROZOK, card 27
    moved to ROZOK_NEW; during the wait the warehouse clicked the #467 drift button (our orders
    ROZOK takes the pagáč's name) — the next list bound it to the pagáč with the rožok's alias
    and wordings, the DL copy following 27. A human decides."""
    _seed_catalogs(pg)
    _push(pg, V1, hours_old=6)
    codex_sync._record_history(pg)
    _push(pg, _reused(V1), hours_old=5)
    codex_sync.run(pg, _cfg(apply=False))
    _drift_click(pg, ROZOK, "Pagáč syrový 60g")
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg(apply=False))
    assert _binding(pg, ROZOK) is None
    reason = _review_reason(pg, "orders", ROZOK)
    assert "79" in reason and "27" in reason
    assert _binding(pg, ROZOK, "dl")[0] == "27"
    # review 27 🔵: the pick way out keeps data the review suspects is the rožok's — the taught
    # rows are pointed at, like the bound contest review does
    assert "Naučené" in reason
    # review 29 🔵: the text says the pagáč carries the code NOW, and never advises renaming
    # our card to the name it already has
    assert "ktorá ho teraz nesie" in reason
    assert "na „Pagáč syrový 60g“ — pri ďalšom" not in reason


def test_an_other_card_review_reads_the_catalog_the_whole_plan_leaves(pg):
    """Review 26 🔵 (pins round 25's deferral): card 31 moves CHLIEB → CHLIEB_NEW and card 27
    moves ROZOK → CHLIEB in one push — the ROZOK group runs first; its review said „delete it
    (Kôš)" for the CHLIEB number the same plan already retires."""
    _baseline(pg)
    v2 = [dict(r, code=CHLIEB) if r["card_code"] == "27"
          else dict(r, code=CHLIEB_NEW) if r["card_code"] == "31" else r for r in V1]
    _push(pg, v2, hours_old=4)
    codex_sync.run(pg, _cfg())
    reason = _review_reason(pg, "orders", ROZOK)
    assert "nie 27" in reason and "zmaž" not in reason


# --- review 27: one list is no proof for the earlier carrier either; every way out is pinned --

def test_a_drift_renamed_unbound_number_waits_while_the_earlier_carrier_is_missing_once(pg):
    """Review 27 🟡: the drift-renamed unbound ROZOK (named like the pagáč now on its code)
    was decided on a list missing card 27 (the rožok, on ROZOK_NEW) ONCE — no earlier carrier
    „alive", bound to the pagáč for good, a dry-run included. It waits; with 27 back a human
    decides, the rožok's alias untouched."""
    _seed_catalogs(pg)
    _seed_memory(pg)
    _push(pg, V1, hours_old=6)
    codex_sync._record_history(pg)
    _push(pg, _reused(V1), hours_old=5)
    codex_sync.run(pg, _cfg(apply=False))
    _drift_click(pg, ROZOK, "Pagáč syrový 60g")
    _push(pg, [r for r in _reused(V1) if r["card_code"] != "27"], hours_old=4)
    codex_sync.run(pg, _cfg(apply=False))
    assert _binding(pg, ROZOK) is None
    assert "27" in _waits(pg).get(("orders", ROZOK), "")
    _push(pg, _reused(V1), hours_old=3)
    codex_sync.run(pg, _cfg())
    assert _binding(pg, ROZOK) is None and _orders(pg)[ROZOK]["alias"] == "rozok slanina"
    assert "79" in _review_reason(pg, "orders", ROZOK)


def test_an_unbound_number_named_like_a_new_carrier_of_the_same_product_is_bound(pg):
    """Review 27 🔵 (pins round 26's same-product rule): card 27 moved to ROZOK_NEW renamed
    „Rožok slaninový 70g", card 127 carries ROZOK under 27's old name — the same product,
    never the drift-button suspicion: bound to 127."""
    v2 = [dict(r, code=ROZOK_NEW, name="Rožok slaninový 70g") if r["card_code"] == "27"
          else r for r in V1] + [_row(ROZOK, "127", "Rožok so slaninou 70g")]
    _first_post_deploy(pg, v2)
    assert _binding(pg, ROZOK)[0] == "127"


def test_a_carrier_change_review_never_offers_the_carrier_nows_name(pg):
    """Review 27 🔵: ROZOK sat on cards 27 (rožok) and 79 (pagáč); now card 80 carries it,
    named like the pagáč; our ROZOK „Bageta stará" matches none — renaming to „Pagáč syrový
    60g" is the carrier now's name (a human again): never offered; the rožok's name is."""
    _seed_catalogs(pg)
    snapshot.upsert_catalog_card(pg, ROZOK, "Bageta stará")
    snapshot.rebuild_from_overrides(pg)
    _push(pg, V1 + [_row(ROZOK, "79", "Pagáč syrový 60g")], hours_old=6)
    codex_sync._record_history(pg)
    v2 = ([dict(r, code=ROZOK_NEW) if r["card_code"] == "27" else r for r in V1]
          + [_row(ROZOK, "80", "Pagáč syrový 60g")])
    for hours in (5, 4):
        _push(pg, v2, hours_old=hours)
        codex_sync.run(pg, _cfg(apply=False))
    reason = _review_reason(pg, "orders", ROZOK)
    assert "na „Rožok so slaninou 70g“" in reason
    assert "na „Pagáč syrový 60g“" not in reason
    assert "Naučené" in reason


def test_a_carrier_change_review_never_offers_a_card_gone_from_codex(pg):
    """Review 27 🔵: card 88 (the bageta that carried BAGETA before) left CODEX entirely — the
    review offered „rename to its name", which binds a card gone from CODEX (review 10's rule:
    never point at a card gone from CODEX)."""
    _seed_catalogs(pg)
    snapshot.upsert_catalog_card(pg, BAGETA, "Bageta stará")
    snapshot.rebuild_from_overrides(pg)
    _push(pg, V1 + [_row(BAGETA, "88", "Bageta šunková 120g")], hours_old=6)
    codex_sync._record_history(pg)
    v2 = [dict(r, code=BAGETA) if r["card_code"] == "27" else r for r in V1]
    for hours in (5, 4):
        _push(pg, v2, hours_old=hours)
        codex_sync.run(pg, _cfg())
    reason = _review_reason(pg, "orders", BAGETA)
    assert reason and "premenuj" not in reason


def test_a_pick_advice_says_a_restored_card_bound_to_another_product_is_cleared(pg):
    """Review 27 🔵 (pins `Pick.same`'s cleared branch): our ROZOK (card 27) renamed by the
    drift button to the bageta's name while card 80 (the bageta — the card the picker offers)
    also carries ROZOK: the contest review's pick restores ROZOK as card 80 — another product,
    its alias is cleared; said so, and true."""
    _baseline(pg)
    v = V1 + [_row(ROZOK, "80", "Bageta šunková 120g")]
    v[-1]["changed_at"] = "2026-09-29T10:00:00+02:00"
    _push(pg, v, hours_old=4)
    codex_sync.run(pg, _cfg())
    _drift_click(pg, ROZOK, "Bageta šunková 120g")
    _push(pg, v, hours_old=3)
    codex_sync.run(pg, _cfg())
    assert _pick_card(pg) == "80"
    reason = _review_reason(pg, "orders", ROZOK)
    assert "jej alias sa vyčistí" in reason and "jej údaje ostanú" not in reason
    snapshot.retire_catalog_card(pg, ROZOK)
    snapshot.rebuild_from_overrides(pg)
    card_guard.add_from_codex(pg, "orders", ROZOK, actor="sklad")
    _push(pg, v, hours_old=2)
    codex_sync.run(pg, _cfg())
    assert _orders(pg)[ROZOK]["alias"] == ""


# --- review 28: an unbound number whose code has NO carrier now — the history decides too ----

def test_an_unbound_number_follows_its_card_when_the_reuser_moves_on(pg):
    """Review 28 🟡: card 27 (our rožok) moved ROZOK → ROZOK_NEW, the pagáč (79) took ROZOK and
    then moved on to PAGAC_W — no card carries ROZOK now and the unbound number was bound to the
    code's LAST carrier, the pagáč: renumbered to PAGAC_W as the pagáč with the rožok's data and
    wordings. Its name says card 27: it follows card 27."""
    _seed_catalogs(pg)
    _seed_memory(pg)
    _push(pg, V1, hours_old=6)
    codex_sync._record_history(pg)
    _push(pg, _reused(V1), hours_old=5)
    codex_sync.run(pg, _cfg())
    _push(pg, _reused(V1, pagac_code=PAGAC_W), hours_old=4)
    codex_sync.run(pg, _cfg())
    orders = _orders(pg)
    assert PAGAC_W not in orders and orders[ROZOK_NEW]["name"] == "Rožok so slaninou 70g"
    assert _binding(pg, ROZOK_NEW)[0] == "27"
    assert set(_gtins(pg, "item_memory")) == {ROZOK_NEW}


def test_an_unbound_number_waits_while_the_codes_last_carrier_is_missing_once(pg):
    """Review 28 🟡: … the pagáč missing from ONE list, no card on ROZOK — the dry-run bound
    the number to the pagáč (its last carrier) for good. It waits; then follows card 27."""
    _seed_catalogs(pg)
    _push(pg, V1, hours_old=6)
    codex_sync._record_history(pg)
    _push(pg, _reused(V1), hours_old=5)
    codex_sync.run(pg, _cfg(apply=False))
    _push(pg, [r for r in _reused(V1) if r["card_code"] != "79"], hours_old=4)
    codex_sync.run(pg, _cfg(apply=False))
    assert _binding(pg, ROZOK) is None
    assert "79" in _waits(pg).get(("orders", ROZOK), "")
    _push(pg, _reused(V1), hours_old=3)
    codex_sync.run(pg, _cfg())
    assert _orders(pg)[ROZOK_NEW]["name"] == "Rožok so slaninou 70g"
    assert _binding(pg, ROZOK_NEW)[0] == "27"


def test_a_drift_renamed_unbound_number_whose_old_card_left_codex_goes_to_a_human(pg):
    """Review 28 🔵: card 27 left CODEX for good and the pagáč took ROZOK; during the wait the
    drift button renamed our orders ROZOK to the pagáč — it was bound to the pagáč keeping the
    rožok's alias and wordings (a bound number in this shape is reset + its rows reviewed).
    Whose data it carries is unknown: a human decides, pointed at the curated fields."""
    _seed_catalogs(pg)
    _push(pg, V1, hours_old=6)
    codex_sync._record_history(pg)
    gone = [dict(r, code=ROZOK) if r["card_code"] == "79" else r
            for r in V1 if r["card_code"] != "27"]
    _push(pg, gone, hours_old=5)
    codex_sync.run(pg, _cfg(apply=False))
    _drift_click(pg, ROZOK, "Pagáč syrový 60g")
    _push(pg, gone, hours_old=4)
    codex_sync.run(pg, _cfg(apply=False))
    assert _binding(pg, ROZOK) is None
    reason = _review_reason(pg, "orders", ROZOK)
    assert "27" in reason and "jej alias (Produkty)" in reason


def test_a_pick_advice_restoring_a_card_of_the_same_product_says_so(pg):
    """Review 28 🔵 (pins `Pick.same`): card 27 left CODEX; ROZOK is carried by 80 under 27's
    own name (the card the picker offers) and by 81 — the gone review's pick restores our
    ROZOK (bound to 27) as card 80: the same product, its data stays — said so."""
    _baseline(pg)
    v = [r for r in V1 if r["card_code"] != "27"] + [
        _row(ROZOK, "80", "Rožok so slaninou 70g"), _row(ROZOK, "81", "Zemiaková placka 90g")]
    for hours in (4, 3):
        _push(pg, v, hours_old=hours)
        codex_sync.run(pg, _cfg())
    assert _pick_card(pg) == "80"
    assert "jej údaje ostanú (ten istý výrobok)" in _review_reason(pg, "orders", ROZOK)


def test_a_dl_carrier_review_points_at_the_dl_curated_fields(pg):
    """Review 28 🔵 (pins the per-catalog pointer): a DL carrier-change review names doplnok /
    hmotnosť / cena, not the orders alias."""
    _seed_catalogs(pg)
    dl_snapshot.upsert_dl_catalog_card(pg, BAGETA, "Bageta stará", doplnok="", mass=None,
                                       sklad="1", cena=None)
    dl_snapshot.dl_rebuild_from_overrides(pg)
    _push(pg, V1 + [_row(BAGETA, "88", "Bageta šunková 120g")], hours_old=6)
    codex_sync._record_history(pg)
    v2 = [dict(r, code=BAGETA) if r["card_code"] == "27" else r for r in V1] + [
        _row(BAGETA_ELSE, "88", "Bageta šunková 120g")]
    for hours in (5, 4):
        _push(pg, v2, hours_old=hours)
        codex_sync.run(pg, _cfg())
    assert "jej doplnok / hmotnosť / cena (Produkty)" in _review_reason(pg, "dl", BAGETA)


# --- review 29: ONE rule for an unbound number, whatever the number of carriers now ----------

def test_a_duplicate_carrier_never_turns_a_drift_review_into_a_binding(pg):
    """Review 29 🟡: the drift-renamed unbound ROZOK (named like the pagáč that took the code
    over from the rožok) — a duplicate carrier 80 shows up on ONE list: the multi-carrier rule
    bound it to the pagáč by name (no history rules), and the first apply then renumbered it to
    the pagáč's next code with the rožok's alias and wordings. Still a human's call."""
    _seed_catalogs(pg)
    _seed_memory(pg)
    _push(pg, V1, hours_old=6)
    codex_sync._record_history(pg)
    _push(pg, _reused(V1), hours_old=5)
    codex_sync.run(pg, _cfg(apply=False))
    _drift_click(pg, ROZOK, "Pagáč syrový 60g")
    _push(pg, _reused(V1) + [_row(ROZOK, "80", "Bageta šunková 120g")], hours_old=4)
    codex_sync.run(pg, _cfg(apply=False))
    assert _binding(pg, ROZOK) is None
    assert "27" in _review_reason(pg, "orders", ROZOK)
    for hours in (3, 2):
        _push(pg, _reused(V1, pagac_code=PAGAC_W), hours_old=hours)
        codex_sync.run(pg, _cfg())
    assert PAGAC_W not in _orders(pg) and _binding(pg, ROZOK) is None
    assert set(_gtins(pg, "item_memory")) == {ROZOK}


def test_cards_seeded_together_never_count_as_a_take_over(pg):
    """Review 29 🔵 (pins the tie rule): ROZOK sat on cards 27 and 28 in the pre-deploy list
    (the migration seed — one `first_seen` for both); 28 leaves — our rožok, named like 27, is
    bound to 27 with no review (neither took the code over from the other)."""
    _seed_catalogs(pg)
    _push(pg, V1 + [_row(ROZOK, "28", "Bageta šunková 120g")], hours_old=6)
    codex_sync._record_history(pg)
    for hours in (5, 4):
        _push(pg, V1, hours_old=hours)
        codex_sync.run(pg, _cfg(apply=False))
    assert _binding(pg, ROZOK)[0] == "27"
    assert not _review_reason(pg, "orders", ROZOK)


def test_a_take_over_review_names_who_carries_the_code_now(pg):
    """Review 29 🔵: our unbound ROZOK drift-named like the pagáč (79, which took ROZOK over
    from the rožok and moved on); card 80 carries ROZOK now — the review said the pagáč carried
    it „last" and never named 80, whose pick it then advised."""
    _seed_catalogs(pg)
    _push(pg, V1, hours_old=6)
    codex_sync._record_history(pg)
    _push(pg, _reused(V1), hours_old=5)
    codex_sync.run(pg, _cfg(apply=False))
    _drift_click(pg, ROZOK, "Pagáč syrový 60g")
    v3 = _reused(V1, pagac_code=PAGAC_W) + [_row(ROZOK, "80", "Bageta šunková 120g")]
    for hours in (4, 3):
        _push(pg, v3, hours_old=hours)
        codex_sync.run(pg, _cfg(apply=False))
    reason = _review_reason(pg, "orders", ROZOK)
    assert "teraz ho nesie karta CODEX 80" in reason and "pred ňou niesla karta CODEX 27" in reason


def test_a_code_no_card_carries_names_its_last_and_earlier_carriers(pg):
    """Review 29 🔵 (pins the no-carrier review text): ROZOK went 27 → the pagáč → nobody; our
    „Bageta stará" is named like neither — the review names the pagáč as the last carrier and
    the rožok before it."""
    _seed_catalogs(pg)
    snapshot.upsert_catalog_card(pg, ROZOK, "Bageta stará")
    snapshot.rebuild_from_overrides(pg)
    _push(pg, V1, hours_old=6)
    codex_sync._record_history(pg)
    _push(pg, _reused(V1), hours_old=5)
    codex_sync.run(pg, _cfg(apply=False))
    for hours in (4, 3):
        _push(pg, _reused(V1, pagac_code=PAGAC_W), hours_old=hours)
        codex_sync.run(pg, _cfg(apply=False))
    reason = _review_reason(pg, "orders", ROZOK)
    assert "naposledy ho niesla karta CODEX 79, predtým 27" in reason


# --- review 30: a card arriving on a code no card carried; the footer's Kôš promise ------------

def test_a_card_arriving_on_a_carrierless_code_never_takes_our_number_on_one_list(pg):
    """Review 30 🟡: our DL „Bageta stará" on a code CODEX had no card for (a #467 „missing"
    card); card 90 — another product — shows up on that code: on ONE list the number was bound
    to it and renamed, and when 90 left again it was REMOVED (and a DL number whose code left
    CODEX never comes back from the Kôš). It waits, then a human decides; nothing removed."""
    _seed_catalogs(pg)
    dl_snapshot.upsert_dl_catalog_card(pg, BAGETA, "Bageta stará", doplnok="bageta", mass=None,
                                       sklad="1", cena=None)
    dl_snapshot.dl_rebuild_from_overrides(pg)
    for hours in (8, 7):
        _push(pg, V1, hours_old=hours)
        codex_sync.run(pg, _cfg())
    arrived = V1 + [_row(BAGETA, "90", "Pagáč nový 60g")]
    _push(pg, arrived, hours_old=6)
    codex_sync.run(pg, _cfg())
    assert "doteraz ho nenesla žiadna karta" in _waits(pg).get(("dl", BAGETA), "")
    _push(pg, arrived, hours_old=5)
    codex_sync.run(pg, _cfg())
    assert _dl(pg)[BAGETA]["name"] == "Bageta stará" and _binding(pg, BAGETA, "dl") is None
    assert "90" in _review_reason(pg, "dl", BAGETA)
    # review 31 🔵: one card on the code — the wording says so
    assert "nevieme, či je to naša karta" in _review_reason(pg, "dl", BAGETA)
    for hours in (4, 3):
        _push(pg, V1, hours_old=hours)
        codex_sync.run(pg, _cfg())
    assert BAGETA in _dl(pg), "never removed on the strength of a stranger's card"


def test_the_ops_footer_says_a_dl_number_whose_code_left_codex_does_not_come_back(pg):
    """Review 30 🔵: the footer promised every change can be undone in the Kôš — a DL number
    whose code left CODEX is refused there (#467); the alert says so when the plan has one."""
    _baseline(pg)
    gone = [r for r in V1 if r["card_code"] != "55"]
    for hours in (4, 3):
        _push(pg, gone, hours_old=hours)
        codex_sync.run(pg, _cfg())
    body = pg.execute("SELECT body_html FROM pending_alerts ORDER BY id DESC LIMIT 1"
                      ).fetchone()[0]
    assert "Kôš nevráti (#467)" in body
    v2 = [dict(r, name="Chlieb pšeničný voľný 1000g") if r["code"] == CHLIEB else r
          for r in gone]
    _push(pg, v2, hours_old=2)
    codex_sync.run(pg, _cfg())
    body = pg.execute("SELECT body_html FROM pending_alerts ORDER BY id DESC LIMIT 1"
                      ).fetchone()[0]
    assert "premenovan" in body or "Chlieb pšeničný voľný" in body
    assert "#467" not in body, "a rename-only plan has nothing the Kôš refuses"


# --- review 31: the seed is stable; round 30's paths are pinned ---------------------------------

def _last_alert(pg):
    return pg.execute("SELECT body_html FROM pending_alerts ORDER BY id DESC LIMIT 1"
                      ).fetchone()[0]


def test_a_list_older_than_the_history_never_moves_its_beginning(pg):
    """Review 31 🔵: the history's beginning (`seeded_at`, the oldest first sighting) moved back
    when a re-sent list OLDER than the seed added a pair — every card on a code since the seed
    then counted as „arrived" and a drifted unbound card waited / went to a human instead of
    following its card. A list older than the history's beginning records nothing."""
    _seed_catalogs(pg)
    _push(pg, V1, hours_old=6)
    codex_sync._record_history(pg)
    _push(pg, V1 + [_row("9990000000307", "95", "Starý kus 10g")], hours_old=9)
    assert codex_sync.run(pg, _cfg())["mode"] == "skipped"
    drift = [dict(r, name="Rožok slaninový 70g") if r["card_code"] == "27" else r for r in V1]
    _push(pg, drift, hours_old=5)
    codex_sync.run(pg, _cfg())
    assert _orders(pg)[ROZOK]["name"] == "Rožok slaninový 70g", "bound to its seed card"
    # review 32 (contract changed): the older list IS recorded — every accepted push is (round
    # 12: it was pickable) — its new pairs as seen at the history's beginning, which never moves
    seed = pg.execute("SELECT first_seen FROM codex_card_history WHERE card_code = '27' "
                      "AND code = %s", (ROZOK,)).fetchone()[0]
    assert pg.execute("SELECT min(first_seen) FROM codex_card_history").fetchone()[0] == seed
    assert pg.execute("SELECT first_seen FROM codex_card_history WHERE card_code = '95'"
                      ).fetchone()[0] == seed


def test_a_reuse_seen_only_in_a_list_older_than_the_history_still_opens_the_window(pg):
    """Review 32 🟡: a list older than the history's beginning was not recorded at all (round
    31) — but it was accepted and pickable: the pagáč's sighting on ROZOK in it no longer opened
    the reuse window, and a pagáč wording taught from it moved with the rožok's renumber.
    Recorded (clamped to the beginning), the row taught since is held for a human."""
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=7)
    assert codex_sync.run(pg, _cfg())["mode"] == "skipped"
    _teach(pg, "C60", "pagac syrovy", ROZOK, at=NOW - timedelta(hours=4.5))
    _push(pg, _reused(V1), hours_old=4)
    codex_sync.run(pg, _cfg())
    assert _gtin_of(pg, "C60") == ROZOK, "held on ROZOK, never moved with the rožok"
    assert "naučených priradení" in _review_reason(pg, "orders", ROZOK)


def test_the_footer_names_a_dl_renumber_whose_old_code_left_codex(pg):
    """Review 31 🔵 (pins `old_dead`): card 27 moved ROZOK → ROZOK_NEW and ROZOK left CODEX —
    the DL renumber retires ROZOK for good (the Kôš refuses it, 409): the footer says so; when
    another card still carries the old code (a reuse), the Kôš returns it — no such sentence."""
    _baseline(pg)
    _push(pg, _v2_renumbered(), hours_old=1)
    codex_sync.run(pg, _cfg())
    assert "Kôš nevráti (#467)" in _last_alert(pg)
    aid = pg.execute("SELECT id FROM audit_log WHERE table_name = 'dl_catalog_overrides' AND "
                     "row_id = %s AND action = 'delete' ORDER BY id DESC LIMIT 1",
                     (ROZOK,)).fetchone()[0]
    try:
        audit.restore(pg, aid, by="sklad")
        raise AssertionError("the Kôš returned a DL number whose code left CODEX")
    except audit.RestoreError as e:
        assert e.status == 409


def test_the_footer_stays_quiet_for_a_reused_old_code(pg):
    """Review 31 🔵: a renumber whose old code another card carries now — the Kôš can return
    the DL number, the footer promises nothing it cannot keep."""
    _baseline(pg)
    _push(pg, _reused(V1), hours_old=1)
    codex_sync.run(pg, _cfg())
    assert "#467" not in _last_alert(pg)


def test_the_footer_stays_quiet_for_an_orders_only_removal(pg):
    """Review 31 🔵: CHLIEB is ours in the orders catalog only — its removal is Kôš-restorable."""
    _baseline(pg)
    gone = [r for r in V1 if r["card_code"] != "31"]
    for hours in (4, 3):
        _push(pg, gone, hours_old=hours)
        codex_sync.run(pg, _cfg())
    body = _last_alert(pg)
    assert CHLIEB in body and "#467" not in body


def test_a_blocked_plan_with_a_dl_removal_carries_the_footer(pg):
    """Review 31 🔵: the blocked alert lists the would-be changes — with the same footer."""
    _baseline(pg)
    gone = [r for r in V1 if r["card_code"] != "55"]
    _push(pg, gone, hours_old=4)
    codex_sync.run(pg, _cfg())
    both = [dict(r, code=ROZOK_NEW) if r["card_code"] == "27" else r for r in gone]
    _push(pg, both, hours_old=3)
    assert codex_sync.run(pg, _cfg(codex_sync_max_code_changes=1))["mode"] == "blocked"
    assert "Kôš nevráti (#467)" in _last_alert(pg)


def test_a_card_codex_never_had_is_never_touched(pg):
    """Review 31 🔵 (pins „no history → never touched"): our DL card on a code no CODEX card
    ever carried (a #467 „missing" card) — no review, no wait, list after list."""
    _seed_catalogs(pg)
    dl_snapshot.upsert_dl_catalog_card(pg, BAGETA, "Bageta stará", doplnok="", mass=None,
                                       sklad="1", cena=None)
    dl_snapshot.dl_rebuild_from_overrides(pg)
    for hours in (6, 5, 4):
        _push(pg, V1, hours_old=hours)
        codex_sync.run(pg, _cfg())
    assert not _review_reason(pg, "dl", BAGETA) and ("dl", BAGETA) not in _waits(pg)


def test_the_first_post_deploy_list_waits_for_a_card_new_on_its_code(pg):
    """Review 31 🔵 (pins the first sync after deploy): no synced list yet, a card arrives on a
    code the seed list had no card on — our unbound number waits."""
    _seed_catalogs(pg)
    dl_snapshot.upsert_dl_catalog_card(pg, BAGETA, "Bageta stará", doplnok="", mass=None,
                                       sklad="1", cena=None)
    dl_snapshot.dl_rebuild_from_overrides(pg)
    _first_post_deploy(pg, V1 + [_row(BAGETA, "90", "Bageta stará")], runs=1)
    assert "doteraz ho nenesla" in _waits(pg).get(("dl", BAGETA), "")
    assert _binding(pg, BAGETA, "dl") is None


def test_a_card_arriving_beside_a_seed_carrier_never_delays_ours(pg):
    """Review 31 🔵 (pins `all(...)`): card 80 arrives on ROZOK beside card 27, which was there
    at the seed — our rožok is decided on that list (bound to 27)."""
    _first_post_deploy(pg, V1 + [_row(ROZOK, "80", "Bageta šunková 120g")], runs=1)
    assert _binding(pg, ROZOK)[0] == "27"


def test_a_card_arriving_on_a_code_that_had_carriers_is_a_carrier_change(pg):
    """Review 32 🔵 (pins `not earlier`): cards 90 and 91 carried BAGETA at the seed and left;
    card 92 arrives later — a carrier CHANGE (the leavers are long gone), never the
    carrier-less arrival wait: our DL number named like none of them goes to a human on that
    list."""
    _seed_catalogs(pg)
    dl_snapshot.upsert_dl_catalog_card(pg, BAGETA, "Bageta stará", doplnok="", mass=None,
                                       sklad="1", cena=None)
    dl_snapshot.dl_rebuild_from_overrides(pg)
    _push(pg, V1 + [_row(BAGETA, "90", "Bageta šunková 120g"),
                    _row(BAGETA, "91", "Bageta cesnaková 120g")], hours_old=6)
    codex_sync._record_history(pg)
    for hours in (5, 4):
        _push(pg, V1, hours_old=hours)
        codex_sync.run(pg, _cfg(apply=False))
    _push(pg, V1 + [_row(BAGETA, "92", "Pagáč nový 60g")], hours_old=3)
    codex_sync.run(pg, _cfg(apply=False))
    assert ("dl", BAGETA) not in _waits(pg)
    assert "92" in _review_reason(pg, "dl", BAGETA)


# --- review 33: a sighting older than the history is no evidence of who our number is -------

def _missing_bageta(pg):
    """Our DL card BAGETA with curated data — a #467 „missing" card: no CODEX card carries its
    code in any list we watched."""
    _seed_catalogs(pg)
    dl_snapshot.upsert_dl_catalog_card(pg, BAGETA, "Bageta stará", doplnok="moja", mass=0.12,
                                       sklad="1", cena=0.5)
    dl_snapshot.dl_rebuild_from_overrides(pg)


def _stredisko_1_beginning(pg):
    return pg.execute("SELECT min(first_seen) FROM codex_card_history WHERE stredisko = 1"
                      ).fetchone()[0]


def test_a_card_seen_only_before_the_history_began_never_removes_a_missing_card(pg):
    """Review 33 🟡: a list OLDER than the history's beginning showed card 90 on BAGETA. Its
    pair is recorded at the beginning (review 32), so the „ONE card on the code since the
    history began" rule bound our missing DL card to 90 with no name check — and 90 being gone
    from every later list removed it for good (the Kôš refuses a DL number whose code left
    CODEX, #467). No list since the beginning showed a carrier: never touched."""
    _missing_bageta(pg)
    for hours in (5, 4.5):
        _push(pg, V1, hours_old=hours)
        assert codex_sync.run(pg, _cfg())["mode"] == "apply"
    _push(pg, V1 + [_row(BAGETA, "90", "Pagáč nový 60g")], hours_old=7)
    assert codex_sync.run(pg, _cfg())["mode"] == "skipped"
    _push(pg, V1, hours_old=4)
    codex_sync.run(pg, _cfg())
    assert BAGETA in _dl(pg), "our missing card was removed on a pre-history sighting"
    assert _binding(pg, BAGETA, "dl") is None
    assert not _review_reason(pg, "dl", BAGETA) and ("dl", BAGETA) not in _waits(pg)


def test_a_missing_card_never_follows_a_card_seen_only_before_the_history_began(pg):
    """Review 33 🟡: card 90 (seen on BAGETA only in a list older than the beginning) now
    carries PAGAC_W — our missing DL card was bound to it and renumbered there, our doplnok /
    mass / cena carried onto another product. Never touched."""
    _missing_bageta(pg)
    _push(pg, V1, hours_old=5)
    codex_sync.run(pg, _cfg())
    _push(pg, V1 + [_row(BAGETA, "90", "Pagáč nový 60g")], hours_old=7)
    codex_sync.run(pg, _cfg())
    _push(pg, V1 + [_row(PAGAC_W, "90", "Pagáč nový 60g")], hours_old=4)
    codex_sync.run(pg, _cfg())
    dl = _dl(pg)
    assert BAGETA in dl and PAGAC_W not in dl, "our missing card followed a pre-history card"
    assert dl[BAGETA]["doplnok"] == "moja"
    assert _binding(pg, BAGETA, "dl") is None
    assert _last_report(pg)["renumbers"] == []


def test_an_older_list_is_recorded_at_the_oldest_first_sighting(pg):
    """Review 33 🔵 (pins `min`): the history's beginning is its OLDEST first sighting — with
    two distinct ones, an older list's new pair is recorded at the oldest, never the newest."""
    _seed_catalogs(pg)
    _push(pg, V1, hours_old=6)
    codex_sync.run(pg, _cfg())
    _push(pg, V1 + [_row(PAGAC_W, "91", "Pagáč nový 60g")], hours_old=5)
    codex_sync.run(pg, _cfg())
    seed = _stredisko_1_beginning(pg)
    _push(pg, V1 + [_row(BAGETA, "95", "Starý kus 10g")], hours_old=9)
    codex_sync.run(pg, _cfg())
    assert pg.execute("SELECT first_seen FROM codex_card_history WHERE card_code = '95'"
                      ).fetchone()[0] == seed == NOW - timedelta(hours=6)


def test_an_older_list_never_sets_back_a_name_last_seen_at_the_beginning(pg):
    """Review 33 🔵 (pins: only first_seen is clamped, last_seen stays the list's own age):
    card 27 seen once, at the beginning, under its newer name — an older list with its old
    name never sets it back (review 13's rule)."""
    _seed_catalogs(pg)
    renamed = [dict(r, name="Rožok slaninový 70g") if r["card_code"] == "27" else r for r in V1]
    _push(pg, renamed, hours_old=5)
    codex_sync.run(pg, _cfg())
    _push(pg, V1, hours_old=7)
    codex_sync.run(pg, _cfg())
    assert _history_name(pg, "27", ROZOK) == "Rožok slaninový 70g"


def test_another_strediskos_older_history_never_moves_stredisko_1s_beginning(pg, caplog):
    """Review 33 🔵: `Codex.seeded_at` is stredisko 1's beginning — the clamp is per stredisko:
    with stredisko 4 seen first, an older list's new stredisko-1 pair is recorded at stredisko
    1's beginning, never at stredisko 4's earlier one (that moved `seeded_at` back)."""
    _seed_catalogs(pg)
    _push(pg, [_row(BAGETA, "400", "Bageta cestovná 120g", sklad=4, stredisko=4)], hours_old=8)
    codex_sync._record_history(pg)
    _push(pg, V1, hours_old=6)
    codex_sync._record_history(pg)
    seed = _stredisko_1_beginning(pg)
    _push(pg, V1 + [_row(BAGETA, "95", "Starý kus 10g")], hours_old=7)
    with caplog.at_level(logging.WARNING, logger="orders.codex_sync"):
        codex_sync._record_history(pg)
    assert _stredisko_1_beginning(pg) == seed == NOW - timedelta(hours=6)
    # review 34 🔵: the clamp is per stredisko — so is its warning (stredisko 4 began earlier)
    assert any("older than the history" in r.getMessage() for r in caplog.records)


# --- review 34: a card seen before the history began AND again since is no seed card -----

PAGAC_ON_BAGETA = _row(BAGETA, "90", "Pagáč nový 60g")


def test_a_card_seen_before_the_history_began_and_again_since_is_an_arrival(pg):
    """Review 34 🟡: card 90 was on our missing card's code in a list OLDER than the history's
    beginning, then again in a list since. Its clamped first sighting (the beginning) made it
    „the ONE card on the code since the history began": our DL card was bound and renamed at
    once — no one-list wait, no name check. It ARRIVED (no list since the beginning had a
    carrier): it waits one list, then a human decides — exactly as without the older list."""
    _missing_bageta(pg)
    _push(pg, V1, hours_old=5)
    codex_sync.run(pg, _cfg())
    _push(pg, V1 + [PAGAC_ON_BAGETA], hours_old=7)
    assert codex_sync.run(pg, _cfg())["mode"] == "skipped"
    _push(pg, V1 + [PAGAC_ON_BAGETA], hours_old=4)
    codex_sync.run(pg, _cfg())
    assert _binding(pg, BAGETA, "dl") is None
    assert "doteraz ho nenesla" in _waits(pg).get(("dl", BAGETA), "")
    assert _dl(pg)[BAGETA]["name"] == "Bageta stará"
    _push(pg, V1 + [PAGAC_ON_BAGETA], hours_old=3)
    codex_sync.run(pg, _cfg())
    assert _binding(pg, BAGETA, "dl") is None and _review_reason(pg, "dl", BAGETA)
    assert _dl(pg)[BAGETA]["name"] == "Bageta stará"


def test_a_card_seen_before_the_history_began_and_again_since_never_removes_ours(pg):
    """Review 34 🟡: the same card 90 then leaves CODEX — the binding the clamped sighting made
    removed our missing DL card for good (the Kôš refuses it, #467). Never bound → never
    removed."""
    _missing_bageta(pg)
    _push(pg, V1, hours_old=5)
    codex_sync.run(pg, _cfg())
    _push(pg, V1 + [PAGAC_ON_BAGETA], hours_old=7)
    codex_sync.run(pg, _cfg())
    _push(pg, V1 + [PAGAC_ON_BAGETA], hours_old=4)
    codex_sync.run(pg, _cfg())
    for hours in (3, 2):
        _push(pg, V1, hours_old=hours)
        codex_sync.run(pg, _cfg())
    assert BAGETA in _dl(pg), "removed on a card that was never on its code since the beginning"
    assert _last_report(pg)["removals"] == []


def test_a_sighting_before_the_history_began_never_hides_a_take_over(pg):
    """Review 34 🟡: card 27 (our rožok) leaves ROZOK, pagáč 90 carries it — and 90 was on ROZOK
    in a list older than the history's beginning. The drift button renamed our unbound rožok
    to the pagáč: `_took_over` read 90's clamped first sighting (the beginning) and saw it
    „seeded together" with 27 — bound to the pagáč, the rožok alias kept, no human. 90 took
    the code over from 27 since the beginning: a human decides (round 9's rule)."""
    _seed_catalogs(pg)
    _push(pg, V1, hours_old=6)
    codex_sync._record_history(pg)
    pagac = _row(ROZOK, "90", "Pagáč nový 60g")
    _push(pg, V1 + [pagac], hours_old=8)
    assert codex_sync.run(pg, _cfg(apply=False))["mode"] == "skipped"
    gone = [r for r in V1 if r["card_code"] != "27"] + [pagac]
    _push(pg, gone, hours_old=5)
    codex_sync.run(pg, _cfg(apply=False))
    _drift_click(pg, ROZOK, "Pagáč nový 60g")
    _push(pg, gone, hours_old=4)
    codex_sync.run(pg, _cfg(apply=False))
    assert _binding(pg, ROZOK) is None
    assert "27" in _review_reason(pg, "orders", ROZOK)


class _Explaining:
    """A connection whose INSERT statements are EXPLAINed (client-side bound) before they run."""

    def __init__(self, conn):
        self.conn, self.plans = conn, []

    def execute(self, sql, params=None):
        if "INSERT INTO codex_card_history" in sql:
            cur = psycopg.ClientCursor(self.conn)
            self.plans.append("\n".join(r[0] for r in cur.execute("EXPLAIN " + sql,
                                                                  params).fetchall()))
        return self.conn.execute(sql, params)


def test_the_history_reads_each_strediskos_beginning_once_per_list(pg):
    """Review 34 🟡: the per-stredisko beginning was a correlated subquery — evaluated once per
    (stredisko, card, code) group, each scanning that stredisko's history: ~1000x slower on a
    production-size list (3.5 s vs 5 ms for 6000 cards), twice per push, inside the push's
    request and under the sync's locks. Computed once per list: no SubPlan in the plan."""
    _seed_catalogs(pg)
    _push(pg, V1, hours_old=6)
    codex_sync._record_history(pg)
    _push(pg, V1, hours_old=5)
    spy = _Explaining(pg)
    codex_sync_list.update_history(spy, NOW - timedelta(hours=5))
    assert spy.plans and not any("SubPlan" in p for p in spy.plans), spy.plans


def _seen_since(pg, card):
    return pg.execute("SELECT seen_since FROM codex_card_history WHERE stredisko = 1 AND "
                      "card_code = %s", (card,)).fetchone()[0]


def test_seen_since_is_the_first_list_since_the_beginning_that_showed_the_pair(pg):
    """Review 34 (pins `seen_since`): a list of the beginning's own age is since the beginning;
    a pair seen only in an older list has none; seen again since → that list, and a later
    re-sent (older) list never moves it — a later value only errs towards „arrived"."""
    _seed_catalogs(pg)
    _push(pg, V1, hours_old=6)
    codex_sync._record_history(pg)
    _push(pg, V1 + [_row(BAGETA, "90", "Pagáč nový 60g")], hours_old=6)
    codex_sync._record_history(pg)
    assert _seen_since(pg, "90") == NOW - timedelta(hours=6)
    late = _row(PAGAC_W, "92", "Bageta cesnaková 120g")
    _push(pg, V1 + [late], hours_old=8)
    codex_sync._record_history(pg)
    assert _seen_since(pg, "92") is None
    for hours in (4, 5):
        _push(pg, V1 + [late], hours_old=hours)
        codex_sync._record_history(pg)
    assert _seen_since(pg, "92") == NOW - timedelta(hours=4)


# --- review 35: a card seen before the history began is still ANOTHER card on the code ------

# at the history's beginning card 27 (our rožok) already carries ROZOK_NEW and the pagáč 90
# carries ROZOK — the #478 incident shape, before the deploy
MOVED_BEFORE = ([r for r in V1 if r["card_code"] != "27"]
                + [_row(ROZOK, "90", "Pagáč nový 60g"), _row(ROZOK_NEW, "27", "Rožok so slaninou 70g")])


def _rozok_before_we_watched(pg, lists):
    """The beginning = MOVED_BEFORE; then a list OLDER than it shows card 27 on ROZOK (V1);
    then `lists` (list, hours old) are synced."""
    _seed_catalogs(pg)
    _push(pg, MOVED_BEFORE, hours_old=6)
    codex_sync._record_history(pg)
    _push(pg, V1, hours_old=8)
    assert codex_sync.run(pg, _cfg())["mode"] == "skipped"
    for cards, hours in lists:
        _push(pg, cards, hours_old=hours)
        codex_sync.run(pg, _cfg())


def test_a_card_on_the_code_before_the_history_began_blocks_the_one_card_rule(pg):
    """Review 35 🟡: the pagáč 90 is the ONE card on ROZOK since the history began — but card
    27 (our rožok) carried ROZOK in a list older than that. Round 33 dropped 27 from the
    candidates AND from the count: our unbound rožok was bound to the pagáč with no name check
    and renamed (round 1's 🔴). Another card ever on the code → the name rule / a human; the
    review names the card seen before we watched."""
    _rozok_before_we_watched(pg, [(MOVED_BEFORE, 5), (MOVED_BEFORE, 4)])
    assert _binding(pg, ROZOK) is None and _binding(pg, ROZOK, "dl") is None
    assert _orders(pg)[ROZOK]["name"] == "Rožok so slaninou 70g"
    reason = _review_reason(pg, "orders", ROZOK)
    assert "staršom ako začiatok sledovania" in reason and "27" in reason, reason
    assert "sa volá ako karta CODEX 27" in reason, reason
    # review 36: renamed like the pagáč it would be bound to 90, which took the code over from
    # 27 — another human; a way out that does not work is never offered
    assert "na „Pagáč nový 60g“" not in reason, reason


def test_a_card_on_the_code_before_the_history_began_never_lets_its_reuser_remove_ours(pg):
    """Review 35 🟡: as above, then the pagáč 90 leaves CODEX — bound to it, our rožok was
    REMOVED from both catalogs (the DL number for good, #467) while its card 27 lives on."""
    gone = [r for r in MOVED_BEFORE if r["card_code"] != "90"]
    _rozok_before_we_watched(pg, [(gone, 5), (gone, 4)])
    assert ROZOK in _orders(pg) and ROZOK in _dl(pg)
    assert _last_report(pg)["removals"] == []
    assert _binding(pg, ROZOK) is None


def test_an_arrival_wait_names_a_card_seen_before_the_history_began(pg):
    """Review 35 🔵: card 91 carried our missing card's code only in a list older than the
    beginning; card 90 arrives — „doteraz ho nenesla žiadna karta" was untrue: the wait says
    no card carried it since we watched, and names 91."""
    _missing_bageta(pg)
    _push(pg, V1, hours_old=5)
    codex_sync.run(pg, _cfg())
    _push(pg, V1 + [_row(BAGETA, "91", "Bageta cesnaková 120g")], hours_old=7)
    codex_sync.run(pg, _cfg())
    _push(pg, V1 + [PAGAC_ON_BAGETA], hours_old=4)
    codex_sync.run(pg, _cfg())
    why = _waits(pg).get(("dl", BAGETA), "")
    assert "od začiatku sledovania ho nenesla" in why and "91" in why, why


# --- review 36: a card seen before the history began carried the code FIRST -----------------

def test_a_card_seen_before_the_history_began_reveals_a_take_over(pg):
    """Review 36 🔵: our rožok was renamed with the #467 drift button to the pagáč 90, the
    holder of ROZOK at the history's beginning — and a list older than the beginning shows
    card 27 (the rožok) on ROZOK. `_took_over` read 27's clamped first sighting (the
    beginning) as „seeded together" with 90: bound to the pagáč with the rožok's alias, and
    removed once the pagáč left CODEX. 27 carried the code FIRST: the pagáč took it over → a
    human (round 9's rule), nothing removed."""
    _seed_catalogs(pg)
    _drift_click(pg, ROZOK, "Pagáč nový 60g")
    _push(pg, MOVED_BEFORE, hours_old=6)
    codex_sync._record_history(pg)
    _push(pg, V1, hours_old=8)
    assert codex_sync.run(pg, _cfg())["mode"] == "skipped"
    for hours in (5, 4):
        _push(pg, MOVED_BEFORE, hours_old=hours)
        codex_sync.run(pg, _cfg())
    assert _binding(pg, ROZOK) is None
    assert "pred ňou niesla karta CODEX 27" in _review_reason(pg, "orders", ROZOK)
    gone = [r for r in MOVED_BEFORE if r["card_code"] != "90"]
    for hours in (3, 2):
        _push(pg, gone, hours_old=hours)
        codex_sync.run(pg, _cfg())
    assert ROZOK in _orders(pg) and _last_report(pg)["removals"] == []


def test_a_card_seen_before_the_history_began_and_back_since_still_came_first(pg):
    """Review 36 🔵: as above, but the rožok 27 is BACK on ROZOK beside the pagáč 90 — it has a
    first list since the beginning now, later than its clamped first sighting: it was on the
    code before we watched, so still before the pagáč (`Codex.before_watch`) → a human."""
    _seed_catalogs(pg)
    _drift_click(pg, ROZOK, "Pagáč nový 60g")
    _push(pg, MOVED_BEFORE, hours_old=6)
    codex_sync._record_history(pg)
    _push(pg, V1, hours_old=8)
    codex_sync.run(pg, _cfg())
    both = V1 + [_row(ROZOK, "90", "Pagáč nový 60g")]
    for hours in (5, 4):
        _push(pg, both, hours_old=hours)
        codex_sync.run(pg, _cfg())
    assert _binding(pg, ROZOK) is None
    assert "pred ňou niesla karta CODEX 27" in _review_reason(pg, "orders", ROZOK)


def test_a_review_naming_a_card_seen_before_the_history_began_speaks_of_several(pg):
    """Review 36 🔵 (pins the count): the pagáč 90 on the code now, the rožok 27 only before
    we watched, our name matches neither — the head names two cards, so „nesedí so žiadnou
    z nich", never the one-card wording."""
    _seed_catalogs(pg)
    _drift_click(pg, ROZOK, "Bageta stará")
    _push(pg, MOVED_BEFORE, hours_old=6)
    codex_sync._record_history(pg)
    _push(pg, V1, hours_old=8)
    codex_sync.run(pg, _cfg())
    for hours in (5, 4):
        _push(pg, MOVED_BEFORE, hours_old=hours)
        codex_sync.run(pg, _cfg())
    reason = _review_reason(pg, "orders", ROZOK)
    assert "nesedí so žiadnou z nich" in reason, reason


# --- review 37: a card never seen since the beginning is never „missing once" ---------------

def test_a_card_seen_only_before_the_history_began_is_never_missing_once(pg):
    """Review 37 🔵: card 27 carried ROZOK only in a list older than the beginning and is in no
    list since; our rožok drift-renamed to the pagáč 90 (the holder at the beginning). 27 was
    in no list since, yet the first sync after the deploy (no previous synced list) called it
    „v tomto zozname chýba (raz)" and waited a list — an untrue reason in the dry-run report
    the owner reads. A human decides on that list."""
    _seed_catalogs(pg)
    _drift_click(pg, ROZOK, "Pagáč nový 60g")
    no27 = [r for r in MOVED_BEFORE if r["card_code"] != "27"]
    _push(pg, no27, hours_old=6)
    codex_sync._record_history(pg)
    _push(pg, V1, hours_old=8)
    assert codex_sync.run(pg, _cfg())["mode"] == "skipped"
    _push(pg, no27, hours_old=5)
    codex_sync.run(pg, _cfg())
    assert ("orders", ROZOK) not in _waits(pg)
    assert "pred ňou niesla karta CODEX 27" in _review_reason(pg, "orders", ROZOK)
    assert _binding(pg, ROZOK) is None


# --- review 38: our unknown Kôš card of ANOTHER product under the new code ------------------

KOS_BAGETA = "9990000000253"   # synthetic: our bageta's code — freed in CODEX, then reused


BAGETA_TAUGHT = "Bageta cesnaková"          # the bageta's taught wording (a nástenka answer)
BAGETA_SHIPPED = "Bageta cesnaková veľká"    # a wording only its delivery history knows


def _kos_bageta(pg, *, taught=True, shipped=False, name="Bageta cesnaková 100g"):
    """Our bageta KOS_BAGETA (orders + DL) with its mapping rows (orders + DL), deleted (Kôš)
    and never bound — the warehouse deleted it before the deploy (the #467 cleanup of freed
    codes). Rows keyed exactly as the matcher keys them (review 39 🔵: a made-up key made the
    recall check vacuous). `name`: what it was called when deleted."""
    snapshot.upsert_catalog_card(pg, KOS_BAGETA, name, alias="bageta cesnak")
    snapshot.rebuild_from_overrides(pg)
    dl_snapshot.upsert_dl_catalog_card(pg, KOS_BAGETA, name,
                                       doplnok="bageta", mass=0.1, sklad="1", cena=0.5)
    dl_snapshot.dl_rebuild_from_overrides(pg)
    for source, raw, on in (("human", BAGETA_TAUGHT, taught),
                            ("ship", BAGETA_SHIPPED, shipped)):
        if on:
            pg.execute(
                "INSERT INTO dl_item_memory (supplier_ean, item_key, item_raw, gtin, card, "
                "delivered_on, cnt, source, created_at) VALUES ('S9', %s, %s, %s, 'Bageta', "
                "%s, 1, %s, %s)",
                (memory.item_key(raw), raw, KOS_BAGETA, date(2026, 9, 1), source, _BEFORE))
            pg.execute(
                "INSERT INTO item_memory (customer_ean, item_key, item_raw, gtin, card, "
                "delivered_on, source, created_at) VALUES ('C9', %s, %s, %s, 'Bageta', %s, %s, "
                "%s)", (memory.item_key(raw), raw, KOS_BAGETA, date(2026, 9, 1), source, _BEFORE))
    snapshot.retire_catalog_card(pg, KOS_BAGETA)
    snapshot.rebuild_from_overrides(pg)
    dl_snapshot.retire_dl_catalog_card(pg, KOS_BAGETA)
    dl_snapshot.dl_rebuild_from_overrides(pg)


# card 86 (the bageta) left CODEX, CODEX gave its freed code to card 27 (our rožok)
BAGETA_REUSED = ([r for r in V1 if r["code"] != ROZOK]
                 + [_row(KOS_BAGETA, "27", "Rožok so slaninou 70g")])


def _kos_bageta_lists(pg, lists):
    _seed_catalogs(pg)
    _push(pg, V1 + [_row(KOS_BAGETA, "86", "Bageta cesnaková 100g")], hours_old=6)
    assert codex_sync.run(pg, _cfg())["mode"] == "apply"
    for cards, hours in lists:
        _push(pg, cards, hours_old=hours)
        codex_sync.run(pg, _cfg())


def test_an_unknown_kos_card_of_another_product_is_overwritten_and_its_rows_go_to_a_human(pg):
    """Review 38 🟡: card 27 (our rožok) moved to the bageta's freed code; our Kôš bageta
    under it was never bound — the renumber restored it and its taught rows were adopted by
    the rožok SILENTLY. Review 39 🟡 (contract changed): blocking the renumber instead held
    every order line of the rožok on a code CODEX no longer has (orders recall never reads the
    catalog — the block protected nothing) and led the warehouse to a pick that restored the
    bageta's data. The rožok follows its card (CODEX's truth, OUR data over the dead card's);
    the bageta's taught rows, adopted as they sit, go to a human in both catalogs (the
    review-11 rule)."""
    _kos_bageta(pg)
    _kos_bageta_lists(pg, [(BAGETA_REUSED, 5)])
    dl, orders = _dl(pg), _orders(pg)
    assert ROZOK not in dl and ROZOK not in orders
    assert (dl[KOS_BAGETA]["name"], dl[KOS_BAGETA]["doplnok"], dl[KOS_BAGETA]["mass"],
            dl[KOS_BAGETA]["cena"]) == ("Rožok so slaninou 70g", "rožok slanina", 0.07, 0.35)
    assert (orders[KOS_BAGETA]["name"], orders[KOS_BAGETA]["alias"]) == (
        "Rožok so slaninou 70g", "rozok slanina")
    for scope in ("dl", "orders"):
        reason = _review_reason(pg, scope, ROZOK)
        assert "„Bageta cesnaková 100g“" in reason and KOS_BAGETA in reason, (scope, reason)
        assert "iný výrobok" in reason, reason
        # pins `drift = took if named_like`: named unlike card 27, never a drift-button name
        assert "Prevziať názov" not in reason, reason


def test_an_unknown_kos_card_with_delivery_history_only_is_said_never_silent(pg):
    """Review 38: no taught rows — nothing for a human to fix, the renumber goes through; the
    other product's delivery history on the code stays as history — said in the report
    (review 12's rule), never silent."""
    _kos_bageta(pg, taught=False, shipped=True)
    _kos_bageta_lists(pg, [(BAGETA_REUSED, 5)])
    assert ROZOK not in _dl(pg) and _dl(pg)[KOS_BAGETA]["name"] == "Rožok so slaninou 70g"
    for scope in ("dl", "orders"):     # review 40 🔵: the orders note was unpinned
        holds = [h for h in _last_report(pg)["holds"] if h["scope"] == scope]
        assert holds and "Bageta cesnaková 100g" in holds[0]["why"], (scope, holds)


def test_a_kos_number_whose_card_left_codex_is_overwritten_when_its_code_is_reused(pg):
    """Review 40 🟡: our bageta, bound to card 86, removed by the sync itself when 86 left
    CODEX — its code then reused for card 27 (our rožok). Known as ANOTHER card, it blocked the
    renumber (a pick advised; every rožok line on the dead code held as codex_missing) while
    the same Kôš card unknown was overwritten — the treatment review 39 chose. Card 86 is gone
    for good: no two live products to keep apart. Restored with OUR data, bound to 27, its
    taught rows to a human."""
    _seed_catalogs(pg)
    snapshot.upsert_catalog_card(pg, KOS_BAGETA, "Bageta cesnaková 100g", alias="bageta cesnak")
    snapshot.rebuild_from_overrides(pg)
    dl_snapshot.upsert_dl_catalog_card(pg, KOS_BAGETA, "Bageta cesnaková 100g",
                                       doplnok="bageta", mass=0.1, sklad="1", cena=0.5)
    dl_snapshot.dl_rebuild_from_overrides(pg)
    pg.execute(
        "INSERT INTO dl_item_memory (supplier_ean, item_key, item_raw, gtin, card, delivered_on, "
        "cnt, source, created_at) VALUES ('S9', %s, %s, %s, 'Bageta', %s, 1, 'human', %s)",
        (memory.item_key(BAGETA_TAUGHT), BAGETA_TAUGHT, KOS_BAGETA, date(2026, 9, 1), _BEFORE))
    _push(pg, V1 + [_row(KOS_BAGETA, "86", "Bageta cesnaková 100g")], hours_old=7)
    codex_sync.run(pg, _cfg())
    assert _binding(pg, KOS_BAGETA, "dl")[0] == "86"
    for hours in (6, 5):                       # 86 leaves CODEX: the sync removes the bageta
        _push(pg, V1, hours_old=hours)
        codex_sync.run(pg, _cfg())
    assert KOS_BAGETA not in _dl(pg) and _binding(pg, KOS_BAGETA, "dl")[:2] == ("86", False)
    _push(pg, BAGETA_REUSED, hours_old=4)
    codex_sync.run(pg, _cfg())
    dl = _dl(pg)
    assert ROZOK not in dl, "blocked on a Kôš number whose card left CODEX for good"
    assert (dl[KOS_BAGETA]["name"], dl[KOS_BAGETA]["doplnok"], dl[KOS_BAGETA]["mass"]) == (
        "Rožok so slaninou 70g", "rožok slanina", 0.07)
    assert _binding(pg, KOS_BAGETA, "dl")[:2] == ("27", True)
    reason = _review_reason(pg, "dl", ROZOK)
    assert "„Bageta cesnaková 100g“" in reason and "iný výrobok" in reason, reason


def test_a_pick_that_restored_another_products_kos_card_is_reset_and_its_rows_reviewed(pg):
    """Review 40 🟡: during the dry-run the #467 hold asked the warehouse to pick card 27 at the
    bageta's freed code — the picker restored our unknown Kôš bageta AS IT WAS (#477). The
    applied sync then renamed it to the rožok but kept the bageta's doplnok / mass / cena (a
    merge fills only blanks), retired our rožok into it and adopted the bageta's taught rows
    — silently, while the dry-run had promised our data + a review. A pick restoring another
    product's Kôš card is reset like a re-pick of another product; its taught rows to a
    human."""
    _kos_bageta(pg)
    _seed_catalogs(pg)
    _push(pg, V1 + [_row(KOS_BAGETA, "86", "Bageta cesnaková 100g")], hours_old=6)
    codex_sync.run(pg, _cfg(apply=False))
    _push(pg, BAGETA_REUSED, hours_old=5)
    codex_sync.run(pg, _cfg(apply=False))
    card_guard.add_from_codex(pg, "dl", KOS_BAGETA, actor="sklad")
    assert _dl(pg)[KOS_BAGETA]["doplnok"] == "bageta", "the picker restores it as it was"
    _push(pg, BAGETA_REUSED, hours_old=4)
    codex_sync.run(pg, _cfg())
    dl = _dl(pg)
    assert ROZOK not in dl
    assert (dl[KOS_BAGETA]["name"], dl[KOS_BAGETA]["doplnok"], dl[KOS_BAGETA]["mass"],
            dl[KOS_BAGETA]["cena"]) == ("Rožok so slaninou 70g", "rožok slanina", 0.07, 0.35)
    reason = _review_reason(pg, "dl", KOS_BAGETA)
    assert "„Bageta cesnaková 100g“" in reason and "naučených priradení" in reason, reason


def test_a_kos_card_named_like_the_card_that_took_its_code_over_is_no_proof(pg):
    """Review 39 🟡: our Kôš bageta carries card 27's NAME (the #467 drift button offers the
    code's holder — round 9's rule) while card 27 took its code over from the bageta 86. The
    name rule restored it as card 27's product with no review — the bageta's taught rows
    adopted silently. Named like a card that took the code over from another product is no
    proof (`_took_over`, as in `_by_history`): its rows go to a human."""
    _kos_bageta(pg, name="Rožok so slaninou 70g")
    _kos_bageta_lists(pg, [(BAGETA_REUSED, 5)])
    assert ROZOK not in _dl(pg) and _dl(pg)[KOS_BAGETA]["doplnok"] == "rožok slanina"
    reason = _review_reason(pg, "dl", ROZOK)
    assert "pred ňou niesla karta CODEX 86" in reason and KOS_BAGETA in reason, reason


def test_a_nameless_kos_card_is_never_called_another_product(pg):
    """Review 39 🔵: a bare Kôš marker no snapshot names — the products were never compared:
    the review says we do not know what it was, never „iný výrobok"."""
    _seed_catalogs(pg)
    pg.execute("INSERT INTO dl_catalog_overrides (gtin, name, retired, deleted_at, updated_at) "
               "VALUES (%s, '', true, now(), now())", (KOS_BAGETA,))
    pg.execute(
        "INSERT INTO dl_item_memory (supplier_ean, item_key, item_raw, gtin, card, delivered_on, "
        "cnt, source, created_at) VALUES ('S9', %s, %s, %s, 'Bageta', %s, 1, 'human', %s)",
        (memory.item_key(BAGETA_TAUGHT), BAGETA_TAUGHT, KOS_BAGETA, date(2026, 9, 1), _BEFORE))
    _push(pg, V1 + [_row(KOS_BAGETA, "86", "Bageta cesnaková 100g")], hours_old=6)
    codex_sync.run(pg, _cfg())
    _push(pg, BAGETA_REUSED, hours_old=5)
    codex_sync.run(pg, _cfg())
    reason = _review_reason(pg, "dl", ROZOK)
    assert "nevieme, aký výrobok" in reason and "iný výrobok" not in reason, reason


KOS_ROZOK = "9990000000260"    # synthetic: an older number of our rožok, deleted


def test_an_unknown_kos_card_of_the_same_product_is_restored_with_its_rows(pg):
    """Review 38 (pins the name rule — a bare marker's name too): our older rožok number in
    the Kôš is a bare retirement marker (a snapshot-only card), named by the snapshot that had
    it — card 27's product: the renumber restores it, its taught rows are ours."""
    _seed_catalogs(pg)
    dl_snapshot._freeze(pg, [
        {"gtin": ROZOK, "name": "Rožok so slaninou 70g", "doplnok": "rožok slanina",
         "mass": 0.07, "sklad": "1", "cena": 0.35},
        {"gtin": KOS_ROZOK, "name": "Rožok so slaninou 70g", "doplnok": "", "mass": None,
         "sklad": "1", "cena": None},
    ], [])
    pg.execute(
        "INSERT INTO dl_item_memory (supplier_ean, item_key, item_raw, gtin, card, delivered_on, "
        "cnt, source, created_at) VALUES ('S9', 'rozok slaninovy', 'Rožok slaninový', %s, "
        "'Rožok', %s, 1, 'human', %s)", (KOS_ROZOK, date(2026, 9, 1), _BEFORE))
    assert dl_snapshot.retire_dl_catalog_card(pg, KOS_ROZOK)
    dl_snapshot.dl_rebuild_from_overrides(pg)
    assert pg.execute("SELECT name FROM dl_catalog_overrides WHERE gtin = %s",
                      (KOS_ROZOK,)).fetchone()[0] == ""
    _push(pg, V1, hours_old=6)
    codex_sync.run(pg, _cfg())
    moved = [dict(r, code=KOS_ROZOK) if r["card_code"] == "27" else r for r in V1]
    _push(pg, moved, hours_old=5)
    codex_sync.run(pg, _cfg())
    dl = _dl(pg)
    assert ROZOK not in dl and dl[KOS_ROZOK]["name"] == "Rožok so slaninou 70g"
    assert not _review_reason(pg, "dl", ROZOK)
    gtins = {c["gtin"] for c in dl_snapshot.dl_catalog_for_management(pg)}
    recalled = dl_memory.resolve(pg, "S9", "Rožok slaninový", catalog_gtins=gtins)
    assert recalled is not None and recalled.gtin == KOS_ROZOK
