---
paths:
  - "email-extractor/app/orders/customer.py"
  - "email-extractor/app/orders/store_key.py"
  - "email-extractor/app/orders/store_rules.py"
  - "email-extractor/app/orders/order_customers.py"
  - "email-extractor/app/orders/customer_layers.py"
  - "email-extractor/app/orders/codex_customers.py"
  - "email-extractor/app/orders/snapshot.py"
  - "email-extractor/tests/test_orders_customer.py"
  - "email-extractor/tests/test_orders_store_*.py"
  - "email-extractor/tests/test_codex_customers*.py"
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

## #493 — a customer with SEVERAL SHOPS ordering in ONE mail (one attachment per shop)

Incident 2026-10-16: five xlsx files (headers „BISTRO", „Predajňa: Tržnica", „ŽILINA",
„ŠKOLA, <ulica>", and an empty „Predajňa:" whose shop is only in the file name „… PR.xlsx"),
the sender address on ONE card → `llm`/`exact_email` for every order → 21 ORDER files under
one EAN (16 wrong). Three shops existed only in CODEX. The pieces, in the order they act:

- **Who each ORDER is for is decided in ONE place, `order_customers.resolve_orders`** — per
  order, with its shop key, the sender's learned shop rules and the multi-shop flag, and
  **never** `… or email_matched` any more (that fallback is how one card swallowed the mail).
  The mail-level `email_matched` is reporting only. All four `ask_customer` call sites of the
  pipeline go through `order_customers.ask` (the date-conflict branch included — it used to
  hold every order on the mail-level card, so the date answer shipped all shops to it).
- **The shop key** (`store_key`): block header + the attachment the order was read from
  (`extract` records `sourceFile` — located deterministically from the item quotes in the
  `===== <file> =====` sections of `combined_text`, never asked of the model, so no
  `prompt_hash` change). Normalized: `dl_match.fold`, stopwords = order/document words, OUR
  OWN brand („OBJ Pekárová žena …" is an order TO us), week/day words, NOMINATIVE month names
  only (genitives are street names: „29. augusta"), legal forms, numbers. `key` drops words
  every order of the mail shares (keeps its own words when that would empty it); `tokens` keep
  everything; `header_key` = the header alone.
- **Rung order inside `resolve`**: (1) a learned shop rule (`store_rule`) — for ANY sender,
  above the family check; (2) family: street in the header (`_by_store`, #101); (3) family:
  shop-key 4-letter stems vs card name + CODEX trade name + city + street (`_by_store_name`,
  `store_name`) — EXACTLY ONE card hit, stems shared by every family card dropped; (4) family:
  `_by_delivery_address` — but NOT in a multi-shop mail (the mail-level text cannot say which
  shop it describes); (5) the old llm / exact_email rungs for a non-family sender.
- **The poistka** (`customer.collapsed_shops`): in a multi-shop mail (`store_key.
  is_multi_store`: the model reported a shop header block on SOME order — even an empty
  „Predajňa:" — AND the orders name ≥2 different shops, `StoreKey.identity` = the header
  words, or the FILE words under an empty header), a card ≥2 different shops landed on is a
  collapse: EVERY order on it that got there by a non-shop rule (`llm`, `exact_email`,
  `delivery_address`; `STORE_RULES` = store_rule/store_address/store_name never) becomes
  unresolved — also one that names no shop at all (review round 1: the empty-header PR file
  shipped to the one card) → held on the #431 placeholder + one question per shop. File
  names with NO header block anywhere never make shops (a one-shop customer sends one file
  per day).
- **Merging (#81.1) happens in two steps**: `pipeline._merge_by_day` keys on (date, order
  number, header, FILE) — two shops whose header blocks are both empty differ only by their
  file and must never become one ORDER file (review round 1); after resolution
  `order_customers.merge_by_card` merges the orders of one day that resolved to the SAME card
  (two files of one shop = one ORDER file). Unresolved orders are never merged.
- **Per-shop question** (`teach.ask_customer(key=)`): dedupe `item_key = <sender>|shop:<key>`,
  the shop in `wording` (the board card shows only `wording` + candidates — `reason` is not
  rendered), the key in `context` (`store`, `store_file`, `store_key`, `store_tokens`). Asked per
  shop whenever the mail is multi-shop, the sender owns ≥2 cards, or it already has shop
  rules (`scoped`); otherwise the #159 per-sender question, unchanged.
- **The answer teaches** through ONE helper, `order_customers.learn` — called by the board
  route (`_api_orders_answer_customer`), the „Nový zákazník" route and `teach.KINDS['customer']
  .apply` (the HTTP routes never went through `.apply`): a per-sender question teaches the
  sender address on the card (#159); a SHOP question teaches ONLY a `customer_store_rules` row
  (audited `create`, soft-delete, Kôš: in `audit._SOFT_DELETE_TABLES`, label „Predajňa
  zákazníka") — never the address (review round 1: the address on every answered shop's card
  made the sender a family whose mails without a shop clue were asked every time). It records
  `{email_added, store_rule}` (`teach.set_answer`); `teach.undo` → `order_customers.unlearn`
  forgets the address ONLY when that answer added it (a pre-#493 answer without the flag keeps
  the old forget) and soft-deletes the question's rules. A different card for the same
  (sender, key) replaces the old rule (audited delete + create). Rules match as `rule words ⊆
  order tokens` (a rule taught from a 5-file mail, {pr}, also hits a 1-file mail); the MOST
  SPECIFIC applicable rules decide (`store_key.match_rule`: a rule strictly contained in
  another applicable one yields — the answer for exactly „BISTRO Dolina" beats {dolina}, else
  a re-ask loops); disagreeing top rules = `conflict` → asked, also for a sender on ONE card
  (never a fall-through to `llm`/`exact_email`); a rule whose leftover key words name ANOTHER
  family card is not applied (asked) — `customer._by_shop_rule`.
- **`hold.retry_unknown_customer_questions`** (worker sweep, board save) settles a per-shop
  question only by a SHOP rung (`STORE_RULES`), never `exact_email` — the sender's one card is
  exactly the collapse; an auto answer records `email_added: false` (its undo used to strip the
  very address it matched on). It loads `customers_for_management` once per sweep (it layers
  ~1000 CODEX cards).

## #493 — the CODEX customer layer and the snapshot (read before touching `snapshot.py`)

- **Layers**: the frozen pre-CODEX BASE ← board overrides (`_merge_customers`) ← CODEX
  (`customer_layers.apply_codex`, pure): union of addresses, only BLANK name/city/street/zip/
  trade_name filled, missing EANs added as CODEX-only cards (identity `orig = (ean, codex
  street)`), a board-deleted EAN never re-added, `customer_overrides.removed_emails` never
  re-added on that card. Fail-open: always the last pushed copy.
- **Never layer on an already-layered snapshot**: `order_snapshots.customer_base_id` points at
  the pre-CODEX base; `rebuild_from_overrides`/`customers_for_management` read the base
  (`customer_base_id(conn, latest)`), so a card/address CODEX dropped leaves the list. The
  frozen order snapshot still contains the layer (runs stay reproducible). Content hash: the
  trade name joins the line only when set, so a list without CODEX hashes exactly as before
  (the e2e-orders corpus stays byte-identical — verified offline, 32/37 + the 5 #120 defects).
- **A hand-added override owns its EAN** (`_merge_customers`): every base row with that EAN is
  excluded, not only its current street. Before, a street edit of a board-created card left the
  copy baked in the cumulative base under the OLD street as a ghost duplicate card (checked on
  prod 2026-10-16: no base row shares a hand-added card's EAN except its own baked copies).
- **Never write CODEX-filled values back into an override**: `remember_/forget_customer_email`
  edit the PRE-CODEX row (`_own_row`; a CODEX-only card gets a minimal override: name + the
  board's own addresses, blank address fields), and the board save goes through
  `snapshot.unlayer_edit` (a field still equal to CODEX's keeps the stored value; CODEX
  addresses stay CODEX's; a CODEX address missing from the form = `removed_emails`). Writing
  the shown values would make the board own CODEX data (a later CODEX fix never arrives) and
  shift a hand-added card's identity.
- **Measured impact before the deploy** (read-only, prod list × DuckDB, 131 senders of 60
  days): only the incident sender became a family (1 → 4 cards); four senders went 0 → 1 card
  (now `exact_email`). The #435 NAME-STEM families did not change for any `llm` run of those
  60 days (CODEX adds only 9 EANs the list lacked; names are filled only when blank). Re-run
  both checks (scratch script: effective list + `raw.firma`, owners per sender and
  `_stem_family` of each `llm` pick, before/after) before changing the union semantics.

