"""Dashboard data API + auth-gate tests (Flask test client + real Postgres)."""
import os

from app import db
from app.config import Config
from app.httpapi import create_app

PG_DSN = os.environ.get("PG_TEST_DSN")


def _client():
    cfg = Config(pg_dsn=PG_DSN, data_dir="/tmp", api_token="tok",
                 dash_password="secret", secret_key="test-secret")
    app = create_app(cfg)
    app.testing = True
    return app.test_client()


def _login(c):
    c.post("/login", data={"password": "secret"})


def test_dashboard_requires_login(pg):
    c = _client()
    assert c.get("/").status_code == 302          # redirect to /login
    assert c.get("/api/messages").status_code == 401


def test_login_opens_dashboard(pg):
    c = _client()
    _login(c)
    assert c.get("/").status_code == 200
    assert c.get("/api/messages").status_code == 200


def test_bad_password_rejected(pg):
    c = _client()
    assert c.post("/login", data={"password": "nope"}).status_code == 401
    assert c.get("/api/messages").status_code == 401


def test_machine_endpoint_uses_token_not_session(pg):
    # /files is token-gated, not session — no session must give 403 (token), not a login redirect.
    assert _client().get("/files/x/0").status_code == 403


def test_list_search_and_category_filter(pg):
    pg.execute("INSERT INTO messages (message_id, from_addr, subject, body_text, category, processed) "
               "VALUES ('a','x@x.sk','Objednávka','telo kvasok','ai_orders', true)")
    pg.execute("INSERT INTO messages (message_id, from_addr, subject, category) "
               "VALUES ('b','y@y.sk','Faktura','invoices')")
    c = _client()
    _login(c)
    d = c.get("/api/messages").get_json()
    assert d["total"] == 2
    assert d["counts"]["done"] == 1
    assert c.get("/api/messages?q=kvasok").get_json()["total"] == 1
    inv = c.get("/api/messages?category=invoices").get_json()
    assert inv["total"] == 1
    assert inv["items"][0]["subject"] == "Faktura"


def test_state_reviewed_and_fix_counts(pg):
    pg.execute("INSERT INTO messages (message_id, category, proc_status, processed) "
               "VALUES ('d','ai_orders','ok', true)")
    pg.execute("INSERT INTO messages (message_id, category, proc_status) VALUES ('r','ai_orders','review')")
    pg.execute("INSERT INTO messages (message_id, category, proc_status) VALUES ('e','ai_orders','error')")
    pg.execute("INSERT INTO messages (message_id, category, processing_at) VALUES ('p','ai_orders', now())")
    pg.execute("INSERT INTO messages (message_id, category, review_status) VALUES ('v','ai_orders','corrected')")
    pg.execute("INSERT INTO fix_requests (message_id, status) VALUES ('e','open')")
    c = _client()
    _login(c)

    def total(qs):
        return c.get("/api/messages?" + qs).get_json()["total"]

    assert total("state=done") == 1
    assert total("state=review") == 1
    assert total("state=error") == 1
    assert total("state=processing") == 1
    assert total("state=onfix") == 1
    assert total("reviewed=corrected") == 1
    counts = c.get("/api/messages").get_json()["counts"]
    assert counts["on_fix"] == 1
    assert counts["review"] == 1
    assert counts["error"] == 1


def test_detail_with_timeline_and_404(pg):
    pg.execute("INSERT INTO messages (message_id, subject, category) VALUES ('m','Test','ai_orders')")
    mid = pg.execute("SELECT id FROM messages WHERE message_id='m'").fetchone()[0]
    db.log_event(pg, "m", "ai_orders", "claimed", "ok")
    db.log_event(pg, "m", "ai_orders", "uploaded_orion", "ok", outcome="EDI",
                 detail={"edi_file": "O.txt"})
    c = _client()
    _login(c)
    d = c.get(f"/api/message/{mid}").get_json()
    assert d["subject"] == "Test"
    assert len(d["events"]) == 2
    assert d["events"][-1]["stage"] == "uploaded_orion"
    assert d["edi_file"] == "O.txt"
    assert c.get("/api/message/99999999").status_code == 404


def test_search_matches_attachment_text(pg):
    pg.execute("INSERT INTO messages (message_id, subject, category) VALUES ('att','S','ai_orders')")
    pg.execute("INSERT INTO attachments (message_id, idx, filename, extracted_text) "
               "VALUES ('att',0,'f.pdf','tajnyklucvtexte')")
    c = _client()
    _login(c)
    d = c.get("/api/messages?q=tajnyklucvtexte").get_json()
    assert d["total"] == 1
    assert d["items"][0]["subject"] == "S"


def test_token_authorizes_api_and_files(pg):
    pg.execute("INSERT INTO messages (message_id, subject, category) VALUES ('t','S','ai_orders')")
    c = _client()
    # no session, but a valid machine token authorizes the data API
    assert c.get("/api/messages?token=tok").status_code == 200
    # /files with a valid token but missing file -> 404 (authorized), not 403
    assert c.get("/files/nope/0?token=tok").status_code == 404


