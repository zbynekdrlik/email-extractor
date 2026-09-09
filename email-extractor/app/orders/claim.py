"""Reusable "claim this row, OR atomically tell me who/what already holds it"
primitive (#271).

`desadv.claim_send_or_identify()` (#216, review-caught before merge) proved the shape:
answer "did I just claim this, or does something else already hold it" in ONE atomic
SQL round trip, via a data-modifying CTE with a `NOT EXISTS` fallback `SELECT` — never
a plain claim call followed by a SEPARATE read, which leaves a real TOCTOU window (a
different claimant could reclaim the row in between, changing what the follow-up read
reports). This module extracts that shape as a reusable primitive so a NEW two-phase
ledger doesn't have to reinvent it — or, as happened to `edi.py`'s own `claim_send()`
(used by both `pipeline.py` and `static_worker.py`), silently ship the SAME TOCTOU-
shaped gap a second time because nothing generic existed to reach for.

## The five claim/dedup mechanisms this project runs (#271's own audit) — and which
## ONE this module is actually for

The project prevents "two things claim the same work" FIVE independently-designed
ways. This module does not replace all five — each solves a genuinely different
problem shape, and forcing all of them through one abstraction would be exactly the
"rewrite every lock" over-reach #271 explicitly rejected:

1. **`messages.processing_at`/`attempts`** (`worker._claim`, `static_worker._claim`,
   `dl_worker.py`'s own claim) — `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP
   LOCKED) RETURNING ...` against a WORK QUEUE: pick the next free row, mark it mine.
   There is no natural "who already holds it" question here — the query either
   returns a row (I got one) or it doesn't (someone else has every eligible row, or
   there are none) — so this module's claim-or-identify shape does not fit; a work
   queue has no external side effect to protect, only a queue to drain.
2. **`edi_sent`/`desadv_sent`** — a two-phase upload LEDGER guarding a genuinely
   irreversible external side effect (an ORION upload: #51/#153/#200). THIS is the
   shape this module generalizes — see `claim_or_identify()` below.
3. **`import_alert_incidents`** — a partial UNIQUE INDEX (`WHERE closed_at IS NULL`)
   plus an application-level check. The uniqueness constraint itself IS the claim;
   there is no staleness/reclaim window to manage (an open incident just stays open
   until explicitly closed), so the two-phase claim-then-confirm shape below is
   unneeded — a plain `INSERT ... ON CONFLICT DO NOTHING` already has no TOCTOU gap
   worth closing (there is nothing to "identify" beyond "an open incident exists").
4. **schema migrations** (`db.py`'s `DO $$ ... PERFORM pg_advisory_xact_lock(...) ...
   END $$` blocks) — a ONE-TIME, whole-database serialization of a DDL change, not a
   per-row claim against live traffic at all.
5. **`customer_overrides`/`dl_supplier_overrides`** — a unique index plus an advisory
   lock guarding a single conflicting WRITE (#248's own problem), not a claim held
   across a slow external side effect.

**Reach for this module when a NEW feature needs "claim this identity before a slow/
irreversible external side effect, with an atomic answer to who currently holds it" —
not for a plain work queue (#1), a stays-open marker (#3), a one-time migration (#4),
or a single write-conflict guard (#5).**
"""
from __future__ import annotations

import logging

log = logging.getLogger("orders.claim")


def claim_or_identify(conn, *, insert_sql: str, insert_params: tuple,
                      identify_sql: str, identify_params: tuple) -> tuple[bool, tuple]:
    """Wrap a caller-supplied `INSERT ... ON CONFLICT ... DO UPDATE ... WHERE <staleness
    / eligibility guard> RETURNING <cols>` (`insert_sql`/`insert_params`) together with
    a caller-supplied `SELECT <SAME cols, same order, same types> FROM <table> WHERE
    <identity>` (`identify_sql`/`identify_params`) in ONE atomic statement:

        WITH ins AS (<insert_sql>)
        SELECT true  AS claimed, i.* FROM ins i
        UNION ALL
        SELECT false AS claimed, s.* FROM (<identify_sql>) s
         WHERE NOT EXISTS (SELECT 1 FROM ins)

    The `ins` CTE always attempts the INSERT; when the conflict path's own `WHERE`
    refuses the (re)claim, NOTHING is written and `ins` yields zero rows — at that
    point the table row is UNCHANGED by this statement, so `identify_sql` reading it
    back, wrapped in the SAME statement, is exactly the pre-existing state with no gap
    for a concurrent caller to have changed it in between (the #216 TOCTOU fix,
    generalized).

    Returns `(claimed, info)`:
    - `claimed=True` — the claim was taken (a fresh insert or an eligible reclaim);
      `info` is whatever `insert_sql`'s own `RETURNING` clause produced (often nothing
      the caller needs — many callers `RETURNING NULL::<type>` deliberately, to keep
      the "claimed" branch's tuple shape trivial and independent of the newly-written
      values, exactly mirroring `desadv.claim_send_or_identify()`'s own `SELECT true,
      NULL FROM ins`).
    - `claimed=False` — refused; `info` is whatever `identify_sql` reports for the
      CURRENT holder/state (empty tuple if, in a genuinely impossible case, no row
      came back at all — never raises).

    **Contract the caller must uphold:**
    - `insert_sql`'s `RETURNING` clause and `identify_sql`'s `SELECT` list must
      return the SAME NUMBER of columns, in COMPATIBLE TYPES — `UNION ALL` requires
      it, and a mismatch on either of those two fails LOUDLY at execution time (a
      Postgres type/arity error), never silently.
    - They must ALSO return columns in the SAME ORDER — but this half of the
      contract is NOT enforced by Postgres: a same-arity, compatible-type column
      SWAP (e.g. `RETURNING a, b` vs `SELECT b, a`) is accepted silently and returns
      TRANSPOSED values. Get the order right; nothing will catch it for you if you
      don't.
    - `identify_sql` must match AT MOST ONE ROW for the identity it was given —
      this wrapper calls `fetchone()` (never a loop), so any extra matching row is
      silently discarded, not an error. (`insert_sql`'s own `ON CONFLICT` target
      already guarantees this for the claimed branch; the caller is responsible for
      giving `identify_sql` an equally-unique `WHERE`.)
    - Neither string needs to (or should) end in `NOT EXISTS (...)`, a trailing
      `;`, or a trailing SQL comment — this wrapper parenthesizes each one as its
      own derived table/CTE and adds `NOT EXISTS (SELECT 1 FROM ins)` in the OUTER
      `WHERE` around `identify_sql` itself (see the composed SQL shape above), so
      `identify_sql` is otherwise free to carry its own `ORDER BY`/`LIMIT` if ever
      needed.

    Table/column names are never interpolated here — both SQL strings are written
    entirely by the caller (as `edi.py`/`desadv.py` already do for their own ledgers),
    so this primitive carries no dynamic-identifier risk of its own.
    """
    sql = (f"WITH ins AS ({insert_sql})\n"
           f"SELECT true AS claimed, i.* FROM ins i\n"
           f"UNION ALL\n"
           f"SELECT false AS claimed, s.* FROM ({identify_sql}) s\n"
           f" WHERE NOT EXISTS (SELECT 1 FROM ins)")
    row = conn.execute(sql, tuple(insert_params) + tuple(identify_params)).fetchone()
    if row is None:
        # Cannot happen for a well-formed pair of statements (the "claimed" branch
        # always yields exactly one row when the INSERT/UPDATE takes; the "identify"
        # branch always yields at most one row otherwise, from a WHERE on a unique
        # identity) — but never let a shape violation surface as an unpacked None.
        log.error("claim.claim_or_identify: no row returned — insert_sql/identify_sql "
                  "column shapes may not match")
        return False, ()
    return bool(row[0]), tuple(row[1:])


