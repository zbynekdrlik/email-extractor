"""DL worker — release on answered question + terminal close + sibling release."""
from __future__ import annotations

import logging

import psycopg
from psycopg.types.json import Json

from . import dl_match, dl_nonwarehouse, dl_report, dl_snapshot, llm, report
from .dl_message import CATEGORY, _as_message, _run_and_finish

log = logging.getLogger("orders.dl_worker")

# --- #240: an answered dl_item/dl_supplier question gives its document another chance --

def release_for_question(conn, cfg, qid: int, client=None, upload=None,
                         post=None, list_dirs=None) -> list[dict]:
    """Once every `dl_item`/`dl_supplier` nástenka question raised for ONE message has
    been answered, that message is given a genuine second chance to finish — the exact
    thing that was missing before this ticket (`teach._apply_dl_item`/`_apply_dl_supplier`
    used to just write memory and stop, so an answered question never unstuck the
    document that raised it, per the design comment on #240).

    Waits for EVERY sibling `dl_item`/`dl_supplier` question of the SAME message before
    reprocessing (mirrors `hold.release_for_question`'s own "every question_id answered"
    gate) — reprocessing after only SOME of them are answered would just re-raise the
    exact same still-open questions for nothing. The caller (`teach._apply_dl_item`/
    `_apply_dl_supplier`) already marks `qid` itself 'answered' BEFORE calling this, so
    it never counts itself as still-open.

    Re-runs the WHOLE message through the ordinary, already-tested `_process_message`
    pipeline (`_run_and_finish`) rather than a bespoke "redecide without a model call"
    path (the design AI-orders' `hold.py` uses): unlike AI-orders' hold, a DL document
    blocked on `dl_supplier` never even reached item matching the first time (the item
    loop only runs after a successful supplier match) — there is no earlier decision to
    redecide, the model genuinely has to be asked. Safety against re-uploading an
    ALREADY-shipped document (e.g. one that partially shipped, then later got its
    excluded item's `dl_item` question answered) is NOT new code here — it is the SAME
    `desadv.claim_send_or_identify` claim every `_process_document` call already makes
    before any upload; reprocessing this message again, the ledger reports the confirmed
    claim exactly like a genuine R17 self-retry and refuses the upload (see
    `_process_document`'s own `already_shipped_this_run` handling) — see
    `test_release_for_question_never_reuploads_an_already_partially_shipped_document`.

    Returns the reprocessed message's own `documents` list (mirrors AI-orders'
    `hold.release_for_question`'s `released` list) — `[]` when nothing happened yet (a
    sibling question is still open), or when there is genuinely nothing to reprocess
    (the message row is gone, or no catalog snapshot is loaded).

    Deep-review finding on this ticket's own PR (#240): the "every sibling answered?"
    check below has no lock of its own — two near-simultaneous answers to SIBLING
    questions of the SAME message could each independently observe "every question
    answered" under READ COMMITTED and both dispatch a reprocess (the exact race
    `hold._release_locked`'s own docstring already documents and fixes for AI-orders,
    #118). Bounded to WASTED work, never corruption (`desadv.claim_send_or_identify` is
    still the real, atomic backstop against a double-upload) — but a doubled LLM call, a
    duplicate `order_runs` row and duplicate digest noise per collision is real, avoidable
    waste. `pg_advisory_xact_lock(hashtext(message_id))`, taken on a SEPARATE connection
    and held for the WHOLE check-through-reprocess decision (mirrors the `db.py`
    migration-lock idiom this codebase already uses, and `hold._release_locked`'s own
    "separate connection, held for the whole decision" shape — DL has no natural row to
    `FOR UPDATE` the way `held_orders` gives AI-orders, so a plain advisory lock keyed on
    the message id is the closest honest equivalent).

    Correction (review of THIS lock's own first draft, same PR): the SECOND of two racing
    answers does NOT "no-op" once it acquires the lock — the sibling questions it re-reads
    are the SAME `status='answered'` rows the first racer already saw (answering a
    question doesn't reopen it), so the second racer still runs its own full
    `_run_and_finish` pass (a second LLM call, a second `order_runs` row) — the lock only
    SERIALIZES the two attempts instead of letting them run truly concurrently; it does
    not skip the second one. What actually stops the waste from becoming a real
    duplicate DELIVERY is the same `desadv.claim_send_or_identify` claim two paragraphs
    up — the second pass's own upload attempt sees the first pass's already-confirmed
    claim and refuses to re-ship. The lock's whole value is narrower than "no-op": it
    guarantees the two attempts never touch `desadv.claim_send_or_identify` at the same
    instant (no TOCTOU on the claim check itself), and it keeps the SQL cheap
    (no genuinely parallel Postgres work on the same message) — it does not, by itself,
    prevent the wasted second LLM call and `order_runs` row this docstring already
    called out above as the acceptable cost.

    #265 gap 2: when the just-answered question is `dl_supplier`, this ALSO releases
    every OTHER same-sender `dodacie_listy` message stuck in `review` with no `order_
    questions` row of its own — see `_release_stuck_siblings`'s own docstring for the
    full evidence and why this is scoped to `dl_supplier` only.

    Deep-review finding on this ticket's own PR (#265): the sibling widening keys on
    the TIED message's own `messages.from_addr` (the raw envelope address, read below
    via `message = _as_message(msg_row)`), NOT `order_questions.payload['sender_
    email']` — `_process_document` sets that payload field to `doc.get('supplierEmail')
    or from_addr`, the DOCUMENT-extracted address, which can genuinely differ from the
    envelope address (a 3PL/warehouse operator's own contact email inside the mail
    body, for example). `_release_stuck_siblings`'s own query matches candidate
    siblings by `messages.from_addr` — using anything else as the search key here would
    silently no-op (or worse, match a DIFFERENT sender's messages) whenever the two
    addresses diverge."""
    qrow = conn.execute(
        "SELECT message_id, kind FROM order_questions WHERE id = %s", (qid,)).fetchone()
    if not qrow:
        return []
    message_id, kind = qrow[0], qrow[1]
    with psycopg.connect(cfg.pg_dsn) as lock_tx:
        lock_tx.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (message_id,))
        still_open = conn.execute(
            """SELECT 1 FROM order_questions
                WHERE message_id = %s AND kind IN ('dl_item', 'dl_supplier')
                  AND status = 'open' LIMIT 1""", (message_id,)).fetchone()
        if still_open:
            return []
        msg_row = conn.execute(
            """SELECT message_id, subject, from_addr, from_name, combined_text,
                      body_text, has_attachments FROM messages
                WHERE message_id = %s""", (message_id,)).fetchone()
        if not msg_row:
            return []
        message = _as_message(msg_row)
        assert message is not None  # msg_row proven present above ⟹ _as_message returns a dict
        snapshot_id = dl_snapshot.latest_snapshot_id(conn)
        if not snapshot_id:
            log.warning("release_for_question(%s): no DL catalog snapshot yet — cannot "
                        "reprocess %s", qid, message_id)
            return []
        catalog = dl_snapshot.load_catalog(conn, snapshot_id)
        suppliers = dl_snapshot.load_suppliers(conn, snapshot_id)
        if client is None:
            client = llm.from_config(cfg)
        result = _run_and_finish(conn, cfg, client, message, snapshot_id, catalog,
                                 suppliers, upload=upload, post=post,
                                 list_dirs=list_dirs)
        # #265 gap 2 (dl_supplier) + #365 (dl_item): a same-sender sibling message whose
        # own `dl_item`/`dl_supplier` question DEDUPED onto THIS message's open question
        # (`ask_generic`'s `ON CONFLICT ... DO NOTHING`) has NO `order_questions` row of its
        # own, so `release_for_question` (which reprocesses only `qid`'s OWN message) never
        # unsticks it. Before #365 such a sibling still PARTIAL-shipped; #365 now HOLDS it,
        # so without this it would strand FOREVER (no question tied to it, no re-claim). The
        # existing `_release_stuck_siblings` (keyed on the raw envelope `from_addr`, with ALL
        # the #265 safety exclusions) resets those orphans back into the claim pool — safe
        # for BOTH kinds because releasing only RE-QUEUES: the reprocess re-extracts and
        # re-runs the deterministic rung on the REAL document (now with the just-taught
        # memory), so a false-positive from_addr match never ships a wrong EDI, and the
        # `desadv.claim_send_or_identify` claim still refuses any already-shipped re-upload.
        if kind in ("dl_supplier", "dl_item"):
            _release_stuck_siblings(conn, message_id, message.get("from_addr", ""))
        return (result or {}).get("documents", [])


