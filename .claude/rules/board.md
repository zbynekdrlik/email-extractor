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
