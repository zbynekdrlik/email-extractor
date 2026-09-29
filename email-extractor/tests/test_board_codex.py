"""#467 — the nástenka never saves a DL card number CODEX has no stock card for.

Three write paths of a DL card (the board Produkty sklad editor, the inline „➕ Nová karta" on
a dl_item question, the legacy `/api/znalosti/dl-products`) + the dl_item answer itself refuse a
code missing from the pushed CODEX list with a 409 that names CODEX cards with a similar name and
their code. The Produkty sklad list flags cards whose name drifted from CODEX's. A missing/stale
list FAILS OPEN (the old behaviour). Flask test client + real Postgres; synthetic data only.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from app.config import Config
from app.httpapi import create_app, dl_key
from app.orders import codex_cards, dl_snapshot, teach

PG_DSN = os.environ.get("PG_TEST_DSN")

G_GOOD = "9990000000017"
G_MUKA = "9990000000031"
CODEX = [
    {"code": G_GOOD, "card_code": "27", "stredisko": 1, "sklad": 1,
     "name": "Rožok so slaninou a syrom 70g"},
    {"code": G_MUKA, "card_code": "40", "stredisko": 1, "sklad": 100,
     "name": "Múka pšeničná T650"},
    {"code": "9990000000055", "card_code": "50", "stredisko": 1, "sklad": 1,
     "name": "Chlieb pšeničný 1000g REZANÝ"},
]


def _client():
    app = create_app(Config(pg_dsn=PG_DSN, data_dir="/tmp", api_token="tok",
                            dash_password="secret", secret_key="test-secret"))
    app.testing = True
    c = app.test_client()
    c.get("/sklad-dl/" + dl_key("test-secret"))
    return c


def _codex(pg, hours_old=1):
    codex_cards.replace_cards(pg, CODEX,
                              source_as_of=datetime.now(UTC) - timedelta(hours=hours_old))


def _base(pg):
    dl_snapshot._freeze(pg, [{"gtin": "DBASE0", "name": "Base", "doplnok": "", "mass": None,
                              "sklad": "", "cena": None}], [])


def _seed(pg, gtin, name, **kw):
    dl_snapshot.upsert_dl_catalog_card(pg, gtin, name, **kw)
    dl_snapshot.dl_rebuild_from_overrides(pg)


# --- Produkty sklad (board products, scope dl) -------------------------------------------

def test_a_new_dl_card_with_a_code_codex_lacks_is_refused_with_similar_cards(pg):
    _base(pg)
    _codex(pg)
    _seed(pg, G_GOOD, "Bagetka s kečupom a syrom 80 gr", sklad="1")
    r = _client().post("/api/board/products?scope=dl",
                       json={"gtin": "3698", "name": "Rožok so slaninou a syrom 70g"})
    assert r.status_code == 409
    body = r.get_json()
    assert "3698" in body["error"] and "CODEX" in body["error"]
    top = body["codex"]["similar"][0]
    assert top["code"] == G_GOOD and top["name"] == "Rožok so slaninou a syrom 70g"
    assert top["in_catalog"] is True and top["catalog_name"] == "Bagetka s kečupom a syrom 80 gr"
    assert pg.execute("SELECT count(*) FROM dl_catalog_overrides WHERE gtin='3698'"
                      ).fetchone()[0] == 0
    assert pg.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 0


def test_a_new_dl_card_with_a_code_codex_has_is_saved(pg):
    _base(pg)
    _codex(pg)
    r = _client().post("/api/board/products?scope=dl",
                       json={"gtin": G_MUKA, "name": "Múka pšeničná T650", "sklad": "100"})
    assert r.status_code == 200 and r.get_json()["action"] == "create"


def test_editing_a_card_whose_code_codex_lacks_is_refused(pg):
    """„Číslo, ktoré v CODEXe neexistuje, sa neuloží" — also on an edit: the card can never
    ship, the fix is the right code (delete + the CODEX card), never a rename of a dead one."""
    _base(pg)
    _codex(pg)
    _seed(pg, "3698", "Rožok so slaninou a syrom 70g")
    r = _client().post("/api/board/products?scope=dl",
                       json={"gtin": "3698", "name": "Rožok so slaninou 70g"})
    assert r.status_code == 409
    assert pg.execute("SELECT name FROM dl_catalog_overrides WHERE gtin='3698'"
                      ).fetchone()[0] == "Rožok so slaninou a syrom 70g"


def test_the_codex_check_fails_open_on_a_stale_list(pg):
    _base(pg)
    _codex(pg, hours_old=codex_cards.STALE_HOURS + 3)
    r = _client().post("/api/board/products?scope=dl",
                       json={"gtin": "3698", "name": "Rožok so slaninou a syrom 70g"})
    assert r.status_code == 200


def test_the_orders_catalog_is_not_codex_checked(pg):
    _codex(pg)
    r = _client().post("/api/board/products?scope=orders", json={"gtin": "3698", "name": "X"})
    assert r.status_code == 200


def test_the_dl_list_flags_name_drift_and_missing_codes_and_filters_them(pg):
    _base(pg)
    _codex(pg)
    _seed(pg, G_GOOD, "Bagetka s kečupom a syrom 80 gr")
    _seed(pg, G_MUKA, "Múka pšeničná T650")
    _seed(pg, "3698", "Rožok so slaninou a syrom 70g")
    c = _client()
    data = c.get("/api/board/products?scope=dl").get_json()
    by = {i["gtin"]: i["codex"] for i in data["items"]}
    assert by[G_GOOD] == {"status": "drift", "name": "Rožok so slaninou a syrom 70g"}
    assert by[G_MUKA]["status"] == "ok"
    assert by["3698"]["status"] == "missing"
    meta = data["meta"]["codex"]
    assert meta["active"] is True and meta["codes"] == 3 and meta["as_of"]
    issues = c.get("/api/board/products?scope=dl&codex=issues").get_json()
    # the `_base` fixture card DBASE0 is not a CODEX card either — it is listed as missing too
    assert sorted(i["gtin"] for i in issues["items"]) == ["3698", G_GOOD, "DBASE0"]
    assert G_MUKA not in {i["gtin"] for i in issues["items"]}


def test_the_legacy_dl_products_api_is_checked_too(pg):
    _base(pg)
    _codex(pg)
    c = _client()
    c.post("/login", data={"password": "secret"})
    r = c.post("/api/znalosti/dl-products", json={"gtin": "3698", "name": "Rožok"})
    assert r.status_code == 409 and r.get_json()["codex"]["code"] == "3698"


# --- the inline „➕ Nová karta" on a dl_item question ----------------------------------

def _question(pg, cands=None):
    return teach.ask_dl_item(pg, message_id="m467", supplier_ean="S1",
                             supplier_name="Pekáreň s.r.o.",
                             wording="Rožok so slaninou a syrom 70g", quantity=20, unit="ks",
                             candidates=cands or [])


def test_the_new_card_on_a_question_refuses_a_code_codex_lacks(pg):
    """The incident's entry point: 3698 was typed into „Nová karta" — now refused, the
    question stays open, and the answer names the CODEX card to use instead."""
    _base(pg)
    _codex(pg)
    _seed(pg, G_GOOD, "Bagetka s kečupom a syrom 80 gr")
    qid = _question(pg)
    r = _client().post(f"/api/board/questions/{qid}/answer", json={"new_item": {
        "gtin": "3698", "name": "Rožok so slaninou a syrom 70g"}})
    assert r.status_code == 409
    sim = r.get_json()["codex"]["similar"]
    assert sim[0]["code"] == G_GOOD and sim[0]["in_catalog"] is True
    assert teach.get(pg, qid)["status"] == "open"
    assert pg.execute("SELECT count(*) FROM dl_catalog_overrides WHERE gtin='3698'"
                      ).fetchone()[0] == 0


def test_the_new_card_on_a_question_never_overwrites_an_existing_card(pg):
    """Adjacent bug: typing the code of a card we ALREADY have into „Nová karta" used to
    upsert it with blank mass/sklad/cena — a kg-tracked card silently lost its sklad=100. Now
    it is refused with the existing card, which the sklad picks with one click."""
    _base(pg)
    _codex(pg)
    _seed(pg, G_MUKA, "Múka pšeničná T650", mass=None, sklad="100", cena=0.37)
    qid = _question(pg)
    r = _client().post(f"/api/board/questions/{qid}/answer", json={"new_item": {
        "gtin": G_MUKA, "name": "Múka hladká"}})
    assert r.status_code == 409
    assert r.get_json()["existing"] == {"gtin": G_MUKA, "name": "Múka pšeničná T650"}
    row = pg.execute("SELECT name, sklad, cena FROM dl_catalog_overrides WHERE gtin=%s",
                     (G_MUKA,)).fetchone()
    assert row[0] == "Múka pšeničná T650" and row[1] == "100" and float(row[2]) == 0.37
    assert teach.get(pg, qid)["status"] == "open"


def test_the_new_card_on_a_question_with_a_valid_new_code_answers(pg):
    _base(pg)
    _codex(pg)
    qid = _question(pg)
    r = _client().post(f"/api/board/questions/{qid}/answer", json={"new_item": {
        "gtin": "9990000000055", "name": "Chlieb pšeničný rezaný 1000 gr"}})
    assert r.status_code == 200
    assert teach.get(pg, qid)["status"] == "answered"


def test_picking_a_catalog_card_whose_code_codex_lacks_is_refused(pg):
    """An existing (old) question can still offer a card CODEX dropped, and the free
    „Iné číslo položky" box reaches any catalog card — neither may teach a dead code."""
    _base(pg)
    _codex(pg)
    _seed(pg, "3698", "Rožok so slaninou a syrom 70g")
    qid = _question(pg, cands=[{"gtin": "3698", "name": "Rožok so slaninou a syrom 70g"}])
    r = _client().post(f"/api/board/questions/{qid}/answer", json={"choice": "3698"})
    assert r.status_code == 409 and r.get_json()["codex"]["missing"] is True
    assert teach.get(pg, qid)["status"] == "open"


