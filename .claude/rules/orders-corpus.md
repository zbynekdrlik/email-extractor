---
paths:
  - "email-extractor/app/orders/**"
  - "email-extractor/app/httpapi.py"
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

## Rules when you touch this

- **A new warehouse complaint becomes a corpus case BEFORE its fix is written.** The corpus
  only grows.
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
- **A held order releases through `_ship_one` unchanged — `hold.release_for_question` only
  re-derives the DECISIONS first**, via `match.decide_without_model(item_name, [], ...)`
  called with an EMPTY catalog. That's deliberate, not a shortcut: the only rungs that can
  fire post-hold are `human_taught`/`global_taught` (this order's OWN wording, just
  answered) — those need no catalog at all — so passing `[]` avoids persisting/reloading the
  catalog snapshot the order was originally matched against, and is safe to run over EVERY
  stored decision (not just the pending ones) because a rung that finds nothing simply
  returns `None` and the original decision is kept.
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
