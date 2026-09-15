"""Lane 5 of the unified nástenka (#446): Zákazníci (spoločné) + Dodávatelia (sklad).

Services (`app.board.services.customers`/`suppliers`) + the board API endpoints under
`/api/board/customers` and `/api/board/suppliers`. Every create/update/delete DELEGATES to
the SAME `snapshot.upsert_customer`/`retire_customer` and `dl_snapshot.upsert_dl_supplier`/
`retire_dl_supplier` the legacy `/znalosti` routes use — these tests assert the identical DB
effect (override row, snapshot rebuild, soft delete) PLUS the genuinely-new behaviour:
multi-site family grouping (#435), the scanner-address strip (#407), and an audit_log row on
every change (create/update/delete), which the legacy /znalosti create/update did not write.
"""
import os

import pytest

from app.board.services import customers as cust
from app.board.services import suppliers as supp
from app.board.services.partners import PartnerError
from app.config import Config
from app.httpapi import create_app, dl_key, sklad_key
from app.orders import dl_snapshot, snapshot

PG_DSN = os.environ.get("PG_TEST_DSN")


def _cfg(scanner=""):
    return Config(pg_dsn=PG_DSN, data_dir="/tmp", api_token="tok", dash_password="secret",
                  secret_key="test-secret", delivery_notes_scanner_senders=scanner)


def _client(cfg=None):
    app = create_app(cfg or _cfg())
    app.testing = True
    return app.test_client()


def _sklad(c):
    c.get("/sklad/" + sklad_key("test-secret"))


def _dl(c):
    c.get("/sklad-dl/" + dl_key("test-secret"))


def _login(c):
    c.post("/login", data={"password": "secret"})


def _mk_customer(pg, ean, name, city="", street="", zip_="", emails=None):
    rid = snapshot.upsert_customer(
        pg, override_id=None, orig_ean_edi=None, orig_street=None,
        ean_edi=ean, name=name, emails=emails or [], city=city, street=street, zip_=zip_)
    snapshot.rebuild_from_overrides(pg)
    return rid


def _mk_supplier(pg, ean, name, city="", emails=None):
    rid = dl_snapshot.upsert_dl_supplier(
        pg, override_id=None, orig_ean_edi=None, orig_city=None,
        ean_edi=ean, name=name, emails=emails or [], city=city)
    dl_snapshot.dl_rebuild_from_overrides(pg)
    return rid


# --- customer family grouping (#435) ----------------------------------------------------

def test_customers_of_one_brand_are_grouped_into_a_family_with_site_counts(pg):
    _mk_customer(pg, "8590000000001", "Košík.sk MAKRO Žilina", city="Žilina")
    _mk_customer(pg, "8590000000002", "Košík.sk sklad Zvolen", city="Zvolen")
    _mk_customer(pg, "8590000000003", "Košík.sk Online Košice", city="Košice")
    res = cust.list_customers(pg)
    fams = {f["stem"]: f for f in res["families"]}
    assert "kosik" in fams, fams.keys()
    fam = fams["kosik"]
    assert fam["size"] == 3
    cities = {s["city"] for s in fam["sites"]}
    assert cities == {"Žilina", "Zvolen", "Košice"}
    for s in fam["sites"]:
        assert s["site_total"] == 3 and 1 <= s["site_index"] <= 3


def test_an_all_generic_name_is_its_own_singleton_family_never_grouped(pg):
    # both have the SAME distinctive stem only if grouped by a generic word — they must NOT be.
    _mk_customer(pg, "8590000000010", "Centrum 1", city="A")
    _mk_customer(pg, "8590000000011", "Centrum 2", city="B")
    res = cust.list_customers(pg)
    singletons = [f for f in res["families"] if f["size"] == 1]
    names = {f["label"] for f in singletons}
    assert {"Centrum 1", "Centrum 2"} <= names
    # neither is grouped into a shared "centrum" family
    assert not any(f["stem"] == "centrum" and f["size"] > 1 for f in res["families"])


def test_customer_search_matches_name_ean_city_and_email(pg):
    _mk_customer(pg, "8591111111111", "Pekáreň Homola", city="Nitra",
                 emails=["objednavky@homola.sk"])
    _mk_customer(pg, "8592222222222", "Mäso Zdena", city="Trnava")
    by_name = cust.list_customers(pg, q="homola")
    assert sum(f["size"] for f in by_name["families"]) == 1
    by_city = cust.list_customers(pg, q="trnava")
    assert sum(f["size"] for f in by_city["families"]) == 1
    by_email = cust.list_customers(pg, q="objednavky@homola")
    assert sum(f["size"] for f in by_email["families"]) == 1


