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
from app.orders import pipeline, snapshot, teach

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
    # exactly one short Odoo message for the whole e-mail (#139) — the item-level wording
    # ("torta") no longer belongs in Odoo, only on the linked nástenka
    assert len(rec.posts) == 1
    assert "torta" not in rec.posts[0]
    assert "čaká" in rec.posts[0].lower()
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
    # exactly one short Odoo message (#139): the order shipped partially AND raised a new
    # question, but it is still ONE processed e-mail — not one post per order plus one per
    # question. Item-level wording ("torta") is off Odoo; the count/wording of what's
    # unresolved is what's left.
    assert len(rec.posts) == 1
    assert "torta" not in rec.posts[0]
    assert "neúplných" in rec.posts[0].lower() or "neúplná" in rec.posts[0].lower()
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


def test_a_change_request_gets_its_own_wording_and_no_board_link(pg, env):
    """08-03 CÉDER incident: a change-of-order used to be mislabeled with the generic
    "treba zadať ručne" review wording AND wrongly carried a /sklad link — nothing is EVER
    queued for a change request (it is always resolved by hand in ORION), so the message
    must say "žiadosť o zmenu" and never point at the board."""
    rec = Recorder()
    result = pipeline.run(
        pg, _cfg(dashboard_base_url="http://46.224.130.35:8099", secret_key="s"), MAIL, env,
        client=ScriptedClient(_answers(change=True)), upload=rec.upload, post=rec.post)
    assert result["status"] == "review"
    assert len(rec.posts) == 1
    assert "žiadosť o zmenu" in rec.posts[0].lower()
    assert "treba zadať ručne" not in rec.posts[0].lower()
    assert "nástenke" not in rec.posts[0].lower()
    assert "/sklad/" not in rec.posts[0]


def test_a_change_request_with_an_unmatched_item_never_holds(pg, env):
    """#93 review finding: the hold condition explicitly excludes `is_change` — a change
    request is always handled by hand in ORION regardless of matching, so it must go
    straight to review (today's behaviour) even when one of its lines also raises a
    warehouse question, never sit waiting in held_orders."""
    rec = Recorder()
    result = pipeline.run(pg, _cfg(), MAIL, env,
                          client=ScriptedClient(_answers(items=(("torta", None, 0.2),),
                                                         change=True)),
                          upload=rec.upload, post=rec.post)
    assert result["status"] == "review"
    assert rec.uploads == []
    assert pg.execute("SELECT count(*) FROM held_orders").fetchone()[0] == 0
    # the wording still gets a question — a change request neither prevents nor auto-
    # resolves it, it just isn't why THIS order is stuck (it's stuck on being a change)
    assert len(teach.open_questions(pg)) == 1


# --- #159: an unrecognized customer is a WAREHOUSE QUESTION, not a dead end -------

def test_an_unknown_customer_with_time_left_holds_and_asks_who_it_is(pg, env):
    """The order must not silently leave the pipeline — it becomes ONE customer-kind
    question on the board and the whole order holds, exactly like an unmatched ITEM
    already does (#93), instead of the old dead end (`status="review"` with nothing ever
    written to `order_questions`/`held_orders` — #159's own root cause)."""
    rec = Recorder()
    answers = _answers()
    answers[0]["senderEmail"] = "cudzi@nikde.sk"      # the address is in no customer row
    answers[0]["companyName"] = "Neznáma firma s.r.o."
    answers[1] = {"ean_edi": "", "confidence": 0.1}
    mail = dict(MAIL, from_addr="cudzi@nikde.sk")
    result = pipeline.run(pg, _cfg(), mail, env, client=ScriptedClient(answers),
                          upload=rec.upload, post=rec.post)
    assert result["status"] == "held"
    assert rec.uploads == []
    assert len(rec.posts) == 1
    assert "čaká" in rec.posts[0].lower()
    qs = teach.open_questions(pg)
    assert len(qs) == 1 and qs[0]["kind"] == "customer"
    assert qs[0]["context"]["sender_email"] == "cudzi@nikde.sk"
    held = pg.execute(
        "SELECT customer_ean, delivery_date, status FROM held_orders").fetchone()
    assert held == ("", "04.08.2026", "held")
    row = pg.execute("SELECT processed FROM messages WHERE message_id='m1'").fetchone()
    assert row == (False,)


