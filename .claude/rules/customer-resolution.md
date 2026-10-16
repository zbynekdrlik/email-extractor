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
- **NO automatic decision by a card's NAME (review round 7 — the ROZHODNUTÉ on #493).**
  Rounds 4-7 each found a new wrong card through the shop-name heuristic (a town every shop
  shares, ONE shared word, numbers / short codes a card cannot be compared on: „BISTRO 1" +
  „BISTRO 2" → both on the one BISTRO card, „Hlavná 2210" → the „Hlavná 1713" card, „BISTRO
  BB" → BISTRO of another town). A card bearing the shop's name now ONLY ranks first in the
  shop's question (`candidates_for_question`, +40 on a shared stem of `key`). A shop ships
  automatically only by (1) the warehouse's rule for EXACTLY that shop (`store_rule`) or (2)
  the street in its header (`_by_store`, #101, `store_address`). Every new shop is asked
  ONCE, then its rule decides — the incident's first mail after the deploy = 5 questions (one
  per shop), every later mail ships. Never re-add a name rung "with one more check": that is
  the treadmill the four rounds proved.
- **The shop key** (`store_key`): block header + the attachment the order was read from
  (`extract` records `sourceFile`, never asked of the model, so no `prompt_hash` change).
  Normalized: `dl_match.fold`, stopwords = order/document words, OUR OWN brand („OBJ Pekárová
  žena …" is an order TO us), week/day words, NOMINATIVE month names only (genitives are
  street names: „29. augusta"), legal forms. Numbers are dropped from a FILE name but KEPT in a
  header („Predajňa 1" / „Predajňa 2" are two shops) up to 5 digits — a house number, also a
  year-like one (round 6: dropping them made „Hlavná 1713" and „Hlavná 2210" ONE shop; a year
  re-asks once a year, the safe direction); `_HEADER_NOISE_RE` strips a date („16.10.", „16.
  10. 2026"), a date range WITH its dot („16.-20.10.", „16.—20.10." — „BISTRO 1 - 16.10."
  keeps its 1, round 7), a calendar week („42. týždeň", „43 KT", „KW 42") and an order number
  after „Obj." / 6+ digit numbers (round 5: „BISTRO 16.10." changed identity every mail).
  A header's shop CODE stays in the identity (`tokens(codes=True)`, only in `for_orders`):
  „T1" / „W2" (only „KW42" / „WK42" / „tyž42" are certainly weeks) and a lone capital letter
  („A Centrum" / „B Centrum", round 15 — both were {centrum}: one order, one answer for two
  shops). NEVER in the street comparison (round 16: „19/A" on a card then required its A, the
  „1" of „T1" became a house number — `_street_tokens` keeps the plain `tokens`).
  `tokens` = every word of header + file = the shop's IDENTITY (`text`): what a rule and the
  per-shop question dedupe are keyed on — never a mail-relative value (round 3). `key` =
  `tokens` minus the words every order of the mail shares AND the sender's company / name
  words — only the question ranking's view (round 4: a brand word must not rank the
  head-office card first).
- **Several shops next to a table** (`extract._several_tables`: two attachments with a
  recognized table, or the model naming two shops): read by the MODEL, never the table path
  (`parse_table` reads the first table only, straight through the next sheet, and keeps only
  the model's FIRST order — the second shop's order vanished or merged, rounds 9-11). One
  table of one shop keeps the table path (the corpus' Košík / price-list mails). Accepted
  residual (pre-existing on main): a table mail plus another shop's body order the model
  reports WITHOUT a store still loses that body order.
- **Which attachment** (`store_key.locate_file`): the ONE section the order's block HEADER
  phrase is in, else the section holding most of its items — an item counts by its QUOTE, and
  by its name only when the quote is NOWHERE — every text is checked against the mail BODY
  FIRST and one found there counts for no attachment, even when a form lists the same product
  (round 7: a reformatted quote matched no section; round 8: the incident's order forms share
  their product rows, a name is in every file and must never tie a located quote; rounds 9-10:
  a body order's quote / name in a form put it into that form's shop), compared word by word
  (`_words_text`: any separator equal, whole words — „chlieb 120" ≠ „chlieb 1200"); header and
  items naming two files, or a tie, decide nothing (round 5: the incident's files share most
  rows — by the header every Tržnica order carries ONE identity). An order of a mail WITH
  attachments whose file cannot be told is `fileUnknown` (`extract._located`) — never guessed.
  A file is compared by its words, or by its whole name when it has none („Objednávka.xlsx",
  round 7).
- **Multi-shop mail** (`store_key.is_multi_store`): ≥2 different shops AND a header block
  (`names_a_block`) on some order or the sender's learned shop rules. Shops = the distinct
  identities, plus orders naming no shop, one per distinct (block, file): under a block of
  their own (`names_a_block`: a generic shop word „Predajňa:", a real word, a lone CAPITAL
  letter „A", a code „T1", or a number no document word owns „č. 2"; never a noise-only
  header — a date incl. „04/08/2026", a certain week code „KW 42" / „WK42", an ambiguous code
  „T42" that EVERY order of the mail carries alike (its week, `_shared_codes`, round 15),
  „Objednávka č. 123", „Hárok 1", „v pondelok", „a.s.", a bare year — built from the same
  word lists `tokens` uses, rounds 8-15), from a found file whose name has no shop words (round 12: „OBJ Pekárová
  žena.xlsx" next to the Tržnica form; „Predajňa A" / „Predajňa B" counted once — both shops
  went to the sender's card), or `file_unknown` — the mail HAS attachments the order's could
  not be told from — each such order on its own (rounds 11-13: two blank „Predajňa:" forms
  counted once; a body mail with no attachment is never "untold", round 14). Never file names
  alone, never the sender's card count (rounds 3-4: a 2-card sender's two files of one day, a
  template file name, „Objednávka Prešov" + „… Prešov pečivo" are no shops — #81.1/#418
  exactly as on main). Measured (round 13, prod read-only): 435 of 443 order runs since
  2026-07-30 carry `store = ""` on every order — the header heuristics act on real multi-shop
  mails only. Accepted residual: a multi-shop customer whose files carry NO header block (or
  only a bare code every sheet shares, „T1", round 16) and who has no rules yet gets the
  pre-#493 behaviour — one merged order: a question for a family sender, a ship to the card
  of a sender whose address is on one card (as on main); the incident's mails carry headers,
  and its first answered mail teaches rules. Also accepted (asked, never a wrong card): the
  identity is case-sensitive for one-letter words („ŠKOLA A JEDÁLEŇ" vs „Škola a jedáleň")
  and a „T42 Centrum" header changes identity weekly — re-asked.
- **Rung order inside `resolve`**: (1) a learned shop rule (`store_rule`) — exactly its shop,
  for ANY sender; (2) family: street in the header (`_by_store`, #101) — NEVER for a sender
  with SHOP HISTORY (`order_customers.shop_history`): learned shop rules (round 26: a rule's
  card was another shop's street match) or a per-SHOP question (`|shop:` — never the per-order
  `|shop-mail:`, round 28) still OPEN on the board (round 27: the next mail shipped a shop by
  the street while its own question was open, then the answer named another card) — of ANY
  address of the sender's FAMILY or pointing at one of its cards (round 28: the family's
  second address skipped the history), or of a co-address on the sender's OWN card (round
  29, `own_cards` — addresses only, never that card's EAN: S2 on the one central card shipped
  S1's taught BISTRO to it, the incident misroute). Accepted: any rule naming a family card
  (also another chain's) makes the family a shop-history sender — even the #101 GT pair is
  then asked once per shop, then its rules ship; such a sender is a shop-history sender everywhere
  (`history` in `resolve_orders`, `collapsed_shops(shop_history=)`) and ships a shop only by
  ITS rule. In a multi-shop mail `strict`: the street's folded words INCLUDING the house number (digit
  and letter runs apart — „35BB", round 9; a house number WITH its one-letter suffix after a
  slash or glued is ONE unit, „19/A" = „19/a" = „19A" ≠ „19/B" ≠ „19", rounds 17-18: the 19/B
  shop matched the „19/A" card, then a bare „19" card; a letter written APART is never glued
  — „7 B. Bystrica" (with its dot) is house 7, round 19) must all be in the header (round 8:
  „BISTRO Hlavná 5" matched „Hlavná 12"; the #101 corpus case GT1 / GT2 still resolves) —
  `_by_street_strict` (rounds 24-25 — the CAUSE of rounds 17-23, each of which found a new
  way to tell two branches on ONE street apart by their numbers wrongly: „19/a" vs „19", „7 B.
  Bystrica", „19/A.", „GT1", a numberless card): a card BLOCKS when its street shares ANY word
  (numbers aside) with the header — so „ul. Hlavná 14" / „Nám. SNP 5" block „Hlavná 2" /
  „Námestie SNP 5" (round 25); the rung decides only when EXACTLY ONE card shares a word, its
  street (words + house numbers, a suffixed number one unit) is ONE RUN of the header, and the
  header's house numbers (minus the undecidable ones) are EXACTLY the card's — a shop number,
  a floor or a code written apart is no house number („Predajňa 2, Hlavná 14" ≠ „Hlavná 2",
  „BISTRO 1 Hlavná" ≠ „Hlavná 1", round 25). Also None: a numberless card for a header
  carrying any number; a header with an undecidable house number (`_undecidable_numbers`:
  one letter written apart without a dot „19 A", a slash letter with a dot „19/A.", a shop
  code's number „GT1") that ANY family card carries; a header or FILE naming the TOWN of
  another family card (`_names_another_town`: the town's main part „Košice" of „Košice -
  Sever", any of its words of 3+ letters, the initials „bb" of a two-word town; round 26;
  within the card's own town any word of another card's DISTRICT, „Košice - Juh", round 27,
  „Bratislava - II", round 28).
  The rung only PROPOSES a card — the whole-mail fit below decides whether it stands. Branches on one
  street are always asked ONCE, then the answer's rule decides. The #101 shape (two branches
  on two streets, GT1 / GT2) still ships. NEVER re-add a same-street number comparison „with
  one more check" — that is the treadmill rounds 17-23 proved. Accepted residuals: a shop on
  a street whose name EXTENDS a family card's street with the same house number („Nová Hlavná
  5" vs the „Hlavná 5" card) or in a town no family card has, when no other card shares a
  word, ships to that card ONLY when every other family shop is in the same mail on its own
  street (the whole-mail fit below) — text-wise it IS that card's address. Accepted over-asks (safe): a zip / floor / second number in the
  header, a street-type word („ulica", „námestie") shared by two cards, a Slovak one-letter
  word after a house number („Hlavná 5 v Žiline"). Any other mail
  exactly as #101; (3) family: `_by_delivery_address` —
  NOT in a multi-shop mail; (4) the old llm / exact_email rungs.
- **The poistka** (`customer.collapsed_shops`): a shop ships ONLY when a SHOP rung decided it
  (`STORE_RULES` = store_rule / store_address) — in a multi-shop mail for EVERY order (also a
  lone new shop on its own card, round 2, and an order naming no shop, round 1); for a sender
  with learned shop rules for every order that names a shop (round 3), whose file could not
  be told (`file_unknown`, round 6), that sits under a header block of its own (round 9: a
  lone „Predajňa:" form went to the address card), or — in a mail of several ATTACHMENTS
  (`extract` records the count, round 8) — any order (round 7: a „Objednávka.xlsx" order next
  to „… BISTRO.xlsx" is another of its shops). Converges: each shop asked once, then its rule.
  **Whole-mail fit** (`_street_unproven`, round 26 — the cause of rounds 17-26, each of
  which found one more way a single order's street pointed at a card that was not its
  shop): in a multi-shop mail the street decisions stand ONLY when every shop of the mail
  (each identity — also a shop named only by its FILE, round 27) is decided by the street, no two shops land on one card and
  every family card (`customer.family_of`) is taken — the #101 GT1 / GT2 shape. Otherwise
  every street decision of the mail is asked („A Hlavná" + „B Hlavná" on one card, one header
  in two files = two shops, the incident's 5 shops on 4 cards). Rejected: switching the street
  off in multi-shop mails (breaks #101, shipped on main and in the corpus).
  Accepted
  residuals (safe direction — asked, never a wrong card): a shop with NO identity at all (an
  empty header and a file name with no words) is asked per order every mail (no rule can be
  keyed on nothing); a date range without the dot after its first day („16-20.10.") leaves
  that day in the identity, so such a shop is re-asked each week; two blank „Predajňa:"
  blocks of a BODY-only mail on one day are one order (as on main — the mail carries no
  shop information at all).
- **Merging (#81.1), all inside `order_customers.resolve_orders`**: a multi-shop mail merges
  per (day, order number, shop identity) — two shops never merge, two files of one shop do,
  an order naming NO shop is never merged at all (round 5); a sender with learned shop rules
  merges per (day, order number, header, FILE) and never merges an untold-file order (round 7:
  merged, one answer shipped both files' goods to one card); any other mail merges exactly as
  before #493 (`merge_by_day`: day, order number, header — a merged order names its ONE file,
  else none and `fileUnknown`). After resolution `merge_by_card` merges orders of one day +
  header that resolved to the SAME card. Accepted residual: two differently-named files of ONE
  shop in a multi-shop mail that stay unresolved are asked twice and ship as two ORDER files.
- **Per-shop question** (`teach.ask_customer(key=)` → `order_customers.shop_question`):
  dedupe `item_key = <sender>|shop:<identity>`, the shop in `wording` (the board card shows
  only `wording` + candidates — `reason` is not rendered), the identity in `context`
  (`store`, `store_file`, `store_key`, `store_tokens`). Per shop in a multi-shop mail or for a
  sender with shop rules (`scoped`); a per-SENDER question asked there carries `no_address`
  and is asked for ONE order of ONE mail (`order_customers.unnamed_question`: key
  `<sender>|shop-mail:<message_id>:<order index>`, wording „<sender> · objednávka bez
  označenia predajne (10× rožok, …)") — never folded into the sender's plain question (round
  4), never shared by two orders / mails (round 5); its answer teaches nothing (a per-order
  decision), never puts the address on a shop's card, and the worker sweep never settles it
  (round 3).
- **Our own domain is never a customer** (`store_rules.is_own_address` — the EXACT domain or
  a subdomain, never a substring; `OUR_DOMAIN` moved there, `pipeline._sender_address` uses
  it; rounds 28-29): a staff forward of an unknown customer keeps the staff address as the
  sender — a shop rule keyed on it was shared by every customer that staff forwards
  („Predajňa 1" of one customer shipped another's order), and a plain answer put the staff
  address on the customer's card (every later forward → that card). Such an address owns no
  rule (`remember` → None, `for_sender` → []), no shop history, EVERY question of it is keyed
  per MAIL (`cust:<message_id>…`, `teach.ask_customer`), and no answer puts it on a card
  (`order_customers.learn`, the „Nový zákazník" route). Open (pre-existing, not #493): the
  #369 „nie je objednávka" mail rule is keyed on the staff `from_addr` + subject shape.
- **The answer teaches** through ONE helper, `order_customers.learn` — called by the board
  route (`_api_orders_answer_customer`), the „Nový zákazník" route and `teach.KINDS['customer']
  .apply` (the HTTP routes never went through `.apply`): a plain per-sender question teaches the
  sender address on the card (#159); a SHOP question teaches ONLY a `customer_store_rules` row
  (audited `create`, soft-delete, Kôš: in `audit._SOFT_DELETE_TABLES`, label „Predajňa
  zákazníka") — never the address, and „Nový zákazník" for a shop creates the card WITHOUT it
  (rounds 1-2: the address on every answered shop's card made the sender a family whose mails
  without a shop clue were asked every time). It records `{email_added, store_rule}`
  (`teach.set_answer`); `teach.undo` → `order_customers.unlearn` forgets the address ONLY when
  that answer added it (a pre-#493 answer without the flag keeps the old forget) and
  soft-deletes the question's rules. Answering retires EVERY other live rule of that (sender,
  shop) (a Kôš restore of a replaced rule must not leave a permanent conflict).
- **Rules match EXACTLY their shop** (`store_key.match_rule`): rule words == the order's whole
  identity (`tokens`) — never a subset (round 2) and never the mail-relative key (round 3).
  Disagreeing live rules of the same words = `conflict` → asked, also for a sender on ONE
  card. The price of exactness is a re-ask when a shop's header/file gains a word — never a
  wrong card.
- **`hold.retry_unknown_customer_questions`** (worker sweep, board save) settles a per-shop
  question only by a learned rule (`store_rule`) — never the street (round 26: re-resolved
  ALONE, the street shipped two shops the whole-mail fit had refused; a card that gains the
  shop's street in CODEX no longer auto-answers, the warehouse does once) and never
  `exact_email` — the sender's one card is exactly the collapse; an auto answer records `email_added: false` (its undo used to strip the
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

