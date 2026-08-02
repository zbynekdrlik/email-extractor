"""End-to-end pipeline + shadow mode (#67).

The stages are tested individually elsewhere; this pins how they compose, and above all
what the pipeline is allowed to DO to the outside world in each mode:

| mode | ORION upload | Odoo message | item memory | message marked |
|---|---|---|---|---|
| shadow | never | never | never | never |
| live   | once (ledger) | yes | on ship only | by the worker |

The model is scripted here (no network): what is under test is the composition, not the
model's judgement.
"""
import json

import pytest

from app.config import Config
from app.orders import pipeline, snapshot

CATALOG_CSV = (
    "GTIN,Sklad,Názov,doplnok\n"
    "G50,1,Rožok štandart 50g,\n"
    "G70,1,Rožok kváskový 70g,\n"
    "VIA,1,Vianočka 400g,\n"
)
CUSTOMER_CSV = (
    "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
    "Pekáreň Testovacia s.r.o.,2000000000001,Martin,Košútka 1,,,sklad@pekaren.sk\n"
)

MAIL = {"message_id": "m1", "subject": "Objednávka", "from_addr": "sklad@pekaren.sk",
        "from_name": "Sklad", "combined_text": "na 04.08.2026 prosím 120x rožok 50g, 7x vianočka 400g, "
        "5x torta, 3x chlieb", "today": "2026-07-30"}


class ScriptedClient:
    """Answers in the order the pipeline asks: extract, customer, then one per item."""

    last_prompt_hash = "testprompt12"

    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = []

    def json_call(self, system, user, schema, name="result"):
        self.asked.append(name)
        if not self.answers:
            raise AssertionError(f"pipeline asked for an unscripted answer: {name}")
        return self.answers.pop(0)


def _answers(items=(("rožok 50g", "G50", 0.95), ("vianočka 400g", "VIA", 0.95)),
             change=False):
    """extract answer, then the customer answer, then one answer per item."""
    # quantities must match MAIL's text, or the citation check drops the item before it
    # ever reaches the matcher (which is its own test, above)
    quantities = {"rožok 50g": 120, "vianočka 400g": 7, "torta": 5, "chlieb": 3}
    extracted_items = []
    for name, _gtin, _conf in items:
        qty = quantities[name]
        extracted_items.append({"name": name, "quantity": qty, "unit": "ks",
                                "sourceQuote": f"{qty}x {name}"})
    extract_answer = {
        "senderName": "Sklad", "senderEmail": "sklad@pekaren.sk",
        "companyName": "Pekáreň Testovacia s.r.o.", "isChangeRequest": change,
        "notes": "",
        "orders": [{"orderNumber": "", "deliveryDate": "04.08.2026",
                    "recipientGroup": "", "items": extracted_items}],
    }
    out = [extract_answer, {"ean_edi": "2000000000001", "confidence": 0.95}]
    for _name, gtin, conf in items:
        out.append({"gtin": gtin or "NO_MATCH", "confidence": conf,
                    "matchedCatalogName": "", "reason": ""})
    return out


@pytest.fixture
def env(pg):
    sid = snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('m1', 'ai_orders')")
    return sid


def _cfg(**kw):
    base = dict(pg_dsn="", data_dir="/tmp", orders_shadow=False,
                odoo_url="", odoo_api_key="", orders_channel_id=0)
    base.update(kw)
    return Config(**base)


class Recorder:
    def __init__(self):
        self.uploads = []
        self.posts = []

    def upload(self, cfg, name, content):
        self.uploads.append((name, content))
        return True

    def post(self, cfg, html, transport=None):
        self.posts.append(html)
        return {"id": 1}


# --- live run ------------------------------------------------------------

def test_a_clean_order_is_built_uploaded_reported_and_remembered(pg, env):
    rec = Recorder()
    result = pipeline.run(pg, _cfg(), MAIL, env, client=ScriptedClient(_answers()),
                          upload=rec.upload, post=rec.post)
    assert result["status"] == "ok"
    assert len(rec.uploads) == 1
    name, content = rec.uploads[0]
    assert name.startswith("ORDER_000001_20260804_") and content.startswith("HDR")
    assert content.count("LIN") == 2
    assert len(rec.posts) == 1 and "Pekáreň Testovacia" in rec.posts[0]
    # the shipped items become history for the next order
    assert pg.execute("SELECT count(*) FROM item_memory").fetchone()[0] == 2
    assert pg.execute(
        "SELECT count(*) FROM edi_sent").fetchone()[0] == 1