def close_message_not_warehouse(conn, qid: int) -> dict:
    """#307: the skladníčka marks a DL question "netýka sa skladu / nie je to dodací list
    pre sklad" — the whole mail is NOT a warehouse delivery note (a režíjna faktúra, a
    promo, ...). Terminal and MESSAGE-level:

    (1) close EVERY still-open `dl_item`/`dl_supplier` question of the SAME message with
        the terminal status 'not_warehouse' (they all belong to one misrouted mail — the
        reported KLEŠČ case is a whole režíjna faktúra, not one stray line);
    (2) mark the message HANDLED WITHOUT EDI — `processed=true`, plus a rollup skip
        `email_events` entry (deliberately NOT the OK/EDI logger — the skip-branch rule in
        `.claude/rules/n8n-workflow-edits.md`), so the dashboard/daily digest still SHOWS
        it ("netýka sa skladu — vybavené bez EDI") for Marek, nothing is silently lost;
    (3) upload NOTHING to ORION.

    #314 (supersedes #307's "sender deliberately NOT remembered"): the SUPPLIER is now
    remembered (`dl_nonwarehouse.record_for_message`), keyed on the supplier's OWN identity
    from the document (registry EAN ∪ normalized name), NOT blindly the email address — so
    the tlaciaren@-forwards-everything risk #307 flagged is avoided, and a GTIN-match safety
    override in `_process_document` (req 4) means a later mail that DOES carry a catalog item
    is never silently dropped. A later NON-warehouse mail from the same supplier is now
    short-circuited (terminal skip, zero questions) instead of re-asking forever.

    Marking the message `processed` is what keeps the question from coming back: the dedup
    index (`WHERE status='open'`) does not suppress a fresh question once this one is
    terminal, so leaving the message unprocessed would let the stale-reclaim / reprocess
    path re-raise the identical question. A `processed` message is never re-claimed
    (`_claim`'s own `WHERE processed = false`)."""
    qrow = conn.execute(
        "SELECT message_id FROM order_questions WHERE id = %s", (qid,)).fetchone()
    if not qrow:
        return {"closed": 0, "message_id": None}
    message_id = qrow[0]
    # #314 req 1: remember this supplier as non-warehouse BEFORE the close UPDATE (the
    # payloads it reads are the same either way, but recording first keeps the intent clear)
    # so a LATER mail from the same supplier is short-circuited in _process_document.
    dl_nonwarehouse.record_for_message(conn, message_id)
    closed = conn.execute(
        """UPDATE order_questions
              SET status = 'not_warehouse', answer = '{"not_warehouse": true}'::jsonb,
                  answered_by = %s, answered_at = now()
            WHERE message_id = %s AND kind IN ('dl_item', 'dl_supplier')
              AND status = 'open'
            RETURNING id""", ("sklad", message_id)).fetchall()
    conn.execute(
        """UPDATE messages
              SET processed = true, processed_at = now(), processed_by = %s,
                  processing_at = NULL
            WHERE message_id = %s""", (CATEGORY, message_id))
    report.log_event(conn, message_id, stage="not_warehouse", status="not_warehouse",
                     outcome="netýka sa skladu — vybavené bez EDI (sklad)",
                     detail={"closed_questions": len(closed)},
                     rollup=True, workflow=dl_report.WORKFLOW)
    log.info("DL message %s marked not_warehouse (sklad); %s question(s) closed, no EDI",
             message_id, len(closed))
    return {"closed": len(closed), "message_id": message_id}


