# Email Processing Dashboard + Telemetry + Fix Queue — Design

Date: 2026-06-26
Status: approved (brainstorming)

## Goal (plain language)

A modern **live** web dashboard over the email-extractor Postgres DB where the
user can: see **every email**, where it was processed and **how it turned out**,
**search any email** (incl. body and attachment text), do quick fixes
(re-classify, re-process), and **flag an email "for fixing"** into a DB queue
that Claude later works (was it mis-sorted, or mis-processed?).

Prerequisite that makes the dashboard possible: **every n8n workflow writes its
result into the database, keyed to the email it processed** (telemetry), so the
dashboard can show each email's journey and outcome.

## Scope

In scope:
- Telemetry data model + a Postgres rollup so the "current state" of each email
  is cheap to list and filter.
- Instrumenting each n8n workflow to write its result to the DB per email.
- A modern single-page dashboard (layout A: searchable list left, detail right),
  live-updating, served by the existing Flask app, behind a simple login.
- Operator actions from the dashboard: re-classify, re-process (retry).
- A fix queue: "give to Claude for fixing" → a `fix_requests` row Claude works.

Out of scope (tracked separately):
- Building the `reklamacie` consumer (known separate gap).
- HTTPS / Cloudflare hardening (follow-up issue; MVP keeps HTTP + login).
- Postgres full-text search (tsvector); MVP uses `ILIKE`. FTS is a later upgrade.

## Architecture

Extends the **existing** `email-extractor` Flask app (`app/httpapi.py`) and
Postgres schema (`app/db.py`). One container, one deploy, no new service. The
machine token stays for n8n's `/files` + `/eml`; a session login gates the human
dashboard.

### Data model (`app/db.py`, idempotent migrations — production has real data)

All migrations use `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` so
they apply safely to the live DB without touching existing rows.

`email_events` — append-only processing timeline, one row per step:
- `id BIGSERIAL PK`
- `message_id TEXT NOT NULL` (references `messages.message_id`)
- `ts TIMESTAMPTZ DEFAULT now()`
- `workflow TEXT` — sorter / extractor / dispatcher / ai_orders / static_orders / invoices / dodacie_listy / reklamacie
- `stage TEXT` — ingested / extracted / classified / claimed / verified / matched / edi_built / uploaded_orion / odoo_posted / forwarded / marked_processed / skipped / review / error / reclassified / requeued / fix_requested
- `status TEXT` — ok / warn / error / review / skip
- `outcome TEXT` — short human (Slovak) summary
- `detail JSONB` — rich structured detail (item count, customer, GTINs, EDI filename, ORION path, Odoo record/url, why-review reason, error text, model, durations)
- indexes: `(message_id, ts)`, `(status)`, `(stage)`

Denormalized current-state columns on `messages` (fast list/filter without
aggregating events): `proc_status TEXT`, `proc_stage TEXT`, `proc_outcome TEXT`,
`last_event_at TIMESTAMPTZ`, `attempts INT DEFAULT 0`, `edi_file TEXT`,
`orion_path TEXT`, `odoo_url TEXT`, `forwarded_to TEXT`.

Rollup: a `AFTER INSERT ON email_events` trigger (`trg_email_events_rollup`)
updates the matching `messages` row — sets `proc_stage`/`proc_status`/
`proc_outcome`/`last_event_at` to the new event, fills artifact columns
(`edi_file`, `orion_path`, `odoo_url`, `forwarded_to`) when present in `detail`,
and increments `attempts` on terminal stages. Logic lives in one tested place;
n8n only INSERTs one event.

`fix_requests` — the fix queue:
- `id BIGSERIAL PK`, `message_id TEXT NOT NULL`
- `problem_type TEXT` — `mis_sorted` | `mis_processed` | `other`
- `expected_category TEXT` (nullable; set for `mis_sorted`)
- `description TEXT` — the user's note
- `status TEXT DEFAULT 'open'` — open | in_progress | fixed | wontfix
- `snapshot JSONB` — email state captured at flag time (category, proc_outcome, key detail) = the repro context even if state later changes
- `created_at TIMESTAMPTZ DEFAULT now()`, `created_by TEXT`, `resolved_at TIMESTAMPTZ`, `resolution TEXT`

### Telemetry write contract

Each n8n consumer adds a **"Log Result" Postgres INSERT into `email_events`** at
every terminal branch (success / review / skip / error) and at cheap key
intermediates (claimed, extracted/verified). Payload uses the `message_id` of
the row the consumer pulled, its own workflow name, the stage, status, a Slovak
`outcome`, and a `detail` JSON with the rich fields. The sorter writes a
`classified` event (category + rule/AI + confidence); the Python extractor
writes an `ingested`/`extracted` event at insert time. The trigger rolls each
onto `messages`. Existing `Mark Processed` nodes are left as-is (they were tuned
"the hard way"); the Log node is added alongside, minimizing risk to the live
pipeline.

### Dashboard (Flask, same app)

- **Auth:** `GET /login` (form) → Flask signed-session cookie (`SECRET_KEY` from
  env). Human routes (`/`, `/api/*`) require the session; machine routes
  (`/files`, `/eml`) keep the token. Single password from env `DASH_PASSWORD`
  (not committed; lives in add-on options / container env). `SECRET_KEY` from env
  (random per deploy is fine — sessions just re-login).
