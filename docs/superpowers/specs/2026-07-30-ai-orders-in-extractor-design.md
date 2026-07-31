# AI Orders — move the n8n pipeline into the extractor

Date: 2026-07-30 · Status: proposed (architecture reviewed with the user; scope decided: AI orders only)

## Problem

The `AI auto orders` n8n workflow (id `wlORIhkVZISCdZNmBTM4Z`) turns free-form customer
order emails into ORION EDI files. It is 48 nodes, ~90 KB of JavaScript inside Code
nodes and 4 LLM steps. Measured on the live DB (2026-07-30): 195 orders since 2026-06-25,
155 in the last 30 days, of which **40 (26 %) ended in "needs review"**.

It cannot be improved safely any more. Three concrete causes, all found by reading the
live workflow (not from impressions):

1. **Eight stacked rescue rules, each added after one incident, spread across three
   different Code nodes** (`PREPARE PRODUCT CANDIDATES`, `ENRICH MATCH`, `GROUP BY ORDER`):
   alias-rescue, alias-exact-with-weight, memory-rescue, borderline gate, unique-card
   rescue, weight guard, history weight-override, sibling rescue. Their **precedence is
   an accident of `if`-statement order** — it is written down nowhere, and nothing tests
   it. Fixing one order type silently changes the others. This is the user's actual
   complaint ("improve one type, break five").
2. **Nothing is reproducible.** The catalog and the customer list are read **live from a
   Google Sheet** on every run, so the same email yields different results on different
   days. There is no way to write a regression test.
3. **No tests at all**, so the confidence gate drifted 0.85 → 0.75 → 0.70 within one week,
   each move driven by a single complaint, with no measurement of what the move broke.

Diagnostics are also lost: the reason an item failed to match exists only in the n8n
execution, which n8n prunes after ~2 days.

## Goal

Reimplement the pipeline as Python inside the existing extractor add-on, with:

- an **explicit, ordered matching ladder** instead of nested rescue `if`s,
- a **decision trace** per item, persisted in Postgres,
- a **30-email golden corpus with AI evaluation** so a change that helps one order type
  cannot silently regress another,
- a **shadow phase** where the new engine runs alongside n8n and is compared to it,
  before anything is switched over.

n8n keeps running the whole time. The switch is one flag, and reverting is one flag.

## Scope

**In scope:** `category = 'ai_orders'` (155/month). Decision by the user, 2026-07-30.

**Out of scope for this spec:** `Static auto orders` (1287/month, workflow
`O8IYhUESjaWmPMTI`). It shares three things with this pipeline — the catalog, item
matching and the EDI writer — and calls `AI auto orders` for "extra content". Those three
will exist twice (Python + n8n) for the length of this project; that is accepted, and
static orders become phase 2 on top of the tested Python core. Filed as its own issue.

## Decisions

1. **Same add-on, new package** `app/orders/`, running as a second worker loop next to the
   IMAP poller. No new service, no new deploy target, same Postgres.
2. **Catalog and customers are mirrored into Postgres** (`catalog_snapshot`,
   `customer_snapshot`) hourly from the same Google Sheet
   (the "EAN slovnormal" document, tabs `all` + `companies`; the document id stays in the
   add-on options, not in git), each import getting a snapshot id. **The pipeline reads a snapshot, never the Sheet.** Same email +
   same snapshot = same result. Without this, the evaluation harness cannot exist.
3. **Extraction stays LLM; matching becomes deterministic with the LLM as one input.**
   Free-form email → orders is genuinely a language task (gpt-5.4, `reasoning: high`, per
   the standing "most expensive models" decision). Picking the catalog card is not: the
   code scores candidates, and the LLM's answer enters the ladder as one rung (rungs 5/7),
   not as the decision-maker.
4. **The ladder is data, not control flow** — an ordered list of rules, each with an
   explicit `overrides` set. Adding a rule means adding a row plus a golden case; it
   cannot reorder the existing ones by accident.
