"""#408: spam folder allowlist — poll Junk/spam folders for allowlisted senders only.

Tests cover:
- Allowlist matching (domain suffix, exact address, case insensitivity)
- Spam folder first-sight UIDNEXT initialization (no backfill)
- Allowlisted sender in spam folder IS ingested
- Non-allowlisted sender in spam folder is SKIPPED (never stored)
- Dedup: same Message-ID in Junk and INBOX → one row (existing dedup)
- Config parsing of the two new options
"""
import json

import psycopg
import pytest

from app import config as config_mod
from app import db, imap_poll, main
from app.config import Config
from app.main import _matches_spam_allowlist

# ---------------------------------------------------------------------------
# Unit tests: _matches_spam_allowlist
# ---------------------------------------------------------------------------


class TestMatchesSpamAllowlist:
    """Pure-function tests — no DB, no IMAP."""

    def test_domain_match(self):
        assert _matches_spam_allowlist("noreply@inforcloudsuite.com",
                                       "inforcloudsuite.com") is True

    def test_domain_match_case_insensitive(self):
        assert _matches_spam_allowlist("Noreply@InforCloudSuite.COM",
                                       "inforcloudsuite.com") is True

    def test_subdomain_match(self):
        assert _matches_spam_allowlist("noreply@mail.inforcloudsuite.com",
                                       "inforcloudsuite.com") is True

    def test_no_match(self):
        assert _matches_spam_allowlist("spam@phishing.ru",
                                       "inforcloudsuite.com") is False

    def test_exact_address_match(self):
        assert _matches_spam_allowlist("noreply@inforcloudsuite.com",
                                       "noreply@inforcloudsuite.com") is True

    def test_exact_address_no_match(self):
        assert _matches_spam_allowlist("other@inforcloudsuite.com",
                                       "noreply@inforcloudsuite.com") is False

    def test_multiple_entries(self):
        allowlist = "inforcloudsuite.com, trusted@example.com"
        assert _matches_spam_allowlist("noreply@inforcloudsuite.com", allowlist) is True
        assert _matches_spam_allowlist("trusted@example.com", allowlist) is True
        assert _matches_spam_allowlist("other@example.com", allowlist) is False

    def test_empty_allowlist(self):
        assert _matches_spam_allowlist("a@b.com", "") is False

    def test_empty_from_addr(self):
        assert _matches_spam_allowlist("", "inforcloudsuite.com") is False

    def test_no_at_in_from_addr(self):
        assert _matches_spam_allowlist("broken-addr", "example.com") is False

    def test_domain_not_substring(self):
        """inforcloudsuite.com should NOT match notinforcloudsuite.com."""
        assert _matches_spam_allowlist("x@notinforcloudsuite.com",
                                       "inforcloudsuite.com") is False


# ---------------------------------------------------------------------------
# IMAP fake (reuses the pattern from test_imap_poll.py)
# ---------------------------------------------------------------------------

class FakeIMAP:
    def __init__(self, uidvalidity=1, messages=None, uidnext=None):
        self.uidvalidity = uidvalidity
        self.messages = messages or {}
        self._uidnext = uidnext or (max(self.messages, default=0) + 1)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, pw):
        pass

    def select_folder(self, folder, readonly=False):
        return {b"UIDVALIDITY": self.uidvalidity, b"UIDNEXT": self._uidnext}

    def search(self, criteria):
        lo = int(str(criteria[1]).split(":")[0])
        return [u for u in sorted(self.messages) if u >= lo]

    def fetch(self, uids, parts):
        return {u: {b"RFC822": self.messages[u]} for u in uids if u in self.messages}


def _raw_email(n: int, from_addr: str = "sender@test.sk") -> bytes:
    return (f"Message-ID: <m{n}@test>\r\nFrom: {from_addr}\r\n"
            f"Subject: mail {n}\r\nDate: Tue, 09 Sep 2026 08:00:00 +0200\r\n"
            f"\r\nTelo mailu {n}\r\n").encode()


# ---------------------------------------------------------------------------
# Integration tests: spam folder init + polling
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg(pg, tmp_path):
    from tests.conftest import PG_DSN
    pg.execute("TRUNCATE folder_state")
    if pg.execute("SELECT to_regclass('imap_failures')").fetchone()[0]:
        pg.execute("TRUNCATE imap_failures")
    return Config(
        pg_dsn=PG_DSN, data_dir=str(tmp_path), folders=["INBOX"],
        public_base_url="http://email-extractor:8099", api_token="tok",
        spam_folders=["Junk"],
        spam_folder_allowlist="inforcloudsuite.com",
    )


@pytest.fixture
def conn(cfg):
    c = psycopg.connect(cfg.pg_dsn, autocommit=True)
    yield c
    c.close()


