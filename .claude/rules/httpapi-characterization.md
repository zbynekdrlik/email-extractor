---
paths:
  - "email-extractor/app/httpapi.py"
  - "email-extractor/app/httpapi_*.py"
  - "email-extractor/tests/test_httpapi_characterization.py"
---

# Characterization tests protecting the `app/httpapi.py` split (#268)

`app/httpapi.py` (2693 lines pre-split) is being split into 9 modules across an
11-step chain (full plan: issue #268's own comment thread). Step 1 added
`tests/test_httpapi_characterization.py` — three tests that pin CURRENT behaviour so a
later step's pure-relocation move can be checked byte-for-byte, not just
behaviourally. Read this before touching either the split OR the characterization
tests themselves.

## Checksum a set of derived/formatted string constants, not the raw template too

When several page constants are built from one shared raw template via `.replace()` /
`.format()` (here: `ASK_HTML`/`ASK_DL_HTML` from `_ASK_HTML_TEMPLATE`), hash the
DERIVED constants, not the raw template as a fourth/extra entry. The derived form
strictly contains the template's own content (any template mutation changes every
derived hash too) **and** additionally catches a bug in the formatting chain itself
(a mistyped placeholder that leaves `__TITLE__` literally in the response — the
template-only hash would miss this). Hashing the template on top is pure double-
counting of the same bytes for zero extra protection. If a future split step adds a
new derived-from-shared-template constant, hash the derivative, not the source.

## Route-table characterization via `app.url_map.iter_rules()`

Exclude Flask's built-in `static` endpoint (`r.endpoint == "static"`) — it's registered
regardless of how `create_app()` is written and carries no split-related signal.
Normalize away `HEAD`/`OPTIONS` from `r.methods` (Flask adds them automatically) so the
comparison is stable, not brittle. Recompute the expected list from the LIVE app
(`.venv/bin/python -c "... app.url_map.iter_rules() ..."`) rather than transcribing it
by hand from source — a hand-transcribed list is exactly the kind of thing this test
exists to catch errors in.

## Proving a characterization test can genuinely fail — perturb the SPECIFIC thing it protects

Before trusting a new characterization test, prove it can fail: back up the file
first (`cp app/httpapi.py /tmp/.../httpapi.py.bak`), make the SMALLEST realistic
mutation of the exact thing the test protects (a route path typo like
`dl-products` -> `dl_products`, ONE character in an HTML constant, a swapped
today/yesterday query), run ONLY that one test, capture the failure output, revert the
mutation, then `diff` the file against the backup to confirm a byte-identical revert
BEFORE committing anything. `diff a b && echo IDENTICAL` is cheap and removes any doubt
that the revert left a stray change behind. Do this for every new characterization
test in a future split step — the whole point of these tests is that they can fail;
one that structurally cannot is worthless.

## The `_db_tx`/`_db` occurrence counts in the #268 plan comment are approximate

The plan's own comment states "42× `with _db(`, 8× `with _db_tx(`" — a live `grep -c`
against `bd9e28e`/HEAD found 43/9 (one more of each). Harmless for step 1 (no test uses
a hardcoded count), but re-`grep` before relying on either number in a later step
rather than trusting the plan comment's arithmetic.

## `test_orders_digest_happy_path_returns_todays_and_yesterdays_provenance_stats` is
## flaky on a non-UTC dev box ~2h/night — a KNOWN pre-existing gap, not a new bug

