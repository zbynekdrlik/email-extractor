"""Who each ORDER of one mail is for — per shop, with the #493 poistka (#101/#159/#493).

`pipeline._run` used to resolve every order with `customer.resolve(store=<header>) or
<the mail-level customer>`: a mail whose orders were for several shops of one customer, with
the sender's address on ONE card, sent every shop's order to that card (the 2026-10-16
incident — 21 ORDER files of 5 shops under one EAN). This module is the ONE place the
per-order decision is made now:

1. every order gets its shop key (`store_key.for_orders`: block header + the attachment it
   was read from); whether the mail carries several shops is decided once
   (`store_key.is_multi_store` — a header block, or a sender known to have several shops);
2. one delivery day + order number is ONE order (#81.1, `merge_orders`) — per SHOP in a
   multi-shop mail (two shops are never merged, two files of one shop are), per (day, order
   number, header) otherwise, exactly as before #493;
3. every order is resolved with its key, the sender's learned shop rules and the multi-shop
   flag — never falling back to the mail-level customer; then orders of one day that
   resolved to the SAME card (and header) are one order again (`merge_by_card`);
4. the poistka (`customer.collapsed_shops`): in a multi-shop mail only a SHOP rung may
   decide a shop — anything else is asked;
5. an unresolved order is asked PER SHOP (`ask`, dedupe key sender + shop key) whenever the
   mail is multi-shop or the sender is a known multi-shop sender — the answer teaches a shop
   rule (`learn`), so each shop is asked once.

`learn` is the ONE place a human customer answer is remembered — the board answer route,
the new-customer route and `teach.KINDS['customer'].apply` all call it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import customer, snapshot, store_key, store_rules, teach

log = logging.getLogger("orders.order_customers")


def placeholder() -> customer.Matched:
    """The #431 hold placeholder — `hold.place` never receives `matched=None`."""
    return customer.Matched(ean_edi="", name="", confidence=0.0, rule="unmatched", note="")


@dataclass
class OrderCustomers:
    keys: list = field(default_factory=list)       # store_key.StoreKey per order
    matched: list = field(default_factory=list)    # customer.Matched | None per order
    scoped: list = field(default_factory=list)     # ask THIS shop (not the sender)?
    collapsed: set = field(default_factory=set)    # indexes the poistka turned back
    # a multi-shop mail / sender: a per-SENDER question here must not teach the address
    shop_context: bool = False


def resolve_orders(conn, customers: list[dict], sender: str, extracted: dict, llm: dict,
                   orders: list[dict]) -> tuple[list[dict], OrderCustomers]:
    """Merge the orders of one delivery day and resolve the customer of each (see the module
    docstring). Returns the (merged) orders + their customers. Reads the sender's shop rules
    — a pure read, so it applies in shadow too, like a memory lookup."""
    rules = store_rules.for_sender(conn, sender)
    shop_sender = bool(rules) or customer.owner_count(customers, sender) >= 2
    multi = store_key.is_multi_store(store_key.for_orders(orders), shop_sender)
    by_day = (merge_orders(orders, lambda o: (_day(o), _shop(o))) if multi
              else merge_by_day(orders))
    keys = store_key.for_orders(by_day)
    matched = [customer.resolve(customers, sender, extracted.get("senderName", ""),
                                extracted.get("companyName", ""), llm=llm,
                                store=k.header, key=k, rules=rules, multi_store=multi,
                                delivery_text=extracted.get("notes", ""))
               for k in keys]
    collapsed = customer.collapsed_shops(keys, matched, multi)
    for i in collapsed:
        matched[i] = None
    scoped = [bool(k.key) and (multi or shop_sender) for k in keys]
    oc = OrderCustomers(keys=keys, matched=matched, scoped=scoped, collapsed=collapsed,
                        shop_context=multi or shop_sender)
    for i, (k, m) in enumerate(zip(keys, matched, strict=True)):
        log.info("order %d/%d shop %r (file %r, key %r) -> %s (%s)%s%s", i + 1, len(keys),
                 k.header, k.source_file, k.text, m.ean_edi if m else None,
                 m.rule if m else "asked", " [poistka]" if i in collapsed else "",
                 " [multi-shop]" if multi else "")
    return merge_by_card(by_day, oc)


