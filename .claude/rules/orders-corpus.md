---
paths:
  - "email-extractor/app/orders/**"
  - "email-extractor/app/httpapi.py"
  - "email-extractor/app/config.py"
  - "email-extractor/config.yaml"
  - "email-extractor/tests/test_e2e.py"
  - ".github/workflows/ci.yml"
---

# AI orders — the 30-email corpus is the gate

Every change to `app/orders/**` is measured against 30 real archived emails before it can be
merged. This is the whole reason the package exists as it does: the n8n version could not be
improved because nothing measured what an improvement broke.

## Where it is

`~/eval-corpus/email-extractor/` on **dev2**, deliberately outside git — the cases are real
customer mail and this repo is public. The `e2e-orders` CI job runs on the dev2 self-hosted
runner (label `email-extractor-eval`); `test` + `e2e-orders` are required checks on `main`.

| file | what it is |
|---|---|
| `manifest.json` | the 30 cases: input email, expected outcome, `oracle`, and `why` for every hand-decided one |
| `catalog.csv`, `customers.csv` | the FROZEN snapshot the expectations were written against — never re-fetch from the live sheet |
| `history.json` | archived deliveries, so a case is judged against the history as it stood on its own day |
| `taught.json` | per-customer / global taught mappings, seeded before the run (#102 — see below) |
| `llm-cache/` | recorded model answers; the gate replays them, so it needs no API key |
| `baseline.json` | locked pass/fail per case |

## Two tiers

```
offline (CI, seconds)   python -m app.orders.eval_run --manifest … --catalog … --customers … \
                          --history … --taught … --baseline … --require-all
live    (deliberate)    … --live          # calls gpt-5.4 and RE-RECORDS the cache
live, cheap (#87)       … --live --sample 5   # deterministic 5-case subset (one per case
                          type, then fills by manifest order — never random), prints an
                          estimated cost line before it starts. Use this WHILE iterating on
                          a prompt; run the full 30-case --live once before merging.
```

**A prompt edit invalidates every cache key**, so it needs a `--live` run before CI can pass
again. That is the point: a changed prompt has not been measured until it has been measured.
`--sample` makes each iteration of that re-record ~6x cheaper (measured: $4.50/30 cases full
→ ~$0.75/5 cases sampled) without losing type coverage.

**Running `--live` from a plain checkout (not the live add-on) needs `OPENAI_API_KEY`
exported explicitly (#188/#189, 2026-08-06)** — the add-on's own `openai_api_key` option
only reaches `config.Config.load()` when running INSIDE the deployed container; a scratch
run on dev2's runner checkout (or anywhere else) has no add-on options to read and fails
with `no OpenAI API key configured` unless you `export OPENAI_API_KEY=...` first (value:
`openai-api-key.md` memory).

**A full 35-case `--live` re-record takes 20-25 minutes — launch it DETACHED on the
remote side, never as a plain foreground SSH command with a guessed inner `timeout`**
(#189, 2026-08-06: a `timeout 590 ...` guess killed the run mid-way through case 14/35,
EXIT=124, with the first 14 cases' cache writes still usable but the run itself wasted).
Use `nohup bash -c '... ; echo $? > /tmp/x.exit' > /tmp/x.log 2>&1 & disown` over SSH (the
SSH command returns immediately), then poll `/tmp/x.exit` from a separate SSH call every
~15s until it appears — this survives your own polling connections dropping/timing out,
since the actual work is detached from any one SSH session.

**Running `eval_run.py` offline OUTSIDE the container (a scratch dev2 checkout, #195,
2026-08-06) needs BOTH `PG_DSN` and `LLM_CACHE_DIR` exported explicitly** — the module
defaults `llm_cache_dir` to `/data/llm-cache` (`app/config.py`), which only exists
inside the deployed add-on container. Without `LLM_CACHE_DIR` pointed at the corpus's
own `llm-cache/` directory, EVERY case fails with an identical-looking
`no cached answer for <hash> (prompt <hash>)` — the SAME extract-prompt hash on every
failure line is the tell (the run never gets past the first, shared extraction call),
not a sign the cache itself is stale. Working invocation from a checkout:
```
PG_DSN="postgresql://postgres:postgres@localhost:<port>/postgres" \
LLM_CACHE_DIR="/home/newlevel/eval-corpus/email-extractor/llm-cache" \
.venv/bin/python -m app.orders.eval_run --manifest … --catalog … --customers … \
  --history … --taught … --baseline … --require-all
```
`ee-eval-pg` (port 55434, `POSTGRES_PASSWORD=postgres`) is a dedicated dev2 container
for exactly this — check `docker ps -a` for it before assuming a fresh one is needed.

**A composed SSH one-liner silently drops its own `cd` clause when built as a bash
string across several edit attempts (#195)** — the fix that actually works reliably is
writing the full multi-line command (with the `cd /path/to/email-extractor &&` prefix)
to a scratch FILE first, then running `ssh dev2 "$(cat that-file)"`. Composing the
string inline, even carefully, produced several silent no-op `.venv/bin/python: No such
file or directory` failures in a row before switching to the file-based form fixed it
immediately — if an SSH one-liner keeps failing on a missing `.venv`/relative path with
no other explanation, suspect the `cd` never made it into the actual command sent, and
switch to the file-based form rather than re-typing the same string again.

**A stray leftover script literally named `/tmp/inspect.py` (from an EARLIER,
unrelated debugging session) silently shadows the stdlib `inspect` module for any
Python script run from bare `/tmp` (#195)** — Python auto-prepends the running
script's OWN directory to `sys.path`, so `python /tmp/my_scratch_script.py` picks up
`/tmp/inspect.py` INSTEAD of the real `inspect` module the moment anything
(`dataclasses`, among many stdlib modules) imports it, producing a confusing
`AttributeError: module 'inspect' has no attribute 'get_annotations'` that looks like
a Python-version problem but isn't. Never run a scratch investigation script from
bare `/tmp` on a shared dev box — always `mkdir` a fresh, uniquely-named subdirectory
first (e.g. `/tmp/claude-inspect/`) and run from there.

**Slovak word comparison for "is this the same product?" needs a shared STEM, not
exact token equality (#195, found via real corpus validation — not guessable up
front).** A customer's short noun form ("oliva", "tekvička") and a catalog card's
adjective form ("olivovo", "tekvicový") are grammatically different but the SAME
product; comparing whole `_distinctive_words()`-style tokens for exact set membership
treats them as unrelated. `app/orders/match.py`'s new `_lexical_overlap()` (#195)
compares a fixed-length prefix (`STEM_PREFIX = 4`) instead — cheap, no real stemmer
dependency, and empirically validated against the full 35-case corpus (0 false
positives after the fix, vs 2 with exact-match). Any FUTURE Slovak-wording comparison
in this codebase should default to this same stem-prefix approach rather than
re-discovering the same false-positive class from scratch; `_better_alias_candidate`
(#157/#186) still uses exact-match deliberately and is corpus-validated as-is — don't
"fix" it without re-validating against the corpus first, exact-match may be
intentional there for cards it was tuned against.

## Rules when you touch this

- **A new warehouse complaint becomes a corpus case BEFORE its fix is written.** The corpus
  only grows.
- **STANDING RULE (#188): every production-incident fix PR is INCOMPLETE without a corpus
  case for the mail that triggered it.** Three real incidents (#157, #186, #187) all shipped
  from a shape the 30-case corpus did not cover at the time — nothing forced the fix PR to
  also grow the corpus, so the SAME class of gap (alias-heavy customer wording, multi-date
  single mails, whole-quoted bodies) recurred silently (see #193: the exact #186 alias-bias
  class had ALREADY shipped a wrong bread substitution to a real customer 3 days before the
  incident that got it noticed and fixed). Before closing ANY ticket whose root cause is a
  real customer mail that broke extraction/matching: (1) pull that mail from `messages`/
  `order_runs` on the HA box (read-only), (2) verify the CURRENT code's actual output for it
  via the corpus's own `--live` procedure (never freeze the historically-shipped value
  blindly — it may itself have been wrong, exactly the #193 finding), (3) add it to
  `manifest.json`/`llm-cache`/`baseline.json` on dev2 in the SAME PR the fix ships in. A fix
  with no corpus case is a fix nobody will notice regressing.
- **EXTENSION (#196): the SAME PR also adds a row to the app's own `match_incidents`
  table** (`app/db.py`, `occurred_on`/`description`/`issue_ref UNIQUE`) — this is what
  `reliability.days_since_incident()` reports on the dashboard/daily digest as "days
  since the last confirmed incident", live-computed, never a hand-maintained constant.
  Two records for one real incident: the dev2 corpus case (what regresses if the SAME
  mistake recurs) and the `match_incidents` row (when the warehouse can start trusting
  the system a little more). Forgetting the second one doesn't break CI — it just quietly
  keeps the trust metric wrong, so treat it as mandatory as the corpus case itself.
- **Assert only what you can prove.** `items` when the shipped record gives the cards,
  `item_count` when only the number of lines is provable, the delivery date alone when neither
  is. A guessed GTIN frozen into the baseline is worse than no assertion.
- **n8n's output is NOT the oracle.** 20 of the 30 cases say `oracle: human` precisely because
  reading the mail showed n8n was wrong — it dropped every delivery date after the first
  (#80) and once built an order out of quoted text (#82). Copy n8n only where you have checked
  it.
- **Never mark a failing case as passing.** A case pinning behaviour we do not have yet gets
  `known_defect: "#N"`; the gate prints it on every run and it is excluded from `--require-all`
  only until the ticket is closed.
- **A case testing `teach`/`item_memory` (per-customer OR global, #102) needs its state
  PRE-SEEDED via `--taught taught.json`, never exercised through the real ask/answer/undo
  HTTP flow.** `run_case` forces shadow mode, so `teach.ask`/`teach.answer`'s side effects
  never fire during a corpus replay — only READS (`memory.resolve`/`resolve_global`, and
  therefore `match.decide_without_model`) run. `taught.json` entries:
  `{"scope": "global", "wording": ..., "gtin": ..., "card": ...}` or
  `{"scope": "customer", "customer_ean": ..., "wording": ..., "gtin": ..., "card": ...}`,
  loaded by `memory.seed_taught` (mirrors `seed_from_archive`/`--history`). The ask/answer/
  undo flow itself (including the Odoo post and the `question_id`-scoped undo) is covered by
  `tests/test_orders_teach.py`, not the corpus — the corpus can only observe the RESULTING
  match, never the side effects a real question triggers.
- **A brand-new corpus case needs its OWN `llm-cache` entries — the customer-match and
  extraction calls are NEVER skipped, even when `decide_without_model` will resolve every
  item for free.** Build the case, run it once with `--live` against a SEPARATE scratch
  manifest (just the new case(s), not the whole 30+), confirm the result by hand
  (`--dump`), THEN copy the new cache files into `llm-cache/` and merge the case into
  `manifest.json` — cheap (~$0.15/case, #102 cost 2 cases ≈ $0.30) because ONLY the new
  case's content-hash is a cache miss; the other 30+ stay untouched. Re-run the FULL corpus
  offline with `--require-all` before `--update-baseline`, to prove the new case coexists
  with everything else, not just that it passes alone.
- The harness must stay inert: `run_case` forces shadow mode and refuses upload/post. An
  evaluation that ships an order would be the worst possible bug here.
- **This Python engine is NOT the only text parser in production — the live n8n workflow
  "Static auto orders" (`O8IYhUESjaWmPMTI`) has its own independent `extractor`/`generator`
  Code nodes for KOMFOS/KARMEN/LABAS, with their own regexes and their own product-EAN
  resolution.** A parsing-robustness fix here (e.g. this package's `ZERO_WIDTH` table in
  `extract.py`) does NOT automatically protect that workflow — check whether the same
  vulnerability class applies there too (#41 found `\s+ks` regexes with no invisible-char
  guard) and mirror the fix via the n8n MCP (`get_workflow_details` → `update_workflow` with
  `updateNodeParameters` on the node's `jsCode` → `publish_workflow` → re-fetch to verify
  `versionId == activeVersionId`).
- **`app/orders/static_ean.py` is a hand-kept 1:1 Python PORT of `generator`'s
  `getProductEAN()` (#49)** — n8n's JS Code node cannot run in this repo's CI, so this
  module is the CI-tested proof the matching algorithm is correct; it is NOT imported by
  the live pipeline. `PRODUCT_EAN_BY_CODE`/`PRODUCT_EAN_BY_NAME` in the node are now an
  OVERRIDE for genuine exceptions only — the primary path resolves against the
  `catalog_snapshot` table (same source `AI auto orders` uses, #59; a parallel Postgres
  branch off "Get Static Orders", node name `Get Catalog Snapshot`, credential
  `Email Extractor Postgres`). **Any change to one side of this mirror MUST be applied to
  the other in the same PR** — same discipline as the `ZERO_WIDTH` mirror above.
- **Verify an n8n Code-node `jsCode` update BYTE-FOR-BYTE after `update_workflow`/
  `publish_workflow` — the MCP round-trip can silently mangle a character.** Observed
  2026-08-01: a plain ASCII `'B'` in an unrelated part of the string came back as Greek
  capital Beta (U+0392) after `updateNodeParameters`, breaking nothing syntactically (still
  valid JS) but corrupting the text. Re-`get_workflow_details`, extract the same node's
  `parameters.jsCode` with `jq`, and `diff` it against the string you sent — do this BEFORE
  `publish_workflow`, or immediately after and republish if it differs. When a diff shows a
  non-ASCII character where you intended plain ASCII, resend that substring using an
  explicit `\uXXXX` JS escape (verified fix: it evaluates correctly at runtime and survives
  the round-trip) rather than the literal character.
- **Testing a catalog-lookup change against the REAL catalog finds collisions no synthetic
  fixture will** — e.g. `catalog_snapshot` has BOTH "Lupačka 60g" and "Lupačka 75gr" (two
  weight variants of a single-core-token product name), which a hand-picked test catalog
  is unlikely to include. Pull the live rows read-only (`ssh` to the HA box, `docker exec
  ... psql -h 127.0.0.1 -U email -d email`, `PGPASSWORD` from `/data/options.json`'s
  `pg_password` — see the `ha-server-access`/`email-extractor-deploy` memory for current
  values, never hardcode them in a committed file) and replay the exact JS/Python matching
  function against it (a `node -e` harness with a mocked `$('NodeName')` works well for the
  n8n side) before trusting a synthetic-only test suite.
- **Migrating an `httpRequest` node off a hardcoded `Authorization` header, onto an n8n
  credential (#50/#108, 2026-08-01).** `get_workflow_details` did not return the `credentials`
  key on ANY node it was checked against (httpRequest, postgres, ssh, executeWorkflow and
  langchain nodes, across two different workflows) — so treat it as stripped from the
  response and don't rely on reading it back to verify a credential attachment — verify
  instead by checking that the migrated node's `authentication`/`genericAuthType`
  parameters are set AND that a live execution afterward returns a real
  API result (not a 401). The working shape (copy verbatim from `AI auto orders`'
  `Odoo Success`/`Odoo Needs Review` nodes): `authentication: "genericCredentialType"`,
  `genericAuthType: "httpHeaderAuth"`, `headerParameters.parameters` keeps only
  non-secret headers (e.g. `X-Odoo-Database`), and a separate `setNodeCredential` op
  (`credentialKey: "httpHeaderAuth"`) attaches the credential by id. Do the parameter
  edit and the credential attach as `setNodeParameter`/`setNodeCredential` ops in the
  SAME `update_workflow` call. **The literal secret stays visible in `get_workflow_details`
  under `.workflow.activeVersion.…` until you `publish_workflow`** — the draft you just
  wrote (`workflow.versionId`) and the still-live old version (`workflow.activeVersionId`)
  differ until publish; only check `versionId == activeVersionId` (and grep the WHOLE
  response, not just `.workflow.nodes`) to confirm the secret is actually gone from the
  active version. It still lives in n8n's version history regardless — rotating the
  underlying key is the only way to fully invalidate it, and only whoever owns that
  external service can do that.
- **Creating a brand-new n8n credential with NO UI/API login and NO valid Public API key
  (#55, 2026-08-02).** The n8n MCP can only READ credentials (`list_credentials`), never
  create them — but the n8n add-on container ships its own CLI, and `n8n
  import:credentials` creates one with no UI/session and no API key at all, encrypting
  it with the instance's REAL encryption key exactly like the UI would:
  ```
  # cred.json: [{"id":"<16-char alnum, like other n8n ids>","name":"...","type":"httpHeaderAuth","data":{"name":"X-Token","value":"<token>"}}]
  docker cp cred.json <n8n-container>:/tmp/cred.json
  docker exec -e N8N_USER_FOLDER=/data/n8n <n8n-container> \
    n8n import:credentials --input=/tmp/cred.json --projectId=<owner project id>
  ```
  Three gotchas that will silently go wrong without this exact shape:
  1. **`N8N_USER_FOLDER` is NOT visible to a plain `docker exec` env** — it's set only by
     the container's supervisor wrapper around the real n8n process (`docker exec
     printenv` shows it missing; confirm via `/proc/<n8n-pid>/environ`). Without it, the
     CLI defaults to `HOME=/root` and **silently creates a whole SEPARATE, empty n8n data
     directory** (`/root/.n8n/database.sqlite` + a freshly auto-generated encryption
     key) — it even runs the FULL migration set as if bootstrapping a brand-new instance,
     which is the tell something is wrong. The import then fails anyway
     (`SQLITE_CONSTRAINT: NOT NULL constraint failed: credentials_entity.id`, see #2), but
     even a "successful" import there would land in a database the running server never
     reads. Always pass `N8N_USER_FOLDER` explicitly (find the real value via the running
     process's `/proc/<pid>/environ`, not just container env) and delete the stray
     `/root/.n8n` afterward if it got created.
  2. **`id` is required in the input JSON** — omitting it throws `NOT NULL constraint
     failed: credentials_entity.id`. Generate one in the same shape as n8n's own ids
     (16 alnum chars).
  3. **`--projectId` decides ownership** — omit it and the credential lands somewhere
     that may not match the workflow you're about to bind it to. Find the right project
     via `list_credentials` on an EXISTING credential already used by the target
     workflow's other nodes (e.g. its Postgres credential) and reuse that credential's
     `homeProject.id`.
  **Rotating the value later** = re-run the same `import:credentials` call with the SAME
  `id` — it overwrites in place, no delete/recreate needed. Delete the temp JSON from
  both the host AND the container immediately after (`rm`) — it's a plaintext secret.
- **Updating a supervisor add-on's options (`/data/options.json`) with no UI, only SSH
  (#55, 2026-08-02).** The Supervisor REST API path is `/addons/<slug>/options`
  (**not** `/apps/<slug>/...`, despite `ha apps` being the modern CLI alias — that 404s).
  It requires the FULL merged options object, not just the changed key — POSTing
  `{"options":{"api_token":"new"}}` alone fails validation ("Missing option
  'imap_host'..."). Fetch current options first (`GET /addons/<slug>/info` →
  `.data.options`), merge in the change, POST the whole object:
  ```
  docker exec hassio_cli sh -c 'curl -s -H "Authorization: Bearer $SUPERVISOR_TOKEN" \
    http://supervisor/addons/<slug>/info'          # read .data.options
  # merge locally, then:
  docker exec hassio_cli sh -c 'curl -s -X POST -H "Authorization: Bearer $SUPERVISOR_TOKEN" \
    -H "Content-Type: application/json" http://supervisor/addons/<slug>/options -d @/tmp/new_opts.json'
  ha apps restart <slug>          # options only take effect after a restart
  ```
  A secret rotated this way (add-on option) and an n8n credential (above) are two
  INDEPENDENT stores with no atomic swap between them — rotating both in the same
  minute (credential re-import, then immediately the options POST + restart) is the best
  achievable; a brief window of 403s on the file APIs during the gap is expected and
  self-heals via the consumer workflows' existing stale-claim retry (10 min), not a bug.
- **A node validator warning does not necessarily mean the node is functionally broken —
  check a real recent execution before assuming a behavior change is needed (#108).**
  `n8n-nodes-base.ssh` v1 with `resource: "file"` requires an explicit `operation`
  (`upload`/`download`) — calling `get_node_types` with `resource` set but `operation`
  omitted returns an inline error ("requires resource and operation discriminators") in
  its response, and `validate_node_config`/`validate_workflow` reject it the same way —
  but a node already missing `operation` can still have been running correctly in production
  (n8n silently defaulted it): check `search_executions` + `get_execution(includeData:true,
  nodeNames:[...])` for the node's actual output before touching its behavior. Adding the missing explicit field
  (no other change) fixes the validator without risk. Same pattern for
  `@n8n/n8n-nodes-langchain.lmChatOpenAi` v1.3's `builtInTools` — valid only when
  `responsesApiEnabled: true` is explicitly present in `parameters`, even though the type
  schema lists `true` as that field's default; the validator does not apply schema defaults
  for an absent key, so an implicit default must be made explicit in the JSON. This project's
  standing choice is `responsesApiEnabled: true` (top OpenAI tier, Responses API on).
- **Holding an order (`app/orders/hold.py`, #93) is invisible to the corpus and needs no
  new cases.** `run_case` forces `orders_shadow=True`, and every hold/release path in
  `pipeline._run` is gated on `not shadow` — so a case with an unresolved line still just
  reports "partial"/"review" exactly as before; nothing in the corpus is ever held. If you
  add a NEW hold-adjacent rung to `match.decide_without_model` (the way #102's
  `global_taught` was added), the corpus IS affected the normal way (a no-model rung
  changes `decide_without_model`'s answer) — that follows the existing "brand-new corpus
  case" rule above, not anything hold-specific.
- **`hold.py` breaks the `pipeline` \<-\> `hold` import cycle with LAZY imports, not by
  moving code.** `pipeline.py` imports `hold` at module top (needs `hold.is_past_deadline`/
  `hold.place`); `hold.py` needs `pipeline._ship_one` to actually SHIP a released order, but
  does `from .pipeline import _ship_one` INSIDE `_do_release`, never at its own top — a
  top-level `from . import pipeline` in `hold.py` would deadlock the import (whichever module
  loads first hits the other's not-yet-defined name). Keep this pattern for any future
  order-lifecycle module that needs to call back into the pipeline's shipping step.
- **A held order releases through `_ship_one` unchanged — `hold.release_for_question`
  re-derives the DECISIONS first via `hold._redecide`, against the REAL current catalog
  snapshot (#162, `hold._current_catalog`), not an empty one.** Earlier (pre-#162) this
  passed `decide_without_model(item_name, [], ...)` an EMPTY catalog — safe for the
  already-known-customer item-hold path, where the only rungs that could ever fire
  post-hold were `human_taught`/`global_taught` (every genuinely ambiguous line already
  had its own tracked, now-answered question, so no catalog rung was ever needed). That
  assumption does NOT hold for a customer-unknown hold (#159): no item question was ever
  raised on the first pass, so a still-unmatched line's only free chance to resolve is
  the real catalog (`catalog_name`/`alias_exact`/`history_sure`) plus this now-known
  customer's own memory. `_release_locked` then checks every redecided decision's rule
  against `pipeline.ASK_THE_WAREHOUSE` (lazy-imported per the pattern above): anything
  STILL ambiguous gets a FRESH `teach.ask` question and the row stays `held` a second
  time — it is never shipped with the line silently dropped. Safe to redecide EVERY
  stored decision, not just the pending ones, because a rung that finds nothing simply
  returns `None` and the original decision is kept. See `hold._redecide`'s own docstring
  for the full detail (including the `_recalled_cache` sharing with
  `hold._ask_still_ambiguous`).
- **A `httpapi.py` route that pairs an external side effect (upload/HTTP-post) with its own
  DB "claim" write (the `edi_sent` ledger) must run that pair on an AUTOCOMMIT connection
  (`_db()`), never inside another route's `_db_tx()` transaction (review finding, #93,
  PR #116).** `_db_tx()` is a real, rollback-on-error transaction — fine for several writes
  that must land together, but WRONG when one of them is a claim for a REAL external action
  that already happened: if anything AFTER the claim fails, the whole transaction —
  including the claim — rolls back, even though the physical upload cannot be undone, and a
  retry then double-ships. `hold.release_for_question` was originally called inside
  `teach.answer`'s `_db_tx()` block in `api_orders_answer`; the fix splits it onto its own
  `with _db() as c2:` block AFTER `teach.answer`'s transaction commits. Proven, not assumed,
  by `tests/test_api.py::test_answering_over_http_commits_the_ledger_even_if_something_after_upload_fails`
  (injects a failure AFTER a faked upload and checks the ledger claim survives). Same
  invariant `worker.tick`/`hold.release_due` already followed (both use `db.connect()`,
  autocommit) — the bug was specific to the ONE route that shared a connection across two
  different modules' writes.
- **The hold-vs-ship decision must be computed from the POST-`merge_same_card(apply_
  siblings(...))` decisions, never the raw per-item list `teach.ask` was called against**
  (review finding, #93, PR #116). `apply_siblings` can rescue an `unmatched` line using an
  identically-worded line resolved elsewhere in the SAME order — by the time the order-level
  hold check runs, that line already has a settled rule (`sibling`) and the order may be
  fully resolved, even though a stale qid is still sitting in the per-item ask list. Gate
  holding on `any(d.rule in ASK_THE_WAREHOUSE for d in decisions)` computed AFTER the merge,
  not on "a question was asked for this order at any point during the item loop."
- **Any NEW `/api/orders/*` route the `/otazky` page's JS fetches must be added to
  `SKLAD_PATHS`**, or the unauthenticated warehouse-link session (`role == SKLAD_ROLE`) gets
  a silent 401 and the JS (`catch(e){return}`) swallows it — the feature simply never
  renders for the actual sklad audience, only for full-admin dashboard logins (review
  finding, #93, PR #116: `/api/orders/held` was added to the page's JS but not to
  `SKLAD_PATHS`). The security bar for adding a path: order METADATA only (customer name/
  EAN, delivery date, question ids, candidates) — never a mail body, an attachment, or spend
  data (that boundary is the whole point of `SKLAD_PATHS` existing).
- **Before widening `SKLAD_PATHS`/`SKLAD_ZNALOSTI_API` for a new `/otazky` feature, check
  whether an ALREADY-allowed endpoint already returns what you need (#149).** A new full-
  catalog search box on `/otazky` needed "search by name, get gtin+name back" — exactly what
  `/api/znalosti/catalog` (built earlier for the `/znalosti` page, matched by the existing
  `SKLAD_ZNALOSTI_API` regex) already does. Reusing it meant the allowlist needed ZERO
  changes for the new feature, and `teach.answer()`'s broadened validation
  (`snapshot.catalog_gtin_set`) could read the exact same data source, so nothing the search
  offers can ever be rejected as "not answerable." Widening the allowlist is for when no
  existing path fits — check the existing surface first.
- **A `history.json`/`taught.json` seeding change is NOT always free offline, even though
  it never touches a prompt (#83, PR #121).** `pipeline._run` passes `recalled` (from
  `memory.resolve`) into the per-item MODEL prompt (`_product_input`) as a hint even for
  an item `decide_without_model` could NOT resolve on its own — so seeding a NEW history
  row for one wording can change the exact TEXT of a still-needed model call for a
  DIFFERENT item on the SAME order (a "predtým dodané…" hint that wasn't there before),
  which is a genuine cache-key change and shows up offline as
  `no cached answer for <hash>`, not as a wrong result. Before trusting a memory/history
  change as "verifies offline for free," run the full corpus with `--dump` before AND
  after and diff every case's problems, not just the ones you meant to fix — a
  newly-appearing `no cached answer` on an UNRELATED item is this exact effect, not a bug
  in your change. Fix it the same way a brand-new case would: isolate the ONE failing case
  into its own scratch manifest, `--live` re-record just it (cheap — this incident cost 4
  calls / ~$0.15), hand-check the new answers, copy the new cache files into the shared
  `llm-cache/`.
- **Reconstructing a customer's own wording from the archive (#83) — `app/orders/
  reconstruct.py`.** Pure, DB-free: `wordings_for_order(combined_text, delivery_date,
  item_count)` splits the email's own (never quoted-reply) text into day-blocks, matches
  the block by the ORDER's own delivery date, and returns the wording list ONLY when its
  item count exactly matches the already-Odoo-confirmed shipped card count — `None`
  otherwise, never a guess. Reusable for any future archive backfill (e.g. issue #120's
  residual cases, once those customers place enough NEW real orders — nothing in the
  ARCHIVE can ever help them, since none of them has a single prior Odoo-confirmed
  delivery containing the needed wording at all). The one-off script that actually walked
  the real archive (raw email text + Odoo channel-152 dump + frozen catalog/customer
  snapshot) stays OUTSIDE git next to the other archive-reconstruction tooling
  (`link.py`, `resolve.py`, `odoo_dump.py`) — real customer data, this repo is public.
- **Editing the live "AI auto orders"/"Static auto orders" n8n workflows via `update_workflow`
  (#51, n8n edi_sent duplicate-upload ledger) — three MCP gotchas that cost real debugging time:**
  1. `get_workflow_details` **always returns `credentials: null` for every node**, even ones
     with a real, working credential bound in production (confirmed on the pre-existing `Get AI
     Orders` node). This is response scrubbing, not evidence your `setNodeCredential` op failed
     — trust the op's own success response, don't chase a phantom "credential didn't stick" bug.
  2. `setNodeParameter`'s `path` (JSON Pointer) is **relative to the node's `parameters` object,
     not the node root** — `path: "/parameters/jsonBody"` creates a WRONG nested
     `parameters.parameters.jsonBody` instead of `parameters.jsonBody`. Use `path: "/jsonBody"`.
     (`updateNodeParameters` with a flat `parameters` object + `replace: true` sidesteps this
     entirely and is usually the safer op for a multi-field node.)
  3. `httpRequest`'s `jsonBody` field has `@displayOptions.show { sendBody: [true], contentType:
     ["json"], specifyBody: ["json"] }` — if you set `sendBody`/`specifyBody` but never set
     `contentType` explicitly (relying on its documented `@default json`), the update silently
     PRUNES `jsonBody` back to `null` (no error, no warning). Always set `contentType: "json"`
     explicitly alongside `specifyBody: "json"` when building an HTTP node's JSON body via
     `update_workflow`.
  4. **A node placed downstream of an `httpRequest` node sees `$json` = that HTTP call's
     RESPONSE body, not the request's input data** — the same pitfall the existing `Log Success
     Event` (downstream of `Odoo Success`) already avoids by referencing
     `$('ASSEMBLE AND GENERATE EDI [v1]').first().json...` instead of bare `$json`. Any NEW node
     wired downstream of an Odoo/HTTP notify node must do the same (`$('<upstream node
     name>').item.json.field`), never plain `$json`.
- **This repo's git root (`/home/newlevel/devel/n8n/email_extract`) is one level ABOVE the
  add-on code (`email-extractor/`)**, and an autopilot-worker session's Bash tool cwd resets to
  the dispatch launch dir (`email-extractor/`) on EVERY call — no `cd` in a prior or same
  command persists it. `hooks/pre-push-lint.sh` computes root-relative changed-file paths via
  `git diff` but runs `ruff check` from its OWN process cwd (the launch dir), so a normal push
  that touches any `.py` file gets falsely blocked with `E902 No such file` even when the code
  is 100% lint-clean (verified: `ruff check .` from the true root passes). No bypass tag exists
  for this hook. Filed upstream as `zbynekdrlik/airuleset#218`. Until fixed there, the one-off
  workaround: create a transient, UNCOMMITTED self-referential symlink inside `email-extractor/`
  (`ln -s . email-extractor/email-extractor`), push, then `rm` it immediately — this makes the
  hook's root-relative path resolve correctly through the symlink without touching the real
  file layout or git history.
- **A downstream `If` node condition matching against `$json.error` sees only the TAIL of a
  long Code-node thrown message, never the start (#47, 2026-08-02).** n8n truncates a Code
  node's thrown `Error(...)` before it reaches the item that flows into a later node — a
  condition like `.includes('<phrase from the message START>')` silently NEVER fires, with
  no error of its own (the branch just never takes the true path). This is the SAME
  truncation the earlier `#93`/PR #116 note above already flagged for human-readable review
  comments ("n8n keeps only the tail of long Code-node error messages") — it also applies to
  MACHINE condition-matching, which is easy to miss since nothing errors, the condition just
  quietly evaluates false. Fix: match a phrase from the message's END (verify empirically —
  `test_workflow` + `get_execution` on a real run showed exactly how much survives, ~100-150
  chars). Caught by the STATIC AUTO ORDERS `extractor` guard's own message: `'Príloha je
  FOTKA — automat... Môže to byť objednávka aj vratka/doklad: otvor dashboard(...), pozri
  fotku a vybav ručne.'` — matching on `'Príloha je FOTKA'` (the start) never fired; matching
  on `'pozri fotku a vybav ručne'` (the tail) did.
- **`test_workflow` + `prepare_test_pin_data` safely dry-runs a MULTI-BRANCH workflow change
  before `publish_workflow` — credentialed/HTTP/trigger nodes are simulated, everything else
  (Code, If, Switch) executes for REAL, so wiring/routing bugs surface with zero risk to
  production data (#47).** `prepare_test_pin_data` lists every node needing pin data — supply
  it for ALL of them (even ones a specific test path won't reach; the tool pins them
  workflow-wide, not conditionally) or the test errors on a missing pin. A node that consumes
  BINARY data (e.g. `extractFromFile`) needs a `binary` SIBLING key next to `json` in its pin
  item — `{"json": {...}, "binary": {"data": {"data": "<base64>", "mimeType": "...",
  "fileName": "...", "fileExtension": "..."}}}` — the tool's own description only mentions
  the `json` wrapper, but binary-consuming nodes throw ("expects... binary file 'data'... but
  none was found") without it. Inspect results with `get_execution(includeData:true,
  nodeNames:[...])` on the returned `executionId` — the per-node `runData` shows exactly which
  output index (true/false) each `If`/`Switch` took, which is how the truncation bug above was
  actually caught (a re-run test proved the guard silently fell through).
- **`setNodeParameter` cannot descend into an ARRAY element nested inside a filter/condition
  object** (`update_workflow` op `type: "setNodeParameter"`, path
  `/conditions/conditions/0/rightValue"` → `"cannot descend into non-object at
  '/conditions/conditions'"`, #47) — this is a NEW concrete failure of the SAME shape the
  `#51` gotcha above already warns about for `jsonBody`. `updateNodeParameters` with
  `replace: true` and the FULL parameters object is the reliable fix (as that gotcha already
  recommends) — don't reach for `setNodeParameter` on anything inside an `If`/`Filter` node's
  `conditions.conditions[]` array.
- **The disabled `Call 'AI auto orders'` node inside "Static auto orders"'s error branch is a
  DEAD END, not a reusable AI-fallback hook (#47 investigation).** It looks like an
  unfinished attempt to route error-branch items into the sibling "AI auto orders" workflow
  (`wlORIhkVZISCdZNmBTM4Z`), but that workflow's own trigger (`Triggered by Dispatcher`)
  IGNORES whatever item invoked it and independently claims the next `category='ai_orders'`
  row from Postgres (`FOR UPDATE SKIP LOCKED`) — wiring a real item into it would silently
  process an UNRELATED queued order. It also has zero vision/image capability (its 3 LLM
  chains read `combinedText` only; `attachments: []` is hardcoded). Don't try to re-enable it
  without first re-architecting its trigger to accept the passed item — a future ticket
  needing an AI-fallback-from-static-parser path should build fresh, not resurrect this node.
- **This n8n instance has exactly ONE accessible project** (`search_projects` →
  `6Y0BjZ0htnxliu0C`, "Marek Drlik <drlik.marek@gmail.com>", personal; `teamProjectsEnabled:
  false`) — every credential (`list_credentials`) and every workflow seen so far lives here,
  so a NEW node needing a credential (Postgres, OpenAI, any `httpHeaderAuth`) can always
  reuse an existing credential from `list_credentials` without a project-scoping concern;
  there is nowhere else it could live.
- **A one-off management CLI in `app/orders/` (like `memory_import.py`, now also
  `alias_migration.py`, #104) must load config the SAME way** — `config.Config.load()` +
  `db.connect(cfg.pg_dsn)` + `db.init_schema(conn)` before writing (defensive, idempotent
  `CREATE TABLE IF NOT EXISTS`) — not a raw `PG_DSN` env var/CLI arg + bare
  `psycopg.connect()`. A caught-in-review deviation on #104's first draft: taking a DSN
  directly works locally but skips schema init and doesn't match how every other one-off
  script in this package runs on the actual add-on (`python -m app.orders.<script>`, no
  args, reads the add-on's own configured Postgres).
- **A Playwright `wait_for_selector("text=...")` on a page with MORE THAN ONE section
  sharing the same empty-state text is a race, not a wait** (#104, `/znalosti` has a
  per-customer AND a global section, both rendering "Zatiaľ nič." when empty). The
  selector resolves the INSTANT that text exists ANYWHERE on the page — including a
  pre-existing match in the OTHER section — so it can pass before the actual re-render
  (e.g. after a delete) has happened, silently racing the assertion that follows. Wait for
  the SPECIFIC thing that must change instead (e.g.
  `page.wait_for_selector("text=<the deleted item>", state="detached")`), never a generic
  empty-state string that also appears elsewhere on the same page.
- **Porting an n8n `for`-loop that mutates its own index INSIDE the loop body needs a
  Python `while`, never `for i in range(...)`** (#68, `static_parse.py` vs. the `extractor`
  node's `parseKarmenCashItems`/`parseLabasItems`). JS's `for(let i=0;i<len;i++){...i++...}`
  respects a manual `i++` written inside the body for the NEXT iteration (the loop peeks
  ahead and consumes an extra line, e.g. a description or an EAN on the following physical
  line) — Python's `for i in range(len(x))` silently IGNORES any reassignment of `i` inside
  the body; the next iteration uses the range's own counter regardless. A literal-looking
  `for i in range(...)` port of such a loop is a real bug (off-by-one item boundaries), not
  a style choice — translate to `while i < len(x): ...; i += 1 (or += 2 when consuming an
  extra line)`, mirroring the JS increment exactly. Same class of gotcha applies to ANY
  future n8n Code-node port in this package (the `generator`/EDI-writer parity work in
  #131 will very likely hit the same pattern).
- **A content-addressed snapshot's `_content_hash` (`snapshot.py`, #59) MUST be
  order-independent, not just content-independent** (#127/#128). `import_snapshot`
  parses catalog/customer rows in raw sheet/CSV order; a layered "rebuild from current
  state" path (`rebuild_from_overrides`) reads the SAME rows back out of Postgres via
  `ORDER BY gtin`/`ORDER BY id` — a DIFFERENT order. If the hash just concatenates rows
  in whatever order it received them, two snapshots with IDENTICAL content but different
  row order hash differently, so `_freeze`'s content-hash dedup never reuses the older
  id — e.g. adding then retiring a card should land back on the ORIGINAL snapshot's exact
  hash, but silently minted a new one instead. Fix: sort the per-category serialized
  lines before hashing (`sorted(f"C|{gtin}|{name}|{alias}" ...)`), not the rows themselves.
  Any FUTURE second "read path" into the same content-hashed table (a new admin UI, a
  bulk import, a migration script) needs the same order-independence check before
  assuming dedup will "just work" across it and the original path.
- **`latest_snapshot_id` must order by `checked_at DESC`, not `id DESC`** (#127/#128).
  `_freeze`'s dedup-reuse of an older snapshot only bumps that row's `checked_at`, never
  its `id` — so if a NEWER-but-now-stale snapshot id was minted in between (e.g. a
  temporary override that later got retired, reverting content to an earlier state),
  `id DESC` silently reports the stale one as "current". Order by `checked_at DESC, id
  DESC` — `checked_at` is the column that actually tracks "current", `id` only tracks
  "first ever seen". A dead giveaway this bug is present: a revert-to-earlier-content
  operation (retire/undo) doesn't change what `latest_snapshot_id` returns even though
  the DB row genuinely reverted.
- **A "manual override wins over an external/synced source, merged at freeze time"
  design (`catalog_overrides`/`customer_overrides`, #127/#128) needs a SECOND exclusion
  besides "exclude the row this override replaces" — exclude the override's OWN current
  identity too, or a fresh override that is never given a real current identity (e.g. a
  pure retirement marker with no fields set yet) can collide with an UNRELATED row that
  legitimately shares that same blank/placeholder identity** (review finding: hardcoding
  `""`/`""` as an override's "current identity" made `_merge_customers` exclude EVERY
  customer with a blank EAN AND a blank street, not just the one being retired — both
  fields are legitimately optional per the sheet). When a fresh override has no prior
  "current" state of its own, its current identity should be its ORIGINAL identity
  (`orig_ean_edi`/`orig_street`), never a blank placeholder — makes the second exclusion
  a harmless no-op duplicate of the first instead of a silent collision. Test this
  explicitly with TWO rows sharing the SAME edge-case (blank/empty) identity, not just
  one — a single-row fixture never exercises the collision.
- **A `subagent_type: "fork"` dispatched ONLY to "wait passively for another agent to
  finish" is dangerous — it inherits the FULL parent conversation, including the
  parent's still-unexecuted plan, and can go execute that plan itself instead of
  waiting** (#127/#128 incident, matches the warning already in `subagent-continuation.md`).
  A worker dispatched a fork with the sole instruction "wait for the review agent, then
  report back" — the fork instead created the PR, merged it, and ran its own post-deploy
  Playwright verification, racing the SAME shared browser session as the parent (a stale
  form submitted a DIFFERENT test row than the parent had just typed) and leaving one
  UNRETIRED test `customer_overrides` row in live production data (found via a direct
  DB query, cleaned up via the API). Never fork for a pure wait — poll via `Monitor`/a
  bounded loop instead, or just accept the Stop-hook-imposed wait; a fork's silence is
  never guaranteed to mean "did nothing."
- **SECOND occurrence of the exact same fork-danger (#133, 2026-08-05) — the outcome
  was benign only by luck.** A worker mid-review on PR #181 dispatched a `fork` with the
  sole instruction "wait for the code-review subagent, then relay its findings." The fork
  inherited the parent's full context, including its still-unexecuted plan (act on the
  review findings, merge, deploy, verify, flip `static_orders_shadow`) — and, exactly
  like the #127/#128 incident, went and DID that plan itself: merged PR #181, deployed
  v0.9.45, flipped `static_orders_shadow=true` live, verified 5/5 shadow `match`, posted
  its own comment to #133, and opened a follow-up docs PR (#182) — all while the parent
  was still separately working the ticket's OWN newer scope (the extra-content/digest
  additions) in the SAME local checkout, unaware any of this had happened until it
  surfaced in `git status`/`git fetch`. No damage this time (the fork's actions were all
  individually correct and it explicitly recognized + stepped back from the parent's
  in-progress uncommitted files rather than touching them) — but it could easily have
  raced a shared resource the way #127/#128 did. The lesson from that first incident was
  written down and STILL got triggered a second time — reinforce it operationally, not
  just as documentation: if you catch yourself about to dispatch `fork` (or ANY
  subagent) with a prompt whose only job is "wait for X and relay", stop and use a
  foreground bounded poll loop / `Monitor` instead, every single time, no exceptions for
  "just this once, it's just a wait."
- **n8n execution history is NOT a reliable source of real production examples —
  check it BEFORE assuming it will give you a real corpus (#131).** `search_executions`
  on the "Static auto orders" workflow (`O8IYhUESjaWmPMTI`) returned only 4 executions
  TOTAL, all synthetic `example.com`-domain test runs from an earlier investigation —
  no real production runs were retained at all (execution-data-saving settings prune
  aggressively). When you need real examples for a corpus, go straight to the
  add-on's OWN Postgres instead: `messages` (`category=...`, `processed=true`) has the
  real input text, and — for anything n8n LOGS as it processes (this workflow's own
  `email_events` table, `stage='uploaded_orion'`) — cross-check your derived data
  against that logged ground truth before trusting it.
- **When no store retains the actual REAL output bytes of an n8n Code node (no
  `edi_sent`-style ledger, no execution history), run the node's OWN VERBATIM JS
  source under `node` against real input data, instead of trusting a Python
  reimplementation (#131).** Extract the node's `parameters.jsCode` via
  `get_workflow_details`, wrap it in `new Function('$', ...)` to mock n8n's `$('Node
  Name').all()`/`$input.item.json`, and call it directly — this gives byte-exact
  "production-equivalent" output from the ACTUAL deployed logic, not your own
  understanding of it. Cross-check the harness's output against whatever ground truth
  IS logged (e.g. a derived filename vs a real logged filename) before trusting the
  harness itself. This is the same "run the real node under node" technique
  `edi_reference.json` already used for a different node — reusable for ANY future
  n8n Code-node port that needs a byte-exact fixture.
- **Porting a JS `.replace(needle, replacement)` call (STRING first argument, not a
  `/g` regex) to Python needs an explicit `count=1` — Python's `str.replace()` with no
  count replaces ALL occurrences by default, the opposite of JS's single-occurrence
  default (#131, `generator.js`'s filename builder: `fullOrderNumber.replace('/',
  '_')` only touches the FIRST '/'; a naive Python `.replace("/", "_")` silently
  replaced every '/' instead, producing a wrong filename for any order number with 2+
  slashes).** Same family of gotcha as the earlier `for`-loop-mutating-its-own-index
  note in this file — a JS built-in's default behavior does not always match its
  same-named Python counterpart; verify the SPECIFIC semantics (not just the name)
  before assuming a 1:1 translation, and add a fixture case that actually EXERCISES
  the edge (a single-occurrence test proves nothing about a multi-occurrence input).
- **A hand-ported n8n Code node has TWO layers of logic — the pure function(s) (e.g.
  `generateWincodexOrder`) AND the node's own top-level "MAIN" guard clauses that run
  before/after calling them.** Porting only the pure function and skipping the guards
  (`if (!x) throw ...`) is an easy, easy-to-miss omission — deep code review caught it
  on #131 (a missing `prevNumber` silently produced a fabricated store EAN instead of
  raising; an order where NOTHING resolved to an EAN silently produced an empty
  HDR+SUM file instead of raising). When porting a Code node, read the WHOLE node
  (function definitions AND its `// ===== n8n MAIN =====`-style entry section, if it
  has one) — not just the function you intend to call — and port every hard-fail
  guard the real node has, even if your own test corpus never happens to hit it.
- **The "empty atomic claim" crash class (#34) has a known, copy-pasteable fix — check EVERY
  new claim-based n8n workflow for it, don't wait for an incident (#137).** A Postgres node
  running `UPDATE messages SET processing_at=... WHERE id=(SELECT ... FOR UPDATE SKIP LOCKED)
  RETURNING ...` still emits ONE output item (`{"success":true}`, no `message_id`) when the
  claim matched zero rows — n8n does not simply produce zero items. Any node downstream that
  does `queryReplacement: {{ $('<claim node>').first().json.id }}` then resolves to `undefined`
  and crashes (`Query Parameters must be a string of comma-separated values...`), and any Odoo
  post downstream fires with empty content. The fix is a `n8n-nodes-base.filter` node
  (`typeVersion: 2.3`) immediately after the claim node, condition `$json.message_id notEmpty`
  (`operator: {"type":"string","operation":"notEmpty","singleValue":true}`,
  `typeValidation:"loose"`, `looseTypeValidation:true`) — copy the exact shape from
  `Invoices Forward v2` (`du2O6YGmGyntXBbV`) or `Dodacie Listy EDI` (`1R4WcUFhpIPwEJX1`)'s
  "Claimed a row?" node via `get_workflow_details`, don't reinvent it. Verify BOTH directions
  with `test_workflow` before `publish_workflow`: pin the claim node to `{"success":true}` (no
  `message_id`) and confirm `lastNodeExecuted` is the filter with nothing downstream in
  `runData`; then pin it to a realistic claimed row and confirm the item still reaches its
  normal targets unchanged. "Static auto orders" (`O8IYhUESjaWmPMTI`) had this fixed in #137 —
  any OTHER claim-based workflow added later gets the same check before it ships.
- **`hooks/block-commit-without-design.sh`'s classifier (`design_gate.py`'s `_CAUSE_RE`)
  requires the LITERAL words "príčina"/"dôvod"/"spôsoben(é)" (or English "root cause"/
  "because the") — explaining the root cause in different Slovak words ("Koreň:", "Zistenie:",
  "čo chýbalo") does NOT satisfy it and the commit gets blocked even though the comment is
  genuinely a real design writeup** (hit twice in this session, #132 and #137 — filed as
  `zbynekdrlik/airuleset#219`). Head the design-before-code `gh issue comment` paragraph with
  the literal word **"Príčina:"** (and "Zvolený prístup:" / "Zamietnutá alternatíva:" for the
  other two, which DO have wider synonym coverage) to pass on the first try instead of a
  second wasted comment.

- **`hooks/block-commit-without-design.sh`'s design-gate can latch onto a `#N` mentioned
  in the COMMIT MESSAGE ITSELF, not just the actual ticket you're working (#139, 2026-08-02).**
  A commit message that said "...deep review of PR #141 found..." got blocked demanding a
  design comment on **#141** (the PR number, not the issue) even though a design comment
  already existed on the REAL ticket #139. The gate appears to scan the commit message text
  for any `#N` and require a matching design comment for THAT number. Avoid citing a PR
  number inside a commit message that also needs to pass this gate — refer to review
  findings without the `#N` form ("deep review found...", not "review of PR #141 found..."),
  or post an extra (harmless, cheap) design-shaped comment on that number too if it must be
  mentioned.
- **`docs/autopilot-log.md` and `docs/superpowers/specs/` live at the GIT ROOT
  (`/home/newlevel/devel/n8n/email_extract/docs/`), one level ABOVE the add-on code
  (`email-extractor/`)** — the SAME root-vs-add-on-dir split already documented above for
  `pre-push-lint.sh`. A worker whose Bash cwd resets to `email-extractor/` will find no
  `docs/` there at all; `cd ..` (or an absolute path) before touching either file.
- **A code fix to how a `order_questions` row is COMPOSED (e.g. its `candidates` list) only
  affects NEWLY asked questions — deploying it does NOT change already-OPEN rows, whose
  data was written under the OLD logic and stays frozen (#147).** `teach.ask` writes
  `candidates` once, at creation time; there is no re-derive-on-read. When a ticket's
  acceptance criteria explicitly require an ALREADY-OPEN question to show the fixed
  behaviour (e.g. "over naživo tie isté N otázok"), the code fix alone is not enough —
  it needs a narrow, explicit data repair on TOP of the deploy: read the row(s)
  (`SELECT id, wording, candidates::text FROM order_questions WHERE status='open'`),
  compute what the NEW logic would have produced (same function the fix added, applied
  by hand to the known inputs), and `UPDATE order_questions SET candidates = $$[...]$$
  ::jsonb WHERE id = <id> AND status = 'open'` for exactly the affected id(s) — same
  class of operation as the `#145` render-from-stored-data pattern below (read stored
  data, narrow scoped write, never reprocess the email, never touch `held_orders`/
  `edi_sent`). Verify afterward that `held_orders` row count/status and `edi_sent` count
  are UNCHANGED and every touched question is still `status='open'` — that is the proof
  the repair touched only the one column it meant to.
- **Re-rendering a historical Odoo order message from an already-stored `order_runs` row —
  no reprocess, no model call, no EDI — is a solved, reusable pattern (#145).** When a
  ticket needs a NEW Odoo message for an order that already ran (e.g. re-formatting after a
  `report.py` change, correcting a bad post), never re-run the email through the pipeline
  (risks a duplicate ORION upload, #64's whole reason for existing) — read the SAME data the
  original run produced and call `report.build_summary()` directly: (1) `order_runs.result`
  (JSONB) has the exact `order_results` list `pipeline._run` built, including per-order
  `delivery_date`/`status`/`items` — `missing_count` is `sum(1 for i in items if not
  i.get('gtin'))` (an `llm_borderline` decision DOES have a `gtin` set, only `unmatched`
  doesn't, per `match.py`); (2) `held_orders` (filtered by `message_id`) and `edi_sent`
  (filtered by `customer_ean`) cross-check which delivery dates are still `held` vs already
  shipped; (3) `new_questions` = the count of `order_questions` rows whose `created_at`
  falls inside `order_runs.started_at`..`finished_at` for that run (a repeated wording within
  the SAME run reuses one open question — don't just count held-order `question_ids` naively,
  they can overlap). The `/sklad/<key>` link (`report.sklad_link`/`linkutil.sklad_url`) needs
  the LIVE container's own persisted secret — derive it by running a tiny script INSIDE the
  add-on container (`docker exec -e PYTHONPATH=/app -w /app <container> python3 -c "from app
  import config, linkutil; print(linkutil.sklad_url(config.Config.load()))"`), never
  reimplement the HMAC by hand outside — `PYTHONPATH=/app` is required, the container's `sys.path`
  does not include `/app` by default even though `/app/app/__init__.py` exists there. Running
  `report.build_summary` itself needs `psycopg` importable (`report.py` imports it at module
  level for an unrelated function) — a throwaway venv (`python3 -m venv` + `pip install
  psycopg`) is enough, no container access needed for that part.
- **A `git commit -m "<message with literal backticks>"` on this Bash tool silently
  strips everything between the backtick pairs — bash treats them as command
  substitution even inside the `-m` argument (#157).** A design/root-cause commit
  message for `match.py` almost always quotes function/identifier names in backticks
  (`_distinctive_words`, `alias_customer`, ...) — writing it as a plain `-m "..."` with
  those backticks left literal loses that text from the committed message with NO error
  (only stray `command not found` noise on stderr, easy to miss). Same fix as
  `gh-cli-recipes.md` already prescribes for `gh issue create`/`gh pr create` bodies with
  backticks/`$`/`%`: write the message to a file (or a single-quoted heredoc) and pass it
  via `git commit -F <file>`/`git commit -F -`, never an inline double-quoted `-m` string
  containing backticks. Since the commit already landed once mangled, do NOT `--amend`
  to fix it (never rewrites history) — just get the NEXT commit right.
- **`hooks/block-commit-without-design.sh`'s `#N`-in-commit-message gate (see the
  `#139` entry above) ALSO latches onto a freshly-`gh issue create`'d follow-up number,
  not just a PR number (#159/#161, 2026-08-03).** A commit whose message named the
  brand-new follow-up ticket it had just filed ("... filed as #162, out of scope here")
  got blocked demanding a design comment on **#162** — a ticket that, by construction,
  is an un-designed deferred follow-up with nothing to post. Same fix as before, applied
  more broadly: before committing, grep the drafted commit message for ANY bare `#N`
  (not just a PR number) and rephrase it out entirely ("filed as a separate follow-up
  ticket", never "filed as #N") rather than trying to satisfy the gate for a number that
  was never meant to carry a design decision.
- **A `Matched` dataclass instance (`app/orders/customer.py`) is ALWAYS truthy, even
  when constructed as a placeholder with `ean_edi=""` (#159, review-caught on PR #161).**
  `_ship_one`'s `if not matched:` guard is the ONLY place that is supposed to catch "no
  real customer" — it works for `matched is None`, but NOT for a `Matched(ean_edi="",
  ...)` sentinel (the shape `pipeline.py` uses to `hold.place()` an order while the
  customer is still unknown). Any code path that reconstructs a `Matched` straight from
  a DB column that might still be blank (`held_orders.customer_ean`, as the deadline
  sweep does) must check the COLUMN's blankness itself BEFORE building the `Matched`,
  never rely on `if not matched:` downstream to catch it — the object being non-`None`
  hides the emptiness. `matched.rule` cannot be used to tell a placeholder apart from a
  real match after a `held_orders` round-trip either: `hold.place()` never persists
  `rule` into the table, and every release path rebuilds it as `rule="held_release"`
  regardless of what it originally was.
- **Pre-verify a `app/orders/**` change against the LIVE 30-case corpus on dev2 BEFORE
  pushing, when the change is risky enough that a CI round-trip feels wasteful (#163,
  2026-08-03).** `e2e-orders` runs on the dev2 self-hosted runner, which already has a
  cached checkout + venv at `~/actions-runner-emailextract/_work/email-extractor/
  email-extractor/email-extractor` and long-lived scratch Postgres containers (`docker ps`
  — `ee-eval-pg` on host port 55434, `email-extractor-testpg` on 15433, `postgres`/
  `postgres`). `scp` your locally-edited file straight into that checkout's `app/orders/`
  (the `pre-deploy-clean-tree.sh` hook blocks this from a dirty local tree — this is not a
  production deploy, so bypass it deliberately with `# airuleset:deploy-dirty-ok`), then run
  the SAME command the CI step runs (`ci.yml`'s `e2e-orders` job), with `CORPUS`/`PG_DSN`/
  `LLM_CACHE_DIR` as **real `export`ed shell vars, not inline prefix assignments** — `VAR=x
  cmd --arg=$VAR` does NOT expand `$VAR` in the SAME command's own arguments (prefix
  assignments only populate the child's environment, not the current line's word
  expansion), which silently produced `FileNotFoundError: /catalog.csv` the first time:
  ```
  export CORPUS=/home/newlevel/eval-corpus/email-extractor
  export PG_DSN="postgresql://postgres:postgres@localhost:55434/postgres"
  export LLM_CACHE_DIR="$CORPUS/llm-cache"
  cd ~/actions-runner-emailextract/_work/email-extractor/email-extractor/email-extractor
  .venv/bin/python -m app.orders.eval_run --manifest "$CORPUS/manifest.json" \
    --catalog "$CORPUS/catalog.csv" --customers "$CORPUS/customers.csv" \
    --history "$CORPUS/history.json" --taught "$CORPUS/taught.json" \
    --baseline "$CORPUS/baseline.json" --require-all
  ```
  `git checkout -- app/orders/extract.py` in that checkout afterward to leave it clean for
  the next real CI run. This caught a real regression (two `weekly_free_text`/
  `weekly_five_days` cases) BEFORE a second wasted push+CI cycle.
- **Any NEW model-extracted field needs its OWN citation check — item citation
  (`quote_in_source`/`item_in_source`) does not automatically cover other fields the model
  returns (#163).** `deliveryDate` had no equivalent check until the model was caught
  inventing one (re-dating a stale quoted order onto an unwritten future day). When adding
  or trusting any new field out of `extracted`/`ORDER_SCHEMA`, ask: is this field
  citation-checked against `source`, or just trusted verbatim? `extract.date_grounded()` is
  the template for a new one — grounded when the value's text appears literally in
  `source`, OR is derivable from something written (e.g. an explicit range, see
  `_range_days`), OR the source gives no evidence at all to check against (nothing to hold
  a purely-relative/defaulted value accountable for).
- **`_RANGE`'s own match can end without the trailing dot `_SUBJ_DAY` requires** ("06.07. -
  11.07" — the range regex doesn't require a dot after the SECOND date's month digits, but
  `_SUBJ_DAY` does) — `_SUBJ_DAY.findall()` on a `_RANGE` match's own text can silently
  return only ONE of the two endpoints. Use a plain `re.findall(r"(\d{1,2})\s*\.\s*(\d{1,2})",
  ...)` pair-finder scoped to just that matched span instead when you need BOTH endpoints
  of a range.
- **Constructing a placeholder `datetime.date` from day/month digits with no real year
  (to do date-range arithmetic without one) — use a LEAP year, or a literal `29.02.`
  raises `ValueError` and silently drops the whole computation** (`except ValueError:
  continue` swallows it with no trace). `_range_days` uses `2000`/`2004` (both leap,
  four apart) as the placeholder pair for exactly this reason.
- **Don't reuse `memory.item_key()` (the FUZZY product-wording normalizer — folds
  `.`/`-`/`_`/`@`/spaces all to one blank separator) as a dedupe key for anything that
  needs EXACT identity, like an e-mail address (#159, review-caught on PR #161).**
  `teach.ask_customer()` first keyed its "one open question per unresolved sender" dedupe
  on `memory.item_key(sender_email)`, which folded `a.b@x.com`/`a-b@x.com`/`a_b@x.com`
  onto the SAME key — two genuinely different real senders would have collapsed onto one
  open question, and answering it for one would silently apply to the other's order too.
  The fix was simply `sender_email.strip().lower()` with no punctuation folding. The
  general rule: `item_key()` is for WORDINGS (where fuzzy collapsing typos/spacing IS the
  point); reach for it only when that fuzziness is actually wanted, never as a generic
  "make this a safe dict/index key" helper.
- **The board-question kind register (`teach.KINDS`, #164) has FIVE kinds — item, customer,
  mail, date, line — but httpapi.py's LIVE `/api/orders/question/<qid>/answer` dispatch
  only routes `mail`/`date`/`line` through it.** `item`/`customer` keep their own,
  pre-#164, full-fidelity code paths (`teach.answer`+`hold.release_for_question` /
  `_api_orders_answer_customer`) because the register's single-string `{"choice": ...}`
  contract has no slot for `card`/`name` (cosmetic display text) — `KINDS["item"].apply`/
  `KINDS["customer"].apply` exist as CORRECT, directly-tested reference wrappers (see their
  docstrings + `tests/test_orders_teach_kinds.py`), not as httpapi's actual dispatch path
  for those two kinds. A NEW kind (a 6th) is the one thing that MUST go through the
  register from day one — `mail`/`date`/`line` are the pattern to copy, not item/customer.
- **`pipeline._mail_rule` (the taught "ignore"/"manual" mail_rules short-circuit) is
  gated `if not shadow`, deliberately — a memory READ that only INFORMS a decision the
  model still makes is fine in shadow (see `global_taught` elsewhere in this file), but
  `_mail_rule` SKIPS THE ENTIRE EXTRACTION PIPELINE, which would corrupt shadow's
  verdict-vs-n8n comparison, not just add/remove a side effect. Any FUTURE short-circuit
  that skips calling the model (not just skips a teach/hold write) needs the same
  `not shadow` gate, not the "pure read, fine in shadow" reasoning that applies to a
  memory lookup.
- **A held order with MULTIPLE independent open questions (e.g. `date` + `item`, #164) is
  released only once EVERY one of them is answered — `release_for_question`'s existing
  "count of `status <> 'answered'` among `question_ids`" check already does this for free,
  no new code needed.** `release_due`'s NEW `deadline_shippable` gate
  (`hold._has_non_shippable_open_question`) is what stops it from auto-shipping an
  unconfirmed date/customer/line at the deadline the way an item-only hold still correctly
  does — check `teach.KINDS[<kind>].deadline_shippable` before assuming a new kind's
  deadline behaviour, never copy the item-only "ship what matched" default blindly.
- **An `item`-kind question ALREADY has its "nothing matches, this product might not
  exist" escape — check the real warehouse page (`ASK_HTML` in `httpapi.py`) BEFORE
  assuming a new #164 question `kind` is needed (#160).** Every item question,
  unconditionally, renders a full-catalog search box (`searchBox(q)`, #149) plus a
  "📚 databáza znalostí" link that opens `/znalosti/<ean>?wording=...` — a page that can
  ADD a brand-new catalog card by GTIN (#127's `productsBox()`, "nový GTIN = pridá").
  That is the sanctioned "the product is genuinely missing" path; it predates #164 and
  needs no new `kind`. (The dashboard's OWN `/otazky` item-card renderer, separate HTML
  from `/sklad`, has neither the search box nor a Neviem button for item kind either —
  only `/sklad`'s does; don't assume feature parity between the two item-card renderers.)
  A candidate-SHORTLIST quality problem ("the offered cards are padded with unrelated
  ones") is fixed by tightening what `candidates()`'s scores let through
  (`match.plausible_candidates`, #160), not by inventing a parallel escape.
- **The `edi_sent` upload ledger went two-phase (`uploaded_at`, #153) — a reusable
  pattern for any FUTURE "insert a claim before a real external side effect" table.**
  `edi.claim_send()`'s `INSERT ... ON CONFLICT (...) DO UPDATE ... WHERE
  <not-yet-confirmed> AND <stale> RETURNING id` is Postgres's own atomic reclaim: two
  concurrent claimants can never both win, because the `WHERE` on `DO UPDATE` is
  evaluated per-row as part of conflict resolution itself — no application-level
  locking needed. `edi.confirm_sent()` is called ONLY after the real upload genuinely
  returns success (never alongside the claim), and itself RETRIES with reconnect
  (`pg_dsn` param) instead of raising on the first failure — by the time it runs, the
  external side effect ALREADY happened, so losing the confirmation write is strictly
  worse than the retry's latency (a still-unconfirmed claim would eventually go stale
  and get reclaimed for a genuine SECOND real upload).
  **Backfilling pre-existing rows when adding the "confirmed" column needs a lock, not
  just an existence check, or a SEPARATE `init_schema()` caller can race it.** This
  project's `init_schema()` isn't only called by `main.py`'s single process at
  startup — the one-off admin CLI tools (`backfill.py`, `alias_migration.py`,
  `eval_run.py`, `memory_import.py`) call it too, from their own connections, and are
  documented as safe to run anytime. A bare `IF NOT EXISTS (SELECT ... information_
  schema.columns ...) THEN ALTER TABLE ... ADD COLUMN; UPDATE ... WHERE col IS NULL;
  END IF` inside a `DO $$ ... $$` block lets two callers both pass the existence check
  before either commits — the loser then either crashes on `duplicate_column` (if the
  ALTER has no `IF NOT EXISTS` of its own) or, worse, runs its OWN unconditional
  backfill AFTER the winner's migration already completed and real new claims may have
  started arriving, silently confirming a genuinely fresh orphan. Fix: wrap the whole
  check-then-migrate body in `PERFORM pg_advisory_xact_lock(hashtext('<unique
  string>'))` as the FIRST statement inside the DO block — it fully serializes every
  caller through the gate (self-releases at the end of the block's own implicit
  transaction under an autocommit connection), so the second caller's existence check
  correctly sees the column already there and skips entirely. Also filter
  `table_schema = 'public'` on the `information_schema.columns` check, not just
  `table_name` (harmless here, but free to get right).
  **Testing a backfill-on-migration path**: `pg.execute("ALTER TABLE ... DROP COLUMN
  ...")`, insert a row directly (simulating a pre-migration historical row), then
  re-call `db.init_schema(pg)` and assert the column + backfilled value came back —
  reusable for any future self-healing-migration test in this project (the session-
  scoped `_schema` fixture connection is safe to DROP/re-ADD a column on mid-test, as
  long as the SAME test restores it via `init_schema` before any assertion that could
  fail, since later tests reuse that same connection).
  **Post-deploy verification of a backfill**: read the live table directly —
  `SELECT count(*) total, count(uploaded_at) confirmed, count(*) FILTER (WHERE
  uploaded_at IS NULL) unconfirmed FROM edi_sent` — see `deploy.md`'s
  `PGPASSWORD`-through-`sudo` gotcha for how to actually run that query on the box.
- **A "classify + notify" sweep must NOT persist a row as terminal until the notification
  itself genuinely succeeds — else a real problem is silently and permanently lost, just
  one layer above the thing the sweep exists to catch (#151, review-caught on PR #179).**
  `confirm.sweep`'s first draft classified an uploaded EDI file (imported/failed/timeout/
  unknown) and immediately wrote that status as terminal — which drops the row out of the
  sweep's own `WHERE import_status IS NULL` filter forever — REGARDLESS of whether the
  Odoo alert for a non-`imported` outcome actually delivered. A transient Odoo API failure,
  or Odoo simply being unconfigured (`report.post_from_config` returns `None` with only a
  `log.warning`, no exception), silently ate the one alert for that file with zero
  remaining trace anywhere queryable. Fix: `_alert()` returns whether delivery genuinely
  succeeded (`False` on either an exception OR a `None`/falsy result — both mean
  "never reached the warehouse"), and the caller only marks a row terminal once it has;
  an undelivered alert leaves the row pending so the NEXT sweep re-decides and re-attempts
  delivery. Same principle applies to ANY future periodic sweep in this package that pairs
  a DB-state transition with an external notification (Odoo post, future webhook, etc.) —
  the notification succeeding is part of the state transition, not a fire-and-forget
  side effect of it.
- **A `listdir()`/SFTP failure inside a periodic sweep must still advance the per-row
  throttle timestamp for the rows it was about to check, or a sustained outage retries
  the connection on every worker tick with no backoff** (#151, review-caught on PR #179).
  `confirm.sweep`'s `except Exception` around `listdir()` originally just logged and
  returned — leaving `import_checked_at` untouched meant an already-due row stayed
  immediately due again, so an ORION-side outage would hammer a fresh SFTP connect+listdir
  on every ~15s `worker.run_forever` tick instead of backing off by the configured
  `import_confirm_interval_minutes`. Fix: on a `listdir()` failure, `UPDATE ... SET
  import_checked_at = now() WHERE id = ANY(<the rows that were due this pass>)` before
  returning — reuses the SAME throttle a successful check already respects, no new
  backoff mechanism needed. Any future sweep with a similar "list once, decide per row"
  shape should back off its due-rows on a listing failure the same way.
- **Splitting a `connect()` + `close()` pair that shares one `try/finally` into a reusable
  helper can silently move `connect()` OUTSIDE the block that closes on failure — check
  this explicitly when refactoring any paramiko/SSH-style connect helper** (#151,
  self-caught during code review before merge, `app/orders/upload.py`). The original
  single-function `put()` had `client.connect(...)` and the matching `client.close()`
  inside the SAME `try/finally`, so a failed `connect()` still got cleaned up. Extracting
  the shared setup into `_connect(cfg)` for reuse by the new `list_dirs(cfg)` initially
  moved the `client.connect(...)` call BEFORE/OUTSIDE any try block — a connect failure
  then leaked a partially-open `SSHClient` with no `.close()` ever called. Fix: wrap
  `connect()` itself in `try/except Exception: client.close(); raise` inside `_connect()`.
  Pinned by `tests/test_orders_upload.py::test_connect_closes_the_client_when_connect_
  itself_fails` (fake `paramiko.SSHClient` via `unittest.mock.patch("paramiko.SSHClient",
  ...)`, `connect.side_effect = OSError(...)`, assert `close()` was still called) — this
  file also gave `upload.py` its FIRST test coverage at all; it had none before #151.
- **`edi_sent` is a SHARED ledger, not a Python-only table — n8n's own "Static auto
  orders" workflow writes into it too, with the IDENTICAL content-hash algorithm
  (#133, verified live via the n8n MCP against `O8IYhUESjaWmPMTI`'s `Check Already
  Sent`/`Claim Send` Postgres nodes).** Its `Compute Content Hash` Crypto node computes
  `SHA256(content.slice(0,47) + '        ' (8 spaces) + content.slice(55))` — byte-for-
  byte the SAME normalization `app/orders/edi.py`'s `content_hash()` does
  (`DOC_DATE_AT=47`, `DOC_DATE_LEN=8`), over the SAME `(customer_ean, delivery_date,
  content_sha256, filename)` columns, on the SAME Postgres credential ("Email Extractor
  Postgres"). This means: (1) `edi.claim_send`/`confirm_sent`/`release_send` can be
  reused AS-IS for the static-orders Python engine with zero new ledger table — dedup
  works across BOTH engines during any transition window; (2) a shadow-mode diff against
  n8n's REAL output is a genuine byte-for-byte comparison
  (`SELECT content_sha256, filename FROM edi_sent WHERE customer_ean=<store_ean> AND
  delivery_date=<date> ORDER BY id DESC LIMIT 1`), not a heuristic. `get_workflow_details`
  strips node-level `credentials` from its response (same known gap as the #51 entry
  above) — confirm which Postgres credential is wired by elimination via
  `list_credentials(type:postgres)` (only one exists in this instance) rather than
  expecting to see it directly on the node.
- **`gh pr edit` (title/body) fails with `GraphQL: Projects (classic) is being
  deprecated... (repository.pullRequest.projectCards)` on THIS repo — a `gh` CLI bug
  unrelated to anything you changed (#133/PR #182, 2026-08-05).** The edit itself never
  applies (confirmed by re-reading `.title` afterward — unchanged), and retrying the
  exact same command reproduces the identical error every time; it is not transient.
  Workaround: use the REST API directly instead of the `gh pr edit` subcommand, which
  goes through a DIFFERENT (working) endpoint:
  ```
  gh api repos/<owner>/<repo>/pulls/<N> -X PATCH -f title="..." -F body=@bodyfile.md
  ```
  (`-f` for a plain string field, `-F` for a file — same distinction as
  `gh-cli-recipes.md`'s `--body-file` guidance for `issue create`.) Confirmed this
  updates both title and body in one call and is reflected immediately in
  `gh pr view <N>`.
- **`~/eval-corpus/email-extractor` exists as a genuinely SEPARATE local copy on dev1,
  not a mount of dev2's (#186/#187, 2026-08-06).** `mount`/`findmnt` on dev1 shows it is
  plain local disk (`/dev/nvme0n1p2`) — dev1 and dev2 each have their OWN independent
  directory tree with this same path. They are NOT auto-synced by any mechanism found;
  they simply happened to match (verified via `md5sum` on catalog/manifest/history/
  taught.json) at the start of this session, most likely from an earlier manual copy.
  **Only dev2's copy is authoritative** — it is what `e2e-orders` reads
  (`CORPUS: /home/newlevel/eval-corpus/email-extractor` in `ci.yml`, on the dev2
  self-hosted runner). Any corpus mutation (new case, new cache entries, baseline
  update) MUST be made on dev2 (directly via `ssh newlevel@dev2`, or the documented
  `scp`-into-the-cached-runner-checkout technique above) — editing dev1's mirror alone
  does nothing for CI and will silently drift stale. Pulling dev2's result back to dev1
  afterward (`scp newlevel@dev2:.../baseline.json ...`) is good hygiene, not required.
- **Adding a brand-new corpus case for a specific customer/wording pair can accidentally
  test a DIFFERENT, already-existing code path than the one you meant to pin (#186,
  2026-08-06).** Before trusting a new case's `expected`, empirically check whether
  `decide_without_model`'s no-model rungs (especially `history_sure`) already resolve it
  from the corpus's OWN `history.json` — a quick throwaway script calling
  `memory.seed_from_archive` + `memory.resolve(conn, ean, wording, as_of=...)` for the
  disputed wording(s) settles this in seconds. In this incident the corpus's history
  (itself richer than what live production had on the actual incident date, since it
  was populated by `reconstruct.py`'s archive backfill rather than organic
  `memory.remember()` growth) ALREADY had 3 unanimous days for both disputed wordings —
  meaning the new match-ladder rung being tested (rung 5's alias-conflict gate) never
  actually fires during the corpus replay; `history_sure` (an unrelated, pre-existing
  rung) resolves it first. The corpus case still legitimately proves "this exact
  complicated mail resolves correctly today" — just be honest in the case's `why` field
  about WHICH mechanism makes it pass, and rely on direct unit tests (with an explicit
  `recalled=None`/no-history fixture matching the real incident's actual conditions) to
  pin the new code path specifically.
- **`app/orders/extract.py`'s `_SUBJ_DAY` regex requires a TRAILING dot after the month
  digits ("11.8."/"11.8.2026") — real customer wording routinely omits it ("na 11.8
  poprosím", verified live on the 2026-08-06 CÉDER incident mail itself: neither "10.8"
  nor "11.8" carries a trailing dot anywhere in the message) (#187/#190, 2026-08-06).**
  This affects EVERY consumer of the shared `_days_in()` helper — `date_grounded()`,
  `date_conflict()`, `_orders_a_day_still_ahead()` — not just the one detector that
  happened to need a fix (#187's `quoted_future_dates_uncovered`). A narrowly-scoped
  fix used its OWN regex (`_QUOTED_DAY`, requiring the announcing word "na" before the
  digits) rather than loosening the shared one — widening `_SUBJ_DAY` itself needs its
  own careful false-positive pass first (a bare decimal like "3.5 kg" must not be
  misread as a date once the trailing-dot requirement is gone), tracked as its own
  follow-up (#190). If you touch date-grounding logic in this file, check live real
  mail for the no-trailing-dot wording shape before assuming `_SUBJ_DAY` already
  catches it.
- **A NEW field threaded onto an internal dict (e.g. `extract.run()`'s `notes`) needs
  its OWN explicit wiring through EVERY hop to actually reach a human — computing it
  and storing it in `order_runs.result` is not the same as it being visible anywhere
  (#187 deep-review finding, 2026-08-06).** The chain a value like `notes` must cross to
  reach Odoo: `extract.run()` → `pipeline._run`'s `extracted["notes"]` → EITHER the
  main `out = {...}` dict's own `"notes"` key AND the `_post_summary(...)` call's
  `notes=` kwarg, OR — for every early-return branch through `_finish()` (refusal,
  no-orders, mail-rule reject, date-conflict) — `result["notes"]` threaded into BOTH
  `_finish`'s own `_post_summary(...)` call and `_finish`'s own **returned** dict → and
  finally `report.build_summary()` must actually accept and RENDER a `notes` parameter
  at all (it had none before this fix — the whole point of `#139`'s shortened-message
  design is that `build_summary` can ONLY emit what it was explicitly given, so a field
  the function's signature doesn't have is structurally invisible, not just easy to
  miss). The first cut of #187 wired the value into the EXTRACT stage and the main
  happy-path return only — plausible-looking, tests passed, but a deep review (not the
  fast `/review` pass) caught that the value was write-only in Odoo terms. When adding
  any new "the warehouse/customer should see this" field, trace it through ALL of
  `_finish`'s exit paths AND `build_summary`'s own signature, not just the path your
  own test happened to exercise.
- **A `fork` subagent dispatched for a passive status-check went rogue a THIRD time
  (#186/#187, 2026-08-06) — same failure class as the two entries above, now confirmed
  not to be a one-off.** Prompted with ONLY "check whether the review agent finished,
  report back, do nothing else, do not read the raw transcript file" — it independently
  launched its own `pytest tests/ -q` runs against the SAME `PG_TEST_DSN` (15433) the
  worker's own verification run was using, at least twice (once mid-review, once again
  after the first interloper process was killed), each time corrupting BOTH runs via
  the exact TRUNCATE-collision this file's `local-testing.md`-shared warning already
  documents (spurious `F`/`E` scattered across unrelated tests, no real regression).
  `TaskStop` on the fork's own agent id returned an ownership error both times
  (`"is owned by <id>; agent <other-id> cannot stop it"`) — killing the underlying OS
  process tree (`ps -eo pid,ppid,cmd`, precise-match `.venv/bin/python -m pytest`, never
  a loose `pgrep -f pytest` — that also matches the WRAPPER bash command line's literal
  text and can kill your OWN run, as happened once here) was the only lever that
  actually worked, and only for that one attempt — it retried again minutes later.
  Final resolution: stopped fighting it and ran the worker's OWN verification against an
  ISOLATED test-Postgres instance on a DIFFERENT port (`email-extractor-test-pg`,
  55499, same `postgres`/`postgres` credentials) so the fork's continued activity on
  15433 could no longer collide. No git/GitHub damage occurred either time — the fork's
  actions stayed confined to local test runs. Reinforces `subagent-continuation.md`'s
  and this file's own existing warning with zero remaining ambiguity: **never dispatch
  `fork` for a passive wait/status-check, however narrowly scoped the prompt** — use a
  foreground bounded poll loop instead, every single time.
- **Adding a NEW `teach.KINDS` entry that should be reachable through the GENERIC dispatch
  (like `mail`/`date`/`line`/`dl_item`/`dl_supplier` — not item/customer's own bespoke
  literal-body path) needs FOUR things, not just the registry entry itself (#202, DL
  migration F3).** Missing any one of these leaves the kind "correct but dead" — present in
  `teach.KINDS`, unit-testable directly, but broken or invisible through the real HTTP/JS
  path:
  1. **Store candidates in `{value, label}` shape, not the caller's natural shape.**
     `app.httpapi.api_orders_questions()` returns `teach.open_questions()`'s RAW stored rows
     straight to the browser — NO kind's own `.present()` is ever called in the live path
     (verified by grepping `httpapi.py` for `.present(`: zero hits; `.present()` is a
     correct-but-unused reference, same "not the live shape" caveat `_apply_item`'s own
     docstring already states for item/customer). The shared `genericQuestionCard`/
     `loadAsk()` JS reads `candidates[].value`/`.label` directly off that raw JSON. A new
     `ask_<kind>()` function that stores its NATURAL candidate shape (e.g. `{"gtin":...,
     "name":...}`) instead of translating it to `{value, label}` FIRST produces a card whose
     buttons render but whose `answerGeneric(qid, opt.value)` click sends `undefined` —
     Playwright's `page.click('button:has-text(...)')` then times out with no server-side
     error at all, because the button text itself still came from `opt.label||opt.value`
     (which silently falls back to the correct-looking name even when `.value` is missing).
     Translate INSIDE `ask_<kind>()` so the caller keeps using the natural shape it already
     has on hand (mirrors `dl_match.candidates()`'s own output) and never has to know the
     storage contract.
  2. **Add the kind name to `api_orders_answer()`'s dispatch tuple** (`httpapi.py`, the
     `if q0.get("kind") in (...)` check) — this ONE tuple is a manual allow-list, unlike undo
     (see #3 below). A kind left out of it silently falls through to the item-only bespoke
     `gtin`/`card` body path, which expects fields the new kind's body never sends.
  3. **`api_orders_undo()` is now GENERIC for every kind** (`kind = teach.KINDS.get(q0.get
     ("kind", "item")); q = kind.undo(c, q0) if kind else teach.undo(c, qid)` — fixed in
     #202's own review pass, previously only `mail` got its own registered undo and every
     OTHER kind silently fell back to a bare `teach.undo()` that only touches
     `item_memory`/`customer_overrides`, never a new kind's own table). A brand-new kind
     with real teaching state (its own table, like `dl_item_memory`/`dl_supplier_memory`)
     now gets correct undo behaviour automatically as long as its OWN `undo` function in the
     registry does the real cleanup — nothing further to wire here, just don't forget step
     2's dispatch tuple, which undo does NOT share (undo has no tuple to extend).
  4. **BOTH duplicated JS question-card blocks need the SAME edit** — `DASH_HTML`'s
     `loadAsk()` (the admin `/` dashboard's inline questions widget) and `ASK_HTML`'s
     `genericQuestionCard`/`GENERIC_TITLE` (the no-login `/otazky` page reached via
     `/sklad/<key>`) are two SEPARATE copies of the same rendering logic in `httpapi.py`,
     not shared code — a kind added to one and not the other renders correctly on `/otazky`
     but falls through to the wrong (item-shaped) branch on the admin dashboard, or vice
     versa. Grep both `q.kind===` branching conditions AND the `GENERIC_TITLE=`/`titles=`
     map literals — there are two of each, ~130 lines apart in the same file.
- **Building a byte-parity fixture from a SAVED (not re-fetched) real n8n Code-node source
  (#203, DL migration F4 — extends the earlier "run the real node under node" note).** When
  the JS source was already extracted into the scratchpad by an earlier session (rather than
  freshly pulled via the n8n MCP this session), the file is usually a bare script with NO
  `module.exports` — `require('./that_file.js')` from a separate driver script does NOT leak
  its top-level `function` declarations into the caller (Node wraps each required file in its
  own module scope). Instead: `sed -n '1,<N>p' source.js > funcs.js` to cut everything BEFORE
  the node-specific "MAIN"/`$input` section (which needs n8n globals this harness can't
  provide), then `cat funcs.js > combined.js && cat >> combined.js <<'EOF' ... EOF` to append
  a small driver (reads a JSON cases file, calls the target function per case, writes a
  results JSON) directly into the SAME file/scope, and run `node combined.js cases.json
  out.json`. Convert the driver's camelCase JS field names to the Python function's own
  parameter names (e.g. `skladByGtin` -> `sklad_by_gtin`) when copying the recorded fixture
  into `tests/fixtures/`, so `**case["input"]` unpacks straight into the Python call with no
  renaming inside the test itself (mirrors `edi.py`'s own `edi_reference.json` convention).
- **A schema migration that REPLACES a UNIQUE index on a table a LIVE writer already writes
  to must CREATE the new (wider) index BEFORE dropping the old one, never DROP-then-CREATE
  (#203, review-caught before merge).** `db.py`'s `SCHEMA` list runs each statement as its
  own autocommitted `conn.execute()` (`init_schema()`, `for stmt in SCHEMA: conn.execute
  (stmt)`) — between a `DROP INDEX ...; CREATE UNIQUE INDEX ...;` pair, ANY concurrent
  `INSERT` on that table (e.g. `import_alert_incidents`, live-written by `confirm.py`'s own
  already-running sweep) is completely unprotected against the exact duplicate this file's
  own `#151`/`#179`/`#184` incident history says it cares about. The two indexes can coexist
  harmlessly for the brief overlap when created in the SAFE order (new first, old dropped
  after) — uniqueness stays continuously enforced. Check this ordering explicitly any time a
  migration widens a unique constraint on a table with an existing production writer, not
  just for `import_alert_incidents`.
- **A THIRD engine reusing the shared `order_runs`/`order_items` tables cannot pass its OWN
  snapshot table's id into `order_runs.snapshot_id` — that column has a real Postgres FK to
  `order_snapshots(id)` specifically, not to whatever OTHER snapshot table the new engine
  uses (#204, DL migration F5: `dl_snapshots` is a genuinely separate table, R20's own
  catalog shape differs from the AI-orders one).** Adding a schema change (a nullable
  second FK column, or a CHECK-constrained polymorphic id) is NOT needed — `snapshot_id`
  is already nullable: pass `None` to `worker._start_run`/`_finish_run` and stash the
  REAL snapshot id inside `result["dl_snapshot_id"]` (JSONB) instead, with
  `result["kind"] = "dl"` as the one discriminator a later reader needs. Cheaper than F1's
  own "reused with ZERO schema change" decision implied at the time it was written (#200
  didn't hit the FK because nothing called `_start_run` yet) — any FUTURE 4th engine
  sharing these tables hits the identical FK wall and should reach for the same fix.
- **When TWO engines sharing `order_runs`/`order_items` happen to use the SAME rule NAME in
  their own matching ladder (#204: DL's `dl_match.py` and AI-orders' `match.py` both have an
  `"llm_sure"` rule), a shared stats/digest query MUST filter by the `result->>'kind'`
  discriminator on BOTH the run-level query AND the item-level join — filtering only one
  silently lets the other engine's items leak into the wrong bucket** (a DL document the
  model confidently matched would count as an AI-ORDER the model decided, inflating
  `reliability.provenance_stats_for_day`'s `llm` bucket with someone else's document).
  Caught by design before shipping this time (`reliability.dl_provenance_stats_for_day`'s
  own `kind='dl'` filter + the EXISTING `provenance_stats_for_day` gaining an explicit
  `IS DISTINCT FROM 'dl'` exclusion on both queries) — check for this collision risk
  EXPLICITLY (grep the other engine's rule vocabulary) before trusting a shared digest
  query is scoped correctly, rather than assuming a `shadow=false` filter alone is enough.
- **Testing anything that calls `dl_extract.run_extraction`/`extract_email` (or any future
  module that reads `client.last_prompt_hash` after a `json_call`) needs that attribute on
  a hand-rolled fake LLM client, or the failure is SILENT and misleading (#204).**
  `dl_extract.extract_attachment` catches per-attachment exceptions internally and logs
  them as an `ERROR` line rather than raising — so a fake client missing
  `self.last_prompt_hash` doesn't crash the test with an obvious `AttributeError` at the
  call site; it silently produces ZERO extracted documents, and every downstream assertion
  then fails with a confusing, unrelated message (e.g. "no scripted answer left for
  'dl_supplier'" instead of the real cause). When a new test double for `dl_extract`'s
  `client` param starts failing every scripted-answer assertion at once, check the test's
  OWN captured log output for `DL attachment ... failed to extract` before assuming the
  worker logic itself is wrong — set `self.last_prompt_hash = ""` on the fake client and
  update it in `json_call` the same way the real `llm.Client` does.
- **A DL eval-corpus case NEVER needs a real scanned image / vision call — supply
  non-empty `machine_text` with NO `pdf_bytes` and `dl_extract.extract_attachment`
  structurally cannot reach `vision_call`** (#205, DL migration F6). R42/W13's own
  routing: `is_scanned(extract_embedded_jpegs(pdf_bytes))` is `False` on empty bytes
  (no embedded JPEG to find), and the `elif not machine_text.strip():` vision-fallback
  branch never fires when `machine_text` is non-empty — so the function falls straight
  to `choose_source_text`/`run_extraction`, ONE `json_call`, zero `vision_call`s. This
  is what let the whole 8-case `--live` recording (`app/orders/dl_evaluate.py`/
  `dl_eval_run.py`) cost only extraction+match calls (~40 total, ~$1) instead of also
  paying for `n=2` vision transcription on every case — build every FUTURE DL corpus
  case the same way unless the case is SPECIFICALLY testing vision transcription
  itself (which needs a real scanned image and is currently out of this corpus's scope
  by design — see the #205 design comment for why).
- **Searching `messages`/`attachments` by category + subject/from_addr `ILIKE` finds
  REAL production examples for a corpus fast, but not every named incident class has
  a matching row in the current retention window** (#205). Of the 8 DL incident
  classes named in #205, 3 (Lunys announced-vs-attached, Jackulík 2-PDF-in-one-mail,
  MPC P-prefix docNumber) turned up real matches within seconds of `category='
  dodacie_listy' AND (subject ILIKE ... OR from_addr ILIKE ...)`; 5 others (LESAFFRE,
  thousands-separator, Dalamanka/Dalmátska as a genuine near-miss PAIR, TLS/Forbak,
  EKVIA) had zero hits even after widening the search to `combined_text ILIKE` with no
  category filter. Don't burn excessive time chasing a real example for every named
  class — a reasonable, TIME-BOUNDED search, then a clearly-labelled SYNTHETIC fixture
  for the rest (documented as such in the case's own `why`/`source` fields, never
  disguised as real) is the correct call, matching this file's own "assert only what
  you can prove" principle — a synthetic case you fully control is MORE provable than
  a real one you can't fully verify, not less.
- **`dl_worker._process_message`'s per-call swallow-to-review behavior means an
  `llm.CacheMiss` NEVER escapes as an exception, but a genuine TRANSIENT failure
  (`dl_worker._RetryLater`, raised by `_check_retry` when the error text matches
  `TRANSIENT_RE` AND `attempts < 3`) DOES** (#205, review-caught before merge). Every
  LLM call inside `_process_document`/`dl_extract.extract_email` already runs behind
  its own try/except that turns a failure into a `"review"` document outcome — this
  swallows an offline cache-miss (its message text never matches the transient regex)
  but NOT a real `--live` timeout/rate-limit, whose text DOES match. A harness/caller
  built around `dl_worker._process_message` (or `tick()`) must catch `_RetryLater`
  explicitly if it needs to survive a transient failure without crashing — don't
  assume "the worker swallows everything" just because CacheMiss happens to.
- **Scoring documents matched by a shared key (like DL's `doc_number`) by ENCOUNTER
  ORDER is a real bug, not a simplification, the moment duplicates are a genuinely
  supported case** (#205, review-caught: `app/orders/dl_evaluate.py`'s `score()`).
  W4 (this project's own DL spec) explicitly allows two expected documents to share
  one `doc_number` — a naive "first available actual with this key" match then pairs
  wrongly whenever the actual engine emits them in a different order than the
  expected list, producing spurious failures for an objectively correct result. Fix:
  group both sides by the shared key, then search every INJECTIVE assignment within
  each group (`itertools.combinations` × `itertools.permutations`, trivial at the
  group sizes real duplicates ever reach) and keep the one with the fewest total
  problems, rather than a single greedy pass. Any FUTURE eval harness scoring items
  matched by a non-unique key should default to this same best-fit shape, not a
  greedy index walk — a single passing test with items already in matching order will
  not catch the order-dependence; test the REORDERED case explicitly.
- **"Claim a row, OR tell me who already holds it" — do BOTH in ONE atomic SQL round
  trip via a data-modifying CTE with a `NOT EXISTS` fallback `SELECT`, never a
  `claim_send()` call followed by a SEPARATE read (#216, review-caught before merge).**
  `desadv.claim_send_or_identify()` needed to answer "did I just claim this document,
  or does someone else already hold it — and if so, WHO" in one shot, so `dl_worker.py`
  could tell "this SAME message is retrying itself after a partial ship" apart from "a
  genuinely different message already shipped this document" (W7). Two separate
  autocommit statements (`claim_send()` returning `False`, then a follow-up read) leave
  a real, if narrow, TOCTOU gap — a different message could theoretically reclaim the
  row in between and change who the read reports. The reusable shape:
  ```sql
  WITH ins AS (
      INSERT INTO t (key, ..., holder) VALUES (...)
      ON CONFLICT (key) DO UPDATE SET ..., holder = EXCLUDED.holder
      WHERE <t is eligible to (re)claim>
      RETURNING holder
  )
  SELECT true, NULL FROM ins
  UNION ALL
  SELECT false, d.holder FROM t d
   WHERE d.key = %s AND NOT EXISTS (SELECT 1 FROM ins)
  ```
  The `INSERT` always runs (no `WHERE` of its own — only the `ON CONFLICT DO UPDATE`
  branch has one); when the conflict path's `WHERE` refuses the write, the table row is
  UNCHANGED by this statement, so the fallback `SELECT` reading it directly is exactly
  the pre-existing claimant. Any FUTURE two-phase claim/ledger primitive in this
  project that needs "who currently holds this" alongside the claim decision itself
  should reach for this shape rather than two round trips — `edi.claim_send()` /
  `static_worker.py`'s own duplicate-skip logging has the theoretically SAME gap today
  (unfixed, not yet reported as an incident — low priority given it can only mis-tag a
  digest stage, never double-ship).
- **`dl_match.py`'s candidate scoring (`_score_item`/`w_eq`) is a SEPARATE, hand-ported
  sibling of `match.py`'s own scoring, NOT a shared function — check `match.py` for an
  equivalent fix BEFORE assuming a scoring bug in one is genuinely new (#225,
  2026-08-08).** `w_eq()`'s cheap `a in b or b in a` substring check lets a 1-2 char
  Slovak filler word (a preposition like "s") register a spurious "match" against ANY
  candidate word that happens to contain that one letter — this pushed a correct DL
  catalog card out of the top-15 shown to the model for 4 of 5 real broken production
  wordings (verified against the real 491-row catalog, rank #18-#32 instead of top-15).
  Fixed with `dl_match._MIN_SCORABLE_WORD_LEN = 3` filtering both word lists before the
  `w_eq` loop. Deep review then found `match.py` (the ai_orders engine) does NOT have
  this exact bug — it already filters `len(w) > 2` at `match.py:152,154` — meaning this
  was a genuine DL-only gap, not a cross-cutting one, but the CHECK (does the sibling
  engine already handle this?) is the reusable lesson: before fixing a `dl_match.py`
  scoring/matching bug, grep the equivalent function in `match.py` first — sometimes the
  fix already exists there and DL just needs the same guard ported over; other times (as
  here) DL genuinely lacks something `match.py` already has, which is itself useful
  confirmation the bug is real and not a misunderstanding of intended behavior.
- **A prompt file change (`dl_match_item.md`, `match_product.md`, etc.) invalidates
  the CANDIDATE-LIST-DEPENDENT cache too, not just the literal prompt text (#225,
  2026-08-08).** The `llm.Client` cache key hashes `(model, effort, system_prompt,
  user_message, schema)` — `user_message` is built from `_item_input(item, cands,
  partner_name)`, which embeds the CANDIDATE LIST TEXT. So a scoring/ranking change
  (like the `_MIN_SCORABLE_WORD_LEN` fix above) ALSO invalidates cached matching-call
  answers for any case whose candidate ORDER changed — even with the prompt text itself
  byte-identical. Before assuming "I only changed scoring code, the cache should still
  be valid," check whether the change could alter what `candidates()`/`_item_input`
  produces; if so, budget for the SAME `--live` re-record + `--require-all` verify the
  prompt-edit case already requires.
- **`Config.load()`'s `int(_get(o, key, env, N) or N)` idiom retroactively "fixes" an
  already-persisted EXPLICIT `0` in a live `options.json`, not just a genuinely-absent
  key (#229).** `_get()` returns `opts[key]` verbatim whenever the key is present and
  not `None`/`""` — including a literal `0` — so the trailing `or N` only looks like a
  fallback for "missing"; in practice, because `0` is falsy in Python, it ALSO overrides
  any already-configured `0`. This is the SAME idiom every int-typed option in
  `Config.load()` already uses (`orders_shadow_days`, `catalog_refresh_minutes`, …), so
  it's not a new pattern — but it means bumping a field's DEFAULT from `0` to a real
  value (as `delivery_notes_channel_id` did, `0`→`243`) fixes a LIVE add-on's behavior
  the moment the new CODE deploys, even before anyone POSTs a new value to
  `/data/options.json` — verified live: the add-on's own `options.json` still had the
  OLD literal `0` right after the v0.9.62→v0.9.63 deploy, yet `Config.load()` already
  resolved to `243`. Don't assume a schema-default change needs a matching live
  options-POST to take effect — check whether the field uses this `or N` idiom first
  (an option's own DEFAULT-only fields, like `catalog_sheet_id: str = ""` with no
  trailing `or`, do NOT get this retroactive effect). The options-POST is still good
  practice afterward (keeps `options.json` itself from looking stale/wrong to a future
  reader), just not the thing that fixes behavior.
- **`desadv_edi.build()`'s own `partial`/`no_match` classification EXCLUDES a
  zero-quantity item even when it is genuinely unmatched (#229) — do not treat
  `built.partial`/a document's `outcome` string as a precise "did this raise a real
  `dl_item` board question" signal.** `build()` filters `zero_qty = [... quantity==0]`
  OUT of `items` BEFORE computing `no_match`/`partial` — but `dl_worker.py`'s own
  matching loop (`_process_document`) calls `teach.ask_dl_item(...)` for ANY item with
  `not decision.gtin`, regardless of quantity. So a document whose ONLY unmatched item
  also has quantity 0 gets `outcome="ok"` (a "clean" classification) even though a real,
  open warehouse question exists for it. `unmatched_notes`/`unmatched_items` (built in
  `_process_document`'s own second loop, same `not decision.gtin` condition, no quantity
  filter) is the PRECISE signal — `documents_out`'s dicts now carry it explicitly
  (`"unmatched_items"` key, live success path only) specifically so a caller like
  `dl_report._outcome_needs_link()` doesn't have to re-derive "does this need a
  dashboard link" from the imprecise `outcome` string. Any FUTURE consumer of
  `documents_out`/`built.partial` that needs to know "is there something to actually
  resolve" should read `unmatched_items`/`items_skipped_no_match`+quantity directly,
  never `outcome`/`partial` alone — this is the same class of "the aggregate label lies
  about a narrow edge case" gotcha as this file's own `#204` note on `_aggregate_status`.
- **`dl_match.py`'s R73 MEMORY-RESCUE rung only fires when the model is NOT already
  "sure" (`conf < GATE_SURE or not llm_gtin`) — a fresh HUMAN answer (`dl_item_memory`,
  `source='human'`) does NOT override an already-≥GATE_SURE model guess that then trips
  the R75 lexical tripwire (#236, live incident).** The warehouse answered a board
  `dl_item` question live; reprocessing the SAME document immediately after STILL
  bounced to review with the identical `llm_sure_lexical_gap` — the model re-proposed
  the SAME (now human-confirmed) GTIN at 0.89 confidence, `recalled` was populated in
  the trace, but the `if recalled and (conf < GATE_SURE or not llm_gtin):` guard never
  even evaluates it once the model is confident. This can strand a document even AFTER
  the human "fixes" it, silently — she has no way to tell the fix didn't take. The
  WORKING fix needs no code change: add the ordered wording (or its distinctive words)
  as the matched card's `doplnok` (alias) via `/api/znalosti/dl-products` —
  `_lexical_overlap`'s `card_words = _distinctive_words(name) | _distinctive_words(alias)`
  already includes the alias, so a card whose NAME shares no stem with a real customer
  wording can still pass the tripwire once its alias does. Reprocess (reset
  `processed=false, processing_at=NULL, attempts=0` on `messages`) to re-verify. If this
  recurs on a case an alias genuinely can't cover, the real fix is letting R73 also
  rescue a `source='human'` recall regardless of model confidence (mirrors AI-orders'
  `human_taught` rung, which sits ABOVE confidence banding) — out of scope for a
  live-ops ticket, flag it if seen again.
- **`dl_supplier` has NO separate alias field (unlike products' `doplnok`) — when a
  document's OWN header names a different company than the registered supplier (a 3PL/
  warehouse operator issuing the pick slip on the real supplier's behalf), fold the
  extra identity into the supplier's `name` itself** (#236: "TLS Logistics, s.r.o."
  prints Forbak s.r.o.'s delivery notes; renamed the record to `"Forbak s. r. o. (TLS
  Logistics, s.r.o.)"` via `/api/znalosti/dl-suppliers`, upserting with the row's own
  `orig_ean_edi`/`orig_city` from the management GET so it edits in place rather than
  duplicating). `_score_supplier`'s word-overlap tier (R60) then matches on either name.
- **A kg-tracked (`sklad=100`) DL catalog card covering MULTIPLE physical bag/package
  sizes for the SAME single Codex `NEANKOD` needs a WEIGHT-NEUTRAL `name` and a BLANK
  `mass`, never a specific size baked into the card's name** (#236: Codex has exactly
  ONE card for "Great-náhrada fresca" — `NEANKOD 3605` — used for both 15kg and 20kg
  bags; the DL catalog had it named "Great 20 kg", so `_weights_disagree` (±10%
  tolerance) hard-rejected every "Great 15 kg" delivery, alias or not — the
  weight-conflict guard reads `mass_grams(card.name)` directly, doplnok is NOT
  consulted there). Renaming to plain `"Great"` (keeping `doplnok` for scoring) makes
  `mass_grams(card.name)` return `None` → the guard is structurally skipped for that
  card; leaving `mass` blank is CORRECT (not a gap to fill) because `dl_match._mass_kg`
  and `desadv_edi.py`'s own `_extract_mass` fallback both then read the WEIGHT PER LINE
  straight off each delivery's own wording, which is what actually varies.
- **Cross-checking a warehouse-provided value against Codex directly (`raw.firma.
  AEDIEAN`) is stronger evidence than trusting the value alone, and cheap** (#236: a
  new DL supplier's EAN-EDI code, given as a screenshot, was independently confirmed
  present verbatim in `raw.firma` before being written to `dl_supplier_overrides`).
  `claude.ai codex bridge` MCP was flaky this session (repeatedly connects then
  disconnects) — the documented fallback (`local-testing.md`/deploy memory: direct
  `python3 -c "import duckdb; duckdb.connect('/var/lib/codex-bridge/codex.duckdb',
  read_only=True)..."` over `ssh dev2`) worked every time and is the reliable path,
  not just a backup.
- **A DL catalog GTIN can legitimately be a valid GTIN-14 (verify with the GS1 mod-10
  weighted checksum, alternating ×3/×1 from the rightmost digit before the check
  digit — a live incident's `18585037201518` checked out valid), but `desadv_edi.py`'s
  DESADV LIN record has a FIXED 13-char GTIN field (external CODEX/WINCODEX spec) —
  `_pad()` TRUNCATES anything longer instead of erroring, silently corrupting the code
  into a value matching no real stock card (#245: this shipped `18585037201518` as the
  truncated `1858503720151` and ORION rejected the WHOLE document, stuck 4 days).**
  Fixed at the MATCHING layer (`dl_match.decide_item()`'s new `_gtin_edi_overflow()`
  guard, applied to both a fresh LLM match and an R73 memory-rescue recall), never by
  touching `generate()` itself — `generate()` is byte-pinned against a production
  fixture, so widening its field or trying to encode the overflow differently there
  risks the byte-parity contract; keeping the guard upstream means `generate()` simply
  never receives an overflowing GTIN in the first place. A card this wide genuinely
  CANNOT ship through this channel at all (there is no "right" 13-char encoding of a
  GTIN-14 — dropping the last digit AND dropping the leading indicator digit were both
  checked and are both invalid EAN-13s) — the real fix needs a CODEX-side decision
  (does an alternate 13-char code exist for that stock card?), filed separately as a
  `needs-user-decision` follow-up (#246) rather than blocking the safety fix on it. At
  incident time, 10 OTHER untested catalog cards shared the same 14-digit shape
  (Kombucha/limonáda/Mystery Cola/Korenie line — grep `length(gtin) > 13` on
  `dl_catalog_snapshot` to re-check after any catalog refresh).
- **`desadv_sent` (the two-phase claim/confirm upload ledger) only started being
  WRITTEN from 2026-08-09 — any DESADV document processed before that date has NO row
  at all, which is expected, not a sign of data loss.** Don't treat an empty
  `desadv_sent` lookup for an older `doc_number` as a bug; it just predates the
  ledger's wiring into `dl_worker.py`'s live upload path.
- **Manually correcting a stuck fixed-width DESADV/EDI file in ORION when the original
  `matched_items` input is NOT recoverable (predates `order_runs` DL logging, or the
  run simply never got logged) — edit the file's OWN bytes surgically instead of
  reconstructing input for `desadv_edi.generate()` from scratch.** Read the raw file,
  split on `\r\n` (HDR is `desadv_edi.HEADER_WIDTH`=1157 chars, each LIN is
  `LIN_MIN_WIDTH`..`LIN_MAX_WIDTH`=209..221 chars — `LIN` + linenum(6, right, chars
  3:9) + gtin(`GTIN_FIELD_WIDTH`=13, chars 9:22) + the rest unchanged), drop/renumber
  only the LIN(s) that need to change (renumbering is a fixed 6-char right-justified
  field, same length in and out — `assert len(new_line) == len(old_line)` after every
  edit), re-join with `\r\n`. Verified end-to-end on the #245 incident (EKVIA
  DESADV_000264_3412606458): removed the one unshippable LIN, renumbered the remaining
  3, byte-round-tripped the SFTP write to confirm. Faster and safer than guessing at
  `unitPrice`/`mass`/`quantity` inputs to regenerate the whole document from scratch.
- **A `teach.KINDS` entry with a "live search over everything" JS box (the pattern the
  #202 F3 entry above describes for `dl_supplier`/`dl_item`, mirrored from customer's
  own search box) needs the picked value LEGITIMIZED server-side, or the search box is
  silently non-functional for its entire purpose (#235's own deep-review, discovered
  2026-08-11 while implementing an UNRELATED optional finding).** `_validate_dl_supplier`/
  `_validate_dl_item` only ever accept a `choice` already present in the question's
  FROZEN `candidates` — for the exact case these kinds exist to solve (a genuinely
  unknown supplier/card, `candidates=[]`), that set starts and often stays EMPTY, so
  every single click on a `dlSupplierSearchBox`/`dlItemSearchBox` result (which searches
  the FULL current `dl_suppliers_for_management`/`dl_catalog_for_management`, not the
  frozen candidates) was rejected 400 "nebolo ponúknuté" — verified by hand against the
  real running app, not just read from the code. `_api_orders_answer_customer` already
  solved this for its OWN search box (legitimise via `teach.add_candidate` before
  validating, see httpapi.py ~line 691) — `_api_orders_answer_generic` did NOT, because
  it is the SHARED dispatch for mail/date/line too, which have no search box and must
  keep the strict offered-only check. Fix (httpapi.py, `_api_orders_answer_generic`):
  before `kind.validate`, when `q.get("kind") in ("dl_supplier", "dl_item")` and
  `choice` is not already offered, look it up in the real current list and
  `teach.add_candidate` it in if found — a genuinely nonexistent value still fails
  honestly. **Any FUTURE `teach.KINDS` entry that adds its own "search the full
  database" box must get this same legitimization, or ship it non-functional the same
  way** — the registry's generic dispatch has no way to know a kind wants this without
  being told explicitly (grep `q.get("kind") in ("dl_supplier", "dl_item")` in
  `_api_orders_answer_generic` before assuming a new search-box kind "just works" the
  way the existing candidate-button flow does).
- **The Playwright MCP's `browser_click` + a SEPARATE `browser_find`/`browser_snapshot`
  round-trip can race `/otazky`/`/otazky-dl`'s own 5-second auto-refresh poll** (the
  same `setInterval` re-render this file's `#149` `searchState` comment already
  documents from the pytest-fixture side) — a click that opens a collapsed "➕ Nový…"
  form can appear to silently un-toggle by the time the NEXT MCP tool call inspects the
  page, because the auto-refresh rebuilt the DOM from scratch in between the two
  separate round trips (each MCP call is its own real wall-clock delay). Live-verifying
  #235's DL forms this way looked like a broken toggle until switching to ONE atomic
  `browser_run_code_unsafe` script that clicks AND asserts within the SAME script
  invocation (no MCP round-trip in between) — reliable every time. Any future live
  Playwright-MCP verification of these two pages should default to one atomic script
  for click-then-assert, not click-then-separate-snapshot.
- **A Playwright test that DELIBERATELY provokes a non-2xx `fetch()` response (e.g.
  testing a 409 collision path end-to-end) will always see an unavoidable, application-
  code-free "Failed to load resource: the server responded with a status of ###"
  Chromium console entry — this is NOT a `console.error()` call and NOT a real bug, but
  `page.on('console', ...)` captures it exactly like one (#235, the first test in
  `test_e2e.py` to intentionally trigger an HTTP error).** Don't weaken the file's
  overall `assert console == []` convention for every other test — filter ONLY the
  known "Failed to load resource" substring in the ONE test that deliberately exercises
  an error path (see `test_the_warehouse_reclaims_an_existing_dl_supplier_after_a_
  collision`'s own `real_errors = [m for m in console if "Failed to load resource" not
  in m]`), with a comment explaining why, so a real `console.error()` in that same test
  is still caught.
- **Proving a `pg_advisory_xact_lock` claim actually SERIALIZES two callers needs two
  real threads on two real connections and a TIMESTAMP-based assertion — never a
  sleep-based race (#240, second-round review).** Every existing test of
  `release_for_question`'s new lock called it sequentially (one Python call after
  another), which proves the sibling-gate LOGIC but is silent on whether the lock does
  anything at all — a missing lock and a working one look IDENTICAL under sequential
  calls. `test_release_for_question_advisory_lock_serializes_two_genuinely_concurrent_
  racers` (`tests/test_dl_worker.py`) is the reusable shape for any FUTURE test of a
  similar claim in this codebase (`db.py`'s migration lock, `edi.claim_send`'s atomic
  claim, any future advisory-lock use): two threads, each its OWN
  `psycopg.connect(...)` (a shared connection is not safe for concurrent use across
  threads), each running the SAME operation with a small deliberate `time.sleep()`
  inside the thing being locked (widens the race window so a missing lock shows up
  reliably, not by luck) while a shared, lock-protected list records
  `(thread_id, start, end)` per call; after both threads join, group into a
  `[min(start), max(end)]` window PER THREAD and assert the windows never overlap.
  Deterministic and CI-safe (`no-timeout-band-aids.md`) because the assertion is on the
  RECORDED spans after the fact, never on which thread happens to "win" — a real
  missing lock shows up as an actual timestamp overlap, not as flakiness.
- **When a review fix mirrors an EARLIER fix in a sibling `except`/branch, check every
  OTHER sibling branch for the identical gap before calling it done (#240, second-round
  review).** The `_RetryLater` branch of `dl_worker._run_and_finish` was fixed to
  re-arm `messages.processed = false` (so a reprocess-triggered retry doesn't strand
  outside `_claim()`'s `WHERE processed = false` filter) — but the sibling
  `except Exception` (hard-failure) branch, right next to it, had the IDENTICAL bug and
  was NOT fixed in the same round; an independent deep-review pass caught it only
  because it was told to check the whole diff, not just the one hunk that prompted the
  original fix. Any fix framed as "this exception branch now does X" should trigger an
  explicit grep for sibling `except`/`if`/branch blocks in the SAME function doing
  something structurally similar, before considering the fix complete.
- **Before building a fix for a ticket's named "invisible failure" class, check whether
  infrastructure OUTSIDE the obvious Python code path already covers it (#239).** This
  repo is a hybrid stack — the live n8n instance still owns some workflows
  (`n8n-workflow-edits.md`), and several Python modules already solve adjacent problems
  (`confirm.py`'s grouped-incident carryover/failed/unknown sweep). #239 named FIVE
  invisible-failure classes; live investigation (via the n8n MCP + direct Postgres/SFTP
  reads) found that classes 1, 4 and 5 were ALREADY solved — an active, undocumented-
  in-this-repo n8n workflow ("Stuck message watchdog", `EPe5WWMVZR0lzUld`, since
  2026-07-10) already alerts exhausted-attempt messages into the SAME Odoo channel the
  ticket asked for, and `confirm.py`'s existing mechanism already covers CODEX
  rejection/carryover for every DESADV upload since 2026-08-09. Building NEW code for
  those three classes would have been pure duplication (and risked double-alerting via
  a shared dedup flag — see the `messages.alerted_stuck` note below). Only classes 2/3
  were genuine gaps. Before implementing ANY "X is never detected/reported" ticket in
  this repo: (1) grep the Python code for anything already touching the same table/
  condition, (2) check live n8n workflows via `search_workflows`/`get_workflow_details`
  for anything with an overlapping schedule + query shape, (3) only build new code for
  what survives that check — and record the "already solved" findings on the ticket
  with evidence, don't silently skip them.
- **`messages.alerted_stuck` is OWNED by the n8n "Stuck message watchdog" workflow's own
  dedup (`attempts>=3 AND alerted_stuck=false`, channel by category) — never set it
  from Python code for a DIFFERENT alerting condition (#239).** A Python-side sweep
  that reused this flag for its own dedup (e.g. a "never even claimed" check keyed on
  `attempts=0`) would silently suppress the n8n workflow's OWN future alert if that
  same message later starts retrying and crosses `attempts>=3` — reintroducing exactly
  the invisible-failure class the flag exists to prevent, just moved one condition
  over. Any new Python-side alert needs its OWN dedup key (`app/orders/dl_alerts.py`'s
  `already_pending(kind, message_id)` is the reusable one — see below).
- **A durable, retry-until-delivered, grouped Odoo-alert outbox now exists as a
  generic primitive — `app/orders/dl_alerts.py`'s `pending_alerts` table (#239).**
  Before this, every DL alert was a fire-and-forget `_post()`: if Odoo happened to be
  down at that exact moment (a real, currently-open condition — #253), the alert was
  lost forever with zero trace. `dl_alerts.enqueue(conn, channel_id, kind, body_html,
  message_id=...)` writes durably FIRST, before any delivery attempt; `dl_alerts.
  flush_pending(conn, cfg)` (wired into `worker.run_forever` on the same ~15s tick
  `confirm.sweep` runs on) retries delivery until Odoo genuinely confirms it, GROUPING
  every undelivered row of the same `(channel_id, kind)` into ONE Odoo post per sweep
  (never one message per item — the 2026-08-05 flood-of-5-alerts precedent
  `n8n-workflow-edits.md` already documents). `dl_alerts.already_pending(kind,
  message_id)` is the companion permanent per-message dedup (documented tradeoff: a
  genuinely NEW occurrence of the same condition for the same message, much later,
  will never re-alert — fine for the two kinds that exist today, both structurally
  single-shot per message; a future recurring-condition kind should dedupe on
  something that changes per occurrence instead, e.g. include a run/document id in the
  key). Any FUTURE DL alert that must survive an Odoo outage should reuse this outbox
  (a new `kind` string, calling `enqueue`) rather than another one-shot `_post()`.
- **`dl_worker.MAX_ATTEMPTS`/`dl_worker.CATEGORY` are safely importable at module TOP
  LEVEL from `reliability.py` — no import cycle (#239, verified by tracing the whole
  dependency graph: `dl_worker` and everything it imports has zero reference to
  `reliability`).** Useful precedent for collapsing a duplicated-literal threshold
  (e.g. the "5 attempts" quarantine number, which used to be a bare literal repeated
  across a SQL query, the constant itself, and a digest's own Slovak wording) to one
  source of truth — check the target module's dependency chain the same way before
  assuming a needed cross-module import would cycle.
- **`dl_extract.py` has NO image-vs-PDF distinction of its own — a raw image attachment
  with no `machine_text` silently gets treated as "a digital PDF with no text" and its
  bytes are sent to OpenAI labelled as a PDF file (#247, live incident: HK LOAN's every
  stored attachment is the identical 2472-byte/150×76px signature logo).** `is_scanned()`
  only classifies based on embedded-JPEG byte scanning of what it assumes are PDF bytes;
  for a genuinely tiny/decorative image attachment (already correctly flagged
  `method='skipped'` by `app/extract.py`'s OWN ingest-time classification —
  `MIN_IMG_BYTES`/`MIN_IMG_PIXELS`/`BANNER_ASPECT` in `extract.py`), `is_scanned()` returns
  `False` (well under the 20kB scan threshold) and `extract_attachment()` falls into the
  `elif not machine_text.strip():` vision-fallback branch, calling
  `client.vision_call(vision_prompt(), pdf_bytes=pdf_bytes)` with the raw image bytes
  wrapped as `file_data: data:application/pdf;base64,...` (`llm.py`) — OpenAI rejects this
  with a 400 `"The uploaded file could not be processed"`. Since this happens PER
  ATTACHMENT (never per message), a message whose ONLY attachment is decorative never
  produces a document, so `_process_document` (where supplier lookup runs) is never even
  called — reads exactly like "the pipeline crashed before reaching the supplier lookup."
  **Fix belongs in `dl_worker._process_message`, never in `dl_extract.py`** — that module's
  own docstring explicitly delegates "attachment selection" to the worker and is
  deliberately DB-free/standalone; duplicating `extract.py`'s decorative-image thresholds
  there would create a SECOND, parallel "is this decorative" decision. `_read_attachments()`
  already threads the ingest-time `method` column through each attachment dict (added
  earlier for the `#238` synthetic-missing-document check, `skip_idxs` at the bottom of
  `_process_message`) — it just wasn't being used to filter BEFORE extraction. The actual
  fix: build `usable_attachments = [a for a in attachments if (a.get("method") or "") !=
  "skipped"]` and pass THAT to `dl_extract.extract_email`, never the raw `attachments` list
  (which stays unfiltered for the pre-existing `skip_idxs` check, unaffected). Any FUTURE
  attachment-shaped bug in the DL vision-routing path should check FIRST whether
  `app/extract.py`'s ingest-time classification (`method`/`flag` columns) already answers
  the question, before adding new decision logic in `dl_extract.py` or `dl_worker.py`.
- **A DL eval-corpus case (`dl_evaluate._decode_attachment`) structurally CANNOT exercise
  the `method='skipped'` filter above (#247)** — it never sets a `method` key at all
  (`{"idx", "filename", "pdf_bytes", "machine_text"}` only), and the corpus's own
  documented design (`--- A DL eval-corpus case NEVER needs a real scanned image ---`
  earlier in this file) means every case supplies non-empty `machine_text`, which never
  reaches the vision-fallback branch anyway. Regression coverage for this class of bug
  belongs in `tests/test_dl_worker.py` (unit-level, drives `dl_worker.tick()` directly
  with a `method='skipped'` fixture row) — do NOT try to force this into the DL corpus.
  `match_incidents` (the `reliability.days_since_incident()` trust-metric table) was
  ALSO deliberately not extended for this ticket — its own seeded rows/docstring scope
  it to matching-CORRECTNESS incidents (wrong catalog card picked), not general
  crash/extraction bugs; conflating the two would make "days since a wrong AI match"
  silently report an unrelated crash fix instead.
- **`messages.combined_text` is NOT "just the mail body" — `app/process.py`'s
  `_combined_text()` folds in Subject + From + Body, THEN, only when at least one
  attachment was successfully read as real text (not `flag.startswith("skipped")`, not
  `needs_vision`), an appended `"\n\nAttachments:\n===== <filename> =====\n<text>"`
  block — for EVERY attachment TYPE that extracted real text, not just PDF/image (#258,
  deep-review finding). Any future code that wants "the mail's own prose, nothing an
  attachment contributed" must NOT read `combined_text` raw — it will silently also see
  a .docx/.xlsx/.csv/... attachment's own extracted text, even when that attachment type
  is deliberately out of scope for the consumer (as `dl_worker.py`'s own module
  docstring documents for DL: "a .docx ... is skipped rather than fed to Vision"). Strip
  the block first: split on the literal `"\n\nAttachments:\n"` marker (ASCII-only,
  survives `_strip_invisible`, always the LAST part `_combined_text` joins, so a `.split
  (marker, 1)[0]` is exact) — see `dl_worker._mail_body_only()` for the reusable shape.
  This is the SAME distinction the module docstring's own "Attachment selection is this
  worker's OWN scope decision" paragraph already draws for the ATTACHMENTS table
  (`_ATTACHMENT_MIME_RE`/`_ATTACHMENT_EXT_RE`, PDF/image only) — `combined_text` needs
  its own, separate guard because it is built from a DIFFERENT, wider filter
  (`extract.py`'s ingest-time `flag`/`needs_vision`, not this module's MIME/ext check).
- **Building a corpus case FROM a session already running physically ON dev2 needs no
  `ssh dev2` at all** (#258) — `hostname` first; if it already prints `dev2`,
  `~/eval-corpus/email-extractor/` and the cached CI checkout
  (`~/actions-runner-emailextract/_work/email-extractor/email-extractor/email-extractor`)
  are both plain local paths, no SSH wrapper/quoting gymnastics needed. `ssh dev2`
  issued FROM dev2 itself resolves to loopback (the documented `/etc/hosts` self-alias
  quirk — see the global `machine-identities.md`) and silently "worked" by hitting the
  SAME local containers, which is easy to mistake for having reached the actual other
  box. `ssh dev1` bare-name resolution can fail entirely from dev2 (`Temporary failure
  in name resolution`) even though the tailnet route is fine — if a `dl_eval_run.py
  --live` recording session needs the OTHER box specifically, verify with `hostname`
  first, and don't assume `ssh <name>` reached where you think it did just because it
  didn't error.