def test_like_metacharacters_are_literal(pg):
    pg.execute("INSERT INTO messages (message_id, subject, category) VALUES ('x1','50% zlava','ai_orders')")
    pg.execute("INSERT INTO messages (message_id, subject, category) VALUES ('x2','nic','ai_orders')")
    c = _client()
    _login(c)
    d = c.get("/api/messages?q=%25").get_json()    # %25 decodes to a literal '%'
    assert d["total"] == 1
    assert d["items"][0]["subject"] == "50% zlava"


def test_invalid_date_filter_returns_400(pg):
    c = _client()
    _login(c)
    assert c.get("/api/messages?from=2026-13-99").status_code == 400
    assert c.get("/api/messages?to=garbage").status_code == 400


def test_date_to_is_inclusive_of_whole_day(pg):
    pg.execute("INSERT INTO messages (message_id, subject, category) VALUES ('td','dnes','ai_orders')")
    c = _client()
    _login(c)
    today = pg.execute("SELECT to_char(now(),'YYYY-MM-DD')").fetchone()[0]
    d = c.get(f"/api/messages?to={today}").get_json()
    assert d["total"] == 1     # a message created today is within to=today (inclusive)


# ---- #14 operator actions ----

def test_reclassify_changes_category_and_logs(pg):
    pg.execute("INSERT INTO messages (message_id, subject, category, processed) "
               "VALUES ('rc','S','invoices', true)")
    mid = pg.execute("SELECT id FROM messages WHERE message_id='rc'").fetchone()[0]
    c = _client()
    _login(c)
    assert c.post(f"/api/message/{mid}/reclassify", json={"category": "ai_orders"}).status_code == 200
    row = pg.execute("SELECT category, original_category, processed, review_status "
                     "FROM messages WHERE id=%s", (mid,)).fetchone()
    assert row == ("ai_orders", "invoices", False, "corrected")
    ev = pg.execute("SELECT stage, status FROM email_events WHERE message_id='rc' "
                    "ORDER BY id DESC LIMIT 1").fetchone()
    assert ev == ("reclassified", "ok")


def test_reclassify_bad_category_400_and_missing_404(pg):
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('rc2','invoices')")
    mid = pg.execute("SELECT id FROM messages WHERE message_id='rc2'").fetchone()[0]
    c = _client()
    _login(c)
    assert c.post(f"/api/message/{mid}/reclassify", json={"category": "nope"}).status_code == 400
    assert c.post("/api/message/9999999/reclassify", json={"category": "ai_orders"}).status_code == 404


def test_reprocess_resets_flags_and_logs(pg):
    pg.execute("INSERT INTO messages (message_id, category, processed, error) "
               "VALUES ('rp','ai_orders', true, 'boom')")
    mid = pg.execute("SELECT id FROM messages WHERE message_id='rp'").fetchone()[0]
    c = _client()
    _login(c)
    assert c.post(f"/api/message/{mid}/reprocess").status_code == 200
    assert pg.execute("SELECT processed, error FROM messages WHERE id=%s", (mid,)).fetchone() == (False, None)
    assert pg.execute("SELECT count(*) FROM email_events WHERE message_id='rp' "
                      "AND stage='requeued'").fetchone()[0] == 1
    assert c.post("/api/message/9999999/reprocess").status_code == 404


# ---- #15 fix queue ----

def test_fix_request_inserts_snapshot_event_and_shows_in_queue(pg):
    pg.execute("INSERT INTO messages (message_id, subject, category, proc_status, proc_outcome) "
               "VALUES ('fx','Objednavka','ai_orders','review','prazdny')")
    mid = pg.execute("SELECT id FROM messages WHERE message_id='fx'").fetchone()[0]
    c = _client()
    _login(c)
    r = c.post(f"/api/message/{mid}/fix",
               json={"problem_type": "mis_processed", "description": "zle qty"})
    assert r.status_code == 200
    fid = r.get_json()["fix_id"]
    row = pg.execute("SELECT problem_type, status, snapshot->>'subject', created_by "
                     "FROM fix_requests WHERE id=%s", (fid,)).fetchone()
    assert row == ("mis_processed", "open", "Objednavka", "dashboard")
    assert pg.execute("SELECT count(*) FROM email_events WHERE message_id='fx' "
                      "AND stage='fix_requested'").fetchone()[0] == 1
    assert c.get("/api/messages?state=onfix").get_json()["total"] == 1
    q = c.get("/api/fix-queue").get_json()["items"]
    assert len(q) == 1
    assert q[0]["problem_type"] == "mis_processed"
    assert q[0]["subject"] == "Objednavka"


def test_fix_validates_inputs(pg):
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('fx2','ai_orders')")
    mid = pg.execute("SELECT id FROM messages WHERE message_id='fx2'").fetchone()[0]
    c = _client()
    _login(c)
    assert c.post(f"/api/message/{mid}/fix", json={"problem_type": "bogus"}).status_code == 400
    assert c.post(f"/api/message/{mid}/fix",
                  json={"problem_type": "mis_sorted", "expected_category": "nope"}).status_code == 400
    assert c.post("/api/message/9999999/fix", json={"problem_type": "other"}).status_code == 404


