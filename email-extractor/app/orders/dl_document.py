"""DL worker — the per-document pipeline (EDI build + safe ORION upload)."""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from . import (
    desadv,
    desadv_edi,
    dl_alerts,
    dl_match,
    dl_memory,
    dl_nonwarehouse,
    dl_report,
    dl_snapshot,
    report,
    teach,
)
from . import upload as upload_mod
from .dl_events import _event, _post
from .dl_matching import _match_item, _match_supplier
from .dl_retry import _check_landed, _check_retry, _is_transient

log = logging.getLogger("orders.dl_worker")

def _num(value) -> float:
    try:
        f = float(value)
        return f if f == f else 0.0   # filters NaN
    except (TypeError, ValueError):
        return 0.0


def _shipped_items(decisions: list[tuple[dict, dl_match.Decision]]) -> list[dict]:
    """The gtin/quantity of every item that actually ends up ON THE EDI — same filter
    `desadv_edi.build()` itself applies (real gtin via `desadv_edi._is_unmatched`,
    non-zero quantity — review finding, #205: routed through the SAME shared predicate
    `generate()`/`build()` already share per #203, rather than a third ad-hoc `gtin`
    truthy check that could silently diverge from it). Exposed on `_process_document`'s
    returned dict (#205, DL migration F6) so a caller — the eval harness's own
    per-document scoring, or a future debugging/digest read — never has to re-derive it
    from the aggregate `all_items` list, which carries no per-document tag and cannot be
    split back apart for a multi-document message."""
    return [{"gtin": d.gtin, "quantity": item.get("quantity")}
           for item, d in decisions
           if not desadv_edi._is_unmatched(d.gtin) and _num(item.get("quantity")) != 0]


# --- #314: non-warehouse supplier short-circuit helpers ---------------------

def _document_has_catalog_match(client, message: dict, doc: dict,
                                catalog: list[dict]) -> bool:
    """#314 GTIN-match SAFETY OVERRIDE (req 4): does this document carry AT LEAST ONE item
    that matches a catalog GTIN? Uses the SAME item matcher (`_match_item` +
    `desadv_edi._is_unmatched`) as normal processing — never a cheap divergent pre-scan —
    so a remembered non-warehouse supplier that DOES send a real warehouse delivery is
    never silently dropped (live evidence: EKVIA/Messer, both marked not_warehouse, also
    ship real catalog goods). Short-circuits on the first match. A transient matcher error
    re-raises via `_check_retry` (the message retries later); a NON-transient (or budget-
    exhausted) matcher error FAILS SAFE — returns True (keep the mail = the pre-#314 ask/
    review path), never False (adversarial-review finding, #314): treating a model outage
    as 'no catalog match' is exactly what would flip skip-vs-keep to a silent skip with no
    evidence about the document's content. Returns True on the first real match OR on any
    matcher error, False only when the matcher genuinely ran on every item and matched
    none."""
    for item in doc.get("items") or []:
        try:
            decision = _match_item(client, item, catalog, None, "")
        except Exception as e:
            _check_retry(message.get("attempts", 0), str(e))
            return True   # fail SAFE: an error is not evidence of "no match" — keep the mail
        if not desadv_edi._is_unmatched(decision.gtin):
            return True
    return False


def _skip_not_warehouse(conn, shadow: bool, message: dict, doc_number: str,
                        supplier_name: str) -> dict:
    """#314: terminal handling for a document from a REMEMBERED non-warehouse supplier with
    NO catalog match — no board question, no upload. The `not_warehouse` outcome flows into
    `_aggregate_status` → `_run_and_finish`'s rollup event (`stage/status='not_warehouse'`),
    the SAME event shape #307's manual `close_message_not_warehouse` produces, so it stays
    visible in the daily digest for Marek (req 2) — never a silent drop. `_run_and_finish`
    marks the message processed as usual."""
    _event(conn, shadow, message["message_id"], stage="not_warehouse",
          status="not_warehouse",
          outcome="netýka sa skladu — automaticky (zapamätaný dodávateľ), bez EDI",
          detail={"doc_number": doc_number, "supplier_name": supplier_name},
          rollup=False, workflow=dl_report.WORKFLOW)
    return {"outcome": "not_warehouse", "doc_number": doc_number,
           "supplier_name": supplier_name}


# --- #365: hold-instead-of-partial-ship helpers -----------------------------

