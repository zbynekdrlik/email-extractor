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
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import customer, edi, extract, llm, match, memory, report, snapshot
from . import upload as upload_mod

log = logging.getLogger("orders.pipeline")

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

    # The customer is per EMAIL, so it is decided once even for a multi-order email.
    sender = _sender_address(message, extracted, customers)
    cands = customer.candidates(customers, sender,
                                extracted.get("senderName", ""),
                                extracted.get("companyName", ""))
    cust_answer = client.json_call(_prompt("match_customer.md"),
                                   _customer_input(cands, message, extracted),
                                   CUSTOMER_SCHEMA, name="customer")
    matched = customer.resolve(customers, sender,
                               extracted.get("senderName", ""),
                               extracted.get("companyName", ""), llm=cust_answer)

    orders = _merge_by_day(orders)
    conflict = extract.date_conflict(message.get("subject", ""),
                                     [o.get("deliveryDate", "") for o in orders])
    if conflict:
        return _finish(conn, cfg, message, shadow, post, status="review",
                       items=[], result={"shipped": False, "reject_reason": conflict,
                                         "customer": {}, "unverified": [],
                                         "notes": extracted.get("notes", "")})
    all_items: list[dict] = []
    statuses: list[str] = []
    previews: list[dict] = []
    order_results: list[dict] = []
    for order in orders:
        decisions = []
        for item in order.get("items") or []:
            recalled = (memory.resolve(conn, matched.ean_edi, item["name"],
                                       as_of=str(message.get("today") or ""))
                        if matched else None)
            # The wording may already BE a card, an alias, or an unanimous history — then
            # the answer is certain and asking the model is paid-for redundancy (#86).
            decision = match.decide_without_model(item["name"], catalog, recalled=recalled)
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
        decisions = match.merge_same_card(match.apply_siblings(decisions))
        all_items.extend(_decision_dict(d) for d in decisions)
        status, preview = _ship_one(conn, cfg, message, order, matched, decisions,
                                    extracted, shadow, upload, post)
        statuses.append(status)
        previews.append(preview)
        # One EDI file per order is what n8n produces, so the result must stay per order:
        # flattening hides the second delivery date and its items entirely (#78).
        order_results.append({
            "delivery_date": order.get("deliveryDate", ""),
            "order_number": order.get("orderNumber", ""),
            "recipient_group": order.get("recipientGroup", ""),
            "status": status,
            "items": [_decision_dict(d) for d in decisions],
            "edi_filename": preview.get("edi_filename", ""),
            "edi_preview": preview.get("edi_preview", ""),
        })

    status = ("error" if "error" in statuses else
              "review" if all(s == "review" for s in statuses) else
              # one order shipped and another went to review is NOT a clean email: saying
              # "ok" would hide a delivery date nobody sent (#78)
              "partial" if ("partial" in statuses or "review" in statuses) else "ok")
    # customer + delivery date belong in the result: the shadow diff (#67) and the
    # evaluation harness (#66) both compare on them, not just on the item list.
    out = {"status": status, "items": all_items, "prompt_hash": client.last_prompt_hash,
           "shadow": shadow,
           "would_ship": any(s in ("ok", "partial") for s in statuses),
           "customer_ean": matched.ean_edi if matched else "",
           "customer_name": matched.name if matched else "",
           "customer_rule": matched.rule if matched else "unmatched",
           "delivery_date": (orders[0].get("deliveryDate") or "") if orders else "",
           "orders": len(orders), "order_results": order_results}
    # In shadow mode the preview IS the deliverable: it is what would have been uploaded.
    for preview in previews:
        if preview:
            out.update(preview)
            break
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
        key = (str(order.get("deliveryDate") or ""), str(order.get("orderNumber") or ""))
        if key not in merged:
            merged[key] = dict(order, items=list(order.get("items") or []))
            continue
        into = merged[key]
        into["items"].extend(order.get("items") or [])
        groups = [g for g in (into.get("recipientGroup"), order.get("recipientGroup")) if g]
        into["recipientGroup"] = ", ".join(dict.fromkeys(groups))
    return list(merged.values())


def _ship_one(conn, cfg, message, order, matched, decisions, extracted, shadow,
              upload, post) -> tuple[str, dict]:
    """Build, send and report ONE order. Returns (status, preview)."""
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
        result["reject_reason"] = "E-mail je zmena už zadanej objednávky"
        result["change_prefix"] = edi.change_prefix(matched.ean_edi, delivery, order_no)
    elif not shipped_items:
        result["reject_reason"] = "Žiadnu položku sa nedalo priradiť ku karte"

    if result.get("reject_reason"):
        _finish(conn, cfg, message, shadow, post, status="review",
                items=result["items"], result=result)
        return "review", {}

    built = edi.build(ean=matched.ean_edi, store=matched.name, orderNumber=order_no,
                      deliveryDate=delivery, items=items)
    name = edi.filename(matched.ean_edi, delivery, order_no)
    result["edi_filename"] = name
    preview = {"edi_preview": built.content, "edi_filename": name}

    if shadow:
        # Same verdict, zero side effects: n8n still owns this message.
        return ("partial" if missing else "ok"), preview

    if not edi.claim_send(conn, matched.ean_edi, delivery, built.content, name):
        log.warning("EDI for %s / %s already sent — not uploading again",
                    matched.ean_edi, delivery)
        result["shipped"] = True
        result["reject_reason"] = ""
        _finish(conn, cfg, message, shadow, post, status="ok", items=result["items"],
                result=result, skip_post=True)
        return "ok", preview

    try:
        upload(cfg, name, built.content)
    except Exception as e:
        edi.release_send(conn, matched.ean_edi, delivery, built.content)
        log.exception("upload of %s failed", name)
        result["reject_reason"] = f"Odoslanie do ORIONu zlyhalo: {e!r}"
        _finish(conn, cfg, message, shadow, post, status="error", items=result["items"],
                result=result)
        return "error", preview

    result["shipped"] = True
    for d in shipped_items:
        memory.remember(conn, matched.ean_edi, d.item_name, d.gtin, d.card,
                        delivered_on=_delivery_day(delivery), source="ship")
    _finish(conn, cfg, message, shadow, post,
            status="partial" if missing else "ok", items=result["items"], result=result,
            detail={"edi_file": name, "orion_path": edi.orion_path(name)})
    return ("partial" if missing else "ok"), preview


def _delivery_day(delivery_date: str) -> str:
    stamp = edi._format_date(delivery_date)
    if stamp.strip():
        return f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}"
    from datetime import UTC, datetime
    return datetime.now(UTC).date().isoformat()


def _finish(conn, cfg, message, shadow, post, status: str, items: list,
            result: dict, detail: dict | None = None, skip_post: bool = False) -> dict:
    """Report + event, unless we are shadowing (then: nothing leaves this process)."""
    if not shadow:
        html = report.build(result)
        if not skip_post:
            try:
                post(cfg, html)
            except Exception:
                log.exception("posting the Odoo report failed")
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
    return result.get("reject_reason") or "Odoo kontrola (AI orders)"


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
