"""Board redesign lane 3 — Kôš / História zmien (#444, spec §4/§5/§8 lane 3).

Covers the audit list/filter/search/paging service, the full `restore()` for every audit
action (delete / update / create / answer / undo / reopen), the append-only + double-restore
409 safety rules, and the `/api/board/audit` + `/api/board/audit/<id>/restore` endpoints and
the `/nastenka/kos` tab page. Restore must NEVER re-ship anything, NEVER touch an ORION
ledger — it only reverts curated/override/memory rows via the sanctioned engine functions.
"""
import os

import pytest

from app.board.services import audit
from app.config import Config
from app.httpapi import create_app, sklad_key
from app.orders import memory, snapshot, teach

PG_DSN = os.environ.get("PG_TEST_DSN")


def _client(dash="secret"):
    cfg = Config(pg_dsn=PG_DSN or "postgresql://unused", data_dir="/tmp", api_token="tok",
                 dash_password=dash, secret_key="test-secret")
    app = create_app(cfg)
    app.testing = True
    return app.test_client()


def _sklad(c):
    c.get("/sklad/" + sklad_key("test-secret"))


def _login(c):
    c.post("/login", data={"password": "secret"})


def _ask(pg, wording="Šiška", ean="2000000000001"):
    return teach.ask(pg, message_id="m1", customer_ean=ean, customer_name="Zákazník A",
                     wording=wording, quantity=30, unit="ks",
                     candidates=[{"gtin": "SLI50", "name": "Šiška 50g"},
                                 {"gtin": "SLI90", "name": "Šiška 90g"}],
                     delivery_date="04.08.2026", reason="neznáme znenie")


# --- the list/filter/search/paging service --------------------------------------------

def test_list_audit_returns_rows_newest_first(pg):
    audit.record(pg, actor="admin", table="mail_rules", row_id=1, action="delete")
    audit.record(pg, actor="sklad", table="item_memory", row_id=2, action="create")
    res = audit.list_audit(pg)
    assert res["total"] == 2
    # newest first
    assert res["items"][0]["action"] == "create"
    assert res["items"][1]["action"] == "delete"
    # each item carries the spec fields the UI renders
    top = res["items"][0]
    for f in ("id", "ts", "actor", "table_name", "row_id", "action", "note",
              "question_id", "message_id"):
        assert f in top


def test_list_audit_filters_by_table(pg):
    audit.record(pg, actor="admin", table="mail_rules", row_id=1, action="delete")
    audit.record(pg, actor="admin", table="item_memory", row_id=2, action="delete")
    res = audit.list_audit(pg, table="mail_rules")
    assert res["total"] == 1
    assert res["items"][0]["table_name"] == "mail_rules"


def test_list_audit_filters_by_action(pg):
    audit.record(pg, actor="admin", table="mail_rules", row_id=1, action="delete")
    audit.record(pg, actor="admin", table="mail_rules", row_id=2, action="create")
    res = audit.list_audit(pg, action="create")
    assert res["total"] == 1
    assert res["items"][0]["action"] == "create"


def test_list_audit_action_chip_groups(pg):
    audit.record(pg, actor="a", table="mail_rules", row_id=1, action="delete")
    audit.record(pg, actor="a", table="mail_rules", row_id=2, action="create")
    audit.record(pg, actor="a", table="mail_rules", row_id=3, action="update")
    audit.record(pg, actor="a", table="order_questions", row_id=4, action="answer")
    # "zmeny" = create + update ; "odpovede" = answer/undo/reopen ; "zmazane" = delete
    assert audit.list_audit(pg, action="zmeny")["total"] == 2
    assert audit.list_audit(pg, action="odpovede")["total"] == 1
    assert audit.list_audit(pg, action="zmazane")["total"] == 1


def test_list_audit_search_matches_actor_note_and_rowid(pg):
    audit.record(pg, actor="skladnicka", table="mail_rules", row_id=999, action="delete",
                 note="nechcený newsletter")
    audit.record(pg, actor="admin", table="item_memory", row_id=2, action="delete")
    assert audit.list_audit(pg, q="skladnicka")["total"] == 1
    assert audit.list_audit(pg, q="newsletter")["total"] == 1
    assert audit.list_audit(pg, q="999")["total"] == 1
    assert audit.list_audit(pg, q="nic-take")["total"] == 0


def test_list_audit_pages(pg):
    for i in range(25):
        audit.record(pg, actor="a", table="mail_rules", row_id=i, action="delete")
    p0 = audit.list_audit(pg, page=0, page_size=10)
    p1 = audit.list_audit(pg, page=1, page_size=10)
    p2 = audit.list_audit(pg, page=2, page_size=10)
    assert p0["total"] == 25
    assert len(p0["items"]) == 10 and len(p1["items"]) == 10 and len(p2["items"]) == 5
    # no overlap across pages
    ids = {r["id"] for r in p0["items"]} | {r["id"] for r in p1["items"]} \
        | {r["id"] for r in p2["items"]}
    assert len(ids) == 25