def _skip_answered_item_keys(conn, message_id: str) -> set[str]:
    """The `order_questions.item_key`s of every `dl_item` question of THIS message the sklad
    has answered with "nemá kartu — pošli bez tejto položky" (`answer->>'choice'` equals the
    `teach.DL_ITEM_SHIP_WITHOUT` sentinel). Read on the message's reprocess so a
    deliberately-skipped line is shipped WITHOUT it instead of re-holding the whole document
    forever. Matched against `teach.dl_item_key(supplier_ean, wording)` — the exact key
    `ask_dl_item` stored the question under — so the skip survives the reprocess even though
    the line stays genuinely unmatched (there is no card for it). A real-card answer stores
    the GTIN as the choice, `close_message_sklad_unknown`/`not_warehouse` store a different
    jsonb shape with no `choice` key at all — neither equals the sentinel, so only a real
    "pošli bez nej" answer is returned here."""
    rows = conn.execute(
        """SELECT item_key FROM order_questions
            WHERE message_id = %s AND kind = 'dl_item' AND status = 'answered'
              AND answer->>'choice' = %s""",
        (message_id, teach.DL_ITEM_SHIP_WITHOUT)).fetchall()
    return {r[0] for r in rows}


def _hold_review_reason(item_names: list[str]) -> str:
    """The ❗ 'potrebuje kontrolu' body for a HELD document — names the unmatched line(s) and
    tells the sklad exactly what to do on the board (find the card, or confirm „pošli bez
    nej"). Deliberately NOT the ⚠️ 'EDI šlo BEZ nich' partial-upload wording — nothing was
    uploaded."""
    n = len(item_names)
    listed = ", ".join(name for name in item_names if name) or "neznáme položky"
    return (f"Dodací list má {n} nespárovanú/é skladovú/é položku/y — z bezpečnosti sa "
            f"NEnahráva do ORIONu, kým ich nevybavíš na nástenke (nájdeš kartu, alebo "
            f"potvrdíš „pošli bez tejto položky“), aby doklad odišiel KOMPLETNÝ: {listed}.")


# --- one document (R60-R97) -------------------------------------------------