def close_message_sklad_unknown(conn, qid: int) -> dict:
    """#305 (variant A): the skladníčka clicked „Neviem" on a DL question — she cannot
    identify which card/supplier a line belongs to. Marek's decision (14.8.): ODLOŽ the
    whole delivery note. The sibling of `close_message_not_warehouse` (#307), terminal and
    MESSAGE-level:

    (1) close EVERY still-open `dl_item`/`dl_supplier` question of the SAME message with
        the terminal status 'sklad_unknown' — the whole DL is deferred, not just the one
        line she clicked (she cannot complete the note without every card resolved);
    (2) mark the message HANDLED WITHOUT EDI — `processed=true`, plus a rollup skip
        `email_events` entry with `status='review'` (the REVIEW flavour, so Marek sees it
        as "needs attention" in the daily digest — vs #307's terminal 'done' flavour), and
        deliberately NOT the OK/EDI logger (the skip-branch rule in
        `.claude/rules/n8n-workflow-edits.md`);
    (3) upload NOTHING to ORION.

    Marking the message `processed` is load-bearing, not cosmetic: the ask-dedup index is
    only `WHERE status='open'`, so leaving the message unprocessed would let the
    stale-reclaim / reprocess path re-raise the identical question (`_claim`'s own
    `WHERE processed = false`). The `email_events` row is what the daily digest counts as
    "odložené (sklad nevie)" (`reliability.dl_provenance_stats_for_day`), so a deferred DL
    is never silently lost — Marek resolves it by hand.

    A message deferred this way keeps its `sklad_unknown` question rows, so
    `_release_stuck_siblings` (which requires ZERO `order_questions` rows) never re-picks
    it. Everything runs in one `deps.db_tx()` transaction (safe — no external side effect).
    """
    qrow = conn.execute(
        "SELECT message_id FROM order_questions WHERE id = %s", (qid,)).fetchone()
    if not qrow:
        return {"closed": 0, "message_id": None}
    message_id = qrow[0]
    closed = conn.execute(
        """UPDATE order_questions
              SET status = 'sklad_unknown', answer = '{"sklad_unknown": true}'::jsonb,
                  answered_by = %s, answered_at = now()
            WHERE message_id = %s AND kind IN ('dl_item', 'dl_supplier')
              AND status = 'open'
            RETURNING id""", ("sklad", message_id)).fetchall()
    conn.execute(
        """UPDATE messages
              SET processed = true, processed_at = now(), processed_by = %s,
                  processing_at = NULL
            WHERE message_id = %s""", (CATEGORY, message_id))
    report.log_event(conn, message_id, stage="sklad_unknown", status="review",
                     outcome="Sklad nevie identifikovať dodací list — odložený na ručné "
                             "doriešenie (nájdeš ho v dennom súhrne).",
                     detail={"closed_questions": len(closed)},
                     rollup=True, workflow=dl_report.WORKFLOW)
    log.info("DL message %s deferred (sklad Neviem); %s question(s) closed, no EDI",
             message_id, len(closed))
    return {"closed": len(closed), "message_id": message_id}


