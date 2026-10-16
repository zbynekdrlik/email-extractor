"""#493 review round 6 — a shop is never decided by a guess.

* the shop-name rung ships only to a card that carries EVERY distinguishing word of the shop
  („Kaufland Martin" is not „Kaufland Žilina" just because no family card says Martin; a new
  „BISTRO Trnava" is not the known „BISTRO Dolina");
* an order whose attachment could not be told (its lines are in two files) never borrows a
  sibling's file — for a sender with learned shop rules it is asked, never shipped by a rule
  of the other file nor by the sender's address;
* a header's 4-5 digit number stays part of the shop (two shops „Hlavná 1713" / „Hlavná
  2210" stay two — a year-like house number too); only 6+ digit numbers go — and a date
  range („16.-20.10.") goes whole.

Synthetic data only (the repo is public).
"""
from test_orders_store_rules import (
    CATALOG_CSV,
    SENDER,
    TOWNS_CSV,
    Recorder,
    ScriptedClient,
    _brand_env,
    _cfg,
    _env,
    _run,
    _shop_questions,
)

from app.orders import extract, order_customers, pipeline, snapshot, store_key, store_rules, teach

ZILINA_FAMILY_CSV = (
    "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
    f"Tesco Žilina,2999000000111,Žilina,Hlavná 1,,,{SENDER}\n"
    f"Kaufland Žilina,2999000000112,Žilina,Hlavná 2,,,{SENDER}\n"
    "Tesco Martin,2999000000113,Martin,Hlavná 3,,,\n"
    "Kaufland Martin,2999000000114,Martin,Hlavná 4,,,\n"
    f"Centrála reťazca,2999000000101,Mestečko,Hlavná 9,,,{SENDER}\n"
)
MARTIN = [("Tesco Martin", "TE", "vianočka 400g", 7, "VIA"),
          ("Kaufland Martin", "KA", "rožok kváskový 70g", 5, "G70")]
# BISTRO's own row is only in its file; the other shop's only row is in BOTH files
OVERLAP = ("Subject: objednávka\n\nAttachments:\n"
           "===== Kvetka BISTRO.xlsx =====\n[Sheet: H]\nna 04.08.2026\n120x rožok 50g\n"
           "7x vianočka 400g\n"
           "===== Kvetka TR.xlsx =====\n[Sheet: H]\nna 04.08.2026\n7x vianočka 400g\n")


def _overlap_run(pg, mid, company="Kvetka s.r.o."):
    pg.execute("INSERT INTO messages (message_id, category) VALUES (%s, 'ai_orders')", (mid,))
    orders = [{"orderNumber": "", "deliveryDate": "04.08.2026", "recipientGroup": "",
               "store": "", "items": [{"name": n, "quantity": q, "unit": "ks",
                                       "sourceQuote": f"{q}x {n}"}]}
              for n, q in (("rožok 50g", 120), ("vianočka 400g", 7))]
    answers = [{"senderName": "Nákup", "senderEmail": SENDER, "companyName": company,
                "isChangeRequest": False, "notes": "", "orders": orders},
               {"ean_edi": "2999000000401", "confidence": 0.97}]
    answers += [{"gtin": g, "confidence": 0.95, "matchedCatalogName": "", "reason": ""}
                for g in ("G50", "VIA")]
    rec = Recorder()
    mail = {"message_id": mid, "subject": "objednávka", "from_addr": SENDER,
            "from_name": "Nákup", "combined_text": OVERLAP, "today": "2026-07-30"}
    result = pipeline.run(pg, _cfg(), mail, snapshot.latest_snapshot_id(pg),
                          client=ScriptedClient(answers), upload=rec.upload, post=rec.post)
    return result, rec


def _bistro_rule(pg, ean):
    known = store_key.for_orders([{"store": "", "sourceFile": "Kvetka BISTRO.xlsx"}])[0]
    store_rules.remember(pg, sender_email=SENDER, key=known, ean_edi=ean, question_id=1,
                         by="sklad")


def test_a_shop_whose_town_no_family_card_carries_is_asked(pg):
    """The Martin cards do not carry the sender's address: „Kaufland Martin" hit only
    Kaufland Žilina (by „Kaufland") in both views — Martin is on no card of the family."""
    snapshot.import_snapshot(pg, CATALOG_CSV, ZILINA_FAMILY_CSV)
    result, rec = _run(pg, "m-mt", stores=MARTIN)
    assert rec.uploads == []
    assert set(_shop_questions(pg)) == {"Tesco Martin", "Kaufland Martin"}


def test_a_new_shop_in_another_town_is_not_its_namesakes_card(pg):
    _env(pg)
    result, rec = _run(pg, "m-new", stores=[("BISTRO Trnava", "BT", "rožok 50g", 120, "G50"),
                                            ("Predajňa: Tržnica", "TR", "vianočka 400g", 7,
                                             "VIA")])
    assert rec.uploads == [], "a card's name never ships a shop (review round 7)"
    assert set(_shop_questions(pg)) == {"BISTRO Trnava", "Predajňa: Tržnica"}


