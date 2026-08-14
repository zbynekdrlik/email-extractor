"""#314 — memory of suppliers the warehouse marked "Netýka sa skladu" (not a warehouse
delivery note), so their LATER mails stop generating board questions forever.

Continuation of #307, which added the terminal "Netýka sa skladu" close
(`dl_worker.close_message_not_warehouse`) but DELIBERATELY did not remember the sender
(its own docstring: "one sender sends both warehouse and non-warehouse mail — auto-
suppressing would silently drop a future REAL delivery note"). #314 makes remembering
SAFE by two design choices, both enforced here + in `dl_worker._process_document`:

  * keyed on the supplier's OWN IDENTITY from the document — registry `ean_edi` ∪ a
    normalized name — never blindly the email address (req 3: one sender, e.g. a
    `tlaciaren@` that forwards everything, can send both warehouse and non-warehouse mail).
    The email is stored for audit/fallback but is the sole match basis ONLY for a row that
    has no other identity at all (an unregistered supplier we could only key by address);
  * a GTIN-match SAFETY OVERRIDE lives in the worker (req 4): a remembered supplier whose
    mail DOES carry a catalog item is never silently dropped — this module only answers the
    "is this supplier known not-warehouse" question, the worker decides skip-vs-keep.

Deliberately a small standalone lookup table + pure functions, exactly like the sibling
`dl_supplier_memory.py` — NOT an override/rebuild framework. `sweep_open_questions` /
`close_message_not_warehouse` reuse #307's own terminal close path, never a parallel one.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("orders.dl_nonwarehouse")

# A conservative trailing Slovak legal-form suffix — stripped so "Messer Tatragas, spol.
# s r.o." and "MESSER TATRAGAS spol s r o" key identically. NOT a general normalizer.
# The leading separator is MANDATORY (`[\s,]+`, not `[\s,]*`): the legal form is always a
# SEPARATE trailing token, and requiring a real gap before it stops the "a.s." branch from
# greedily eating a genuine word ending — e.g. the trailing "as" of "Tatragas" (proven by
# test_name_key_strips_legal_suffix_and_normalizes, which caught exactly this).
_LEGAL_SUFFIX_RE = re.compile(
    r"[\s,]+(spol\.?\s*s\s*r\.?\s*o\.?|s\.?\s*r\.?\s*o\.?|a\.?\s*s\.?|"
    r"k\.?\s*s\.?|v\.?\s*o\.?\s*s\.?|s\.?\s*p\.?)\.?\s*$",
    re.IGNORECASE | re.UNICODE)


def _name_key(name: str) -> str:
    """Conservative supplier-name identity key: lowercase, strip a trailing legal-form
    suffix, drop punctuation, collapse whitespace. NOT `memory.item_key` (the FUZZY wording
    normalizer — reusing it for an identity key is the #159 mistake): this only removes the
    cosmetic variation in how the SAME company name is written, it never folds two genuinely
    different names together."""
    s = str(name or "").strip().lower()
    if not s:
        return ""
    s = _LEGAL_SUFFIX_RE.sub("", s)
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _email_key(email: str) -> str:
    """Exact identity, never fuzzy — same reasoning `dl_supplier_memory._norm` gives."""
    return str(email or "").strip().lower()


def remember(conn, supplier_ean: str = "", name: str = "", sender_email: str = "") -> bool:
    """Record a supplier as non-warehouse. Keyed on (supplier_ean, name_key). The email is
    kept only as a fallback identity: when a REAL identity (ean or name) exists, the stored
    email is blanked so it can never split one supplier into several rows nor become a match
    key (see `resolve`). Idempotent (`ON CONFLICT DO NOTHING`). Returns False only when
    there is no usable identity at all."""
    ean = str(supplier_ean or "").strip()
    name_key = _name_key(name)
    email = _email_key(sender_email)
    if ean or name_key:
        # A real supplier identity exists — the address is not the key (req 3). Blank it so
        # the SAME supplier mailing from two addresses is ONE row, and so `resolve`'s
        # email-fallback clause (which requires ean='' AND name_key='') never matches this
        # row on address alone.
        email = ""
    if not (ean or name_key or email):
        return False
    # RETURNING id → a real insert returns a row, an ON CONFLICT no-op returns None
    # (adversarial-review finding, #314): so `seed_from_history`/`record_for_message` count
    # only rows they ACTUALLY added, and the every-boot bootstrap log reports 0 seeded once
    # the memory is populated, instead of the full historical count every time.
    row = conn.execute(
        """INSERT INTO dl_nonwarehouse_supplier (supplier_ean, name_key, sender_email, name)
               VALUES (%s, %s, %s, %s)
           ON CONFLICT (supplier_ean, name_key, sender_email) DO NOTHING
           RETURNING id""",
        (ean, name_key, email, str(name or ""))).fetchone()
    if row is not None:
        log.info("dl non-warehouse supplier remembered: ean=%r name_key=%r email=%r",
                 ean, name_key, email)
    return row is not None


def resolve(conn, supplier_ean: str = "", name: str = "", sender_email: str = "") -> dict | None:
    """Is this supplier remembered non-warehouse? Matches on the supplier's OWN identity
    first — registry EAN OR normalized name — and falls back to the sender email ONLY when
    BOTH sides carry no better identity: the ROW is email-only (no ean, no name — an
    unregistered supplier we could key only by address, e.g. david@grena.sk) AND the QUERY
    itself extracted no ean and no name. Req 3 is encoded directly in the SQL, on BOTH sides
    (adversarial-review finding, #314): without the query-side blankness a bare email-only
    memory row (which `seed_from_history` GUARANTEES exists, since every pre-#314 dl_supplier
    closure payload had no `supplier_name`) would suppress EVERY later mail from that shared
    address — even one whose document extracts a different, real supplier identity — which is
    exactly the tlaciaren@-forwards-everything loss this ticket exists to prevent. Returns
    the matched row (dict) or None."""
    ean = str(supplier_ean or "").strip()
    name_key = _name_key(name)
    email = _email_key(sender_email)
    row = conn.execute(
        """SELECT supplier_ean, name_key, sender_email, name
             FROM dl_nonwarehouse_supplier
            WHERE (supplier_ean <> '' AND supplier_ean = %(ean)s)
               OR (name_key <> '' AND name_key = %(name_key)s)
               OR (supplier_ean = '' AND name_key = '' AND sender_email <> ''
                   AND sender_email = %(email)s
                   AND %(ean)s = '' AND %(name_key)s = '')
            LIMIT 1""",
        {"ean": ean, "name_key": name_key, "email": email}).fetchone()
    if not row:
        return None
    return {"supplier_ean": row[0], "name_key": row[1], "sender_email": row[2],
            "name": row[3]}


def _identity_from_question(kind: str, payload: dict, from_addr: str) -> tuple[str, str, str]:
    """The (ean, name, email) a dl_item/dl_supplier question carries — dl_item stores the
    matched registry `supplier_ean`/`supplier_name`; dl_supplier stores `sender_email` and
    (since #314) the extracted `supplier_name`. `from_addr` is the fallback email."""
    payload = payload or {}
    ean = payload.get("supplier_ean", "") or ""
    name = payload.get("supplier_name", "") or ""
    email = payload.get("sender_email", "") or (from_addr or "")
    return ean, name, email


def record_for_message(conn, message_id: str) -> int:
    """#314 req 1: remember the supplier(s) of a message the warehouse just marked
    not_warehouse. Reads the identity from that message's own dl_item/dl_supplier questions
    (payload), with the message's `from_addr` as the fallback email. Returns the number of
    remember() calls that recorded an identity (idempotent, so re-running is a no-op)."""
    if not message_id:
        return 0
    mrow = conn.execute("SELECT from_addr FROM messages WHERE message_id = %s",
                        (message_id,)).fetchone()
    from_addr = (mrow[0] if mrow else "") or ""
    rows = conn.execute(
        """SELECT kind, payload FROM order_questions
            WHERE message_id = %s AND kind IN ('dl_item', 'dl_supplier')""",
        (message_id,)).fetchall()
    n = 0
    for kind, payload in rows:
        ean, name, email = _identity_from_question(kind, payload, from_addr)
        if remember(conn, ean, name, email):
            n += 1
    return n


def seed_from_history(conn) -> int:
    """One-time backfill (idempotent): remember every supplier the warehouse ALREADY marked
    not_warehouse (#307) before this feature existed, so the memory recognises them without
    waiting for another click. #314 req 3."""
    rows = conn.execute(
        """SELECT q.kind, q.payload, m.from_addr
             FROM order_questions q
             LEFT JOIN messages m ON m.message_id = q.message_id
            WHERE q.status = 'not_warehouse'
              AND q.kind IN ('dl_item', 'dl_supplier')""").fetchall()
    n = 0
    for kind, payload, from_addr in rows:
        ean, name, email = _identity_from_question(kind, payload, from_addr or "")
        if remember(conn, ean, name, email):
            n += 1
    return n


def sweep_open_questions(conn, cfg=None) -> int:
    """#314 req 3: close currently-OPEN dl_item/dl_supplier questions whose supplier is
    already remembered non-warehouse. Reuses #307's terminal close path
    (`dl_worker.close_message_not_warehouse`) — never a parallel mechanism — so each such
    message is marked processed with a rollup `not_warehouse` event, exactly like a real
    warehouse click. Returns the number of MESSAGES closed.

    Two SAFETY gates (adversarial-review findings, #314) — close is message-level, so it
    must never destroy a genuine delivery:

      * only messages that SHIPPED NOTHING are eligible — `proc_status NOT IN
        ('ok','partial','duplicate')` (a partial ship of a remembered mixed supplier, e.g.
        EKVIA/Messer, whose unmatched line the req-4 override deliberately kept as an open
        question, must NOT be swept — that would overwrite `partial`→`not_warehouse` and
        misreport a real delivery as "netýka sa skladu") AND no `email_events` error row
        (the `_release_stuck_siblings`/#265 lesson: an upload failure is not a skip);
      * a message is closed ONLY when EVERY one of its open dl_item/dl_supplier questions
        resolves to a remembered supplier — so a multi-supplier mail (one remembered, one
        not) is never wholly closed on the strength of a single remembered document."""
    from . import dl_worker  # lazy: dl_worker imports teach which imports this module
    rows = conn.execute(
        """SELECT q.id, q.payload, q.message_id, m.from_addr
             FROM order_questions q
             JOIN messages m ON m.message_id = q.message_id
            WHERE q.status = 'open' AND q.kind IN ('dl_item', 'dl_supplier')
              AND COALESCE(m.proc_status, '') NOT IN ('ok', 'partial', 'duplicate')
              AND NOT EXISTS (SELECT 1 FROM email_events e
                               WHERE e.message_id = q.message_id AND e.status = 'error')
            ORDER BY q.id""").fetchall()
    by_msg: dict[str, list[tuple]] = {}
    for qid, payload, message_id, from_addr in rows:
        by_msg.setdefault(message_id, []).append((qid, payload or {}, from_addr or ""))
    closed = 0
    for qs in by_msg.values():
        all_remembered = all(
            resolve(conn,
                    payload.get("supplier_ean", "") or "",
                    payload.get("supplier_name", "") or "",
                    (payload.get("sender_email", "") or from_addr)) is not None
            for _qid, payload, from_addr in qs)
        if all_remembered and qs:
            dl_worker.close_message_not_warehouse(conn, qs[0][0])
            closed += 1
    return closed


def bootstrap(conn, cfg=None) -> dict:
    """One-time-ish, fully idempotent startup step (#314 req 3): seed the memory from
    historical not_warehouse closures, then close any still-open question of a now-
    remembered supplier. Safe to run on EVERY boot (both halves are idempotent — a re-run
    finds nothing new to seed and no open remembered-supplier question to close)."""
    seeded = seed_from_history(conn)
    swept = sweep_open_questions(conn, cfg)
    log.info("dl_nonwarehouse bootstrap: seeded=%d, swept_messages=%d", seeded, swept)
    return {"seeded": seeded, "swept": swept}
