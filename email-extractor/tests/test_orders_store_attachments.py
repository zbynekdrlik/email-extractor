"""#493 review round 8 — which attachment / block an order is, never guessed into one card.

* order FORMS of several shops share their product rows: an item is located by its quote
  first, its name only when the quote is in no file (a name in every file made a located
  order "unknown", and two shops shipped to the sender's one card);
* "several files" is the number of attachments, not of files an order was found in;
* a header block that names nothing („Predajňa:") is still a block of its own;
* in a shop context the street rung compares house numbers too („BISTRO Hlavná 5" is not the
  card on „Hlavná 12");
* header noise: a week word before a date takes only itself, „Objekt" is no order word.

Synthetic data only (the repo is public).
"""
from test_orders_store_no_guess import _bistro_rule
from test_orders_store_rules import (
    CATALOG_CSV,
    SENDER,
    TOWNS_CSV,
    Recorder,
    ScriptedClient,
    _cfg,
)

from app.orders import customer, extract, pipeline, snapshot, store_key, store_rules, teach

FORMS = ("Subject: objednávka\n\nAttachments:\n"
         "===== OBJ Pekárová žena PR.xlsx =====\n[Sheet: H]\nPredajňa:\nna 04.08.2026\n"
         "Rožok štandart 50g | 120\nVianočka 400g | 0\n"
         "===== OBJ Pekárová žena TR.xlsx =====\n[Sheet: H]\nPredajňa: Tržnica\nna 04.08.2026\n"
         "Rožok štandart 50g | 40\nVianočka 400g | 7\n")
GTIN = {"rožok 50g": "G50", "Rožok štandart 50g": "G50", "vianočka 400g": "VIA",
        "Vianočka 400g": "VIA"}


def _item(name, qty, quote=None):
    return {"name": name, "quantity": qty, "unit": "ks",
            "sourceQuote": quote if quote is not None else f"{qty}x {name}"}


def _order(store, *items):
    return {"orderNumber": "", "deliveryDate": "04.08.2026", "recipientGroup": "",
            "store": store, "items": list(items)}


def _run_mail(pg, mid, text, orders):
    pg.execute("INSERT INTO messages (message_id, category) VALUES (%s, 'ai_orders')", (mid,))
    answers = [{"senderName": "Nákup", "senderEmail": SENDER, "companyName": "Reťazec",
                "isChangeRequest": False, "notes": "", "orders": orders},
               {"ean_edi": "", "confidence": 0.2}]
    answers += [{"gtin": GTIN[i["name"]], "confidence": 0.95, "matchedCatalogName": "",
                 "reason": ""} for o in orders for i in o["items"]]
    rec = Recorder()
    mail = {"message_id": mid, "subject": "objednávka", "from_addr": SENDER,
            "from_name": "Nákup", "combined_text": text, "today": "2026-07-30"}
    result = pipeline.run(pg, _cfg(), mail, snapshot.latest_snapshot_id(pg),
                          client=ScriptedClient(answers), upload=rec.upload, post=rec.post)
    return result, rec


def _shipped(result):
    return [(r["customer_ean"], [i["gtin"] for i in r["items"]])
            for r in result["order_results"] if r["status"] == "ok"]


def test_forms_sharing_product_rows_keep_each_order_in_its_file():
    """The quote „Rožok štandart 50g | 120" is in the PR file only; the NAME is in both —
    the quote decides (round 7 counted the name too and lost the PR file)."""
    out = extract.verify({"orders": [
        _order("Predajňa:", _item("Rožok štandart 50g", 120, "Rožok štandart 50g | 120")),
        _order("Predajňa: Tržnica", _item("Rožok štandart 50g", 40, "Rožok štandart 50g | 40"),
               _item("Vianočka 400g", 7, "Vianočka 400g | 7"))]}, FORMS)
    assert [(o["sourceFile"], bool(o.get("fileUnknown"))) for o in out["orders"]] == [
        ("OBJ Pekárová žena PR.xlsx", False), ("OBJ Pekárová žena TR.xlsx", False)]


