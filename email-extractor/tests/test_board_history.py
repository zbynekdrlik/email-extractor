"""Lane 7 of the unified nástenka (#448): História objednávok + História dodacích listov.

Service (`app.board.services.history`/`history_detail`/`history_actions`/`teachback`) + board
API under `/api/board/history*`. The two history tabs are a THIN read over machinery that
already exists — `order_runs`+`order_items` (both engines write them, #200), `email_events`,
the `edi_sent`/`desadv_sent` ORION ledgers and the sanctioned reset / hold-release paths. The
tests assert the genuinely-new behaviour AND the hard safety invariants of #448:

  * list/filter/search/paging for both scopes with the correct plain-Slovak status mapping,
  * detail = items + match trace (card/rule/confidence) + email_events timeline + partner,
  * teachback writes the RIGHT memory table per scope with `source='teachback'` + a `teach`
    audit row, and NEVER touches `edi_sent`/`desadv_sent`,
  * `rerun` REFUSES (409) any document that could already have shipped (uploaded ledger row,
    ORION presence, or a non-error terminal state) and only resets a never-uploaded one
    (with `attempts=0`); it refuses a busy (claimed) message,
  * `manual` delegates to the hold-release-without-ship path and never touches a ledger,
  * the original file preview is board-gated AND history-scoped.
"""
import os

from psycopg.types.json import Json

from app.config import Config
from app.httpapi import create_app, dl_key, sklad_key

PG_DSN = os.environ.get("PG_TEST_DSN")


def _cfg(data_dir="/tmp"):
    return Config(pg_dsn=PG_DSN, data_dir=data_dir, api_token="tok",
                  dash_password="secret", secret_key="test-secret")


def _client(cfg=None):
    app = create_app(cfg or _cfg())
    app.testing = True
    return app.test_client()


def _sklad(c):
    c.get("/sklad/" + sklad_key("test-secret"))


def _dl(c):
    c.get("/sklad-dl/" + dl_key("test-secret"))


# --- seeders -----------------------------------------------------------------------

def _msg(pg, message_id, *, category="ai_orders", proc_status="ok", subject="",
         from_name="", from_addr="a@b.sk", proc_outcome="", edi_file=None,
         processed=True, processing_at=None, attempts=1):
    return pg.execute(
        """INSERT INTO messages
               (message_id, category, proc_status, subject, from_name, from_addr,
                proc_outcome, edi_file, processed, processing_at, attempts, created_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now()) RETURNING id""",
        (message_id, category, proc_status, subject, from_name, from_addr,
         proc_outcome, edi_file, processed, processing_at, attempts)).fetchone()[0]


