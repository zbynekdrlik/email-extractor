"""#467 — the nástenka never saves a DL card number CODEX has no stock card for.

Since #477 no path CREATES a card from a typed number any more (403, a card comes only from the
CODEX picker — `test_board_codex_pick.py`), so the #467 CODEX check guards what is left: an EDIT
of a DL card (board Produkty sklad, legacy `/api/znalosti/dl-products`), a dl_item answer that
picks a card, and the Kôš restore. A refusal is a 409 naming CODEX cards with a similar name and
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
CODEX_ONLY = "len výberom z CODEXu"


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

def test_editing_a_dl_card_to_a_code_codex_lacks_names_similar_codex_cards(pg):
    """The refusal help: CODEX cards with a similar name + their code, and whether (and under
    which name) we already have them."""
    _base(pg)
    _codex(pg)
    _seed(pg, G_GOOD, "Bagetka s kečupom a syrom 80 gr", sklad="1")
    _seed(pg, "3698", "Rožok so slaninou a syrom 70g")
    r = _client().post("/api/board/products?scope=dl",
                       json={"gtin": "3698", "name": "Rožok so slaninou a syrom 70g"})
    assert r.status_code == 409
    body = r.get_json()
    assert "3698" in body["error"] and "CODEX" in body["error"]
    top = body["codex"]["similar"][0]
    assert top["code"] == G_GOOD and top["name"] == "Rožok so slaninou a syrom 70g"
    assert top["in_catalog"] is True and top["catalog_name"] == "Bagetka s kečupom a syrom 80 gr"
    assert pg.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 0


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
    _seed(pg, "3698", "Rožok so slaninou a syrom 70g")
    r = _client().post("/api/board/products?scope=dl",
                       json={"gtin": "3698", "name": "Rožok so slaninou 70g"})
    assert r.status_code == 200


def test_the_orders_catalog_is_not_codex_checked(pg):
    """An edit of an orders card is never CODEX-checked (the #467 check is the DL import's)."""
    _codex(pg)
    from app.orders import snapshot
    snapshot.upsert_catalog_card(pg, "3698", "X")
    snapshot.rebuild_from_overrides(pg)
    r = _client().post("/api/board/products?scope=orders", json={"gtin": "3698", "name": "Y"})
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
    _seed(pg, "3698", "Rožok")
    c = _client()
    c.post("/login", data={"password": "secret"})
    r = c.post("/api/znalosti/dl-products", json={"gtin": "3698", "name": "Rožok 70g"})
    assert r.status_code == 409 and r.get_json()["codex"]["code"] == "3698"


# --- a dl_item answer that picks a card -----------------------------------------------

def _question(pg, cands=None):
    return teach.ask_dl_item(pg, message_id="m467", supplier_ean="S1",
                             supplier_name="Pekáreň s.r.o.",
                             wording="Rožok so slaninou a syrom 70g", quantity=20, unit="ks",
                             candidates=cands or [])


def test_picking_a_catalog_card_whose_code_codex_lacks_is_refused(pg):
    """An existing (old) question can still offer a card CODEX dropped — it may not teach a
    dead code."""
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
    """A `new: true` POST is a creation — refused since #477 (403), never an overwrite: the
    orders card keeps its alias, the DL one its mass/sklad/cena."""
    _base(pg)
    _codex(pg)
    _seed(pg, G_MUKA, "Múka pšeničná T650", sklad="100", cena=0.37)
    from app.orders import snapshot
    snapshot.upsert_catalog_card(pg, "G-ORD-1", "Rožok grahamový", alias="graham")
    snapshot.rebuild_from_overrides(pg)
    c = _client()
    r = c.post("/api/board/products?scope=dl", json={
        "gtin": G_MUKA, "name": "Iný názov", "sklad": "", "cena": "", "new": True})
    assert r.status_code == 403 and CODEX_ONLY in r.get_json()["error"]
    row = pg.execute("SELECT name, sklad, cena FROM dl_catalog_overrides WHERE gtin=%s",
                     (G_MUKA,)).fetchone()
    assert row[0] == "Múka pšeničná T650" and row[1] == "100" and float(row[2]) == 0.37
    r = c.post("/api/board/products?scope=orders", json={
        "gtin": "G-ORD-1", "name": "Iný", "doplnok": "", "new": True})
    assert r.status_code == 403
    assert pg.execute("SELECT name, alias FROM catalog_overrides WHERE gtin='G-ORD-1'"
                      ).fetchone() == ("Rožok grahamový", "graham")
    # without the flag (the editor of an existing card) it is a normal update
    assert c.post("/api/board/products?scope=orders", json={
        "gtin": "G-ORD-1", "name": "Rožok grahamový 60g"}).status_code == 200


def test_the_produkty_new_card_refuses_a_deleted_cards_number_too(pg):
    """A card in the Kôš is not overwritten by a creation attempt either — it would wipe
    sklad/cena while the card stayed hidden (the #462 xN class after a restore)."""
    _base(pg)
    _codex(pg)
    _seed(pg, G_MUKA, "Múka pšeničná T650", sklad="100", cena=0.37)
    dl_snapshot.retire_dl_catalog_card(pg, G_MUKA)
    dl_snapshot.dl_rebuild_from_overrides(pg)
    r = _client().post("/api/board/products?scope=dl", json={
        "gtin": G_MUKA, "name": "Múka hladká", "sklad": "", "cena": "", "new": True})
    assert r.status_code == 403
    row = pg.execute("SELECT name, retired, sklad, cena FROM dl_catalog_overrides "
                     "WHERE gtin=%s", (G_MUKA,)).fetchone()
    assert row[:3] == ("Múka pšeničná T650", True, "100") and float(row[3]) == 0.37


def test_a_new_card_number_with_leading_zeros_is_never_a_duplicate(pg):
    """„0" + an existing code would be a SECOND card for one CODEX code — no typed path can
    create it (403), neither the Produkty „Nová karta" nor the question's „➕ Nová karta"."""
    _base(pg)
    _codex(pg)
    _seed(pg, G_MUKA, "Múka pšeničná T650", sklad="100", cena=0.37)
    c = _client()
    r = c.post("/api/board/products?scope=dl", json={
        "gtin": "0" + G_MUKA, "name": "Múka", "new": True})
    assert r.status_code == 403
    qid = _question(pg)
    r = c.post(f"/api/board/questions/{qid}/answer", json={"new_item": {
        "gtin": "0" + G_MUKA, "name": "Múka"}})
    assert r.status_code == 403
    assert pg.execute("SELECT count(*) FROM dl_catalog_overrides WHERE gtin=%s",
                      ("0" + G_MUKA,)).fetchone()[0] == 0


def test_restoring_a_deleted_card_whose_code_codex_lacks_is_refused(pg):
    """The Kôš restore is the one other way a DL card number goes live again — a dead code
    (the incident's 3698, deleted by the sklad) must not come back."""
    _base(pg)
    _codex(pg)
    _seed(pg, "3698", "Rožok so slaninou a syrom 70g")
    c = _client()
    c.post("/login", data={"password": "secret"})
    assert c.delete("/api/board/products/3698?scope=dl").status_code == 200
    aid = pg.execute("SELECT id FROM audit_log WHERE table_name='dl_catalog_overrides' "
                     "AND row_id='3698' AND action='delete'").fetchone()[0]
    r = c.post(f"/api/board/audit/{aid}/restore")
    assert r.status_code == 409 and "3698" in r.get_json()["error"]
    assert pg.execute("SELECT retired FROM dl_catalog_overrides WHERE gtin='3698'"
                      ).fetchone()[0] is True


def _restore_id(pg, table, row_id):
    return pg.execute("SELECT id FROM audit_log WHERE table_name=%s AND row_id=%s "
                      "AND action='delete'", (table, row_id)).fetchone()[0]


def test_no_write_path_creates_a_new_number_written_unlike_codex(pg):
    """The source of such duplicates is closed too — the legacy API and a board POST without
    `new` refuse a NEW number (any NEW number since #477) like the „Nová karta" does."""
    _base(pg)
    _codex(pg)
    c = _client()
    c.post("/login", data={"password": "secret"})
    r = c.post("/api/znalosti/dl-products", json={"gtin": "0" + G_MUKA, "name": "Múka"})
    assert r.status_code == 403 and CODEX_ONLY in r.get_json()["error"]
    r = c.post("/api/board/products?scope=dl", json={"gtin": "0" + G_MUKA, "name": "Múka"})
    assert r.status_code == 403 and CODEX_ONLY in r.get_json()["error"]
    assert pg.execute("SELECT count(*) FROM dl_catalog_overrides WHERE gtin=%s",
                      ("0" + G_MUKA,)).fetchone()[0] == 0


def test_the_restore_guard_refuses_only_dead_dl_codes(pg):
    """With a live CODEX list, restoring a DL card whose code CODEX has works, and a deleted
    ORDERS card is not CODEX-checked at all."""
    _base(pg)
    _codex(pg)
    _seed(pg, G_MUKA, "Múka pšeničná T650", sklad="100")
    from app.orders import snapshot
    snapshot.upsert_catalog_card(pg, "3698", "Rožok grahamový")
    snapshot.rebuild_from_overrides(pg)
    c = _client()
    c.post("/login", data={"password": "secret"})
    assert c.delete(f"/api/board/products/{G_MUKA}?scope=dl").status_code == 200
    assert c.delete("/api/board/products/3698?scope=orders").status_code == 200
    r = c.post(f"/api/board/audit/{_restore_id(pg, 'dl_catalog_overrides', G_MUKA)}/restore")
    assert r.status_code == 200
    assert pg.execute("SELECT retired, deleted_at FROM dl_catalog_overrides WHERE gtin=%s",
                      (G_MUKA,)).fetchone() == (False, None)
    r = c.post(f"/api/board/audit/{_restore_id(pg, 'catalog_overrides', '3698')}/restore")
    assert r.status_code == 200
    assert pg.execute("SELECT deleted_at FROM catalog_overrides WHERE gtin='3698'"
                      ).fetchone()[0] is None


def test_a_refused_free_pick_is_not_added_to_the_offered_cards(pg):
    """The CODEX check runs BEFORE a free/search pick is legitimised, so a refused dead code
    never lingers as an offered button on the question."""
    _base(pg)
    _codex(pg)
    _seed(pg, "3698", "Rožok so slaninou a syrom 70g")
    qid = _question(pg)
    r = _client().post(f"/api/board/questions/{qid}/answer", json={"choice": "3698"})
    assert r.status_code == 409
    assert [c["value"] for c in teach.get(pg, qid)["candidates"]] == []


def test_a_similar_card_we_already_have_carries_our_own_number(pg):
    """The one-click „Použiť kartu" in the refusal help must send OUR gtin (the exact string
    the catalog and the answer path use), not the normalized CODEX code."""
    _base(pg)
    _codex(pg)
    _seed(pg, G_GOOD, "Bagetka s kečupom a syrom 80 gr")
    qid = _question(pg)
    r = _client().post(f"/api/board/questions/{qid}/answer", json={"choice": "3698"})
    assert r.status_code == 409
    top = r.get_json()["codex"]["similar"][0]
    assert top["catalog_gtin"] == G_GOOD