def test_two_shops_of_shared_forms_never_ship_to_the_senders_one_card(pg):
    snapshot.import_snapshot(pg, CATALOG_CSV, TOWNS_CSV)          # the sender on ONE card
    result, rec = _run_mail(pg, "m-forms", FORMS, [
        _order("Predajňa:", _item("Rožok štandart 50g", 120, "Rožok štandart 50g | 120")),
        _order("Predajňa: Tržnica", _item("Rožok štandart 50g", 40, "Rožok štandart 50g | 40"),
               _item("Vianočka 400g", 7, "Vianočka 400g | 7"))])
    assert rec.uploads == []
    assert len([q for q in teach.open_questions(pg) if q["kind"] == "customer"]) == 2


def test_an_untold_sibling_still_makes_several_files(pg):
    """A rule sender on one card: „Kvetka BISTRO.xlsx" + „Objednávka.xlsx", the BISTRO
    order's line is in BOTH files (its file is unknown) — the „Objednávka.xlsx" order is
    still one of several files: asked, never shipped to the sender's card by address."""
    snapshot.import_snapshot(pg, CATALOG_CSV, TOWNS_CSV)
    _bistro_rule(pg, "2999000000111")
    text = ("Subject: objednávka\n\nAttachments:\n"
            "===== Kvetka BISTRO.xlsx =====\n[Sheet: H]\nna 04.08.2026\n120x rožok 50g\n"
            "===== Objednávka.xlsx =====\n[Sheet: H]\nna 04.08.2026\n120x rožok 50g\n"
            "7x vianočka 400g\n")
    result, rec = _run_mail(pg, "m-untold", text, [
        _order("", _item("rožok 50g", 120)),
        _order("", _item("rožok 50g", 120), _item("vianočka 400g", 7))])
    assert _shipped(result) == []
    assert rec.uploads == []


def test_an_unnamed_block_next_to_a_named_block_is_its_own_shop(pg):
    """ONE attachment, two blocks: „BISTRO" (a rule) and an empty „Predajňa:" — the unnamed
    block is another shop of the mail: asked, never shipped to the sender's card."""
    snapshot.import_snapshot(pg, CATALOG_CSV, TOWNS_CSV)
    known = store_key.for_orders([{"store": "BISTRO", "sourceFile": "Objednávka.xlsx"}])[0]
    store_rules.remember(pg, sender_email=SENDER, key=known, ean_edi="2999000000111",
                         question_id=1, by="sklad")
    text = ("Subject: objednávka\n\nAttachments:\n"
            "===== Objednávka.xlsx =====\n[Sheet: H]\nBISTRO\nna 04.08.2026\n120x rožok 50g\n"
            "Predajňa:\nna 04.08.2026\n7x vianočka 400g\n")
    result, rec = _run_mail(pg, "m-block", text, [
        _order("BISTRO", _item("rožok 50g", 120)),
        _order("Predajňa:", _item("vianočka 400g", 7))])
    assert _shipped(result) == [("2999000000111", ["G50"])]
    assert len(rec.uploads) == 1
    keys = store_key.for_orders([{"store": "BISTRO"}, {"store": "Predajňa:"}])
    assert store_key.is_multi_store(keys)


FAMILY = [
    {"ean_edi": "2999000000601", "name": "BISTRO Mestečko", "emails": [SENDER],
     "city": "Mestečko", "street": "Hlavná 12", "zip": ""},
    {"ean_edi": "2999000000602", "name": "Tržnica Mestečko", "emails": [SENDER],
     "city": "Mestečko", "street": "Riečna 3", "zip": ""},
]


