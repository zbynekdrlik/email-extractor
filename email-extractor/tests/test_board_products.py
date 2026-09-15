"""Lane 4 of the unified nástenka (#445): Produkty sklad + Produkty objednávky.

Service (`app.board.services.catalog`) + board API under `/api/board/products*`. Every
create/update/delete DELEGATES to the SAME `snapshot`/`dl_snapshot` machinery the old
`/znalosti` endpoints use (these tests assert the identical DB effect + the new audit rows,
never a re-implementation); every alias add/remove delegates to the `memory`/`dl_memory`
write paths. The genuinely-new behaviour: one board page over BOTH catalog scopes reachable
by the `sklad` role (DL products were admin-only before), soft delete (never hard), and the
per-card alias manager.
"""
import os

from app.config import Config
from app.httpapi import create_app, dl_key, sklad_key
from app.orders import dl_memory, dl_snapshot, memory, snapshot

PG_DSN = os.environ.get("PG_TEST_DSN")


def _cfg(data_dir="/tmp"):
    return Config(pg_dsn=PG_DSN, data_dir=data_dir, api_token="tok",
                  dash_password="secret", secret_key="test-secret")


def _client(cfg=None):
    app = create_app(cfg or _cfg())
    app.testing = True
    return app.test_client()


def _sklad(c):
    # the ORDERS warehouse key — session role SKLAD_ROLE
    c.get("/sklad/" + sklad_key("test-secret"))


def _dl(c):
    # the DELIVERY-NOTES warehouse key — session role SKLAD_DL_ROLE
    c.get("/sklad-dl/" + dl_key("test-secret"))


def _login(c):
    c.post("/login", data={"password": "secret"})


def _base_snapshot(pg):
    """A real base snapshot must exist for `rebuild_from_overrides` to freeze anything (in
    prod there always is one). Freeze a minimal base so the raw-snapshot readers
    (`catalog_gtin_set`) actually see override cards after a rebuild."""
    snapshot._freeze(pg, [{"gtin": "BASE0", "name": "Base", "alias": ""}], [])


def _base_snapshot_dl(pg):
    """A DL base snapshot so `dl_rebuild_from_overrides` freezes and the frozen DL catalog
    (what the DL matcher reads) actually reflects override deletes — the DL twin of
    `_base_snapshot`."""
    dl_snapshot._freeze(pg, [{"gtin": "DBASE0", "name": "Base", "doplnok": "",
                              "mass": None, "sklad": "", "cena": None}], [])


def _seed_orders(pg, gtin, name, alias=""):
    snapshot.upsert_catalog_card(pg, gtin, name, alias=alias)
    snapshot.rebuild_from_overrides(pg)


def _seed_dl(pg, gtin, name, doplnok="", mass=None, sklad="", cena=None):
    dl_snapshot.upsert_dl_catalog_card(pg, gtin, name, doplnok=doplnok, mass=mass,
                                       sklad=sklad, cena=cena)
    dl_snapshot.dl_rebuild_from_overrides(pg)


# --- list / search / paging --------------------------------------------------------

def test_orders_products_list_returns_cards(pg):
    _seed_orders(pg, "G1", "Rožok grahamový", alias="graham")
    _seed_orders(pg, "G2", "Chlieb tmavý")
    c = _client()
    _sklad(c)
    r = c.get("/api/board/products?scope=orders")
    assert r.status_code == 200
    data = r.get_json()
    gtins = {i["gtin"] for i in data["items"]}
    assert {"G1", "G2"} <= gtins
    row = next(i for i in data["items"] if i["gtin"] == "G1")
    assert row["name"] == "Rožok grahamový"
    assert row["alias"] == "graham"


