"""Entry point: poll IMAP -> extract -> store files -> upsert Postgres, on a loop."""
from __future__ import annotations

import logging
import time

import psycopg

from . import __version__, config, db, httpapi, imap_poll, store
from .process import process_raw

log = logging.getLogger("email-extractor")

# Retrying protects against transient errors (OCR OOM, Postgres hiccup); giving up
# after MAX_UID_ATTEMPTS keeps one permanently broken email from wedging the whole
# folder. The UID stays in imap_failures as skipped=true either way — never a silent
# loss (#20). Defined in db.py so the dashboard API can state the same number.
MAX_UID_ATTEMPTS = db.MAX_UID_ATTEMPTS


def run_once(cfg, conn) -> int:
    new_count = 0
    for folder in cfg.folders:
        prev_validity, prev_uid = db.get_folder_state(conn, folder)
        try:
            uidvalidity, msgs = imap_poll.poll_folder(cfg, conn, folder)
        except Exception as e:
            log.error("poll failed for folder %s: %s", folder, e)
            continue
        # A mailbox re-numbering resets the watermark; persist the new UIDVALIDITY
        # even when nothing (or nothing successful) came out of this poll, or the
        # rescan is re-detected on every cycle.
        base_uid = prev_uid if prev_validity == uidvalidity else 0
        if prev_validity != uidvalidity:
            retired = db.retire_stale_uid_failures(conn, folder, uidvalidity)
            if retired:
                log.warning("%s was re-numbered: %d unreceived email(s) from the previous "
                            "UIDVALIDITY can no longer be retried (kept on record)",
                            folder, retired)
        if not msgs:
            if prev_validity != uidvalidity:
                db.set_folder_state(conn, folder, uidvalidity, base_uid)
            continue
        # The watermark may only cover an unbroken run of successfully handled UIDs:
        # everything from the first still-retryable failure upward is re-fetched next
        # poll (already-stored emails dedup on message_id, so re-reads are harmless).
        done_through = base_uid
        blocked = False
        for uid, raw in sorted(msgs, key=lambda m: m[0]):
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
                db.clear_uid_failure(conn, folder, uidvalidity, uid)
                if not blocked:
                    done_through = max(done_through, uid)
            except Exception as e:
                log.exception("failed to process uid=%s in %s", uid, folder)
                attempts = db.record_uid_failure(conn, folder, uidvalidity, uid, repr(e))
                if attempts > MAX_UID_ATTEMPTS:
                    db.mark_uid_skipped(conn, folder, uidvalidity, uid)
                    log.error("GIVING UP on uid=%s in %s after %d attempts (%s) — "
                              "recorded in imap_failures, visible on the dashboard; "
                              "this email was NOT ingested",
                              uid, folder, attempts, e)
                    if not blocked:
                        done_through = max(done_through, uid)
                else:
                    log.error("uid=%s in %s will be retried (attempt %d/%d)",
                              uid, folder, attempts, MAX_UID_ATTEMPTS)
                    blocked = True
        db.set_folder_state(conn, folder, uidvalidity, done_through)
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
    conn = db.connect(cfg.pg_dsn)
    db.init_schema(conn)          # run migrations BEFORE the dashboard serves requests
    httpapi.start(cfg)
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