def test_the_street_rung_compares_house_numbers_in_a_shop_context():
    key = store_key.for_orders([{"store": "BISTRO Hlavná 5"}, {"store": "Tržnica"}])[0]
    assert customer.resolve(FAMILY, SENDER, "", "", key=key, multi_store=True) is None
    right = store_key.for_orders([{"store": "BISTRO Hlavná 12"}, {"store": "Tržnica"}])[0]
    hit = customer.resolve(FAMILY, SENDER, "", "", key=right, multi_store=True)
    assert (hit.ean_edi, hit.rule) == ("2999000000601", "store_address")
    # a sender's ordinary mail keeps the #101 comparison exactly as before #493
    plain = customer.resolve(FAMILY, SENDER, "", "", store="BISTRO Hlavná 5")
    assert (plain.ean_edi, plain.rule) == ("2999000000601", "store_address")


def test_a_week_word_before_a_date_and_an_objekt_keep_their_meaning():
    assert store_key.tokens("Týždeň 19.10-23.10. ŽILINA", keep_numbers=True) == ["zilina"]
    assert store_key.tokens("KW 42 BISTRO", keep_numbers=True) == ["bistro"]
    assert store_key.tokens("Objekt 1713", keep_numbers=True) == ["objekt", "1713"]


# --- review round 9 -------------------------------------------------------------------------

BODY = ("Subject: objednávka\n\nBody: Dobrý deň, posielam objednávku PR. A pre BISTRO ešte "
        "10x rožok štandart 50g.\n\nAttachments:\n"
        "===== OBJ Pekárová žena PR.xlsx =====\n[Sheet: H]\nPredajňa:\nna 04.08.2026\n"
        "Rožok štandart 50g | 120\nVianočka 400g | 7\n")
BODY_ORDERS = [
    _order("Predajňa:", _item("Rožok štandart 50g", 120, "Rožok štandart 50g | 120"),
           _item("Vianočka 400g", 7, "Vianočka 400g | 7")),
    _order("", _item("Rožok štandart 50g", 10, "10x rožok štandart 50g"))]


def test_a_body_order_is_never_placed_in_an_attachment_by_its_name():
    """The body line „10x rožok štandart 50g" is in the BODY: the item's name is also in the
    PR form, but a quote found in the body is no attachment's (round 7's name fallback put
    the body order into the PR file)."""
    out = extract.verify({"orders": BODY_ORDERS}, BODY)
    assert [(o["sourceFile"], bool(o.get("fileUnknown"))) for o in out["orders"]] == [
        ("OBJ Pekárová žena PR.xlsx", False), ("", True)]


def test_a_body_order_of_another_shop_never_takes_the_attachments_rule(pg):
    snapshot.import_snapshot(pg, CATALOG_CSV, TOWNS_CSV)
    known = store_key.for_orders([{"store": "Predajňa:",
                                   "sourceFile": "OBJ Pekárová žena PR.xlsx"}])[0]
    store_rules.remember(pg, sender_email=SENDER, key=known, ean_edi="2999000000113",
                         question_id=1, by="sklad")
    result, rec = _run_mail(pg, "m-body", BODY, BODY_ORDERS)
    assert _shipped(result) == [("2999000000113", ["G50", "VIA"])]
    assert len(rec.uploads) == 1


def test_a_rule_senders_lone_blank_block_is_asked(pg):
    """A sender with learned shop rules sends ONE form whose only block is „Predajňa:" and
    whose file name names nothing — which of its shops? Asked, never its address card."""
    snapshot.import_snapshot(pg, CATALOG_CSV, TOWNS_CSV)
    _bistro_rule(pg, "2999000000111")
    text = ("Subject: objednávka\n\nAttachments:\n"
            "===== Objednávka.xlsx =====\n[Sheet: H]\nPredajňa:\nna 04.08.2026\n"
            "7x vianočka 400g\n")
    result, rec = _run_mail(pg, "m-blank", text, [_order("Predajňa:", _item("vianočka 400g", 7))])
    assert _shipped(result) == [] and rec.uploads == []