# #265 gap 2: `ask_dl_supplier`'s per-sender dedupe (`ON CONFLICT ... DO NOTHING`, the
# SAME `order_questions(customer_ean, item_key) WHERE status='open'` index every other
# kind shares) means only the FIRST `dodacie_listy` message from a still-unregistered
# sender ever gets a row of its OWN — every LATER message from that sender is left
# `processed=true`/`proc_status='review'` forever, with NO `order_questions` row
# referencing it at all (live evidence on #265: HK LOAN had 5 such messages against
# exactly 1 tied question). Answering the one open question only ever gave that FIRST
# message a second chance (`release_for_question` above) — the other 4 stayed stuck.
#
# This closes the gap WITHOUT reprocessing every sibling synchronously inside the
# request that just answered the nástenka question (unbounded cost for a sender with
# many stuck messages, and this whole function runs under the advisory lock the caller
# already holds) — it just resets them back into the SAME pool `_claim()` already
# rate-limits to one message per ~15s tick; the actual reprocessing reuses that
# existing, already-tested machinery unchanged. `_STUCK_SIBLING_LIMIT` bounds how many
# rows get flipped per call — a sender with more than that many still gets the rest on
# the NEXT answered question for it (or the existing `stuck_classified_sweep`/hourly
# n8n watchdog eventually surfaces anything that never gets picked up).
#
# Deliberately scoped to `dl_supplier` only (see the #265 design comment for the full
# rejected-alternative reasoning) — the only identity that can correlate an
# UNPROCESSED sibling message with the just-answered question, without extracting it
# first, is the raw envelope `from_addr`, and that matches `ask_dl_supplier`'s own
# dedupe key exactly (keyed on the sender ADDRESS). A `dl_item` question's dedupe key is
# (supplier_ean, wording) instead — by the time one exists the supplier is already
# matched, but there is no similarly reliable way to find OTHER not-yet-extracted
# messages sharing that exact wording without extracting them first. No live evidence
# of the `dl_item` version of this gap has been observed; it needs its own design if it
# ever is, not a copy of this one.
#
# #365 UPDATE (the gap became live-lossy): before #365 a `dl_item`-deduped sibling still
# PARTIAL-shipped, so the strand was invisible; #365 HOLDS it instead, turning the strand
# into a permanent no-re-entry hold. `release_for_question` now fires this widening for a
# `dl_item` answer too. The #265 "no reliable correlation" worry does NOT bite here: a
# HELD sibling is already PROCESSED (`processed=true`/`proc_status='review'`, no question
# row of its own — the exact orphan shape this query targets), so `from_addr` is not
# correlating an unextracted message, only selecting which already-held same-sender orphans
# to re-queue. Over-broad matching (a same-sender orphan of a DIFFERENT unknown wording) is
# harmless: release only RE-QUEUES, and the reprocess re-holds it (raising its OWN fresh
# question, since the deduping question is now closed) or ships it if newly taught. The
# `desadv.claim_send_or_identify` claim stays the backstop against any already-shipped
# re-upload.
#
# Deep-review finding on this ticket's own PR (#265) — a REAL, proven safety bug in
# the first cut: `processed=true AND proc_status='review' AND no order_questions row`
# ALSO matches a message whose upload to ORION genuinely FAILED (a timeout, a network
# error) — `_process_document`'s upload-except branch calls `desadv.release_send()`
# (deleting the claim) and never raises a `dl_item`/`dl_supplier` question, so that
# message is indistinguishable from a genuine unmatched-supplier orphan by THAT
# predicate alone. Resetting it here would re-enable exactly the automatic upload
# retry #239 deliberately REMOVED (a released claim + a fresh per-attempt filename
# means a second attempt can upload a genuine SECOND copy of an already-landed
# document — see `.claude/rules/n8n-workflow-edits.md`'s "#239" section). Every
# upload-failure AND every other genuine processing exception in this module logs its
# own review event with `status="error"` (`_event(..., status="error", ...)` at the
# supplier-match-exception, upload-exception, and attachment-extraction-error call
# sites) — `status="review"` is reserved for the plain "nothing matched, nothing
# failed" outcomes (an unmatched supplier, an unmatched item, a correction mail). The
# `NOT EXISTS ... status = 'error'` clause below is what makes that distinction real:
# it excludes ANY message with so much as one logged failure from ever being reset by
# this widening, at the cost of leaving a message that BOTH failed AND is a genuine
# orphan stuck (safe default — `stuck_classified_sweep`/the hourly n8n watchdog still
# surface it).
_STUCK_SIBLING_LIMIT = 20
# #323 residual 2: how many orphan-review candidate rows the name-rung SCANS per call to
# find up to `_STUCK_SIBLING_LIMIT` name matches — normalization (`supplier_name_key`) is
# done in Python, so the SQL can only pre-filter loosely. Deliberately generous: the #265
# orphan-review set (`processed=true`/`proc_status='review'`/no question/no error) is a
# handful per supplier in practice, so this bound is never reached; a genuinely larger
# backlog gets its stragglers on the NEXT card-add/answer, exactly like the by-addr rung.
_STUCK_SIBLING_CANDIDATE_LIMIT = 500