def test_an_unknown_customer_at_the_deadline_still_reviews_and_says_so(pg, env):
    """Once the delivery date itself arrives there is no more time left to wait for an
    answer — same deadline backstop principle #93 already applies to unmatched items, and
    the old "review, nothing queued" behaviour is exactly right here (nobody could ship an
    order with no customer to address it to)."""
    rec = Recorder()
    answers = _answers()
    answers[0]["senderEmail"] = "cudzi@nikde.sk"
    answers[0]["companyName"] = "Neznáma firma s.r.o."
    answers[1] = {"ean_edi": "", "confidence": 0.1}
    mail = dict(MAIL, from_addr="cudzi@nikde.sk", today="2026-08-04")   # today IS the delivery date
    result = pipeline.run(pg, _cfg(), mail, env, client=ScriptedClient(answers),
                          upload=rec.upload, post=rec.post)
    assert result["status"] == "review"
    assert rec.uploads == []
    assert "nájdený" in rec.posts[0].lower()
    assert pg.execute("SELECT count(*) FROM held_orders").fetchone()[0] == 0


def test_an_unknown_customer_question_ranks_candidates_by_address_signal(pg, env):
    """The candidate list a human sees is ranked using the delivery address found in the
    e-mail's own text — proven end-to-end through the pipeline, not just at
    `customer.candidates_for_question()`'s own unit level."""
    extra_customer_csv = CUSTOMER_CSV + (
        "Potraviny nie otraviny Žilina,2000000000861,Žilina,na bráne 4,,,"
        "evakozakova9@gmail.com\n")
    sid = snapshot.import_snapshot(pg, CATALOG_CSV, extra_customer_csv)
    rec = Recorder()
    answers = _answers()
    answers[0]["senderEmail"] = "cudzi@nikde.sk"
    answers[0]["companyName"] = ""
    answers[1] = {"ean_edi": "", "confidence": 0.1}
    mail = dict(MAIL, from_addr="cudzi@nikde.sk",
               combined_text=MAIL["combined_text"] + "\nNa bráne 4, 010 01 Žilina")
    result = pipeline.run(pg, _cfg(), mail, sid, client=ScriptedClient(answers),
                          upload=rec.upload, post=rec.post)
    assert result["status"] == "held"
    qs = teach.open_questions(pg)
    assert qs[0]["candidates"][0]["ean_edi"] == "2000000000861"


def test_the_same_order_is_never_uploaded_twice(pg, env):
    rec = Recorder()
    for _ in range(2):
        pipeline.run(pg, _cfg(), MAIL, env, client=ScriptedClient(_answers()),
                     upload=rec.upload, post=rec.post)
    assert len(rec.uploads) == 1, "the ledger must refuse the duplicate"


def test_a_failed_upload_releases_the_ledger_so_it_can_be_retried(pg, env):
    def failing(cfg, name, content):
        raise OSError("ORION unreachable")

    rec = Recorder()
    result = pipeline.run(pg, _cfg(), MAIL, env, client=ScriptedClient(_answers()),
                          upload=failing, post=rec.post)
    assert result["status"] == "error"
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 0
    assert pg.execute("SELECT count(*) FROM item_memory").fetchone()[0] == 0, \
        "nothing may be learnt from an order that never arrived"
    # the raw Python exception must never reach Odoo (#139 review finding) — a
    # skladníčka reading the message on her phone cannot do anything with "OSError(...)"
    assert len(rec.posts) == 1
    assert "OSError" not in rec.posts[0] and "ORION unreachable" not in rec.posts[0]


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
    # The wording must be one the model actually decides — never a free rung. "vianočka
    # 400g" IS a catalog card, so since #86 it is answered for free and a scripted model
    # failure never reaches it. It also cannot contain the single word "vianočka" at all
    # (#140): once catalog cards with a single core token count too, ANY wording containing
    # that one word would uniquely resolve to VIA via unique_core_card, again never
    # reaching the model. A product absent from this test's 3-card catalog dodges both.
    answers[0]["orders"][1]["items"][0]["name"] = "makový závin s orechmi"
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


# --- exactly one Odoo message per processed e-mail (#139) ------------------

