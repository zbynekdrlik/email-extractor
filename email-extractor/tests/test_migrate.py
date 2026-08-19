"""Schema-migration engine tests (#269).

Every test runs against a THROWAWAY database created from scratch, so the fresh-DB,
baseline-detection, partial-migration and concurrency paths are exercised in full
isolation (never against the shared session schema). DB-backed → needs PG_TEST_DSN;
fails loudly if it is missing, per the no-skip test policy.
"""
import logging
import os
import threading
from contextlib import contextmanager
from uuid import uuid4

import psycopg
import pytest
from psycopg import conninfo

from app import db, migrate

PG_DSN = os.environ.get("PG_TEST_DSN")


@contextmanager
def fresh_db():
    """Create a brand-new empty database, yield (connection, dsn), drop it afterwards."""
    if not PG_DSN:
        pytest.fail("PG_TEST_DSN not set — migration tests need a Postgres "
                    "(CI postgres service, or a local docker PG)")
    admin = psycopg.connect(PG_DSN, autocommit=True)
    name = f"ee_mig_{uuid4().hex[:12]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    try:
        child_dsn = conninfo.make_conninfo(
            **{**conninfo.conninfo_to_dict(PG_DSN), "dbname": name})
        conn = psycopg.connect(child_dsn, autocommit=True)
        try:
            yield conn, child_dsn
        finally:
            conn.close()
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def _regclass(conn, table):
    return conn.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone()[0]


# --- fresh empty DB: full schema created from zero, baseline recorded ---

def test_fresh_db_creates_full_schema_and_records_baseline():
    with fresh_db() as (conn, _dsn):
        assert _regclass(conn, "messages") is None          # genuinely empty to start
        done = db.init_schema(conn)
        # #314: every declared revision runs on a fresh DB (baseline + any later ones) —
        # revision-agnostic so this never needs touching again when a rev 3+ is added.
        assert done == [r.revision for r in db.REVISIONS]
        # ledger table + a spread of real tables (core, a late-added one, and rev-2's own
        # dl_nonwarehouse_supplier) all exist
        for t in ("schema_version", "messages", "attachments", "email_events",
                  "desadv_sent", "order_questions", "dl_supplier_overrides",
                  "dl_nonwarehouse_supplier"):
            assert _regclass(conn, t) is not None, f"{t} missing after fresh create"
        rows = conn.execute(
            "SELECT revision, name FROM schema_version ORDER BY revision").fetchall()
        assert rows == [(r.revision, r.name) for r in db.REVISIONS]
        # the rollup trigger (proof the baseline really ran, not just the table shells)
        assert conn.execute(
            "SELECT 1 FROM pg_trigger WHERE tgname='trg_email_events_rollup'").fetchone()


# --- existing DB with no ledger (live prod OR a lagging dev DB): the idempotent baseline
# is re-run once (non-destructive, self-healing), then recorded. This is the exact scenario
# that a "record without re-running" design got wrong: a DB missing a later-added column
# (#153 edi_sent.uploaded_at) must be HEALED, not skipped, exactly as the old init_schema did.

def test_existing_db_baseline_is_rerun_idempotently_and_heals_lag(caplog):
    with fresh_db() as (conn, _dsn):
        # simulate an existing DB migrated by the OLD idempotent path, then a lagging schema:
        for stmt in db.SCHEMA:
            conn.execute(stmt)
        conn.execute("INSERT INTO messages (message_id) VALUES ('pre-mech-row')")
        conn.execute("ALTER TABLE edi_sent DROP COLUMN uploaded_at")   # DB now lags baseline
        assert _regclass(conn, "schema_version") is None

        with caplog.at_level(logging.INFO, logger="email_extractor.migrate"):
            done = db.init_schema(conn)

        assert done == [r.revision for r in db.REVISIONS]   # #314: baseline + rev 2 both ran
        assert any("applied schema baseline r0001" in m for m in caplog.messages)
        # the lagging column was HEALED (idempotent ADD COLUMN re-ran), not skipped
        assert conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='edi_sent' AND column_name='uploaded_at'").fetchone()
        # baseline recorded, and live data left untouched (non-destructive)
        assert conn.execute(
            "SELECT revision, name FROM schema_version ORDER BY revision").fetchall() \
            == [(r.revision, r.name) for r in db.REVISIONS]
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


# --- up-to-date DB: O(1) fast path, nothing applied ---

def test_second_call_is_noop_fast_path(caplog):
    with fresh_db() as (conn, _dsn):
        db.init_schema(conn)                       # first: baseline
        with caplog.at_level(logging.INFO, logger="email_extractor.migrate"):
            done = db.init_schema(conn)            # second: up-to-date
        assert done == []
        _top = max(r.revision for r in db.REVISIONS)      # #314: 'up-to-date at r<max>'
        assert any(f"up-to-date at r{_top:04d}" in m for m in caplog.messages)
        assert conn.execute("SELECT count(*) FROM schema_version").fetchone()[0] \
            == len(db.REVISIONS)