def _release_stuck_siblings(conn, exclude_message_id: str, sender_email: str) -> int:
    """Resets up to `_STUCK_SIBLING_LIMIT` orphaned same-sender `dodacie_listy`
    messages (`processed=true`, `proc_status='review'`, no `order_questions` row of
    their own, and no `status='error'` event ever logged for it — see the section
    comment above for why that last exclusion is load-bearing, not decorative) back
    into the normal claim pool. Returns how many were reset — `0` when `sender_email`
    is blank or nothing matched, never raises.

    Known, accepted residual (deep-review finding on this ticket's own PR, #265): a
    released sibling that reprocesses back to a permanent, question-less `review` (a
    #265 correction mail, or a mail with no usable source) stays eligible for a
    SECOND widening if the SAME sender's `dl_supplier` question is ever reopened and
    re-answered — bounded in practice: once taught, `dl_supplier_memory`'s
    memory-rescue rung means no NEW `dl_supplier` question is ever raised for that
    sender again unless its taught card is later retired from the catalog (rare). A
    `released_once` tracking column would close this fully but needs a schema
    migration — deferred as a documented, low-frequency, non-safety limitation rather
    than blocking this fix on it."""
    sender_email = (sender_email or "").strip()
    if not sender_email:
        return 0
    rows = conn.execute(
        """SELECT message_id FROM messages
            WHERE category = %s AND processed = true AND proc_status = 'review'
              AND message_id <> %s AND lower(from_addr) = lower(%s)
              AND NOT EXISTS (SELECT 1 FROM order_questions oq
                              WHERE oq.message_id = messages.message_id)
              AND NOT EXISTS (SELECT 1 FROM email_events e
                              WHERE e.message_id = messages.message_id
                                AND e.status = 'error')
            ORDER BY created_at ASC LIMIT %s""",
        (CATEGORY, exclude_message_id, sender_email, _STUCK_SIBLING_LIMIT)).fetchall()
    if not rows:
        return 0
    ids = [r[0] for r in rows]
    conn.execute(
        """UPDATE messages SET processed = false, processing_at = NULL, attempts = 0
            WHERE message_id = ANY(%s)""", (ids,))
    log.info("dl sibling release: reset %d orphaned same-sender sibling message(s) for "
             "%r back into the claim pool", len(ids), sender_email)
    return len(ids)