# ---------------------------------------------------------------------------
# #412: attempt-tracked ledger claim — work-queue-style retry with attempts,
# stale-reclaim, and max-attempts guard.  Used by `dl_invoice_runs` now;
# structured so `desadv_sent`/`edi_sent` can adopt later (after their own
# migration adds the same columns) WITHOUT changing behaviour in this lane.
# ---------------------------------------------------------------------------

def ledger_claim(conn, *, table: str, pk_col: str, pk_val,
                 stale_minutes: int, max_attempts: int) -> tuple[bool, int]:
    """Claim or reclaim a row in a ledger table with attempt tracking.

    The target table MUST have these columns (names are fixed by convention):
    - ``{pk_col}`` — the primary key (single column, TEXT or compatible)
    - ``claimed_at  TIMESTAMPTZ NOT NULL DEFAULT now()``
    - ``attempts    INT NOT NULL DEFAULT 1``
    - ``outcome     TEXT`` (nullable — NULL means "in progress")
    - ``finished_at TIMESTAMPTZ`` (nullable)

    Behaviour:
    - **Fresh insert** (no row exists): inserts with ``attempts=1``,
      ``claimed_at=now()``, ``outcome=NULL``.
    - **Stale reclaim** (row exists, ``outcome IS NULL``, ``claimed_at`` older
      than *stale_minutes*, ``attempts < max_attempts``): bumps ``attempts``
      and resets ``claimed_at`` to now.
    - **Refused** (row exists but is freshly held, finished, or exhausted):
      nothing is written.

    Returns ``(True, attempts)`` on a successful claim/reclaim,
    ``(False, 0)`` on refusal (the caller can distinguish finished from
    exhausted by querying the row if needed — this primitive deliberately
    does not, to stay cheap).

    ``table`` and ``pk_col`` are **internal constant identifiers** (never user
    input) — they are interpolated via f-string, matching the project's
    existing pattern in ``claim_or_identify`` and throughout ``desadv.py``/
    ``edi.py``.
    """
    # The INSERT ... ON CONFLICT ... DO UPDATE ... WHERE is Postgres's own
    # atomic reclaim: two concurrent callers can never both win, because the
    # WHERE on DO UPDATE is evaluated per-row as part of conflict resolution.
    row = conn.execute(
        f"""INSERT INTO {table} ({pk_col}, claimed_at, attempts)
            VALUES (%s, now(), 1)
            ON CONFLICT ({pk_col})
            DO UPDATE SET claimed_at = now(),
                          attempts = {table}.attempts + 1
            WHERE {table}.outcome IS NULL
              AND {table}.claimed_at < now() - make_interval(mins => %s)
              AND {table}.attempts < %s
            RETURNING attempts""",
        (pk_val, stale_minutes, max_attempts),
    ).fetchone()
    if row is None:
        return False, 0
    return True, int(row[0])


def ledger_finish(conn, *, table: str, pk_col: str, pk_val,
                  outcome: str) -> None:
    """Record outcome + finished_at on a claimed ledger row.

    Only updates rows with ``outcome IS NULL`` (idempotent — a second call
    for the same row after it is already finished is a silent no-op).
    """
    conn.execute(
        f"""UPDATE {table}
               SET outcome = %s, finished_at = now()
             WHERE {pk_col} = %s AND outcome IS NULL""",
        (outcome, pk_val))
