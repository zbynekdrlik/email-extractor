"""Hold an order while its question is unanswered — but only until the delivery date (#93).

Shipping the matched part of an order now and the taught line later would write TWO ORION
documents for ONE delivery day — the exact defect fixed in #81.1 (40 and 10 delivered instead
of 50). So a pending question (#88's teach-once loop) holds its WHOLE order: nothing is built,
nothing uploaded, until either the question is answered or the delivery date arrives.

Two release paths, both funnelled through the SAME `pipeline._ship_one` / `edi.claim_send`
ledger the live pipeline already ships through, so a late answer arriving after the deadline
has already shipped can never double-upload:

- `release_for_question` — fires right after `teach.answer` settles a question. Only once
  EVERY question id a held order is waiting on has been answered does it re-check its stored
  decisions against fresh memory (no LLM call — `match.decide_without_model` again) and ship.
- `release_due` — the deadline backstop, a periodic sweep. Ships whatever matched using the
  ORIGINALLY stored decisions, unchanged — today's "ship what matched, name what's missing"
  behaviour, now gated on the delivery date instead of firing immediately.

`worker._claim` excludes any message with an open (`status='held'`) row here, so a held
message is never silently re-run through the LLM while it waits.
"""
from __future__ import annotations

import logging

import psycopg
from psycopg.types.json import Json

from . import edi

log = logging.getLogger("orders.hold")


# --- the deadline ----------------------------------------------------------

def is_past_deadline(delivery_date: str, today: str = "") -> bool:
    """True once there is no more time left to wait for an answer.

    An unparsable/blank delivery date behaves exactly like the code always did before
    holding existed: shipped immediately, never held — there is no date to wait for.
    """
    stamp = edi._format_date(delivery_date)
    if not stamp.strip():
        return True
    now_stamp = edi._format_date(today) if today else ""
    if not now_stamp.strip():
        from datetime import UTC, datetime
        now_stamp = datetime.now(UTC).strftime("%Y%m%d")
    return stamp <= now_stamp


# --- recording a hold --------------------------------------------------------

def _dump_decisions(decisions) -> list[dict]:
    return [{"item_name": d.item_name, "gtin": d.gtin, "card": d.card,
             "confidence": d.confidence, "rule": d.rule, "note": d.note,
             "review": d.review, "trace": d.trace, "quantity": d.quantity,
             "unit": d.unit} for d in decisions]


def _load_decisions(rows: list[dict]):
    from .match import Decision
    return [Decision(item_name=d.get("item_name", ""), gtin=d.get("gtin"),
                     card=d.get("card", ""), confidence=float(d.get("confidence") or 0.0),
                     rule=d.get("rule", ""), note=d.get("note", ""),
                     review=bool(d.get("review")), trace=d.get("trace") or {},
                     quantity=d.get("quantity"), unit=d.get("unit", "ks"))
            for d in rows]


def _apply_confirmed_quantity(conn, decisions: list, qid: int, quantity) -> None:
    """#360: the warehouse confirmed/corrected THIS line's quantity on the board — apply it
    to the matching stored decision(s) so the human-confirmed value is what actually ships.

    Matched by `item_key` on the answered question's wording (the same normalizer
    `teach.ask` keyed the question on), so it survives spacing/typo differences between the
    stored `Decision.item_name` and the question's `wording`. A no-op when nothing matches
    (the extracted quantity then stands, the safe default). Runs on the RAW loaded decisions
    BEFORE `_redecide` — `_redecide` copies `d.quantity` verbatim (it only re-derives the
    card), and `merge_same_card` sums the corrected quantity into any merged card, so the
    correction survives both the redecide and the ship path unchanged.
    """
    from . import memory, teach
    if quantity is None or qid is None:
        return
    q = teach.get(conn, qid)
    key = memory.item_key(q.get("wording", "")) if q else ""
    if not key:
        return
    for d in decisions:
        if memory.item_key(d.item_name) == key:
            d.quantity = quantity


