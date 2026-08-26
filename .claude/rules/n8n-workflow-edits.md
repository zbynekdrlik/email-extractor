# Editing the n8n consumer workflows (static orders, dodacie, faktúry, reklamácie)

The order/delivery-note pipelines that still live in n8n have no tests and no CI — a bad edit
ships straight to production and can lose a real customer order. These three rules come from
incidents, not theory. Read them before touching any of those workflows.

Workflow ids: static orders `O8IYhUESjaWmPMTI`, dodacie `1R4WcUFhpIPwEJX1`, faktúry
`du2O6YGmGyntXBbV`, reklamácie `LIpkBHdpcYN7YMdM`, dispatcher `TjIzExr4uUs5f4Ci`,
ai orders `wlORIhkVZISCdZNmBTM4Z`.

## The n8n-pz MCP is reachable from an isolated worktree dispatch — don't assume otherwise (#271)

A dispatch prompt hedged "the n8n-pz MCP is not available to you" (reasoning that a
worktree-isolated `autopilot-worker` might not inherit the parent session's MCP
config) — this was WRONG in practice: `mcp__n8n-pz__get_workflow_details` and
`search_workflows` worked identically from inside a `.claude/worktrees/agent-*/`
dispatch as from an interactive session, live-verified 2026-08-13. Try the MCP tool
directly before assuming it's unavailable and falling back to a weaker method (a
"pin, not live check" design, a stale-assumption comment, etc.) — a dispatch's own
hedge about tool availability is a guess, not a fact, and checking costs one call.

**Live status snapshot (2026-08-13, via this MCP): `Email Dispatcher`
(`TjIzExr4uUs5f4Ci`) has `Trigger AI Orders`/`Trigger Static`/`Trigger Dodacie` all
`disabled: true` — n8n no longer dispatches `ai_orders`/`static_orders`/
`dodacie_listy` at all, the Python engines fully own them.** Both `AI auto orders`
(`wlORIhkVZISCdZNmBTM4Z`) and `Static auto orders` (`O8IYhUESjaWmPMTI`) are
themselves `active: false`. Their own claim-query nodes still carry a live,
hardcoded re-claim window worth pinning against (`Get AI Orders`: `interval '30
minutes'`; `Get Static Orders`: `interval '10 minutes'` — see
`.claude/rules/orders-corpus.md`'s `CLAIM_STALE_MINUTES` entry for what this backs),
but there is no LIVE n8n process to race against for either category today — only a
rollback-time convention worth keeping in sync. Re-verify this snapshot live rather
than trusting it forever if a ticket's correctness depends on it (workflows can be
re-enabled).

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

## #265 shipped: correction-mail detection + sibling-release widening — two reusable
## gotchas a deep-review pass caught, both worth checking on ANY future similar feature