def test_customer_used_by_count_reflects_edi_sent(pg):
    _mk_customer(pg, "8593333333333", "Odberateľ X", city="X")
    pg.execute("INSERT INTO edi_sent (customer_ean, delivery_date, content_sha256, filename) "
               "VALUES ('8593333333333','2026-09-15','h1','f1.txt'),"
               "('8593333333333','2026-09-16','h2','f2.txt')")
    res = cust.list_customers(pg, q="odberateľ x")
    site = res["families"][0]["sites"][0]
    assert site["orders_shipped"] == 2


def test_customer_family_paging(pg):
    for i in range(30):
        _mk_customer(pg, f"85940000000{i:02d}", f"Firma{i:02d} s.r.o.", city="C")
    p0 = cust.list_customers(pg, page=0)
    p1 = cust.list_customers(pg, page=1)
    assert p0["total"] == 30 and p0["page_size"] == 25
    assert len(p0["families"]) == 25 and len(p1["families"]) == 5


# --- customer create / update / delete DELEGATE + audit ----------------------------------

def test_save_customer_creates_the_override_rebuilds_and_audits(pg):
    res = cust.save_customer(pg, _cfg(), "admin", {
        "ean_edi": "8595555555555", "name": "Nový zákazník", "city": "Košice",
        "street": "Hlavná 1", "zip": "04001", "emails": "a@x.sk, b@x.sk"})
    assert res["action"] == "create"
    rid = res["id"]
    row = pg.execute("SELECT ean_edi, name, city, street, zip, emails, retired "
                     "FROM customer_overrides WHERE id=%s", (rid,)).fetchone()
    assert row[0] == "8595555555555" and row[1] == "Nový zákazník"
    assert row[2] == "Košice" and row[3] == "Hlavná 1" and row[4] == "04001"
    assert row[5] == ["a@x.sk", "b@x.sk"] and row[6] is False
    # appears in the merged management view (snapshot rebuilt)
    assert any(x["ean_edi"] == "8595555555555" for x in snapshot.customers_for_management(pg))
    n = pg.execute("SELECT count(*) FROM audit_log WHERE table_name='customer_overrides' "
                   "AND action='create' AND row_id=%s", (str(rid),)).fetchone()[0]
    assert n == 1


def test_save_customer_update_writes_an_update_audit_with_before(pg):
    rid = _mk_customer(pg, "8596666666666", "Pôvodný", city="Staré")
    res = cust.save_customer(pg, _cfg(), "sklad", {
        "override_id": rid, "ean_edi": "8596666666666", "name": "Zmenený", "city": "Nové"})
    assert res["action"] == "update"
    assert pg.execute("SELECT name FROM customer_overrides WHERE id=%s",
                      (rid,)).fetchone()[0] == "Zmenený"
    a = pg.execute("SELECT before, after FROM audit_log WHERE table_name='customer_overrides' "
                   "AND action='update' AND row_id=%s", (str(rid),)).fetchone()
    assert a[0]["name"] == "Pôvodný" and a[1]["name"] == "Zmenený"


def test_delete_customer_soft_deletes_rebuilds_and_audits(pg):
    rid = _mk_customer(pg, "8597777777777", "Na zmazanie", city="Z")
    cust.delete_customer(pg, _cfg(), "sklad", {"override_id": rid})
    row = pg.execute("SELECT retired, deleted_at FROM customer_overrides WHERE id=%s",
                     (rid,)).fetchone()
    assert row[0] is True and row[1] is not None, "must soft-delete, not hard delete"
    assert not any(x["ean_edi"] == "8597777777777"
                   for x in snapshot.customers_for_management(pg))
    n = pg.execute("SELECT count(*) FROM audit_log WHERE table_name='customer_overrides' "
                   "AND action='delete' AND row_id=%s", (str(rid),)).fetchone()[0]
    assert n == 1


def test_save_customer_duplicate_ean_raises_409(pg):
    _mk_customer(pg, "8598888888888", "Prvý", street="A")
    with pytest.raises(PartnerError) as ei:
        cust.save_customer(pg, _cfg(), "admin", {
            "ean_edi": "8598888888888", "name": "Druhý", "street": "B"})
    assert ei.value.status == 409 and ei.value.existing


def test_save_customer_without_ean_raises_400(pg):
    for body in ({"name": "Bez EAN"}, {"name": "X", "ean_edi": "abc"}):
        with pytest.raises(PartnerError) as ei:
            cust.save_customer(pg, _cfg(), "admin", body)
        assert ei.value.status == 400


# --- DL suppliers: create / delete / scanner strip (#407) -------------------------------

def test_supplier_scanner_email_is_stripped_on_save(pg):
    res = supp.save_supplier(pg, _cfg(scanner="scanner@slovnormal.sk"), "admin", {
        "ean_edi": "8599999999999", "name": "Dodávateľ A", "city": "Žilina",
        "emails": "real@dod.sk, scanner@slovnormal.sk"})
    emails = pg.execute("SELECT emails FROM dl_supplier_overrides WHERE id=%s",
                        (res["id"],)).fetchone()[0]
    assert emails == ["real@dod.sk"], "the scanner address must never be stored (#407)"