def test_two_filled_in_price_lists_are_read_by_the_model():
    """`parse_table` reads only the first table: two shops' price lists lost the second
    shop's order. Such a mail is read by the model; one table still goes the table path."""
    hdr = "Názov\t%DPH\tVO bez DPH\tVO s DPH\n"
    text = ("Subject: objednávka\n\nAttachments:\n"
            "===== Cennik A.xlsx =====\n[Sheet: H]\nPredajňa: Alfa\n" + hdr
            + "Rožok štandart 50g\t20\t0.10\t0.12\t120\n\n"
            "===== Cennik B.xlsx =====\n[Sheet: H]\nPredajňa: Beta\n" + hdr
            + "Rožok štandart 50g\t20\t0.10\t0.12\t40\nVianočka 400g\t20\t1\t1.2\t7\n")
    assert extract.table_attachments(text) == 2
    orders = [_order("Predajňa: Alfa", _item("Rožok štandart 50g", 120,
                                             "Rožok štandart 50g\t20\t0.10\t0.12\t120")),
              _order("Predajňa: Beta", _item("Rožok štandart 50g", 40,
                                             "Rožok štandart 50g\t20\t0.10\t0.12\t40"),
                     _item("Vianočka 400g", 7, "Vianočka 400g\t20\t1\t1.2\t7"))]

    class Model:
        last_prompt_hash = "x"

        def json_call(self, *a, **k):
            return {"senderName": "N", "senderEmail": SENDER, "companyName": "",
                    "isChangeRequest": False, "notes": "", "orders": orders}
    out = extract.run(Model(), {"combined_text": text, "today": "2026-07-30",
                                "subject": "objednávka"})
    assert out["source"] == "model"
    assert [(o["store"], o["sourceFile"]) for o in out["orders"]] == [
        ("Predajňa: Alfa", "Cennik A.xlsx"), ("Predajňa: Beta", "Cennik B.xlsx")]
    one = text[:text.index("===== Cennik B.xlsx")]
    assert extract.table_attachments(one) == 1


def test_header_noise_keeps_a_year_and_knows_its_order_words():
    assert store_key.tokens("KW 43.2026 BISTRO", keep_numbers=True) == ["2026", "bistro"]
    assert store_key.tokens("Objed. 12345 BISTRO", keep_numbers=True) == ["bistro"]
    assert store_key.tokens("Objekt 1713", keep_numbers=True) == ["objekt", "1713"]


def test_a_date_alone_is_no_shop_block_and_a_glued_house_number_still_matches():
    keys = store_key.for_orders([{"store": "16.10."}, {"store": "Tržnica 17.10."}])
    assert not store_key.is_multi_store(keys), "a date-only header names no block"
    fam = [{"ean_edi": "2999000000701", "name": "GT jedna", "emails": [SENDER], "city": "BB",
            "street": "Družby 35", "zip": ""},
           {"ean_edi": "2999000000702", "name": "GT dva", "emails": [SENDER], "city": "BB",
            "street": "29. augusta 19", "zip": ""}]
    key = store_key.for_orders([{"store": "GT1 Družby 35BB"}, {"store": "GT2"}])[0]
    hit = customer.resolve(fam, SENDER, "", "", key=key, multi_store=True)
    assert (hit.ean_edi, hit.rule) == ("2999000000701", "store_address")


# --- review round 10 ------------------------------------------------------------------------

