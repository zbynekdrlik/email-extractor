---
paths:
  - "email-extractor/app/orders/dl_alerts.py"
  - "email-extractor/tests/test_dl_alerts.py"
---

# `pending_alerts` durable alert outbox — retention + routing gotchas

The `pending_alerts` outbox (`dl_alerts.py`, #239) durably records DL processing-health
alerts before any Odoo delivery, retries grouped delivery on the ~15s worker tick, and
bounds its own growth. Two reusable lessons from #319 (held channel-0 retention):

## Adding retention/expiry for a NEW row-state — mirror `prune_delivered`, and fix the
## module-top docstring in the SAME change

There are now TWO bounded-retention states: DELIVERED rows (`prune_delivered()`,
`DELIVERED_RETENTION_DAYS`) and HELD channel-0 undelivered rows (`purge_held()`,
`HELD_RETENTION_DAYS`, #319). A future third retention class copies the SAME shape:

- constant + a function `def purge_x(conn, older_than_days=<CONST>) -> int:` with the same
  `max(1, int(older_than_days))` guard + `... RETURNING id` + `return len(rows)`;
- wire it into the SAME `worker.run_forever` maintenance tick right after
  `prune_delivered(conn)` (inside the existing `try/except`, no new timer/path);
- **narrow the WHERE precisely.** `purge_held` deletes ONLY `channel_id = 0 AND
  delivered_at IS NULL AND created_at < now() - window`. An undelivered row with a REAL
  channel (a genuine Odoo delivery failure) must NEVER be purged — that is the exact
  silent-loss this outbox exists to prevent. A "delete every old undelivered row" sweep
  is WRONG; only channel-0 rows (no delivery target) are safe to expire.
- **update the module-top docstring narrative in the SAME diff.** A review 🔴/🔵 caught
  the "finding 4 — unbounded growth" paragraph still calling delivered rows "the ONE
  state" with retention after a second state was added. The authoritative per-function
  docstring is not enough — the opening narrative a reader scans first must stay accurate.

## A value FROZEN at enqueue time that should later resolve to CURRENT config must be
## RE-DERIVED at flush, never read frozen off the row

`enqueue()` stores `channel_id = 0` when no ops channel is configured yet (#310). The old
`flush_pending()` read that frozen `0` off the row and skipped the group forever — so once
`ops_channel_id` WAS configured later, already-held rows never reached it. Fix:
`target = channel_id or report.ops_channel(cfg)` — re-derive the current ops channel per
channel-0 group at flush time; `if not target: continue` keeps it held while still unset
(never passes 0 downstream → no #310 misroute to the sales channel 152). Same class as
`orders-corpus.md`'s #229 note (an already-persisted `0` retroactively resolves via the
`or N` idiom): any enqueued/persisted "not configured yet" sentinel that a later config
should satisfy must be recomputed against live config at consume time, not trusted frozen.

## Testing

`test_dl_alerts.py` uses the `pg` fixture (TRUNCATEs `pending_alerts` per test).
`purge_held` retention is proven with a 4-row fixture differing on each discriminating
column (age / channel / delivered) asserting the exact survivor set — a wrong predicate on
any one condition fails it. The re-route is proven by asserting held-then-delivered across
two `flush_pending` calls with `cfg.ops_channel_id` unset then set.

## A dedup guard over this outbox must key on DELIVERED state, not just age — an
## UNDELIVERED row dedupes regardless of age; the recency window is DELIVERED-only (#327)

`already_pending(kind, message_id)` originally bounded EVERY row by `DEDUP_WINDOW_HOURS`
(4h) — correct for a DELIVERED alert (recency protection against an immediate re-ask after
delivery), but WRONG for a HELD channel-0 operator alert (`delivered_at IS NULL`, no ops
channel configured yet — the #310 hold): it never resolves, so the window expired and the
#308 `human_processing.sweep` (and the `dl_stuck_classified` sweep) re-enqueued the same
message every ~4h, piling up duplicate held rows (live #319: 65 held rows for only 10
distinct messages). Fix: the window applies ONLY to delivered rows —
`WHERE kind=%s AND message_id=%s AND (delivered_at IS NULL OR created_at > now() -
make_interval(hours => %s))`. An undelivered row suppresses a re-enqueue forever (there is
nothing to remind about while the first alert hasn't even gone out; the held row itself
still delivers once an ops channel is set, per `flush_pending`'s re-derive). **Any FUTURE
dedup guard over a durable outbox that can HOLD a row undelivered must make the same split
— bounding an unresolvable held row by a recency window silently re-duplicates it every
window.** (This supersedes the earlier "permanent per-message dedup" tradeoff note the
`n8n-workflow-edits.md` #239 entry recorded: the dedup is neither permanent nor
window-only, it is delivered-aware.)

## A ONE-TIME cleanup of accumulated garbage = a numbered migration REVISION (a DELETE),
## never a perpetual `worker.run_forever` maintenance-tick function (#327)

The pre-#327 bug left ~60 duplicate held rows. The cleanup shipped as a NEW
`Revision(3, "dedup_held_channel0_alerts", db.DEDUP_HELD_ALERTS)` in `db.REVISIONS`
(`db.py`) — a plain `DELETE FROM pending_alerts WHERE channel_id=0 AND delivered_at IS NULL
AND message_id IS NOT NULL AND id NOT IN (SELECT min(id) ... GROUP BY kind, message_id)`
keeping the OLDEST row per (kind, message_id). Chosen OVER a `dedup_held()` maintenance
function wired next to `prune_delivered`/`purge_held`: dedup is NOT a retention class (no
age threshold) — it is a ONE-TIME cleanup, and after the predicate fix no new held
duplicates form, so a perpetual GROUP-BY dedup query on the ~15s hot loop is pure waste. A
migration revision runs exactly once, atomically, tracked in `schema_version` — the honest
"jednorazové zmazanie". **Scope it as narrowly as `purge_held`** (`channel_id=0 AND
delivered_at IS NULL AND message_id IS NOT NULL`): a NULL-`message_id` alert (`spend_cap`,
`question_*`) is never deduped, so it MUST be excluded from the GROUP-BY survivor query or
distinct alerts collapse to one; a real-channel (≠0) or delivered row is never touched.
The subquery's `WHERE` must be byte-identical to the outer `WHERE` so every `min(id)` is
itself a delete-candidate (a wider subquery population could let a delivered/real-channel
row own the `min` and orphan-delete a whole group). A data-cleanup DELETE is a legitimate
migration revision (transaction-safe, idempotent on re-run) — see `schema-migrations.md`.