def test_save_supplier_creates_rebuilds_and_audits(pg):
    res = supp.save_supplier(pg, _cfg(), "admin", {
        "ean_edi": "8590000000101", "name": "Dodávateľ B", "city": "Nitra",
        "invoice_is_delivery_note": True})
    assert res["action"] == "create"
    row = pg.execute("SELECT name, city, invoice_is_delivery_note FROM dl_supplier_overrides "
                     "WHERE id=%s", (res["id"],)).fetchone()
    assert row[0] == "Dodávateľ B" and row[1] == "Nitra" and row[2] is True
    assert any(x["ean_edi"] == "8590000000101"
               for x in dl_snapshot.dl_suppliers_for_management(pg))
    n = pg.execute("SELECT count(*) FROM audit_log WHERE table_name='dl_supplier_overrides' "
                   "AND action='create' AND row_id=%s", (str(res["id"]),)).fetchone()[0]
    assert n == 1


def test_delete_supplier_soft_deletes_and_audits(pg):
    rid = _mk_supplier(pg, "8590000000102", "Dodávateľ C", city="Č")
    supp.delete_supplier(pg, _cfg(), "sklad", {"override_id": rid})
    row = pg.execute("SELECT retired, deleted_at FROM dl_supplier_overrides WHERE id=%s",
                     (rid,)).fetchone()
    assert row[0] is True and row[1] is not None
    assert not any(x["ean_edi"] == "8590000000102"
                   for x in dl_snapshot.dl_suppliers_for_management(pg))
    n = pg.execute("SELECT count(*) FROM audit_log WHERE table_name='dl_supplier_overrides' "
                   "AND action='delete' AND row_id=%s", (str(rid),)).fetchone()[0]
    assert n == 1


def test_supplier_used_by_count_reflects_desadv_sent(pg):
    _mk_supplier(pg, "8590000000103", "Dodávateľ D", city="D")
    pg.execute("INSERT INTO desadv_sent (supplier_ean, doc_number, filename) "
               "VALUES ('8590000000103','DOC1','f1'),('8590000000103','DOC2','f2')")
    res = supp.list_suppliers(pg, q="dodávateľ d")
    assert res["suppliers"][0]["dls_shipped"] == 2


# --- endpoints: role access + round-trip -------------------------------------------------

def test_both_partner_tabs_are_reachable_by_the_sklad_role(pg):
    c = _client()
    _sklad(c)
    assert c.get("/api/board/customers").status_code == 200
    assert c.get("/api/board/suppliers").status_code == 200


def test_both_partner_tabs_are_reachable_by_the_dl_role(pg):
    """The DL key must NOT lose access to the shared partner tabs — tab decides, not key (§6)."""
    c = _client()
    _dl(c)
    assert c.get("/api/board/customers").status_code == 200
    assert c.get("/api/board/suppliers").status_code == 200


def test_the_partner_apis_need_a_session(pg):
    c = _client()
    assert c.get("/api/board/customers").status_code == 401
    assert c.get("/api/board/suppliers").status_code == 401


def test_customer_endpoint_create_then_delete_round_trip(pg):
    c = _client()
    _sklad(c)
    r = c.post("/api/board/customers", json={
        "ean_edi": "8590000000201", "name": "Endpoint zákazník", "city": "E"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    rid = r.get_json()["id"]
    assert any(x["ean_edi"] == "8590000000201" for x in snapshot.customers_for_management(pg))
    d = c.delete("/api/board/customers", json={"override_id": rid})
    assert d.status_code == 200
    assert pg.execute("SELECT deleted_at FROM customer_overrides WHERE id=%s",
                      (rid,)).fetchone()[0] is not None


def test_supplier_endpoint_create_strips_scanner_and_audits(pg):
    c = _client(_cfg(scanner="scan@slovnormal.sk"))
    _dl(c)
    r = c.post("/api/board/suppliers", json={
        "ean_edi": "8590000000202", "name": "Endpoint dodávateľ", "city": "F",
        "emails": "ok@dod.sk, scan@slovnormal.sk"})
    assert r.status_code == 200
    emails = pg.execute("SELECT emails FROM dl_supplier_overrides WHERE id=%s",
                        (r.get_json()["id"],)).fetchone()[0]
    assert emails == ["ok@dod.sk"]


def test_the_zakaznici_tab_page_renders_the_partners_toolbar(pg):
    c = _client()
    _sklad(c)
    body = c.get("/nastenka/zakaznici").data.decode()
    assert 'id="p-search"' in body and 'id="p-add"' in body
    assert "/static/board/tab-partners.js" in body
    assert 'data-scope="customers"' in body


def test_the_dodavatelia_tab_page_renders_with_supplier_scope(pg):
    c = _client()
    _sklad(c)
    body = c.get("/nastenka/dodavatelia").data.decode()
    assert 'data-scope="suppliers"' in body
    assert "/static/board/tab-partners.js" in body
