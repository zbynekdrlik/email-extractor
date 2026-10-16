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


# --- review round 29 ------------------------------------------------------------------------

ONE_CARD = [
    {"ean_edi": "2999000000501", "name": "Reťazec centrála", "trade_name": "",
     "city": "Mestečko", "street": "Hlavná 9", "emails": [S1, S2]},
    {"ean_edi": "2999000000502", "name": "Reťazec BISTRO", "trade_name": "",
     "city": "Mestečko", "street": "Krátka 1", "emails": []},
]


def test_a_co_address_of_the_senders_one_card_shares_its_shop_history(pg, monkeypatch):
    """Round 29: S1 and S2 sit on ONE card (no family). S1's shops were answered (rules);
    S2's BISTRO-only mail shipped to the central card — the incident misroute against a
    human rule for exactly that shop. A co-address on the sender's own card shares the
    history: the shop is asked."""
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    snapshot.import_snapshot(pg, CATALOG_CSV, BASE_CSV)
    codex_customers.replace_customers(
        pg, [dict(s, zip="", codex_id=s["ean_edi"]) for s in ONE_CARD])
    snapshot.rebuild_from_overrides(pg)
    key = store_key.for_orders([{"store": "BISTRO", "sourceFile": "OBJ Pekárová žena BI.xlsx"}])[0]
    store_rules.remember(pg, sender_email=S1, key=key, ean_edi="2999000000502",
                         question_id=1, by="sklad")
    assert order_customers.shop_history(pg, S2, None, own_cards=ONE_CARD)
    assert not order_customers.shop_history(pg, S2, None)
    stores = (("BISTRO", "BI", "rožok 50g", 120, "G50"),)
    assert _run_from(pg, "m-s2", S2, stores).uploads == []


def test_a_rule_pointing_at_a_family_card_is_the_familys_shop_history(pg):
    """Round 29 (a test the `ean_edi` clause lacked): another sender's rule naming one of the
    family's cards makes the family a shop-history sender."""
    family = [dict(c, emails=[S1]) for c in FAMILY]
    assert not order_customers.shop_history(pg, S1, family)
    key = store_key.for_orders([{"store": "Hlavná"}])[0]
    store_rules.remember(pg, sender_email="iny@zakaznik-test.sk", key=key,
                         ean_edi="2999000000301", question_id=1, by="sklad")
    assert order_customers.shop_history(pg, S1, family)


def test_a_staff_forward_question_belongs_to_its_mail_and_teaches_no_address(pg):
    """Round 29: two forwards of unknown customers by one staff address shared ONE plain
    question, and its answer put the staff address on the customer's card — every later
    forward then went to that card. Any question of our own address is keyed per mail and
    its answer teaches no address."""
    snapshot.import_snapshot(pg, CATALOG_CSV, BASE_CSV)
    for mid in ("g1", "g2"):
        pg.execute("INSERT INTO messages (message_id, category) VALUES (%s, 'ai_orders')",
                   (mid,))
    q1 = teach.ask_customer(pg, "g1", STAFF, [], "", {"sender_email": STAFF})
    q2 = teach.ask_customer(pg, "g2", STAFF, [], "", {"sender_email": STAFF})
    assert q1 and q2 and q1 != q2
    answer = order_customers.learn(pg, teach.get(pg, q1), "2999000000999", "sklad")
    assert answer["email_added"] is False
    card = next(c for c in snapshot.customers_for_management(pg)
                if c["ean_edi"] == "2999000000999")
    assert STAFF not in [e.lower() for e in card.get("emails") or []]


def test_our_own_domain_is_the_exact_domain():
    assert store_rules.is_own_address(f"jana@{store_rules.OUR_DOMAIN}")
    assert store_rules.is_own_address(f"Jana@Mail.{store_rules.OUR_DOMAIN.upper()}")
    assert not store_rules.is_own_address(f"x@not{store_rules.OUR_DOMAIN}")
    assert not store_rules.is_own_address(f"{store_rules.OUR_DOMAIN}@gmail.com")
    assert not store_rules.is_own_address(f"a@{store_rules.OUR_DOMAIN}.example.com")
    assert not store_rules.is_own_address("")


def test_a_new_customer_for_a_staff_forward_never_gets_the_staff_address(pg):
    """Round 29: „Nový zákazník" on a staff forward's question created the card WITH the
    staff address — every later forward of that address then went to the new card."""
    from test_orders_store_rules import _client

    snapshot.import_snapshot(pg, CATALOG_CSV, BASE_CSV)
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('g3', 'ai_orders')")
    qid = teach.ask_customer(pg, "g3", STAFF, [], "", {"sender_email": STAFF})
    r = _client().post(f"/api/board/questions/{qid}/answer", json={"new_customer": {
        "ean_edi": "2999000000777", "name": "Nový odberateľ", "city": "Mestečko"}})
    assert r.status_code == 200, r.get_json()
    card = next(c for c in snapshot.customers_for_management(pg)
                if c["ean_edi"] == "2999000000777")
    assert STAFF not in [e.lower() for e in card.get("emails") or []]


# --- review round 30 ------------------------------------------------------------------------

def test_a_staff_address_already_on_a_card_decides_nothing():
    """Round 30: a pre-#493 answer had put a staff address on a customer card (prod: one card
    carried it, two forwards of an unknown customer shipped to it). An address of our own
    domain is never the sender's identity: no exact-address card, no exact flag for the
    model, no CODEX-order owner."""
    from app.orders import codex_orders, customer

    cards = [{"ean_edi": "2999000000887", "name": "Kaviareň", "city": "Mestečko",
              "street": "Hlavná 1", "emails": [STAFF, "kaviaren@zakaznik-test.sk"]}]
    assert customer.owners_of(cards, STAFF) == []
    assert customer.resolve(cards, STAFF, "Jana", "") is None
    assert [c["exact_email"] for c in customer.candidates(cards, STAFF, "Jana", "")] == [False]
    assert codex_orders._owner_ean(cards, STAFF) is None
    # the customer's own address still decides, exactly as before
    assert customer.resolve(cards, "kaviaren@zakaznik-test.sk", "", "").rule == "exact_email"


def test_answers_never_write_a_staff_address_and_display_names_are_parsed(pg):
    snapshot.import_snapshot(pg, CATALOG_CSV, BASE_CSV)
    assert snapshot.remember_customer_email(pg, "2999000000999", STAFF) is False
    assert store_rules.is_own_address(f"Jana <{STAFF}>")
    assert not store_rules.is_own_address("Jana <jana@zakaznik-test.sk>")


def test_a_new_customer_never_stores_a_submitted_staff_address(pg):
    """Round 30: the „Nový zákazník" route stored any own-domain address the request listed."""
    from test_orders_store_rules import _client

    snapshot.import_snapshot(pg, CATALOG_CSV, BASE_CSV)
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('g4', 'ai_orders')")
    qid = teach.ask_customer(pg, "g4", S1, [], "", {"sender_email": S1})
    r = _client().post(f"/api/board/questions/{qid}/answer", json={"new_customer": {
        "ean_edi": "2999000000778", "name": "Nový odberateľ", "city": "Mestečko",
        "emails": f"{STAFF}, obchod@zakaznik-test.sk"}})
    assert r.status_code == 200, r.get_json()
    card = next(c for c in snapshot.customers_for_management(pg)
                if c["ean_edi"] == "2999000000778")
    emails = [e.lower() for e in card.get("emails") or []]
    assert STAFF not in emails and "obchod@zakaznik-test.sk" in emails and S1 in emails