def place(conn, message_id: str, matched, order: dict, decisions, extracted: dict,
          question_ids: list[int]) -> int:
    """Record a held order. Returns its id."""
    row = conn.execute(
        """INSERT INTO held_orders
               (message_id, customer_ean, customer_name, delivery_date, order_number,
                store, recipient_group, question_ids, order_json, extracted_json,
                decisions_json)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           RETURNING id""",
        (message_id, matched.ean_edi, matched.name, order.get("deliveryDate", ""),
         order.get("orderNumber", ""), order.get("store", ""),
         order.get("recipientGroup", ""), list(question_ids or []), Json(order),
         Json({"isChangeRequest": bool(extracted.get("isChangeRequest")),
               "unverified": extracted.get("unverified") or [],
               "notes": extracted.get("notes") or ""}),
         Json(_dump_decisions(decisions)))).fetchone()
    hid = int(row[0])
    log.info("holding order #%s for %s / %s (questions %s)", hid, matched.ean_edi,
             order.get("deliveryDate", ""), question_ids)
    return hid


def has_open(conn, message_id: str) -> bool:
    """True while any order of this message is still waiting — worker._claim's guard.

    #164: a `mail`-kind question ("is this even an order?") never gets a `held_orders`
    row (there is no order to hold — see `pipeline._run`'s "no orders" branch), so this
    now ALSO counts an open `mail`-kind question for the same message. Narrowed to
    `kind='mail'` deliberately: an open `item`/`customer`/`date`/`line` question already
    holding an order is covered by the `held_orders` check above, and widening this to
    EVERY open question kind could let a stray, unrelated open question on an already-
    shipped order's message block re-claiming something it has nothing to do with.
    """
    return conn.execute(
        """SELECT 1 FROM held_orders WHERE message_id = %s AND status = 'held'
           UNION ALL
           SELECT 1 FROM order_questions
            WHERE message_id = %s AND kind = 'mail' AND status = 'open'
           LIMIT 1""",
        (message_id, message_id)).fetchone() is not None


# --- reading ---------------------------------------------------------------

def _row(r) -> dict | None:
    if not r:
        return None
    return {"id": r[0], "message_id": r[1], "customer_ean": r[2], "customer_name": r[3] or "",
            "delivery_date": r[4] or "", "order_number": r[5] or "", "store": r[6] or "",
            "recipient_group": r[7] or "", "question_ids": list(r[8] or []),
            "order": r[9] or {}, "extracted": r[10] or {}, "decisions": r[11] or [],
            "status": r[12], "release_reason": r[13], "created_at": r[14],
            "released_at": r[15]}


_COLS = ("id, message_id, customer_ean, customer_name, delivery_date, order_number, "
         "store, recipient_group, question_ids, order_json, extracted_json, "
         "decisions_json, status, release_reason, created_at, released_at")


def get(conn, held_id: int) -> dict | None:
    return _row(conn.execute(
        f"SELECT {_COLS} FROM held_orders WHERE id = %s", (held_id,)).fetchone())


def list_held(conn, limit: int = 200) -> list[dict]:
    """Every order still waiting, oldest delivery date first — nothing waits invisibly."""
    rows = conn.execute(
        f"""SELECT {_COLS} FROM held_orders WHERE status = 'held'
            ORDER BY delivery_date, created_at LIMIT %s""", (limit,)).fetchall()
    return [d for r in rows if (d := _row(r)) is not None]


# --- releasing ---------------------------------------------------------------

def _current_catalog(conn) -> list[dict]:
    """The REAL catalog snapshot (#162) — `_redecide` needs it so `catalog_name`/
    `alias_exact`/`history_sure` can fire, not just `human_taught`/`global_taught`.
    Empty when no snapshot exists yet (most unit tests) — matches the old hardcoded `[]`
    for that case exactly."""
    from . import snapshot
    sid = snapshot.latest_snapshot_id(conn)
    return snapshot.load_catalog(conn, sid) if sid else []


