"""Release / expire / manual-resolve / customer+date / deadline-sweep paths for
held orders (#424 split of hold.py) — every way a held order leaves the queue.
Re-exported by the `hold` facade."""
from __future__ import annotations

import logging
from html import escape

import psycopg
from psycopg.types.json import Json

from .hold_place import (  # noqa: F401 (used below)
    _apply_confirmed_quantities,
    _db_today,
    _dump_decisions,
    _load_decisions,
    get,
    is_past_deadline,
)
from .hold_redecide import (  # noqa: F401 (used below)
    _ask_still_ambiguous,
    _current_catalog,
    _mark_message_done_if_clear,
    _post_still_held,
    _redecide,
    _ship,
)

log = logging.getLogger("orders.hold")

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


def _do_release_locked(conn, cfg, hid: int, upload, post, as_of: str = "") -> dict | None:
    """#384: the deadline sweep's per-row release, SERIALIZED the SAME way `_release_locked`
    serializes the answered path — a per-row `FOR UPDATE` lock held across ship + the guarded
    final flip. Plain `_do_release` (kept unchanged, so its ledger-backstop test still
    exercises the ledger) takes NO lock: it relied on `edi.claim_send`'s content-hash ledger
    to stop a duplicate ORION upload if the sweep and the answered path raced. A „Vyriešené
    ručne" (#384) release ships NOTHING and takes NO ledger claim, so an UNSERIALIZED sweep
    could upload an EDI for an order the warehouse already resolved by hand → a duplicate
    physical delivery the ledger cannot catch. This closes that window: whichever of the
    sweep / manual-resolve / answered-release wins the row lock first, the others re-read
    `status != 'held'` and skip. The customer-unknown → review branch runs BEFORE the lock
    (it ships nothing, so it is outside the duplicate-upload risk — same as `_do_release`)."""
    row = get(conn, hid)
    if not row or row["status"] != "held":
        return None
    if not row["customer_ean"]:
        return release_to_review(
            conn, cfg, row, post,
            "Zákazník nebol nájdený v tabuľke zákazníkov (do termínu dodania)")
    with psycopg.connect(cfg.pg_dsn) as tx:
        locked = tx.execute(
            "SELECT status FROM held_orders WHERE id = %s FOR UPDATE", (hid,)).fetchone()
        if not locked or locked[0] != "held":
            return None  # a manual / answered release won the row while we waited on the lock
        status, preview, _reason = _ship(conn, cfg, row, upload, post, redecide=False,
                                         as_of=as_of)
        if status == "error":
            log.warning("release of held order #%s (deadline) for %s / %s did not ship — "
                        "staying held", hid, row["customer_ean"], row["delivery_date"])
            return {"id": hid, "status": status, "preview": preview}
        tx.execute(
            """UPDATE held_orders SET status = 'released', release_reason = 'deadline',
                   released_at = now() WHERE id = %s AND status = 'held'""", (hid,))
    log.info("released held order #%s (deadline) for %s / %s -> %s", hid,
             row["customer_ean"], row["delivery_date"], status)
    _mark_message_done_if_clear(conn, row["message_id"])
    return {"id": hid, "status": status, "preview": preview}


