---
paths:
  - "email-extractor/app/orders/human_processing.py"
  - "email-extractor/tests/test_human_processing.py"
---

# `human_processing` sweep (#308) — hard-won incident lessons (2026-08-14)

The `human_processing.sweep` closes the "silent pit" where the n8n classifier drops a
message it cannot place (a near-empty scan → `needs_vision`) into the terminal
`human_processing` category that no engine watches. Two layers: (1) vision-assisted
rescue into a real processor category, (2) a mandatory notify net. The FIRST live deploy
(0.9.95) caused a real incident — these lessons are why the code looks the way it does.

## `human_processing` is a LARGE CATCH-ALL, not a small "a human must handle" bucket

The ticket assumed `human_processing` holds a few stuck warehouse scans. **Live reality:
846 messages accumulated since June — payslips (`Výplatná páska`), job-board replies
(`profesia.sk` "Nová reakcia na ponuku"), HR mail — NOT delivery notes.** A net that
notified ALL of them flooded the warehouse Odoo channel 243 with 10 operator-shaped
notices about payslips before it was caught. **Before deploying ANY backfilling sweep over
an existing category, count the LIVE volume + sample the real content first** (read-only
`SELECT category, count(*)` + a subject sample on the prod DB) — a category's name is not
its contents, and a test DB never has the historical accumulation.

## A backfilling sweep needs a HARD first-deploy-date cutoff

`BACKLOG_CUTOFF = "2026-08-14"` (the day the sweep first ran): the candidate query takes
ONLY `created_at >= BACKLOG_CUTOFF`. The pre-existing historical backlog must NEVER
auto-enter a pipeline or channel — it belongs at most in a deliberate ops digest. A FIXED
date (not a rolling window) is deliberate: the backlog stays excluded forever. Any future
sweep that begins processing an already-populated table needs the same guard from day one,
or it dumps months of backlog into live pipelines/channels on first deploy.

## "System couldn't classify" is an OPERATOR/triage concern → ops channel, never warehouse

Layer-2 `_notify` routes to `report.ops_channel(cfg)` (the #310 hold: 0 when unset →
`dl_alerts.flush_pending` HOLDS a channel-0 group, counted on the dashboard, never
delivered), NEVER `delivery_notes_channel_id` (243). Genuine documents are routed to their
engine by the Layer-1 vision rescue; the net catches only the residual an operator must
triage. This is the same audience split as #310 — an unclassifiable mail is not something
the warehouse can act on.

## NEVER revert/reclassify a message back into a category a still-running sweep processes

The re-ask loop that produced a duplicate `dl_supplier` question (56 → 57) was caused by
**my own incident remediation**: I reverted a rescued message back to `human_processing`
while the un-hotfixed (no-cutoff) sweep was still running — it re-rescued the message 10
seconds later (`email_events` showed the `reverted` event immediately followed by a fresh
`rescued` event + a new review + question 57). During an incident, either deploy the
scope-fixing code FIRST, or leave the already-terminal message inert (`processed=true`) —
do NOT feed it back to a live sweep. `close_message_not_warehouse` was NOT at fault: it
correctly marks the message `processed=true` for BOTH `dl_item` and `dl_supplier`
(`tests/test_httpapi_new_dl.py::test_not_warehouse_terminally_closes_the_whole_dl_message_without_edi`
asserts it); the loop was re-rescue, not a close-handler gap.

## A rescue/reclassify sweep needs a per-message idempotency guard

`_recently_rescued(conn, message_id)` blocks re-rescuing a message that has a
`workflow='human_processing' stage='rescued'` event within `dl_alerts.DEDUP_WINDOW_HOURS`.
A rescue changes category and normally leaves the candidate set — but if the message is
ever returned to `human_processing` (a manual dashboard reclassify, an incident revert),
this guard stops the sweep from fighting the reclassify and re-raising the same downstream
question. Any future rescue-style sweep should be idempotent per message the same way.

## Live read-only verification recipe (used throughout the incident)

Read-only checks against the deployed add-on's own DB, through the app's config (no direct
psql quoting): base64 a small script to the HA host, `docker cp` into
`app_e0ac7775_email_extractor`, run `PYTHONPATH=/app python3` — `config.Config.load()` +
`db.connect(cfg.pg_dsn)` gives an autocommit connection for `SELECT`s and for driving app
functions directly (`dl_worker.close_message_not_warehouse(conn, qid)` neutralized a
question through the real code path, not a hand-written UPDATE). See `.claude/rules/
deploy.md` for the base64/`docker cp` mechanics and the `sudo -S` password pattern.
