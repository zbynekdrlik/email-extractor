"""Customer matching (#62, the customer half).

An unmatched customer stops the whole document — there is nowhere to address the order —
so this is the single most expensive place to be wrong. The rules below are the ones the
live pipeline had to learn, in their order of authority.
"""
from app.orders import customer

CUSTOMERS = [
    {"ean_edi": "2000000000864", "name": "Potraviny nie otraviny Martin",
     "emails": ["objednavky.pno.martin@gmail.com"], "city": "Martin",
     "street": "Košútka 1", "zip": ""},
    {"ean_edi": "2000000000865", "name": "Potraviny nie otraviny Ružomberok",
     "emails": ["ruzomberok@potravinynieotraviny.sk"], "city": "Ružomberok",
     "street": "Zarevúca 4935/27", "zip": ""},
    {"ean_edi": "8589000020001", "name": "TESCO STORES SR, a.s. — Ružinov",
     "emails": ["faktury@tesco.com"], "city": "Bratislava", "street": "Cesta na Senec",
     "zip": ""},
    {"ean_edi": "8589000020002", "name": "TESCO STORES SR, a.s. — Petržalka",
     "emails": ["faktury@tesco.com"], "city": "Bratislava", "street": "Rusovská",
     "zip": ""},
]


# --- the table wins when it is unambiguous -------------------------------

def test_an_address_written_in_the_table_decides_even_when_the_model_is_unsure():
    """30.07.2026: the mail came from objednavky.pno.martin@gmail.com, which IS in the
    table — written as "Marek Pavlovič <objednavky.pno.martin@gmail.com>". The model saw
    only a public gmail address, correctly refused to guess (0.08), and the WHOLE order
    fell over although every item matched. A hand-written address is the warehouse
    stating whose it is."""
    hit = customer.resolve(
        CUSTOMERS, sender_email="objednavky.pno.martin@gmail.com",
        sender_name="Marek Pavlovič", company_name="",
        llm={"ean_edi": "", "confidence": 0.08})
    assert (hit.ean_edi, hit.rule) == ("2000000000864", "exact_email")
    assert hit.confidence >= 0.95


def test_a_confident_model_match_is_not_overridden_by_the_address():
    """The same address may order for a DIFFERENT branch than the one it is registered
    to, so a sure model answer keeps priority."""
    hit = customer.resolve(
        CUSTOMERS, sender_email="objednavky.pno.martin@gmail.com", sender_name="",
        company_name="Potraviny nie otraviny Ružomberok",
        llm={"ean_edi": "2000000000865", "confidence": 0.93})
    assert (hit.ean_edi, hit.rule) == ("2000000000865", "llm")


def test_an_address_shared_by_several_customers_is_never_guessed():
    """Seven addresses in the real table belong to several branches (one Tesco address
    for six shops), so the address alone cannot decide."""
    hit = customer.resolve(CUSTOMERS, sender_email="faktury@tesco.com", sender_name="",
                           company_name="", llm={"ean_edi": "", "confidence": 0.2})
    assert hit is None


def test_a_shared_address_still_resolves_when_the_model_names_one_of_them():
    hit = customer.resolve(CUSTOMERS, sender_email="faktury@tesco.com", sender_name="",
                           company_name="TESCO Petržalka",
                           llm={"ean_edi": "8589000020002", "confidence": 0.88})
    assert hit.ean_edi == "8589000020002"


# --- candidates handed to the model --------------------------------------

def test_the_exact_address_match_is_marked_for_the_model():
    cands = customer.candidates(CUSTOMERS, "objednavky.pno.martin@gmail.com", "", "")
    assert cands[0]["ean_edi"] == "2000000000864"
    assert cands[0]["exact_email"] is True


def test_a_generic_domain_never_scores_as_a_domain_match():
    """gmail.com is shared by half the world; matching on it would attach an order to a
    random customer."""
    cands = customer.candidates(CUSTOMERS, "niekto.iny@gmail.com", "", "")
    assert all(not c["exact_email"] for c in cands)
    assert cands[0]["score"] < 80, "no domain bonus for a free provider"


def test_a_company_domain_does_score():
    cands = customer.candidates(CUSTOMERS, "sklad@potravinynieotraviny.sk", "", "")
    assert cands[0]["ean_edi"] == "2000000000865"


def test_the_company_name_from_the_signature_is_used():
    cands = customer.candidates(CUSTOMERS, "ktosi@inde.sk", "",
                                "Potraviny nie otraviny Ružomberok")
    assert cands[0]["ean_edi"] == "2000000000865"


# --- the gate ------------------------------------------------------------

def test_an_uncertain_model_answer_does_not_become_a_customer():
    hit = customer.resolve(CUSTOMERS, sender_email="ktosi@inde.sk", sender_name="",
                           company_name="", llm={"ean_edi": "2000000000864",
                                                 "confidence": 0.5})
    assert hit is None, "a wrongly addressed order is worse than one for review"