def test_the_result_carries_the_per_item_trace_for_order_items(pg, env):
    result = pipeline.run(pg, _cfg(), MAIL, env, client=ScriptedClient(_answers()),
                          upload=lambda *a: True, post=lambda *a, **k: None)
    assert len(result["items"]) == 2
    first = result["items"][0]
    assert first["gtin"] and first["rule"] and json.dumps(first["trace"])


def test_an_unmatched_item_with_time_left_holds_the_whole_order(pg, env):
    """#93: MAIL's delivery date (04.08.2026) is still ahead of its "today" (2026-07-30),
    so an unresolved line no longer ships the matched part now and the taught line later —
    that write TWO ORION documents for one delivery day (#81.1). It holds instead."""
    rec = Recorder()
    answers = _answers(items=(("rožok 50g", "G50", 0.95), ("torta", None, 0.2)))
    result = pipeline.run(pg, _cfg(), MAIL, env, client=ScriptedClient(answers),
                          upload=rec.upload, post=rec.post)
    assert result["status"] == "held"
    assert rec.uploads == [], "nothing ships while the order is held"
    # the question for the warehouse still reaches Odoo (#102) — that IS the visibility
    assert len(rec.posts) == 1
    assert "torta" in rec.posts[0] and "Neznáme znenie" in rec.posts[0]
    # nothing is learnt from an order that never shipped
    assert pg.execute("SELECT count(*) FROM item_memory").fetchone()[0] == 0
    held = pg.execute(
        "SELECT customer_ean, delivery_date, status FROM held_orders").fetchone()
    assert held == ("2000000000001", "04.08.2026", "held")
    # the message stays unprocessed while it waits
    row = pg.execute("SELECT processed FROM messages WHERE message_id='m1'").fetchone()
    assert row == (False,)


def test_an_unmatched_item_at_the_deadline_still_ships_the_rest_and_says_so(pg, env):
    """Once the delivery date itself has arrived there is no more time to wait — the order
    ships exactly as it always did, missing line named in the report."""
    rec = Recorder()
    answers = _answers(items=(("rožok 50g", "G50", 0.95), ("torta", None, 0.2)))
    mail = dict(MAIL, today="2026-08-04")   # today IS the delivery date
    result = pipeline.run(pg, _cfg(), mail, env, client=ScriptedClient(answers),
                          upload=rec.upload, post=rec.post)
    assert result["status"] == "partial"
    assert rec.uploads[0][1].count("LIN") == 1
    # a genuinely NEW question for the warehouse also reaches Odoo (#102), posted first
    # (while the item is being decided), then the main order report
    assert len(rec.posts) == 2
    assert "torta" in rec.posts[0] and "Neznáme znenie" in rec.posts[0]
    assert "torta" in rec.posts[1]
    assert "NEÚPLNÁ" in rec.posts[1].upper()
    # only the shipped item is remembered
    assert pg.execute("SELECT count(*) FROM item_memory").fetchone()[0] == 1
    assert pg.execute("SELECT count(*) FROM held_orders").fetchone()[0] == 0


def test_a_change_request_is_not_uploaded_and_names_the_original_file(pg, env):
    rec = Recorder()
    result = pipeline.run(pg, _cfg(), MAIL, env,
                          client=ScriptedClient(_answers(change=True)),
                          upload=rec.upload, post=rec.post)
    assert result["status"] == "review"
    assert rec.uploads == [], "a second ORION order must never be created"
    assert "ORDER_000001_20260804_" in rec.posts[0]


def test_an_unknown_customer_stops_the_document(pg, env):
    rec = Recorder()
    answers = _answers()
    answers[0]["senderEmail"] = "cudzi@nikde.sk"      # the address is in no customer row
    answers[0]["companyName"] = "Neznáma firma s.r.o."
    answers[1] = {"ean_edi": "", "confidence": 0.1}
    mail = dict(MAIL, from_addr="cudzi@nikde.sk")
    result = pipeline.run(pg, _cfg(), mail, env, client=ScriptedClient(answers),
                          upload=rec.upload, post=rec.post)
    assert result["status"] == "review"
    assert rec.uploads == []
    assert "nájdený" in rec.posts[0].lower()


