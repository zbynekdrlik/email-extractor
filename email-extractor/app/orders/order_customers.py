"""Who each ORDER of one mail is for — per shop, with the #493 poistka (#101/#159/#493).

`pipeline._run` used to resolve every order with `customer.resolve(store=<header>) or
<the mail-level customer>`: a mail whose orders were for several shops of one customer, with
the sender's address on ONE card, sent every shop's order to that card (the 2026-10-16
incident — 21 ORDER files of 5 shops under one EAN). This module is the ONE place the
per-order decision is made now:

1. every order gets its shop key (`store_key.for_orders`: block header + the attachment it
   was read from) and is resolved with it, the sender's learned shop rules and the
   "several shops in this mail" flag — never falling back to the mail-level customer;
2. the poistka (`customer.collapsed_shops`): different shop headers that landed on ONE card
   through a rule that never looked at the shop become unresolved again;
3. an unresolved order of a multi-shop mail (or of a sender with several cards) is asked
   PER SHOP (`ask`, dedupe key sender + shop key) — the answer teaches a shop rule
   (`learn`), so each shop is asked once.

`learn` is the ONE place a human customer answer is remembered — the board answer route,
the new-customer route and `teach.KINDS['customer'].apply` all call it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from psycopg.types.json import Json

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


def resolve_orders(conn, customers: list[dict], sender: str, extracted: dict, llm: dict,
                   orders: list[dict]) -> OrderCustomers:
    """Resolve the customer of every order (see the module docstring). Reads the sender's
    shop rules — a pure read, so it applies in shadow too, like a memory lookup."""
    keys = store_key.for_orders(orders)
    multi = store_key.is_multi_store(keys)
    rules = store_rules.for_sender(conn, sender)
    matched = [customer.resolve(customers, sender, extracted.get("senderName", ""),
                                extracted.get("companyName", ""), llm=llm,
                                store=k.header, key=k, rules=rules, multi_store=multi,
                                delivery_text=extracted.get("notes", ""))
               for k in keys]
    collapsed = customer.collapsed_shops(keys, matched)
    for i in collapsed:
        matched[i] = None
    family = customer.owner_count(customers, sender) >= 2
    scoped = [bool(k.key) and (multi or family) for k in keys]
    for i, (k, m) in enumerate(zip(keys, matched, strict=True)):
        log.info("order %d/%d shop %r (file %r, key %r) -> %s (%s)%s", i + 1, len(keys),
                 k.header, k.source_file, k.text, m.ean_edi if m else None,
                 m.rule if m else "asked", " [poistka]" if i in collapsed else "")
    return OrderCustomers(keys=keys, matched=matched, scoped=scoped, collapsed=collapsed)


def ask(conn, *, message: dict, sender: str, extracted: dict, customers: list[dict],
        free_text: str, order: dict, key, scoped: bool, on_new=None) -> int | None:
    """Raise (or reuse) the `customer` question for an unresolved order — per SHOP when
    `scoped` (dedupe sender + shop key), else per sender exactly as before (#159)."""
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
                 "delivery_address_guess": customer.guess_delivery_address(free_text)},
        on_new=on_new, key=shop)


def learn(conn, q: dict, ean_edi: str, by: str, email_added: bool | None = None) -> dict:
    """Remember a HUMAN answer to a `customer` question: the sender address on the chosen
    card (#159) and — for a question asked for one shop — the shop rule (#493). Records on
    the question what it taught (`answer`), so undo takes back exactly that and nothing
    else (an address that was already on the card is never forgotten by an undo).
    Rebuilds the snapshot when the card list changed. Returns the recorded `answer`.
    `email_added` overrides the flag when the caller itself wrote the address (the
    „Nový zákazník" card is CREATED with it by the same answer)."""
    ctx = q.get("context") or {}
    sender = str(ctx.get("sender_email") or "")
    added = snapshot.remember_customer_email(conn, ean_edi, sender)
    if email_added is None:
        email_added = added
    rule_id = None
    key = store_key.from_context(ctx)
    if key is not None:
        rule_id = store_rules.remember(conn, sender_email=sender, key=key, ean_edi=ean_edi,
                                       question_id=q["id"], by=by)
    answer = {"email_added": bool(email_added), "store_rule": rule_id}
    conn.execute("UPDATE order_questions SET answer = %s WHERE id = %s",
                 (Json(answer), q["id"]))
    if added:
        snapshot.rebuild_from_overrides(conn)
    log.info("customer question %s taught: card %s, address added=%s, shop rule=%s (by %s)",
             q["id"], ean_edi, email_added, rule_id, by)
    return answer
