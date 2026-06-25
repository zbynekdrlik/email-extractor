"""Entry point: poll IMAP -> extract -> store files -> upsert Postgres, on a loop."""
from __future__ import annotations

import logging
import time

import psycopg

from . import __version__, config, db, httpapi, imap_poll, store
from .process import process_raw

log = logging.getLogger("email-extractor")


def run_once(cfg, conn) -> int:
    new_count = 0
    for folder in cfg.folders:
        try:
            uidvalidity, msgs = imap_poll.poll_folder(cfg, conn, folder)
        except Exception as e:
            log.error("poll failed for folder %s: %s", folder, e)
            continue
        if not msgs:
            continue
        max_uid = 0
        for uid, raw in msgs:
            try:
                rec = process_raw(raw)
                raw_path, files = store.save_message(
                    cfg.data_dir, rec["identity"], raw, rec["attachments"],
                    cfg.public_base_url, cfg.api_token,
                )
                if db.insert_message(conn, rec, folder, uid, uidvalidity, raw_path, files):
                    new_count += 1
                    log.info("stored %s [%s] atts=%d needs_vision=%s",
                             rec["identity"][:60], folder, len(rec["attachments"]),
                             rec["needs_vision"])
            except Exception:
                log.exception("failed to process uid=%s in %s", uid, folder)
            max_uid = max(max_uid, uid)
        db.set_folder_state(conn, folder, uidvalidity, max_uid)
    return new_count


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = config.Config.load()
    if not cfg.imap_user or not cfg.imap_pass or not cfg.pg_dsn:
        raise SystemExit("Config error: imap_user, imap_pass and pg_dsn are required "
                         "(set them in the add-on options).")
    log.info("email-extractor %s starting; folders=%s interval=%ss",
             __version__, cfg.folders, cfg.poll_interval)
    httpapi.start(cfg)
    conn = db.connect(cfg.pg_dsn)
    db.init_schema(conn)
    while True:
        try:
            n = run_once(cfg, conn)
            if n:
                log.info("cycle complete: %d new message(s)", n)
        except psycopg.OperationalError as e:
            log.error("database connection lost (%s); reconnecting...", e)
            try:
                conn = db.connect(cfg.pg_dsn)
            except Exception as e2:
                log.error("reconnect failed: %s", e2)
        except Exception:
            log.exception("cycle error")
        time.sleep(cfg.poll_interval)


if __name__ == "__main__":
    main()
