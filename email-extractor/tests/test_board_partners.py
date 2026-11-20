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
from app.board.services import partners
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
    p0 = cust.list_customers(pg, page=0, size=50)
    assert p0["total"] == 30 and p0["page_size"] == 50 and len(p0["families"]) == 30
    # #504: the default page holds 100 rows, an unknown size falls back to it
    assert cust.list_customers(pg)["page_size"] == 100
    assert partners.page_size("7") == 100 and partners.page_size("-1") == 100
    assert partners.page_size("200") == 200 and partners.page_size("0") == 0


# --- #504: sort by column, rows per page, EAN search ------------------------------------

def _sites(res):
    return [s for f in res["families"] for s in f["sites"]]


def test_customers_sort_by_ean_both_directions_blank_ean_never_first(pg):
    _mk_customer(pg, "8590000000300", "Alfa", city="Z")
    _mk_customer(pg, "8590000000100", "Cézar", city="A")
    _mk_customer(pg, "8590000000200", "Beta", city="M")
    up = [s["ean_edi"] for s in _sites(cust.list_customers(pg, sort="ean"))]
    assert up == ["8590000000100", "8590000000200", "8590000000300"]
    down = [s["ean_edi"] for s in _sites(cust.list_customers(pg, sort="ean", desc=True))]
    assert down == ["8590000000300", "8590000000200", "8590000000100"]
    assert [s["name"] for s in _sites(cust.list_customers(pg, sort="city"))] == [
        "Cézar", "Beta", "Alfa"]
    # a blank EAN sorts last in both directions
    rows = [{"ean_edi": "", "name": "X"}, {"ean_edi": "2", "name": "Y"},
            {"ean_edi": "10", "name": "Z"}]
    assert [r["name"] for r in partners.sorted_rows(rows, "ean", False, "n")] == ["Y", "Z", "X"]
    assert [r["name"] for r in partners.sorted_rows(rows, "ean", True, "n")] == ["Z", "Y", "X"]


def test_customers_sort_by_orders_shipped(pg):
    _mk_customer(pg, "8590000000401", "Málo", city="A")
    _mk_customer(pg, "8590000000402", "Veľa", city="B")
    pg.execute("INSERT INTO edi_sent (customer_ean, delivery_date, content_sha256, filename) "
               "VALUES ('8590000000402','2026-09-15','h1','f1.txt'),"
               "('8590000000402','2026-09-16','h2','f2.txt'),"
               "('8590000000401','2026-09-16','h3','f3.txt')")
    res = cust.list_customers(pg, sort="count", desc=True)
    assert [s["name"] for s in _sites(res)] == ["Veľa", "Málo"]


def test_customers_sorted_by_ean_page_flat_rows_but_keep_the_site_tag(pg):
    _mk_customer(pg, "8590000000502", "Košík.sk MAKRO Žilina", city="Žilina")
    _mk_customer(pg, "8590000000501", "Košík.sk sklad Zvolen", city="Zvolen")
    _mk_customer(pg, "8590000000503", "Iný zákazník", city="Nitra")
    by_name = cust.list_customers(pg)
    assert by_name["total"] == 2 and by_name["rows"] == 3   # one family + one singleton
    by_ean = cust.list_customers(pg, sort="ean", size=50)
    assert by_ean["total"] == 3 and all(f["size"] == 1 for f in by_ean["families"])
    first = by_ean["families"][0]["sites"][0]
    assert first["ean_edi"] == "8590000000501" and first["site_total"] == 2
    one = cust.list_customers(pg, sort="ean", size=50, page=0)
    assert len(one["families"]) == 3
    assert cust.list_customers(pg, sort="bogus")["sort"] == "name"


def test_customer_search_finds_an_ean_typed_with_spaces(pg):
    _mk_customer(pg, "2000000000730", "Caffetteria Test", city="Košice")
    _mk_customer(pg, "2000000000999", "Iný", city="Košice")
    for q in ("2000000000730", "2000 0000 00730", "00730"):
        hits = _sites(cust.list_customers(pg, q=q))
        assert [s["ean_edi"] for s in hits] == ["2000000000730"], q


def test_suppliers_sort_by_ean_and_page_size(pg):
    _mk_supplier(pg, "8597000000002", "Beta dodávateľ", city="B")
    _mk_supplier(pg, "8597000000001", "Alfa dodávateľ", city="A")
    res = supp.list_suppliers(pg, sort="ean", desc=True, size=200)
    eans = [r["ean_edi"] for r in res["suppliers"] if r["ean_edi"].startswith("8597")]
    assert eans == ["8597000000002", "8597000000001"]
    assert res["page_size"] == 200


def test_board_customers_api_passes_sort_dir_and_size(pg):
    _mk_customer(pg, "8590000000601", "Prvý", city="A")
    _mk_customer(pg, "8590000000602", "Druhý", city="B")
    c = _client()
    _login(c)
    res = c.get("/api/board/customers?sort=ean&dir=desc&size=50").get_json()
    assert res["sort"] == "ean" and res["desc"] is True and res["page_size"] == 50
    assert [s["ean_edi"] for s in _sites(res)] == ["8590000000602", "8590000000601"]
    sup = c.get("/api/board/suppliers?sort=ean&size=abc").get_json()
    assert sup["sort"] == "ean" and sup["page_size"] == 100


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


