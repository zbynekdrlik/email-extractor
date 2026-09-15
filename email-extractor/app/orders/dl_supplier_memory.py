"""Taught DL supplier addresses (#202, DL migration F3) — the nástenka's "ktorý dodávateľ?"
question teaches a `sender_email -> ean_edi` mapping, so the SAME address is never asked twice.

Deliberately NOT a `customer_overrides`-style full override/rebuild system (see the design
comment on #202): AI-orders built that machinery because customer RECORDS need broad manual
editing (retiring, adding, editing address fields) merged against a live sheet snapshot on every
read. DL suppliers stay entirely sheet-driven (`app/orders/dl_snapshot.py`'s own
`dl_supplier_snapshot`) — this module only ever needs to answer one small question, "which EAN
does this address belong to", so a standalone lookup table is the honest size for what was
actually asked, not an invitation to build the bigger system nobody requested.
"""
from __future__ import annotations

import logging

log = logging.getLogger("orders.dl_supplier_memory")


def _norm(email: str) -> str:
    """Exact identity, never fuzzy — same reasoning `teach.ask_customer`'s own docstring gives
    for why `memory.item_key` (a WORDING normalizer) must never be reused for an address."""
    return str(email or "").strip().lower()


def remember(conn, sender_email: str, ean_edi: str, name: str = "",
             *, cfg=None) -> bool:
    """Teach (or correct) which supplier this address belongs to. `ON CONFLICT ... DO UPDATE`
    (not `DO NOTHING`) — unlike a delivery record, a taught mapping is a single current fact
    about one address; a re-teach is a correction of a mis-click, not a second historical
    event, so overwriting in place is right (mirrors `dl_snapshots`' own `_freeze` "one current
    row per identity" pattern, not `item_memory`'s append-only shipment history).

    #407: when `cfg` is provided and the address is a configured scanner/relay sender
    (`delivery_notes_scanner_senders`), the write is silently refused — a scanner forwards
    mail from EVERY supplier and is never a supplier identity."""
    email = _norm(sender_email)
    if not (email and ean_edi):
        return False
    # #407: refuse to learn a scanner/relay address as a supplier identity.
    if cfg is not None:
        from .dl_questions import is_scanner_sender
        if is_scanner_sender(cfg, email):
            log.info("dl supplier memory REFUSED scanner address: %s (not an identity)",
                     email)
            return False
    conn.execute(
        """INSERT INTO dl_supplier_memory (sender_email, ean_edi, name)
               VALUES (%s, %s, %s)
           ON CONFLICT (sender_email) DO UPDATE
               SET ean_edi = EXCLUDED.ean_edi, name = EXCLUDED.name""",
        (email, str(ean_edi), name or ""))
    log.info("dl supplier taught: %s -> %s (%s)", email, ean_edi, name)
    return True


def resolve(conn, sender_email: str) -> dict | None:
    """The taught supplier for this address, or `None` when nobody has taught one yet."""
    email = _norm(sender_email)
    if not email:
        return None
    row = conn.execute(
        "SELECT ean_edi, name FROM dl_supplier_memory "
        "WHERE sender_email = %s AND deleted_at IS NULL",  # #442 soft-delete
        (email,)).fetchone()
    return {"ean_edi": row[0], "name": row[1] or ""} if row else None


def forget(conn, sender_email: str) -> bool:
    """The undo-half of `remember()` — a mis-taught address goes back to asking."""
    email = _norm(sender_email)
    if not email:
        return False
    row = conn.execute(
        "DELETE FROM dl_supplier_memory WHERE sender_email = %s RETURNING id",
        (email,)).fetchone()
    return row is not None


# --- #447 board lane 6: curate a taught supplier mapping from the nástenka „Naučené sklad"
# tab. Unlike `forget()` (a HARD delete for the reopen-the-question undo flow), the board must
# SOFT-delete (spec §5 — recoverable from the Kôš); `resolve()` already filters
# `deleted_at IS NULL`, so a soft delete stops the mapping by construction. `update_by_id`
# edits the target (ean_edi/name) of one existing row without changing its sender_email key,
# so it can never turn a genuine address into a scanner identity (#407) — the address is
# fixed. Both return the PRE-edit `before` dict (audit + Kôš restore) or None when nothing
# matched.

def soft_delete(conn, rid: int) -> dict | None:
    """Soft-delete ONE supplier-memory row by id. Idempotent (`deleted_at IS NULL` guard)."""
    row = conn.execute(
        "UPDATE dl_supplier_memory SET deleted_at = now() "
        "WHERE id = %s AND deleted_at IS NULL "
        "RETURNING sender_email, ean_edi, name", (rid,)).fetchone()
    if not row:
        return None
    return {"sender_email": row[0], "ean_edi": row[1], "name": row[2] or ""}


def update_by_id(conn, rid: int, *, ean_edi: str, name: str) -> dict | None:
    """Edit the taught target (EAN + name) of one row in place. `ean_edi` is required (the
    mapping is meaningless without it) — a blank raises ValueError (route → 400). Returns the
    PRE-edit `before` dict, or None when the row does not exist / is deleted."""
    ean_edi = str(ean_edi or "").strip()
    if not ean_edi:
        raise ValueError("chýba EAN dodávateľa")
    before = conn.execute(
        "SELECT ean_edi, name FROM dl_supplier_memory "
        "WHERE id = %s AND deleted_at IS NULL", (rid,)).fetchone()
    if not before:
        return None
    conn.execute(
        "UPDATE dl_supplier_memory SET ean_edi = %s, name = %s WHERE id = %s",
        (ean_edi, name or "", rid))
    return {"ean_edi": before[0], "name": before[1] or ""}
