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

## Re-shipping ONE missing document out of a multi-document DL message via the Python engine (#251)

The section above ("Re-sending orders after an incident") is for the OLD n8n `edi_sent`
ledger and a full whole-message reprocess. The **current Python DL engine** (`dl_worker.py`)
needs a DIFFERENT, narrower technique when the loss is an old n8n-era message that had
2+ delivery notes in one mail and only SOME of them ever reached ORION (the classic
`LIMIT 1` attachment bug, #204/#238) — because `desadv_sent` (the DL engine's own
two-phase claim ledger, started **2026-08-09**) has **no row at all** for any document
uploaded before that date. Resetting `messages.processed=false` and letting the live
worker reprocess the WHOLE message via `_claim`/`_process_message` would make the engine
treat the ALREADY-shipped sibling document(s) in that same mail as brand new and
genuinely try to re-upload them — a real duplicate delivery, and (for a message with 2+
missing documents, e.g. one mail with 3 attachments) it would also ship every missing
document of that message in ONE synchronous pass, violating a strict one-document-at-a-
time safety requirement.

The safe shape (used for #251's 3 messages / 4 documents, zero duplicates, zero code
changes): drive `dl_worker._process_document()` — the SAME function the live engine calls
per document — **directly**, one target `docNumber` at a time, never `_process_message`/
`_claim`:

1. Build the `message` dict from the real `messages` row (`dl_worker._as_message`), read
   its attachments (`dl_worker._read_attachments` — works fine even if the original PDF
   bytes were purged from `/data/store`, since `machine_text`/OCR text is stored
   separately and is what the extractor actually needs).
2. `dl_extract.extract_email(client, attachments)` ONCE — read-only, finds every document
   in the mail including the already-shipped sibling(s). Select only the ONE `docNumber`
   you intend to ship; never call `_process_document` for the sibling(s) at all (don't
   even pre-seed the ledger for them — simplest and just as safe, since they're just
   never touched).
3. **Shadow-preview first**: `_process_document(..., shadow=True)` — zero writes (uses
   `desadv.already_sent()` read-only), shows the built EDI's `outcome`/`line_count`/
   `items_skipped_no_match`. Pass a mutable `all_items=[]` list to the call and print it
   afterward — it gets the FULL per-item `Decision` (rule/confidence/trace), which the
   top-level `result` dict does NOT expose, and is what you need to actually understand
   an `outcome: partial`.
4. Only after reviewing the shadow output, re-run with `shadow=False` to actually claim +
   upload + confirm + post to Odoo (best-effort, see the Odoo-outage note below).
5. Verify in ORION (`upload.list_dirs(cfg)`, read-only) after EVERY live call before
   moving to the next document — never batch multiple live calls without verifying
   between them.

**LLM item-matching is genuinely non-deterministic across calls — don't panic on a single
`outcome: partial`.** One #251 shadow preview reported an item unmatched
(`items_skipped_no_match`); a second preview of the exact same document, seconds later,
matched it cleanly at 0.96 confidence with unanimous recent history support. This is
expected model variance, not a bug — the system is safe either way (an unmatched item
never ships silently, it always raises a `dl_item` board question instead). Re-run the
shadow preview once or twice if a result looks surprising before trusting it; if it stays
unmatched, that's real ambiguity, let the board question fire.

**This does NOT touch `messages.processed`/`processing_at`/`attempts`/`proc_status` at
all** — deliberately. It's a targeted per-document backfill, not a message-level
reprocess; the message's own historical lifecycle (already completed under the old n8n
workflow) is left alone. The audit trail lives in `desadv_sent` (the new confirmed claim)
and `email_events` (written by `_process_document` itself, `rollup=False`) plus whatever
you write to the originating ticket.

**A message whose Odoo post fails is NOT a sign the remediation failed** — `_process_document`
already treats posting as best-effort (R97: a posting failure never blocks the real
claim/upload/confirm, which happen BEFORE the post call) — check the `RESULT:`/ledger/
ORION evidence, not whether an Odoo traceback was printed.

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

## Never auto-retry an upload whose failure could have left bytes on the target (#239)