def test_a_multi_date_multi_question_email_produces_exactly_one_odoo_message(pg, env):
    """The real trigger for #139: msg 5564 had 5 delivery dates and 4 new warehouse
    questions and produced 6 separate Odoo messages in 3 seconds — read on the phone as
    "a lot of orders failed". Here: two delivery dates, each with one wording the model
    cannot place, so both orders hold and raise a NEW question. Must be exactly ONE Odoo
    message for the whole e-mail, not one per order and one per question."""
    answers = _two_order_answers()
    answers[0]["orders"][0]["items"][0]["name"] = "babovka"
    answers[0]["orders"][1]["items"][0]["name"] = "štrúdľa"
    answers[2] = {"gtin": "NO_MATCH", "confidence": 0.2, "matchedCatalogName": "",
                 "reason": "nič sa nezhoduje"}
    answers[3] = {"gtin": "NO_MATCH", "confidence": 0.2, "matchedCatalogName": "",
                 "reason": "nič sa nezhoduje"}
    rec = Recorder()
    result = pipeline.run(pg, _cfg(), TWO_DATE_MAIL, env,
                          client=ScriptedClient(answers), upload=rec.upload, post=rec.post)
    assert result["status"] == "held"
    assert len(rec.uploads) == 0, "both orders hold — nothing ships yet"
    assert len(teach.open_questions(pg)) == 2, "both wordings raised their own question"
    assert len(rec.posts) == 1, "one processed e-mail must post exactly ONE Odoo message"
    assert "babovka" not in rec.posts[0] and "štrúdľa" not in rec.posts[0]


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


# --- #147: the stored question must always offer the engine's own candidate -----

STRUDLA_CATALOG_CSV = (
    "GTIN,Sklad,Názov,doplnok\n"
    "G50,1,Rožok štandart 50g,\n"
    "Z1,1,Zavin tvarohovo-cucoriedkovy 200g,\n"
    "Z2,1,Zavin makovo-visnovy 200g,\n"
    "Z3,1,Zavin orechovy 350gr,\n"
    "Z4,1,Zavin kakaovy 350gr,\n"
    "Z5,1,Zavin makovy 350gr,\n"
    "Z6,1,Zavin jablkovo-orechovy 200g,\n"
    "JS,1,Jablkova strudla 200g,\n"
    "MS,1,Makova strudla 200g,\n"
)


def test_the_stored_question_always_offers_the_engines_own_candidate(pg):
    """#147: the live nástenka offered 6 unrelated 'Závin' cards for the wording 'štrúdľa'
    and NOT the one card the engine itself named as its (rejected, low-confidence)
    candidate. Root cause: `pipeline._run` scores+truncates `item_cands` to 6 BEFORE the
    model call — it cannot know which card the model will name — and the SYNONYMS rule in
    `match._score()` scores every 'zavin' card at 75 vs. 'Jablková štrúdla's plain
    substring match at 65, pushing the real answer past the [:6] cutoff. The sklad had
    nothing to click."""
    sid = snapshot.import_snapshot(pg, STRUDLA_CATALOG_CSV, CUSTOMER_CSV)
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('m2', 'ai_orders')")
    mail = {"message_id": "m2", "subject": "Objednávka", "from_addr": "sklad@pekaren.sk",
            "from_name": "Sklad", "combined_text": "na 04.08.2026 prosím 2x štrúdľa",
            "today": "2026-07-30"}
    extract_answer = {
        "senderName": "Sklad", "senderEmail": "sklad@pekaren.sk",
        "companyName": "Pekáreň Testovacia s.r.o.", "isChangeRequest": False, "notes": "",
        "orders": [{"orderNumber": "", "deliveryDate": "04.08.2026", "recipientGroup": "",
                    "items": [{"name": "štrúdľa", "quantity": 2, "unit": "ks",
                               "sourceQuote": "2x štrúdľa"}]}],
    }
    answers = [extract_answer, {"ean_edi": "2000000000001", "confidence": 0.95},
               {"gtin": "JS", "confidence": 0.56, "matchedCatalogName": "", "reason": ""}]
    pipeline.run(pg, _cfg(), mail, sid, client=ScriptedClient(answers),
                upload=lambda *a: True, post=lambda *a, **k: None)
    qs = teach.open_questions(pg)
    assert len(qs) == 1
    cand_gtins = [c["gtin"] for c in qs[0]["candidates"]]
    assert cand_gtins and cand_gtins[0] == "JS", (
        f"the engine's own proposed candidate must head the list, got {cand_gtins}")


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