def _redecide(conn, customer_ean: str, decisions: list, as_of: str = "",
             catalog: list[dict] | None = None, _recalled_cache: dict | None = None) -> list:
    """Give every stored line one more chance against FRESH memory (#93) AND the REAL
    catalog (#162), without another LLM call.

    Originally this ran `decide_without_model` with an EMPTY catalog — safe for the
    pre-#162 item-hold path, where the only rungs that could ever fire post-hold were
    `human_taught`/`global_taught` (every genuinely ambiguous line already had its own
    tracked, now-answered question). That assumption does not hold for a customer-
    unknown hold (#159): no item question was ever raised on the first pass, so the
    ONLY chance a still-unmatched line has to resolve for free is the real catalog —
    `catalog_name`/`alias_exact`/`history_sure` — plus this NOW-known customer's own
    per-customer history (`memory.resolve` below already used the real `customer_ean`,
    even before this change). `catalog` defaults to the CURRENT snapshot
    (`_current_catalog`) — loaded once by the caller when redeciding several
    decisions, or lazily here for direct callers/tests.

    Safe to run over every decision, not only the ones that were pending: an item that
    already resolved before the hold (catalog, model, prior history) asks the same
    no-model question again and gets back the identical answer it already had, since
    `decide_without_model` only returns something MORE certain than what is already
    stored — never something weaker.

    `as_of` (#117) keeps the SAME date fence `pipeline.py`'s first pass already applies: a
    shipment dated after "now" must not decide a release happening now, either. A human
    answer (`memory.resolve`'s `human` rows) is exempt from `as_of` regardless — see
    `memory.resolve`'s own docstring — so this never blocks the very answer that triggered
    the release.

    `_recalled_cache` (#162 review finding) is an internal `{item_name: Recalled|None}`
    memo, keyed by `item_name` (safe: `memory.resolve` depends only on `customer_ean` +
    `item_name` + `as_of`, all fixed for one call) — pass the SAME dict a caller also
    hands `_ask_still_ambiguous` and neither ever re-fetches a wording the other already
    looked up. `None` (the default) means "no caller wants to share the cache" — an
    ordinary local dict is used and simply discarded.
    """
    from . import memory
    from .match import decide_without_model, merge_same_card

    if catalog is None:
        catalog = _current_catalog(conn)
    cache = {} if _recalled_cache is None else _recalled_cache

    changed = False
    out = []
    for d in decisions:
        if d.item_name in cache:
            recalled = cache[d.item_name]
        else:
            recalled = memory.resolve(conn, customer_ean, d.item_name, as_of=as_of)
            cache[d.item_name] = recalled
        global_recalled = memory.resolve_global(conn, d.item_name)
        fresh = decide_without_model(d.item_name, catalog, recalled=recalled,
                                     global_recalled=global_recalled)
        if fresh is not None and (str(fresh.gtin) != str(d.gtin) or fresh.rule != d.rule):
            fresh.quantity, fresh.unit = d.quantity, d.unit
            out.append(fresh)
            changed = True
        else:
            out.append(d)
    return merge_same_card(out) if changed else decisions


def _ask_still_ambiguous(conn, row: dict, decisions: list, still_asking: list,
                         catalog: list[dict], as_of: str,
                         _recalled_cache: dict | None = None) -> tuple[list[int], list[str]]:
    """Raise a fresh warehouse question for every decision STILL in `ASK_THE_WAREHOUSE`
    after a real-catalog `_redecide` (#162) — mirrors `pipeline._run`'s own per-item ask
    loop exactly (same `teach.ask`/`match.candidates`/`match.candidates_for_question`/
    `match.plausible_candidates` machinery, #160), just outside the `_run` per-email
    loop and spending no model call (the first pass already paid for the stored
    `Decision`).

    Returns `(new_question_ids, unaskable_item_names)` — `unaskable` is populated only
    in the near-impossible case `teach.ask` itself refuses (e.g. a blank wording key);
    never silently drop those either — the caller surfaces them, per #162's own
    constraint 4.

    `_recalled_cache`: see `_redecide`'s docstring — pass the SAME dict `_redecide` was
    given for this same release, so a `still_asking` line (which `_redecide` already
    looked up moments earlier) never pays for `memory.resolve` twice.
    """
    from . import match, memory, teach

    cache = {} if _recalled_cache is None else _recalled_cache
    new_qids: list[int] = []
    unaskable: list[str] = []
    for d in still_asking:
        if d.item_name in cache:
            recalled = cache[d.item_name]
        else:
            recalled = memory.resolve(conn, row["customer_ean"], d.item_name, as_of=as_of)
            cache[d.item_name] = recalled
        item_cands = match.candidates(d.item_name, catalog, customer_name=row["customer_name"],
                                      memory_gtin=recalled.gtin if recalled else "")
        ask_cands = match.candidates_for_question(item_cands, catalog, d)
        # #160: mirrors pipeline._run's own shortlist-quality filter exactly — never pad
        # to a fixed count with a weakly-related card.
        shown_cands = match.plausible_candidates(ask_cands)
        qid = teach.ask(
            conn, message_id=row["message_id"], customer_ean=row["customer_ean"],
            customer_name=row["customer_name"], wording=d.item_name, quantity=d.quantity,
            unit=d.unit,
            candidates=[{"gtin": str(c.get("gtin")), "name": c.get("name", "")}
                       for c in shown_cands],
            delivery_date=row["delivery_date"], reason=d.note)
        if qid:
            new_qids.append(qid)
        else:
            unaskable.append(d.item_name)
    return new_qids, unaskable