def test_first_sight_spam_folder_initializes_at_uidnext(pg, cfg, conn, monkeypatch):
    """A spam folder with no prior state should be initialized at UIDNEXT,
    skipping all existing messages (no backfill)."""
    fake = FakeIMAP(uidvalidity=1, messages={10: b"A", 20: b"B", 30: b"C"}, uidnext=31)
    monkeypatch.setattr(imap_poll, "IMAPClient", lambda *a, **kw: fake)

    # No state yet for Junk
    assert db.get_folder_state(conn, "Junk") == (None, 0)

    # run_once should initialize Junk at UIDNEXT and NOT ingest any messages.
    # Stub INBOX poll to return nothing.
    monkeypatch.setattr(main.imap_poll, "poll_folder",
                        lambda cfg, conn, folder: (1, []))
    n = main.run_once(cfg, conn)
    assert n == 0, "no messages should be ingested on first-sight init"

    # Junk folder state should now be (1, 30) — uidnext-1
    validity, last_uid = db.get_folder_state(conn, "Junk")
    assert validity == 1
    assert last_uid == 30


def test_allowlisted_sender_in_spam_folder_is_ingested(pg, cfg, conn, monkeypatch):
    """A message from an allowlisted sender in the spam folder must be stored."""
    # Pre-set Junk folder state so it's not first-sight.
    db.set_folder_state(conn, "Junk", 1, 0)

    allowlisted_raw = _raw_email(1, from_addr="noreply@inforcloudsuite.com")

    def fake_poll(c, co, folder):
        if folder == "Junk":
            return (1, [(5, allowlisted_raw)])
        return (1, [])

    monkeypatch.setattr(main.imap_poll, "poll_folder", fake_poll)
    n = main.run_once(cfg, conn)
    assert n == 1

    # Verify the message was stored.
    row = conn.execute("SELECT message_id FROM messages WHERE message_id = '<m1@test>'").fetchone()
    assert row is not None


def test_non_allowlisted_sender_in_spam_folder_is_skipped(pg, cfg, conn, monkeypatch):
    """A message from a non-allowlisted sender in the spam folder must NOT be stored."""
    db.set_folder_state(conn, "Junk", 1, 0)

    spam_raw = _raw_email(2, from_addr="marketing@phishing.ru")

    def fake_poll(c, co, folder):
        if folder == "Junk":
            return (1, [(10, spam_raw)])
        return (1, [])

    monkeypatch.setattr(main.imap_poll, "poll_folder", fake_poll)
    n = main.run_once(cfg, conn)
    assert n == 0, "non-allowlisted sender must not be ingested"

    # Verify no message was stored.
    row = conn.execute("SELECT count(*) FROM messages").fetchone()
    assert row[0] == 0

    # Watermark should still advance past the skipped UID.
    validity, last_uid = db.get_folder_state(conn, "Junk")
    assert last_uid == 10, "watermark must advance past a deliberately skipped UID"


def test_dedup_across_inbox_and_junk(pg, cfg, conn, monkeypatch):
    """The same Message-ID in INBOX and Junk should result in exactly one row."""
    db.set_folder_state(conn, "Junk", 1, 0)

    # Same message in both INBOX and Junk (same Message-ID).
    raw = _raw_email(3, from_addr="noreply@inforcloudsuite.com")

    def fake_poll(c, co, folder):
        if folder == "INBOX":
            return (1, [(1, raw)])
        if folder == "Junk":
            return (1, [(5, raw)])
        return (1, [])

    monkeypatch.setattr(main.imap_poll, "poll_folder", fake_poll)
    n = main.run_once(cfg, conn)
    # One new from INBOX, dedup'd in Junk (or vice versa, order doesn't matter).
    assert n == 1

    row = conn.execute("SELECT count(*) FROM messages WHERE message_id = '<m3@test>'").fetchone()
    assert row[0] == 1


# ---------------------------------------------------------------------------
# Config parsing tests
# ---------------------------------------------------------------------------

CONFIG_YAML_PATH = config_mod.Path(__file__).resolve().parents[1] / "config.yaml"


def _load_with_options(tmp_path, monkeypatch, options: dict) -> Config:
    opts = tmp_path / "options.json"
    opts.write_text(json.dumps(options))
    monkeypatch.setattr(config_mod, "OPTIONS_PATH", opts)
    monkeypatch.delenv("PG_DSN", raising=False)
    monkeypatch.delenv("PG_PASSWORD", raising=False)
    return Config.load()


def test_spam_folders_default(tmp_path, monkeypatch):
    cfg = _load_with_options(tmp_path, monkeypatch, {})
    assert cfg.spam_folders == ["Junk"]


def test_spam_folders_from_options(tmp_path, monkeypatch):
    cfg = _load_with_options(tmp_path, monkeypatch, {"spam_folders": "Junk, Spam"})
    assert cfg.spam_folders == ["Junk", "Spam"]