def test_an_untold_file_never_borrows_its_siblings_rule(pg):
    """A family sender with a rule for the BISTRO file: the other shop's order ties between
    the two files — it is not merged into BISTRO's (review round 7) and never rides on its
    rule: BISTRO's own goods ship by the rule, the other order is asked."""
    _brand_env(pg)
    _bistro_rule(pg, "2999000000402")
    result, rec = _overlap_run(pg, "m-ov")
    shipped = [(r["customer_ean"], [i["gtin"] for i in r["items"]])
               for r in result["order_results"] if r["status"] == "ok"]
    assert shipped == [("2999000000402", ["G50"])]
    assert len(rec.uploads) == 1
    assert len([q for q in teach.open_questions(pg) if q["kind"] == "customer"]) == 1


def test_a_rule_sender_on_one_card_is_asked_for_an_untold_file(pg):
    """The same mail from a sender on ONE card (its other shops are learned rules): the
    order whose file is unknown is asked — never shipped to the sender's card by its address,
    nor by the other file's rule; the BISTRO order ships by its rule alone."""
    snapshot.import_snapshot(pg, CATALOG_CSV, TOWNS_CSV)
    _bistro_rule(pg, "2999000000111")
    result, rec = _overlap_run(pg, "m-ov1", company="Reťazec")
    shipped = [(r["customer_ean"], [i["gtin"] for i in r["items"]])
               for r in result["order_results"] if r["status"] == "ok"]
    assert shipped == [("2999000000111", ["G50"])]
    assert len(rec.uploads) == 1
    assert len([q for q in teach.open_questions(pg) if q["kind"] == "customer"]) == 1


def test_the_extraction_marks_an_order_whose_file_it_could_not_tell():
    out = extract.verify({"orders": [
        {"deliveryDate": "04.08.2026", "store": "",
         "items": [{"name": "vianočka 400g", "quantity": 7, "unit": "ks",
                    "sourceQuote": "7x vianočka 400g"}]},
        {"deliveryDate": "04.08.2026", "store": "",
         "items": [{"name": "rožok 50g", "quantity": 120, "unit": "ks",
                    "sourceQuote": "120x rožok 50g"}]}]}, OVERLAP)
    assert [(o["sourceFile"], bool(o.get("fileUnknown"))) for o in out["orders"]] == [
        ("", True), ("Kvetka BISTRO.xlsx", False)]
    plain = extract.verify({"orders": [
        {"deliveryDate": "04.08.2026", "store": "",
         "items": [{"name": "rožok 50g", "quantity": 120, "unit": "ks",
                    "sourceQuote": "120x rožok 50g"}]}]}, "Body: 120x rožok 50g\n")
    assert not plain["orders"][0].get("fileUnknown"), "no attachment — nothing to tell"


def test_two_shops_differing_by_a_house_number_stay_two():
    assert store_key.tokens("Hlavná 1713", keep_numbers=True) == ["hlavna", "1713"]
    assert store_key.tokens("Hlavná 22105", keep_numbers=True) == ["hlavna", "22105"]
    # a year stays: dropping it would make „Hlavná 2010" and „Hlavná 2011" ONE shop; a
    # header with a year only re-asks once a year (the safe direction)
    assert store_key.tokens("Hlavná 2010", keep_numbers=True) == ["hlavna", "2010"]
    assert store_key.tokens("Obj 450031", keep_numbers=True) == [], "an order number"
    keys = store_key.for_orders([{"store": "Hlavná 1713"}, {"store": "Hlavná 2210"}])
    assert keys[0].identity != keys[1].identity


def test_a_header_s_date_range_is_not_the_shop():
    assert store_key.tokens("BISTRO 16.-20.10.", keep_numbers=True) == ["bistro"]
    assert store_key.tokens("BISTRO 16. - 20. 10. 2026", keep_numbers=True) == ["bistro"]
    assert store_key.tokens("Týždeň od 19.10-23.10. ŽILINA", keep_numbers=True) == ["zilina"]


# --- review round 7 -------------------------------------------------------------------------

def test_shops_numbered_or_coded_like_a_card_are_asked(pg):
    """„BISTRO 1" + „BISTRO 2" both resembled the one BISTRO card (two shops on one card — the
    incident class), a new „BISTRO BB" the BISTRO of another town: every one is asked."""
    _env(pg)
    result, rec = _run(pg, "m-num", stores=[("BISTRO 1", "B1", "rožok 50g", 120, "G50"),
                                            ("BISTRO 2", "B2", "vianočka 400g", 7, "VIA"),
                                            ("BISTRO BB", "BB", "rožok kváskový 70g", 5,
                                             "G70"),
                                            ("Predajňa: Tržnica", "TR", "chlieb kváskový", 3,
                                             "CHL")])
    assert rec.uploads == []
    assert set(_shop_questions(pg)) == {"BISTRO 1", "BISTRO 2", "BISTRO BB",
                                        "Predajňa: Tržnica"}