def test_dl_products_list_returns_dl_cards(pg):
    _seed_dl(pg, "D1", "Múka T650", doplnok="muka", mass=1.0, sklad="100", cena=0.4)
    c = _client()
    _sklad(c)
    r = c.get("/api/board/products?scope=dl")
    assert r.status_code == 200
    data = r.get_json()
    row = next(i for i in data["items"] if i["gtin"] == "D1")
    assert row["name"] == "Múka T650"
    assert row["doplnok"] == "muka"
    assert row["sklad"] == "100"


def test_search_filters_by_name_gtin_and_alias(pg):
    _seed_orders(pg, "SG1", "Rožok grahamový", alias="graham,pletený")
    _seed_orders(pg, "SG2", "Chlieb tmavý")
    c = _client()
    _sklad(c)
    # by name
    assert {i["gtin"] for i in c.get("/api/board/products?scope=orders&q=graham").get_json()["items"]} == {"SG1"}
    # by gtin
    assert {i["gtin"] for i in c.get("/api/board/products?scope=orders&q=SG2").get_json()["items"]} == {"SG2"}
    # by alias/doplnok phrase
    assert {i["gtin"] for i in c.get("/api/board/products?scope=orders&q=pletený").get_json()["items"]} == {"SG1"}


def test_search_matches_a_card_by_its_memory_alias_wording(pg):
    """Search over „aliases" — a card whose learned wording matches q is returned even when
    its own name/gtin/doplnok do not."""
    _seed_orders(pg, "MG1", "Karta jedna")
    memory.add_global_alias(pg, "úplne iné znenie", "MG1", "Karta jedna", by="test")
    c = _client()
    _sklad(c)
    hits = {i["gtin"] for i in c.get("/api/board/products?scope=orders&q=iné znenie").get_json()["items"]}
    assert "MG1" in hits


def test_list_pages_results(pg):
    for i in range(55):
        snapshot.upsert_catalog_card(pg, f"P{i:03d}", f"Karta {i:03d}")
    c = _client()
    _sklad(c)
    p0 = c.get("/api/board/products?scope=orders&page=0").get_json()
    assert len(p0["items"]) == p0["meta"]["page_size"]
    assert p0["meta"]["total"] == 55
    assert p0["meta"]["has_more"] is True
    p1 = c.get("/api/board/products?scope=orders&page=1").get_json()
    assert len(p1["items"]) == 55 - p0["meta"]["page_size"]
    assert p1["meta"]["has_more"] is False
    # no overlap between pages
    assert not ({i["gtin"] for i in p0["items"]} & {i["gtin"] for i in p1["items"]})


def test_list_meta_carries_the_scope_field_descriptor(pg):
    c = _client()
    _sklad(c)
    ometa = c.get("/api/board/products?scope=orders").get_json()["meta"]
    assert any(f["key"] == "name" for f in ometa["fields"])
    dmeta = c.get("/api/board/products?scope=dl").get_json()["meta"]
    dkeys = {f["key"] for f in dmeta["fields"]}
    assert {"name", "doplnok", "mass", "sklad", "cena"} <= dkeys


def test_an_unknown_scope_is_a_400(pg):
    c = _client()
    _sklad(c)
    assert c.get("/api/board/products?scope=nonsense").status_code == 400


# --- the whole point of lane 4: the sklad role reaches BOTH scopes ------------------

def test_sklad_role_reaches_dl_products_via_the_board_gate(pg):
    """DL products were admin-only on /znalosti; on the board the TAB decides scope, so the
    orders sklad key must reach the DL products list too (spec §6)."""
    _seed_dl(pg, "DLX", "DL karta")
    c = _client()
    _sklad(c)   # the ORDERS key
    r = c.get("/api/board/products?scope=dl")
    assert r.status_code == 200
    assert any(i["gtin"] == "DLX" for i in r.get_json()["items"])
    # and the old admin-only znalosti DL API still refuses this same session
    assert c.get("/api/znalosti/dl-products").status_code == 401


def test_the_board_products_api_needs_a_session(pg):
    assert _client().get("/api/board/products?scope=orders").status_code == 401