def _process_document(conn, cfg, client, message: dict, doc: dict, catalog: list[dict],
                      suppliers: list[dict], shadow: bool, all_items: list[dict],
                      upload=None, post=None, list_dirs=None) -> dict:
    subject, from_addr = message.get("subject", ""), message.get("from_addr", "")
    doc_number = doc.get("docNumber") or ""
    delivery_date = doc.get("deliveryDate", "")
    # #240: computed once, reused both for the memory-rescue lookup in `_match_supplier`
    # and for the `teach.ask_dl_supplier` call below — the SAME address must key both,
    # or a taught mapping would never actually match what a later ask/resolve looks up.
    sender_email = doc.get("supplierEmail") or from_addr
    upload = upload or (lambda c, name, content, dir_override=None:
                        upload_mod.put(c, name, content, dir_override=dir_override))
    # #229 follow-up 2: computed once, reused by every build_review/build_success call
    # below — each function decides for ITSELF whether this specific message actually
    # needs the link (review always does; success only when it raised a real question).
    # #231: the DL-only nástenka link, never the mixed AI-orders `sklad_link`.
    link = report.dl_sklad_link(cfg)

    if doc.get("status") == "needsReview":
        reason = doc.get("reviewReason") or "Dokument potrebuje kontrolu"
        _post(cfg, shadow, lambda: dl_report.build_review(
            reason, doc.get("supplierName", ""), doc_number, delivery_date, from_addr,
            subject, link=link), post=post)
        _event(conn, shadow, message["message_id"], stage="review", status="review",
              outcome=reason, detail={"doc_number": doc_number}, rollup=False,
              workflow=dl_report.WORKFLOW)
        return {"outcome": "review", "doc_number": doc_number,
               "supplier_name": doc.get("supplierName", ""), "reason": reason}

    try:
        supplier_decision = _match_supplier(conn, client, doc, suppliers, sender_email)
    except Exception as e:
        _check_retry(message.get("attempts", 0), str(e))
        # #312: the raw exception repr must NOT reach the warehouse channel (243) — a
        # clean, actionable sentence goes there; the technical detail stays in the log
        # and in `email_events.detail` (internal), never on the user surface.
        log.warning("DL supplier match failed for message %s doc %s: %s",
                    message["message_id"], doc_number, e)
        reason = "Nepodarilo sa priradiť dodávateľa — over dodací list ručne."
        _post(cfg, shadow, lambda: dl_report.build_review(
            reason, "", doc_number, delivery_date, from_addr, subject, link=link),
            post=post)
        _event(conn, shadow, message["message_id"], stage="review", status="error",
              outcome=reason, detail={"doc_number": doc_number, "error": str(e)},
              rollup=False, workflow=dl_report.WORKFLOW)
        return {"outcome": "review", "doc_number": doc_number, "supplier_name": "",
               "reason": reason}

    # #314: is this supplier remembered as non-warehouse (the warehouse marked an earlier
    # mail "Netýka sa skladu")? Keyed on the supplier's own identity — registry EAN (when
    # matched) ∪ the extracted name — never blindly the email (see dl_nonwarehouse.resolve).
    nw_remembered = dl_nonwarehouse.resolve(
        conn, supplier_decision.ean_edi,
        supplier_decision.name or doc.get("supplierName", ""), sender_email) is not None

    if not supplier_decision.matched:
        # #314: a remembered non-warehouse supplier whose document has NO catalog match is
        # handled terminally, with no dl_supplier question (req 2). But a document that DOES
        # carry a catalog item is NEVER silently dropped (req 4, safety override) — it keeps
        # the existing ask+review path so a human can still identify the unregistered
        # supplier and ship the genuine goods.
        if nw_remembered and not _document_has_catalog_match(client, message, doc, catalog):
            return _skip_not_warehouse(conn, shadow, message, doc_number,
                                       doc.get("supplierName", ""))
        if not shadow:
            cands = dl_match.supplier_candidates(
                doc.get("supplierName", ""), doc.get("supplierEmail", ""),
                doc.get("supplierCity", ""), suppliers)
            teach.ask_dl_supplier(conn, message["message_id"], sender_email, cands,
                                  delivery_date=delivery_date,
                                  supplier_name=doc.get("supplierName", ""),
                                  supplier_city=doc.get("supplierCity", ""))
        _post(cfg, shadow, lambda: dl_report.build_review(
            supplier_decision.note, "", doc_number, delivery_date, from_addr, subject,
            link=link), post=post)
        _event(conn, shadow, message["message_id"], stage="review", status="review",
              outcome=supplier_decision.note, detail={"doc_number": doc_number},
              rollup=False, workflow=dl_report.WORKFLOW)
        return {"outcome": "review", "doc_number": doc_number, "supplier_name": "",
               "reason": supplier_decision.note}

    matched_items: list[dict] = []
    decisions: list[tuple[dict, dl_match.Decision]] = []
    # #314: (item, recalled, note) per unmatched item — the dl_item asks are DEFERRED to
    # after the loop, so a remembered non-warehouse supplier with no catalog match can be
    # short-circuited (terminal skip, zero questions) before any question is raised.
    unmatched_asks: list[tuple[dict, dl_memory.Recalled | None, str]] = []
    catalog_gtins = {str(c.get("gtin")) for c in catalog}
    # #337: the RETIRED override cards (absent from the frozen catalog, so NOT in
    # `catalog_gtins` — the memory-rescue filter stays intact). Used ONLY to recognize an
    # item that failed to match the ACTIVE catalog as a known-but-manual retired product.
    retired_cards = dl_snapshot.retired_dl_cards(conn)
    retired_gtins = {str(c["gtin"]) for c in retired_cards}
    for item in doc.get("items") or []:
        recalled = dl_memory.resolve(conn, supplier_decision.ean_edi, item.get("name", ""),
                                     catalog_gtins=catalog_gtins,
                                     as_of=message.get("today", ""))
        try:
            decision = _match_item(client, item, catalog, recalled, supplier_decision.name)
        except Exception as e:
            _check_retry(message.get("attempts", 0), str(e))
            # #312: the item note is warehouse-facing (it becomes the dl_item board
            # question's reason) — a clean sentence, raw exception only to the log.
            log.warning("DL item match failed for message %s item %r: %s",
                        message["message_id"], item.get("name", ""), e)
            decision = dl_match.Decision(
                item_name=item.get("name", ""), gtin=None, card="", mass=0.0,
                confidence=0.0, rule="match_failed",
                note="Položku sa nepodarilo priradiť ku karte — over ju ručne.",
                review=True)
        # #337: ACTIVE-MATCH-FIRST retired-card recognition. Only an item that did NOT
        # match an active card (`not decision.gtin`) and is not a matcher exception
        # (`match_failed`) is checked against the RETIRED cards — so a product with an
        # active sibling card (e.g. #246's 13-digit card next to a retired 14-digit one)
        # still ships via its own active match and never reaches here. A recognized retired
        # product becomes a `retired_manual` decision: `gtin=None` (never ships, never a
        # catalog match) + an honest manual-only note, and its board question is SKIPPED
        # below. The history signal uses an UNFILTERED `dl_memory.resolve` (catalog_gtins=
        # None) purely to IDENTIFY the retired GTIN — never to ship it (gtin stays None).
        if retired_cards and not decision.gtin and decision.rule != "match_failed":
            retired_recall = dl_memory.resolve(
                conn, supplier_decision.ean_edi, item.get("name", ""),
                catalog_gtins=None, as_of=message.get("today", ""))
            recall_gtin = (str(retired_recall.gtin) if retired_recall
                           and str(retired_recall.gtin) in retired_gtins else "")
            retired_card = dl_match.match_retired(
                item.get("name", ""), retired_cards, recall_gtin=recall_gtin)
            if retired_card is not None:
                decision = dl_match.Decision(
                    item_name=decision.item_name, gtin=None,
                    card=retired_card.get("name", ""), mass=0.0,
                    confidence=decision.confidence, rule="retired_manual",
                    note=("Známy produkt s vyradenou skladovou kartou — automatický EDI "
                          "preň nemá bezpečný cieľ, vybav ručne v CODEXe (kartu "
                          "nepridávaj do znalostí)."),
                    review=True, trace=decision.trace)
        decisions.append((item, decision))
        matched_items.append({
            "gtin": decision.gtin, "name": decision.item_name,
            "matchedCatalogName": decision.card, "quantity": item.get("quantity"),
            "unit": item.get("unit"), "unitPrice": item.get("unitPrice"),
            "totalPrice": item.get("totalPrice"), "mass": decision.mass})
        all_items.append({
            "name": decision.item_name, "quantity": item.get("quantity"),
            "unit": item.get("unit"), "gtin": decision.gtin, "card": decision.card,
            "confidence": decision.confidence, "rule": decision.rule,
            "trace": decision.trace})
        # Deep-review finding on #204's own PR: gate on `not decision.gtin` — the item
        # is genuinely EXCLUDED from the EDI whenever `desadv_edi._is_unmatched(gtin)`
        # would fire (i.e. `gtin` is falsy), which is EVERY rule that ends up here with
        # no card (`unmatched`, the R75 lexical-gap tripwire `llm_sure_lexical_gap`,
        # and this function's own `match_failed` fallback) — not only the literal
        # string `"unmatched"`. Keying on the rule NAME instead of the actual EDI
        # exclusion left those other two rules silently invisible: no nástenka
        # question, no line in the Odoo message, nothing — exactly the loss class
        # this migration exists to remove.
        # #314: DEFER the ask (was inline `if not decision.gtin and not shadow`) — see the
        # `unmatched_asks` declaration above. The EDI-exclusion gate `not decision.gtin` is
        # the #204 deep-review finding, unchanged (every rule with no real card — `unmatched`,
        # the R75 `llm_sure_lexical_gap` tripwire, `match_failed` — is captured, not just the
        # literal `"unmatched"` rule name).
        # #337: a `retired_manual` item is a KNOWN retired card — route it to review via
        # its note (below), but NEVER raise a per-delivery board question for it.
        if not decision.gtin and decision.rule != "retired_manual":
            unmatched_asks.append((item, recalled, decision.note))

    # #314: a remembered non-warehouse supplier whose document produced NO catalog GTIN
    # match is handled terminally — no dl_item questions, no upload (req 2). A document WITH
    # at least one real match falls straight through to the normal build/claim/ship path
    # below (req 4, safety override), and its unmatched items still raise their questions.
    has_catalog_match = any(not desadv_edi._is_unmatched(d.gtin) for _, d in decisions)
    # #314 adversarial-review finding: a `match_failed` decision is a matcher EXCEPTION (the
    # `except` branch in the loop above), NOT a genuine "no match" — never skip on it, or a
    # model outage on a remembered supplier's mail would become a silent drop. Fall through
    # to the normal review+ask path (mirrors case B's now-fail-safe _document_has_catalog_match).
    has_match_failure = any(d.rule == "match_failed" for _, d in decisions)
    if nw_remembered and not has_catalog_match and not has_match_failure:
        return _skip_not_warehouse(conn, shadow, message, doc_number,
                                   supplier_decision.name)

    # #365: the sklad can answer a dl_item question with "nemá kartu — pošli bez tejto
    # položky"; that skip is recorded on the (now answered) question row and read back here
    # on the message's reprocess, so a deliberately-skipped line is shipped WITHOUT it
    # instead of re-holding the whole document forever (the loop this ticket must avoid).
    # LIVE path only — shadow (the eval corpus) never asks a question nor holds, so the
    # shadow outcome stays byte-identical (no corpus drift): the corpus measures MATCHING
    # ("partial" = shippable-but-incomplete), never the live HOLD policy layered on top.
    skipped_keys = (_skip_answered_item_keys(conn, message["message_id"])
                    if not shadow else set())
    pending_asks = [(item, recalled, note) for item, recalled, note in unmatched_asks
                    if teach.dl_item_key(supplier_decision.ean_edi, item.get("name", ""))
                    not in skipped_keys]

    # Fire the deferred dl_item questions. For a non-remembered supplier this is byte-for-
    # byte the pre-#314 behaviour (the asks simply moved out of the loop); for a remembered
    # supplier reached here (it HAS a catalog match) it is the safety-override path. #365: a
    # line the sklad already said "pošli bez nej" for is filtered out (`pending_asks`), so it
    # is never re-asked on reprocess.
    #
    # #365 review finding: the HOLD gate keys on lines that ACTUALLY have a board question —
    # NOT merely on "unmatched". `ask_dl_item` REFUSES to ask (returns None) for a line whose
    # wording is already human-taught yet still comes back unmatched (the #236 R75/R74
    # tripwire class) or whose name is blank — holding such a line would be a permanent
    # DEAD-END (no question row ⟹ `release_for_question` is unreachable ⟹ stuck forever, a
    # regression on the pre-#365 partial-ship). So only a line that got a real qid (a fresh
    # question, OR an existing open one it deduped onto) is `held`; an ask-refused line is
    # excluded from the EDI and the document ships partial exactly as before.
    held_items: list[dict] = []
    if not shadow:
        for item, recalled, note in pending_asks:
            cands = dl_match.candidates(item.get("name", ""), catalog,
                                        memory_gtin=(recalled.gtin if recalled else ""))
            qid = teach.ask_dl_item(conn, message["message_id"], supplier_decision.ean_edi,
                                    supplier_decision.name, item.get("name", ""),
                                    item.get("quantity"), item.get("unit", ""), cands,
                                    delivery_date=delivery_date, reason=note,
                                    catalog_gtins=catalog_gtins)
            if qid is not None:
                held_items.append(item)

    header = {"customerName": supplier_decision.name,
             "customerEanEdi": supplier_decision.ean_edi}
    # #262: an informal delivery announcement (mail body text, no printed document)
    # extracts with NO docNumber at all — synthesize a STABLE identity here, keyed on
    # the message itself, BEFORE build() ever sees an empty docNumber. This is the
    # ONLY call site build() has, so its own `extraction_doc_number or
    # _generate_doc_number(...)` wall-clock fallback (R83) is never reached from the
    # live worker any more — see `desadv_edi.generate_stable_doc_number()`'s own
    # docstring for why a wall-clock value is unsafe here (a stale-claim reclaim or
    # an R17 retry would change the desadv_sent dedup key on every attempt).
    stable_doc_number = doc_number or desadv_edi.generate_stable_doc_number(
        message["message_id"])
    extraction = {"docNumber": stable_doc_number, "deliveryDate": delivery_date}
    built = desadv_edi.build(header, extraction, matched_items, catalog)

    if not built.can_create:
        # #337: name the retired products explicitly in the review, so a document that is
        # all (or partly) retired cards reads as a HONEST known-but-manual outcome, not a
        # bare "no items with GTIN". (A mixed document that DOES ship shows the same note
        # per item via `_finish_shipped`'s `unmatched_notes`.)
        reason = built.reject_reason
        retired_names = [d.card or d.item_name for _, d in decisions
                         if d.rule == "retired_manual"]
        if retired_names:
            reason = ((reason + " " if reason else "")
                      + "Vyradené skladové karty (automatický EDI nemá bezpečný cieľ, "
                      "vybav ručne v CODEXe): " + ", ".join(retired_names) + ".")
        _post(cfg, shadow, lambda: dl_report.build_review(
            reason, supplier_decision.name, built.doc_number, delivery_date,
            from_addr, subject, link=link), post=post)
        _event(conn, shadow, message["message_id"], stage="review", status="review",
              outcome=reason, detail={"doc_number": built.doc_number},
              rollup=False, workflow=dl_report.WORKFLOW)
        return {"outcome": "review", "doc_number": built.doc_number,
               "supplier_name": supplier_decision.name, "reason": reason}

    if shadow:
        is_dup = desadv.already_sent(conn, supplier_decision.ean_edi, built.doc_number)
        outcome = "duplicate" if is_dup else ("partial" if built.partial else "ok")
        return {"outcome": outcome, "doc_number": built.doc_number,
               "supplier_name": supplier_decision.name,
               "supplier_ean": supplier_decision.ean_edi, "filename": built.filename,
               "line_count": built.line_count,
               "items_skipped_no_match": built.items_skipped_no_match,
               "price_substitutions": built.price_substitutions,
               "items": _shipped_items(decisions)}

    # #365: a shippable document (`can_create`) that still has a warehouse-relevant item
    # AWAITING a board answer is HELD — never partial-shipped. No claim, no upload: the ORION
    # document is never sent incomplete (the msg 8804 / question 101 incident). The dl_item
    # question is already raised above; here we post the ❗ "potrebuje kontrolu / Rieš na
    # nástenke" message (the SAME `build_review` shape a `can_create=False` document uses, and
    # the price-mismatch check uses) and return a `review` outcome — `messages` ends
    # processed/review, and answering the question re-runs the whole message
    # (`release_for_question`) so a taught card ships the COMPLETE EDI, while a "pošli bez nej"
    # answer (filtered out of `pending_asks` above) ships it partial-yet-human-confirmed.
    #
    # #365 review finding: skip the hold when the document is ALREADY on ORION
    # (`already_sent` — a pre-#365 partial ship, or a doc still carrying a second pending line
    # after an earlier ship). Holding it would post a misleading "NEnahráva do ORIONu" while
    # the doc IS in ORION; falling through lets `claim_send_or_identify` recognise the
    # confirmed claim and log a duplicate (no re-upload, no false hold message). `already_sent`
    # is a read-only check (the same one the shadow branch above uses). A document whose only
    # unmatched items are all skip-answered (or ask-refused) has an empty `held_items` and
    # falls straight through to the normal ship/duplicate handling below.
    if held_items and not desadv.already_sent(conn, supplier_decision.ean_edi,
                                              built.doc_number):
        held_names = [item.get("name", "") for item in held_items]
        reason = _hold_review_reason(held_names)
        _post(cfg, shadow, lambda: dl_report.build_review(
            reason, supplier_decision.name, built.doc_number, delivery_date, from_addr,
            subject, link=link), post=post)
        _event(conn, shadow, message["message_id"], stage="review", status="review",
              outcome=reason, detail={"doc_number": built.doc_number, "held": True,
              "held_items": held_names}, rollup=False, workflow=dl_report.WORKFLOW)
        return {"outcome": "review", "doc_number": built.doc_number,
               "supplier_name": supplier_decision.name, "reason": reason, "held": True}

    claimed, holder = desadv.claim_send_or_identify(
        conn, supplier_decision.ean_edi, built.doc_number, built.filename,
        message_id=message["message_id"])
    if not claimed:
        # #216: a claim refusal has TWO different causes, and only one of them is a
        # genuine W7 duplicate. R17's transient retry re-processes the WHOLE message
        # on the next tick, including any document that already shipped earlier in a
        # prior (partially-failed) attempt of THIS SAME message — that self-caused
        # re-skip must not be counted the same as a real different message (e.g. a
        # supplier's genuine re-announcement) having already sent this exact document.
        # Compare the CURRENT claimant (read atomically alongside the claim decision
        # itself, `claim_send_or_identify` — never a separate follow-up read, review
        # finding on this ticket's own PR) against the message being processed now.
        if holder == message["message_id"]:
            dl_report.log_already_shipped_this_run(
                conn, message["message_id"], built.doc_number, supplier_decision.ean_edi)
        else:
            # W7 fix: visible in the daily digest, never a silent skip (R32's "quiet by
            # design" is exactly what lost the Lunys X1/X2 pair in the incident spec §4
            # documents).
            dl_report.log_duplicate(conn, message["message_id"], built.doc_number,
                                    supplier_decision.ean_edi)
        return {"outcome": "duplicate", "doc_number": built.doc_number,
               "supplier_name": supplier_decision.name}

    upload_name = desadv_edi.upload_name(built.filename)
    upload_dir = getattr(cfg, "orion_dl_dir", upload_mod.DL_DIR)
    list_dirs = list_dirs or upload_mod.list_dirs

    def _finish_shipped() -> dict:
        """The shared "document is CONFIRMED on ORION" tail — called after a normal
        upload success, AND (finding 6, #239) after a transient upload failure whose
        stable-identity presence check proved the bytes already landed (reply lost,
        not the upload itself), or whose single safe retry succeeded. A previous design
        comment on #239 explicitly deferred wiring retry INTO this function's inline
        body to avoid duplicating this ~30-line tail into a second, driftable copy —
        this closure is that extraction: every success path shares EXACTLY one shape,
        never two independently-maintained ones. Captures `conn`/`cfg`/`shadow`/
        `message`/`subject`/`from_addr`/`delivery_date`/`link`/`supplier_decision`/
        `built`/`decisions`/`post` from the enclosing scope — the same free variables
        the pre-finding-6 inline code already used directly."""
        desadv.confirm_sent(conn, supplier_decision.ean_edi, built.doc_number,
                            pg_dsn=getattr(cfg, "pg_dsn", ""))

        today = message.get("today") or datetime.now(UTC).date().isoformat()
        for _item, decision in decisions:
            if not desadv_edi._is_unmatched(decision.gtin):
                # R91: item-history write, ONLY for what actually shipped.
                dl_memory.remember(conn, supplier_decision.ean_edi, decision.item_name,
                                  decision.gtin, decision.card, today, source="ship")

        shipped, unmatched_notes, borderline_notes, history_notes = [], [], [], []
        for item, decision in decisions:
            if (not desadv_edi._is_unmatched(decision.gtin)
                    and _num(item.get("quantity")) != 0):
                shipped.append({"name": decision.card or decision.item_name,
                               "quantity": item.get("quantity"),
                               "unit": item.get("unit")})
                if decision.rule == "llm_borderline":
                    borderline_notes.append(
                        f"{decision.item_name} ({decision.card}, istota "
                        f"{round(decision.confidence * 100)} %)")
                elif decision.rule == "weight_override":
                    history_notes.append(f"{decision.item_name} -> {decision.card} "
                                         f"({decision.note})")
            elif not decision.gtin:
                # Same fix as the nástenka gate above: any decision with no real gtin
                # was excluded from the EDI, regardless of which rule produced it.
                unmatched_notes.append(f"{decision.item_name} ({decision.note})")

        outcome = "partial" if built.partial else "ok"
        _post(cfg, shadow, lambda: dl_report.build_success(
            supplier_decision.name, built.doc_number, delivery_date, from_addr, subject,
            shipped, unmatched_items=unmatched_notes, borderline_notes=borderline_notes,
            history_notes=history_notes, price_substitutions=built.price_substitutions,
            filename=built.filename, partial=built.partial, link=link),
            post=post)
        _event(conn, shadow, message["message_id"], stage="uploaded_orion", status="ok",
              outcome=f"EDI vytvorené: {built.filename}",
              detail={"doc_number": built.doc_number, "edi_file": built.filename},
              rollup=False, workflow=dl_report.WORKFLOW)
        return {"outcome": outcome, "doc_number": built.doc_number,
               "supplier_name": supplier_decision.name,
               "supplier_ean": supplier_decision.ean_edi, "filename": built.filename,
               "line_count": built.line_count,
               "items_skipped_no_match": built.items_skipped_no_match,
               "items_skipped_zero_qty": built.items_skipped_zero_qty,
               "price_substitutions": built.price_substitutions,
               # #229 follow-up 2, review finding: `outcome`/`built.partial` alone is
               # NOT a precise "did this raise a real dl_item board question" signal --
               # desadv_edi.build() excludes a zero-quantity item from its own
               # no_match/partial computation even when unmatched, but dl_worker's own
               # teach.ask_dl_item call above fires for ANY unmatched item regardless
               # of quantity. `unmatched_notes` is that exact, precise signal (same
               # list build_success's own link condition already reads) -- carry it
               # through so build_success renders the dashboard link on exactly this
               # signal, not an imprecise proxy from `outcome`.
               "unmatched_items": unmatched_notes,
               "items": _shipped_items(decisions)}

    def _alert_and_release(err: Exception) -> dict:
        """The pre-finding-6 "upload genuinely failed" tail, unchanged: release the
        claim so the document can be retried by a LATER independent attempt (a human
        reprocess, or R17's own message-level retry), and durably enqueue the alert so
        the failure stays visible instead of silent (#239 class 2, requirement 3) --
        never a fire-and-forget best-effort post (Odoo being down at this exact moment
        would otherwise lose the alert with no trace, ever). Reached whenever a safe
        retry was not possible: a NON-transient failure, a transient one whose presence
        check proved the document is genuinely absent AND whose single retry also
        failed, or a transient one whose presence check itself could not be attempted
        (finding 6, #239)."""
        desadv.release_send(conn, supplier_decision.ean_edi, built.doc_number)
        log.exception("DL upload of %s failed", built.filename)
        note = ("Odoslanie dodacieho listu do ORIONu zlyhalo — skús znova alebo "
               "nahlás administrátorovi")
        # #312: the raw upload exception must NOT reach the warehouse channel (243) — a
        # clean sentence goes into the alert; the raw error stays in the log
        # (`log.exception` above) and in `email_events.detail` (below), never on the user
        # surface. #336: the clean "nahranie do ORIONu zlyhalo" sentence + the dashboard
        # action link now live ONCE in the per-kind header `dl_alerts.flush_pending` builds
        # for the whole `dl_upload_failed` group (`GROUPED_ITEM_KINDS`); this alert is just
        # ONE short line naming the supplier + delivery note.
        # Deep-review finding on this ticket's own PR: `stuck_classified_sweep` below
        # already bails out when `delivery_notes_channel_id` resolves to 0 (unset) —
        # this call site needs the SAME guard, or an unset channel would enqueue a
        # `pending_alerts` row that can NEVER be delivered (nothing posts to channel 0)
        # and would sit pending forever, growing the "stuck backlog" gauge with no way
        # to resolve it.
        channel = int(getattr(cfg, "delivery_notes_channel_id", 0) or 0)
        if channel:
            try:
                dl_alerts.enqueue(
                    conn, channel, "dl_upload_failed",
                    dl_alerts.item_line(supplier_decision.name or from_addr,
                                        f"dodací list {built.doc_number}"),
                    message_id=message["message_id"])
            except Exception:
                log.exception("failed to enqueue the DL upload-failure alert for %s",
                              built.filename)
        else:
            log.warning("delivery_notes_channel_id is unset — the upload-failure "
                       "alert for %s could not be enqueued", built.filename)
        _event(conn, shadow, message["message_id"], stage="review", status="error",
              outcome=note, detail={"error": repr(err), "filename": built.filename},
              rollup=False, workflow=dl_report.WORKFLOW)
        return {"outcome": "review", "doc_number": built.doc_number,
               "supplier_name": supplier_decision.name, "reason": note}

    try:
        upload(cfg, upload_name, built.content, dir_override=upload_dir)
    except Exception as e:
        # #239 finding 6 (remainder): a TRANSIENT failure now gets a stable-identity
        # presence check BEFORE deciding what to do — the safety this ticket's earlier
        # increments built (temp-write+rename upload, `desadv_edi.stable_prefix()`/
        # `already_landed()`) but never wired into a decision. A NON-transient failure
        # (`_is_transient(str(e))` False) skips the check entirely and keeps exactly
        # the pre-finding-6 behaviour — `landed` stays `None`.
        landed = _check_landed(conn, cfg, list_dirs, supplier_decision.ean_edi,
                               built.doc_number) if _is_transient(str(e)) else None
        if landed is True:
            # The reply was lost, but the bytes are already on ORION under an earlier
            # attempt's name — confirming, never re-uploading, is what actually
            # prevents the v0.9.70 duplicate-delivery incident (a blind retry here
            # would be the exact bug this whole ticket exists to fix).
            log.warning(
                "DL upload of %s: the reply was lost but the document is already on "
                "ORION under an earlier attempt's name (stable identity match) — "
                "confirming instead of re-uploading (%s)", built.filename, e)
            return _finish_shipped()
        if landed is False:
            # Genuinely absent everywhere a document could legitimately be sitting —
            # exactly ONE retry is safe here, bounded (never a loop), with the SAME
            # claim held throughout (never release-then-reclaim, which is precisely
            # what removed the anti-duplicate protection in the v0.9.70 incident).
            log.info(
                "DL upload of %s: transient failure (%s), document not yet on ORION "
                "— retrying exactly once with the same claim", built.filename, e)
            try:
                upload(cfg, upload_name, built.content, dir_override=upload_dir)
            except Exception as e2:
                # #373: the retry's OWN upload has the SAME failure mode as the first
                # attempt — its temp-write+rename may have landed the bytes on ORION while
                # only the confirming reply was lost. Releasing the claim now would remove
                # the anti-duplicate protection and let a later manual reprocess upload a
                # SECOND copy (the v0.9.70 duplicate-delivery class, one attempt later). So
                # presence-check ONCE MORE before releasing — gated exactly like the first
                # check (transient e2), same tri-state: found+trustworthy → confirm the SAME
                # claim; absent / unavailable / collision → the release+alert path below.
                # NEVER a third upload — the "exactly one retry" bound is unchanged.
                landed2 = (_check_landed(conn, cfg, list_dirs, supplier_decision.ean_edi,
                                         built.doc_number)
                           if _is_transient(str(e2)) else None)
                if landed2 is True:
                    log.warning(
                        "DL upload retry of %s failed but the document is already on ORION "
                        "under the retry's own name (stable identity match) — confirming "
                        "instead of releasing (%s)", built.filename, e2)
                    return _finish_shipped()
                log.exception("DL upload retry of %s also failed (original: %s)",
                              built.filename, e)
                return _alert_and_release(e2)
            return _finish_shipped()
        # `landed is None`: either non-transient, or the presence check itself could
        # not be attempted (the SFTP connection that just failed the upload is very
        # likely down for a follow-up listdir too) — no safe retry is possible either
        # way, so this keeps the pre-finding-6 behaviour exactly.
        return _alert_and_release(e)

    return _finish_shipped()
