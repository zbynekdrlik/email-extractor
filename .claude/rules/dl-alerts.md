---
paths:
  - "email-extractor/app/orders/dl_alerts.py"
  - "email-extractor/tests/test_dl_alerts.py"
  - "email-extractor/app/orders/confirm.py"
  - "email-extractor/app/orders/confirm_carryover.py"
  - "email-extractor/app/orders/confirm_manual.py"
  - "email-extractor/tests/test_orders_confirm*.py"
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
`WHERE kind=%s AND message_id=%s AND (delivered_at IS NULL OR delivered_at > now() -
make_interval(hours => %s))` (the recency term was anchored on `created_at` here until
#334 corrected it to `delivered_at` — see the #334 section below). An undelivered row
suppresses a re-enqueue forever (there is
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

## The DELIVERED-row dedup window must anchor on `delivered_at`, never `created_at` (#334)

The #327 split above bounds a DELIVERED row's dedup by a recency window — but that window's
recency term must be measured from **`delivered_at`** (the actual delivery), NOT `created_at`.
`flush_pending()` only ever writes `SET delivered_at = now()`; it NEVER touches `created_at`.
So a row that sat HELD for days at `channel_id=0` (the #310/#332 backlog, waiting for an ops
channel) then finally delivered has a `created_at` far outside the 4h window. Anchoring the
window on `created_at` (`created_at > now() - make_interval(hours => %s)`) made the
just-delivered row stop matching the dedup predicate the instant it delivered — so the #308
`human_processing.sweep` (whose candidate query re-selects `category='human_processing' AND
processed=false` forever, never durably marking a message notified) re-enqueued the same
`message_id` ~15s later, and the ops channel got a DUPLICATE grouped post, repeating every ~4h.
Live incident 2026-08-16 21:42: right after the ops channel was configured (#332), 15 held
`human_processing_review` alerts delivered, then 15s later the same 10 message_ids were
re-enqueued and channel 592 got a duplicate post.

Fix (one word in the predicate + docstrings): `delivered_at IS NULL OR delivered_at > now() -
make_interval(hours => %s)` — mirrors `confirm.py`'s `last_alert_at` recency (measured from
the last alert). This preserves the intended 4h reminder cadence, now measured from the real
delivery. **Any FUTURE recency/reminder window over a durable outbox whose rows can be HELD
undelivered for an arbitrary time before delivery must anchor on the DELIVERY timestamp
(`delivered_at`/`last_alert_at`), never the creation timestamp** — `created_at` silently
breaks the instant a held-then-delivered row exists, which is exactly the state this outbox
is built to support. Regression: `test_dl_alerts.py::
test_already_pending_a_held_then_delivered_row_still_dedupes_even_though_created_at_is_stale`
(insert `created_at = now() - interval '3 days'`, `delivered_at = now()`, assert
`already_pending()` is True). The two pre-existing delivered-row window tests were re-scoped
to age `delivered_at` instead of `created_at` (the same "re-scope the tests that encoded the
old anchor" step #327 itself did when it moved the window to delivered-only).

## Grouped ops alerts: short per-item lines + a per-kind flush-time header, and a
## once-daily-morning re-reminder cadence (#336)

The pre-#336 wall: `human_processing._notify`/`dl_worker.stuck_classified_sweep`/
`dl_document._alert_and_release` each enqueued a FULL explanation sentence + Od/Predmet +
a microsecond timestamp PER message, and `flush_pending` just `"".join`-ed the group — so N
stuck messages produced N repeated sentences (a live 3177-char `human_processing_review`
post). Fixed by splitting the two concerns:

- **Short per-item body + per-kind header at flush.** Each enqueue site now stores ONE
  short line via `dl_alerts.item_line(sender, subject, received=None)` (`• odosielateľ —
  predmet (prijaté D.M.)`, HTML-escaped, no microsecond timestamp). `flush_pending` renders
  the group: for a kind in `GROUPED_ITEM_KINDS` (a `{kind: header_template}` registry, the
  template carrying `{n}`) it builds ONE header (count + explanation + a
  `report.dashboard_link(cfg)` action link) + up to `DISPLAY_ITEM_CAP` (10) lines +
  „… a ešte N ďalší/ďalšie/ďalších" (`report.more_line`, #476); every OTHER kind keeps the legacy `"".join` (question_reminder/escalation
  already store a full formatted body from `question_alerts._group_html`; spend_cap is a
  one-off). **To add a FUTURE grouped ops kind: add it to `GROUPED_ITEM_KINDS` AND make its
  enqueue site call `item_line` — do only ONE and it either double-wraps or stays a wall.**
  Mirrors `question_alerts._group_html`'s own header/cap/„…ďalších"/link convention — reuse
  that shape, don't invent a second one.
- **Cadence: `reminder_suppressed(conn, cfg, kind, message_id, now=None)` replaces the flat
  4h `already_pending` re-ask for the two SWEEP kinds** (`human_processing_review`,
  `dl_stuck_classified` — the only ones that re-discover the same stuck message every tick).
  The FIRST alert always fires (any hour/day — never delay a first notification); a
  RE-reminder fires at most ONCE per day, only after the configured morning hour, skipping
  Sat/Sun (reuses `confirm.morning_check_active` + `confirm.LOCAL_TZ` — the SAME carryover
  cadence, not a second policy). Anchored on `delivered_at`, never `created_at` (#334): a
  held-then-delivered row's daily gate must measure from the real delivery. While a row is
  still undelivered there is nothing to remind about → suppress regardless of age (#327).
  `already_pending` is KEPT unchanged (still a valid delivered-window primitive with its own
  #327/#334 tests); `reminder_suppressed` is the new, separate cadence built on the same
  `delivered_at` anchor.

## The grouped action link is chosen by the RECIPIENT channel, never one admin link for all (#473)

`_format_grouped` used to append `report.dashboard_link(cfg)` (the password-gated admin
dashboard) to EVERY grouped kind — including the two that reach the WAREHOUSE delivery-notes
channel 243 (`dl_upload_failed`, `scanner_not_dl`, 66 scanner posts in 30 days), where nobody
has the password (after #470 the bare base lands on `/login`). `flush_pending` now passes the
group's delivery `target` + its rows' `message_id`s, and `_action_line` decides:

- `target` provably the ops channel (truthy AND `== report.ops_channel(cfg)`) → admin
  dashboard, unchanged — the owner reclassifies there. FAIL-CLOSED: anything not provably ops
  (`None` / `0` with the ops channel unset / a warehouse channel) never gets the admin link;
  `_format_grouped`'s `channel_id` is a required keyword.
- any other (warehouse) channel → `WAREHOUSE_HISTORY_KINDS[kind]` board History link
  (`dl_upload_failed` → `dl_history`: ONE mail → its detail `?q=<mid>` ONLY if that tab lists it
  — `_deep_link_mail` applies the detail route's own `is_history_document` guard, so an
  invoice-as-DL mail gets the tab, never a 404 deep link; several → the tab); a kind NOT in that
  map (`scanner_not_dl`, a non-DL scan never listed in the DL history, action = rescan) gets NO
  link — never the admin fallback.

**To add a warehouse-channel grouped kind:** put it in `GROUPED_ITEM_KINDS` + (if its mail is in
a História tab) `WAREHOUSE_HISTORY_KINDS`, and call `item_line` at enqueue. Tests:
`test_dl_alerts.py::test_a_warehouse_*` + `test_an_ops_channel_alert_keeps_the_admin_dashboard_link`
+ `test_the_admin_link_is_fail_closed_to_the_provable_ops_channel` +
`test_an_upload_failed_alert_for_a_mail_the_dl_history_does_not_list_links_the_tab`.
The full "which message links where" table lives in `board.md` (#473 section).

Testing: `test_dl_alerts.py` proves the format (`test_format_grouped_...` — 12 items → one
header + 10 lines + „a ešte 2 ďalšie" (#476) + dashboard link, explanation appears ONCE) and the
cadence (`test_reminder_suppressed_...` — explicit `now=` at Sat-afternoon/Mon/Tue/weekend
crossings, delivered_at set to a prior day). The three flush-mechanics tests that encoded
the old exact-`"".join` body were re-scoped to substring assertions; `test_dl_worker.py`'s
`..._stamps_a_detection_time...` (the removed microsecond timestamp) became
`..._uses_a_short_line_with_no_microsecond_timestamps`.

## Import-confirmation (`confirm.py`) carryover alerts count + name ONLY the files still
## waiting — never the incident's whole membership (#476)

`confirm.py` posts its grouped ORION-import alerts DIRECTLY (not through `pending_alerts`), one
open incident per (channel, kind, source), members in `import_alert_incident[_desadv]_members`.
Live 30.9.: the delivery-notes channel was reminded „Stále 4 dodacie listy neprevzatých" twice a
day while 3 of the 4 had been imported two days earlier — the reminder printed `_open_incident`'s
`file_count` = `count(*)` over EVERY member ever added. Rules that fell out:

- **"Still waiting" = no terminal `import_status` AND `_decide()` on THIS sweep's own
  `list_dirs()` listing still finds the file in its queued folder.** The second condition
  matters: a member whose own throttled re-check (5 min) has not run yet can already sit in
  `archCodex` — the ledger still says NULL, the listing says imported. A reminder counts
  `confirm._pending_members`; imported members STAY in the incident (history +
  `_check_incidents_for_clear`'s per-member scope) — never delete them to "fix" a count.
  `failed`/`unknown` reminders keep the whole membership (terminal members never self-heal).
- **The throttle spreads the rows out — a group of due rows is NOT the list of waiting files.**
  Each row has its own 5-minute re-check cycle, so the first sweep past the morning hour often
  has 1 of N waiting files due (review 🟡, probe-reproduced: „1 dodací list" for 4 waiting).
  `sweep` therefore WIDENS every carryover group with `_unresolved_rows` (no throttle) that are
  still queued, `_stuck` (the one morning/same-day predicate) and on the same channel — the
  opening alert and the incident name all of them. A carryover group exists only while
  something waits, so there is no "reminder about nothing"; once nothing waits the incident
  closes through the unchanged all-clear path.
- **Every carryover alert lists its files** (`confirm_carryover.items`: „DL <číslo>
  (<dodávateľ>)" from `dl_snapshot.dl_suppliers_for_management`, „objednávka na <deň>
  (<odberateľ>)" from `snapshot.customers_for_management`) through `report.capped_list` — the
  capped `<ul>` + `report.more_line`, the ONE „… a ešte N ďalší/ďalšie/ďalších" whose remainder
  word agrees with N (also `question_alerts._group_html` and `dl_alerts._format_grouped` — one
  kind per post, every header noun masculine — which all printed „a 1 ďalších"). Slovak
  agreement everywhere via `confirm._agree` (1 masc dodací list / 1 fem objednávka / 2-4 / 5+:
  neprevzatý/-á/-é/-ých, skončil/-a/-i/-o, zmizol/-a/-i/-o; ho/ju/ich; je/sú). A reminder's
  „od" is the OLDEST UPLOAD among the waiting files (a never-importing file keeps an incident
  open for days while newer files join), with the day when it is not today. ONE spelling in
  the alert and its all-clear: „CODEX" / „v CODEXe" (like the #467 messages).
- **WHY a DESADV never imports is read, not guessed.** CODEX rejects a WHOLE delivery note when
  one LIN code has no stock card (#467). While building a carryover alert the waiting DESADV
  files are read READ-ONLY from `in_DL` (`upload.read_files`: one SFTP session,
  `sftp.open(path, "r")` only; latin-1 decode keeps fixed-width offsets byte-exact; a file gone
  between listing and read is skipped) and `desadv_edi.lin_codes` (the inverse of `generate()`'s
  LIN layout, `LIN_CODE_AT` + `GTIN_FIELD_WIDTH`; it splits on `\n` ONLY — the file is uploaded
  as UTF-8 and read back as latin-1, so an „Å" arrives as „Ã\x85" and `str.splitlines()` breaks
  on \x85, letting a HDR tail pose as a fake code) is checked against `codex_cards.live_guard` →
  „DL … : CODEX ho neprevezme — kód X v CODEXe neexistuje. Zadajte ho ručne.", that file first,
  plus ONE note with the card list's date (a card made after the last push is not in it yet).
- **Fail-open END TO END — a detail lookup never loses the alert** (review 🟡: the first cut
  guarded only the SFTP read; a DB error in `live_guard` or the name lookup raised through
  `_handle_group`/`sweep`, lost the alert AND skipped the rest of the worker tick).
  `confirm_carryover.items` wraps the name lookup and the whole CODEX check (`live_guard` is
  consulted BEFORE any ORION read — stale / never pushed → no read at all) and logs; the alert
  goes out with at least „DL <číslo>" per file. Pinned with a REAL failure (the tables renamed
  away inside the test, restored in `finally`), not a mock. Reads happen only while an alert is
  being built (opening or due reminder), never on an ordinary sweep.
- **Tests inject `now` (Aug-2026 constants) — a CODEX push in a test must use `source_as_of`
  relative to THAT `now`** (`TUE_MORNING - 1h`), not `datetime.now()`: `live_guard(conn, now)`
  measures staleness against the injected time, and `_data_as_of` is `min(source_as_of,
  synced_at)`, so a real-clock push looks ~2 months in the future and a real-clock "now" in the
  sweep can land on a weekend (no carryover at all). Pins: `tests/test_orders_confirm_carryover.py`
  (the 4-member/3-imported regression, staggered re-checks, the not-yet-rechecked archCodex
  member, unknown-code line + list date, stale / never-pushed / unreadable / DB-failure
  fail-open, `lin_codes` round-trip through `generate()`, `read_files` + the production reader
  never write, agreement + `capped_list`).
- **Mutation guards for the widening** (review rounds 2-3 — `_stuck`, the channel filter, the
  carryover-only kind filter, both sorts and `min(uploads)` all survived deletion before): a
  file uploaded THIS morning is never pulled in (the #133 false alarm), a row routing to another
  channel never joins, a waiting file is NEVER widened into a failed/unknown group (those rows
  go terminal — it would be stamped `failed` and never self-heal), the list reads oldest upload
  first with a CODEX-rejected file on top, and „od" is the oldest waiting upload — each has its
  own test; a fixture that is ALREADY in the asserted order proves nothing, build it reversed.
- **Live read-only check of the text** (no Odoo post): in the add-on container build it with
  `confirm._carryover_html(conn, confirm._pending_members(conn, <incident id>,
  upload.list_dirs(cfg), confirm.DESADV_LEDGER), confirm.DESADV_LEDGER, reminder=True,
  read_files=lambda n: upload.read_files(cfg, cfg.orion_dl_dir, n), now=datetime.now(UTC))`.

## A DL the warehouse typed into CODEX BY HAND = terminal `import_status = 'manual'` (#494)

Live 13.10.: CODEX refused 4 DESADVs of one supplier, the warehouse typed the 4 DLs in by hand,
our files stayed in `in_DL` — and the carryover reminder kept telling the warehouse „treba ich
prijať v CODEXe" every morning (following it = a duplicate receipt). `confirm_manual` (called
from `confirm.sweep` via `_mark_manual`) fixes it with a FOURTH terminal status:

- **The signal = `codex_receipts` (#485):** a receipt of the SAME supplier
  (`codex_receipts.for_supplier` → `supplier_eans @> ARRAY[ean]`) whose `dl_numbers` contain
  the row's `doc_number` compared via `invoice_dedup.digits`/`numbers_of` (digits only, leading
  zeros and prefixes dropped, < 5 digits never matches — CODEX stores numbers as DOUBLE, a
  printed `0100…` arrives without its zero). NO freshness gate (`codex_receipts.live()` is NOT
  used): a receipt is evidence of PRESENCE, an older copy is still true; no copy / no match / a
  failing lookup = the row keeps waiting (logged). archCodex is decided FIRST — CODEX's own
  import of our file is ALSO a receipt with our DL number, it must stay `imported`.
- **Date floor (review round 1):** the receipt's `receipt_date_to` must be ≥ the row's
  `desadv_sent.delivery_date` (the #485 delivery fact) − `invoice_dedup.DATE_WINDOW_DAYS`. A
  receipt cannot predate the delivery it books; an older one with the same digits is a reused
  counter or an INVOICE number typed into CODEX's DL field („often the invoice number", #485) —
  marking on it would drop a genuinely waiting delivery from the reminder and tell ops to delete
  its file. A row WITHOUT `delivery_date` (only rows before #485, none waiting at deploy) is
  never marked. The warehouse back-dates a hand-typed receipt to the delivery day (live: typed
  13.10., booked 2.10. = the delivery day) — so the floor is the DELIVERY day, never
  `uploaded_at` (that upload was 5 days after the delivery).
- **Runs BEFORE any alert of the sweep is built, over EVERY unresolved DESADV row still queued in
  the listing** (`_unresolved_rows`, no throttle) — a carryover alert/reminder names every waiting
  file (#476), so marking only the throttled due rows would let a reminder in the 5-minute gap
  still list a manual file. The marked ids are then dropped from that sweep's `desadv_rows`
  (else a stale due-row dict would still be grouped as carryover in the same pass).
- **Terminal = everything else falls out for free:** not in `due_rows`/`_unresolved_rows`/
  `_pending_members` (all `import_status IS NULL`), never `_decide`d again (so deleting the file
  is never the `unknown` alarm), and `_check_incidents_for_clear` closes its carryover incident
  with the ordinary all-clear. `import_status` is TEXT without CHECK → no migration.
- **`import_confirmed_at` of a manual row is NOT an import pass:** `_activity_today_at` (#255)
  reads `import_status = 'imported'` rows only — counting a manual mark would flag every file
  uploaded earlier that day as a same-day rejection. Any FUTURE terminal status that writes
  `import_confirmed_at` must keep that filter.
- **ONE ops alert per file, never repeated, never the warehouse thread:** grouped ops kind
  `dl_manual_in_codex` (`GROUPED_ITEM_KINDS` header: check the receipt in CODEX, then delete the
  file + one `dl_alerts.item_line` per file: DL, supplier, CODEX receipt, exact wire file name
  `Z-DESADV_…`; the ops channel gets the dashboard link per #473), outbox key
  `desadv-manual:<desadv_sent id>`, channel `report.ops_channel(cfg)`
  (0 → held, delivered once an ops channel is set). The guarded `UPDATE … WHERE import_status IS
  NULL RETURNING id` and `dl_alerts.enqueue` run in ONE `conn.transaction()` — `manual` without
  its alert (a silently swallowed „delete the file") or a second alert are both impossible.
- **The app never touches the file on ORION** (design: no automatic delete outside the upload,
  #239). A human deletes it on the ops alert; the row is already `manual`, so no alarm follows.
- Pins: `tests/test_orders_confirm_manual.py` (manual + one alert, never repeated, grouped
  delivery to ops, escaping, no receipt / other supplier / short number / older receipt /
  undated row unchanged, one-day window + days-later receipt count, digits normalisation incl.
  prefixes, archCodex wins, fail-open lookup, transaction rollback, guarded update, vanished
  manual file = no alarm, imported + manual incident closes, not-yet-due manual file never in a
  reminder, manual mark ≠ activity). Mutation-checked: removing the activity filter, the
  due-row filter, the all-rows pass, the `IS NULL` guard, the transaction, the multi-number
  match, the ops channel, the lookup `try`, the date floor or its window each fails a named
  test. Fixtures use MADE-UP DL / receipt numbers — never the incident's (public repo).
- **Known residual (not handled, deliberately):** `manual` is terminal and the one ops alert is
  the only safeguard — if nobody deletes the file and CODEX later imports it, the duplicate
  receipt is not detected. Also a later deletion of the hand-typed receipt in CODEX never turns
  the row back to waiting.
- **Live read-only check after a deploy:** `SELECT id, doc_number, import_status,
  import_confirmed_at FROM desadv_sent WHERE import_status = 'manual'` + `SELECT channel_id,
  kind, message_id, delivered_at FROM pending_alerts WHERE kind = 'dl_manual_in_codex'` (one row
  per file, channel = ops 592, never 243).