def test_the_same_order_is_never_uploaded_twice(pg, env):
    rec = Recorder()
    for _ in range(2):
        pipeline.run(pg, _cfg(), MAIL, env, client=ScriptedClient(_answers()),
                     upload=rec.upload, post=rec.post)
    assert len(rec.uploads) == 1, "the ledger must refuse the duplicate"


def test_a_failed_upload_releases_the_ledger_so_it_can_be_retried(pg, env):
    def failing(cfg, name, content):
        raise OSError("ORION unreachable")

    result = pipeline.run(pg, _cfg(), MAIL, env, client=ScriptedClient(_answers()),
                          upload=failing, post=lambda *a, **k: None)
    assert result["status"] == "error"
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 0
    assert pg.execute("SELECT count(*) FROM item_memory").fetchone()[0] == 0, \
        "nothing may be learnt from an order that never arrived"


# --- shadow mode ---------------------------------------------------------

def test_shadow_touches_nothing_outside_its_own_tables(pg, env):
    rec = Recorder()
    result = pipeline.run(pg, _cfg(orders_shadow=True), MAIL, env,
                          client=ScriptedClient(_answers()),
                          upload=rec.upload, post=rec.post)
    assert result["status"] == "ok", "the run still produces its verdict"
    assert result["would_ship"] is True
    assert rec.uploads == [] and rec.posts == []
    assert pg.execute("SELECT count(*) FROM item_memory").fetchone()[0] == 0
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 0
    assert pg.execute("SELECT count(*) FROM email_events").fetchone()[0] == 0


def test_shadow_still_records_what_it_would_have_sent(pg, env):
    result = pipeline.run(pg, _cfg(orders_shadow=True), MAIL, env,
                          client=ScriptedClient(_answers()),
                          upload=lambda *a: True, post=lambda *a, **k: None)
    assert result["edi_preview"].startswith("HDR")
    assert result["edi_filename"].startswith("ORDER_000001_")


# --- comparison with n8n -------------------------------------------------

def test_the_diff_against_n8n_reports_only_real_differences(pg):
    """The shadow phase is judged by this diff; a cosmetic difference must not look like
    a disagreement, and a different card MUST."""
    ours = {"customer_ean": "2000000000001", "delivery_date": "04.08.2026",
            "items": [{"gtin": "G50", "quantity": 120}, {"gtin": "VIA", "quantity": 7}]}
    same = {"customer_ean": "2000000000001", "delivery_date": "04.08.2026",
            "items": [{"gtin": "VIA", "quantity": 7}, {"gtin": "G50", "quantity": 120.0}]}
    assert pipeline.diff(ours, same) == []

    other = {"customer_ean": "2000000000001", "delivery_date": "04.08.2026",
             "items": [{"gtin": "G70", "quantity": 120}, {"gtin": "VIA", "quantity": 7}]}
    differences = pipeline.diff(ours, other)
    assert differences and any("G50" in d or "G70" in d for d in differences)


def test_a_missing_n8n_run_is_reported_as_such(pg):
    ours = {"customer_ean": "1", "delivery_date": "04.08.2026", "items": []}
    assert pipeline.diff(ours, None) == ["n8n nemá výsledok pre túto správu"]


# --- one email, several orders (#78) -------------------------------------

TWO_DATE_MAIL = dict(MAIL, combined_text=(
    "na 04.08.2026 prosím 120x rožok 50g\n"
    "na 05.08.2026 prosím 7x vianočka 400g"))


def _two_order_answers():
    """One email, two delivery dates — what 20 of the 127 real ground-truth mails look like."""
    return [
        {"senderName": "Sklad", "senderEmail": "sklad@pekaren.sk",
         "companyName": "Pekáreň Testovacia s.r.o.", "isChangeRequest": False, "notes": "",
         "orders": [
             {"orderNumber": "A1", "deliveryDate": "04.08.2026", "recipientGroup": "",
              "items": [{"name": "rožok 50g", "quantity": 120, "unit": "ks",
                         "sourceQuote": "120x rožok 50g"}]},
             {"orderNumber": "A2", "deliveryDate": "05.08.2026", "recipientGroup": "",
              "items": [{"name": "vianočka 400g", "quantity": 7, "unit": "ks",
                         "sourceQuote": "7x vianočka 400g"}]}]},
        {"ean_edi": "2000000000001", "confidence": 0.95},
        {"gtin": "G50", "confidence": 0.95, "matchedCatalogName": "", "reason": ""},
        {"gtin": "VIA", "confidence": 0.95, "matchedCatalogName": "", "reason": ""},
    ]


