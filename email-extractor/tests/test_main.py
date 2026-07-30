"""run_once watermark safety (#20): a failed UID must never be skipped silently.

The bug: `max_uid` advanced for every UID regardless of outcome, so an email whose
extraction/store/insert raised was logged-and-skipped AND its UID was persisted →
the next poll started above it and that order was lost forever, with no record.
"""
import psycopg
import pytest

from app import config, db, main
from app.process import process_raw


def _raw(n: int) -> bytes:
    return (f"Message-ID: <m{n}@test>\r\nFrom: odosielatel@test.sk\r\n"
            f"Subject: mail {n}\r\nDate: Tue, 28 Jul 2026 08:00:00 +0200\r\n"
            f"\r\nTelo mailu {n}\r\n").encode()


@pytest.fixture
def cfg(pg, tmp_path):
    from tests.conftest import PG_DSN
    pg.execute("TRUNCATE folder_state")
    if pg.execute("SELECT to_regclass('imap_failures')").fetchone()[0]:
        pg.execute("TRUNCATE imap_failures")
    return config.Config(pg_dsn=PG_DSN, data_dir=str(tmp_path), folders=["INBOX"],
                         public_base_url="http://email-extractor:8099", api_token="tok")


@pytest.fixture
def conn(cfg):
    c = psycopg.connect(cfg.pg_dsn, autocommit=True)
    yield c
    c.close()


def _feed(monkeypatch, uids_raws, fail_uids=(), uidvalidity=1):
    """Stub the IMAP poll with a fixed batch; raise inside processing for fail_uids."""
    monkeypatch.setattr(main.imap_poll, "poll_folder",
                        lambda cfg, conn, folder: (uidvalidity, list(uids_raws)))
    real = process_raw          # the module function, never an already-patched wrapper
    fail_bodies = {f"Telo mailu {u}" for u in fail_uids}

    def flaky(raw: bytes):
        text = raw.decode()
        if any(b in text for b in fail_bodies):
            raise RuntimeError("OCR out of memory")
        return real(raw)

    monkeypatch.setattr(main, "process_raw", flaky)


def _state(conn):
    return db.get_folder_state(conn, "INBOX")


def test_watermark_stops_below_failed_uid(cfg, conn, monkeypatch):
    _feed(monkeypatch, [(10, _raw(10)), (11, _raw(11)), (12, _raw(12))], fail_uids=[11])
    main.run_once(cfg, conn)
    assert _state(conn) == (1, 10), "watermark must not pass the failed UID 11"


def test_failed_uid_is_recorded_not_silent(cfg, conn, monkeypatch):
    _feed(monkeypatch, [(10, _raw(10)), (11, _raw(11))], fail_uids=[11])
    main.run_once(cfg, conn)
    rows = db.list_uid_failures(conn)
    assert len(rows) == 1
    assert rows[0]["uid"] == 11
    assert rows[0]["attempts"] == 1
    assert rows[0]["skipped"] is False
    assert "out of memory" in rows[0]["last_error"]
    assert db.count_uid_failures(conn) == 1


def test_failed_uid_is_retried_and_clears_on_success(cfg, conn, monkeypatch):
    batch = [(10, _raw(10)), (11, _raw(11)), (12, _raw(12))]
    _feed(monkeypatch, batch, fail_uids=[11])
    main.run_once(cfg, conn)
    assert _state(conn) == (1, 10)
    # next poll: the transient error is gone → 11 and 12 are processed, watermark moves
    _feed(monkeypatch, [(11, _raw(11)), (12, _raw(12))])
    main.run_once(cfg, conn)
    assert _state(conn) == (1, 12)
    assert db.list_uid_failures(conn) == []
    ids = {r[0] for r in conn.execute("SELECT message_id FROM messages").fetchall()}
    assert ids == {"<m10@test>", "<m11@test>", "<m12@test>"}


def test_poison_uid_is_dead_lettered_so_the_folder_never_wedges(cfg, conn, monkeypatch):
    """A permanently broken email must not block every later email forever."""
    for _ in range(main.MAX_UID_ATTEMPTS):
        _feed(monkeypatch, [(11, _raw(11)), (12, _raw(12))], fail_uids=[11])
        main.run_once(cfg, conn)
        assert _state(conn) == (1, 0), "watermark stays put while retries remain"
    _feed(monkeypatch, [(11, _raw(11)), (12, _raw(12))], fail_uids=[11])
    main.run_once(cfg, conn)
    assert _state(conn) == (1, 12), "after MAX_UID_ATTEMPTS the poison UID is passed"
    rows = db.list_uid_failures(conn)
    assert len(rows) == 1 and rows[0]["uid"] == 11
    assert rows[0]["skipped"] is True, "it stays on record as skipped, never silent"
    assert rows[0]["attempts"] > main.MAX_UID_ATTEMPTS - 1


def test_all_ok_advances_to_the_highest_uid(cfg, conn, monkeypatch):
    _feed(monkeypatch, [(10, _raw(10)), (11, _raw(11)), (12, _raw(12))])
    assert main.run_once(cfg, conn) == 3
    assert _state(conn) == (1, 12)
    assert db.list_uid_failures(conn) == []


def test_uidvalidity_change_is_persisted_even_with_a_failure(cfg, conn, monkeypatch):
    """Otherwise the mailbox re-numbering is re-detected every cycle (endless rescan)."""
    _feed(monkeypatch, [(5, _raw(5))])
    main.run_once(cfg, conn)
    assert _state(conn) == (1, 5)
    _feed(monkeypatch, [(1, _raw(21))], fail_uids=[21], uidvalidity=77)
    main.run_once(cfg, conn)
    assert _state(conn)[0] == 77, "new UIDVALIDITY must be stored"
    assert _state(conn)[1] == 0, "but no UID may be marked as done"


def test_poll_failure_leaves_the_folder_untouched(cfg, conn, monkeypatch):
    def boom(cfg, conn, folder):
        raise RuntimeError("IMAP login failed")

    monkeypatch.setattr(main.imap_poll, "poll_folder", boom)
    assert main.run_once(cfg, conn) == 0
    assert _state(conn) == (None, 0)
