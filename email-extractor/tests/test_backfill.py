"""Regression (#270): backfill.main() must call store.save_message with its REAL signature.

mypy surfaced that app/backfill.py called store.save_message() with 6 positional args (a
stale extra `cfg.api_token`) while store.save_message takes 5. backfill wraps each message
in its own `try/except Exception`, so the resulting TypeError was swallowed per message and
backfill silently processed nothing at all — never raising, never inserting a row.

This test drives backfill.main() over ONE fake message with everything mocked except the
store.save_message SIGNATURE (mock.patch(..., autospec=True) enforces the real arity): a
wrong-arity call raises TypeError, backfill swallows it, and db.insert_message is never
reached. Reaching db.insert_message is therefore the proof the call is arity-correct.
"""
from __future__ import annotations

import sys
import types
from unittest import mock

from app import backfill


def test_backfill_reaches_insert_message_proving_save_message_arity(monkeypatch, tmp_path):
    cfg = types.SimpleNamespace(
        imap_user="u", imap_pass="p", imap_host="h", imap_port=993,
        pg_dsn="postgresql://x/db", data_dir=str(tmp_path),
        public_base_url="http://example.test", api_token="tok",
    )
    monkeypatch.setattr(backfill.config.Config, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(backfill.db, "connect", lambda dsn: object())
    monkeypatch.setattr(backfill.db, "init_schema", lambda conn: None)
    monkeypatch.setattr(backfill, "gather", lambda c, limit: [("INBOX", 1, 2, b"raw-bytes")])
    monkeypatch.setattr(
        backfill, "process_raw",
        lambda raw: {"identity": "<msg@example.test>", "attachments": []})

    inserts: list = []
    monkeypatch.setattr(
        backfill.db, "insert_message",
        lambda *a, **k: (inserts.append(a), True)[1])
    monkeypatch.setattr(sys, "argv", ["backfill", "--limit", "1"])

    # autospec pins store.save_message's REAL signature — a wrong-arity call raises
    # TypeError, which backfill's own per-message try/except swallows, so insert_message
    # is only ever reached when the call is arity-correct.
    with mock.patch("app.backfill.store.save_message", autospec=True) as sm:
        sm.return_value = (str(tmp_path / "raw.eml"), [])
        backfill.main()

    assert inserts, (
        "backfill.main() never reached db.insert_message — store.save_message was called "
        "with the wrong number of arguments (a swallowed TypeError)")
