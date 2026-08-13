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
    # --- #273: message_id is the referencing side of a FK (ON DELETE CASCADE) —
    # Postgres does not auto-index it. Queried directly in httpapi_dashboard_data.py
    # (x2) and dl_worker.py; a cascading delete from messages also scans this
    # sequentially without it. Plain btree CREATE INDEX IF NOT EXISTS (no
    # CONCURRENTLY, no advisory lock — same shape as idx_messages_status/
    # idx_events_message below: a single atomic DDL statement Postgres itself
    # serializes across concurrent init_schema() callers, unlike the ALTER TABLE +
    # backfill migrations elsewhere in this file that genuinely need the lock). ---
    "CREATE INDEX IF NOT EXISTS idx_attachments_message ON attachments(message_id)",
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
    # --- #153: two-phase confirmation. A row is inserted as a CLAIM before the upload
    # happens; without a way to tell "claimed" from "genuinely uploaded" apart, a run
    # that dies between the claim and the upload (crash, kill, restart — anything
    # outside the `except` around upload()) leaves an orphan row that the next attempt
    # reads as "already sent" and silently drops the order forever (13 real orders,
    # 2026-08-03). NULL = claim only; set = the upload actually succeeded.
    #
    # The DO block adds the column AND backfills every row that already existed AT THAT
    # EXACT MOMENT, in the same breath, guarded by "does the column exist yet" so it can
    # only ever fire once. That is deliberately NOT a hardcoded cutoff timestamp: the
    # add-on is a single process, and init_schema() runs at startup before any message
    # is processed (see main.py) — so every row present when this block first runs was
    # written by the OLD one-phase code (claim immediately followed by upload, with no
    # gap represented in the DB) and is real, historical, physically-delivered EDI. A
    # hardcoded cutoff would fail in one of two directions instead: too early wrongly
    # treats an order shipped just before deploy as an orphan (triggering exactly the
    # duplicate upload this fix exists to prevent); too late could backfill — and
    # thereby hide — a genuinely new orphan created by the NEW two-phase code after
    # deploy. Tying the backfill to the column's own creation has neither failure mode.
    #
    # init_schema() is not ONLY called by main.py's single-process startup — the
    # one-off admin CLI tools (backfill.py, alias_migration.py, eval_run.py,
    # memory_import.py) call it too, from their own separate connections, and are
    # documented as safe to run at any time (review finding, PR #176). Without the
    # advisory lock below, two such callers could both pass the "column missing" check
    # before either commits, race the ALTER, and the loser would crash on
    # duplicate_column — or, worse, run its OWN backfill sometime AFTER the winner's
    # migration already completed and real new activity started, silently confirming a
    # genuinely fresh orphan. `pg_advisory_xact_lock` fully serializes every caller
    # through this block (held only for the DO block's own implicit transaction, so it
    # self-releases immediately under this autocommit connection) — the second caller
    # then re-evaluates the IF and correctly finds the column already there.
    """
    DO $$
    BEGIN
        PERFORM pg_advisory_xact_lock(hashtext('email-extractor:edi_sent.uploaded_at#153'));
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = 'edi_sent'
               AND column_name = 'uploaded_at'
        ) THEN
            ALTER TABLE edi_sent ADD COLUMN uploaded_at TIMESTAMPTZ;
            UPDATE edi_sent SET uploaded_at = sent_at WHERE uploaded_at IS NULL;
        END IF;
    END $$
    """,
    # --- #151: import CONFIRMATION. Uploading a file to `in/` is not proof it ever
    # reached ORION — this is the state the periodic sweep in `orders/confirm.py`
    # writes. NULL = not yet resolved AND still watched, which INCLUDES a carryover
    # (#133, 2026-08-05: a file still legitimately waiting for the warehouse's manual
    # morning acceptance click is deliberately never given a terminal status, so it
    # self-heals to 'imported' the moment it's accepted); 'imported'/'failed'/'unknown'
    # are the only TERMINAL values (see confirm.py's docstring for what each means and
    # why 'unknown' — a file gone from `in`, `archCodex` AND `unconfirmed` — is treated
    # as unresolved and alerted, never as silent success). The old 'timeout' value
    # (a ~60-minute "Communicator will pick it up automatically" alert, based on a wrong
    # model — import is manual, not automatic) is REMOVED; it will never be written
    # again, though historical rows may still carry it.
    #
    # Same advisory-lock DO-block shape as the `uploaded_at` migration above, and the same
    # reasoning for backfilling pre-existing rows rather than leaving them NULL: every row
    # already CONFIRMED uploaded (`uploaded_at IS NOT NULL`) when this block first runs
    # predates the whole import-confirmation feature and was, in the overwhelming
    # majority, imported long ago without incident — sweeping all of them retroactively
    # the moment this ships would flood Odoo with alerts for ancient, already-settled
    # orders instead of watching NEW uploads going forward (the ticket's actual ask). A
    # row with `uploaded_at IS NULL` (still just a claim, never confirmed) is left alone —
    # import confirmation is meaningless before the upload itself is confirmed. ---
    """
    DO $$
    BEGIN
        PERFORM pg_advisory_xact_lock(hashtext('email-extractor:edi_sent.import_status#151'));
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = 'edi_sent'
               AND column_name = 'import_status'
        ) THEN
            ALTER TABLE edi_sent ADD COLUMN import_status TEXT;
            ALTER TABLE edi_sent ADD COLUMN import_confirmed_at TIMESTAMPTZ;
            ALTER TABLE edi_sent ADD COLUMN import_checked_at TIMESTAMPTZ;
            UPDATE edi_sent SET import_status = 'imported', import_confirmed_at = uploaded_at
             WHERE uploaded_at IS NOT NULL AND import_status IS NULL;
        END IF;
    END $$
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
    # --- #164: every board-resolvable dead end funnels through the SAME order_questions
    # table/index/dashboard the item(#88)/customer(#159) questions already use — a
    # kind-agnostic `payload` (what the kind needs to answer/apply) and `answer` (what was
    # picked, for the NEW kinds' unified {"choice": ...} contract) generalize it instead of
    # bespoke columns per new kind. Additive only: a live open item/customer row has
    # payload='{}' / answer=NULL by default and keeps working with no backfill. ---
    "ALTER TABLE order_questions ADD COLUMN IF NOT EXISTS payload jsonb NOT NULL "
    "DEFAULT '{}'::jsonb",
    "ALTER TABLE order_questions ADD COLUMN IF NOT EXISTS answer jsonb",
    # --- #164: what a 'mail' question ("is this even an order?") teaches — a sender +
    # normalized-subject pattern, so the SAME kind of mail from the SAME sender stops
    # asking. `ignore` short-circuits BEFORE the LLM call (saves the extraction cost too);
    # `manual` still costs nothing extra but skips straight to a 'review' outcome with the
    # taught instruction, never re-asking. `question_id` traces the rule back to the
    # question that created it (mirrors `global_item_memory.question_id`), so `teach.undo`
    # can retract only the rule ITS OWN question created. ---
    """
    CREATE TABLE IF NOT EXISTS mail_rules (
        id            BIGSERIAL PRIMARY KEY,
        sender_norm   TEXT NOT NULL,
        subject_key   TEXT NOT NULL,
        action        TEXT NOT NULL CHECK (action IN ('ignore', 'manual')),
        question_id   BIGINT REFERENCES order_questions(id),
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_mail_rules_key "
    "ON mail_rules(sender_norm, subject_key)",
    # --- #133 "DOPLNENIE ROZHODNUTIA": a durable, DB-backed queue for the grouped Odoo
    # digest of cleanly-uploaded static orders — every call reads/writes straight from
    # this table (no in-memory state), so it survives an add-on restart by construction.
    # A row with flushed_at IS NULL is still pending; flushing sets it on every pending
    # row at once. Only clean, fresh, non-actionable uploads are ever queued here — a
    # duplicate skip, an empty order, an upload error, or an order with an actionable
    # extra-content note never enter this table (see static_digest.py). ---
    """
    CREATE TABLE IF NOT EXISTS static_order_digest (
        id          BIGSERIAL PRIMARY KEY,
        message_id  TEXT NOT NULL,
        filename    TEXT,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        flushed_at  TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_static_order_digest_pending "
    "ON static_order_digest(created_at) WHERE flushed_at IS NULL",
    # --- #133 (2026-08-05 correction): a durable, per-(channel, kind) INCIDENT for the
    # grouped import-confirmation alert (app/orders/confirm.py) — replaces the old
    # one-message-per-file timeout alert. `kind` is 'carryover' (still unaccepted from a
    # prior day) / 'failed' (landed in ORION's "unconfirmed" folder) / 'unknown' (vanished
    # from all three watched folders). At most one OPEN (closed_at IS NULL) row per
    # (channel_id, kind) at a time — every function in confirm.py reads/writes straight
    # from this table, no in-memory state, so it survives an add-on restart by
    # construction. ---
    """
    CREATE TABLE IF NOT EXISTS import_alert_incidents (
        id            BIGSERIAL PRIMARY KEY,
        channel_id    BIGINT NOT NULL,
        kind          TEXT NOT NULL CHECK (kind IN ('carryover', 'failed', 'unknown')),
        opened_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_alert_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        closed_at     TIMESTAMPTZ
    )
    """,
    # UNIQUE (not just an index): "at most one open incident per (channel, kind)" is an
    # invariant `_open_incident`'s own SELECT relies on — make it DB-enforced, not just
    # implicit from the single-threaded worker loop being the only caller today (review
    # suggestion, PR #184).
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_import_alert_incidents_open "
    "ON import_alert_incidents(channel_id, kind) WHERE closed_at IS NULL",
    # --- review finding on PR #184: the FIRST cut cleared/counted incidents off a
    # GLOBAL proxy ("something, somewhere, was imported" / a blindly-incremented
    # counter) — reproduced live: an unrelated healthy order importing on a DIFFERENT
    # channel falsely closed a still-open incident, and a persistently-rediscovered
    # carryover row inflated its own incident's reported count without bound. This
    # child table tracks EXACTLY which `edi_sent` row belongs to which incident, so both
    # "how many files, really" (count of members) and "is THIS incident's own set of
    # files actually resolved" (are all members no longer NULL-status) are answered
    # from the incident's own members, never a proxy. The PRIMARY KEY is what makes a
    # rediscovered row a no-op re-insert (`ON CONFLICT DO NOTHING`), not a re-count. ---
    """
    CREATE TABLE IF NOT EXISTS import_alert_incident_members (
        incident_id BIGINT NOT NULL REFERENCES import_alert_incidents(id) ON DELETE CASCADE,
        edi_sent_id BIGINT NOT NULL REFERENCES edi_sent(id) ON DELETE CASCADE,
        added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (incident_id, edi_sent_id)
    )
    """,
    # --- #196: a small, append-only, HONEST log of confirmed wrong-shipment incidents
    # (never a hand-maintained "days since X" constant that ages — "days since the last
    # incident" is always LIVE-computed as now() - max(occurred_on)). Deliberately
    # distinct from `import_alert_incidents` above (that table is about ORION import
    # CONFIRMATION alerts; this one is about MATCH-QUALITY trust). Seeded once with the
    # two real incidents this batch (#195) already found corpus-cased on dev2 — every
    # FUTURE incident-fix PR adds a row here in the SAME PR it adds the dev2 corpus case
    # (`.claude/rules/orders-corpus.md`'s #188 standing rule). ---
    """
    CREATE TABLE IF NOT EXISTS match_incidents (
        id          BIGSERIAL PRIMARY KEY,
        occurred_on DATE NOT NULL,
        description TEXT NOT NULL,
        issue_ref   TEXT NOT NULL UNIQUE,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    INSERT INTO match_incidents (occurred_on, description, issue_ref) VALUES
        ('2026-08-03', 'CÉDER: alias-note bias skladom nepotvrdenú kartu (chlieb) — '
                       'oprava #157', '#157'),
        ('2026-08-06', 'CÉDER: sebaisté (0.96-0.97) modelové rozhodnutie s tou istou '
                       'alias-bias príčinou — oprava #186/#189', '#186')
    ON CONFLICT (issue_ref) DO NOTHING
    """,
    # #289 (2026-08-13): PNO Poprad's shorthand date-range subject ("17. - 22. 08. 2026")
    # silently dropped 5 of 6 delivery days — a real wrong-shipment incident, remediated
    # live via a one-off shadow-verified re-ship, root-caused and fixed in the SAME PR
    # that adds this row (`.claude/rules/orders-corpus.md`'s #188/#196 standing rules).
    """
    INSERT INTO match_incidents (occurred_on, description, issue_ref) VALUES
        ('2026-08-13', 'PNO Poprad: skrátený rozsah dátumu v predmete („17. - 22. 08. '
                       '2026“) stratil 5 z 6 dní objednávky — oprava #289', '#289')
    ON CONFLICT (issue_ref) DO NOTHING
    """,
    # One post per calendar day, never once per worker tick — same "claim, don't
    # spam" pattern order_spend_alerts already uses for the monthly cap.
    """
    CREATE TABLE IF NOT EXISTS order_digest_sent (
        day     DATE PRIMARY KEY,
        sent_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,

    # ==========================================================================
    # #200 F1: delivery-notes (dodacie listy, "DL") migration — foundation only.
    # See docs/superpowers/specs/2026-08-07-delivery-notes-python-design.md for
    # the full rules map (R1-R97) and known n8n weaknesses (W1-W16) these tables
    # are built to fix. No pipeline code reads/writes these yet — they exist so
    # later phases have a schema to build on, and so the worst structural bugs
    # (W2/W3/W4) are fixed at the schema level from day one, not bolted on later.
    # ==========================================================================
    #
    # --- desadv_sent: two-phase claim/confirm upload ledger, mirroring
    # edi_sent's #153 design (app/orders/edi.py:213-297) but with a DIFFERENT
    # identity: (supplier_ean, doc_number), no content hash. DL identity is the
    # DOCUMENT (one delivery note has one number from its own supplier), not its
    # bytes — the n8n registry never hashed content either (R90). Scoping by
    # supplier_ean (not just doc_number) fixes W4: a bare short doc number
    # (e.g. "68944") can legitimately repeat across two different suppliers
    # without colliding. Two-phase from inception fixes W2/W3: a claim is
    # reserved BEFORE the upload, confirmed only after it genuinely succeeds —
    # unlike the n8n registry, which reads/writes only AFTER the upload,
    # leaving the exact race window that lost/duplicated real DL uploads. ---
    """
    CREATE TABLE IF NOT EXISTS desadv_sent (
        id            BIGSERIAL PRIMARY KEY,
        supplier_ean  TEXT NOT NULL,
        doc_number    TEXT NOT NULL,
        filename      TEXT,
        sent_at       TIMESTAMPTZ DEFAULT now(),
        uploaded_at   TIMESTAMPTZ,
        UNIQUE (supplier_ean, doc_number)
    )
    """,
    # --- dl_item_memory: item_memory's sibling (db.py:397-410) for DL product
    # matching, keyed by SUPPLIER instead of customer, with one structural
    # difference — the `cnt` column. R66's weighted-majority rule (a mixed
    # history takes the newest card's GTIN only when it carries >= 60% of ALL
    # deliveries, weighted by cnt) needs the n8n table's own per-row delivery
    # COUNT preserved verbatim; item_memory.resolve() instead counts DISTINCT
    # DAYS (a deliberate, different fix for a different n8n bug — see its own
    # docstring). Conflating the two semantics in one table would risk
    # reintroducing the exact bug item_memory was built to fix, so this is a
    # separate table, not a nullable column bolted onto item_memory. ---
    """
    CREATE TABLE IF NOT EXISTS dl_item_memory (
        id           BIGSERIAL PRIMARY KEY,
        supplier_ean TEXT NOT NULL,
        item_key     TEXT NOT NULL,
        item_raw     TEXT,
        gtin         TEXT NOT NULL,
        card         TEXT,
        delivered_on DATE NOT NULL,
        cnt          INTEGER NOT NULL DEFAULT 1,
        source       TEXT,
        created_at   TIMESTAMPTZ DEFAULT now(),
        UNIQUE (supplier_ean, item_key, gtin, delivered_on, cnt)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dl_item_memory_lookup "
    "ON dl_item_memory(supplier_ean, item_key)",
    # --- DL catalog + supplier snapshots (app/orders/dl_snapshot.py) — content-
    # addressed, same pattern as order_snapshots/catalog_snapshot/customer_snapshot
    # (snapshot.py), but a SEPARATE versioning line: the DL catalog's shape
    # (name/gtin/mass/doplnok/sklad/cena, R20) is different from the orders
    # catalog's (gtin/name/alias only), so freezing them together would force
    # one snapshot id to change whenever EITHER pipeline's source sheet changes,
    # even when the other pipeline's own data is untouched. ---
    """
    CREATE TABLE IF NOT EXISTS dl_snapshots (
        id             BIGSERIAL PRIMARY KEY,
        content_sha256 TEXT NOT NULL,
        catalog_rows   INT  NOT NULL,
        supplier_rows  INT  NOT NULL,
        imported_at    TIMESTAMPTZ DEFAULT now(),
        checked_at     TIMESTAMPTZ DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dl_snapshots_hash ON dl_snapshots(content_sha256)",
    """
    CREATE TABLE IF NOT EXISTS dl_catalog_snapshot (
        snapshot_id BIGINT NOT NULL REFERENCES dl_snapshots(id) ON DELETE CASCADE,
        gtin        TEXT   NOT NULL,
        name        TEXT   NOT NULL,
        doplnok     TEXT,
        mass        NUMERIC,
        sklad       TEXT,
        cena        NUMERIC,
        PRIMARY KEY (snapshot_id, gtin)
    )
    """,
    # No unique key on ean_edi — same reasoning as customer_snapshot: a supplier
    # row can legitimately have a blank EAN, and (unlikely but not impossible)
    # two branches of the same supplier could share one.
    """
    CREATE TABLE IF NOT EXISTS dl_supplier_snapshot (
        id          BIGSERIAL PRIMARY KEY,
        snapshot_id BIGINT NOT NULL REFERENCES dl_snapshots(id) ON DELETE CASCADE,
        ean_edi     TEXT,
        name        TEXT NOT NULL,
        emails      TEXT[],
        city        TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dl_supplier_snapshot_snap ON dl_supplier_snapshot(snapshot_id)",
    # --- dl_supplier_memory (#202, DL migration F3): the nástenka's "ktorý dodávateľ?"
    # teaching — a taught sender-EMAIL -> supplier EAN mapping, so the SAME address never
    # asks twice. Deliberately NOT a `customer_overrides`-style full override/rebuild system
    # (design comment on #202): DL suppliers stay entirely sheet-driven
    # (dl_supplier_snapshot above); this table only ever needs to answer "which EAN does
    # this address belong to", one small standalone lookup, same spirit as
    # `dl_item_memory` sitting next to it rather than trying to be `item_memory`. ---
    """
    CREATE TABLE IF NOT EXISTS dl_supplier_memory (
        id           BIGSERIAL PRIMARY KEY,
        sender_email TEXT NOT NULL UNIQUE,
        ean_edi      TEXT NOT NULL,
        name         TEXT,
        created_at   TIMESTAMPTZ DEFAULT now()
    )
    """,
    # ==========================================================================
    # #203 F4: DESADV EDI builder + upload + import confirmation. desadv_sent (F1)
    # already has the two-phase claim/confirm ledger (sent_at/uploaded_at); this phase
    # extends it with the SAME import-confirmation columns edi_sent got in #151, so
    # `orders/confirm.py`'s sweep can watch DESADV uploads through `in_DL` the same way
    # it already watches ORDER_ uploads through `in`. Same advisory-lock DO-block shape
    # and the same backfill reasoning as edi_sent's own #151 migration (db.py:553-568):
    # any row already `uploaded_at IS NOT NULL` when this block first runs predates the
    # feature and is treated as already resolved — in practice desadv_sent is EMPTY
    # today (no worker writes it yet, #203 ships the builder+ledger primitives only;
    # the worker wiring is a later phase), so this backfill is a no-op now and a safety
    # net later, exactly like edi_sent's own migration was written to be. ---
    """
    DO $$
    BEGIN
        PERFORM pg_advisory_xact_lock(
            hashtext('email-extractor:desadv_sent.import_status#203'));
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = 'desadv_sent'
               AND column_name = 'import_status'
        ) THEN
            ALTER TABLE desadv_sent ADD COLUMN import_status TEXT;
            ALTER TABLE desadv_sent ADD COLUMN import_confirmed_at TIMESTAMPTZ;
            ALTER TABLE desadv_sent ADD COLUMN import_checked_at TIMESTAMPTZ;
            UPDATE desadv_sent SET import_status = 'imported',
                   import_confirmed_at = uploaded_at
             WHERE uploaded_at IS NOT NULL AND import_status IS NULL;
        END IF;
    END $$
    """,
    # --- import_alert_incidents gains a `source` discriminator ('edi'/'desadv', #203)
    # so a DESADV incident never gets grouped with an ORDER_ incident under the same
    # (channel_id, kind) row — the alert TEXT differs per source (a delivery note is
    # never an "objednávka"), and the two ledgers' rows live in separate members tables
    # (see import_alert_incident_desadv_members below) that a single incident can't
    # straddle without a polymorphic FK. Additive (DEFAULT 'edi' keeps every existing
    # row — all of them genuinely edi_sent-sourced today — valid with zero backfill
    # needed). CHECK mirrors the sibling `kind` column's own DB-enforced-invariant
    # philosophy (see the #184 comment on that column) — a stray/mistyped source value
    # must fail loudly, not silently fall back to EDI_LEDGER wherever confirm.py reads
    # it back. The old (channel_id, kind) unique-open-incident index is replaced by a
    # (channel_id, kind, source) one; the NEW index is created BEFORE the old one is
    # dropped (never the other order) — `edi_sent`'s own import-confirmation sweep is
    # an already-live production writer into this table (the #151/#179/#184 incident
    # history this file documents), so a DROP-then-CREATE ordering would leave a real
    # window with NO uniqueness enforced on (channel_id, kind) at all; creating the
    # wider index first means uniqueness is continuously enforced throughout (both
    # indexes briefly coexist, which is harmless). DROP+CREATE on an INDEX is a
    # structural change, not a data-loss one (database-migrations.md). ---
    "ALTER TABLE import_alert_incidents ADD COLUMN IF NOT EXISTS source TEXT "
    "NOT NULL DEFAULT 'edi'",
    "ALTER TABLE import_alert_incidents DROP CONSTRAINT IF EXISTS "
    "import_alert_incidents_source_check",
    "ALTER TABLE import_alert_incidents ADD CONSTRAINT "
    "import_alert_incidents_source_check CHECK (source IN ('edi', 'desadv'))",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_import_alert_incidents_open_v2 "
    "ON import_alert_incidents(channel_id, kind, source) WHERE closed_at IS NULL",
    "DROP INDEX IF EXISTS idx_import_alert_incidents_open",
    # --- import_alert_incident_desadv_members: the DESADV counterpart of
    # import_alert_incident_members, same shape, own FK to desadv_sent. A SEPARATE
    # table rather than widening the existing one with a second nullable FK column —
    # that would force dropping the live table's PRIMARY KEY (a PK column cannot be
    # NULL) for a still-zero-traffic ledger; a plain additive CREATE TABLE carries none
    # of that risk (database-migrations.md: prefer incremental, never touch a live
    # table's PK for a feature nothing writes to yet). Since #203's own design comment
    # settles this in favour of the separate table over the polymorphic-FK alternative
    # — see the rejected-alternative note there. ---
    """
    CREATE TABLE IF NOT EXISTS import_alert_incident_desadv_members (
        incident_id    BIGINT NOT NULL REFERENCES import_alert_incidents(id)
                           ON DELETE CASCADE,
        desadv_sent_id BIGINT NOT NULL REFERENCES desadv_sent(id) ON DELETE CASCADE,
        added_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (incident_id, desadv_sent_id)
    )
    """,
    # --- #216: desadv_sent gains message_id, so a retry-after-partial-ship can tell
    # "THIS SAME message already shipped this document earlier" apart from "a genuinely
    # DIFFERENT message announced the same document" (W7's real-duplicate signal). No
    # backfill: a legacy row predating this column simply has no known claimant, and
    # `desadv.claimed_by()` treats that as "" — which can never equal a real
    # message_id, so an existing row's reporting is completely unchanged by this
    # migration. Plain `ADD COLUMN IF NOT EXISTS`, no advisory-lock DO block, matching
    # the `import_alert_incidents.source` precedent just above: Postgres serializes
    # concurrent `ADD COLUMN IF NOT EXISTS` calls via its own table lock, and there is
    # no backfill step here to race. ---
    "ALTER TABLE desadv_sent ADD COLUMN IF NOT EXISTS message_id TEXT",
    # --- #221: direct web curation of DL catalog cards + suppliers, mirroring #127/#128's
    # catalog_overrides/customer_overrides — layered ON TOP of the frozen dl_catalog_snapshot/
    # dl_supplier_snapshot (the sheet itself is never read anymore, #129), merged in at
    # freeze time by dl_snapshot.py's own dl_rebuild_from_overrides. Deliberately a SEPARATE
    # pair of tables from catalog_overrides/customer_overrides, not a shared/widened one — a
    # GTIN can legitimately exist in BOTH the AI-orders catalog and the DL catalog
    # (dl_snapshot.py's own merge_catalog union), and editing a DL-only field on it must
    # never also rewrite what the AI-orders engine sees for the same product; same reasoning
    # dl_snapshots already used to stay independent of order_snapshots (R20 design comment
    # on dl_snapshot.py). ---
    """
    CREATE TABLE IF NOT EXISTS dl_catalog_overrides (
        gtin       TEXT PRIMARY KEY,
        name       TEXT NOT NULL,
        doplnok    TEXT,
        mass       NUMERIC,
        sklad      TEXT,
        cena       NUMERIC,
        retired    BOOLEAN NOT NULL DEFAULT false,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # Surrogate id, not ean_edi — same reasoning as customer_overrides: a supplier row can
    # legitimately have a blank EAN, and (like customer branches) two rows could share one.
    # orig_ean_edi/orig_city pin the ORIGINAL snapshot row an override replaces (NULL
    # orig_ean_edi = a brand-new supplier, not an edit) — city, not street, because
    # dl_supplier_snapshot/dl_snapshot.load_suppliers never persists street/zip at all (see
    # that module's own R21 docstring), so city is the only disambiguator actually available.
    """
    CREATE TABLE IF NOT EXISTS dl_supplier_overrides (
        id           BIGSERIAL PRIMARY KEY,
        orig_ean_edi TEXT,
        orig_city    TEXT,
        ean_edi      TEXT,
        name         TEXT NOT NULL,
        emails       TEXT[],
        city         TEXT,
        retired      BOOLEAN NOT NULL DEFAULT false,
        updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_dl_supplier_overrides_orig "
    "ON dl_supplier_overrides(orig_ean_edi, orig_city) WHERE orig_ean_edi IS NOT NULL",
    # --- #239: a durable outbox for a processing-health alert that has NO other trace
    # if its Odoo post is lost — an upload-failure alert (class 2) or a stuck-classified
    # alert (class 3). Requirement 3 of #239: "an alert that cannot be delivered must be
    # recorded and retried, never silently dropped — otherwise you have rebuilt the very
    # problem this ticket exists to fix, one layer up." `app/orders/dl_alerts.py`
    # enqueues here FIRST (durable, before any delivery attempt) and a periodic sweep
    # (`flush_pending`, on the same ~15s tick `confirm.sweep` already runs on) retries
    # delivery, grouped by (channel_id, kind), until Odoo genuinely confirms it — never
    # one Odoo message per item (the 2026-08-05 5-alerts-at-once flood the user deleted
    # is the precedent this avoids). `message_id` is nullable (a class-2 alert always has
    # one; a future non-message-scoped alert kind would not) and is what
    # `dl_alerts.already_pending()` dedupes a persistently-stuck message on. ---
    """
    CREATE TABLE IF NOT EXISTS pending_alerts (
        id           BIGSERIAL PRIMARY KEY,
        channel_id   INTEGER NOT NULL,
        kind         TEXT NOT NULL,
        message_id   TEXT,
        body_html    TEXT NOT NULL,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        delivered_at TIMESTAMPTZ,
        attempts     INTEGER NOT NULL DEFAULT 0,
        last_error   TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pending_alerts_undelivered "
    "ON pending_alerts(kind, channel_id) WHERE delivered_at IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_pending_alerts_message "
    "ON pending_alerts(kind, message_id) WHERE message_id IS NOT NULL",
    # --- #237: a board question that stays `open` too long gets NO further signal
    # today — only the one-shot `on_new` notify fires, at creation. `reminder_sent_at`/
    # `escalated_at` are the per-question cadence state `app/orders/question_alerts.py`
    # reads/writes: NULL means "not yet sent"; once set, that level never fires again
    # for this row (no daily nag — "escalate once", see that module's own docstring).
    # Additive, nullable, no backfill needed: every existing row (open or answered)
    # simply starts as "never reminded", which is the honest truth for all of them. ---
    "ALTER TABLE order_questions ADD COLUMN IF NOT EXISTS reminder_sent_at TIMESTAMPTZ",
    "ALTER TABLE order_questions ADD COLUMN IF NOT EXISTS escalated_at TIMESTAMPTZ",
    # --- #248: DB-level uniqueness for a hand-added (`orig_ean_edi IS NULL`) row's
    # `ean_edi` — the app-level advisory lock in `upsert_customer`/`upsert_dl_supplier`
    # already closes the actual race (see those functions' own #248 comments for the
    # full trace), but the ticket asks for the invariant to be true at the DB level too,
    # not just for callers that remember to take the lock. Checked LIVE against
    # production 2026-08-12 before writing this: `SELECT ean_edi, count(*) FROM
    # customer_overrides WHERE orig_ean_edi IS NULL AND NOT retired AND ean_edi <> ''
    # GROUP BY ean_edi HAVING count(*) > 1` (and the same for dl_supplier_overrides)
    # returned ZERO duplicate groups — 2 active hand-added customer rows, 1 active
    # hand-added supplier row, none of them null/blank EAN, none colliding. So a plain
    # `CREATE UNIQUE INDEX` would succeed today. It is written as a guarded de-dup step
    # anyway, unconditionally, so a duplicate discovered LATER (a different environment,
    # a bug elsewhere, a restored backup) can never turn this migration into a crash
    # loop on boot — the whole reason this ticket exists. Same guarded-DO-block shape as
    # the #153/#151 migrations above (advisory lock so two `init_schema` callers — the
    # live add-on AND a one-off admin CLI, see #153's own docstring — can't race the
    # de-dup itself; an IF-NOT-EXISTS-yet check on the TARGET INDEX so the retiring
    # UPDATE can only ever run once, before the index exists, never again after — once
    # the index exists this block is a single cheap `pg_indexes` lookup on every future
    # boot). The de-dup keeps the most-recently-updated ACTIVE row per duplicate
    # `ean_edi` group (the freshest edit is the most likely to be the correct one) and
    # RETIRES the rest — never deletes a row, so a wrongly-chosen loser is still fully
    # visible and recoverable in the dashboard, just no longer counted as active. ---
    """
    DO $$
    BEGIN
        PERFORM pg_advisory_xact_lock(
            hashtext('email-extractor:customer_overrides.ean_edi_unique#248'));
        IF NOT EXISTS (
            SELECT 1 FROM pg_indexes
             WHERE schemaname = 'public'
               AND indexname = 'idx_customer_overrides_new_ean'
        ) THEN
            UPDATE customer_overrides t
               SET retired = true, updated_at = now()
             WHERE t.orig_ean_edi IS NULL AND NOT t.retired
               AND t.ean_edi IS NOT NULL AND t.ean_edi <> ''
               AND t.id <> (
                   SELECT d.id FROM customer_overrides d
                    WHERE d.orig_ean_edi IS NULL AND NOT d.retired
                      AND d.ean_edi = t.ean_edi
                    ORDER BY d.updated_at DESC, d.id DESC LIMIT 1);
        END IF;
    END $$
    """,
    # #248 review finding: the index below must exclude a blank `ean_edi` the SAME way
    # the de-dup UPDATE above does (`AND t.ean_edi <> ''`) — Postgres treats `''` as an
    # ORDINARY, EQUAL value for a unique index (unlike NULL, which the index correctly
    # never sees any two rows sharing), so without this exclusion two active hand-added
    # rows that both happen to carry a blank EAN would collide on `CREATE UNIQUE INDEX`
    # and crash boot — exactly the failure this migration exists to prevent. Reproduced
    # against a real Postgres before this fix: two `ean_edi=''` active rows raised
    # `UniqueViolation: Key (ean_edi)=() is duplicated` on `init_schema()`. Does not
    # happen against production data today (confirmed live: 0 blank-EAN active rows),
    # but the de-dup step above is explicitly unconditional for exactly this class of
    # future/other-environment case, so the index it protects must be too.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_customer_overrides_new_ean "
    "ON customer_overrides(ean_edi) WHERE orig_ean_edi IS NULL AND NOT retired "
    "AND ean_edi <> ''",
    """
    DO $$
    BEGIN
        PERFORM pg_advisory_xact_lock(
            hashtext('email-extractor:dl_supplier_overrides.ean_edi_unique#248'));
        IF NOT EXISTS (
            SELECT 1 FROM pg_indexes
             WHERE schemaname = 'public'
               AND indexname = 'idx_dl_supplier_overrides_new_ean'
        ) THEN
            UPDATE dl_supplier_overrides t
               SET retired = true, updated_at = now()
             WHERE t.orig_ean_edi IS NULL AND NOT t.retired
               AND t.ean_edi IS NOT NULL AND t.ean_edi <> ''
               AND t.id <> (
                   SELECT d.id FROM dl_supplier_overrides d
                    WHERE d.orig_ean_edi IS NULL AND NOT d.retired
                      AND d.ean_edi = t.ean_edi
                    ORDER BY d.updated_at DESC, d.id DESC LIMIT 1);
        END IF;
    END $$
    """,
    # Same blank-EAN exclusion as the customer index above — see that comment.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_dl_supplier_overrides_new_ean "
    "ON dl_supplier_overrides(ean_edi) WHERE orig_ean_edi IS NULL AND NOT retired "
    "AND ean_edi <> ''",
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
