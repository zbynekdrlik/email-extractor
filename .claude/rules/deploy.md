---
paths:
  - "email-extractor/config.yaml"
  - "email-extractor/app/__init__.py"
  - "email-extractor/Dockerfile"
---

# Deploying the live HA add-on (`e0ac7775_email_extractor`)

Host/SSH/dashboard credentials live in **local memory only** (`ha-server-access.md`,
`extractor-addon-supervisor.md`, `email-extractor-deploy.md`) — never commit them here.
This file has the PROCEDURE; memory has the VALUES.

## It is a real supervisor add-on, not a raw container

Installed from this repo as a HA Supervisor store add-on (slug `e0ac7775_email_extractor`,
container `app_e0ac7775_email_extractor` since 2026-07-30's `addon_` → `app_` rename).
Config lives in `/data/options.json` INSIDE the container, not in git.

## Deploy = one command, after main CI has built + pushed the GHCR image

`build` in `.github/workflows/ci.yml` only pushes `ghcr.io/zbynekdrlik/email-extractor-amd64:<version>`
on a `main` push (reads the version from `config.yaml`) — wait for that job green first.

```bash
ssh <ha-user>@<ha-host>          # values: memory ha-server-access.md; sshpass -p "$PW" if no key
export SUPERVISOR_TOKEN=$(cat /run/s6/container_environment/HASSIO_TOKEN)   # required — `ha` else says "unauthorized"
ha store reload                                          # Supervisor's store-metadata cache does NOT
ha addons info e0ac7775_email_extractor --raw-json \      # auto-refresh right after a CI build; without
  | jq '.data | {version, version_latest, update_available}'   # this, `ha addons update` can say
ha addons update e0ac7775_email_extractor                 # "No update available" even though the
ha addons info e0ac7775_email_extractor --raw-json \      # image already exists in GHCR
  | jq '.data | {version, state}'                          # confirm the new version + state:"started"
ha addons logs e0ac7775_email_extractor | tail -20
```

(`ha addons ...` is deprecated in favour of `ha apps ...` — still works, just prints a
warning; both alias the same command.)

## Post-deploy verification

- Liveness: `curl http://<ha-host>:8099/health` → `{"ok":true,"version":"<x.y.z>"}`.
- Version-on-DOM: any page (`/otazky`, `/otazky-dl`, the main dashboard) shows `v<x.y.z>`
  in the header — read it with Playwright, not curl.
- Functional: `/otazky`/`/otazky-dl` list live open warehouse questions — a real, current
  cross-check for whatever the ticket changed in the matching ladder (`app/orders/match.py`)
  or the `/sklad`/`/sklad-dl` role boundary (`app/httpapi.py`'s `_role_kinds`, #231).

**Verifying an unauthenticated-link/session boundary with the Playwright MCP browser —
clear cookies FIRST, every time.** The MCP browser profile (`.playwright-mcp/`) is
PERSISTENT across separate Claude sessions against this SAME live host — a cookie from an
earlier admin `dash_password` login can still be sitting in the jar. Since a real admin
login (`session["auth"]`) is DELIBERATELY unrestricted regardless of whatever `/sklad`/
`/sklad-dl` role a session also picks up (#231's own fix — `_role_kinds()` checks `auth`
first), navigating straight to `/sklad-dl/<key>` with a stale admin cookie still present
will show the FULL unfiltered board and look like the split isn't working — a false
negative on the boundary, not a real bug. `await page.context().clearCookies()` (via
`browser_run_code_unsafe`) before EVERY fresh unauthenticated-link check, then verify `/`
actually redirects to `/login` first as proof the session is genuinely clean.

## Known gotchas

- **`ha addons info <slug> --raw-json` nests everything under `.data`** — `jq
  '{version, state, update_available}'` on the raw output silently returns all-`null`
  (valid JSON, wrong keys) because the real fields are `.data.version`/`.data.state`/
  `.data.update_available`. The PLAIN `ha addons info <slug>` (no `--raw-json`) prints a
  flattened YAML-ish dump with `version:`/`state:` at the top level instead — use THAT for
  a quick human read, and `.data.<field>` when piping `--raw-json` through `jq` (#163,
  2026-08-03). Also strip the "`addons` is deprecated, use `apps`" warning line before
  parsing if you don't redirect stderr — it doesn't break `jq` here (jq reads stdout only)
  but it's easy to mistake for the reason a filter came back empty.
- **`scp` to this box fails** ("subsystem request failed", SFTP subsystem likely
  disabled). Base64-encode locally, pipe through `ssh ... "echo '<b64>' | base64 -d >
  /tmp/x"`, then `docker cp` from the HOST into the container if the file needs to land
  inside it.
- **No `sqlite3` CLI inside the n8n add-on container** (only relevant if you're cross-
  checking n8n's own DB) — use node's bundled sqlite3 module instead; `docker exec`
  output truncates around 64 KB, so large dumps need chunking/base64.
- **`sudo docker exec -e PGPASSWORD app_... psql ...` silently fails with "no password
  supplied"** even after `export PGPASSWORD=...` in the same SSH session — `sudo`
  resets the environment by default, so the bare `-e PGPASSWORD` (no `=value`, meant to
  forward the caller's own env var) never reaches the container. Fix: pass the literal
  value inline through a nested shell instead of relying on env forwarding across
  `sudo`: `sudo docker exec app_e0ac7775_email_extractor sh -c "PGPASSWORD=<pw> psql -h
  127.0.0.1 -U email -d email -c '<query>'"` (#153 post-deploy DB verification —
  confirming the edi_sent.uploaded_at backfill landed on the 48 live rows).
- **A `psql -c '<query>'` containing JSONB operators (`->>`, `->`) or nested single-
  quoted strings breaks through the ssh → `sudo docker exec` → `sh -c` → `psql -c`
  quoting chain — don't fight it, write the SQL to a FILE and run `psql -f`** (#205,
  post-deploy shadow-window verification). A query like `result->>'kind'='dl'` needs
  its OWN single quotes preserved through THREE nested shells; every attempt at
  escaping it inline (doubled quotes, backslash-escaping) either got eaten by one layer
  or produced `column "kind" does not exist` (the `->>'kind'` silently parsed as a bare
  identifier). The reliable path, same "base64 to the host, `docker cp` into the
  container" technique the scp-workaround above already uses: write the SQL to a local
  scratch file, `base64 -w0` it into the ssh session (`echo '<b64>' | base64 -d >
  /tmp/x.sql`), `sudo docker cp /tmp/x.sql app_..._email_extractor:/tmp/x.sql`, then
  `sudo docker exec app_..._email_extractor sh -c 'PGPASSWORD=<pw> psql -h 127.0.0.1 -U
  email -d email -f /tmp/x.sql'` — zero quoting to get right, works first try. Also
  note: `order_runs` has NO `created_at` column (only `started_at`/`finished_at`) —
  check `\d order_runs` (same file-based technique) before guessing a column name.
- **Updating a supervisor add-on's options with a LARGER merged JSON payload (not a
  one-line change)** — the existing "fetch current options, merge, POST the whole
  object" recipe above works, but for a payload of any real size, inline it as a FILE
  the same way: base64 the merged `{"options": {...}}` JSON to the host, `docker cp` it
  into `hassio_cli`, then `sudo docker exec hassio_cli sh -c 'curl -s -X POST -H
  "Authorization: Bearer $SUPERVISOR_TOKEN" -H "Content-Type: application/json"
  http://supervisor/addons/<slug>/options -d @/tmp/x.json'` — `-d @/tmp/x.json` reads
  the body from the file inside the container, so nothing about the JSON's own quoting
  or escaping has to survive the shell at all (#205, enabling `delivery_notes_shadow`
  live). Delete the scratch file on BOTH the host and inside the container/`hassio_cli`
  afterward — it's plaintext add-on config, not a secret, but still cleanup hygiene.
- **Retiring a feature that reads a `config.yaml` option (#129, disabling the Google
  Sheet fetch) — do NOT remove the option from `options:`/`schema:`.** The live add-on
  already has the key set in its own `/data/options.json`; removing it from the
  store-published `config.yaml` risks the Supervisor rejecting/warning on the next
  options validation. Leave the schema key declared (`str?`/etc, unchanged) and just
  stop the CODE from reading it — an unread-but-still-declared option is harmless, and
  it's the only way to guarantee no live-add-on validation risk. Same for the `Config`
  dataclass field that parses it: leave it too, rather than making that a second,
  separate refactor.
- **A `checked_at`/similar "last refreshed" timestamp can legitimately bump ONE more
  time right after a deploy that just REMOVED the periodic refresh that used to touch
  it — don't mistake it for the new code still running the old behavior (#129).** `ha
  addons update` doesn't swap the container instantly: the OLD version keeps running
  (on its own interval-based loop) right up until the new image is pulled and the
  container is recreated. If the old code's refresh interval happens to come due in
  that window, it fires ONE last time under the OLD code — perfectly normal, not a
  bug. Verify which code actually did it: `sudo docker inspect <container> --format
  '{{.State.StartedAt}}'` (new container's start time) vs the timestamp on the
  row/column that moved — if the bump is BEFORE the new container started, it was the
  old process's last legitimate tick; confirm the NEW code by checking the timestamp
  stays frozen from then on (re-query a minute or two later).
- **The container for BOTH `docker logs` and `docker exec ... psql` is `app_e0ac7775_
  email_extractor` — the old `addon_e0ac7775_email_extractor` name from before the
  2026-07-30 rename NO LONGER EXISTS.** `sudo docker logs addon_...` / `sudo docker
  exec addon_... sh -c ...` both fail with `Error: No such container:
  addon_e0ac7775_email_extractor` — but a `grep`/pipeline built around that failing
  command (`sudo docker logs addon_... 2>/dev/null | grep X`) silently swallows the
  stderr and returns an EMPTY grep result (exit 1, "0 matches"), which reads exactly
  like "the thing I searched for genuinely isn't in the logs" — a false negative, not
  an error you'd notice. Always use `app_e0ac7775_email_extractor` (see "It is a real
  supervisor add-on" above), and if a log/DB check ever comes back suspiciously empty,
  re-run WITHOUT `2>/dev/null` first to rule out a silently-failed container name
  before trusting the empty result.

- **`cat file | ssh host "cmd" 2>&1 </dev/null` silently drops the piped content —
  `</dev/null` on the SAME command line overrides the pipe as stdin for the whole
  compound command, so `ssh` gets an empty stdin instead of `file`'s content (#224/#225,
  2026-08-08).** `cat scratch.py | sshpass -p "$PW" ssh host "cat > /tmp/x.py" 2>&1
  </dev/null` creates a 0-byte `/tmp/x.py` with NO error — the trailing `< /dev/null`
  (added out of habit to avoid an interactive ssh prompt) wins over the `cat |` pipe for
  stdin, and `cat >` on an empty stdin just makes an empty file and exits 0. If you need
  BOTH "ssh never blocks on a prompt" AND "pipe real content through", do NOT redirect
  stdin at all when you're piping content in — the pipe itself already provides
  non-interactive stdin; only add `</dev/null` on ssh calls that send NO piped input.
- **Forcing a "shadow re-run" of specific already-processed messages, to verify a
  matching/extraction fix against real production data (#224/#225).** `dl_worker.
  _peek_for_shadow`'s `NOT EXISTS (SELECT 1 FROM order_runs r WHERE r.message_id =
  m.message_id AND r.shadow)` means a message with ANY existing shadow `order_runs` row
  (success OR failure) is excluded from ever being re-picked. To force a fresh shadow
  run: `DELETE FROM order_runs WHERE id IN (<the specific failing run ids>) AND
  shadow=true` (verify EVERY id is `shadow=true` and the message has ZERO non-shadow
  runs first, read-only, before deleting) — the worker's own loop
  (`worker.run_forever`, `sleep(15)` only when nothing was handled) then naturally
  re-picks and re-processes each freed-up message within its normal tick cycle, ONE per
  tick, no restart needed. **Caveat:** `_peek_for_shadow` ALSO filters on `m.created_at
  > now() - make_interval(days => delivery_notes_shadow_days)` (default 3) — a message
  older than that window stays invisible even after its shadow row is deleted. Fix:
  temporarily widen `delivery_notes_shadow_days` via the documented options-POST
  technique above, `ha addons restart`, wait for it to be picked up, then revert the
  option and restart AGAIN immediately — a config-only, fully reversible bump, not a
  destructive action.
- **A multi-document attachment's SECOND (and later) document is invisible to a query
  that reads `result->'documents'->0->>...` — that `->0` only ever sees document INDEX
  ZERO (#224/#225 post-deploy verification, 2026-08-08).** One real message legitimately
  produced TWO documents from one attachment (doc 612006 `ok` + doc 68944 `partial`,
  same `order_runs` row) — a first-pass check of `result->'documents'->0->>'reason'`
  reported "clean" while document[1]'s own `items_skipped_no_match` still had a real
  (unrelated, out-of-scope) miss. Always read `jsonb_array_elements(result->
  'documents')` (or `jsonb_pretty(result->'documents')` for a full manual read) when
  auditing DL run outcomes — never assume a message produced only one document.
- **Restarting the add-on kills any in-flight worker tick mid-processing and leaves an
  orphaned `order_runs` row with `status='running'`** (observed 2026-08-08: run 401).
  After any restart, check `SELECT id,message_id FROM order_runs WHERE status='running'
  AND started_at < now() - interval '10 minutes';` and delete stale shadow rows
  (`shadow=true`) — a live-engine stale row needs investigation, not blind deletion.
- **The dashboard's own `/api/messages?q=<text>&limit=N` (session-cookie-gated) is a much
  lighter post-deploy functional-verification path than the SSH→docker exec→psql chain
  for a simple lookup** (#258) — once logged in via Playwright (`dash_password`, see
  above), `page.evaluate(() => fetch('/api/messages?q=...', {credentials:'include'}))`
  returns real live JSON (`proc_status`/`proc_outcome`/`last_event_at`/etc.) with zero
  quoting gymnastics, and doubles as a real UI-adjacent check (same code path the
  dashboard itself uses) instead of a raw table read. Reach for the multi-layer psql
  chain only when you need something the dashboard API doesn't expose (raw
  `email_events` rows, `desadv_sent`, schema introspection).
- **Reprocessing a single already-reviewed (never-shipped) message as a genuine
  functional post-deploy check, THEN reading back via `email_events` (not just
  `messages.proc_outcome`), is what actually proves a review-reason WORDING change
  landed** (#258) — `messages.proc_outcome` only ever shows the LATEST rollup summary
  (`"N dokument(y): Nx review"`, generic regardless of which exact code path produced
  it); the real, distinguishing text only lives in `email_events.outcome` for that
  specific event. `SELECT e.stage, e.status, e.outcome, e.ts FROM email_events e JOIN
  messages m ON m.message_id = e.message_id WHERE m.id = <id> ORDER BY e.ts DESC LIMIT
  5` — read the newest row's exact wording, don't trust the summary column alone to
  distinguish "the new code path ran" from "the old one did, coincidentally producing a
  similarly-shaped summary".
- **A `report.post()`/`dl_alerts.flush_pending` failure with `urllib.error.HTTPError:
  HTTP Error 405: Not Allowed` in the container logs is almost certainly Odoo itself
  being down, NOT a bug in this repo — check LIVE before assuming a code regression
  (#237, second real occurrence of the exact #253 signature).** Confirmed read-only,
  from inside the container, with NO message ever sent (safe, side-effect-free):
  ```python
  import json, urllib.request, urllib.error
  opts = json.load(open("/data/options.json"))
  req = urllib.request.Request(opts["odoo_url"].rstrip("/") + "/",
      headers={"Authorization": f"Bearer {opts['odoo_api_key']}"})
  try:
      urllib.request.urlopen(req, timeout=20)
  except urllib.error.HTTPError as e:
      print(e.code, e.headers.get("server"), e.read()[:200])
  ```
  The tell (both incidents, identical): `GET /` → `503`, body is an "ERP - Maintenance"
  HTML page (not an Odoo error page); `POST /json/2/<anything>` → `405 Not Allowed`
  from `nginx/1.31.3` (not Odoo's own JSON error shape) — this is nginx rejecting the
  method because the whole backend behind it is in maintenance, not Odoo rejecting the
  specific endpoint/payload. `dl_alerts`'s own durable outbox (`pending_alerts`,
  `MAX_FLUSH_ATTEMPTS=200`, ~15s tick ≈ 50 min of retries) is BY DESIGN for exactly
  this — nothing is lost, it delivers once Odoo recovers (confirmed by #253's own
  closing comment: the earlier outage self-resolved and the blocked message went out).
  Never try to "fix" this in the repo — file a fresh tracking issue (mirroring #253's
  `Scope-gate: user-request` shape) if it recurs, and move on; there is nothing to
  change here.
- **Verifying a NEW alert-producing feature (a sweep that calls `dl_alerts.enqueue`)
  against LIVE production data, without ever letting a message actually reach a real
  Odoo channel — a rollback-based dry run on your OWN connection, not a callback
  substitution** (#237). `question_alerts.sweep(conn, cfg)` (like `confirm.sweep`) calls
  `dl_alerts.enqueue()` directly rather than accepting an injectable `post` callback, so
  the #247/#262 "pass a capture function in place of the poster" technique doesn't
  apply cleanly here — the DB writes (both the `pending_alerts` insert AND the
  `order_questions.reminder_sent_at`/`escalated_at` UPDATE) would still happen for
  real even with a stubbed `enqueue`. The safe alternative: open your OWN
  `psycopg.connect(cfg.pg_dsn, autocommit=False)` (the app's real connections are
  autocommit), call the real sweep function against it, print whatever you need to
  verify (the composed HTML, the row counts), then **always `conn.rollback()`** in a
  `finally` block regardless of outcome — nothing persists, so no `pending_alerts` row
  ever exists for a LATER `flush_pending()` call (on a DIFFERENT connection) to
  discover and actually deliver. Reusable for any future backend sweep in this
  codebase that writes durably before an eventual Odoo post.
- **`curl http://localhost:8099/health` from inside an SSH session on the HA box can
  fail with `Recv failure: Connection reset by peer` even though the add-on is
  perfectly healthy (#268 kroky 5/7/8 post-deploy check).** `curl` resolves
  `localhost` to `::1` (IPv6) FIRST and the connection gets reset on that path — the
  add-on's Flask dev server binds `0.0.0.0` (IPv4-only). Force IPv4 explicitly:
  `curl -4 -s http://127.0.0.1:8099/health` (or use `127.0.0.1` instead of
  `localhost` — either alone is enough, `-4` is the more robust fix since it also
  covers a future` localhost` typo). A plain `curl -s http://localhost:8099/health`
  failing is NOT evidence the add-on is down; check with `-4` before escalating.
- **The Supervisor `/addons/<slug>/info` response's `schema` field is a LIST of `{name,
  type, ...}` dicts, not a `{key: type}` dict** (integration round A, verifying the
  #255 config-wiring knobs live, 2026-08-13) — `k in schema` on the raw JSON-decoded
  value throws `AttributeError: 'list' object has no attribute 'get'` if you assume
  dict shape. Check membership via `[s.get("name") for s in schema]`. `options` (the
  SAME response's sibling field, live `/data/options.json` content) IS a plain
  `{key: value}` dict as expected — only `schema` has this list-of-dicts shape.
- **Deriving the live `/sklad/<key>` and `/sklad-dl/<key>` signed warehouse-link keys
  for post-deploy role-boundary verification** (integration round C1, 2026-08-13) —
  both keys are computed from `app.secret_key` (`app.linkutil.sklad_key`/`dl_key`),
  never stored in `/data/options.json`, so don't guess or reuse an old value from
  memory without re-deriving live (the key changes if `secret_key`/the persisted
  `.session_secret` ever changes):
  ```
  sudo docker exec app_e0ac7775_email_extractor python3 -c "
  from app.config import Config
  from app.httpapi import create_app
  from app.linkutil import sklad_key, dl_key
  cfg = Config.load(); app = create_app(cfg)
  print('sklad:', sklad_key(app.secret_key))
  print('dl:', dl_key(app.secret_key))"
  ```
  Then, per role, in Playwright: `clearCookies()` → navigate to
  `/sklad/<key>`/`/sklad-dl/<key>` (redirects to `/otazky`/`/otazky-dl`) →
  `fetch('/api/orders/questions', {credentials:'include'})` and check the returned
  `kind` values are the expected subset (`customer`/`mail` for orders,
  `dl_item`/`dl_supplier` for DL) — proves the role-boundary filter, not just a 200
  status, per this file's own existing "role/kind security boundary" note above.

## Functionally verifying a board-ENDPOINT fix on prod WITHOUT touching a real customer
## DL — the synthetic-question pattern (#305, 2026-08-14)

To prove a deployed `/api/orders/question/<qid>/answer` behaviour end-to-end on the live
box, do NOT click the button on a REAL open question (that defers/mutates a real
customer's delivery note — e.g. #305's board showed only the live HK LOAN #236 question,
which must not be deferred as a "test"). Instead drive a SYNTHETIC question through the
real deployed code, then clean up:

1. **Create** via the app's OWN code path in the container (a clearly-synthetic
   `message_id` like `VERIFY-305-<ts>`):
   `cat script.py | sshpass -p "$PW" ssh <ha-host> 'sudo docker exec -i -e PYTHONPATH=/app
   app_e0ac7775_email_extractor python3 -'` where script.py does
   `db.connect(cfg.pg_dsn)` + an `INSERT INTO messages (...VERIFY...)` + `teach.ask_dl_item
   (...)`, printing the new `qid`. (`from app import config, db` — `db` is `app.db`, NOT
   `app.orders.db`; that import error is the first thing to fix.)
2. **Exercise the REAL HTTP endpoint** from the Playwright MCP session that already holds
   the `/sklad-dl/<key>` cookie — `browser_evaluate` a
   `fetch('/api/orders/question/<qid>/answer', {method:'POST', credentials:'include',
   headers:{'Content-Type':'application/json'}, body: JSON.stringify({choice:'unknown'})})`
   and read the JSON (e.g. `{closed:1, ok:true, sklad_unknown:true}` for a working #305
   defer; the pre-fix code returned a no-op `{ok:true, question:<open>}`).
3. **Verify + clean up** in one more container script: `SELECT` the question status,
   message `processed`, latest `email_events` row, then `DELETE` all three synthetic rows
   and confirm `(0,0,0)` remaining.

This exercises the exact deployed code path with concrete observed values, uses only the
app's own functions + the real HTTP endpoint, and leaves zero residue in prod. Passwordless
`sudo docker` works for `newlevelmedia`; `/run/s6/container_environment/HASSIO_TOKEN` is
readable WITHOUT sudo (`-rw-r--r--`) so the deploy needs no sudo/password for the token —
only the `cat` of it over ssh needs `# airuleset:secret-read-ok` (captured into
`SUPERVISOR_TOKEN` for `ha`, never printed).

**Building the `build_dl_digest` from LIVE data to check its CONTENT (#312) is the same
container-python shape, but fully READ-ONLY** — `reliability.dl_provenance_stats_for_day
(conn, yesterday, include_current_health=False)` + `report.build_dl_digest(stats)`, then
assert the HTML lacks the phrases you removed and `include_current_health=True` still
carries them (the admin-stats path). Never POST it.

**Shell-quoting the run-card:** `notify --run-card --goal '…„Neviem"…'` — the ASCII `"`
inside a Slovak `„…"` quote pair breaks a DOUBLE-quoted `--goal`/`--achieved` arg (the `"`
closes the shell quote, argparse then errors "unrecognized arguments" and NO card fires,
though a piped `| tail` masks the real exit code). Single-quote the whole value.
