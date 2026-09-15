"""Lane 2 of the unified nástenka (#443): Otázky sklad + Otázky objednávky.

Service (`app.board.services.questions`) + board API endpoints under `/api/board/*`.
Every answer/undo path DELEGATES to the SAME `teach`/`hold`/`httpapi_orders_questions`
machinery the old boards use — these tests assert the identical DB effect, never a
re-implementation. Reopen (expired → open + hold back to held + audit `reopen`) and the
scope-guarded original preview (`/files`/`/eml` restricted to messages that carry a
question) are the genuinely-new behaviour.

Scope mapping (confirmed from the ticket's own E2E: `/nastenka/otazky-objednavky` answers
an ITEM question): `otazky-objednavky` = orders (item/customer/mail/date/line);
`otazky-sklad` = dl (dl_item/dl_supplier).
"""
import json
import os

from app.config import Config
from app.httpapi import create_app, dl_key, sklad_key
from app.orders import teach

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


def _msg(pg, mid="m1", subject="Objednávka chleba", from_addr="cust@x.sk",
         from_name="Pekáreň"):
    pg.execute(
        "INSERT INTO messages (message_id, from_addr, from_name, subject) "
        "VALUES (%s, %s, %s, %s)", (mid, from_addr, from_name, subject))


def _q(pg, kind="item", wording="rožok", customer_ean="2000000000001",
       customer_name="Zákazník A", item_key=None, candidates=None, message_id="m1",
       status="open", context=None, payload=None):
    item_key = item_key if item_key is not None else f"{kind}:{wording}"
    row = pg.execute(
        """INSERT INTO order_questions
               (message_id, customer_ean, customer_name, wording, item_key, kind,
                candidates, delivery_date, reason, context, payload, status)
           VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, '', '', %s::jsonb, %s::jsonb, %s)
           RETURNING id""",
        (message_id, customer_ean, customer_name, wording, item_key, kind,
         json.dumps(candidates or []), json.dumps(context or {}),
         json.dumps(payload or {}), status)).fetchone()
    return int(row[0])


# --- list / filter / search --------------------------------------------------------

def test_open_list_is_scoped_to_the_tab_kinds(pg):
    _msg(pg)
    qi = _q(pg, kind="item", wording="rožok", item_key="item:rozok")
    qd = _q(pg, kind="dl_item", customer_ean="", wording="múka", item_key="dlitem:x:muka")
    c = _client()
    _sklad(c)
    orders = {x["id"] for x in
              c.get("/api/board/questions?scope=orders&status=open").get_json()["items"]}
    assert qi in orders and qd not in orders
    dl = {x["id"] for x in
          c.get("/api/board/questions?scope=dl&status=open").get_json()["items"]}
    assert qd in dl and qi not in dl


def test_expired_bucket_shows_only_expired(pg):
    _msg(pg)
    q = _q(pg, kind="item", item_key="item:a", status="expired")
    c = _client()
    _sklad(c)
    assert q not in {x["id"] for x in
                     c.get("/api/board/questions?scope=orders&status=open")
                     .get_json()["items"]}
    assert q in {x["id"] for x in
                 c.get("/api/board/questions?scope=orders&status=expired")
                 .get_json()["items"]}


def test_answered_bucket_shows_answered(pg):
    _msg(pg)
    q = _q(pg, kind="item", item_key="item:a")
    pg.execute("UPDATE order_questions SET status='answered', answered_at=now() "
               "WHERE id=%s", (q,))
    c = _client()
    _sklad(c)
    assert q in {x["id"] for x in
                 c.get("/api/board/questions?scope=orders&status=answered")
                 .get_json()["items"]}


def test_search_filters_by_wording(pg):
    _msg(pg)
    a = _q(pg, item_key="item:a", wording="rožok maslový")
    b = _q(pg, item_key="item:b", wording="chlieb ražný")
    c = _client()
    _sklad(c)
    ids = {x["id"] for x in
           c.get("/api/board/questions?scope=orders&status=open&q=chlieb")
           .get_json()["items"]}
    assert b in ids and a not in ids


def test_search_matches_sender_email_in_context(pg):
    _msg(pg)
    a = _q(pg, kind="customer", customer_ean="", wording="", item_key="cust:a",
           context={"sender_email": "hladany@dodavatel.sk"})
    b = _q(pg, kind="customer", customer_ean="", wording="", item_key="cust:b",
           context={"sender_email": "iny@nikde.sk"})
    c = _client()
    _sklad(c)
    ids = {x["id"] for x in
           c.get("/api/board/questions?scope=orders&status=open&q=hladany")
           .get_json()["items"]}
    assert a in ids and b not in ids


