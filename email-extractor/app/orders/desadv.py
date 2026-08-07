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

log = logging.getLogger("orders.desadv")

# Same role as edi.py's own CLAIM_STALE_MINUTES: how long a bare (unconfirmed) claim is
# trusted to mean "another worker may genuinely be mid-upload right now". A deliberately
# SEPARATE constant, not an import of edi.py's — the two ledgers are independent knobs
# that currently share a value by coincidence, not by contract.
CLAIM_STALE_MINUTES = 10


def claim_send(conn, supplier_ean: str, doc_number: str, filename: str) -> bool:
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
    """
    ean, doc = str(supplier_ean or ""), str(doc_number or "")
    if not ean or not doc:
        log.warning("desadv.claim_send refused — missing supplier_ean/doc_number "
                    "(supplier_ean=%r doc_number=%r)", ean, doc)
        return False
    row = conn.execute(
        """INSERT INTO desadv_sent (supplier_ean, doc_number, filename)
           VALUES (%s, %s, %s)
           ON CONFLICT (supplier_ean, doc_number)
           DO UPDATE SET sent_at = now(), filename = EXCLUDED.filename
           WHERE desadv_sent.uploaded_at IS NULL
             AND desadv_sent.sent_at < now() - make_interval(mins => %s)
           RETURNING id""",
        (ean, doc, filename, CLAIM_STALE_MINUTES),
    ).fetchone()
    if row is None:
        log.warning("DESADV already sent (or claimed within %sm) for supplier=%s doc=%s "
                    "— refusing a duplicate upload", CLAIM_STALE_MINUTES, supplier_ean,
                    doc_number)
    return row is not None


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


def release_send(conn, supplier_ean: str, doc_number: str) -> None:
    """Give the claim back after a failed upload, so the document can be retried."""
    conn.execute(
        "DELETE FROM desadv_sent WHERE supplier_ean = %s AND doc_number = %s",
        (str(supplier_ean or ""), str(doc_number or "")))
