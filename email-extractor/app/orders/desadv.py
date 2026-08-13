"""DESADV (dodací list EDI) upload ledger — two-phase claim/confirm (#200 F1).

This is the delivery-notes (DL) counterpart of `edi.py`'s `edi_sent` ledger, but with
a deliberately DIFFERENT identity and two-phase from inception rather than retrofitted:

- **Identity is `(supplier_ean, doc_number)`, never bare `doc_number`.** The n8n
  registry ("dodacie listy" Data Table) keys ONLY on the human doc number (R90) — two
  different suppliers can legitimately reuse a short number (the mapping's own example:
  Jackulík's "68944") and collide as a false "duplicate", silently losing a real DL
  (W4). Scoping by supplier fixes that at the schema level.
- **No content hash.** `edi_sent` hashes the built EDI content because two DIFFERENT
  orders can legitimately share `(customer_ean, delivery_date)` — the content is what
  actually distinguishes them. A DL's identity is the DOCUMENT itself: one delivery
  note has exactly one number from its own supplier. The n8n registry never hashed
  content either (R90), and a genuine content change under the same
  `(supplier_ean, doc_number)` is a correction to the same document, not a new send.
- **Claim BEFORE upload, from day one.** The n8n workflow reads/writes the registry
  only AFTER the upload succeeds (W2/W3) — a crash between upload and registration
  either loses the registration (next attempt re-uploads = a genuine ORION duplicate)
  or a race between two same-docNumber messages both pass the pre-upload dedup check
  before either registers. `edi_sent` only grew this discipline later, after 13 real
  orders were lost (#153); this ledger starts with it.

See docs/superpowers/specs/2026-08-07-delivery-notes-python-design.md for the full
rules map this fixes (R90, W2, W3, W4).

No pipeline code calls this yet (#200 is foundation-only) — these functions exist so a
later phase's worker has a tested, correct primitive to build on.
"""
from __future__ import annotations

import logging
import time

import psycopg

from . import claim as claim_mod
from . import desadv_edi

log = logging.getLogger("orders.desadv")

# Same role as edi.py's own CLAIM_STALE_MINUTES: how long a bare (unconfirmed) claim is
# trusted to mean "another worker may genuinely be mid-upload right now". A deliberately
# SEPARATE constant, not an import of edi.py's — the two ledgers are independent knobs
# that currently share a value by coincidence, not by contract.
CLAIM_STALE_MINUTES = 10


def claim_send(conn, supplier_ean: str, doc_number: str, filename: str,
               message_id: str = "") -> bool:
    """Claim the right to upload this document, or reclaim an orphaned one.

    False = a CONFIRMED upload already exists for this (supplier, doc_number),
    another claim is still fresh (< CLAIM_STALE_MINUTES) and may be mid-upload right
    now, or supplier_ean/doc_number is empty. The reclaim is atomic the same way
    edi.claim_send's is: `DO UPDATE ... WHERE` is evaluated by Postgres per-row as
    part of conflict resolution, so two workers racing this call can never both win.

    An empty identity is refused outright (review finding on #200's PR): this
    ledger has no content hash, so — unlike edi_sent — an empty doc_number would
    collapse EVERY numberless document from one supplier onto a single ledger row,
    silently losing every one after the first. R83 always generates a fallback
    docNumber when extraction found none; a caller reaching this with a genuinely
    empty one has a bug upstream, not a legitimate claim.

    `message_id` (#216) records WHICH message currently holds this claim — written
    on both the initial INSERT and a reclaim (a reclaim's new claimant may genuinely
    be a different message than the orphaned one). Optional/empty stays NULL, so a
    caller that doesn't pass it behaves exactly as before this column existed. See
    `claimed_by()` for the read-only counterpart this exists to feed: telling "this
    SAME message is retrying itself after a partial ship" apart from "a different
    message genuinely duplicated this document" (W7).
    """
    ean, doc = str(supplier_ean or ""), str(doc_number or "")
    mid = str(message_id or "") or None
    if not ean or not doc:
        log.warning("desadv.claim_send refused — missing supplier_ean/doc_number "
                    "(supplier_ean=%r doc_number=%r)", ean, doc)
        return False
    row = conn.execute(
        """INSERT INTO desadv_sent (supplier_ean, doc_number, filename, message_id)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (supplier_ean, doc_number)
           DO UPDATE SET sent_at = now(), filename = EXCLUDED.filename,
                         message_id = EXCLUDED.message_id
           WHERE desadv_sent.uploaded_at IS NULL
             AND desadv_sent.sent_at < now() - make_interval(mins => %s)
           RETURNING id""",
        (ean, doc, filename, mid, CLAIM_STALE_MINUTES),
    ).fetchone()
    if row is None:
        log.warning("DESADV already sent (or claimed within %sm) for supplier=%s doc=%s "
                    "— refusing a duplicate upload", CLAIM_STALE_MINUTES, supplier_ean,
                    doc_number)
    return row is not None


