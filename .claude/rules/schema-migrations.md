---
paths:
  - "email-extractor/app/migrate.py"
  - "email-extractor/app/db.py"
  - "email-extractor/app/db_schema.py"
  - "email-extractor/tests/test_migrate.py"
---

# Schema migrations — the versioned `app/migrate.py` engine (#269)

Since 0.9.93 `db.init_schema()` is NOT "run the whole `SCHEMA` list every start" any more.
It calls `migrate.run_migrations(conn, db.REVISIONS)`: a numbered-revision runner backed by a
`schema_version` ledger table. An up-to-date DB does an O(1) version check and applies nothing;
an outdated DB applies only the missing revisions.

## How to add a schema change — a NEW revision, NEVER edit the baseline

`db.SCHEMA` is **frozen** as revision 1 (`db.REVISIONS = [migrate.Revision(1,"baseline",SCHEMA)]`).
Immutable-migrations rule (Alembic/yoyo/Rails): a schema change is a NEW numbered `Revision`
appended to `db.REVISIONS`, e.g.

```python
REVISIONS = [
    migrate.Revision(migrate.BASELINE_REVISION, "baseline", SCHEMA),
    migrate.Revision(2, "add_foo_index", ["CREATE INDEX IF NOT EXISTS ... "]),
]
```

- Revision ids MUST be strictly ascending + unique — `run_migrations` code-enforces this and
  raises `ValueError` before touching the DB (a mis-appended entry fails fast in CI).
- A NEW revision's statements run inside ONE transaction (statements + ledger stamp commit
  atomically → exactly-once, and its statements need NOT be idempotent). **Because of that
  transaction they must be transaction-safe: no `CREATE INDEX CONCURRENTLY`, no `VACUUM`.** A
  migration that genuinely needs a non-transactional statement must grow its own per-statement
  path like the baseline does.
- NEVER edit the baseline's statements to make a schema change — that is exactly the #269 risk
  (a non-idempotent statement silently re-run against prod on every boot) the mechanism removes.

## The baseline is SELF-HEALING (run-then-record), never record-without-running

On a DB with no `schema_version` row yet (a live prod DB from before this mechanism, OR a dev DB
that lags), `run_migrations` RUNS the idempotent baseline (`SCHEMA`, per-statement autocommit —
exactly what prod ran every boot for months) and THEN records it. This heals a lagging schema
(adds any missing table/column) and is non-destructive.

**BANNED design: "detect existing schema → record baseline WITHOUT running it" (the original #269
proposal, B1).** It is NOT self-healing: a DB missing a later-added column gets recorded as
"baseline done" without the column. Proven live during #269 — B1 recorded `(1,'baseline')` while
`edi_sent.uploaded_at` (#153) was absent → 35 tests failed on `UndefinedColumn`. The fix (B2)
always runs the idempotent baseline before recording; that is what shipped.

Deploy proof (0.9.93, live): first boot logged `applied schema baseline r0001 (101 idempotent
statement(s))`, second boot logged `schema up-to-date at r0001`, row counts unchanged before/after.

## Testing migrations — `reapply_schema`, and `schema_version` is NOT truncated

`init_schema` being version-gated broke the old test idiom "drop a column, call `db.init_schema`,
assert it comes back" (init_schema is now an O(1) no-op once the baseline is recorded).

- A migration test that drops a column/index and expects init_schema to HEAL it must use the
  **`reapply_schema` fixture** (conftest) — it `DELETE`s the ledger then runs init_schema, so the
  baseline re-runs (simulating a pre-mechanism/lagging DB, the real prod heal path). Do NOT call
  `db.init_schema(pg)` directly for a re-run — it will skip.
- The session `_schema` fixture `DROP`s the ledger + re-applies the baseline once per session, so a
  persistent local test DB self-heals to the current schema at session start.
- `schema_version` is deliberately EXCLUDED from the `pg` fixture's `TRUNCATE` list (it is META,
  managed by the runner — truncating it per-test would fight the mechanism). This is the exception
  to `local-testing.md`'s "a new table must go in the TRUNCATE list" rule.
- The version fast-path / concurrent-start / partial-failure paths are tested in `test_migrate.py`
  on throwaway `CREATE DATABASE` DBs (a `fresh_db()` helper), fully isolated from the shared DB.

## Adding the FIRST real revision (rev 2) breaks ~8 `test_migrate.py` assertions — expected, generalize them (#314)

Until #314 `db.REVISIONS` had ONLY the baseline, so many `test_migrate.py` tests hardcoded
"baseline is the only revision" (`done == [migrate.BASELINE_REVISION]`, ledger `== [(1,
"baseline")]`, `"up-to-date at r0001"`, `count == 1`, `applied == [0,0,0,1]`) AND reused
revision **ids 2/3** for their own synthetic in-test revisions. Appending the first real
`Revision(2, ...)` breaks BOTH classes at once (8 tests on #314):
- **The "baseline-only" assertions** — generalize them to `db.REVISIONS` so they never
  break again on a rev 3+: `done == [r.revision for r in db.REVISIONS]`, ledger `== [(r.revision,
  r.name) for r in db.REVISIONS]`, `f"up-to-date at r{max(r.revision for r in db.REVISIONS):04d}"`,
  `count == len(db.REVISIONS)`, `applied == [0,0,0,len(db.REVISIONS)]`.
- **The synthetic in-test revision ids** (`test_partial_migration`, `test_revision_failure`,
  `test_pending_revisions`) — bump 2/3 to HIGH unused ids (90/91), because those tests call
  `db.init_schema(conn)` FIRST (now applying the real rev 2), so a local `Revision(2, ...)`
  collides (already-applied id 2 → skipped/no-error, breaking the "applies 2 then 3" / "raises
  DuplicateTable" intent). This is legitimate mechanism-test maintenance, not weakening — do
  it in the same commit as the new revision, with a clear note.

## `SCHEMA` now lives in `app/db_schema.py`, and its statement ORDER is chronological, not per-domain (#309)

Since #309 the baseline DDL list is `app/db_schema.py`'s `SCHEMA` (101 statements),
re-exported by `db.py` (`from .db_schema import SCHEMA`); `db.REVISIONS[0]` still wraps
it as revision 1. `db.SCHEMA` / `from app.db import SCHEMA` still resolve (test_migrate's
`for stmt in db.SCHEMA` is unaffected — same object).

**Never reorder `SCHEMA`, and never split it into per-domain sub-lists.** It reads like
it *could* be grouped by domain (messages → attachments → orders → desadv → questions),
but statements ~82–100 are LATE in-baseline migrations appended chronologically —
`ALTER import_alert_incidents`, a `DROP INDEX ... open` + `CREATE ... open_v2` swap (must
stay in that order), `ALTER order_questions`, `ALTER desadv_sent`, `DO $$` blocks — that
reference tables defined far EARLIER in the list. The migrate baseline runs the list in
sequence (self-healing, per-statement autocommit), so the order is load-bearing (FK/column
dependencies, the index swap). A per-domain sub-list split would move statement 82 up into
its table's domain cluster and change execution order. Keeping `SCHEMA` as ONE ordered
verbatim data list is deliberate (see #309's design comment) — a NEW schema change is still
a new `Revision` appended to `db.REVISIONS` (top of this file), NEVER an edit to `SCHEMA`.
