"""The warehouse's learned SHOP rules (#493): (sender address, shop key) -> customer card.

A customer with several shops sends one attachment per shop. When the resolver cannot pin a
shop to a card (two shops in one city, a header with no name at all), the order is held and
the warehouse is asked ONE question for that shop (`teach.ask_customer(key=...)`). Its
answer is remembered here, so the same shop of the same sender goes to the same card from
the next mail on — every shop is asked at most once.

Written ONLY by a human board answer (`order_customers.learn`); never by the model, the
automatic re-resolve sweep or any other automation. Every write is audited
(`board.services.audit`) and soft-deleted (`deleted_at`), so the Kôš can bring a rule back or
take it away, and undoing the answer takes its rule back.

`customer.resolve` reads the rules of ONE sender per mail (`for_sender`) and matches them in
pure code (`store_key.match_rule`): a rule applies only to EXACTLY its shop (its words equal
the order's whole shop identity — never a subset, never a mail-relative part); live rules of
one shop that disagree are asked again (`customer._by_shop_rule`) — never by recency.
"""
from __future__ import annotations

import logging

from .store_key import StoreKey

log = logging.getLogger("orders.store_rules")

TABLE = "customer_store_rules"
_COLS = ("sender_email", "store_key", "store_label", "ean_edi", "question_id", "created_by")
# our own mail domain: the sender of a staff forward whose original sender is unknown
# (`pipeline._sender_address`) — shared by every customer the staff forwards
OUR_DOMAIN = "slovnormal.sk"


def is_own_address(value) -> bool:
    """An address of our own domain (or a subdomain) — never a customer's identity (review
    round 28: rules and the shop question keyed on a staff forwarder were shared by every
    customer it forwards; „Predajňa 1" of one customer shipped another's order). The exact
    domain, never a substring („x@notslovnormal.sk" is a customer, round 29)."""
    addr = str(value or "").strip().lower()
    if "@" not in addr:
        return False
    domain = addr.rsplit("@", 1)[1]
    return domain == OUR_DOMAIN or domain.endswith("." + OUR_DOMAIN)


def _sender(value) -> str:
    """The rule owner: the normalized address — never one of our own (no rules for it)."""
    sender = str(value or "").strip().lower()
    return "" if is_own_address(sender) else sender


def any_live(conn, senders, eans) -> bool:
    """Is any live rule owned by one of these addresses or pointing at one of these cards?
    (`order_customers.shop_history` — the family's history, review round 28)."""
    senders = sorted({s for s in (_sender(v) for v in senders or ()) if s})
    eans = sorted({str(e) for e in eans or () if e})
    if not senders and not eans:
        return False
    return conn.execute(
        f"SELECT 1 FROM {TABLE} WHERE deleted_at IS NULL "
        "AND (sender_email = ANY(%s) OR ean_edi = ANY(%s)) LIMIT 1",
        (senders, eans)).fetchone() is not None


def for_sender(conn, sender_email: str) -> list[tuple[frozenset, str]]:
    """The live rules of this sender: [(the shop key's words, card EAN)]."""
    sender = _sender(sender_email)
    if not sender:
        return []
    rows = conn.execute(
        f"SELECT store_key, ean_edi FROM {TABLE} WHERE sender_email = %s "
        "AND deleted_at IS NULL ORDER BY id", (sender,)).fetchall()
    return [(frozenset((r[0] or "").split()), r[1]) for r in rows if (r[0] or "").strip()]


def _row(conn, rid) -> dict | None:
    r = conn.execute(f"SELECT {', '.join(_COLS)} FROM {TABLE} WHERE id = %s",
                     (rid,)).fetchone()
    return dict(zip(_COLS, r, strict=True)) if r else None


def _audit(conn, **kw) -> None:
    from ..board.services import audit  # leaf module, lazy like teach does
    audit.record(conn, table=TABLE, **kw)


def remember(conn, *, sender_email: str, key: StoreKey, ean_edi: str, question_id: int,
             by: str) -> int | None:
    """Teach (sender, shop key) -> card from a human answer. Returns the live rule id.

    The same answer again is a no-op; a DIFFERENT card for the same shop replaces the old
    rule (soft delete + audit, Kôš-restorable — never silently overwritten). A blank sender,
    shop key or card teaches nothing (None)."""
    sender, ean = _sender(sender_email), str(ean_edi or "").strip()
    if not (sender and key is not None and key.text and ean):
        return None
    live = conn.execute(
        f"SELECT id, ean_edi FROM {TABLE} WHERE sender_email = %s AND store_key = %s "
        "AND deleted_at IS NULL ORDER BY id", (sender, key.text)).fetchall()
    same = next((int(rid) for rid, old_ean in live if old_ean == ean), None)
    # every OTHER live rule of this shop goes (also the second live rule a Kôš restore of a
    # replaced one left behind — else the shop stays a conflict, asked forever)
    for rid, old_ean in live:
        if old_ean == ean:
            continue
        before = _row(conn, rid)
        conn.execute(f"UPDATE {TABLE} SET deleted_at = now() WHERE id = %s", (rid,))
        _audit(conn, actor=by or "sklad", row_id=rid, action="delete", before=before,
               note=f"nahradené odpoveďou na otázku #{question_id}",
               question_id=question_id)
        log.warning("shop rule %s/%r: %s replaced by %s (question %s)", sender, key.text,
                    old_ean, ean, question_id)
    if same is not None:
        log.info("shop rule %s/%r -> %s already known (rule %s)", sender, key.text, ean, same)
        return same
    rid = conn.execute(
        f"""INSERT INTO {TABLE} (sender_email, store_key, store_label, ean_edi, question_id,
                                 created_by)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        (sender, key.text, key.label[:300], ean, question_id, by or "")).fetchone()[0]
    _audit(conn, actor=by or "sklad", row_id=rid, action="create", after=_row(conn, rid),
           note=f"predajňa {key.label} → {ean}", question_id=question_id)
    log.info("shop rule learned: %s / %r -> %s (question %s, by %s)", sender, key.text, ean,
             question_id, by)
    return int(rid)


def forget_for_question(conn, question_id: int, by: str = "auto:teach") -> int:
    """Take back the rule(s) a question's answer taught (undo). Returns how many."""
    rows = conn.execute(
        f"SELECT id FROM {TABLE} WHERE question_id = %s AND deleted_at IS NULL",
        (question_id,)).fetchall()
    for (rid,) in rows:
        before = _row(conn, rid)
        conn.execute(f"UPDATE {TABLE} SET deleted_at = now() WHERE id = %s", (rid,))
        _audit(conn, actor=by, row_id=rid, action="delete", before=before,
               note=f"odpoveď na otázku #{question_id} vrátená", question_id=question_id)
        log.warning("shop rule %s taken back with question %s", rid, question_id)
    return len(rows)
