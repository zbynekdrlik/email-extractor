---
paths:
  - "email-extractor/tests/**"
  - "email-extractor/tests/conftest.py"
---

# Running pytest locally against the test Postgres — never two invocations at once

**This project's checkout + test containers live on dev2, not dev1 (#289, 2026-08-13
— corrected; the global `machine-identities.md` default "most projects live on dev1"
does NOT hold for email-extractor).** Verified live: `hostname` in a session working
this repo returns `dev2`, and `ssh dev2` from inside such a session is a SELF-LOOP
(MagicDNS resolves a node's own name to `127.0.1.1` locally, per
`machine-identities.md`'s own quirk note) — landing back on the same box, same
filesystem, same git state. Don't assume dev1 from the global convention; run
`hostname` once if genuinely unsure which box a session is on before reaching for
`ssh dev1`/`ssh dev2` to "get to" this repo's checkout — you may already be there.

There are several throwaway `postgres:16` docker containers on THIS box (`docker ps |
grep postgres`) exposed on different host ports (`email-extractor-testpg` on 15433 is
the one used most, `ee-eval-pg`/`ee-test-pg`/others exist too — check which is actually
up before picking a port). Point `PG_TEST_DSN` at one of them:

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

## A full local suite that stops advancing (dots stop appearing) mid-run — diagnose via
## `pg_stat_activity`, don't assume it's just contention (integration round A, 2026-08-13)

`local-testing.md`'s existing contention note above ("genuinely NOT hung, just
contended") is real but NOT the only cause of an apparently-stalled run. A genuinely
STUCK run (zero NEW dots for many consecutive minutes, not just slow) is diagnosable in
seconds against the SAME test-Postgres container the suite is using:

```bash
docker exec <test-pg-container> psql -U postgres -c \
  "SELECT pid, state, wait_event_type, wait_event, now()-query_start AS dur, left(query,120) \
   FROM pg_stat_activity WHERE datname='postgres' ORDER BY query_start;"
```

A row stuck `state = 'idle in transaction'` for many minutes with NO `wait_event_type`
(i.e. Postgres is waiting on the CLIENT, not the other way round) — alongside OTHER
rows `active`/`wait_event_type = 'Lock'` on a `TRUNCATE ...`/`INSERT ...` — means an
EARLIER test's connection is holding a row lock open (a `FOR UPDATE`, an advisory lock)
and never committing, which blocks every LATER test's session-scoped schema-reset
fixture. Root cause found live: `tests/test_orders_hold.py::test_two_concurrent_
answers_to_sibling_questions_release_it_exactly_once` spawns two threads and does
`t1.join(timeout=15)`/`t2.join(timeout=15)` — a `timeout=` join does NOT kill the
thread, it only stops WAITING for it. Under heavy CPU contention (several sibling
fleet-worktree pytest runs at once), one thread can stall deep inside `hold.
_release_locked`'s own `with psycopg.connect(...) as tx:` block past its last query,
holding open a `FOR UPDATE` lock on `held_orders` — the TEST FUNCTION returns (its own
30s combined timeout elapses) but the orphaned thread/connection lives on, wedging
every subsequent test that needs the schema truncated. Filed as `#291` (test-infra
robustness gap, not a `hold.py` logic bug — proven by a clean, all-passing 1519-test
re-run once the stuck connection was `pg_terminate_backend`'d and sibling contention
cleared). **Fix for a wedged run: `SELECT pg_terminate_backend(<pid>)` on the stuck
`idle in transaction` backend, then re-run the WHOLE suite fresh** (don't just resume —
a forcibly-killed mid-transaction connection leaves the NEXT several tests in a
misleading `AdminShutdown`/`[BAD]` cascade that looks like new failures but is purely
an artifact of the kill, not evidence of a real bug).

**The `#160` "missing summary line" verification technique (zero `F`/`E`/`s`/`x` in the
captured dot-progress output) is what actually confirms a clean re-run** — this incident
is a second, independent confirmation that trusting dots-reached-100% + an empty
`collections.Counter` is more reliable than waiting for a "N passed in Xs" line that may
never get captured.

## The `#160` Counter technique breaks down the moment there IS a real failure — count
## the `FAILED`/`ERROR` SUMMARY lines, not raw characters in the whole file (integration
## round B, 2026-08-13)

`collections.Counter(ch for ch in open(log).read() if ch in 'FEsx.')` over the ENTIRE
captured file (not just the dot-progress lines) is only safe when the run is CLEAN — a
genuine failure prints a full traceback + this project's own `log.exception`/
`log.warning` output into the same file, and THAT text is riddled with the letters
`F`/`E`/`s`/`x` too (the words "Error", "Exception", "failed", a stack trace's file
paths). A single real test failure inflated one such count to `{'.': 1582, 's': 275,
'x': 41, 'E': 39, 'F': 15}` — reading like 15 distinct failures across the suite, when
there was exactly ONE. Don't panic-diagnose a wide blast radius from an inflated raw
character count: `grep -c "^FAILED\|^ERROR"` (pytest's own short-summary lines, always
prefixed at the start of a line) gives the TRUE failure count regardless of how much
traceback noise surrounds it, and `grep "^FAILED"` names every actually-failed test.
Reserve the raw-Counter technique for confirming a run is clean (zero failures) —
once ANY `FAILED`/`ERROR` line exists, switch to counting those lines instead.

## A `PG_TEST_DSN`-collision symptom that is NOT "scattered F/E" — a clean, CONSISTENT
## `n == 0` with "no DL catalog snapshot yet — DL worker idle" (integration round B,
## 2026-08-13)

The existing collision warning above describes "scattered F/E in an otherwise-passing
run" as the tell for two concurrent `pytest` invocations against the same
`PG_TEST_DSN`. A DIFFERENT, equally real symptom showed up this session: running a
SINGLE targeted test (`-k <name>`) while an EARLIER full-suite background run was
still mid-flight on the SAME port produced a clean, deterministic `assert n == 1`
failure (`n` was `0`) with the log line `WARNING orders.dl_worker: no DL catalog
snapshot yet — DL worker idle` — reproduced identically on TWO unrelated existing
tests run alone this way, both of which pass individually once nothing else is
running. Root cause: the concurrent run's OWN test setup (`pg` fixture) `TRUNCATE`d
`dl_snapshots` mid-way through this test's `_snapshot(pg)` → `dl_worker.tick(...)`
window, wiping the snapshot row this test had just inserted before `tick()` could read
it back. The tell that this is contamination, not a real bug: (1) `ps aux | grep
pytest` shows a SECOND python process against the exact same `PG_TEST_DSN` port, and
(2) a scratch script calling `dl_snapshot.import_snapshot`/`latest_snapshot_id`
directly (bypassing pytest and any concurrent fixture entirely) proves the snapshot
path works fine in isolation. Before trusting ANY single-test-alone failure as real,
run `ps aux | grep pytest` FIRST — this class of failure looks exactly like a genuine
regression (a clean, reproducible assertion failure, not flakiness) and can easily
mislead a fix attempt if taken at face value.

## `#291` shipped a fix — any NEW "two real threads race a real Postgres connection"
## test MUST use `tests/_race.py::run_racers`, never hand-roll `t.join(timeout=...)` again

The wedge mechanism this file already documents above (a stalled thread's
`join(timeout=...)` returning without killing it, leaving a stray Postgres backend
holding a lock that blocks every later test's `pg` fixture TRUNCATE) is now FIXED at
the test-infrastructure level, not just diagnosed. `tests/_race.py::run_racers(pg,
threads, timeout=15, label="...")` replaces the old `t1.start(); t2.start();
t1.join(timeout=15); t2.join(timeout=15)` idiom: it marks every racer thread daemon,
joins with the same bounded timeout, and — the moment ANY thread is still alive past
its join — terminates every stray non-idle backend on the test database (via
`pg_stat_activity` + `pg_terminate_backend`, releasing whatever lock it holds) and
`pytest.fail()`s the CURRENT test loudly instead of silently continuing. All 11
existing racer tests (`test_orders_hold.py`, `test_snapshot.py` x3,
`test_httpapi_new_customer.py`, `test_httpapi_new_dl.py` x2, `test_orders_teach.py`,
`test_dl_worker.py`, `test_dl_snapshot.py` x2) were migrated to it. `tests/
test_race_helper.py` proves the stall-detection+cleanup+fail path actually works
(a deliberately-stalled racer holding a `FOR UPDATE` lock; the helper both fails the
test AND releases the lock, proven by a follow-up `TRUNCATE` succeeding right after).

**Any FUTURE test that races two real threads against real Postgres connections
(or Flask test-client requests that open their own connections internally) must call
`run_racers` instead of writing a new hand-rolled start/join loop** — copy the shape
from any of the 11 migrated tests (`from _race import run_racers` as a local import,
same convention as the existing local `import threading`/`import psycopg`). Do NOT
reintroduce the old idiom even for "just one more quick racer test" — that is exactly
how the suite acquired 11 instances of the same hazard in the first place.