def test_each_order_of_a_multi_date_email_is_reported_separately(pg, env):
    """n8n writes one EDI file per delivery date, so a flattened result cannot be scored:
    the second order's date and items would be invisible."""
    rec = Recorder()
    result = pipeline.run(pg, _cfg(), TWO_DATE_MAIL, env,
                          client=ScriptedClient(_two_order_answers()),
                          upload=rec.upload, post=rec.post)
    assert result["status"] == "ok"
    assert len(rec.uploads) == 2, "one EDI per order"

    orders = result["order_results"]
    assert [o["delivery_date"] for o in orders] == ["04.08.2026", "05.08.2026"]
    assert [o["order_number"] for o in orders] == ["A1", "A2"]
    assert [[i["gtin"] for i in o["items"]] for o in orders] == [["G50"], ["VIA"]]
    assert [[i["quantity"] for i in o["items"]] for o in orders] == [[120], [7]]
    assert [o["status"] for o in orders] == ["ok", "ok"]
    # every order's file is nameable, and the two differ
    names = [o["edi_filename"] for o in orders]
    assert all(names) and names[0] != names[1]


def test_a_multi_date_email_where_one_order_fails_reports_per_order_status(pg, env):
    """#93: the second order still has time before its own delivery date (05.08.2026 vs
    "today" 2026-07-30 in TWO_DATE_MAIL), so its unmatched line HOLDS the order rather
    than shipping nothing and reporting "review" the moment it is seen."""
    answers = _two_order_answers()
    # The wording must be one the model actually decides: "vianočka 400g" IS a catalog card,
    # so since #86 it is answered for free and a scripted model failure never reaches it.
    answers[0]["orders"][1]["items"][0]["name"] = "vianočka veľká sviatočná"
    answers[3] = {"gtin": "NO_MATCH", "confidence": 0.1, "matchedCatalogName": "",
                  "reason": "nič sa nezhoduje"}
    rec = Recorder()
    result = pipeline.run(pg, _cfg(), TWO_DATE_MAIL, env, client=ScriptedClient(answers),
                          upload=rec.upload, post=rec.post)
    orders = result["order_results"]
    assert [o["status"] for o in orders] == ["ok", "held"]
    assert len(rec.uploads) == 1, "the held order must not be uploaded yet"
    assert result["status"] == "held", "an email is not done while one of its orders holds"
    assert pg.execute(
        "SELECT delivery_date FROM held_orders").fetchone() == ("05.08.2026",)


# --- recipient groups are one order, one line (#81.1) ---------------------

GROUPS_MAIL = dict(MAIL, combined_text=(
    "na 04.08.2026 Vás prosíme objednať 120x rožok 50g na pacientov "
    "a 30x rožok 50g na zamestnancov"))


def _group_answers():
    """What the model really returns for "40ks na pacientov a 10ks na zamestnancov": one
    order per recipient group, same delivery date."""
    def order(group, qty):
        return {"orderNumber": "", "deliveryDate": "04.08.2026", "recipientGroup": group,
                "items": [{"name": "rožok 50g", "quantity": qty, "unit": "ks",
                           "sourceQuote": f"{qty}x rožok 50g"}]}
    return [
        {"senderName": "Sklad", "senderEmail": "sklad@pekaren.sk",
         "companyName": "Pekáreň Testovacia s.r.o.", "isChangeRequest": False, "notes": "",
         "orders": [order("pacienti", 120), order("zamestnanci", 30)]},
        {"ean_edi": "2000000000001", "confidence": 0.95},
        {"gtin": "G50", "confidence": 0.95, "matchedCatalogName": "", "reason": ""},
        {"gtin": "G50", "confidence": 0.95, "matchedCatalogName": "", "reason": ""},
    ]