def test_ship_without_is_never_codex_checked(pg):
    _base(pg)
    _codex(pg)
    qid = _question(pg)
    r = _client().post(f"/api/board/questions/{qid}/answer", json={"choice": "ship_without"})
    assert r.status_code == 200


# --- review findings (same branch) ------------------------------------------------------

def test_a_new_card_flag_never_overwrites_an_existing_card_in_either_scope(pg):
    """The Produkty „Nová karta" sends `new: true`: a number that already has a card is
    refused (409 + the card), never an overwrite — the orders form would otherwise CLEAR the
    card's alias (tri-state `doplnok: ""`), the DL one its mass/sklad/cena."""
    _base(pg)
    _codex(pg)
    _seed(pg, G_MUKA, "Múka pšeničná T650", sklad="100", cena=0.37)
    from app.orders import snapshot
    snapshot.upsert_catalog_card(pg, "G-ORD-1", "Rožok grahamový", alias="graham")
    snapshot.rebuild_from_overrides(pg)
    c = _client()
    r = c.post("/api/board/products?scope=dl", json={
        "gtin": G_MUKA, "name": "Iný názov", "sklad": "", "cena": "", "new": True})
    assert r.status_code == 409
    assert r.get_json()["existing"] == {"gtin": G_MUKA, "name": "Múka pšeničná T650"}
    row = pg.execute("SELECT name, sklad, cena FROM dl_catalog_overrides WHERE gtin=%s",
                     (G_MUKA,)).fetchone()
    assert row[0] == "Múka pšeničná T650" and row[1] == "100" and float(row[2]) == 0.37
    r = c.post("/api/board/products?scope=orders", json={
        "gtin": "G-ORD-1", "name": "Iný", "doplnok": "", "new": True})
    assert r.status_code == 409 and r.get_json()["existing"]["gtin"] == "G-ORD-1"
    assert pg.execute("SELECT name, alias FROM catalog_overrides WHERE gtin='G-ORD-1'"
                      ).fetchone() == ("Rožok grahamový", "graham")
    # without the flag (the editor of an existing card) it is a normal update
    assert c.post("/api/board/products?scope=orders", json={
        "gtin": "G-ORD-1", "name": "Rožok grahamový 60g"}).status_code == 200


