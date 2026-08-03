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
        ADD COLUMN IF NOT EXISTS forwarded_to  TEXT,
        ADD COLUMN IF NOT EXISTS alerted_stuck BOOLEAN NOT NULL DEFAULT false
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
    # --- sorter classification timeline event (DB-side: no n8n change needed).
    # Fires when the sorter UPDATEs category; rollup=false so it's a timeline
    # marker that doesn't set proc_status (the email isn't processed yet). ---
    """
    CREATE OR REPLACE FUNCTION messages_classified_event() RETURNS trigger AS $func$
    BEGIN
        IF NEW.category IS NOT NULL AND (OLD.category IS DISTINCT FROM NEW.category) THEN
            INSERT INTO email_events (message_id, workflow, stage, status, outcome, detail, rollup)
            VALUES (NEW.message_id, 'sorter', 'classified', 'ok',
                    'zaradené: ' || NEW.category,
                    jsonb_build_object('category', NEW.category), false);
        END IF;
        RETURN NEW;
    END;
    $func$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS trg_messages_classified ON messages",
    """
    CREATE TRIGGER trg_messages_classified
        AFTER UPDATE OF category ON messages
        FOR EACH ROW EXECUTE FUNCTION messages_classified_event()
    """,
    # --- #22: drop the API token that used to be baked into every stored file_url.
    # It leaked through any DB dump/backup and rotating the token invalidated every
    # historical URL. Idempotent (runs on every start); nothing reads file_url with a
    # token — n8n builds its own URLs and authenticates with the X-Token header. ---
    """
    UPDATE attachments
       SET file_url = split_part(file_url, '?', 1)
     WHERE file_url LIKE '%?token=%'
    """,
    # --- emails that failed to ingest (#20). The IMAP watermark stops below a
    # failed UID so it is retried; after MAX_UID_ATTEMPTS it is passed over and
    # kept here as skipped=true, so a broken email can neither be lost silently
    # nor wedge the folder for every later email. ---
    """
    CREATE TABLE IF NOT EXISTS imap_failures (
        folder      TEXT   NOT NULL,
        uidvalidity BIGINT NOT NULL,
        uid         BIGINT NOT NULL,
        attempts    INT    NOT NULL DEFAULT 1,
        skipped     BOOLEAN NOT NULL DEFAULT false,
        first_seen  TIMESTAMPTZ DEFAULT now(),
        last_seen   TIMESTAMPTZ DEFAULT now(),
        last_error  TEXT,
        PRIMARY KEY (folder, uidvalidity, uid)
    )
    """,
    # --- #59: frozen catalog + customer list for the order pipeline. The n8n version
    # reads the Google Sheet live on every run, so the same email gives different
    # results on different days and no regression test can exist. A snapshot is
    # content-addressed and immutable: a run records the id it used and stays
    # replayable after the sheet changes. ---
    """
    CREATE TABLE IF NOT EXISTS order_snapshots (
        id             BIGSERIAL PRIMARY KEY,
        content_sha256 TEXT NOT NULL,
        catalog_rows   INT  NOT NULL,
        customer_rows  INT  NOT NULL,
        imported_at    TIMESTAMPTZ DEFAULT now(),
        checked_at     TIMESTAMPTZ DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_order_snapshots_hash ON order_snapshots(content_sha256)",
    """
    CREATE TABLE IF NOT EXISTS catalog_snapshot (
        snapshot_id BIGINT NOT NULL REFERENCES order_snapshots(id) ON DELETE CASCADE,
        gtin        TEXT   NOT NULL,
        name        TEXT   NOT NULL,
        alias       TEXT,
        PRIMARY KEY (snapshot_id, gtin)
    )
    """,
    # No unique key on ean_edi: the sheet legitimately holds rows with an empty EAN and
    # several branches sharing one, so a surrogate id is the only safe identity here.
    """
    CREATE TABLE IF NOT EXISTS customer_snapshot (
        id          BIGSERIAL PRIMARY KEY,
        snapshot_id BIGINT NOT NULL REFERENCES order_snapshots(id) ON DELETE CASCADE,
        ean_edi     TEXT,
        name        TEXT NOT NULL,
        emails      TEXT[],
        city        TEXT,
        street      TEXT,
        zip         TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_customer_snapshot_snap ON customer_snapshot(snapshot_id)",
    # --- #127/#128: direct web curation of product cards + customers, as the sole
    # manual-edit source layered ON TOP of the (still-live, #129 turns it off) sheet
    # read. These are NOT snapshotted themselves — they are merged into catalog/
    # customer rows at freeze time (snapshot.py's `_apply_catalog_overrides`/
    # `_apply_customer_overrides`), so a manual edit gets the exact same versioning
    # the sheet already has for free (a new snapshot, old ones stay replayable). ---
    """
    CREATE TABLE IF NOT EXISTS catalog_overrides (
        gtin       TEXT PRIMARY KEY,
        name       TEXT NOT NULL,
        retired    BOOLEAN NOT NULL DEFAULT false,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # Surrogate id, not ean_edi: the sheet legitimately repeats an EAN across branches
    # and can leave it empty (same comment as customer_snapshot above), and #101 showed
    # even an e-mail can belong to two customers at once — so ean_edi is not a safe
    # override identity either. orig_ean_edi/orig_street pin the ORIGINAL sheet row an
    # override replaces (NULL orig_ean_edi = a brand-new customer, not an edit).
    """
    CREATE TABLE IF NOT EXISTS customer_overrides (
        id           BIGSERIAL PRIMARY KEY,
        orig_ean_edi TEXT,
        orig_street  TEXT,
        ean_edi      TEXT,
        name         TEXT NOT NULL,
        emails       TEXT[],
        city         TEXT,
        street       TEXT,
        zip          TEXT,
        retired      BOOLEAN NOT NULL DEFAULT false,
        updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_customer_overrides_orig "
    "ON customer_overrides(orig_ean_edi, orig_street) WHERE orig_ean_edi IS NOT NULL",
    # --- #60: one row per pipeline run, plus the per-item decision trace. The trace is
    # the reason an item matched or did not; today that lives only in the n8n execution,
    # which n8n prunes after ~2 days, so a warehouse complaint can no longer be
    # diagnosed after the weekend. shadow=true means the run only observed. ---
    """
    CREATE TABLE IF NOT EXISTS order_runs (
        id          BIGSERIAL PRIMARY KEY,
        message_id  TEXT NOT NULL,
        snapshot_id BIGINT REFERENCES order_snapshots(id),
        shadow      BOOLEAN NOT NULL DEFAULT false,
        status      TEXT,
        error       TEXT,
        result      JSONB,
        started_at  TIMESTAMPTZ DEFAULT now(),
        finished_at TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_order_runs_message ON order_runs(message_id, shadow)",
    # What the run cost (#89). Without these the €30/month tripwire cannot exist: the API's
    # usage block was parsed and discarded, so spend was invisible.
    "ALTER TABLE order_runs ADD COLUMN IF NOT EXISTS calls INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE order_runs ADD COLUMN IF NOT EXISTS cached_calls INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE order_runs ADD COLUMN IF NOT EXISTS tokens_in INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE order_runs ADD COLUMN IF NOT EXISTS tokens_cached INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE order_runs ADD COLUMN IF NOT EXISTS tokens_out INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE order_runs ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(10,6) NOT NULL DEFAULT 0",
    "ALTER TABLE order_runs ADD COLUMN IF NOT EXISTS model TEXT",
    "CREATE INDEX IF NOT EXISTS idx_order_runs_month ON order_runs(started_at)",
    """
    CREATE TABLE IF NOT EXISTS order_spend_alerts (
        month    TEXT PRIMARY KEY,
        cost_eur NUMERIC(10,2),
        sent_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS order_items (
        id         BIGSERIAL PRIMARY KEY,
        run_id     BIGINT NOT NULL REFERENCES order_runs(id) ON DELETE CASCADE,
        name       TEXT,
        quantity   NUMERIC,
        unit       TEXT,
        gtin       TEXT,
        card       TEXT,
        confidence REAL,
        rule       TEXT,
        trace      JSONB
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_order_items_run ON order_items(run_id)",
    # --- #63: shipment history per customer + wording. The n8n Data Table it replaces
    # has NO unique key, so one shipment writes a row per item and the JS dedups them —
    # a seed row was once read as 18 deliveries and released the weight override far too
    # early. The key is (customer, wording, card, DAY): re-running one order must never
    # look like a second delivery. ---
    """
    CREATE TABLE IF NOT EXISTS item_memory (
        id           BIGSERIAL PRIMARY KEY,
        customer_ean TEXT NOT NULL,
        item_key     TEXT NOT NULL,
        item_raw     TEXT,
        gtin         TEXT NOT NULL,
        card         TEXT,
        delivered_on DATE NOT NULL,
        source       TEXT,
        created_at   TIMESTAMPTZ DEFAULT now(),
        UNIQUE (customer_ean, item_key, gtin, delivered_on)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_item_memory_lookup ON item_memory(customer_ean, item_key)",
    # --- #88: the teach-once loop. A wording the engine cannot settle becomes ONE question
    # with its candidate cards; the warehouse answers it with a click on the dashboard and the
    # answer lands in item_memory(source='human'), which outranks every model rung. Measured:
    # the whole tail is 15 (customer, wording) pairs, so this closes it. ---
    """
    CREATE TABLE IF NOT EXISTS order_questions (
        id            BIGSERIAL PRIMARY KEY,
        message_id    TEXT NOT NULL,
        customer_ean  TEXT NOT NULL,
        customer_name TEXT,
        wording       TEXT NOT NULL,
        item_key      TEXT NOT NULL,
        quantity      NUMERIC,
        unit          TEXT,
        candidates    JSONB NOT NULL DEFAULT '[]'::jsonb,
        delivery_date TEXT,
        reason        TEXT,
        status        TEXT NOT NULL DEFAULT 'open',
        answer_gtin   TEXT,
        answer_card   TEXT,
        answered_by   TEXT,
        answered_at   TIMESTAMPTZ,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # One OPEN question per (customer, wording): the same nickname in ten emails is one
    # question, and asking twice is the notification noise the user removed everywhere else.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_order_questions_open "
    "ON order_questions(customer_ean, item_key) WHERE status = 'open'",
    # --- #159: a SECOND question kind sharing this same table/index/dashboard — "who is
    # this customer?" instead of "which card is this wording?". `kind='item'` is every
    # existing row (default, unchanged); `kind='customer'` rows always carry
    # customer_ean='' (the customer is exactly what is unknown) and item_key = a
    # normalized key on the SENDER ADDRESS, so the SAME unique-open index above dedupes
    # them for free. `context` carries what a human needs to answer (sender e-mail/name,
    # company name, a best-effort delivery-address line pulled from the mail's own raw
    # text) — never from the model, so it can never touch prompt_hash. ---
    "ALTER TABLE order_questions ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'item'",
    "ALTER TABLE order_questions ADD COLUMN IF NOT EXISTS "
    "context JSONB NOT NULL DEFAULT '{}'::jsonb",
    # --- #102: a wording the ladder cannot place at all (0 catalog matches, e.g. "Twister")
    # is not one customer's nickname — it is a product name, and the answer is the same for
    # every customer. A DEDICATED table, not a sentinel EAN inside item_memory: a sentinel
    # value would have to be remembered by every future customer_ean=%s query site, and could
    # collide with (or be silently excluded from) code that assumes that column always names a
    # real customer. `question_id` traces every row back to the question that created it, so
    # `teach.undo` can retract ONLY the global mapping ITS OWN question created — a different
    # customer's later, redundant answer to the same wording must never be able to erase it. ---
    """
    CREATE TABLE IF NOT EXISTS global_item_memory (
        id           BIGSERIAL PRIMARY KEY,
        item_key     TEXT NOT NULL UNIQUE,
        item_raw     TEXT,
        gtin         TEXT NOT NULL,
        card         TEXT,
        question_id  BIGINT REFERENCES order_questions(id),
        taught_by    TEXT,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # --- #64: ledger of documents actually uploaded to ORION. A duplicate upload creates
    # a duplicate order there and cannot be undone from our side (#51), so the identity
    # (customer, delivery date, content hash) may be claimed exactly once. ---
    """
    CREATE TABLE IF NOT EXISTS edi_sent (
        id             BIGSERIAL PRIMARY KEY,
        customer_ean   TEXT NOT NULL,
        delivery_date  TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        filename       TEXT,
        sent_at        TIMESTAMPTZ DEFAULT now(),
        UNIQUE (customer_ean, delivery_date, content_sha256)
    )
    """,
    # --- #93: hold an order while its question is unanswered, but only until the delivery
    # date. Shipping the matched part now and the taught line later would write TWO ORION
    # documents for one delivery day (#81.1) — so a pending question holds its WHOLE order.
    # Enough of the run is stored as JSONB to ship it later without another LLM call:
    # the customer, the order's own dict (delivery date/order number), the extracted
    # context (isChangeRequest/unverified/notes) and the per-item decisions. ---
    """
    CREATE TABLE IF NOT EXISTS held_orders (
        id              BIGSERIAL PRIMARY KEY,
        message_id      TEXT NOT NULL,
        customer_ean    TEXT NOT NULL,
        customer_name   TEXT,
        delivery_date   TEXT,
        order_number    TEXT,
        store           TEXT,
        recipient_group TEXT,
        question_ids    BIGINT[] NOT NULL DEFAULT '{}',
        order_json      JSONB NOT NULL DEFAULT '{}'::jsonb,
        extracted_json  JSONB NOT NULL DEFAULT '{}'::jsonb,
        decisions_json  JSONB NOT NULL DEFAULT '[]'::jsonb,
        status          TEXT NOT NULL DEFAULT 'held'
                            CHECK (status IN ('held', 'released')),
        release_reason  TEXT CHECK (release_reason IN ('answered', 'deadline')),
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        released_at     TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_held_orders_status ON held_orders(status)",
    # What worker._claim reads to keep a held message out of the re-claim query — without
    # this a message with an open question would look "just claimed a while ago" once the
    # 30-minute stale window passes and get run through the LLM again for nothing.
    "CREATE INDEX IF NOT EXISTS idx_held_orders_open_message "
    "ON held_orders(message_id) WHERE status = 'held'",
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


# How many polls a failing UID is retried before the watermark is allowed past it
# (#20). Lives here so both the ingest loop and the dashboard API can state it.
MAX_UID_ATTEMPTS = 5

# A claim (messages.processing_at) younger than this means an n8n worker is really
# working on that email; the same window the n8n dispatcher uses to re-claim stale
# rows. Operator actions must not clear a claim inside it (#25).
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
        tuple(_no_nul(p) for p in (
            rec["identity"], h.get("message_id"), folder, uid, uidvalidity,
            h.get("from_addr"), h.get("from_name"), h.get("to_addrs"), h.get("cc_addrs"),
            h.get("subject"), h.get("date"), rec["body_text"], rec["body_source"],
            rec["combined_text"], rec["has_attachments"], rec["needs_vision"], raw_path,
            content_sig)),
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
