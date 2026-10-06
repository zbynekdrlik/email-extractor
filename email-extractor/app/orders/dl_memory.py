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


# #465: a later non-human ship history for ANOTHER card only contradicts a human answer when
# it carries at least this weight (sum of per-day max(cnt), same measure as R66) — one stray
# delivery is not a majority.
NEWER_CONTRARY_MIN = 2

# #488: the „Nie je skladová položka — vždy vynechať" rule (see the section at the bottom): the
# sentinel stored in `gtin` — letters, never a card number — and the rule's display label
NOT_STOCK = "not_stock"
NOT_STOCK_LABEL = "Nie je skladová položka — vždy vynechať"


@dataclass(frozen=True)
class Recalled:
    gtin: str
    card: str
    strength: int           # sum of per-day max(cnt) — see module docstring
    unanimous: bool         # only ever shipped this one card for this wording
    last_day: str
    weight_override: bool   # may override the R74 weight-conflict guard
    human: bool = False     # a warehouse answer (nástenka), outranks every weighted rung
    # #465 — the history VERDICT `dl_match.decide_item`'s R73 rescue checks before it may
    # substitute this gtin silently (see `resolve()`): every DISTINCT still-in-catalog human
    # answer for the wording (>1 = the sklad contradicted itself), the card a strictly-NEWER
    # non-human ship history mostly went to when that is a DIFFERENT card, and whether the
    # sklad already explicitly settled this wording on the board (a resolved memory-conflict
    # question, or a question of the very message being reprocessed).
    human_gtins: tuple[str, ...] = ()
    newer_gtin: str = ""
    confirmed: bool = False

    @property
    def note(self) -> str:
        return f"{self.strength}x, naposledy {self.last_day}"


def dl_item_question_key(supplier_ean: str, wording: str) -> str:
    """The synthetic `order_questions.item_key` a `dl_item` question is stored under —
    `dlitem:{supplier_ean}:{item_key(wording)}`. Lives HERE (not in `teach`, which imports
    this module) so `resolve()` can read the board's own answers for a wording without an
    import cycle; `teach.dl_item_key` delegates to it, so the two can never drift."""
    return f"dlitem:{supplier_ean}:{item_key(wording)}"


