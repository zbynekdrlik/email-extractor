"""Schema migration + rollup-trigger tests (real Postgres via the pg fixture)."""
import psycopg
import pytest

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


# --- #273: attachments.message_id is a foreign key (ON DELETE CASCADE) with no index —
# Postgres never auto-indexes the referencing side of a FK. The column is queried directly
# in httpapi_dashboard_data.py (twice) and dl_worker.py, and every cascading delete from
# messages scans attachments sequentially without one.

def test_attachments_message_id_is_indexed(pg):
    assert pg.execute(
        "SELECT 1 FROM pg_indexes WHERE tablename='attachments' "
        "AND indexname='idx_attachments_message'").fetchone()


# --- #203 F4: desadv_sent import-confirmation columns + incident source split ---

def test_desadv_sent_has_import_confirmation_columns(pg):
    cols = {r[0] for r in pg.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='desadv_sent'").fetchall()}
    for col in ("import_status", "import_confirmed_at", "import_checked_at"):
        assert col in cols


def test_desadv_sent_import_migration_backfills_pre_existing_confirmed_rows(pg):
    """Same backfill contract as edi_sent's own #151 migration: any row already
    `uploaded_at IS NOT NULL` when the migration first runs is treated as imported."""
    pg.execute("ALTER TABLE desadv_sent DROP COLUMN IF EXISTS import_status")
    pg.execute("ALTER TABLE desadv_sent DROP COLUMN IF EXISTS import_confirmed_at")
    pg.execute("ALTER TABLE desadv_sent DROP COLUMN IF EXISTS import_checked_at")
    pg.execute(
        "INSERT INTO desadv_sent (supplier_ean, doc_number, filename, uploaded_at) "
        "VALUES ('123', 'D1', 'DESADV_x.txt', now())")
    db.init_schema(pg)
    row = pg.execute(
        "SELECT import_status FROM desadv_sent WHERE doc_number = 'D1'").fetchone()
    assert row[0] == "imported"


# --- #248: the CREATE UNIQUE INDEX migration must tolerate rows already duplicated at
# the moment it first runs — never crash boot (see the ticket's own "must not crash on
# boot after deploy" requirement + the design comment on #248 for the live-DB check
# that found zero duplicates in production today; this proves the migration is safe
# EVEN IF that ever stops being true). ---

def test_customer_overrides_new_ean_index_migration_tolerates_pre_existing_duplicates(pg):
    """Simulates a box that has NOT yet run the #248 migration and already has two
    ACTIVE hand-added customer rows sharing one EAN (bypassing `upsert_customer`
    entirely, the way a pre-#248 row could have been created). `init_schema` must not
    raise, must keep exactly the most-recently-updated row active, and must leave the
    older duplicate retired (never deleted) with the unique index now in place."""
    pg.execute("DROP INDEX IF EXISTS idx_customer_overrides_new_ean")
    pg.execute(
        """INSERT INTO customer_overrides
               (orig_ean_edi, orig_street, ean_edi, name, emails, city, street, zip,
                retired, updated_at)
           VALUES (NULL, NULL, '6000000000001', 'Starý duplikát', ARRAY['a@x.sk'],
                   'Košice', 'Stará 1', '04001', false, now() - interval '1 day')"""
    )
    pg.execute(
        """INSERT INTO customer_overrides
               (orig_ean_edi, orig_street, ean_edi, name, emails, city, street, zip,
                retired, updated_at)
           VALUES (NULL, NULL, '6000000000001', 'Nový duplikát', ARRAY['b@x.sk'],
                   'Prešov', 'Nová 2', '08001', false, now())"""
    )

    db.init_schema(pg)   # must not raise — the whole point of #248's guarded de-dup step

    # Query by NAME, not by `updated_at` order — the migration's own retiring UPDATE
    # bumps `updated_at` on the row it retires (same convention as `retire_customer`),
    # so `updated_at DESC` would no longer distinguish winner from loser afterward.
    rows = {name: retired for name, retired in pg.execute(
        "SELECT name, retired FROM customer_overrides "
        "WHERE ean_edi='6000000000001'").fetchall()}
    assert rows == {"Nový duplikát": False, "Starý duplikát": True}, (
        "the freshest row must stay active and survive; the older duplicate must be "
        "retired, never deleted")
    active = pg.execute(
        "SELECT count(*) FROM customer_overrides "
        "WHERE ean_edi='6000000000001' AND NOT retired").fetchone()
    assert active == (1,)
    assert pg.execute(
        "SELECT 1 FROM pg_indexes WHERE indexname='idx_customer_overrides_new_ean'"
    ).fetchone(), "the unique index must exist after the migration runs"


def test_dl_supplier_overrides_new_ean_index_migration_tolerates_pre_existing_duplicates(pg):
    """Mirror of the customer test above for `dl_supplier_overrides`."""
    pg.execute("DROP INDEX IF EXISTS idx_dl_supplier_overrides_new_ean")
    pg.execute(
        """INSERT INTO dl_supplier_overrides
               (orig_ean_edi, orig_city, ean_edi, name, emails, city, retired, updated_at)
           VALUES (NULL, NULL, '6000000000002', 'Starý dodávateľ', ARRAY['a@dl.sk'],
                   'Košice', false, now() - interval '1 day')"""
    )
    pg.execute(
        """INSERT INTO dl_supplier_overrides
               (orig_ean_edi, orig_city, ean_edi, name, emails, city, retired, updated_at)
           VALUES (NULL, NULL, '6000000000002', 'Nový dodávateľ', ARRAY['b@dl.sk'],
                   'Prešov', false, now())"""
    )

    db.init_schema(pg)

    rows = {name: retired for name, retired in pg.execute(
        "SELECT name, retired FROM dl_supplier_overrides "
        "WHERE ean_edi='6000000000002'").fetchall()}
    assert rows == {"Nový dodávateľ": False, "Starý dodávateľ": True}
    active = pg.execute(
        "SELECT count(*) FROM dl_supplier_overrides "
        "WHERE ean_edi='6000000000002' AND NOT retired").fetchone()
    assert active == (1,)


def test_customer_overrides_new_ean_index_ignores_blank_ean_duplicates(pg):
    """#248 review finding: Postgres treats '' as an ORDINARY, EQUAL value for a unique
    index (unlike NULL, which two rows can always share) — so two ACTIVE hand-added
    rows that both happen to carry a blank ean_edi must NOT collide on the new index,
    or `init_schema()` crashes boot on data the de-dup step (which explicitly excludes
    blank EAN, `AND ean_edi <> ''`) never touches."""
    pg.execute("DROP INDEX IF EXISTS idx_customer_overrides_new_ean")
    for label in ("A", "B"):
        pg.execute(
            """INSERT INTO customer_overrides
                   (orig_ean_edi, orig_street, ean_edi, name, emails, city, street, zip,
                    retired, updated_at)
               VALUES (NULL, NULL, '', %s, ARRAY['blank@x.sk'], 'Košice', %s, '',
                       false, now())""",
            (f"Bez EAN {label}", f"Ulica {label}"))

    db.init_schema(pg)   # must not raise

    active = pg.execute(
        "SELECT count(*) FROM customer_overrides "
        "WHERE ean_edi = '' AND orig_ean_edi IS NULL AND NOT retired").fetchone()
    assert active == (2,), (
        "two active rows sharing a BLANK ean_edi must both survive untouched — the "
        "unique index must not treat '' as a real duplicate")


def test_dl_supplier_overrides_new_ean_index_ignores_blank_ean_duplicates(pg):
    """Mirror of the customer test above for dl_supplier_overrides."""
    pg.execute("DROP INDEX IF EXISTS idx_dl_supplier_overrides_new_ean")
    for label in ("A", "B"):
        pg.execute(
            """INSERT INTO dl_supplier_overrides
                   (orig_ean_edi, orig_city, ean_edi, name, emails, city, retired,
                    updated_at)
               VALUES (NULL, NULL, '', %s, ARRAY['blank@dl.sk'], %s, false, now())""",
            (f"Bez EAN {label}", f"Mesto {label}"))

    db.init_schema(pg)   # must not raise

    active = pg.execute(
        "SELECT count(*) FROM dl_supplier_overrides "
        "WHERE ean_edi = '' AND orig_ean_edi IS NULL AND NOT retired").fetchone()
    assert active == (2,)
    assert pg.execute(
        "SELECT 1 FROM pg_indexes WHERE indexname='idx_dl_supplier_overrides_new_ean'"
    ).fetchone(), "the unique index must exist after the migration runs"


def test_import_alert_incidents_has_a_source_column_defaulting_to_edi(pg):
    pg.execute(
        "INSERT INTO import_alert_incidents (channel_id, kind) VALUES (1, 'failed')")
    row = pg.execute(
        "SELECT source FROM import_alert_incidents WHERE channel_id = 1").fetchone()
    assert row[0] == "edi"


def test_import_alert_incidents_source_is_check_constrained(pg):
    """Same DB-enforced-invariant philosophy the sibling `kind` column already has
    (#184) — a stray/mistyped source value must fail loudly, not silently resolve to
    the wrong ledger wherever confirm.py reads it back (review finding on #203)."""
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute(
            "INSERT INTO import_alert_incidents (channel_id, kind, source) "
            "VALUES (1, 'failed', 'bogus')")


def test_at_most_one_open_incident_per_channel_kind_source(pg):
    pg.execute(
        "INSERT INTO import_alert_incidents (channel_id, kind, source) "
        "VALUES (1, 'failed', 'edi')")
    # Same (channel, kind) but a DIFFERENT source is allowed to open independently.
    pg.execute(
        "INSERT INTO import_alert_incidents (channel_id, kind, source) "
        "VALUES (1, 'failed', 'desadv')")
    with pytest.raises(psycopg.errors.UniqueViolation):
        pg.execute(
            "INSERT INTO import_alert_incidents (channel_id, kind, source) "
            "VALUES (1, 'failed', 'edi')")


def test_import_alert_incident_desadv_members_references_desadv_sent(pg):
    assert pg.execute(
        "SELECT to_regclass('import_alert_incident_desadv_members')").fetchone()[0]
    incident_id = pg.execute(
        "INSERT INTO import_alert_incidents (channel_id, kind, source) "
        "VALUES (2, 'unknown', 'desadv') RETURNING id").fetchone()[0]
    row_id = pg.execute(
        "INSERT INTO desadv_sent (supplier_ean, doc_number, filename) "
        "VALUES ('456', 'D2', 'DESADV_y.txt') RETURNING id").fetchone()[0]
    pg.execute(
        "INSERT INTO import_alert_incident_desadv_members (incident_id, desadv_sent_id) "
        "VALUES (%s, %s)", (incident_id, row_id))
    # A repeat insert is a harmless no-op dedup via ON CONFLICT at the call-site level
    # (mirrors import_alert_incident_members's own PK-as-dedup contract) — here we just
    # confirm the PK actually rejects a genuine duplicate, proving the dedup mechanism
    # exists to be relied on.
    with pytest.raises(psycopg.errors.UniqueViolation):
        pg.execute(
            "INSERT INTO import_alert_incident_desadv_members "
            "(incident_id, desadv_sent_id) VALUES (%s, %s)", (incident_id, row_id))


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


def test_schema_seeds_the_known_match_incidents(pg):
    """#196: match_incidents is append-only and self-seeding (idempotent, ON CONFLICT DO
    NOTHING) — 'days since incident' must never depend on a separate manual step a
    future deploy could forget. #289 added a third seeded row (2026-08-13)."""
    db.init_schema(pg)   # the pg fixture already truncated it — reseed, then check
    rows = {r[0] for r in pg.execute("SELECT issue_ref FROM match_incidents").fetchall()}
    assert rows == {"#157", "#186", "#289"}
    db.init_schema(pg)   # idempotent: re-running must not duplicate or error (UNIQUE)
    n = pg.execute("SELECT count(*) FROM match_incidents").fetchone()[0]
    assert n == 3


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


def test_insert_message_strips_nul_bytes(pg):
    """Weak scans' PDF text layers can contain NUL (0x00) bytes; Postgres text
    columns reject them. Incident 2026-07-15: a scanned DL (uid 18236) failed to
    ingest on every cycle with DataError — the mail never entered the system."""
    rec = {
        "identity": "<m-nul@x>",
        "headers": {"message_id": "<m-nul@x>", "from_addr": "tlaciaren@slovnormal.sk",
                    "from_name": "Sc\x00an", "to_addrs": ["a@x.sk"], "cc_addrs": [],
                    "subject": "Scan\x00", "date": "2026-07-15"},
        "body_text": "telo\x00 skenu", "body_source": "plain",
        "combined_text": "Subject: Scan\x00\n\ntelo\x00 skenu",
        "has_attachments": True, "needs_vision": False,
        "attachments": [{"filename": "sken\x00.pdf", "mime": "application/pdf",
                         "size": 10, "method": "pdf-text", "ocr_conf": None,
                         "pages": 1, "chars": 5, "needs_vision": False,
                         "flag": None, "text": "DL \x00text so NUL"}],
    }
    assert db.insert_message(pg, rec, "INBOX", 18236, 1, "/x/raw.eml",
                             [{"idx": 0, "sha256": "s", "path": "/p", "url": "u"}]) is True
    subj, comb = pg.execute(
        "SELECT subject, combined_text FROM messages WHERE message_id=%s",
        ("<m-nul@x>",)).fetchone()
    assert "\x00" not in subj and subj == "Scan"
    assert "\x00" not in comb
    fname, text = pg.execute(
        "SELECT filename, extracted_text FROM attachments WHERE message_id=%s",
        ("<m-nul@x>",)).fetchone()
    assert "\x00" not in fname and "\x00" not in text


def test_migration_strips_tokens_from_stored_file_urls(pg):
    """#22: 2685 live rows had ?token=<secret> persisted; a DB dump leaked the token
    and rotating it broke every historical URL."""
    pg.execute("INSERT INTO messages (message_id) VALUES ('tok@t')")
    pg.execute("""INSERT INTO attachments (message_id, idx, file_url) VALUES
                  ('tok@t', 0, 'http://email-extractor:8099/files/tok_t/0?token=SECRET123'),
                  ('tok@t', 1, 'http://email-extractor:8099/files/tok_t/1')""")
    db.init_schema(pg)          # migrations are idempotent and run on every start
    urls = [r[0] for r in pg.execute(
        "SELECT file_url FROM attachments WHERE message_id='tok@t' ORDER BY idx").fetchall()]
    assert urls == ["http://email-extractor:8099/files/tok_t/0",
                    "http://email-extractor:8099/files/tok_t/1"]
    assert not any("SECRET123" in u for u in urls)