def _run(pg, message_id, *, status="ok", result=None, items=None, shadow=False):
    rid = pg.execute(
        """INSERT INTO order_runs (message_id, shadow, status, result, finished_at)
           VALUES (%s,%s,%s,%s, now()) RETURNING id""",
        (message_id, shadow, status, Json(result or {}))).fetchone()[0]
    for it in (items or []):
        pg.execute(
            """INSERT INTO order_items
                   (run_id, name, quantity, unit, gtin, card, confidence, rule, trace)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (rid, it.get("name"), it.get("quantity"), it.get("unit"), it.get("gtin"),
             it.get("card"), it.get("confidence"), it.get("rule"),
             Json(it.get("trace") or {})))
    return rid


def _event(pg, message_id, *, stage="uploaded_orion", status="ok", outcome="", workflow="ai_orders"):
    pg.execute(
        """INSERT INTO email_events (message_id, workflow, stage, status, outcome, detail, rollup)
           VALUES (%s,%s,%s,%s,%s,%s, false)""",
        (message_id, workflow, stage, status, outcome, Json({})))


# --- list / status mapping ---------------------------------------------------------

def test_orders_history_list_maps_status_labels(pg):
    _msg(pg, "m-ok", proc_status="ok", subject="Objednávka A", from_name="Pekáreň s.r.o.")
    _msg(pg, "m-held", proc_status="held", subject="Objednávka B")
    _msg(pg, "m-rev", proc_status="review", subject="Objednávka C")
    _msg(pg, "m-err", proc_status="error", subject="Objednávka D")
    c = _client()
    _sklad(c)
    r = c.get("/api/board/history?scope=orders")
    assert r.status_code == 200
    data = r.get_json()
    labels = {it["message_id"]: it["status_label"] for it in data["items"]}
    assert labels["m-ok"] == "odišlo do ORIONu"
    assert labels["m-held"] == "čaká na sklad"
    assert labels["m-rev"] == "na kontrole"
    assert labels["m-err"] == "zlyhalo"


def test_history_scope_partitions_orders_vs_dl(pg):
    _msg(pg, "o1", category="ai_orders", subject="ord")
    _msg(pg, "s1", category="static_orders", subject="static ord")
    _msg(pg, "d1", category="dodacie_listy", subject="dl")
    c = _client()
    _sklad(c)
    orders = {it["message_id"] for it in c.get("/api/board/history?scope=orders").get_json()["items"]}
    dl = {it["message_id"] for it in c.get("/api/board/history?scope=dl").get_json()["items"]}
    assert orders == {"o1", "s1"}
    assert dl == {"d1"}


def test_history_list_unknown_scope_400(pg):
    c = _client()
    _sklad(c)
    assert c.get("/api/board/history?scope=nope").status_code == 400


def test_history_list_requires_auth(pg):
    c = _client()
    assert c.get("/api/board/history?scope=orders").status_code == 401


def test_history_filter_by_status(pg):
    _msg(pg, "a", proc_status="ok")
    _msg(pg, "b", proc_status="error")
    c = _client()
    _sklad(c)
    ids = {it["message_id"] for it in
           c.get("/api/board/history?scope=orders&status=error").get_json()["items"]}
    assert ids == {"b"}


def test_history_search_over_subject_and_partner_and_item(pg):
    _msg(pg, "s1", subject="Rožky pre pondelok", from_name="Alfa")
    _msg(pg, "s2", subject="Iné", from_name="Beta")
    _run(pg, "s2", items=[{"name": "špeciálny rožok", "gtin": "G"}])
    c = _client()
    _sklad(c)
    by_subject = {it["message_id"] for it in
                  c.get("/api/board/history?scope=orders&q=pondelok").get_json()["items"]}
    assert by_subject == {"s1"}
    by_partner = {it["message_id"] for it in
                  c.get("/api/board/history?scope=orders&q=Beta").get_json()["items"]}
    assert by_partner == {"s2"}
    by_item = {it["message_id"] for it in
               c.get("/api/board/history?scope=orders&q=špeciálny").get_json()["items"]}
    assert by_item == {"s2"}


def test_history_list_pages(pg):
    for i in range(30):
        _msg(pg, f"p{i:02d}", subject=f"doc {i}")
    c = _client()
    _sklad(c)
    p0 = c.get("/api/board/history?scope=orders&page=0").get_json()
    assert p0["meta"]["total"] == 30
    assert len(p0["items"]) == p0["meta"]["page_size"]
    p1 = c.get("/api/board/history?scope=orders&page=1").get_json()
    assert {i["message_id"] for i in p0["items"]}.isdisjoint(
        {i["message_id"] for i in p1["items"]})


# --- detail ------------------------------------------------------------------------

def test_orders_detail_has_items_trace_and_timeline(pg):
    _msg(pg, "m1", proc_status="ok", subject="Obj", from_name="Alfa", edi_file="ORDER_1.txt")
    _run(pg, "m1", status="ok",
         result={"customer_ean": "EAN1", "customer_name": "Alfa", "edi_filename": "ORDER_1.txt"},
         items=[{"name": "rožok", "quantity": 10, "unit": "ks", "gtin": "111",
                 "card": "Rožok 50g", "confidence": 0.97, "rule": "memory"}])
    _event(pg, "m1", stage="uploaded_orion", status="ok", outcome="EDI vytvorené: ORDER_1.txt")
    c = _client()
    _sklad(c)
    r = c.get("/api/board/history/m1")
    assert r.status_code == 200
    d = r.get_json()
    assert d["status_label"] == "odišlo do ORIONu"
    assert d["partner"]["name"] == "Alfa"
    assert len(d["items"]) == 1
    it = d["items"][0]
    assert it["card"] == "Rožok 50g" and it["rule"] == "memory" and it["confidence"] == 0.97
    assert any(e["outcome"].startswith("EDI vytvorené") for e in d["events"])
    assert "ORDER_1.txt" in d["doc_numbers"]


def test_detail_uses_latest_nonshadow_run(pg):
    _msg(pg, "m1", subject="x")
    _run(pg, "m1", shadow=True, items=[{"name": "shadow-item", "gtin": "S"}])
    _run(pg, "m1", shadow=False, items=[{"name": "real-item", "gtin": "R"}])
    c = _client()
    _sklad(c)
    d = c.get("/api/board/history/m1").get_json()
    names = {it["name"] for it in d["items"]}
    assert names == {"real-item"}


def test_detail_unknown_message_404(pg):
    c = _client()
    _sklad(c)
    assert c.get("/api/board/history/nope").status_code == 404


def test_detail_non_history_category_404(pg):
    _msg(pg, "spam", category="no_processing", subject="promo")
    c = _client()
    _sklad(c)
    assert c.get("/api/board/history/spam").status_code == 404


# --- rerun safety (the core of #448) -----------------------------------------------

def test_rerun_resets_a_never_uploaded_error_message(pg):
    mid = _msg(pg, "err1", proc_status="error", subject="zlyhalo", processed=True, attempts=3)
    _run(pg, "err1", status="error")
    c = _client()
    _sklad(c)
    r = c.post("/api/board/history/err1/rerun")
    assert r.status_code == 200, r.get_json()
    row = pg.execute("SELECT processed, processing_at, attempts FROM messages WHERE id=%s",
                     (mid,)).fetchone()
    assert row[0] is False and row[1] is None and row[2] == 0


def test_rerun_refuses_a_shipped_order(pg):
    _msg(pg, "ok1", proc_status="ok", subject="odišlo", edi_file="ORDER_9.txt")
    c = _client()
    _sklad(c)
    r = c.post("/api/board/history/ok1/rerun")
    assert r.status_code == 409


def test_rerun_refuses_when_edi_sent_uploaded_row_exists(pg):
    _msg(pg, "e2", proc_status="review", subject="čiastočne", edi_file="ORDER_5.txt")
    pg.execute(
        "INSERT INTO edi_sent (customer_ean, delivery_date, content_sha256, filename, uploaded_at) "
        "VALUES ('EANX','2026-09-15','sha', 'ORDER_5.txt', now())")
    c = _client()
    _sklad(c)
    assert c.post("/api/board/history/e2/rerun").status_code == 409


def test_rerun_refuses_when_manually_resolved(pg):
    _msg(pg, "man1", proc_status="manual", subject="ručne")
    c = _client()
    _sklad(c)
    assert c.post("/api/board/history/man1/rerun").status_code == 409


def test_rerun_refuses_a_busy_message(pg):
    _msg(pg, "busy1", proc_status="error", subject="beží", processed=False,
         processing_at="now()")
    # set a real recent claim
    pg.execute("UPDATE messages SET processing_at = now() WHERE message_id='busy1'")
    c = _client()
    _sklad(c)
    assert c.post("/api/board/history/busy1/rerun").status_code == 409


def test_rerun_dl_refuses_when_desadv_uploaded(pg):
    _msg(pg, "dl1", category="dodacie_listy", proc_status="ok", subject="DL")
    _run(pg, "dl1", status="ok",
         result={"documents": [{"doc_number": "DOC7", "supplier_ean": "SUP1"}]})
    pg.execute("INSERT INTO desadv_sent (supplier_ean, doc_number, filename, uploaded_at) "
               "VALUES ('SUP1','DOC7','DESADV.txt', now())")
    c = _client()
    _dl(c)
    assert c.post("/api/board/history/dl1/rerun?scope=dl").status_code == 409


# --- manual (hold release without ship) --------------------------------------------

def test_manual_releases_held_orders_without_touching_ledger(pg):
    _msg(pg, "h1", proc_status="held", subject="drží sa", processed=False)
    pg.execute(
        """INSERT INTO held_orders
               (message_id, customer_ean, customer_name, delivery_date, question_ids,
                order_json, extracted_json, decisions_json, status)
           VALUES ('h1','EAN','Alfa','2026-09-20','{}'::bigint[],
                   '{}'::jsonb, '{}'::jsonb, '[]'::jsonb, 'held')""")
    c = _client()
    _sklad(c)
    r = c.post("/api/board/history/h1/manual")
    assert r.status_code == 200, r.get_json()
    st = pg.execute("SELECT status, release_reason FROM held_orders WHERE message_id='h1'").fetchone()
    assert st[0] == "released" and st[1] == "manual"
    # nothing was uploaded
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 0
    # audited
    assert pg.execute("SELECT count(*) FROM audit_log WHERE action='manual'").fetchone()[0] >= 1


def test_manual_refuses_when_nothing_held(pg):
    _msg(pg, "nh", proc_status="ok", subject="nič sa nedrží")
    c = _client()
    _sklad(c)
    assert c.post("/api/board/history/nh/manual").status_code == 409


def test_manual_refuses_dl_scope(pg):
    _msg(pg, "dlm", category="dodacie_listy", proc_status="review", subject="DL")
    c = _client()
    _dl(c)
    assert c.post("/api/board/history/dlm/manual?scope=dl").status_code == 409


# --- teachback (writes memory, never the shipped doc / ledger) ----------------------

def test_teachback_orders_writes_customer_alias_and_audit(pg):
    _msg(pg, "t1", proc_status="ok", subject="Obj")
    _run(pg, "t1", result={"customer_ean": "CUST1", "customer_name": "Alfa"},
         items=[{"name": "rožok tmavý", "gtin": "OLD", "card": "Zlá karta"}])
    c = _client()
    _sklad(c)
    r = c.post("/api/board/history/t1/teach",
               json={"name": "rožok tmavý", "gtin": "NEW1", "card": "Správna karta"})
    assert r.status_code == 200, r.get_json()
    row = pg.execute(
        "SELECT gtin, source FROM item_memory WHERE customer_ean='CUST1'").fetchone()
    assert row[0] == "NEW1" and row[1] == "teachback"
    assert pg.execute(
        "SELECT count(*) FROM audit_log WHERE action='teach' AND table_name='item_memory'"
    ).fetchone()[0] == 1
    # never touched a ledger
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 0


def test_teachback_dl_writes_supplier_alias(pg):
    _msg(pg, "td", category="dodacie_listy", proc_status="ok", subject="DL")
    _run(pg, "td", result={"documents": [{"doc_number": "D1", "supplier_ean": "SUPP"}]},
         items=[{"name": "múka", "gtin": "OLD", "card": "Zlá"}])
    c = _client()
    _dl(c)
    r = c.post("/api/board/history/td/teach?scope=dl",
               json={"name": "múka", "gtin": "NEWDL", "card": "Múka T650"})
    assert r.status_code == 200, r.get_json()
    row = pg.execute(
        "SELECT gtin, source FROM dl_item_memory WHERE supplier_ean='SUPP'").fetchone()
    assert row[0] == "NEWDL" and row[1] == "teachback"
    assert pg.execute("SELECT count(*) FROM desadv_sent").fetchone()[0] == 0


def test_teachback_requires_gtin_and_wording(pg):
    _msg(pg, "t2", proc_status="ok")
    _run(pg, "t2", result={"customer_ean": "C"})
    c = _client()
    _sklad(c)
    assert c.post("/api/board/history/t2/teach", json={"name": "x"}).status_code == 400
    assert c.post("/api/board/history/t2/teach", json={"gtin": "G"}).status_code == 400


def test_teachback_audit_is_restorable(pg):
    """The `teach` audit row can be reverted from the Kôš (soft-deletes the taught alias)."""
    from app.board.services import audit
    _msg(pg, "t3", proc_status="ok")
    _run(pg, "t3", result={"customer_ean": "CX"},
         items=[{"name": "chlieb", "gtin": "OLD"}])
    c = _client()
    _sklad(c)
    c.post("/api/board/history/t3/teach", json={"name": "chlieb", "gtin": "Z1", "card": "K"})
    row = pg.execute("SELECT id FROM audit_log WHERE action='teach'").fetchone()
    audit.restore(pg, int(row[0]), by="admin")
    mem = pg.execute("SELECT deleted_at FROM item_memory WHERE customer_ean='CX'").fetchone()
    assert mem[0] is not None  # soft-deleted -> no longer used by matching


# --- original preview (board-gated + history-scoped) --------------------------------

def test_history_file_guard_404_for_non_history_message(pg):
    _msg(pg, "np", category="no_processing", subject="promo")
    c = _client()
    _sklad(c)
    assert c.get("/api/board/history/np/files/0").status_code == 404
