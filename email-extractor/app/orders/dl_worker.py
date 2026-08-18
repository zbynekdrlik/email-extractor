"""Delivery-notes (DL) worker — #204, DL migration F5.

Wires F2 (`dl_extract`), F3 (`dl_match`/`dl_memory`/`dl_supplier_memory`/`teach`'s
`dl_item`/`dl_supplier` kinds) and F4 (`desadv_edi`/`desadv`/`upload`) into ONE worker
loop, per the binding spec (`docs/superpowers/specs/2026-08-07-delivery-notes-python-
design.md`, R1-R17, R60-R97, W1-W16, §4). Three modes, the SAME shape `static_worker.py`
already uses (see that module's own docstring):

- `delivery_notes_engine=n8n`, `delivery_notes_shadow=false` (DEFAULT) — completely
  inert. The live n8n "Dodacie Listy EDI" workflow keeps running unchanged.
- `delivery_notes_engine=n8n`, `delivery_notes_shadow=true` — runs the FULL pipeline
  (extraction, matching, EDI build) for comparison only: **claims nothing, uploads
  nothing, marks nothing, teaches nothing** — every DB write in this module is gated on
  `not shadow`. A shadow "duplicate" verdict is read via `desadv.already_sent()`
  (read-only), never `desadv.claim_send()` (which always writes).
- `delivery_notes_engine=python` — claims per MESSAGE (R10: atomic claim, 30-min stale
  reclaim, R11: quarantine at 5 attempts) but decides PER DOCUMENT (R16/W1a fixed by F2:
  every attachment is processed, and one attachment can carry more than one delivery
  note). A message is marked `processed` only once every document extracted from it has
  reached a terminal per-document outcome (ok/partial/review/duplicate) — which, because
  this worker processes every document of a claimed message in ONE synchronous pass (not
  across several ticks), is naturally true every time EXCEPT the R17 transient-retry
  path (`_RetryLater`, below), which leaves the message unmarked for the 30-min reclaim.

**R17/W9 retry semantics are implemented explicitly here, NOT via `worker.tick`'s generic
exception-retry** (which retries ANY exception up to `attempts=5` with no
transient/non-transient distinction — that does not match R17). `_check_retry` classifies
a caught LLM/vision failure against `TRANSIENT_RE` (the same phrase list n8n's own
"Retry transient?" node uses, with NO bare digits — a reason routinely carries money
amounts) and `message["attempts"] < TRANSIENT_RETRY_LIMIT` (3, matching W9's own
"attempts 1-2 retry, attempts 3-4 review, quarantine at 5" reading of the claim's
already-incremented `attempts`). Within that window it raises `_RetryLater`, caught ONLY
by the live-engine branch of `tick()`, which leaves `processing_at` untouched (R10's own
30-minute stale window is what makes it reclaimable) and marks NOTHING. Outside that
window — a non-transient failure, or a transient one that has already exhausted its
retries — the SAME failure becomes a per-document/per-attachment "review" outcome
(`dl_report.build_review`) and processing continues with the rest of the message; the
message is then marked processed as normal (this is R17's own "Attempts 3-4 or
non-transient reason -> Odoo review" — mirrored, not the "retry forever" a bare
`try/except` around the whole tick would give).

**`order_runs`/`order_items` are reused UNMODIFIED (F1's #200 design decision)**, with one
technical resolution F1 itself did not need to make: `order_runs.snapshot_id` has a FK to
`order_snapshots(id)`, NOT `dl_snapshots(id)` — a DL run passes `snapshot_id=None`
(the column is nullable) to `worker._start_run`/`_finish_run` and stashes the REAL DL
snapshot id inside `result["dl_snapshot_id"]` instead. `result["kind"] = "dl"` is the
only discriminator a later reader needs (`reliability.py`'s own `#204` section explains
why every AI-orders query now excludes it explicitly).

**Duplicate documents (W7) and an announced-vs-attached mismatch (spec §4) are NEVER
posted as their own immediate Odoo message** — both are logged to `email_events`
(`dl_report.log_duplicate`/`log_announced_mismatch`) and surfaced in the DAILY digest
(`reliability.dl_provenance_stats_for_day`) instead, per the spec's explicit "hlásenie v
dennom sumári (nie ticho)" for W7. The announced-vs-attached scan (`_subject_doc_numbers`)
deliberately covers ONLY the documented Lunys "IS KARAT" subject shape
(`<digits>LT<digits>`, e.g. "2610LT0100000001") — a supplier with a different subject
convention simply has nothing to compare against (a safe default: no false positives,
never a false "you're missing a DL" alert for a subject shape nobody has verified yet).

**A refused DESADV claim is NOT always a genuine W7 duplicate (#216).** R17's retry
re-processes the WHOLE message on its next tick — including a document that already
shipped successfully in an earlier, partially-failed attempt of THIS SAME message.
`desadv_sent.message_id` records who currently holds the claim; `_process_document`
calls `desadv.claim_send_or_identify()` (the claim decision AND the current claimant's
read, ATOMICALLY in one round trip — never `claim_send()` followed by a separate
`claimed_by()` read, which would leave a TOCTOU gap) to tell "this message is
retrying itself" (logged as `already_shipped_this_run`, excluded from the digest's
`duplicates` count) apart from "a different message really duplicated this document"
(`duplicate_skip`, counted).

**Attachment selection is this worker's OWN scope decision** (`dl_extract.py`'s own
docstring explicitly leaves worker/claim wiring to this phase): only attachments whose
`mime`/filename look like a PDF or an image are read — a real delivery note is always one
of those two; anything else (a signature image, a `.docx`, ...) is skipped rather than fed
to Vision, which n8n's own `Get Attachment Meta ... LIMIT 1` query implicitly also
preferred (R16) before F2 widened it to every PDF-shaped attachment (W1a).

**A mail-body-sourced (#258) document whose OWN mail reads as a CORRECTION/AMENDMENT
never auto-ships (#265).** HK LOAN (`gnip@hkloan.eu`) writes delivery notes directly
into the mail body and routinely follows up with a short mail that only restates ONE
changed line of an earlier, separately-sent full delivery ("Zvyšok dodania bez zmien" —
"rest unchanged"). This worker has zero cross-message memory — extracting such a
follow-up ALONE would silently produce a document missing every item the follow-up
never repeats. Per the owner's binding decision on #265 (2026-08-13, "možnosť 1"):
`_looks_like_correction` detects this from the subject/body (gated `not shadow` — a
shadow run still extracts a correction mail for comparison, per this module's own
"shadow runs the FULL pipeline" contract; it never claims/uploads/teaches regardless,
via `_process_document`'s own shadow branch) BEFORE `dl_extract.extract_email` is ever
called for a LIVE run, and routes straight to `review` — no model call, no
supplier/item matching, no `dl_item`/`dl_supplier` question, no claim, no upload, ever.
The review text explicitly tells the warehouse that if the original document was
already imported into CODEX, the fix has to happen there BY HAND — this engine cannot
amend an already-imported document and must never try (there is currently no
system-side way to do it). See the #265 design comment (`gh issue comment`) for the
full rejected-alternative reasoning behind the exact trigger-word set.

**`release_for_question` also releases orphaned same-sender siblings for a `dl_
supplier` answer (#265 gap 2).** `teach.ask_dl_supplier`'s per-sender dedupe means only
the FIRST stuck message from a still-unregistered sender ever gets its own `order_
questions` row — every later message from that sender is left `processed=true`/`proc_
status='review'` forever, tied to nothing. Once the sender is finally taught, `_release_
stuck_siblings` resets every OTHER orphaned same-sender message back into the normal
`_claim()` pool (bounded, never a synchronous storm) instead of leaving them stuck.
Deliberately scoped to `dl_supplier` only — see that function's own docstring for why.
"""
from __future__ import annotations

