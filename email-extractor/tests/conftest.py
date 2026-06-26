"""Shared fixtures.

DB-backed tests require a real Postgres via PG_TEST_DSN (CI provides a postgres
service; locally use a throwaway docker PG). Without it those tests FAIL loudly —
they never silently skip (per the no-skip test policy).
"""
import os

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