def test_a_date_range_never_eats_a_shop_number_and_an_order_number_goes():
    assert store_key.tokens("BISTRO 1 - 16.10.", keep_numbers=True) == ["bistro", "1"]
    assert store_key.tokens("BISTRO 2 – 16.10.", keep_numbers=True) == ["bistro", "2"]
    assert store_key.tokens("BISTRO 16.—20.10.", keep_numbers=True) == ["bistro"]
    assert store_key.tokens("Obj. č. 4512 BISTRO", keep_numbers=True) == ["bistro"]
    assert store_key.tokens("Objednávka 45120 BISTRO", keep_numbers=True) == ["bistro"]
    assert store_key.tokens("Predajňa č. 2", keep_numbers=True) == ["2"]


def test_a_file_with_no_shop_words_is_still_another_file():
    day = {"store": "", "deliveryDate": "04.08.2026", "orderNumber": ""}
    bistro = dict(day, sourceFile="Kvetka BISTRO.xlsx", items=[{"name": "rožok", "quantity": 3}])
    plain = dict(day, sourceFile="Objednávka.xlsx", items=[{"name": "chlieb", "quantity": 2}])
    merged = order_customers.merge_by_day([bistro, plain])
    assert len(merged) == 1 and merged[0]["sourceFile"] == ""
    assert merged[0]["fileUnknown"] is True
    assert len(order_customers.merge_by_day([bistro, plain], by_file=True)) == 2
    again = order_customers.merge_by_day([plain, dict(plain, items=[{"name": "x",
                                                                     "quantity": 1}])])
    assert again[0]["sourceFile"] == "Objednávka.xlsx" and not again[0].get("fileUnknown")


def test_a_rule_senders_order_from_a_file_naming_no_shop_is_asked(pg):
    """A sender on one card with a learned BISTRO rule sends „Kvetka BISTRO.xlsx" and
    „Objednávka.xlsx" (no header): BISTRO's goods ship by its rule; the other file's order —
    another of its shops, which one the mail does not say — is asked, never shipped to the
    sender's own card by its address."""
    snapshot.import_snapshot(pg, CATALOG_CSV, TOWNS_CSV)
    _bistro_rule(pg, "2999000000111")
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('m-wl', 'ai_orders')")
    text = ("Subject: objednávka\n\nAttachments:\n"
            "===== Kvetka BISTRO.xlsx =====\n[Sheet: H]\nna 04.08.2026\n120x rožok 50g\n"
            "===== Objednávka.xlsx =====\n[Sheet: H]\nna 04.08.2026\n7x vianočka 400g\n")
    orders = [{"orderNumber": "", "deliveryDate": "04.08.2026", "recipientGroup": "",
               "store": "", "items": [{"name": n, "quantity": q, "unit": "ks",
                                       "sourceQuote": f"{q}x {n}"}]}
              for n, q in (("rožok 50g", 120), ("vianočka 400g", 7))]
    answers = [{"senderName": "Nákup", "senderEmail": SENDER, "companyName": "Reťazec",
                "isChangeRequest": False, "notes": "", "orders": orders},
               {"ean_edi": "", "confidence": 0.2}]
    answers += [{"gtin": g, "confidence": 0.95, "matchedCatalogName": "", "reason": ""}
                for g in ("G50", "VIA")]
    rec = Recorder()
    mail = {"message_id": "m-wl", "subject": "objednávka", "from_addr": SENDER,
            "from_name": "Nákup", "combined_text": text, "today": "2026-07-30"}
    result = pipeline.run(pg, _cfg(), mail, snapshot.latest_snapshot_id(pg),
                          client=ScriptedClient(answers), upload=rec.upload, post=rec.post)
    shipped = [(r["customer_ean"], [i["gtin"] for i in r["items"]])
               for r in result["order_results"] if r["status"] == "ok"]
    assert shipped == [("2999000000111", ["G50"])]
    assert len([q for q in teach.open_questions(pg) if q["kind"] == "customer"]) == 1


def test_an_items_name_locates_its_file_when_its_quote_was_reformatted():
    src = ("Attachments:\n===== OBJ A.xlsx =====\n[Sheet: H]\nRožok štandart 50g\t0.05\t120\n"
           "===== OBJ B.xlsx =====\n[Sheet: H]\nVianočka 400g\t0.23\t7\n")
    out = extract.verify({"orders": [{"deliveryDate": "04.08.2026", "store": "", "items": [
        {"name": "Rožok štandart 50g", "quantity": 120, "unit": "ks",
         "sourceQuote": "120x Rožok štandart 50g"}]}]}, src)
    assert [(o["sourceFile"], bool(o.get("fileUnknown"))) for o in out["orders"]] == [
        ("OBJ A.xlsx", False)]
