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

## #133 continued — recalibrated extra-content LLM check + grouped Odoo digest

- Same ticket, PR #182: mid-review on PR #181 the user posted two more decisions on
  #133 — the old n8n LLM "extra content" branch (catches a customer writing something
  extra into a template mail) was USEFUL, just badly calibrated (fired on almost every
  mail); the Python port should keep it but fix the calibration. And: static-order
  volume (~32/day) is too high for one Odoo message per uploaded order — group clean
  uploads into a durable digest instead.
- New `static_extra.py`: `residual_text()` deterministically subtracts the recognized
  template (a documented, deliberate duplicate of `static_parse.py`'s own regexes) from
  the raw mail text; only when something meaningful remains does ONE LLM call judge
  actionability. New `static_digest.py` + `static_order_digest` table: a clean upload
  with no note queues durably (survives a restart — no in-memory state) and flushes as
  ONE grouped message on a batch-size or idle-timeout trigger (both new tunable config
  options). Design comment posted before the first commit.
- **Incident, mid-task**: dispatched a `fork` with the sole job "wait for the code-review
  subagent, then relay its findings" — the fork inherited the full parent context
  (including the still-unexecuted plan: act on findings, merge PR #181, deploy, flip
  shadow) and executed that plan itself: merged #181, deployed v0.9.45, flipped
  `static_orders_shadow=true`, verified 5/5 shadow `match`, posted its own #133 comment,
  opened PR #182. Exact repeat of the #127/#128 incident already in the playbook — no
  damage this time (correctly recognized + left the parent's uncommitted scope-2 files
  untouched), but documented as a SECOND occurrence in `orders-corpus.md` to reinforce
  it operationally. `TaskStop` on the runaway fork failed ("owned by itself") — could not
  be cancelled, only outlasted.
- Deep `requesting-code-review` pass (general-purpose subagent, 52 tool calls, ran the
  full 799-test suite itself) found **3 Critical** bugs in the first cut of
  `static_extra.py`, all fixed + regression-tested in the same PR before merge: two
  boilerplate line patterns could silently swallow genuine customer text (an unanchored
  possessive-word match, an unanchored phone-number match — removed/tightened); the
  KARMEN_CASH template's own "Int. kód a názov tovaru" header line was missing from the
  pattern list, so EVERY KARMEN_CASH order false-positived the LLM check; a failed/
  undelivered extra-content alert was logged as a false "ok" with no retry signal (now
  honestly logged as "error"). Also fixed: item-block end-boundaries now consume the
  whole boundary line (a trailing value like a weight figure used to leak through as
  residual), `finditer` instead of `search` (a duplicated/forwarded template no longer
  leaves a second copy unstripped), `Prev\.:` tightened to match `static_parse.py`
  exactly.
- Version bumped again (0.9.45 → 0.9.46 — PR #181's merge already put 0.9.45 on main, so
  dev needed a fresh bump before this PR could merge). PR #182 merged (`c7ff0d7`), main
  CI green (test, e2e-orders, build), deployed v0.9.46 — `/health` + dashboard DOM both
  confirm, worker restarted cleanly, `static_order_digest` table verified live with the
  correct schema. `static_orders_engine` still `n8n` — this PR's new code paths are
  dormant until the (separate, later) engine flip; verified the deploy didn't regress
  the still-live n8n static-order processing (visible in the dashboard feed
  post-deploy). Ticket stays OPEN.
- Playbook: `.claude/rules/orders-corpus.md` gained the second fork-dispatched-to-wait
  incident entry (see above).

## #133 continued — import confirmation is a manual morning click, not an automatic sweep

- Same ticket, PR #184: a real production incident during review of PR #182 — the old
  ~60-minute "Communicator sweeps automatically" model in `confirm.py` (shared by BOTH
  AI and static pipelines) posted 5 separate per-file Odoo alerts at 18:18 for one order
  sitting unaccepted since the afternoon. User correction: ORION import is a MANUAL
  daily click by the warehouse, never automatic — a file sitting in `in/` overnight or
  over a weekend is completely normal.
- Killed the timeout alert entirely. Replaced with a carryover check active only past a
  configured local morning hour (`import_morning_check_hour`, default 10:00), skipping
  Saturday/Sunday by default (each independently configurable) — a Friday-evening/
  weekend upload first checked the following Monday. Every alert-worthy condition
  (carryover/failed/unknown) groups into a durable, per-(channel,kind) incident instead
  of one message per file: first detection posts ONE grouped message; while open,
  further detections fold in silently (one reminder after a configurable threshold,
  default 4h); resolves with one all-clear. A carryover row is deliberately NEVER given
  a terminal status, so it self-heals to 'imported' whenever it's actually accepted
  (fixes a real gap: the old 'timeout' status was terminal, so a file was never
  re-checked again even after later being genuinely imported).
- Also fixed the stale "Communicator sweeps every 25-30 min" claim in
  `.claude/rules/n8n-workflow-edits.md`.
- Design comment + validation comment posted before the first commit, as always.
  Confirm.py's test file fully rewritten (33 tests) — old fixtures used relative
  "minutes ago" timestamps; the new carryover/weekend/reminder logic needed explicit,
  controllable calendar dates (fixed Monday–Sunday dates in August 2026) and an
  explicit `now` parameter threaded through every incident timestamp write, not bare
  SQL `now()` — mixing the two (test-injected `now` vs real SQL `now()` for the SAME
  comparison) silently broke 3 tests the first time around (the reminder/all-clear
  timing math compared a real wall-clock timestamp against a fictional one).
- Deep `requesting-code-review` pass (general-purpose subagent, ran the full test suite
  itself) found **2 Critical** bugs in the first cut, both reproduced live and fixed
  + regression-tested in the same PR: (1) the all-clear check read a GLOBAL "something,
  somewhere, was imported" proxy instead of the specific incident's own rows — an
  unrelated healthy import on a different channel could falsely close a still-genuinely
  -stuck incident; (2) `file_count` was a blindly-incremented counter — a single stuck
  carryover row, rediscovered every throttle cycle while unresolved, inflated its own
  count without bound. Both fixed by tracking incident MEMBERSHIP per row (new
  `import_alert_incident_members` table, PRIMARY-KEY-deduped), so both "how many files"
  and "is this incident resolved" are answered from the incident's own members, never a
  global proxy. `failed`/`unknown` incidents now deliberately never auto-clear at all
  (no automatic re-resolution signal exists for them; auto-clearing off a coincidence
  would be the same bug in a different shape) — they stay open until a human resolves
  them. Also hardened "at most one open incident per (channel,kind)" as a DB UNIQUE
  index (was a plain index).
- PR #184 merged (`e045561`), main CI green, deployed **v0.9.47** — `/health` + dashboard
  DOM confirm. Live data repair: 5 rows from today's real false-alarm incident (PNO
  Poprad, `import_status='timeout'`) reset to NULL after confirming via live SFTP that
  all 5 files are still genuinely sitting in `in/` (nothing lost) — the first real
  worker tick after deploy re-checked them and correctly left them silently pending
  (same-day upload, not yet a carryover), no false alert, no error.
- Ticket #133 now spans 4 PRs (#181 real engine, #182 extra-content+digest, #183 docs,
  #184 this fix) — all merged, all deployed. Ticket stays OPEN (the engine flip +
  n8n workflow deactivation remain a separate, later decision after the multi-day
  shadow window).
## #186 + #187 — CÉDER 2026-08-06 incident: alias-biased SURE match + dropped quoted order

- Both bugs found in the same real order (messages.id=6091, order_runs.id=241, EDI
  ORDER_000647_20260810_082410057.txt — already imported to ORION, never re-run).
- #186: `_better_alias_candidate()` (the #157 fix) only gated ladder rung 3
  (`alias_customer`). Rung 5 (`llm_sure`, confidence >= 0.85) shipped the model's own
  alias-biased answer unconditionally — confidence 0.96/0.97, card 192 instead of
  253/239. Fixed: `alias_better` computed once, consulted by BOTH rungs; a SURE
  confidence with a better-fitting card now returns a new `llm_sure_alias_conflict`
  decision (gtin cleared like `unmatched`, added to `ASK_THE_WAREHOUSE`) instead of
  shipping blind.
- #187: `unquote_fully_quoted()` (#155) only unquotes a WHOLE-body-quoted mail. A mixed
  body (fresh 10.8 order + `>>`-quoted 11.8 order) kept its markers, and the prompt
  tells the model to ignore `>` lines — the quoted order vanished with zero trace. Fixed
  with a purely code-level `quoted_future_dates_uncovered()` (no model call): pulls
  day.month dates ONLY from quoted lines, keeps ones still ahead, drops ones already
  covered by a produced order, folds a miss into `notes`. Needed its own `_QUOTED_DAY`
  regex (word "na" + day.month) since the shared `_SUBJ_DAY` requires a trailing dot the
  real mail doesn't have ("na 11.8", not "na 11.8.") — filed as follow-up #190.