def test_fix_queue_status_filter_and_resolve(pg):
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('fq','ai_orders')")
    pg.execute("INSERT INTO fix_requests (message_id, problem_type, status) "
               "VALUES ('fq','mis_sorted','open')")
    fid = pg.execute("SELECT id FROM fix_requests WHERE message_id='fq'").fetchone()[0]
    c = _client()
    _login(c)
    assert len(c.get("/api/fix-queue?status=open").get_json()["items"]) == 1
    assert len(c.get("/api/fix-queue?status=fixed").get_json()["items"]) == 0
    r = c.post(f"/api/fix/{fid}/resolve", json={"status": "fixed", "resolution": "opravene v #99"})
    assert r.status_code == 200
    assert pg.execute("SELECT status, resolution, resolved_at IS NOT NULL "
                      "FROM fix_requests WHERE id=%s", (fid,)).fetchone() == ("fixed", "opravene v #99", True)
    assert c.post(f"/api/fix/{fid}/resolve", json={"status": "bogus"}).status_code == 400
    assert c.post("/api/fix/9999999/resolve", json={"status": "fixed"}).status_code == 404


def test_actions_require_auth(pg):
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('au','ai_orders')")
    mid = pg.execute("SELECT id FROM messages WHERE message_id='au'").fetchone()[0]
    c = _client()   # no login, no token
    assert c.post(f"/api/message/{mid}/reclassify", json={"category": "invoices"}).status_code == 401
    assert c.post(f"/api/message/{mid}/fix", json={"problem_type": "other"}).status_code == 401
    assert c.get("/api/fix-queue").status_code == 401


def test_fix_does_not_clobber_proc_status(pg):
    pg.execute("INSERT INTO messages (message_id, subject, category, proc_status, proc_stage, "
               "proc_outcome, processed) "
               "VALUES ('done','S','ai_orders','ok','uploaded_orion','EDI nahrate', true)")
    mid = pg.execute("SELECT id FROM messages WHERE message_id='done'").fetchone()[0]
    c = _client()
    _login(c)
    assert c.post(f"/api/message/{mid}/fix", json={"problem_type": "mis_processed"}).status_code == 200
    # a done order flagged for fixing stays done — proc_status NOT flipped to 'review'
    assert pg.execute("SELECT proc_status, proc_outcome FROM messages WHERE id=%s",
                      (mid,)).fetchone() == ("ok", "EDI nahrate")
    counts = c.get("/api/messages").get_json()["counts"]
    assert counts["review"] == 0
    assert counts["done"] == 1
    assert counts["on_fix"] == 1     # but it shows in the on-fix bucket


def test_reclassify_does_not_clobber_proc_status(pg):
    pg.execute("INSERT INTO messages (message_id, category, proc_status) "
               "VALUES ('rv','invoices','review')")
    mid = pg.execute("SELECT id FROM messages WHERE message_id='rv'").fetchone()[0]
    c = _client()
    _login(c)
    c.post(f"/api/message/{mid}/reclassify", json={"category": "ai_orders"})
    # proc_status stays 'review' (pipeline-owned); only category/processed change
    assert pg.execute("SELECT proc_status, category, processed FROM messages WHERE id=%s",
                      (mid,)).fetchone() == ("review", "ai_orders", False)


def test_actions_accept_json_without_content_type_header(pg):
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('ct','invoices')")
    mid = pg.execute("SELECT id FROM messages WHERE message_id='ct'").fetchone()[0]
    c = _client()
    _login(c)
    # raw JSON body, no application/json header (curl -d / n8n default) -> still parsed
    r = c.post(f"/api/message/{mid}/reclassify", data='{"category":"ai_orders"}',
               content_type="text/plain")
    assert r.status_code == 200
    assert pg.execute("SELECT category FROM messages WHERE id=%s", (mid,)).fetchone()[0] == "ai_orders"


def test_fix_resolve_writes_audit_event(pg):
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('re','ai_orders')")
    pg.execute("INSERT INTO fix_requests (message_id, problem_type, status) "
               "VALUES ('re','other','open')")
    fid = pg.execute("SELECT id FROM fix_requests WHERE message_id='re'").fetchone()[0]
    c = _client()
    _login(c)
    c.post(f"/api/fix/{fid}/resolve", json={"status": "fixed", "resolution": "done"})
    ev = pg.execute("SELECT stage, status FROM email_events WHERE message_id='re' "
                    "ORDER BY id DESC LIMIT 1").fetchone()
    assert ev == ("fix_resolved", "ok")
    # rollup=False -> the resolve event does not set proc_status
    assert pg.execute("SELECT proc_status FROM messages WHERE message_id='re'").fetchone()[0] is None


def test_fix_queue_paginates(pg):
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('pg1','ai_orders')")
    for _ in range(3):
        pg.execute("INSERT INTO fix_requests (message_id, problem_type, status) "
                   "VALUES ('pg1','other','open')")
    c = _client()
    _login(c)
    d = c.get("/api/fix-queue?limit=2").get_json()
    assert d["total"] == 3
    assert len(d["items"]) == 2
    assert len(c.get("/api/fix-queue?limit=2&offset=2").get_json()["items"]) == 1
