"""#493 — which SHOP of a multi-shop customer an order is for (pure, no DB).

The synthetic family mirrors the 2026-10-16 incident's shape (five shops of one customer, one
sender address on every card, headers „BISTRO", „Predajňa: Tržnica", „ŽILINA", „ŠKOLA, <street>"
and an empty „Predajňa:" whose only identifier is the file name). Every name, street and EAN is
invented — the repo is public.
"""
from app.orders import customer, store_key

SENDER = "nakup@retazec-test.sk"
FAMILY = [
    {"ean_edi": "2999000000201", "name": "Školská jedáleň, Hrdinov 7 Mestečko",
     "trade_name": "Súkromná škola", "emails": [SENDER], "city": "Mestečko",
     "street": "Hrdinová 7", "zip": ""},
    {"ean_edi": "2999000000202", "name": "Tržnica Mestečko - Riadok 1",
     "trade_name": "Tržnica Mestečko", "emails": [SENDER], "city": "Mestečko",
     "street": "Riadok 1", "zip": ""},
    {"ean_edi": "2999000000203", "name": "Dolina Makovická", "trade_name": "Dolina Makovická",
     "emails": [SENDER], "city": "Dolina", "street": "Makovická", "zip": ""},
    {"ean_edi": "2999000000204", "name": "BISTRO Dolina Krátka 1", "trade_name": "BISTRO Dolina",
     "emails": [SENDER], "city": "Dolina", "street": "Krátka 1", "zip": ""},
    {"ean_edi": "2999000000999", "name": "Iný zákazník Bratislava", "emails": ["x@iny.sk"],
     "city": "Bratislava", "street": "Hlavná 9", "zip": ""},
]
FILES = {"PR": "OBJ Pekárová žena PR.xlsx", "SKOLA": "OBJ Pekárová žena škola.xlsx",
         "ZA": "OBJ Pekárová žena ZA.xlsx", "BISTRO": "OBJ Pekárová žena BISTRO.xlsx",
         "TR": "OBJ Pekárová žena TR.xlsx"}
ORDERS = [
    {"store": "PR", "sourceFile": FILES["PR"]},
    {"store": "ŠKOLA, Hrdinov 7, MS", "sourceFile": FILES["SKOLA"]},
    {"store": "DOLINA", "sourceFile": FILES["ZA"]},
    {"store": "BISTRO", "sourceFile": FILES["BISTRO"]},
    {"store": "Predajňa: Tržnica", "sourceFile": FILES["TR"]},
]


def _resolve(key, rules=(), multi=True, delivery_text=""):
    return customer.resolve(FAMILY, SENDER, "", "", llm={"ean_edi": "2999000000204",
                                                           "confidence": 0.97},
                            key=key, rules=rules, multi_store=multi,
                            delivery_text=delivery_text)


# --- the store key ------------------------------------------------------------------------

def test_tokens_drop_order_words_our_brand_days_and_numbers():
    assert store_key.tokens("OBJ Pekárová žena BISTRO 42. týždeň pondelok") == ["bistro"]
    assert store_key.tokens("Predajňa: Tržnica") == ["trznica"]
    assert store_key.tokens("ŠKOLA, Hrdinov SNP 7, MT") == ["skola", "hrdinov", "snp", "mt"]
    # a street genitive month stays (it IS the street), a nominative month name does not
    assert "augusta" in store_key.tokens("GT2- 29 augusta 19 BB")
    assert store_key.tokens("objednávka október kw42") == []


def test_the_key_drops_what_every_shop_of_the_mail_shares():
    keys = store_key.for_orders(ORDERS)
    assert [k.text for k in keys] == ["pr", "hrdinov ms skola", "dolina za", "bistro",
                                      "tr trznica"]
    # the full tokens keep everything (a learned rule is matched against them)
    assert keys[0].tokens == frozenset({"pr"})
    assert keys[1].label == "„ŠKOLA, Hrdinov 7, MS“ (príloha OBJ Pekárová žena škola.xlsx)"
    assert store_key.is_multi_store(keys)


def test_a_one_shop_mail_keeps_its_whole_key_and_is_not_multi_store():
    keys = store_key.for_orders([{"store": "", "sourceFile": "OBJ Firma BISTRO.xlsx"},
                                 {"store": "", "sourceFile": "OBJ Firma BISTRO.xlsx"}])
    assert keys[0].key == frozenset({"firma", "bistro"})
    assert not store_key.is_multi_store(keys), "a file name alone never makes two shops"


def test_a_key_that_would_become_empty_keeps_its_own_words():
    keys = store_key.for_orders([{"store": "Mesto"}, {"store": "Mesto Bistro"}])
    assert keys[0].key == frozenset({"mesto"})
    assert keys[1].key == frozenset({"bistro"})


def test_the_attachment_of_an_order_is_located_from_its_quotes():
    source = ("Body: ahoj\n\nAttachments:\n===== A.xlsx =====\n[Sheet: H]\n120x rožok 50g\n"
              "===== B.xlsx =====\n[Sheet: H]\n7x vianočka 400g\n5x rožok kváskový\n")
    assert store_key.locate_file(["120x rožok 50g"], source) == "A.xlsx"
    assert store_key.locate_file(["7x vianočka 400g", "5x rožok kváskový"], source) == "B.xlsx"
    assert store_key.locate_file(["nič také"], source) == "", "no hit — never guessed"
    assert store_key.locate_file(["rožok"], source) == "", "a tie — never guessed"
    assert store_key.locate_file(["120x rožok 50g"], "bez príloh") == ""