def _post_still_held(cfg, post, row: dict, decisions: list, new_qids: list[int],
                     unaskable: list[str]) -> None:
    """The Odoo visibility for a #162 second hold — the warehouse must see that this
    order is STILL waiting, now on a fresh item question, not silently vanish from view
    between the customer answer and whatever eventually resolves it."""
    from . import report
    post = post or (lambda c, html, **kw: report.post_from_config(c, html))
    reason = (f"Zákazník doplnený — {len(unaskable)} položku sa nedalo jednoznačne "
             f"priradiť ani spýtať skladu: {', '.join(unaskable)}") if unaskable else ""
    html = report.build_summary(customer_name=row["customer_name"], orders=[{
        "delivery_date": row["delivery_date"], "status": "held",
        "item_count": len(decisions), "missing_count": 0, "reject_reason": reason}],
        new_questions=len(new_qids), link=report.sklad_link(cfg), cfg=cfg)
    try:
        post(cfg, html)
    except Exception:
        log.exception("posting the re-held order summary failed (held order #%s)", row["id"])


def _ship(conn, cfg, row: dict, upload, post, redecide: bool,
         as_of: str = "") -> tuple[str, dict, str]:
    """Re-decide (if asked) against fresh memory and ship, via the SAME `_ship_one` /
    `edi.claim_send` ledger the live pipeline already ships through. Returns (status,
    preview, reject_reason) — never touches `held_orders.status`; callers decide what a
    returned status means for the row."""
    from . import customer as customer_mod
    from . import report
    from . import upload as upload_mod
    from .pipeline import _ship_one  # lazy: pipeline imports this module at its own top

    upload = upload or (lambda c, name, content: upload_mod.put(c, name, content))
    post = post or (lambda c, html, **kw: report.post_from_config(c, html))

    matched = customer_mod.Matched(ean_edi=row["customer_ean"], name=row["customer_name"],
                                   confidence=1.0, rule="held_release", note="")
    decisions = _load_decisions(row["decisions"])
    if redecide:
        # #162 review finding: every CURRENT call site now passes `redecide=False` —
        # `_release_locked` redecides itself (needs the decided rules BEFORE deciding
        # whether to ship or ask again, which this flag alone cannot express) and
        # `_do_release`/`release_due` never redecides at all (the deadline sweep ships
        # the ORIGINALLY stored decisions, unchanged, by design). Kept as a real,
        # working parameter rather than removed — a future direct caller (or a test
        # proving `_ship` redecides in isolation) can still ask for it.
        decisions = _redecide(conn, row["customer_ean"], decisions, as_of=as_of)

    return _ship_one(conn, cfg, {"message_id": row["message_id"]}, row["order"], matched,
                     decisions, row["extracted"], False, upload, post)


def _mark_message_done_if_clear(conn, message_id: str) -> None:
    """Every order this message produced has now shipped, been reviewed, or been released —
    the message is finally done (#93: it stayed unprocessed while it held)."""
    if not has_open(conn, message_id):
        conn.execute(
            """UPDATE messages
                  SET processed = true, processed_at = now(), processed_by = 'ai_orders',
                      processing_at = NULL
                WHERE message_id = %s""", (message_id,))


def release_to_review(conn, cfg, row: dict, post, reason: str) -> dict:
    """Convert a held order into the SAME visible 'review' outcome any other stuck order
    already gets, instead of shipping with a blank/unconfirmed field or leaving it stuck
    'held' forever with no path forward (#159's original "no real customer" case,
    generalized by #164 to ANY kind of still-open, deadline-non-shippable question —
    `release_due`'s new rule below routes through here for date/customer/mail/line
    questions the same way it already did for an unresolved customer)."""
    from . import report
    post = post or (lambda c, html, **kw: report.post_from_config(c, html))
    html = report.build_summary(customer_name=row.get("customer_name") or "", orders=[{
        "delivery_date": row["delivery_date"], "status": "review",
        "item_count": len(row["decisions"]), "missing_count": 0,
        "reject_reason": reason}], cfg=cfg)
    try:
        post(cfg, html)
    except Exception:
        log.exception("posting the unknown-customer review summary failed")
    report.log_event(conn, row["message_id"], stage="review", status="review",
                     outcome=reason, detail={"held_id": row["id"]})
    conn.execute(
        """UPDATE held_orders SET status='released', release_reason='answered',
               released_at=now() WHERE id=%s""", (row["id"],))
    _mark_message_done_if_clear(conn, row["message_id"])
    return {"id": row["id"], "status": "review"}