def release_for_supplier_card(conn, cfg, ean_edi, name, emails) -> int:
    """#322 + #323: retro-release triggered when a CODEX supplier card is ADDED or EDITED
    via `/znalosti` (not by answering a nástenka question). Runs THREE rungs, all REUSING
    already-tested, safe machinery — never a parallel release path (the #322 constraint),
    so ALL the #265 safety exclusions hold unchanged everywhere: only `processed=true`/
    `proc_status='review'` messages with NO `order_questions` row of their own AND no
    `status='error'` event ever logged are reset (the `#239` double-upload exclusion), and a
    correction mail simply re-hits `_looks_like_correction` on reprocess and returns to
    review — never ships.

    (1) #323 residual 1 — auto-close any OPEN `dl_supplier` nástenka question this card
        UNAMBIGUOUSLY resolves, exactly as Marek's #236 directive requires ("stačí pridať v
        CODEXe, nemusia na nástenke"). The match uses the SAME deterministic rung the live
        pipeline uses (`dl_match.resolve_supplier_from_cards`, ambiguity measured across
        DISTINCT ean over the full current card list); only when it resolves to THIS card's
        ean is the question auto-answered — through the SAME app path a HUMAN answer takes
        (`answered_by='codex-card-auto'`, then `teach.KINDS['dl_supplier'].apply` →
        `dl_supplier_memory.remember` + `release_for_question` → reprocess the tied message +
        `_release_stuck_siblings` by its from_addr). Ambiguity → the question stays open.
    (2) #322 email rung — reset orphaned same-sender stuck messages by `from_addr` for each
        of the card's email addresses (`_release_stuck_siblings`, `exclude_message_id=""`
        excludes nothing — there is no "current" message to skip here).
    (3) #323 residual 2 — the `emails=[]` case: reset orphaned stuck messages whose
        normalized envelope `from_name` equals this card's UNAMBIGUOUS normalized name
        (only when no other card shares that normalized name), same #265 exclusions.

    Returns the total number of messages the retro-release affected (auto-closed questions
    plus reset sibling messages). Passing an `emails=[]` card is fully supported: rungs (1)
    and (3) still fire, so the email having been the ONLY key before #323 no longer strands
    an emailless supplier's stuck messages."""
    released = 0
    card_ean = str(ean_edi or "").strip()
    # (1) residual 1 — open dl_supplier questions this card unambiguously resolves.
    released += _auto_close_matching_supplier_questions(conn, cfg, card_ean)
    # (2) #322 email rung — orphaned same-sender siblings by from_addr.
    seen: set[str] = set()
    for email in emails or []:
        key = str(email or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        released += _release_stuck_siblings(conn, "", key)
    # (3) residual 2 — emails=[] name rung (unambiguous normalized name).
    released += _release_stuck_siblings_by_name(conn, card_ean, str(name or ""))
    return released


def _auto_close_matching_supplier_questions(conn, cfg, card_ean: str) -> int:
    """#323 residual 1: auto-answer every OPEN `dl_supplier` question that THIS just-added
    card unambiguously resolves via the SAME deterministic rung the live pipeline uses
    (`dl_match.resolve_supplier_from_cards`), then let the ordinary answer path
    (`release_for_question`) reprocess the tied message and release its siblings. Returns
    how many questions were auto-closed.

    The rung is passed the FULL current effective card list (so ambiguity is measured across
    every card, exactly as #322 does) and the question's own EXTRACTED identity from its
    payload (`supplier_name`/`supplier_city`/`sender_email`, stored by `ask_dl_supplier`).
    Only a match resolving to THIS card's ean triggers an auto-answer — any ambiguity (the
    rung returns `None`, or resolves to a DIFFERENT card) leaves the question OPEN, so the
    system never answers on the warehouse's behalf on a guess."""
    if not card_ean:
        return 0
    from . import teach
    cards = dl_snapshot.dl_suppliers_for_management(conn)
    closed = 0
    for q in teach.open_questions(conn, limit=_STUCK_SIBLING_CANDIDATE_LIMIT,
                                  kinds=("dl_supplier",)):
        payload = q.get("payload") or {}
        sender_email = str(payload.get("sender_email") or "")
        doc = {"supplierName": str(payload.get("supplier_name") or ""),
               "supplierCity": str(payload.get("supplier_city") or "")}
        dec = dl_match.resolve_supplier_from_cards(doc, cards, sender_email)
        if not (dec and dec.matched and str(dec.ean_edi) == card_ean):
            continue
        # Per-question isolation: this runs INSIDE the /znalosti card-save HTTP request and
        # `_auto_answer_supplier_question`'s `release_for_question` reprocess can make a real
        # LLM/ORION call — a transient failure on ONE question must not abort the whole
        # retro-release (the remaining questions AND the email/name rungs below), nor 500 a
        # card that was already saved. Everything is autocommit + idempotent (the guarded
        # `status='open'` UPDATE makes a later retry a no-op on already-answered ones), so
        # log-and-continue is safe; the worker's own claim/tick stays the backstop.
        try:
            if _auto_answer_supplier_question(conn, cfg, q, card_ean, dec.name):
                closed += 1
        except Exception:
            log.exception("dl codex-card auto-answer: failed on dl_supplier question %s "
                          "(card ean %s) — left for the nástenka/worker path",
                          q.get("id"), card_ean)
    return closed


def _auto_answer_supplier_question(conn, cfg, q: dict, card_ean: str, card_name: str) -> bool:
    """Answer ONE open `dl_supplier` question EXACTLY as the human /otazky flow does
    (`_api_orders_answer_generic`'s tail): legitimize the card as a candidate
    (`teach.add_candidate`, so `_apply_dl_supplier` records the supplier NAME too), mark the
    row answered — guarded on `status='open'` so a concurrent human answer can never be
    double-applied — with the distinct audit marker `answered_by='codex-card-auto'`, then
    fire the registered apply (`teach.KINDS['dl_supplier'].apply` → `dl_supplier_memory.
    remember` + `release_for_question`). Returns True when the row was genuinely
    transitioned. Lazy `teach` import mirrors `_apply_dl_supplier`'s own lazy `dl_worker`
    import — the two modules form a cycle only at their tops."""
    from . import teach
    cand = {"value": card_ean, "label": card_name or card_ean}
    teach.add_candidate(conn, q["id"], cand)
    q2 = dict(q, candidates=[*(q.get("candidates") or []), cand])
    row = conn.execute(
        """UPDATE order_questions
              SET status = 'answered', answer = %s,
                  answered_by = %s, answered_at = now()
            WHERE id = %s AND status = 'open'
            RETURNING id""",
        (Json({"choice": card_ean}), "codex-card-auto", q["id"])).fetchone()
    if not row:
        return False
    teach.KINDS["dl_supplier"].apply(conn, cfg, q2, card_ean, "codex-card-auto")
    log.info("dl codex-card auto-answer: closed dl_supplier question %s with ean %s "
             "(card added/edited in /znalosti), message reprocessed", q["id"], card_ean)
    return True


def _release_stuck_siblings_by_name(conn, card_ean: str, card_name: str) -> int:
    """#323 residual 2 — the `emails=[]` case: reset orphaned same-supplier stuck messages
    whose normalized ENVELOPE `from_name` equals this card's UNAMBIGUOUS normalized name
    (`dl_match.supplier_name_key`). Returns how many were reset; `0` on a blank/ambiguous
    name or no match, never raises.

    Conservative by construction — releases NOTHING unless the card's normalized name maps
    to exactly ONE distinct ean across every current card (the same distinct-ean ambiguity
    measure `resolve_supplier_from_cards` uses); if another card shares the normalized name
    the identity is ambiguous and those messages stay a nástenka question.

    A released message is only put BACK into the claim pool — the reprocess re-extracts and
    re-runs the deterministic rung on the ACTUAL document, so a false-positive `from_name`
    match never ships a wrong EDI: it either resolves correctly or raises a fresh question.
    `from_name` is the only supplier-identity signal a NOT-yet-extracted stuck message
    carries in a queryable column, so it is used only as a conservative SELECTOR of which
    messages are worth reprocessing, never as the final supplier decision. Every #265 safety
    exclusion of the by-address rung holds identically (`processed=true`,
    `proc_status='review'`, no `order_questions` row, no `status='error'` event)."""
    card_ean = str(card_ean or "").strip()
    name_key = dl_match.supplier_name_key(card_name or "")
    if not card_ean or not name_key:
        return 0
    cards = dl_snapshot.dl_suppliers_for_management(conn)
    name_eans = {str(c.get("ean_edi") or "").strip() for c in cards
                 if str(c.get("ean_edi") or "").strip()
                 and dl_match.supplier_name_key(c.get("name", "")) == name_key}
    if name_eans != {card_ean}:
        return 0  # blank name, or the normalized name is shared by another card -> ambiguous
    rows = conn.execute(
        """SELECT message_id, from_name FROM messages
            WHERE category = %s AND processed = true AND proc_status = 'review'
              AND from_name IS NOT NULL AND btrim(from_name) <> ''
              AND NOT EXISTS (SELECT 1 FROM order_questions oq
                              WHERE oq.message_id = messages.message_id)
              AND NOT EXISTS (SELECT 1 FROM email_events e
                              WHERE e.message_id = messages.message_id
                                AND e.status = 'error')
            ORDER BY created_at ASC LIMIT %s""",
        (CATEGORY, _STUCK_SIBLING_CANDIDATE_LIMIT)).fetchall()
    ids = [mid for (mid, fname) in rows
           if dl_match.supplier_name_key(fname or "") == name_key][:_STUCK_SIBLING_LIMIT]
    if not ids:
        return 0
    conn.execute(
        """UPDATE messages SET processed = false, processing_at = NULL, attempts = 0
            WHERE message_id = ANY(%s)""", (ids,))
    log.info("dl codex-card name-rung release: reset %d orphaned stuck message(s) whose "
             "from_name matches unambiguous card name %r back into the claim pool",
             len(ids), card_name)
    return len(ids)
