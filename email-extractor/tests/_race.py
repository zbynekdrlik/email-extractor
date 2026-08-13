"""Shared helper for the "two real threads racing real Postgres connections" tests
scattered across this suite (#291).

Every one of those tests followed the same idiom: start N non-daemon threads, each on
its own `psycopg` connection, then `t.join(timeout=15)`. A `Thread.join(timeout=...)`
in Python never KILLS the thread — it only stops the CALLER from waiting further. Under
genuine scheduler starvation (several sibling pytest runs contending for the same CPU
cores, observed live during an integration round) a racer thread can stall permanently
mid-transaction, deep inside whatever code it is racing, holding a Postgres row lock
(or an advisory lock) open indefinitely. The test function itself returns once its own
join timeout elapses, but the orphaned thread's connection is still a live backend, and
every LATER test's session-scoped `pg` fixture does an unconditional `TRUNCATE ...
RESTART IDENTITY CASCADE` — which needs an ACCESS EXCLUSIVE lock — so it blocks on that
stray lock INDEFINITELY, wedging the entire rest of the suite (27+ minutes observed
live before someone noticed and manually ran `pg_terminate_backend`).

`run_racers()` replaces the hand-rolled start/join loop: it (1) marks every racer
thread daemon so a hung one can never block the pytest PROCESS itself from eventually
exiting, (2) joins each with the same bounded timeout every caller already used, and
(3) the moment ANY thread is still alive past its join, treats this as a genuine stall
— never a logic outcome to reason about — terminates every stray backend left open on
the test database (releasing whatever lock it holds, so later tests are never wedged)
and fails the CURRENT test loudly, since its own result is no longer trustworthy.

This is exactly `.claude/rules/local-testing.md`'s own documented manual recovery for a
wedged run ("`SELECT pg_terminate_backend(<pid>)` on the stuck backend, then re-run"),
automated as a self-healing safety net instead of requiring a human to notice a
multi-minute hang and intervene by hand.
"""
from __future__ import annotations

import threading

import pytest


def run_racers(pg, threads: list[threading.Thread], timeout: float = 15,
               label: str = "") -> None:
    """Start every (not-yet-started) `threading.Thread` in `threads`, join each with
    `timeout`, and fail loudly — after cleaning up any stray backend — if any of them
    is still alive once its join returns.

    `pg` is this test's own DB connection (the `pg`/`_schema` fixture) — used ONLY for
    the stall-cleanup query, never touched by the racers themselves (each racer must
    open its OWN connection; a single psycopg connection is not safe for concurrent use
    across threads).
    """
    for t in threads:
        t.daemon = True
        t.start()
    for t in threads:
        t.join(timeout=timeout)

    stuck = [t.name for t in threads if t.is_alive()]
    if stuck:
        killed = _kill_stray_backends(pg)
        prefix = f"race {label!r}: " if label else "race: "
        pytest.fail(
            f"{prefix}thread(s) {stuck} did not finish within {timeout}s — a genuine "
            "scheduler stall (or a real deadlock), not a logic outcome. Terminated "
            f"{killed} stray Postgres backend(s) on the test database so later tests "
            "are not wedged (#291) — this test's own result is unreliable and must be "
            "treated as a failure, not silently trusted.")


def _kill_stray_backends(pg) -> int:
    """Terminate every OTHER backend on the test database (excluding `pg`'s own),
    releasing whatever lock/open transaction a genuinely-stalled racer's connection may
    still be holding. Safe: pytest runs this suite's tests serially, so during any
    given test the only OTHER backends that can legitimately exist are the ones this
    same test's own racer threads opened — there is no other concurrent legitimate
    session to accidentally kill. Returns how many backends were terminated.
    """
    mine = pg.execute("SELECT pg_backend_pid()").fetchone()[0]
    victims = pg.execute(
        "SELECT pid FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid <> %s AND state <> 'idle'",
        (mine,)).fetchall()
    for (pid,) in victims:
        pg.execute("SELECT pg_terminate_backend(%s)", (pid,))
    return len(victims)