def test_delete_customer_then_restore_brings_it_back(pg):
    """The whole point of the audit: a lane-5 delete is restorable end-to-end via the
    existing lane-3 restore (no new restore branch needed)."""
    from app.board.services import audit
    res = cust.save_customer(pg, _cfg(), "sklad", {
        "ean_edi": "8590000000301", "name": "Vrátiteľný", "city": "V"})
    rid = res["id"]
    cust.delete_customer(pg, _cfg(), "sklad", {"override_id": rid})
    aid = pg.execute("SELECT id FROM audit_log WHERE table_name='customer_overrides' "
                     "AND action='delete' AND row_id=%s", (str(rid),)).fetchone()[0]
    assert audit.restore(pg, aid) is True
    row = pg.execute("SELECT retired, deleted_at FROM customer_overrides WHERE id=%s",
                     (rid,)).fetchone()
    assert row[0] is False and row[1] is None
    assert any(x["ean_edi"] == "8590000000301" for x in snapshot.customers_for_management(pg))


def test_update_customer_then_restore_reverts_the_values(pg):
    from app.board.services import audit
    rid = _mk_customer(pg, "8590000000302", "Pôvodné meno", city="Staré")
    cust.save_customer(pg, _cfg(), "sklad", {
        "override_id": rid, "ean_edi": "8590000000302", "name": "Nové meno", "city": "Nové"})
    aid = pg.execute("SELECT id FROM audit_log WHERE table_name='customer_overrides' "
                     "AND action='update' AND row_id=%s", (str(rid),)).fetchone()[0]
    assert audit.restore(pg, aid) is True
    row = pg.execute("SELECT name, city FROM customer_overrides WHERE id=%s", (rid,)).fetchone()
    assert row[0] == "Pôvodné meno" and row[1] == "Staré"


def test_delete_supplier_then_restore_brings_it_back(pg):
    from app.board.services import audit
    res = supp.save_supplier(pg, _cfg(), "sklad", {
        "ean_edi": "8590000000303", "name": "Vrátiteľný dod.", "city": "V"})
    rid = res["id"]
    supp.delete_supplier(pg, _cfg(), "sklad", {"override_id": rid})
    aid = pg.execute("SELECT id FROM audit_log WHERE table_name='dl_supplier_overrides' "
                     "AND action='delete' AND row_id=%s", (str(rid),)).fetchone()[0]
    assert audit.restore(pg, aid) is True
    row = pg.execute("SELECT retired, deleted_at FROM dl_supplier_overrides WHERE id=%s",
                     (rid,)).fetchone()
    assert row[0] is False and row[1] is None
    assert any(x["ean_edi"] == "8590000000303"
               for x in dl_snapshot.dl_suppliers_for_management(pg))


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


# --- #485: a flag-only save of a SHEET-ONLY card must not re-run old stuck mail -----------

_SHEET_SUPPLIERS = ("Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,"
                    "Číslo mobilu,E-mail\n"
                    "Hárkový dodávateľ,8590000000301,Mesto,,,,stary@dod.sk\n")


def _stuck_mail(pg, mid, from_addr):
    pg.execute("INSERT INTO messages (message_id, category, subject, from_addr, "
               "combined_text, processed, proc_status) VALUES (%s, 'dodacie_listy', "
               "'Re: objednávka', %s, 'text', true, 'review')", (mid, from_addr))


def _processed(pg, mid):
    return pg.execute("SELECT processed FROM messages WHERE message_id=%s",
                      (mid,)).fetchone()[0]


def test_a_flag_only_save_of_a_sheet_only_supplier_card_releases_nothing(pg):
    """The live EKVIA / hammy / LESAFFRE cards exist only in the sheet snapshot (no override
    row): the first save creates one. When it changes nothing about WHO the supplier is —
    only `invoice_is_delivery_note` — it must not re-run the supplier's old stuck review mail
    (and re-post it to the warehouse). A save that adds an e-mail still releases (#322)."""
    dl_snapshot.import_snapshot(pg, "GTIN,Názov,doplnok,hmotnost,Sklad,Cena\n"
                                "8588000000001,Rožok 50g,,0.05,1,0.50\n",
                                "GTIN,Sklad,Názov,doplnok\n", _SHEET_SUPPLIERS)
    _stuck_mail(pg, "stuck-old", "stary@dod.sk")
    _stuck_mail(pg, "stuck-new", "faktury@dod.sk")
    card = next(s for s in dl_snapshot.dl_suppliers_for_management(pg)
                if s["ean_edi"] == "8590000000301")
    assert card["override_id"] is None
    c = _client()
    _login(c)
    body = {"orig_ean_edi": card["orig_ean_edi"], "orig_city": card["orig_city"],
            "ean_edi": "8590000000301", "name": "Hárkový dodávateľ", "city": "Mesto",
            "emails": "stary@dod.sk", "invoice_is_delivery_note": True}
    r = c.post("/api/board/suppliers", json=body)
    assert r.status_code == 200 and r.get_json()["action"] == "create"
    assert _processed(pg, "stuck-old") is True, "a flag-only save re-queued a stuck mail"
    flagged = [s for s in dl_snapshot.dl_suppliers_for_management(pg)
               if s.get("invoice_is_delivery_note")]
    assert [s["ean_edi"] for s in flagged] == ["8590000000301"]
    # adding the invoice address changes the identity → the #322 release still runs for it
    r = c.post("/api/board/suppliers", json={
        **body, "override_id": r.get_json()["id"],
        "emails": "stary@dod.sk, faktury@dod.sk"})
    assert r.status_code == 200
    assert _processed(pg, "stuck-new") is False, "a new e-mail must still unstick its mail"
