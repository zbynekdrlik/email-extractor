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
