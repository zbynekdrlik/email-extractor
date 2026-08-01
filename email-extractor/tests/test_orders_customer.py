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