`#265` shipped both gaps above: `dl_worker._looks_like_correction`/
`_correction_review_reason` route a mail-body-sourced (#258) correction/amendment mail
straight to manual review (never extraction, never a model call, never a claim/upload)
with wording that tells the warehouse to fix an already-imported document manually in
CODEX; `dl_worker._release_stuck_siblings` (called from `release_for_question` only for
a `dl_supplier` answer) resets other orphaned same-sender stuck messages back into the
normal `_claim()` pool. A fresh-context adversarial review of the diff (per
`agents/autopilot-worker.md`'s CYCLE step 6 shape) caught 2 real, proven safety bugs
before merge — both are reusable lessons for ANY future feature in this codebase, not
just this one:

**A plain-ASCII regex stem for a Slovak word family structurally cannot match that
word's OWN diacritic-inflected forms — don't assume it does, PROVE it with a real
`re` probe against the literal cited forms.** The first cut of the "dopln" detection
stem (`r"\bdopln\w*"`) was written because the ticket's own issue text named the risk
class as "DOPLŇUJÚCE/OPRAVNÉ maily" — but "DOPLŇUJÚCE"/"doplňujúce"/"dopĺňame"/
"doplňte" all replace the plain ASCII `l`/`n` with the Slovak diacritic letters
`ľ`/`ĺ`/`ň` (different Unicode codepoints, NOT case variants — `re.IGNORECASE` does
correctly case-fold `Ň`↔`ň`, but a fixed ASCII `n` in the pattern will never match `ň`
regardless of case flags). This was a genuine false-NEGATIVE that would have silently
auto-shipped exactly the incomplete-delivery mail the whole ticket exists to catch —
caught only because a review pass ran `dl_worker._looks_like_correction(form, "")` for
each of the four forms the ticket itself quoted, rather than trusting the regex "looked
right". **Any future Slovak-word-stem regex in this codebase (or similar diacritic-rich
language matching) needs the SAME empirical check**: list every inflected form the
motivating text actually cites, then run the candidate pattern against each one in a
throwaway Python probe (`re.compile(pattern, re.IGNORECASE).search(form)`) BEFORE
trusting it — never assume a stem "obviously" covers its own word family.

**"No question was ever raised for this message" is NOT proof it is safe to auto-
recover — a message can also be `processed=true`/`proc_status='review'` with no
`order_questions` row because a REAL external side effect (an ORION upload) already
FAILED, not because nothing was ever attempted.** `_release_stuck_siblings`'s first
cut reset any same-sender message matching `processed=true AND proc_status='review'
AND no order_questions row` — which also matches a message whose upload genuinely
timed out (`_process_document`'s upload-except branch calls `desadv.release_send()`,
deleting the claim, and returns a plain `review` outcome with NO question, since an
upload failure isn't a "which card is this?" ambiguity). Resetting that message would
have re-enabled exactly the automatic upload-retry `#239` deliberately removed (a
released claim + a fresh per-attempt filename can genuinely re-upload a document that
already landed — see this file's own "Never auto-retry an upload" section above).
Fixed by requiring a THIRD, POSITIVE exclusion: `AND NOT EXISTS (SELECT 1 FROM
email_events e WHERE e.message_id = messages.message_id AND e.status = 'error')` —
every genuine processing exception in `dl_worker.py` (a supplier-match exception, an
upload exception, an attachment-extraction error) logs `status='error'`, while a plain
"nothing matched" outcome logs `status='review'`. **Any FUTURE feature in this
codebase that auto-recovers/re-releases a stuck message based on its CURRENT state
columns must apply the same check**: absence of a positive signal (a question, a
claim) is not the same as absence of a NEGATIVE one (a logged failure) — query for
both before deciding something is safe to retry.

## Merging two branches that each depend on the OTHER's safety invariant — prove the
## composition with ONE test through the REAL merged code, never trust two isolated tests

Integration round B (2026-08-13) merged #239 (safe automatic ORION upload retry,
restructured `_process_document`'s upload except-block into `_check_landed`/
`_finish_shipped`/`_alert_and_release`) and #265 (`_release_stuck_siblings`, whose
`status='error'` exclusion above exists SPECIFICALLY to stay compatible with #239's
retry) — two branches built and adversarially reviewed in COMPLETE isolation, each
0 🔴 0 🟡 0 🔵, each with its own regression tests all green. Neither branch's own test
suite could have caught a REAL regression in the seam between them: #265's own
regression test for the exclusion (`test_release_for_question_sibling_widening_never_
touches_unrelated_messages`) manually `INSERT`s a synthetic `email_events` row instead
of driving a real failure through the code — it pins the SQL predicate, not the actual
interaction. Had #239's restructuring changed WHICH stage/status `_alert_and_release`
logs (a plausible refactor slip), that hand-built fixture would still pass, silently
lying about the merged system's real safety.

**The fix pattern, reusable for any future merge of two branches with an inter-
dependent safety property:** after resolving the textual conflict, write ONE NEW test
that drives the FULL real pipeline through BOTH features together (here:
`test_sibling_release_still_excludes_a_message_whose_upload_genuinely_failed_through_
the_merged_retry_path` — a genuine transient upload failure + the one bounded retry,
both failing, landing in the real merged `_alert_and_release`, THEN answering a
sibling's `dl_supplier` question and checking `_release_stuck_siblings` against what
that real closure produced). A test built from hand-inserted fixture rows proves the
QUERY is correct in isolation; it can never prove the PRODUCER and the CONSUMER of that
data still agree after either side changes shape.

**Non-obvious event-log gotcha this test's own first draft tripped on:** the LATEST
`email_events` row for a message that ends in `review` is usually NOT the diagnostic
`status='error'`/`status='review'` event a specific closure logged (e.g.
`_alert_and_release`'s own `_event(..., stage="review", status="error", rollup=False,
...)`) — `_run_and_finish`'s own ROLLUP summary event, logged immediately afterward via
`report.log_event(..., stage=result.get("status", "ok"), status=result.get("status",
"ok"), rollup=True, ...)`, reuses the SAME `stage="review"` (since `_aggregate_status`
returns `"review"` for an all-review document set) with `status="review"` — always the
newer row. Querying "the latest `stage='review'` event" for a message therefore reads
the ROLLUP, not the specific failure diagnostic. Any future test (or dashboard query)
that needs to distinguish WHY a message ended in review must check for EXISTENCE of
the specific `status` value it cares about (`_release_stuck_siblings`'s own
`NOT EXISTS (... status = 'error')` pattern), never "the most recent event for this
message" — the rollup summary is always more recent than the diagnostic event that
caused it.

## Remediation replay (#241, 29 old stuck DL messages) — three things the #251 path doesn't warn you about

Doing a full batch of #251-style direct `_process_document()` replays (5 documents,
one at a time, shadow-then-live) surfaced three gotchas the #251 section above doesn't
cover:

1. **`db.connect()` uses `autocommit=True`** (`app/db.py`). A replay script never
   needs (and should never write) `conn.commit()`/`conn.rollback()` — every statement
   already lands the instant it executes. Shadow mode's own "zero writes" guarantee
   comes from `_event`/`_post`/`teach.ask_*` being gated on `not shadow` inside
   `_process_document` itself, never from a rolled-back transaction.

2. **A shadow-preview's extracted `docNumber` (and sometimes the outcome itself) is
   NOT guaranteed identical across two calls on the exact same PDF text** — it's an
   LLM extraction call, not a deterministic parse. Live example: the same Messer
   Tatragas attachment returned `docNumber="AVIZO5336710511"` on preview call #1,
   `docNumber=""` on live call #2, and `"AVIZO5336710511"` again on preview call #3 —
   same text, same prompt, three different answers on the field alone. For a
   single-document message this is harmless (there is only one document to mean), but
   a replay script that matches the live call's target document by exact `docNumber`
   string must fall back to "the sole extracted document" when there is exactly one —
   never silently refuse just because the string drifted. **Item MATCHING can drift
   the same way** (already documented playbook-wide, #251's own section above) — but
   this is the FIRST time doc-HEADER extraction itself was observed to drift, not just
   item-level matching.

3. **A shadow-preview's outcome can flip from "review" to "ok" between the original
   investigation and the actual replay, with NO code change in between** — because
   `dl_memory`'s alias/history rescue means a product the catalog genuinely lacked on
   the day of the original comment can match today via `alias_rescue` learned from a
   LATER, unrelated shipment of the same supplier (two of five #241 group-C documents
   did exactly this: an item genuinely unmatched weeks ago matched cleanly today).
   **Always re-run the shadow preview immediately before the live call, even when an
   earlier investigation already characterized the document** — never replay live off
   a preview that is more than a few minutes old. When a preview surprises you with
   "ok"/"partial" instead of the expected "review", the FULL safety chain still
   applies (ORION stable-identity check before AND after), never skip it just because
   the earlier investigation said "will raise a question".

4. **`_read_attachments()`'s PDF/image-only filter (`_ATTACHMENT_MIME_RE`/
   `_ATTACHMENT_EXT_RE`, `dl_worker.py`) is invisible unless you go looking** — a
   supplier whose delivery note is a `.xls`/`.xlsx`/`.docx` attachment gets ZERO
   usable attachments and ZERO extracted documents, with no distinguishing signal
   anywhere in `messages`/`email_events` that says "wrong file type" instead of
   "catalog gap" or "extraction failed". `app/extract.py`'s ingest-time extraction DOES
   read `.xls` text fine (`attachments.method='xls'`, `extracted_text` populated) —
   the gap is specifically `dl_worker`'s own narrower, deliberate scope filter (its own
   docstring calls this out as an intentional decision, not an oversight). Before
   concluding "catalog gap" for any DL document that produced NO extracted documents
   at all, check `attachments.mime`/`method` for that message first — a `.xls`/`.docx`
   attachment reads as "0 documents extracted" exactly like a genuinely bad scan does,
   but the fix is completely different (and is a scope decision only the user can make
   — see #297).

The ORION stable-identity check itself needs NO hand-rolled logic — it already exists
as production code, use it directly rather than re-deriving the folder-tolerant prefix
match narratively described elsewhere in this file:
`desadv_edi.already_landed(upload.list_dirs(cfg), supplier_ean, doc_number)`
(`app/orders/desadv_edi.py`, built for #239's own safe-retry decision — tolerant of the
`Z-`/`Z-Z-` ORION wire-prefix quirks, checks `in_DL`/`archCodex`/`unconfirmed` in one
call).

## Two documents extracted from the SAME message can get the SAME `docNumber` from the
## LLM header extraction — harmless when neither has a GTIN-matched item, but a real
## claim-collision risk to check for the moment either DOES (message 2183 remediation,
## integration round C2, 2026-08-13)

Message 2183 (Bardusch, #297's own .xls delivery-note case) had TWO `.xls` attachments,
each producing its own document via `dl_extract.extract_email()` — both came back with
the IDENTICAL `docNumber` ("AVIZO6875023752"), a model-extraction quirk (both attachments
likely share the same visible header text/reference), not a real duplicate document (the
two have different `deliveryDate`s and different single line items). This was harmless
here ONLY because BOTH documents had 0 GTIN-matched items (workwear, no match in the
SLOVNORMAL catalog) — `desadv_edi.build().can_create` was `False` for both, so
`_process_document` returns at the "cannot create EDI" branch and NEVER reaches
`desadv.claim_send_or_identify()` at all; two live calls, one per document, produced
two independent `review` outcomes + two separate board `dl_item` questions (#43, #44)
with zero claim interaction.

**If a future multi-document-per-message case has EITHER document actually reach the
claim stage with a shared `docNumber`, the SECOND live call would either correctly
identify itself as "already sent" (this message's own `holder == message["message_id"]`
branch, per `#216`'s two-cause distinction already documented above) — SAFE — or, if the
two documents are genuinely DIFFERENT physical deliveries that only coincidentally share
an extracted docNumber, the second would be wrongly treated as an already-shipped
duplicate and silently dropped — UNSAFE, a real document loss, not a double-ship.** Before
running a second live document from the SAME message whose shadow preview showed the
SAME `docNumber` as an already-processed sibling, verify by hand whether they are
genuinely the same document (in which case skipping the second is correct) or two real,
distinct deliveries the extraction merely mislabeled the same way (in which case widen
the investigation — a docNumber collision this ticket's own remediation never needed to
resolve, since neither doc ever reached the claim stage).

## Making the system ANSWER a nástenka board question on the user's behalf — go through
## the SAME app path a human answer takes, never a bespoke close (#323)

When a feature must auto-answer an open `dl_supplier`/`dl_item` (or any `teach.KINDS`)
question — e.g. #323's "adding a CODEX card auto-closes that supplier's open question so
the sklad nemusí na nástenke" — do NOT hand-write a bespoke close. Replicate
`_api_orders_answer_generic`'s own tail exactly, so every side effect the human path fires
(memory write, `release_for_question` reprocess, `_release_stuck_siblings`) happens
naturally:
1. `teach.add_candidate(conn, qid, {"value": <pick>, "label": <name>})` to legitimize the
   pick (so `apply` records the display name; `add_candidate` is a `status='open'` no-op if
   already closed).
2. Guarded `UPDATE order_questions SET status='answered', answer=%s, answered_by=%s,
   answered_at=now() WHERE id=%s AND status='open' RETURNING id` — the `WHERE status='open'`
   is load-bearing (a concurrent human answer must win; on 0 rows, DO NOT call apply).
3. `teach.KINDS[q["kind"]].apply(conn, cfg, q2, <pick>, <by>)` with a DISTINCT `answered_by`
   marker for auditability (`'codex-card-auto'`) — this is what fires `dl_supplier_memory.
   remember` + `release_for_question`.
`deps.db()` is autocommit, so step 2 lands before step 3's real ORION upload (the #116
answer-commits-before-upload separation holds via autocommit, no explicit tx needed).