def _do_release(conn, cfg, row: dict, release_reason: str, upload, post,
                redecide: bool, as_of: str = "") -> dict:
    """The deadline-sweep shape: ship, and ONLY on success mark the row released. Used by
    `release_due` (a single periodic sweep — no concurrent-release race to guard against)
    and directly by tests proving the ledger is the real duplicate-upload backstop.
    `release_for_question` uses `_release_locked` below instead (#118): it needs to
    SERIALIZE this same decision per held-order id, which `_do_release` alone cannot do.

    #159 review finding: a row placed while the CUSTOMER was still unknown starts with
    `customer_ean=""` and stays that way until a REAL answer calls `hold.set_customer` —
    `_ship`'s `Matched(ean_edi=row["customer_ean"], ...)` is a dataclass instance and is
    therefore ALWAYS truthy, so `_ship_one`'s `if not matched:` guard never caught this;
    left unguarded, the deadline sweep would ship a document with a blank customer EAN
    addressed to nobody. A still-empty `customer_ean` here means nobody ever answered
    the "who is this?" question before the delivery date — convert to 'review' instead.
    """
    if not row["customer_ean"]:
        return release_to_review(
            conn, cfg, row, post,
            "Zákazník nebol nájdený v tabuľke zákazníkov (do termínu dodania)")
    status, preview, _reason = _ship(conn, cfg, row, upload, post, redecide, as_of=as_of)
    if status == "error":
        # The upload itself failed (e.g. ORION unreachable) — `_ship_one` already released
        # the ledger claim, so this is genuinely retryable. Leave the row 'held': the next
        # deadline sweep (or another answer, if a sibling question is still open) simply
        # tries again, instead of this being permanently lost as a false 'released'.
        log.warning("release of held order #%s (%s) for %s / %s did not ship — staying held",
                    row["id"], release_reason, row["customer_ean"], row["delivery_date"])
        return {"id": row["id"], "status": status, "preview": preview}
    conn.execute(
        """UPDATE held_orders SET status = 'released', release_reason = %s, released_at = now()
            WHERE id = %s""", (release_reason, row["id"]))
    log.info("released held order #%s (%s) for %s / %s -> %s", row["id"], release_reason,
             row["customer_ean"], row["delivery_date"], status)
    _mark_message_done_if_clear(conn, row["message_id"])
    return {"id": row["id"], "status": status, "preview": preview}


def release_for_question(conn, cfg, qid: int, upload=None, post=None,
                         confirmed_quantity=None) -> list[dict]:
    """Release every held order whose LAST open question was just answered.

    `confirmed_quantity` (#360): the warehouse-confirmed quantity for THIS answered line,
    applied to the matching held decision before it ships (see `_apply_confirmed_quantity`).
    `None` (every pre-#360 caller) keeps the ORIGINAL extracted quantity, unchanged.

    A held order may be waiting on more than one wording; it ships only once every one of
    its question ids is answered. Releasing on the first answer would ship a still-guessed
    line the same way the immediate-partial-ship bug did (#81.1) — so a sibling still-open
    question keeps the whole order held.

    #118: two near-simultaneous answers to SIBLING questions of the same held order can
    each independently observe "every question answered" under READ COMMITTED and both
    dispatch a ship. `_release_locked` serializes the whole check-then-ship-then-mark
    decision per held-order id on its own row lock.
    """
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM held_orders WHERE %s = ANY(question_ids) AND status = 'held'",
        (qid,)).fetchall()]
    released = []
    for hid in ids:
        result = _release_locked(conn, cfg, hid, upload, post,
                                 answered_qid=qid, confirmed_quantity=confirmed_quantity)
        if result is not None:
            released.append(result)
    return released


