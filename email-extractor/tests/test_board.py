"""Board redesign lane 1 — the unified nástenka skeleton (#442).

Covers: the `board` blueprint routes + the ONE gate for `/nastenka*` and `/api/board/*`
(role `sklad` from EITHER HMAC key, admin from session, anyone else refused), the
`/sklad/<k>` + `/sklad-dl/<k>` redirect to `/nastenka`, the r15/r16 migrations
(`audit_log` + `deleted_at` soft-delete columns + the retired->deleted_at backfill), the
existing `/znalosti` DELETE endpoints now SOFT-deleting + auditing (no hard DELETE), the
snapshot readers excluding `deleted_at` rows, and the audit `restore` skeleton.
"""
import os

from app.config import Config
from app.httpapi import create_app, dl_key, sklad_key
from app.orders import memory, snapshot

PG_DSN = os.environ.get("PG_TEST_DSN")


def _client(dash="secret"):
    cfg = Config(pg_dsn=PG_DSN or "postgresql://unused", data_dir="/tmp", api_token="tok",
                 dash_password=dash, secret_key="test-secret")
    app = create_app(cfg)
    app.testing = True
    return app.test_client()


def _sklad(c):
    c.get("/sklad/" + sklad_key("test-secret"))


def _sklad_dl(c):
    c.get("/sklad-dl/" + dl_key("test-secret"))


def _login(c):
    c.post("/login", data={"password": "secret"})


# --- the two signed links now land on the unified nástenka (spec §6) --------------------

def test_the_sklad_link_redirects_to_nastenka():
    c = _client()
    r = c.get("/sklad/" + sklad_key("test-secret"))
    assert r.status_code == 302
    assert "/nastenka" in r.headers["Location"], r.headers["Location"]


def test_the_dl_sklad_link_redirects_to_nastenka():
    c = _client()
    r = c.get("/sklad-dl/" + dl_key("test-secret"))
    assert r.status_code == 302
    assert "/nastenka" in r.headers["Location"], r.headers["Location"]


def test_the_old_boards_still_work_after_the_redirect_change():
    """Lane 1 runs the new nástenka ALONGSIDE the old pages — they are NOT disabled."""
    c = _client()
    _sklad(c)
    assert c.get("/otazky").status_code == 200
    c2 = _client()
    _sklad_dl(c2)
    assert c2.get("/otazky-dl").status_code == 200


# --- the one gate: /nastenka* + /api/board/* -------------------------------------------

TAB_LABELS = [
    "Otázky sklad", "Otázky objednávky", "Produkty sklad", "Produkty objednávky",
    "Naučené sklad", "Naučené objednávky", "Zákazníci", "Dodávatelia",
    "História objednávok", "História dodacích listov", "Kôš",
]


def test_nastenka_renders_all_tabs_and_the_version_for_the_sklad_role():
    c = _client()
    _sklad(c)
    r = c.get("/nastenka")
    assert r.status_code == 200
    body = r.data.decode()
    assert 'data-testid="version"' in body
    for label in TAB_LABELS:
        assert label in body, f"tab {label!r} missing from the nástenka"


def test_the_dl_role_also_reaches_the_nastenka_and_keeps_every_tab():
    """The DL key must NOT lose access: one role `sklad`, both keys valid, tab decides the
    agenda — never the key (spec §6). A regression here would strand the DL warehouse."""
    c = _client()
    _sklad_dl(c)
    r = c.get("/nastenka")
    assert r.status_code == 200
    body = r.data.decode()
    for label in TAB_LABELS:
        assert label in body


def test_admin_reaches_the_nastenka_and_additionally_sees_the_maily_tab():
    c = _client()
    _login(c)
    r = c.get("/nastenka")
    assert r.status_code == 200
    assert "Maily" in r.data.decode()


def test_the_maily_tab_is_admin_only():
    c = _client()
    _sklad(c)
    assert "Maily" not in c.get("/nastenka").data.decode()


def test_an_anonymous_visitor_is_sent_to_login():
    c = _client()
    r = c.get("/nastenka")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_the_board_api_needs_a_session():
    assert _client().get("/api/board/ping").status_code == 401


def test_the_board_api_ping_answers_the_sklad_role():
    c = _client()
    _sklad(c)
    r = c.get("/api/board/ping")
    assert r.status_code == 200
    assert r.get_json().get("ok") is True


def test_the_board_api_ping_answers_the_admin_role():
    c = _client()
    _login(c)
    assert c.get("/api/board/ping").status_code == 200


def test_the_board_gate_does_not_widen_the_mail_archive():
    """Reaching the board never grants the admin data API to a warehouse session."""
    c = _client()
    _sklad(c)
    assert c.get("/api/messages").status_code == 401
    assert c.get("/api/board/ping").status_code == 200


# --- migrations: audit_log + deleted_at + the retired->deleted_at backfill ----------------

SOFT_DELETE_TABLES = [
    "catalog_overrides", "dl_catalog_overrides", "customer_overrides",
    "dl_supplier_overrides", "mail_rules", "item_memory", "global_item_memory",
    "dl_item_memory", "dl_supplier_memory",
]


def _has_column(pg, table, column):
    return pg.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
        (table, column)).fetchone() is not None


def test_audit_log_table_has_every_spec_column(pg):
    cols = {r[0] for r in pg.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name='audit_log'"
    ).fetchall()}
    assert {"id", "ts", "actor", "table_name", "row_id", "action", "before", "after",
            "note", "question_id", "message_id"} <= cols, cols


