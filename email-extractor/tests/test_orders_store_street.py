"""#493 review round 25 — the strict street rung (`customer._by_street_strict`) in a shop
context, and the poistka guard against two shops landing on one card by street.

Synthetic streets / cards only."""
from app.orders import customer, store_key

SENDER = "nakup@retazec-test.sk"


def _family(*cards):
    """cards = (street, city) pairs -> family cards 2999000000801, 802, ..."""
    return [{"ean_edi": f"299900000080{i}", "name": f"Predajňa {i}", "emails": [SENDER],
             "city": city, "street": street, "zip": ""}
            for i, (street, city) in enumerate(cards, 1)]


def _streets(*streets):
    return _family(*[(s, "Mestečko") for s in streets])


def _hit(fam, header):
    key = store_key.for_orders([{"store": header}, {"store": "Iná predajňa"}])[0]
    hit = customer.resolve(fam, SENDER, "", "", key=key, multi_store=True)
    return (hit.ean_edi, hit.rule) if hit else None


def test_another_number_in_the_header_is_no_house_number_of_the_card():
    """A shop number, a floor or a code written apart is no proof of the card's house number
    (round 25: „Predajňa 2, Hlavná 14" shipped to the „Hlavná 2" card). The header's house
    numbers must be exactly the card's, its street written as one run („Hlavná 2")."""
    assert _hit(_streets("Hlavná 2", "Mierová 3"), "Predajňa 2, Hlavná 14") is None
    assert _hit(_streets("Hlavná 1", "Mierová 3"), "BISTRO 1 Hlavná") is None
    assert _hit(_streets("Hlavná 1", "Mierová 3"), "GT 1 Hlavná") is None
    assert _hit(_streets("Hlavná 2", "Mierová 3"), "OC Galéria 2. poschodie, Hlavná 14") is None
    assert _hit(_streets("Hlavná 2", "Mierová 3"), "Predajňa 2: Hlavná") is None
    # the card's own street and number still decide
    assert _hit(_streets("Hlavná 2", "Mierová 3"), "BISTRO Hlavná 2") == (
        "2999000000801", "store_address")
    assert _hit(_streets("M.R. Štefánika 836/33", "Hlavná 7"), "BISTRO Štefánika 836/33") == (
        "2999000000801", "store_address")


def test_a_card_sharing_any_street_word_blocks():
    """„ul. Hlavná 14" / „Nám. SNP 5" carry a word the header does not, yet they are on the
    header's street (round 25: such a card did not block, „Námestie SNP 5" shipped to the
    „SNP 5" card of another town). Any card sharing a street word blocks — asked."""
    assert _hit(_streets("Nám. SNP 5", "SNP 5"), "Námestie SNP 5") is None
    assert _hit(_streets("Hlavná 2", "Mierová 3", "ul. Hlavná 14"), "BISTRO Hlavná 2") is None
    assert _hit(_streets("Hlavná ulica 14", "Hlavná 2"), "Predajňa Hlavná 2") is None


def test_a_header_naming_another_family_town_decides_nothing():
    """„Košice, Hlavná 5" is not the Žilina card on „Hlavná 5" when the family has a shop in
    Košice (round 25); the card's own town in the header is fine."""
    fam = _family(("Hlavná 5", "Žilina"), ("Mierová 3", "Košice"))
    assert _hit(fam, "Košice, Hlavná 5") is None
    assert _hit(fam, "Žilina, Hlavná 5") == ("2999000000801", "store_address")


def test_the_101_shape_still_ships():
    fam = _family(("Družby 35", "Banská Bystrica"), ("29. augusta 19", "Banská Bystrica"))
    assert _hit(fam, "GT1- Družby 35 BB") == ("2999000000801", "store_address")
    assert _hit(fam, "GT2- 29 augusta 19 BB") == ("2999000000802", "store_address")


def _hit_file(fam, header, file):
    key = store_key.for_orders([{"store": header, "sourceFile": file},
                                {"store": "Iná predajňa"}])[0]
    hit = customer.resolve(fam, SENDER, "", "", key=key, multi_store=True)
    return (hit.ean_edi, hit.rule) if hit else None


def test_another_family_town_in_the_header_or_the_file_decides_nothing():
    """Round 26: the town check needed the WHOLE city in the header and read no file name —
    „Košice" of „Košice - Sever", „BB" of „Banská Bystrica", a town only in „OBJ Martin.xlsx"
    all shipped the shop to the Žilina card on the same street."""
    for town, header in (("Košice - Sever", "Košice, Hlavná 5"),
                         ("Bratislava - Ružinov", "Bratislava, Hlavná 5"),
                         ("Banská Bystrica", "Hlavná 5 BB"),
                         ("Banská Bystrica", "Hlavná 5, B. Bystrica")):
        fam = _family(("Hlavná 5", "Žilina"), ("Mierová 3", town))
        assert _hit(fam, header) is None, (town, header)
    fam = _family(("Hlavná 5", "Žilina"), ("Mierová 3", "Martin"))
    assert _hit_file(fam, "Hlavná 5", "OBJ Martin.xlsx") is None
    assert _hit_file(fam, "Hlavná 5", "OBJ Žilina.xlsx") == ("2999000000801", "store_address")


def test_a_sender_with_learned_shop_rules_never_ships_by_the_street():
    """Round 26: a learned rule for shop A on the „Hlavná" card, then shop B by the street
    onto the same card — B's own question was still open. A sender with rules ships a shop
    only by ITS rule; every other shop is asked once."""
    fam = _streets("Hlavná", "Mierová 3")
    rules = [(frozenset({"a", "hlavna"}), "2999000000801")]
    key = store_key.for_orders([{"store": "B Hlavná"}, {"store": "A Hlavná"}])[0]
    assert customer.resolve(fam, SENDER, "", "", key=key, rules=rules) is None
    key = store_key.for_orders([{"store": "Mierová 3"}])[0]
    assert customer.resolve(fam, SENDER, "", "", key=key, rules=rules) is None