## A retro-release / auto-answer that runs INSIDE an HTTP request AND can trigger a real
## LLM/ORION reprocess needs PER-ITEM error isolation (#323)

`release_for_supplier_card` runs synchronously inside the `/znalosti` card-save POST, and
each auto-answered question's `release_for_question` reprocess can make a real model/ORION
call. Wrap each per-item auto-answer in `try/except Exception: log.exception(...)` so one
transient failure never aborts the whole batch (remaining questions + the other rungs) nor
500s an already-saved card. Safe because everything is autocommit + idempotent (the guarded
`status='open'` UPDATE makes a retry a no-op on already-answered ones) and the worker's own
`_claim`/`tick` stays the backstop.

## A not-yet-extracted stuck message's ONLY queryable supplier-identity signal is envelope
## `from_name` — use it as a conservative SELECTOR, never as the final supplier decision (#323)

For the `emails=[]` retro-release case (`_release_stuck_siblings_by_name`), a stuck DL
message has no extracted supplier name in a column — only `messages.from_name`
(envelope display name) and `from_addr`. Match `dl_match.supplier_name_key(from_name)` ==
the card's UNAMBIGUOUS normalized name (only when no other distinct-ean card shares that
normalized name — same DISTINCT-ean ambiguity measure `resolve_supplier_from_cards` uses).
This is SAFE despite `from_name` being a weak signal because releasing only RE-QUEUES the
message: the reprocess re-extracts and re-runs the deterministic rung on the REAL document,
so a false-positive `from_name` match never ships a wrong EDI — it either resolves correctly
or raises a fresh question. Apply ALL the #265 exclusions in the SQL (`processed=true`,
`proc_status='review'`, no `order_questions` row, no `status='error'` event). Normalization
(`dl_match.supplier_name_key`, promoted from private in #323) is the reusable public helper —
never re-derive the diacritic-folding (Slovak-stem-drift risk, see orders-corpus.md).

## HOLD a shippable DL document with an unmatched warehouse item — never partial-ship it (#365)

A delivery note with ≥1 matched item AND ≥1 genuinely-unmatched warehouse item used to upload a
PARTIAL EDI to ORION IMMEDIATELY (dropping the unmatched line) while raising a `dl_item` board
question that could only teach the FUTURE — the warehouse then added the missing row in ORION by
hand (live incident: prod msg 8804 / question 101 — the ~90 g "Buchta maková nebalená" vs 56 g
cards; the matcher was CORRECT, it was the partial-ship POLICY that was wrong). `dl_document.
_process_document` now HOLDS such a document (no claim, no upload, `outcome="review"`), posts the
❗ `build_review` "potrebuje kontrolu / Rieš na nástenke" message (never the ⚠️ partial-upload
one), and lets `release_for_question` re-run the whole message on the board answer — a taught
card ships the COMPLETE EDI; a new "Nemá kartu — pošli bez tejto položky" board answer ships it
partial-yet-human-confirmed. Reusable design points (all cost a review round to get right):

- **The hold gate keys on lines that ACTUALLY got a board question (`held_items`), NOT merely on
  "unmatched".** `ask_dl_item` REFUSES to ask (returns `None`) for a human-taught-yet-unmatched
  line (the #236 R75/R74 tripwire class — a confident model pick that bypasses the memory rescue
  and trips the lexical gap) or a blank name. Holding such a line is a permanent DEAD-END (no
  question row ⟹ `release_for_question` is structurally unreachable ⟹ stuck forever, a regression
  on the old partial-ship). So collect the qid `ask_dl_item` returns (fresh OR deduped-onto-an-
  existing-open) and hold ONLY those; an ask-refused line is excluded from the EDI and the doc
  ships partial exactly as before. Any FUTURE "hold until resolved on the board" gate in this
  engine must gate on a question genuinely existing, never on the raw match verdict.
- **The "ship-without" skip is a SENTINEL (`teach.DL_ITEM_SHIP_WITHOUT = "ship_without"`, never a
  real GTIN) recorded on the ANSWERED question row (`answer->>'choice'`), read back on reprocess
  by `dl_document._skip_answered_item_keys(message_id)`.** The lookup reconstructs the key via the
  SHARED `teach.dl_item_key(supplier_ean, wording)` — the exact key `ask_dl_item` stored the
  question under — so the two can never drift on the diacritic-folding normalization (same "never
  re-derive the folding" rule as `supplier_name_key`). No new table; `order_questions.answer` is
  the durable store; `_undo_dl_item` clears the skip naturally (`answer=NULL`).
- **A deduped same-sender sibling strands FOREVER now that it holds — `release_for_question` fires
  `_release_stuck_siblings` for a `dl_item` answer too (was `dl_supplier`-only, #265).** Two mails
  from one supplier with the SAME unknown wording: the 2nd's ask DEDUPES onto the 1st's open
  question (`ask_generic` `ON CONFLICT DO NOTHING`), so the 2nd has NO own `order_questions` row.
  Answering the 1st reprocesses only ITS message; the 2nd (a processed orphan) is now re-queued by
  the existing from_addr-keyed widening (safe — release only re-queues, the claim guard still
  blocks re-upload). Before #365 the 2nd partial-shipped so the strand was invisible; the hold
  made it lossy.
- **Shadow (the e2e-dl corpus) stays byte-identical — the hold gate + skip lookup are LIVE-path
  only (`if not shadow` / after `if shadow: return`).** The corpus measures MATCHING ("partial" =
  shippable-but-incomplete), never the live HOLD policy layered on top, so a partial→hold policy
  change causes ZERO corpus drift. Any future outcome-POLICY change (not a match change) should be
  gated the same way so the corpus keeps measuring matching alone.
- **Skip the hold when the doc is ALREADY on ORION** (`desadv.already_sent`, read-only) — else a
  doc still carrying a second pending line after an earlier ship posts a misleading "NEnahráva do
  ORIONu" while it IS in ORION; falling through lets `claim_send_or_identify` log the duplicate.
- **Post-deploy: the new skip button renders on a REAL open dl_item card WITHOUT answering it** —
  navigate `/otazky-dl` (admin or the dl_key), read the DOM buttons for "Nemá kartu — pošli bez
  tejto položky", never click it (answering defers/ships a real customer DL). If no live question,
  grep the served ASK_DL_HTML for the button text (proves the template deployed). Both dl_item
  card renderers were edited (ASK_DL_HTML `dlItemQuestionCard` + DASH_HTML's admin generic block) —
  template hashes DASH/ASK/ASK_DL re-pinned (`# airuleset:secret-ok` for the hash blob).
## `desadv_edi.generate()`'s R84 ladder normalizes a line to the CARD's tracking unit —
## add a new unit FORM as its own exact-token branch scoped to kg-tracked, never a
## supplier hack; the catalog `cena` is €/kg so a per-ton→per-kg conversion divides the
## LINE price only, never `cat_price` (#366, 2026-08-26)

The DESADV LIN quantity/price must be in the card's OWN tracking unit: **kg for a
kg-tracked card (`sklad == "100"`), pieces otherwise.** `generate()`'s R84 ladder
(inside `elif is_kg_tracked:`) converts the extracted (quantity, unit) into kg. Before
#366 it knew only two rungs — `unit == "kg"` (identity) and per-piece `mass > 0`
(`qty × mass`) — with an `else` that left the quantity UNCONVERTED and only
`log.warning`ed. So a **tonne** delivery (`unit` = `"ton"`/`"t"`, mass blank — the norm
for bulk flour/salt cards, which carry a WEIGHT-NEUTRAL name + blank `mass` per the
"Great" note above) fell into that `else` and shipped `qty` as-is → ORION imported e.g.
`2 kg` instead of `2000 kg` (live msg 8700, warehouse "2000kg"; ≥2 suppliers — HK LOAN
flour + a salt supplier using `t`; 12 `ton` + 3 `t` lines). Fix = a general ton→kg rung
(`_TON_UNITS` exact-token set + `_is_ton_unit()`, `out_qty = qty*1000`,
`unit_price = up/1000`, `override_unit = "kg"`), checked BEFORE the kg/mass rungs (a
tonne is 1000 kg regardless of any per-piece mass), scoped to kg-tracked cards.

Reusable rules any FUTURE unit/quantity work on `desadv_edi.py` should follow:
- **The rule is keyed on the CARD's unit (kg-tracked), never on a supplier/card name** —
  it must be general (the ticket's own requirement, and the bug spanned ≥2 suppliers).
- **A unit-token matcher is EXACT-token (diacritic-folded via `_to_win1250`, `.rstrip(".")`),
  never a substring/prefix** — the real DL unit vocabulary includes `kt` (kartón), `ba`,
  `ks`, `kus`, `balení` (Czech spelling occurs in real data), any of which a loose match
  would corrupt. Cover Slovak AND Czech inflections (`ton`/`tona`/`tony`/`tonu` +
  `tuna`/`tuny`/`tunu`/`tun`) — a missed form silently re-ships the ×1000 error.
- **The catalog `cena` (`cena_by_gtin`) is stored €/kg for a kg-tracked card** (verified:
  flour 0.368, salt 0.242 — €/ton would be impossible). So a per-ton→per-kg conversion
  divides the LINE's own extracted price by 1000, but the R85 fallback substitutes
  `cat_price` UNDIVIDED. Doing the conversion BEFORE R85 keeps its `cat_price*5 /
  cat_price/5` comparison apples-to-apples (both €/kg).
- **`generate()` is byte-pinned** against `fixtures/desadv_reference.json` — a new unit
  branch is safe only because no fixture case uses that unit; the parity test must still
  pass. Regression coverage for an EDI-generation bug lives in `test_orders_desadv_edi.py`
  (direct `generate()` assertions on the LIN field slices: price `[82:91]`, qty
  `[96:108]`, unit `[108:111]`), NOT the DL corpus — `dl_evaluate` scores the MATCHING
  decisions (extracted qty/unit), which are UPSTREAM of `generate()`, so the corpus is
  structurally blind to a byte-generation bug (same class as the #247/#205 unit-test note).

**Ground-truth check for any "wrong quantity/price in ORION" DL incident — read the
actual EDI BYTES, don't infer from `order_runs`.** A READ-ONLY container SFTP probe using
the add-on's own `upload._connect(cfg)` + `sftp.open(path,"r").read()` reads the shipped
`DESADV_*` file (in `in_DL` if still queued, or `in\archCodex` once imported, tolerating
the `Z-`/`Z-Z-` wire prefix). Parse the LIN fixed-width fields at the offsets above to see
exactly what ORION received (qty/price/unit) vs what it should be. `order_runs.result` shows
the pre-EDI decision (extracted `quantity: 2, unit: ton`) but NOT the shipped bytes — the
file itself is the only proof of the ×1000. Never write/rename/delete on ORION.
