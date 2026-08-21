---
paths:
  - "email-extractor/app/orders/hold.py"
  - "email-extractor/app/orders/teach.py"
  - "email-extractor/app/httpapi_orders_questions.py"
  - "email-extractor/app/httpapi_templates.py"
---

# Editing an order line on the board so the CONFIRMED value ships (#360)

The warehouse board (`/sklad` + the dashboard) lets the sklad confirm/correct a line's
quantity + unit price before answering an `item` question. Two hard-won rules govern any
future "human-corrects-a-value-then-it-ships" board feature.

## A held order can wait on SEVERAL item questions — persist the confirmed value on the
## question row and read back EVERY answered question at ship time, never thread just one

The FIRST cut of #360 threaded a single `confirmed_quantity` for the ONE question being
answered into `hold.release_for_question`/`_release_locked`. A fresh-context review PROVED
this ships the WRONG quantity in a multi-question hold: `_release_locked` returns at the
`remaining > 0` check BEFORE applying the correction, so a question answered BEFORE the last
one has its correction discarded — the order re-loads `held_orders.decisions_json` (original
extracted qty) and only the LAST-answered question's value is applied.

**The correct pattern (what shipped):** `teach.answer` persists the confirmed value onto
`order_questions.quantity`/`unit_price` (COALESCE — `None` keeps the existing value). At
ship time `hold._apply_confirmed_quantities(conn, decisions, question_ids)` reads back the
persisted quantity of EVERY answered `item` question of the held order and applies each to
its matching decision by `memory.item_key`. This is uniform for single- AND multi-question
holds (for an un-corrected question the persisted qty equals the decision's extracted qty →
a no-op) and needs NO `confirmed_quantity` param. Runs on the RAW loaded decisions BEFORE
`_redecide` (which copies `d.quantity`). Apply each value to exactly ONE decision per key
(first match, then consume the key) — two identically-worded lines share one question but
stay separate decisions that `merge_same_card` later SUMS, so applying to both doubles it.
Regression test: `test_a_correction_on_an_earlier_answered_question_of_a_multi_question_hold_still_ships`
(answer the earlier question with a corrected qty, then the last; the earlier correction must ship).

## `_num` for a board-submitted quantity/price must reject `<= 0`, not just negatives

`app/httpapi_orders_questions._num` coerces a board number (JSON number, Slovak-decimal
string "12,50", blank/absent). It rejects `<= 0` (returns `None`), NOT just negatives: a
mis-entered 0 must FALL BACK to the extracted quantity (COALESCE keeps it), never ship a
`0.000` ORION LIN (`edi.build` appends a LIN for any non-`NO_MATCH` gtin regardless of qty).
Also rejects `bool` (an int subclass). This is a real-money path — a wrong/zero quantity
ships to the warehouse.

## Price is display + stored correction ONLY — the ORION ORDER_ EDI has no price field

`edi.build`'s fixed-width LIN carries line#, GTIN, quantity, unit `PCE`, description — NO
price field (byte-parity port pinned by `edi_reference.json`). So the board price is a
verification value + stored correction, labelled honestly ("cena sa neposiela do ORIONu —
len kontrola"); never send it to ORION. `unit_price` lives ONLY on `order_questions`
(a migrate revision), not on the `Decision`/`order_items` (deliberately — price never ships
nor is displayed after answer, so threading it through the Decision's ~12 construction sites
incl. the DL engine buys nothing).

## Adding an optional field to `ORDER_SCHEMA` invalidates ONLY the extraction cache

Adding `unitPrice` (optional, NOT in `required` — `llm.json_call` is `strict:False`) changes
the extraction schema hash, so the `e2e-orders` corpus gate CacheMisses every extraction
call and must be `--live` re-recorded (`orders-corpus.md`). But the MATCHING cache
(`_item_input`) is keyed on item name + candidates, unchanged by a pure extraction-schema
edit → matching replays from the untouched cache, so only extraction re-runs and the
baseline holds as long as extraction still produces the same items/dates. DE-RISK cheaply:
`--live --sample 5` first (~$0.75) — if the sampled outcomes still pass, the full 30+-case
re-record is safe; then verify offline with `--require-all` (= exactly what CI runs).