# --- restore: every action type -------------------------------------------------------

def test_restore_delete_undeletes_a_card_and_rebuilds_the_snapshot(pg):
    snapshot.upsert_catalog_card(pg, "GDEL", "Karta na zmazanie")
    snapshot.rebuild_from_overrides(pg)
    assert any(x["gtin"] == "GDEL" for x in snapshot.catalog_for_management(pg))
    snapshot.retire_catalog_card(pg, "GDEL")
    snapshot.rebuild_from_overrides(pg)
    aid = audit.record(pg, actor="admin", table="catalog_overrides", row_id="GDEL",
                       action="delete")
    assert not any(x["gtin"] == "GDEL" for x in snapshot.catalog_for_management(pg))
    assert audit.restore(pg, aid) is True
    # the card is back in the EFFECTIVE catalog (proves the snapshot was rebuilt, not just
    # deleted_at cleared)
    assert any(x["gtin"] == "GDEL" for x in snapshot.catalog_for_management(pg))
    row = pg.execute(
        "SELECT retired, deleted_at FROM catalog_overrides WHERE gtin='GDEL'").fetchone()
    assert row[0] is False and row[1] is None


def test_restore_update_writes_back_before(pg):
    snapshot.upsert_catalog_card(pg, "GUPD", "Novy nazov")
    aid = audit.record(pg, actor="admin", table="catalog_overrides", row_id="GUPD",
                       action="update", before={"name": "Povodny nazov"},
                       after={"name": "Novy nazov"})
    assert audit.restore(pg, aid) is True
    got = pg.execute("SELECT name FROM catalog_overrides WHERE gtin='GUPD'").fetchone()[0]
    assert got == "Povodny nazov"


def test_restore_create_soft_deletes_the_created_row(pg):
    rid = memory.add_global_alias(pg, "createme", "G5", "Karta 5", by="test")
    aid = audit.record(pg, actor="admin", table="global_item_memory", row_id=rid,
                       action="create")
    assert memory.resolve_global(pg, "createme") is not None
    assert audit.restore(pg, aid) is True
    # the created row is now soft-deleted (gone from matching), still in the table
    assert pg.execute(
        "SELECT deleted_at FROM global_item_memory WHERE id=%s", (rid,)).fetchone()[0] \
        is not None
    assert memory.resolve_global(pg, "createme") is None


def test_restore_answer_reopens_the_question_via_teach_undo(pg):
    qid = _ask(pg)
    teach.answer(pg, qid, "SLI50", "Šiška 50g", by="test")
    assert teach.get(pg, qid)["status"] == "answered"
    aid = pg.execute(
        "SELECT id FROM audit_log WHERE table_name='order_questions' AND action='answer' "
        "AND question_id=%s ORDER BY id DESC LIMIT 1", (qid,)).fetchone()[0]
    assert audit.restore(pg, aid) is True
    assert teach.get(pg, qid)["status"] == "open", "answer restore must reopen the question"
    # the taught mapping is gone (teach undo ran, not a raw status flip)
    assert memory.resolve(pg, "2000000000001", "Šiška") is None


def test_restore_undo_reapplies_the_prior_answer(pg):
    qid = _ask(pg)
    teach.answer(pg, qid, "SLI50", "Šiška 50g", by="test")
    teach.undo(pg, qid)
    assert teach.get(pg, qid)["status"] == "open"
    undo_aid = pg.execute(
        "SELECT id FROM audit_log WHERE table_name='order_questions' AND action='undo' "
        "AND question_id=%s ORDER BY id DESC LIMIT 1", (qid,)).fetchone()[0]
    assert audit.restore(pg, undo_aid) is True
    q = teach.get(pg, qid)
    assert q["status"] == "answered" and q["answer_gtin"] == "SLI50"


def test_restore_reopen_reexpires_the_question(pg):
    qid = _ask(pg)
    pg.execute("UPDATE order_questions SET status='open' WHERE id=%s", (qid,))
    aid = audit.record(pg, actor="admin", table="order_questions", row_id=qid,
                       action="reopen", question_id=qid)
    assert audit.restore(pg, aid) is True
    assert teach.get(pg, qid)["status"] == "expired"


# --- safety: append-only, double-restore 409, restore-of-restore refused --------------

def test_a_double_restore_is_a_409_noop(pg):
    snapshot.upsert_catalog_card(pg, "GD2", "x")
    snapshot.retire_catalog_card(pg, "GD2")
    aid = audit.record(pg, actor="admin", table="catalog_overrides", row_id="GD2",
                       action="delete")
    assert audit.restore(pg, aid) is True
    with pytest.raises(audit.RestoreError) as e:
        audit.restore(pg, aid)
    assert e.value.status == 409


