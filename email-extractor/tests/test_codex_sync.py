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

from app import db
from app.board.services import audit
from app.config import Config
from app.httpapi import create_app
from app.orders import card_guard, codex_cards, codex_sync, dl_snapshot, snapshot

PG_DSN = os.environ.get("PG_TEST_DSN")
NOW = datetime.now(UTC)

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
    pg.execute(
        "INSERT INTO item_memory (customer_ean, item_key, item_raw, gtin, card, delivered_on, "
        "source) VALUES ('C1', 'rozok slanina', 'rožok slanina', %s, 'Rožok', %s, 'ship'), "
        "('C2', 'rozky so slaninou', 'rožky so slaninou', %s, 'Rožok', %s, 'human')",
        (gtin, date(2026, 9, 1), gtin, date(2026, 9, 2)))
    pg.execute(
        "INSERT INTO global_item_memory (item_key, item_raw, gtin, card, taught_by) "
        "VALUES ('slaninovy rozok', 'slaninový rožok', %s, 'Rožok', 'sklad')", (gtin,))
    pg.execute(
        "INSERT INTO dl_item_memory (supplier_ean, item_key, item_raw, gtin, card, "
        "delivered_on, cnt, source) VALUES ('S1', 'rozok slanina 70', 'Rožok slanina 70g', "
        "%s, 'Rožok', %s, 1, 'ship')", (gtin, date(2026, 9, 3)))


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
    _baseline(pg)
    _seed_memory(pg)
    snapshot.upsert_catalog_card(pg, ROZOK_NEW, "Rožok slaninový (vybraný z CODEXu)")
    snapshot.rebuild_from_overrides(pg)
    _push(pg, _v2_renumbered(), hours_old=1)
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

def _teach(pg, customer, key, gtin):
    pg.execute("INSERT INTO item_memory (customer_ean, item_key, item_raw, gtin, card, "
               "delivered_on, source) VALUES (%s, %s, %s, %s, 'x', %s, 'human')",
               (customer, key, key, gtin, date(2026, 9, 20)))


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
    _push(pg, [r for r in V1 if r["code"] != ROZOK], hours_old=3)
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

    def boom(conn, table, old, new, note):
        calls.append(table)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return real(conn, table, old, new, note)

    monkeypatch.setattr(codex_sync, "_rewrite_memory", boom)
    res = codex_sync.run_safely(pg, _cfg())
    assert res["mode"] == "error" and "boom" in res["error"]
    assert ROZOK in _orders(pg) and ROZOK_NEW not in _orders(pg)
    assert set(_gtins(pg, "item_memory")) == {ROZOK}
    assert pg.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 0
    assert pg.execute("SELECT count(*) FROM codex_card_history").fetchone()[0] == history
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
