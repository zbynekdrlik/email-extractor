"""Numbered-revision schema migrator (#269).

Replaces the old `for stmt in SCHEMA: conn.execute(stmt)` — which re-ran ~100 idempotent
DDL statements on EVERY start (add-on boot AND every one-shot CLI tool) — with a versioned
runner backed by a `schema_version` ledger table:

  * an UP-TO-DATE DB (ledger already records the latest revision) runs an O(1) version
    check and applies nothing;
  * an OUTDATED DB applies only the missing revisions, in order;
  * a FRESH empty DB (CI) gets the full baseline schema created from zero;
  * an EXISTING DB with no ledger (a live production DB migrated by the old idempotent
    path, OR a dev DB that lags) has the idempotent baseline run ONCE more — exactly what
    the old code did on every boot, so nothing destructive happens and any lagging schema
    self-heals — and the baseline is then recorded so every later boot is O(1).

Standalone engine on purpose: it does NOT import `db`, so there is no import cycle.
`db.py` owns the SCHEMA data and builds the `REVISIONS` list from it, then calls
`run_migrations()` from its `init_schema()` wrapper (every existing caller is unchanged).

Immutable-migrations rule (like Alembic / yoyo / Rails / Django): the baseline revision
is FROZEN. A future schema change is a NEW numbered `Revision` appended to `db.REVISIONS`
— NEVER an edit to an already-shipped revision's statements. That is what removes the #269
risk (a non-idempotent statement silently re-run against production on every boot): from
now on a change runs ONCE, atomically, recorded — and a new revision need not even be
idempotent, because the ledger guarantees exactly-once.

Baseline vs new-revision execution (deliberate asymmetry, see run_migrations):
  * the BASELINE runs its statements per-statement (autocommit), the SAME proven,
    idempotent, self-healing way production has run them on every boot — the first boot of
    this mechanism changes NOTHING about how startup behaves, it only records afterwards;
  * a NEW revision runs its statements + the ledger stamp inside ONE transaction, so it
    applies exactly once and a crash mid-revision rolls back cleanly.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import NamedTuple

log = logging.getLogger("email_extractor.migrate")

BASELINE_REVISION = 1

# The ledger is META — managed by this runner directly, never part of any revision's own
# statements. Idempotent, so creating it on an already-migrated live DB is a safe no-op.
SCHEMA_VERSION_DDL = """
    CREATE TABLE IF NOT EXISTS schema_version (
        revision   INT PRIMARY KEY,
        name       TEXT NOT NULL,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
"""

# Session-level advisory lock serialising concurrent starters (add-on boot + a one-shot
# CLI tool running at the same instant). hashtext() returns int4; Postgres widens it to
# the pg_advisory_lock(bigint) overload — the SAME convention as the DO-block xact locks
# already in db.py. A session lock (not xact) is required because the runner spans several
# independent per-revision transactions; it is released in `finally`, and if the process
# dies mid-migration Postgres releases it automatically when the session ends.
_LOCK_SQL = "SELECT pg_advisory_lock(hashtext('email-extractor:schema_migrations'))"
_UNLOCK_SQL = "SELECT pg_advisory_unlock(hashtext('email-extractor:schema_migrations'))"


class Revision(NamedTuple):
    revision: int
    name: str
    statements: Sequence[str]


def _applied_revisions(conn) -> set[int]:
    return {r[0] for r in conn.execute("SELECT revision FROM schema_version").fetchall()}


def _record(conn, rev: Revision) -> None:
    # ON CONFLICT DO NOTHING is belt-and-suspenders next to the advisory lock: even a
    # concurrent starter that somehow raced past the lock can never double-insert a row.
    conn.execute(
        "INSERT INTO schema_version (revision, name) VALUES (%s, %s) "
        "ON CONFLICT (revision) DO NOTHING",
        (rev.revision, rev.name),
    )


def pending_revisions(conn, revisions: Sequence[Revision]) -> list[int]:
    """Dry-run primitive: the revision ids that WOULD be applied, in order, WITHOUT
    applying them. If the ledger table does not exist yet, every revision is pending."""
    try:
        applied = _applied_revisions(conn)
    except Exception:
        applied = set()
    return [rev.revision for rev in revisions if rev.revision not in applied]


def run_migrations(conn, revisions: Sequence[Revision]) -> list[int]:
    """Apply every not-yet-applied revision in order. Returns the ids applied this run.

    Safe on: a fresh empty DB (creates everything), a live/existing DB (re-runs the
    idempotent baseline once — non-destructive, self-healing — then records it), and an
    up-to-date DB (does nothing, O(1)).
    """
    conn.execute(_LOCK_SQL)
    try:
        conn.execute(SCHEMA_VERSION_DDL)
        applied = _applied_revisions(conn)
        done: list[int] = []
        for rev in revisions:
            if rev.revision in applied:
                continue
            if rev.revision == BASELINE_REVISION:
                # Frozen legacy schema: every statement is idempotent (IF NOT EXISTS /
                # guarded) — the exact set production has run per-statement on every boot
                # for months. Run it the SAME proven way (autocommit, per statement) so the
                # first boot of this mechanism changes nothing about how startup behaves; it
                # just records the baseline afterwards. Self-healing: a fresh DB gets the
                # whole schema, a lagging existing DB gets its missing tables/columns added
                # (never skipped), a fully-current DB runs ~100 harmless no-ops once.
                for stmt in rev.statements:
                    conn.execute(stmt)
                _record(conn, rev)
                log.info(
                    "applied schema baseline r%04d '%s' (%d idempotent statement(s))",
                    rev.revision, rev.name, len(rev.statements),
                )
            else:
                # A genuinely new revision: statements + the ledger stamp commit ATOMICALLY,
                # so it applies exactly once and a crash mid-revision rolls back cleanly (its
                # statements need NOT be idempotent — that is the point of the ledger).
                with conn.transaction():
                    for stmt in rev.statements:
                        conn.execute(stmt)
                    _record(conn, rev)
                log.info(
                    "applied schema revision r%04d '%s' (%d statement(s))",
                    rev.revision, rev.name, len(rev.statements),
                )
            done.append(rev.revision)
        if not done:
            current = max(applied) if applied else 0
            log.info("schema up-to-date at r%04d (%d revision(s) recorded)",
                     current, len(applied))
        return done
    finally:
        conn.execute(_UNLOCK_SQL)
