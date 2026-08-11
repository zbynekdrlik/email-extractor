"""/znalosti (#104): direct curation of wording->card knowledge, without waiting for the
pipeline to raise an order_questions row first. Dashboard API + auth-gate tests (Flask test
client + real Postgres), same pattern as test_api.py."""
import os

from app.config import Config
from app.httpapi import create_app, sklad_key
from app.orders import dl_snapshot, memory, snapshot

PG_DSN = os.environ.get("PG_TEST_DSN")


def _client():
    cfg = Config(pg_dsn=PG_DSN, data_dir="/tmp", api_token="tok", dash_password="secret",
                 secret_key="test-secret")
    app = create_app(cfg)
    app.testing = True
    return app.test_client()


def _login(c):
    c.post("/login", data={"password": "secret"})


def _snap(pg):
    catalog_csv = ("GTIN,Názov,doplnok\n"
                  "G1,Rožok štandart 50g,\n"
                  "G2,Vianočka 400g,twister\n")
    customer_csv = ("Názov organizácie,EAN kód EDI,E-mail\n"
                    "Pekáreň Rožok,111,pekaren@rozok.sk\n")
    return snapshot.import_snapshot(pg, catalog_csv, customer_csv)


# ---- pages ------------------------------------------------------------------------------

def test_znalosti_page_requires_login(pg):
    _snap(pg)
    assert _client().get("/znalosti").status_code == 302
    assert _client().get("/znalosti/111").status_code == 302


def test_znalosti_page_served_after_login(pg):
    _snap(pg)
    c = _client()
    _login(c)
    r = c.get("/znalosti")
    assert r.status_code == 200
    assert b'data-testid="version"' in r.data
    r2 = c.get("/znalosti/111")
    assert r2.status_code == 200


def test_znalosti_reachable_via_the_warehouse_link(pg):
    """The whole point of #104: reachable from the same signed sklad link as /otazky."""
    _snap(pg)
    c = _client()
    c.get("/sklad/" + sklad_key("test-secret"))
    assert c.get("/znalosti").status_code == 200
    assert c.get("/znalosti/111").status_code == 200
    assert c.get("/api/znalosti/global").status_code == 200
    assert c.get("/api/znalosti/customer/111").status_code == 200
    # the security boundary is unchanged everywhere else
    assert c.get("/api/messages").status_code == 401


# ---- global aliases -----------------------------------------------------------------------

