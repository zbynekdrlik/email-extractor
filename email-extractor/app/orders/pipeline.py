"""The pipeline (#67): one email -> orders -> EDI -> ORION -> Odoo.

Composition only; every stage is its own module and its own tests. What lives here is the
part that must not go wrong twice:

- **Shadow mode uploads nothing, posts nothing, learns nothing and writes no event.** It
  produces the same verdict and records what it WOULD have sent, so it can be compared
  against n8n while n8n keeps running.
- **The ledger is claimed before the upload and released when the upload fails**, so a
  duplicate order in ORION is impossible and a failed one is retryable.
- **Only what actually shipped is remembered.** Learning from an order that never arrived
  would teach the matcher from a fiction.
- **Exactly ONE Odoo message per processed e-mail (#139).** Every order's outcome and
  every new warehouse question is accumulated during `_run` and posted as ONE short
  summary at the very end (`_post_summary`) — never one message per order or per question.
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import customer, edi, extract, hold, llm, match, memory, report, snapshot, teach
from . import upload as upload_mod

log = logging.getLogger("orders.pipeline")

# The rungs that mean the engine could not settle the line on its own. Each becomes
# ONE question for the warehouse (#88) — answering it teaches the wording for good.
# What a human can actually settle. `unique_card` is NOT here (#103): a product we make
# in exactly one gramáž has no alternative to choose between, so asking is noise.
ASK_THE_WAREHOUSE = ("unmatched", "llm_borderline", "history_weight")

PROMPTS = Path(__file__).with_name("prompts")

CUSTOMER_SCHEMA = {
    "type": "object",
    "properties": {"ean_edi": {"type": "string"}, "confidence": {"type": "number"},
                   "reason": {"type": "string"}},
    "required": ["ean_edi", "confidence"],
}
PRODUCT_SCHEMA = {
    "type": "object",
    "properties": {"gtin": {"type": "string"}, "confidence": {"type": "number"},
                   "matchedCatalogName": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["gtin", "confidence"],
}


def _prompt(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def _customer_input(cands: list[dict], email: dict, extracted: dict) -> str:
    lines = []
    for i, c in enumerate(cands, 1):
        mark = " | PRESNÁ ZHODA E-MAILU ODOSIELATEĽA" if c.get("exact_email") else ""
        lines.append(f"{i}. {c.get('name', '')} | EAN: {c.get('ean_edi', '')} | "
                     f"Obec: {c.get('city', '')} | Ulica: {c.get('street', '')} | "
                     f"E-mail: {', '.join(c.get('emails') or []) or '-'}{mark}")
    return (f"FROM: {extracted.get('senderName') or email.get('from_name', '')} "
            f"<{extracted.get('senderEmail') or email.get('from_addr', '')}>\n"
            f"FIRMA V PODPISE: {extracted.get('companyName', '')}\n"
            f"SUBJECT: {email.get('subject', '')}\n\nKANDIDÁTI:\n" + "\n".join(lines))


def _product_input(item: dict, cands: list[dict], customer_name: str,
                   recalled) -> str:
    listing = "\n".join(
        f"{i}. {c.get('name', '')} | GTIN: {c.get('gtin', '')} | "
        f"Alias: {c.get('alias') or '-'}" for i, c in enumerate(cands, 1))
    history = ""
    if recalled:
        history = (f"\nPREDTÝM DODANÉ tomuto zákazníkovi pre presne toto znenie: "
                   f"„{recalled.card}“ (GTIN {recalled.gtin}, {recalled.note})\n")
    return (f"ZÁKAZNÍK: „{customer_name}“\n{history}\n"
            f"POLOŽKA Z OBJEDNÁVKY:\n„{item.get('name', '')}“ "
            f"(množstvo: {item.get('quantity')} {item.get('unit', 'ks')})\n\n"
            f"KANDIDÁTI Z KATALÓGU:\n{listing}\n")


def _as_edi_items(decisions) -> list[dict]:
    return [{"gtin": d.gtin or "NO_MATCH", "quantity": d.quantity, "unit": d.unit,
             "name": d.item_name, "matchedCatalogName": d.card} for d in decisions]


def _decision_dict(d) -> dict:
    return {"name": d.item_name, "quantity": d.quantity, "unit": d.unit, "gtin": d.gtin,
            "card": d.card, "confidence": d.confidence, "rule": d.rule, "note": d.note,
            "review": d.review, "trace": d.trace}


def run(conn, cfg, message: dict, snapshot_id: int, client=None, upload=None,
        post=None) -> dict:
    """Process one email. Returns the run result (also stored by the worker).

    A thin wrapper so that what the run COST is attached in ONE place, whichever of the
    pipeline's exit paths produced the result (#89) — a per-path copy would be forgotten on
    the next reject path added.
    """
    client = client or llm.from_config(cfg)
    out = _run(conn, cfg, message, snapshot_id, client, upload, post)
    if hasattr(client, "spend"):
        out["spend"] = client.spend()
    return out


def _run(conn, cfg, message: dict, snapshot_id: int, client, upload=None,
         post=None) -> dict:
    shadow = bool(getattr(cfg, "orders_shadow", False))
    upload = upload or (lambda c, name, content: upload_mod.put(c, name, content))
    post = post or (lambda c, html, **kw: report.post_from_config(c, html))

    catalog = snapshot.load_catalog(conn, snapshot_id)
    customers = snapshot.load_customers(conn, snapshot_id)

    extracted = extract.run(client, message)
    if extracted.get("refusal"):
        return _finish(conn, cfg, message, shadow, post, status="review", items=[],
                       result={"shipped": False, "reject_reason": extracted["refusal"],
                               "customer": {}, "unverified": extracted.get("unverified", []),
                               "notes": extracted.get("notes", "")})

    orders = extracted.get("orders") or []
    if not orders:
        return _finish(conn, cfg, message, shadow, post, status="review", items=[],
                       result={"shipped": False,
                               "reject_reason": "AI nenašla v e-maile žiadnu objednávku",
                               "customer": {}, "unverified": extracted.get("unverified", []),
                               "notes": extracted.get("notes", "")})

    # The customer is asked of the model ONCE per email — a second call would cost money to
    # answer the same question. It is RESOLVED per order, though: one attachment may hold
    # two shops of the same chain side by side, and then each half has its own EAN (#101).
    sender = _sender_address(message, extracted, customers)
    cands = customer.candidates(customers, sender,
                                extracted.get("senderName", ""),
                                extracted.get("companyName", ""))
    cust_answer = client.json_call(_prompt("match_customer.md"),
                                   _customer_input(cands, message, extracted),
                                   CUSTOMER_SCHEMA, name="customer")

    def _customer_for(store: str = ""):
        return customer.resolve(customers, sender,
                                extracted.get("senderName", ""),
                                extracted.get("companyName", ""), llm=cust_answer,
                                store=store)

    email_matched = matched = _customer_for()

    orders = _merge_by_day(orders)
    conflict = extract.date_conflict(message.get("subject", ""),
                                     [o.get("deliveryDate", "") for o in orders],
                                     body=message.get("combined_text", "") or "")
    if conflict:
        return _finish(conn, cfg, message, shadow, post, status="review",
                       items=[], result={"shipped": False, "reject_reason": conflict,
                                         "customer": {}, "unverified": [],
                                         "notes": extracted.get("notes", "")})
    is_change = bool(extracted.get("isChangeRequest"))
    today = str(message.get("today") or "")
    asked: list[int] = []
    all_items: list[dict] = []
    statuses: list[str] = []
    previews: list[dict] = []
    order_results: list[dict] = []
    # #139: exactly ONE Odoo message per processed e-mail — every order's outcome and
    # every genuinely new question is accumulated here and posted ONCE, at the very end of
    # this function, instead of once per order and once per question (the old shape: 5
    # delivery dates + 4 questions produced 6 separate Odoo messages for one e-mail).
    order_summaries: list[dict] = []
    new_questions: list[dict] = []
    for order in orders:
        # Two shops in one file are two customers; everything below — the memory lookup,
        # the alias that names the customer, the question, the EDI header — must be the
        # one that shop belongs to, not the email's.
        matched = _customer_for(str(order.get("store") or "")) or email_matched
        decisions = []
        order_question_ids: list[int] = []
        for item in order.get("items") or []:
            recalled = (memory.resolve(conn, matched.ean_edi, item["name"],
                                       as_of=str(message.get("today") or ""))
                        if matched else None)
            # What the warehouse taught for THIS wording across every customer (#102) — a
            # pure read, so it applies even in shadow mode; only the ASKING/TEACHING side
            # effects below are shadow-guarded.
            global_recalled = memory.resolve_global(conn, item["name"])
            # The wording may already BE a card, an alias, or an unanimous history — then
            # the answer is certain and asking the model is paid-for redundancy (#86).
            decision = match.decide_without_model(item["name"], catalog, recalled=recalled,
                                                  global_recalled=global_recalled)
            item_cands: list[dict] = []
            if decision is None:
                item_cands = match.candidates(
                    item["name"], catalog,
                    customer_name=matched.name if matched else "",
                    memory_gtin=recalled.gtin if recalled else "")
                answer = client.json_call(
                    _prompt("match_product.md"),
                    _product_input(item, item_cands, matched.name if matched else "",
                                   recalled),
                    PRODUCT_SCHEMA, name="product")
                decision = match.decide(item_name=item["name"], llm=answer, catalog=catalog,
                                        recalled=recalled,
                                        customer_name=matched.name if matched else "")
            decision.quantity = item.get("quantity")
            decision.unit = item.get("unit", "ks")
            decisions.append(decision)
            # A line the engine could not settle becomes ONE question for the warehouse, with
            # its candidate cards (#88). Answering it teaches the wording for good — measured,
            # the whole tail is 15 (customer, wording) pairs. Never in shadow: shadow must
            # leave no trace, and these are questions for a human. A genuinely NEW question
            # (never a duplicate of one already open) also reaches Odoo (#102) — the warehouse
            # reads Odoo, not always the dashboard.
            if not shadow and matched and decision.rule in ASK_THE_WAREHOUSE:
                # #147: item_cands was scored/truncated to 6 BEFORE this decision existed,
                # so the model's own answer can rank below the cutoff (e.g. a SYNONYMS hit
                # on an unrelated card family). Re-head the list with the engine's actual
                # proposed candidate, computed AFTER the decision, so the warehouse always
                # has the one card it needs to confirm.
                ask_cands = match.candidates_for_question(item_cands, catalog, decision)
                qid = teach.ask(
                    conn, message_id=message.get("message_id", ""),
                    customer_ean=matched.ean_edi, customer_name=matched.name,
                    wording=item["name"], quantity=item.get("quantity"),
                    unit=item.get("unit", "ks"),
                    candidates=[{"gtin": str(c.get("gtin")), "name": c.get("name", "")}
                                for c in ask_cands[:6]],
                    delivery_date=order.get("deliveryDate", ""), reason=decision.note,
                    # #139: a new question no longer posts its own Odoo message — it is
                    # counted into the ONE summary this e-mail posts at the end. The
                    # wording itself stays fully visible on the linked /otazky page.
                    on_new=new_questions.append)
                asked.append(qid)
                if qid:
                    order_question_ids.append(qid)
        decisions = match.merge_same_card(match.apply_siblings(decisions))
        all_items.extend(_decision_dict(d) for d in decisions)
        # #93: a question still open for this order holds the WHOLE order — shipping the
        # matched part now and the taught line later would write two ORION documents for
        # one delivery day (#81.1). Once the delivery date itself arrives there is no more
        # time to wait, so the order ships exactly as it always has (see hold.release_due).
        #
        # `order_question_ids` was built from EACH item's OWN rule, before the merge above —
        # a line that started "unmatched" and got rescued by `apply_siblings` (the same
        # wording resolved elsewhere in this order) now carries a settled rule (`sibling`),
        # not one in ASK_THE_WAREHOUSE. Trusting the pre-merge list alone would hold an
        # order that is already fully, correctly resolved (review finding on PR #116) —
        # exactly the case `apply_siblings` exists to ship immediately. So the ask-list
        # gates WHETHER a question exists at all; whether the order still needs one is
        # re-checked against the POST-merge decisions' rules.
        still_asking = any(d.rule in ASK_THE_WAREHOUSE for d in decisions)
        held_id = None
        reject_reason = ""
        if (not shadow and matched and not is_change and order_question_ids and still_asking
                and not hold.is_past_deadline(order.get("deliveryDate", ""), today)):
            held_id = hold.place(conn, message_id=message.get("message_id", ""),
                                 matched=matched, order=order, decisions=decisions,
                                 extracted=extracted, question_ids=order_question_ids)
            status, preview = "held", {}
            report.log_event(
                conn, message.get("message_id", ""), stage="held", status="held",
                outcome=f"Objednávka čaká na odpoveď skladu ({len(order_question_ids)} "
                        "otázok) — dodanie "
                        f"{order.get('deliveryDate', '') or '(bez dátumu)'}",
                detail={"held_id": held_id, "question_ids": order_question_ids,
                        "delivery_date": order.get("deliveryDate", "")})
        else:
            # post_now=False: this order's own Odoo message is folded into the ONE summary
            # posted at the end of `_run` (#139) — never one post per order.
            status, preview, reject_reason = _ship_one(
                conn, cfg, message, order, matched, decisions, extracted, shadow, upload,
                post, post_now=False)
        statuses.append(status)
        previews.append(preview)
        order_summaries.append({
            "delivery_date": order.get("deliveryDate", ""), "status": status,
            "item_count": len(decisions),
            "missing_count": sum(1 for d in decisions if not d.gtin),
            "reject_reason": reject_reason,
        })
        # One EDI file per order is what n8n produces, so the result must stay per order:
        # flattening hides the second delivery date and its items entirely (#78).
        order_results.append({
            "delivery_date": order.get("deliveryDate", ""),
            "order_number": order.get("orderNumber", ""),
            "recipient_group": order.get("recipientGroup", ""),
            "store": order.get("store", ""),
            "customer_ean": matched.ean_edi if matched else "",
            "customer_name": matched.name if matched else "",
            "status": status,
            "items": [_decision_dict(d) for d in decisions],
            "edi_filename": preview.get("edi_filename", ""),
            "edi_preview": preview.get("edi_preview", ""),
        })

    status = ("error" if "error" in statuses else
              # a held order means this email is not done yet, no matter what its siblings
              # did — the message stays unprocessed until every held order releases (#93)
              "held" if "held" in statuses else
              "review" if all(s == "review" for s in statuses) else
              # one order shipped and another went to review is NOT a clean email: saying
              # "ok" would hide a delivery date nobody sent (#78)
              "partial" if ("partial" in statuses or "review" in statuses) else "ok")
    # customer + delivery date belong in the result: the shadow diff (#67) and the
    # evaluation harness (#66) both compare on them, not just on the item list.
    out = {"status": status, "items": all_items, "prompt_hash": client.last_prompt_hash,
           "shadow": shadow,
           "would_ship": any(s in ("ok", "partial") for s in statuses),
           # The email-level customer, NOT the last order's: a two-shop email has no single
           # customer, and reporting whichever shop happened to be last would be a lie.
           "customer_ean": email_matched.ean_edi if email_matched else "",
           "customer_name": email_matched.name if email_matched else "",
           "customer_rule": email_matched.rule if email_matched else "unmatched",
           "delivery_date": (orders[0].get("deliveryDate") or "") if orders else "",
           "orders": len(orders), "order_results": order_results}
    # In shadow mode the preview IS the deliverable: it is what would have been uploaded.
    for preview in previews:
        if preview:
            out.update(preview)
            break
    # #139: the ONE Odoo message for this whole processed e-mail, covering every order and
    # every new question raised above. `unverified` is EMAIL-level (extraction runs once
    # per e-mail, shared unchanged across every order derived from it) — summed ONCE here,
    # never per order, or a multi-order e-mail would double/triple-count the same list.
    _post_summary(cfg, post, shadow,
                  customer_name=email_matched.name if email_matched else "",
                  orders=order_summaries, new_questions=len(new_questions),
                  unverified_count=len(extracted.get("unverified") or []))
    return out


def _merge_by_day(orders: list[dict]) -> list[dict]:
    """One delivery date is ONE order.

    A mail saying "40 ks for patients and 10 ks for staff" comes back as one order per
    recipient group with the SAME delivery date. Shipping those separately wrote two EDI files
    for one day — two orders in ORION — and the warehouse got 40 or 10, never 50 (#81.1). A
    recipient group is a note on the order, not a separate order.
    """
    merged: dict[tuple, dict] = {}
    for order in orders:
        # The SHOP belongs in the key (#101): a recipient group shares a delivery, but two
        # shops are two customers with two EANs, and merging them writes one shop's name
        # over both shops' pastry.
        key = (str(order.get("deliveryDate") or ""), str(order.get("orderNumber") or ""),
               str(order.get("store") or ""))
        if key not in merged:
            merged[key] = dict(order, items=list(order.get("items") or []))
            continue
        into = merged[key]
        into["items"].extend(order.get("items") or [])
        groups = [g for g in (into.get("recipientGroup"), order.get("recipientGroup")) if g]
        into["recipientGroup"] = ", ".join(dict.fromkeys(groups))
    return list(merged.values())


def _ship_one(conn, cfg, message, order, matched, decisions, extracted, shadow,
              upload, post, post_now: bool = True) -> tuple[str, dict, str]:
    """Build, send and report ONE order. Returns (status, preview, reject_reason).

    `post_now=False` (used by `_run`'s multi-order loop, #139) still logs the event
    timeline but never posts its own Odoo message — the caller folds every order of the
    e-mail into ONE combined summary instead. `hold.py`'s release paths call this with the
    default `post_now=True`: each release is its own, later, single-order event, so
    posting immediately there is already "one message per processed thing".
    """
    items = _as_edi_items(decisions)
    shipped_items = [d for d in decisions if d.gtin]
    missing = [d for d in decisions if not d.gtin]
    is_change = bool(extracted.get("isChangeRequest"))
    delivery = order.get("deliveryDate", "")
    order_no = order.get("orderNumber", "")

    result = {
        "customer": {"name": matched.name if matched else "",
                     "ean_edi": matched.ean_edi if matched else ""},
        "delivery_date": delivery, "order_number": order_no,
        "items": [_decision_dict(d) for d in decisions],
        "unverified": extracted.get("unverified", []),
        "notes": extracted.get("notes", ""), "is_change_request": is_change,
        "shipped": False,
    }

    if not matched:
        result["reject_reason"] = "Zákazník nebol nájdený v tabuľke zákazníkov"
    elif is_change:
        prefix = edi.change_prefix(matched.ean_edi, delivery, order_no)
        result["change_prefix"] = prefix
        result["reject_reason"] = (
            "E-mail je zmena už zadanej objednávky — uprav ju ručne v ORIONe (pôvodný "
            f"súbor začína {prefix})")
    elif not shipped_items:
        result["reject_reason"] = "Žiadnu položku sa nedalo priradiť ku karte"

    if result.get("reject_reason"):
        _finish(conn, cfg, message, shadow, post, status="review",
                items=result["items"], result=result, post_now=post_now)
        return "review", {}, result["reject_reason"]

    built = edi.build(ean=matched.ean_edi, store=matched.name, orderNumber=order_no,
                      deliveryDate=delivery, items=items)
    name = edi.filename(matched.ean_edi, delivery, order_no)
    result["edi_filename"] = name
    preview = {"edi_preview": built.content, "edi_filename": name}

    if shadow:
        # Same verdict, zero side effects: n8n still owns this message.
        return ("partial" if missing else "ok"), preview, ""

    if not edi.claim_send(conn, matched.ean_edi, delivery, built.content, name):
        log.warning("EDI for %s / %s already sent — not uploading again",
                    matched.ean_edi, delivery)
        result["shipped"] = True
        result["reject_reason"] = ""
        # Already reported once for this exact content — never re-post, regardless of
        # what the caller asked for.
        _finish(conn, cfg, message, shadow, post, status="ok", items=result["items"],
                result=result, post_now=False)
        return "ok", preview, ""

    try:
        upload(cfg, name, built.content)
    except Exception as e:
        edi.release_send(conn, matched.ean_edi, delivery, built.content)
        log.exception("upload of %s failed", name)
        # #139 review finding: the raw Python exception repr is a technical detail (the
        # exact thing the shortened Odoo message must never carry) — the full detail
        # stays in the log above and in `error_detail`/`detail=` for the admin-facing
        # event timeline (`_outcome` below), while Odoo gets one short human sentence.
        result["reject_reason"] = ("Odoslanie do ORIONu zlyhalo — skús znova alebo nahlás "
                                   "administrátorovi")
        result["error_detail"] = repr(e)
        _finish(conn, cfg, message, shadow, post, status="error", items=result["items"],
                result=result, detail={"error": repr(e)}, post_now=post_now)
        return "error", preview, result["reject_reason"]

    result["shipped"] = True
    for d in shipped_items:
        memory.remember(conn, matched.ean_edi, d.item_name, d.gtin, d.card,
                        delivered_on=_delivery_day(delivery), source="ship")
    _finish(conn, cfg, message, shadow, post,
            status="partial" if missing else "ok", items=result["items"], result=result,
            detail={"edi_file": name, "orion_path": edi.orion_path(name)}, post_now=post_now)
    return ("partial" if missing else "ok"), preview, ""


def _delivery_day(delivery_date: str) -> str:
    stamp = edi._format_date(delivery_date)
    if stamp.strip():
        return f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}"
    from datetime import UTC, datetime
    return datetime.now(UTC).date().isoformat()


def _post_summary(cfg, post, shadow: bool, customer_name: str, orders: list[dict],
                  new_questions: int = 0, unverified_count: int = 0) -> None:
    """The ONE Odoo message for a processed e-mail (#139) — every caller of `_finish`/
    `_ship_one` funnels through here exactly once per e-mail (or once per later, standalone
    hold-release event). Never raises: a notification failure must never break order
    processing, exactly like the old per-order post it replaces."""
    if shadow:
        return
    html = report.build_summary(customer_name=customer_name, orders=orders,
                                new_questions=new_questions,
                                unverified_count=unverified_count, link=report.sklad_link(cfg))
    try:
        post(cfg, html)
    except Exception:
        log.exception("posting the Odoo summary failed")


def _finish(conn, cfg, message, shadow, post, status: str, items: list,
            result: dict, detail: dict | None = None, post_now: bool = True) -> dict:
    """Log the event timeline and, when `post_now`, post this SINGLE order/rejected-email
    as its own one-line Odoo summary (#139) — unless we are shadowing (then nothing leaves
    this process). `post_now=False` is how `_run`'s multi-order loop defers every order's
    post into the ONE combined summary `_post_summary` sends at the end of the run."""
    if not shadow:
        if post_now:
            _post_summary(cfg, post, shadow, customer_name=(result.get("customer") or {}).get(
                "name", ""), orders=[{
                    "delivery_date": result.get("delivery_date", ""), "status": status,
                    "item_count": len(items),
                    "missing_count": sum(1 for i in items if not i.get("gtin")),
                    "reject_reason": result.get("reject_reason", ""),
                }],
                # #139 review finding: the AGEL-incident phantom-item safeguard
                # (`extract.py`'s `unverified`) must stay visible even after the
                # shortening — never dropped just because the item list itself is gone.
                unverified_count=len(result.get("unverified") or []))
        report.log_event(
            conn, message.get("message_id", ""),
            stage="uploaded_orion" if result.get("shipped") else "review",
            status="ok" if status in ("ok", "partial") else status,
            outcome=_outcome(status, result), detail=detail or {})
    return {"status": status, "items": items, "shadow": shadow,
            "would_ship": bool(result.get("shipped")) or status in ("ok", "partial"),
            # keep the shape identical on the reject paths, so a consumer never has to
            # special-case "this email produced no order at all"
            "customer_ean": (result.get("customer") or {}).get("ean_edi", ""),
            "customer_name": (result.get("customer") or {}).get("name", ""),
            "delivery_date": result.get("delivery_date", ""),
            "orders": 0, "order_results": []}


def _outcome(status: str, result: dict) -> str:
    if result.get("shipped"):
        text = f"EDI vytvorené: {result.get('edi_filename', '')}"
        missing = [i for i in result.get("items", []) if not i.get("gtin")]
        if missing:
            text += f" (NEÚPLNÁ — chýba {len(missing)} položiek)"
        return text
    # `error_detail` (#139) is the full technical reason (e.g. an upload exception's
    # repr) for the admin-facing event timeline; `reject_reason` alone is what a
    # non-technical warehouse worker would read in Odoo, so it stays short there.
    return (result.get("error_detail") or result.get("reject_reason")
           or "Odoo kontrola (AI orders)")


# --- shadow comparison ---------------------------------------------------

def diff(ours: dict, theirs: dict | None) -> list[str]:
    """Real differences between our verdict and n8n's, in plain Slovak.

    Item ORDER and numeric formatting are not differences; a different card, quantity,
    customer or delivery date are.
    """
    if not theirs:
        return ["n8n nemá výsledok pre túto správu"]
    out = []
    if str(ours.get("customer_ean") or "") != str(theirs.get("customer_ean") or ""):
        out.append(f"iný zákazník: my {ours.get('customer_ean')!r} / "
                   f"n8n {theirs.get('customer_ean')!r}")
    if str(ours.get("delivery_date") or "") != str(theirs.get("delivery_date") or ""):
        out.append(f"iný dátum dodania: my {ours.get('delivery_date')!r} / "
                   f"n8n {theirs.get('delivery_date')!r}")

    def as_map(items):
        return {str(i.get("gtin")): float(i.get("quantity") or 0) for i in items or []}

    a, b = as_map(ours.get("items")), as_map(theirs.get("items"))
    for gtin in sorted(set(a) - set(b)):
        out.append(f"kartu {gtin} máme my, n8n nie")
    for gtin in sorted(set(b) - set(a)):
        out.append(f"kartu {gtin} má n8n, my nie")
    for gtin in sorted(set(a) & set(b)):
        if a[gtin] != b[gtin]:
            out.append(f"iné množstvo pri {gtin}: my {a[gtin]:g} / n8n {b[gtin]:g}")
    return out


OUR_DOMAIN = "slovnormal.sk"


def _sender_address(message: dict, extracted: dict, customers: list[dict]) -> str:
    """Who actually sent this mail.

    The envelope address is authoritative and wins. The model's reading is only a fallback,
    because on a reply it happily reports the address it found in the QUOTED text — on one
    real order it returned our own `predaj@slovnormal.sk`, the customer went unresolved and
    the whole order was parked (#81.3).

    The one case where the model's reading is worth more: the envelope IS our own address,
    i.e. somebody here forwarded a customer's mail.
    """
    envelope = (message.get("from_addr") or "").strip()
    stated = (extracted.get("senderEmail") or "").strip()
    if envelope and OUR_DOMAIN not in envelope.lower():
        return envelope
    known = {e.lower() for c in customers for e in (c.get("emails") or [])}
    if stated and stated.lower() in known:
        return stated
    return envelope or stated
