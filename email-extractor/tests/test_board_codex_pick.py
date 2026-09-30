"""#477 — a card enters our catalog ONLY by picking it from the CODEX stock-card list.

Owner order 2026-09-30: no free-typed card anywhere — the board Produkty „Pridať", the inline
„➕ Nová karta" on an item / dl_item question and the legacy `/api/znalosti/*` create all answer
403 „Nové karty sa pridávajú len výberom z CODEXu" (their own test files pin that). The one way
in, pinned here: „Vybrať kartu z CODEXu" on a question — `GET /api/board/codex-cards` lists the
pushed CODEX cards (orders = stredisko 1 / sklad 1, dl = stredisko 1, active rows only, never the
other strediská's junk sklady #337), and a `codex_card` answer writes exactly THAT code + CODEX
name into the catalog override (audited) and answers the question through the normal answer
path. A code we already have is only selected; one whose card sits in the Kôš restores it.
Flask test client + real Postgres; synthetic codes/names only (public repo).
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from app.config import Config
from app.httpapi import create_app, dl_key
from app.orders import codex_cards, dl_memory, dl_snapshot, teach

PG_DSN = os.environ.get("PG_TEST_DSN")

G_ROZOK = "9990000000017"      # stredisko 1: sklad 1 + 600 (a finished good, also a DL card)
G_MUKA = "9990000000031"       # stredisko 1: sklad 100 + 625 (kg-tracked raw material)
G_OBAL = "9990000000048"       # stredisko 1: sklad 700 only
G_STARY = "9990000000062"      # stredisko 1 / sklad 1 but INACTIVE in CODEX
G_POBOCKA = "9990000000079"    # only on a junk stredisko (402) — never offered (#337)
G_CHLIEB = "9990000000086"     # stredisko 1 / sklad 1
G_LONG = "99900000000093"      # 14 digits, stredisko 1 / sklad 500 — can never ship in a DESADV
G_SHORT = "4711"               # a short EAN kód — its legacy „0"+code twin still fits a DESADV
CODEX = [
    {"code": G_SHORT, "card_code": "4711", "stredisko": 1, "sklad": 1,
     "name": "Slanina krájaná"},
    {"code": G_LONG, "card_code": "93", "stredisko": 1, "sklad": 500,
     "name": "Nápoj dlhý kód"},
    {"code": G_ROZOK, "card_code": "27", "stredisko": 1, "sklad": 1,
     "name": "Rožok so slaninou a syrom 70g"},
    {"code": G_ROZOK, "card_code": "27", "stredisko": 1, "sklad": 600,
     "name": "Rožok so slaninou a syrom 70g"},
    {"code": G_MUKA, "card_code": "40", "stredisko": 1, "sklad": 625,
     "name": "Múka pšeničná T650"},
    {"code": G_MUKA, "card_code": "40", "stredisko": 1, "sklad": 100,
     "name": "Múka pšeničná T650"},
    {"code": G_OBAL, "card_code": "48", "stredisko": 1, "sklad": 700,
     "name": "Obal papierový na rožok"},
    {"code": G_STARY, "card_code": "62", "stredisko": 1, "sklad": 1,
     "name": "Rožok starý vyradený", "inactive": True},
    {"code": G_POBOCKA, "card_code": "79", "stredisko": 402, "sklad": 402,
     "name": "Rožok z pobočky"},
    {"code": G_CHLIEB, "card_code": "86", "stredisko": 1, "sklad": 1,
     "name": "Chlieb kváskový 500g"},
]


def _client(login=False):
    app = create_app(Config(pg_dsn=PG_DSN, data_dir="/tmp", api_token="tok",
                            dash_password="secret", secret_key="test-secret"))
    app.testing = True
    c = app.test_client()
    if login:
        c.post("/login", data={"password": "secret"})
    else:
        c.get("/sklad-dl/" + dl_key("test-secret"))
    return c


def _codex(pg, hours_old=1):
    codex_cards.replace_cards(pg, CODEX,
                              source_as_of=datetime.now(UTC) - timedelta(hours=hours_old))


def _base(pg):
    dl_snapshot._freeze(pg, [{"gtin": "DBASE0", "name": "Base", "doplnok": "", "mass": None,
                              "sklad": "", "cena": None}], [])
    from app.orders import snapshot
    snapshot._freeze(pg, [{"gtin": "BASE0", "name": "Base", "alias": ""}], [])


def _seed_dl(pg, gtin, name, **kw):
    dl_snapshot.upsert_dl_catalog_card(pg, gtin, name, **kw)
    dl_snapshot.dl_rebuild_from_overrides(pg)


def _dl_question(pg, mid="m477", wording="Múka pšeničná hladká T650"):
    return teach.ask_dl_item(pg, message_id=mid, supplier_ean="S1",
                             supplier_name="Mlyn s.r.o.", wording=wording, quantity=20,
                             unit="kg", candidates=[])


def _codes(resp):
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return [i["code"] for i in resp.get_json()["items"]]


def _overrides(pg, table="dl_catalog_overrides"):
    return pg.execute(f"SELECT gtin, name FROM {table} ORDER BY gtin").fetchall()


def _audit(pg):
    return pg.execute("SELECT actor, table_name, row_id, action, after FROM audit_log "
                      "ORDER BY id").fetchall()


# --- the picker list ------------------------------------------------------------------

def test_the_picker_lists_only_active_codex_cards_of_the_scopes_sklady(pg):
    _codex(pg)
    c = _client()
    # orders = stredisko 1 / sklad 1: the inactive card and the junk-stredisko one are never
    # offered, the kg raw material (sklad 100/625) is not an orders card
    assert _codes(c.get("/api/board/codex-cards?scope=orders&q=rozok")) == [G_ROZOK]
    assert _codes(c.get("/api/board/codex-cards?scope=orders&q=muka")) == []
    # dl = every sklad of stredisko 1 — still no inactive / junk-stredisko card
    assert sorted(_codes(c.get("/api/board/codex-cards?scope=dl&q=rozok"))) == [G_ROZOK, G_OBAL]
    muka = c.get("/api/board/codex-cards?scope=dl&q=múka T650").get_json()["items"]
    assert muka == [{"code": G_MUKA, "name": "Múka pšeničná T650", "card_code": "40",
                     "sklad": 100, "in_catalog": False, "in_trash": False}]
    # found by the EAN kód (a part of it) and by the CODEX card number too
    assert _codes(c.get("/api/board/codex-cards?scope=orders&q=0000086")) == [G_CHLIEB]
    assert _codes(c.get("/api/board/codex-cards?scope=orders&q=27")) == [G_ROZOK]


def test_an_empty_search_lists_nothing_but_says_how_fresh_the_list_is(pg):
    _codex(pg)
    data = _client().get("/api/board/codex-cards?scope=dl").get_json()
    assert data["items"] == []
    assert data["codex"]["active"] is True and data["codex"]["as_of_local"]
    assert _client().get("/api/board/codex-cards?scope=nonsense&q=x").status_code == 400


def test_a_stale_codex_list_is_still_offered_with_the_warning(pg):
    """„CODEX list stale → the picker still lists the last known cards with a visible
    warning" — the pick itself must not stop working while the dev2 push is down."""
    _codex(pg, hours_old=codex_cards.STALE_HOURS + 5)
    data = _client().get("/api/board/codex-cards?scope=dl&q=muka").get_json()
    assert [i["code"] for i in data["items"]] == [G_MUKA]
    assert data["codex"]["stale"] is True and data["codex"]["active"] is False