def test_bad_scope_is_rejected(pg):
    c = _client()
    _sklad(c)
    assert c.get("/api/board/questions?scope=bogus&status=open").status_code == 400


def test_board_questions_needs_a_session(pg):
    # board_gate: an unauthenticated /api/board/* call is 401, never open.
    assert _client().get("/api/board/questions?scope=orders").status_code == 401


# --- reopen an expired question ----------------------------------------------------

def test_reopen_expired_reopens_puts_hold_back_and_audits(pg):
    _msg(pg)
    q = _q(pg, kind="item", item_key="item:a", status="expired")
    pg.execute("UPDATE order_questions SET answer='{\"expired\": true}'::jsonb, "
               "answered_by='auto-expiry', answered_at=now() WHERE id=%s", (q,))
    pg.execute(
        """INSERT INTO held_orders (message_id, customer_ean, customer_name, question_ids,
               status, release_reason, released_at)
           VALUES ('m1', '2000000000001', 'Zákazník A', %s, 'released', 'expired', now())""",
        ([q],))
    c = _client()
    _sklad(c)
    r = c.post(f"/api/board/questions/{q}/reopen")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert pg.execute("SELECT status FROM order_questions WHERE id=%s",
                      (q,)).fetchone()[0] == "open"
    held = pg.execute("SELECT status, release_reason FROM held_orders "
                      "WHERE %s = ANY(question_ids)", (q,)).fetchone()
    assert held[0] == "held" and held[1] is None
    n = pg.execute("SELECT count(*) FROM audit_log WHERE question_id=%s AND action='reopen'",
                   (q,)).fetchone()[0]
    assert n == 1


def test_reopen_refuses_a_non_expired_question(pg):
    _msg(pg)
    q = _q(pg, kind="item", item_key="item:a", status="open")
    c = _client()
    _sklad(c)
    assert c.post(f"/api/board/questions/{q}/reopen").status_code == 409


# --- undo an answered question (delegates to teach; writes audit) -------------------

def test_undo_reopens_and_writes_audit(pg):
    _msg(pg)
    q = _q(pg, kind="item", candidates=[{"gtin": "G1", "name": "Karta A"}],
           item_key="item:a")
    with __import__("app.db", fromlist=["connect"]).connect(PG_DSN) as conn:
        teach.answer(conn, q, gtin="G1", card="Karta A", by="sklad")
    c = _client()
    _sklad(c)
    r = c.post(f"/api/board/questions/{q}/undo")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert pg.execute("SELECT status FROM order_questions WHERE id=%s",
                      (q,)).fetchone()[0] == "open"
    assert pg.execute("SELECT count(*) FROM audit_log WHERE question_id=%s AND action='undo'",
                      (q,)).fetchone()[0] >= 1


# --- answer delegation: identical DB effect as the legacy endpoint ------------------

def test_answer_item_matches_the_legacy_endpoint_effect(pg):
    _msg(pg, mid="m1")
    _msg(pg, mid="m2")
    qa = _q(pg, message_id="m1", customer_ean="2000000000001",
            candidates=[{"gtin": "G1", "name": "Karta"}], item_key="item:a")
    qb = _q(pg, message_id="m2", customer_ean="2000000000002",
            candidates=[{"gtin": "G1", "name": "Karta"}], item_key="item:a")
    c = _client()
    _sklad(c)
    assert c.post(f"/api/board/questions/{qa}/answer",
                  json={"gtin": "G1", "card": "Karta"}).status_code == 200
    assert c.post(f"/api/orders/question/{qb}/answer",
                  json={"gtin": "G1", "card": "Karta"}).status_code == 200
    # both taught a human item_memory row for gtin G1 (same DB shape, different customer)
    eans = [r[0] for r in pg.execute(
        "SELECT customer_ean FROM item_memory WHERE source='human' AND gtin='G1' "
        "ORDER BY customer_ean").fetchall()]
    assert eans == ["2000000000001", "2000000000002"]
    for q in (qa, qb):
        assert pg.execute("SELECT status FROM order_questions WHERE id=%s",
                          (q,)).fetchone()[0] == "answered"