def test_a_customer_the_model_invented_is_refused():
    hit = customer.resolve(CUSTOMERS, sender_email="ktosi@inde.sk", sender_name="",
                           company_name="", llm={"ean_edi": "9999999999999",
                                                 "confidence": 0.99})
    assert hit is None, "the EAN must exist in the snapshot"


# --- one address, two branches: the block header in the file decides (#101) ----

GT = [
    {"ean_edi": "2000000000856", "name": "GT1 Gazdovský trh, Banská Bystrica",
     "emails": ["petra.durkosova@gazdovskytrh.sk"], "city": "Banská Bystrica",
     "street": "Družby 35", "zip": ""},
    # The table really does call this one GT1 too — a typo the matching must survive,
    # which is exactly why the STREET decides and the name does not.
    {"ean_edi": "2000000000857", "name": "GT1 Gazdovský trh, Banská Bystrica",
     "emails": ["petra.durkosova@gazdovskytrh.sk"], "city": "Banská Bystrica",
     "street": "29 augusta 19", "zip": ""},
]


def test_two_branches_on_one_address_are_told_apart_by_the_block_header():
    """Beh 26, Gazdovský trh: one xlsx holds BOTH shops side by side, so one email is two
    customers. The sender address belongs to both rows, the model cannot choose, and the
    whole 40-line order stopped. The file itself says which is which — the block header
    over each half carries the street."""
    a = customer.resolve(GT, sender_email="petra.durkosova@gazdovskytrh.sk",
                         sender_name="Durkošová", company_name="Gazdovský trh",
                         llm={"ean_edi": "", "confidence": 0.2},
                         store="GT1- Družby 35 BB")
    b = customer.resolve(GT, sender_email="petra.durkosova@gazdovskytrh.sk",
                         sender_name="Durkošová", company_name="Gazdovský trh",
                         llm={"ean_edi": "", "confidence": 0.2},
                         store="GT2- 29 augusta 19 BB")
    assert (a.ean_edi, a.rule) == ("2000000000856", "store_address")
    assert b.ean_edi == "2000000000857"
    assert "29 augusta 19" in b.note


def test_a_block_header_that_matches_neither_branch_decides_nothing():
    """Guessing a branch is worse than stopping: the order would go to the wrong shop."""
    assert customer.resolve(GT, sender_email="petra.durkosova@gazdovskytrh.sk",
                            sender_name="", company_name="",
                            llm={"ean_edi": "", "confidence": 0.2},
                            store="GT9- Hlavná 1 Zvolen") is None


def test_a_block_header_that_matches_both_branches_decides_nothing():
    assert customer.resolve(GT, sender_email="petra.durkosova@gazdovskytrh.sk",
                            sender_name="", company_name="",
                            llm={"ean_edi": "", "confidence": 0.2},
                            store="Gazdovský trh Banská Bystrica") is None


def test_without_a_block_header_a_shared_address_still_refuses_to_guess():
    assert customer.resolve(GT, sender_email="petra.durkosova@gazdovskytrh.sk",
                            sender_name="", company_name="",
                            llm={"ean_edi": "", "confidence": 0.2}) is None


# --- #159: ranking candidates for the WAREHOUSE's "who is this?" question ------
#
# `candidates_for_question` is a SEPARATE function from `candidates()` above on purpose:
# `candidates()` feeds the model's own prompt (`_customer_input`), and reordering or
# rescoring it would change the exact text sent to the model — a corpus `llm-cache` miss.
# This one only ever reaches a human's screen.

FARMERIA = [
    {"ean_edi": "2000000000861", "name": "Potraviny nie otraviny Žilina",
     "emails": ["evakozakova9@gmail.com"], "city": "Žilina", "street": "na bráne 4",
     "zip": "01001"},
    {"ean_edi": "2000000000864", "name": "Potraviny nie otraviny Martin",
     "emails": ["objednavky.pno.martin@gmail.com"], "city": "Martin",
     "street": "Košútka 1", "zip": ""},
    {"ean_edi": "8589000020001", "name": "TESCO STORES SR, a.s. — Ružinov",
     "emails": ["faktury@tesco.com"], "city": "Bratislava", "street": "Cesta na Senec",
     "zip": ""},
]

# The 2026-08-03 incident, verbatim: the sender is unknown to the table (a fresh gmail
# address), but the delivery address in the mail's own text is the SAME street/city EAN
# 2000000000861 is registered under — different case, and the mail also carries a PSČ the
# candidate row does not.
FARMERIA_MAIL_TEXT = "obj pekaova\n8 položiek\nNa bráne 4, 010 01 Žilina\ntermín 06.08.2026"