import logging

from . import (
    dl_alerts,
    dl_extract,  # noqa: F401 (re-export for dl_worker.dl_extract monkeypatch)
    dl_snapshot,
    llm,
    report,
    worker,
)
from .dl_correction import (  # noqa: F401 (re-export: public dl_worker API)
    _COMBINED_TEXT_ATTACHMENTS_MARKER,
    _CORRECTION_DOPLN_SUBJECT_RE,
    _CORRECTION_EXCERPT_LIMIT,
    _CORRECTION_STRONG_RE,
    _correction_review_reason,
    _looks_like_correction,
    _mail_body_only,
)
from .dl_document import (  # noqa: F401 (re-export: public dl_worker API)
    _document_has_catalog_match,
    _num,
    _process_document,
    _shipped_items,
    _skip_not_warehouse,
)
from .dl_events import (  # noqa: F401 (re-export: public dl_worker API)
    _event,
    _flag_attachment,
    _post,
)
from .dl_matching import (  # noqa: F401 (re-export: public dl_worker API)
    ITEM_SCHEMA,
    PROMPTS_DIR,
    SUPPLIER_SCHEMA,
    _item_input,
    _item_prompt,
    _match_item,
    _match_supplier,
    _supplier_input,
    _supplier_prompt,
)
from .dl_message import (  # noqa: F401 (re-export: public dl_worker API)
    _ATTACHMENT_EXT_RE,
    _ATTACHMENT_MIME_RE,
    _ATTACHMENT_SPREADSHEET_EXT_RE,
    _ATTACHMENT_SPREADSHEET_MIME_RE,
    _BODY_TEXT_IDX,
    _SUBJECT_DOC_RE,
    CATEGORY,
    CLAIM_STALE_MINUTES,
    MAX_ATTEMPTS,
    SHADOW_DAYS,
    _aggregate_status,
    _as_message,
    _claim,
    _peek_for_shadow,
    _process_message,
    _read_attachments,
    _run_and_finish,
    _subject_doc_numbers,
    _summary_outcome,
    refresh_due,
)
from .dl_questions import (  # noqa: F401 (re-export: public dl_worker API)
    _STUCK_SIBLING_LIMIT,
    _release_stuck_siblings,
    close_message_not_warehouse,
    close_message_sklad_unknown,
    release_for_question,
    release_for_supplier_card,
)
from .dl_retry import (  # noqa: F401 (re-export: public dl_worker API)
    TRANSIENT_RE,
    TRANSIENT_RETRY_LIMIT,
    _check_landed,
    _check_retry,
    _is_transient,
    _RetryLater,
)