def test_answer_line_generic_delegates(pg):
    _msg(pg)
    q = _q(pg, kind="line", customer_ean="", wording="fantómový riadok",
           item_key="line:m1:x")
    c = _client()
    _sklad(c)
    r = c.post(f"/api/board/questions/{q}/answer", json={"choice": "keep"})
    assert r.status_code == 200, r.get_data(as_text=True)
    row = pg.execute("SELECT status, answer FROM order_questions WHERE id=%s",
                     (q,)).fetchone()
    assert row[0] == "answered" and row[1] == {"choice": "keep"}


def test_answer_customer_unknown_delegates(pg):
    _msg(pg)
    q = _q(pg, kind="customer", customer_ean="", wording="",
           item_key="cust:m1", context={"sender_email": "cudzi@nikde.sk"})
    c = _client()
    _sklad(c)
    r = c.post(f"/api/board/questions/{q}/answer", json={"unknown": True})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert pg.execute("SELECT status FROM order_questions WHERE id=%s",
                      (q,)).fetchone()[0] == "answered"


def test_answer_dl_not_warehouse_delegates(pg):
    _msg(pg, mid="m1")
    q = _q(pg, kind="dl_item", customer_ean="", wording="múka",
           item_key="dlitem:x:muka")
    c = _client()
    _dl(c)
    r = c.post(f"/api/board/questions/{q}/answer", json={"not_warehouse": True})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert pg.execute("SELECT status FROM order_questions WHERE id=%s",
                      (q,)).fetchone()[0] == "not_warehouse"
    assert pg.execute("SELECT processed FROM messages WHERE message_id='m1'"
                      ).fetchone()[0] is True


# --- §6: the board decides scope by TAB, not by which key logged in -----------------

def test_dl_session_may_answer_an_orders_question_on_the_board(pg):
    """A DL-key session keeps every tab on the unified board (spec §6) — so it may answer
    an ORDERS question there. The LEGACY orders endpoint still refuses it (unchanged)."""
    _msg(pg, mid="m1")
    _msg(pg, mid="m2")
    q1 = _q(pg, message_id="m1", kind="item",
            candidates=[{"gtin": "G1", "name": "K"}], item_key="item:a")
    q2 = _q(pg, message_id="m2", kind="item",
            candidates=[{"gtin": "G1", "name": "K"}], item_key="item:b")
    c = _client()
    _dl(c)
    assert c.post(f"/api/board/questions/{q1}/answer",
                  json={"gtin": "G1", "card": "K"}).status_code == 200
    # the legacy endpoint keeps its per-key kind boundary
    assert c.post(f"/api/orders/question/{q2}/answer",
                  json={"gtin": "G1", "card": "K"}).status_code == 403


# --- original preview + scope-guarded /files, /eml ---------------------------------

def test_preview_returns_metadata_and_scopes_file_access(pg, tmp_path):
    from app.store import message_dir
    cfg = _cfg(data_dir=str(tmp_path))
    _msg(pg, mid="m1", subject="Faktúra 123", from_addr="a@b.sk", from_name="Dodávateľ")
    pg.execute("INSERT INTO attachments (message_id, idx, filename, mime) "
               "VALUES ('m1', 0, 'sken.pdf', 'application/pdf')")
    d = message_dir(str(tmp_path), "m1")
    d.mkdir(parents=True, exist_ok=True)
    (d / "att0__sken.pdf").write_bytes(b"%PDF-1.4 test")
    q = _q(pg, kind="item", message_id="m1", item_key="item:a")
    c = _client(cfg)
    _sklad(c)
    pv = c.get(f"/api/board/questions/{q}/preview")
    assert pv.status_code == 200, pv.get_data(as_text=True)
    j = pv.get_json()
    assert j["subject"] == "Faktúra 123" and j["from_addr"] == "a@b.sk"
    assert any(a["idx"] == 0 for a in j["attachments"])
    # a message that carries a question → the sklad session may open its attachment
    assert c.get("/api/board/files/m1/0").status_code == 200
    # a message with NO question → refused (scope guard), never leaks another mail's file
    assert c.get("/api/board/files/no-such-mid/0").status_code in (403, 404)


def test_preview_404_for_unknown_question(pg):
    c = _client()
    _sklad(c)
    assert c.get("/api/board/questions/999999/preview").status_code == 404
