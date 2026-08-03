"""SFTP transport to ORION (#67 `put`, #151 `list_dirs`) — no live host here, paramiko is
faked. Pins the one thing that actually broke during #151's own code review: splitting
`put()`'s connect logic into a shared `_connect()` briefly moved `client.connect()`
OUTSIDE the try/finally that closes it, leaking a partially-open client on a connect
failure. `list_dirs()` is covered too since it shares the exact same `_connect()`.
"""
from unittest.mock import MagicMock, patch

import pytest

from app.config import Config
from app.orders import upload

ORION_DIR = "C:\\ORION\\COMMUNICATOR\\data\\in"


def _cfg(**kw):
    base = dict(orion_host="192.168.1.10", orion_port=22, orion_user="u", orion_pass="p",
               orion_dir=ORION_DIR)
    base.update(kw)
    return Config(**base)


def test_connect_raises_when_orion_host_is_not_configured():
    with pytest.raises(OSError):
        upload._connect(_cfg(orion_host=""))


def test_connect_closes_the_client_when_connect_itself_fails():
    """Regression, self-caught in review: a failed connect() must not leak a partially-
    open client — the original single-function put() always closed it via one shared
    try/finally; splitting the connect logic into _connect() must preserve that."""
    fake_client = MagicMock()
    fake_client.connect.side_effect = OSError("connection refused")
    with patch("paramiko.SSHClient", return_value=fake_client):
        with pytest.raises(OSError):
            upload._connect(_cfg())
    fake_client.close.assert_called_once()


def test_connect_returns_the_open_client_on_success():
    fake_client = MagicMock()
    with patch("paramiko.SSHClient", return_value=fake_client):
        got = upload._connect(_cfg())
    assert got is fake_client
    fake_client.close.assert_not_called()


def test_list_dirs_lists_all_three_folders_read_only_and_closes_up():
    fake_sftp = MagicMock()
    fake_sftp.listdir.side_effect = [["a.txt"], ["b.txt", "Z-c.txt"], []]
    fake_client = MagicMock()
    fake_client.open_sftp.return_value = fake_sftp
    with patch("paramiko.SSHClient", return_value=fake_client):
        dirs = upload.list_dirs(_cfg())
    assert dirs == {"in": {"a.txt"}, "archCodex": {"b.txt", "Z-c.txt"}, "unconfirmed": set()}
    fake_sftp.listdir.assert_any_call(ORION_DIR)
    fake_sftp.listdir.assert_any_call(ORION_DIR + "\\archCodex")
    fake_sftp.listdir.assert_any_call(ORION_DIR + "\\unconfirmed")
    fake_sftp.close.assert_called_once()
    fake_client.close.assert_called_once()


def test_list_dirs_never_writes_renames_or_deletes_anything():
    """Hard constraint: the import-confirmation sweep is read-only against ORION."""
    fake_sftp = MagicMock()
    fake_sftp.listdir.return_value = []
    fake_client = MagicMock()
    fake_client.open_sftp.return_value = fake_sftp
    with patch("paramiko.SSHClient", return_value=fake_client):
        upload.list_dirs(_cfg())
    for method in ("file", "rename", "remove", "rmdir", "mkdir", "put", "putfo"):
        getattr(fake_sftp, method).assert_not_called()