`_seed_todays_run`'s own docstring already says the endpoint computes "today" via SQL
`now()`, so the seed must use `now()` too — but it doesn't fully follow its own rule:
`order_runs.started_at/finished_at` do use SQL `now()`, but `match_incidents.occurred_on`
is seeded with Python's `datetime.date.today() - datetime.timedelta(days=3)` (the
HOST's LOCAL timezone), while `days_since_incident` is computed by the endpoint against
Postgres's `CURRENT_DATE` (the test container is `Etc/UTC`). On a non-UTC dev box
(this one is `Europe/Bratislava`, CEST = UTC+2), during the ~22:00-24:00 UTC / 00:00-
02:00 CEST window the local date has already rolled to "tomorrow" while Postgres's UTC
date hasn't — `days_since_incident` comes back 2 instead of the asserted `>= 3` and the
test fails with no code change involved. **Never in CI** (GitHub Actions `ubuntu-latest`
runners are UTC, so Python and Postgres always agree there). Filed with full
root-cause evidence + a proposed fix (seed via SQL `CURRENT_DATE - interval '3 days'`,
matching the `order_runs` seed's own already-correct pattern) as
zbynekdrlik/email-extractor#277 — deliberately left OPEN and UNFIXED as a
`needs-user-decision`, since fixing it means editing this exact characterization-test
file mid-split, which the #268 chain's later steps may have opinions about. If you hit
this failure locally (isolated single-file run, or a full-suite run that happens to
straddle the window), check the CURRENT wall-clock time against UTC before assuming
your OWN change broke it — `date` (local) vs `docker exec <test-pg-container> psql -U
postgres -c "SELECT now(), current_date;"` (UTC) settles it in one command.

## Every remaining split step (4-11) is ALSO a pure code-move — expect
## `hooks/pre-push-test-check.sh`'s Gate 1 to block EVERY push in this chain

Gate 1 blocks `git push` when `.py` feature files changed but no test file did. Every
step in this 11-step plan is by design a verbatim relocation with the SAME existing
tests as the only proof (never a new test, never touching
`test_httpapi_characterization.py` — see "PROVING A CHARACTERIZATION TEST" above, and
the #268 plan's own explicit "no legitimate reason to touch those tests" constraint per
step). So Gate 1 will fire on step 4 (templates), step 10 (`orders_questions`), and
every step in between. The sanctioned fix is `[no-test: <reason>]` on the LAST commit
of the push (the hook only reads `git log -1`) — kroky 2-3 used
`[no-test: pure code-move refactor ..., zero logic change — verified by the FULL
pre-existing test suite ... passing UNMODIFIED before and after]`. If a commit's
message needs fixing AFTER `git commit` (to add the tag, or for any other reason),
`git reset --soft HEAD~1` + recommit is this project's sanctioned recovery — never
`--amend` (commit-conventions.md).

## `Deps` (cfg/db/db_tx/data_dir) already exists since krok 5 — reuse it, never
## redefine `_db`/`_db_tx` in a new split module

Krok 5 added a small `Deps` dataclass to `app/httpapi_common.py` (leaf module — no
Flask/DB import, so every `register(app, deps)` module, INCLUDING `httpapi.py`
itself, can import it with zero circular-import risk). `create_app()` builds it ONCE
(`deps = Deps(cfg=cfg, db=_db, db_tx=_db_tx, data_dir=data_dir)`) right after `_busy`
is defined, and passes the SAME object to every `register()` call. Every future split
step (6 `dashboard_data`, 9 `znalosti`, 10 `orders_questions`, 11 cleanup) that moves
routes needing `_db()`/`_db_tx()`/`cfg`/`data_dir` should import this EXISTING `Deps`
and call `deps.db()`/`deps.db_tx()` — never define a second `_db`/`_db_tx` pair, and
never invent a second carrier class. Register each new module's routes at the exact
call-site position the original routes occupied in `create_app()` (route registration
order itself is irrelevant to Flask — only `before_request`/`after_request`/
`errorhandler` ORDER matters, and that section is untouched by every step so far).
Verify the shared-object claim isn't just assumed: build a stub `cfg`
(`types.SimpleNamespace` with the fields `create_app` reads) and wrap each
`register()` to capture the `deps` argument — `all(d is captured[0] for d in
captured)` proves identity without needing a live Postgres connection.

## Verifying `/files/<mid>/<idx>` or `/eml/<mid>` live needs the message_id
## URL-ENCODED — it routinely contains `<`, `>`, `@`

A real email Message-ID (what n8n passes as `mid`, and what `store.message_dir`
resolves against — see its own docstring) looks like `<20260812155446@manaroots.com>`.
Passed raw into a `curl`/browser URL it breaks the path segment. `python3 -c
"import urllib.parse; print(urllib.parse.quote(mid))"` first, then build the URL:
`curl ".../files/$ENC/0?token=$TOKEN"`. A quick way to get a real `mid` + attachment
`idx` to test against: fetch `/api/messages?limit=50` (session-authenticated) for an
item with `has_attachments: true`, then `/api/message/<id>` for its own
`message_id`/`attachments[].idx`.

## Removing a moved helper's LAST use of a module import can silently break an
## existing test that monkeypatches `httpapi.<module>` (krok 6, `db`)

Krok 6 moved `_busy` (and with it, `httpapi.py`'s ONLY remaining use of the `db`
module — `db.active_claim`/`db.CLAIM_STALE_MINUTES`) into `httpapi_dashboard_data.py`.
Two EXISTING tests (`test_httpapi.py::test_a_failing_endpoint_is_logged_and_returns_a_
clean_500`, `test_api.py::test_fix_request_and_its_event_commit_together`) do
`from app import httpapi; monkeypatch.setattr(httpapi.db, "...", broken)` — they
reach the route they actually want to break (`api_imap_failures` in
`httpapi_reports.py`, `api_fix` in `httpapi_fixqueue.py`) only because `httpapi.db`
happens to be the SAME module OBJECT as `httpapi_reports.db`/`httpapi_fixqueue.db`
(Python module singletons — `monkeypatch.setattr` on the shared object patches it
everywhere it's imported). Dropping `from . import db` from `httpapi.py` once its
own last caller moves away makes `httpapi.db` raise `AttributeError` and breaks both
tests — a real behavior-preservation requirement, not a false positive.

**Before removing ANY module-level import in `httpapi.py` that a split step makes
locally unused, grep the test suite for `httpapi\.<name>` (e.g. `grep -rn
"httpapi\.db\|httpapi\.<other>" tests/`)** — a hit means some test reaches a DIFFERENT
module's code through `httpapi.py`'s own namespace as a monkeypatch handle. If a hit
exists, keep the import with `# noqa: F401` and a comment explaining which tests need
it and why patching it still reaches the real code (same module object). Kroky 10-11
(`orders_questions`, cleanup) should run this same grep before dropping
`_role_kinds`'s or any other still-referenced-only-in-tests import.

## ...OR a DIRECT `from app.httpapi import <name>` in a test — not just monkeypatch
## (krok 9, `ZNALOSTI_HTML`)

The `httpapi.<name>` grep above only catches the MONKEYPATCH shape
(`httpapi.db`/`monkeypatch.setattr(httpapi.X, ...)`). Krok 9 hit the OTHER shape: once
`znalosti_page` (the only thing that rendered `ZNALOSTI_HTML`) moved into
`httpapi_znalosti.py`, `ZNALOSTI_HTML` looked locally unused in `httpapi.py` — but
`tests/test_httpapi_characterization.py`'s krok-1 checksum test does
`from app.httpapi import (..., ZNALOSTI_HTML, ...)` directly, because that test's whole
POINT is pinning the pre-split public surface of `app.httpapi` and must stay
unmodified across every later step. Dropping the import broke the full suite live on
the first attempt: `ImportError: cannot import name 'ZNALOSTI_HTML' from
'app.httpapi'` — caught before commit only because the full local suite was run before
committing, not by the targeted checks.

**So the pre-removal check for ANY newly-unused import in `httpapi.py` is TWO greps,
not one:** `grep -rn "httpapi\.<name>" tests/` (monkeypatch reach-through) AND
`grep -rn "from app\.httpapi import" tests/` followed by checking whether `<name>`
appears in any of those import lists (direct import, mainly
`test_httpapi_characterization.py`'s own `ASK_DL_HTML`/`ASK_HTML`/`DASH_HTML`/
`LOGIN_HTML`/`ZNALOSTI_HTML`/`create_app` list — any future step touching one of THOSE
five HTML constants specifically needs this check). A hit in either grep means
`# noqa: F401` + a comment, same shape as `db`. Krok 11 (final `httpapi.py` cleanup +
re-export audit) should run BOTH greps against every symbol it's tempted to drop.

## Krok 10 (`orders_questions`, the riskiest step) landed clean — three reusable notes
## for krok 11 and any future split of this shape

**After krok 10, `httpapi.py` has ZERO remaining `_db()`/`_db_tx()` CALLS — only the
two `def`s survive** (kept solely to build `Deps(cfg=cfg, db=_db, db_tx=_db_tx, ...)`).
Every call site that used to live directly in `create_app()` has now moved into a split
module and goes through `deps.db()`/`deps.db_tx()`. Krok 11's cleanup/re-export audit
should expect this — if a future `grep '_db(' app/httpapi.py` (excluding the two `def`
lines) ever finds a bare call again, something regressed the split.

**Programmatic byte-diff beats eyeballing for a large (400+ line) verbatim move.** For
krok 10 (446 lines, all 4 two-connection pairs + the role/kind boundary in one block),
extracting the pre-move block with `sed -n 'START,ENDp'`, applying the claimed
mechanical substitutions (`_db()`→`deps.db()`, `_db_tx()`→`deps.db_tx()`, `\bcfg\b`→
`deps.cfg`) with a small Python script, and then `diff`-ing the transformed block
against the actual new file gives a mechanical, zero-doubt proof that NOTHING else
changed — far more reliable than reading 446 lines twice looking for a stray edit.
Worth reusing for any future large verbatim-move step (this repo or elsewhere).

**Don't hand-guess import ordering in a new split module — write it, then `ruff check
--fix`.** Ruff's import sort is NOT plain case-sensitive alphabetical (it groups
CONSTANTS/Classes/functions and sorts within each group, e.g. `_EAN_STRIP_RE, Deps,
_parse_emails_field` — a constant, then a class, then a function, alphabetical within
each bucket) — non-obvious enough that a manual guess got it wrong on the first pass.
Cheaper to let `ruff check --fix .` auto-correct than to reason it out by hand.

**Live-verifying a role/kind security boundary post-deploy: clear cookies, hit BOTH
warehouse links, read the actual `kind` values in the JSON body — not just the HTTP
status.** A shared endpoint like `/api/orders/questions` returns 200 for EITHER
warehouse-role session (it's on both `SKLAD_PATHS` and `SKLAD_DL_PATHS`) — the real
boundary is enforced INSIDE the handler via `_role_kinds()`, filtering which `kind`
values come back, not by blocking the path. So a 200-only check proves nothing; fetch
the body and assert the returned `kind`s are exactly the expected subset (confirmed
live on krok 10's deploy: the orders link saw only `customer`/`mail`, the DL link saw
only `dl_item`/`dl_supplier`). Use `page.context().clearCookies()` before switching
between the admin session and either warehouse link, per the existing cookie-jar gotcha
in `deploy.md` — the persistent MCP browser profile will otherwise silently reuse a
still-valid admin cookie and mask a boundary that isn't actually enforcing.
