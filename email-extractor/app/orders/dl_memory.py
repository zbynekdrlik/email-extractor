"""DL item-match history (#200 F1) — item_memory's sibling for delivery notes.

Same shape as app/orders/memory.py's `item_memory` (db.py:397-410), keyed by SUPPLIER
instead of customer, with one structural difference: the `cnt` column.

R66 (the delivery-notes matching rule this table exists to eventually serve) resolves
a mixed history by taking the newest card's GTIN only when it carries >= 60% of ALL
deliveries, WEIGHTED BY the n8n table's own per-row `cnt` field — a raw delivery count
that must be preserved verbatim. That is a genuinely different semantics from
`item_memory.resolve()`, which counts DISTINCT DELIVERY DAYS instead (a deliberate fix
for a DIFFERENT n8n bug — see that module's own docstring: a seed row's raw `cnt` was
once misread as "18 deliveries" there). Conflating the two by bolting a nullable `cnt`
onto `item_memory` would risk reintroducing exactly the bug `item_memory` was built to
fix, so this is a dedicated table.

The n8n Data Table this replaces ("dodacie_pamat_poloziek", MBCwHVhzsKjbQkVl) has no
unique key either — R66 documents its own dedup rule for that: duplicate rows are the
same underlying record when (gtin, day, cnt) match. This table's UNIQUE constraint
enforces that identity directly, so no JS-style re-dedup is ever needed on read.

CAVEAT (review finding on #200's PR): because `cnt` is part of that identity, TWO rows
can legitimately coexist for the same (supplier, wording, gtin, day) with DIFFERENT
`cnt` values — faithful to R66's own dedup rule, and harmless today since n8n only
ever writes `cnt=1` per real delivery (R91). It only bites if the SAME source row is
re-imported after its own `cnt` was edited upstream. Whichever later phase builds
`resolve()`'s weighted-majority read should take `max(cnt)` per (gtin, day), never
`sum(cnt)`, or a re-import could double-count a single delivery.

`resolve()` (#202, DL migration F3) is the promised counterpart, mirroring
`memory.resolve()`'s overall shape (a human-taught row wins unconditionally; otherwise a
weighted read over real deliveries) but implementing R66's OWN, genuinely different
majority rule — weighted by `cnt`, not by a plain count of distinct days. It follows
this module's own earlier guidance verbatim: duplicate rows for the same (gtin, day) are
collapsed by taking `max(cnt)`, never `sum(cnt)`, before summing across days — which
also reconciles R66's two-sentence "Strength = distinct days (or seed cnt)" into ONE
formula: a real single delivery is a `cnt=1` row (contributes 1), and a seed row that
already collapsed several real deliveries into one row carries that count directly — so
summing `max(cnt)` per day, across days, is "distinct days" in the common case and
"seed cnt" in the collapsed-import case, without needing two separate code paths.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .memory import item_key  # same normalization — R66 keys on EXACT wording incl. gramáž

log = logging.getLogger("orders.dl_memory")

# R66: a mixed history takes the newest record's card only when it carries this share of
# all weighted deliveries.
MAJORITY = 0.6
# R66: weightOverride (the WEIGHT-CONFLICT guard's escape, R74) needs an unanimous history
# of at least this many weighted deliveries — same bar as item_memory's WEIGHT_OVERRIDE_DAYS.
WEIGHT_OVERRIDE_MIN = 3


@dataclass(frozen=True)
class Recalled:
    gtin: str
    card: str
    strength: int           # sum of per-day max(cnt) — see module docstring
    unanimous: bool         # only ever shipped this one card for this wording
    last_day: str
    weight_override: bool   # may override the R74 weight-conflict guard
    human: bool = False     # a warehouse answer (nástenka), outranks every weighted rung

    @property
    def note(self) -> str:
        return f"{self.strength}x, naposledy {self.last_day}"


def remember(conn, supplier_ean: str, item: str, gtin: str, card: str,
             delivered_on, cnt: int = 1, source: str = "ship") -> bool:
    """Record one delivery (or one imported n8n history row). Returns False when this
    exact (supplier, wording, gtin, day, cnt) is already known.

    `cnt` is coerced defensively (review finding on #200's PR): a falsy value (0,
    None, "") OR a genuinely negative/non-numeric one (an export glitch, an upstream
    typo) all collapse to 1 rather than either poisoning R66's future weighted-majority
    math with a negative weight, or raising and aborting a whole batch import over one
    bad row.
    """
    key = item_key(item)
    if not (supplier_ean and key and gtin):
        return False
    try:
        cnt_val = max(1, int(cnt))
    except (TypeError, ValueError):
        cnt_val = 1
    row = conn.execute(
        """INSERT INTO dl_item_memory
               (supplier_ean, item_key, item_raw, gtin, card, delivered_on, cnt, source)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (supplier_ean, item_key, gtin, delivered_on, cnt) DO NOTHING
           RETURNING id""",
        (str(supplier_ean), key, str(item), str(gtin), card or "", delivered_on,
         cnt_val, source),
    ).fetchone()
    return row is not None


def import_n8n_rows(conn, rows: list[dict]) -> int:
    """One-off import of the n8n Data Table `dodacie_pamat_poloziek` (MBCwHVhzsKjbQkVl)
    export — rows with cust/item/gtin/card/at/src/cnt. Mirrors
    `memory.import_n8n_rows` exactly, plus carrying `cnt` through instead of
    discarding it (that field is the whole reason this table exists separately —
    see the module docstring). Returns the number of rows actually stored.

    The source table's `at` is a full timestamp; several rows of one shipment differ
    only by time and collapse into one delivery day here, same as `memory.py`'s import.

    A row's `cnt` is passed through RAW (not pre-cast) — `remember()` coerces it
    defensively, so one row with a garbled `cnt` (a non-numeric export glitch) is
    stored as `cnt=1` instead of raising and aborting the rest of the batch (review
    finding on #200's PR).
    """
    stored = 0
    for r in rows:
        day = str(r.get("at") or "")[:10]
        if not day:
            continue
        if remember(conn, str(r.get("cust") or ""), str(r.get("item") or ""),
                    str(r.get("gtin") or ""), str(r.get("card") or ""),
                    delivered_on=day, cnt=r.get("cnt"),
                    source=str(r.get("src") or "n8n")):
            stored += 1
    log.info("dl item memory: imported %d of %d n8n rows", stored, len(rows))
    return stored


def resolve(conn, supplier_ean: str, item: str, catalog_gtins=None,
           as_of: str = "") -> Recalled | None:
    """R66: what we have on record shipping this SUPPLIER for this wording, or `None` when
    the history does not speak clearly. Silence is a valid answer — `dl_match.decide_item`'s
    MEMORY RESCUE simply does not fire, and the line is decided (or asked about) some other
    way.

    `catalog_gtins` (R66: "catalog-card disappearance invalidates the memory") restricts every
    rung to gtins still present in the CURRENT catalog — pass the live snapshot's gtin set;
    `None` (the default) skips this filter, e.g. for a caller with no catalog handy yet.

    `as_of` mirrors `memory.resolve()`'s own semantics: restrict to deliveries strictly BEFORE
    this day. Optional (defaults to no restriction) — DL has no eval corpus yet to make this
    load-bearing, but the parameter exists so one can be built later without an API change.

    A `source='human'` row (the nástenka's "ktorá karta je táto DL položka?" answer, #202)
    outranks everything below unconditionally — mirrors `memory.resolve()`'s own taught-first
    rung exactly, including the weight-guard override (a human decision is not something the
    guard exists to second-guess).

    A supplier can legitimately have been taught MORE THAN ONE (gtin, wording) mapping over
    time — a correction, or teaching the same wording again after the first card was retired.
    All candidate gtins (newest group first) are checked against `catalog_gtins` in turn, so a
    still-valid OLDER human teach is not silently skipped in favour of falling through to
    machine-inferred ship history just because the MOST RECENT teach happens to name a gtin
    that has since left the catalog (review finding on this issue's PR).
    """
    key = item_key(item)
    if not (supplier_ean and key):
        return None
    taught_rows = conn.execute(
        """SELECT gtin, max(card) AS card, max(delivered_on) AS last_day, max(created_at) AS at
             FROM dl_item_memory
            WHERE supplier_ean = %s AND item_key = %s AND source = 'human'
            GROUP BY gtin ORDER BY at DESC""",
        (str(supplier_ean), key)).fetchall()
    for gtin, card, last_day, _at in taught_rows:
        if catalog_gtins is None or str(gtin) in catalog_gtins:
            return Recalled(gtin=str(gtin), card=card or "", strength=1, unanimous=True,
                            last_day=str(last_day), weight_override=True, human=True)

    rows = conn.execute(
        """SELECT gtin, delivered_on, max(cnt) AS c, max(card) AS card
             FROM dl_item_memory
            WHERE supplier_ean = %s AND item_key = %s AND source <> 'human'
              AND (%s::date IS NULL OR delivered_on < %s::date)
            GROUP BY gtin, delivered_on""",
        (str(supplier_ean), key, as_of or None, as_of or None)).fetchall()
    if catalog_gtins is not None:
        rows = [r for r in rows if str(r[0]) in catalog_gtins]
    if not rows:
        return None

    by_gtin: dict[str, dict] = {}
    for gtin, day, cnt, card in rows:
        agg = by_gtin.setdefault(str(gtin), {"weight": 0, "days": 0, "last_day": None,
                                             "card": ""})
        agg["weight"] += int(cnt)
        agg["days"] += 1
        agg["card"] = card or agg["card"]
        if agg["last_day"] is None or str(day) > agg["last_day"]:
            agg["last_day"] = str(day)

    total = sum(a["weight"] for a in by_gtin.values())
    newest_gtin = max(by_gtin, key=lambda g: (by_gtin[g]["last_day"], by_gtin[g]["weight"]))
    unanimous = len(by_gtin) == 1
    if unanimous:
        chosen_gtin = next(iter(by_gtin))
    elif by_gtin[newest_gtin]["weight"] / total >= MAJORITY:
        chosen_gtin = newest_gtin
    else:
        log.info("dl memory undecided for %r (%s): %s", item, supplier_ean,
                 {g: a["weight"] for g, a in by_gtin.items()})
        return None

    chosen = by_gtin[chosen_gtin]
    return Recalled(
        gtin=chosen_gtin, card=chosen["card"], strength=chosen["weight"],
        unanimous=unanimous, last_day=chosen["last_day"] or "",
        weight_override=unanimous and chosen["weight"] >= WEIGHT_OVERRIDE_MIN)