# --- create / update delegate to the SAME snapshot machinery + audit ----------------

def test_create_orders_card_has_the_same_db_effect_as_znalosti_and_audits(pg):
    _base_snapshot(pg)
    c = _client()
    _sklad(c)
    r = c.post("/api/board/products?scope=orders",
               json={"gtin": "NEW1", "name": "Nová karta", "doplnok": "alias1"})
    assert r.status_code == 200
    assert r.get_json()["action"] == "create"
    # identical effect to /api/znalosti/products: the card is in the effective catalog
    row = next(x for x in snapshot.catalog_for_management(pg) if x["gtin"] == "NEW1")
    assert row["name"] == "Nová karta" and row["alias"] == "alias1"
    # and it is a real, searchable, teachable card (in catalog_gtin_set after rebuild)
    assert "NEW1" in snapshot.catalog_gtin_set(pg)
    # the board records the change
    n = pg.execute("SELECT count(*) FROM audit_log WHERE table_name='catalog_overrides' "
                   "AND action='create' AND row_id='NEW1'").fetchone()[0]
    assert n == 1


def test_update_orders_card_name_only_keeps_alias_and_audits_update(pg):
    _seed_orders(pg, "UP1", "Staré meno", alias="ponechaj")
    c = _client()
    _sklad(c)
    # name-only edit: no alias/doplnok key -> the tri-state must NOT wipe the alias
    r = c.post("/api/board/products?scope=orders", json={"gtin": "UP1", "name": "Nové meno"})
    assert r.status_code == 200
    assert r.get_json()["action"] == "update"
    row = next(x for x in snapshot.catalog_for_management(pg) if x["gtin"] == "UP1")
    assert row["name"] == "Nové meno"
    assert row["alias"] == "ponechaj"
    n = pg.execute("SELECT count(*) FROM audit_log WHERE table_name='catalog_overrides' "
                   "AND action='update' AND row_id='UP1'").fetchone()[0]
    assert n == 1


def test_create_dl_card_delegates_and_audits(pg):
    c = _client()
    _sklad(c)
    r = c.post("/api/board/products?scope=dl",
               json={"gtin": "DN1", "name": "DL nová", "doplnok": "d", "mass": "1,5",
                     "sklad": "100", "cena": "0,40"})
    assert r.status_code == 200
    row = next(x for x in dl_snapshot.dl_catalog_for_management(pg) if x["gtin"] == "DN1")
    assert row["name"] == "DL nová"
    assert row["mass"] == 1.5   # parse_number handled the comma decimal
    assert row["cena"] == 0.40
    n = pg.execute("SELECT count(*) FROM audit_log WHERE table_name='dl_catalog_overrides' "
                   "AND row_id='DN1'").fetchone()[0]
    assert n == 1


def test_update_dl_card_name_only_keeps_mass_sklad_cena(pg):
    """A DL card edit that sends ONLY name/gtin must NOT wipe mass/sklad/cena/doplnok —
    `dl_snapshot.upsert_dl_catalog_card` overwrites all fields, so `_dl_upsert` reads the
    current card and preserves any field the editor did not send (spec-flagged risk)."""
    _seed_dl(pg, "DKEEP", "Staré", doplnok="d1", mass=1.5, sklad="100", cena=0.4)
    c = _client()
    _sklad(c)
    r = c.post("/api/board/products?scope=dl", json={"gtin": "DKEEP", "name": "Nové meno"})
    assert r.status_code == 200
    assert r.get_json()["action"] == "update"
    row = next(x for x in dl_snapshot.dl_catalog_for_management(pg) if x["gtin"] == "DKEEP")
    assert row["name"] == "Nové meno"
    assert row["mass"] == 1.5
    assert row["sklad"] == "100"
    assert row["cena"] == 0.4
    assert row["doplnok"] == "d1"