def _release_locked(conn, cfg, hid: int, upload, post,
                    answered_qid: int | None = None, confirmed_quantity=None) -> dict | None:
    """One held order's answered-release decision, serialized per-id (#118).

    A short, SEPARATE transaction (its own connection, never `conn`) locks the
    `held_orders` row `FOR UPDATE` for the WHOLE check-then-ship-then-mark decision — a
    lock held only around the remaining-count check, then released before shipping, would
    be provably useless: a second caller unblocked after the first released would just
    re-read the identical unlocked state and reach the identical "release" decision, since
    nothing durable was written under the lock. So the lock spans the decision through the
    final `status = 'released'` write, and the SECOND of two racing sibling answers simply
    blocks until the first fully finishes (ships and commits, or fails and rolls back to
    'held') — never a torn double-ship.

    The actual upload/ledger-claim (`_ship_one` via `edi.claim_send` + `upload()`) keeps
    running on `conn` — the caller's own, pre-existing (autocommit, in production)
    connection — completely unaffected by this lock transaction's commit or rollback. That
    preserves the #116 invariant: an already-physically-uploaded document is never undone
    by a later, unrelated failure. It also means `held_orders.status` only ever flips to
    'released' AFTER `_ship_one` has fully, successfully RETURNED — never pre-emptively
    claimed before the ship result is known — matching the existing, deliberate guarantee
    `tests/test_api.py::test_answering_over_http_commits_the_ledger_even_if_something_
    after_upload_fails` pins (an exception thrown AFTER a successful upload must leave the
    row 'held' so a retry can happen).

    #162: when a still-ambiguous line needs a fresh question, `teach.ask`'s `INSERT`
    below runs on `conn` (autocommit — durable the instant it is written), and the
    `held_orders.question_ids`/`decisions_json` write that RECORDS it runs on `tx`
    (durable only once this whole `with tx:` block commits) — the SAME `conn`-durable-
    now vs `tx`-durable-at-block-end split this docstring already describes for the ship
    path above. A crash between the two would leave an orphan `order_questions` row not
    yet referenced by any `held_orders.question_ids` — harmless and self-healing:
    `teach.ask`'s own `ON CONFLICT (customer_ean, item_key) WHERE status = 'open' DO
    NOTHING` + existing-id fallback means the NEXT release attempt for this row re-asks
    the same wording and gets back the SAME question id, never a duplicate.
    """
    with psycopg.connect(cfg.pg_dsn) as tx:
        locked = tx.execute(
            "SELECT question_ids, status FROM held_orders WHERE id = %s FOR UPDATE",
            (hid,)).fetchone()
        if not locked or locked[1] != "held":
            return None  # already released by a sibling answer that won the race, or gone
        remaining_row = tx.execute(
            "SELECT count(*) FROM order_questions WHERE id = ANY(%s) AND status <> 'answered'",
            (locked[0],)).fetchone()
        remaining = remaining_row[0] if remaining_row else 0
        if remaining:
            return None
        row = get(conn, hid)
        if not row:
            return None
        as_of = str(_db_today(conn))
        catalog = _current_catalog(conn)
        # Shared between `_redecide` and `_ask_still_ambiguous` below (#162 review
        # finding) — a `still_asking` line's `memory.resolve` was already looked up by
        # `_redecide` a few lines earlier; the SAME dict lets `_ask_still_ambiguous`
        # reuse it instead of paying for the identical lookup twice.
        recalled_cache: dict = {}
        loaded = _load_decisions(row["decisions"])
        # #360: apply the warehouse-confirmed quantity for the answered line BEFORE redecide,
        # so the human-corrected value flows through both the ship path and any re-hold.
        _apply_confirmed_quantity(conn, loaded, answered_qid, confirmed_quantity)
        decisions = _redecide(conn, row["customer_ean"], loaded,
                              as_of=as_of, catalog=catalog, _recalled_cache=recalled_cache)

        # #162: a customer-unknown hold never got a chance to ask about its ambiguous
        # items on the first pass (the whole point of ASK_THE_WAREHOUSE gating on
        # `matched` in pipeline.py). Redeciding against the real catalog + this
        # now-known customer's memory just above may resolve some of them for free —
        # anything STILL in ASK_THE_WAREHOUSE must be asked about now, never shipped
        # with the line silently dropped (the already-known-customer item-hold path
        # never reaches this branch: every line it could ask about already has its own
        # tracked, now-answered question, so `decide_without_model` resolves it via
        # `human_taught` above and its rule is no longer in ASK_THE_WAREHOUSE).
        from . import report
        from .pipeline import ASK_THE_WAREHOUSE
        still_asking = [d for d in decisions if d.rule in ASK_THE_WAREHOUSE]
        if still_asking:
            new_qids, unaskable = _ask_still_ambiguous(conn, row, decisions, still_asking,
                                                        catalog, as_of,
                                                        _recalled_cache=recalled_cache)
            all_qids = list(dict.fromkeys(list(locked[0] or []) + new_qids))
            tx.execute(
                """UPDATE held_orders SET question_ids = %s, decisions_json = %s
                    WHERE id = %s""", (all_qids, Json(_dump_decisions(decisions)), hid))
            log.info(
                "held order #%s stays held: %d line(s) still ambiguous after the customer "
                "resolved — %d fresh question(s) raised (%s), %d could not even be asked "
                "about (%s)", hid, len(still_asking), len(new_qids), new_qids,
                len(unaskable), unaskable)
            report.log_event(
                conn, row["message_id"], stage="held", status="held",
                outcome=f"Zákazník doplnený, objednávka opäť čaká na sklad "
                        f"({len(new_qids)} nových otázok) — dodanie "
                        f"{row['delivery_date'] or '(bez dátumu)'}",
                detail={"held_id": hid, "question_ids": new_qids,
                        "unaskable_items": unaskable, "delivery_date": row["delivery_date"]})
            _post_still_held(cfg, post, row, decisions, new_qids, unaskable)
            return {"id": hid, "status": "held", "preview": {}}

        row = dict(row, decisions=_dump_decisions(decisions))
        status, preview, _reason = _ship(conn, cfg, row, upload, post, redecide=False,
                                         as_of=as_of)
        if status == "error":
            log.warning(
                "release of held order #%s (answered) for %s / %s did not ship — staying "
                "held", hid, row["customer_ean"], row["delivery_date"])
            return {"id": hid, "status": status, "preview": preview}
        tx.execute(
            """UPDATE held_orders SET status = 'released', release_reason = 'answered',
                   released_at = now() WHERE id = %s""", (hid,))
    log.info("released held order #%s (answered) for %s / %s -> %s", hid,
             row["customer_ean"], row["delivery_date"], status)
    _mark_message_done_if_clear(conn, row["message_id"])
    return {"id": hid, "status": status, "preview": preview}