def test_every_soft_delete_table_has_a_deleted_at_column(pg):
    for t in SOFT_DELETE_TABLES:
        assert _has_column(pg, t, "deleted_at"), f"{t} is missing deleted_at"


def test_the_retired_rows_are_backfilled_to_deleted_at(pg):
    """A pre-migration DB carries `retired=true` rows with NULL `deleted_at`; the r16
    backfill must stamp `deleted_at` on them (spec §5 — retired unified to deleted_at)."""
    from app import db
    pg.execute("INSERT INTO catalog_overrides (gtin, name, retired, updated_at) "
               "VALUES ('BF-RETIRED', 'x', true, now())")
    pg.execute("UPDATE catalog_overrides SET deleted_at = NULL WHERE gtin = 'BF-RETIRED'")
    rev = next(r.revision for r in db.REVISIONS if r.name == "add_deleted_at_soft_delete")
    pg.execute("DELETE FROM schema_version WHERE revision = %s", (rev,))
    db.init_schema(pg)     # re-applies r16 -> its backfill runs again
    got = pg.execute(
        "SELECT deleted_at FROM catalog_overrides WHERE gtin = 'BF-RETIRED'").fetchone()[0]
    assert got is not None, "retired row was not backfilled to deleted_at"


# --- the /znalosti DELETE endpoints now SOFT-delete + audit (no hard DELETE) -------------

def test_deleting_a_global_alias_soft_deletes_it_and_writes_an_audit_row(pg):
    c = _client()
    _login(c)
    rid = memory.add_global_alias(pg, "twister", "G9", "Karta 9", by="test")
    assert rid
    before = pg.execute("SELECT count(*) FROM global_item_memory").fetchone()[0]
    r = c.delete(f"/api/znalosti/global/{rid}")
    assert r.status_code == 200
    after = pg.execute("SELECT count(*) FROM global_item_memory").fetchone()[0]
    assert after == before, "the row was HARD-deleted — soft delete keeps it in the table"
    row = pg.execute("SELECT deleted_at FROM global_item_memory WHERE id=%s", (rid,)).fetchone()
    assert row[0] is not None, "deleted_at was not set"
    # the deleted alias no longer resolves nor lists
    assert memory.resolve_global(pg, "twister") is None
    assert all(a["id"] != rid for a in memory.list_global_aliases(pg))
    # an audit_log row records the delete
    n = pg.execute(
        "SELECT count(*) FROM audit_log WHERE table_name='global_item_memory' "
        "AND action='delete' AND row_id=%s", (str(rid),)).fetchone()[0]
    assert n == 1, "no audit_log delete row"


def test_deleting_a_catalog_card_soft_deletes_it_and_audits(pg):
    c = _client()
    _login(c)
    snapshot.upsert_catalog_card(pg, "GDEL", "Karta na zmazanie")
    snapshot.rebuild_from_overrides(pg)
    assert any(x["gtin"] == "GDEL" for x in snapshot.catalog_for_management(pg))
    r = c.delete("/api/znalosti/products/GDEL")
    assert r.status_code == 200
    row = pg.execute(
        "SELECT retired, deleted_at FROM catalog_overrides WHERE gtin='GDEL'").fetchone()
    assert row is not None, "the override row was HARD-deleted"
    assert row[0] is True and row[1] is not None, "retire must set BOTH retired and deleted_at"
    assert not any(x["gtin"] == "GDEL" for x in snapshot.catalog_for_management(pg))
    n = pg.execute(
        "SELECT count(*) FROM audit_log WHERE table_name='catalog_overrides' "
        "AND action='delete' AND row_id='GDEL'").fetchone()[0]
    assert n == 1


def test_snapshot_rebuild_excludes_a_row_flagged_ONLY_by_deleted_at(pg):
    """deleted_at alone (retired still false) must exclude the card from the effective
    catalog — proving the reader treats deleted_at exactly like retired (spec §5)."""
    pg.execute("INSERT INTO catalog_overrides (gtin, name, retired, deleted_at, updated_at) "
               "VALUES ('GSOFT', 'Soft-deleted', false, now(), now())")
    assert not any(x["gtin"] == "GSOFT" for x in snapshot.catalog_for_management(pg))


# --- the audit restore skeleton (deleted_at restore only, spec §5) ----------------------

def test_restore_undeletes_a_soft_deleted_row_and_audits(pg):
    from app.board.services import audit
    rid = memory.add_global_alias(pg, "restoreme", "G7", "Karta 7", by="test")
    c = _client()
    _login(c)
    c.delete(f"/api/znalosti/global/{rid}")
    audit_id = pg.execute(
        "SELECT id FROM audit_log WHERE table_name='global_item_memory' "
        "AND action='delete' AND row_id=%s", (str(rid),)).fetchone()[0]
    assert audit.restore(pg, audit_id) is True
    assert pg.execute(
        "SELECT deleted_at FROM global_item_memory WHERE id=%s", (rid,)).fetchone()[0] is None
    assert memory.resolve_global(pg, "restoreme") is not None
    n = pg.execute(
        "SELECT count(*) FROM audit_log WHERE table_name='global_item_memory' "
        "AND action='restore' AND row_id=%s", (str(rid),)).fetchone()[0]
    assert n == 1
