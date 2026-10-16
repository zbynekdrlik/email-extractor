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


def _matched(ean, rule="store_address"):
    return customer.Matched(ean_edi=ean, name="", confidence=0.9, rule=rule, note="")


def test_two_shops_landing_on_one_card_by_street_are_asked():
    """Two DIFFERENT shops of one mail decided by the street onto ONE card contradict each
    other — both are asked (round 25: „A Hlavná" + „B Hlavná" both shipped to the numberless
    „Hlavná" card as two ORDER files). A learned rule is the warehouse's own decision."""
    keys = store_key.for_orders([{"store": "A Hlavná"}, {"store": "B Hlavná"},
                                 {"store": "Mierová 3"}])
    matched = [_matched("2999000000801"), _matched("2999000000801"),
               _matched("2999000000802")]
    assert customer.collapsed_shops(keys, matched, multi=True) == {0, 1}
    ruled = [_matched("2999000000801", "store_rule"), _matched("2999000000801", "store_rule"),
             _matched("2999000000802")]
    assert customer.collapsed_shops(keys, ruled, multi=True) == set()


def test_one_shop_on_two_days_or_in_two_files_is_no_collision():
    keys = store_key.for_orders([
        {"store": "GT1- Družby 35 BB", "sourceFile": "cykl GT.xlsx"},
        {"store": "GT1- Družby 35 BB 17.10.", "sourceFile": "cykl GT 2.xlsx"},
        {"store": "GT2- 29 augusta 19 BB", "sourceFile": "cykl GT.xlsx"}])
    matched = [_matched("2999000000801"), _matched("2999000000801"),
               _matched("2999000000802")]
    assert customer.collapsed_shops(keys, matched, multi=True) == set()