def claim_send_or_identify(conn, supplier_ean: str, doc_number: str, filename: str,
                           message_id: str = "") -> tuple[bool, str]:
    """`claim_send()` + `claimed_by()`, ATOMICALLY, in ONE round trip — closes a
    review-caught TOCTOU window on #216's own PR: `claim_send()` returning False and a
    SEPARATE follow-up `claimed_by()` read are two plain autocommit statements with no
    shared transaction, leaving a gap in which a genuinely different message could
    theoretically reclaim this same row and change its `message_id` before the read
    runs. In practice this could only ever mis-tag WHICH stage gets logged (never
    double-ship — the actual claim/reclaim decision below is exactly `claim_send()`'s
    own atomic `ON CONFLICT ... WHERE`), but a single statement removes the gap
    entirely rather than merely shrinking it.

    Returns `(claimed, current_message_id)`:
    - `claimed=True` — the claim was taken (a fresh insert or a stale reclaim);
      `current_message_id` is always `""` (nothing else to identify, the caller may
      proceed to upload).
    - `claimed=False` — refused (a CONFIRMED upload already exists, or another claim
      is still fresh); `current_message_id` is whoever holds it now, same value
      `claimed_by()` would return ("" for a legacy row with no `message_id`).

    The data-modifying CTE (`ins`) always runs the INSERT; the fallback `SELECT` only
    contributes a row via `NOT EXISTS (SELECT 1 FROM ins)` when the conflict path's
    own `WHERE` refused to update — at that point the table row is UNCHANGED by this
    statement, so reading it back directly is exactly the pre-existing claimant.
    `claim_send()`/`claimed_by()` stay as their own tested primitives (other callers,
    and directness for tests) — this is the race-free combination `dl_worker.py`
    actually uses in production.

    #271: built on the shared `claim.claim_or_identify()` primitive — the SQL text
    below is byte-identical to this function's own pre-#271 hand-written CTE (just
    split into an `insert_sql`/`identify_sql` pair the primitive re-joins the same
    way), proven equivalent by this module's FULL existing test suite passing
    unmodified against the ported version (see #271's own completion evidence)."""
    ean, doc = str(supplier_ean or ""), str(doc_number or "")
    mid = str(message_id or "") or None
    if not ean or not doc:
        log.warning("desadv.claim_send_or_identify refused — missing "
                    "supplier_ean/doc_number (supplier_ean=%r doc_number=%r)", ean, doc)
        return False, ""
    insert_sql = (
        "INSERT INTO desadv_sent (supplier_ean, doc_number, filename, message_id) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (supplier_ean, doc_number) "
        "DO UPDATE SET sent_at = now(), filename = EXCLUDED.filename, "
        "              message_id = EXCLUDED.message_id "
        "WHERE desadv_sent.uploaded_at IS NULL "
        "AND desadv_sent.sent_at < now() - make_interval(mins => %s) "
        "RETURNING NULL::text")
    identify_sql = ("SELECT d.message_id FROM desadv_sent d "
                    "WHERE d.supplier_ean = %s AND d.doc_number = %s")
    claimed, info = claim_mod.claim_or_identify(
        conn,
        insert_sql=insert_sql,
        insert_params=(ean, doc, filename, mid, CLAIM_STALE_MINUTES),
        identify_sql=identify_sql,
        identify_params=(ean, doc))
    holder = (info[0] or "") if info else ""
    if not claimed and info:
        log.warning("DESADV already sent (or claimed within %sm) for supplier=%s doc=%s "
                    "— refusing a duplicate upload", CLAIM_STALE_MINUTES, supplier_ean,
                    doc_number)
    elif not claimed:
        # claim_or_identify's own defensive "no row at all" case — cannot happen given
        # ean/doc are non-empty (see its docstring), but this must never be logged as
        # "already sent" (info == () here, not a real holder) — that would be a false
        # audit claim in a money-critical log.
        log.error("desadv.claim_send_or_identify: no row returned for supplier=%s "
                  "doc=%s — treating as refused", supplier_ean, doc_number)
    return claimed, holder


