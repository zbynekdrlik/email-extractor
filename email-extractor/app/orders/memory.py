"""Item memory (#63): what we actually shipped THIS customer for THIS wording.

The information a customer omits ("rožok", no flavour, no weight) is usually not in the
email at all — it is in our own shipment history. This module owns that history and the
rules for reading it, so the matching ladder has exactly one place to ask.

It replaces the n8n Data Table `mX1EIECccDfTa9ab`, which has no unique key: one shipment
writes a row per item, the JS then deduped by (gtin, day, cnt), and a seed row carrying
`cnt` was once read as 18 deliveries — releasing the weight override far too early. Here
the key is in the schema and strength is counted in DAYS, which is what "we shipped it
this often" actually means.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass

log = logging.getLogger("orders.memory")

# A wobbly history resolves to the newest card only with this share of all deliveries.
MAJORITY = 0.6
# Overriding the weight guard ships a different gramáž than the customer wrote, so it
# needs an unambiguous history of at least this many distinct delivery days.
WEIGHT_OVERRIDE_DAYS = 3


@dataclass(frozen=True)
class Recalled:
    gtin: str
    card: str
    strength: int          # distinct delivery days for this card
    unanimous: bool        # only ever shipped this one card for this wording
    last_day: str
    weight_override: bool  # may override the weight guard (unanimous + 3 days)
    # A warehouse answer (#88), not machine-learned history: it decides on its own, outranks
    # every model rung, and is never subject to the as-of window below.
    human: bool = False

    @property
    def note(self) -> str:
        return f"{self.strength}x, naposledy {self.last_day}"


def item_key(name: str) -> str:
    """Normalize a wording into its memory key.

    Diacritics and case are noise; a stated weight is NOT — "rožok 70g" and "rožok" are
    different products, and collapsing them is exactly the Céder incident (70 g shipped
    as štandart 50 g). Spaces inside the weight are collapsed so "70 g" == "70g".
    """
    s = unicodedata.normalize("NFD", str(name or "").lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"(\d+)\s*(kg|gr|g|ml|l)\b", r"\1\2", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def remember(conn, customer_ean: str, item: str, gtin: str, card: str,
             delivered_on, source: str = "ship") -> bool:
    """Record one delivery. Returns False when this delivery is already known.

    The unique key is (customer, wording, card, DAY): the pipeline writes one row per
    shipped item, and a re-run of the same order must not look like a second delivery.
    """
    key = item_key(item)
    if not (customer_ean and key and gtin):
        return False
    row = conn.execute(
        """INSERT INTO item_memory
               (customer_ean, item_key, item_raw, gtin, card, delivered_on, source)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (customer_ean, item_key, gtin, delivered_on) DO NOTHING
           RETURNING id""",
        (str(customer_ean), key, str(item), str(gtin), card or "", delivered_on, source),
    ).fetchone()
    return row is not None


def resolve(conn, customer_ean: str, item: str, as_of: str = "") -> Recalled | None:
    """What we shipped for this wording, or None when the history does not speak clearly.

    Silence is a valid answer: an item with no clear history goes to the warehouse for
    checking instead of being guessed at.

    `as_of` (the day the email arrived) restricts the history to deliveries BEFORE it. That
    is right in production — a delivery booked for next week cannot decide today's order —
    and it is what keeps the golden corpus honest, since history seeded from the archive
    would otherwise contain the very order being scored.

    A HUMAN answer (#88) is exempt from `as_of` and wins outright: a mapping the warehouse
    typed is an instruction, not evidence, so it applies to the very email that triggered the
    question — including one that arrives the same day.
    """
    key = item_key(item)
    if not (customer_ean and key):
        return None
    taught = conn.execute(
        """SELECT gtin, max(card), max(delivered_on)
             FROM item_memory
            WHERE customer_ean = %s AND item_key = %s AND source = 'human'
            GROUP BY gtin ORDER BY max(created_at) DESC LIMIT 1""",
        (str(customer_ean), key)).fetchone()
    if taught:
        return Recalled(gtin=str(taught[0]), card=taught[1] or "", strength=1,
                        unanimous=True, last_day=str(taught[2]), weight_override=True,
                        human=True)
    rows = conn.execute(
        """SELECT gtin,
                  max(card)                    AS card,
                  count(DISTINCT delivered_on) AS days,
                  max(delivered_on)            AS last_day
             FROM item_memory
            WHERE customer_ean = %s AND item_key = %s
              AND (%s::date IS NULL OR delivered_on < %s::date)
            GROUP BY gtin""",
        (str(customer_ean), key, as_of or None, as_of or None)).fetchall()
    if not rows:
        return None

    total = sum(r[2] for r in rows)
    newest = max(rows, key=lambda r: (r[3], r[2]))
    unanimous = len(rows) == 1
    if unanimous:
        chosen = rows[0]
    elif newest[2] / total >= MAJORITY:
        chosen = newest
    else:
        log.info("memory undecided for %r (%s): %s", item, customer_ean,
                 {r[0]: r[2] for r in rows})
        return None

    return Recalled(
        gtin=str(chosen[0]),
        card=chosen[1] or "",
        strength=int(chosen[2]),
        unanimous=unanimous,
        last_day=str(chosen[3]),
        weight_override=unanimous and int(chosen[2]) >= WEIGHT_OVERRIDE_DAYS,
    )


def remember_global(conn, item: str, gtin: str, card: str, question_id: int | None,
                     taught_by: str = "") -> bool:
    """Teach a wording GLOBALLY — the answer for every customer, not just the one who asked
    (#102). A product name ("Twister") is not one buyer's private nickname; once someone
    tells us what it means, every future customer using the same word should be answered for
    free, never asked again.

    `ON CONFLICT (item_key) DO NOTHING`: first teach wins. Changing an existing global answer
    needs an explicit `undo` first — the same UX the per-customer human answer already has, no
    new concept. Returns False when a global mapping for this wording already exists.
    """
    key = item_key(item)
    if not (key and gtin):
        return False
    row = conn.execute(
        """INSERT INTO global_item_memory (item_key, item_raw, gtin, card, question_id,
                                           taught_by)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (item_key) DO NOTHING
           RETURNING id""",
        (key, str(item), str(gtin), card or "", question_id, taught_by or ""),
    ).fetchone()
    return row is not None


def resolve_global(conn, item: str) -> Recalled | None:
    """The globally-taught answer for this wording, or None when nobody has taught one.

    Always a fallback: `match.decide_without_model` only consults this AFTER the customer's
    own taught mapping, the catalog-certain rungs, and the customer's own unanimous delivery
    history have all had their turn — a real customer-specific signal outranks a generic
    crowd answer.
    """
    key = item_key(item)
    if not key:
        return None
    row = conn.execute(
        "SELECT gtin, card FROM global_item_memory WHERE item_key = %s", (key,)).fetchone()
    if not row:
        return None
    return Recalled(gtin=str(row[0]), card=row[1] or "", strength=1, unanimous=True,
                    last_day="", weight_override=True)


def forget_global(conn, item: str, question_id: int) -> None:
    """Undo a global teaching — but ONLY the row THIS question created (#102).

    A different customer's later answer to the same wording never creates a second global row
    (remember_global's ON CONFLICT DO NOTHING), so its `question_id` still points at whichever
    question taught it first. Undoing that LATER, redundant answer must not erase a mapping
    some OTHER question owns — checking `question_id` is what keeps the retraction traceable
    to its origin, which is the whole point of recording it.
    """
    key = item_key(item)
    if not key:
        return
    conn.execute(
        "DELETE FROM global_item_memory WHERE item_key = %s AND question_id = %s",
        (key, question_id))


def import_n8n_rows(conn, rows: list[dict]) -> int:
    """One-off import of the existing n8n Data Table rows. Returns rows actually stored.

    The source table's `at` is a full timestamp, so several rows of one shipment differ
    only by time; they collapse into one delivery day here, which is the point.
    """
    stored = 0
    for r in rows:
        day = str(r.get("at") or "")[:10]
        if not day:
            continue
        if remember(conn, str(r.get("cust") or ""), str(r.get("item") or ""),
                    str(r.get("gtin") or ""), str(r.get("card") or ""),
                    delivered_on=day, source=str(r.get("src") or "n8n")):
            stored += 1
    log.info("item memory: imported %d of %d n8n rows", stored, len(rows))
    return stored


def seed_taught(conn, entries: list[dict]) -> int:
    """One-off seeding of taught mappings for the eval corpus (#102), mirroring
    `seed_from_archive` — the ask/answer/teach.py HTTP flow is what production uses and what
    `test_orders_teach.py` covers; the offline eval harness only needs the RESULTING state
    (`item_memory(human)` / `global_item_memory`), which is what a corpus case can actually
    observe (the harness forces shadow mode, so `teach.ask`/`teach.answer`'s side effects
    never run during a corpus replay — only reads like `memory.resolve`/`resolve_global` do).

    Each entry: `{"scope": "customer", "customer_ean": ..., "wording": ..., "gtin": ...,
    "card": ...}` or `{"scope": "global", "wording": ..., "gtin": ..., "card": ...}`.
    Returns the number of mappings actually stored (new ones only).
    """
    stored = 0
    for e in entries:
        scope = str(e.get("scope") or "customer")
        wording, gtin, card = str(e.get("wording") or ""), str(e.get("gtin") or ""), \
            str(e.get("card") or "")
        if scope == "global":
            if remember_global(conn, wording, gtin, card, question_id=None,
                               taught_by=str(e.get("taught_by") or "corpus")):
                stored += 1
        else:
            ean = str(e.get("customer_ean") or "")
            if remember(conn, ean, wording, gtin, card, delivered_on=_today(conn),
                       source="human"):
                stored += 1
    log.info("eval corpus: seeded %d of %d taught mapping(s)", stored, len(entries))
    return stored


def _today(conn):
    return conn.execute("SELECT current_date").fetchone()[0]


def seed_from_archive(conn, orders: list[dict]) -> int:
    """Seed the delivery history from orders we really shipped.

    The history inherited from n8n's Data Table covered 11 customers and 123 lines, so for
    almost every customer there was nothing to remember and each unweighted wording ("Chlieb
    pšenično ražný", "Šiška") was decided by a guess instead of by what the warehouse has
    always delivered (#81.2). Each entry is `{customer_ean, delivered_on, items:[{name, card,
    gtin}]}`; a line without a card teaches nothing and is skipped. Idempotent, like every
    other write here.
    """
    added = 0
    for order in orders:
        ean = str(order.get("customer_ean") or "")
        day = str(order.get("delivered_on") or "")
        if not (ean and day):
            continue
        for item in order.get("items") or []:
            if not (item.get("gtin") and item.get("card")):
                continue
            if remember(conn, ean, item.get("name") or item["card"], item["gtin"],
                        item["card"], delivered_on=day, source="archive"):
                added += 1
    return added
