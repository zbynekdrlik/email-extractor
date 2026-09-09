"""Read-only incremental IMAP poll: yield new messages per folder by UID."""
from __future__ import annotations

from imapclient import IMAPClient

from . import db


def init_spam_folder(cfg, conn, folder: str) -> None:
    """Initialize a spam folder's cursor at UIDNEXT so no backfill occurs.

    Called on a spam folder's first-ever poll (no folder_state row).  The next
    poll_folder call will then start from UIDNEXT, fetching only truly NEW messages.

    Raises ``ValueError`` when the server does not report UIDNEXT — writing a 0
    watermark would backfill the entire folder, the exact outcome this function
    exists to prevent (F1 review finding).
    """
    with IMAPClient(cfg.imap_host, port=cfg.imap_port, ssl=True) as c:
        c.login(cfg.imap_user, cfg.imap_pass)
        sel = c.select_folder(folder, readonly=True)
        uidvalidity = int(sel.get(b"UIDVALIDITY", 0))
        uidnext = sel.get(b"UIDNEXT")
        if uidnext is None or int(uidnext) <= 0:
            raise ValueError(
                f"IMAP server did not report UIDNEXT for {folder!r} — "
                f"cannot initialize spam folder without it (would backfill)")
        uidnext = int(uidnext)
    # Set watermark to uidnext - 1 so the next poll fetches only UIDs >= uidnext.
    db.set_folder_state(conn, folder, uidvalidity, uidnext - 1)


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