# --- outdated DB: only the missing revisions run, in order ---

def test_partial_migration_applies_only_missing_in_order():
    with fresh_db() as (conn, _dsn):
        db.init_schema(conn)                       # every real revision applied
        # #314: use HIGH synthetic ids (90/91) so they never collide with a real appended
        # revision (id 2 is now taken by add_dl_nonwarehouse_supplier).
        rev90 = migrate.Revision(90, "test_add_a", ["CREATE TABLE mig_test_a (id INT)"])
        rev91 = migrate.Revision(91, "test_add_b", ["CREATE TABLE mig_test_b (id INT)"])
        done = migrate.run_migrations(conn, [db.REVISIONS[0], rev90, rev91])
        assert done == [90, 91]                     # rev 1 skipped, 90 then 91 in order
        assert _regclass(conn, "mig_test_a") is not None
        assert _regclass(conn, "mig_test_b") is not None
        assert conn.execute(
            "SELECT revision FROM schema_version ORDER BY revision").fetchall() \
            == [(r.revision,) for r in db.REVISIONS] + [(90,), (91,)]
        # applied_at ordering matches revision ordering (90 recorded before 91)
        assert migrate.run_migrations(conn, [db.REVISIONS[0], rev90, rev91]) == []


def test_revision_failure_rolls_back_and_records_nothing():
    with fresh_db() as (conn, _dsn):
        db.init_schema(conn)
        # #314: HIGH synthetic id (90) — id 2 is now a real revision.
        bad = migrate.Revision(90, "bad", [
            "CREATE TABLE mig_ok (id INT)",
            "CREATE TABLE mig_ok (id INT)",   # duplicate → error mid-revision
        ])
        with pytest.raises(psycopg.errors.DuplicateTable):
            migrate.run_migrations(conn, [db.REVISIONS[0], bad])
        # atomic: neither the table nor the failed revision's ledger row survived — only the
        # real revisions applied by init_schema remain.
        assert _regclass(conn, "mig_ok") is None
        assert conn.execute(
            "SELECT revision FROM schema_version ORDER BY revision").fetchall() \
            == [(r.revision,) for r in db.REVISIONS]
        # advisory lock was released in finally → a retry can proceed
        assert migrate.pending_revisions(conn, [db.REVISIONS[0]]) == []


def test_run_migrations_rejects_out_of_order_or_duplicate_revisions():
    """The immutable-migrations invariant (strictly ascending, unique ids) is CODE-enforced,
    not just documented — a mis-ordered/duplicate REVISIONS entry fails loudly BEFORE any DB
    work, never silently runs a revision out of sequence."""
    rev2 = migrate.Revision(2, "a", ["CREATE TABLE mig_x (id INT)"])
    rev3 = migrate.Revision(3, "b", ["CREATE TABLE mig_y (id INT)"])
    with fresh_db() as (conn, _dsn):
        for bad in ([db.REVISIONS[0], rev3, rev2],           # out of order
                    [db.REVISIONS[0], rev2, rev2],           # duplicate id
                    [db.REVISIONS[0], db.REVISIONS[0]]):     # duplicate baseline
            with pytest.raises(ValueError, match="ascending"):
                migrate.run_migrations(conn, bad)
        # a rejected call never touched the DB (no ledger table created)
        assert _regclass(conn, "schema_version") is None


# --- dry-run primitive ---

def test_pending_revisions_reports_without_applying():
    with fresh_db() as (conn, _dsn):
        # before any migration the ledger table is absent → everything pending
        assert _regclass(conn, "schema_version") is None
        assert migrate.pending_revisions(conn, db.REVISIONS) \
            == [r.revision for r in db.REVISIONS]

        db.init_schema(conn)                       # applies every real revision
        # #314: HIGH synthetic id (90) — id 2 is now a real, already-applied revision.
        rev90 = migrate.Revision(90, "future", ["CREATE TABLE mig_dry (id INT)"])
        assert migrate.pending_revisions(conn, [db.REVISIONS[0], rev90]) == [90]
        assert _regclass(conn, "mig_dry") is None  # dry-run applied nothing
        assert migrate.pending_revisions(conn, db.REVISIONS) == []


# --- concurrent starts (two containers/CLI at once) are serialized ---

def test_concurrent_start_no_duplicate_baseline():
    from _race import run_racers  # #291: never hand-roll thread start/join against real PG
    with fresh_db() as (conn, dsn):
        results: dict[int, list[int]] = {}
        errors: list[Exception] = []

        def worker(i):
            try:
                c = psycopg.connect(dsn, autocommit=True)
                try:
                    results[i] = migrate.run_migrations(c, db.REVISIONS)
                finally:
                    c.close()
            except Exception as e:      # pragma: no cover - only on a real race bug
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,), name=f"starter-{i}")
                   for i in range(4)]
        # `conn` (this fresh DB's own connection) is used only for run_racers' stall
        # cleanup; the racers each open their OWN connection to the same fresh DB.
        run_racers(conn, threads, timeout=15, label="schema_concurrent_start")

        assert not errors, errors
        # exactly one baseline row regardless of who won the race
        assert conn.execute(
            "SELECT count(*) FROM schema_version WHERE revision=1").fetchone()[0] == 1
        # exactly one worker applied it; the rest saw it already done (#314: the winner
        # applies every declared revision, not just the baseline)
        applied = sorted(len(v) for v in results.values())
        assert applied == [0, 0, 0, len(db.REVISIONS)]
        assert _regclass(conn, "messages") is not None   # schema really was built


