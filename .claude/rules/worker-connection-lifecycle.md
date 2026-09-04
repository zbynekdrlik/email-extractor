---
paths:
  - "email-extractor/app/main.py"
  - "email-extractor/app/orders/worker.py"
---

# Every long-lived worker loop that HOLDS its own connection must RECONNECT on `psycopg.OperationalError` (#380)

The add-on runs two long-lived loops, each holding ONE Postgres connection for its whole
life: the IMAP poller (`main.py::main`) and the order-worker thread
(`orders/worker.py::run_forever`, which also drives the static/DL/confirm/question/
human-processing/dl_alerts sweeps on that SAME shared connection). The bundled Postgres
CAN crash and recover mid-life (2026-09-04: disk-full → `PANIC: No space left on device`,
auto-recovered a minute later). When it does, the held connection is **permanently
closed** — every subsequent query raises `psycopg.OperationalError: the connection is
closed`.

**A `while True` loop that holds a connection MUST catch `psycopg.OperationalError`
specifically (BEFORE any generic `except Exception`) and REPLACE the connection — never
retry the same dead one.** `main.py::main` did this from the start; `run_forever` did NOT
(its generic `except Exception: log.exception("order worker tick failed")` swallowed the
`OperationalError` and spun the SAME closed connection every 15 s for 2h46m until a manual
`ha addons restart` — zero orders processed the whole time, #380).

Rules for any such loop here:

- **Order the handlers:** `except psycopg.OperationalError` must come BEFORE `except
  Exception` (it is a subclass — a generic handler first would swallow it). A non-connection
  bug (`ProgrammingError`/`DataError`) still falls through to the generic handler, so error
  visibility is unchanged.
- **Replace, don't retry:** on `OperationalError`, log, best-effort `conn.close()` (a
  debug log on failure, never a silent `except: pass` — `script-failure-policy`), then
  `conn = connect()` in its own try. On reconnect failure, log and fall through to the
  shared `sleep(...)` — the NEXT iteration retries. NEVER `continue` straight into a
  reconnect (that is the tight spin); always pace it with the loop's existing sleep, and
  re-check `stop` at the top of the loop.
- **Make it testable:** inject the connection factory (`connect=` DI kwarg, default
  `lambda: db.connect(cfg.pg_dsn)`) so a test can drive the reconnect with a fake dead
  connection + a fresh one, without a real DB outage (`test_orders_worker_reconnect.py`).
- **The thread WRAPPER must not die either:** there is no supervisor that restarts the
  order-worker thread. `start_order_worker`'s `loop()` retries the INITIAL `db.connect`
  (log + sleep + retry) and restarts `run_forever` on any unexpected crash — a bare
  `try/except: log.exception("died")` that exits leaves order processing silently dead
  until a manual restart.

**Before adding a NEW long-lived loop/daemon here, grep for other `db.connect` holders**
(`grep -rn "db.connect(" app/`) and confirm each is either connect-per-use (fine — the HTTP
API's `httpapi._db`/`_db_tx` are per-request factories) or a one-off CLI script
(fine — `backfill`, `alias_migration`, `*_eval_run`, `memory_import` run once and exit).
Only a loop that HOLDS a connection across ticks needs this reconnect discipline.