def test_the_picker_freshness_is_the_full_list_meta_without_loading_the_list(pg):
    """The picker asks the list's freshness on every search — `codex_cards.freshness` reads
    it from the sync ledger alone and must equal `meta_for(load())` in every state."""
    assert codex_cards.freshness(pg) == codex_cards.meta_for(codex_cards.load(pg))  # never
    for hours in (1, codex_cards.STALE_HOURS + 2):
        _codex(pg, hours_old=hours)
        assert codex_cards.freshness(pg) == codex_cards.meta_for(codex_cards.load(pg))


def test_the_picker_marks_the_cards_we_already_have_and_the_ones_in_the_kos(pg):
    _base(pg)
    _codex(pg)
    _seed_dl(pg, "0" + G_SHORT, "Slanina stará", sklad="1")
    _seed_dl(pg, G_MUKA, "Múka pšeničná T650", sklad="100")
    dl_snapshot.retire_dl_catalog_card(pg, G_MUKA)
    dl_snapshot.dl_rebuild_from_overrides(pg)
    c = _client()
    slanina = c.get("/api/board/codex-cards?scope=dl&q=slanina").get_json()["items"][0]
    # OUR exact number (a card created before #467 as „0"+code) — what the answer keys on
    assert slanina["in_catalog"] is True and slanina["catalog_gtin"] == "0" + G_SHORT
    assert slanina["catalog_name"] == "Slanina stará"
    muka = c.get("/api/board/codex-cards?scope=dl&q=muka").get_json()["items"][0]
    assert muka["in_catalog"] is False and muka["in_trash"] is True
    assert muka["trash_name"] == "Múka pšeničná T650"


