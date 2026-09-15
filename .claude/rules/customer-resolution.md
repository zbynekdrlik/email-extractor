---
paths:
  - "email-extractor/app/orders/customer.py"
  - "email-extractor/tests/test_orders_customer.py"
---

# Customer resolution (`customer.resolve`) — multi-site families, name stems, corpus safety

`customer.resolve()` decides WHICH customer card an order e-mail is for. Getting it wrong
ships an order to the wrong shop, so every change here is measured against the AI-orders
corpus. The rules below come from #62/#101/#159/#418/#435 incidents.

## The rung order (post-#435)

0. **Multi-site FAMILY → delivery address decides ABOVE the llm pick.** A family is a set of
   one company's branch cards. It is detected two ways:
   - `owners > 1`: cards sharing the sender's exact e-mail address, OR
   - `owners == 0` AND the model confidently picked a card (`conf >= GATE_SURE`): cards
     sharing that card's distinctive name stem (`_name_stem`).
   Within a family, `_by_store(store)` then `_by_delivery_address(delivery_text)` pick
   exactly one card → that card; no unambiguous match → `None` → the pipeline raises the
   `customer` board question. NEVER the model's silently-picked central card — that was the
   #435 Košík.sk incident (real sender `supply@kosik.sk` on no card; six orders all went to
   the Košice central card, one really for MAKRO Žilina).
1. confident llm pick (`>= GATE_SURE`, EAN in snapshot) — for a NON-family sender.
2. `exact_email` — sender uniquely owns exactly ONE card.
3. otherwise `None` (a wrongly-addressed order is worse than a review).

## Corpus safety is the hard gate — the `owners == 0` restriction is load-bearing

The stem-family path (0b) fires ONLY when the sender is on NO card. This is what keeps the
change corpus-safe: **every corpus `should_ship` case whose sender is on no card must have a
name-stem family of size 1**, or the stem path would turn a confident ship into a board
question and break the corpus. Verified against the frozen `customers.csv` on dev2 for #435
(kosik/agel/domovina/kolinovce were each unique). A sender that uniquely owns one card
(`owners == 1`, e.g. PNO Brezno via `brezno@potravinynieotraviny.sk`) is DELIBERATELY excluded
from the stem path so its unique-address resolution stays unchanged.

Before touching family/stem logic: re-run the offline corpus `--require-all` (see
`orders-corpus.md`) — a green run is the only proof the change did not regress a ship case.

## Anti-pattern: do NOT define the family by e-mail DOMAIN

Grouping cards by the sender's non-generic e-mail domain (`x@kosik.sk` → all `@kosik.sk`
cards) is TEMPTING but breaks the corpus: the "Potraviny nie otraviny" chain shares the
domain `potravinynieotraviny.sk` across branches (819 brezno@, 862 ruzomberok@, both
`should_ship`, `owners == 1`), so a domain-family would force those confident ships into a
board question. The name STEM + the `owners == 0` gate is the precise signal; the domain is
too coarse.

## `_name_stem` — reuse `dl_match.fold`, drop generic institution words

`_name_stem` folds via `dl_match.fold` (the ONE folding helper in the package — NEVER
re-derive diacritic folding, the #265 Slovak-stem lesson) then returns the first token that
is neither a pure number nor a word in `_GENERIC_NAME_WORDS`. The generic-word drop is what
keeps two genuinely-separate orgs sharing a generic institutional prefix apart ("Centrum pre
deti a rodiny Kolinovce/Lipová" → "kolinovce"/"lipova", not both "centrum"). An empty stem
(all-generic name) NEVER groups. A genuine SK chain (COOP/Tesco/Karmen) is a REAL family, so
grouping it and letting the address decide is CORRECT — do NOT add chain brand words to the
stoplist to disable grouping. Residual (review 🔵, safe-by-default): two unrelated orgs
sharing a distinctive brand token could be welded into one family, but the fallback is
always a board question on ambiguity, never a silent wrong-ship — pinned by
`test_a_brand_token_collision_falls_back_to_a_question_never_a_silent_wrong_ship`.

## Live shadow verification (read-only, in-container)

To prove a resolution change on real prod messages WITHOUT writes/reprocess: `sudo docker
exec -i app_e0ac7775_email_extractor python3 -` piping a script that loads
`snapshot.load_customers(conn, snapshot.latest_snapshot_id(conn))`, reads the message's
`from_addr`/`from_name` + `order_runs.result.extracted.notes`, and calls
`customer.resolve(...)`. `db.connect(cfg.pg_dsn)` (NOT `cfg`). `order_runs` columns are
`id, message_id, snapshot_id, shadow, status, error, result, …, model` (no `engine`
column). This is pure/read-only — no claim, no upload. The ssh user (`newlevelmedia`) has
passwordless `sudo docker`, and `ha addons …` needs `SUPERVISOR_TOKEN` from
`/run/s6/container_environment/HASSIO_TOKEN`.