- **Frontend:** single-page, **vanilla JS + modern CSS, no build step**, served
  by Flask (matches the zero-build deploy; one container). Layout A
  (master-detail). **Live** via polling (≈5 s, pausable) refreshing list +
  counts. Version label from `__version__` in the top bar (mandatory rule).
  Readable contrast (light surfaces, dark text) — explicit, not theme-inherited.
- **APIs (session-gated):**
  - `GET /api/messages` — list; filters `category`, `proc_status`,
    `review_status`, date range; full-text `q` via `ILIKE` over `subject`,
    `from_addr`, `from_name`, `body_text`, `combined_text`, and
    `attachments.extracted_text`; pagination; returns the count strip.
  - `GET /api/message/<id>` — detail: message fields, attachments, the
    `email_events` timeline, and any `fix_requests` for it.
  - `POST /api/message/<id>/reclassify {category}` — set `category` (+
    `original_category`), `processed=false`, `processing_at=NULL`; log a
    `reclassified` event (re-trigger via the existing dispatcher).
  - `POST /api/message/<id>/reprocess` — `processed=false`, `processing_at=NULL`,
    `error=NULL`; log a `requeued` event (retry).
  - `POST /api/message/<id>/fix {problem_type, expected_category?, description}`
    — insert a `fix_requests` row (open) + snapshot + log `fix_requested`.
  - `GET /api/fix-queue?status=` — list `fix_requests` (dashboard panel + Claude).
  - `POST /api/fix/<id>/resolve {status, resolution}` — close a fix request.

### Fix loop

Dashboard **"🔧 dať na opravu"** opens a modal: problem type (zle sortnutý / zle
spracovaný / iné), an `expected_category` dropdown when mis-sorted, and a free
text note → `POST /api/message/<id>/fix`. The fix-queue panel shows each request
with status. Claude works the queue by reading
`fix_requests WHERE status='open'` joined to the message + events + attachments,
fixes following regression-test-first (each becomes a regression test), and sets
the request to `fixed` with a resolution. This procedure is documented in the
project playbook/memory; the only project code is the API + modal.

## Error handling

- Migrations idempotent; safe re-run on the live DB.
- The rollup trigger never raises on a missing `messages` row (no-op if absent).
- Telemetry INSERTs are best-effort from n8n — a failed Log node must not stall
  the pipeline (n8n node `onError: continueRegularOutput` where applicable).
- API mutations validate `id` + `category` against the known set; 400 on bad
  input, 404 on missing message, redirect/401 without a session.

## Testing

- `db.py`: migration creates tables/columns/trigger; trigger rolls a sample event
  onto `messages`; re-running migrations is a no-op (idempotent).
- API (Flask test client): list/filter/search; detail + timeline; reclassify /
  reprocess / fix mutate correctly and write the right event; auth gate
  (redirect/401 without session, 200 with).
- Frontend: a Playwright E2E — login → list loads → search → open detail → see
  timeline → reclassify → open fix modal → submit — asserting **zero console
  errors/warnings** and the **version label present + matching backend** (the
  mandatory web rules). Runs against the deployed dashboard.
- Telemetry (live verification): a processed email produces `email_events` rows
  and the `messages` denorm updates.

## Issue decomposition

Repo-code issues (dev→main PRs, CI green, autopilot-workable):
1. **DB schema** — `email_events`, denorm columns on `messages`, rollup trigger,
   `fix_requests`; idempotent migrations + tests. (foundation — first)
2. **Auth** — `/login` + signed session, gate human endpoints, keep token for
   machine endpoints; tests.
3. **List/detail APIs** — `/api/messages` (filter/search/counts),
   `/api/message/<id>` (detail + timeline); tests.
4. **Operator actions API** — reclassify, reprocess; tests.
5. **Fix-queue API** — fix, fix-queue, resolve; tests.
6. **Dashboard frontend (layout A)** — SPA, live polling, search/filters,
   detail + timeline + attachments, action buttons, fix modal, fix-queue panel,
   version label; Playwright E2E.
7. **Deploy + verify** — deploy to the HA box, live Playwright verification,
   confirm telemetry writes.

n8n instrumentation (done by Claude via the n8n MCP, verified live, recorded in
memory — NOT a repo PR; filed as a tracking issue so it isn't dropped):
8. **Telemetry events in all consumers** — Log Result events in ai_orders,
   static_orders, invoices, dodacie_listy + sorter `classified` + extractor
   `ingested`/`extracted`, per the write contract above.

Bundling per the airuleset gate: #1 is the foundation (first). #2–#5 are small
backend slices and may bundle within the ≤600 LoC / ≤4-issue ceiling; #6 is its
own PR (frontend + E2E). #7 is the deploy. #8 is n8n/MCP work.

## Follow-ups (filed, not dropped)

- HTTPS / Cloudflare hardening of the public dashboard.
- Postgres FTS (tsvector) if `ILIKE` search gets slow at volume.
- `reklamacie` consumer (pre-existing gap) — once built, telemetry + dashboard
  cover it automatically.
