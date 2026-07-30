"""Incremental IMAP poll logic (#27: this module was excluded from coverage).

IMAPClient is a network client, so it is faked here (the only mocking the test
policy allows — an external service). Everything else is the real code path.
"""
from app import db, imap_poll
from app.config import Config


class FakeIMAP:
    """Minimal stand-in for imapclient.IMAPClient used as a context manager."""

    def __init__(self, uidvalidity=1, messages=None, **kw):
        self.uidvalidity = uidvalidity
        self.messages = messages or {}     # uid -> raw bytes
        self.logged_in = False
        self.selected = None
        self.readonly = None
        self.searched = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, pw):
        self.logged_in = (user, pw)

    def select_folder(self, folder, readonly=False):
        self.selected, self.readonly = folder, readonly
        return {b"UIDVALIDITY": self.uidvalidity}

    def search(self, criteria):
        self.searched = criteria
        lo = int(str(criteria[1]).split(":")[0])
        return [u for u in sorted(self.messages) if u >= lo]

    def fetch(self, uids, parts):
        return {u: {b"RFC822": self.messages[u]} for u in uids if u in self.messages}


def _cfg():
    return Config(imap_host="imap.test", imap_port=993, imap_user="u", imap_pass="p")


def _install(monkeypatch, fake):
    monkeypatch.setattr(imap_poll, "IMAPClient", lambda *a, **kw: fake)
    return fake


def test_first_run_reads_everything(pg, monkeypatch):
    pg.execute("TRUNCATE folder_state")
    fake = _install(monkeypatch, FakeIMAP(messages={3: b"A", 4: b"B"}))
    validity, msgs = imap_poll.poll_folder(_cfg(), pg, "INBOX")
    assert validity == 1
    assert [u for u, _ in msgs] == [3, 4]
    assert fake.readonly is True, "the mailbox must never be opened writable"
    assert fake.logged_in == ("u", "p")


def test_only_uids_above_the_watermark_are_returned(pg, monkeypatch):
    pg.execute("TRUNCATE folder_state")
    db.set_folder_state(pg, "INBOX", 1, 4)
    _install(monkeypatch, FakeIMAP(messages={3: b"A", 4: b"B", 5: b"C", 6: b"D"}))
    _, msgs = imap_poll.poll_folder(_cfg(), pg, "INBOX")
    assert [u for u, _ in msgs] == [5, 6]


def test_a_renumbered_mailbox_is_rescanned_from_the_start(pg, monkeypatch):
    pg.execute("TRUNCATE folder_state")
    db.set_folder_state(pg, "INBOX", 1, 99)
    _install(monkeypatch, FakeIMAP(uidvalidity=42, messages={1: b"A", 2: b"B"}))
    validity, msgs = imap_poll.poll_folder(_cfg(), pg, "INBOX")
    assert validity == 42
    assert [u for u, _ in msgs] == [1, 2], "a new UIDVALIDITY means the UIDs are new"


def test_messages_are_returned_in_uid_order(pg, monkeypatch):
    pg.execute("TRUNCATE folder_state")
    _install(monkeypatch, FakeIMAP(messages={7: b"C", 5: b"A", 6: b"B"}))
    _, msgs = imap_poll.poll_folder(_cfg(), pg, "INBOX")
    assert [u for u, _ in msgs] == [5, 6, 7]
    assert [r for _, r in msgs] == [b"A", b"B", b"C"]


def test_nothing_new_yields_an_empty_batch(pg, monkeypatch):
    pg.execute("TRUNCATE folder_state")
    db.set_folder_state(pg, "INBOX", 1, 9)
    _install(monkeypatch, FakeIMAP(messages={3: b"A"}))
    _, msgs = imap_poll.poll_folder(_cfg(), pg, "INBOX")
    assert msgs == []


def test_a_uid_the_server_will_not_hand_over_is_skipped_not_faked(pg, monkeypatch):
    """A UID that returns no RFC822 body must not enter the batch as an empty mail."""
    pg.execute("TRUNCATE folder_state")

    fake = FakeIMAP(messages={5: b"A", 6: b"B"})
    real_fetch = fake.fetch
    fake.fetch = lambda uids, parts: {k: v for k, v in real_fetch(uids, parts).items() if k != 6}
    _install(monkeypatch, fake)
    _, msgs = imap_poll.poll_folder(_cfg(), pg, "INBOX")
    assert [u for u, _ in msgs] == [5]
