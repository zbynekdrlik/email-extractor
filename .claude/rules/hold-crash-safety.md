---
paths:
  - "email-extractor/app/orders/hold.py"
  - "email-extractor/app/orders/pipeline.py"
  - "email-extractor/app/orders/worker.py"
  - "email-extractor/app/orders/static_worker.py"
---

# Never let an order be SILENTLY LOST — hold.place invariant + crash → ops alert

Two safety invariants, both from #431 (a real customer order lost silently on 2026-09-14).

## 1. `hold.place` NEVER receives `matched=None` — hold on a placeholder `Matched`, never crash

`hold.place(... matched, ...)` reads `matched.ean_edi`/`matched.name` in its INSERT. Passing
`None` used to raise `AttributeError: 'NoneType' … 'ean_edi'` deep in the INSERT, which the
worker's catch-all turned into a stuck `error` message with **no board question = a silently
lost order**. `hold.place` now refuses `matched=None` up front with a clear `ValueError`
(the single choke-point backstopping every caller).

The caller's rule: **when the customer is UNRESOLVED (`matched is None`), always hold on a
placeholder `customer.Matched(ean_edi="", name="", confidence=0.0, rule="unmatched", note="")`,
regardless of `is_change`.** The #431 bug was that the placeholder assignment in
`pipeline._run`'s date-conflict branch was nested under `if matched is None and not
is_change:`, so a CHANGE REQUEST (`isChangeRequest=True`) from an ambiguous 2-card sender
(`customer.resolve()=None`, #418) left `hold_matched=None`. The fix: `if matched is None:` at
the outer level (placeholder ALWAYS assigned), with the customer-question body nested in an
inner `if not is_change:` (a change request gets NO board question per #421, but is still
HELD — never crashed). Any FUTURE branch that builds a hold with a possibly-unresolved
customer must follow the same shape: resolve the placeholder FIRST, gate the QUESTION on
`is_change`, never gate the placeholder on it.

The crash needs ALL of: `matched is None` (2-card/ambiguous sender, #418) + `is_change=True`
(#421) + a `date_conflict` (#420) — three branches that never met in the tests. When adding a
test for a hold path, cover the `is_change=True` combination explicitly, not only
`is_change=False` (that path already had the placeholder and never crashed).

## 2. A final-attempt pipeline crash must PING OPS, not just log an error event

`worker.tick` AND `static_worker.tick` (byte-parallel, keep them in lock-step like the
#372/#373 retry logic) catch any pipeline exception. #330 made the FINAL attempt
(`attempts >= MAX_ATTEMPTS`) log a `report.log_event(status="error")` (dashboard-visible), but
nothing pinged the operator — a deterministic crash stayed invisible on the phone. Both engines
now ALSO enqueue a durable ops-channel alert on that final attempt:

```python
ops_ch = report.ops_channel(cfg)
if not dl_alerts.already_pending(conn, "<engine>_pipeline_crash", message["message_id"]):
    body = (f"<p>{escape(report.crash_outcome(e, 'run_live'))}</p>"
            + dl_alerts.item_line(message.get("from_addr", ""), message.get("subject", "")))
    dl_alerts.enqueue(conn, ops_ch, "<engine>_pipeline_crash", body,
                      message_id=message["message_id"])
```

- Distinct `kind` per engine (`order_pipeline_crash` / `static_order_pipeline_crash`) so dedup/
  grouping stay separate. Dedup via `already_pending` (belt-and-suspenders; the branch already
  fires at most once per message because `_claim` won't re-claim past `MAX_ATTEMPTS`).
- **`escape()` the `crash_outcome`** before HTML interpolation — `str(exc)` can carry `<`/`>`/`&`
  (every other `dl_alerts` body escapes its dynamic text). `from html import escape`.
- Status stays `'error'` (NOT `'review'`): a crash is an error, and `'review'` would let the
  review-list consumers in `httpapi_dashboard_data.py` treat a crashed message as a normal
  review item and mask it. The ops alert is the operator-visibility fix, not a status change.

## 3. Releasing a crashed message for reprocess — reset `attempts=0`, the 3-column reset is NOT enough

A message that crashed `MAX_ATTEMPTS` (5) times sits at `attempts=5`. `_claim`'s guard is
`COALESCE(attempts,0) < MAX_ATTEMPTS`, so the sanctioned 3-column reset alone
(`processed=false, processing_at=NULL, processed_by=NULL`) will NEVER be re-claimed — you MUST
also set `attempts=0` (the full re-send reset, see `n8n-workflow-edits.md`). Safe whenever the
crash was PRE-ship (verify: no `edi_sent`/ORION file — a `hold.place` crash is always pre-ship,
so there is zero duplicate-upload risk). A change-request + unresolved-customer order never
auto-ships anyway, so releasing it only surfaces it on the board.
