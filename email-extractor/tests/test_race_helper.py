"""Proves `tests/_race.py::run_racers` genuinely detects and recovers from a stalled
racer thread, instead of silently letting it wedge every later test (#291).

Before this helper existed, every "two real threads racing real Postgres connections"
test in this suite did a hand-rolled `t.join(timeout=15)` with NO stall detection and
NO cleanup — a thread that genuinely stalled (extreme scheduler contention, or a real
deadlock) mid-transaction left its connection open holding a lock, which then blocked
every subsequent test's session-scoped schema TRUNCATE indefinitely. See the #291
issue comments for a from-scratch deterministic repro of that exact wedge mechanism.
"""
import logging
import os
import threading
import time

import psycopg
import pytest
from _race import run_racers

PG_DSN = os.environ.get("PG_TEST_DSN")
log = logging.getLogger(__name__)


def test_run_racers_detects_a_stalled_thread_kills_its_backend_and_fails_loudly(pg):
    """A racer that takes a row lock and then genuinely stalls (still running past its
    join window) must not be allowed to silently wedge later tests. `run_racers` must
    (a) fail THIS test loudly instead of passing silently, and (b) actually release
    the lock by terminating the stray backend — proven here by a second connection
    successfully TRUNCATE-ing the very table the stalled racer locked, right after."""
    try:
        pg.execute("CREATE TABLE IF NOT EXISTS race_helper_demo (id int primary key)")
        pg.execute("DELETE FROM race_helper_demo")
        pg.execute("INSERT INTO race_helper_demo VALUES (1)")

        def stall():
            conn = psycopg.connect(PG_DSN)
            conn.execute("SELECT * FROM race_helper_demo WHERE id = 1 FOR UPDATE")
            # Simulate a thread that never gets scheduled back in time to commit —
            # long enough to still be alive when run_racers checks (its own join
            # timeout below is far shorter), short enough this thread naturally
            # finishes on its own rather than dangling for the rest of the suite.
            time.sleep(2)
            try:
                conn.commit()
            except Exception as e:
                # Expected: run_racers already terminated this backend by the time we
                # get here, so the commit fails against a dead connection.
                log.info("stall-race commit failed as expected after backend "
                         "termination: %r", e)
            finally:
                try:
                    conn.close()
                except Exception as e:
                    log.info("stall-race close failed as expected after backend "
                             "termination: %r", e)

        t = threading.Thread(target=stall, name="stall-race")
        with pytest.raises(pytest.fail.Exception, match="did not finish within"):
            run_racers(pg, [t], timeout=0.3, label="demo")

        # The lock must actually be gone — proven the same way a later test's own
        # session-scoped TRUNCATE would need it to be, bounded here so this proof
        # itself cannot hang if the fix regresses.
        victim = psycopg.connect(PG_DSN, autocommit=True)
        try:
            victim.execute("SET statement_timeout = '3000'")
            victim.execute("TRUNCATE race_helper_demo")
        finally:
            victim.close()
    finally:
        # #291 review: bound this cleanup too, on its OWN throwaway connection — never
        # the shared session-scoped `pg`, since a `SET statement_timeout` on `pg` would
        # leak into every LATER test in this session. If `pg_terminate_backend` were
        # ever slow to actually release the lock, an unbounded DROP here (in the one
        # test whose entire purpose is proving hangs get caught) could itself hang.
        cleanup = psycopg.connect(PG_DSN, autocommit=True)
        try:
            cleanup.execute("SET statement_timeout = '3000'")
            cleanup.execute("DROP TABLE IF EXISTS race_helper_demo")
        finally:
            cleanup.close()


def test_run_racers_passes_through_cleanly_when_every_thread_finishes_in_time(pg):
    """The happy path (every racer finishes well inside its join window) must behave
    exactly like the hand-rolled start/join loop it replaces — no false-positive
    failure, no unnecessary backend termination."""
    results: list[str] = []

    def quick(key):
        results.append(key)

    t1 = threading.Thread(target=quick, args=("a",), name="quick-a")
    t2 = threading.Thread(target=quick, args=("b",), name="quick-b")
    run_racers(pg, [t1, t2], timeout=5, label="quick")

    assert sorted(results) == ["a", "b"]
