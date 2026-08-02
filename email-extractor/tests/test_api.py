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


def test_token_authorizes_files_not_api(pg):
    pg.execute("INSERT INTO messages (message_id, subject, category) VALUES ('t','S','ai_orders')")
    c = _client()
    # the data API is session-only — a machine token must NOT authorize it
    assert c.get("/api/messages?token=tok").status_code == 401
    # but the file APIs accept the token (missing file -> 404, i.e. authorized)
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


# ---- #20: emails that never made it in must be visible somewhere ----

def test_imap_failures_endpoint_lists_pending_and_skipped(pg):
    pg.execute("TRUNCATE imap_failures")
    db.record_uid_failure(pg, "INBOX", 1, 41, "RuntimeError('OCR out of memory')")
    for _ in range(db.MAX_UID_ATTEMPTS):
        db.record_uid_failure(pg, "INBOX", 1, 42, "ValueError('broken part')")
    db.mark_uid_skipped(pg, "INBOX", 1, 42)
    c = _client()
    _login(c)
    d = c.get("/api/imap-failures").get_json()
    assert d["total"] == 2
    assert d["pending"] == 1 and d["skipped"] == 1
    assert d["max_attempts"] == db.MAX_UID_ATTEMPTS
    by_uid = {i["uid"]: i for i in d["items"]}
    assert by_uid[41]["attempts"] == 1 and by_uid[41]["skipped"] is False
    assert by_uid[42]["skipped"] is True
    assert "OCR out of memory" in by_uid[41]["last_error"]


def test_imap_failures_endpoint_needs_login(pg):
    assert _client().get("/api/imap-failures").status_code == 401


def test_dashboard_has_the_imap_failures_tab(pg):
    c = _client()
    _login(c)
    html = c.get("/").get_data(as_text=True)
    assert "/api/imap-failures" in html
    assert "tabImap" in html


# ---- #21: /files and /eml must never serve another email's originals ----

def test_files_are_not_served_across_a_colliding_message_id(pg, tmp_path):
    from app import store
    long_prefix = "y" * 130
    id_a, id_b = f"<{long_prefix}.a@m.example>", f"<{long_prefix}.b@m.example>"
    store.save_message(str(tmp_path), id_a, b"EML-A",
                       [{"filename": "a.pdf", "_data": b"PDF-A"}], "http://x")
    store.save_message(str(tmp_path), id_b, b"EML-B",
                       [{"filename": "b.pdf", "_data": b"PDF-B"}], "http://x")
    cfg = Config(pg_dsn=PG_DSN, data_dir=str(tmp_path), api_token="tok",
                 dash_password="secret", secret_key="test-secret")
    app = create_app(cfg)
    app.testing = True
    c = app.test_client()
    assert c.get(f"/files/{id_a}/0?token=tok").data == b"PDF-A"
    assert c.get(f"/files/{id_b}/0?token=tok").data == b"PDF-B"
    assert c.get(f"/eml/{id_a}?token=tok").data == b"EML-A"
    assert c.get(f"/eml/{id_b}?token=tok").data == b"EML-B"


def test_files_still_serve_legacy_storage_dirs(pg, tmp_path):
    from app import store
    mid = "<legacy.dir@m.example>"
    old = tmp_path / store.legacy_safe_id(mid)
    old.mkdir()
    (old / "raw.eml").write_bytes(b"OLD-EML")
    (old / "att0__x.pdf").write_bytes(b"OLD-PDF")
    cfg = Config(pg_dsn=PG_DSN, data_dir=str(tmp_path), api_token="tok",
                 dash_password="secret", secret_key="test-secret")
    app = create_app(cfg)
    app.testing = True
    c = app.test_client()
    assert c.get(f"/files/{mid}/0?token=tok").data == b"OLD-PDF"
    assert c.get(f"/eml/{mid}?token=tok").data == b"OLD-EML"


# ---- #25: operator actions must not steal an in-flight claim (double-process) ----

def _msg_claimed(pg, minutes_ago: float) -> int:
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('claimed@t','ai_orders')")
    return pg.execute(
        """UPDATE messages SET processing_at = now() - (%s || ' minutes')::interval,
                               processed_by = 'n8n-worker'
           WHERE message_id='claimed@t' RETURNING id""", (minutes_ago,)).fetchone()[0]


def test_reprocess_refuses_while_a_worker_holds_the_message(pg):
    mid = _msg_claimed(pg, 1)
    c = _client()
    _login(c)
    r = c.post(f"/api/message/{mid}/reprocess")
    assert r.status_code == 409, "a fresh claim must not be cleared under the worker"
    assert "spracúva" in r.get_json()["error"]
    row = pg.execute("SELECT processing_at, processed_by FROM messages WHERE id=%s",
                     (mid,)).fetchone()
    assert row[0] is not None and row[1] == 'n8n-worker', "claim left untouched"


def test_reclassify_refuses_while_a_worker_holds_the_message(pg):
    mid = _msg_claimed(pg, 2)
    c = _client()
    _login(c)
    r = c.post(f"/api/message/{mid}/reclassify", json={"category": "invoices"})
    assert r.status_code == 409
    row = pg.execute("SELECT category, processing_at FROM messages WHERE id=%s",
                     (mid,)).fetchone()
    assert row == ("ai_orders", row[1]) and row[1] is not None


def test_reprocess_clears_a_stale_claim(pg):
    mid = _msg_claimed(pg, db.CLAIM_STALE_MINUTES + 1)
    c = _client()
    _login(c)
    assert c.post(f"/api/message/{mid}/reprocess").status_code == 200
    assert pg.execute("SELECT processing_at FROM messages WHERE id=%s",
                      (mid,)).fetchone()[0] is None


