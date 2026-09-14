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

## „Genuinely new X right on the question card" now has an ORDERS-item member — mirror it exactly (#426)

The board's "create the missing thing right on the question, one click" pattern now covers
all three orders/DL kinds: customer (`new_customer`, #234), DL supplier/product
(`new_supplier`/`new_item`, #235), and — since #426 — the ORDERS item card
(`new_product`). Any FUTURE addition of this shape reuses the SAME structure; two traps
specific to the item kind:

- **The item candidate dict for `teach.add_candidate` uses key `"gtin"` + `"name"`, NOT
  `"value"`/`"label"`.** `teach.answer` (the item path) checks
  `offered = {str(c.get("gtin")) for c in candidates}` — a `{value,label}` candidate (the
  shape the DL/generic kinds use, because they route through `_api_orders_answer_generic` →
  `kind.validate`) would be invisible to it. `new_product` routes through `teach.answer`
  directly (like the normal item tail), so it must add `{gtin,name}`.
- **`teach.answer` accepts a freshly-created gtin two ways, both belt-and-suspenders:**
  `add_candidate` puts it in the question's own offered set, AND `rebuild_from_overrides`
  re-freezes it into `catalog_gtin_set(conn)` (which `answer` also checks). Either alone
  would suffice; `_api_orders_answer_new_product` does both, mirroring `new_customer`.
- **Same two-connection discipline (#116):** `upsert_catalog_card` + `rebuild_from_overrides`
  + `add_candidate` + `teach.answer` in ONE `deps.db_tx()`; `hold.release_for_question`
  AFTERWARDS on a separate `deps.db()` (autocommit) — a real ORION upload must never sit in
  a rollback-able tx. The 409 collision check runs inside the same `db_tx` before any write
  (a `return 409` there has written nothing, so it is a harmless empty commit).
- **The UI form `newProductForm(q)` mirrors `newDlProductForm`** but posts
  `{new_product:{gtin,name,doplnok?}, quantity, unit_price}` (carries the `lineFields`
  `oqty_/oprice_` values, like `teach()`), sets `form.dataset.open` so the 5 s refresh
  (`boardBusy()`) never wipes a half-filled form, and on a 409 renders „Použiť existujúcu
  kartu" → `teach()` (which itself re-reads the qty/price inputs). The inline `item` branch
  in `load()` was extracted into `itemQuestionCard(q)` — DOM behaviour byte-preserved.
- **Editing `_ASK_HTML_TEMPLATE` changes BOTH `ASK_HTML` and `ASK_DL_HTML` hashes** (they
  share the template) — re-pin both in `test_httpapi_characterization.py`
  (`# airuleset:secret-ok` on the sha line, and append that same bypass to the `git commit`
  command, since the 64-char hex trips `block-sensitive-staging.sh`).

## Any question-settle write needs the atomic `WHERE status='open' RETURNING` guard, and side effects go AFTER it (#428)

`teach.answer` (the `item` settle — also reached by #426 `new_product` and the #360 line
edit, since they call it) originally did a NON-atomic check-then-act: a Python
`if q["status"] != "open": raise AlreadyAnswered` (read from an earlier SELECT), then
`UPDATE … SET status='answered' … WHERE id = %s` with **no `AND status='open'`, no
`RETURNING`**, and the two `memory.remember*` writes ran **BEFORE** that UPDATE. Two
concurrent answers (two people / a double-click) both passed the Python check, both wrote
memory, and the second UPDATE silently overwrote the first (no `edi_sent`/`_release_locked`
duplicate-ship, but a lost answer + a duplicate memory write).

The fix mirrors the already-hardened `answer_customer` (#234) and `_api_orders_answer_generic`
(#323) — the invariant for EVERY question-settle path in this codebase:

- Settle with an atomic guard: `UPDATE order_questions SET status='answered', … WHERE id=%s
  AND status='open' RETURNING id`. Under READ COMMITTED the row lock serializes racers; the
  loser matches 0 rows.
- On 0 rows → `raise AlreadyAnswered` (re-read for the message detail) and apply **NO** side
  effect. The endpoint turns that into a 409 ("otázka už bola zodpovedaná" → client refreshes).
- Put memory/release side effects **AFTER** the successful guard, never before — so the loser
  writes nothing. (Keep the early Python `status != 'open'` fast-path too; `answer_customer`
  keeps both — it just is not the real guard.)
- Callers stay unchanged: `api_orders_answer` (item tail) and `_api_orders_answer_new_product`
  (#426 — whose whole `db_tx`, incl. the `catalog_overrides` card write, rolls back on the
  raise) already catch `AlreadyAnswered → 409`; `_apply_item` calls `answer()` so it inherits it.

When adding a NEW `teach.KINDS` kind or any new answer path, copy this shape — the guard
belongs on the WRITE, not in one caller. RED proof = the `run_racers` helper (`tests/_race.py`):
two real threads/connections that teach DIFFERENT gtins, asserting exactly one winner, one
`AlreadyAnswered`, and exactly one human `item_memory` row (a leaked loser-write shows as a 2nd
row because the conflict key is `(customer_ean, item_key, gtin, delivered_on)`).