def test_two_recipient_groups_on_one_day_are_one_order_with_the_summed_quantity(pg, env):
    """The real failure: 40 ks for patients + 10 ks for staff became TWO EDI files for one
    delivery date — two orders in ORION for one day — and the warehouse received 40, or 10,
    but never 50."""
    rec = Recorder()
    result = pipeline.run(pg, _cfg(), GROUPS_MAIL, env,
                          client=ScriptedClient(_group_answers()),
                          upload=rec.upload, post=rec.post)
    assert result["status"] == "ok"
    assert len(rec.uploads) == 1, "one delivery date is one EDI file"
    content = rec.uploads[0][1]
    assert content.count("LIN") == 1, "one card is one ORION line"
    assert len(result["order_results"]) == 1
    items = result["order_results"][0]["items"]
    assert [i["gtin"] for i in items] == ["G50"]
    assert items[0]["quantity"] == 150, "120 + 30, nothing lost"


def test_two_wordings_that_match_the_same_card_also_become_one_line(pg, env):
    answers = _group_answers()
    answers[0]["orders"][1]["items"][0]["name"] = "rožok štandart"
    answers[0]["orders"][1]["items"][0]["sourceQuote"] = "30x rožok 50g"
    rec = Recorder()
    result = pipeline.run(pg, _cfg(), GROUPS_MAIL, env, client=ScriptedClient(answers),
                          upload=rec.upload, post=rec.post)
    assert rec.uploads[0][1].count("LIN") == 1
    assert result["order_results"][0]["items"][0]["quantity"] == 150


def test_two_different_delivery_dates_still_stay_two_orders(pg, env):
    """The merge must key on the DAY, not flatten every order in the email.

    The mail names BOTH days on purpose: since the AGEL Levoča case (2026-08-01) an order
    for a day the body never mentions goes to a human, so a fixture that ordered for
    05.08. out of a mail that only says 04.08. would be testing the wrong thing.
    """
    answers = _group_answers()
    answers[0]["orders"][1]["deliveryDate"] = "05.08.2026"
    mail = dict(GROUPS_MAIL, combined_text=(
        "na 04.08.2026 Vás prosíme objednať 120x rožok 50g na pacientov "
        "a na 05.08.2026 30x rožok 50g na zamestnancov"))
    rec = Recorder()
    result = pipeline.run(pg, _cfg(), mail, env, client=ScriptedClient(answers),
                          upload=rec.upload, post=rec.post)
    assert len(rec.uploads) == 2
    assert [o["delivery_date"] for o in result["order_results"]] == ["04.08.2026", "05.08.2026"]


# --- who sent it: the envelope wins (#81.3) -------------------------------

def test_the_real_sender_decides_even_when_the_model_quotes_our_own_address(pg, env):
    """A reply quotes our own text, so the model reported `predaj@slovnormal.sk` as the
    sender and the customer went unresolved — the whole order was parked. The envelope
    address is who actually sent the mail; the model's reading is only a fallback."""
    answers = _answers()
    answers[0]["senderEmail"] = "predaj@slovnormal.sk"     # quoted, not the real sender
    answers[0]["senderName"] = "Predaj - Slovnormal"
    answers[0]["companyName"] = ""
    answers[1] = {"ean_edi": "", "confidence": 0.0}        # model finds no customer
    rec = Recorder()
    result = pipeline.run(pg, _cfg(), MAIL, env, client=ScriptedClient(answers),
                          upload=rec.upload, post=rec.post)
    assert result["customer_ean"] == "2000000000001", result.get("customer_rule")
    assert len(rec.uploads) == 1


def test_an_email_address_inside_a_display_name_still_matches(pg):
    """The customer sheet holds `Eva Kozakova <eva@example.sk>` in the e-mail cell."""
    from app.orders import snapshot
    sid = snapshot.import_snapshot(
        pg, CATALOG_CSV,
        "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
        "Potraviny Žilina,2000000000861,Žilina,Na bráne 4,,,Eva Kozakova <eva@example.sk>\n")
    customers = snapshot.load_customers(pg, sid)
    from app.orders import customer
    matched = customer.resolve(customers, "eva@example.sk", "", "")
    assert matched and matched.ean_edi == "2000000000861"


