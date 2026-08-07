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


def remember(conn, sender_email: str, ean_edi: str, name: str = "") -> bool:
    """Teach (or correct) which supplier this address belongs to. `ON CONFLICT ... DO UPDATE`
    (not `DO NOTHING`) — unlike a delivery record, a taught mapping is a single current fact
    about one address; a re-teach is a correction of a mis-click, not a second historical
    event, so overwriting in place is right (mirrors `dl_snapshots`' own `_freeze` "one current
    row per identity" pattern, not `item_memory`'s append-only shipment history)."""
    email = _norm(sender_email)
    if not (email and ean_edi):
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
        "SELECT ean_edi, name FROM dl_supplier_memory WHERE sender_email = %s",
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
