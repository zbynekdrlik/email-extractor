"""#494 — an owner-confirmed CODEX code for a card (`codex_code_overrides`, „card=code").

The CODEX copy (`raw.sm002.NEANKOD`) can carry a code CODEX itself refuses at import while the
real one is missing from the copy. The owner's mapping is applied where the pushed list enters
the add-on (`codex_cards.replace_cards`), so every consumer — the #467 guard, the #478 sync and
bindings, the DL output — reads the confirmed code, and no later push can bring the old one
back. A card without a mapping is untouched.

All codes/names are SYNTHETIC — this repo is public.
"""
from __future__ import annotations

import logging
import os
from datetime import UTC, date, datetime, timedelta

from test_dl_worker import (
    _DL_DELIVERY_DATE,
    SUPPLIER_EAN,
    SUPPLIER_MATCHED,
    SUPPLIERS_CSV,
    FakeClient,
    _attach,
    _msg,
)
from test_dl_worker import _cfg as _dl_cfg

from app.config import Config
from app.httpapi import create_app
from app.orders import codex_cards, codex_sync, dl_memory, dl_snapshot, dl_worker

WRONG = "9990000000123"        # what the CODEX copy says, CODEX refuses it
RIGHT = "9990000000456"        # what the owner confirmed
OTHER = "9990000000789"
CARD = "148"
NAME = "Syr strúhaný"
BREAD = "Chlieb biely 500g"
NOW = datetime.now(UTC)
OVERRIDE = {CARD: RIGHT}


def _row(code, card=CARD, name=NAME, stredisko=1, sklad=100):
    return {"code": code, "card_code": card, "stredisko": stredisko, "sklad": sklad,
            "name": name, "inactive": False, "changed_at": None}


LIST = [_row(WRONG), _row(OTHER, card="31", name=BREAD, sklad=1)]


def _push(pg, hours_old=1, overrides=OVERRIDE, cards=None):
    return codex_cards.replace_cards(pg, LIST if cards is None else cards,
                                     source_as_of=NOW - timedelta(hours=hours_old),
                                     code_overrides=overrides)


def _stored(pg):
    return set(pg.execute("SELECT code, card_code, stredisko FROM codex_stock_cards"
                          ).fetchall())


# --- the option -----------------------------------------------------------------------------

def test_the_option_parses_card_equals_code():
    assert codex_cards.parse_code_overrides(f"{CARD}={RIGHT}") == {CARD: RIGHT}
    assert codex_cards.parse_code_overrides(f" {CARD} = {RIGHT} , 27=0555.0 ") == {
        CARD: RIGHT, "27": "555"}
    assert codex_cards.parse_code_overrides("") == {}
    assert codex_cards.parse_code_overrides(None) == {}


def test_a_bad_entry_is_refused_loudly_and_the_good_one_stays(caplog):
    with caplog.at_level(logging.ERROR, logger="orders.codex_cards"):
        got = codex_cards.parse_code_overrides(f"nonsense, 27=abc, ={RIGHT}, {CARD}={RIGHT}")
    assert got == {CARD: RIGHT}
    assert sum("codex_code_overrides" in r.getMessage() for r in caplog.records) == 3


def test_the_config_reads_the_option(monkeypatch):
    monkeypatch.setenv("CODEX_CODE_OVERRIDES", f"{CARD}={RIGHT}")
    assert Config.load().codex_code_overrides == f"{CARD}={RIGHT}"


# --- the list -------------------------------------------------------------------------------

def test_the_confirmed_code_replaces_the_copys_on_the_cards_rows(pg):
    res = _push(pg)
    assert res == {"rows": 2, "codes": 2}
    assert _stored(pg) == {(OTHER, "31", 1), (RIGHT, CARD, 1)}
    cards = codex_cards.live_guard(pg)
    assert cards.has(RIGHT) and not cards.has(WRONG)
    assert cards.name_for(RIGHT) == NAME
    assert codex_cards.pickable(pg)[RIGHT]["card_code"] == CARD


def test_a_card_without_a_mapping_is_untouched(pg):
    _push(pg, overrides=None)
    assert _stored(pg) == {(OTHER, "31", 1), (WRONG, CARD, 1)}
    _push(pg, overrides={"999": RIGHT})
    assert _stored(pg) == {(OTHER, "31", 1), (WRONG, CARD, 1)}


def test_the_same_acsklp_on_another_stredisko_is_another_card(pg):
    """ACSKLP is unique only WITHIN a stredisko (#478): the mapping is for the stredisko-1 card;
    its old code goes everywhere that card carried it, another product keeps its own code."""
    _push(pg, cards=[_row(WRONG), _row(WRONG, stredisko=0), _row(OTHER, stredisko=4)])
    assert _stored(pg) == {(OTHER, CARD, 4), (RIGHT, CARD, 0), (RIGHT, CARD, 1)}


def test_a_copy_that_already_has_the_right_code_changes_nothing(pg):
    _push(pg, cards=[_row(RIGHT)])
    assert _stored(pg) == {(RIGHT, CARD, 1)}