def test_reclassify_still_works_when_nothing_is_in_flight(pg):
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('free@t','ai_orders')")
    mid = pg.execute("SELECT id FROM messages WHERE message_id='free@t'").fetchone()[0]
    c = _client()
    _login(c)
    assert c.post(f"/api/message/{mid}/reclassify", json={"category": "invoices"}).status_code == 200
    assert pg.execute("SELECT category FROM messages WHERE id=%s", (mid,)).fetchone()[0] == "invoices"


# --- #93 review finding: the /answer route's release must not share ONE rollback-able
# transaction with teach.answer, or a failure AFTER the real ORION upload rolls back the
# edi_sent ledger claim too — even though the document was already physically delivered —
# and a retry re-uploads it (the exact #81.1 double-shipment defect this feature exists to
# prevent). ---

def test_answering_over_http_commits_the_ledger_even_if_something_after_upload_fails(
        pg, monkeypatch):
    from psycopg.types.json import Json

    from app.orders import hold, report

    pg.execute("INSERT INTO messages (message_id, category) VALUES ('m93', 'ai_orders')")
    qid = pg.execute(
        """INSERT INTO order_questions (message_id, customer_ean, customer_name, wording,
                                        item_key, quantity, unit, candidates, delivery_date,
                                        reason)
           VALUES ('m93', '2000000000864', 'Pekáreň', 'torta', 'torta', 5, 'ks', %s,
                   '04.08.2026', 'test') RETURNING id""",
        (Json([{"gtin": "TOR", "name": "Torta čokoládová"}]),)).fetchone()[0]
    pg.execute(
        """INSERT INTO held_orders (message_id, customer_ean, customer_name, delivery_date,
                                    order_number, question_ids, order_json, extracted_json,
                                    decisions_json)
           VALUES ('m93', '2000000000864', 'Pekáreň', '04.08.2026', '', %s, %s, %s, %s)""",
        ([qid], Json({"deliveryDate": "04.08.2026", "orderNumber": ""}),
         Json({"isChangeRequest": False, "unverified": [], "notes": ""}),
         Json([{"item_name": "torta", "gtin": None, "card": "", "confidence": 0.1,
                "rule": "unmatched", "note": "", "review": False, "trace": {},
                "quantity": 5, "unit": "ks"}])))

    uploads = []
    monkeypatch.setattr(
        "app.orders.upload.put",
        lambda cfg, name, content: uploads.append((name, content)) or True)
    real_log_event = report.log_event
    calls = {"n": 0}

    def flaky_log_event(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom — something unrelated fails AFTER the real upload")
        return real_log_event(*a, **kw)

    monkeypatch.setattr("app.orders.report.log_event", flaky_log_event)

    cfg = Config(pg_dsn=PG_DSN, data_dir="/tmp", api_token="tok", dash_password="secret",
                 secret_key="test-secret", odoo_url="", odoo_api_key="", orders_channel_id=0)
    app = create_app(cfg)          # testing=False → Flask turns the raised error into a 500
    c = app.test_client()
    _login(c)
    r = c.post(f"/api/orders/question/{qid}/answer",
              json={"gtin": "TOR", "card": "Torta čokoládová"})
    # the request itself fails on the unrelated post-upload crash — expected, and NOT the
    # point of this test
    assert r.status_code == 500

    # what MUST hold regardless: the document really was uploaded once, and the ledger claim
    # for it is DURABLY committed — never rolled back by the surrounding request's failure
    assert len(uploads) == 1
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 1
    # the answer itself is not lost either — it committed in its own, earlier transaction
    assert pg.execute(
        "SELECT status FROM order_questions WHERE id=%s", (qid,)).fetchone() == ("answered",)

    # proof, not assumption: retrying the SAME release (the question is already answered,
    # so this re-derives the identical "torta" -> TOR decision and rebuilds the identical
    # document) must not upload a second document — the ledger is what stops it, and the
    # row must still be there to retry in the first place (held_orders never advanced past
    # 'held' — the release itself never finished)
    assert pg.execute("SELECT status FROM held_orders").fetchone() == ("held",)
    released = hold.release_for_question(
        pg, cfg, qid, upload=lambda cfg, name, content: uploads.append((name, content)),
        post=lambda *a, **k: None)
    assert len(released) == 1 and released[0]["status"] == "ok"
    assert len(uploads) == 1, "a retry must never re-upload once the ledger already claimed it"


def test_fix_request_and_its_event_commit_together(pg, monkeypatch):
    """A failed second write must not leave an orphan fix row (a duplicate work item)."""
    pg.execute("INSERT INTO messages (message_id, subject) VALUES ('fx@t','Predmet')")
    mid = pg.execute("SELECT id FROM messages WHERE message_id='fx@t'").fetchone()[0]
    from app import httpapi

    def boom(*a, **kw):
        raise RuntimeError("event insert failed")

    monkeypatch.setattr(httpapi.db, "log_event", boom)
    cfg = Config(pg_dsn=PG_DSN, data_dir="/tmp", api_token="tok",
                 dash_password="secret", secret_key="test-secret")
    app = create_app(cfg)          # testing=False → Flask turns the error into a 500
    c = app.test_client()
    _login(c)
    r = c.post(f"/api/message/{mid}/fix", json={"problem_type": "other", "description": "x"})
    assert r.status_code == 500
    assert pg.execute("SELECT count(*) FROM fix_requests").fetchone()[0] == 0, \
        "the fix row must roll back with the failed event write"
