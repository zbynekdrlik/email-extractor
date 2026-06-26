"""PostgreSQL layer: schema, dedup, message/attachment inserts, IMAP folder state."""
from __future__ import annotations

import psycopg
from psycopg.types.json import Json

from . import mailparse

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS messages (
        id              BIGSERIAL PRIMARY KEY,
        message_id      TEXT UNIQUE NOT NULL,
        header_message_id TEXT,
        folder          TEXT,
        imap_uid        BIGINT,
        imap_uidvalidity BIGINT,
        from_addr       TEXT,
        from_name       TEXT,
        to_addrs        TEXT[],
        cc_addrs        TEXT[],
        subject         TEXT,
        sent_at         TEXT,
        body_text       TEXT,
        body_source     TEXT,
        combined_text   TEXT,
        has_attachments BOOLEAN DEFAULT FALSE,
        needs_vision    BOOLEAN DEFAULT FALSE,
        category        TEXT,
        classified_at   TIMESTAMPTZ,
        original_category TEXT,
        human_reviewed  BOOLEAN NOT NULL DEFAULT FALSE,
        review_status   TEXT,
        corrected_at    TIMESTAMPTZ,
        processed       BOOLEAN NOT NULL DEFAULT FALSE,
        processed_by    TEXT,
        processing_at   TIMESTAMPTZ,
        content_sig     TEXT,
        status          TEXT DEFAULT 'new',
        error           TEXT,
        raw_eml_path    TEXT,
        created_at      TIMESTAMPTZ DEFAULT now(),
        processed_at    TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS attachments (
        id              BIGSERIAL PRIMARY KEY,
        message_id      TEXT NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
        idx             INTEGER,
        filename        TEXT,
        mime            TEXT,
        size            BIGINT,
        sha256          TEXT,
        method          TEXT,
        ocr_conf        REAL,
        pages           INTEGER,
        chars           INTEGER,
        needs_vision    BOOLEAN DEFAULT FALSE,
        flag            TEXT,
        file_path       TEXT,
        file_url        TEXT,
        extracted_text  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS processed (
        id           BIGSERIAL PRIMARY KEY,
        message_id   TEXT NOT NULL,
        handled_by   TEXT,
        category     TEXT,
        result       TEXT,
        processed_at TIMESTAMPTZ DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS folder_state (
        folder       TEXT PRIMARY KEY,
        uidvalidity  BIGINT,
        last_uid     BIGINT DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status)",
    # --- telemetry: per-email processing timeline (one row per step) ---
    """
    CREATE TABLE IF NOT EXISTS email_events (
        id          BIGSERIAL PRIMARY KEY,
        message_id  TEXT NOT NULL,
        ts          TIMESTAMPTZ DEFAULT now(),
        workflow    TEXT,
        stage       TEXT,
        status      TEXT,
        outcome     TEXT,
        detail      JSONB
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_message ON email_events(message_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_events_status ON email_events(status)",
    "CREATE INDEX IF NOT EXISTS idx_events_stage ON email_events(stage)",
    # rollup=false for operator/audit events (reclassify, fix, ...) so they appear
    # in the timeline but do NOT overwrite the pipeline-owned proc_* state.
    "ALTER TABLE email_events ADD COLUMN IF NOT EXISTS rollup BOOLEAN NOT NULL DEFAULT true",
    # --- denormalized current processing state on messages (cheap list/filter) ---
    """
    ALTER TABLE messages
        ADD COLUMN IF NOT EXISTS proc_status   TEXT,
        ADD COLUMN IF NOT EXISTS proc_stage    TEXT,
        ADD COLUMN IF NOT EXISTS proc_outcome  TEXT,
        ADD COLUMN IF NOT EXISTS last_event_at TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS attempts      INT DEFAULT 0,
        ADD COLUMN IF NOT EXISTS edi_file      TEXT,
        ADD COLUMN IF NOT EXISTS orion_path    TEXT,
        ADD COLUMN IF NOT EXISTS odoo_url      TEXT,
        ADD COLUMN IF NOT EXISTS forwarded_to  TEXT
    """,
    # --- self-healing migration for columns added after the initial 2026-06-25
    # deploy: the live prod DB predates several columns and CREATE TABLE IF NOT
    # EXISTS is a no-op on it, so add every non-original column idempotently.
    """
    ALTER TABLE messages
        ADD COLUMN IF NOT EXISTS header_message_id TEXT,
        ADD COLUMN IF NOT EXISTS folder            TEXT,
        ADD COLUMN IF NOT EXISTS imap_uid          BIGINT,
        ADD COLUMN IF NOT EXISTS imap_uidvalidity  BIGINT,
        ADD COLUMN IF NOT EXISTS from_addr         TEXT,
        ADD COLUMN IF NOT EXISTS from_name         TEXT,
        ADD COLUMN IF NOT EXISTS to_addrs          TEXT[],
        ADD COLUMN IF NOT EXISTS cc_addrs          TEXT[],
        ADD COLUMN IF NOT EXISTS subject           TEXT,
        ADD COLUMN IF NOT EXISTS sent_at           TEXT,
        ADD COLUMN IF NOT EXISTS body_text         TEXT,
        ADD COLUMN IF NOT EXISTS body_source       TEXT,
        ADD COLUMN IF NOT EXISTS combined_text     TEXT,
        ADD COLUMN IF NOT EXISTS has_attachments   BOOLEAN DEFAULT FALSE,
        ADD COLUMN IF NOT EXISTS needs_vision      BOOLEAN DEFAULT FALSE,
        ADD COLUMN IF NOT EXISTS category          TEXT,
        ADD COLUMN IF NOT EXISTS classified_at     TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS original_category TEXT,
        ADD COLUMN IF NOT EXISTS human_reviewed    BOOLEAN NOT NULL DEFAULT FALSE,
        ADD COLUMN IF NOT EXISTS review_status     TEXT,
        ADD COLUMN IF NOT EXISTS corrected_at      TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS processed         BOOLEAN NOT NULL DEFAULT FALSE,
        ADD COLUMN IF NOT EXISTS processed_by      TEXT,
        ADD COLUMN IF NOT EXISTS processing_at     TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS content_sig       TEXT,
        ADD COLUMN IF NOT EXISTS status            TEXT DEFAULT 'new',
        ADD COLUMN IF NOT EXISTS error             TEXT,
        ADD COLUMN IF NOT EXISTS raw_eml_path      TEXT,
        ADD COLUMN IF NOT EXISTS created_at        TIMESTAMPTZ DEFAULT now(),
        ADD COLUMN IF NOT EXISTS processed_at      TIMESTAMPTZ
    """,
    """
    ALTER TABLE attachments
        ADD COLUMN IF NOT EXISTS idx            INTEGER,
        ADD COLUMN IF NOT EXISTS filename       TEXT,
        ADD COLUMN IF NOT EXISTS mime           TEXT,
        ADD COLUMN IF NOT EXISTS size           BIGINT,
        ADD COLUMN IF NOT EXISTS sha256         TEXT,
        ADD COLUMN IF NOT EXISTS method         TEXT,
        ADD COLUMN IF NOT EXISTS ocr_conf       REAL,
        ADD COLUMN IF NOT EXISTS pages          INTEGER,
        ADD COLUMN IF NOT EXISTS chars          INTEGER,
        ADD COLUMN IF NOT EXISTS needs_vision   BOOLEAN DEFAULT FALSE,
        ADD COLUMN IF NOT EXISTS flag           TEXT,
        ADD COLUMN IF NOT EXISTS file_path      TEXT,
        ADD COLUMN IF NOT EXISTS file_url       TEXT,
        ADD COLUMN IF NOT EXISTS extracted_text TEXT
    """,
    # --- rollup: every email_events INSERT updates the messages denorm state ---
    # No-op (zero rows) when the messages row is absent, so it never raises.
    """
    CREATE OR REPLACE FUNCTION email_events_rollup() RETURNS trigger AS $func$
    BEGIN
        IF NEW.rollup THEN
            UPDATE messages SET
                proc_stage    = NEW.stage,
                proc_status   = NEW.status,
                proc_outcome  = NEW.outcome,
                last_event_at = NEW.ts,
                attempts      = COALESCE(attempts, 0)
                                + CASE WHEN NEW.stage = 'claimed' THEN 1 ELSE 0 END,
                edi_file      = COALESCE(NEW.detail->>'edi_file', edi_file),
                orion_path    = COALESCE(NEW.detail->>'orion_path', orion_path),
                odoo_url      = COALESCE(NEW.detail->>'odoo_url', odoo_url),
                forwarded_to  = COALESCE(NEW.detail->>'forwarded_to', forwarded_to)
            WHERE message_id = NEW.message_id;
        END IF;
        RETURN NEW;
    END;
    $func$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS trg_email_events_rollup ON email_events",
    """
    CREATE TRIGGER trg_email_events_rollup
        AFTER INSERT ON email_events
        FOR EACH ROW EXECUTE FUNCTION email_events_rollup()
    """,
    # --- fix queue: emails the user flagged for Claude to fix ---
    """
    CREATE TABLE IF NOT EXISTS fix_requests (
        id                BIGSERIAL PRIMARY KEY,
        message_id        TEXT NOT NULL,
        problem_type      TEXT,
        expected_category TEXT,
        description       TEXT,
        status            TEXT DEFAULT 'open',
        snapshot          JSONB,
        created_at        TIMESTAMPTZ DEFAULT now(),
        created_by        TEXT,
        resolved_at       TIMESTAMPTZ,
        resolution        TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_fix_status ON fix_requests(status)",
    "CREATE INDEX IF NOT EXISTS idx_fix_message ON fix_requests(message_id)",
]


def connect(dsn: str):
    return psycopg.connect(dsn, autocommit=True)


def init_schema(conn) -> None:
    for stmt in SCHEMA:
        conn.execute(stmt)


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


def insert_message(conn, rec: dict, folder: str, uid: int, uidvalidity: int,
                   raw_path: str, att_files: list[dict]) -> bool:
    """Insert one email + its attachments. Returns False if already present (dedup)."""
    h = rec["headers"]
    content_sig = mailparse.content_signature(
        h.get("from_addr"), h.get("subject"), rec.get("combined_text"))
    row = conn.execute(
        """
        INSERT INTO messages (message_id, header_message_id, folder, imap_uid,
            imap_uidvalidity, from_addr, from_name, to_addrs, cc_addrs, subject,
            sent_at, body_text, body_source, combined_text, has_attachments,
            needs_vision, raw_eml_path, content_sig)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (message_id) DO NOTHING
        RETURNING id
        """,
        (rec["identity"], h.get("message_id"), folder, uid, uidvalidity,
         h.get("from_addr"), h.get("from_name"), h.get("to_addrs"), h.get("cc_addrs"),
         h.get("subject"), h.get("date"), rec["body_text"], rec["body_source"],
         rec["combined_text"], rec["has_attachments"], rec["needs_vision"], raw_path,
         content_sig),
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
            (rec["identity"], i, a.get("filename"), a.get("mime"), a.get("size"),
             f.get("sha256"), a.get("method"), a.get("ocr_conf"), a.get("pages"),
             a.get("chars"), a.get("needs_vision"), a.get("flag"), f.get("path"),
             f.get("url"), a.get("text")),
        )
    return True