5. **Every decision is traced and stored.** `order_items.trace` records which rule fired,
   with the values it saw. This is what today is only in a deleted n8n execution.
6. **Prompts are versioned files in the repo** (`app/orders/prompts/*.md`), and the prompt
   hash is stored on every run — a prompt edit is visible in the run history and in evals.
7. **ORION upload is idempotent** via an `edi_sent` ledger keyed on
   (customer, delivery date, content hash). Double upload becomes impossible rather than
   unlikely (this is the risk behind issue #51).
8. **Odoo reporting** stays as it is: JSON-2 `message_post` into the same channels, same
   Slovak wording, same rule that every skipped or borderline item is named explicitly.

## Architecture

```
messages (category='ai_orders', processed=false)
  │  claim: processing_at + attempts   (same protocol the n8n dispatcher uses)
  ▼
orders.worker
  ├── snapshot.load()          catalog + customers, one snapshot id per run
  ├── extract.py    (LLM)      email text + attachments → Order[] with source quotes
  │     └── tables.py          deterministic parsers (Košík, price-list) override the LLM
  │                            on recognized tabular attachments
  ├── verify.py                citation check, phantom guard, quantity sanity
  ├── customer.py              exact-email → domain → name scoring → LLM tie-break
  ├── match.py                 candidate scoring + THE LADDER (below) → gtin + trace
  ├── edi.py                   fixed-width HDR/LIN/SUM writer (byte-identical to today)
  ├── upload.py                SSH to C:\ORION\COMMUNICATOR\data\in\Z-*.txt + edi_sent ledger
  └── report.py                Odoo message + email_events + messages.proc_* fields
```

### The matching ladder

Ordered, each rung declaring what it may override. First rung that fires decides;
the trace records the rung and its inputs.

| # | rule | may override |
|---|---|---|
| 1 | catalog alias equals the ordered wording **and the alias itself states a weight** | weight guard |
| 2 | delivery history, unanimous, ≥ 3 distinct deliveries | weight guard |
| 3 | catalog alias names the ordering customer | confidence gate |
| 4 | delivery history (below the confidence gate) | confidence gate |
| 5 | LLM confidence ≥ 0.85 | — |
| 6 | exactly one card of that kind in the catalog (≥ 2 core tokens, weight ratio ≤ 3×) | weight guard, flagged for review |
| 7 | LLM confidence 0.70–0.85 | —, flagged for review |
| 8 | the same wording matched elsewhere in the same email | — |
| — | otherwise: unmatched, with the reason | — |

The weight guard (ordered weight vs card weight must agree within ±10 %) and the
confidence gate (0.70) keep today's values; what changes is that their interaction with
the rescues is declared instead of emergent.

### Data model (new tables)

- `catalog_snapshot(snapshot_id, gtin, name, alias, imported_at)`
- `customer_snapshot(snapshot_id, ean_edi, name, emails[], city, street, zip)`
- `item_memory(customer_ean, item_key, gtin, card, delivered_on, source)` — unique on
  (customer, item_key, gtin, delivered_on); replaces the n8n Data Table
  `mX1EIECccDfTa9ab`, whose missing key forces a dedup workaround in the current code.
- `order_runs(id, message_id, snapshot_id, prompt_hash, status, created_at, shadow)`
- `order_items(run_id, name, quantity, unit, gtin, card, rule, confidence, trace jsonb)`
- `edi_sent(customer_ean, delivery_date, content_sha256, filename, sent_at)`

Migrations are idempotent `CREATE TABLE IF NOT EXISTS` in `db.SCHEMA`, matching the
existing extractor pattern.

## Evaluation harness

**Corpus:** 30 real emails, taken from the 195 stored `ai_orders` messages (all 195 have
their `raw.eml` on the volume, 121 have the EDI we actually produced). Coverage is by
order type, not by count:

free text · tabular attachment · price list with quantities filled in · multiple delivery
dates in one email · multiple recipient groups (patients/staff) · change request ·
ordered weight absent from the catalog · sender on a generic gmail domain · alias-driven
customer-specific card · history-driven wording · multi-order same PO number · empty /
non-order email.

Each case: input `.eml` + snapshot id + **expected output** (customer EAN, delivery date,
items with GTIN and quantity, and whether it should have gone to review). Ground truth
comes from the EDI we actually uploaded plus the warehouse's corrections in Odoo.

**Two tiers, one harness. Where each RUNS is dictated by where the corpus may live:**

- **Deterministic (LLM answers from an on-disk cache):** runs in seconds and exercises the
  whole ladder, the EDI writer and the reporting. This is the tier that catches "the Céder
  fix broke AGEL". It runs **on the add-on box**, because the corpus is real customer mail
  and never enters git — plus in CI against a committed **synthetic** corpus of the same
  incident shapes, so the harness itself is regression-tested everywhere.
- **Live (nightly and on demand):** the same corpus through real gpt-5.4; this is also what
  records the cache the deterministic tier replays.

Both tiers score per case **and per order type**, against a locked baseline: **a case that
once passed may never stop passing**, and a regression exits non-zero (so it gates a CI job
and a nightly run alike). The corpus lives at `/data/eval/manifest.json` on the volume; the
harness is `app/orders/evaluate.py` with `python -m app.orders.eval_run` as its entry
point.

Every new warehouse complaint becomes a corpus case **before** its fix is written
(`regression-test-first`). The corpus only grows.

## Shadow phase and cutover

1. **Shadow:** the worker reads the same messages but claims nothing, uploads nothing,
   posts nothing, and never sets `processed`. It writes `order_runs(shadow=true)` and a
   diff against what n8n produced for that message (customer, date, per-item GTIN,
   EDI line count). A daily digest lists only the disagreements.
2. **Cutover** when the diff is clean for several consecutive days: an add-on option
   (`ai_orders_engine: n8n | python`) switches the dispatcher path. The n8n workflow is
   **deactivated, not deleted**.
3. **Revert** is flipping that option back and re-activating the workflow.

## Failure behaviour (unchanged rules)

- Nothing is ever silently dropped: an unmatched item, a borderline match, a
  weight-override and a history-override each appear by name in the Odoo message.
- An incomplete order still ships what could be matched, with the missing items **in the
  message header** (user decision, 2026-07-30).
- A missing customer, a change request, or zero matched items still stops the whole
  document.
- A message is never reprocessed once its run uploaded to ORION.

## What measuring the corpus changed in this design (2026-07-31)

Building the 30-email corpus (#77) and running the engine over the same mails n8n had
processed corrected three assumptions above:

1. **"One email = one order" was wrong, in both directions.** n8n produced exactly one EDI
   file per email — never more — while the mails routinely ask for several delivery days (a
   July plan with 9 dates, an August plan with 13, a week with 6). So the pipeline returns a
   result **per order** (`order_results`) and the harness scores per delivery date. Conversely
   two recipient groups sharing ONE date must collapse into ONE order, because shipping them
   separately wrote two ORION documents for one day (#80, #81).
2. **n8n cannot be the oracle.** The plan said ground truth "comes from the EDI we actually
   uploaded". For 20 of the 30 cases that answer is demonstrably wrong — dropped dates, an
   order built out of quoted text, a weight-mismatched card. Each case now records which
   oracle decided it and why.
3. **The corpus cannot live in git and the gate cannot call the model.** The cases and the
   recorded answers are customer mail, in a public repo, so the gate runs on a self-hosted
   runner against a bundle on dev2 and replays recorded answers. What a case asserts is graded
   by what can be proved: exact cards, or only the number of lines, or only the dates.

## Testing rules for this package

- Synthetic fixtures for unit tests; the golden corpus is real mail and stays out of git
  (paths + hashes in git, bytes on the volume) because it is customer data.
- The ladder gets one test per rung plus one test per pair of rungs whose precedence
  matters (1 vs 6, 2 vs 5, 3 vs 7).
- The EDI writer is byte-compared against files we really uploaded.
- Coverage gate stays at the repo's 85 %.
