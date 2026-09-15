"""Lane 6 of the unified nástenka (#447): Naučené sklad + Naučené objednávky.

Service (`app.board.services.rules` + `rules_edit`) + board API under `/api/board/rules*`.
Five „naučené" kinds across two scopes:

  orders — mail (mail_rules) · alias (item_memory, curated) · global (global_item_memory)
  dl     — dl_alias (dl_item_memory, curated) · supplier (dl_supplier_memory)

Every delete is SOFT + audited (spec §5), every update DELEGATES to the engine write paths
(these tests assert the identical DB effect + the audit rows + that matching honours the
soft delete, never a re-implementation). The whole point: the warehouse can finally SEE,
search, edit and undo what the system learned, with a link back to the question/doc it came
from.
"""
import os

from app.config import Config
from app.httpapi import create_app, dl_key, sklad_key
from app.orders import dl_memory, dl_supplier_memory, memory, pipeline

PG_DSN = os.environ.get("PG_TEST_DSN")


def _cfg(data_dir="/tmp", scanner=""):
    return Config(pg_dsn=PG_DSN, data_dir=data_dir, api_token="tok",
                  dash_password="secret", secret_key="test-secret",
                  delivery_notes_scanner_senders=scanner)


def _client(cfg=None):
    app = create_app(cfg or _cfg())
    app.testing = True
    return app.test_client()


def _sklad(c):
    c.get("/sklad/" + sklad_key("test-secret"))


def _dl(c):
    c.get("/sklad-dl/" + dl_key("test-secret"))


# --- seed helpers ------------------------------------------------------------------

def _seed_question(pg, message_id, wording, kind="mail", by="", answered=True):
    row = pg.execute(
        """INSERT INTO order_questions
               (message_id, customer_ean, customer_name, wording, item_key, kind,
                status, answered_by, answered_at)
           VALUES (%s, '', '', %s, %s, %s, %s, %s, now())
           RETURNING id""",
        (message_id, wording, wording.lower(), kind,
         "answered" if answered else "open", by)).fetchone()
    return int(row[0])