def _matched(ean, rule="store_address"):
    return customer.Matched(ean_edi=ean, name="", confidence=0.9, rule=rule, note="")


GT = frozenset({"2999000000801", "2999000000802"})


def test_the_street_ships_only_when_the_mail_fits_the_family_exactly():
    """Round 26 (the cause): in a mail of several shops the street decides only when EVERY
    shop of the mail is on exactly one card, no two shops share a card and every family card
    is taken — the #101 GT1 / GT2 shape. Anything else asks every shop of the mail."""
    gt = store_key.for_orders([{"store": "GT1- Družby 35 BB", "sourceFile": "cykl GT.xlsx"},
                               {"store": "GT2- 29 augusta 19 BB", "sourceFile": "cykl GT.xlsx"},
                               {"store": "GT1- Družby 35 BB", "sourceFile": "cykl GT.xlsx"}])
    both = [_matched("2999000000801"), _matched("2999000000802"), _matched("2999000000801")]
    assert customer.collapsed_shops(gt, both, multi=True, family=GT) == set()
    # a third family card no shop of the mail is on
    assert customer.collapsed_shops(gt, both, multi=True,
                                    family=GT | {"2999000000803"}) == {0, 1, 2}
    # one shop of the mail decided by nothing
    assert customer.collapsed_shops(
        gt, [_matched("2999000000801"), None, _matched("2999000000801")], multi=True,
        family=GT) == {0, 2}


def test_two_shops_landing_on_one_card_by_street_are_asked():
    """Two DIFFERENT shops of one mail decided by the street onto ONE card contradict each
    other — every shop of the mail is asked (round 25: „A Hlavná" + „B Hlavná" both shipped
    to the numberless „Hlavná" card as two ORDER files)."""
    keys = store_key.for_orders([{"store": "A Hlavná"}, {"store": "B Hlavná"},
                                 {"store": "Mierová 3"}])
    matched = [_matched("2999000000801"), _matched("2999000000801"),
               _matched("2999000000802")]
    assert customer.collapsed_shops(keys, matched, multi=True, family=GT) == {0, 1, 2}


def test_one_header_in_two_files_is_two_shops():
    """Round 26: „Predajňa Hlavná" in „OBJ Žilina.xlsx" and in „OBJ Martin.xlsx" are two shops
    (the file is part of the identity) — both on one card is a contradiction, asked."""
    keys = store_key.for_orders([{"store": "Predajňa Hlavná", "sourceFile": "OBJ Žilina.xlsx"},
                                 {"store": "Predajňa Hlavná", "sourceFile": "OBJ Martin.xlsx"}])
    matched = [_matched("2999000000801"), _matched("2999000000801")]
    assert customer.collapsed_shops(keys, matched, multi=True, family=GT) == {0, 1}


def test_one_shop_on_two_days_is_one_shop():
    keys = store_key.for_orders([
        {"store": "GT1- Družby 35 BB", "sourceFile": "cykl GT.xlsx"},
        {"store": "GT1- Družby 35 BB 17.10.", "sourceFile": "cykl GT 2.xlsx"},
        {"store": "GT2- 29 augusta 19 BB", "sourceFile": "cykl GT.xlsx"}])
    matched = [_matched("2999000000801"), _matched("2999000000801"),
               _matched("2999000000802")]
    assert customer.collapsed_shops(keys, matched, multi=True, family=GT) == set()


# --- the worker sweep (DB) ------------------------------------------------------------------

STREET_FAMILY = [
    {"ean_edi": "2999000000301", "name": "Reťazec Hlavná", "trade_name": "",
     "city": "Mestečko", "street": "Hlavná"},
    {"ean_edi": "2999000000302", "name": "Reťazec Mierová", "trade_name": "",
     "city": "Mestečko", "street": "Mierová 3"},
]
STREET_STORES = (("A Hlavná", "AH", "rožok 50g", 120, "G50"),
                 ("B Hlavná", "BH", "vianočka 400g", 7, "VIA"),
                 ("Mierová 3", "MI", "rožok kváskový 70g", 5, "G70"))


def test_the_worker_sweep_never_answers_a_shop_question_by_the_street(pg, monkeypatch):
    """Round 26: the sweep re-resolved each asked shop ALONE and shipped „A Hlavná" and „B
    Hlavná" to the one „Hlavná" card the mail-level check had refused. A shop question is
    settled automatically only by a learned rule — the street never answers it."""
    from test_orders_store_rules import BASE_CSV, CATALOG_CSV, _cfg, _run, _shop_questions

    from app.orders import codex_customers, hold, snapshot

    uploads = []
    monkeypatch.setattr("app.orders.upload.put",
                        lambda cfg, name, content: uploads.append(name) or True)
    snapshot.import_snapshot(pg, CATALOG_CSV, BASE_CSV)
    codex_customers.replace_customers(
        pg, [dict(s, emails=[SENDER], zip="", codex_id=s["ean_edi"]) for s in STREET_FAMILY])
    snapshot.rebuild_from_overrides(pg)
    result, rec = _run(pg, "m-street", stores=STREET_STORES)
    assert rec.uploads == []
    assert {"A Hlavná", "B Hlavná", "Mierová 3"} <= set(_shop_questions(pg))
    assert hold.retry_unknown_customer_questions(pg, _cfg()) == []
    assert uploads == []
    assert {"A Hlavná", "B Hlavná", "Mierová 3"} <= set(_shop_questions(pg))
