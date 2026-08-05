# Autopilot log

Terse per-ticket record: issue #, commit SHAs, RED→GREEN test names, decisions, shared PR #.

## 2026-08-02 — #93 (PR #116)

- **#93** (AI orders: hold an order while its question is unanswered — but only until the
  delivery date): new `held_orders` table + `app/orders/hold.py`. A question still open
  for one of an order's lines (post `apply_siblings`/`merge_same_card` — a line that gets
  SIBLING-RESCUED no longer counts, review finding below) holds the WHOLE order instead of
  shipping the matched part now (which risked a second ORION document once the taught line
  arrived — the #81.1 defect). Two release paths, both through the existing `_ship_one`/
  `edi.claim_send` ledger: `hold.release_for_question` (fires after `teach.answer`, only
  once every question a held order waits on is answered, re-derives decisions against
  fresh memory with `match.decide_without_model(item_name, [], ...)` — empty catalog is
  deliberate, only catalog-free rungs can fire post-hold), and `hold.release_due` (the
  periodic deadline sweep, ships what matched using the ORIGINALLY stored decisions).
  `worker._claim` excludes any message with an open held order (never silently re-run
  through the LLM); the message stays `processed=false` until every held order for it
  resolves. Dashboard: `/api/orders/held` + a panel on `/otazky`.
- `pipeline` ⇄ `hold` circular import broken with a LAZY `from .pipeline import _ship_one`
  inside `hold._do_release` (never at `hold.py`'s own top) — `pipeline.py` imports `hold`
  at module top for `is_past_deadline`/`place`.
- Commits: `e7e1a1b` (version bump 0.9.16), `9e072f4` (feature + tests), `4f2fdf0` +
  uncommitted-at-write-time follow-up (review-finding fixes, see below) — feature work, no
  RED/GREEN split per `regression-test-first.md`.
- **Two review passes, both found real bugs before merge** (`/review` self-pass, then a
  dispatched deep-review subagent):
  1. CRITICAL: `/api/orders/question/<id>/answer` wrapped `teach.answer` AND
     `hold.release_for_question` — including the real ORION upload + the `edi_sent` ledger
     claim — in ONE shared non-autocommit transaction; a failure AFTER the upload rolled
     back the ledger claim too, even though the document had already shipped, letting a
     retry double-upload. Fixed: the release runs on its own autocommit connection.
  2. `_do_release` used to mark a held row `'released'` even when the ship itself errored
     (upload failed) — permanently losing a retryable order. Fixed: stays `'held'` on error.
  3. IMPORTANT: the hold decision was gated on the PRE-merge, per-item question list — a
     line rescued by `match.apply_siblings` (same wording resolved elsewhere in the SAME
     order) left an already-fully-resolved order held anyway on the now-moot question.
     Fixed: gated on `any(d.rule in ASK_THE_WAREHOUSE for d in decisions)` computed AFTER
     `merge_same_card(apply_siblings(...))`.
  4. IMPORTANT: `/api/orders/held` was missing from `SKLAD_PATHS` — the unauthenticated
     warehouse link (the actual audience for "your order is waiting on you") got a silent
     401 and the `/otazky` panel never rendered. Fixed: added to `SKLAD_PATHS` (order
     metadata only — customer name/EAN, delivery date, question ids — same shape as the
     already-sklad-visible questions/taught endpoints, no mail body/attachments/spend).
- Filed as follow-ups (pre-existing, out of scope for #93 itself): **#117** (`worker.py`
  never sets `message["today"]`, so `memory.resolve`'s `as_of` date-fence is a no-op in
  production), **#118** (a narrow race between two near-simultaneous answers to sibling
  questions of one held order can leave it stuck `held` — self-heals via the deadline
  sweep, no data loss, no duplicate upload).
- Tests: `tests/test_orders_hold.py` (new, 9 tests — the 4 behaviours the issue names
  explicitly, plus the sibling-rescue-must-not-hold regression, plus the ledger-not-just-
  status-flag proof), `test_orders_pipeline.py` (held vs immediate-ship at the deadline,
  multi-order held status aggregation, change-request-with-unmatched-item never holds),
  `test_orders_worker.py` (`_claim` excludes an open held message even past the stale
  window, released rows don't block reclaim, a run that holds isn't marked processed),
  `test_api.py` (the transaction-boundary regression, HTTP-level), `test_httpapi.py`
  (sklad role can reach `/api/orders/held`). Full suite: 458 tests green, `ruff check .`
  clean, coverage 91%+ (gate 85%).
- Shared PR: **#116**.

## 2026-08-02 — #102 (PR #115)

- **#102** (AI objednávky: neznámy výrobok — otázka aj do Odoo, odpoveď platí globálne):
  new `global_item_memory` table (dedicated table, not a sentinel EAN — argued on the issue);
  `match.decide_without_model` gains a `global_taught` rung, placed LAST among the no-model
  rungs (below `human_taught`/`catalog_name`/`alias_exact`/`history_sure` — customer's own
  signals always win); `teach.ask` gains `on_new` (fires only on a genuinely NEW question,
  wired to `report.build_question` + the existing Odoo `post`); `teach.answer` teaches BOTH
  per-customer AND global; `teach.undo` retracts the global row ONLY when it was created by
  the SAME question (`question_id`-scoped — a different, later question can never erase
  someone else's teaching).
- Commits: `e471fd2` (version bump 0.9.15), `72c86f4` (feature + tests, single commit —
  feature work, not a bug fix, so no RED/GREEN split per `regression-test-first.md`).
- Tests: `test_orders_memory.py` (remember_global/resolve_global/forget_global/seed_taught),
  `test_orders_match.py` (global_taught rung + full precedence matrix vs human/catalog/
  history), `test_orders_teach.py` (on_new callback incl. failure-swallowing, global write in
  answer, question_id-scoped undo, 2 full pipeline-level end-to-end tests), `test_orders_
  report.py` (build_question HTML + escaping), `test_eval.py` (new `--taught` corpus seed).
- Corpus (dev2, outside git): 2 new real cases (`syn102-twister-global-2026-08-02`,
  `syn102-twister-customer-override-2026-08-02`) added to the 30-case golden corpus via a
  small `--live` recording (~$0.30, 4 API calls) against real customers (LinHeart EAN
  2000000000736, PNO Brezno EAN 2000000000819) and a new `taught.json` seed file (mirrors
  `--history`) — the offline harness forces shadow mode, so ask/answer/undo side effects
  never fire during a replay; only the resulting match is observable. Full 32-case corpus
  passes `--require-all`; baseline updated. `ci.yml`'s `e2e-orders` job now also requires+
  passes `taught.json`.
- Shared PR: **#115** — merged `e4f105f`, main CI green (test+e2e-orders+build). Deployed
  v0.9.15 to the live add-on (`ha addons update e0ac7775_email_extractor`, after `ha store
  reload`). Post-deploy: dashboard DOM confirms `v0.9.15`; live functional check run
  in-container against the real Postgres (real `global_item_memory` table present, `teach.ask`
  → `on_new` → `report.build_question` wiring fires correctly, global resolves for a
  never-asked customer with rule `global_taught`, a customer's own mapping still wins with
  `human_taught`, `undo` removes the global row) — used a stubbed `post()` to avoid spamming
  the real Odoo "objednávky" channel with test noise (the real HTTP delivery path is the
  same `report.post_from_config` the pre-existing daily order report already uses in
  production, and is unit-tested against the real Odoo endpoint shape). Note: the live
  add-on currently runs with `ai_orders_engine=n8n` / `orders_shadow=true`, so this new code
  path does not fire on real production traffic yet — expected, pre-existing rollout state,
  unrelated to this ticket.

## 2026-08-01 — batch #36 + #41 + #87 (PR #107)

- **#36** (Static generator: add EAN for KOMFOS 'CHLIEB 500g PSEN.RAZNY'): n8n-only, no repo
  diff. Added `'CHLIEB 500g PSEN.RAZNY SLOVNORMAL': '8588001805623'` to `PRODUCT_EAN_BY_NAME`
  in the live `O8IYhUESjaWmPMTI` workflow's `generator` node. Verified via re-fetch +
  Node functional test. Closed directly (not via PR `Closes`).
- **#41** (invisible Unicode chars at ingest, AGEL "45​ks" incident class): two halves.
  (a) n8n-only: `extractor` node in `O8IYhUESjaWmPMTI` gets a `cleanInvisible()` pass over
  `inputText` before any parser runs (same codepoint set as Python's `ZERO_WIDTH`). Verified
  via re-fetch + Node functional test. (b) repo: `app/process.py::_combined_text` strips the
  same invisible-format codepoints. RED `e0ace09` (`test_combined_text_strips_zero_width_space`,
  `tests/test_process.py`) → GREEN `7dd817d`. Closed via PR #107 `Closes #41`.
- **#87** (eval_run --sample N + --live price estimate): `app/orders/eval_run.py` gets
  `select_sample()` (deterministic, one case per distinct manifest `type`, then fills by
  manifest order) and `_estimated_cost_usd()` (scaled from the measured $4.50/30-case
  baseline via `llm.PRICES`). Commits `47bb149` (feature), `2e9b6a1` + `fdbaab4` (deep-review
  fixes: unpriced-model must raise, tempfile write must be inside the cleanup try/finally).
  Tests in `tests/test_eval.py`. Closed via PR #107 `Closes #87`.
- Version bump `bb17403`: 0.9.12 → 0.9.13.
- Shared PR: **#107** — merged `4b2ba23`, main CI green, deployed + verified (dashboard shows
  `v0.9.13`, `/health` reports `{"ok":true,"version":"0.9.13"}`, 0 console errors).
- Filed **#108** (follow-up, out of scope): hardcoded `Authorization` headers on 3 HTTP nodes
  in the same n8n workflow, discovered incidentally while editing it for #36/#41.
- Decision: kept the invisible-char defense LOCAL in `app/process.py` (not imported from
  `app/orders/extract.py`) — core ingest must not depend on the `orders/` feature subpackage.

## #49 — Static auto orders: EAN via catalog_snapshot, not manual maps only (2026-08-01)

- `app/orders/static_ean.py` (new): 1:1 Python port of generator.js's `getProductEAN()`
  catalog-lookup half (exact name/alias match, else unambiguous order-independent
  core-token match with a weight guard — never guesses across gramáž/flavour). RED
  `6d5baac` (`tests/test_orders_static_ean.py`, module didn't exist) → GREEN `ce6d95b`.
  Independent code review (dispatched subagent) found 1x🟡 (rung-3 accepted-risk
  tradeoff wasn't pinned by a test) + 3x🔵 (coverage gaps, no rule-provenance return,
  pre-existing substring-map behavior) — fixed in `a72709a` (added the
  `test_KNOWN_TRADEOFF_...` pin + weight-variant pair mirroring the real catalog +
  reached 100% coverage on the module).
- n8n workflow `O8IYhUESjaWmPMTI` (node `generator`): added `Get Catalog Snapshot`
  Postgres node (parallel branch off `Get Static Orders`, same credential) +
  `getProductEAN()` catalog fallback. Published `1c192cf7…` then republished
  `ae74a5d8…` (fixed a one-character mojibake the MCP round-trip introduced in a dead
  error string — see the playbook note in `.claude/rules/orders-corpus.md`). Verified
  against the LIVE `catalog_snapshot` (127 rows) via direct psql + a `node -e` harness
  replaying the exact KARMEN P000534 incident and the real Lupačka 60g/75g
  weight-collision — both resolve correctly.
- Version bump `fda2ff1`: 0.9.13 → 0.9.14.
- Shared PR: **#111** — merged `e2820fe3`, main CI green (test+e2e-orders+build), HA
  add-on redeployed (`ha store reload` + `ha addons update` — new gotcha, add-on didn't
  see the update until the store cache was reloaded), dashboard shows `v0.9.14`, 0
  console errors, order worker started.
- Playbook-only follow-up PR **#112** (`b5f07da` → `6707976`, merged, no version bump —
  doc-only, outside `email-extractor/`): the two playbook findings above.

## 2026-08-02 — #50 + #108 (batch, one PR)

- #50: Odoo API key hardcoded in `Authorization` header on 3 HTTP nodes in n8n workflow
  `O8IYhUESjaWmPMTI` (`odoo send message1`, `Odoo Error Alert`, `Odoo Skip Alert`).
  Migrated all 3 to the existing "Odoo Bearer" credential (`N9WJBbMicZ3pwLp9`,
  `genericAuthType: httpHeaderAuth`), matching the pattern already live in `AI auto
  orders`' `Odoo Success`/`Odoo Needs Review`. Old key still lives in n8n version
  history — rotation in the Odoo UI + updating the "Odoo Bearer" credential value is a
  remaining USER-side step, cannot be done from this session.
- #108 (rescoped by an earlier autopilot run, duplicate credential part folded into
  #50): `Upload a file` (SSH node) was missing the explicit `operation: "upload"`
  discriminator — confirmed via a real past execution (720844) that the upload already
  worked; added the missing field with no behavior change. `OpenAI Chat Model` was
  missing explicit `responsesApiEnabled: true` (the type schema's own default) — made
  explicit per the project's standing choice (top OpenAI tier, Responses API on).
- All 5 node edits applied via `update_workflow` (14 ops, atomic) → `publish_workflow`;
  verified `versionId == activeVersionId`, literal secret gone from the active version,
  `validate_node_config` clean for all 5 nodes. Functional verification: same "Odoo
  Bearer" credential proven live-authenticating in the sibling `AI auto orders`
  workflow (execution 728698 — real Odoo message id returned, not a 401); this
  workflow's own executions were frozen at 2026-07-31T09:23 for the whole session
  (no new productions runs to observe directly).
- Playbook updated (`.claude/rules/orders-corpus.md`): credential-migration shape +
  the "validator warning ≠ broken behavior, check a real execution first" gotcha.
- Shared PR: **#114** — merged `58d2fb230b`, main CI green (test+e2e-orders+build). No
  add-on version bump (n8n-side + docs only, no app code changed).

## 2026-08-02 — #117 + #118 (follow-ups z #93, bundle, PR #119)

- #117: `worker._as_message()` nikdy nenastavil `"today"`, takže `memory.resolve(as_of=…)`
  dostával v produkcii prázdny reťazec a jeho dátumová poistka bola no-op — dodávka
  datovaná PO objednávke mohla ovplyvniť jej rozhodnutie. Wired
  `datetime.now(UTC).date().isoformat()` (worker.py:97) a `as_of` pretiahnuté cez
  `hold._redecide` / `_ship` / release cesty. Odpoveď skladu (`human` riadky) je z poistky
  vyňatá zámerne.
- #118: `hold.release_for_question` počítal zvyšné otvorené otázky bez zámku — dve súbežné
  odpovede na súrodenecké otázky jednej objednávky mohli obe vidieť `remaining=1` a ani
  jedna neuvoľnila. Fix: `SELECT … FROM held_orders WHERE id = %s FOR UPDATE` okolo celého
  check-then-ship-then-mark rozhodnutia (hold.py:278); ship krok zostáva na vlastnom
  autocommit spojení podľa opravy z #116.
- RED `a02799d` → GREEN `d8282b3`; offline korpus (30 prípadov, `--require-all`) bez zmeny
  oproti baseline (rovnakých 6 known-defect #83). Žiadny `--live` beh — čistá zmena kódu.
- PR **#119** — merge `10a6e66`, main CI zelené (test + e2e-orders + build), nasadené
  **v0.9.17**, overené čítaním DOM dashboardu a grep-om nasadeného kódu v kontejneri.

## 2026-08-02 — #83 (rekonštrukcia histórie zákazníckeho znenia, PR #121)

- Nový čistý modul `app/orders/reconstruct.py`: `extract_day_blocks()` rozdelí VLASTNÝ
  (necitovaný) text archívnej objednávky na bloky podľa dňa; `wordings_for_order()` vráti
  zákaznícke znenie položiek LEN keď sa ich počet PRESNE zhoduje s nezávisle overeným
  Odoo zoznamom kariet — inak `None` (žiadne hádanie). RED `ddd1859` → GREEN `f891500`,
  13 testov, syntetické fixtures, 100 % pokrytie modulu.
- Jednorazovým skriptom (mimo git, reálne dáta) doplnené do
  `~/eval-corpus/email-extractor/history.json`: 5 objednávok / 29 riadkov (CÉDER,
  04.07.–03.08.) dostalo skutočné zákaznícke znenie namiesto `name=card`. Ručne overené
  proti reálnym e-mailom. Zatvorilo to 1 z 6 `known_defect: "#83"` prípadov
  (`info-2026-07-24-a61839`) — 1 nové `--live` volanie (~0,15 USD, downstream match pre
  inú položku, ktorej kontext sa zmenil) zaznamenané do zdieľanej `llm-cache/`.
- Zvyšných 5 prípadov ostáva `known_defect`, prepojené na **#120** (dôkaz: žiadna
  Odoo-potvrdená dodávka pred dátumom e-mailu vôbec neexistuje — buď je to prvá
  objednávka zákazníka, alebo jediná staršia potvrdená dodávka je obeťou #80
  (n8n zahodilo takmer všetky riadky)). Vyrieši sa samo, keď pribudnú budúce objednávky
  (`order_items.name`+`card` sa zapisuje na každý beh už teraz).
- Offline korpus (32 prípadov, `--require-all`): 27/32 prešlo (predtým 26/32), RC=0.
- PR **#121** — merge `02a4188`, main CI zelené (test + e2e-orders + build), nasadené
  **v0.9.18** (`ha store reload` + `ha addons update`), overené čítaním DOM dashboardu
  (`v0.9.18`, `LIVE`, 0 console errorov) a logmi kontajnera
  (`orders.worker order worker started (engine=n8n shadow=True)`).

## 2026-08-02 — #51 (n8n edi_sent duplicitná ochrana, hotové) + #55 (Header Auth, čaká na 1 ručný krok)

- **#51**: Python engine mal ochranu proti duplicitnému uploadu do ORIONu (`edi_sent`,
  PR #74) už od 07-30, ale n8n vetva (`AI auto orders` = `wlORIhkVZISCdZNmBTM4Z`,
  `Static auto orders` = `O8IYhUESjaWmPMTI`) je stále tá, ktorá reálne uploaduje — Python
  beží iba v shadow móde. Pridaný rovnaký claim-before-upload vzor priamo v n8n: nový
  `Crypto` uzol hashuje `wincodexContent` s vybieleným dátumom vytvorenia dokladu (offset
  47:55, rovnaká logika ako `edi.content_hash()`), Postgres SELECT proti `edi_sent`
  (customer_ean+delivery_date), a podľa rozhodnutia z 2026-08-01: rovnaký obsah → tichý
  skip; iný obsah v ten istý deň → Odoo kanál 152, marker `❌Objednavka vyzaduje
  kontrolu` (kompatibilné s parserom denného sumáru); claim sa berie PRED uploadom
  (`ON CONFLICT DO NOTHING RETURNING id`) a uvoľňuje pri zlyhaní (`Upload a file` predtým
  nemal error-output vôbec). Overené: hash-blanking logika samostatne v Node.js (rovnaký
  order na 2 rôzne dni → identický hash), `edi_sent` unique constraint priamo na živej
  produkčnej DB (syntetické test dáta, upratané), `connections`/`parameters` byte-exact
  cez `get_workflow_details`. Jedna skutočná chyba nájdená a opravená pri review (uzol za
  HTTP request node čítal `$json`, ktorý HTTP uzol prepísal na odpoveď Odoo API — presne
  tá istá pasca, ktorej sa `Log Success Event` vyhýba explicitnou referenciou). Oba
  workflow publikované, #51 zatvorený.
- **#55**: Overené naživo — token extraktora je presne v 3 uzloch (`Fetch Attachment`
  v Dodacie Listy EDI, `Fetch Original eml` + `Fetch Invoice PDF` v Invoices Forward v2).
  n8n MCP nevie zakladať credentials; skúsil som cez Playwright prihlásiť sa do n8n UI,
  ale nemám prihlasovacie údaje (nie sú v pamäti, žiadna aktívna session). Položená
  otázka používateľovi (❓ ASKED, label `needs-answer` na #55) — buď založí Header Auth
  credential `email-extractor X-Token` sám, alebo pošle prihlasovacie údaje. Node
  rewiring (presné `id`/`url` všetkých 3 uzlov už zdokumentované v approach komentári) je
  pripravený, spustí sa hneď ako credential existuje.
- Bump **0.9.19** (n8n-only zmena pre #51 nemá kód v repe, ale tento log záznam áno →
  verzia sa bumpuje kvôli nemu).

## 2026-08-02 — #55 dokončené (Header Auth credential + rotácia tokenu)

- Prekážka z predchádzajúcich behov (n8n MCP nevie zakladať credentials, žiadny
  prihlasovací prístup do UI) vyriešená priamo cez n8n CLI v kontajneri
  (`docker exec app_6560bdea_hass-n8n n8n import:credentials`), bez UI a bez
  Public API kľúča. Zachytená a uprataná jedna pasca: CLI bez explicitného
  `N8N_USER_FOLDER=/data/n8n` omylom založí NOVÚ, prázdnu inštanciu v `/root/.n8n`
  s vlastným šifrovacím kľúčom — treba ho vždy nastaviť explicitne (reálny beh
  n8n má tento env len cez svoj supervisor wrapper, `docker exec` bez neho ho
  nevidí).
- Credential `email-extractor X-Token` (Header Auth, id `XgnAWAoB13f1D7kD`)
  založený vo vlastníckom projekte zhodnom s existujúcim "Email Extractor
  Postgres" credentialom (Marek Drlik, personal). Všetky 3 uzly (`Fetch
  Attachment` v Dodacie Listy EDI 1R4WcUFhpIPwEJX1, `Fetch Original eml` +
  `Fetch Invoice PDF` v Invoices Forward v2 du2O6YGmGyntXBbV) prepnuté na
  `authentication: genericCredentialType` + `genericAuthType: httpHeaderAuth` +
  tento credential, `?token=` odstránený z URL, oba workflow publikované.
- Overené naživo `curl`-om priamo proti nasadenému serveru (rovnaké cesty, aké
  volajú uzly): všetky 3 s hlavičkou `X-Token` → 200, bez tokenu → 403.
- Rotácia tokenu (starý `446cd89f...` → nový, len v pamäti/add-on options,
  NIKDY v gite): credential preimportovaný s novou hodnotou → add-on options
  aktualizované cez Supervisor API (`/addons/.../options`) → add-on reštartovaný
  → znova overené: nový token 200, starý token aj bez tokenu 403 na všetkých
  3 cestách. Poradie minimalizuje okno nesúladu (credential update tesne pred
  reštartom servera).
- Grep repa aj `git log --all -p`: starý token sa nikde v gite nikdy nenachádzal.
  Priamym grepom n8n SQLite DB (mimo repo) našli sa navyše 3 UŽ ARCHIVOVANÉ
  jednorazové debug workflow ("DL — Dump PDFs (TEMP)", "DL — Eval (SQL)",
  "TEMP — Sub1 regression test on DL 67832") so starým tokenom v URL — keďže
  token je už rotovaný (stará hodnota je mŕtva) a tieto workflow sú archivované
  (nespustiteľné, mimo produkcie), ponechané bez zásahu; zdokumentované na
  tickete ako informačné zistenie, nie ako riziko.
- Bump **0.9.21** — žiadna zmena Python kódu (`app/httpapi.py` už od #22
  prijíma token aj v hlavičke `X-Token`), celá oprava je na strane n8n +
  add-on options.

## 2026-08-02 — #120 zatvorené s dôkazom (žiadny kód) + #47 KOMFOS vision klasifikácia (n8n)

- **#120**: overené priamo proti živému dev2 korpusu (32 prípadov) — 5 zvyšných
  `known_defect: "#120"` prípadov (CÉDER `info@`, `dedlipova6`, `lucia-jelenikova`) potvrdené
  ako genuinely nerekonštruovateľné z archívu (`reconstruct.py`'s `wordings_for_order()`
  správne odmieta párovanie bez presnej zhody počtu položiek). Zatvorené komentárom s
  dôkazom + presnou podmienkou na znovuotvorenie (žiadna nová objednávka od týchto 3
  zákazníkov do ~3 mesiacov, alebo prípad ostane `known_defect` aj po novej objednávke).
  Žiadny kód, žiadna PR — čisté bookkeeping.
- **#47**: KOMFOS pobočka 67 posiela fotky (PNG) s prázdnym telom — extraktor ich správne
  označí `needs_vision`, ale workflow "Static auto orders" (`O8IYhUESjaWmPMTI`) mal len
  guard-throw na kanál 152 (ručné vybavenie). Korekčný komentár na tickete (14.7.) zmenil
  predpoklad: tieto fotky sú VRATKY, nie objednávky — riešenie musí najprv klasifikovať.
  Overené a zamietnuté: znovuzapnutie vypnutého `Call 'AI auto orders'` uzla (ignoruje
  odovzdanú položku, claimne nesúvisiaci riadok z DB — slepý koniec); plný dual-transcript
  vision vzor z dodacích listov (Sub1, `n:2` + aritmetická kontrola — zbytočná réžia pre
  nefinančnú kategorizáciu).
  Nová vetva vložená do existujúceho error-branchu workflowu (14 nových uzlov): Postgres
  SELECT `attachments.file_url` → fetch cez existujúce `/files/<mid>/<idx>` API
  (`X-Token` credential z #55) → jedno OpenAI vision volanie (`gpt-5.4`, `n:1`,
  `response_format: json_object`) klasifikuje `objednavka`/`vratka`/`iny_doklad` →
  `objednavka` sa vloží späť ako vstup PRE `extractor` (100% znovupoužitie existujúceho
  regex parsera → EDI reťazca) → `vratka`/`iny_doklad` → nová alertka na Odoo kanál **368**
  (reklamácie, vzor prevzatý zo sesterského workflowu "Reklamacie tovaru/ staznosti").
  Žiadna iná chyba (napr. `generator`'s "Chýba prevNumber!") nie je ovplyvnená — guard
  matchuje presne na frázu z guard-hlášky.
  **Nájdená a opravená chyba počas testovania** (dôkaz TDD-cyklu na n8n úrovni): n8n skracuje
  dlhú chybovú hlášku Code uzla na CHVOST pred jej doručením — pôvodná podmienka
  `.includes('Príloha je FOTKA')` (začiatok hlášky) nikdy nezapla. Opravené na frázu z
  konca hlášky (`pozri fotku a vybav ručne`), overené execution `732057` (FAIL, guard sa
  nespustil) → oprava → execution `732064`/`732065` (obe cesty PASS): objednávka-vetva
  reálne re-parsovala syntetický transkript cez `extractor` (partner=KOMFOS, 1 položka,
  dátumy OK); vratka-vetva korektne odpálila `Mark Vision Handled`+`Log Vision Event`+
  `Odoo Vratka Alert` na kanál 368. Byte-exact verifikácia po publish — žiadne
  MCP round-trip poškodenie diakritiky.
  Bump **0.9.22** (n8n-only zmena, žiadny Python kód — konvencia z #51/#55).
- Design/review komentáre: [design](https://github.com/zbynekdrlik/email-extractor/issues/47#issuecomment-5155534893),
  [review](https://github.com/zbynekdrlik/email-extractor/issues/47#issuecomment-5155593743).

## 2026-08-02 — #104 dokončené, etapa (c) prezývky (PR #130)

- Priama web kurácia znalosti "znenie → karta", bez čakania na otázku od pipeline. Nové
  funkcie `memory.py` (`add_customer_alias`/`add_global_alias`/`list_*`/`delete_*`) —
  per-zákazník ide do `item_memory(source='human')` (rovnaká cesta ako `teach.answer()`),
  globálne do `global_item_memory` (#102).
  RED `tests/test_orders_memory.py` (`test_a_direct_customer_alias_is_stored_and_resolves`
  a 10 ďalších) → GREEN `9efd880`.
- Migrácia `app/orders/alias_migration.py` (`python -m app.orders.alias_migration`) —
  jednorazový import stĺpca `doplnok` do `global_item_memory(taught_by='sheet-import')`,
  idempotentné, first-teach-wins nad ľudským priradením. Odklon od doslovného textu
  ticketu (ktorý spomínal `item_memory`) — `doplnok` je vlastnosť KARTY, nie zákazníka,
  zdôvodnené v design komentári na #104.
  RED `tests/test_orders_alias_migration.py` → GREEN `f7893ad`.
- Nová stránka `/znalosti` (globálne + vyhľadanie zákazníka) a `/znalosti/<ean>` (jeho
  priradenia), dostupná cez ten istý podpísaný `/sklad/<key>` odkaz ako `/otazky`;
  `SKLAD_PATHS` rozšírené o `SKLAD_ZNALOSTI_PAGE`/`SKLAD_ZNALOSTI_API` regexy. Odkaz z
  každej otvorenej otázky na `/otazky` priamo na predvyplnenú stránku.
  RED `tests/test_httpapi_znalosti.py` → GREEN `7372dfb`; E2E Playwright test cez reálny
  prehliadač (`test_e2e.py::test_the_warehouse_link_can_reach_the_knowledge_base_and_teach_a_wording`) — v jej vývoji odhalený a opravený flaky `wait_for_selector` (rovnaký text
  "Zatiaľ nič." v dvoch sekciách).
- Review: CLI migrácie zjednotená s `memory_import.py`'s `config.Config.load()` vzorom;
  `unicodedata` import presunutý na vrch súboru.
- Nasadené v0.9.24, overené naživo (Playwright na produkcii): pridanie/zmazanie
  priradenia pre reálneho zákazníka (Gazdovský trh, #101), migrácia naimportovala 18
  reálnych aliasov z hárku, 0 chýb v konzole.
- Nadväzujúce tickety podľa vlastného poradia používateľa: #127 (a: výrobky), #128 (b:
  odberatelia), #129 (vypnutie hárku).
- Design/review komentáre: [design](https://github.com/zbynekdrlik/email-extractor/issues/104#issuecomment-5155849171),
  [review](https://github.com/zbynekdrlik/email-extractor/issues/104#issuecomment-5155952598).

## 2026-08-02 — #68 dokončené, fáza 1 (Python port regexového parsovania, PR #134)

- Faithful 1:1 port `extractor` uzla (n8n workflow "Static auto orders",
  `O8IYhUESjaWmPMTI`) do `app/orders/static_parse.py` — partner detekcia, číslo
  objednávky, dátumy, lokalita, 3 rodiny parserov položiek (KARMEN "Vyšlá objednávka",
  KARMEN_CASH, LABAS). Rovnaký vzťah ako `static_ean.py` má ku `generator`'s
  `getProductEAN()` — CI-testovaný dôkaz správnosti, **NIE zapojený do živého workera**.
  RED `tests/test_orders_static_parse.py` (34 testov, syntetické fixtúry) → GREEN `804fa1e`.
- Kľúčová prekážka pri porte: JS `for`-cyklus s manuálnym `i++` vnútri tela (KARMEN_CASH,
  LABAS parsery) — Python `for i in range(...)` ignoruje zmenu `i` v tele, takže vyžadovalo
  preklad na `while i < len(lines):` s explicitným `i += 1`/`i += 2`, aby index presne
  sledoval JS sémantiku.
- **STEP 0 zistenie:** ticketov popis väzby "static workflow calls AI auto orders for extra
  content" je zastaraný — priame čítanie živého workflowu (`get_workflow_details`) ukázalo,
  že ten uzol (`Call 'AI auto orders'`) je vypnutý (`disabled: true`), mŕtvy koniec bez
  field-mappingu; skutočný "extra content" mechanizmus je samostatný `Basic LLM Chain`,
  ktorý AI auto orders vôbec nevolá. Re-pointing NEROBENÝ — nie je čo re-pointovať.
- Nasadené v0.9.25, overené naživo: modul úspešne beží v nasadenom kontajneri (import +
  volanie potvrdené cez SSH), 0 chýb v konzole, žiadna regresia na #104's `/znalosti`.
- Nadväzujúce tickety (fázovanie): #131 (EDI writer parita + reálny korpus), #132
  (shadow-mode worker, potrebuje #131), #133 (skutočný cutover, potrebuje #132 aj
  samotný AI-objednávkový cutover).
- Design/review komentáre: [design](https://github.com/zbynekdrlik/email-extractor/issues/68#issuecomment-5155851542),
  [review](https://github.com/zbynekdrlik/email-extractor/issues/68#issuecomment-5156057090).

## 2026-08-02 — #127+#128 dokončené (priama kurácia katalógu + odberateľov, PR #135)

- Nadväzujúce tickety na #104's rozhodnutie ("presun aj zvyšok"), etapy (a) výrobky a
  (b) odberatelia — bundle-nuté do jedného PR (rovnaká kódová oblasť, zdieľaná
  merge/verziovacia infraštruktúra).
- Nové tabuľky `catalog_overrides` (PK=gtin) a `customer_overrides` (surogát `id` +
  `orig_ean_edi`/`orig_street` identita pôvodného hárkového riadku) v `app/db.py`.
  Manuálne úpravy sa zlučujú s hárkovými riadkami PRI zamrazení nového snapshotu
  (`snapshot._apply_catalog_overrides`/`_apply_customer_overrides`,
  `rebuild_from_overrides()` pre okamžitý efekt bez čakania na hodinové obnovenie) —
  override vždy vyhráva, znovupoužíva CELÝ existujúci `order_snapshots`
  content-hash verziovací mechanizmus z #59.
  RED `9b9e3a9` → GREEN `decbae6`.
- Nové REST `/api/znalosti/products` (GET/POST/DELETE) a `/api/znalosti/clients`
  (GET/POST/DELETE), zaradené do `SKLAD_ZNALOSTI_API` — rovnaká bezpečnostná hranica
  ako existujúce alias endpoints. Nová JS UI sekcia na `/znalosti`.
  RED `e8ff068` → GREEN `ed12ee0`.
- Počas stavby nájdené a opravené DVE PREDCHÁDZAJÚCE chyby vlastnými RED→GREEN commitmi:
  `_content_hash` bol závislý od poradia riadkov (rozbíjal dedup medzi rôzne
  zoradenými, ale obsahovo identickými snapshotmi); `latest_snapshot_id` triedil podľa
  `id DESC` namiesto `checked_at DESC` (dedup-reuse staršieho id sa nehlásil ako
  aktuálny). RED `eda28b5` → GREEN `1d89946`.
- **Kritický nález z hĺbkového code review (subagent):** `retire_customer`'s vetva pre
  ešte-len-hárkového zákazníka vkladala prázdne `''` ako vlastnú aktuálnu identitu —
  `_merge_customers` ju vždy vylučuje z merge-u, takže PRVÉ takéto vyradenie by ticho
  vylúčilo KAŽDÉHO iného zákazníka s prázdnym EAN aj ulicou zároveň (obe polia sú
  legitímne voliteľné). Opravené na použitie pôvodnej identity namiesto placeholder-u.
  RED `636bdac` → GREEN `7153fd2`.
- **Gotcha (pre budúce podobné dedup-cez-content-hash zmeny):** override-mergujúca
  cesta (`rebuild_from_overrides`, číta cez SQL `ORDER BY gtin/id`) a hárková cesta
  (`import_snapshot`, číta v CSV poradí) musia hashovať OBSAHOVO rovnaký výsledok bez
  ohľadu na poradie riadkov — inak sa "revert na presne pôvodný stav" (napr. vyradenie
  karty pridanej omylom) nepozná ako identický a zbytočne vyrobí nový snapshot namiesto
  dedup-reuse starého.
- **⚠️ SÚBEŽNÝ FORK-DUPLICITA (dôležité pre budúce autopilot behy):** dispatched worker
  omylom spustil vlastný `subagent_type: "fork"` s cieľom len "počkať na review agenta" —
  fork zdedil CELÝ kontext vrátane rozpracovaného plánu, a namiesto pasívneho čakania
  sám vytvoril PR #135, zmergoval ho a spustil vlastné post-deploy overenie (rovnaký
  zdieľaný Playwright browser session ako hlavný worker, spôsobilo race na formulári).
  Fork nechal jeden neretirovaný testovací `customer_overrides` záznam (id=2,
  `9990000000042`) — nájdené a vyčistené priamym DB dotazom + API DELETE. Ponaučenie
  presne podľa `subagent-continuation.md`'s varovania: `fork` NIKDY nepoužívať len na
  "počkaj a nič nerob" — zdedený kontext ho zvádza dorobiť zvyšok plánu sám.
- Nasadené v0.9.26 (add-on `e0ac7775_email_extractor`), overené naživo: Playwright +
  priame API na produkcii (pridanie/úprava/vyradenie karty výrobku AJ odberateľa,
  overenie že zmena e-mailu skutočne zasahuje do `customer.resolve()`), 0 chýb
  v konzole. Testovacie záznamy po overení vyradené (retired=true).
- #129 (vypnutie čítania hárku) EXPLICITNE nezačaté v tomto behu — vlastný text ticketu
  vyžaduje #127/#128 overené v produkcii "aspoň niekoľko dní" pred jeho spustením;
  #129 dostal komentár s odôvodnením, ostáva otvorený.
- Design/review komentáre: [design #127](https://github.com/zbynekdrlik/email-extractor/issues/127#issuecomment-5156188060),
  [design #128](https://github.com/zbynekdrlik/email-extractor/issues/128#issuecomment-5156188983),
  [review #127](https://github.com/zbynekdrlik/email-extractor/issues/127#issuecomment-5156416200),
  [review #128](https://github.com/zbynekdrlik/email-extractor/issues/128#issuecomment-5156417972).

## #131 — Static orders (Python core): EDI writer parita + reálny korpus (2026-08-02)

- Zistenie: `app/orders/edi.py` je port INÉHO n8n uzla ("ASSEMBLE AND GENERATE EDI" zo
  "AI auto orders"), nie Static-orders' vlastného `generator` uzla
  (`O8IYhUESjaWmPMTI`). Riadok-po-riadku porovnanie odhalilo 6 skutočných rozdielov:
  dátum v HDR z `issueDate` nie `today`, orezanie čísla objednávky sprava (nie zľava),
  buyer name vs store name ako DVE odlišné polia, vlastný výpočet `storeEAN`
  (`PARTNER_CONFIG`/`LABAS_STORES`/`KARMEN_CASH_STORES`), diakritika sa MAŽE nie
  translitrujte, iná konvencia názvu súboru.
- Nový modul `app/orders/static_edi.py` (samostatný, nie vetva v `edi.py` — rozdiely sú
  príliš štrukturálne). RED `c5d26bc` → GREEN `d485130`.
- **Reálny korpus namiesto syntetického:** žiadne úložisko neuchováva skutočne nahraný
  EDI obsah pre statické objednávky (na rozdiel od AI objednávok, kde `edi_sent` má
  aspoň hash) a n8n execution history mala len 4 syntetické testovacie behy. Namiesto
  toho: 12 REÁLNYCH spracovaných e-mailov z Postgres (`messages`,
  `category='static_orders'`), pokrývajúcich všetkých 4 partnerov, spracovaných cez
  `static_parse`, overených proti reálnemu `email_events.detail` (12/12 sedí), a
  spustený SKUTOČNÝ `generator` JS zdroj pod node (nie reimplementácia) pre bajtovo
  presný "produkčný" výstup. Python `static_edi.py` sedí bajtovo 12/12. Korpus mimo
  gitu na dev2 (`~/eval-corpus/email-extractor/static-edi`), zapojený do existujúceho
  `e2e-orders` CI jobu (žiadny nový runner) cez `app/orders/static_edi_corpus_check.py`.
- **Hĺbkový code review (samostatný subagent) našiel 2 skutočné medzery v presnosti:**
  (1) `.replace('/', '_')` nahradza v JS len PRVÝ výskyt "/" v čísle objednávky, môj
  Python nahrádzal VŠETKY — opravené na `.replace("/", "_", 1)`, overené proti
  reálnemu JS; (2) chýbali 2 "hard-fail" guardy z n8n MAIN sekcie (chýbajúci
  `prevNumber`, objednávka bez jedinej rozpoznanej položky) — teraz `raise ValueError`
  presne ako produkčný uzol. Fix `84c6500`.
- Nasadené v0.9.28 (add-on `e0ac7775_email_extractor`). Overené naživo: DOM verzia
  "v0.9.28", 0 chýb v konzole, a `static_edi_corpus_check.py` spustený PRIAMO v
  nasadenom kontajneri proti reálnemu 12-prípadovému korpusu — 12/12 sedí.
- **Parity-only, žiadny cutover** — `static_edi.py` NIE JE importovaný nikde v živom
  pipeline (grep-overené v nasadenom kontajneri, 0 zásahov).
- Design/validácia/review komentáre: [validácia](https://github.com/zbynekdrlik/email-extractor/issues/131#issuecomment-5156638041),
  [design](https://github.com/zbynekdrlik/email-extractor/issues/131#issuecomment-5156636401),
  [review](https://github.com/zbynekdrlik/email-extractor/issues/131#issuecomment-5156816711).
  PR #136 (main..dev), merge `d43e200`.

## 2026-08-02 — #132 + #137 (PR #138)

- **#137** (n8n "Static auto orders" `O8IYhUESjaWmPMTI`: poistka proti prázdnemu claimu) —
  n8n-side only, no repo diff. Confirmed via `get_workflow_details` that `Get Static Orders`
  (atomic claim) fed `Normalize`/`Get Catalog Snapshot` directly, with `Mark OK`/`Mark
  Skipped`/`Mark Error` all using `queryReplacement: {{ $('Get Static Orders').first().json.id
  }}` — same undefined-id crash class as #34, the one that produced 39 Odoo spam messages on
  "AI auto orders" 2026-08-02. Added a "Claimed a row?" `n8n-nodes-base.filter` node, same
  shape `Invoices Forward v2`/`Dodacie Listy EDI` already use. Verified with `test_workflow`
  both directions (empty claim → nothing downstream runs; real claim → unchanged) before
  `publish_workflow`; confirmed `versionId == activeVersionId` after.
- **#132** (Static orders Python core: shadow-mode worker) — new `app/orders/static_worker.py`
  wiring `static_parse` (#68) + `static_edi` (#131) into the worker loop for
  `category='static_orders'`, SHADOW ONLY (own `_peek_for_shadow`, reuses
  `worker.resolve_engine`/`_start_run`/`_finish_run`). `static_orders_engine=python` logs and
  does nothing (#133 is the separate cutover ticket). `worker.run_forever` now also drives
  `static_worker.tick()` on the same connection/thread. 13 new tests
  (`tests/test_orders_static_worker.py`) — shadow claims nothing, engine resolver rejects
  unknown values, day bound honoured, shadow run recorded with `shadow=true`, every parse/
  build failure path (missing dates, empty order, photo-only, no EAN resolved) records
  `review` instead of crashing. Commit `6e1dd5f` (feature, no separate RED/GREEN split —
  greenfield feature, tests written alongside per `tdd-workflow.md`).
- Design/validation comments: [validated #132](https://github.com/zbynekdrlik/email-extractor/issues/132#issuecomment-5156978825),
  [design #132](https://github.com/zbynekdrlik/email-extractor/issues/132#issuecomment-5156997604),
  [validated #137](https://github.com/zbynekdrlik/email-extractor/issues/137#issuecomment-5156940218),
  [design #137](https://github.com/zbynekdrlik/email-extractor/issues/137#issuecomment-5156998427).
- Playbook: added the "empty atomic claim" n8n fix recipe (exact filter-node shape +
  `test_workflow` before/after verification) and a `design_gate.py` classifier gotcha
  (`_CAUSE_RE` needs the literal word "príčina"/"dôvod", not "koreň"/"zistenie") to
  `.claude/rules/orders-corpus.md`; filed `zbynekdrlik/airuleset#219` for the classifier gap.

## 2026-08-02 — #139 (PR #141)

- **Odoo notifications shortened to headline + nástenka link.** Root cause: `report.build()`
  (long per-order HTML) and `report.build_question()` (per-question) were posted once PER
  ORDER and once PER NEW QUESTION inside `pipeline._run`'s loop/`teach.ask(on_new=...)` — one
  real e-mail (msg 5564) with 5 delivery dates + 4 new questions produced 6 Odoo messages in
  3 seconds, read on the phone as "a lot of orders failed". Verified live in Odoo (channel
  152, `mail.message/search_read`) BEFORE the fix: exactly those 6 messages, timestamps
  09:08:33–09:08:36.
- New `report.build_summary(customer_name, orders, new_questions, unverified_count, link)` —
  ONE short message per processed e-mail: what arrived, outcome counts (ok/partial/held/
  review/error), and a link to the existing `/sklad/<key>` nástenka whenever anything is
  unresolved. Structurally cannot leak item names/traces/JSON/run ids (only aggregate counts
  in its signature). `pipeline._run` accumulates every order's outcome + every new question
  during the loop (`post_now=False` on `_ship_one`/`_finish`) and posts exactly once at the
  end; `hold.py`'s later, standalone single-order release events keep posting immediately
  (already "one message per thing"), just in the new short shape.
- New add-on option `dashboard_base_url` (NOT `public_base_url` — that one is the machine
  address n8n uses over docker, the exact 0.9.10 bug) + shared `app/linkutil.py`
  (`persistent_secret`/`sklad_key`/`sklad_url`) used by BOTH `httpapi.py` and
  `orders/report.py`, so the two `/sklad/<key>` derivations can never drift apart.
- Deep review (dispatched subagent, full diff `c077bd5..41cf134`) found 1 Critical
  regression before merge: `extract.py`'s `unverified` (AGEL-incident phantom-item
  safeguard) had NOT been carried into `build_summary`'s aggregate shape and had become
  fully invisible — fixed with a separate `unverified_count` param, summed ONCE at the
  e-mail level (never per-order — the list is shared unchanged across every order derived
  from one e-mail). Also fixed: a failed ORION upload leaked the raw Python exception repr
  into Odoo (now a short human sentence; full `repr(e)` preserved via a new
  `result["error_detail"]` field for the admin-facing event timeline only) and wired the
  previously-unused `missing_count` into the "partial" line.
- Commits: `c0c7a4c` (version bump 0.9.30), `496f045` [red], `41cf134` [green],
  `3a4e178` (review-findings tests) [red], `158aa77` (review-findings fix) [green].
- Deployed live: `ha addons update e0ac7775_email_extractor` → v0.9.30, then
  `dashboard_base_url=http://46.224.130.35:8099` set via the Supervisor options API
  (fetch full options → merge → POST — partial POST 400s) + `ha addons restart`. Verified:
  DOM shows `v0.9.30`, zero console errors, and the REAL `/sklad/<key>` link (computed
  server-side inside the container via `linkutil.sklad_url`) opens unauthenticated straight
  to `/otazky` showing the exact same 4 live questions (babovka/štrúdľa/vianočka kvásková/
  chlebík granč) that previously spammed 6 Odoo messages.
- Design/validation/review comments:
  [design](https://github.com/zbynekdrlik/email-extractor/issues/139#issuecomment-5158759052),
  [validated](https://github.com/zbynekdrlik/email-extractor/issues/139#issuecomment-5158760929),
  [design (review-fix)](https://github.com/zbynekdrlik/email-extractor/issues/139#issuecomment-5159021123),
  [review](https://github.com/zbynekdrlik/email-extractor/issues/139#issuecomment-5159081893).
- Playbook: this repo's `docs/autopilot-log.md` and `docs/superpowers/specs/` live at the
  GIT ROOT (`/home/newlevel/devel/n8n/email_extract/docs/`), one level ABOVE the add-on code
  — same root-vs-add-on-dir split `.claude/rules/orders-corpus.md` already documents for
  `pre-push-lint.sh`; don't go looking for `docs/` inside `email-extractor/`.

## #140 — AI objednávky: jediný kandidát v katalógu má prejsť aj bez gramáže (2026-08-02)

- Root cause: `unique_core_card()` (`app/orders/match.py`) required >= 2 core tokens on
  BOTH the ordered wording and the catalog card name before considering a match at all —
  meant to stop a generic single word ("rožok") auto-deciding among weight variants (Céder
  incident), but it also made a genuinely-single-word wording with a genuinely-unique
  single-word catalog card structurally unreachable, contrary to the explicit product
  decision ("iba jedna babovka → ber ju, aj bez gramáže").
- Fix: replaced both `len(...) < 2` floors with `not ...` (>= 1 token) — uniqueness now
  decided purely by `len(hits) == 1`. Céder-class safety unchanged: 2+ real candidates
  still return `None` (still asks). Commits: `5413bf7` (version bump 0.9.31),
  `efba71e` (RED: `test_a_single_word_order_passes_when_the_catalog_has_exactly_one_
  candidate`, fixture fixes for `test_orders_hold.py`/`test_orders_pipeline.py`/
  `test_orders_match.py`/`test_orders_static_ean.py`), `522be7b` (GREEN),
  `6f3a638` (review-fix: pinned the single-token-card superset-absorption tradeoff,
  mirroring `static_ean.py`'s own `KNOWN_TRADEOFF` acceptance).
- Verified on the LIVE 30-email corpus (dev2, `--require-all`): 0/32 cases changed
  outcome, and a rule-level diff of every corpus item (before/after) showed **0 items
  changed rule/review** — this fix does not resolve the #140 report's own 4 items
  (babovka/štrúdľa/vianočka kvásková/chlebík granč all have 2+ real live-catalog
  candidates, correctly still ask), only removes a provably-dead-weight guard for a
  future genuinely-single-candidate case.
- Deployed `0.9.31` via `ha addons update e0ac7775_email_extractor`. Verified:
  `/health` → `{"ok":true,"version":"0.9.31"}`, DOM `v0.9.31` on `/otazky`, and the exact
  4 items from the #140 incident (Výberofka Levoča, msg 5564) still correctly ask the
  warehouse (multiple real candidates each) — confirms no regression on the reported case.
- PR #143 (Closes #140), merge `4b05de1d`.
- Design/validation/review comments:
  [design](https://github.com/zbynekdrlik/email-extractor/issues/140#issuecomment-5159267028),
  [validated](https://github.com/zbynekdrlik/email-extractor/issues/140#issuecomment-5159268326),
  [review](https://github.com/zbynekdrlik/email-extractor/issues/140#issuecomment-5159419209),
  [design (review-fix)](https://github.com/zbynekdrlik/email-extractor/issues/140#issuecomment-5159469150),
  [review (final)](https://github.com/zbynekdrlik/email-extractor/issues/140#issuecomment-5159488393).
- Playbook: `hooks/block-commit-without-design.sh`'s design gate also latches onto a
  `#N` mentioned in a COMMIT MESSAGE's prose (e.g. "code review of PR #143 found...")
  — same gotcha `.claude/rules/orders-corpus.md` already documents for #139; avoid
  citing a PR number inside a commit message body that must pass this gate.

## #145 — Odoo 152: zmazať 6 starých správ z e-mailu Výberofka a poslať jednu novú v skrátenom tvare

- Data-fix ticket, no code change → no PR (per issue's own explicit instructions).
- Verified live: exactly 6 `mail.message` rows in ch.152 (id `20554622`–`20554627`) matched
  the e-mail (msg 5564, `order_runs.id=33`); backed up (issue comment
  https://github.com/zbynekdrlik/email-extractor/issues/145#issuecomment-5159613454),
  then `mail.message/unlink` on exactly those 6 ids.
- Posted 1 new message (id `21167967`) rendered directly via `report.build_summary()`
  from the already-stored `order_runs.result`/`held_orders`/`edi_sent` — no reprocess,
  no model call, no EDI. Verified `/sklad/<key>` nástenka still shows the same 4 open
  questions (id 8/9/10/12) and `held_orders` (id 2/3/4) unchanged.
- Docs-only commit `16f0947` (dev): captured the render-from-stored-data pattern in
  `.claude/rules/orders-corpus.md` for reuse `[no-design: docs-only playbook entry]`.
- Evidence/close comment:
  https://github.com/zbynekdrlik/email-extractor/issues/145#issuecomment-5159624370

## #147 — Nástenka: v ponuke kariet chýba práve navrhnutý kandidát

- Root cause: `pipeline._run` scores+truncates `item_cands` to top-6 BEFORE the model
  call (it's the model's INPUT), so the model's own answer can rank below the cutoff —
  the SYNONYMS rule in `match._score()` scored every "Zavin" card at 75 vs. "Jablková
  štrúdla"'s plain substring match at 65, silently dropping the exact card the engine
  proposed. Design comment (root cause + approach + rejected alt):
  https://github.com/zbynekdrlik/email-extractor/issues/147#issuecomment-5159660009
- RED: `tests/test_orders_pipeline.py::test_the_stored_question_always_offers_the_engines_own_candidate`
  (`5f482bd`) reproduces the live bug with a 9-card catalog (6 Zavin distractors + the
  real "Jablková/Maková štrúdla" cards).
- GREEN (`64d74d8`): `match.proposed_gtin()` + `match.candidates_for_question()` — the
  engine's own proposed candidate (from `decision.gtin` or the raw
  `decision.trace["llm"]["gtin"]` for a rejected `unmatched`) always heads the stored
  question's candidate list. Review finding fixed in the same PR (`3e2d414`):
  `proposed_gtin` crashed on an explicit `trace["llm"]=None` (present key, None value —
  `dict.get(key, default)` doesn't fall back on that).
- PR #148, merged `3ce0097`. Main CI green (test + e2e-orders + build), 30-email corpus
  `--require-all` green (only pre-existing `known_defect: "#120"` failures).
- Deployed v0.9.32. **Code fix alone only changes NEWLY asked questions** — the 4
  existing OPEN questions from `order_runs.id=33` still had their `candidates` stored
  under the old buggy logic. Narrow data-repair (same pattern as #145): direct
  `UPDATE order_questions SET candidates = ...` on ids 9/12/10 only (id 8 was already
  correct), no reprocess, no model call, `held_orders`/`edi_sent` untouched (verified:
  3 rows still `held`, `edi_sent` count still 2, all 4 questions still `status='open'`).
  Verified live via Playwright: all 4 questions now show their proposed candidate as
  the first button. Evidence:
  https://github.com/zbynekdrlik/email-extractor/issues/147#issuecomment-5159785104

## 2026-08-02 — #149 (PR #150)

- **#149** (Nástenka: pri otázke musí ísť vybrať ľubovoľný produkt z celého katalógu):
  `/otazky` (ASK_HTML) gets a per-question live-search box under the 6 quick candidates
  (#147 ordering untouched) — reuses the ALREADY sklad-allowed `/api/znalosti/catalog`
  search endpoint, no `SKLAD_PATHS`/`SKLAD_ZNALOSTI_API` allowlist change.
- RED: `tests/test_orders_teach.py::test_an_answer_outside_the_candidates_but_in_the_full_catalog_is_accepted`
  + `tests/test_e2e.py::test_the_warehouse_can_search_the_whole_catalog_when_no_candidate_fits`
  (`556640a`).
- GREEN (`6531901`): `teach.answer()` accepts a gtin either offered as a candidate OR
  present in the current catalog snapshot (`snapshot.catalog_gtin_set`, new — same raw
  source `/api/znalosti/catalog` already reads, so anything search offers is answerable).
  A search pick teaches/releases/records exactly like a candidate click (same
  `/api/orders/question/<id>/answer` endpoint).
- Deep review (`a75b2ee`): 0 Critical/Important, 4 Minor — fixed the stale-response race
  in the search box's `run()` (mirrors `load()`'s `render` guard) + documented that
  `catalog_gtin_set`/search read the RAW (non-override-merged) snapshot, same as
  `/api/znalosti/catalog` already did before this PR (pre-existing, not introduced here).
- PR #150, merged `ff95d42`. Main CI green (test + e2e-orders 30-email corpus
  `--require-all` + build). Deployed v0.9.33.
- Verified live via Playwright on the exact issue #149 case ("chlebík granč", question
  id 12, `Výberofka Levoča`): typed "chlieb" in its search box, got 24 real catalog
  matches with weight in the name (e.g. "Multicereálny kváskový chlieb 500g"); zero
  console errors. Left the question untouched — confirmed still `status='open'` in DB
  afterward (no answer/reprocess/model call triggered).

## #157 — alias 'names_customer' upgrade + merge_same_card silently mismatched CÉDER's 4-day order

- Bump: 0.9.35 -> 0.9.36 (`2dd97f8`).
- RED (`23e9f5e`): `tests/test_orders_match.py`, new `CEDER_CATALOG` fixture (cards
  192/253/239/284, the real names/aliases from the incident) — reproduces the exact
  production trace (`Chlieb olivovo paradajkový`/`multicereálny` wrongly settling as
  `alias_customer` on card 192; `merge_same_card` summing 3 different wordings into one
  line of qty 5).
- GREEN (`2d294eb`): two independent guards in `app/orders/match.py`, both word-level and
  deterministic (`_distinctive_words` — folded, weights stripped via existing `_norm`, a
  tiny "chlieb"-family stopword set excluded). (1) `_better_alias_candidate` gates rung 3
  (`alias_customer`) — if another catalog card's OWN name matches the wording better than
  the model's pick, the alias note does not confirm and the line falls through the rest
  of the ladder (typically lands `llm_borderline`, review=True — asks the warehouse).
  (2) `_wordings_differ` gates `merge_same_card` — a same-(gtin,unit) collision only sums
  when the two wordings share at least one distinctive word; otherwise the lines stay
  separate with their own quantities (bucket-per-wording-group rewrite).
- No prompt/model changes (out of scope by design — avoids invalidating the corpus
  llm-cache). Card 284's Vianočka case (the SAME mechanism firing CORRECTLY) and the
  "Chlieb pšenično ražný" -> 192 line the model got right are both pinned unchanged.
- PR #158, merged `88b4ba4`. Main CI green (test + e2e-orders 30-email corpus + build,
  unchanged — both guards are conservative enough to touch zero of the existing 30 cases).
  Deployed v0.9.36; `/health` confirms; `/otazky` DOM shows `v0.9.36`, 0 console errors,
  live real data (open questions + recently-taught list) rendering correctly.
- Manual ORION correction for the 4 already-imported CÉDER days stays the user's own job
  (documented on #157) — this PR is code/tests/deploy only, no reprocessing of msg 5596
  or any other already-uploaded order.

## #159 — unmatched customer was a silent dead end; change-of-order got the wrong board link

- Bump: 0.9.36 -> 0.9.37 (`af2a1a8`).
- RED (`48819ce`): new coverage across `customer.py`, `teach.py`, `hold.py`,
  `snapshot.py`, `pipeline.py`, `report.py`, `httpapi.py`, `test_e2e.py` — pins the
  target behaviour (customer-kind question+hold, ranked candidates, durable remember,
  "neviem" stays visible, `/sklad` link generalized, change-of-order gets its own
  wording+no link) all failing against unmodified code.
- GREEN (`95685a3`): `order_questions` gets `kind`/`context` columns (item default,
  customer new); `customer.candidates_for_question()` ranks by a street/city substring
  signal against the mail's OWN raw text — deliberately separate from `customer.
  candidates()` (feeds the model prompt / `prompt_hash`, untouched); `teach.
  ask_customer`/`answer_customer`, `hold.set_customer`/`release_unknown_customer`,
  `snapshot.remember_customer_email` (durable via the existing #128 override
  mechanism); `pipeline._run`'s `matched is None` branch holds + asks instead of
  reviewing; `report.build_summary`'s link now checks whether a run genuinely left
  something open, and a change-of-order gets its own "žiadosť o zmenu" wording +
  neither link (coordinator addendum, the 08-03 CÉDER incident).
- Deep adversarial review (independent subagent) on PR #161 found 2 real bugs before
  merge: (1) the deadline sweep could ship an order with a BLANK customer EAN if a
  customer-unknown hold reached its delivery deadline unanswered (`Matched` is always
  truthy even with `ean_edi=""`, so `_ship_one`'s `if not matched:` guard never caught
  it) — fixed with a shared `hold._release_as_review` helper + a guard in `_do_release`.
  (2) `teach.undo` on a customer question never reverted the remembered sender e-mail
  (`snapshot.remember_customer_email` lives entirely outside `teach.answer_customer`) —
  every future order from that address would keep silently mis-resolving to the wrong
  customer; fixed with a `kind=='customer'` branch + new `snapshot.forget_customer_
  email`. Also fixed a narrower dedupe-key collision (`ask_customer` reused a FUZZY
  product-wording normalizer that folded `.`/`-`/`_`/`@`; now the address itself,
  case/whitespace-normalized only). RED+GREEN in `fd7d297`/`dfef9da`.
- A third review finding — item-level ambiguity silently dropped when the customer was
  initially unknown (release re-decides against an empty catalog, no item question was
  ever raised in that path) — needs a genuine design fork (re-run full matching vs.
  raise a fresh item hold); filed as its own follow-up issue, out of scope for this PR.
  Does not affect the driving live incident (msg 5661: every item already resolved
  0.81-0.96, only the customer failed).
- PR #161, merged `0e7f341`. Main CI green (test + e2e-orders 30-email corpus
  `--require-all`, unchanged prompts/schema — confirmed zero cache misses + build).
  Deployed v0.9.37 via `ha addons update`; `/health` confirms; schema migration
  (`kind`/`context` columns) verified live via `\d order_questions`; `/` and `/otazky`
  DOM both show `v0.9.37`, 0 console errors, existing taught-list/undo functionality
  unaffected. No currently-open customer question existed live to exercise end-to-end
  (the driving message 5661 was independently resolved by a separate override addition
  before this PR merged) — verified via the full automated suite (unit through
  Playwright e2e) instead; never reprocessed message 5661 or any other message.

## 2026-08-03 — #162 (PR #165)

- **#162** (unmatched-customer hold release skipped item-level re-asking — an ambiguous
  item could silently ship partial): `hold._redecide` now defaults to the REAL current
  catalog snapshot (`hold._current_catalog`) instead of an always-empty one, still no
  model call. `hold._release_locked` checks every redecided decision's rule against
  `pipeline.ASK_THE_WAREHOUSE`: anything still ambiguous raises a fresh `teach.ask`
  question (new `hold._ask_still_ambiguous`, mirrors `pipeline._run`'s own per-item ask)
  and the row stays `held` a second time (`question_ids`/`decisions_json` updated) —
  never ships with the line silently dropped. A line `teach.ask` itself can't even raise
  a question for (empty-after-normalizing wording) is named in the Odoo notification
  instead of vanishing (`hold._post_still_held`).
- RED `b0d1e07`/`c0faadf`, GREEN `6e9178b`, extra coverage `82d4fcf`. Deep adversarial
  review (dispatched subagent, verified RED→GREEN empirically by reverting the fix) found
  0 🔴, 2 🟡 (missing multi-question test; stale `orders-corpus.md` playbook entry), 5 🔵
  (all addressed) — fixed in `a4705ce`: added the multi-fresh-question test, rewrote the
  stale playbook paragraph, shared a `_recalled_cache` between `_redecide`/
  `_ask_still_ambiguous`, documented the now-dead `_ship(redecide=True)` branch and the
  narrow self-healing conn/tx write-order window, corrected `report.build_summary`'s
  docstring for the one sanctioned item-wording exception.
- PR #165, merged `9f0bf0b`. Main CI green (test + e2e-orders 30-email corpus, unchanged
  prompts). Deployed v0.9.38 via `ha addons update`; `/health` confirms; `/` and `/otazky`
  DOM both show `v0.9.38`, 0 console errors; `/otazky` "Otázky skladu" tab loads cleanly.
  No live customer-unknown order with a genuinely ambiguous item existed to exercise
  end-to-end — verified via the full local test suite (20 tests in `test_orders_hold.py`,
  92.3% overall coverage) instead; never reprocessed any live message.

## 2026-08-03 — #163: reject a delivery date the model invents

- Root cause: `extract.verify()`/`extract.run()` citation-checked every ITEM against the
  source text but had no equivalent check for `deliveryDate` — the model could invent a
  future day (re-dating a stale month-old quoted order onto "next Saturday") with nothing
  in the mail to back it up, and `date_conflict()` then asserted it as a genuine second
  date. Live incident: msg id 5679, subject "RE: catering 25.7. SL", model claimed
  08.08.2026 — occurs nowhere in the text.
- Version bump `08fa700` (0.9.38 -> 0.9.39). RED `f366d30`
  (`test_a_stale_quoted_order_re_dated_by_the_model_is_not_shipped`), GREEN `edcba28`
  (`extract.date_grounded()` wired into `extract.run()`, filters `result["orders"]` before
  it reaches `pipeline.py`).
- e2e-orders corpus (30-case, offline) caught a real regression from the first version of
  the fix: `weekly_free_text`/`weekly_five_days` cases write a week as an explicit range
  ("od 06.07. - 11.07.") broken down by weekday name — individual days never repeat their
  digits, so the naive literal-citation check wrongly dropped 4 legitimate orders each.
  Fixed in `7e0f4d9` by deriving every day spanned by an explicit range (`_range_days`).
  Pre-verified against the LIVE corpus on dev2 (cached CI-runner checkout + scratch
  Postgres containers, see `.claude/rules/orders-corpus.md`) BEFORE re-pushing — saved a
  second CI round-trip.
- Deep code review (dispatched subagent) found 0 🔴, 1 🟡 (missing table-branch test), 2 🔵
  (Feb-29 placeholder-year edge case in `_range_days`; a dropped order was only visible in
  a log line) — all fixed in `c99bdab`.
- PR #166, merged `9f8ae43`. Main CI green (test + e2e-orders unchanged prompts + build).
  Deployed v0.9.39 via `ha addons update`; `/health` confirms `{"ok":true,"version":
  "0.9.39"}`; `/` and `/otazky` DOM both show `v0.9.39`. Functional verification: ran the
  DEPLOYED container's own code (`docker exec ... python3 -c ...`) against the exact
  msg-5679 shape — confirmed it drops the order and states the date could not be found,
  never asserting 08.08.2026 as fact. Never reprocessed message 5679 or any other live
  message.

## #164 — every board-resolvable dead end funnels through one place (2026-08-03)

- Root cause: `pipeline._run` had eight terminal branches, only two (item #88, customer
  #159) ever wrote a board question. Live proof cited in the ticket: message 5679's
  date-conflict branch discarded the already-resolved customer and returned "review" with
  zero rows in `order_questions`/`held_orders`.
- Design: `Reason` enum + `TECHNICAL_REASONS`, enforced as an invariant inside `_finish`
  (a "review"/"error" outcome must be technical OR carry an open question — a fallback
  generic `mail` question fires + logs CRITICAL otherwise). Kind-agnostic `teach.KINDS`
  register (item/customer/mail/date/line), each declaring `learns` + `deadline_shippable`
  (enforced non-empty at import). New `mail_rules` table (sender+subject pattern →
  ignore/manual, short-circuits BEFORE the LLM call, `not shadow`-gated). New
  `hold.set_delivery_date` + `release_due`'s `deadline_shippable` rule (a hold still
  waiting on a non-shippable question converts to review at the deadline instead of
  auto-shipping an unconfirmed date/customer/line).
- Commits: `c89edc1` (version bump) → `8820b12` [red: 3 named regression tests against the
  pre-implementation baseline, via `git stash` isolation] → `bcd1def` [green:
  implementation] → `aa5eaaa` (shadow-mode mail_rules fix, found in self-review) →
  `538fdb3` (worker._claim mail-kind exclusion test) → `3c6bcca` (item/customer registry
  coverage). RED tests: `test_a_hold_with_date_and_item_questions_ships_only_once_both_
  are_answered` + `test_release_due_never_ships_a_still_open_non_shippable_question`
  (`tests/test_orders_hold.py`), `test_the_full_exit_matrix_never_lets_a_resolvable_
  reason_go_silent` (`tests/test_orders_pipeline.py`),
  `test_legacy_rows_and_the_sklad_boundary_survive_the_new_kind_register`
  (`tests/test_api.py`) — all failed against pre-implementation code (`teach.ask_date`/
  `ask_mail` did not exist), all pass after.
- Self-review before push caught 3 real bugs: the mail_rules short-circuit would have
  corrupted shadow-mode's diff-vs-n8n comparison (fixed, `not shadow` gate);
  `worker._claim`'s SQL had no exclusion for an open `mail`-kind question (it has no
  `held_orders` row, unlike item/customer/date holds — added + tested); an existing test
  (`test_asking_with_no_sender_address_at_all_returns_none`) assumed the OLD
  `ask_customer` contract and needed rewriting for the new `cust:<message_id>` fallback
  key.
- PR #168, merged `e8c22c5`. Main CI green (test, e2e-orders — unchanged prompts/schemas,
  build). Local coverage 92.18% (CI gate 85%). Deployed v0.9.40 via `ha addons update`;
  `/health` confirms `{"ok":true,"version":"0.9.40"}`; DOM (`/`, `/otazky`) both show
  `v0.9.40`; migration confirmed live (`mail_rules` table + `order_questions.payload`/
  `answer` columns present in the production DB); `/otazky` renders cleanly, zero console
  errors.
- Playbook: new `.claude/rules/local-testing.md` (concurrent local `pytest` runs against
  the SAME dev1 test Postgres corrupt/hang each other — hit twice this session, cost real
  debugging time) + `.claude/rules/orders-corpus.md` gained 3 entries (the register's
  item/customer live-dispatch split, the shadow-gate reasoning for a pipeline-skipping
  short-circuit vs. a memory-read, the multi-kind-hold + `deadline_shippable` interaction).

## 2026-08-03 — #160: item-question shortlist padding

- Issue #160: "Kváskový slimák s pizzovou plnkou" (no catalog card) got a padded 6-card
  shortlist including an unrelated 700g bread loaf (score 15, one incidental shared word
  "kváskový") right next to the model's own honest low-confidence guess; the warehouse
  picked a wrong sweet card that wasn't even in the shortlist (found via full-catalog
  search). The wrong-shipment half was already resolved live via the warehouse's own
  `vrátiť`/undo (verified: exactly one `edi_sent` row, memory fully corrected) before this
  PR — this ticket's remaining scope was the CODE fix for the padding itself.
- Design comment posted before code: root cause (`ask_cands[:6]` in pipeline.py/hold.py
  had no relevance floor at all) + chosen approach (`match.plausible_candidates()`, a
  post-decision/post-LLM display filter using the score `candidates()` already computes,
  floor 50.0) + rejected alternative (extending `GENERIC_PRODUCT_WORDS` — rejected, feeds
  LIVE ladder-decision rungs, not just display).
- RED: `tests/test_orders_match.py::test_plausible_candidates_*` (668086f) — failed with
  `AttributeError: module 'app.orders.match' has no attribute 'plausible_candidates'`.
  GREEN: `fea43b8` adds `match.plausible_candidates()`, wires it into both call sites
  (pipeline.py's per-item ask, hold.py's `_ask_still_ambiguous` mirrored re-ask).
  Follow-up commits: `1529a2b` (docstring precision from self-review), `cefc6a9`
  (boundary test at score == floor, from the independent deep-review subagent's one Minor
  finding).
- No new question `kind` needed — every item question on the real `/sklad` warehouse page
  already, unconditionally, carries a full-catalog search box (#149) + a "databáza
  znalostí" add-a-new-card link; that is the sanctioned "nothing plausible" escape.
- Purely a post-LLM display filter: does not touch `match.candidates()` (the model's own
  prompt input), the prompt files, or ORDER_SCHEMA/PRODUCT_SCHEMA — `e2e-orders` (the
  frozen-LLM-cache gate) stayed green with no re-record needed, confirming hash-safety.
- PR #173, merged `63f81b5`. Main CI green (test, e2e-orders, build). Deployed v0.9.42 via
  `ha addons update`; `/health` confirms `{"ok":true,"version":"0.9.42"}`; dashboard AND
  `/sklad/<key>` DOM both show `v0.9.42`; `/sklad/<key>` renders live ("Nič nečaká."),
  "Naposledy naučené" list shows the real taught mapping (`Kváskový slimák s pizzovou
  plnkou → Toskánsky slimák 100g`) from the earlier live undo.
- Doc-only follow-up PR #174 (playbook notes), merged `d64dd51` — no version bump, no
  redeploy needed.
- Playbook: `.claude/rules/orders-corpus.md` gained an entry (an item question's escape
  hatch — search box + add-a-new-card link — already exists; a shortlist-quality problem
  is a `candidates()`-score-floor fix, not a new #164 kind) + `.claude/rules/
  local-testing.md` gained a note (captured pytest -q output can end at `[100%]` with no
  trailing summary line even on a green run — verify via exit code + absence of failure
  markers).

## #153 — edi_sent two-phase confirmation (orphaned claim silently skipped an order)

- Root cause: `edi.claim_send` inserted a CLAIM row before `upload()` and only released
  it (`release_send`) inside the `except` around `upload()` — a run that died OUTSIDE
  that branch (crash/kill/restart) left an orphan claim that the next attempt read as
  "already sent" and silently dropped the order (13 real orders lost, 2026-08-03).
- Fix: additive `edi_sent.uploaded_at` (self-healing `db.SCHEMA`/`init_schema`, no
  separate migration system). `claim_send` is now an atomic `INSERT ... ON CONFLICT ...
  DO UPDATE ... WHERE uploaded_at IS NULL AND sent_at < 10min ago RETURNING id` — a
  stale unconfirmed claim is reclaimed; a confirmed upload (any age) or a fresh claim
  (<10min, another worker may be mid-upload) still blocks a duplicate. New
  `edi.confirm_sent()` stamps it, called from `pipeline._ship_one` only after `upload()`
  genuinely succeeds, and retries with reconnect (review finding) since losing that one
  write is worse than the retry's latency. Pre-existing rows are backfilled as confirmed
  inside the same migration step that adds the column, serialized via
  `pg_advisory_xact_lock` against the project's OTHER `init_schema()` callers (one-off
  CLI tools) — a second review finding, since only the main app's single-process
  ordering was originally argued as safe.
- RED: `tests/test_orders_edi.py::test_an_orphaned_stale_claim_is_reclaimed_not_
  silently_skipped` + 4 more failed with `UndefinedColumn` on commit `76a6a22`; GREEN on
  `f045aaa`. Deep `requesting-code-review` pass found 2 🟡 + 2 🔵, all fixed on `8f4ed8b`.
- PR #176, merged `7fc344c`. Main CI green (test, e2e-orders, build). Deployed v0.9.43
  via `ha addons update`; `/health` confirms `{"ok":true,"version":"0.9.43"}`; live
  Postgres check post-deploy: `edi_sent` has the `uploaded_at` column, 48/48 existing
  rows backfilled as confirmed, 0 left unconfirmed. Dashboard DOM shows `v0.9.43`.
- Doc-only follow-up PR #177 (playbook notes), merged `a34ed0d` — no version bump, no
  redeploy needed (no application code changed).
- Playbook: `.claude/rules/orders-corpus.md` gained the two-phase-confirmation ledger
  pattern (atomic reclaim, confirm-with-retry, advisory-lock-guarded backfill migration,
  the DROP-COLUMN test technique) + `.claude/rules/deploy.md` gained the
  `sudo`-resets-env `PGPASSWORD` gotcha for post-deploy DB verification.

## #151 — EDI import confirmation must key on archCodex, never the Z- prefix

- Issue #151 (`Potvrdenie importu do ORIONu: nahratie súboru nie je dôkaz, že ho
  Communicator vzal`). Version bump `a2509ec` (0.9.43 → 0.9.44). Live read-only SFTP
  check against production ORION (before any code change) proved the ticket's own
  proposed signal (`Z-` filename prefix) is wrong: both files named in the ticket were
  already in `in/archCodex` (i.e. imported) with NO `Z-` prefix, hours after upload —
  posted as the "stále platný" validation comment + the design comment on #151.
- `app/orders/confirm.py` (new) — periodic sweep wired into `worker.run_forever` next
  to `hold.release_due`, gated on `ai_orders_engine=python`. Decision table: `archCodex`
  presence (with or without `Z-`) → imported, silent; `unconfirmed` → failed, alert
  immediately; still in `in/` under a configurable timeout (default 60 min) → silent;
  past it → timeout alert; gone from all three directories → `unknown`, alert
  immediately (never silent success). Every terminal state permanently drops the row
  out of the sweep — a file is alerted at most once, no separate dedup bookkeeping.
  `edi_sent` gained 3 additive columns (`import_status`/`import_confirmed_at`/
  `import_checked_at`) via the SAME advisory-lock DO-block pattern #153 used for
  `uploaded_at`, with pre-existing rows backfilled straight to `imported` (never
  retroactively swept — avoids an alert flood on deploy).
  `report.post_from_config` gained an optional `channel_id` override (new
  `delivery_notes_channel_id` option) so a `DESADV_*` alert can route to the
  delivery-notes channel instead of the orders one.
- RED: `tests/test_orders_confirm.py` failed on `ImportError` (module didn't exist) on
  commit `3154429`; GREEN on `5c4d709`. Self-review caught + fixed a real regression
  (`f72eb94`): splitting `put()`'s connect logic for `list_dirs()` moved
  `client.connect()` outside the try/finally that closes it — a socket leak on a failed
  connect. Deep `requesting-code-review` pass (general-purpose subagent) found 3 🟡 +
  4 🔵, all addressed on `1e61994`: an undelivered alert (transient Odoo failure, or
  Odoo simply unconfigured) used to mark the row terminal anyway, silently and
  permanently losing that one alert — now a row only becomes terminal once the alert
  genuinely delivers, and an undelivered one retries next sweep; a `listdir()` failure
  now backs off by the normal throttle interval instead of hammering ORION every ~15s
  worker tick; `tests/test_orders_upload.py` added (`upload.py` had zero coverage
  before this ticket) plus edge-case tests for a blank filename and the exact timeout
  boundary.
- PR #179, merged `23b48eb`. Main CI green (test, e2e-orders, build). Deployed v0.9.44
  via `ha addons update`; `/health` confirms `{"ok":true,"version":"0.9.44"}`; dashboard
  DOM shows `v0.9.44`. Live Postgres check: all 48 `edi_sent` rows (incl. both files
  named in the ticket, `ORDER_000846_20260805_110834114.txt` and
  `ORDER_000846_20260806_110835190.txt`) show `import_status='imported'` via the
  migration backfill — worker log confirms `order worker started (engine=python
  shadow=False static_shadow=False)` with no errors after the new code deployed.
- Playbook: `.claude/rules/n8n-workflow-edits.md` corrected — the `Z-` prefix is NOT the
  imported signal, `archCodex` presence is (with or without it); the old claim was
  actively misleading.

## #133 — static orders: real python engine + AI fallback + shadow diff vs n8n

- Issue #133 (`Static orders (Python core): skutočný cutover z n8n na Python`), scoped to
  the code side only per the user's 2026-08-05T16:01 comment — the engine flip and n8n
  workflow deactivation are a separate, later decision; the ticket stays open.
  `static_worker.tick`'s `engine=python` branch was a deliberate no-op (#132) — this wired
  it up for real: `_claim` (same protocol `worker._claim` uses), parse, resolve every
  item's EAN, build + upload through the SAME two-phase `edi_sent` ledger the AI engine
  AND n8n's own "Claim Send"/"Check Already Sent" nodes already share (verified live via
  the n8n MCP — see the `orders-corpus.md` playbook entry). No silent per-item skip
  (today's n8n behaviour is the defect this ticket removes): parsing failure, a photo
  order, a header defect, or ANY unresolved EAN routes the WHOLE message to the AI
  pipeline under the same claim. `run_shadow` now diffs the Python-built EDI against
  n8n's real `edi_sent` row by content hash and persists a verdict
  (`match`/`mismatch`/`would_fallback`/`empty_order`/`no_n8n_output`) in
  `order_runs.result`.
- Design comment posted before the first commit (`Príncina:`/`Zvolený prístup:`/
  `Zamietnutá alternatíva:`), per the design-gate discipline. Live-verified ticket
  validity posted first (current add-on options read via SSH, current code state
  confirmed).
- 24 new/changed tests (`test_orders_static_worker.py`, `test_orders_report.py`) — claim/
  release on every failure path (upload exception, a crash mid-fallback), the
  `held_orders`/`mail`-question re-claim guards, all four fallback triggers, the
  duplicate/empty-order distinct event stages, and all five shadow-diff verdicts. Full
  suite green locally (zero F/E/s/x) before every push.
- PR #181. Deep `requesting-code-review` pass (general-purpose subagent) found ONE
  **Critical**: `_fallback_to_ai` forwarded `cfg` unchanged into `pipeline.run()` —
  `cfg.orders_shadow` is the AI engine's OWN, unrelated, still-undecided shadow toggle;
  left `True` while `static_orders_engine=python` is live, EVERY static-order fallback
  would silently run in AI-shadow mode (no claim, no hold, no upload, no event) while
  `tick`'s own `has_open` check still marked the message processed — an order lost with
  zero trace. Fixed (`8ed1243`) with `dataclasses.replace(cfg, orders_shadow=False)`
  before calling the AI pipeline, pinned by a regression test that models the real
  pipeline's shadow-sensitivity. Also fixed 2 Minor (a stale config.py comment, missing
  double-failure-path test coverage). Merged `a358e12`, main CI green (test, e2e-orders,
  build).
- Deployed v0.9.45 via `ha addons update`; `/health` + dashboard DOM both confirm
  `v0.9.45`. `static_orders_shadow` flipped to `true` (via the Supervisor options API —
  full merged-options POST, per the existing gotcha in `orders-corpus.md`),
  `static_orders_engine` untouched (`n8n`). Worker restarted cleanly:
  `order worker started (engine=python shadow=False static_shadow=True)`. First 5 shadow
  runs after restart, read live from Postgres: **5/5 `match`** — the Python-built EDI is
  byte-identical to what n8n actually uploaded, on real production orders.
- Playbook: `.claude/rules/orders-corpus.md` gained the `edi_sent`-is-shared-with-n8n
  finding (exact hash algorithm + column shapes, live-verified via the n8n MCP) — the
  reusable fact that let both the ledger reuse and the shadow diff work without a new
  table.
- Note for whoever picks up the flip decision later: while this PR was in review, the
  user posted two more #133 comments expanding scope (a recalibrated "extra content" LLM
  branch, and a grouped Odoo digest for static uploads) — observed a separate,
  concurrent in-progress worker already handling that (its own design comment + new
  `static_extra.py`/`static_digest.py` files), left untouched.