log = logging.getLogger("orders.dl_worker")

resolve_engine = worker.resolve_engine


# --- #239 class 3: classified as DL but never even attempted ----------------

# Deliberately generous — `worker.run_forever`'s tick claims a message within ~15s
# under normal operation, so this many minutes with ZERO order_runs rows is already a
# strong anomaly (delivery_notes_engine misconfigured, or the worker thread died before
# its first claim), not routine backlog. Matches CLAIM_STALE_MINUTES for consistency.
STUCK_CLASSIFIED_MINUTES = 30


def stuck_classified_sweep(conn, cfg, threshold_minutes: int = STUCK_CLASSIFIED_MINUTES) -> int:
    """A message classified `dodacie_listy` that never got a first processing attempt
    AT ALL (zero `order_runs` rows) within a generous threshold — the ONE class none of
    the existing safety nets cover: the hourly n8n "Stuck message watchdog"
    (`EPe5WWMVZR0lzUld`, active, alerts channel 243 for this category) only fires once
    `attempts>=3`, which a message that was NEVER claimed (engine misconfigured, the
    worker thread died before its first claim) never reaches — `attempts` stays 0
    forever.

    Deduped via `dl_alerts.already_pending` keyed on `message_id` — deliberately NOT
    `messages.alerted_stuck` (see the design comment on #239 for why: that flag belongs
    to the n8n watchdog's own dedup, and setting it here would silently suppress that
    watchdog's own future alert if the message later starts retrying and crosses
    `attempts>=3`). Returns how many NEW messages were enqueued this pass (0 on a clean
    sweep, or when every candidate was already alerted).

    #310: this is an OPERATOR/engine-liveness alert ("processing never started — check
    the DL engine"), NOT something the warehouse can act on — it routes to
    `report.ops_channel(cfg)`, never `delivery_notes_channel_id` (243). When the ops
    channel is unset (0) the alert is STILL detected, enqueued durably (channel_id=0 =>
    `flush_pending` treats it as "Odoo not configured", the row stays counted in
    `pending_count`/on the dashboard, retry is a no-op) and logged at WARNING below —
    never silently dropped, never on a warehouse channel."""
    channel = report.ops_channel(cfg)
    rows = conn.execute(
        """SELECT m.message_id, m.subject, m.from_addr, m.created_at
             FROM messages m
            WHERE m.category = %s AND m.processed = false
              AND m.created_at < now() - make_interval(mins => %s)
              AND NOT EXISTS (SELECT 1 FROM order_runs r
                              WHERE r.message_id = m.message_id)
            ORDER BY m.created_at ASC LIMIT 20""",
        (CATEGORY, max(1, int(threshold_minutes)))).fetchall()
    n = 0
    for message_id, subject, from_addr, created_at in rows:
        # #336: throttle the RE-reminder to once per morning (skip weekends) — the first
        # alert still fires promptly. Replaces the old flat ~4h `already_pending` re-ask.
        if dl_alerts.reminder_suppressed(conn, cfg, "dl_stuck_classified", message_id):
            continue
        # #336: ONE short line (`• odosielateľ — predmet (prijaté D.M.)`); the explanation
        # sentence + the dashboard action link are added ONCE in the per-kind header
        # `dl_alerts.flush_pending` builds for the whole group (`GROUPED_ITEM_KINDS`), never
        # repeated per message (the pre-#336 wall). The old microsecond `zistené`/`prijaté`
        # timestamps are dropped — the once-daily cadence makes them moot.
        dl_alerts.enqueue(conn, channel, "dl_stuck_classified",
                          dl_alerts.item_line(from_addr, subject, created_at),
                          message_id=message_id)
        n += 1
    if n:
        log.warning("dl stuck-classified: %d message(s) never got a first attempt", n)
    return n


