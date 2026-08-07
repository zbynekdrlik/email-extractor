# Editing the n8n consumer workflows (static orders, dodacie, faktúry, reklamácie)

The order/delivery-note pipelines that still live in n8n have no tests and no CI — a bad edit
ships straight to production and can lose a real customer order. These three rules come from
incidents, not theory. Read them before touching any of those workflows.

Workflow ids: static orders `O8IYhUESjaWmPMTI`, dodacie `1R4WcUFhpIPwEJX1`, faktúry
`du2O6YGmGyntXBbV`, reklamácie `LIpkBHdpcYN7YMdM`, dispatcher `TjIzExr4uUs5f4Ci`.

## 1. A Postgres / crypto / HTTP node REPLACES the item json — the payload after it is gone

`Check Already Sent` (SELECT with no hit) emits `{}`; `Claim Send`
(`INSERT … RETURNING id`) emits `{"id": "24"}`. Anything downstream that reads a field of the
*business* payload — `wincodexContent`, `wincodexFilename`, `email`, `items` — reads it off
that replacement object and finds nothing.

So: **never route the main payload THROUGH a DB node.** Either

- read the payload back by node reference (`$('generator').first().json.…`), or
- put a small Code node right after the DB node that re-emits it (`Restore EDI Payload` is
  exactly that), and only then continue.

`Convert to File`'s `sourceProperty` is a property NAME, not an expression — it cannot reach
sideways to another node, which is why that node in particular needs the payload re-emitted.

