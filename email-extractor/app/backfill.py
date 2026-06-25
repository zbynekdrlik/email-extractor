"""One-off backfill: process the N most recent existing emails across folders into Postgres.

Unlike the incremental poll (new UIDs per folder), this gathers the newest N messages
by date across all mail folders and inserts them (dedup via message_id). Use to seed
the table from an existing mailbox.

    python -m app.backfill --limit 500
"""
from __future__ import annotations

import argparse
import logging

from imapclient import IMAPClient

from . import config, db, store
from .process import process_raw

log = logging.getLogger("backfill")

SKIP_FOLDERS = {"Trash", "Junk", "Drafts", "Sent", "Sent Items",
                "Deleted Items", "INBOX/edi"}


def gather(cfg, limit: int):
    """Return [(folder, uidvalidity, uid, raw), ...] for the newest `limit` messages."""
    with IMAPClient(cfg.imap_host, port=cfg.imap_port, ssl=True) as c:
        c.login(cfg.imap_user, cfg.imap_pass)
        candidates = []
        for folder in [f[2] for f in c.list_folders()]:
            if folder in SKIP_FOLDERS:
                continue
            try:
                sel = c.select_folder(folder, readonly=True)
            except Exception:
                continue
            uidvalidity = int(sel.get(b"UIDVALIDITY", 0))
            uids = c.search(["ALL"])
            if not uids:
                continue
            recent = uids[-limit:]
            info = c.fetch(recent, ["INTERNALDATE"])
            for uid in recent:
                d = info.get(uid, {}).get(b"INTERNALDATE")
                if d:
                    candidates.append((d, folder, uidvalidity, uid))
        candidates.sort(key=lambda x: x[0], reverse=True)
        chosen = candidates[:limit]
        log.info("gathered %d candidates; fetching newest %d", len(candidates), len(chosen))

        by_folder: dict = {}
        for _d, folder, uv, uid in chosen:
            by_folder.setdefault((folder, uv), []).append(uid)
        out = []
        for (folder, uv), uids in by_folder.items():
            c.select_folder(folder, readonly=True)
            fetched = c.fetch(uids, ["RFC822"])
            for uid in uids:
                raw = fetched.get(uid, {}).get(b"RFC822")
                if raw:
                    out.append((folder, uv, uid, raw))
        return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=500)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = config.Config.load()
    if not cfg.imap_user or not cfg.pg_dsn:
        raise SystemExit("imap_user and pg_dsn required")
    conn = db.connect(cfg.pg_dsn)
    db.init_schema(conn)
    items = gather(cfg, args.limit)
    new = 0
    for i, (folder, uv, uid, raw) in enumerate(items, 1):
        try:
            rec = process_raw(raw)
            raw_path, files = store.save_message(
                cfg.data_dir, rec["identity"], raw, rec["attachments"],
                cfg.public_base_url, cfg.api_token,
            )
            if db.insert_message(conn, rec, folder, uid, uv, raw_path, files):
                new += 1
        except Exception:
            log.exception("failed on %s/%s", folder, uid)
        if i % 50 == 0:
            log.info("processed %d/%d (new=%d)", i, len(items), new)
    log.info("backfill done: %d processed, %d new", len(items), new)


if __name__ == "__main__":
    main()
