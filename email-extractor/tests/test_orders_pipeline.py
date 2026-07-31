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


def test_an_unmatched_item_still_ships_the_rest_and_says_so(pg, env):
    rec = Recorder()
    answers = _answers(items=(("rožok 50g", "G50", 0.95), ("torta", None, 0.2)))
    result = pipeline.run(pg, _cfg(), MAIL, env, client=ScriptedClient(answers),
                          upload=rec.upload, post=rec.post)
    assert result["status"] == "partial"
    assert rec.uploads[0][1].count("LIN") == 1
    assert "torta" in rec.posts[0]
    assert "NEÚPLNÁ" in rec.posts[0].upper()
    # only the shipped item is remembered
    assert pg.execute("SELECT count(*) FROM item_memory").fetchone()[0] == 1


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
    answers = _two_order_answers()
    answers[3] = {"gtin": "NO_MATCH", "confidence": 0.1, "matchedCatalogName": "",
                  "reason": "nič sa nezhoduje"}
    rec = Recorder()
    result = pipeline.run(pg, _cfg(), TWO_DATE_MAIL, env, client=ScriptedClient(answers),
                          upload=rec.upload, post=rec.post)
    orders = result["order_results"]
    assert [o["status"] for o in orders] == ["ok", "review"]
    assert len(rec.uploads) == 1, "the failed order must not be uploaded"
    assert result["status"] == "partial", "part of the email shipped, part did not"