def set_customer(conn, qid: int, ean_edi: str, name: str) -> None:
    """The unmatched-customer question (#159) is now answered with a REAL pick — tell
    every held order still waiting on it who it actually belongs to, BEFORE releasing.
    `release_for_question`/`_ship` build the `Matched` object straight from
    `held_orders.customer_ean`/`customer_name`, so this must land first — a held order
    placed while the customer was unknown always started with `customer_ean=''`."""
    conn.execute(
        "UPDATE held_orders SET customer_ean=%s, customer_name=%s "
        "WHERE %s = ANY(question_ids) AND status='held'", (ean_edi, name, qid))


def set_delivery_date(conn, qid: int, date: str) -> None:
    """The 'ktorý deň platí?' question (#164) is now answered with a REAL date — tell
    every held order still waiting on it, the SAME way `set_customer` does for a resolved
    customer, BEFORE releasing. Two places need the answered date: `held_orders.
    delivery_date` (the column `release_due`'s own deadline fence reads) AND
    `order_json.deliveryDate` (what `_ship`/`_redecide`/`edi.build` actually read when
    the order eventually ships) — a held order placed while the date was in DISPUTE
    always started with whatever ONE candidate date `pipeline._run` happened to store."""
    conn.execute(
        """UPDATE held_orders
              SET delivery_date = %s,
                  order_json = jsonb_set(order_json, '{deliveryDate}', to_jsonb(%s::text))
            WHERE %s = ANY(question_ids) AND status = 'held'""",
        (date, date, qid))


def release_unknown_customer(conn, cfg, qid: int, post=None) -> list[dict]:
    """Release every held order waiting on a customer question the warehouse answered
    "neviem, kto to je" (#159). Nobody could ship an order with no customer to address it
    to, so this does NOT ship — it converts the order into the SAME 'review' outcome every
    other stuck order already gets (report.build_summary's dashboard hint, an
    email_events row, the message marked processed), instead of leaving it silently stuck
    'held' forever with no path forward. Shares `release_to_review` with `_do_release`'s
    own deadline-guard for the identical "still no real customer" outcome."""
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM held_orders WHERE %s = ANY(question_ids) AND status = 'held'",
        (qid,)).fetchall()]
    reason = "Zákazník nebol nájdený v tabuľke zákazníkov"
    released = []
    for hid in ids:
        row = get(conn, hid)
        if not row:
            continue
        released.append(release_to_review(conn, cfg, row, post, reason))
    return released