# --- the pick answers the question ------------------------------------------------------

def test_picking_a_codex_card_on_a_dl_item_question_adds_exactly_that_card_and_answers(pg):
    _base(pg)
    _codex(pg)
    qid = _dl_question(pg)
    r = _client().post(f"/api/board/questions/{qid}/answer",
                       json={"codex_card": {"code": G_MUKA}})
    assert r.status_code == 200, r.get_data(as_text=True)
    # exactly ONE card: the CODEX code + the CODEX name, kg-tracked because CODEX has it on
    # sklad 100 (sklad 625 is its second row) — never a bulk import
    assert pg.execute("SELECT gtin, name, sklad, mass, cena FROM dl_catalog_overrides"
                      ).fetchall() == [(G_MUKA, "Múka pšeničná T650", "100", None, None)]
    audit = [a for a in _audit(pg) if a[1] == "dl_catalog_overrides"]
    assert len(audit) == 1
    actor, _t, row_id, action, after = audit[0]
    assert (row_id, action) == (G_MUKA, "create") and actor == "sklad"
    assert after["source"] == "codex" and after["codex_card"] == "40"
    # the question went through the NORMAL answer path: answered + the wording taught
    q = teach.get(pg, qid)
    assert q["status"] == "answered" and q["answer"]["choice"] == G_MUKA
    assert dl_memory.resolve(pg, "S1", "Múka pšeničná hladká T650").gtin == G_MUKA


def test_a_dl_card_takes_the_lowest_stredisko_1_sklad_when_codex_has_no_kg_row(pg):
    _base(pg)
    _codex(pg)
    qid = _dl_question(pg, mid="m477b", wording="Rožok so slaninou 70g")
    assert _client().post(f"/api/board/questions/{qid}/answer",
                          json={"codex_card": {"code": G_ROZOK}}).status_code == 200
    assert pg.execute("SELECT sklad FROM dl_catalog_overrides WHERE gtin=%s",
                      (G_ROZOK,)).fetchone() == ("1",)


def test_picking_a_code_we_already_have_selects_our_card_and_writes_nothing(pg):
    """A code already in our catalog is just selected, never duplicated or overwritten (the
    #467 lesson: an upsert would wipe mass/sklad/cena)."""
    _base(pg)
    _codex(pg)
    _seed_dl(pg, "0" + G_SHORT, "Slanina krájaná stará", sklad="1", cena=0.37)
    before = pg.execute("SELECT gtin, name, sklad, cena FROM dl_catalog_overrides").fetchall()
    qid = _dl_question(pg, wording="Slanina krájaná")
    r = _client().post(f"/api/board/questions/{qid}/answer",
                       json={"codex_card": {"code": G_SHORT}})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert pg.execute("SELECT gtin, name, sklad, cena FROM dl_catalog_overrides"
                      ).fetchall() == before
    assert not any(a[3] == "create" for a in _audit(pg))
    assert teach.get(pg, qid)["answer"]["choice"] == "0" + G_SHORT


