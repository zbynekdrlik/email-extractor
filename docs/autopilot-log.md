# Autopilot log

Terse per-ticket record: issue #, commit SHAs, RED→GREEN test names, decisions, shared PR #.

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
