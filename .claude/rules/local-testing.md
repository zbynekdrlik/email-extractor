---
paths:
  - "email-extractor/tests/**"
  - "email-extractor/tests/conftest.py"
---

# Running pytest locally against the dev1 test Postgres — never two invocations at once

There are several throwaway `postgres:16` docker containers on dev1 (`docker ps | grep
postgres`) exposed on different host ports (`email-extractor-testpg` on 15433 is the one
used most, `ee-eval-pg`/`ee-test-pg`/others exist too — check which is actually up before
picking a port). Point `PG_TEST_DSN` at one of them:

**In WORKTREE-mode dispatch (parallel `autopilot-worker` fleet rounds, #317), also check
what OTHER SIBLING WORKTREE WORKERS are already using — not just your own history
(#275, 2026-08-13).** Several isolated worktree workers on this repo run concurrently, each
in its own `.claude/worktrees/agent-*/email-extractor` checkout, and each picks its own
`PG_TEST_DSN` independently — nothing coordinates port choice between them. `ps aux | grep
pytest` before starting shows every currently-running sibling's own `PG_TEST_DSN` right in
its command line; picking a port ALREADY in use by a sibling reproduces the exact #164
TRUNCATE-collision risk this file already warns about, just triggered by a DIFFERENT
worker's process instead of your own. Cross-check against BOTH `docker ps -a` (which
containers exist) AND `ps aux | grep pytest` (which ports are actually busy RIGHT NOW)
before picking one.

```
export PG_TEST_DSN="postgresql://postgres:postgres@localhost:15433/postgres"
```

**Never run two `pytest` invocations against the SAME `PG_TEST_DSN` at the same time —
they corrupt each other's runs, not just slow each other down (#164, 2026-08-03).** The
`pg` fixture `TRUNCATE`s the whole schema before every test; a second process's `FOR
UPDATE` transaction (e.g. anything touching `hold.release_for_question`) racing that
`TRUNCATE` produces either a flaky failure in an UNRELATED, otherwise-100%-passing test
(a transient "assert [] == [...]" on a test that passes cleanly alone) or an outright
**hang** — `docker exec <container> psql -U postgres -c "SELECT pid, state, query, now()
- query_start FROM pg_stat_activity WHERE datname='postgres'"` will show one session
`idle in transaction` blocking another's `TRUNCATE`. If you (or a background command
launched earlier) start a second pytest run before the first finished, this is the first
thing to check — not "why did an unrelated test suddenly fail."

The practical rule: track EVERY `run_in_background` pytest/coverage invocation you launch
and always wait for its `EXIT=` marker (or `ps aux | grep pytest` to confirm nothing else
is running) before starting the next one. A background test run and a "quick single-test"
foreground run in the same turn is exactly how this collides.

**A plain FOREGROUND `pytest tests/ -q` call (no `run_in_background: true` at all) can
still silently keep running in the background — the harness auto-backgrounds any Bash
call that exceeds its own ~120s default timeout, even one you never meant to background
(#268 krok 4, 2026-08-13).** The tool result reads "Command did not complete within its
120s timeout and was moved to the background (ID: ...)" and hands you an output-file
path — easy to skim past while composing the next command. If you then start a SECOND
`pytest tests/ -q` (e.g. because you forgot the first is still alive, or wanted to retry
with a longer `timeout` parameter), you now have two concurrent full-suite runs against
the SAME `PG_TEST_DSN` — the exact #164 collision above, just triggered by the harness's
own timeout instead of a deliberate `run_in_background`. Tell: a run's dot-progress shows
scattered `F`/`E` with no obvious cause, and `ps aux | grep pytest` (filter by `cwd`,
`/proc/<pid>/cwd`, since an unrelated project's pytest on the same box is a false
positive) shows more than one `pytest tests/` process against this repo. Fix: `kill -9`
every stray one, confirm `ps aux | grep pytest` is clean, THEN run a single fresh pass —
don't trust a contaminated run's failures as real without first checking for a
concurrent second process.

If you ever DO get a hang: find the blocking backend with the query above and
`SELECT pg_terminate_backend(<pid>)` for the one that's `idle in transaction` — it's safe,
it's always a stray test connection on a throwaway local DB, never anything live.

## A NEW table your feature writes to must be added to the `pg` fixture's own TRUNCATE
## list, or its rows silently leak across tests (#221)

The `pg` fixture in `conftest.py` does NOT `TRUNCATE *` — it names every table
explicitly. Adding a new table in `db.py`'s `SCHEMA` (e.g. a new `_overrides` table
mirroring `catalog_overrides`/`customer_overrides`) is invisible to that list until you
edit it too — nothing errors, the tests just start failing in a way that looks like a
LOGIC bug: an EARLIER test's row (a different name, a different override) shows up in a
LATER test's assertion, because the row was never wiped between tests. The tell:
`KeyError`/`assert X in Y` failures naming data from an unrelated, alphabetically-earlier
test in the same file, or a "must survive untouched" assertion failing on a name you
never wrote in THIS test. Fix: add the new table's name to the `TRUNCATE ... RESTART
IDENTITY CASCADE` list in `conftest.py`'s `pg` fixture in the SAME commit that adds the
table — this is a required companion edit, not an optional cleanup, for any new table a
test is going to write rows into.

## The final "N passed in Xs" summary line can be MISSING from captured output (#160)

`.venv/bin/python -m pytest tests/ -q > out.log 2>&1` on this box has, at least once, ended
the captured file right at the last `[100%]` progress line with NO trailing summary line at
all — even though the run was fully green (exit code 0). Don't treat a missing summary as a
hang, a truncated capture, or a reason to re-run: verify success from **exit code 0 AND zero
`F`/`E`/`s`/`x` characters** in the dot-progress output (`python3 -c "print(collections.
Counter(ch for ch in open('out.log').read() if ch not in '.\n[] %0123456789'))"` — an empty
Counter means every test passed) rather than grepping for `"passed in"`.