def test_a_legacy_twin_too_long_for_a_desadv_is_never_selected(pg):
    """Review 3: „0"+a 13-digit code is a 14-char DL number the DESADV field can never carry
    (`dl_match._gtin_edi_overflow` — the line would be dropped from every later EDI). The pick
    never selects (or restores) it; it adds the canonical CODEX card instead."""
    _base(pg)
    _codex(pg)
    _seed_dl(pg, "0" + G_MUKA, "Múka stará", sklad="100")
    qid = _dl_question(pg)
    r = _client().post(f"/api/board/questions/{qid}/answer",
                       json={"codex_card": {"code": G_MUKA}})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert teach.get(pg, qid)["answer"]["choice"] == G_MUKA
    assert pg.execute("SELECT name, sklad FROM dl_catalog_overrides WHERE gtin=%s",
                      (G_MUKA,)).fetchone() == ("Múka pšeničná T650", "100")


def test_picking_a_code_whose_card_is_in_the_kos_restores_that_card(pg):
    """Review 🟡1: a Kôš card is not a dead end — the pick RESTORES our card exactly as it was
    (never a blank overwrite: mass/sklad/cena and our name kept), audited as a `create` the
    Kôš can take back again, and the question is answered with it."""
    _base(pg)
    _codex(pg)
    _seed_dl(pg, G_MUKA, "Múka hladká T650", sklad="100", cena=0.37, mass=25.0)
    dl_snapshot.retire_dl_catalog_card(pg, G_MUKA)
    dl_snapshot.dl_rebuild_from_overrides(pg)
    qid = _dl_question(pg)
    r = _client().post(f"/api/board/questions/{qid}/answer",
                       json={"codex_card": {"code": G_MUKA}})
    assert r.status_code == 200, r.get_data(as_text=True)
    row = pg.execute("SELECT name, retired, deleted_at, sklad, cena, mass "
                     "FROM dl_catalog_overrides WHERE gtin=%s", (G_MUKA,)).fetchone()
    assert row[:4] == ("Múka hladká T650", False, None, "100")
    assert float(row[4]) == 0.37 and float(row[5]) == 25.0
    assert any(x["gtin"] == G_MUKA for x in dl_snapshot.dl_catalog_for_management(pg))
    audit = [a for a in _audit(pg) if a[1] == "dl_catalog_overrides"]
    assert [(a[2], a[3]) for a in audit] == [(G_MUKA, "create")]
    assert audit[0][4]["restored"] is True
    assert teach.get(pg, qid)["answer"]["choice"] == G_MUKA


def test_a_retired_snapshot_card_comes_back_whole_never_blank(pg):
    """A card that lived only in the frozen snapshot leaves, when deleted, a bare retirement
    marker (`retire_*` writes name '' + blank fields; the next snapshot drops the card; 9 of 19
    deleted DL cards on prod are such markers). Un-deleting that marker alone would make a
    NAMELESS card — the restore refills it from the newest snapshot that still has the card
    (name, doplnok, mass, sklad, cena all ours again)."""
    dl_snapshot._freeze(pg, [{"gtin": G_MUKA, "name": "Múka zo snapshotu", "doplnok": "25kg",
                              "mass": 25.0, "sklad": "100", "cena": 0.4}], [])
    _codex(pg)
    assert dl_snapshot.retire_dl_catalog_card(pg, G_MUKA)
    dl_snapshot.dl_rebuild_from_overrides(pg)
    assert pg.execute("SELECT name FROM dl_catalog_overrides WHERE gtin=%s",
                      (G_MUKA,)).fetchone() == ("",)
    qid = _dl_question(pg)
    r = _client().post(f"/api/board/questions/{qid}/answer",
                       json={"codex_card": {"code": G_MUKA}})
    assert r.status_code == 200, r.get_data(as_text=True)
    card = next(x for x in dl_snapshot.dl_catalog_for_management(pg) if x["gtin"] == G_MUKA)
    assert (card["name"], card["doplnok"], card["mass"], card["sklad"], card["cena"]) == (
        "Múka zo snapshotu", "25kg", 25.0, "100", 0.4)
    assert teach.get(pg, qid)["answer"]["choice"] == G_MUKA


