"""SFTP transport to ORION (#67 `put`, #151 `list_dirs`, extended #203 for DESADV/
`in_DL`) — no live host here, paramiko is faked. Pins the one thing that actually broke
during #151's own code review: splitting `put()`'s connect logic into a shared
`_connect()` briefly moved `client.connect()` OUTSIDE the try/finally that closes it,
leaking a partially-open client on a connect failure. `list_dirs()` is covered too since
it shares the exact same `_connect()`.
"""
from unittest.mock import MagicMock, patch

import pytest

from app.config import Config
from app.orders import upload

ORION_DIR = "C:\\ORION\\COMMUNICATOR\\data\\in"
DL_DIR = "C:\\ORION\\COMMUNICATOR\\data\\in_DL"


def _cfg(**kw):
    base = dict(orion_host="192.168.1.10", orion_port=22, orion_user="u", orion_pass="p",
               orion_dir=ORION_DIR, orion_dl_dir=DL_DIR)
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


def test_list_dirs_lists_all_four_folders_read_only_and_closes_up():
    fake_sftp = MagicMock()
    fake_sftp.listdir.side_effect = [["a.txt"], ["Z-d.txt"], ["b.txt", "Z-c.txt"], []]
    fake_client = MagicMock()
    fake_client.open_sftp.return_value = fake_sftp
    with patch("paramiko.SSHClient", return_value=fake_client):
        dirs = upload.list_dirs(_cfg())
    assert dirs == {"in": {"a.txt"}, "in_DL": {"Z-d.txt"},
                    "archCodex": {"b.txt", "Z-c.txt"}, "unconfirmed": set()}
    fake_sftp.listdir.assert_any_call(ORION_DIR)
    fake_sftp.listdir.assert_any_call(DL_DIR)
    fake_sftp.listdir.assert_any_call(ORION_DIR + "\\archCodex")
    fake_sftp.listdir.assert_any_call(ORION_DIR + "\\unconfirmed")
    fake_sftp.close.assert_called_once()
    fake_client.close.assert_called_once()


def test_list_dirs_never_checks_in_dl_for_its_own_archcodex_or_unconfirmed():
    """Verified LIVE 2026-08-07 against the real ORION box: in_DL has no subfolders of
    its own — a DESADV file's post-import status is read from `in`'s shared archCodex/
    unconfirmed, never an (nonexistent) in_DL\\archCodex."""
    fake_sftp = MagicMock()
    fake_sftp.listdir.side_effect = [["a.txt"], [], [], []]
    fake_client = MagicMock()
    fake_client.open_sftp.return_value = fake_sftp
    with patch("paramiko.SSHClient", return_value=fake_client):
        upload.list_dirs(_cfg())
    called_paths = [c.args[0] for c in fake_sftp.listdir.call_args_list]
    assert DL_DIR + "\\archCodex" not in called_paths
    assert DL_DIR + "\\unconfirmed" not in called_paths


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


# --- put() dir_override (#203) ------------------------------------------------

def test_put_writes_to_orion_dir_by_default():
    fake_file = MagicMock()
    fake_sftp = MagicMock()
    fake_sftp.file.return_value.__enter__.return_value = fake_file
    fake_client = MagicMock()
    fake_client.open_sftp.return_value = fake_sftp
    with patch("paramiko.SSHClient", return_value=fake_client):
        ok = upload.put(_cfg(), "ORDER_x.txt", "content")
    assert ok is True
    written_path = fake_sftp.file.call_args.args[0]
    assert written_path.startswith(ORION_DIR + "\\.part-")
    assert written_path.endswith("-ORDER_x.txt")
    fake_sftp.rename.assert_called_once_with(written_path, ORION_DIR + "\\ORDER_x.txt")


def test_put_writes_to_dir_override_when_given():
    """A DESADV upload targets in_DL, a DIFFERENT top-level folder than orion_dir —
    never a second near-duplicate upload function."""
    fake_file = MagicMock()
    fake_sftp = MagicMock()
    fake_sftp.file.return_value.__enter__.return_value = fake_file
    fake_client = MagicMock()
    fake_client.open_sftp.return_value = fake_sftp
    with patch("paramiko.SSHClient", return_value=fake_client):
        ok = upload.put(_cfg(), "Z-DESADV_x.txt", "content", dir_override=DL_DIR)
    assert ok is True
    written_path = fake_sftp.file.call_args.args[0]
    assert written_path.startswith(DL_DIR + "\\.part-")
    fake_sftp.rename.assert_called_once_with(written_path, DL_DIR + "\\Z-DESADV_x.txt")


