"""#493 — the evaluation harness scores a mail of SEVERAL SHOPS per shop.

The #188 standing rule needs a corpus case for the 2026-10-16 incident mail (five shops, one
attachment each, several delivery days). The per-date shape cannot score it: two shops'
orders of one day are two correct ORDER files, and the incident itself — every shop's order
on ONE card — is invisible without the card. `shop_orders` matches each shipped order by
(delivery date, customer card). Synthetic values only.
"""
from app.orders import evaluate

SHOPS = {
    "customer_ean": "",
    "shop_orders": [
        {"delivery_date": "04.08.2026", "customer_ean": "2999000000204", "item_count": 2},
        {"delivery_date": "04.08.2026", "customer_ean": "2999000000202"},
        {"delivery_date": "05.08.2026", "customer_ean": "2999000000204"},
    ],
    "unshipped": 2,
}


def _order(day, ean, n=1):
    return {"delivery_date": day, "customer_ean": ean, "status": "ok",
            "items": [{"gtin": f"G{i}", "quantity": 1} for i in range(n)]}


def _actual(orders=None, unshipped=2):
    if orders is None:
        orders = [_order("04.08.2026", "2999000000204", 2),
                  _order("04.08.2026", "2999000000202"),
                  _order("2026-08-05", "2999000000204")]
    return {"customer_ean": "", "delivery_date": "", "items": [], "order_results": orders,
            "shipped": bool(orders), "notes": "", "unshipped": unshipped}


def test_each_shop_on_its_own_card_passes():
    s = evaluate.score(SHOPS, _actual())
    assert s.passed is True, s.problems
    assert s.item_recall == 1.0


def test_a_shop_shipped_to_another_shops_card_fails_naming_both():
    orders = _actual()["order_results"]
    orders[1] = _order("04.08.2026", "2999000000203")
    s = evaluate.score(SHOPS, _actual(orders))
    assert s.passed is False
    assert any("chýba" in p and "2999000000202" in p for p in s.problems)
    assert any("navyše" in p and "2999000000203" in p for p in s.problems)


def test_two_shops_collapsed_onto_one_card_fail():
    """The incident: the second shop's order of the day went to the first shop's card."""
    orders = _actual()["order_results"]
    orders[1] = _order("04.08.2026", "2999000000204")
    s = evaluate.score(SHOPS, _actual(orders))
    assert s.passed is False
    assert any("dvakrát" in p for p in s.problems)
    assert any("chýba" in p and "2999000000202" in p for p in s.problems)


def test_a_wrong_number_of_lines_fails():
    orders = _actual()["order_results"]
    orders[0] = _order("04.08.2026", "2999000000204", 3)
    s = evaluate.score(SHOPS, _actual(orders))
    assert s.passed is False
    assert any("položiek" in p for p in s.problems)


def test_a_wrong_number_of_held_orders_fails():
    s = evaluate.score(SHOPS, _actual(unshipped=0))
    assert s.passed is False
    assert any("nezaslan" in p for p in s.problems)


def test_the_run_result_carries_the_card_of_each_shipped_order_and_counts_the_rest():
    run = {"status": "held", "customer_ean": "2999000000101", "items": [],
           "order_results": [
               {"delivery_date": "04.08.2026", "status": "ok", "customer_ean": "2999000000204",
                "items": [{"gtin": "G50", "quantity": 10}]},
               {"delivery_date": "04.08.2026", "status": "review", "customer_ean": "",
                "items": [{"gtin": "VIA", "quantity": 1}]},
               {"delivery_date": "05.08.2026", "status": "held", "customer_ean": "",
                "items": []}]}
    actual = evaluate._actual_from_run(run)
    assert [(o["delivery_date"], o["customer_ean"]) for o in actual["order_results"]] == [
        ("04.08.2026", "2999000000204")]
    assert actual["unshipped"] == 2


def test_the_shop_shape_never_changes_the_other_shapes():
    """`unshipped` / a card per order are new keys of the actual result — the per-date and the
    flat shapes ignore them."""
    flat = {"customer_ean": "X", "delivery_date": "04.08.2026",
            "items": [{"gtin": "G50", "quantity": 1}]}
    actual = {"customer_ean": "X", "delivery_date": "04.08.2026",
              "items": [{"gtin": "G50", "quantity": 1}], "order_results": [],
              "shipped": True, "notes": "", "unshipped": 3}
    assert evaluate.score(flat, actual).passed is True