def remember(conn, supplier_ean: str, item: str, gtin: str | None, card: str,
             delivered_on, cnt: int | None = 1, source: str = "ship") -> bool:
    """Record one delivery (or one imported n8n history row). Returns False when this
    exact (supplier, wording, gtin, day, cnt) is already known AND no source promotion
    happened (#402: a `source='human'` write that collides with a `source='ship'` row
    promotes it and returns True).

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
        cnt_val = 1 if cnt is None else max(1, int(cnt))
    except (TypeError, ValueError):
        cnt_val = 1
    # #402: a source='human' write must never be silently lost when a 'ship' row with the
    # same conflict key already exists — the warehouse's answer outranks machine-inferred
    # history. DO UPDATE promotes the existing row to 'human' (and refreshes item_raw/card)
    # only when the incoming source IS 'human' and the stored row is NOT already 'human'.
    # The WHERE clause makes a same-source duplicate behave like DO NOTHING (no row returned
    # by RETURNING, so the function returns False — preserving the existing dedup semantics).
    # #465: a human answer that collides with its own SOFT-DELETED row (superseded by a
    # resolved memory conflict, or removed in the Kôš) revives it — the UNIQUE identity is not
    # partial, so without this the new answer would be silently swallowed.
    row = conn.execute(
        """INSERT INTO dl_item_memory
               (supplier_ean, item_key, item_raw, gtin, card, delivered_on, cnt, source)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (supplier_ean, item_key, gtin, delivered_on, cnt) DO UPDATE
              SET source = 'human', item_raw = EXCLUDED.item_raw, card = EXCLUDED.card,
                  deleted_at = NULL
            WHERE EXCLUDED.source = 'human'
              AND (dl_item_memory.source IS DISTINCT FROM 'human'
                   OR dl_item_memory.deleted_at IS NOT NULL)
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
           as_of: str = "", message_id: str = "") -> Recalled | None:
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

    #465: the human rung ALSO reports the history verdict (`Recalled.human_gtins`/
    `newer_gtin`/`confirmed`) — the newest human answer still LEADS, but it no longer speaks
    alone: `dl_match.decide_item` refuses to rescue with it silently when the sklad
    contradicted itself, when later deliveries mostly went to another card, or when the card
    shares no word with the wording (a board misclick poisoned every later below-threshold
    match of one Dobrota roll wording onto a fruit card). `message_id` is the message being
    matched — its OWN answered question counts as confirmation (the reprocess right after the
    sklad answered it), never another message's plain answer (which is exactly the misclick).

    #488: a „nie je skladová položka" rule row (`gtin = NOT_STOCK`) is never a card — every rung
    skips it, even with no catalog filter (the engine reads the rule via `not_stock_keys`).
    """
    key = item_key(item)
    if not (supplier_ean and key):
        return None
    taught_rows = conn.execute(
        """SELECT gtin, max(card) AS card, max(delivered_on) AS last_day, max(created_at) AS at
             FROM dl_item_memory
            WHERE supplier_ean = %s AND item_key = %s AND source IN ('human', 'teachback')
              AND deleted_at IS NULL AND gtin <> %s
            GROUP BY gtin ORDER BY at DESC""",
        (str(supplier_ean), key, NOT_STOCK)).fetchall()
    valid_taught = [r for r in taught_rows
                    if catalog_gtins is None or str(r[0]) in catalog_gtins]
    if valid_taught:
        gtin, card, last_day, _at = valid_taught[0]
        human_gtins = tuple(str(r[0]) for r in valid_taught)
        newer_gtin = _newer_contrary_gtin(conn, supplier_ean, key, str(gtin), last_day,
                                          catalog_gtins, as_of)
        confirmed = _board_confirmed(conn, supplier_ean, item, str(gtin), message_id)
        if len(human_gtins) > 1 or newer_gtin:
            log.warning("dl memory verdict for %r (%s): newest human answer %s, all human "
                        "answers %s, newer contrary history %r, confirmed=%s", item,
                        supplier_ean, gtin, human_gtins, newer_gtin, confirmed)
        return Recalled(gtin=str(gtin), card=card or "", strength=1, unanimous=True,
                        last_day=str(last_day), weight_override=True, human=True,
                        human_gtins=human_gtins, newer_gtin=newer_gtin, confirmed=confirmed)

    rows = conn.execute(
        """SELECT gtin, delivered_on, max(cnt) AS c, max(card) AS card
             FROM dl_item_memory
            WHERE supplier_ean = %s AND item_key = %s AND source <> 'human'
              AND deleted_at IS NULL AND gtin <> %s
              AND (%s::date IS NULL OR delivered_on < %s::date)
            GROUP BY gtin, delivered_on""",
        (str(supplier_ean), key, NOT_STOCK, as_of or None, as_of or None)).fetchall()
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


def _newer_contrary_gtin(conn, supplier_ean: str, key: str, human_gtin: str, human_day,
                         catalog_gtins, as_of: str) -> str:
    """#465: the card that non-human ship history delivered STRICTLY AFTER the human answer's
    day mostly went to, when that is a DIFFERENT card than the human answer and carries at
    least `NEWER_CONTRARY_MIN` weight (R66's own per-day max(cnt) measure) — else "". A
    single stray delivery, or later history agreeing with the human answer, is no conflict."""
    rows = conn.execute(
        """SELECT gtin, delivered_on, max(cnt)
             FROM dl_item_memory
            WHERE supplier_ean = %s AND item_key = %s
              AND source NOT IN ('human', 'teachback') AND deleted_at IS NULL
              AND delivered_on > %s
              AND (%s::date IS NULL OR delivered_on < %s::date)
            GROUP BY gtin, delivered_on""",
        (str(supplier_ean), key, human_day, as_of or None, as_of or None)).fetchall()
    weight: dict[str, int] = {}
    for gtin, _day, cnt in rows:
        if catalog_gtins is not None and str(gtin) not in catalog_gtins:
            continue
        weight[str(gtin)] = weight.get(str(gtin), 0) + int(cnt)
    if not weight:
        return ""
    top = sorted(weight, key=lambda g: (-weight[g], g))[0]
    if (top != str(human_gtin) and weight[top] >= NEWER_CONTRARY_MIN
            and weight[top] > weight.get(str(human_gtin), 0)):
        return top
    return ""


def _board_confirmed(conn, supplier_ean: str, item: str, gtin: str, message_id: str) -> bool:
    """#465: did the sklad EXPLICITLY settle this wording on the nástenka with `gtin`? The
    newest answered `dl_item` question for the (supplier, wording) that is EITHER a resolved
    memory-conflict question (`payload.memory_conflict`, both cards were on screen) OR a
    question of the very message being matched now (the reprocess right after the answer).
    A plain answer from ANOTHER message never counts — that is exactly the q189 misclick,
    which must not keep shipping silently on every later delivery. #467: a `codex_missing`
    question is deliberately NOT a standing confirmation either (it is just a list of cards,
    a misclick there must be caught the same way); its drifted-name case is covered by the
    CODEX name counting for the R73 lexical check instead (`dl_match._memory_conflict`)."""
    row = conn.execute(
        """SELECT answer->>'choice' FROM order_questions
            WHERE kind = 'dl_item' AND status = 'answered' AND customer_ean = ''
              AND item_key = %s
              AND (payload->>'memory_conflict' = 'true' OR message_id = %s)
            ORDER BY answered_at DESC NULLS LAST, id DESC LIMIT 1""",
        (dl_item_question_key(supplier_ean, item), message_id or "")).fetchone()
    return bool(row) and str(row[0] or "") == str(gtin)


def supersede_taught(conn, supplier_ean: str, wording: str, keep_gtin: str, *,
                     actor: str = "auto:teach", question_id=None,
                     message_id=None) -> list[int]:
    """#465: the sklad resolved a memory conflict for this wording with `keep_gtin` — SOFT-
    delete every OTHER live human/teachback answer for the SAME (supplier, wording), so the
    contradiction is gone for good (never 'the latest wins' again). Soft delete only (spec §5
    — the row stays, recoverable from the Kôš); real ship history (source='ship'/n8n) is
    evidence and is never touched. Each superseded row gets a `dl_item_memory` `delete` audit
    row tied to `question_id` — the SAME shape the board's alias delete writes, so the Kôš
    lists it, 'Vrátiť' restores it, and undoing the answer restores it
    (`restore_superseded`). Returns the superseded row ids."""
    key = item_key(wording)
    if not (supplier_ean and key and keep_gtin):
        return []
    rows = conn.execute(
        """UPDATE dl_item_memory SET deleted_at = now()
            WHERE supplier_ean = %s AND item_key = %s AND gtin <> %s
              AND source IN ('human', 'teachback') AND deleted_at IS NULL
           RETURNING id""",
        (str(supplier_ean), key, str(keep_gtin))).fetchall()
    ids = [int(r[0]) for r in rows]
    (log.warning if ids else log.info)(
        "dl memory conflict resolved for %r (%s): kept %s, superseded human rows %s",
        wording, supplier_ean, keep_gtin, ids)
    for rid in ids:
        _audit(conn, actor=actor, row_id=rid, action="delete", question_id=question_id,
               message_id=message_id, note="#465 memory conflict resolved on the board")
    return ids


def restore_superseded(conn, question_id: int, by: str = "auto:teach") -> list[int]:
    """#465: undo of a resolved memory-conflict answer brings back every human answer that
    answer superseded — through the Kôš's own sanctioned restore (`audit.restore` on each
    `delete` audit row of this question), never a bespoke UPDATE. Rows already restored by
    hand are skipped. Returns the restored `dl_item_memory` ids."""
    rows = conn.execute(
        """SELECT DISTINCT ON (m.id) a.id, m.id FROM audit_log a
             JOIN dl_item_memory m ON m.id::text = a.row_id
            WHERE a.table_name = 'dl_item_memory' AND a.action = 'delete'
              AND a.question_id = %s AND m.deleted_at IS NOT NULL
            ORDER BY m.id, a.id DESC""", (question_id,)).fetchall()   # newest per row
    from ..board.services import audit  # lazy: a leaf module, no import cycle
    restored = []
    for audit_id, mem_id in rows:
        try:
            audit.restore(conn, int(audit_id), by=by)
            restored.append(int(mem_id))
        except Exception:
            log.exception("restoring superseded dl_item_memory row %s (audit %s) failed",
                          mem_id, audit_id)
    log.info("dl memory conflict answer %s undone: restored superseded rows %s",
             question_id, restored)
    return restored


def _audit(conn, **kw) -> None:
    """Best-effort `dl_item_memory` audit row (lazy import of the leaf audit module); an
    audit failure never breaks the memory write it records."""
    try:
        from ..board.services import audit
        audit.record(conn, table="dl_item_memory", **kw)
    except Exception:
        log.exception("audit of dl_item_memory row %s failed", kw.get("row_id"))


# --- #445 board lane 4: curated (nástenka) alias management for a DL card. The parallels of
# memory.add_customer_alias / delete_item_memory_row on the SUPPLIER-keyed dl_item_memory —
# same soft-delete doctrine (spec §5: never hard-delete; the row stays, recoverable from the
# Kôš, and `resolve()` already filters `deleted_at IS NULL`). Kept here beside `remember` so
# every dl_item_memory write path lives in ONE module.

CURATED_SOURCES = ("human", "sheet-import", "teachback")


def add_dl_alias(conn, supplier_ean: str, wording: str, gtin: str, card: str,
                 source: str = "human") -> int | None:
    """Teach a wording for ONE supplier directly (nástenka Produkty sklad card / história
    teachback), dated today. `source` defaults to 'human' (the card editor); the history
    teachback passes 'teachback' — BOTH are honoured by `resolve()`'s taught-first rung
    (`source IN ('human','teachback')`), so either treats it exactly like a warehouse answer.
    Returns the new row's id, or None when the (supplier, wording, gtin, day, cnt) identity
    already exists (idempotent) or a required field is missing."""
    key = item_key(wording)
    if not (supplier_ean and key and gtin):
        return None
    row = conn.execute(
        """INSERT INTO dl_item_memory
               (supplier_ean, item_key, item_raw, gtin, card, delivered_on, cnt, source)
           VALUES (%s, %s, %s, %s, %s, current_date, 1, %s)
           ON CONFLICT (supplier_ean, item_key, gtin, delivered_on, cnt) DO NOTHING
           RETURNING id""",
        (str(supplier_ean), key, str(wording), str(gtin), card or "", source)).fetchone()
    return int(row[0]) if row else None


def delete_dl_item_memory_row(conn, row_id: int, supplier_ean: str) -> bool:
    """SOFT-delete ONE curated DL alias (source='human'/'sheet-import' only), scoped to
    `supplier_ean` so a card page can never remove another supplier's row by guessing an id,
    and restricted to curated sources so real delivery history (source='ship') is never
    deletable here (that would corrupt `resolve()`'s weighted majority). Idempotent: an
    already-deleted row matches nothing and returns False. Mirrors
    `memory.delete_item_memory_row` exactly."""
    row = conn.execute(
        """UPDATE dl_item_memory SET deleted_at = now()
            WHERE id = %s AND supplier_ean = %s AND source = ANY(%s)
              AND deleted_at IS NULL
           RETURNING id""",
        (row_id, str(supplier_ean), list(CURATED_SOURCES))).fetchone()
    return row is not None


def update_dl_item_memory_row(conn, row_id: int, *, item_raw: str, gtin: str,
                              card: str) -> dict | None:
    """#447 board lane 6: edit ONE curated DL alias in place (source='human'/'sheet-import'
    only). Recomputes `item_key` from the new wording (else `resolve()` keeps matching the
    OLD key). Returns the PRE-edit `before` dict (audit + Kôš update-restore) or None when the
    row does not exist / is deleted / is not curated. Mirrors `memory.update_item_memory_row`."""
    before = conn.execute(
        "SELECT item_key, item_raw, gtin, card FROM dl_item_memory "
        "WHERE id = %s AND deleted_at IS NULL AND source = ANY(%s)",
        (row_id, list(CURATED_SOURCES))).fetchone()
    if not before:
        return None
    if str(gtin) == NOT_STOCK and before[2] != NOT_STOCK:
        # #488: a typed number must never turn an alias into a silent-drop rule — the rule is
        # born only by the board answer (with its confirmation) or `remember_not_stock`
        raise ValueError("„Nie je skladová položka“ vzniká len odpoveďou na otázke na nástenke")
    if before[2] == NOT_STOCK and (item_key(item_raw) != before[0] or str(gtin) != NOT_STOCK):
        # #488: nor is a rule retargeted to another wording (it would silently drop a real stock
        # line) or turned into a typed card — delete it and answer / add the alias instead
        raise ValueError("Pravidlo „Nie je skladová položka“ sa nedá presmerovať — zmaž ho a "
                         "odpovedz na otázku (alebo pridaj alias v Produkty sklad)")
    conn.execute(
        "UPDATE dl_item_memory SET item_key = %s, item_raw = %s, gtin = %s, card = %s "
        "WHERE id = %s",
        (item_key(item_raw), str(item_raw), str(gtin), card or "", row_id))
    return {"item_key": before[0], "item_raw": before[1], "gtin": before[2],
            "card": before[3] or ""}


# --- #488: the learned LINE-level rule „Nie je skladová položka — vždy vynechať". A recurring
# service line of ONE supplier (EKVIA „PREPRAVNÉ" = transport) is decided once: a row with the
# sentinel `gtin = NOT_STOCK`, source 'human', keyed like the dl_item question. `resolve()` never
# reads it as a card (every rung skips it); `not_stock_keys` is what the engine reads
# (`dl_not_stock` owns the question halves and the engine helpers). Every write is audited on
# `dl_item_memory`, so the Kôš lists and reverts it.

def remember_not_stock(conn, supplier_ean: str, wording: str, *, actor: str, question_id=None,
                       message_id=None) -> int | None:
    """Learn the rule for (supplier, wording); returns its row id (None when a field is
    missing). A live rule is never duplicated — it is RE-ASSERTED as the newest decision
    (`created_at = now()`, so it wins over a card taught after it again), audited as an
    `update` whose Kôš restore puts the old `created_at` back. Otherwise a new row — or the
    same-day soft-deleted one revived (the UNIQUE identity is not partial) with a fresh
    `created_at`, unlike `remember()` (the rule's precedence is its age) — audited `create`."""
    key = item_key(wording)
    if not (supplier_ean and key):
        return None
    live = conn.execute(
        """UPDATE dl_item_memory m SET created_at = now()
             FROM (SELECT id, created_at AS old_at FROM dl_item_memory
                    WHERE supplier_ean = %s AND item_key = %s AND gtin = %s
                      AND deleted_at IS NULL ORDER BY id LIMIT 1) o
            WHERE m.id = o.id
           RETURNING m.id, o.old_at, m.created_at""",
        (str(supplier_ean), key, NOT_STOCK)).fetchone()
    if live:
        rid, old_at, new_at = int(live[0]), live[1], live[2]
        _audit(conn, actor=actor, row_id=rid, action="update", question_id=question_id,
               message_id=message_id,
               before={"created_at": old_at.isoformat() if old_at else None},
               after={"created_at": new_at.isoformat() if new_at else None},
               note="#488 pravidlo „vždy vynechať“ znovu potvrdené")
        log.info("not-stock rule for %r (%s) re-asserted as the newest decision (row %s)",
                 wording, supplier_ean, rid)
        return rid
    rid = int(conn.execute(
        """INSERT INTO dl_item_memory
               (supplier_ean, item_key, item_raw, gtin, card, delivered_on, cnt, source)
           VALUES (%s, %s, %s, %s, %s, current_date, 1, 'human')
           ON CONFLICT (supplier_ean, item_key, gtin, delivered_on, cnt) DO UPDATE
              SET deleted_at = NULL, created_at = now(), source = 'human',
                  item_raw = EXCLUDED.item_raw, card = EXCLUDED.card
           RETURNING id""",
        (str(supplier_ean), key, str(wording), NOT_STOCK, NOT_STOCK_LABEL)).fetchone()[0])
    _audit(conn, actor=actor, row_id=rid, action="create", question_id=question_id,
           message_id=message_id,
           after={"supplier_ean": str(supplier_ean), "item_raw": str(wording), "item_key": key,
                  "gtin": NOT_STOCK, "card": NOT_STOCK_LABEL, "source": "human"},
           note="#488 nie je skladová položka — vždy vynechať")
    log.warning("not-stock rule learned: %r of supplier %s is never a stock line (row %s, "
                "question %s, by %s)", wording, supplier_ean, rid, question_id, actor)
    return rid


def forget_not_stock(conn, supplier_ean: str, wording: str, *, actor: str, question_id=None,
                     message_id=None) -> list[int]:
    """Soft-delete every live rule for (supplier, wording), each with a `delete` audit row (the
    Kôš „Vrátiť" brings it back). Returns the removed ids."""
    rows = conn.execute(
        "UPDATE dl_item_memory SET deleted_at = now() WHERE supplier_ean = %s AND item_key = %s"
        " AND gtin = %s AND deleted_at IS NULL RETURNING id",
        (str(supplier_ean), item_key(wording), NOT_STOCK)).fetchall()
    ids = [int(r[0]) for r in rows]
    for rid in ids:
        _audit(conn, actor=actor, row_id=rid, action="delete", question_id=question_id,
               message_id=message_id, note="#488 pravidlo „vždy vynechať“ zrušené")
    log.warning("not-stock rule for %r (%s) removed: rows %s", wording, supplier_ean, ids)
    return ids


def not_stock_keys(conn, supplier_ean: str) -> set[str]:
    """The `item_key`s of this supplier's wordings whose NEWEST live taught decision
    (`source IN human/teachback`, the taught-first rung of `resolve()`) is the rule — a card
    taught later for the same wording sends the line back to the matcher."""
    if not supplier_ean:
        return set()
    rows = conn.execute(
        """SELECT item_key FROM (
               SELECT DISTINCT ON (item_key) item_key, gtin FROM dl_item_memory
                WHERE supplier_ean = %s AND source IN ('human', 'teachback')
                  AND deleted_at IS NULL
                ORDER BY item_key, created_at DESC NULLS LAST, id DESC) newest
            WHERE gtin = %s""",
        (str(supplier_ean), NOT_STOCK)).fetchall()
    return {r[0] for r in rows}