def test_a_push_endpoint_applies_the_option(pg):
    cfg = Config(pg_dsn=os.environ.get("PG_TEST_DSN"), data_dir="/tmp", api_token="tok",
                 secret_key="s", ops_channel_id=77, codex_code_overrides=f"{CARD}={RIGHT}")
    r = create_app(cfg).test_client().post(
        "/api/codex/cards", headers={"X-Token": "tok"},
        json={"cards": LIST, "source_as_of": NOW.isoformat()})
    assert r.status_code == 200
    assert _stored(pg) == {(OTHER, "31", 1), (RIGHT, CARD, 1)}


# --- the sync and the bindings --------------------------------------------------------------

def _prod_shape(pg):
    """Our DL catalog carries both numbers (the old one is a live, unbound card with the real
    history; the wrong one is bound to the CODEX card and taught by a human)."""
    dl_snapshot._freeze(pg, [
        {"gtin": WRONG, "name": NAME, "doplnok": "", "mass": 2, "sklad": "100", "cena": 4.7},
        {"gtin": RIGHT, "name": NAME, "doplnok": "strúhaná tehla", "mass": 2, "sklad": "100",
         "cena": None},
        {"gtin": OTHER, "name": BREAD, "doplnok": "", "mass": 0.5, "sklad": "1", "cena": 0.8},
    ], [])
    _push(pg, hours_old=9, overrides=None)
    assert codex_sync.run(pg, _sync_cfg())["mode"] == "apply"
    pg.execute("DELETE FROM codex_card_bindings WHERE gtin = %s", (RIGHT,))
    pg.execute("INSERT INTO dl_item_memory (supplier_ean, item_key, item_raw, gtin, card, "
               "delivered_on, cnt, source, created_at) VALUES ('S1', 'syr strukany', "
               "'Syr strúhaný', %s, %s, %s, 1, 'human', %s)",
               (WRONG, NAME, date(2026, 10, 7), NOW - timedelta(hours=12)))


def _sync_cfg():
    return Config(pg_dsn="", data_dir="/tmp", ops_channel_id=77, codex_sync_apply=True)


def _live(pg):
    return {r["gtin"]: r for r in dl_snapshot.dl_catalog_for_management(pg)}


def test_the_sync_moves_our_card_to_the_confirmed_code_and_cannot_revert_it(pg):
    _prod_shape(pg)
    for hours in (5, 3):
        _push(pg, hours_old=hours)
        assert codex_sync.run(pg, _sync_cfg())["mode"] == "apply"
    live = _live(pg)
    assert WRONG not in live and RIGHT in live
    assert live[RIGHT]["cena"] == 4.7, "the merge keeps our curated data"
    assert pg.execute("SELECT card_code, active FROM codex_card_bindings "
                      "WHERE scope = 'dl' AND gtin = %s", (RIGHT,)).fetchone() == (CARD, True)
    assert pg.execute("SELECT DISTINCT gtin FROM dl_item_memory WHERE deleted_at IS NULL"
                      ).fetchall() == [(RIGHT,)], "the taught row follows the card"
    _push(pg, hours_old=1)
    res = codex_sync.run(pg, _sync_cfg())
    assert (res["renumbered"], res["removed"]) == (0, 0)
    assert WRONG not in _live(pg) and RIGHT in _live(pg)


# --- the DL output --------------------------------------------------------------------------

CATALOG_CSV = ("GTIN,Názov,doplnok,hmotnost,Sklad,Cena\n"
               f"{WRONG},{NAME},,2,100,4.7\n"
               f"{RIGHT},{NAME},,2,100,4.7\n"
               f"{OTHER},{BREAD},,0.5,1,0.80\n")


def _doc():
    lines = [{"name": NAME, "quantity": 2, "unit": "ks", "unitPrice": 9.4, "totalPrice": 18.8,
              "vatRate": 10}]
    return {"documents": [{
        "supplierName": "Dodávateľ", "supplierCity": "Prešov", "supplierEmail": "d@lunys.sk",
        "docNumber": "0100000301", "deliveryDate": _DL_DELIVERY_DATE,
        "documentTotalWithoutVAT": 18.8, "items": lines}]}


def test_a_memory_row_with_the_old_code_never_ships_it(pg, tmp_path):
    """The incident: the newest human answer and the model both say the code CODEX refuses."""
    dl_snapshot.import_snapshot(pg, CATALOG_CSV, "GTIN,Sklad,Názov,doplnok\n", SUPPLIERS_CSV)
    _push(pg)
    dl_memory.remember(pg, SUPPLIER_EAN, NAME, RIGHT, NAME, "2026-09-23")
    dl_memory.remember(pg, SUPPLIER_EAN, NAME, WRONG, NAME, "2026-10-07", source="human")
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    uploaded = []
    client = FakeClient({"dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [{"gtin": WRONG, "matchConfidence": 0.95,
                                      "matchReason": "scripted"}]})
    dl_worker.tick(pg, _dl_cfg(delivery_notes_engine="python", data_dir=str(tmp_path)),
                   client=client,
                   upload=lambda c, name, content, dir_override=None: uploaded.append(content))
    assert len(uploaded) == 1
    assert RIGHT in uploaded[0] and WRONG not in uploaded[0]