def test_the_address_signal_ranks_the_right_customer_first_even_with_no_email_or_name_hit():
    cands = customer.candidates_for_question(
        FARMERIA, sender_email="zilina@farmeria.sk", sender_name="", company_name="",
        free_text=FARMERIA_MAIL_TEXT)
    assert cands[0]["ean_edi"] == "2000000000861"
    assert cands[0]["address_match"] is True


def test_no_address_hit_in_the_free_text_never_boosts_anyone():
    cands = customer.candidates_for_question(
        FARMERIA, sender_email="zilina@farmeria.sk", sender_name="", company_name="",
        free_text="objednávka bez akejkoľvek adresy v texte")
    assert all(c["address_match"] is False for c in cands)


def test_the_address_signal_only_ranks_never_filters_or_decides():
    """Never an auto-match key (#159) — this only ORDERS the list shown to a human; the
    caller (pipeline.py) still always asks regardless of score. Pinned here as: even the
    address-matched candidate's LOWER-scoring siblings stay in the returned list — the
    function ranks, it never drops a candidate or narrows down to a single "decided"
    answer just because one scored far higher (review finding on PR #161: the previous
    version of this test only asserted `len(cands) > 1`, which is true even if the
    function silently filtered — this checks the actual siblings survive by identity)."""
    cands = customer.candidates_for_question(
        FARMERIA, sender_email="zilina@farmeria.sk", sender_name="", company_name="",
        free_text=FARMERIA_MAIL_TEXT)
    eans = {c["ean_edi"] for c in cands}
    assert eans == {"2000000000861", "2000000000864", "8589000020001"}, \
        "all three candidates survive — none dropped just because one scored higher"


def test_email_and_name_signals_still_work_without_any_address_hit():
    """The existing `_score` signals (exact/domain e-mail, company/sender name) must
    still be used — the address is an ADDITIONAL signal, not a replacement."""
    cands = customer.candidates_for_question(
        FARMERIA, sender_email="objednavky.pno.martin@gmail.com", sender_name="",
        company_name="", free_text="")
    assert cands[0]["ean_edi"] == "2000000000864"


def test_guess_delivery_address_finds_the_line_carrying_a_postal_code():
    assert customer.guess_delivery_address(FARMERIA_MAIL_TEXT) == "Na bráne 4, 010 01 Žilina"


def test_guess_delivery_address_is_empty_when_no_postal_code_appears():
    assert customer.guess_delivery_address("objednávka bez adresy") == ""


# --- #418: delivery-address rung — multi-site sender resolved by delivery city/street ----

KOSIK = [
    {"ean_edi": "2000000000797", "name": "Košík.sk — MAKRO store Košice",
     "emails": ["objednavky@kosik.sk"], "city": "Košice", "street": "Moldavská 32",
     "zip": ""},
    {"ean_edi": "2000000000798", "name": "Košík.sk — MAKRO store Žilina",
     "emails": ["objednavky@kosik.sk"], "city": "Žilina", "street": "Prielohy 1",
     "zip": ""},
    {"ean_edi": "2000000000799", "name": "Košík.sk — MAKRO store Zvolen",
     "emails": ["objednavky@kosik.sk"], "city": "Zvolen", "street": "Ulica Stráž 17",
     "zip": ""},
]

KOSIK_ZILINA_TEXT = (
    "Objednávka č. 4500317338\n"
    "Doručenie na sklad Prielohy 1, 010 07, Žilina, MAKRO store Žilina\n"
    "Termín dodania: 14.09.2026"
)


def test_delivery_address_resolves_to_the_matching_site_card():
    """#418: two+ cards share an email — the delivery city in the mail text picks the
    right one. The model is unsure and there is no multi-shop block header (store='')."""
    hit = customer.resolve(
        KOSIK, sender_email="objednavky@kosik.sk", sender_name="Košík.sk",
        company_name="Košík.sk", llm={"ean_edi": "", "confidence": 0.2},
        delivery_text=KOSIK_ZILINA_TEXT)
    assert hit is not None, "should resolve, not fall through to question"
    assert hit.ean_edi == "2000000000798"
    assert hit.rule == "delivery_address"


def test_delivery_address_zvolen_resolves_to_zvolen_card():
    """Same sender, but mail says Zvolen."""
    zvolen_text = (
        "Objednávka č. 4500317337\n"
        "Doručenie na sklad Ulica Stráž 17, 960 01, Zvolen\n"
        "Termín dodania: 14.09.2026"
    )
    hit = customer.resolve(
        KOSIK, sender_email="objednavky@kosik.sk", sender_name="Košík.sk",
        company_name="Košík.sk", llm={"ean_edi": "", "confidence": 0.2},
        delivery_text=zvolen_text)
    assert hit is not None
    assert hit.ean_edi == "2000000000799"
    assert hit.rule == "delivery_address"


