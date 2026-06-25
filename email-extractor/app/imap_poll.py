"""Read-only incremental IMAP poll: yield new messages per folder by UID."""
from __future__ import annotations

from imapclient import IMAPClient

from . import db


def poll_folder(cfg, conn, folder: str) -> tuple[int, list[tuple[int, bytes]]]:
    """Return (uidvalidity, [(uid, raw_rfc822), ...]) for messages newer than last seen."""
    with IMAPClient(cfg.imap_host, port=cfg.imap_port, ssl=True) as c:
        c.login(cfg.imap_user, cfg.imap_pass)
        sel = c.select_folder(folder, readonly=True)
        uidvalidity = int(sel.get(b"UIDVALIDITY", 0))
        prev_validity, last_uid = db.get_folder_state(conn, folder)
        if prev_validity != uidvalidity:
            last_uid = 0  # mailbox re-numbered (or first run): re-scan from the start
        uids = [u for u in c.search(["UID", f"{last_uid + 1}:*"]) if u > last_uid]
        results = []
        if uids:
            fetched = c.fetch(uids, ["RFC822"])
            for uid in sorted(uids):
                raw = fetched.get(uid, {}).get(b"RFC822")
                if raw:
                    results.append((uid, raw))
        return uidvalidity, results
