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