def test_a_question_context_rebuilds_the_same_key():
    key = store_key.for_orders(ORDERS)[2]
    ctx = {"store": key.header, "store_file": key.source_file, "store_key": key.text,
           "store_tokens": sorted(key.tokens)}
    back = store_key.from_context(ctx)
    assert (back.key, back.tokens, back.header_key) == (key.key, key.tokens, key.header_key)
    assert store_key.from_context({"sender_email": SENDER}) is None


# --- the resolve rungs ----------------------------------------------------------------------

def test_the_incident_shops_resolve_by_name_or_are_asked():
    """BISTRO / Tržnica / ŠKOLA name ONE card each; ŽILINA-like „DOLINA" names two cards
    (two shops in one town) and „PR" names none — both are asked, never guessed, and the
    model's confident pick of the BISTRO card never decides a shop."""
    keys = store_key.for_orders(ORDERS)
    got = {k.header: _resolve(k) for k in keys}
    assert (got["BISTRO"].ean_edi, got["BISTRO"].rule) == ("2999000000204", "store_name")
    assert (got["Predajňa: Tržnica"].ean_edi,
            got["Predajňa: Tržnica"].rule) == ("2999000000202", "store_name")
    assert got["ŠKOLA, Hrdinov 7, MS"].ean_edi == "2999000000201"
    assert got["DOLINA"] is None
    assert got["PR"] is None


def test_a_learned_shop_rule_decides_above_everything():
    keys = store_key.for_orders(ORDERS)
    rules = [(frozenset({"pr"}), "2999000000203")]
    hit = _resolve(keys[0], rules=rules)
    assert (hit.ean_edi, hit.rule) == ("2999000000203", "store_rule")
    # the rule matches the full tokens, so a one-file mail (no mail-common stripping) hits too
    one = store_key.for_orders([{"store": "PR", "sourceFile": FILES["PR"]}])[0]
    assert _resolve(one, rules=rules).ean_edi == "2999000000203"


def test_two_rules_that_disagree_decide_nothing():
    key = store_key.for_orders([{"store": "BISTRO DOLINA"}])[0]
    rules = [(frozenset({"bistro"}), "2999000000204"), (frozenset({"dolina"}), "2999000000203")]
    assert store_key.match_rule(rules, key) == ""
    assert _resolve(key, rules=rules, multi=True) is None, "both name stems hit — asked"


def test_a_rule_naming_a_card_no_longer_in_the_list_is_ignored():
    key = store_key.for_orders([{"store": "PR"}])[0]
    assert _resolve(key, rules=[(frozenset({"pr"}), "2999000000777")]) is None


def test_the_street_rung_still_comes_first(monkeypatch):
    """#101: a header carrying the street decides by the street."""
    key = store_key.for_orders([{"store": "BISTRO Riadok 1"}])[0]
    hit = _resolve(key)
    assert (hit.ean_edi, hit.rule) == ("2999000000202", "store_address")


def test_the_mail_level_delivery_text_never_picks_a_shop_in_a_multi_shop_mail():
    key = store_key.for_orders([{"store": "PR"}])[0]
    notes = "Dodať na adresu Krátka 1, Dolina"
    assert _resolve(key, multi=True, delivery_text=notes) is None
    # a one-shop mail keeps #418
    assert _resolve(key, multi=False, delivery_text=notes).rule == "delivery_address"


def test_a_stem_every_family_card_shares_is_no_signal():
    fam = [dict(c, name=f"Reťazec {c['name']}", trade_name="")
           for c in FAMILY if c["ean_edi"] != "2999000000999"]
    key = store_key.for_orders([{"store": "Reťazec"}])[0]
    assert customer._by_store_name(fam, key) is None


def test_a_sender_on_no_family_keeps_the_old_rungs():
    """Regression: a single-card sender still resolves by the model / its address."""
    hit = customer.resolve(FAMILY, "x@iny.sk", "", "", llm={"ean_edi": "", "confidence": 0},
                           key=store_key.for_orders([{"store": "BISTRO"}])[0])
    assert (hit.ean_edi, hit.rule) == ("2999000000999", "exact_email")


# --- the poistka ----------------------------------------------------------------------------

def _m(ean, rule):
    return customer.Matched(ean_edi=ean, name="", confidence=0.9, rule=rule, note="")


def test_the_poistka_turns_back_only_the_non_shop_resolutions_of_a_collapsed_card():
    keys = store_key.for_orders([{"store": "BISTRO"}, {"store": "PR"}, {"store": "TR"},
                                 {"store": "BISTRO"}])
    matched = [_m("A", "store_name"), _m("A", "llm"), _m("B", "exact_email"),
               _m("A", "store_name")]
    assert customer.collapsed_shops(keys, matched) == {1}


def test_the_poistka_ignores_one_shop_on_several_dates_and_empty_headers():
    keys = store_key.for_orders([{"store": "BISTRO"}, {"store": "BISTRO"}, {"store": ""}])
    matched = [_m("A", "llm"), _m("A", "llm"), _m("A", "llm")]
    assert customer.collapsed_shops(keys, matched) == set()


def test_a_shop_question_lists_the_whole_family_and_its_name_hits_first():
    key = store_key.for_orders([{"store": "DOLINA"}])[0]
    cands = customer.candidates_for_question(FAMILY, SENDER, "", "", key=key)
    eans = [c["ean_edi"] for c in cands]
    assert set(eans[:2]) == {"2999000000203", "2999000000204"}, "the two Dolina shops first"
    assert {"2999000000201", "2999000000202"} <= set(eans), "every family card is offered"