def release_for_question(conn, cfg, qid: int, upload=None, post=None) -> list[dict]:
    """Release every held order whose LAST open question was just answered.

    #360: at ship time the human-confirmed quantity of EVERY answered `item` question of the
    order (persisted on `order_questions.quantity` by `teach.answer`) is applied to its
    decision, so a correction on any question ships — see `_apply_confirmed_quantities`.

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
        result = _release_locked(conn, cfg, hid, upload, post)
        if result is not None:
            released.append(result)
    return released


def _release_locked(conn, cfg, hid: int, upload, post) -> dict | None:
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
        # #360: apply every answered item-question's confirmed quantity (from
        # order_questions.quantity) BEFORE redecide, so a correction on ANY of the order's
        # questions — not just the last one answered — flows through the ship path and any
        # re-hold. locked[0] is the held order's question_ids (all answered here: remaining==0).
        _apply_confirmed_quantities(conn, loaded, locked[0])
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


def resolve_manually(conn, cfg, qid: int, post=None) -> list[dict]:
    """#384 „Vyriešené ručne": the warehouse entered this order into CODEX BY HAND, so every
    held order of this question's message waiting on `qid` is released WITHOUT any claim or
    ORION upload — nothing must reach ORION (a duplicate upload = a duplicate physical
    delivery). Each is marked released with `release_reason='manual'`, a `manual` rollup
    event (→ `messages.proc_status='manual'`), and its message closed if nothing else holds.

    The caller (`api_orders_answer`'s manual branch) has already answered the QUESTION with
    the `manual` sentinel AND refused the action if `qid` maps to held rows of more than one
    message (Fable finding 1: an order question dedupes across messages via
    `ON CONFLICT (customer_ean, item_key) WHERE status='open'`, so a bare per-qid release
    could free a FOREIGN mail's order that was never hand-entered → permanently lost). So by
    here every still-held order on `qid` belongs to the ONE message the board card showed;
    release them all together (several delivery days of one mail close as one, per #384)."""
    from . import report
    post = post or (lambda c, html, **kw: report.post_from_config(c, html))
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM held_orders WHERE %s = ANY(question_ids) AND status = 'held'",
        (qid,)).fetchall()]
    resolved = []
    for hid in ids:
        result = _resolve_one_manually(conn, cfg, hid, post)
        if result:
            resolved.append(result)
    return resolved


def _resolve_one_manually(conn, cfg, hid: int, post) -> dict | None:
    """One held order's manual-resolution, serialized per-id against the normal
    answered-release (#118) — the SAME `held_orders` row `FOR UPDATE` lock `_release_locked`
    takes, so a concurrent real `release_for_question` upload and this no-upload close can
    never both act on the same row (whichever wins the lock, the other sees `status != 'held'`
    and skips → never a torn double-ship). The guarded flip to 'released'/'manual' runs FIRST,
    inside the lock (Fable finding 3): only AFTER it commits do the Odoo post + rollup event +
    message-done run, so a crash after the post can never leave the row 'held' for a later
    sibling answer to ship an order the warehouse already handled by hand."""
    from . import report
    with psycopg.connect(cfg.pg_dsn) as tx:
        locked = tx.execute(
            "SELECT status FROM held_orders WHERE id = %s FOR UPDATE", (hid,)).fetchone()
        if not locked or locked[0] != "held":
            return None  # already released by a racing normal answer, or gone
        row = get(conn, hid)
        if not row:
            return None
        tx.execute(
            """UPDATE held_orders SET status = 'released', release_reason = 'manual',
                   released_at = now() WHERE id = %s AND status = 'held'""", (hid,))
    # The row is now safely 'released' — no later sibling answer can ship it. Only NOW the
    # cosmetic Odoo post + the rollup event + the message-done marker (Fable finding 3).
    html = report.build_summary(customer_name=row.get("customer_name") or "", orders=[{
        "delivery_date": row["delivery_date"], "status": "manual",
        "item_count": len(row["decisions"]), "missing_count": 0,
        "reject_reason": "Objednávku zadal sklad ručne do CODEXu — nič sa neposiela "
                         "do ORIONu."}], cfg=cfg)
    try:
        post(cfg, html)
    except Exception:
        log.exception("posting the manual-resolution summary failed (held order #%s)", hid)
    report.log_event(conn, row["message_id"], stage="manual", status="manual",
                     outcome="Vyriešené ručne skladom — objednávka zadaná do CODEXu, "
                             "nič sa neposiela do ORIONu",
                     detail={"held_id": hid})
    _mark_message_done_if_clear(conn, row["message_id"])
    log.info("held order #%s resolved manually (nothing shipped) for %s / %s", hid,
             row["customer_ean"], row["delivery_date"])
    return {"id": hid, "status": "manual"}


def unresolve_manually(conn, qid: int) -> list[str]:
    """#384 undo: a manual resolution was a mis-click — put every held order it released
    back to 'held' so the board card reappears, and reset its message. Flip `held_orders`
    to 'held' FIRST, THEN reset the message (Fable finding 5): the reverse would briefly
    leave the message `processed=false` with NO 'held' row, and `worker._claim`'s
    `WHERE processed=false` would re-claim → reprocess → possibly re-upload a duplicate of
    the hand-entered order. Nothing was ever shipped, so this restores the exact
    pre-manual-resolve state; the deadline sweep then resumes only what it always would have
    done for that held order (Fable finding 6 — `release_reason='manual'` never shipped, so
    there is no NEW ship risk beyond the pre-existing past-deadline item-hold behaviour).
    Returns the affected message ids. The caller reopens the QUESTION itself."""
    rows = conn.execute(
        """UPDATE held_orders SET status = 'held', release_reason = NULL, released_at = NULL
            WHERE %s = ANY(question_ids) AND status = 'released' AND release_reason = 'manual'
            RETURNING message_id""", (qid,)).fetchall()
    message_ids = list({r[0] for r in rows})
    for mid in message_ids:
        conn.execute(
            """UPDATE messages SET processed = false, processed_at = NULL,
                   processed_by = NULL, processing_at = NULL WHERE message_id = %s""",
            (mid,))
    return message_ids


def close_expired_holds(conn, cfg, post=None) -> list[dict]:
    """#421: a held order whose gating board question(s) expired (#341) must not stay
    `held` forever. Scan for any still-`held` row whose gating questions are ALL terminal
    (none `status='open'`) AND at least one is `expired`, and close it TERMINALLY WITHOUT
    any ship (`status='released', release_reason='expired'`) — mirroring the expiry's own
    manual-review routing.

    Deliberately NOT the deadline `release_due` path (ship-what-matched): an expired
    question means the warehouse never confirmed that line within its working-day window,
    so the order is entered by hand in CODEX (the #365 "never partial-ship an unconfirmed
    line" doctrine). Closing without a ship touches no ORION and cannot duplicate a
    physical delivery — the safe direction.

    STATE-based (not keyed on one sweep's expired ids) so BOTH shapes are caught: the whole
    hold expiring at once, AND the staggered dedup case — an OLDER deduped sibling question
    expired in an earlier sweep while a NEWER sibling stayed open, then that newer sibling
    was answered. `_release_locked` counts an `expired` sibling as still-pending and leaves
    such a hold `held`, so without this scan it would fall to the deadline ship-what-matched
    path. Because the scan requires ≥1 expired question, a normal all-`answered` hold
    (release pending / mid-ship via `release_for_question`) is never touched — no race with
    a real ship, and `close_expired_holds` never touches ORION regardless.

    Only a hold with NO remaining open question is closed; one still gated by a live
    question is left `held`. The guarded `status='held'` flip makes a row already released
    by any other path (deadline/answered/manual) a silent no-op — never re-shipped, never
    relabelled, so a later tick never re-alerts. Raises one ops alert per closed hold.
    Returns one dict per closed hold ({"id", "message_id"})."""
    from . import dl_alerts, report
    rows = conn.execute(
        """SELECT id, message_id FROM held_orders h
            WHERE h.status = 'held'
              AND NOT EXISTS (SELECT 1 FROM order_questions q
                               WHERE q.id = ANY(h.question_ids) AND q.status = 'open')
              AND EXISTS (SELECT 1 FROM order_questions q
                           WHERE q.id = ANY(h.question_ids) AND q.status = 'expired')"""
    ).fetchall()
    if not rows:
        return []
    channel = int(getattr(cfg, "orders_channel_id", 0) or 0)
    link = report.sklad_link(cfg)
    closed: list[dict] = []
    for hid, mid in rows:
        flipped = conn.execute(
            """UPDATE held_orders SET status = 'released', release_reason = 'expired',
                   released_at = now() WHERE id = %s AND status = 'held' RETURNING id""",
            (hid,)).fetchone()
        if not flipped:
            continue   # a concurrent path released it first — never re-touch
        report.log_event(conn, mid, stage="review", status="review",
                         outcome="Otázka na nástenke expirovala — držaná objednávka sa "
                                 "zavrela bez odoslania do ORIONu; vybav ju ručne v CODEXe.",
                         detail={"held_id": hid}, rollup=True)
        _mark_message_done_if_clear(conn, mid)
        body = ("<p>&#9888; Držaná objednávka sa zavrela, lebo otázka na nástenke "
                "expirovala — nič sa neposlalo do ORIONu, vybav ju ručne v CODEXe. "
                f"{escape(link)}</p>")
        dl_alerts.enqueue(conn, channel, "held_order_expired", body, message_id=mid)
        closed.append({"id": hid, "message_id": mid})
        log.info("closed held order #%s as expired (nothing shipped) — message %s", hid, mid)
    return closed


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
        # #384: the LOCK-SAFE deadline release — serialized against „Vyriešené ručne" and the
        # answered path so the sweep can never upload an order already resolved (a hand-entry
        # duplicate the edi_sent ledger cannot catch). `None` = the row was released by one of
        # those paths while we held nothing yet — skip it.
        result = _do_release_locked(conn, cfg, hid, upload, post, as_of=today)
        if result:
            released.append(result)
    return released