**Releasing a claim REMOVES the anti-duplicate protection — it does not make a retry
safe.** PR #256 added an automatic retry of a failed ORION upload, reasoning "the claim
was released, so a retry can safely re-upload without a duplicate" — backwards: the
claim being released is EXACTLY what removes the protection. Live incident chain
(`upload.py::put()` writes straight to the FINAL name with no temp-write + rename,
`desadv_edi.py::filename()` stamps a fresh timestamp on every attempt so the retry's
name can never collide with the first one, `desadv.py::release_send()` deletes the
ledger row on failure, and `TRANSIENT_RE` treats `timed out` as safely retryable) meant
a transfer whose BYTES landed but whose REPLY timed out got silently re-uploaded under
a second filename — two copies of one delivery note in ORION, both taken in at the
warehouse's next manual import. Fixed for real by removing the retry entirely
(0.9.71), then (0.9.73, #239) by two structural pieces together:

1. **A safe upload writes to a TEMPORARY name and renames to the final name only after
   the write completes.** A single SFTP `rename()` is atomic on the same filesystem —
   either it happened (final name present) or it didn't (only the temp name exists,
   under a name nothing else recognizes). This makes "is the final name present"
   trustworthy evidence of whether the transfer genuinely completed, regardless of
   whether the CLIENT received a confirming response.
2. **Presence must be checked by the document's STABLE IDENTITY (buyer/supplier EAN +
   doc number), never by filename.** A filename embeds a fresh per-attempt timestamp,
   so filename equality can never answer "did an EARLIER attempt already land this
   document" — only the part of the name that stays constant across every retry does
   (`desadv_edi.stable_prefix()`). Check that stable prefix against EVERY folder the
   document could legitimately be sitting in (queued, imported, import-failed) — a
   match in any of them means the upload already happened, whatever this attempt's own
   response said.

A safe automatic retry needs BOTH pieces together, checked immediately before deciding
to retry — never one alone, and never "the claim was released" as a substitute for
either. Any FUTURE upload-with-retry in this codebase (a different ORION target, a
different external transfer) should default to this same shape from the start, rather
than discovering the gap the expensive way.