def _day(order: dict) -> tuple:
    return (str(order.get("deliveryDate") or ""), str(order.get("orderNumber") or ""))


def _shop(order: dict) -> frozenset:
    """The order's shop identity (header + file words — independent of the other orders)."""
    return store_key.for_orders([order])[0].identity


def merge_by_day(orders: list[dict]) -> list[dict]:
    """One delivery day + order number + shop header is ONE order (#81.1, #101) — the merge
    of a mail that does NOT carry several shops, exactly as before #493 (`pipeline.
    _merge_by_day`). A mail of several shops merges per shop identity instead."""
    return merge_orders(orders, lambda o: (_day(o), str(o.get("store") or "")))


def merge_orders(orders: list[dict], key) -> list[dict]:
    """Merge the orders sharing `key(order)` — items concatenated in order, recipient groups
    joined (the #81.1 shape `pipeline._merge_by_day` always had)."""
    merged: dict = {}
    for order in orders:
        k = key(order)
        if k not in merged:
            merged[k] = dict(order, items=list(order.get("items") or []))
            continue
        into = merged[k]
        into["items"].extend(order.get("items") or [])
        groups = [g for g in (into.get("recipientGroup"), order.get("recipientGroup")) if g]
        into["recipientGroup"] = ", ".join(dict.fromkeys(groups))
    return list(merged.values())


def merge_by_card(orders: list[dict], oc: OrderCustomers) -> tuple[list[dict], OrderCustomers]:
    """#81.1 after #493: orders of one delivery day + order number + header that resolved to
    the SAME card are ONE order (two files of one shop must not become two ORDER files for
    one day). Two different headers are two shops (#101) and stay apart; unresolved orders
    are never merged — each is asked for its own shop. The first order's shop key / match
    carries the merged one."""
    groups: dict = {}
    for i, (o, m) in enumerate(zip(orders, oc.matched, strict=True)):
        k = ((*_day(o), str(o.get("store") or ""), m.ean_edi)
             if m is not None and m.ean_edi else ("unresolved", i))
        groups.setdefault(k, []).append(i)
    if all(len(idxs) == 1 for idxs in groups.values()):
        return orders, oc
    keep = [idxs[0] for idxs in groups.values()]
    out_orders = []
    for idxs in groups.values():
        out_orders.append(merge_orders([orders[i] for i in idxs], lambda o: 0)[0])
        if len(idxs) > 1:
            log.info("orders %s of one day resolved to one card %s — merged into one",
                     [i + 1 for i in idxs], oc.matched[idxs[0]].ean_edi)
    return out_orders, OrderCustomers(
        keys=[oc.keys[i] for i in keep], matched=[oc.matched[i] for i in keep],
        scoped=[oc.scoped[i] for i in keep],
        collapsed={keep.index(i) for i in oc.collapsed if i in keep},
        shop_context=oc.shop_context)


def ask(conn, *, message: dict, sender: str, extracted: dict, customers: list[dict],
        free_text: str, order: dict, key, scoped: bool, on_new=None,
        shop_context: bool = False) -> int | None:
    """Raise (or reuse) the `customer` question for an unresolved order — per SHOP when
    `scoped` (dedupe sender + shop key), else per sender exactly as before (#159). A
    per-sender question in a `shop_context` (a multi-shop mail / sender) records
    `no_address`: its answer must not put the address on a shop's card (`learn`)."""
    shop = key if scoped else None
    cands = customer.candidates_for_question(
        customers, sender, extracted.get("senderName", ""), extracted.get("companyName", ""),
        free_text=free_text, key=shop)
    return teach.ask_customer(
        conn, message_id=message.get("message_id", ""), sender_email=sender,
        candidates=[{"ean_edi": c.get("ean_edi", ""), "name": c.get("name", ""),
                     "city": c.get("city", ""), "street": c.get("street", ""),
                     "address_match": bool(c.get("address_match"))} for c in cands],
        delivery_date=order.get("deliveryDate", ""),
        context={"sender_email": sender, "sender_name": extracted.get("senderName", ""),
                 "company_name": extracted.get("companyName", ""),
                 "delivery_address_guess": customer.guess_delivery_address(free_text),
                 **({"no_address": True} if shop_context else {})},
        on_new=on_new, key=shop)


