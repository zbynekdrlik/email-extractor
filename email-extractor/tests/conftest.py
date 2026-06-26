"""Shared fixtures.

DB-backed tests require a real Postgres via PG_TEST_DSN (CI provides a postgres
service; locally use a throwaway docker PG). Without it those tests FAIL loudly —
they never silently skip (per the no-skip test policy).
"""
import os
import threading

import psycopg
import pytest

from app import db

PG_DSN = os.environ.get("PG_TEST_DSN")


@pytest.fixture(scope="session")
def _schema():
    if not PG_DSN:
        pytest.fail("PG_TEST_DSN not set — DB tests need a Postgres "
                    "(CI postgres service, or a local docker PG)")
    conn = psycopg.connect(PG_DSN, autocommit=True)
    db.init_schema(conn)
    yield conn
    conn.close()


@pytest.fixture
def pg(_schema):
    """Clean slate before each DB test."""
    _schema.execute(
        "TRUNCATE messages, attachments, email_events, fix_requests RESTART IDENTITY CASCADE")
    return _schema


@pytest.fixture
def live_server(pg):
    """Run the real Flask app in a background thread against the test DB; yields
    its base URL. Used by the Playwright E2E (real browser, real backend)."""
    from werkzeug.serving import make_server

    from app.config import Config
    from app.httpapi import create_app

    cfg = Config(pg_dsn=PG_DSN, data_dir="/tmp", api_token="tok",
                 dash_password="secret", secret_key="e2e-secret")
    srv = make_server("127.0.0.1", 0, create_app(cfg), threaded=True)
    srv.daemon_threads = True   # don't block teardown joining a worker held on a keep-alive socket
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        t.join(timeout=5)