def test_a_body_order_with_a_reformatted_or_bare_quote_stays_out_of_the_forms(pg):
    """The model reformats the body quote („10 x rožok štandart 50 g") or quotes the bare
    name: the item's text is in the BODY first — never placed into the PR form by its name."""
    snapshot.import_snapshot(pg, CATALOG_CSV, TOWNS_CSV)
    known = store_key.for_orders([{"store": "Predajňa:",
                                   "sourceFile": "OBJ Pekárová žena PR.xlsx"}])[0]
    store_rules.remember(pg, sender_email=SENDER, key=known, ean_edi="2999000000113",
                         question_id=1, by="sklad")
    for mid, quote in (("m-ref", "10 x rožok štandart 50 g"), ("m-bare", "Rožok štandart 50g")):
        out = extract.verify({"orders": [BODY_ORDERS[0], _order(
            "", _item("Rožok štandart 50g", 10, quote))]}, BODY)
        assert [o["sourceFile"] for o in out["orders"]] == ["OBJ Pekárová žena PR.xlsx", ""]
        result, rec = _run_mail(pg, mid, BODY, [BODY_ORDERS[0], _order(
            "", _item("Rožok štandart 50g", 10, quote))])
        assert _shipped(result) == [("2999000000113", ["G50", "VIA"])], mid


def test_one_workbook_of_several_shops_price_lists_is_read_by_the_model():
    """One xlsx, a filled price list per shop on its own sheet, and the model names two shops:
    the code would read straight through both sheets into ONE order — the model reads it."""
    hdr = "Názov\t%DPH\tVO bez DPH\tVO s DPH\n"
    text = ("Subject: objednávka\n\nAttachments:\n===== Cennik.xlsx =====\n"
            "[Sheet: Alfa]\nPredajňa: Alfa\n" + hdr + "Rožok štandart 50g\t20\t0.10\t0.12\t120\n"
            "[Sheet: Beta]\nPredajňa: Beta\n" + hdr + "Rožok štandart 50g\t20\t0.10\t0.12\t40\n"
            "Vianočka 400g\t20\t1\t1.2\t7\n")
    orders = [_order("Predajňa: Alfa", _item("Rožok štandart 50g", 120,
                                             "Rožok štandart 50g\t20\t0.10\t0.12\t120")),
              _order("Predajňa: Beta", _item("Rožok štandart 50g", 40,
                                             "Rožok štandart 50g\t20\t0.10\t0.12\t40"),
                     _item("Vianočka 400g", 7, "Vianočka 400g\t20\t1\t1.2\t7"))]

    class Model:
        last_prompt_hash = "x"

        def json_call(self, *a, **k):
            return {"senderName": "N", "senderEmail": SENDER, "companyName": "",
                    "isChangeRequest": False, "notes": "", "orders": orders}
    out = extract.run(Model(), {"combined_text": text, "today": "2026-07-30",
                                "subject": "objednávka"})
    assert out["source"] == "model"
    assert [o["store"] for o in out["orders"]] == ["Predajňa: Alfa", "Predajňa: Beta"]


def test_a_date_only_header_never_makes_goods_files_shops():
    keys = store_key.for_orders([{"store": "16.10.", "sourceFile": "Objednávka pečivo.xlsx"},
                                 {"store": "", "sourceFile": "Objednávka chlieb.xlsx"}])
    assert not store_key.is_multi_store(keys)


# --- review round 16 ------------------------------------------------------------------------

def _street_family(*streets):
    return [{"ean_edi": f"299900000080{i}", "name": f"Predajňa {i}", "emails": [SENDER],
             "city": "Mestečko", "street": s, "zip": ""} for i, s in enumerate(streets, 1)]


def _street_hit(fam, header):
    key = store_key.for_orders([{"store": header}, {"store": "Iná predajňa"}])[0]
    hit = customer.resolve(fam, SENDER, "", "", key=key, multi_store=True)
    return (hit.ean_edi, hit.rule) if hit else None