# --- one tick ----------------------------------------------------------------

def tick(conn, cfg, client=None, upload=None, post=None, list_dirs=None) -> int:
    """Process at most one `dodacie_listy` message. Returns 0 or 1 (whether a MESSAGE
    reached a terminal outcome this tick — a transient retry, per R17, returns 0: nothing
    was actually completed).

    `client`/`upload`/`post`/`list_dirs` are injected so the worker's claim/shadow/
    retry/upload guarantees can be tested without the LLM stack, a real ORION host, or
    Odoo — same convention `static_worker.tick` already uses. Production passes the
    real `llm.from_config(cfg)` and leaves `upload`/`post`/`list_dirs` at their
    defaults (`upload_mod.put`/`dl_report.post`/`upload_mod.list_dirs`). `list_dirs`
    (finding 6, #239) is the stable-identity presence check a transient upload failure
    consults before deciding whether a single safe retry is possible — see
    `_check_landed()`.
    """
    engine = resolve_engine(getattr(cfg, "delivery_notes_engine", "n8n"))
    shadow = bool(getattr(cfg, "delivery_notes_shadow", False))
    if engine != "python" and not shadow:
        return 0

    snapshot_id = dl_snapshot.latest_snapshot_id(conn)
    if not snapshot_id:
        log.warning("no DL catalog snapshot yet — DL worker idle")
        return 0
    catalog = dl_snapshot.load_catalog(conn, snapshot_id)
    suppliers = dl_snapshot.load_suppliers(conn, snapshot_id)
    if client is None:
        client = llm.from_config(cfg)

    if engine == "python":
        message = _claim(conn)
        if not message:
            return 0
        result = _run_and_finish(conn, cfg, client, message, snapshot_id, catalog,
                                 suppliers, upload=upload, post=post,
                                 list_dirs=list_dirs)
        return 1 if result is not None else 0

    message = _peek_for_shadow(conn, getattr(cfg, "delivery_notes_shadow_days",
                                             SHADOW_DAYS))
    if not message:
        return 0
    run_id = worker._start_run(conn, message["message_id"], None, shadow=True)
    try:
        result = _process_message(conn, cfg, client, message, snapshot_id, catalog,
                                  suppliers, shadow=True, upload=upload, post=post)
    except _RetryLater as e:
        # Deep-review finding, #204: a shadow peek is not a claim — there is nothing
        # to "retry", but a routine transient LLM hiccup is not an ERROR either; log
        # it at info (not a full traceback) so it doesn't read as a genuine failure.
        log.info("DL shadow pipeline hit a transient failure for %s (no observable "
                 "effect — shadow claims/marks/writes nothing): %s",
                 message["message_id"], e)
        worker._finish_run(conn, run_id, "error",
                           {"kind": "dl", "dl_snapshot_id": snapshot_id, "reason": str(e)},
                           error=str(e))
        return 0
    except Exception as e:
        log.exception("DL shadow pipeline failed for %s", message["message_id"])
        worker._finish_run(conn, run_id, "error",
                           {"kind": "dl", "dl_snapshot_id": snapshot_id}, error=repr(e))
        return 0
    worker._finish_run(conn, run_id, result.get("status", "ok"), result)
    return 1
