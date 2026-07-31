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
    """
    key = item_key(item)
    if not (customer_ean and key):
        return None
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