## A worktree-isolated worker needs its OWN venv AND its OWN test-Postgres container (#273, 2026-08-13)

A `.claude/worktrees/agent-<id>/` checkout shares only `.git` with the main tree and any
sibling worktrees dispatched in the same fleet round — it has **no `.venv/`** (worktrees
never share working-tree files), so the first thing to do is `python3 -m venv .venv &&
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt` before any test can
run at all.

More importantly: a fleet round can have 2-3 sibling autopilot-workers running full local
suites CONCURRENTLY on the same box, each in its own worktree. `email-extractor-testpg`
(port 15433) is the box's one long-lived, commonly-reused container — check `ps aux |
grep pytest` (not just `docker ps`) before pointing `PG_TEST_DSN` at it; if another
worker's `pytest` is already running against it, the "never two invocations against the
same DSN" rule above applies EQUALLY across separate worktrees, not just separate
invocations in your own session. Reach for a DIFFERENT throwaway container instead
(`email-extractor-test-pg` on port 55499 is already documented in
`n8n-workflow-edits.md`'s fork-danger incident as exactly this kind of isolated fallback;
`ee-eval-pg`/`ee-test-pg` are two more options) — confirm it's genuinely idle first
(`docker exec <container> psql -U postgres -c "SELECT pid, state, query FROM
pg_stat_activity WHERE datname='postgres'"`, only your own `SELECT` should show).

**3 sibling workers each running the full 1503-test suite (incl. Playwright E2E) on the
same 8-core box drives load average past 12 and swap into several GB — a single run can
take ~14 minutes wall-clock this way (vs. seconds for a scoped file like `test_db.py`
alone), and it is genuinely NOT hung, just contended.** Before assuming a stuck run:
check `/proc/<pid>/status` (`State: S` sleeping, not `D`/zombie) and whether CPU time
(`ps -o etimes,time`) is barely accumulating relative to wall-clock elapsed — that pattern
means it's waiting on scheduler/I/O contention, not stuck in a real hang. For a suite that
launches its own subprocess (Playwright's driver spawns a `chrome-headless-shell` child),
`ps --ppid <pid>` shows the live child still consuming CPU, which is the clean way to
confirm real progress instead of a wedge. `uptime`/`free -h` (load average vs `nproc`,
swap usage) is the fast way to confirm "the whole box is just busy" as the explanation
before investigating your own change.

## Multiple parallel worktree-fleet workers need their OWN dedicated test-Postgres container, not the shared ports (#255, 2026-08-13)

During a multi-worker fleet round (several `.claude/worktrees/agent-<id>/` checkouts
running concurrently, each an independent `autopilot-worker`), the well-known ports this
file already documents (`email-extractor-testpg` 15433, `ee-eval-pg` 55434, etc.) can
ALREADY be busy with a sibling worker's own `pytest` run at the exact moment you need to
verify — `ps aux | grep pytest` (filter each hit's own `cd .../agent-<id>/...` prefix in
its command line) is the way to check, not just `docker ps` (a container being UP does
not mean it's currently idle). Rather than wait/retry against a port a sibling might be
using, spin up your OWN throwaway container on an unused port and use that exclusively
for this session's verification:

```
docker run -d --name ee-agent-<your-worktree-id-prefix> -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=postgres -p <free-port>:5432 postgres:16
# then, after a few seconds for it to accept connections:
export PG_TEST_DSN="postgresql://postgres:postgres@localhost:<free-port>/postgres"
```

`db.init_schema()` runs automatically via `conftest.py`'s session-scoped `_schema`
fixture on first use — no manual schema setup needed. This costs nothing extra (a fresh
`postgres:16` container starts in seconds) and completely removes the #164 TRUNCATE-race
risk this file's own top section warns about, for the price of one `docker run`. Also
needs its own venv if the worktree checkout doesn't already have one (`python3 -m venv
.venv && .venv/bin/pip install -q -r requirements.txt -r requirements-dev.txt`) — a
fresh worktree checkout has no `.venv/` of its own, it is gitignored like everywhere else
in this repo.

## `pytest.mark.skipif` in a NEW test line blocks the push, even though `.skip(` is what's actually banned (#224, 2026-08-08)

`hooks/block-test-skips.sh`'s content scanner matches the raw substring `pytest\.mark\.skip`
against every ADDED line in a test file — this also matches `pytest.mark.skipif(...)`, even
though a CONDITIONAL, environment-based skip (not a blanket "this test doesn't run" skip) is
a different thing and is NOT what `test-strictness.md` is banning. The hook fails with
`No stderr output` (a generic wrapper message, not the real reason) rather than a clear
rejection — don't waste time guessing at a crash; check the diff for a fresh
`pytest.mark.skipif`/`pytest.mark.skip` line first. `tests/test_extract.py` already has an
existing (pre-hook, grandfathered) `@pytest.mark.skipif(not _has_ocr(), ...)` for the
tesseract/poppler-dependent OCR tests — do NOT copy that pattern into a NEW test. Since
poppler is an UNCONDITIONAL Dockerfile + CI dependency in this project (same as tesseract),
a new test needing it should assert `shutil.which("pdftoppm")` truthy and let the test FAIL
loudly if it's somehow missing, rather than skip — matches `test-strictness.md`'s own "a
missing dependency must fail loudly, never skip" principle anyway, and sidesteps the hook
entirely (no bypass tag needed for a scoped, well-justified assertion instead of a skip).