def test_spam_folder_allowlist_default(tmp_path, monkeypatch):
    cfg = _load_with_options(tmp_path, monkeypatch, {})
    assert cfg.spam_folder_allowlist == "inforcloudsuite.com"


def test_spam_folder_allowlist_from_options(tmp_path, monkeypatch):
    cfg = _load_with_options(tmp_path, monkeypatch,
                             {"spam_folder_allowlist": "a.com, b@c.com"})
    assert cfg.spam_folder_allowlist == "a.com, b@c.com"


def test_config_yaml_declares_spam_options():
    """Pin that config.yaml's options: and schema: blocks declare both new options."""
    import re
    text = CONFIG_YAML_PATH.read_text()
    options_block, sep, schema_block = text.partition("\nschema:\n")
    assert sep, "config.yaml has no schema: block"
    for name in ("spam_folders", "spam_folder_allowlist"):
        assert re.search(rf"^\s+{name}:", options_block, re.M), \
            f"{name} missing from config.yaml options: block"
        assert re.search(rf"^\s+{name}:", schema_block, re.M), \
            f"{name} missing from config.yaml schema: block"


# ---------------------------------------------------------------------------
# Review-finding regression tests (F1, F2, F3)
# ---------------------------------------------------------------------------

def test_uidvalidity_change_on_spam_folder_reinitializes_no_backfill(
    pg, cfg, conn, monkeypatch,
):
    """F1: a UIDVALIDITY change on a spam folder must NOT backfill old messages —
    it re-initializes at the max UID in the batch instead of scanning from 0."""
    # Pre-set Junk with validity=1, last_uid=30 (previously initialized).
    db.set_folder_state(conn, "Junk", 1, 30)

    # Server returns a NEW validity (2) with old allowlisted messages.
    allowlisted_raw = _raw_email(50, from_addr="noreply@inforcloudsuite.com")

    def fake_poll(c, co, folder):
        if folder == "Junk":
            return (2, [(5, allowlisted_raw), (10, allowlisted_raw)])
        return (1, [])

    monkeypatch.setattr(main.imap_poll, "poll_folder", fake_poll)
    n = main.run_once(cfg, conn)
    assert n == 0, "a UIDVALIDITY change on a spam folder must NOT ingest anything"

    # State should be re-initialized at the max UID (10) under the new validity (2).
    validity, last_uid = db.get_folder_state(conn, "Junk")
    assert validity == 2
    assert last_uid == 10

    # No messages stored.
    row = conn.execute("SELECT count(*) FROM messages").fetchone()
    assert row[0] == 0


def test_init_spam_folder_raises_without_uidnext(pg, cfg, conn, monkeypatch):
    """F1: init_spam_folder must raise when the server does not report UIDNEXT,
    rather than writing watermark 0 (which would backfill the whole folder)."""

    class NoUidnextIMAP:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, u, p):
            pass

        def select_folder(self, folder, readonly=False):
            return {b"UIDVALIDITY": 1}  # no UIDNEXT

    monkeypatch.setattr(imap_poll, "IMAPClient", lambda *a, **kw: NoUidnextIMAP())
    with pytest.raises(ValueError, match="UIDNEXT"):
        imap_poll.init_spam_folder(cfg, conn, "Junk")

    # folder_state must NOT have been written.
    assert db.get_folder_state(conn, "Junk") == (None, 0)


def test_non_allowlisted_broken_message_does_not_stall_watermark(
    pg, cfg, conn, monkeypatch,
):
    """F2: a non-allowlisted message whose process_raw would raise must still be
    skipped cleanly — the allowlist check runs BEFORE extraction, so no
    imap_failures row is created and the watermark advances."""
    db.set_folder_state(conn, "Junk", 1, 0)

    # A non-allowlisted message with garbage that would crash process_raw.
    broken_raw = b"INVALID-NOT-AN-EMAIL"

    # An allowlisted message after it, to prove the watermark advances.
    good_raw = _raw_email(99, from_addr="noreply@inforcloudsuite.com")

    def fake_poll(c, co, folder):
        if folder == "Junk":
            return (1, [(5, broken_raw), (10, good_raw)])
        return (1, [])

    monkeypatch.setattr(main.imap_poll, "poll_folder", fake_poll)
    n = main.run_once(cfg, conn)

    # The allowlisted message should be ingested.
    assert n == 1

    # The broken non-allowlisted message must NOT create an imap_failures row.
    failures = db.list_uid_failures(conn)
    assert len(failures) == 0, "non-allowlisted spam must not land in imap_failures"

    # Watermark should be at 10 (past both messages).
    validity, last_uid = db.get_folder_state(conn, "Junk")
    assert last_uid == 10