# --- put() temp-write + rename (#239 finding 6) --------------------------------

def test_put_never_writes_the_final_name_directly():
    """#239 finding 6: an interrupted write must never leave bytes under the FINAL,
    validly-named target — a real incident's root cause. The write always targets a
    temp name; only rename() ever touches the final name."""
    fake_file = MagicMock()
    fake_sftp = MagicMock()
    fake_sftp.file.return_value.__enter__.return_value = fake_file
    fake_client = MagicMock()
    fake_client.open_sftp.return_value = fake_sftp
    with patch("paramiko.SSHClient", return_value=fake_client):
        upload.put(_cfg(), "ORDER_x.txt", "content")
    written_path = fake_sftp.file.call_args.args[0]
    assert written_path != ORION_DIR + "\\ORDER_x.txt"


def test_put_never_renames_when_the_write_itself_raises():
    """A write failure must leave only the (unrecognizable) temp name behind — never
    call rename(), so the final name is never touched at all."""
    fake_sftp = MagicMock()
    fake_sftp.file.side_effect = OSError("connection timed out")
    fake_client = MagicMock()
    fake_client.open_sftp.return_value = fake_sftp
    with patch("paramiko.SSHClient", return_value=fake_client):
        with pytest.raises(OSError):
            upload.put(_cfg(), "ORDER_x.txt", "content")
    fake_sftp.rename.assert_not_called()


def test_put_best_effort_removes_the_temp_file_on_a_write_failure():
    """A write failure must not leave an orphaned .part-* file on ORION forever."""
    fake_file = MagicMock()
    fake_file.write.side_effect = OSError("connection reset")
    fake_sftp = MagicMock()
    fake_sftp.file.return_value.__enter__.return_value = fake_file
    fake_client = MagicMock()
    fake_client.open_sftp.return_value = fake_sftp
    with patch("paramiko.SSHClient", return_value=fake_client):
        with pytest.raises(OSError):
            upload.put(_cfg(), "ORDER_x.txt", "content")
    written_path = fake_sftp.file.call_args.args[0]
    fake_sftp.remove.assert_called_once_with(written_path)
    fake_sftp.rename.assert_not_called()


def test_put_reraises_the_original_error_even_if_temp_cleanup_itself_fails():
    """A SECOND failure (the cleanup remove() itself) must never hide or replace the
    REAL error the caller needs to see and act on."""
    fake_file = MagicMock()
    fake_file.write.side_effect = OSError("connection reset")
    fake_sftp = MagicMock()
    fake_sftp.file.return_value.__enter__.return_value = fake_file
    fake_sftp.remove.side_effect = OSError("cleanup also failed")
    fake_client = MagicMock()
    fake_client.open_sftp.return_value = fake_sftp
    with patch("paramiko.SSHClient", return_value=fake_client):
        with pytest.raises(OSError, match="connection reset"):
            upload.put(_cfg(), "ORDER_x.txt", "content")


def test_put_propagates_a_rename_failure_without_leaving_it_silent():
    """If the rename itself fails (e.g. the connection drops right after the write),
    put() must still raise — the caller's own failure handling (release the claim, no
    retry, durable alert) is what makes this visible, same as a write failure."""
    fake_file = MagicMock()
    fake_sftp = MagicMock()
    fake_sftp.file.return_value.__enter__.return_value = fake_file
    fake_sftp.rename.side_effect = OSError("connection timed out")
    fake_client = MagicMock()
    fake_client.open_sftp.return_value = fake_sftp
    with patch("paramiko.SSHClient", return_value=fake_client):
        with pytest.raises(OSError):
            upload.put(_cfg(), "ORDER_x.txt", "content")
    fake_sftp.close.assert_called_once()
    fake_client.close.assert_called_once()