def test_a_bare_marker_with_no_snapshot_history_gets_the_codex_card(pg):
    """A blank marker whose card no snapshot remembers has nothing to restore — the pick
    fills it from CODEX like a new card (never a nameless one)."""
    _base(pg)
    _codex(pg)
    pg.execute("INSERT INTO dl_catalog_overrides (gtin, name, retired, deleted_at, updated_at) "
               "VALUES (%s, '', true, now(), now())", (G_MUKA,))
    qid = _dl_question(pg)
    r = _client().post(f"/api/board/questions/{qid}/answer",
                       json={"codex_card": {"code": G_MUKA}})
    assert r.status_code == 200, r.get_data(as_text=True)
    card = next(x for x in dl_snapshot.dl_catalog_for_management(pg) if x["gtin"] == G_MUKA)
    assert (card["name"], card["sklad"]) == ("Múka pšeničná T650", "100")


def test_a_dl_code_longer_than_the_desadv_gtin_field_is_never_pickable(pg):
    """Review: a 14-digit CODEX code (the #245 beverage cards on sklad 500) can never ship in a
    DESADV (13-char field, `dl_match._gtin_edi_overflow`) — picking it would add a junk card and
    loop on a fresh question every reprocess. Not offered, refused, never marked pickable."""
    _base(pg)
    _codex(pg)
    c = _client()
    assert _codes(c.get("/api/board/codex-cards?scope=dl&q=napoj")) == []
    qid = _dl_question(pg, mid="m477long", wording="Nápoj dlhý kód")
    r = c.post(f"/api/board/questions/{qid}/answer", json={"codex_card": {"code": G_LONG}})
    assert r.status_code == 409
    assert _overrides(pg) == [] and teach.get(pg, qid)["status"] == "open"
    from app.orders import card_guard
    payload = card_guard.mark_pickable(pg, "dl", {"codex": {"similar": [{"code": G_LONG}]}})
    assert payload["codex"]["similar"][0]["pickable"] is False


def test_the_exact_number_wins_over_a_legacy_zero_prefixed_twin(pg):
    """Review: with both „0"+code (a pre-#467 card) and the exact code live, the pick selects
    the EXACT number — never the legacy twin because it sorts first."""
    _base(pg)
    _codex(pg)
    _seed_dl(pg, "0" + G_SHORT, "Slanina stará", sklad="1")
    _seed_dl(pg, G_SHORT, "Slanina krájaná", sklad="1")
    qid = _dl_question(pg, wording="Slanina krájaná")
    r = _client().post(f"/api/board/questions/{qid}/answer",
                       json={"codex_card": {"code": G_SHORT}})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert teach.get(pg, qid)["answer"]["choice"] == G_SHORT


def test_an_orders_snapshot_card_in_the_kos_comes_back_whole_with_its_name_shown(pg):
    """Review 3: the ORDERS bare-marker path — the picker names the Kôš card (from the snapshot,
    the marker itself is blank) and the pick restores it whole (name + alias)."""
    from app.orders import snapshot
    snapshot._freeze(pg, [{"gtin": G_CHLIEB, "name": "Chlieb zo snapshotu", "alias": "kvas"}], [])
    _codex(pg)
    assert snapshot.retire_catalog_card(pg, G_CHLIEB)
    snapshot.rebuild_from_overrides(pg)
    item = _client().get("/api/board/codex-cards?scope=orders&q=kvaskovy").get_json()["items"][0]
    assert item["in_trash"] is True and item["trash_name"] == "Chlieb zo snapshotu"
    qid = teach.ask(pg, message_id="m477os", customer_ean="2000000000864",
                    customer_name="Pekáreň", wording="chlieb kvaskovy", quantity=1, unit="ks",
                    candidates=[])
    r = _client().post(f"/api/board/questions/{qid}/answer",
                       json={"codex_card": {"code": G_CHLIEB}})
    assert r.status_code == 200, r.get_data(as_text=True)
    card = next(x for x in snapshot.catalog_for_management(pg) if x["gtin"] == G_CHLIEB)
    assert (card["name"], card["alias"]) == ("Chlieb zo snapshotu", "kvas")
    assert teach.get(pg, qid)["answer_card"] == "Chlieb zo snapshotu"


