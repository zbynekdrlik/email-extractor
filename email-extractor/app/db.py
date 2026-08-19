"""PostgreSQL layer: schema, dedup, message/attachment inserts, IMAP folder state."""
from __future__ import annotations

import psycopg
from psycopg.types.json import Json

from . import mailparse, migrate
from .db_schema import SCHEMA

# --- #314 (revision 2): non-warehouse supplier memory ------------------------------
# The suppliers the warehouse marked "Netýka sa skladu" (#307), remembered so their LATER
# mails stop generating board questions (see app/orders/dl_nonwarehouse.py). Keyed on the
# supplier's OWN identity from the document — registry EAN ∪ normalized name — with the
# email a stored fallback only. The UNIQUE (supplier_ean, name_key, sender_email) index is
# the dedup contract remember()'s ON CONFLICT relies on; empty strings collide as equal,
# which is DELIBERATE here (that IS the dedup — one row per identity), the opposite of the
# #248 case where a blank collision was a bug.
DL_NONWAREHOUSE_SUPPLIER = [
    """
    CREATE TABLE IF NOT EXISTS dl_nonwarehouse_supplier (
        id           BIGSERIAL PRIMARY KEY,
        supplier_ean TEXT NOT NULL DEFAULT '',
        name_key     TEXT NOT NULL DEFAULT '',
        sender_email TEXT NOT NULL DEFAULT '',
        name         TEXT NOT NULL DEFAULT '',
        marked_by    TEXT NOT NULL DEFAULT 'sklad',
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_dl_nonwarehouse_supplier_identity "
    "ON dl_nonwarehouse_supplier (supplier_ean, name_key, sender_email)",
    "CREATE INDEX IF NOT EXISTS idx_dl_nonwarehouse_supplier_ean "
    "ON dl_nonwarehouse_supplier (supplier_ean) WHERE supplier_ean <> ''",
    "CREATE INDEX IF NOT EXISTS idx_dl_nonwarehouse_supplier_name "
    "ON dl_nonwarehouse_supplier (name_key) WHERE name_key <> ''",
]


# --- #327 (revision 3): one-time cleanup of duplicate HELD operator alerts -----------
# Before #327, dl_alerts.already_pending() deduped every pending_alerts row (delivered or
# not) only WITHIN DEDUP_WINDOW_HOURS — so a HELD channel-0 alert (no ops channel configured
# yet, the #310 hold) that never resolves had its dedup window expire and the #308 sweep
# re-enqueued the same message every ~4h, piling up duplicate held rows (live #319: 65 held
# rows for only 10 distinct messages). The predicate fix (dl_alerts.already_pending, now
# delivered_at-aware) stops NEW duplicates; this one-time revision removes the ALREADY-
# accumulated ones — keeping the OLDEST row (min(id)) per (kind, message_id), deleting the
# newer duplicates. Deliberately NARROW, mirroring dl_alerts.purge_held (#319): only
# channel_id=0 AND delivered_at IS NULL AND a non-NULL message_id. A REAL-channel undelivered
# row (a genuine Odoo delivery failure) is NEVER touched; a NULL-message_id alert (e.g.
# spend_cap) is legitimately never deduped and left alone. Runs exactly once (schema_version
# ledger); after the fix no new held duplicates form, so this stays a one-shot cleanup.
DEDUP_HELD_ALERTS = [
    """
    DELETE FROM pending_alerts
    WHERE channel_id = 0
      AND delivered_at IS NULL
      AND message_id IS NOT NULL
      AND id NOT IN (
          SELECT min(id) FROM pending_alerts
          WHERE channel_id = 0 AND delivered_at IS NULL AND message_id IS NOT NULL
          GROUP BY kind, message_id
      )
    """,
]


# --- #331 (revision 4): drop the dead legacy `processed` table --------------------
# `processed` was the original 2026-06-25 n8n-era contract ("each terminal n8n workflow
# writes message_id"); the Python engines took over the categories long ago and NOTHING
# reads or writes it any more — 0 rows over its whole history, no app/test reference, absent
# from conftest's TRUNCATE list, and none of the live n8n workflows touch it. The real
# "processed" contract today is the messages.processed column + the email_events rollup
# (n8n reads via `status`). The CREATE was also removed from db_schema.py's baseline, so on a
# fresh/self-healing DB the table is never created and this DROP is a plain IF EXISTS no-op;
# on a pre-drop prod DB (ledger at r3) this is the ONE statement that removes it.
DROP_PROCESSED = [
    "DROP TABLE IF EXISTS processed",
]


# --- #342 (revision 5): CODEX order evidence + the List-Unsubscribe promo signal --------
# `codex_orders` is a thin slice of the order HEADERS a `codex-bridge` push tool
# (`tools/codex_orders_push.py`) reads read-only from the ERP's DuckDB and POSTs to the
# add-on. It exists SOLELY as EVIDENCE that the warehouse already entered an order manually
# in CODEX, so the worker can auto-resolve an open `mail`-kind board question ("je toto
# vôbec objednávka?") instead of leaving it to nag the nástenka. PK = order number (the
# push is idempotent, upserting by that key). `customer_ean` is the customer's EDI EAN,
# bridged on the dev side via `raw.firma` (NICO → AEDIEAN) — the SAME identity the add-on's
# own customer cards carry (`customer_snapshot.ean_edi`), so the sweep matches on an exact
# string, no IČO column needed here (the boundary from #337 stays: CODEX docs are evidence,
# never a product/card import). The `messages.list_unsubscribe` column captures the mail's
# bulk-mail header at ingest so the pipeline can route an obvious promo flyer to
# no_processing instead of asking "is this an order?" at all (#342 req 5).
CODEX_ORDERS = [
    """
    CREATE TABLE IF NOT EXISTS codex_orders (
        order_number   BIGINT PRIMARY KEY,
        customer_nico  BIGINT,
        customer_ean   TEXT NOT NULL,
        customer_name  TEXT,
        issue_date     DATE,
        delivery_date  DATE,
        line_count     INTEGER,
        updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # The sweep looks up "does this customer have a CODEX order issued on/after the mail's
    # date" — keyed on (customer_ean, issue_date), the exact predicate it filters on.
    "CREATE INDEX IF NOT EXISTS idx_codex_orders_customer "
    "ON codex_orders (customer_ean, issue_date)",
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS list_unsubscribe TEXT",
]

# #352: the daily order digest was removed in #347 (v0.9.112); its send-dedup claim
# table `order_digest_sent` is now a pure orphan (zero readers/writers). Drop it via a
# versioned revision and remove its CREATE from the frozen baseline (same shape as #331's
# `drop_processed_table`). IF EXISTS makes it a no-op on a fresh DB.
DROP_ORDER_DIGEST_SENT = [
    "DROP TABLE IF EXISTS order_digest_sent",
]


# SCHEMA above is FROZEN as revision 1 (the baseline). NEVER edit those statements for a
# schema change — append a NEW numbered migrate.Revision to this list instead
# (immutable-migrations, #269). run_migrations() applies only the unapplied revisions, in
# order, each in its own transaction, and records them in the schema_version ledger.
REVISIONS = [
    migrate.Revision(migrate.BASELINE_REVISION, "baseline", SCHEMA),
    migrate.Revision(2, "add_dl_nonwarehouse_supplier", DL_NONWAREHOUSE_SUPPLIER),
    migrate.Revision(3, "dedup_held_channel0_alerts", DEDUP_HELD_ALERTS),
    migrate.Revision(4, "drop_processed_table", DROP_PROCESSED),
    migrate.Revision(5, "add_codex_orders", CODEX_ORDERS),
    migrate.Revision(6, "drop_order_digest_sent", DROP_ORDER_DIGEST_SENT),
]


def connect(dsn: str):
    return psycopg.connect(dsn, autocommit=True)


def init_schema(conn) -> list[int]:
    """Bring the DB up to the latest schema revision (see app/migrate.py).

    Returns the revision ids applied on this call (``[]`` when already up-to-date).
    Every historical caller (main.py boot, backfill.py and the orders CLI tools,
    conftest) keeps calling this unchanged — they ignore the return value and now get
    versioning + the O(1) up-to-date fast path for free instead of re-running ~100 DDL
    statements each time.
    """
    return migrate.run_migrations(conn, REVISIONS)


def log_event(conn, message_id: str, workflow: str, stage: str, status: str,
              outcome: str = "", detail: dict | None = None, rollup: bool = True) -> None:
    """Append one processing-timeline row.

    rollup=True (pipeline events): the trigger rolls the state onto messages.
    rollup=False (operator/audit events: reclassify, fix, resolve): timeline-only,
    so a dashboard action never overwrites the pipeline-owned proc_* state.
    """
    conn.execute(
        """INSERT INTO email_events (message_id, workflow, stage, status, outcome, detail, rollup)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (message_id, workflow, stage, status, outcome,
         Json(detail) if detail is not None else None, rollup),
    )


def get_folder_state(conn, folder: str) -> tuple[int | None, int]:
    row = conn.execute(
        "SELECT uidvalidity, last_uid FROM folder_state WHERE folder = %s", (folder,)
    ).fetchone()
    return (row[0], row[1]) if row else (None, 0)


def set_folder_state(conn, folder: str, uidvalidity: int, last_uid: int) -> None:
    conn.execute(
        """
        INSERT INTO folder_state (folder, uidvalidity, last_uid)
        VALUES (%s, %s, %s)
        ON CONFLICT (folder) DO UPDATE SET uidvalidity = EXCLUDED.uidvalidity,
                                           last_uid = EXCLUDED.last_uid
        """,
        (folder, uidvalidity, last_uid),
    )


# How many polls a failing UID is retried before the watermark is allowed past it
# (#20). Lives here so both the ingest loop and the dashboard API can state it.
MAX_UID_ATTEMPTS = 5

# A claim (messages.processing_at) younger than this means a worker is really
# working on that email — a guard so operator actions never clear a claim inside it
# (#25). This value (10) is NOT provably matched to any one worker's own re-claim
# window any more (#271, review finding, 2026-08-13): a live check of the n8n side
# found the "AI auto orders" workflow's own re-claim window is 30 minutes (matching
# `worker.CLAIM_STALE_MINUTES`, pinned by `tests/test_orders_worker.py::
# test_claim_stale_minutes_matches_n8n_ai_orders_window`), not 10 — so this constant
# guards ALL categories' claims with one number that does not actually equal any
# single category's real re-claim window. Whether this operator-guard value should
# be revisited is tracked as part of the SAME open follow-up as the related
# static_orders divergence (`.claude/rules/orders-corpus.md`'s CLAIM_STALE_MINUTES
# entry, needs-user-decision) — not changed here.
CLAIM_STALE_MINUTES = 10


def active_claim(conn, mid: int):
    """Return processing_at when a worker currently holds this message, else None."""
    row = conn.execute(
        """SELECT processing_at FROM messages
           WHERE id = %s AND processed = false AND processing_at IS NOT NULL
             AND processing_at > now() - (%s || ' minutes')::interval""",
        (mid, CLAIM_STALE_MINUTES),
    ).fetchone()
    return row[0] if row else None


def record_uid_failure(conn, folder: str, uidvalidity: int, uid: int, err: str) -> int:
    """Remember that this UID failed to ingest; return how many times it has failed."""
    return conn.execute(
        """
        INSERT INTO imap_failures (folder, uidvalidity, uid, last_error)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (folder, uidvalidity, uid) DO UPDATE
            SET attempts = imap_failures.attempts + 1,
                last_seen = now(),
                last_error = EXCLUDED.last_error
        RETURNING attempts
        """,
        (folder, uidvalidity, uid, (err or "")[:2000]),
    ).fetchone()[0]


def mark_uid_skipped(conn, folder: str, uidvalidity: int, uid: int) -> None:
    """Give up on this UID (watermark may pass it) but keep it on record."""
    conn.execute(
        "UPDATE imap_failures SET skipped = true WHERE folder=%s AND uidvalidity=%s AND uid=%s",
        (folder, uidvalidity, uid),
    )


def clear_uid_failure(conn, folder: str, uidvalidity: int, uid: int) -> None:
    conn.execute(
        "DELETE FROM imap_failures WHERE folder=%s AND uidvalidity=%s AND uid=%s",
        (folder, uidvalidity, uid),
    )


def list_uid_failures(conn, limit: int = 200) -> list[dict]:
    rows = conn.execute(
        """SELECT folder, uidvalidity, uid, attempts, skipped, first_seen, last_seen, last_error
           FROM imap_failures ORDER BY skipped DESC, last_seen DESC LIMIT %s""",
        (limit,),
    ).fetchall()
    return [{
        "folder": r[0], "uidvalidity": r[1], "uid": r[2], "attempts": r[3], "skipped": r[4],
        "first_seen": r[5].isoformat() if r[5] else None,
        "last_seen": r[6].isoformat() if r[6] else None,
        "last_error": r[7],
    } for r in rows]


def count_uid_failures(conn) -> tuple[int, int]:
    """(pending, skipped) — exact counts, independent of list_uid_failures' limit."""
    row = conn.execute(
        """SELECT count(*) FILTER (WHERE NOT skipped), count(*) FILTER (WHERE skipped)
           FROM imap_failures""").fetchone()
    return (row[0], row[1])


def retire_stale_uid_failures(conn, folder: str, uidvalidity: int) -> int:
    """The mailbox was re-numbered, so pending UIDs from the previous UIDVALIDITY can
    never be retried — mark them skipped instead of showing them as 'still retrying'
    forever. They stay on record (that is the point of the table)."""
    return conn.execute(
        """UPDATE imap_failures SET skipped = true
           WHERE folder = %s AND uidvalidity <> %s AND NOT skipped""",
        (folder, uidvalidity),
    ).rowcount


def _no_nul(v):
    """Strip NUL (0x00) bytes from str/list values — Postgres text columns reject
    them, and a weak scan's PDF text layer occasionally contains one (2026-07-15:
    a scanned DL failed to ingest on every poll cycle with DataError)."""
    if isinstance(v, str):
        return v.replace("\x00", "")
    if isinstance(v, list):
        return [_no_nul(x) for x in v]
    return v


def insert_message(conn, rec: dict, folder: str, uid: int, uidvalidity: int,
                   raw_path: str, att_files: list[dict]) -> bool:
    """Insert one email + its attachments. Returns False if already present (dedup)."""
    h = rec["headers"]
    content_sig = mailparse.content_signature(
        h.get("from_addr"), h.get("subject"), rec.get("combined_text") or "")
    row = conn.execute(
        """
        INSERT INTO messages (message_id, header_message_id, folder, imap_uid,
            imap_uidvalidity, from_addr, from_name, to_addrs, cc_addrs, subject,
            sent_at, body_text, body_source, combined_text, has_attachments,
            needs_vision, raw_eml_path, content_sig, list_unsubscribe)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (message_id) DO NOTHING
        RETURNING id
        """,
        tuple(_no_nul(p) for p in (
            rec["identity"], h.get("message_id"), folder, uid, uidvalidity,
            h.get("from_addr"), h.get("from_name"), h.get("to_addrs"), h.get("cc_addrs"),
            h.get("subject"), h.get("date"), rec["body_text"], rec["body_source"],
            rec["combined_text"], rec["has_attachments"], rec["needs_vision"], raw_path,
            content_sig, h.get("list_unsubscribe"))),
    ).fetchone()
    if not row:
        return False
    files = {f["idx"]: f for f in att_files}
    for i, a in enumerate(rec["attachments"]):
        f = files.get(i, {})
        conn.execute(
            """
            INSERT INTO attachments (message_id, idx, filename, mime, size, sha256,
                method, ocr_conf, pages, chars, needs_vision, flag, file_path,
                file_url, extracted_text)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            tuple(_no_nul(p) for p in (
                rec["identity"], i, a.get("filename"), a.get("mime"), a.get("size"),
                f.get("sha256"), a.get("method"), a.get("ocr_conf"), a.get("pages"),
                a.get("chars"), a.get("needs_vision"), a.get("flag"), f.get("path"),
                f.get("url"), a.get("text"))),
        )
    # Start the processing timeline (rollup=False: keep proc_status NULL/'nové' —
    # the email isn't processed yet, just ingested).
    n_att = len(rec["attachments"])
    log_event(conn, rec["identity"], "extractor", "ingested", "ok",
              outcome=f"prijaté + extrahované ({n_att} príloh)"
                      + (", potrebuje AI Vision" if rec.get("needs_vision") else ""),
              detail={"attachments": n_att, "needs_vision": bool(rec.get("needs_vision")),
                      "folder": folder},
              rollup=False)
    return True