def test_add_and_list_global_alias(pg):
    _snap(pg)
    c = _client()
    _login(c)
    r = c.post("/api/znalosti/global", json={"wording": "Twister", "gtin": "VIA",
                                             "card": "Vianočka 400g"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    items = c.get("/api/znalosti/global").get_json()["items"]
    assert len(items) == 1 and items[0]["gtin"] == "VIA"


def test_add_global_alias_needs_gtin(pg):
    _snap(pg)
    c = _client()
    _login(c)
    r = c.post("/api/znalosti/global", json={"wording": "Twister", "card": "x"})
    assert r.status_code == 400


def test_add_global_alias_conflict_is_409(pg):
    _snap(pg)
    c = _client()
    _login(c)
    c.post("/api/znalosti/global", json={"wording": "Twister", "gtin": "VIA", "card": "x"})
    r = c.post("/api/znalosti/global", json={"wording": "Twister", "gtin": "G50", "card": "y"})
    assert r.status_code == 409


def test_delete_global_alias(pg):
    _snap(pg)
    c = _client()
    _login(c)
    rid = c.post("/api/znalosti/global",
                json={"wording": "Twister", "gtin": "VIA", "card": "x"}).get_json()["id"]
    assert c.delete(f"/api/znalosti/global/{rid}").status_code == 200
    assert memory.resolve_global(pg, "Twister") is None
    assert c.delete(f"/api/znalosti/global/{rid}").status_code == 404


# ---- per-customer aliases -----------------------------------------------------------------

def test_add_and_list_customer_alias(pg):
    _snap(pg)
    c = _client()
    _login(c)
    r = c.post("/api/znalosti/customer/111",
              json={"wording": "rožok", "gtin": "G1", "card": "Rožok štandart 50g"})
    assert r.status_code == 200
    d = c.get("/api/znalosti/customer/111").get_json()
    assert d["customer_name"] == "Pekáreň Rožok"
    assert len(d["items"]) == 1 and d["items"][0]["source"] == "human"


def test_customer_page_data_includes_the_editable_record(pg):
    """#128: the /znalosti/<ean> edit form needs the full row + override identity, not
    just the aliases list this endpoint already returned."""
    _snap(pg)
    c = _client()
    _login(c)
    d = c.get("/api/znalosti/customer/111").get_json()
    assert d["record"]["ean_edi"] == "111"
    assert d["record"]["override_id"] is None
    assert d["record"]["orig_ean_edi"] == "111"


def test_delete_customer_alias_is_scoped_to_its_customer(pg):
    _snap(pg)
    c = _client()
    _login(c)
    rid = c.post("/api/znalosti/customer/111",
                json={"wording": "rožok", "gtin": "G1", "card": "x"}).get_json()["id"]
    assert c.delete(f"/api/znalosti/customer/222/{rid}").status_code == 404
    assert c.delete(f"/api/znalosti/customer/111/{rid}").status_code == 200


def test_customer_alias_actually_wins_at_match_time(pg):
    """Not just stored — resolvable, the same way a teach.answer() click already is."""
    _snap(pg)
    c = _client()
    _login(c)
    c.post("/api/znalosti/customer/111",
          json={"wording": "domáci chlieb", "gtin": "CH1", "card": "Chlieb domáci 1kg"})
    hit = memory.resolve(pg, "111", "domáci chlieb")
    assert hit is not None and hit.gtin == "CH1" and hit.human is True


# ---- search (card / customer picker) -------------------------------------------------------

def test_catalog_search_finds_by_name_substring(pg):
    _snap(pg)
    c = _client()
    _login(c)
    items = c.get("/api/znalosti/catalog?q=rožok").get_json()["items"]
    assert [i["gtin"] for i in items] == ["G1"]


def test_customer_search_finds_by_name_or_ean(pg):
    _snap(pg)
    c = _client()
    _login(c)
    by_name = c.get("/api/znalosti/customers?q=rožok").get_json()["items"]
    assert [i["ean_edi"] for i in by_name] == ["111"]
    by_ean = c.get("/api/znalosti/customers?q=111").get_json()["items"]
    assert [i["ean_edi"] for i in by_ean] == ["111"]


# ---- #127: direct product-card curation ----------------------------------------------------

def test_products_list_and_search(pg):
    _snap(pg)
    c = _client()
    _login(c)
    items = c.get("/api/znalosti/products").get_json()["items"]
    assert {i["gtin"] for i in items} == {"G1", "G2"}
    found = c.get("/api/znalosti/products?q=vianočka").get_json()["items"]
    assert [i["gtin"] for i in found] == ["G2"]


def test_add_a_new_product_card_is_visible_immediately(pg):
    _snap(pg)
    c = _client()
    _login(c)
    r = c.post("/api/znalosti/products", json={"gtin": "NEW1", "name": "Chlieb domáci 1kg"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    items = c.get("/api/znalosti/products?q=domáci").get_json()["items"]
    assert [i["gtin"] for i in items] == ["NEW1"]


def test_add_product_needs_gtin_and_name(pg):
    _snap(pg)
    c = _client()
    _login(c)
    assert c.post("/api/znalosti/products", json={"gtin": "X"}).status_code == 400
    assert c.post("/api/znalosti/products", json={"name": "bez GTIN"}).status_code == 400


def test_edit_an_existing_product_card(pg):
    _snap(pg)
    c = _client()
    _login(c)
    c.post("/api/znalosti/products", json={"gtin": "G1", "name": "Rožok — nový popis"})
    items = c.get("/api/znalosti/products?q=rožok").get_json()["items"]
    assert items[0]["name"] == "Rožok — nový popis"
    assert items[0]["overridden"] is True


def test_retire_a_product_card_removes_it_from_the_list(pg):
    _snap(pg)
    c = _client()
    _login(c)
    assert c.delete("/api/znalosti/products/G1").status_code == 200
    gtins = {i["gtin"] for i in c.get("/api/znalosti/products").get_json()["items"]}
    assert "G1" not in gtins and "G2" in gtins
    assert c.delete("/api/znalosti/products/NOPE").status_code == 404


def test_znalosti_product_writes_reachable_via_the_warehouse_link(pg):
    _snap(pg)
    c = _client()
    c.get("/sklad/" + sklad_key("test-secret"))
    assert c.get("/api/znalosti/products").status_code == 200
    r = c.post("/api/znalosti/products", json={"gtin": "SK1", "name": "od skladu"})
    assert r.status_code == 200
    # retire (DELETE) is method-agnostic in SKLAD_ZNALOSTI_API too, not just GET/POST —
    # review finding: this was untested (mirrors the #93/PR#116 SKLAD_PATHS gap)
    assert c.delete("/api/znalosti/products/SK1").status_code == 200
    assert c.get("/api/messages").status_code == 401


# ---- #128: direct customer curation ---------------------------------------------------------

def test_clients_list_marks_override_identity(pg):
    _snap(pg)
    c = _client()
    _login(c)
    items = c.get("/api/znalosti/clients").get_json()["items"]
    row = next(i for i in items if i["ean_edi"] == "111")
    assert row["override_id"] is None
    assert row["orig_ean_edi"] == "111"


def test_add_a_new_customer_is_visible_immediately(pg):
    _snap(pg)
    c = _client()
    _login(c)
    r = c.post("/api/znalosti/clients", json={
        "ean_edi": "999", "name": "Nový odberateľ", "emails": "novy@x.sk",
        "city": "Košice", "street": "Nová 1", "zip": "04001"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    items = c.get("/api/znalosti/clients?q=nový").get_json()["items"]
    assert [i["ean_edi"] for i in items] == ["999"]


def test_add_customer_needs_a_name(pg):
    _snap(pg)
    c = _client()
    _login(c)
    assert c.post("/api/znalosti/clients", json={"ean_edi": "1"}).status_code == 400


def test_znalosti_clients_post_refuses_a_blank_ean(pg):
    """#234: the EAN kód EDI can never be forgotten, whichever screen a customer is added
    from."""
    _snap(pg)
    c = _client()
    _login(c)
    r = c.post("/api/znalosti/clients", json={"name": "Bez EAN s.r.o."})
    assert r.status_code == 400
    assert c.get("/api/znalosti/clients").get_json()["items"]
    assert not any(i["name"] == "Bez EAN s.r.o."
                  for i in c.get("/api/znalosti/clients").get_json()["items"])
    r2 = c.post("/api/znalosti/clients", json={"ean_edi": "SK-123", "name": "Zlý EAN s.r.o."})
    assert r2.status_code == 400


def test_edit_an_existing_customer_by_its_original_sheet_identity(pg):
    _snap(pg)
    c = _client()
    _login(c)
    items = c.get("/api/znalosti/clients").get_json()["items"]
    row = next(i for i in items if i["ean_edi"] == "111")
    r = c.post("/api/znalosti/clients", json={
        "override_id": row["override_id"], "orig_ean_edi": row["orig_ean_edi"],
        "orig_street": row["orig_street"], "ean_edi": "111", "name": "Pekáreň Rožok OPRAVA",
        "emails": "pekaren@rozok.sk", "city": "", "street": "", "zip": ""})
    assert r.status_code == 200
    items2 = c.get("/api/znalosti/clients").get_json()["items"]
    assert any(i["name"] == "Pekáreň Rožok OPRAVA" for i in items2)
    assert not any(i["name"] == "Pekáreň Rožok" for i in items2)


def test_retire_a_customer_removes_it_from_the_list(pg):
    _snap(pg)
    c = _client()
    _login(c)
    items = c.get("/api/znalosti/clients").get_json()["items"]
    row = next(i for i in items if i["ean_edi"] == "111")
    r = c.delete("/api/znalosti/clients", json={
        "override_id": row["override_id"], "orig_ean_edi": row["orig_ean_edi"],
        "orig_street": row["orig_street"]})
    assert r.status_code == 200
    eans = {i["ean_edi"] for i in c.get("/api/znalosti/clients").get_json()["items"]}
    assert "111" not in eans


def test_znalosti_client_writes_reachable_via_the_warehouse_link(pg):
    _snap(pg)
    c = _client()
    c.get("/sklad/" + sklad_key("test-secret"))
    assert c.get("/api/znalosti/clients").status_code == 200
    # #234: ean_edi must be numeric — a plain code like "SK1" is refused everywhere now
    r = c.post("/api/znalosti/clients", json={"ean_edi": "7000000000501", "name": "od skladu"})
    assert r.status_code == 200
    # retire (DELETE) is method-agnostic in SKLAD_ZNALOSTI_API too, not just GET/POST —
    # review finding: this was untested (mirrors the #93/PR#116 SKLAD_PATHS gap)
    assert c.delete("/api/znalosti/clients", json={"override_id": None, "orig_ean_edi": None,
                                                    "orig_street": None}).status_code == 404
    c.post("/api/znalosti/clients", json={"ean_edi": "7000000000502", "name": "na vyradenie"})
    with_id = c.get("/api/znalosti/clients?q=vyradenie").get_json()["items"][0]
    assert c.delete("/api/znalosti/clients",
                    json={"override_id": with_id["override_id"],
                          "orig_ean_edi": with_id["orig_ean_edi"],
                          "orig_street": with_id["orig_street"]}).status_code == 200
    assert c.get("/api/messages").status_code == 401


# ---- #221: DL catalog + supplier curation (mirror of #127/#128 above, DL's own line) -----

DL_CATALOG_CSV = ("GTIN,Názov,doplnok,hmotnost,Sklad,Cena\n"
                  "D1,Múka hladká T512 25kg,,25,100,\"12,50\"\n"
                  "D2,Olej repkový 10l,olej repka,10,200,\"9,90\"\n")
DL_OBJEDNAVKY_CSV = "GTIN,Sklad,Názov,doplnok\n"
DL_SUPPLIER_CSV = ("Názov organizácie,EAN kód EDI,Obec,E-mail\n"
                   "Signatus s.r.o.,8586010000001,Košice,objednavky@signatus.sk\n")


def _dl_snap(pg):
    return dl_snapshot.import_snapshot(pg, DL_CATALOG_CSV, DL_OBJEDNAVKY_CSV, DL_SUPPLIER_CSV)


def test_dl_products_list_and_search(pg):
    _dl_snap(pg)
    c = _client()
    _login(c)
    items = c.get("/api/znalosti/dl-products").get_json()["items"]
    assert {i["gtin"] for i in items} == {"D1", "D2"}
    found = c.get("/api/znalosti/dl-products?q=olej").get_json()["items"]
    assert [i["gtin"] for i in found] == ["D2"]


def test_add_a_new_dl_product_card_is_visible_immediately(pg):
    _dl_snap(pg)
    c = _client()
    _login(c)
    r = c.post("/api/znalosti/dl-products", json={"gtin": "NEW1", "name": "Chlieb domáci 1kg",
                                                   "mass": "1,2", "sklad": "50", "cena": "2,5"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    items = c.get("/api/znalosti/dl-products?q=domáci").get_json()["items"]
    assert [i["gtin"] for i in items] == ["NEW1"]
    assert items[0]["mass"] == 1.2
    assert items[0]["cena"] == 2.5


def test_add_dl_product_needs_gtin_and_name(pg):
    _dl_snap(pg)
    c = _client()
    _login(c)
    assert c.post("/api/znalosti/dl-products", json={"gtin": "X"}).status_code == 400
    assert c.post("/api/znalosti/dl-products", json={"name": "bez GTIN"}).status_code == 400


def test_edit_an_existing_dl_product_card(pg):
    _dl_snap(pg)
    c = _client()
    _login(c)
    c.post("/api/znalosti/dl-products", json={"gtin": "D1", "name": "Múka — nový popis"})
    items = c.get("/api/znalosti/dl-products?q=múka").get_json()["items"]
    assert items[0]["name"] == "Múka — nový popis"
    assert items[0]["overridden"] is True


def test_editing_a_dl_product_card_is_a_full_replace_not_a_partial_patch(pg):
    """Review finding on PR #223: upsert_dl_catalog_card replaces ALL fields on every
    call, same design as #127's upsert_catalog_card — a POST that sends only gtin+name
    (no doplnok/mass/sklad/cena) blanks the others rather than keeping them. This is
    INTENTIONAL (the /znalosti form always pre-fills the whole row before saving, same
    as productsBox/clientsBox already do) — pinned explicitly so it stays a documented
    contract, not an accidental gap."""
    _dl_snap(pg)
    c = _client()
    _login(c)
    c.post("/api/znalosti/dl-products", json={"gtin": "D1", "name": "Múka s detailmi",
                                               "mass": "25", "sklad": "100", "cena": "12,5"})
    c.post("/api/znalosti/dl-products", json={"gtin": "D1", "name": "Múka - iba názov"})
    items = c.get("/api/znalosti/dl-products?q=múka").get_json()["items"]
    assert items[0]["name"] == "Múka - iba názov"
    assert items[0]["mass"] is None and items[0]["sklad"] == "" and items[0]["cena"] is None


def test_retire_a_dl_product_card_removes_it_from_the_list(pg):
    _dl_snap(pg)
    c = _client()
    _login(c)
    assert c.delete("/api/znalosti/dl-products/D1").status_code == 200
    gtins = {i["gtin"] for i in c.get("/api/znalosti/dl-products").get_json()["items"]}
    assert "D1" not in gtins and "D2" in gtins
    assert c.delete("/api/znalosti/dl-products/NOPE").status_code == 404


def test_znalosti_dl_product_writes_reachable_via_the_warehouse_link(pg):
    _dl_snap(pg)
    c = _client()
    c.get("/sklad/" + sklad_key("test-secret"))
    assert c.get("/api/znalosti/dl-products").status_code == 200
    r = c.post("/api/znalosti/dl-products", json={"gtin": "SK1", "name": "od skladu"})
    assert r.status_code == 200
    assert c.delete("/api/znalosti/dl-products/SK1").status_code == 200
    assert c.get("/api/messages").status_code == 401


def test_dl_suppliers_list_marks_override_identity(pg):
    _dl_snap(pg)
    c = _client()
    _login(c)
    items = c.get("/api/znalosti/dl-suppliers").get_json()["items"]
    row = next(i for i in items if i["ean_edi"] == "8586010000001")
    assert row["override_id"] is None
    assert row["orig_ean_edi"] == "8586010000001"


def test_add_a_new_dl_supplier_is_visible_immediately(pg):
    _dl_snap(pg)
    c = _client()
    _login(c)
    r = c.post("/api/znalosti/dl-suppliers", json={
        "ean_edi": "999", "name": "Nový dodávateľ", "emails": "novy@x.sk", "city": "Košice"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    items = c.get("/api/znalosti/dl-suppliers?q=nový").get_json()["items"]
    assert [i["ean_edi"] for i in items] == ["999"]


def test_add_dl_supplier_needs_a_name(pg):
    _dl_snap(pg)
    c = _client()
    _login(c)
    assert c.post("/api/znalosti/dl-suppliers", json={"ean_edi": "1"}).status_code == 400


def test_edit_an_existing_dl_supplier_by_its_original_identity(pg):
    _dl_snap(pg)
    c = _client()
    _login(c)
    items = c.get("/api/znalosti/dl-suppliers").get_json()["items"]
    row = next(i for i in items if i["ean_edi"] == "8586010000001")
    r = c.post("/api/znalosti/dl-suppliers", json={
        "override_id": row["override_id"], "orig_ean_edi": row["orig_ean_edi"],
        "orig_city": row["orig_city"], "ean_edi": "8586010000001",
        "name": "Signatus OPRAVA", "emails": "objednavky@signatus.sk", "city": "Košice"})
    assert r.status_code == 200
    items2 = c.get("/api/znalosti/dl-suppliers").get_json()["items"]
    assert any(i["name"] == "Signatus OPRAVA" for i in items2)
    assert not any(i["name"] == "Signatus s.r.o." for i in items2)


def test_retire_a_dl_supplier_removes_it_from_the_list(pg):
    _dl_snap(pg)
    c = _client()
    _login(c)
    items = c.get("/api/znalosti/dl-suppliers").get_json()["items"]
    row = next(i for i in items if i["ean_edi"] == "8586010000001")
    r = c.delete("/api/znalosti/dl-suppliers", json={
        "override_id": row["override_id"], "orig_ean_edi": row["orig_ean_edi"],
        "orig_city": row["orig_city"]})
    assert r.status_code == 200
    eans = {i["ean_edi"] for i in c.get("/api/znalosti/dl-suppliers").get_json()["items"]}
    assert "8586010000001" not in eans


def test_znalosti_dl_supplier_writes_reachable_via_the_warehouse_link(pg):
    _dl_snap(pg)
    c = _client()
    c.get("/sklad/" + sklad_key("test-secret"))
    assert c.get("/api/znalosti/dl-suppliers").status_code == 200
    r = c.post("/api/znalosti/dl-suppliers", json={"ean_edi": "SK1", "name": "od skladu"})
    assert r.status_code == 200
    assert c.delete("/api/znalosti/dl-suppliers",
                    json={"override_id": None, "orig_ean_edi": None,
                          "orig_city": None}).status_code == 404
    c.post("/api/znalosti/dl-suppliers", json={"ean_edi": "SK2", "name": "na vyradenie"})
    with_id = c.get("/api/znalosti/dl-suppliers?q=vyradenie").get_json()["items"][0]
    assert c.delete("/api/znalosti/dl-suppliers",
                    json={"override_id": with_id["override_id"],
                          "orig_ean_edi": with_id["orig_ean_edi"],
                          "orig_city": with_id["orig_city"]}).status_code == 200
    assert c.get("/api/messages").status_code == 401


def test_sklad_link_does_not_match_a_path_that_merely_starts_with_dl_products(pg):
    """Review finding on PR #223: SKLAD_ZNALOSTI_API's new `dl-products(/[^/]+)?|
    dl-suppliers` alternatives are anchored with ^/$ — pin that an unrelated path
    sharing the prefix is NOT accidentally granted to the unauthenticated warehouse
    link (a regex without `$` would match this)."""
    _dl_snap(pg)
    c = _client()
    c.get("/sklad/" + sklad_key("test-secret"))
    assert c.get("/api/znalosti/dl-productsX").status_code == 401
    assert c.get("/api/znalosti/dl-suppliersX").status_code == 401