def test_the_same_card_in_different_units_stays_two_lines(pg, env):
    """Review finding: merging by card alone would add 2 kg to 3 ks and ship "5" of an
    ambiguous unit. Only lines that agree on the unit may be added up."""
    answers = _group_answers()
    answers[0]["orders"][1]["items"][0]["unit"] = "kg"
    rec = Recorder()
    result = pipeline.run(pg, _cfg(), GROUPS_MAIL, env, client=ScriptedClient(answers),
                          upload=rec.upload, post=rec.post)
    items = result["order_results"][0]["items"]
    assert [(i["quantity"], i["unit"]) for i in items] == [(120, "ks"), (30, "kg")]
    assert rec.uploads[0][1].count("LIN") == 2


# --- #86: the pipeline must not pay for a line the catalog already answers ---

def test_a_line_that_is_a_catalog_card_never_reaches_the_model(pg, env):
    """Measured 2026-07-31: 89 % of model spend is one call per ordered line, made even
    when the wording IS a card. Here the model is scripted with NO item answers at all, so
    if the pipeline asks for one the test fails on an unscripted answer."""
    mail = dict(MAIL, combined_text="na 04.08.2026 prosím 7x Vianočka 400g")
    extract_answer = {
        "senderName": "Sklad", "senderEmail": "sklad@pekaren.sk",
        "companyName": "Pekáreň Testovacia s.r.o.", "isChangeRequest": False, "notes": "",
        "orders": [{"orderNumber": "", "deliveryDate": "04.08.2026", "recipientGroup": "",
                    "items": [{"name": "Vianočka 400g", "quantity": 7, "unit": "ks",
                               "sourceQuote": "7x Vianočka 400g"}]}],
    }
    client = ScriptedClient([extract_answer, {"ean_edi": "2000000000001",
                                              "confidence": 0.95}])
    run = pipeline.run(pg, _cfg(orders_shadow=True), mail, env, client=client,
                       upload=lambda *a, **k: None, post=lambda *a, **k: None)

    assert client.asked == ["orders", "customer"], "an item call was paid for needlessly"
    item = run["items"][0]
    assert (item["gtin"], item["rule"]) == ("VIA", "catalog_name")
    assert run["order_results"][0]["items"][0]["quantity"] == 7


def test_two_shops_on_one_delivery_date_stay_two_orders(pg, env):
    """Beh 26 (#101): the merge key was (date, orderNumber), so two SHOPS ordering for the
    same day collapsed into one order — one EDI file, one shop's name, both shops' pastry.

    A recipient group is a note on an order (they share a delivery); a shop is a different
    customer with a different EAN, and merging those two is a wrong document in ORION.
    """
    orders = [
        {"deliveryDate": "31.07.2026", "orderNumber": "", "store": "GT1- Družby 35 BB",
         "items": [{"name": "Rožok štandart 50g", "quantity": 3, "unit": "ks"}]},
        {"deliveryDate": "31.07.2026", "orderNumber": "", "store": "GT2- 29 augusta 19 BB",
         "items": [{"name": "Rožok štandart 50g", "quantity": 5, "unit": "ks"}]},
    ]
    merged = pipeline._merge_by_day(orders)
    assert len(merged) == 2, "two shops are two orders"
    assert {o["store"] for o in merged} == {"GT1- Družby 35 BB", "GT2- 29 augusta 19 BB"}
    assert [len(o["items"]) for o in merged] == [1, 1]


def test_two_recipient_groups_of_the_same_shop_still_merge(pg, env):
    """The store split must not undo the group merge it sits next to."""
    orders = [
        {"deliveryDate": "31.07.2026", "orderNumber": "", "store": "GT1- Družby 35 BB",
         "recipientGroup": "pacienti",
         "items": [{"name": "Rožok štandart 50g", "quantity": 3, "unit": "ks"}]},
        {"deliveryDate": "31.07.2026", "orderNumber": "", "store": "GT1- Družby 35 BB",
         "recipientGroup": "zamestnanci",
         "items": [{"name": "Rožok štandart 50g", "quantity": 5, "unit": "ks"}]},
    ]
    merged = pipeline._merge_by_day(orders)
    assert len(merged) == 1 and len(merged[0]["items"]) == 2