def retry_unknown_customer_questions(conn, cfg, upload=None, post=None) -> list[dict]:
    """A customer added on /znalosti (rather than answered straight on the question card,
    #234 §3) must also unstick any order still waiting for it. For every OPEN `customer`
    question, re-resolve the sender against the CURRENT customer list and act ONLY when
    the match is a genuine exact-address hit with a real EAN — never the model rung (no
    store header, no model call reaches this far, so `resolve()`'s `llm` rung never fires)
    and never a fuzzy name/branch hit (`store=""` makes `_by_store` always return `None`):
    a wrongly addressed order is worse than one still waiting (`customer.py`'s own module
    docstring). Called from `/api/znalosti/clients` right after a save, and from the
    order worker's own periodic tick (mirrors `release_due`'s own sweep)."""
    from . import customer as customer_mod
    from . import report, teach
    from . import snapshot as snapshot_mod

    rows = conn.execute(
        "SELECT id FROM order_questions WHERE kind = 'customer' AND status = 'open'"
    ).fetchall()
    released: list[dict] = []
    for (qid,) in rows:
        q = teach.get(conn, qid)
        if not q:
            continue
        ctx = q.get("context") or {}
        sender_email = str(ctx.get("sender_email") or "")
        if not sender_email:
            continue
        customers = snapshot_mod.customers_for_management(conn)
        matched = customer_mod.resolve(customers, sender_email, ctx.get("sender_name", ""),
                                       ctx.get("company_name", ""))
        if not (matched and matched.rule == "exact_email" and matched.ean_edi):
            continue
        teach.add_candidate(conn, qid, {
            "ean_edi": matched.ean_edi, "name": matched.name, "city": "", "street": "",
            "address_match": False, "source": "auto"})
        try:
            teach.answer_customer(conn, qid, ean_edi=matched.ean_edi, name=matched.name,
                                  by="auto")
        except (teach.AlreadyAnswered, teach.NotACandidate):
            continue
        report.log_event(
            conn, q["message_id"], stage="review", status="ok",
            outcome=f"Automaticky doplnený zákazník {matched.name} ({matched.ean_edi})",
            detail={"question_id": qid, "ean_edi": matched.ean_edi}, rollup=False)
        set_customer(conn, qid, matched.ean_edi, matched.name)
        released.extend(release_for_question(conn, cfg, qid, upload=upload, post=post))
    return released


def _has_non_shippable_open_question(conn, question_ids: list[int]) -> bool:
    """#164: does this held order still wait on a question whose KIND is not safe to
    silently ship past the deadline? `item` questions keep today's behaviour (ship what
    matched, drop what didn't — `deadline_shippable=True`); `customer`/`date`/`mail`/
    `line` are NOT — shipping past their deadline unanswered would send an unconfirmed
    customer, an invented date, or a fabricated line, exactly the "never ship a guessed
    value" constraint this whole ticket exists to close."""
    if not question_ids:
        return False
    from . import teach
    rows = conn.execute(
        "SELECT DISTINCT kind FROM order_questions WHERE id = ANY(%s) AND status <> 'answered'",
        (list(question_ids),)).fetchall()
    return any(not teach.KINDS[k or "item"].deadline_shippable for (k,) in rows)


def release_due(conn, cfg, upload=None, post=None, today: str = "") -> list[dict]:
    """The deadline backstop: whatever is still waiting when its delivery date arrives
    ships what matched — exactly like the pipeline always has, just no longer immediate.

    #164: a row still waiting on a NON-`deadline_shippable` question (customer/date/mail/
    line) is the one exception — it goes to `release_to_review` instead, because shipping
    it would mean sending an unconfirmed customer/date/line the warehouse never actually
    confirmed. `item`-only holds are completely unaffected (`deadline_shippable=True`
    keeps the exact pre-#164 ship-what-matched behaviour).
    """
    today = today or str(_db_today(conn))
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM held_orders WHERE status = 'held'").fetchall()]
    released = []
    for hid in ids:
        row = get(conn, hid)
        if not row or not is_past_deadline(row["delivery_date"], today):
            continue
        if _has_non_shippable_open_question(conn, row["question_ids"]):
            released.append(release_to_review(
                conn, cfg, row, post,
                "Termín dodania prišiel, ale otázka (dátum/zákazník/mail/riadok) ešte "
                "nie je zodpovedaná — treba doriešiť ručne"))
            continue
        released.append(_do_release(conn, cfg, row, "deadline", upload, post, redecide=False,
                                    as_of=today))
    return released


def _db_today(conn):
    return conn.execute("SELECT current_date").fetchone()[0]
