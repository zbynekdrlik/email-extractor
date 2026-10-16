"""#493 review round 28 — a sender's SHOP HISTORY (learned rules, open shop questions) is the
family's, never one address's; an address of our own domain (a staff forward) never owns
shop rules or a shared shop question. Synthetic data only."""
from test_orders_store_rules import (
    BASE_CSV,
    CATALOG_CSV,
    Recorder,
    ScriptedClient,
    _cfg,
    _shop_questions,
)

from app.orders import (
    codex_customers,
    order_customers,
    pipeline,
    snapshot,
    store_key,
    store_rules,
    teach,
)

S1 = "nakup@retazec-test.sk"
S2 = "objednavky@retazec-test.sk"
STAFF = f"jana@{store_rules.OUR_DOMAIN}"
FAMILY = [
    {"ean_edi": "2999000000301", "name": "Reťazec Hlavná", "trade_name": "",
     "city": "Mestečko", "street": "Hlavná"},
    {"ean_edi": "2999000000302", "name": "Reťazec Mierová", "trade_name": "",
     "city": "Mestečko", "street": "Mierová 3"},
]
STORES = (("A Hlavná", "AH", "rožok 50g", 120, "G50"),
          ("B Hlavná", "BH", "vianočka 400g", 7, "VIA"),
          ("Mierová 3", "MI", "rožok kváskový 70g", 5, "G70"))


def _env(pg, emails=(S1, S2)):
    snapshot.import_snapshot(pg, CATALOG_CSV, BASE_CSV)
    codex_customers.replace_customers(
        pg, [dict(s, emails=list(emails), zip="", codex_id=s["ean_edi"]) for s in FAMILY])
    snapshot.rebuild_from_overrides(pg)


def _run_from(pg, mid, sender, stores):
    parts = ["Subject: objednávka\n\nFrom: " + sender + "\n\nBody: Potvrďte príjem.\n\n"
             "Attachments:\n"]
    for header, code, item, qty, _gtin in stores:
        parts.append(f"===== OBJ Pekárová žena {code}.xlsx =====\n[Sheet: H]\n{header}\n"
                     f"na 04.08.2026\n{qty}x {item}\n")
    mail = {"message_id": mid, "subject": "objednávka", "from_addr": sender,
            "from_name": "Nákup", "combined_text": "".join(parts), "today": "2026-07-30"}
    orders = [{"orderNumber": "", "deliveryDate": "04.08.2026", "recipientGroup": "",
               "store": header, "items": [{"name": item, "quantity": qty, "unit": "ks",
                                           "sourceQuote": f"{qty}x {item}"}]}
              for header, _c, item, qty, _g in stores]
    answers = [{"senderName": "Nákup", "senderEmail": sender, "companyName": "Reťazec",
                "isChangeRequest": False, "notes": "", "orders": orders},
               {"ean_edi": "", "confidence": 0.2}]
    answers += [{"gtin": g, "confidence": 0.95, "matchedCatalogName": "", "reason": ""}
                for *_r, g in stores]
    pg.execute("INSERT INTO messages (message_id, category) VALUES (%s, 'ai_orders')", (mid,))
    rec = Recorder()
    pipeline.run(pg, _cfg(), mail, snapshot.latest_snapshot_id(pg),
                 client=ScriptedClient(answers), upload=rec.upload, post=rec.post)
    return rec


def test_shop_history_of_one_family_address_holds_for_its_other_address(pg, monkeypatch):
    """Round 28: the cards carry two addresses of the chain. Mail 1 (S1) asked „A Hlavná" /
    „B Hlavná" (one card); mail 2 from S2 with „B Hlavná" + „Mierová 3" fitted the family and
    shipped B by the street while B's question was open — the history was keyed on S1 only."""
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    _env(pg)
    assert _run_from(pg, "m1", S1, STORES).uploads == []
    assert _run_from(pg, "m2", S2, STORES[1:]).uploads == []


def test_a_rule_of_the_other_family_address_is_shop_history_too(pg):
    _env(pg)
    key = store_key.for_orders([{"store": "B Hlavná"}])[0]
    store_rules.remember(pg, sender_email=S1, key=key, ean_edi="2999000000999",
                         question_id=1, by="sklad")
    family = [dict(c, emails=[S1, S2]) for c in FAMILY]
    assert order_customers.shop_history(pg, S2, family)
    assert not order_customers.shop_history(pg, "iny@zakaznik-test.sk", None)


def test_our_own_domain_never_owns_shop_rules_or_a_shared_shop_question(pg):
    """Round 28: a staff forward of an unknown customer's mail keeps the staff address as the
    sender — rules and the shop question keyed on it were shared by every customer the staff
    forwards („Predajňa 1" of one customer shipped another's order). Our own address teaches
    no rule and its shop question belongs to its mail."""
    key = store_key.for_orders([{"store": "Predajňa 1", "sourceFile": "Objednávka.xlsx"}])[0]
    assert store_rules.remember(pg, sender_email=STAFF, key=key, ean_edi="2999000000401",
                                question_id=1, by="sklad") is None
    assert store_rules.for_sender(pg, STAFF) == []
    for mid in ("f1", "f2"):
        pg.execute("INSERT INTO messages (message_id, category) VALUES (%s, 'ai_orders')",
                   (mid,))
    q1 = teach.ask_customer(pg, "f1", STAFF, [], "", {"sender_email": STAFF}, key=key)
    q2 = teach.ask_customer(pg, "f2", STAFF, [], "", {"sender_email": STAFF}, key=key)
    assert q1 and q2 and q1 != q2, "one shop question per forwarded mail"
    assert not order_customers.shop_history(pg, STAFF, None)


def test_an_unnamed_order_question_is_no_shop_history(pg):
    """Round 28: the open per-order questions of a mail with blank „Predajňa:" forms kept an
    ordinary one-card sender in shop mode (each new mail raised another). Only a SHOP
    question (`|shop:`) is shop history."""
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('u1', 'ai_orders')")
    teach.ask_customer(pg, "u1", S1, [], "", {"sender_email": S1, "no_address": True,
                                              "slot": 0})
    assert not order_customers.shop_history(pg, S1, None)


def test_an_open_shop_question_matches_its_exact_sender(pg):
    key = store_key.for_orders([{"store": "B Hlavná"}])[0]
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('q1', 'ai_orders')")
    teach.ask_customer(pg, "q1", "a_b@x.sk", [], "", {"sender_email": "a_b@x.sk"}, key=key)
    assert order_customers.shop_history(pg, "A_B@x.sk", None)
    assert not order_customers.shop_history(pg, "a_b@x.sk.evil", None)
    assert not order_customers.shop_history(pg, "aab@x.sk", None), "no LIKE wildcard"
    pg.execute("UPDATE order_questions SET status = 'answered'")
    assert not order_customers.shop_history(pg, "a_b@x.sk", None)


def test_the_shop_questions_of_two_mails_from_one_sender_stay_one_question(pg, monkeypatch):
    """The per-shop dedupe of an ordinary sender is unchanged: one question per shop."""
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    _env(pg)
    _run_from(pg, "m1", S1, STORES)
    _run_from(pg, "m2", S1, STORES)
    assert sorted(_shop_questions(pg)) == ["A Hlavná", "B Hlavná", "Mierová 3"]
