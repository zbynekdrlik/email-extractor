"""/znalosti (#104): direct curation of wording->card knowledge, without waiting for the
pipeline to raise an order_questions row first. Dashboard API + auth-gate tests (Flask test
client + real Postgres), same pattern as test_api.py."""
import os

from app.config import Config
from app.httpapi import create_app, sklad_key
from app.orders import memory, snapshot

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
