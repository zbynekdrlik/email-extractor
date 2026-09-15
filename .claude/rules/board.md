---
paths:
  - "email-extractor/app/board/**"
  - "email-extractor/app/templates/board/**"
  - "email-extractor/app/static/board/**"
---

# The unified nástenka `/nastenka` (#441 epic, board redesign — spec `docs/superpowers/specs/2026-09-15-board-redesign-design.md`)

One warehouse+orders webpage with tabs (Otázky sklad · Otázky objednávky · Produkty
sklad · Produkty objednávky · Naučené sklad · Naučené objednávky · Zákazníci ·
Dodávatelia · História objednávok · História dodacích listov · Kôš; admin also „Maily"),
replacing the three old entrypoints (`/otazky`, `/otazky-dl`, `/znalosti`). Approach A
(owner-approved): Flask + Jinja templates IN FILES + vanilla JS ES modules, no build step,
no CDN, no new runtime dependency. Rolled out lane-by-lane (spec §8); the new board runs
ALONGSIDE the old pages until the warehouse confirms, then lane 8 retires them.

## Architecture rules (spec §3) — obey when adding any tab/route/service

- **This is the ONE Flask BLUEPRINT in the app.** Every OTHER module uses
  `register(app, deps)` (see `httpapi.py`'s own docstring — it deliberately avoids
  blueprints). The board is the sanctioned exception for the new subsystem. `httpapi.py`
  imports `register_board` + `board_gate` and (a) delegates `/nastenka*` + `/api/board/*`
  in `_gate` to `board.auth.board_gate()`, (b) calls `register_board(app, deps)` in
  `create_app`. Do NOT convert other modules to blueprints to "match".
- **Route = parse input + call a service + respond. Service = logic + SQL.** No SQL in a
  route, no HTML in Python (templates render via `render_template`; JS/CSS live under
  `app/static/board/`). Each route/service module ≤ ~200 lines.
- **Templates + static ship via the existing `COPY app/ ./app/`** — `Flask(__name__)`
  (name `app.httpapi`) roots `templates/` + `static/` at the `app/` package, so
  `render_template("board/layout.html")` and `/static/board/...` resolve with NO
  Dockerfile change and NO blueprint-level `template_folder`/`static_folder` needed.
- **Call the existing engines, never copy them:** `orders.teach`, `orders.hold`,
  `orders.snapshot`/`dl_snapshot`, `orders.dl_questions`, the `httpapi_znalosti` service
  functions. A tab is a thin view over machinery that already exists.
- **`board/services/audit.py` is a LEAF** (imports only stdlib + psycopg — never
  `app.board` or `app.orders`). That is what lets `orders.teach` import it LAZILY (inside
  `answer`/`undo`) with no import cycle. Keep it a leaf; keep such cross-layer imports lazy.

## The ONE gate — `board/auth.py`

`board_gate()` covers BOTH `/nastenka*` and `/api/board/*` in one place (replacing the old
three regex allow-lists). Role `sklad` = a session from EITHER signed HMAC key
(`SKLAD_ROLE` from `/sklad/<k>`, `SKLAD_DL_ROLE` from `/sklad-dl/<k>` — both keys stay
valid); role `admin` = `session["auth"]` (a dash_password login), ALWAYS unrestricted.
Anyone else → redirect `/login` for a page, 401 for an `/api/` path. **The DL/orders split
is decided by the TAB, never by which key logged in — a DL session keeps every tab (spec
§6). Never re-narrow the board gate by key.** `auth.actor()` (admin|sklad|anon) is the
audit-log actor. `httpapi._gate` delegates board paths here BEFORE its own session/role
checks (a blueprint `before_request` would never run — the app-level `_gate` redirects
first), so the delegation, not a blueprint hook, is the guard.

## Soft delete + audit (spec §5) — the data doctrine every tab inherits

- **No hard DELETE from any UI path.** A "delete" sets `deleted_at timestamptz` (migrate
  r16, on 9 tables) — the row STAYS, recoverable from the Kôš. Where a legacy `retired`
  boolean exists (the 4 override tables: `catalog_overrides`, `dl_catalog_overrides`,
  `customer_overrides`, `dl_supplier_overrides`) BOTH are set and kept in sync; `retired`
  is NOT dropped. Every snapshot/matching READER treats `deleted_at IS NOT NULL` exactly
  like `retired` — the 4 override loaders compute `(retired OR deleted_at IS NOT NULL)`;
  the memory/rule readers (`item_memory`, `global_item_memory`, `dl_item_memory`,
  `dl_supplier_memory`, `mail_rules`) filter `AND deleted_at IS NULL`. **Any NEW reader of
  these tables must add the same filter, or a soft-deleted row silently re-enters matching.**
- **`audit_log` (migrate r15) records every change** made through the board AND the existing
  `teach.answer`/`undo` paths, via `audit.record(conn, actor=, table=, row_id=, action=,
  before=, after=, ...)`. `row_id` is TEXT (tables key on mixed pk types) — record the REAL
  primary key the restore path uses (for `customer_overrides`/`dl_supplier_overrides` that
  is the surrogate `id`, resolved even on an identity-delete — NOT the EAN). `restore()` is
  a lane-1 skeleton (undoes a soft delete by `id::text`/`gtin` from a trusted table
  whitelist; table/pk are trusted literals, `row_id` is a bound param — never interpolate a
  value). Full before/after restore + the Kôš UI land in lane 3.

## Post-deploy verification of a board change

`/nastenka` needs a signed key. Read the live `/sklad/<key>` (derive per `deploy.md`'s
key-derivation recipe, or use the known live key), then in Playwright: `clearCookies()` →
prove `/` redirects to `/login` (clean session) → navigate `/sklad/<key>` (must 302 →
`/nastenka`) → assert `[data-testid="version"]` == `/version`, the tab bar renders, and the
console has ZERO errors/warnings. Confirm the old `/otazky`/`/otazky-dl` still return 200
until lane 8. Read-only — never click/answer a real question on prod.

## Reusable gotchas from lane 1

- **Adding a migration revision (r15/r16 here):** append a NEW `migrate.Revision` to
  `db.REVISIONS` (never edit the frozen baseline); statements must be transaction-safe (no
  `CREATE INDEX CONCURRENTLY`); a new soft-deletable table added to a UI delete path needs
  its column + reader filter + `conftest.py` TRUNCATE-list entry in the SAME commit.
- **The route-map characterization test** (`test_httpapi_characterization.py`) pins every
  route — a new board route must be added to `EXPECTED_ROUTES` in the same commit.
- **The design gate** (`block-commit-without-design.sh`) wants MECHANICAL markers for a
  non-trivial ticket: `Triage:` + `Prístup 1/2/3` + trade-off word (`výhody/nevýhody`) +
  `Architektúra:` header WITH the literal word „štruktúra/topológia" AND a framework word +
  `Shared-benefit:` one-liner-with-value. See `git-commit-hygiene.md` for the full trap list.

## Kôš / História zmien (#444 lane 3) — restore semantics per action

`board/services/audit.py` now holds the FULL `restore(conn, audit_id, by)` (spec §3 names it
"audit_log zápis + vrátenie" — write + list + restore all live here on purpose). `list_audit`
(table/action-group/free-text filter + paging) feeds the `/api/board/audit` list; `restore`
reverts ONE recorded change and ALWAYS appends a NEW append-only `restore` row (history is
never mutated). It dispatches on the audited row's `action`:

- **delete** → clear `deleted_at` (+ `retired=false` on the 4 override tables) + rebuild the
  snapshot for an override table (orders → `snapshot.rebuild_from_overrides`, DL →
  `dl_snapshot.dl_rebuild_from_overrides`). Un-deleting WITHOUT the rebuild leaves the card
  invisible to matching — the rebuild is load-bearing, not cosmetic.
- **create** → soft-delete the created row (`deleted_at=now()`, +`retired=true` where the col
  exists) + rebuild.
- **update** → write the recorded `before` dict back. Column names are validated against
  `information_schema.columns` for that table (never interpolate an unknown name); the pk is a
  bound param, never interpolated; jsonb columns are wrapped in `Json()`.
- **answer** → `teach.undo(question_id)` (drops the taught mapping, reopens the question) —
  the SANCTIONED engine path, never a raw status flip.
- **undo** → re-apply the LAST prior `answer` (its `after` gtin/card) via `teach.answer` — the
  same teach path a human answer takes.
- **reopen** → re-expire the question (`status='expired'`), the inverse of the Otázky-tab
  reopen (lane 2).

Safety invariants (the whole point of the ticket): a restore NEVER uploads to / touches an
ORION ledger (`edi_sent`/`desadv_sent`/`upload`) — it only reverts curated/override/memory
rows and `order_questions` state through engine functions. `restore` of an already-reverted
row → `RestoreError(409)` (a clear no-op, e.g. delete-restore when `deleted_at IS NULL`
already); of a `restore` row itself → refused; of a missing audit id → 404. The endpoint
(`board/trash.py`) maps `RestoreError.status` straight to the HTTP code.

- **audit.py is still a LEAF** — the `orders.snapshot`/`dl_snapshot`/`teach` imports inside
  the restore helpers are LAZY (inside the function body), so no import cycle even though
  `teach` imports `audit` lazily the other way. Keep any future cross-layer restore import lazy.
- **A NEW audit `action` that a future lane records must get its own `restore` branch here**
  (and, if it's a new reversible entity, its pk in `_SOFT_DELETE_TABLES` + a snapshot-rebuild
  entry in `_SNAPSHOT_TABLES` if it feeds a snapshot). Un-handled action → `RestoreError(400)`.
- **A new tab fills in its own `/nastenka/<slug>` route** via its own `register_<tab>(bp, deps)`
  called from `register_board`, rendering `render_board(slug, tab_template="board/<tab>.html")`
  (module-level helper, reused). A specific slug route out-ranks the generic `/nastenka/<tab>`
  placeholder — that IS the lane-by-lane rollout (spec §8). Add every new board route to
  `EXPECTED_ROUTES` in `test_httpapi_characterization.py` in the same commit.
## Lane 2 — Otázky sklad + Otázky objednávky (#443): delegate, never re-implement

- **Tab → scope mapping (do NOT get this backwards):** `otazky-objednavky` = scope
  `orders` = `ORDERS_KINDS` (item/customer/mail/date/line); `otazky-sklad` = scope `dl`
  = `DL_KINDS` (dl_item/dl_supplier — dodacie listy are the warehouse-inbound flow). The
  partition itself is owned by `httpapi_security.ORDERS_KINDS/DL_KINDS` (import-time
  completeness assert) and reused via `services.questions.SCOPE_KINDS` — never re-derive it.
- **Answer/undo DELEGATE to the legacy dispatch — the fleet's "no duplicated business
  logic" rule made mechanical.** `httpapi_orders_questions.register()` now RETURNS
  `{"answer": _answer_dispatch, "undo": _undo_dispatch}` (its two former route bodies,
  lifted to nested functions that take an `allowed_kinds=_UNSET` param); the legacy
  `/api/orders/question/<qid>/answer|undo` routes are thin wrappers that pass nothing
  (so `allowed_kinds` derives from the session role — byte-identical behaviour). `create_app`
  captures that return and hands it to `register_board(app, deps, questions_api=...)`, which
  passes it to `questions_orders.register(bp, deps, questions_api)`. The board's
  answer/undo routes call `questions_api["answer"](qid, allowed_kinds=None)` — **`None` =
  unrestricted**, because spec §6 makes the TAB (not the key) decide scope and `board_gate`
  already authorized the session. A DL-key board session may therefore answer an ORDERS
  question; the legacy endpoint still refuses it (its own per-key kind gate is unchanged).
  When adding a NEW board action that mirrors a legacy one, extract-and-delegate the SAME
  way — never copy the dispatch.
- **`teach.answer`/`teach.KINDS[..].undo` already write the `audit_log` row** (via
  `teach._audit_change`, lazy import of the leaf `board.services.audit`). So the board's
  answer/undo need NO extra audit write; only the genuinely-new `reopen` writes its own
  `audit.record(action="reopen", ...)`.
- **Reopen an EXPIRED question** = `services.questions.reopen`: an atomic
  `UPDATE ... SET status='open', answer=NULL, ..., reminder_sent_at=NULL, escalated_at=NULL
  WHERE id=%s AND status='expired' RETURNING id` (loser of a race matches 0 rows → 409),
  then `hold.reopen_expired(conn, qid)` puts a held order that `close_expired_holds` closed
  with `release_reason='expired'` back to `held` (a DL/no-hold question matches nothing →
  no-op). It deliberately does NOT reset `messages.processed` (the held order kept its
  stored decisions; a reopen must never trigger an LLM re-run) — unlike `unresolve_manually`,
  which DOES reset the message because a manual resolve shipped nothing.
- **`teach.expired_questions(conn, limit, kinds)`** is the new sibling of
  `open_questions`/`recently_taught` — the three status buckets the tab reads (open /
  expired / answered). Add any future status getter the same way, in `teach`, so the
  `_COLS`/`_row` shape lives in ONE place.
- **Original preview WITHOUT widening the legacy `/files` gate:** a pure `sklad` browser
  session cannot open `/files`/`/eml` (they are admin/token-only via `httpapi_files._auth`).
  The board serves its OWN `/api/board/files/<mid>/<int:idx>` + `/api/board/eml/<mid>`,
  gated by `board_gate` PLUS an in-service scope check (`services.questions.file_path`/
  `eml_path` return `None` unless the `mid` CARRIES a question) → the route 404s. This
  reaches an original for the warehouse without touching the legacy token-only routes.
- **`questions.py` (route layer) holds the scope-INDEPENDENT endpoints once** (answer/undo/
  reopen/preview/files/eml act on a qid/mid); `scope` is a `?scope=` query-param on the LIST
  only. `questions_dl.py` carries the DL card-affordance descriptor (the genuinely
  scope-specific part), merged with `questions_orders.ORDERS_CARD_ACTIONS` into the list
  response `meta.card_actions` so `tab-questions.js` renders each kind's buttons from data.
- **Per-tab content template + script:** `board/__init__._TAB_CONTENT` maps a tab slug →
  `(content_template, tab_script, scope)`; `layout.html` `{% include content_template %}`s
  it and loads `tab_script` (falling back to the lane-1 placeholder + `ui.js`). A later lane
  fills in its own tab by adding a row there — no change to `layout.html`.

## Lane 4 — Produkty sklad + Produkty objednávky (#445): delegate to the catalog engines, soft-delete, split the service

- **Tab → scope mapping:** `produkty-objednavky` = scope `orders` (AI-orders catalog,
  `snapshot.py`); `produkty-sklad` = scope `dl` (delivery-note catalog, `dl_snapshot.py`).
  The `sklad` role reaches BOTH via the board gate (DL products were admin-only on `/znalosti`
  before) — decided by the TAB, never the key (spec §6). `scope` is a `?scope=` query-param on
  EVERY products route, validated in the service (`catalog._scope` → `ValueError` → 400).
- **Create/update/delete DELEGATE, never copy.** `services/catalog.py` calls the exact engine
  functions `/znalosti` uses: orders `snapshot.upsert_catalog_card`(alias tri-state) /
  `retire_catalog_card` + `rebuild_from_overrides`; DL `dl_snapshot.upsert_dl_catalog_card` /
  `retire_dl_catalog_card` + `dl_rebuild_from_overrides`. `retire_*` already set BOTH
  `retired`+`deleted_at` (#442), so a board delete is soft by construction. The board ADDS an
  `audit.record` on create/update/delete/alias-add/alias-remove (the old `/znalosti` endpoints
  only audited DELETE) — that is the board doctrine (spec §5: every board change is audited).
- **`catalog_gtin_set` / `rebuild_from_overrides` are a NO-OP without a pre-existing base
  snapshot.** `rebuild_from_overrides` returns None when `latest_snapshot_id is None`; a fresh
  test DB has no `order_snapshots` row, so `catalog_for_management` (merges overrides live)
  shows a newly-created card but `catalog_gtin_set` (reads the frozen snapshot) stays EMPTY. A
  test asserting "deleted card vanishes from `catalog_gtin_set`" must first
  `snapshot._freeze(pg, [{"gtin":..,"name":..,"alias":""}], [])` a base snapshot (see
  `test_board_products._base_snapshot`). In prod there is always a base snapshot, so this is a
  test-only precondition.
- **Per-card ALIASES = the wording→gtin memory rows.** Orders: `global_item_memory` (global) +
  `item_memory` (per-customer, curated sources only); DL: `dl_item_memory` (per-supplier).
  Add/remove DELEGATE to `memory.add_global_alias`/`add_customer_alias`/`delete_global_row`/
  `delete_item_memory_row`. DL had NO curated add/soft-delete helper (teach's `_undo_dl_item`
  hard-DELETEs) — added `dl_memory.add_dl_alias` (source='human', ON CONFLICT DO NOTHING
  RETURNING id) + `dl_memory.delete_dl_item_memory_row` (soft delete, curated-source +
  supplier_ean scoped), the exact parallels of the orders `memory.py` helpers. `dl_memory.
  resolve` already filters `deleted_at IS NULL`, so a soft delete correctly drops the alias
  from matching. Any FUTURE alias write path must go through these, never a raw INSERT/DELETE.
- **Search over „aliasy" = a second query.** `catalog_aliases.alias_gtins(conn, tables, needle)`
  collects DISTINCT (gtin, item_raw) from the scope's memory tables and fold-matches in Python
  (the fleet `_fold` is Python, not SQL). The table names come ONLY from the trusted hardcoded
  `_SCOPES[..]["alias_tables"]` tuple — never request input — so the f-string interpolation is
  safe; keep it that way (never interpolate a user value into the FROM clause).
- **Split the service to stay ≤200 r.** `catalog.py` (list/detail/upsert/delete) +
  `catalog_aliases.py` (alias list/add/remove/search). `products_orders.py` is the route layer
  (all products routes on the ONE board blueprint, `?scope=`); `products_dl.py` is the DL card
  FIELD descriptor (doplnok/mass/sklad/cena) — a data module, no routes, mirroring
  `questions_dl.py`. The list response `meta.fields`/`meta.alias` drive the editor form so
  `tab-products.js` builds each scope from data, no hardcode.
- **DL name-only edit must not wipe mass/sklad/cena.** `dl_snapshot.upsert_dl_catalog_card`
  overwrites ALL fields (unlike the orders alias tri-state), so `catalog._dl_upsert` reads the
  CURRENT card and keeps any field the editor did not send (`_val` fallback) — the JS editor
  prefills them all, but the fallback stops a name-only programmatic call from clearing them.

## Lane 6 — Naučené sklad + Naučené objednávky (#447): one unified row model over 5 „naučené" tables

- **Tab → scope → kinds:** `naucene-objednavky` = scope `orders` = kinds `mail` (`mail_rules`) ·
  `alias` (`item_memory`, curated) · `global` (`global_item_memory`); `naucene-sklad` = scope `dl`
  = kinds `dl_alias` (`dl_item_memory`, curated) · `supplier` (`dl_supplier_memory`). The `?kind=`
  query selects the table; each kind lives in exactly ONE scope, so update/delete derive the scope
  from the kind server-side (`services.rules.scope_of`) — the client never picks a mismatched
  scope. `rules.SCOPE_KINDS`/`_KIND_TABLE`/`KIND_LABELS` are the single source; `table_for(kind)`
  hands `rules_edit` a TRUSTED literal table name for the audit row (never request input).
- **Read vs write split (≤200 r., like lane 4's `catalog.py`+`catalog_aliases.py`):**
  `services/rules.py` = the unified ROW model + list/search/paging + origin joins (READ only);
  `services/rules_edit.py` = update + delete DISPATCH, each DELEGATING to an engine write path.
  `rules_orders.py` = the route layer (all rules routes on the ONE board blueprint, `?scope=`+
  `?kind=`) + the ORDERS editor descriptor; `rules_dl.py` = the DL editor descriptor (data module,
  no routes, mirrors `products_dl.py`). The list `meta` carries `kinds` (chips) + `edit` (the
  kind's editor fields) + each row's `values` (editor prefill) so `tab-rules.js` builds every kind
  from data, no hardcode.
- **Delete is SOFT + audited; matching stops by construction.** Aliases reuse the lane-4 soft-delete
  helpers (`memory.delete_global_row`/`delete_item_memory_row`, `dl_memory.delete_dl_item_memory_row`
  — curated-source + ean scoped). `mail_rules` and `dl_supplier_memory` had NO soft-delete helper
  (teach's `_undo_mail` and `dl_supplier_memory.forget` HARD-delete for the reopen-the-question undo
  flow — a DIFFERENT semantic), so lane 6 ADDED the canonical soft-delete helpers beside them:
  `teach.soft_delete_mail_rule`, `dl_supplier_memory.soft_delete`. Every reader
  (`pipeline._mail_rule`, `memory.resolve`, `dl_memory.resolve`, `dl_supplier_memory.resolve`)
  already filters `deleted_at IS NULL`, so a soft delete drops the rule from matching immediately —
  that IS the RED→GREEN proof. The `audit_log` `_SOFT_DELETE_TABLES` whitelist already lists all 5
  tables, so the lane-3 Kôš restore reverts a lane-6 delete with no audit change.
- **Update is in-place + delegated; an alias update MUST recompute `item_key`.** New engine helpers
  (the canonical write path — never raw memory SQL in the board): `teach.update_mail_rule`
  (validates `action ∈ {ignore,manual}`), `memory.update_global_row`/`update_item_memory_row`,
  `dl_memory.update_dl_item_memory_row` (each recomputes `item_key` from the new wording — else
  `resolve()` keeps matching the OLD normalized key, silently), `dl_supplier_memory.update_by_id`.
  Each returns the PRE-edit `before` dict of REAL column names, recorded as the audit `update`
  before/after so `audit._restore_update` (writes `before` back by validated columns) reverts it.
- **NO create path in lane 6 — a rule is only ever born by ANSWERING a question (the engines).** So
  the #407 scanner-address guard cannot be bypassed from this tab; `dl_supplier_memory.remember`
  still refuses a scanner address, and `update_by_id` edits only ean/name (never `sender_email`),
  so it can't turn a genuine address into a scanner identity. A regression pin proves the guard.
- **Origin link + preview reuse lane 2, don't rebuild it.** A row with a `question_id` links to
  `/nastenka/otazky-objednavky` / `/nastenka/otazky-sklad` (`?q=<message_id>&status=answered`) —
  **NOT `/nastenka/otazky-<scope>`**: the tab SLUGS are `otazky-objednavky`/`otazky-sklad`, while
  the JS `data-scope` is `orders`/`dl`, so `otazky-orders`/`otazky-dl` hard-404 via the
  `board_tab` `_SLUGS` check (a real 🔴 caught in review — map scope→slug, `OTAZKY_SLUG`). Any
  future cross-tab board link must map the scope to the real tab slug, never concatenate the raw
  scope. tab-questions.js now seeds its filter from `location.search` (additive, no-params =
  unchanged) and offers an inline „Originál" via the EXISTING scope-guarded
  `/api/board/questions/<qid>/preview`. Only `mail`/`global` carry a `question_id`; the memory
  kinds (`alias`/`dl_alias`/`supplier`) show source/date origin text only.
- **`e2e-orders`/`e2e-dl` corpora stay byte-identical** — lane 6 changes only UI + audit + new
  read/soft-delete/update paths, never the resolve/match logic itself, so no corpus expectation moves.