def confirm_sent(conn, supplier_ean: str, doc_number: str, pg_dsn: str = "") -> None:
    """Stamp the claim as a genuinely CONFIRMED upload — call this ONLY after the
    upload actually succeeded. Until this runs, the claim stays reclaimable by
    `claim_send` once it goes stale.

    By the time this runs the document is ALREADY physically in ORION — losing this
    one write is strictly worse than the few seconds a retry costs (an unconfirmed
    claim left behind would eventually go stale and be reclaimed for a genuine SECOND
    upload). Retries a few times; a `pg_dsn` lets it replace `conn` with a fresh
    connection between attempts. If every attempt still fails, this re-raises.
    """
    ean = str(supplier_ean or "")
    doc = str(doc_number or "")
    stmt = ("UPDATE desadv_sent SET uploaded_at = now() "
            "WHERE supplier_ean = %s AND doc_number = %s")
    attempts = 4
    for attempt in range(1, attempts + 1):
        try:
            cur = conn.execute(stmt, (ean, doc))
            if getattr(cur, "rowcount", None) == 0:
                # Same exposure edi.confirm_sent already has (review finding on #200's
                # PR, parity not regression): the claim row is gone underneath (released
                # or reclaimed elsewhere) — the confirmation silently landed on nothing,
                # and a later re-announcement could re-upload. Loud, not raised: the
                # document IS already physically in ORION either way.
                log.warning("confirm_sent found no matching claim for supplier=%s doc=%s "
                           "— was it released or reclaimed already?", supplier_ean,
                           doc_number)
            log.info("DESADV confirmed sent for supplier=%s doc=%s", supplier_ean, doc_number)
            return
        except Exception:
            log.exception("confirm_sent attempt %d/%d failed for supplier=%s doc=%s — the "
                          "document IS already uploaded, only the confirmation write "
                          "failed so far", attempt, attempts, supplier_ean, doc_number)
            if attempt == attempts:
                raise
            if pg_dsn:
                try:
                    conn = psycopg.connect(pg_dsn, autocommit=True)
                except Exception:
                    log.exception("reconnect for confirm_sent retry failed")
            time.sleep(1)


def already_sent(conn, supplier_ean: str, doc_number: str) -> bool:
    """READ-ONLY: was this (supplier, doc_number) already CONFIRMED uploaded? Never
    claims, never inserts, never touches a row — the shadow-mode counterpart of
    `claim_send()` (#204, DL migration F5). Shadow's whole guarantee is that nothing
    observable leaves the process; `claim_send()` always performs an INSERT/UPDATE (an
    orphaned reclaim is a genuine, intentional side effect for the LIVE engine), so
    shadow must never call it. A bare (unconfirmed, still-fresh) claim does NOT count
    here — only a genuinely confirmed upload is "already sent"; an in-flight live claim
    racing a shadow peek must not make shadow report a false duplicate."""
    ean, doc = str(supplier_ean or ""), str(doc_number or "")
    if not ean or not doc:
        return False
    row = conn.execute(
        "SELECT 1 FROM desadv_sent WHERE supplier_ean = %s AND doc_number = %s "
        "AND uploaded_at IS NOT NULL", (ean, doc)).fetchone()
    return row is not None