**Real incident (2026-08-03, #152):** a dedup guard was inserted before `Convert to File`;
every static order died on `The value in "wincodexContent" is not set` and 13 orders were lost.

## Proving a workflow fix — repro locally, and keep the repro OUT of this repo

There is no CI for these workflows, so the RED/GREEN proof is a local harness: pull the real
failing run with `get_execution … includeData:true`, then replay the Code-node bodies with
`new Function('$', '$input', code)` and stub `$('<node>')` to the captured outputs. That
reproduces the exact production error before the fix and passes after it.

Keep that harness in the session scratchpad, **never commit it** — it is built from a real
customer order (partner, EANs, quantities) and this repo is public.

## 2. A claim taken BEFORE a side effect must be released on EVERY failure path

The pattern `INSERT claim → do the side effect → mark done` is right, but the claim row is a
lie until the side effect actually happened. Wire the error output of **every** node between
the claim and the side effect (not just the upload node) into the release. Otherwise the next
attempt reads the orphan claim as "already done", skips the work and marks the message
processed — the order is gone and nothing says so.

When reviewing such a guard, ask: *which node could fail between the claim and the release?*
Each one needs `onError: continueErrorOutput` into the release path.

The remaining structural fix — an `uploaded_at` column so a claim and a confirmed send are
distinguishable — is #153.

## 3. A skip / duplicate branch must NEVER chain into the success logger

`Log … Event` nodes write `email_events` with `rollup=true`, and the DB trigger copies the
LAST rollup event onto `messages.proc_status/proc_outcome`. So if a skip branch is wired
`Log Duplicate Skip Event → Mark OK → Log OK Event`, the honest skip event is immediately
overwritten by `uploaded_orion / ok / "EDI vytvorené: <file>"`. The dashboard, the daily digest
and every audit then report a delivered order that never left the building.

Give skip/duplicate branches their own terminal marker node (`Mark Duplicate Handled`) that
updates `messages` but does **not** feed the OK logger.

## Verifying an order really reached ORION — read the Windows server, not the DB

`proc_outcome = "EDI vytvorené: X.txt"` only means a node ran. The ground truth is the file
on the ORION box (`orion_host` in the add-on options; SFTP with paramiko from inside the
add-on container, since the LAN address is not routable from the dev machines):

- `C:\ORION\COMMUNICATOR\data\in` — uploaded, waiting for WINCODEX/Communicator
- `C:\ORION\COMMUNICATOR\data\in\archCodex` — **already imported — this is the signal, not
  the `Z-` prefix** (corrected #151, 2026-08-03: live evidence showed a file already sitting
  in `archCodex` — therefore imported — with NO `Z-` prefix a full hour later, while the
  last `Z-` rename in `archCodex` had happened 5+ hours earlier. The `Z-` rename is a
  separate, infrequent, uncontrolled batch job Communicator/WINCODEX runs independently of
  the actual import — keying a check on it reports a safely-imported order as "unconfirmed"
  for hours. Check for presence in `archCodex` WITH OR WITHOUT the `Z-` prefix; never
  require the prefix. `KARMEN_`/`KOMFOS_` files, unrelated to us, keep their own name either
  way.)
- `C:\ORION\COMMUNICATOR\data\in\unconfirmed` — import failed

**Correction (2026-08-05, #133) — import is a MANUAL step, not an automatic sweep.** The
earlier claim here ("Communicator sweeps roughly every 25–30 min") was WRONG — files move
out of `in/` ONLY when pani skladníčka (the warehouse) manually clicks "prijať objednávky
z ORIONu" in CODEX, once each morning when she arrives. A file legitimately sits in `in/`
all evening, overnight, and over the weekend — that is NORMAL, not a stuck import. Real
incident: `confirm.py`'s original 60-minute-timeout model fired 5 separate per-file "stuck"
alerts at 18:18 for orders sitting unaccepted since the afternoon — a false alarm, deleted
by the user. Import confirmation (`app/orders/confirm.py`, keyed on `archCodex`/
`unconfirmed` presence, never on `Z-`) now alerts only on a genuine anomaly (`unconfirmed`
presence, or vanishing from all three folders) or a CARRYOVER (still unaccepted from a
PRIOR day, checked once the configured morning hour arrives, default 10:00, skipping
Saturday/Sunday by default) — grouped per incident, never one message per file. See
`confirm.py`'s own module docstring for the full model.

## Re-sending orders after an incident — order of operations is binding

1. Prove per order that it is **absent** from ORION (`in`, `archCodex`, `unconfirmed`, also
   with a `Z-` prefix). A run that already uploaded is NEVER re-run — that is a duplicate
   delivery in the warehouse.
2. Delete the orphan `edi_sent` claim FIRST, then reset the message
   (`processed=false, processing_at=NULL, processed_by=NULL, attempts=0`). Reversed, the
   dispatcher can pick the message up while its own stale claim still blocks it and mark it
   done again.
3. Release **one** order first, verify its file (name, size, line count, quantities against the
   source mail) in ORION, and only then release the rest.
4. Deleting claims by a `filename LIKE` pattern also deletes claims of orders already re-sent
   in this same session — re-insert those rows, or scope the delete by explicit ids.
5. Close out with: file present for every order, `attempts=1` per message, claim count ==
   distinct filename count, and `uploaded_orion/ok` count == order count with 0 skip / 0 error.

## `in_DL` (DESADV upload target) — live directory structure, resolves a doc conflict (#203)

The Python migration design spec flagged a real conflict in its own sources: does `in_DL`
have its own `archCodex`/`unconfirmed`, or does it share `in`'s? **Verified LIVE 2026-08-07
via read-only SFTP** (the add-on's own `orion_host`/`orion_user`/`orion_pass` options, same
credential the n8n "Granc server" SSH cred targets):

- `C:\ORION\COMMUNICATOR\data\in_DL` is a **SIBLING** of `in`, not nested under it, and has
  **NO `archCodex`/`unconfirmed` of its own** (`FileNotFoundError` on both when listed).
- DESADV files land in `in_DL` on upload, already carrying the `Z-` prefix (R89 — the upload
  ITSELF writes `Z-<filename>`, unlike ORDER_ files which upload unprefixed into `in`).
- Once Communicator imports a DESADV file, it moves into the SAME **shared** `in\archCodex`
  (confirmed live: 190 real `Z-DESADV_*` entries already sitting there, all Z-prefixed) —
  never a separate `in_DL\archCodex`.

So any future check of a DESADV upload's import status must: watch `in_DL` for "still
queued", but `in`'s own `archCodex`/`unconfirmed` for "imported"/"failed" — exactly the split
`app/orders/confirm.py`'s `_Ledger.queued_key` (`"in_DL"` for DESADV vs `"in"` for ORDER_)
encodes, while `archCodex`/`unconfirmed` stay unconditionally read from `in`'s base
(`upload.list_dirs()`). If a NEW ORION upload target is ever added, verify its real
directory structure live the same way — don't trust a design doc's prose description of
folder layout without an SFTP listing, the conflict here was real.
