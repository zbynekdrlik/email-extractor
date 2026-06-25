"""PostgreSQL layer: schema, dedup, message/attachment inserts, IMAP folder state."""
from __future__ import annotations

import psycopg

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
]


def connect(dsn: str):
    return psycopg.connect(dsn, autocommit=True)


def init_schema(conn) -> None:
    for stmt in SCHEMA:
        conn.execute(stmt)


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
    row = conn.execute(
        """
        INSERT INTO messages (message_id, header_message_id, folder, imap_uid,
            imap_uidvalidity, from_addr, from_name, to_addrs, cc_addrs, subject,
            sent_at, body_text, body_source, combined_text, has_attachments,
            needs_vision, raw_eml_path)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (message_id) DO NOTHING
        RETURNING id
        """,
        (rec["identity"], h.get("message_id"), folder, uid, uidvalidity,
         h.get("from_addr"), h.get("from_name"), h.get("to_addrs"), h.get("cc_addrs"),
         h.get("subject"), h.get("date"), rec["body_text"], rec["body_source"],
         rec["combined_text"], rec["has_attachments"], rec["needs_vision"], raw_path),
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
