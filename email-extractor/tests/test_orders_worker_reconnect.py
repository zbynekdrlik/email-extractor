"""#380: the order-worker thread must reconnect after a Postgres OperationalError.

Incident 2026-09-04: the bundled Postgres crashed (disk full -> PANIC), auto-recovered
and was accepting connections again a minute later — but `worker.run_forever` holds ONE
connection for its whole life and its `except Exception: log.exception(...)` swallowed
`psycopg.OperationalError: the connection is closed`, retrying the SAME dead connection
every 15s until a manual add-on restart 2h46m later (zero static/Karmen + AI + DL orders
processed the whole time). `app/main.py::main`'s IMAP loop already reconnects on
`psycopg.OperationalError`; the worker loop did not.

This drives the real `run_forever` loop with a connection whose first query raises the
exact post-crash error and a `connect` factory that hands back a fresh (healthy)
connection, and proves the loop reconnects via the factory, closes the dead connection,
runs the next tick on the fresh one, and never spins on the dead connection again.
"""
import threading

import psycopg

from app.config import Config
from app.orders import worker


class _DeadConn:
    """A connection whose every query raises the exact post-crash error."""

    def __init__(self):
        self.execute_calls = 0
        self.closed = False

    def execute(self, *a, **k):
        self.execute_calls += 1
        raise psycopg.OperationalError("the connection is closed")

    def close(self):
        self.closed = True


class _RecordingConn:
    """Wraps a real, healthy connection and records that the loop actually used it."""

    def __init__(self, real):
        self._real = real
        self.used = False

    def execute(self, *a, **k):
        self.used = True
        return self._real.execute(*a, **k)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _cfg(**kw):
    base = dict(pg_dsn="", data_dir="/tmp", ai_orders_engine="n8n", orders_shadow=False)
    base.update(kw)
    return Config(**base)


def test_run_forever_reconnects_after_the_connection_is_closed(pg):
    """The first tick's query raises `OperationalError: the connection is closed`; the
    loop must close that dead connection, obtain a fresh one from the injected factory,
    and run the very next tick on the fresh connection — never re-using the dead one."""
    dead = _DeadConn()
    fresh = _RecordingConn(pg)
    factory_calls = {"n": 0}

    def connect():
        factory_calls["n"] += 1
        return fresh

    stop = threading.Event()
    sleeps = []

    def fake_sleep(seconds):
        # Iteration 1 fails on the dead conn and reconnects (one sleep); iteration 2 runs
        # cleanly on the fresh conn (second sleep). Stop after that so the loop exits.
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            stop.set()

    worker.run_forever(dead, _cfg(), stop=stop, sleep=fake_sleep, connect=connect)

    assert factory_calls["n"] == 1, "the loop must obtain a fresh connection exactly once"
    assert dead.closed is True, "the dead connection must be closed on reconnect"
    assert dead.execute_calls == 1, "the loop must never spin on the dead connection again"
    assert fresh.used is True, "the very next tick must run on the fresh connection"
    assert len(sleeps) == 2 and all(s == 15 for s in sleeps), (
        "bounded 15s backoff, one sleep per loop iteration — never a tight loop")
