"""Upload the EDI document to the ORION machine (#67, extended #203 for DESADV/in_DL).

ORION watches a Windows folder over SFTP. The transport is injectable so the pipeline's
composition can be tested without a live host, and so a future change of transport does
not touch the pipeline.

**#239 finding 6: `put()` writes to a TEMPORARY name and renames to the final name only
after the write completes.** The original single-step write (`sftp.file(target, "w")`)
could leave an INCOMPLETE/corrupt file under the final, validly-named target if the
transfer was interrupted mid-write — Communicator would try to import it as if it were
complete. A single SFTP `rename()` is atomic on the same filesystem: either it happened
(final name present, temp name gone) or it didn't (only the temp name exists, under a
name nothing else recognizes) — no half-state. This makes "is the final name present"
trustworthy evidence the transfer genuinely completed, independent of whether the
CLIENT received a confirming response — see `desadv_edi.already_landed()`, the
identity-based presence check this enables. `.claude/rules/n8n-workflow-edits.md` has
the full incident history.
"""
from __future__ import annotations

import logging
import time

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


def _temp_name(name: str) -> str:
    """A name nothing else on ORION recognizes as a real EDI file — never matches
    `desadv_edi.stable_prefix()`'s pattern, so a leftover temp file (a crash between
    write and rename) can never be mistaken for a landed document by `already_landed()`
    or by Communicator's own import scan."""
    now = time.time()
    stamp = f"{int(now * 1000) % 1_000_000_000:09d}"
    return f".part-{stamp}-{name}"


def put(cfg, name: str, content: str, dir_override: str = "") -> bool:
    """Write `content` as `<dir_override or orion_dir>/<name>`, via a temp-write +
    rename (#239 finding 6 — see the module docstring). Raises on failure — the caller
    releases the ledger claim so the order/document can be retried.

    `dir_override` (#203, DL migration F4) lets a DESADV upload target `in_DL` — a
    DIFFERENT top-level folder than orders' own `orion_dir` (`in`) — without a second,
    near-duplicate upload function. Orders never pass it (`orion_dir` stays the default)."""
    client = _connect(cfg)
    try:
        sftp = client.open_sftp()
        try:
            base = dir_override or getattr(cfg, "orion_dir", edi.ORION_DIR)
            target = f"{base}\\{name}"
            tmp_target = f"{base}\\{_temp_name(name)}"
            try:
                with sftp.file(tmp_target, "w") as fh:
                    fh.write(content)
                sftp.rename(tmp_target, target)
            except Exception:
                # Review finding: an earlier draft only cleaned up on a WRITE failure —
                # a RENAME failure (e.g. the connection drops right after a successful
                # write) left the temp file orphaned forever with nothing cleaning it
                # up. Best-effort cleanup covers BOTH failure points now. Safe even when
                # the rename itself actually succeeded before the confirming response
                # was lost (the exact "bytes landed, only the reply vanished" ambiguity
                # this whole design exists to survive): removing an already-renamed-away
                # temp name simply no-ops (FileNotFoundError, caught below). Never lets
                # a SECOND (cleanup) failure hide or replace the REAL error the caller
                # needs to see.
                try:
                    sftp.remove(tmp_target)
                except Exception:
                    log.warning("could not remove temp file %s after a failed upload "
                               "(may already be gone if the rename itself actually "
                               "succeeded)", tmp_target)
                raise
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
