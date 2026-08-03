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

If you ever DO get a hang: find the blocking backend with the query above and
`SELECT pg_terminate_backend(<pid>)` for the one that's `idle in transaction` — it's safe,
it's always a stray test connection on a throwaway local DB, never anything live.

## The final "N passed in Xs" summary line can be MISSING from captured output (#160)

`.venv/bin/python -m pytest tests/ -q > out.log 2>&1` on this box has, at least once, ended
the captured file right at the last `[100%]` progress line with NO trailing summary line at
all — even though the run was fully green (exit code 0). Don't treat a missing summary as a
hang, a truncated capture, or a reason to re-run: verify success from **exit code 0 AND zero
`F`/`E`/`s`/`x` characters** in the dot-progress output (`python3 -c "print(collections.
Counter(ch for ch in open('out.log').read() if ch not in '.\n[] %0123456789'))"` — an empty
Counter means every test passed) rather than grepping for `"passed in"`.