**The retry itself IS wired now (finding 6's remainder, #239) — the claim is NEVER
released before it is proven safe.** `dl_worker._check_landed()` runs the stable-identity
presence check ONLY for a failure `TRANSIENT_RE` already classifies as transient
(everything else keeps the pre-#239 no-retry behaviour unconditionally). Four branches,
all inside the SAME upload `except` block in `dl_worker._process_document`: (1) the
document is already on ORION under an earlier attempt's name (reply lost, bytes landed)
— confirm the SAME claim, never a second upload; (2) genuinely absent everywhere — the
SAME claim stays held (never release-then-reclaim) through exactly ONE bounded retry;
(3) that one retry also fails — falls back to the pre-existing release+durable-alert
path; (4) the presence check itself cannot even be attempted (most likely: the SFTP
connection that just failed the upload is down for a follow-up listdir too) — same
release+alert fallback as (3), no blind retry ever. The three success paths (a clean
first-try upload, branch 1, branch 2-succeeded) all share ONE extracted closure
(`_finish_shipped()`) for the confirm/history/Odoo-post/event tail, deliberately never
duplicated — a second, independently-maintained copy of that tail is exactly the kind of
drift a future edit could silently desync. Any FUTURE upload-with-retry in this codebase
should reuse this exact shape (a `list_dirs=` DI seam threaded down to the call site,
`_check_landed()`'s `True`/`False`/`None` tri-state, one bounded retry, one shared
success tail) rather than re-deriving it.

## A SYNTHESIZED fallback identity feeding a claim/dedup key must be STABLE across retries too (#262)

The same "stable identity" principle above applies one layer EARLIER than the upload
itself: whatever VALUE gets fed into a claim/dedup key (here, `desadv_sent`'s
`(supplier_ean, doc_number)`) must be identical every time the SAME logical work item
is reprocessed — not just checked correctly at retry time. `desadv_edi.build()`'s
existing no-docNumber fallback (`_generate_doc_number()`, R83) is wall-clock based
(`datetime.now()` → MMDD-HHMM) — harmless while essentially unreached (a formal
printed doc almost always HAS a number), but #262 made "extraction found no
docNumber at all" the NORMAL case for an informal delivery announcement in mail body
text. A stale-claim reclaim or a transient-failure retry recomputes `built.doc_number`
fresh each time; a wall-clock fallback would hand `claim_send_or_identify()` a
DIFFERENT key on each attempt, and a genuinely-already-shipped document would look
brand new on retry — the exact double-upload risk the section above exists to
prevent, just one decision earlier (choosing the document's IDENTITY, before the
first claim attempt is even made). Fixed with `desadv_edi.generate_stable_doc_number
(message_id)` — deterministic (sha256 of the originating message's own stable id),
synthesized by the CALLER (`dl_worker._process_document`) before ever handing an
empty docNumber to `build()`, so `build()`'s own wall-clock fallback is never reached
from the live worker at all. Any FUTURE feature that needs to claim/dedup an entity
with no natural stable identifier (no printed number, no external reference) should
derive it from something that is ALREADY guaranteed stable per retry (a message id, a
row id) — never from wall-clock time, a random value, or anything else that changes
between attempts of the SAME logical item.

## A supplier that sends a mail-body-only CORRECTION/AMENDMENT (#258 path) can silently
ship an INCOMPLETE delivery — the DL engine has zero cross-message memory (#236, #265)

`dl_extract.extract_email()`/`dl_worker._process_message()` extract a document from
EXACTLY one message's own text (`_mail_body_only(combined_text)` for the #258 body-text
path) — nothing anywhere correlates two DIFFERENT messages as "the same physical
delivery". A supplier that follows the printed-document convention (one doc = one
mail = one full doc) is fine; a supplier that writes the delivery straight into the
mail body (HK LOAN, `gnip@hkloan.eu`) routinely sends a SHORT follow-up mail that only
restates the CHANGED line and says "zvyšok bez zmien" ("rest unchanged") — e.g.
"OPRAVA HMOTNOSTI" (subject) with `Múka pšeničná T650 = 15,88 ton (nie 17,74 ton)` and
nothing else, correcting one line of an earlier 3-item mail. **Verified live** (read-only
`dl_extract.extract_email()` call against the real correction mail's own text, no
writes): the extraction returns exactly ONE item — the two items the correction never
repeats are silently gone, and nothing detects it (no exception, no completeness
warning — `#238`'s own missing-document check only catches an attachment that produced
ZERO documents, not a document that produced FEWER items than the physical delivery
actually had).

**Compounding gap: `dl_worker.release_for_question()` reprocesses ONLY the ONE message
its `qid` is tied to** — `teach.ask_dl_supplier()`/`ask_generic()`'s dedupe
(`ON CONFLICT (customer_ean, item_key) WHERE status='open' DO NOTHING`) means only the
FIRST successful `ask` call per sender wins a row; every later `dodacie_listy` message
from that same still-unregistered sender is left `processed=true`/`proc_status=review`
with **no `order_questions` row linked to it at all** — verified live: HK LOAN had 5
such orphaned messages with zero tied questions, vs. exactly 1 (the newest) tied to the
sklad's open `dl_supplier` question. None of the 5 will ever auto-unstick; only the one
tied message reprocesses when the question is answered.

**Diagnostic pattern used to prove both of these (reusable for any future "what would
the engine actually DO with this text" question)**: `docker cp` a small script into the
add-on container, run it with `PYTHONPATH=/app` — `dl_extract.extract_email(client,
[{"machine_text": ..., "pdf_bytes": b""}])` for a pure extraction check, or
`dl_worker._match_item(client, item, catalog, recalled=None, partner_name=...)` for a
pure item-matching check against the REAL current `dl_snapshot.load_catalog()`. Both
are read-only (no claim, no upload, no Odoo post) even though they make a real
`gpt-5.4` call — safe to run against production data/catalog without any `shadow=`
plumbing, since neither function touches the DB or an external system.

Fix is intentionally NOT baked in ad-hoc — filed as `#265` (`Scope-gate:
needs-user-decision`) with the proposed directions (force-review anything mail-body
sourced whose subject looks like a correction; widen `release_for_question` to also
re-check sibling same-sender stuck messages) since there are several valid designs with
real automation-vs-safety tradeoffs. Any FUTURE mail-body-sourced-document engine
(should this pattern extend to another supplier) should assume the SAME two gaps exist
until `#265` actually ships a fix.