def test_create_rejects_missing_gtin_or_name(pg):
    c = _client()
    _sklad(c)
    assert c.post("/api/board/products?scope=orders", json={"name": "x"}).status_code == 400
    assert c.post("/api/board/products?scope=orders", json={"gtin": "g"}).status_code == 400


# --- delete = SOFT delete (never hard) + audit + vanishes from matching -------------

def test_delete_orders_card_soft_deletes_audits_and_vanishes_from_gtin_set(pg):
    _base_snapshot(pg)
    _seed_orders(pg, "DEL1", "Na zmazanie")
    assert "DEL1" in snapshot.catalog_gtin_set(pg)
    c = _client()
    _sklad(c)
    r = c.delete("/api/board/products/DEL1?scope=orders")
    assert r.status_code == 200
    # the override row STAYS (soft delete), with BOTH markers
    row = pg.execute("SELECT retired, deleted_at FROM catalog_overrides "
                     "WHERE gtin='DEL1'").fetchone()
    assert row is not None, "the override row was HARD-deleted"
    assert row[0] is True and row[1] is not None
    # gone from the effective catalog AND from the raw gtin set the matcher/search read
    assert not any(x["gtin"] == "DEL1" for x in snapshot.catalog_for_management(pg))
    assert "DEL1" not in snapshot.catalog_gtin_set(pg)
    n = pg.execute("SELECT count(*) FROM audit_log WHERE table_name='catalog_overrides' "
                   "AND action='delete' AND row_id='DEL1'").fetchone()[0]
    assert n == 1


def test_delete_dl_card_soft_deletes_and_vanishes(pg):
    _base_snapshot_dl(pg)
    _seed_dl(pg, "DDEL", "DL na zmazanie")
    # the frozen DL catalog the matcher reads carries it before the delete
    assert "DDEL" in {x["gtin"] for x in
                      dl_snapshot.load_catalog(pg, dl_snapshot.latest_snapshot_id(pg))}
    c = _client()
    _sklad(c)
    r = c.delete("/api/board/products/DDEL?scope=dl")
    assert r.status_code == 200
    row = pg.execute("SELECT retired, deleted_at FROM dl_catalog_overrides "
                     "WHERE gtin='DDEL'").fetchone()
    assert row[0] is True and row[1] is not None
    assert not any(x["gtin"] == "DDEL" for x in dl_snapshot.dl_catalog_for_management(pg))
    # gone from the frozen DL catalog too (the DL matcher's gtin source) — parity with orders
    assert "DDEL" not in {x["gtin"] for x in
                          dl_snapshot.load_catalog(pg, dl_snapshot.latest_snapshot_id(pg))}
    n = pg.execute("SELECT count(*) FROM audit_log WHERE table_name='dl_catalog_overrides' "
                   "AND action='delete' AND row_id='DDEL'").fetchone()[0]
    assert n == 1


def test_delete_a_missing_card_is_404(pg):
    c = _client()
    _sklad(c)
    assert c.delete("/api/board/products/NOPE?scope=orders").status_code == 404


# --- card detail + aliases (per-customer + global for orders; dl for DL) ------------

def test_card_detail_lists_aliases_and_used_by_count(pg):
    _seed_orders(pg, "CD1", "Karta detail")
    memory.add_global_alias(pg, "globalne znenie", "CD1", "Karta detail", by="test")
    memory.add_customer_alias(pg, "2000000000001", "zakaznicke znenie", "CD1", "Karta detail")
    c = _client()
    _sklad(c)
    d = c.get("/api/board/products/CD1?scope=orders").get_json()
    assert d["card"]["gtin"] == "CD1"
    scopes = {a["scope"] for a in d["aliases"]}
    assert {"global", "customer"} <= scopes
    assert d["counts"]["aliases"] >= 2


def test_card_detail_missing_is_404(pg):
    c = _client()
    _sklad(c)
    assert c.get("/api/board/products/GHOST?scope=orders").status_code == 404