def test_a_shop_code_in_the_identity_never_reaches_the_street_comparison():
    """Round 15 kept „T1" and a lone capital letter in the shop identity; the strict street
    rung must keep comparing streets as before: „19/a" vs the card „19/A", the „1" of „T1"
    is no house number, and an initial „M.R." on the card is no required word."""
    # two branches on ONE street are never told apart by their numbers (round 24): asked
    assert _street_hit(_street_family("Bajkalská 19", "Bajkalská 19/A"),
                       "BISTRO Bajkalská 19/a") is None
    assert _street_hit(_street_family("Hlavná 1", "Hlavná 7"), "T1 Hlavná") is None
    assert _street_hit(_street_family("Hlavná 1", "Hlavná 7"), "T1 Hlavná 7") is None
    assert _street_hit(_street_family("M.R. Štefánika 836/33", "Hlavná 7"),
                       "BISTRO Štefánika 836/33") == ("2999000000801", "store_address")


def test_a_house_numbers_letter_suffix_is_part_of_the_house_number():
    """„19/B" is not „19/A" — the letter of a house number counts, in any case and form
    („19/B", „19B", „19/b"); without a 19/B card the 19/B shop is asked (review round 17)."""
    assert _street_hit(_street_family("Hlavná 19/A", "Hlavná 7"), "BISTRO Hlavná 19/B") is None
    assert _street_hit(_street_family("Hlavná 19/A", "Hlavná 7"), "BISTRO Hlavná 19B") is None
    assert _street_hit(_street_family("Hlavná 19/A", "Hlavná 19/B"), "BISTRO Hlavná 19/b") is None


def test_a_house_number_with_its_letter_is_one_unit():
    """Review round 18: a bare „Hlavná 19" card matched any suffix the header carried — the
    19/B shop shipped to it next to a 19/A card. A suffixed house number is ONE unit: „19/B"
    matches neither „19" nor „19/A" (asked); „19" and „19/A" each find their own card."""
    fam = _street_family("Hlavná 19", "Hlavná 19/A")
    assert _street_hit(fam, "BISTRO Hlavná 19/B") is None
    assert _street_hit(fam, "BISTRO Hlavná 19") is None, "two branches on one street"
    assert _street_hit(fam, "BISTRO Hlavná 19/A") is None, "two branches on one street"
    # a letter written apart („19 A") is not glued — it may as well be a city initial
    # („7 B. Bystrica", round 19): asked, never a guessed card
    assert _street_hit(_street_family("Hlavná 19/A", "Hlavná 7"), "BISTRO Hlavná 19 A") is None


def test_a_city_initial_after_the_house_number_is_no_suffix():
    """„Hlavná 7 B. Bystrica" is house 7 in Banská Bystrica — never the „Hlavná 7/B" card
    (review round 19: a capital written apart was glued to the number)."""
    fam = _street_family("Hlavná 7", "Nová 3")
    assert _street_hit(fam, "Hlavná 7 B. Bystrica") == ("2999000000801", "store_address")
    assert _street_hit(fam, "BISTRO Hlavná 7 B.Bystrica") == ("2999000000801", "store_address")
    # beside a 7/B card on the same street: asked (round 24 — one street, two branches)
    assert _street_hit(_street_family("Hlavná 7", "Hlavná 7/B"), "Hlavná 7 B. Bystrica") is None


def test_a_letter_written_apart_after_a_house_number_is_asked():
    """„19 A" may be house 19/A or house 19 + an initial: a card whose street has that bare
    number (or the number with any letter) is no proof — asked (review round 20: „19 A" and
    „SNP 48 A" shipped to the bare-number card)."""
    assert _street_hit(_street_family("Hlavná 19", "Hlavná 19/A"), "BISTRO Hlavná 19 A") is None
    assert _street_hit(_street_family("Hlavná 19", "Hlavná 19/A"), "BISTRO Hlavná 19 b") is None
    assert _street_hit(_street_family("SNP 48", "SNP 48/A"), "BISTRO SNP 48 A") is None
    # „7 / B. Bystrica": house 7/B or house 7 + an initial — undecidable, asked (round 21)
    assert _street_hit(_street_family("Hlavná 7", "Hlavná 7/B"),
                       "BISTRO Hlavná 7 / B. Bystrica") is None


