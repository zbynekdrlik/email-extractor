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
