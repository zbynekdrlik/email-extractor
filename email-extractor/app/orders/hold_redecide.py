"""Re-decision + ship helpers for a held order (#424 split of hold.py) — a fresh
`match.decide_without_model`, the still-ambiguous re-ask, and the funnel through
`pipeline._ship_one`. Re-exported by the `hold` facade."""
from __future__ import annotations

import logging

from .hold_place import _load_decisions, get, has_open  # noqa: F401 (used below)

log = logging.getLogger("orders.hold")

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
