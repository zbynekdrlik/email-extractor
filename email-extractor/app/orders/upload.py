"""Upload the EDI document to the ORION machine (#67).

ORION watches a Windows folder over SFTP. The transport is injectable so the pipeline's
composition can be tested without a live host, and so a future change of transport does
not touch the pipeline.
"""
from __future__ import annotations

import logging

from . import edi

log = logging.getLogger("orders.upload")
CONNECT_TIMEOUT = 30


def put(cfg, name: str, content: str) -> bool:
    """Write `content` as `<orion_dir>/<name>`. Raises on failure — the caller releases
    the ledger claim so the order can be retried."""
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
        sftp = client.open_sftp()
        try:
            target = f"{getattr(cfg, 'orion_dir', edi.ORION_DIR)}\\{name}"
            with sftp.file(target, "w") as fh:
                fh.write(content)
            log.info("uploaded %s (%d bytes) to %s", name, len(content), host)
        finally:
            sftp.close()
    finally:
        client.close()
    return True