def test_the_new_card_on_a_question_refuses_a_deleted_cards_number(pg):
    """Review 🔵: a number of a card deleted to the Kôš is not free either — upserting it
    would resurrect the card with blank mass/sklad/cena. Refused; restore it from the Kôš."""
    _base(pg)
    _codex(pg)
    _seed(pg, G_MUKA, "Múka pšeničná T650", sklad="100", cena=0.37)
    dl_snapshot.retire_dl_catalog_card(pg, G_MUKA)
    dl_snapshot.dl_rebuild_from_overrides(pg)
    qid = _question(pg)
    r = _client().post(f"/api/board/questions/{qid}/answer", json={"new_item": {
        "gtin": G_MUKA, "name": "Múka hladká"}})
    assert r.status_code == 409 and "Kôš" in r.get_json()["error"]
    row = pg.execute("SELECT retired, sklad FROM dl_catalog_overrides WHERE gtin=%s",
                     (G_MUKA,)).fetchone()
    assert row == (True, "100"), "the deleted card stays deleted and untouched"
    assert teach.get(pg, qid)["status"] == "open"


def test_a_refused_free_pick_is_not_added_to_the_offered_cards(pg):
    """Review 🔵: the CODEX check runs BEFORE the free/search pick is legitimised, so a refused
    dead code never lingers as an offered button on the question."""
    _base(pg)
    _codex(pg)
    _seed(pg, "3698", "Rožok so slaninou a syrom 70g")
    qid = _question(pg)
    r = _client().post(f"/api/board/questions/{qid}/answer", json={"choice": "3698"})
    assert r.status_code == 409
    assert [c["value"] for c in teach.get(pg, qid)["candidates"]] == []


def test_a_similar_card_we_already_have_carries_our_own_number(pg):
    """Review 🔵: the one-click „Použiť kartu" must send OUR gtin (exact string the catalog
    and the answer path use), not the normalized CODEX code."""
    _base(pg)
    _codex(pg)
    _seed(pg, G_GOOD, "Bagetka s kečupom a syrom 80 gr")
    qid = _question(pg)
    r = _client().post(f"/api/board/questions/{qid}/answer", json={"new_item": {
        "gtin": "3698", "name": "Rožok so slaninou a syrom 70g"}})
    top = r.get_json()["codex"]["similar"][0]
    assert top["catalog_gtin"] == G_GOOD
