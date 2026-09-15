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

## Lane 5 — Zákazníci (spoločné) + Dodávatelia (sklad) (#446): reuse the partner engines, one JS per two tabs

- **Tab → API + scope mapping:** `zakaznici` → `/api/board/customers`, scope `customers`;
  `dodavatelia` → `/api/board/suppliers`, scope `suppliers`. Both share ONE template
  (`board/partners.html`) + ONE JS module (`static/board/tab-partners.js`) — `tab-partners.js`
  reads `#board-main[data-scope]` and drives the field set / grouping / API from a `CFG` object.
  Registered via `_TAB_CONTENT` rows + `register_customers`/`register_suppliers` on the ONE board
  blueprint (never a second app).
- **DELEGATE to the existing engines, never copy the /znalosti route logic (and never touch
  `httpapi_znalosti.py` — lane 4 refactors it in parallel).** The board services call the SAME
  functions the /znalosti routes call: `snapshot.customers_for_management`/`upsert_customer`/
  `retire_customer` (customers) and `dl_snapshot.dl_suppliers_for_management`/`upsert_dl_supplier`/
  `retire_dl_supplier` (suppliers). All are keyword-only; the surrogate `id` is the pk (NOT the EAN,
  which repeats across branches). `upsert_*`/`retire_*` do NOT rebuild the snapshot or write audit —
  the CALLER does both (as /znalosti does): `snapshot.rebuild_from_overrides` / `dl_snapshot.
  dl_rebuild_from_overrides`, then `audit.record`.
- **The board audits create/update TOO (the legacy /znalosti create/update did NOT).** Spec §5
  wants every change in the Kôš, so `save_*` classifies `create` vs `update` by whether an override
  row already existed for the identity (`override_id`, else `(orig_ean_edi, orig_city|street)`),
  captures the business-column `before` dict, and records `action=create|update` with `before`/`after`.
  Both override tables are already in `audit._SOFT_DELETE_TABLES` + `_SNAPSHOT_TABLES` (r16 added
  `deleted_at`), so the existing lane-3 restore reverts a board create/update/delete with no new
  restore branch. **No migration needed** — highest revision is 16.
- **Scanner-address strip (#407) lives in the SUPPLIER service, before any save** — reuse
  `dl_questions.is_scanner_sender(cfg, email)` (the single guard), never re-parse the config list.
  Customers have NO scanner concept (that guard is DL-only).
- **Family grouping (#435) reuses `customer.name_stem`** (public alias added for #446 — a pure
  re-export of `_name_stem`, corpus-neutral, never re-derive the diacritic folding, #265). An EMPTY
  stem (all-generic name) NEVER groups — it becomes its own singleton family, so two unrelated
  all-generic orgs are never welded together. Customers page over FAMILIES; suppliers are a flat list.
- **`services/partners.py` stays a SHARED-helper leaf** (PartnerError, name+EAN validation, a generic
  single-row read, folded match) so each entity service (`services/customers.py`,
  `services/suppliers.py`) stays ≤200 lines (spec §3). A single combined partners service hit 282
  lines — split by entity, not by trimming docstrings.
- **`edi_sent.customer_ean` / `desadv_sent.supplier_ean` are the „used by" counters** (orders shipped
  / DLs shipped) — a `GROUP BY <ean>` map joined onto each card. There is no FK table; count by EAN.
- **`_retry_unknown_customer_questions` / `release_for_supplier_card` are called best-effort** after a
  save (same as /znalosti — a saved card may unstick an open question), wrapped in try/except so a
  reprocess failure never fails the save (#323).
