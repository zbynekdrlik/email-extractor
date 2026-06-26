"""Schema migration + rollup-trigger tests (real Postgres via the pg fixture)."""
from app import db


def test_schema_objects_exist(pg):
    for t in ("email_events", "fix_requests"):
        assert pg.execute("SELECT to_regclass(%s)", (t,)).fetchone()[0] is not None
    cols = {r[0] for r in pg.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name='messages'"
    ).fetchall()}
    for col in ("proc_status", "proc_stage", "proc_outcome", "last_event_at", "attempts",
                "edi_file", "orion_path", "odoo_url", "forwarded_to"):
        assert col in cols
    assert pg.execute(
        "SELECT 1 FROM pg_trigger WHERE tgname='trg_email_events_rollup'").fetchone()


def test_rollup_updates_messages(pg):
    pg.execute("INSERT INTO messages (message_id) VALUES ('m1')")
    db.log_event(pg, "m1", "ai_orders", "uploaded_orion", "ok", outcome="EDI nahraté",
                 detail={"edi_file": "ORDER_1.txt", "orion_path": "C:/in/ORDER_1.txt"})
    row = pg.execute(
        "SELECT proc_stage, proc_status, proc_outcome, edi_file, orion_path "
        "FROM messages WHERE message_id='m1'").fetchone()
    assert row == ("uploaded_orion", "ok", "EDI nahraté", "ORDER_1.txt", "C:/in/ORDER_1.txt")


def test_rollup_latest_wins_and_claimed_counts_attempts(pg):
    pg.execute("INSERT INTO messages (message_id) VALUES ('m2')")
    db.log_event(pg, "m2", "disp", "claimed", "ok")
    db.log_event(pg, "m2", "ai_orders", "review", "review", outcome="prázdny obsah")
    db.log_event(pg, "m2", "disp", "claimed", "ok")
    stage, status, attempts = pg.execute(
        "SELECT proc_stage, proc_status, attempts FROM messages WHERE message_id='m2'").fetchone()
    assert stage == "claimed"   # latest event wins
    assert status == "ok"
    assert attempts == 2        # one per 'claimed'


def test_rollup_noop_when_message_absent(pg):
    # No messages row -> the UPDATE matches zero rows and must not raise.
    db.log_event(pg, "ghost", "x", "error", "error", outcome="nikde")
    assert pg.execute(
        "SELECT count(*) FROM email_events WHERE message_id='ghost'").fetchone()[0] == 1


def test_init_schema_idempotent(pg):
    db.init_schema(pg)   # second run must not raise nor duplicate the trigger
    n = pg.execute(
        "SELECT count(*) FROM pg_trigger WHERE tgname='trg_email_events_rollup'").fetchone()[0]
    assert n == 1


def test_classified_trigger_logs_on_category_change(pg):
    pg.execute("INSERT INTO messages (message_id) VALUES ('cls')")
    pg.execute("UPDATE messages SET category='ai_orders' WHERE message_id='cls'")
    ev = pg.execute("SELECT workflow, stage, status, rollup FROM email_events "
                    "WHERE message_id='cls' AND stage='classified' ORDER BY id DESC LIMIT 1").fetchone()
    assert ev == ("sorter", "classified", "ok", False)
    # no duplicate event when category is set to the same value
    pg.execute("UPDATE messages SET category='ai_orders' WHERE message_id='cls'")
    assert pg.execute("SELECT count(*) FROM email_events WHERE message_id='cls' "
                      "AND stage='classified'").fetchone()[0] == 1
    # rollup=false -> proc_status stays NULL ('nové') after classification
    assert pg.execute("SELECT proc_status FROM messages WHERE message_id='cls'").fetchone()[0] is None


def test_insert_message_logs_ingested_event(pg):
    rec = {
        "identity": "<m-ing@x>",
        "headers": {"message_id": "<m-ing@x>", "from_addr": "a@x.sk", "from_name": "A",
                    "to_addrs": [], "cc_addrs": [], "subject": "Obj", "date": "2026-06-26"},
        "body_text": "telo", "body_source": "plain", "combined_text": "telo",
        "has_attachments": False, "needs_vision": False, "attachments": [],
    }
    assert db.insert_message(pg, rec, "INBOX", 1, 1, "/x/raw.eml", []) is True
    ev = pg.execute("SELECT workflow, stage, status, rollup FROM email_events "
                    "WHERE message_id=%s", ("<m-ing@x>",)).fetchone()
    assert ev == ("extractor", "ingested", "ok", False)
    # rollup=False -> the ingest event does not set proc_status (stays 'nové')
    assert pg.execute("SELECT proc_status FROM messages WHERE message_id=%s",
                      ("<m-ing@x>",)).fetchone()[0] is None


def test_non_rollup_event_is_timeline_only(pg):
    pg.execute("INSERT INTO messages (message_id, proc_status, proc_stage, proc_outcome) "
               "VALUES ('nr','ok','uploaded_orion','EDI')")
    db.log_event(pg, "nr", "dashboard", "fix_requested", "review",
                 outcome="na opravu", rollup=False)
    row = pg.execute("SELECT proc_status, proc_stage, proc_outcome "
                     "FROM messages WHERE message_id='nr'").fetchone()
    assert row == ("ok", "uploaded_orion", "EDI")   # unchanged by the non-rollup event
    assert pg.execute("SELECT count(*) FROM email_events "
                      "WHERE message_id='nr'").fetchone()[0] == 1   # but recorded in the timeline