def learn(conn, q: dict, ean_edi: str, by: str, email_added: bool | None = None) -> dict:
    """Remember a HUMAN answer to a `customer` question: for a question asked for ONE SHOP the
    shop rule only (#493 — the address is NOT added: every answered shop's card would make
    the sender a family whose mails with no shop clue are then asked every time); for a
    per-sender question the sender address on the chosen card (#159). Records on the
    question what it taught (`answer`), so undo takes back exactly that and nothing else.
    Rebuilds the snapshot when the card list changed. Returns the recorded `answer`.
    `email_added` overrides the flag when the caller itself wrote the address (the
    „Nový zákazník" card is CREATED with it by the same answer)."""
    ctx = q.get("context") or {}
    sender = str(ctx.get("sender_email") or "")
    key = store_key.from_context(ctx)
    rule_id, added = None, False
    if key is not None or ctx.get("no_address"):
        # review round 1: a SHOP answer teaches the shop rule only — adding the sender's
        # address to every answered shop's card would make it a family whose mails with no
        # shop clue are then asked every single time
        if key is not None:
            rule_id = store_rules.remember(conn, sender_email=sender, key=key,
                                           ean_edi=ean_edi, question_id=q["id"], by=by)
    else:
        added = snapshot.remember_customer_email(conn, ean_edi, sender)
    if email_added is None:
        email_added = added
    answer = {"email_added": bool(email_added), "store_rule": rule_id}
    teach.set_answer(conn, q["id"], answer)
    if added:
        snapshot.rebuild_from_overrides(conn)
    log.info("customer question %s taught: card %s, address added=%s, shop rule=%s (by %s)",
             q["id"], ean_edi, email_added, rule_id, by)
    return answer


def unlearn(conn, q: dict) -> None:
    """Take back what `learn` taught for an answered `customer` question (`teach.undo`): the
    sender address only when THIS answer added it (`answer.email_added`) — an address that
    already sat on the card (a CODEX one, a family's) must survive the undo; an answer
    recorded before #493 carries no flag and keeps the old forget — and the question's shop
    rules (soft delete, audited)."""
    if (q.get("answer") or {}).get("email_added", True):
        snapshot.forget_customer_email(conn, q["answer_gtin"],
                                       (q.get("context") or {}).get("sender_email", ""))
        snapshot.rebuild_from_overrides(conn)
    store_rules.forget_for_question(conn, q["id"])


def shop_question(key: str, sender_email: str, shop, context: dict) -> tuple:
    """The per-SHOP shape of a `customer` question (`teach.ask_customer(key=)`): dedupe key
    sender + shop key, the shop in the title (the board card shows only `wording` + the
    candidates), the key in `context` so the answer can teach the shop rule. Returns
    (item_key, wording, reason, context)."""
    return (f"{key}|shop:{shop.text}", f"{sender_email} · predajňa {shop.label}",
            (f"Odosielateľ objednáva pre viac predajní — ku ktorej karte patrí predajňa "
             f"{shop.label}? Odpoveď si zapamätám pre ďalšie objednávky tejto predajne."),
            dict(context or {}, store=shop.header, store_file=shop.source_file,
                 store_key=shop.text, store_tokens=sorted(shop.tokens),
                 store_label=shop.label))