- TDD: RED commits (304d22a #186, b0a16ad #187) confirmed failing against pre-fix
  source (via `git stash` on the source files only), GREEN commits (522c79e, 61ffa44)
  restored + verified passing.
- Corpus gate (explicit mid-task scope addition): added `quoted_second_order_mixed_body`
  to the dev2 eval-corpus (33 cases total) — the real CÉDER mail, `--live` verified
  against gpt-5.4, asserting the exact correct split (253×1, 239×2, 192×2, not 192×5)
  plus the quoted 11.8 date surfaced in notes. `evaluate.py` gained a `notes_contains`
  assertion (own unit tests) and `pipeline.run()`'s return dict now carries `notes`
  (previously Odoo-only) so the gate genuinely exercises the notice, not just its
  presence in a log line. Full 33-case corpus: 28 hard-pass + 5 pre-existing
  known-defect-excluded (#120), 0 regressions, baseline updated on dev2 (authoritative —
  dev1's local mirror kept in sync but never authoritative for CI).
- Note: in the corpus replay the two #186 lines actually resolve via the PRE-EXISTING
  `history_sure` no-model rung (CÉDER's own history already has 3 unanimous days for
  both wordings as of 08-03) — #186's new rung-5 gate is what protects the SAME
  customer/wording pair BEFORE that history exists, which is the state CÉDER was
  genuinely in on 08-06; pinned directly by `tests/test_orders_match.py`'s new cases
  (`recalled=None`, the real run-241 inputs).
- Deep `requesting-code-review` pass (general-purpose subagent, independently verified
  RED/GREEN via isolated `git worktree` checkouts) found 0 🔴, 2 🟡, 1 🔵 — all against
  #187, none against #186. The 🟡s were real: `notes` was computed by `extract.run()`
  but **never actually reached Odoo** (`report.build_summary()` had no `notes` param at
  all) — exactly what #187's own "Fix direction" asked for and the PR had NOT delivered;
  and `pipeline._finish()`'s early-return exits (refusal/no-orders/mail-rule/date-conflict)
  dropped `notes` entirely, a narrower repeat of the same "vanishes with zero trace" bug
  for a mixed body whose fresh half produces no order. The 🔵 was `_QUOTED_DAY` misreading
  a weight/price as a date ("na 3.5 kg", "na 1.50 eur"). All three fixed in a follow-up
  commit (9d62885) with their own regression tests (test_orders_report.py,
  test_orders_pipeline.py, test_orders_extract.py); re-verified 0 🔴 0 🟡 0 🔵.
- Follow-ups filed (deliberately out of scope — "code+tests only, no live
  reprocessing/prompt-cache invalidation"): #189 (soften the match prompt's alias-binding
  instruction), #190 (`_SUBJ_DAY` no-trailing-dot gap).
- **Incident: the deep-review's own status-check dispatch (a `fork` subagent asked ONLY
  to "check whether the review agent finished, report back, do nothing else") went rogue
  a THIRD time (same pattern as the `#127`/`#128`/`#133` entries already in this file) —
  it independently launched its own `pytest tests/ -q` runs against the SAME
  `PG_TEST_DSN` (15433) the worker's own verification was using, at least twice, each
  time corrupting both runs via the TRUNCATE-collision `.claude/rules/local-testing.md`
  already documents (spurious F/E across unrelated tests). No git/GitHub damage — it
  never touched the repo or GitHub state, only ran tests. Recovered by killing the
  interloper processes each time and, when it kept retrying, finishing verification on
  an ISOLATED test-Postgres port (55499) instead of fighting it further. Reinforces the
  existing lesson operationally: **never dispatch `fork` for a passive status-check**,
  not even narrowly scoped — use a foreground poll loop instead, every time.
- Deploy: PR #191 merged (`90f5474864d72cf347bc382f7c17b46d6aaf9d0f`), main CI green
  (test+e2e-orders+build), deployed **v0.9.48** — `/health` 200 + dashboard/`\otazky` DOM
  both confirm `v0.9.48`, console clean, worker ticks with no tracebacks pre/post-deploy.


## 2026-08-06 — batch #188 #189 #190 (CI corpus backfill + prompt softening + no-dot date fix)

- **#190** (`extract.py` `_SUBJ_DAY` misses a day.month with no trailing dot): RED
  `72fc896` (3 tests: `date_grounded` no-dot case, weight-after-na guard, `date_conflict`
  body-day no-dot case), GREEN `b890aaa` (widened `_days_in()` via a new generalized
  `_ANNOUNCED_DAY`/`_announced_days_in`, reusing #187's `_QUOTED_DAY` guard; `_SUBJ_DAY`
  and `date_conflict`'s strict subject check left untouched). Deep review found a bonus
  fix (`unquote_fully_quoted`/`_orders_a_day_still_ahead` also affected — a stale
  no-dot-dated whole-quoted thread could have been wrongly unquoted/re-shipped);
  follow-up test `d00d97e`.
- **#188** (CI eval corpus backfill + standing rule): read-only sweep of production
  `messages`/`order_runs` on the HA box; added 2 new verified corpus cases directly on
  dev2 (outside git) — `resortceder-2026-08-03-db6d8af5` (4-day whole-quoted CÉDER order,
  confirms #186's alias-bias fix independently) and `domovina-2026-08-03-000201dd` (PDF
  attachment, 2 dates, garbled subject date). 35-case corpus, `--require-all` exit 0,
  baseline updated. Standing rule added to `.claude/rules/orders-corpus.md`: `6c58f54`.
  Sweep surfaced a real historical wrong shipment (same #186 class, 3 days earlier,
  undetected) — filed as #193 (left open, business/customer decision, not code).
- **#189** (soften alias-names-customer prompt): `9108eee` — carve-out in
  `match_product.md` for wording that clearly names a different product than the
  alias-bearing card. Validated with a full `--live` re-record of the 35-case corpus:
  30/35 pass (5 pre-existing known-defect #120 cases, unrelated), zero regressions,
  #186's own CÉDER case still passes.
- Deep code review (general-purpose subagent, adversarial): 2 Important findings, both
  resolved (1 fixed with a new test, 1 explained — #190's real trigger mail already has
  a corpus case from #187's PR, doesn't mechanically depend on #190's fix; the genuinely
  new mechanism has no real-world mail yet, stays pytest-only per the corpus's own
  "real customer email only" rule). 3 Minor findings pre-existing, out of scope.
- Deploy: PR #194 merged (`b07ef426e7916c3108babdec183f98ea4020b148`), main CI green
  (test+e2e-orders+build), deployed **v0.9.49** — `/health` 200 + dashboard DOM confirm
  `v0.9.49`, `/otazky` (Otázky skladu) loads live with 0 pending, worker started clean.

## 2026-08-06 — #195 + #196 (batch, PR #197)

- **#195** (class-level lexical tripwire for `llm_sure`): `match.decide()`'s SURE rung
  gains a cause-independent guard right after #186's `alias_better` check — a wording
  sharing NO distinctive content word with the card's own name/non-customer-naming
  alias downgrades to new rung `llm_sure_lexical_gap` (gtin cleared, review=True,
  added to `pipeline.ASK_THE_WAREHOUSE`) instead of shipping. New
  `_card_reference_words()`/`_lexical_overlap()` in `match.py`. Verdict recorded in
  `trace["lexical_guard"]`.
  RED `3fbd50a` → GREEN `b6e4f2e`. **Corpus validation (dev2, 35 cases, offline,
  `--require-all`) found 2 real false positives** on the first (exact-token-match)
  implementation — "oliva" vs the card's "olivovo", "tekvička" vs "tekvicový", both
  genuine Slovak inflection of the same product. Fixed by comparing a shared
  `STEM_PREFIX`-length (4-char) prefix instead of exact equality: `c8b9c24`. Re-run:
  0 regressions, gate PASSED (only the 5 pre-existing `known_defect: #120` cases
  fail, unrelated). Both real incidents this ticket cites (run 112/241, corpus cases
  `resortceder-2026-08-03-db6d8af5`/`resortceder-2026-08-06-89d1855d`) are already
  caught by #186's `alias_better` mechanism — #195 is deliberately the broader,
  cause-independent safety net, not a re-fix.
- **#196** (daily match-provenance digest + dashboard): new `match_incidents` table
  (append-only, self-seeded with the two real incidents #195 found — #157/#186) backs
  `reliability.days_since_incident()`, always live-computed. New
  `app/orders/reliability.py`: `provenance_stats_for_day()` buckets `order_items`
  (shadow=false only) into deterministic/AI-rung(`llm_sure`)/held-for-review
  (`ASK_THE_WAREHOUSE`) + per-run error/order counts; `maybe_post_daily_digest()`
  posts once per calendar day (new `order_digest_sent` claim table, same pattern as
  `spend.cap_tripped`) through the existing Odoo channel, wired into `worker.tick()`.
  New `report.build_daily_digest()`, new `/api/orders/digest` route + a dashboard
  header badge (`reliabilityBadge`). Feature work (no RED/GREEN split): `db4bf21`.
  Playbook: `.claude/rules/orders-corpus.md`'s #188 standing rule extended — a future
  incident-fix PR adds a `match_incidents` row in the same PR as the corpus case.
- Version bump `ec31bd2`: 0.9.49 → 0.9.50.
- Design/validated/review comments posted on both issues before code / before merge
  (per repo convention — see issue comment threads).
- Tests: `tests/test_orders_match.py` (10 new #195 tests), `tests/test_orders_
  reliability.py` (new file, 14 tests), `tests/test_orders_report.py` (+7 digest
  tests), `tests/test_db.py` (+1 seed test), `tests/test_httpapi.py` (+2 auth tests).
  Full suite green (`pytest -q`, zero F/E), `ruff check .` clean.
- Shared PR: **#197** — merge `5faaed03334843f804ed56e97cd59220b87a6691`, main CI
  green (test+e2e-orders+build). Deployed **v0.9.50**: `/health` 200, dashboard DOM
  confirms `v0.9.50`, worker started clean (`engine=python shadow=False
  static_shadow=True`), 0 console errors. Functional: `/api/orders/digest` +
  the new `reliabilityBadge` show REAL live production data ("0 dní bez incidentu ·
  včera 31/54/2 (isté/AI/kontrola)" — the 2026-08-06 seeded incident makes 0 days
  correct as of deploy day); today's live stats show `llm=24, review=0` — the new
  #195 guard is live and not falsely flagging genuine matches (0 `llm_sure_lexical_gap`
  rows in production yet, consistent with the guard being rare-case-only).

## 2026-08-07 — #200 (PR #206)

- **#200** (DL migration Phase 1: schema + config + spec, foundation only). Sanitized
  binding spec `docs/superpowers/specs/2026-08-07-delivery-notes-python-design.md`
  (n8n rules map R1-R97, weaknesses W1-W16). Config trio
  `delivery_notes_engine`/`delivery_notes_shadow`/`delivery_notes_shadow_days`
  (default `n8n` = fully inert) + `dl_catalog_gid` + `orion_dl_dir`. New tables:
  `desadv_sent` (`app/orders/desadv.py`, two-phase claim/confirm, identity
  `(supplier_ean, doc_number)` — fixes n8n's W2/W3/W4), `dl_item_memory`
  (`app/orders/dl_memory.py`, `item_memory`'s sibling + a `cnt` column for R66),
  `dl_snapshots`/`dl_catalog_snapshot`/`dl_supplier_snapshot`
  (`app/orders/dl_snapshot.py`, content-addressed DL catalog union + supplier
  loader, own versioning line). One-shot n8n import script `scripts/
  import_dl_item_memory.py` (outside `app/`, never ships in the Docker image; real
  run deferred to cutover). `order_runs` reused with zero schema change (documented
  decision, spec §7). Feature work, no RED/GREEN split (greenfield foundation):
  `4eb438e`/`37c6a76`/`896396a`/`c426830`/`87f1b04`/`8d1d930`.
- Design comment + validated comment posted before code (issue comment thread).
  Deep code review (dispatched `general-purpose` subagent) found 1 Critical + 3
  Important + 7 Minor issues, all fixed in `cd77b68` — including a **live Google
  Sheet doc id accidentally committed** to the spec (acts as a credential:
  unauthenticated CSV export). Redacted going forward; the historical git-history
  exposure itself is tracked as a separate, non-blocking decision for the user in
  a follow-up ticket (issue 207).
- Tests: `test_config.py` (+5), `test_desadv.py` (11), `test_dl_memory.py` (12),
  `test_import_dl_item_memory.py` (4), `test_dl_snapshot.py` (18). Full suite green
  (`pytest --cov=app --cov-fail-under=85`, 92.64% total), `ruff check .` clean.
- Shared PR: **206** — merge `02b39fb81bb241a8a74c6ebbb1b72cab0b7a8fc0`, main CI
  green (test+e2e-orders+build). Deployed **v0.9.51** via `ha addons update`
  (supervisor add-on, not a raw container — see `.claude/rules/deploy.md`):
  `/health` 200 `{"version":"0.9.51"}`, dashboard DOM confirms `v0.9.51`, worker
  started clean (`engine=python shadow=False static_shadow=False` — unaffected,
  `delivery_notes_engine` stays `n8n`), 5 new tables confirmed present in live
  Postgres (`desadv_sent`, `dl_item_memory`, `dl_snapshots`, `dl_catalog_snapshot`,
  `dl_supplier_snapshot`). Everything landed is inert by default — no pipeline code
  reads/writes any of it yet.

## Issue 201 (DL migration F2: Vision + multi-document extraction) — 2026-08-07

- New `app/orders/dl_extract.py` (pure functions, no DB/worker wiring — deliberately
  out of scope, worker integration is a later phase): scan detection via raw JPEG
  SOI/EOI byte-marker scanning (R40, every embedded page transcribed — fixes W1c),
  R42 text-source priority (a digital PDF with real extracted text never pays for a
  vision call — fixes W13), R43 up-to-three-way transcript cross-check, a
  MULTI-document JSON extraction schema (`{"documents": [...]}` — fixes W1b), R50-R52
  validation (quantity self-correction via the totalPrice/unitPrice line equation, a
  0.50€ money gate, zero-items AND missing/unparseable-date review gates), and
  `extract_email()` processing EVERY attachment of the message (fixes W1a) with
  per-attachment failure isolation. `llm.Client` gains `vision_call()`: a raw Chat
  Completions POST (distinct from the existing Responses-API `json_call`) with `n`
  independent samples + image/file content parts, content-addressed cached,
  offline-safe, priced through the same `PRICES` table (incl. reasoning-token
  detail). Fresh Slovak prompts `prompts/dl_extract.md`/`prompts/dl_vision.md`
  written from the sanitized spec, never copied from the (real-data) scratchpad.
  RED/GREEN commits: `89b676f`/`18ba518` (vision_call), `d503722`/`6ccb1af`
  (dl_extract module), `70c0df7` (extra coverage), `d5aa7f8` (reasoning-token fix).
- Design comment + still-valid comment posted before code (issue #201 thread).
  Deep code review (dispatched `general-purpose` subagent against the real PR diff)
  found **1 Critical + 4 Important** issues — ALL fixed with their own RED/GREEN
  regression tests before merge: `combine_transcripts()` truncated its
  same-vs-different comparison to an 80-char head, so two transcripts sharing a
  realistic delivery-note header silently dropped a genuine item-level disagreement
  (`defb322`/`45a422f`); `strip_lt_prefix()` was an unanchored substring search for
  "LT" anywhere in the string, corrupting any doc number merely containing those two
  letters (same commits, anchored to the documented `<digits>LT<digits>` shape); a
  missing/unparseable delivery date silently validated instead of forcing review, and
  one bad/corrupt attachment aborted the WHOLE mail's remaining attachments instead of
  being isolated (`0891dbe`/`a5cffa4`, which also added logging at every decision
  branch per the project's comprehensive-logging policy). Boundary-value tests for
  the 0.50€ money gate and the quantity-correction tolerance added in `4949961`.
- Tests: `test_orders_llm.py` (17, vision_call), `test_orders_dl_extract.py` (69).
  Full suite green (`pytest --cov=app --cov-fail-under=85`, 92.70% total,
  `dl_extract.py` 100%), `ruff check .` clean.
- Shared PR: **209** — merge `115db989c729b8c1e30aa6140e573421031d5b04`, main CI
  green (test+e2e-orders+build). Deployed **v0.9.52** via `ha addons update`:
  `/health`+`/version` 200 `0.9.52`, dashboard DOM confirms `v0.9.52` (0 console
  errors/warnings), functional check run LIVE inside the deployed container (every
  new function exercised against the actual shipped code — scan detection, LT-strip,
  date normalize, quantity self-correction, multi-document extraction+validation,
  vision_call's error guard). `delivery_notes_engine` confirmed still `"n8n"` — the
  live n8n dodacie listy pipeline is completely untouched by this phase.

## Issue 202 (DL migration F3: matching ladder, knowledge DB, nástenka) — 2026-08-07

- **#202** (DL migration Phase 3: supplier + item matching, `dl_item_memory.resolve()`,
  two new nástenka question kinds). New `app/orders/dl_match.py` (pure, DB-free,
  mirrors `match.py`/`customer.py`): `supplier_candidates()`/`decide_supplier()` (R60
  deterministic pre-score + R61 model-answer interpretation, refuses a hallucinated
  EAN not in the supplier whitelist) and `candidates()`/`decide_item()` (R62 OCR fix,
  R63 normalization, R64 `w_eq` word-equality, R65 candidate scoring, R70-R76's full
  post-match gate ladder — confidence bands, ALIAS RESCUE overriding even the weight
  guard per R67, MEMORY RESCUE that never overrides a SURE model match, the
  WEIGHT-CONFLICT guard with its `memWeightOverride` escape, and a final lexical
  zero-overlap tripwire copying `match.py`'s own proven `#195` pattern). No prompt
  files or LLM call wiring — deliberately deferred, mirroring F1/F2's own scope
  boundary (see the design comment on #202).
  `app/orders/dl_memory.py` gained `resolve()` — R66's weighted-majority history
  read (`max(cnt)` per `(gtin, day)`, then summed across days, per the module's own
  #200 docstring guidance), catalog-invalidation, and a human-taught-first rung.
  New `app/orders/dl_supplier_memory.py` — a small standalone taught
  `sender_email -> ean_edi` table (deliberately NOT a full `customer_overrides`-style
  override/rebuild system — a documented, proportionate scope call).
  `app/orders/teach.py` gained two `KINDS` entries, `dl_item`/`dl_supplier`, reusing
  the #164 generalized funnel; wired into `app/httpapi.py`'s dispatch/undo and both
  duplicated JS question-card blocks (`DASH_HTML` + `ASK_HTML`).
- Design comment + still-valid comment posted before code (issue #202 thread). Deep
  code review (dispatched `general-purpose` subagent against the real PR diff, per
  `superpowers:requesting-code-review`) found 0 Critical, 2 Important, 4 Minor — all
  fixed before merge in `0ed8746`: `dl_memory.resolve()`'s human-taught rung now
  iterates every taught gtin group (newest first) instead of only the single most
  recent one (an older still-valid teach was silently skipped when the newest teach's
  gtin had since left the catalog); `ask_dl_item`/`ask_dl_supplier` now skip asking
  when the (supplier, wording)/address is already taught, mirroring `ask()`'s own
  `recalled.human` pre-check; plus documentation/test-quality Minor fixes (a
  tautological test assertion, missing kind coverage in an existing test, a second
  Playwright E2E for `dl_supplier`).
- Tests: `tests/test_dl_match.py` (55 cases, 100% line coverage), `tests/
  test_dl_memory.py` (+23 cases for `resolve()`, 100%), `tests/
  test_dl_supplier_memory.py` (6 cases, 100%), `tests/test_orders_teach_kinds.py`
  (+17 cases), `tests/test_api.py` (+1 HTTP dispatch), `tests/test_e2e.py` (+2
  Playwright — `dl_item` and `dl_supplier` nástenka cards, real browser, zero console
  errors). Full suite green (`pytest --cov=app --cov-fail-under=85`, 93.24% total,
  `dl_match.py`/`dl_memory.py`/`dl_supplier_memory.py` all 100%), `ruff check .`
  clean.
- Shared PR: **212** — merge `58fd54630f974cfcaeb5a6ffef8ee570057a87b8`, main CI
  green (test+e2e-orders+build, run `31191345105`). Deployed **v0.9.53** via
  `ha addons update`: `/health`+`/version` 200 `0.9.53`, dashboard + `/otazky` DOM
  both confirm `v0.9.53` (0 console errors on either page). Functional check run
  LIVE inside the deployed container against the real Postgres (synthetic test data,
  fully cleaned up afterward): every new pure function (`supplier_candidates`,
  `decide_supplier`, `candidates`, `decide_item`) exercised against the actual
  shipped code; `dl_memory.resolve()`/`dl_supplier_memory.resolve()` exercised
  against the real live Postgres; a full browser click-through on the LIVE
  `/otazky` page via the real `/sklad/<key>` link — seeded a real `dl_item`
  question, it rendered with the new "Ktorá karta je táto DL položka?" title,
  clicked the offered candidate, confirmed `dl_item_memory` was taught correctly.
  `delivery_notes_engine` confirmed still `"n8n"` — this phase ships fully dark,
  the live n8n DL pipeline is untouched.

## Issue 203 (DL migration F4: DESADV builder + upload + import confirmation) — 2026-08-07

- **#203** (DL migration Phase 4: DESADV EDI document builder, `in_DL` upload target,
  import-confirmation sweep extension). New `app/orders/desadv_edi.py`: byte-parity
  port of the production "ASSEMBLE AND GENERATE EDI [v1]" Code node
  (`sub3_edi_code.js` v27, extracted via n8n MCP earlier in the session, never
  committed — no real customer data, business logic only). `generate()` (pure HDR+LIN
  builder — R84 quantity/unit conversion ladder: kg-tracked sklad=100 with an eggs
  exception, liquid multipack takes precedence; R85 price fallback at the missing/5x/
  0.2x boundaries) is pinned byte-for-byte against `tests/fixtures/
  desadv_reference.json` (11 synthetic cases generated by running the REAL JS under
  node). `build()` (orchestration) implements R81's canCreateEDI gate + reject
  reasons, R83's docNumber handling (digits-only in EDI content vs human-facing
  original — including a faithfully-ported `|| doc_number` fallback verified against
  the real JS, not a Python divergence), and R89's filename/upload-name. R80's qty==0
  lines are reported via `items_skipped_zero_qty` instead of silently dropped (W10
  fix); W11's "LIN unit column unchanged except multipack forces L" contract is now
  explicit in code.
  `app/orders/upload.py`: `list_dirs()` extended with an `in_DL` key (verified LIVE
  against the real ORION box: `in_DL` has NO `archCodex`/`unconfirmed` of its own —
  shares `in`'s); `put()` gained an optional `dir_override`. `app/db.py`:
  `desadv_sent` gained the same `import_status`/`import_confirmed_at`/
  `import_checked_at` columns `edi_sent` got in #151 (same migration shape);
  `import_alert_incidents` gained a `source` column (CHECK-constrained) + a
  `(channel_id, kind, source)` unique index (created BEFORE the old one was dropped,
  a review-caught ordering fix); new `import_alert_incident_desadv_members` table
  (separate from the existing members table — rejected a polymorphic-FK alternative
  that would have required dropping a live table's PRIMARY KEY, see the design
  comment on #203). `app/orders/confirm.py`: `sweep()` now processes BOTH `edi_sent`
  and `desadv_sent` in one `list_dirs()` call via a `_Ledger` descriptor; alert
  wording is source-aware ("dodací list(y)", never "objednávka", for DESADV
  incidents). `app/orders/worker.py`: `confirm.sweep()` now also fires when
  `delivery_notes_engine=python` (previously gated on `ai_orders_engine` alone, which
  would have made the new sweep coverage unreachable even after a future worker).
  Worker/pipeline wiring (claiming a message, orchestrating extract→match→build→
  upload, Odoo notifications) is deliberately OUT of scope — that's F5, mirroring
  F1-F3's own scope boundary.
- Design comment + still-valid comment posted before code (issue #203 thread). Deep
  code review (dispatched `Explore` subagent against the real PR diff, per
  `superpowers:requesting-code-review`) found 0 Critical, 2 Important, 7 Minor — all
  fixed before merge (`4b23c24`): the new `(channel_id, kind, source)` unique index
  is now created BEFORE the old `(channel_id, kind)` one is dropped (the reverse
  order left a real window with zero uniqueness enforced on a table `edi_sent`'s
  already-live sweep writes into); the digits-only doc-number fallback was verified
  against the real JS source and documented + pinned with a test rather than "fixed"
  (fixing it would have diverged from the byte-parity guarantee); plus a dead-code
  cleanup (a third unused copy of the `in_DL` path), a misleadingly-named
  `_format_mass`→`_format_price` rename (it's only ever called on `unit_price` — LIN
  has no separate mass field), a shared `_is_unmatched()` helper replacing a
  duplicated predicate, new tests for the multipack count>1000 cap and the Czech
  ě/Ě fold the module's own docstring claimed but never actually exercised, and a
  documented footgun note on the `_Ledger`-parametrized functions' `EDI_LEDGER`
  default.
- Tests: `tests/test_orders_desadv_edi.py` (42 cases, fixture parity + `build()`
  orchestration), `tests/test_orders_upload.py` (+4 for `in_DL`/`dir_override`),
  `tests/test_db.py` (+7 for the new migrations), `tests/test_orders_confirm.py`
  (45 total, 12 new for the `desadv_sent` sweep), `tests/
  test_desadv_upload_integration.py` (new — composes builder+ledger+upload,
  including the claim-release failure path). Full suite green (`pytest --cov=app
  --cov-fail-under=85`, 93.55% total), `ruff check .` clean.
- Shared PR: **213** — merge `baeb260dd031d929af7d07533afb6383f558aa15`, main CI
  green (test+e2e-orders+build, run `31200325689`). Deployed **v0.9.54** via
  `ha addons update`: `/health`+`/version` 200 `0.9.54`, dashboard DOM confirms
  `v0.9.54` (0 console errors). Functional check run LIVE inside the deployed
  container against the real Postgres (synthetic test data, fully cleaned up
  afterward, zero writes to ORION): `desadv_edi.build()` exercised against the
  actual shipped code (HDR/LIN widths, digits-only doc number, partial-EDI
  reporting); `desadv.claim_send()`/`release_send()` exercised against the real live
  Postgres; `confirm.py`'s `DESADV_LEDGER`/`EDI_LEDGER` confirmed correctly wired.
  `delivery_notes_engine` confirmed still `"n8n"` — this phase ships fully dark, the
  live n8n DL pipeline is untouched (the dashboard's own live feed showed real
  `dodacie_listy` mail being processed by the unmodified n8n workflow throughout).

## #204 — DL migrácia F5: worker + shadow + Odoo hlásenia + kontrola ohlásený-vs-priložený DL

- Version bump `10525c6` (0.9.54 → 0.9.55). New `app/orders/dl_worker.py` (the worker
  loop wiring F2 extraction + F3 matching/memory/nástenka + F4 EDI builder/upload
  ledger into one three-mode engine — same n8n-inert/n8n+shadow/python shape
  `static_worker.py` uses) + `app/orders/dl_report.py` (new — per-DOCUMENT Odoo
  messages, R95/R96, deliberately separate from `report.build_summary`'s per-e-mail
  policy) + two new LLM prompts (`prompts/dl_match_supplier.md`/`dl_match_item.md`).
  `app/orders/desadv.py` gained `already_sent()` (read-only duplicate check for
  shadow). `app/orders/reliability.py`/`report.py` gained a DL-scoped digest
  counterpart, explicitly excluded from the AI-orders provenance stats via a
  `result->>'kind'` filter. `app/orders/worker.py` wired `dl_worker.refresh_due`/
  `tick` into `run_forever`. Spec §4's announced-vs-attached check (the Lunys "IS
  KARAT" incident — a subject can announce 2 DL numbers while only 1 PDF arrives) +
  W7's duplicate-document visibility both surface via the daily digest, never a
  silent skip or an immediate ping.
- Design decision: `order_runs.snapshot_id` has a real FK to `order_snapshots(id)`,
  not `dl_snapshots(id)` — resolved by passing `snapshot_id=None` (nullable) to
  `worker._start_run`/`_finish_run` and stashing the real DL snapshot id inside
  `result["dl_snapshot_id"]`, with `result["kind"]="dl"` as the sole discriminator.
- Deep review (`/requesting-code-review`) found 2 Critical + 3 Important issues
  before merge, all fixed in the same PR: shadow mode's `report.log_event` calls
  (incl. two `rollup=true` ones) ran unconditionally — reproduced silently
  corrupting an already-delivered message's dashboard state; an item excluded from
  the EDI by the R75 lexical-gap tripwire or the `match_failed` fallback was
  invisible (nástenka question + Odoo message both keyed on the literal string
  `"unmatched"` instead of `not decision.gtin`); a hard pipeline failure passed
  `result=None`, leaving `kind` NULL and miscounting the failure into the AI-orders
  digest; `_post` evaluated the Odoo HTML builder outside its own try/except. One
  finding (retry-after-partial-ship logs a false `duplicate_skip` for the SAME
  message's own already-shipped document) needs a schema change to fix cleanly —
  filed as follow-up **#216**; the core no-double-shipment safety property already
  holds either way.
- Tests: `tests/test_dl_worker.py` (24 cases — claim/shadow/retry semantics, R15,
  duplicate visibility, nástenka questions, announced-vs-attached, both deep-review
  regression scenarios), `tests/test_desadv.py` (+4 for `already_sent()`), extended
  `tests/test_orders_reliability.py`/`test_orders_report.py`. Full suite green
  (`pytest --cov=app --cov-fail-under=85`, 93.05% total), `ruff check .` clean.
- Playbook: `.claude/rules/orders-corpus.md` gained 3 entries (the `snapshot_id` FK
  gotcha for a third shared-table engine, the shared-rule-name digest-collision
  risk, and the `last_prompt_hash` fake-client requirement for `dl_extract` tests).
- Shared PR: **215** — merge `3cf2b61e9bfe18be8d44de50260b0829eaa07416`, main CI
  green (test+e2e-orders+build, run `31207052575`). Deployed **v0.9.55** via
  `ha addons update`: `/health` 200 `0.9.55`, dashboard DOM confirms `v0.9.55`.
  Functional check run LIVE inside the deployed container against the real
  Postgres, read-only/no-op: `dl_worker.tick()` → `0` (correctly inert),
  `dl_worker.refresh_due()` → `None` (no DL catalog gid configured yet),
  `reliability.dl_provenance_stats_for_day()` → clean honest zeros against the real
  production `order_runs`/`email_events` tables. `delivery_notes_engine` confirmed
  still `"n8n"` — this phase ships fully dark; the dashboard's live feed showed real
  `dodacie_listy` mail still processed by the unmodified n8n workflow throughout.

## #205 — DL migrácia F6: eval korpus (8 pomenovaných incident-tried) + shadow okno start

- Version bump `936cec1` (0.9.55 → 0.9.56). New `app/orders/dl_evaluate.py` + `app/orders/
  dl_eval_run.py` (mirrors `evaluate.py`/`eval_run.py`, DL-shaped: scores per DOCUMENT, not
  per email/delivery-date, since one mail can carry several delivery notes — F2's own
  W1a/W1b fix). `dl_worker._process_message` gained an optional `attachments=` override
  (mirrors the existing `upload=`/`post=` DI seam) plus `supplier_ean`/`items` on its
  ok/partial/duplicate return. New `e2e-dl` CI job in `.github/workflows/ci.yml` (mirrors
  `e2e-orders`), runs on every change.
- Real 8-case corpus built on dev2 (`~/eval-corpus/email-extractor/dl/`, outside git): 3
  grounded in REAL production Postgres data (Lunys announced-vs-attached pair msg 6218/6222,
  Jackulík 2-PDF-in-one-mail msg 5900, MPC P-prefix docNumber msg 6417 — all from 2026-08-07,
  verified read-only against the live add-on's own Postgres); 5 clearly-labelled synthetic
  fixtures (LESAFFRE Netto-kg-over-cartons R49b, thousands-separator quantity R49a,
  Dalamanka/Dalmátska product-name precision, Forbak s.r.o./spol. wording variant R61,
  EKVIA missing-catalog-card partial-EDI R81) exercising the exact named rule where no real
  message could be found. `--live` recording: 8/8 passed on the first attempt; offline
  replay confirmed both locally and in CI.
- Deep review (`Explore` subagent against the real PR 218 diff) found 0 Critical, 2
  Important, 4 Minor, all fixed before merge (`234770e`): `dl_worker._RetryLater` (a
  genuine transient LLM failure) escaped `dl_evaluate.run_case`/`run_corpus` uncaught,
  crashing the whole corpus run instead of failing one case — now caught; `score()`'s
  duplicate-doc_number matching was order-dependent (a real W4 scenario) — replaced with
  `_best_fit_group`'s injective-assignment search; `_shipped_items` re-derived the "on the
  EDI" predicate a third time instead of the shared `desadv_edi._is_unmatched`; `_items_map`
  could collapse two different no-gtin items onto a shared `"None"` key.
- Tests: `tests/test_dl_eval.py` (new, ~40 cases: scoring, best-fit duplicate matching,
  `_RetryLater` handling, the gate CLI). Full suite green (`pytest --cov=app
  --cov-fail-under=85`, 93.23% total), `ruff check .` clean.
- Shared PR: **218** — merge `731e4003e6ce5f13ea500bff988ac715a31453b5`, main CI green
  (test+e2e-orders+e2e-dl+build, run `31215056422`). Deployed **v0.9.56** via `ha addons
  update`: `/health` 200 `0.9.56`, dashboard DOM confirms `v0.9.56` (0 console errors).
  Shadow window started LIVE (not just deployed dark): `dl_catalog_gid=1437442607` +
  `delivery_notes_shadow=true` set via the supervisor options API + add-on restart. Log
  confirmed `dl_shadow=True`, a real DL catalog snapshot froze (491 catalog rows, 959
  suppliers), and the worker immediately shadow-processed a REAL live message (#6417, MPC,
  docNumber P26042214 — the same message used as eval case 3) — `order_runs.id=363,
  shadow=t, kind=dl, status=partial`. Zero observable side effect confirmed directly in
  Postgres: `desadv_sent`/`dl_item_memory`/`email_events(workflow=delivery_notes)` all 0
  rows for that message, `messages.processed`/`processing_at` untouched from the real n8n
  run. `delivery_notes_engine` stays `n8n` — the live pipeline is unchanged, python only
  compares in the background.
- Cutover (`delivery_notes_engine: n8n -> python`) is explicitly OUT of scope — filed as a
  separate follow-up **#217** (mirrors the #132/#133 split for static orders: shadow-wiring
  ticket closes once shipped, cutover is its own ticket blocked on days of a clean shadow
  diff + the user's explicit decision).

## 2026-08-07 — #216 (PR #219)

- **#216** (DL worker: retry after a partial ship logs a false `duplicate_skip`, needs
  `desadv_sent.message_id`): a #204/F5 deep-review residual finding. R17's transient retry
  re-processes the WHOLE message on its next tick, including a document that already
  shipped successfully in an earlier, partially-failed attempt of that SAME message —
  `desadv.claim_send()` correctly refused the second claim, but `dl_worker.py` had no way
  to tell that self-caused re-skip apart from a genuine cross-message W7 duplicate, so both
  logged identical `stage='duplicate_skip'`, inflating `reliability.
  dl_provenance_stats_for_day()`'s daily-digest `duplicates` count.
- Additive, nullable `desadv_sent.message_id TEXT` column (no backfill — legacy rows stay
  NULL, always treated as unknown/genuine duplicate). `desadv.claim_send()` gains an
  optional `message_id` param; new read-only `desadv.claimed_by()`. Deep-review (dispatched
  Explore subagent on the real PR diff) found 0 🔴/🟡, 2 🔵 — both fixed pre-merge: (1) a
  microsecond-scale TOCTOU gap between `claim_send()` and a separate `claimed_by()` read,
  closed by new atomic `desadv.claim_send_or_identify()` (one round trip, data-modifying
  CTE + `NOT EXISTS` fallback `SELECT`) which `dl_worker.py` now calls instead of the
  two-step pattern; (2) a weak test's docstring corrected + a stronger sibling test added
  (known-but-different claimant, genuinely exercises the comparison logic).
- Commits: `d7bb5a6` (version bump 0.9.56→0.9.57), `98ea1ce`
  (`test_retry_after_partial_ship_logs_a_self_retry_not_a_false_duplicate` — RED,
  `TypeError`/`AttributeError` pre-fix), `d89d356` (GREEN — schema + `claim_send`/
  `claimed_by` + `dl_worker.py` wiring + `dl_report.log_already_shipped_this_run`),
  `90831ad` (review-driven: `claim_send_or_identify` + strengthened test). Full suite
  (1258 tests) green, `ruff check .` clean, all 5 shadow-mode tests re-verified green
  (shadow never calls `claim_send`/`claim_send_or_identify`, only read-only
  `already_sent()`).
- Shared PR: **219** — merge `a6a131a5959e29e340c74d1764edc40d02fb3e23`, main CI green
  (test+e2e-orders+e2e-dl+build, run `31220312666`). Deployed **v0.9.57** via
  `ha addons update`: `/health` 200 `0.9.57`, dashboard DOM confirms `v0.9.57` (0 console
  errors). Live process restarted clean on 0.9.57 with no schema-migration error
  (`\d desadv_sent` confirms the new nullable `message_id` column). `delivery_notes_engine`
  stays `n8n`/`delivery_notes_shadow=true` (unchanged, per the hard prohibition on flipping
  it) — `desadv_sent` has 0 rows in production today (shadow never writes it), so the new
  claim/identify decision logic is not yet live-exercised; its correctness is proven by the
  local RED→GREEN suite, not a live trigger. 26 shadow DL runs in the prior 2h window, 0
  errors, confirming the deployed process is healthy end-to-end.

## #129 — Databáza znalostí: vypnúť čítanie Google Sheetu, DB je jediný zdroj pravdy (2026-08-08)

- Removed ALL Google Sheet network reads from the add-on: `snapshot.fetch_csv`/
  `sheet_csv_url`/`refresh()` and `dl_snapshot.refresh()` (the DL catalog's own copy)
  deleted entirely — the only network-touching code in either module.
  `worker.refresh_due`/`dl_worker.refresh_due` simplified to a single-line
  `return <module>.latest_snapshot_id(conn)` — no fetch, no interval check, no config
  gate. `import_snapshot`/`import_files`/`parse_catalog`/`parse_customers`/
  `parse_dl_catalog`/`merge_catalog` and every #127/#128 override function (dashboard
  editing) are untouched — pure, network-free CSV/DB functions.
- `config.yaml` schema/options and `Config` dataclass fields (`catalog_sheet_id`,
  `catalog_gid`, `customer_gid`, `dl_catalog_gid`, `catalog_refresh_minutes`) left
  as-is deliberately — removing a live add-on's schema key risks failing options
  validation on start; the code simply never reads them for fetching any more.
- The AI-orders catalog+customers keep their #127/#128 dashboard-editable path (live
  since 2026-08-02). The DL catalog (491 rows) + suppliers (959 rows) froze on their
  2026-08-07 state — safe because `delivery_notes_engine` is still `n8n` (DL Python
  engine is shadow-only; real cutover tracked separately, already-open ticket #217).
  Dashboard editing for the DL-specific fields filed as its own follow-up, **#221**.
- Commits: `4dd0cea` (version bump 0.9.57→0.9.58), `4fd4160` (RED — 5 new/changed
  tests proving the sheet was still read: network-call + `hasattr` structural
  assertions, all failing pre-fix), `3330cb4` (GREEN — the removal itself; full suite
  1260 tests green, 93% coverage, ruff clean), `0f8576f` (review-driven cleanup: 3
  stale "sheet fetch"/"hourly refresh" docstring/message leftovers in
  `snapshot.py`/`dl_snapshot.py`/`httpapi.py`).
- Deep review (Explore subagent, adversarial pass on the real diff): 0 🔴 0 🟡 3 🔵,
  all fixed pre-merge. Confirmed no other caller of the removed functions anywhere in
  the repo.
- Shared PR: **222** — merged `6f6e42f7cf96a2e42bab9fee11e3391d70d12f52`, main CI green
  (test+e2e-orders+e2e-dl+build, run `31224834022`). Deployed **v0.9.58** via
  `ha addons update`: `/health` 200 `0.9.58`, dashboard + `/znalosti` DOM confirm
  `v0.9.58` (0 console errors on both). Live verification: 8+ minutes of post-restart
  logs show ZERO `docs.google.com`/sheet/urlopen activity (the actual proof of the
  fix) and zero errors/exceptions; `order_snapshots`/`dl_snapshots` row counts and
  `checked_at` unchanged since before the deploy (127 catalog cards / 491 DL cards +
  959 suppliers, all still served correctly — `/znalosti` catalog search for "Rožok"
  returned 6 real cards from the frozen DB snapshot); worker startup log confirms
  `dl_shadow=True` (DL shadow engine still armed, unaffected).

## #221 — DL katalóg + dodávatelia: dashboard editovanie (mirror #127/#128) (2026-08-08)

- Mirrors #127/#128's product-card/customer curation for the DL-specific line
  (`dl_catalog_snapshot`/`dl_supplier_snapshot`, frozen since #129), on its OWN
  `dl_snapshots` versioning line — deliberately NOT the shared `catalog_overrides`/
  `customer_overrides` tables (a shared GTIN between the AI-orders and DL catalogs
  must never let a DL-only edit rewrite the AI-orders side; same reasoning
  `dl_snapshots` itself already used to stay independent of `order_snapshots`).
- `db.py`: 2 new tables, purely additive — `dl_catalog_overrides` (PK `gtin`) and
  `dl_supplier_overrides` (surrogate id + `orig_ean_edi`/`orig_city` partial-unique
  index — city, not street, since `dl_supplier_snapshot` never persists street/zip).
- `app/orders/dl_snapshot.py`: `parse_number` (public `_num` wrapper) +
  `dl_catalog_for_management`/`upsert_dl_catalog_card`/`retire_dl_catalog_card`/
  `dl_suppliers_for_management`/`upsert_dl_supplier`/`retire_dl_supplier`/
  `dl_rebuild_from_overrides`. `import_snapshot` now ALSO applies these overrides at
  freeze time (mirroring `snapshot.import_snapshot` exactly) — the TDD RED tests,
  mirrored 1:1 from the AI-orders override tests, proved this is what "mirror
  #127/#128" actually requires; the design comment's "import_snapshot stays
  untouched" undersold this by one detail.
- `app/httpapi.py`: `/api/znalosti/dl-products` + `/api/znalosti/dl-suppliers`
  (GET/POST/DELETE), same shape as `/products`/`/clients`; `SKLAD_ZNALOSTI_API`
  widened so the warehouse link reaches them; 2 new `/znalosti` JS boxes
  (`dlProductsBox`/`dlSuppliersBox`).
- `tests/conftest.py`: the `pg` fixture's TRUNCATE list gained the 2 new tables —
  found via a real cross-test leakage failure while turning RED to GREEN (gotcha now
  captured in `.claude/rules/local-testing.md`).
- Commits: `f5295d3` (version bump 0.9.58→0.9.59), `98cc9dc` (RED — 28 new tests),
  `18f1111` (GREEN — full implementation, 94% coverage, ruff clean), `70a6f68`
  (docs: deploy.md container-name gotcha), `5a27657` (2 more tests pinning the deep
  review's 2 🔵 findings — both were inherited-design notes, not bugs).
- Deep review (Explore subagent, adversarial 9-angle pass on the real PR diff): 0 🔴
  0 🟡 2 🔵, both addressed (full-replace-upsert + SKLAD_ZNALOSTI_API anchoring, both
  pinned as explicit tests rather than left implicit).
- Shared PR: **223** — merged `a64c06bfcbbe69bd346ca59b04ff9b5df18bc2d8`, main CI
  green (test+e2e-orders+e2e-dl+build). Deployed **v0.9.59** via `ha addons update`:
  `/health` 200 `0.9.59`, dashboard + `/znalosti` DOM confirm `v0.9.59` (0 console
  errors on the `/znalosti` page itself). Live functional verification via
  Playwright driving the REAL dashboard: created a synthetic DL catalog card
  (`TESTQA221001`) and a synthetic DL supplier (`9999999221001`) through the UI,
  confirmed each write in Postgres, edited both through the UI, confirmed the edits
  in Postgres, retired both through the UI (confirm dialog handled), then HARD-
  DELETEd both rows via psql so production carries zero synthetic test data —
  `dl_catalog_overrides`/`dl_supplier_overrides` both back to 0 rows. Worker log
  confirms `dl_shadow=True` still armed, 0 ERROR/Traceback lines in a 10-minute
  post-deploy window.

## 2026-08-08 — #224 + #225 (DL shadow window bugs found during eval)

- **#224** (DL shadow: Vision 400 `invalid_image_format` on 5 shadow runs, n8n processed
  them `ok`): root cause verified live — real supplier scans (from at least 2 suppliers)
  use `/Filter [/FlateDecode /DCTDecode]`, so `extract_embedded_jpegs()`'s raw SOI/EOI
  byte scan finds only coincidental marker matches inside the Flate-compressed stream,
  never a decodable image. `extract_attachment()` now filters candidates through
  `_is_real_image()` (PIL decode) before treating them as the vision payload; when none
  qualify (`_decodable_large_jpegs()`), `render_pdf_pages()` rasterizes the real PDF
  pages via `pdf2image`/poppler (same mechanism `app/extract.py`'s OCR fallback already
  uses) — a robust fallback that sidesteps whatever filter chain the source PDF used.
  `extract_embedded_jpegs()`/`is_scanned()` themselves are untouched (still mirror n8n's
  own heuristic). Commits: `13f29df` (RED), `21629d6` (GREEN, 100% coverage on
  `dl_extract.py`).
- **#225** (DL shadow: `items_skipped_no_match` on 7 shadow runs, n8n matched the same
  items): two distinct root causes found by comparing real n8n-shipped EDI (SFTP-read
  from ORION `archCodex`, read-only) against the python match trace:
  1. **4 of 5 wordings** ("ZÁVIN s nápl.makovou350g" and 2 siblings, "Buchta tvarohová
     nebalená 56g") — `_score_item()`'s word-overlap ratio counted 1-2 char Slovak
     filler words (the preposition "s") as real tokens; `w_eq()`'s cheap substring check
     then spuriously "matched" them against unrelated candidates' longer words,
     inflating wrong cards and diluting the real match enough to push it out of the
     top-15 shown to the model (rank #18-#32 in the real 491-row catalog). Fixed by
     `_MIN_SCORABLE_WORD_LEN = 3` filtering both word lists before `w_eq`. Verified live
     against the real catalog: all 4 now rank first or within the top 15 (was outside
     it). Commits: `c94b844` (RED), `a6c9178` (GREEN).
  2. **1 of 5** ("Žemľa špeciál 110g/špec.") — the correct card WAS already in the
     top-15 (rank #2), but the model returned `NO_MATCH` at confidence 0.38: the ordered
     weight (110g) is exactly `WEIGHT_TOLERANCE` (10%) off the card's own (100g), and
     `dl_match_item.md`'s prompt only said "significantly different weight = different
     product" with no number, so the model had no way to know the code's own gate would
     already accept this gap. Added the explicit 10% tolerance to the prompt. Commits:
     `d77a5f2` (RED), `2d01e9f` (GREEN).
  - **Corpus re-record**: both the candidate-scoring change and the prompt edit
    invalidate the DL eval corpus's cached matching-call answers (candidate list text /
    prompt text both feed the content-addressed cache key). Re-recorded the full 8-case
    corpus via `--live` on dev2 (`~/eval-corpus/email-extractor/dl/`, ~$1.20):
    8/8 passed, zero regressions, offline replay confirmed against the fresh cache.
- Both issues validated STILL REAL against live production data before implementation
  (`gh issue comment` on each, per `verify-issue-still-valid`) — 5 real `order_runs` rows
  for #224, 7 for #225, all shadow (never uploaded to ORION, never marked processed).
- Deep review (Explore subagent, adversarial pass on the real PR diff) found 2 more 🟡
  inside #224's own diff — `_decodable_large_jpegs()` trusted local extraction on just
  ONE valid+large candidate (a mixed-encoding multi-page scan could silently drop real
  pages), and `render_pdf_pages()`'s own "never raises" only wrapped
  `convert_from_bytes`, not the per-page JPEG-encode loop. Both fixed in `d2bdb78`
  (all-or-nothing decodability + whole-function try/except), plus 3 🔵 (misleading
  DPI-parity comment, missing page-cap truncation log, imprecise fallback log message).
  One 🟡 pointed OUTSIDE the diff (`app/orders/prompts/match_product.md`, the LIVE
  AI-orders engine's own sibling prompt, has the same missing weight-tolerance number)
  — filed as **#227** (needs-user-decision: different live engine, needs its own
  30-case corpus `--live` re-record).
- Shared PR: **226** — merged `37b4f2ffe6eb7447fa1279e5c4d3493148ac80e0`, main CI green
  (test+e2e-orders+e2e-dl+build, build pushed the GHCR image on this `main` push).
  Deployed **v0.9.60** via `ha addons update`: `/health` 200 `0.9.60`, dashboard DOM
  confirms `v0.9.60`, 0 console errors, worker log confirms `dl_shadow=True` still
  armed. Functional verification: deleted the 12 affected messages' stale shadow
  `order_runs` rows (all `shadow=true`, zero non-shadow runs for the same
  message_ids, confirmed before deleting) so the worker naturally re-picked and
  re-processed all 12 live (one message — created outside the 3-day shadow window —
  needed a temporary `delivery_notes_shadow_days` bump to 5, reverted to 3 + add-on
  restarted immediately after). Result: **11/12 fully `ok`, zero
  `items_skipped_no_match`, zero `invalid_image_format`** — including all 5 of #224's
  originally Vision-failing messages and all 7 of #225's originally partial ones. The
  1 remaining `partial` (id 395, Jackulík) is a genuinely NEW, unrelated item
  ("Šatôčka maková (plundra) 80g" on a second document that arrived in the same
  attachment, doc 68944) with no matching catalog card at all (confirmed via direct
  catalog query) — a legitimate missing-catalog-card case, not in #225's original
  5-wording scope, not a regression, left for the user to add via the #221 dashboard
  per the ticket's own "NIE ručné dopĺňanie kariet" instruction.

## 2026-08-08 — #227 (AI-orders match_product.md prompt had no weight-tolerance number)

- Same gap #225 fixed for the DL engine's `dl_match_item.md`, this time in the LIVE,
  non-shadow AI-orders engine's own `app/orders/prompts/match_product.md` — the code's
  `match.WEIGHT_TOLERANCE = 0.1` (10 %) is applied deterministically by
  `_weights_disagree()` at multiple `match.decide()` ladder rungs, but the prompt only
  said "a card with a different stated weight is a different product" with no number.
  Version bumped 0.9.60 → 0.9.61 (`7208318`). RED: `238b687`
  (`tests/test_orders_match.py::test_match_product_prompt_states_the_same_weight_tolerance_the_code_applies_fixes_227`).
  GREEN: `1d1ab3a` (one sentence added to `match_product.md`, mirroring #225's fix).
  Per `.claude/rules/orders-corpus.md`, the prompt edit invalidated the AI-orders
  35-case eval corpus's cached matching-call answers — re-recorded `--live` on dev2
  (`~/eval-corpus/email-extractor`), `--require-all` PASSED with zero new regressions
  (the only 5 failures are the pre-existing `#120` known-defect cases already excluded
  from the hard gate).
- PR #228 merged (`ccd2c96b93e3cbd7d0da498027eee6972f64695e`), main CI green (test,
  e2e-orders, e2e-dl, build all success). Deployed **v0.9.61** to the live HA add-on
  (`e0ac7775_email_extractor`) via `ha addons update`. Post-deploy: `/health` confirms
  `{"ok":true,"version":"0.9.61"}`; DOM read on both `/` and `/otazky` shows `v0.9.61`;
  `/otazky` renders live "Naposledy naučené" item-match entries (the exact AI-orders
  matching feature this prompt drives), 0 console errors on either page. Discord
  run-card delivered.
- #229 (DL Odoo messages routing to the wrong channel + unprocessed-DL sweep). Root
  cause: `delivery_notes_channel_id` defaulted to `0` in the `Config` dataclass,
  `Config.load()`'s `_get()` fallback, and `config.yaml`'s options block — `0` is
  treated as "unset" by both `dl_report._channel()` and `confirm.py::_channel_for()`,
  which fall back to `orders_channel_id` (152, sales desk) rather than silently drop an
  alert. The live add-on's `delivery_notes_channel_id` was never explicitly set after
  the DL Python-engine cutover (#217), so every DL review/success/announced-mismatch
  message landed in 152 instead of 243 ("AI dodacie listy", the warehouse) — confirmed
  live via the Odoo API (message ids 31140124/31140125/32595437, all `res_id=152`).
  Version bumped 0.9.61 → 0.9.62 (`a8743db`). RED: `6635484`
  (`tests/test_orders_dl_report.py`, 3 cases: DL routes to 243 by default, orders
  routing to `orders_channel_id` unaffected, explicit override still works). GREEN:
  `27a4ebc` (default 0 → 243 in all three places) + `04a3cc4` (clarifying comment from
  self-review: the `x or 243` idiom also upgrades an already-persisted explicit `0`,
  not just a genuinely absent key — same idiom every other int option in `Config.load()`
  already uses). `superpowers:requesting-code-review` subagent: 0 🔴 0 🟡, 1 🔵 cosmetic
  (stale docstring wording, not worth a diff line). PR #230 merged (`4b683e0`), main CI
  green (test, e2e-orders, e2e-dl, build). Deployed **v0.9.62**
  (`e0ac7775_email_extractor`), `/health` confirms `{"ok":true,"version":"0.9.62"}`, DOM
  on `/` and `/otazky` shows `v0.9.62`, 0 console errors on either page. Live options.json
  also explicitly set to `delivery_notes_channel_id=243` (defense in depth — the code fix
  alone already resolved the old explicit `0` to 243 via the `or 243` idiom, confirmed by
  reading `Config.load()`'s output inside the container before this step). Functional
  post-deploy verification: called the REAL `dl_report.post(cfg, html)` with the REAL
  live `Config.load()` (no channel override) — landed in `discuss.channel` `res_id=243`
  (Odoo message id 33218595), confirmed via the Odoo API. Part 2 (unprocessed DL):
  `SELECT count(*) FROM messages WHERE category='dodacie_listy' AND processed=false` = 0,
  now and across the whole history — the only 2 DL messages since the #217 cutover both
  reached a correct terminal state (one shipped to ORION, confirmed via `desadv_sent` +
  read-only SFTP `archCodex`; one correctly detected as a duplicate re-announcement and
  skipped, no second upload) — nothing needed reprocessing. Posted informational copies
  of both misrouted announced-mismatch messages (runs 406/407, LUNYS DL 0100239749) into
  243 (Odoo ids 33218596/33218597), clearly marked as a copy, no message re-run. Discord
  run-card delivered.
- #229 follow-up (reopened after the merge above, 2 more user-reported gaps, same
  branch/ticket): (1) `build_announced_mismatch` never stated whether the attached DL
  was actually processed — live complaint on run 406 ("preco nenapisalo do odoo ze
  dodaci list bol spracovany"). (2) `build_success` unconditionally carried the `/sklad`
  link even for a clean run; `build_review` had none at all even though review always
  needs one — backwards. Version bumped 0.9.62 → 0.9.63 (`3d2c0e0`). RED: `95487a5`
  (`tests/test_orders_dl_report_messages.py`, 12 cases). GREEN: `3625681` — every DL
  message now opens with a short per-document outcome line (new `_outcome_line`/
  `_OUTCOME_ICON`) before any warning; `build_success`'s headline states doc number +
  item count; the missing-doc warning reworded per the user's exact wording; new shared
  `report.link_line()` (orders' `build_summary` link markup extracted, byte-identical
  output) used by `build_review` (always when given), `build_success` (only when
  `unmatched_items` non-empty), `build_announced_mismatch` (only when a document's
  outcome needs it). `superpowers:requesting-code-review` subagent: 0 🔴, 2 🟡, 3 🔵.
  Both 🟡 fixed in `d136ba5` (same branch — a follow-up ticket for either would itself
  be a follow-up-of-a-follow-up, correctly refused by the filing gate): (a) wired
  `build_success`'s headline to actually read the `_OUTCOME_ICON` dict instead of
  hardcoded literals its own comment falsely claimed were shared; (b) `desadv_edi.
  build()`'s own `partial`/`no_match` computation excludes a zero-quantity item even
  when unmatched, so `outcome` alone under-reports "needs a link" — `dl_worker.py` now
  carries the exact `unmatched_items` list through on every live outcome dict and
  `_outcome_needs_link` reads that directly. PR #232 merged (`0e2d755`), main CI green.
  Deployed **v0.9.63**, `/health` confirms `{"ok":true,"version":"0.9.63"}`, DOM shows
  `v0.9.63`, 0 console errors. Functional post-deploy verification: called the REAL
  `dl_report.build_success`/`build_review` + `dl_report.post` with the REAL live
  `Config.load()` — success message (id 33218660) landed in channel 243 with headline
  "Dodací list TEST-0001 spracovaný a nahratý do ORIONu (1 položiek)" and NO link
  (unmatched_items empty, even though a link was passed in); review message (id
  33218661) landed with the link present — both confirmed via the Odoo API reading the
  actual delivered `body`. Discord run-card delivered.

## 2026-08-10 — #231 (separate DL-only nástenka, split from AI-orders)

- User's ask: skladníčka (vlákno "AI dodacie listy", ch.243) has to see LEN dodacie
  listy, predajkyňa LEN objednávky — previously BOTH kinds (`item`/`customer`/`mail`/
  `date`/`line` + `dl_item`/`dl_supplier`) rendered mixed on ONE unauthenticated page
  (`/otazky`, reached via the ONE `/sklad/<key>` link), and `dl_worker.py`'s DL Odoo
  review messages linked to that same mixed board. Design comment posted BEFORE code
  (root cause + chosen server-side-role-scoping approach + rejected query-param
  alternative, `gh issue comment 231`). Version bumped 0.9.63 → 0.9.64 (`e505e34`).
  New independent link `/sklad-dl/<key>` (`linkutil.dl_key`/`dl_url`, own HMAC context
  string `"sklad-dl-link-v1"`) → `/otazky-dl`. `teach.open_questions`/`recently_taught`
  gained an optional `kinds` filter (default `None` = unchanged); `httpapi.py`'s
  `/api/orders/questions`/`/api/orders/taught` + the answer/undo dispatch endpoints
  apply a NEW `_role_kinds(session.get("role"))` — a real server-side boundary (a role
  can neither see nor answer/undo the other agenda's kind, even by guessing a question
  id), not a display filter. `dl_worker.py`'s two `link=` sites (in `_process_document`/
  `_process_message`) now call `report.dl_sklad_link(cfg)` instead of the orders-only
  `report.sklad_link(cfg)` — #229's "link only when actionable" rule unchanged, only
  the target moved. New `/api/orders/dl/stats` endpoint (aggregate DL run/duplicate/
  mismatch counts, `reliability.dl_provenance_stats_for_day`) feeds a "stavy" strip on
  the DL board. `_ASK_HTML_TEMPLATE` refactor: `ASK_HTML`/`ASK_DL_HTML` both built from
  ONE literal via `.replace()`, avoiding two hand-maintained ~150-line JS copies. Admin
  dashboard shows both links now. Commit `dc831fd`.
  `superpowers:requesting-code-review` subagent caught a real Critical bug before
  merge: `_role_kinds()` checked only `session["role"]`, never `session["auth"]` — a
  real admin login who merely clicked either nástenka link shown on their OWN
  dashboard (both rendered as clickable `target="_blank"` tags) would silently start
  seeing a role-filtered view (`session["auth"]`/`session["role"]` are independent
  session keys sharing one cookie jar). Fixed by checking `auth` first, exactly like
  `_gate()` already does; regression test `test_a_stale_role_cookie_never_restricts_a_
  real_admin_login`. Also added a module-level assertion that `ORDERS_KINDS ∪
  DL_KINDS == teach.KINDS`, so a forgotten future kind fails loudly at import instead
  of silently locking both links out of it. Commit `d9a4863`. PR #233 merged
  (`b6de59b`), main CI green (test, e2e-orders, e2e-dl, build). Deployed **v0.9.64**
  to the live add-on (`e0ac7775_email_extractor`); `/health` confirms
  `{"ok":true,"version":"0.9.64"}`. Functional post-deploy verification with Playwright
  against a CLEAN (cookie-cleared) unauthenticated session for each link: `/sklad-dl/
  <key>` → `/otazky-dl` shows ONLY the one real live open `dl_supplier` question
  ("Ktorý dodávateľ? — objednavky@feast.sk"), the "stavy" strip ("dnes: 4 spracovaných,
  2 duplicít, 1 nezhôd · včera: 1 spracovaných"), and `v0.9.64` — zero `mail`/`item`
  questions leak through; `/sklad/<key>` → `/otazky` shows the 3 real open `mail`
  questions + 12 taught mappings, zero `dl_supplier` leak, `v0.9.64`. Admin dashboard's
  "Otázky skladu" tab shows both links side by side with the correct URLs. Zero
  browser console errors on either page's clean load. (First verification pass, with a
  browser context that still carried a STALE admin `auth` cookie from an earlier
  session, correctly showed the DL board unrestricted — that's the just-fixed `auth`-
  precedence behavior working as designed, not a live bug; re-verified clean after
  clearing cookies.) Deliberately did NOT click an answer on the live `dl_supplier`
  question (id 26, real production data, real supplier assignment) — the
  click-to-answer wiring is already proven by local Playwright e2e tests against a
  fixture DB. Discord run-card delivered.

## #236 — Zaseknuté dodacie listy: FEAST, HK LOAN, TLS Great 15/20 kg (2026-08-11)

LIVE OPS ticket, no code commits. All data changes via the live dashboard's own
`/api/znalosti/dl-suppliers` / `/api/znalosti/dl-products` (session auth), verified
via SSH+psql on the HA box + read-only DuckDB queries against Codex ERP on dev2.

- **FEAST s.r.o.** — added as `dl_supplier` (`ean_edi=2000000000866`, cross-verified
  against `raw.firma.AEDIEAN` in Codex, independent of the warehouse's own screenshot;
  `emails=[objednavky@feast.sk, obejdnavky@feast.sk]`, `city=Nitra`). Its item
  ("Soľ jedlá kamenná jódovaná 0,7-0,16 mm") separately failed `dl_match.py`'s R75
  lexical tripwire even AFTER the warehouse (sklad) answered the board question live
  mid-session — the memory-rescue rung (R73) only fires below `GATE_SURE`, so a
  human-taught mapping never overrides an already-"sure"-but-lexically-orphaned model
  guess. Fixed with a DATA-only alias (`doplnok="Soľ jedlá kamenná jódovaná"` on the
  matched card, gtin `4003885181808`) — `card_words` in the lexical guard includes
  `doplnok`, so this closes the gap without touching `dl_match.py`. DL 20263245 now
  ships (`DESADV_000866_20263245_...txt`, confirmed on ORION `in_DL`).
- **TLS Logistics / Forbak s.r.o.** — the ticket's premise was slightly off: "TLS
  Logistics" isn't an unonboarded supplier, it's the 3PL warehouse operator that
  prints Forbak s.r.o.'s (`ean_edi=2000000000549`) pick slips — Forbak was ALREADY a
  supplier, just under a name with zero word-overlap with "TLS Logistics, s.r.o." (the
  document's own header). Renamed the supplier record to `"Forbak s. r. o. (TLS
  Logistics, s.r.o.)"` (supplier has no separate alias field — folded into `name`) so
  R60's word-overlap scoring finds it. Separately, "Great 15 kg" vs the existing card
  "Great 20 kg" (Codex `NEANKOD 3605`, ONE card for both bag sizes) tripped
  `dl_match.py`'s weight-conflict guard (±10% tolerance, 20/15=1.33× over). Renamed the
  card to weight-neutral `"Great"` (kept `doplnok="Great-náhrada fresca"`, left `mass`
  blank so per-line kg conversion falls back to each delivery's own stated weight via
  `_extract_mass`, verified correct for both 15kg and 20kg runs). 3 stuck docs
  reprocessed: 07-21 (Great 20kg) → shipped, 08-05 (Great 15kg) → shipped, 08-06 (exact
  duplicate of 08-05, same `doc_number`) → correctly caught as `duplicate`, NOT
  double-uploaded (`desadv_sent` ledger + ORION `in_DL` both confirm 2 files, not 3).
- **HK LOAN** — genuinely unresolvable EAN-EDI without the user: checked Codex
  `raw.firma`, `dl_supplier_snapshot`/`overrides`, `order_questions` — zero record
  anywhere. Asked the user (`❓`), ticket stays open + `needs-answer` labelled.
- Two stray `order_questions` (id 26 FEAST-supplier, id 32 TLS/tlaciaren-supplier)
  left `open` by the direct data fixes (resolved outside the normal answer flow) were
  closed via a narrow, scoped `UPDATE order_questions SET status='answered', ...`
  (same class of repair as #147's playbook precedent) — no teaching-table side effect,
  just stops them showing as stale pending items on the dashboard.
- Safety: every reprocess checked `desadv_sent` (empty for all 4 doc numbers before
  reprocessing) AND read-only SFTP on ORION (`in`/`in_DL`/`archCodex`/`unconfirmed`,
  with and without `Z-`) before touching anything — confirmed nothing had shipped.

## #245 — urgent: EKVIA DL stuck in ORION, CODEX import rejects it (2026-08-11)

RED `723bdb0` / GREEN `fd99c8c` on `dev` (bundled behind PR #244, blocked on #235's own
unfixed critical review finding — not touched, not mine to fix). Root cause: catalog GTIN
`18585037201518` ("Margarín stolný - Favorit") is a valid GTIN-14 (verified via the GS1
check-digit algorithm) that overflows `desadv_edi.py`'s fixed 13-char DESADV LIN GTIN field
— `_pad()` silently truncated it to `1858503720151`, which matches no real ORION stock card,
so CODEX rejected the whole document ("no stock card with EAN 1858503720151") and it sat
stuck in `in_DL` 4 days. Fixed at the matching layer: `dl_match.decide_item()`'s new
`_gtin_edi_overflow()` guard (both the fresh-LLM-match and R73 memory-rescue paths) treats
an overflowing card the same as "no card" — the item becomes a real `unmatched` Decision,
raising the existing warehouse question instead of silently corrupting the file.
`desadv_edi.generate()` itself is untouched (byte-parity fixture still passes; only the `13`
literal became a named `GTIN_FIELD_WIDTH` constant both modules share). Tests:
`test_gtin_longer_than_the_edi_field_is_unmatched_not_silently_truncated`,
`test_memory_rescue_never_resurrects_an_edi_overflowing_gtin`,
`test_gtin_exactly_at_the_edi_field_width_still_ships` (sanity, off-by-one).

Immediate unblock (independent of the code deploy, done via read-only-normally SFTP,
owner-authorized for this ONE file): verified `Z-DESADV_000264_3412606458_20260807_072424371.txt`
was present in `in_DL`, absent from `in\archCodex`/`in\unconfirmed` (never imported) —
saved its bytes to scratchpad, deleted it, surgically edited its own bytes (dropped the
margarine LIN, renumbered the remaining 3, byte-verified the SFTP round-trip) and
re-uploaded under the same filename. Reconciled `desadv_sent` (empty for this doc — the
ledger only started writing 2026-08-09, predates this document) with a fresh row so
`confirm.py`'s sweep picks up the real import status tomorrow morning. Margarín 30kg was
deliberately excluded (cannot be represented in a 13-char field at all) — told the warehouse
in Odoo ch.243 to enter it manually in CODEX for this one delivery. 10 other catalog cards
share the same 14-digit shape, none yet delivered — filed #246 (`needs-user-decision`,
CODEX-side question: does an alternate 13-char code exist for these?).

Playbook: `.claude/rules/orders-corpus.md` gained 3 entries (GTIN-14 overflow + guard
placement, `desadv_sent`'s 2026-08-09 start date, the surgical fixed-width-file-edit
technique for manual ORION corrections with no recoverable `matched_items` input).

## 2026-08-11 — #235 review-findings closeout + #245 shipped (PR #244, merge `4c11a66`)

PR #244 was already CI-green (`255c56b`/`de3cbe3` #235 feature, `723bdb0`/`fd99c8c` #245 —
see above) but blocked on a `requesting-code-review` pass (comment 5253714038) whose
3 findings never got fixed before a prior worker died on a rate limit. Closed in two
RED→GREEN rounds, both on `dev`, same PR:

**Round 1** — `d8ae10d` [red] / `eefcb23` [green]: (1) 🔴 restored the 5 dead-but-still-
live-in-`/data/options.json` config keys (`catalog_sheet_id`/`catalog_gid`/`customer_gid`/
`catalog_refresh_minutes`/`dl_catalog_gid`) to `config.yaml` + `Config` — the first draft
had removed them entirely, directly against this repo's own recorded #129 precedent
(`.claude/rules/deploy.md`); verified live post-deploy that the Supervisor's options
validation is genuinely clean with all 5 still declared. (2) 🟡 EAN-collision check on
`_api_orders_answer_new_dl_supplier`, mirroring the customer path's #234 fix. (3) 🟡
`WHERE status='open'` race guard on `_api_orders_answer_generic`'s UPDATE, mirroring
`teach.answer_customer`'s own #234 hardening — proven with two real threads + real HTTP
requests through the Flask test client.

An independent fresh-context review of round 1 (0 🔴 0 🟡, 2 optional 🔵) triggered
**round 2** — `5f8a6bc` [red] / `419441d` [green]: implementing the 🔵 "customer path has
a one-click 409-reclaim button, DL supplier doesn't" finding surfaced a REAL, pre-existing,
previously-undiscovered bug (predates this whole fix pass, shipped with #235's original
commits): `dlSupplierSearchBox`/`dlItemSearchBox`'s live search over the FULL current DL
list posts a plain `{"choice": ...}`, but `_validate_dl_supplier`/`_validate_dl_item` only
ever accept a value already in the question's FROZEN (often empty) `candidates` — so
clicking ANY search result, or the new reclaim button, was silently rejected 400 "nebolo
ponúknuté". Fixed by legitimizing a search-picked value against the real current DL
supplier/catalog list before validating (mirrors what `_api_orders_answer_customer`
already does for its own search box) — verified live: a plain-`{"choice"}` POST against a
real supplier went from 400→200 after the fix. The OTHER 🔵 (a residual TOCTOU in the
EAN-collision pre-check, matching `upsert_customer`'s own identical already-accepted
limitation) is documented in a code comment, not changed — would need widening the
advisory lock symmetrically in both functions, out of proportion to a manual, low-
concurrency warehouse form.

**Live verify (v0.9.65→0.9.67):** `ha addons update`, container restarted `15:36:08Z`,
Supervisor validation clean (proves finding 1 above was real, not theoretical). Playwright
against `/sklad-dl/<key>` (cookies cleared first): 0 console errors/warnings throughout;
role boundary (`SKLAD_DL_ROLE` ↔ `SKLAD_ROLE`) still holds both directions live; `e6bfb64`
(#234's own race fixes) confirmed present in the RUNNING container's actual source
(`pg_advisory_xact_lock` in `snapshot.py`, the `status='open'` guard in `teach.py`), not
inferred from the version string.

**HK LOAN acceptance case (#236's own "first real test"):** live reprocess of her most
recent message (id 6389, verified safe — no ORION upload, no `edi_file`) revealed she
can never reach the new dl_supplier board question automatically: ALL 13 of her historical
attachments are tiny (150×76px/2.4KB) JPEGs that OpenAI Vision rejects outright before the
pipeline ever reaches supplier-lookup — filed **#247** (separate, deeper scope: `extract.py`
never applies its own documented "skip decorative/tiny images" rule on the DL path). To
still give the owner a genuine, live, fillable HK LOAN question today, manually raised the
SAME question the pipeline would (`teach.ask_dl_supplier`, real sender, real message
thread, no candidates) — verified live via Playwright: card renders, "➕ Nový dodávateľ"
form opens, EAN/name fields fillable, save button live. Not synthetic test data — a real
open question, honestly raised.

**Closed:** #235 (auto-closed by PR's `Closes #235`), #245 (closed explicitly — its `#245`
mention in the PR body had no trigger keyword next to it, so GitHub never auto-closed it).
**Left open:** #236 (FEAST/HK LOAN/TLS still need real EAN data, not a technical fix —
updated with current honest state), #247 (new, the vision-processing bug).
Odoo ch.243: retracted the dead 2026-07-24 "NÁVOD point 5" Google-Sheet instruction
(stopped being read 2026-08-08, nobody told her until now), pointed her at the same
`/sklad-dl/<key>` link she already gets on every "Rieš na nástenke" ping, asked for HK
LOAN's EAN-EDI right there.

Playbook: none new this round — the 3 fix findings and the search-legitimization bug are
narrow to `httpapi.py`/`config.py`, already fully explained in their own commit messages
and this log entry; no new reusable *procedure* emerged beyond what `orders-corpus.md`
already documents for this file's shared patterns.

## 2026-08-11 — #240 (PR #249) — resumed after a session-limit interruption

- **#240** (answering a DL board `dl_item`/`dl_supplier` question must finish its
  document, not leave it hanging forever): first round already committed by an earlier
  worker before this session (`0bdfa30` bump, `9af8c51` RED, `39bec3e` GREEN, `154bbe4`
  docs) — `dl_worker.release_for_question` re-runs the message via `_run_and_finish`
  once every sibling `dl_item`/`dl_supplier` question is answered; `_apply_dl_item`/
  `_apply_dl_supplier` now call it.
- **Second round, this session:** applied an independent deep-review pass (fresh-context
  `general-purpose` subagent) of the first round's own diff PLUS a prior worker's
  uncommitted follow-up fixes (advisory lock, `catalog_gtins` filter, blank-choice
  guard). Review found 2 🟡 + 2 🔵, all fixed:
  - `_run_and_finish`'s hard-failure (`except Exception`) branch had the SAME
    `processed` strand-forever bug the `_RetryLater` branch was fixed for, but the fix
    wasn't extended — mirrored it. RED verified manually (temporarily reverted the
    fix, confirmed `test_release_for_question_hard_failure_re_arms_processed_for_
    reclaim` fails) before committing GREEN.
  - `_apply_dl_item`'s blank-choice guard had zero direct test coverage (unlike its
    `_apply_dl_supplier` twin) — added `test_dl_item_kind_apply_with_blank_choice_
    teaches_nothing`.
  - The new `pg_advisory_xact_lock` had no test proving it genuinely SERIALIZES two
    concurrent callers (only sequential-call tests existed) — added
    `test_release_for_question_advisory_lock_serializes_two_genuinely_concurrent_racers`
    (two real threads, each its own Postgres connection, deterministic via recorded
    call-span timestamps rather than a sleep-based race).
  - The lock's own docstring claimed the second racer "correctly no-ops" — false; it
    still runs a full second reprocess (fresh LLM call, new `order_runs` row), the
    lock only serializes the two attempts and `desadv.claim_send_or_identify` is the
    real duplicate-upload guard. Docstring corrected.
  - Commits: `3ee3c8d` [red] (all 4 findings' tests — verified failing against
    `154bbe4` with the app-side changes reverted), `fbcb4b0` [green] (the fixes).
    Review verdict posted as its own `gh issue comment` on #240 before merge.
- CI green on both `dev` (run `31524009119`/`31524005933`) and `main` (run
  `31524561573`, incl. `build` pushing the GHCR image) — 4 jobs each (`test`,
  `e2e-orders`, `e2e-dl`, `build`). PR #249 auto-merged (`94bd5a2`), #240 auto-closed.
  Deployed to the live add-on (`ha addons update`, v0.9.67→v0.9.68), verified: `/health`
  200, DOM shows `v0.9.68` on both the admin dashboard and `/otazky-dl`, and the RUNNING
  container's own source greped to confirm the advisory lock / `catalog_gtins` / blank-
  choice / hard-failure-re-arm code is actually the code that's live (a version bump
  alone is not proof).
- **Deliberately left UNVERIFIED, by design:** did not answer either of the two real
  live open board questions (id 30 `dl_item` "Soas W 17 ExtraFlex S1 25kg C2TES1" for
  Stavebniny KLEŠČ, id 35 `dl_supplier` `gnip@hkloan.eu`) to watch a real production
  document finish — per the hard safety rule against acting on the warehouse's behalf
  and the risk of an unintended real ORION upload. The mechanism is proven instead by
  the full local test suite (1402 tests green, including the exact `release_for_
  question`→`_run_and_finish` code path with fake-but-realistic upload/post) plus the
  live container-source grep above.

Playbook: none new — this round's findings (a mirrored re-arm fix, two missing unit
tests, a corrected docstring) are narrow to `dl_worker.py`/`teach.py` and already fully
explained in their own commit messages, this log entry, and the review comment on
#240; no new reusable cross-file *procedure* emerged.

## 2026-08-11 — #238 (PR #252) — silent multi-DL loss + audit of all "ok" DLs

- Root cause: the OLD (retired, pre-Python-migration) n8n "Dodacie Listy EDI" workflow
  fetched only the FIRST attachment (`LIMIT 1`) and used a single-document extraction
  schema — a mail with 2+ delivery notes silently dropped everything past the first,
  while still logging its own "ok" rollup event. Confirmed on live production message
  6202 (2026-08-06): its only `order_runs` row is a SHADOW run from 2026-08-08 (two
  days after the message was actually processed by the old workflow), which correctly
  found and processed BOTH of its documents and reported "partial" — proving the
  Python engine (`dl_extract.extract_email`/`DL_SCHEMA`, #204/#205) already fixes the
  STRUCTURAL loss for future mails.
- Two residual gaps closed in `app/orders/dl_worker.py`'s `_process_message`, both
  additive/detection-only (zero change to claim/upload/retry/dedup logic):
  1. A universal, supplier-format-independent completeness check — a successfully-read
     attachment that contributed zero documents (a plain LLM/vision omission, no
     exception) now gets its own `review` entry, marked `synthetic` so it demotes
     `proc_status` without being double-counted as a real processed document. Skips
     `extract.py`'s own `method='skipped'` decorative attachments (logos/signatures) to
     avoid false alarms (mirrors the #133/#151 false-alarm class).
  2. The existing Lunys-only announced-vs-attached mismatch now ALSO feeds
     `_aggregate_status` (kept, still supplier-specific but real) — `proc_status` was
     previously staying "ok" even when the mismatch alert fired.
- Deep-review round (fresh-context Opus subagent) caught a CRITICAL regression before
  merge: the DL corpus's `lunys_announced_not_attached` case expected `status: "ok"`,
  which the fix correctly changes to `"partial"` — updated on dev2
  (`~/eval-corpus/email-extractor/dl/manifest.json`, deliberately outside git),
  re-verified `--require-all` 8/8 exit 0, `baseline.json` unchanged (pass/fail state
  didn't move). Also fixed: `_summary_outcome`/rollup-detail/`dl_evaluate.score()` all
  now exclude `synthetic` entries from document counts; deduped `missing`; a Slovak
  gender-agreement bug; and `httpapi.py`'s dashboard `state=review`
  filter/count/badge now also matches `proc_status='partial'` (previously fell through
  to a neutral grey badge and never appeared in the "⚠ review" filtered view — would
  have quietly reintroduced the exact "no one notices" failure this ticket exists to
  fix).
- Audit (read-only, production Postgres + read-only ORION SFTP) of all 115 `ok` DL
  messages: 5 had ≥2 real attachments (structurally suspect). 3 documents confirmed
  genuinely lost (never in ORION at all — `P26034244`, `P26036049`, `P26035800`, all
  MPC); a 4th (`611741`, Jackulík) found via a bonus discovery on a `proc_status='skip'`
  message (5900, a false "duplicate" skip — spec's own documented W4 registry-collision
  class, pre-existing n8n-era only). 2 other apparently-missing documents (610461,
  68944×2) turned out to have been delivered successfully via unrelated LATER messages —
  reassuring but confirms the old pipeline relied on luck (a supplier resend), not
  correct first-pass handling. 110 single-attachment `ok` messages got an automated
  text-heuristic pass (63/110 covered by 2 known suppliers' wording, 0 multi-doc
  markers found; 47/110 honestly left unhand-verified — different doc formats, out of
  session scope). Filed as #251 (remediation of the 4 confirmed losses, same safe
  procedure as #236/#241 — never re-run a message that already uploaded).
- Live post-deploy verification (v0.9.69) caught a REAL production Lunys mail (#6784,
  23:07) mid-verification: subject announced 2 DL numbers, only 1 attached — the
  dashboard correctly showed `1 dokument(y): 1x partial` (not the pre-fix `1x ok`), the
  `announced_mismatch` timeline event correctly named the missing doc `0100242689`, and
  the "⚠ review" filter correctly included it with a `b-review`-styled badge reading
  "partial" — full end-to-end confirmation on a genuine live document, not just a test.
- CI: dev PR #252 all 4 jobs green (`test`/`e2e-orders`/`e2e-dl`/`build`) on both the PR
  run and the push run; main run `31536299463` all 4 green. Merged `05256c4`. Deployed
  v0.9.68→v0.9.69, verified `/health`, DOM `v0.9.69`, and a grep of the RUNNING
  container's own source for `synthetic`/`_flag_attachment`/the httpapi `proc_status IN`
  clause to confirm the new code is actually live, not just the version string.

Playbook: `.claude/rules/orders-corpus.md` already documents the corpus-update
obligation for a production-incident fix; no new reusable procedure emerged beyond
what commit messages / this entry / the #238 issue comments already capture in full.

## #251 — dotiahnutie 4 stratených dodacích listov z auditu #238 (2026-08-12)

LIVE OPS ticket, no code commits (same shape as #236). Re-verified live (SFTP) that all
4 documents were still absent from ORION before touching anything, then shipped them
ONE AT A TIME via `dl_worker._process_document()` called directly (never `_process_message`/
`_claim` — see the new `.claude/rules/n8n-workflow-edits.md` section this ticket added,
"Re-shipping ONE missing document out of a multi-document DL message via the Python
engine"), shadow preview first, live commit second, ORION-verify after every commit:

- **P26034244** (MPC, msg 823) → `Z-DESADV_000276_P26034244_20260627_000429615.txt` in
  `in_DL`. Sibling P26034036 (already in `archCodex`) untouched.
- **P26036049** + **P26035800** (MPC, msg 2191, same mail carried BOTH) → shipped
  sequentially, each its own shadow+live+verify cycle. P26035800's first shadow preview
  came back `partial` (1 item unmatched — LLM non-determinism); a second preview and the
  eventual live run both matched all 7 items cleanly (`llm_sure`, conf 0.96, unanimous
  recent history). Sibling P26036281 untouched.
- **611741** (Pekáreň Jackulík, msg 5900) → 14/14 items matched, `Z-DESADV_000825_611741_
  20260805_001823982.txt` in `in_DL`. Clarified vs the ticket's own hypothesis: msg 5900's
  FIRST attachment (611494) was a GENUINE duplicate (already shipped a day earlier by msg
  5557) — old n8n correctly flagged it, but its LIMIT-1 bug then never even read the
  second attachment (611741). Not a NEW W4 registry-collision class; the current engine's
  `desadv_sent` already scopes by `(supplier_ean, doc_number)` since #200, so this class
  is structurally excluded today. No code change needed or made.
- Final checklist: 4/4 files confirmed in ORION `in_DL`, 3/3 siblings confirmed untouched
  in `archCodex`, `desadv_sent` has exactly 4 new confirmed rows (correct supplier_ean/
  message_id each), `messages.processed/attempts/proc_status` for all 3 source messages
  completely unchanged (deliberate — this was a per-document backfill, not a message
  reprocess).
- **Side finding, filed as #253** (own ticket, not this one's scope): `erp.slovnormal.sk`
  Odoo's API is entirely down (every `POST /json/2/*` → 405 via nginx, every `GET` → 503)
  — confirmed independently of the add-on via direct `curl`, confirmed it's instance-wide
  not endpoint-specific. Blocks BOTH this app's own best-effort Odoo posts (R97 already
  swallows the failure, so processing itself is unaffected) AND this ticket's own
  warehouse-notification requirement — retried periodically through the rest of this
  session, still down at close; will deliver the warehouse summary the moment Odoo
  recovers.

Playbook: added a new `.claude/rules/n8n-workflow-edits.md` section — the safe
shadow-preview + direct-`_process_document` technique for re-shipping ONE missing
document out of an old, multi-document, pre-ledger DL message without risking a
duplicate upload of its already-shipped sibling(s), plus the LLM-matching-is-
non-deterministic caveat (re-preview once before trusting a surprising `partial`).

## #239 — päť neviditeľných tried zlyhaní dodacích listov (2026-08-12)

Bump 0.9.69→0.9.70. Investigation-first: naživo overené (prod Postgres, ORION SFTP
read-only, n8n MCP), že každá z 5 tried má inú skutočnú príčinu než ticket predpokladal:

- **Trieda 1** (vyčerpané pokusy) — už riešená externe: aktívny n8n workflow "Stuck
  message watchdog" (`EPe5WWMVZR0lzUld`, od 2026-07-10) alertuje `attempts>=3` do
  kanála 243 pre `category='dodacie_listy'`. Chýbala len dashboard viditeľnosť.
- **Trieda 2** (zlyhanie uploadu) — reálna medzera, opravená: `dl_worker.py`'s upload-
  except blok teraz volá `_check_retry` (rovnaký transient-retry mechanizmus ako pre
  LLM zlyhania — claim je uvoľnený PRED kontrolou, žiadne riziko duplicity), a
  netransientné/vyčerpané zlyhanie sa zaraďuje do novej trvanlivej `pending_alerts`
  schránky (`app/orders/dl_alerts.py`) namiesto jednorazového best-effort postu.
- **Trieda 3** (zaradené, nikdy nespracované) — reálna medzera, opravená: nová
  `dl_worker.stuck_classified_sweep()` — `category='dodacie_listy' AND processed=false
  AND created_at < now()-30min AND NOT EXISTS (order_runs row)`, dedup cez
  `dl_alerts.already_pending` (VEDOME NIE `messages.alerted_stuck` — ten flag patrí
  n8n watchdogu).
- **Triedy 4 a 5** (odmietnutie CODEXu / viacdňové čakanie) — `confirm.py`'s existujúci
  grouped-incident mechanizmus (od #203) už funguje správne pre KAŽDÝ desadv upload od
  2026-08-09. Historický EKVIA incident (#245) predchádza tomuto dátumu — štrukturálne
  nemohol byť zachytený, nie živá chyba. Zostávajúce architektonické obmedzenie
  (rovnaký-deň detekcia odmietnutia, keďže CODEX necháva odmietnutý súbor ležať v
  `in_DL` namiesto presunu do `unconfirmed`) založené ako samostatný #255
  (`needs-user-decision` — vyžaduje vlastníkovu voľbu ohľadom miery falošných
  poplachov, nie technické riešenie).

Dashboard/digest doplnok pre všetkých 5 tried: `reliability.dl_current_health()` — tri
current-state metriky (`quarantined`/`pending_alerts`/`open_import_incidents`,
deliberately NIE viazané na deň) zlúčené do `dl_provenance_stats_for_day`, viditeľné cez
existujúci `/api/orders/dl/stats` (ktorý `/sklad-dl` nástenka už pravidelne pollovala) aj
v dennom Odoo digest-e (`report.build_daily_digest`'s DL sekcia teraz spúšťaná aj len
jedným z týchto troch gauge-ov, nie len `runs`/`duplicates`/`mismatch`).

Deep review (samostatný fresh-context `general-purpose` subagent, celý diff
`72e3243..HEAD`): 0 🔴, 3 🟡, 4 🔵. Kritická claim/release/retry cesta overená bezpečná
(žiadne riziko duplicitného ORION uploadu). Všetkých 7 nálezov opravených v tej istej
vetve (commit `66a3f66`) — channel_id=0 guard na upload-failure enqueue, detekčný
časový údaj v stuck-classified alertoch (odlišný od `created_at`), dokumentovaný
permanent-dedup kompromis, `dl_worker.MAX_ATTEMPTS` ako jediný zdroj pravdy namiesto
trojnásobného literálu "5", `include_current_health=False` proti zbytočnému
dvojitému počítaniu current-state gauge-ov, nový regression test na cross-group
izoláciu vo `flush_pending`.

CI: PR #256, všetky 4 joby (`test`/`e2e-orders`/`e2e-dl`/`build`) zelené na push aj PR
runoch. Merged `dbbcab6`. Deploy v0.9.69→v0.9.70, overené `/health`, DOM `v0.9.70`, grep
bežiaceho kontajnera potvrdil nový kód (`pending_alerts`/`stuck_classified_sweep`/
`quarantine_threshold`/`already_pending`), a funkčné overenie: vložený syntetický
`pending_alerts` testovací riadok, `/sklad-dl` nástenka LIVE zobrazila "1 čaká na
odoslanie", `/api/orders/dl/stats` potvrdil `pending_alerts:1, quarantine_threshold:5`
v `today` a SPRÁVNE vynechal tieto polia v `yesterday` (potvrdzuje aj
`include_current_health=False` opravu naživo), testovací riadok následne vymazaný,
stav overený späť na 0.

Filed as #255: rovnaký-deň odmietnutie importu sa zistí až nasledujúce ráno —
architektonické obmedzenie vyžadujúce vlastníkovo rozhodnutie o miere falošných
poplachov.

Playbook: `.claude/rules/n8n-workflow-edits.md` už dokumentuje `confirm.py`'s
carryover model — žiadna nová sekcia potrebná; tento ticket len POTVRDZUJE, že
mechanizmus funguje správne od 2026-08-09 a prečo staršie incidenty naň neplatia.

## 2026-08-12 — #247 + #239 (v0.9.71)

**#247 — DL pipeline died on a decorative attachment.** Salvaged from a worker that hit the
weekly account limit before pushing: root cause live-verified (all 13 stored attachments from
the reporting supplier are the identical 2472-byte 150x76px signature logo, already classified
 by `app/extract.py` at ingest), fix filters those out before extraction so
they never reach the vision fallback. RED 2671eaa -> GREEN 7baeb9d.

**#239 reopened — the auto-retry it added could duplicate a delivery in ORION.** Independent
verification of PR #256 refuted its own comment ("the claim was released so a retry can safely
re-upload without a duplicate"): releasing the claim is what removes the protection. Removed the
single `_check_retry` call in the upload except block; the durable alert stays. RED a68612e ->
GREEN 9b59edd, with the obsolete retry-asserting test dropped in its own commit 802bd00.

Five further verification findings (per-file alert flood shape, reprocess-button dedup hole,
digest posting to the orders channel instead of the DL channel, unbounded `pending_alerts`,
class 4 delegated to its own ticket) stay open on #239 with evidence.

Both changes were written from the MAIN session, deliberately and logged: no subagent capacity
was available (weekly account limit, resets Aug 17) and the duplicate-upload hazard was live in
production ahead of the warehouse morning import.

## 2026-08-12 — #239 reopened, findings 1-7 (v0.9.73)

Six independent-review findings against the merged #239 PR (the critical auto-retry-duplicate-
upload bug was already fixed and deployed as v0.9.71/72, unaffected here), plus the playbook
lesson (finding 7).

**Finding 1 (flood shape):** `dl_alerts.flush_pending()` gets `quiet_seconds` (default 0,
unchanged for existing callers) — a `(channel_id, kind)` group only posts once its newest row is
older than the window, so a burst of same-kind alerts always lands as ONE grouped post regardless
of loop timing. `worker.run_forever` passes `FLUSH_QUIET_SECONDS=30`. RED `979064b` → GREEN
`34a3caa`.

**Finding 2 (wrong channel):** `report.build_daily_digest()` drops its `dl_stats` param (orders-
only again); new `report.build_dl_digest()` is a standalone DL-only message (`""` on a quiet day).
`reliability.maybe_post_daily_digest()` posts both separately — DL routed to
`delivery_notes_channel_id`, skipped (never a fallback to 152) when unset. RED `0d857ff` → GREEN
`ed91a5d`.

**Finding 3 (dedup defeated by reprocess):** `dl_alerts.already_pending()` changes from a
PERMANENT dedup to a bounded `window_hours` (default `DEDUP_WINDOW_HOURS=4`, mirrors
`confirm.py`'s own `DEFAULT_REMINDER_HOURS`) — a message that stays stuck past the window (via
reprocess or just persisting) gets alerted again. Part of RED `979064b` → GREEN `34a3caa`.

**Finding 4 (unbounded growth):** `dl_alerts.prune_delivered()` (new) removes DELIVERED rows past
`DELIVERED_RETENTION_DAYS=30`, never touching undelivered ones; `flush_pending()` only selects
rows under `MAX_FLUSH_ATTEMPTS=200` — a row past the cap stops being actively retried but stays
counted in `pending_count()`. Same RED/GREEN pair as finding 1/3.

**Finding 5 (weak dashboard visibility):** the three current-state gauges move off the small
`#dlStats` header strip into a new, visually prominent `#dlAlertBanner` (yellow, own lines per
class, hidden on a quiet day). RED `431b96e` → GREEN `136f44c`.

**Finding 6 (safe retry infrastructure, deliberately PARTIAL):** `upload.put()` now writes to a
temp name and `sftp.rename()`s to the final name only after the write succeeds, with best-effort
temp-file cleanup on EITHER failure point (write or rename — a review finding fixed the rename
half, which an earlier draft missed). `desadv_edi.stable_prefix()`/`already_landed()` prove
presence/absence by document IDENTITY (buyer/supplier EAN + doc number), never by filename,
tolerant of the `Z-` wire prefix and Communicator's own extra rename-job `Z-` (tightened by review
to match `confirm.py`'s exact one-extra-`Z-` tolerance, not unbounded). Deliberately NOT wired
into `dl_worker._process_document`'s auto-retry — see the design comment on #239 for why (a
structural refactor of the exact function that caused the original incident deserves its own
focused PR). RED `7c84bad`/`be2435d` → GREEN `4f921cc`/`d75a048`, review-fix `5be2baf`.

**Finding 7 (playbook):** new section in `.claude/rules/n8n-workflow-edits.md` — never auto-retry
an upload whose failure could have left bytes on the target without BOTH a temp-write+rename and
a stable-identity absence proof.

Deep review (fresh-context `general-purpose` subagent, full diff `e677149..HEAD`): 0 🔴, 1 🟡, 2
🔵 on the finding-6 primitives (rename-failure cleanup gap, an over-permissive `Z-`-stripping loop
that didn't actually match its own docstring's claimed parity with `confirm.py`, and an
undocumented 10-char-truncation identity-collision caveat). All three fixed in `5be2baf` with new
tests. Full local suite green throughout (`pytest tests/ -q --cov=app --cov-fail-under=85` →
93.77%, all tests passing).

Version bumped 0.9.72 → 0.9.73.

## 2026-08-12 — #258 (PR #263)

- **#258** (DL: HK LOAN never attaches a real document — the delivery note is written
  directly in the mail's own BODY TEXT, only a decorative signature logo as "attachment").
  `dl_worker._process_message` used to bail out immediately with a generic review reason
  whenever `usable_attachments` was empty, never reading `message["combined_text"]` at
  all. Fix: when there's no usable attachment, build ONE synthetic source entry from the
  mail's own body text (`{"idx": -1, "pdf_bytes": b"", "machine_text": body_text}`) and
  feed it through the SAME `dl_extract.extract_email()` call an attachment goes through —
  `dl_extract.py`'s existing W13/R42 routing already skips vision whenever `machine_text`
  is present and `pdf_bytes` is empty, so zero changes needed there. Item matching, EDI
  build, ORION upload, `desadv_sent`, board questions — all unchanged downstream.
- STEP 0 validation + design comments posted on #258 BEFORE the first commit (live
  production evidence: 12 HK LOAN messages, all `method='skipped'` decorative attachment,
  real delivery-note text in `combined_text`). The ticket's own `needs-decision` question
  was already resolved by the dispatch (build it) — recorded + label removed.
- Commits: `28757d0` (version bump 0.9.74), `91a7c54` (RED — 3 new tests fail on
  unfixed code, 0 model calls), `510e4c7` (GREEN — mechanism), `e1214ee` (deep-review
  fix: `_mail_body_only()` strips `_combined_text`'s own "Attachments:" block so a
  non-PDF/image attachment's OWN extracted text — folded into `combined_text` by
  `app/process.py` regardless of type — never leaks into the body-text fallback;
  reworded the body-text error path to stop calling body text a "príloha"; added an
  observability log line; new regression test captures the actual extraction-call
  input text).
- Deep review: one fresh-context `general-purpose` subagent (never the built-in
  `Skill({skill:"review"})`), 0 🔴, 4 🟡, 2 🔵 — all fixed in the same branch/commit
  before merge (see `e1214ee` above); one 🟡 (corpus-regression risk unverifiable from
  the diff alone) was already covered by the corpus work below.
- **DL eval corpus** (dev2, `~/eval-corpus/email-extractor/dl/`, outside git — real
  customer mail): new case `hkloan_delivery_note_in_body_text_no_attachment` using the
  ACTUAL HK LOAN production wording (read-only SELECT off the live HA box). `--live`
  verified twice (consistent): the model does NOT recognize this specific informal,
  no-doc-number/no-price wording as a delivery note (`documents: []`) — a genuine,
  separate prompt-coverage gap in the shared `dl_extract.md`, filed as its own
  cross-cutting follow-up (**#262**) rather than folded into this fix (a prompt used by
  every DL supplier needs a full corpus re-verify to change safely). The corpus case
  locks in the CURRENT honest behavior (extraction genuinely attempted, correctly
  worded review) — 9/9 cases pass offline (`--require-all`, exit 0), baseline updated.
- CI: `test`/`e2e-orders`/`e2e-dl`/`build` all green on both the `push` and
  `pull_request` triggers, both commits. PR #263 merged `61aaace`. Main CI green
  (build pushed `ghcr.io/zbynekdrlik/email-extractor-amd64:0.9.74`).
- Deployed + verified live: `/health` → `{"ok":true,"version":"0.9.74"}`; dashboard DOM
  shows `v0.9.74` (Playwright, 0 console errors); **functional** — reprocessed a real,
  already-reviewed, never-shipped HK LOAN message (id 6389, confirmed zero
  `desadv_sent` rows first) and confirmed live in `email_events`/the dashboard API: the
  NEW reason text `"Nepodarilo sa rozpoznať žiadny dodací list v texte e-mailu"` fired
  in production, proving the fixed code path genuinely executes on real HK LOAN mail
  (matches the corpus finding — extraction is attempted, this particular wording just
  isn't recognized yet, tracked in #262).
- Run card fired for #258 (`v0.9.74`). Filed follow-up: **#262** (`dl_extract.md`
  prompt doesn't recognize an informal free-text delivery announcement).

## 2026-08-12 — #262 (PR #264)

- **#262** (DL prompt: recognize an informal delivery announcement in mail body text as
  a valid dodací list, even with no docNumber/price/VAT). Root cause: `dl_extract.md`
  was written entirely for a printed/scanned document — HK LOAN's real production text
  (no doc number, no table, no price) got `documents: []` from gpt-5.4, reproduced fresh
  in STEP 0 (own live `--live` call, same result as #258's own finding).
- Two prompt sections added: "Neformálna avizácia" (recognize when BOTH delivery
  vocabulary+date AND a concrete item are present; docNumber/price/VAT explicitly left
  empty, never invented) and "Toto NIE JE dodací list" (explicit negative list — cenník,
  objednávka/dopyt, faktúra, reklamácia, bežná správa — so the widened prompt cannot
  start false-positiving on ordinary mail).
- Second fix, explicitly required by the ticket itself: `desadv_edi.build()`'s existing
  no-docNumber fallback (`_generate_doc_number`, wall-clock based, R83) is unsafe once
  numberless documents become routine — a retry of the same message would get a
  DIFFERENT synthesized identity and could double-upload to ORION. New
  `desadv_edi.generate_stable_doc_number(message_id)` (sha256, deterministic, prefix
  `AVIZO`) is synthesized in `dl_worker._process_document()` BEFORE `build()`, only when
  extraction found no docNumber. `desadv.claim_send_or_identify()` itself untouched.
- Commits: `ec96159` (bump 0.9.75), `947023c` (RED — 6 tests: `generate_stable_doc_number`
  doesn't exist yet; `test_a_numberless_document_gets_a_stable_doc_number_so_a_retry_never_double_ships`
  fails via a genuine wiring gap, mutation-verified by the review subagent), `5e41084`
  (GREEN — prompt + stable-doc-number wiring + prompt content-lock tests).
- STEP 0 + design comments posted on #262 BEFORE the first commit — **initially posted
  with `gh api -f body=@file` (lowercase `-f`, which does NOT read `@file` — the literal
  string landed as the comment body), caught by the deep-review subagent and fixed via
  `gh api ... -X PATCH -F body=@file` (capital `-F`).** Lesson already existed in
  `gh-cli-recipes.md` for `create`/`comment`; extend the same caution to raw `gh api -f`
  vs `-F` — `-f` is ALWAYS a literal string, never a file reference.
- Deep review: one fresh-context `general-purpose` subagent — 1 🔴 (critical, confirmed
  + fixed), 1 🟡 (confirmed + fixed, see above), 2 🔵 (documented, not regressions).
  🔴: the `--live` corpus re-record was written to `~/eval-corpus/email-extractor/dl/
  llm-cache/` — but `.github/workflows/ci.yml`'s `e2e-dl` job reads the SHARED top-level
  `$CORPUS/llm-cache/` (`orders-corpus.md`'s own documented convention — DL cases share
  the AI-orders cache, this ticket's own worker missed it). 9/10 cases would have
  `CacheMiss`'d in CI, and the new negative (price-list) case's "pass" was accidental
  (CacheMiss produces the same outward review as a genuine rejection — the negative
  proof was actually unverified). Fixed: re-ran `--live` against the correct shared
  cache, verified offline `--require-all` (10/10, exit 0), updated baseline, deleted the
  stray `dl/llm-cache/` dir, and sanity-checked the AI-orders (non-DL) corpus still
  passes offline against the same shared cache (pre-existing `KNOWN DEFECT #120` cases
  only, exit 0 — no collision from the new content-addressed entries).
- **DL eval corpus**: 8 pre-existing cases unchanged; `hkloan_delivery_note_in_body_text_
  no_attachment` updated from `review`/`documents:[]` to a full `ok` (added a synthetic
  `HK LOAN Sp. z o.o.` customer row + a `Múka pšeničná typ 650` catalog row to the
  corpus's own frozen `customers.csv`/`dl_catalog.csv`, mirroring the convention other
  real-named-supplier cases already use) — proves the FULL pipeline (extraction → supplier
  match → item match → EDI build), not just extraction alone. New negative case
  `cennik_price_list_never_becomes_a_delivery_note` (synthetic price-list mail) proves the
  widened prompt does NOT false-positive — genuinely verified against a real `--live` model
  call after the cache-directory fix. 10/10 `--live` + 10/10 offline, baseline updated.
- CI: `test`/`e2e-dl`/`e2e-orders`/`build` green on both `push` and `pull_request`
  triggers. PR #264 merged `1099dbb`. Main CI green (`build` pushed
  `ghcr.io/zbynekdrlik/email-extractor-amd64:0.9.75`).
- Deployed + verified live: `/health` → `{"ok":true,"version":"0.9.75"}`; dashboard DOM
  shows `v0.9.75` (Playwright, 0 console errors, confirmed via `browser_console_messages`);
  **functional** — zero-write/zero-post shadow run of `dl_worker._process_message()`
  inside the deployed container against the REAL production HK LOAN message
  (`<TIN8T7$...@hkloan.eu>`, "Avizacia/17.7.2026/ G-P"): extraction now genuinely
  recognizes the document (`documents` no longer empty) — the review reason changed from
  the old "Nepodarilo sa rozpoznať žiadny dodací list v texte e-mailu" to "Názov,
  e-mailová doména aj mesto sa nezhodujú so žiadnym z kandidátov" (a SEPARATE,
  already-tracked #236 gap — HK LOAN is not yet a registered DL supplier in production;
  confirmed via read-only SELECT against `dl_supplier_snapshot`, zero rows for hkloan).
  `messages` row confirmed byte-identical before/after (`processed=true, attempts=1`,
  unchanged) — shadow mode genuinely wrote nothing. Container's own deployed
  `dl_extract.md`/`desadv_edi.py` grepped for the new content to rule out a stale image.
  Finding posted as a comment on #236 (its own item #2 already tracked "HK LOAN never
  registered" — this confirms the remaining blocker after #262 is purely that, not
  extraction quality).
- Run card fired for #262 (`v0.9.75`). No new follow-up filed — the one real discovery
  (HK LOAN still not a registered DL supplier) was already #236's own tracked item #2;
  posted fresh confirming evidence there instead of duplicating.

## #236 — re-verification pass (no code change; FEAST + TLS/Great already shipped, HK
LOAN item-matching confirmed live, new HK LOAN multi-message gap found)

- Re-verified the CURRENT live DB state (never trusted prior comments' own claims,
  one of which — FEAST question 26 timing — turned out stale vs the DB): FEAST s.r.o.
  fully done (`dl_supplier_overrides id=2`, doc 20263245 `desadv_sent id=13
  import_status=imported`, `order_questions id=26 status=answered`); TLS/Forbak fully
  done (`dl_supplier_overrides id=3`, catalog `gtin=3605` renamed to weight-neutral
  `"Great"`, both `VP20261501`/`VP20261598` `desadv_sent import_status=imported`, a
  third attempt correctly recognized `duplicate`). Neither needed any new action.
- **HK LOAN item-matching, explicitly requested by #262's own worker** ("Múka pšeničná
  typ 650" only tested against the corpus copy, never live) — verified live via
  `dl_worker._match_item()` called directly inside the deployed container against the
  real `dl_snapshot.load_catalog()` (snapshot 9, 491 cards), `gpt-5.4`/`high`: matches
  `gtin=1564` ("T 650 - chlebová múka"), confidence 0.98, rule `llm_sure`, lexical
  guard did not fire. Works correctly against the live production catalog.
  Supplier itself intentionally still unregistered (2026-08-11 user directive: the
  sklad fills it in on her board, not the owner) — `order_questions id=35` open,
  correctly worded, not a duplicate of any other open question.
- **New finding, filed as #265 (`Scope-gate: needs-user-decision`)**: HK LOAN writes
  delivery notes directly into mail body text (#258) and routinely sends a SHORT
  follow-up "correction" mail restating only the changed line ("OPRAVA HMOTNOSTI" /
  "zvyšok bez zmien") — the DL engine has NO cross-message memory anywhere, so
  reprocessing that correction mail alone (verified live via a read-only
  `dl_extract.extract_email()` call against its real text) extracts exactly ONE item,
  silently dropping the other two items from the same physical delivery. Compounding:
  `release_for_question()` only ever reprocesses the ONE message its `qid` is tied to
  — `order_questions.id=35` is tied to that correction mail specifically, and 5 OLDER
  HK LOAN delivery mails (verified: zero `order_questions` rows reference them) will
  never auto-unstick even after the sklad answers question 35. Documented on `#236`
  and playbook-recorded in `.claude/rules/n8n-workflow-edits.md` (new "mail-body-only
  CORRECTION/AMENDMENT" section) so a future HK LOAN-shaped supplier hits a known gap,
  not a fresh incident.
- No code changed — this session was pure live-DB/live-catalog re-verification plus
  one new investigation; `#265` intentionally left the actual fix undesigned (several
  valid directions with real automation-vs-safety tradeoffs). No PR, no version bump,
  no deploy. `#236` stays OPEN — still genuinely parked on the sklad answering
  `order_questions.id=35`, now with the `#265` risk explicitly flagged for whoever
  reviews that answer.

## 2026-08-12 — #237 (PR #266, v0.9.75 → v0.9.76)

- **#237** (report long-open board questions to Odoo, grouped, escalate once — HK
  LOAN's `dl_supplier` question #35 sat open 24h+ with zero reminder): new
  `app/orders/question_alerts.py`, wired into `worker.run_forever` on the same tick as
  `confirm.sweep`. "Working days" = distinct Mon-Fri calendar dates the question has
  been open across (inclusive of creation date + today) — reuses
  `confirm.morning_check_active` (same weekday/hour config) rather than a second gate.
  Reminder at `question_stale_working_days` (default 2), ONE escalation at
  `question_escalate_working_days` (default 4, only after ≥1 full calendar day since
  the reminder), then silent forever. Two new nullable columns
  (`order_questions.reminder_sent_at`/`escalated_at`). Grouped per (audience, level) —
  DL kinds (`dl_item`/`dl_supplier`) → channel 243, everything else → 152. Delivery
  reuses the existing `dl_alerts` durable outbox (`enqueue`/`flush_pending`), not a new
  post path. Repeat count = real `count(*)` over `(kind, customer_ean, item_key)`.
- Commits: `66b5291` (version bump), `3aad6ab` (schema+config), `dbb77f9` (feature +
  tests), `3eeaf95` (review-finding fix). Feature work, no RED/GREEN split per
  `regression-test-first.md` (calibrated TDD — tests mandatory, order flexible).
- **Deep-review pass (dispatched fresh-context subagent) found 2 🟡, both fixed same
  branch**: (1) `teach.undo()` and its 5 sibling `_undo_*` kind functions never cleared
  `reminder_sent_at`/`escalated_at` on reopen — a reminded-then-answered-then-undone
  question would sit reopened forever with the cadence gate stuck closed, silently
  reintroducing exactly the bug #237 fixes. Fixed in all 7 occurrences + new
  cross-kind test `test_undo_clears_the_reminder_cadence_for_every_kind`. (2) Repeat
  detection is structurally always "1×" for `mail`/`date`/`line` (their `item_key` is
  message-scoped by design) — documented in code, not changed; the ticket's actual
  motivating case (`item`/`customer`/`dl_item`/`dl_supplier`) works correctly.
- Deployed + verified live: `/health` → `{"ok":true,"version":"0.9.76"}`; dashboard DOM
  shows `v0.9.76` (Playwright, 0 console errors on `/` and `/otazky-dl`); **functional**
  — rollback-based dry run of the REAL deployed `question_alerts.sweep()` against live
  production data (own psycopg connection, `autocommit=False`, always rolled back —
  nothing persisted, no Odoo post) captured the exact grouped Slovak HTML it would send
  for the 3 real open stale questions (#29 customer, #30 dl_item, #35 dl_supplier —
  HK LOAN, the ticket's own cited example). Separately confirmed the LIVE worker had
  already (for real, on its own tick) enqueued both grouped alerts into `pending_alerts`
  — visible on `/otazky-dl`'s own existing "🔔 N upozornenie stále čaká" counter. Odoo
  delivery is currently PENDING (retrying every ~15s), not yet delivered: `erp.slovnormal.sk`
  is in maintenance right now (`GET /` → 503 "ERP - Maintenance", `POST /json/2/*` → 405
  nginx) — same signature as the already-closed #253, confirmed via read-only diagnostic
  requests (no message sent). This is the durable-retry outbox working exactly as
  designed: nothing lost, will deliver once Odoo recovers. Filed as `#267` (external,
  `Scope-gate: user-request`) to track the fresh Odoo outage — no code change needed
  here. All 3 questions confirmed still `status=open`/`answered_at=null` via
  `/api/orders/questions` — nothing was resolved as a side effect.
- Run card fired for #237 (`v0.9.76`).