def _seed_mail_rule(pg, sender, subject_key, action="ignore", qid=None,
                    sample_had_attachments=False):
    row = pg.execute(
        """INSERT INTO mail_rules
               (sender_norm, subject_key, action, question_id, sample_had_attachments)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (sender.lower(), subject_key, action, qid, sample_had_attachments)).fetchone()
    return int(row[0])


# --- list / origin -----------------------------------------------------------------

def test_orders_mail_rules_list_carries_origin(pg):
    qid = _seed_question(pg, "m-1", "OBJEDNÁVKA", kind="mail", by="anna")
    rid = _seed_mail_rule(pg, "dodavatel@x.sk", "objednavka", action="ignore", qid=qid)
    c = _client()
    _sklad(c)
    r = c.get("/api/board/rules?scope=orders&kind=mail")
    assert r.status_code == 200
    data = r.get_json()
    row = next(i for i in data["items"] if i["id"] == rid)
    assert row["kind"] == "mail"
    assert row["label"] == "Ignorovaný mail"
    assert row["origin"]["question_id"] == qid
    assert row["origin"]["message_id"] == "m-1"
    assert row["origin"]["by"] == "anna"
    assert row["sample_had_attachments"] is False


def test_orders_alias_and_global_lists(pg):
    memory.add_customer_alias(pg, "EAN1", "rožok grahamový", "G1", "Karta G1")
    memory.add_global_alias(pg, "úplne iné znenie", "G2", "Karta G2", by="peto")
    # a raw ship-history row must NOT appear in the curated alias list
    memory.add_customer_alias(pg, "EAN1", "z dodávky", "G3", "Karta G3", source="ship")
    c = _client()
    _sklad(c)
    al = c.get("/api/board/rules?scope=orders&kind=alias").get_json()["items"]
    wl = {i["key"].get("wording") for i in al}
    assert "rožok grahamový" in wl
    assert "z dodávky" not in wl  # ship source excluded (not curated)
    assert any(i["key"].get("ean") == "EAN1" for i in al)
    gl = c.get("/api/board/rules?scope=orders&kind=global").get_json()["items"]
    assert any(i["key"].get("wording") == "úplne iné znenie" for i in gl)
    assert all(i["label"] == "Globálny alias" for i in gl)


def test_dl_alias_and_supplier_lists(pg):
    dl_memory.add_dl_alias(pg, "SUP1", "múka T650", "D1", "Múka")
    dl_supplier_memory.remember(pg, "sklad@sup.sk", "SUPEAN", "Dodávateľ s.r.o.")
    c = _client()
    _dl(c)
    da = c.get("/api/board/rules?scope=dl&kind=dl_alias").get_json()["items"]
    assert any(i["key"].get("wording") == "múka T650"
               and i["key"].get("ean") == "SUP1" for i in da)
    assert all(i["label"].startswith("Alias DL položky") for i in da)
    sp = c.get("/api/board/rules?scope=dl&kind=supplier").get_json()["items"]
    row = next(i for i in sp if i["key"].get("email") == "sklad@sup.sk")
    assert row["label"] == "Pamäť dodávateľa"
    assert row["target"] == "Dodávateľ s.r.o." or row["key"].get("ean") == "SUPEAN"


def test_search_filters_across_text_fields(pg):
    memory.add_global_alias(pg, "hľadané znenie", "SG1", "Karta jedna", by="t")
    memory.add_global_alias(pg, "úplne iné", "SG2", "Karta dva", by="t")
    c = _client()
    _sklad(c)
    hits = {i["key"].get("wording")
            for i in c.get("/api/board/rules?scope=orders&kind=global&q=hľadané")
            .get_json()["items"]}
    assert hits == {"hľadané znenie"}


def test_list_pages_results(pg):
    for i in range(55):
        memory.add_global_alias(pg, f"znenie {i:03d}", f"P{i:03d}", "Karta", by="t")
    c = _client()
    _sklad(c)
    p0 = c.get("/api/board/rules?scope=orders&kind=global&page=0").get_json()
    assert len(p0["items"]) == p0["meta"]["page_size"]
    assert p0["meta"]["total"] == 55
    assert p0["meta"]["has_more"] is True
    p1 = c.get("/api/board/rules?scope=orders&kind=global&page=1").get_json()
    assert p1["meta"]["has_more"] is False
    assert not ({i["id"] for i in p0["items"]} & {i["id"] for i in p1["items"]})


def test_unknown_scope_or_kind_is_400(pg):
    c = _client()
    _sklad(c)
    assert c.get("/api/board/rules?scope=nonsense&kind=mail").status_code == 400
    assert c.get("/api/board/rules?scope=orders&kind=nonsense").status_code == 400
    # a kind that belongs to the OTHER scope is also rejected
    assert c.get("/api/board/rules?scope=orders&kind=supplier").status_code == 400


def test_sklad_role_reaches_dl_rules_via_the_board_gate(pg):
    dl_supplier_memory.remember(pg, "a@b.sk", "E", "X")
    c = _client()
    _sklad(c)  # the ORDERS key still reaches the DL rules tab (spec §6 — tab decides scope)
    assert c.get("/api/board/rules?scope=dl&kind=supplier").status_code == 200


# --- delete: soft + audit + matching effect ----------------------------------------

def test_delete_mail_rule_soft_deletes_audits_and_stops_matching(pg):
    rid = _seed_mail_rule(pg, "spam@x.sk", "newsletter", action="ignore")
    # before: the rule matches
    assert pipeline._mail_rule(pg, "spam@x.sk", "Newsletter 2026") == "ignore"
    c = _client()
    _sklad(c)
    assert c.delete(f"/api/board/rules/mail/{rid}").status_code == 200
    row = pg.execute("SELECT deleted_at FROM mail_rules WHERE id=%s", (rid,)).fetchone()
    assert row[0] is not None
    # after: soft-deleted rule no longer matches
    assert pipeline._mail_rule(pg, "spam@x.sk", "Newsletter 2026") is None
    n = pg.execute("SELECT count(*) FROM audit_log WHERE table_name='mail_rules' "
                   "AND action='delete' AND row_id=%s", (str(rid),)).fetchone()[0]
    assert n == 1


def test_delete_global_alias_soft_deletes_and_stops_rescue(pg):
    rid = memory.add_global_alias(pg, "recept x", "GX", "Karta GX", by="t")
    assert memory.resolve_global(pg, "recept x") is not None
    c = _client()
    _sklad(c)
    assert c.delete(f"/api/board/rules/global/{rid}").status_code == 200
    assert pg.execute("SELECT deleted_at FROM global_item_memory WHERE id=%s",
                      (rid,)).fetchone()[0] is not None
    assert memory.resolve_global(pg, "recept x") is None
    assert pg.execute("SELECT count(*) FROM audit_log WHERE table_name='global_item_memory' "
                      "AND action='delete' AND row_id=%s", (str(rid),)).fetchone()[0] == 1


def test_delete_customer_alias_soft_deletes_and_stops_rescue(pg):
    rid = memory.add_customer_alias(pg, "CE1", "zákaznícke znenie", "CX", "Karta CX")
    assert memory.resolve(pg, "CE1", "zákaznícke znenie") is not None
    c = _client()
    _sklad(c)
    assert c.delete(f"/api/board/rules/alias/{rid}").status_code == 200
    assert memory.resolve(pg, "CE1", "zákaznícke znenie") is None


def test_delete_dl_alias_soft_deletes_and_stops_rescue(pg):
    rid = dl_memory.add_dl_alias(pg, "DSUP", "dl znenie", "DDX", "Karta DDX")
    assert dl_memory.resolve(pg, "DSUP", "dl znenie") is not None
    c = _client()
    _dl(c)
    assert c.delete(f"/api/board/rules/dl_alias/{rid}").status_code == 200
    assert dl_memory.resolve(pg, "DSUP", "dl znenie") is None


def test_delete_supplier_memory_soft_deletes_and_stops_resolve(pg):
    dl_supplier_memory.remember(pg, "who@sup.sk", "WEAN", "Kto s.r.o.")
    rid = pg.execute("SELECT id FROM dl_supplier_memory WHERE sender_email='who@sup.sk'"
                     ).fetchone()[0]
    assert dl_supplier_memory.resolve(pg, "who@sup.sk") is not None
    c = _client()
    _dl(c)
    assert c.delete(f"/api/board/rules/supplier/{rid}").status_code == 200
    assert dl_supplier_memory.resolve(pg, "who@sup.sk") is None


def test_delete_a_missing_rule_is_404(pg):
    c = _client()
    _sklad(c)
    assert c.delete("/api/board/rules/global/999999").status_code == 404


# --- update: delegate + audit ------------------------------------------------------

def test_update_mail_rule_changes_action_and_audits(pg):
    rid = _seed_mail_rule(pg, "s@x.sk", "predmet", action="ignore")
    c = _client()
    _sklad(c)
    r = c.post(f"/api/board/rules/mail/{rid}",
               json={"subject_key": "novy predmet", "action": "manual"})
    assert r.status_code == 200
    row = pg.execute("SELECT subject_key, action FROM mail_rules WHERE id=%s",
                     (rid,)).fetchone()
    assert row[0] == "novy predmet"
    assert row[1] == "manual"
    n = pg.execute("SELECT count(*) FROM audit_log WHERE table_name='mail_rules' "
                   "AND action='update' AND row_id=%s", (str(rid),)).fetchone()[0]
    assert n == 1


def test_update_mail_rule_rejects_a_bad_action(pg):
    rid = _seed_mail_rule(pg, "s@x.sk", "p", action="ignore")
    c = _client()
    _sklad(c)
    assert c.post(f"/api/board/rules/mail/{rid}",
                  json={"subject_key": "p", "action": "nonsense"}).status_code == 400


def test_update_global_alias_repoints_and_recomputes_item_key(pg):
    rid = memory.add_global_alias(pg, "staré znenie", "OLDG", "Stará", by="t")
    c = _client()
    _sklad(c)
    r = c.post(f"/api/board/rules/global/{rid}",
               json={"wording": "nové znenie", "gtin": "NEWG", "card": "Nová"})
    assert r.status_code == 200
    # the alias now resolves the NEW wording to the NEW card (item_key recomputed)
    rec = memory.resolve_global(pg, "nové znenie")
    assert rec is not None and rec.gtin == "NEWG"
    assert memory.resolve_global(pg, "staré znenie") is None
    assert pg.execute("SELECT count(*) FROM audit_log WHERE table_name='global_item_memory' "
                      "AND action='update' AND row_id=%s", (str(rid),)).fetchone()[0] == 1


def test_update_dl_alias_repoints_and_recomputes(pg):
    rid = dl_memory.add_dl_alias(pg, "USUP", "staré dl", "OLDD", "Stará")
    c = _client()
    _dl(c)
    r = c.post(f"/api/board/rules/dl_alias/{rid}",
               json={"wording": "nové dl", "gtin": "NEWD", "card": "Nová"})
    assert r.status_code == 200
    assert dl_memory.resolve(pg, "USUP", "nové dl").gtin == "NEWD"
    assert dl_memory.resolve(pg, "USUP", "staré dl") is None


def test_update_supplier_memory_changes_target_and_audits(pg):
    dl_supplier_memory.remember(pg, "up@sup.sk", "OLDEAN", "Staré meno")
    rid = pg.execute("SELECT id FROM dl_supplier_memory WHERE sender_email='up@sup.sk'"
                     ).fetchone()[0]
    c = _client()
    _dl(c)
    r = c.post(f"/api/board/rules/supplier/{rid}",
               json={"ean": "NEWEAN", "name": "Nové meno"})
    assert r.status_code == 200
    res = dl_supplier_memory.resolve(pg, "up@sup.sk")
    assert res["ean_edi"] == "NEWEAN"
    assert res["name"] == "Nové meno"
    assert pg.execute("SELECT count(*) FROM audit_log WHERE table_name='dl_supplier_memory' "
                      "AND action='update' AND row_id=%s", (str(rid),)).fetchone()[0] == 1


# --- scanner guard (#407): lane 6 has NO create path, and the engine still refuses ----

def test_scanner_supplier_memory_is_never_created(pg):
    cfg = _cfg(scanner="tlaciaren@slovnormal.sk")
    # the sanctioned learn path REFUSES a scanner address as a supplier identity
    assert dl_supplier_memory.remember(
        pg, "tlaciaren@slovnormal.sk", "SEAN", "X", cfg=cfg) is False
    assert pg.execute(
        "SELECT count(*) FROM dl_supplier_memory WHERE sender_email=%s",
        ("tlaciaren@slovnormal.sk",)).fetchone()[0] == 0
    # and lane 6 exposes no create endpoint (only list/update/delete)
    c = _client(cfg)
    _dl(c)
    assert c.post("/api/board/rules/supplier",
                  json={"email": "x@y.sk"}).status_code in (404, 405)
