---
paths:
  - "email-extractor/app/orders/human_processing.py"
  - "email-extractor/app/httpapi.py"
  - "email-extractor/app/httpapi_dashboard_data.py"
---

# Clearing the ops backlog (`human_processing`) — the sanctioned reclassify procedure

`human_processing` is a large CATCH-ALL, not a small "a human must handle this" bucket:
~846 messages accumulated since June — payslips, `profesia.sk` job-board replies, HR mail,
NOT stuck warehouse documents (`human-processing-sweep.md`, #308 incident). The live
`human_processing.sweep` deliberately NEVER touches this historical backlog:
`BACKLOG_CUTOFF = "2026-08-14"` (a fixed date) excludes every message received before the
sweep first went live, so nothing older ever auto-enters a pipeline or channel. That
backlog therefore has to be cleared DELIBERATELY, by an operator, from the dashboard — this
is how.

## What to do — reclassify each backlog message to `no_processing`

`no_processing` is a TERMINAL category with no processor and it is NEVER a rescue target
(`human_processing.PROCESSOR_CATEGORIES` excludes it) — so moving a message there parks it
for good, visible in the dashboard's reviewed list, without ever re-running anything.

1. **Log in** (the dashboard is password-gated): `POST /login` with the dashboard password
   (`dash_password`, memory `email-extractor-deploy.md` — never in git) → the response sets
   the session cookie. Reuse that cookie (`credentials: "include"`) for every call below.
2. **Reclassify** each backlog message by its `messages.id` (the integer PK, not
   `message_id`): `POST /api/message/<pk>/reclassify` with body
   `{"category": "no_processing"}` (`api_reclassify`, `app/httpapi_dashboard_data.py`).

That endpoint runs exactly one UPDATE:

```sql
UPDATE messages
   SET original_category = COALESCE(original_category, category),
       category = 'no_processing', human_reviewed = true, review_status = 'corrected', ...
 WHERE id = %s
```

and logs a timeline event with **`rollup = false`** — an operator action, NOT a pipeline
stage, so it never overwrites `proc_status`/`proc_outcome` and nothing downstream re-runs.

## Why this is safe + reversible

- **`original_category` is preserved** (`COALESCE(original_category, category)` — set once,
  never clobbered on a re-reclassify), so the move is fully reversible: reclassify back to
  the original category and the right engine picks it up again.
- **Nothing re-runs**: `no_processing` has no processor, and the reclassify event is
  `rollup=false`, so no sweep, no engine, no Odoo post fires. It is pure bookkeeping.
- **`human_reviewed = true` / `review_status = 'corrected'`** moves the row into the
  dashboard's reviewed list (out of the "needs review" filter), so a cleared backlog stays
  visibly cleared and is not re-surfaced.

## Do NOT

- Do NOT lower/remove `BACKLOG_CUTOFF` to "let the sweep clear the backlog" — that dumps
  months of non-warehouse mail into the ops channel + (via vision rescue) live pipelines,
  the exact #308 incident. The backlog is cleared by the operator reclassify above, never
  by the automatic sweep.
- Do NOT reclassify a backlog message to a PROCESSOR category (`dodacie_listy`/`ai_orders`/
  `static_orders`/…) unless it genuinely is that document AND is within the DL engine's own
  age cutoff (`delivery_notes_max_age_days`, #339) — an old delivery note reclassified into
  `dodacie_listy` is exactly the duplicate-delivery risk #339's age gate exists to catch (it
  will route to review, not ship, but don't rely on that as a bulk-clearing shortcut).

The ops-ALERT side of the same area (the grouped `human_processing_review`/
`dl_stuck_classified` notifications this backlog produces) is `dl_alerts.py`'s job — one
readable header + short lines + a once-daily morning reminder (#336, `dl-alerts.md`), not a
per-item wall.
