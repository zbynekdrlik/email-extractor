"""Upload the EDI document to the ORION machine (#67, extended #203 for DESADV/in_DL).

ORION watches a Windows folder over SFTP. The transport is injectable so the pipeline's
composition can be tested without a live host, and so a future change of transport does
not touch the pipeline.
"""
from __future__ import annotations

import logging

from . import edi

log = logging.getLogger("orders.upload")
CONNECT_TIMEOUT = 30
DL_DIR = "C:\\ORION\\COMMUNICATOR\\data\\in_DL"


def _connect(cfg):
    import paramiko

    host = getattr(cfg, "orion_host", "")
    if not host:
        raise OSError("orion_host is not configured")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(host, port=int(getattr(cfg, "orion_port", 22) or 22),
                       username=getattr(cfg, "orion_user", ""),
                       password=getattr(cfg, "orion_pass", ""),
                       timeout=CONNECT_TIMEOUT, allow_agent=False, look_for_keys=False)
    except Exception:
        # A failed connect() can still leave a partially-open transport/socket behind —
        # close it before propagating, same safety the original single-function put()
        # had (its connect() call lived inside the same try/finally as close()).
        client.close()
        raise
    return client


def put(cfg, name: str, content: str, dir_override: str = "") -> bool:
    """Write `content` as `<dir_override or orion_dir>/<name>`. Raises on failure — the
    caller releases the ledger claim so the order/document can be retried.

    `dir_override` (#203, DL migration F4) lets a DESADV upload target `in_DL` — a
    DIFFERENT top-level folder than orders' own `orion_dir` (`in`) — without a second,
    near-duplicate upload function. Orders never pass it (`orion_dir` stays the default)."""
    client = _connect(cfg)
    try:
        sftp = client.open_sftp()
        try:
            base = dir_override or getattr(cfg, "orion_dir", edi.ORION_DIR)
            target = f"{base}\\{name}"
            with sftp.file(target, "w") as fh:
                fh.write(content)
            log.info("uploaded %s (%d bytes) to %s", name, len(content),
                     getattr(cfg, "orion_host", ""))
        finally:
            sftp.close()
    finally:
        client.close()
    return True


def list_dirs(cfg) -> dict[str, set[str]]:
    """READ-ONLY filename listing of `in`, `in_DL`, `in/archCodex`, `in/unconfirmed`
    (#151, extended #203) — the import-confirmation sweep's only way to see what
    Communicator did with an uploaded file. Never writes, renames, or deletes anything.
    Raises on failure, same as `put()` — the caller (`orders.confirm.sweep`) logs and
    simply retries next sweep.

    `in_DL` (DESADV uploads, #203) is a SIBLING of `in`, not nested under it — verified
    LIVE 2026-08-07 against the real ORION box: `in_DL` has NO `archCodex`/`unconfirmed`
    of its own (`FileNotFoundError` on both). A DESADV file's post-import status is
    checked against the SAME `archCodex`/`unconfirmed` folders `in`'s own ORDER_ files
    use — confirmed live: `in\\archCodex` already holds 190 real `Z-DESADV_*` entries.
    """
    base = getattr(cfg, "orion_dir", edi.ORION_DIR)
    dl_base = getattr(cfg, "orion_dl_dir", DL_DIR)
    client = _connect(cfg)
    try:
        sftp = client.open_sftp()
        try:
            return {
                "in": set(sftp.listdir(base)),
                "in_DL": set(sftp.listdir(dl_base)),
                "archCodex": set(sftp.listdir(f"{base}\\archCodex")),
                "unconfirmed": set(sftp.listdir(f"{base}\\unconfirmed")),
            }
        finally:
            sftp.close()
    finally:
        client.close()