# --- alias add / remove delegate to the memory write paths + audit -----------------

def test_add_global_alias_delegates_to_memory_and_audits(pg):
    _seed_orders(pg, "AG1", "Karta alias")
    c = _client()
    _sklad(c)
    r = c.post("/api/board/products/AG1/aliases?scope=orders", json={"wording": "nove znenie"})
    assert r.status_code == 200
    # the SAME write path /znalosti/global uses
    assert any(a["gtin"] == "AG1" for a in memory.list_global_aliases(pg))
    n = pg.execute("SELECT count(*) FROM audit_log WHERE table_name='global_item_memory' "
                   "AND action='create'").fetchone()[0]
    assert n == 1


def test_add_customer_alias_needs_an_ean_and_delegates(pg):
    _seed_orders(pg, "AC1", "Karta alias c")
    c = _client()
    _sklad(c)
    r = c.post("/api/board/products/AC1/aliases?scope=orders",
               json={"wording": "zakaznicke", "ean": "2000000000001"})
    assert r.status_code == 200
    assert any(a["gtin"] == "AC1" for a in memory.list_customer_aliases(pg, "2000000000001"))


def test_add_dl_alias_needs_a_supplier_ean_and_delegates(pg):
    _seed_dl(pg, "AD1", "DL karta alias")
    c = _client()
    _sklad(c)
    # DL memory is always per-supplier — an ean is required
    assert c.post("/api/board/products/AD1/aliases?scope=dl",
                  json={"wording": "bez eanu"}).status_code == 400
    r = c.post("/api/board/products/AD1/aliases?scope=dl",
               json={"wording": "dl znenie", "ean": "3000000000001"})
    assert r.status_code == 200
    row = pg.execute("SELECT source FROM dl_item_memory WHERE supplier_ean='3000000000001' "
                     "AND gtin='AD1' AND deleted_at IS NULL").fetchone()
    assert row is not None and row[0] == "human"


def test_remove_global_alias_soft_deletes_and_audits(pg):
    _seed_orders(pg, "RG1", "Karta rm")
    rid = memory.add_global_alias(pg, "zmaz ma", "RG1", "Karta rm", by="test")
    c = _client()
    _sklad(c)
    before = pg.execute("SELECT count(*) FROM global_item_memory").fetchone()[0]
    r = c.delete("/api/board/products/RG1/aliases?scope=orders",
                 json={"alias_scope": "global", "id": rid})
    assert r.status_code == 200
    after = pg.execute("SELECT count(*) FROM global_item_memory").fetchone()[0]
    assert after == before, "the alias was HARD-deleted"
    assert pg.execute("SELECT deleted_at FROM global_item_memory WHERE id=%s",
                      (rid,)).fetchone()[0] is not None
    assert all(a["id"] != rid for a in memory.list_global_aliases(pg))
    n = pg.execute("SELECT count(*) FROM audit_log WHERE table_name='global_item_memory' "
                   "AND action='delete' AND row_id=%s", (str(rid),)).fetchone()[0]
    assert n == 1


def test_remove_dl_alias_soft_deletes(pg):
    _seed_dl(pg, "RD1", "DL karta rm")
    assert dl_memory.remember(pg, "3000000000009", "dl znenie", "RD1", "DL karta rm",
                              _today(pg), source="human")
    rid = pg.execute("SELECT id FROM dl_item_memory WHERE supplier_ean='3000000000009' "
                     "AND gtin='RD1'").fetchone()[0]
    c = _client()
    _sklad(c)
    r = c.delete("/api/board/products/RD1/aliases?scope=dl",
                 json={"alias_scope": "dl", "id": rid, "ean": "3000000000009"})
    assert r.status_code == 200
    assert pg.execute("SELECT deleted_at FROM dl_item_memory WHERE id=%s",
                      (rid,)).fetchone()[0] is not None


def _today(conn):
    return conn.execute("SELECT current_date").fetchone()[0]