def test_an_orders_card_in_the_kos_is_marked_and_restored_by_the_pick(pg):
    """The same restore in the ORDERS scope (`snapshot.deleted_catalog_cards`), incl. the
    picker's `in_trash` mark."""
    _base(pg)
    _codex(pg)
    from app.orders import snapshot
    snapshot.upsert_catalog_card(pg, G_CHLIEB, "Chlieb kváskový", alias="kvasok")
    snapshot.rebuild_from_overrides(pg)
    snapshot.retire_catalog_card(pg, G_CHLIEB)
    snapshot.rebuild_from_overrides(pg)
    item = _client().get("/api/board/codex-cards?scope=orders&q=kvaskovy").get_json()["items"]
    assert item[0]["code"] == G_CHLIEB and item[0]["in_trash"] is True
    qid = teach.ask(pg, message_id="m477k", customer_ean="2000000000864",
                    customer_name="Pekáreň", wording="chlieb kvaskovy", quantity=2, unit="ks",
                    candidates=[])
    r = _client().post(f"/api/board/questions/{qid}/answer",
                       json={"codex_card": {"code": G_CHLIEB}, "quantity": 2})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert pg.execute("SELECT name, alias, retired, deleted_at FROM catalog_overrides "
                      "WHERE gtin=%s", (G_CHLIEB,)).fetchone() == (
        "Chlieb kváskový", "kvasok", False, None)
    assert G_CHLIEB in snapshot.catalog_gtin_set(pg)
    assert teach.get(pg, qid)["answer_gtin"] == G_CHLIEB


def test_a_codex_pick_taken_back_in_the_kos_can_be_picked_again(pg):
    """Review 🟡1: pick → Kôš „Vrátiť" (soft-deletes it again) → the next pick of that code
    brings the card back — never the dead end a plain refusal was."""
    _base(pg)
    _codex(pg)
    c = _client()
    qid = _dl_question(pg)
    assert c.post(f"/api/board/questions/{qid}/answer",
                  json={"codex_card": {"code": G_MUKA}}).status_code == 200
    aid = pg.execute("SELECT id FROM audit_log WHERE table_name='dl_catalog_overrides' "
                     "AND action='create'").fetchone()[0]
    assert _client(login=True).post(f"/api/board/audit/{aid}/restore").status_code == 200
    assert not any(x["gtin"] == G_MUKA for x in dl_snapshot.dl_catalog_for_management(pg))
    q2 = _dl_question(pg, mid="m477again", wording="Múka pšeničná T650 25kg")
    r = c.post(f"/api/board/questions/{q2}/answer", json={"codex_card": {"code": G_MUKA}})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert any(x["gtin"] == G_MUKA for x in dl_snapshot.dl_catalog_for_management(pg))
    assert teach.get(pg, q2)["answer"]["choice"] == G_MUKA


def test_a_codex_pick_without_a_code_is_refused_on_an_open_question(pg):
    _base(pg)
    _codex(pg)
    qid = _dl_question(pg)
    for body in ({"codex_card": {}}, {"codex_card": {"code": "  "}}):
        r = _client().post(f"/api/board/questions/{qid}/answer", json=body)
        assert r.status_code == 400 and "kód" in r.get_json()["error"]
    assert teach.get(pg, qid)["status"] == "open" and _overrides(pg) == []