# --- #164: the full exit-matrix replay ------------------------------------------------
#
# Every terminal "reason" `pipeline.py` can produce, replayed in ONE place. Each row
# proves the invariant `_finish` now enforces: a "review"/"error" outcome is EITHER
# TECHNICAL (nothing a warehouse click could ever resolve — 0 new questions, no board
# link) OR carries at least one NEW board question — never neither. A new branch added
# later that forgets the board falls back to the `_finish` invariant's own generic `mail`
# question rather than going silent — this table pins the NORMAL path for every reason so
# a future regression shows up as a wrong row here, not as a silent production gap.

def test_the_full_exit_matrix_never_lets_a_resolvable_reason_go_silent(pg, env):
    def _open_qs():
        return teach.open_questions(pg)

    # 1. LLM_REFUSED — technical, no question, no board link.
    refusal_items = [{"name": f"item{i}", "quantity": 1, "unit": "ks",
                      "sourceQuote": f"1x item{i}"} for i in range(12)]
    refusal_answer = {
        "senderName": "Sklad", "senderEmail": "sklad@pekaren.sk",
        "companyName": "Pekáreň Testovacia s.r.o.", "isChangeRequest": False, "notes": "",
        "orders": [{"orderNumber": "", "deliveryDate": "04.08.2026", "recipientGroup": "",
                    "items": refusal_items}],
    }
    rec = Recorder()
    mail1 = dict(MAIL, message_id="mx1")
    before = len(_open_qs())
    result = pipeline.run(pg, _cfg(dashboard_base_url='http://test.local:8099', secret_key='s'), mail1, env, client=ScriptedClient([refusal_answer]),
                          upload=rec.upload, post=rec.post)
    assert result["status"] == "review" and result.get("question_ids", []) == []
    assert len(_open_qs()) == before, "LLM_REFUSED is technical — no board question"
    assert len(rec.posts) == 1 and "nástenke" not in rec.posts[0].lower()

    # 2. NO_ORDERS — resolvable: becomes a `mail`-kind board question.
    no_orders_answer = {
        "senderName": "Sklad", "senderEmail": "sklad@pekaren.sk",
        "companyName": "", "isChangeRequest": False, "notes": "", "orders": [],
    }
    rec = Recorder()
    mail2 = dict(MAIL, message_id="mx2")
    before = len(_open_qs())
    result = pipeline.run(pg, _cfg(dashboard_base_url='http://test.local:8099', secret_key='s'), mail2, env, client=ScriptedClient([no_orders_answer]),
                          upload=rec.upload, post=rec.post)
    assert result["status"] == "held" and len(result.get("question_ids", [])) == 1
    new = _open_qs()
    assert len(new) == before + 1
    assert new[-1]["kind"] == "mail"
    assert len(rec.posts) == 1 and "nástenke" in rec.posts[0].lower()

    # 3. DATE_CONFLICT — resolvable: becomes a `date`-kind board question, order(s) held.
    conflict_answer = {
        "senderName": "Sklad", "senderEmail": "sklad@pekaren.sk",
        "companyName": "Pekáreň Testovacia s.r.o.", "isChangeRequest": False, "notes": "",
        "orders": [{"orderNumber": "", "deliveryDate": "08.08.2026", "recipientGroup": "",
                    "items": [{"name": "rožok 50g", "quantity": 10, "unit": "ks",
                               "sourceQuote": "10x rožok 50g"}]}],
    }
    rec = Recorder()
    mail3 = dict(MAIL, message_id="mx3", subject="Objednávka 29.06.2026",
                combined_text="na 08.08.2026 prosím 10x rožok 50g")
    answers3 = [conflict_answer, {"ean_edi": "2000000000001", "confidence": 0.95}]
    before = len(_open_qs())
    before_held = pg.execute("SELECT count(*) FROM held_orders").fetchone()[0]
    result = pipeline.run(pg, _cfg(dashboard_base_url='http://test.local:8099', secret_key='s'), mail3, env, client=ScriptedClient(answers3),
                          upload=rec.upload, post=rec.post)
    assert result["status"] == "held" and len(result.get("question_ids", [])) == 1
    new = _open_qs()
    assert len(new) == before + 1 and new[-1]["kind"] == "date"
    assert pg.execute("SELECT count(*) FROM held_orders").fetchone()[0] == before_held + 1
    assert len(rec.posts) == 1 and "nástenke" in rec.posts[0].lower()

    # 4. CHANGE_REQUEST — technical, no question, no board link (own #159 wording).
    rec = Recorder()
    mail4 = dict(MAIL, message_id="mx4")
    before = len(_open_qs())
    result = pipeline.run(pg, _cfg(dashboard_base_url='http://test.local:8099', secret_key='s'), mail4, env, client=ScriptedClient(_answers(change=True)),
                          upload=rec.upload, post=rec.post)
    assert result["status"] == "review"
    assert len(_open_qs()) == before, "CHANGE_REQUEST is technical — no board question"
    assert len(rec.posts) == 1 and "nástenke" not in rec.posts[0].lower()

    # 5. ITEM_OPEN — resolvable: an already-raised item question threads through and the
    # order holds (matched customer, plenty of time before the delivery date).
    rec = Recorder()
    mail5 = dict(MAIL, message_id="mx5")
    before = len(_open_qs())
    result = pipeline.run(pg, _cfg(dashboard_base_url='http://test.local:8099', secret_key='s'), mail5, env,
                          client=ScriptedClient(_answers(items=(("torta", None, 0.2),))),
                          upload=rec.upload, post=rec.post)
    assert result["status"] == "held" and len(result.get("question_ids", [])) == 1
    new = _open_qs()
    assert len(new) == before + 1 and new[-1]["kind"] == "item"
    assert len(rec.posts) == 1 and "nástenke" in rec.posts[0].lower()

    # 6. UPLOAD_FAILED — technical, no question, no board link.
    def failing(cfg, name, content):
        raise OSError("ORION unreachable")

    rec = Recorder()
    mail6 = dict(MAIL, message_id="mx6")
    before = len(_open_qs())
    result = pipeline.run(pg, _cfg(dashboard_base_url='http://test.local:8099', secret_key='s'), mail6, env, client=ScriptedClient(_answers()),
                          upload=failing, post=rec.post)
    assert result["status"] == "error"
    assert len(_open_qs()) == before, "UPLOAD_FAILED is technical — no board question"
    assert len(rec.posts) == 1 and "nástenke" not in rec.posts[0].lower()

    # 7. DEDUP_ALREADY_SENT — ships "ok" both times; the invariant does not even apply
    # (only review/error are gated), and no NEW question is ever raised for a re-run.
    rec = Recorder()
    mail7 = dict(MAIL, message_id="mx7")
    pipeline.run(pg, _cfg(dashboard_base_url='http://test.local:8099', secret_key='s'), mail7, env, client=ScriptedClient(_answers()),
                upload=rec.upload, post=rec.post)
    before = len(_open_qs())
    result = pipeline.run(pg, _cfg(dashboard_base_url='http://test.local:8099', secret_key='s'), mail7, env, client=ScriptedClient(_answers()),
                          upload=rec.upload, post=rec.post)
    assert result["status"] == "ok"
    assert len(_open_qs()) == before
    assert len(rec.uploads) == 1, "the second run must never re-upload"