def test_delivery_address_no_match_falls_through_to_none():
    """When the delivery text doesn't match any card's city/street -> None, so the
    pipeline raises the standard customer board question."""
    unknown_text = (
        "Objednávka č. 4500317340\n"
        "Doručenie na sklad Bratislava, Einsteinova 25\n"
        "Termín dodania: 14.09.2026"
    )
    hit = customer.resolve(
        KOSIK, sender_email="objednavky@kosik.sk", sender_name="Košík.sk",
        company_name="Košík.sk", llm={"ean_edi": "", "confidence": 0.2},
        delivery_text=unknown_text)
    assert hit is None


def test_delivery_address_not_used_when_only_one_card_matches_email():
    """When only ONE card owns the address -> exact_email wins, delivery_text is
    irrelevant. Confirms single-card behaviour is unchanged (#418 point 3)."""
    single = [KOSIK[0]]  # only the Košice card
    hit = customer.resolve(
        single, sender_email="objednavky@kosik.sk", sender_name="",
        company_name="", llm={"ean_edi": "", "confidence": 0.2},
        delivery_text=KOSIK_ZILINA_TEXT)
    assert hit is not None
    assert hit.rule == "exact_email"
    assert hit.ean_edi == "2000000000797"


def test_delivery_address_llm_still_wins_over_address_when_sure():
    """A confident model match overrides the delivery-address rung — same priority
    as the existing hierarchy."""
    hit = customer.resolve(
        KOSIK, sender_email="objednavky@kosik.sk", sender_name="",
        company_name="Košík.sk",
        llm={"ean_edi": "2000000000797", "confidence": 0.90},
        delivery_text=KOSIK_ZILINA_TEXT)
    assert hit.ean_edi == "2000000000797"
    assert hit.rule == "llm"


def test_delivery_address_ambiguous_two_cities_match_returns_none():
    """If the delivery text happens to mention BOTH cities -> ambiguous -> None."""
    both_text = "Doručenie: pobočky Košice a Žilina, rozvoz oboch"
    hit = customer.resolve(
        KOSIK, sender_email="objednavky@kosik.sk", sender_name="",
        company_name="", llm={"ean_edi": "", "confidence": 0.2},
        delivery_text=both_text)
    assert hit is None


def test_delivery_address_street_breaks_city_tie():
    """Two cards in the same city — the street must break the tie."""
    same_city = [
        {"ean_edi": "3000000000001", "name": "Pekáreň Bratislava — Ružinov",
         "emails": ["objednavky@pekarenba.sk"], "city": "Bratislava",
         "street": "Cesta na Senec 2", "zip": ""},
        {"ean_edi": "3000000000002", "name": "Pekáreň Bratislava — Petržalka",
         "emails": ["objednavky@pekarenba.sk"], "city": "Bratislava",
         "street": "Rusovská 18", "zip": ""},
    ]
    text = "Dodanie: Rusovská 18, 851 01 Bratislava"
    hit = customer.resolve(
        same_city, sender_email="objednavky@pekarenba.sk", sender_name="",
        company_name="", llm={"ean_edi": "", "confidence": 0.2},
        delivery_text=text)
    assert hit is not None
    assert hit.ean_edi == "3000000000002"
    assert hit.rule == "delivery_address"


# --- #418 review findings F1/F2: word-boundary city matching + notes-only text --------

PNO_SITES = [
    {"ean_edi": "2000000000864", "name": "PNO Martin",
     "emails": ["objednavky@pno.sk"], "city": "Martin",
     "street": "Kollarova 8", "zip": ""},
    {"ean_edi": "2000000000865", "name": "PNO Poprad",
     "emails": ["objednavky@pno.sk"], "city": "Poprad",
     "street": "Sturova 3", "zip": ""},
]


def test_person_name_city_in_signature_does_not_auto_resolve():
    """F2: a city that is also a person name in a signature must NOT decide the site.
    Word-boundary matching prevents 'Martin' in 'S pozdravom Martin Novak' from matching
    when it is part of a person name followed by more word chars (it would however match
    as a standalone word — but in production this text is the model's `notes`, not the raw
    email, so a signature never reaches the rung)."""
    sig_text = "S pozdravom Martina Novakova"
    hit = customer.resolve(
        PNO_SITES, sender_email="objednavky@pno.sk", sender_name="",
        company_name="", llm={"ean_edi": "", "confidence": 0.2},
        delivery_text=sig_text)
    assert hit is None, "a name substring must not auto-resolve"


def test_city_word_boundary_prevents_substring_match():
    """The word-boundary check prevents 'Martin' from matching inside 'Martina'."""
    text = "Dodanie pre Martina"
    hit = customer.resolve(
        PNO_SITES, sender_email="objednavky@pno.sk", sender_name="",
        company_name="", llm={"ean_edi": "", "confidence": 0.2},
        delivery_text=text)
    assert hit is None