def test_the_refusal_help_says_which_similar_codex_cards_can_be_picked(pg):
    """Review 🔵6: the #467 refusal lists similar cards from the WHOLE CODEX list — only the
    ones the picker would offer (stredisko 1, active) may get „Pridať kartu z CODEXu"."""
    _base(pg)
    _codex(pg)
    _seed_dl(pg, "3698", "Rožok so slaninou a syrom 70g")
    qid = _dl_question(pg, mid="m477h", wording="Rožok")   # every rožok card is „similar"
    r = _client().post(f"/api/board/questions/{qid}/answer", json={"choice": "3698"})
    assert r.status_code == 409
    similar = {s["code"]: s for s in r.get_json()["codex"]["similar"]}
    assert similar[G_ROZOK]["pickable"] is True
    assert similar[G_POBOCKA]["pickable"] is False   # junk stredisko (#337)
    assert similar[G_STARY]["pickable"] is False     # inactive in CODEX


def test_a_code_outside_the_pick_scope_is_refused_and_nothing_is_written(pg):
    """Only what the picker lists can be added: a junk-stredisko card (#337), an inactive
    CODEX card, an unknown code, and — on an ORDERS question — a raw material that is not on
    the finished-goods sklad 1."""
    _base(pg)
    _codex(pg)
    qid = _dl_question(pg)
    c = _client()
    for code in (G_POBOCKA, G_STARY, "123456"):
        r = c.post(f"/api/board/questions/{qid}/answer", json={"codex_card": {"code": code}})
        assert r.status_code == 409, code
        assert "CODEX" in r.get_json()["error"]
    item = teach.ask(pg, message_id="m477o", customer_ean="2000000000864",
                     customer_name="Pekáreň", wording="muka hladka", quantity=1, unit="ks",
                     candidates=[])
    r = c.post(f"/api/board/questions/{item}/answer", json={"codex_card": {"code": G_MUKA}})
    assert r.status_code == 409
    assert _overrides(pg) == [] and _overrides(pg, "catalog_overrides") == []
    assert _audit(pg) == []
    assert teach.get(pg, qid)["status"] == "open" and teach.get(pg, item)["status"] == "open"


def test_the_codex_pick_on_another_kind_or_an_answered_question_is_refused(pg):
    _base(pg)
    _codex(pg)
    c = _client()
    mail = teach.ask_mail(pg, message_id="m477m", sender_email="x@y.sk", subject="?")
    assert c.post(f"/api/board/questions/{mail}/answer",
                  json={"codex_card": {"code": G_CHLIEB}}).status_code == 400
    qid = _dl_question(pg)
    pg.execute("UPDATE order_questions SET status='answered' WHERE id=%s", (qid,))
    assert c.post(f"/api/board/questions/{qid}/answer",
                  json={"codex_card": {"code": G_MUKA}}).status_code == 409
    assert _overrides(pg) == [] and _audit(pg) == []


def test_the_kos_restore_of_a_codex_pick_takes_the_card_back_out(pg):
    """The pick is audited as a `create` — the Kôš „Vrátiť" soft-deletes the card again."""
    _base(pg)
    _codex(pg)
    qid = _dl_question(pg)
    assert _client().post(f"/api/board/questions/{qid}/answer",
                          json={"codex_card": {"code": G_MUKA}}).status_code == 200
    aid = pg.execute("SELECT id FROM audit_log WHERE table_name='dl_catalog_overrides' "
                     "AND action='create'").fetchone()[0]
    admin = _client(login=True)
    assert admin.post(f"/api/board/audit/{aid}/restore").status_code == 200
    assert pg.execute("SELECT retired FROM dl_catalog_overrides WHERE gtin=%s",
                      (G_MUKA,)).fetchone() == (True,)


# --- the question cards offer the picker, never a typed card -----------------------------

def test_the_question_cards_offer_the_codex_picker_instead_of_a_new_card_form(pg):
    c = _client()
    for scope, kind, gone in (("dl", "dl_item", "new_item"), ("orders", "item", "new_product")):
        acts = c.get(f"/api/board/questions?scope={scope}").get_json()["meta"]["card_actions"]
        ops = [a["op"] for a in acts[kind]]
        assert ops[0] == "codex_pick", ops
        assert gone not in ops
        assert acts[kind][0]["label"] == "Vybrať kartu z CODEXu"