# --- data survives BOTH the O(1) fast path AND a genuine idempotent baseline re-run ---

def test_data_untouched_across_reruns():
    with fresh_db() as (conn, _dsn):
        db.init_schema(conn)
        conn.execute("INSERT INTO messages (message_id) VALUES ('keep-me-269')")
        before = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        # up-to-date fast path (baseline already recorded → skipped entirely)
        assert db.init_schema(conn) == []
        assert db.init_schema(conn) == []
        # AND a GENUINE baseline re-run (clear the ledger first, like a pre-mechanism DB):
        # the ~100 idempotent statements run again against the live row and must not touch it
        conn.execute("DELETE FROM schema_version")
        assert db.init_schema(conn) == [r.revision for r in db.REVISIONS]  # #314
        after = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        assert before == after == 1
        assert conn.execute(
            "SELECT message_id FROM messages").fetchone()[0] == "keep-me-269"


# --- #331: the dead legacy `processed` table is dropped by a versioned revision ---
# The n8n-era `processed` table had 0 rows over its whole history and zero consumers (no
# app/test read or write, absent from conftest's TRUNCATE list, no live n8n workflow touches
# it). Revision "drop_processed_table" removes it. The baseline no longer CREATEs it either,
# so the drop is a no-op on a fresh DB (IF EXISTS) but genuinely removes it on a pre-drop
# prod DB that still carries it.

def test_drop_processed_removes_the_dead_legacy_table():
    drop_rev = next(r.revision for r in db.REVISIONS if r.name == "drop_processed_table")
    with fresh_db() as (conn, _dsn):
        db.init_schema(conn)                            # full schema incl. the drop revision
        assert _regclass(conn, "processed") is None     # dead table absent after full migrate

        # simulate a pre-drop prod DB: the table exists and the ledger stops BEFORE the drop
        conn.execute(
            "CREATE TABLE processed (id BIGSERIAL PRIMARY KEY, message_id TEXT NOT NULL, "
            "handled_by TEXT, category TEXT, result TEXT, processed_at TIMESTAMPTZ DEFAULT now())")
        # roll back ONLY the drop revision's ledger row (scoped by = drop_rev, not >=, so
        # this stays correct when a later rev N is appended — that row is left untouched).
        conn.execute("DELETE FROM schema_version WHERE revision = %s", (drop_rev,))
        assert _regclass(conn, "processed") is not None

        done = db.init_schema(conn)                     # only the pending drop revision re-runs
        assert drop_rev in done
        assert _regclass(conn, "processed") is None     # and it removed the dead table

        # idempotent no-op: DROP TABLE IF EXISTS — re-migrating with the table already gone
        assert db.init_schema(conn) == []
        assert _regclass(conn, "processed") is None


# --- #352: the orphaned `order_digest_sent` table is dropped by a versioned revision ---
# The daily order digest was removed in #347 (v0.9.112); its send-dedup claim table
# `order_digest_sent` is now a pure orphan (zero app readers/writers). Revision
# "drop_order_digest_sent" removes it. The baseline no longer CREATEs it either, so the
# drop is a no-op on a fresh DB (IF EXISTS) but genuinely removes it on a pre-drop prod DB
# that still carries it. Same self-healing-drop shape as #331's `drop_processed_table`.

def test_drop_order_digest_sent_removes_the_orphaned_table():
    drop_rev = next(r.revision for r in db.REVISIONS if r.name == "drop_order_digest_sent")
    with fresh_db() as (conn, _dsn):
        db.init_schema(conn)                                    # full schema incl. the drop revision
        assert _regclass(conn, "order_digest_sent") is None     # orphan absent after full migrate

        # simulate a pre-drop prod DB: the table exists and the ledger stops BEFORE the drop
        conn.execute(
            "CREATE TABLE order_digest_sent (day DATE PRIMARY KEY, "
            "sent_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        # roll back ONLY the drop revision's ledger row (scoped by = drop_rev, not >=, so
        # this stays correct when a later rev N is appended — that row is left untouched).
        conn.execute("DELETE FROM schema_version WHERE revision = %s", (drop_rev,))
        assert _regclass(conn, "order_digest_sent") is not None

        done = db.init_schema(conn)                             # only the pending drop revision re-runs
        assert drop_rev in done
        assert _regclass(conn, "order_digest_sent") is None     # and it removed the orphan

        # idempotent no-op: DROP TABLE IF EXISTS — re-migrating with the table already gone
        assert db.init_schema(conn) == []
        assert _regclass(conn, "order_digest_sent") is None