# --- #164: a taught mail_rules pattern short-circuits BEFORE the LLM call ---------------

def test_a_taught_ignore_rule_skips_extraction_entirely(pg, env):
    """`ignore` short-circuits before `extract.run` is ever called — the FIRST scripted
    answer would be the extraction call, so a ScriptedClient with ZERO answers proves the
    model was never invoked at all."""
    from app.orders import teach
    pg.execute(
        "INSERT INTO mail_rules (sender_norm, subject_key, action) VALUES (%s, %s, 'ignore')",
        (teach._sender_norm(MAIL["from_addr"]), teach.subject_key(MAIL["subject"])))
    rec = Recorder()
    result = pipeline.run(pg, _cfg(), MAIL, env, client=ScriptedClient([]),
                          upload=rec.upload, post=rec.post)
    assert result["status"] == "ok"
    assert rec.uploads == []
    assert len(rec.posts) == 1 and "ignorované" in rec.posts[0].lower()


def test_a_taught_manual_rule_skips_extraction_and_goes_straight_to_review(pg, env):
    from app.orders import teach
    pg.execute(
        "INSERT INTO mail_rules (sender_norm, subject_key, action) VALUES (%s, %s, 'manual')",
        (teach._sender_norm(MAIL["from_addr"]), teach.subject_key(MAIL["subject"])))
    rec = Recorder()
    result = pipeline.run(pg, _cfg(), MAIL, env, client=ScriptedClient([]),
                          upload=rec.upload, post=rec.post)
    assert result["status"] == "review"
    assert rec.uploads == []
    assert len(teach.open_questions(pg)) == 0, "manual is technical — no new question"
    assert "ručne" in rec.posts[0].lower()
