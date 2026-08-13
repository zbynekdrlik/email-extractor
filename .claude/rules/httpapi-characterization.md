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