def claimed_by(conn, supplier_ean: str, doc_number: str) -> str:
    """READ-ONLY: which message_id currently holds the claim (or confirmed upload) for
    this (supplier, doc_number) — "" when there is no row at all, or the row predates
    `message_id` (a legacy claim, always NULL, never backfilled — #216). Never claims,
    never inserts, never touches a row, same read-only guarantee as `already_sent()`.

    This is what lets `dl_worker.py` tell apart, after `claim_send()` returns False:
    "THIS SAME message already shipped this document in an earlier attempt" (R17
    partial-ship retry — compare the return value to the CURRENT message's own id)
    from "a genuinely DIFFERENT message announced the same document" (a real W7
    cross-message duplicate). A legacy "" never equals a real message_id, so an
    existing pre-#216 row is always (and correctly) reported as the latter."""
    ean, doc = str(supplier_ean or ""), str(doc_number or "")
    if not ean or not doc:
        return ""
    row = conn.execute(
        "SELECT message_id FROM desadv_sent WHERE supplier_ean = %s AND doc_number = %s",
        (ean, doc)).fetchone()
    return (row[0] or "") if row else ""


def release_send(conn, supplier_ean: str, doc_number: str) -> None:
    """Give the claim back after a failed upload, so the document can be retried."""
    conn.execute(
        "DELETE FROM desadv_sent WHERE supplier_ean = %s AND doc_number = %s",
        (str(supplier_ean or ""), str(doc_number or "")))


def has_confirmed_collision(conn, supplier_ean: str, doc_number: str) -> bool:
    """#239 finding 6 review finding: `desadv_edi.stable_prefix()` truncates
    `doc_number` to its first `MAX_DOC_NUMBER_IN_FILENAME` (10) alnum characters for
    the on-wire SFTP filename (R89) — a collision risk that function's OWN docstring
    already flagged: "two genuinely different doc numbers that happen to share their
    first 10 alnum characters would collide onto the same stable prefix... this
    collision risk MUST be re-examined before either function becomes load-bearing for
    an actual safe-retry decision." `desadv_edi.already_landed()` is now exactly that
    load-bearing decision (`dl_worker._check_landed`, finding 6's remainder) — a
    collision there would silently CONFIRM a claim for a document that was never
    actually uploaded (a different, unrelated, already-confirmed document's file was
    matched instead) — the mirror image of the v0.9.70 double-upload incident this
    whole ticket exists to prevent, just silent LOSS instead of silent DUPLICATION.

    READ-ONLY, same guarantee as `already_sent()`/`claimed_by()`: cross-checks the
    LOCAL ledger — does any OTHER already-CONFIRMED `desadv_sent` row for this SAME
    supplier carry a filename that also matches `doc_number`'s own stable prefix? If
    so, THAT row's document is what a presence check actually found, never this one —
    the caller must not trust the match.

    NOT a complete fix for the underlying truncation risk — a collision against a file
    uploaded before this ledger started recording (2026-08-09), or against a genuinely
    DIFFERENT supplier whose EAN happens to share the same last-6-digit suffix
    (`stable_prefix()`'s own `ean_short = ean_edi[-6:]`), would still slip through.
    Left as an acknowledged residual scope, not this function's job to solve — see the
    design comment on #239 for why a full fix (widening the on-wire filename budget, or
    verifying uploaded CONTENT) is a separate, larger, `needs-user-decision`-class
    change."""
    ean, doc = str(supplier_ean or ""), str(doc_number or "")
    if not ean or not doc:
        return False
    prefix = desadv_edi.stable_prefix(ean, doc)
    rows = conn.execute(
        "SELECT filename FROM desadv_sent WHERE supplier_ean = %s "
        "AND uploaded_at IS NOT NULL AND doc_number != %s", (ean, doc)).fetchall()
    return any(desadv_edi._matches_stable_prefix(filename or "", prefix)
              for (filename,) in rows)