def test_an_unsure_house_number_vetoes_only_a_unique_winner():
    """Round 21: „19/A." (a suffix then a dot) shipped to the bare 19 card; and the unsure
    veto ran BEFORE the uniqueness count, so „Hlavná 5 a 7" (both cards named) shipped to
    the 7 card. A header naming two houses decides nothing; an unsure number is asked."""
    assert _street_hit(_street_family("Hlavná 19", "Hlavná 19/A"), "BISTRO Hlavná 19/A.") is None
    assert _street_hit(_street_family("Hlavná 5", "Hlavná 7"), "BISTRO Hlavná 5 a 7") is None
    assert _street_hit(_street_family("Hlavná 1", "Hlavná 12"), "Predajňa 1 A, Hlavná 12") is None


def test_an_undecidable_house_number_or_a_numberless_card_is_never_proof():
    """Round 22: gluing „7 / B." before the count silently picked one reading — another card
    whose words fit then won („Hlavná 5, 7 / B. Bystrica" → the 5 card; a numberless „Hlavná"
    card for „7 / B." or „19/A."). A header with an undecidable house number that a family
    card carries is asked; a card with NO house number is no proof for a header that has one
    („BISTRO Hlavná 12" is not the card „Hlavná")."""
    assert _street_hit(_street_family("Hlavná 5", "Hlavná 7"),
                       "BISTRO Hlavná 5, 7 / B. Bystrica") is None
    assert _street_hit(_street_family("Hlavná 7", "Hlavná"), "BISTRO Hlavná 7 / B. Bystrica") is None
    assert _street_hit(_street_family("Hlavná 19", "Hlavná"), "BISTRO Hlavná 19/A.") is None
    assert _street_hit(_street_family("Hlavná 7", "Hlavná"), "BISTRO Hlavná 12") is None
    fam = _street_family("Hlavná 19", "Hlavná 19/A", "Hlavná")
    assert _street_hit(fam, "BISTRO Hlavná 19/B") is None
    assert _street_hit(fam, "BISTRO Hlavná 19/A") is None, "three branches on one street"


def test_a_numberless_card_naming_the_header_still_blocks():
    """Round 23: dropping a numberless card from the count made „BISTRO Námestie SNP 48" ship
    to „SNP 48" although it names the „Námestie SNP" card's street too, and „GT1 Hlavná" ship
    to „Hlavná 1" (the 1 is the shop code). A numberless card blocks unless a fitting
    numbered card covers all its words; a code's number („GT1") is no house number."""
    assert _street_hit(_street_family("Námestie SNP", "SNP 48"),
                       "BISTRO Námestie SNP 48") is None
    assert _street_hit(_street_family("OC Centrum", "Hlavná 2"), "OC Centrum, Hlavná 2") is None
    assert _street_hit(_street_family("Hlavná", "Hlavná 1"), "GT1 Hlavná") is None


def test_the_strict_street_rung_needs_exactly_one_named_street():
    """Review round 24 (the cause of rounds 17-23 — each found a new way to tell branches on
    ONE street apart by their numbers, and each fix opened another): in a shop context the
    street decides only when the header names EXACTLY ONE family street (its words, numbers
    aside) and that card's house number is the header's. Branches on one street, or a header
    naming two streets, are asked once — then a rule decides."""
    fam = _street_family("Družby 35", "29. augusta 19")       # the #101 shape: two streets
    assert _street_hit(fam, "GT1- Družby 35 BB") == ("2999000000801", "store_address")
    assert _street_hit(fam, "GT2- 29 augusta 19 BB") == ("2999000000802", "store_address")
    assert _street_hit(fam, "BISTRO Družby 36") is None, "its house number is not the header's"
    assert _street_hit(_street_family("Hlavná 19", "Hlavná 7"), "BISTRO Hlavná 7") is None
    assert _street_hit(_street_family("Hlavná 7", "Nová 3"), "Hlavná 7, Nová 3") is None