def test_restore_is_append_only_never_mutates_the_original_row(pg):
    snapshot.upsert_catalog_card(pg, "GAP", "x")
    snapshot.retire_catalog_card(pg, "GAP")
    aid = audit.record(pg, actor="admin", table="catalog_overrides", row_id="GAP",
                       action="delete")
    before_row = pg.execute(
        "SELECT actor, table_name, action, row_id FROM audit_log WHERE id=%s", (aid,)
    ).fetchone()
    n_before = pg.execute("SELECT count(*) FROM audit_log").fetchone()[0]
    audit.restore(pg, aid)
    # the original row is untouched
    after_row = pg.execute(
        "SELECT actor, table_name, action, row_id FROM audit_log WHERE id=%s", (aid,)
    ).fetchone()
    assert after_row == before_row
    # a NEW restore row was appended
    n_after = pg.execute("SELECT count(*) FROM audit_log").fetchone()[0]
    assert n_after == n_before + 1
    assert pg.execute(
        "SELECT count(*) FROM audit_log WHERE action='restore' AND row_id='GAP'"
    ).fetchone()[0] == 1


def test_restore_of_a_restore_row_is_refused(pg):
    snapshot.upsert_catalog_card(pg, "GRR", "x")
    snapshot.retire_catalog_card(pg, "GRR")
    aid = audit.record(pg, actor="admin", table="catalog_overrides", row_id="GRR",
                       action="delete")
    audit.restore(pg, aid)
    restore_aid = pg.execute(
        "SELECT id FROM audit_log WHERE action='restore' AND row_id='GRR'").fetchone()[0]
    with pytest.raises(audit.RestoreError) as e:
        audit.restore(pg, restore_aid)
    assert e.value.status in (400, 409)


def test_restore_of_a_missing_audit_id_is_404(pg):
    with pytest.raises(audit.RestoreError) as e:
        audit.restore(pg, 999999)
    assert e.value.status == 404


# --- endpoints: /api/board/audit + /api/board/audit/<id>/restore ----------------------

def test_audit_endpoint_lists_rows_for_admin(pg):
    audit.record(pg, actor="admin", table="mail_rules", row_id=1, action="delete")
    c = _client()
    _login(c)
    r = c.get("/api/board/audit")
    assert r.status_code == 200
    data = r.get_json()
    assert data["total"] == 1
    assert data["items"][0]["table_name"] == "mail_rules"


def test_audit_endpoint_filters_and_searches(pg):
    audit.record(pg, actor="skladnicka", table="mail_rules", row_id=1, action="delete",
                 note="spam")
    audit.record(pg, actor="admin", table="item_memory", row_id=2, action="create")
    c = _client()
    _login(c)
    assert c.get("/api/board/audit?table=mail_rules").get_json()["total"] == 1
    assert c.get("/api/board/audit?action=create").get_json()["total"] == 1
    assert c.get("/api/board/audit?q=spam").get_json()["total"] == 1


def test_audit_endpoint_needs_a_session(pg):
    assert _client().get("/api/board/audit").status_code == 401


def test_audit_restore_endpoint_undeletes(pg):
    c = _client()
    _login(c)
    memory.add_global_alias(pg, "twist", "G9", "Karta 9", by="test")
    rid = memory.add_global_alias(pg, "restoreme2", "G7", "Karta 7", by="test")
    c.delete(f"/api/znalosti/global/{rid}")
    aid = pg.execute(
        "SELECT id FROM audit_log WHERE table_name='global_item_memory' AND action='delete' "
        "AND row_id=%s", (str(rid),)).fetchone()[0]
    r = c.post(f"/api/board/audit/{aid}/restore")
    assert r.status_code == 200
    assert pg.execute(
        "SELECT deleted_at FROM global_item_memory WHERE id=%s", (rid,)).fetchone()[0] is None
    assert memory.resolve_global(pg, "restoreme2") is not None


def test_audit_restore_endpoint_double_is_409(pg):
    c = _client()
    _login(c)
    rid = memory.add_global_alias(pg, "dbl", "G7", "Karta 7", by="test")
    c.delete(f"/api/znalosti/global/{rid}")
    aid = pg.execute(
        "SELECT id FROM audit_log WHERE table_name='global_item_memory' AND action='delete' "
        "AND row_id=%s", (str(rid),)).fetchone()[0]
    assert c.post(f"/api/board/audit/{aid}/restore").status_code == 200
    assert c.post(f"/api/board/audit/{aid}/restore").status_code == 409


def test_audit_restore_endpoint_needs_a_session(pg):
    assert _client().post("/api/board/audit/1/restore").status_code == 401


# --- the tab page renders the trash view (not the placeholder) ------------------------

def test_the_kos_tab_renders_the_trash_view(pg):
    c = _client()
    _sklad(c)
    r = c.get("/nastenka/kos")
    assert r.status_code == 200
    body = r.data.decode()
    assert 'data-testid="version"' in body
    # the trash tab loads its own module + a filter/search surface, not "Pripravujeme"
    assert "tab-trash.js" in body
    assert 'data-tab="kos"' in body
