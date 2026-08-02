"""Hold an order while its question is unanswered — but only until the delivery date (#93).

Shipping the matched part of an order now and the taught line later would write TWO ORION
documents for ONE delivery day — exactly the #81.1 defect this project already fixed once
(40 and 10 delivered instead of 50). So a pending question holds the WHOLE order, and this
pins the four behaviours the issue names explicitly:

  * a held order uploads NOTHING until answered
  * answering releases it with EXACTLY ONE document
  * the deadline path ships what matched, exactly as it always has
  * a late answer after the deadline already shipped never uploads a second document
"""
import pytest

from app.config import Config
from app.orders import hold, pipeline, snapshot, teach

CATALOG_CSV = (
    "GTIN,Sklad,Názov,doplnok\n"
    "G50,1,Rožok štandart 50g,\n"
    "TOR,1,Torta čokoládová,\n"
)
CUSTOMER_CSV = (
    "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
    "Pekáreň Testovacia s.r.o.,2000000000001,Martin,Košútka 1,,,sklad@pekaren.sk\n"
)
MAIL = {"message_id": "m1", "subject": "Objednávka", "from_addr": "sklad@pekaren.sk",
        "from_name": "Sklad", "combined_text": "na 04.08.2026 prosím 120x rožok 50g, 5x torta",
        "today": "2026-07-30"}


class ScriptedClient:
    last_prompt_hash = "testprompt12"

    def __init__(self, answers):
        self.answers = list(answers)

    def json_call(self, system, user, schema, name="result"):
        if not self.answers:
            raise AssertionError(f"pipeline asked for an unscripted answer: {name}")
        return self.answers.pop(0)


def _answers(extra_item=None, extra_answer=None):
    items = [{"name": "rožok 50g", "quantity": 120, "unit": "ks",
             "sourceQuote": "120x rožok 50g"},
             {"name": "torta", "quantity": 5, "unit": "ks", "sourceQuote": "5x torta"}]
    if extra_item:
        items.append(extra_item)
    extract_answer = {
        "senderName": "Sklad", "senderEmail": "sklad@pekaren.sk",
        "companyName": "Pekáreň Testovacia s.r.o.", "isChangeRequest": False, "notes": "",
        "orders": [{"orderNumber": "", "deliveryDate": "04.08.2026", "recipientGroup": "",
                    "items": items}],
    }
    out = [extract_answer, {"ean_edi": "2000000000001", "confidence": 0.95},
           {"gtin": "G50", "confidence": 0.95, "matchedCatalogName": "", "reason": ""},
           {"gtin": "NO_MATCH", "confidence": 0.1, "matchedCatalogName": "",
            "reason": "nič sa nezhoduje"}]
    if extra_answer:
        out.append(extra_answer)
    return out


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


def _hold_one_order(pg, env):
    """Run the pipeline once: rožok is decided for free, torta asks — the order holds."""
    rec = Recorder()
    result = pipeline.run(pg, _cfg(), MAIL, env, client=ScriptedClient(_answers()),
                          upload=rec.upload, post=rec.post)
    assert result["status"] == "held"
    qid = teach.open_questions(pg)[0]["id"]
    return rec, qid


# --- a question about a line that gets RESCUED must not hold the rest of the order ------

def test_a_sibling_rescued_line_does_not_hold_an_otherwise_complete_order(pg, env):
    """Review finding on PR #116: the hold decision used to be gated on the PRE-merge,
    per-item question list. When "torta" appears twice in one order and the model
    resolves it confidently once and unconfidently the other time (real, documented model
    non-determinism — `match.apply_siblings`'s own CDR Lipová 6 / ČSB incident), the
    sibling rescue leaves the order fully, correctly resolved. It must ship immediately,
    not hold on a question that no longer decides anything."""
    mail = dict(MAIL, combined_text="na 04.08.2026 prosím 2x torta, 3x torta")
    extract_answer = {
        "senderName": "Sklad", "senderEmail": "sklad@pekaren.sk",
        "companyName": "Pekáreň Testovacia s.r.o.", "isChangeRequest": False, "notes": "",
        "orders": [{"orderNumber": "", "deliveryDate": "04.08.2026", "recipientGroup": "",
                    "items": [{"name": "torta", "quantity": 2, "unit": "ks",
                               "sourceQuote": "2x torta"},
                              {"name": "torta", "quantity": 3, "unit": "ks",
                               "sourceQuote": "3x torta"}]}],
    }
    answers = [extract_answer, {"ean_edi": "2000000000001", "confidence": 0.95},
              {"gtin": "TOR", "confidence": 0.9, "matchedCatalogName": "", "reason": ""},
              {"gtin": "NO_MATCH", "confidence": 0.1, "matchedCatalogName": "",
               "reason": "nič sa nezhoduje"}]
    rec = Recorder()
    result = pipeline.run(pg, _cfg(), mail, env, client=ScriptedClient(answers),
                          upload=rec.upload, post=rec.post)
    assert result["status"] == "ok", "sibling rescue already resolved everything — must ship"
    assert len(rec.uploads) == 1
    assert rec.uploads[0][1].count("LIN") == 1, "both torta lines merge into one card line"
    assert pg.execute("SELECT count(*) FROM held_orders").fetchone()[0] == 0
    # the (now moot) question the unconfident pass raised still exists — harmless, it still
    # teaches the wording for next time regardless of this order shipping without it
    assert len(teach.open_questions(pg)) == 1


# --- 1. nothing ships until answered ---------------------------------------

def test_a_held_order_uploads_nothing_until_answered(pg, env):
    rec, _qid = _hold_one_order(pg, env)
    assert rec.uploads == []
    row = pg.execute("SELECT status, delivery_date FROM held_orders").fetchone()
    assert row == ("held", "04.08.2026")
    assert pg.execute("SELECT count(*) FROM item_memory").fetchone()[0] == 0
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 0


# --- 2. answering releases it with exactly one document ---------------------

def test_answering_the_last_open_question_releases_it_with_one_document(pg, env):
    rec, qid = _hold_one_order(pg, env)
    teach.answer(pg, qid, gtin="TOR", card="Torta čokoládová", by="sklad")
    released = hold.release_for_question(pg, _cfg(), qid, upload=rec.upload, post=rec.post)
    assert len(released) == 1 and released[0]["status"] == "ok"
    assert len(rec.uploads) == 1
    content = rec.uploads[0][1]
    assert content.count("LIN") == 2, "both lines ship together, in the ONE document"
    assert pg.execute("SELECT status FROM held_orders").fetchone() == ("released",)
    # the human answer decided the line — proof the release re-checked memory, not just
    # replayed the original guess
    assert pg.execute(
        "SELECT count(*) FROM item_memory WHERE source='human'").fetchone()[0] == 1
    # the whole message is finally done (#93: it stayed unprocessed while it held)
    assert pg.execute(
        "SELECT processed FROM messages WHERE message_id='m1'").fetchone() == (True,)


def test_a_sibling_question_still_open_keeps_the_order_held(pg, env):
    """A held order may be waiting on more than one wording; releasing on the first answer
    would ship a still-guessed line the same way the #81.1 defect did."""
    rec = Recorder()
    answers = _answers(
        extra_item={"name": "šiška", "quantity": 3, "unit": "ks", "sourceQuote": "3x šiška"},
        extra_answer={"gtin": "NO_MATCH", "confidence": 0.1, "matchedCatalogName": "",
                     "reason": "nič sa nezhoduje"})
    # the citation check drops an item the email text never mentions (test_orders_pipeline.py)
    mail = dict(MAIL, combined_text=MAIL["combined_text"] + ", 3x šiška")
    result = pipeline.run(pg, _cfg(), mail, env, client=ScriptedClient(answers),
                          upload=rec.upload, post=rec.post)
    assert result["status"] == "held"
    qs = {q["wording"]: q["id"] for q in teach.open_questions(pg)}
    assert set(qs) == {"torta", "šiška"}

    teach.answer(pg, qs["torta"], gtin="TOR", card="Torta čokoládová", by="sklad")
    released = hold.release_for_question(pg, _cfg(), qs["torta"], upload=rec.upload,
                                         post=rec.post)
    assert released == [], "šiška is still open — the order must stay held"
    assert rec.uploads == []
    assert pg.execute("SELECT status FROM held_orders").fetchone() == ("held",)


# --- 3. the deadline sweep ships what matched, exactly as today -------------

def test_the_deadline_sweep_ships_what_matched_and_names_the_rest(pg, env):
    rec, _qid = _hold_one_order(pg, env)
    released = hold.release_due(pg, _cfg(), upload=rec.upload, post=rec.post,
                                today="2026-08-04")   # the delivery date itself
    assert len(released) == 1 and released[0]["status"] == "partial"
    assert len(rec.uploads) == 1
    assert rec.uploads[0][1].count("LIN") == 1, "only rožok shipped — torta stayed unmatched"
    assert pg.execute("SELECT status, release_reason FROM held_orders").fetchone() \
        == ("released", "deadline")
    assert pg.execute(
        "SELECT processed FROM messages WHERE message_id='m1'").fetchone() == (True,)


def test_the_deadline_sweep_leaves_orders_with_time_left_alone(pg, env):
    rec, _qid = _hold_one_order(pg, env)
    released = hold.release_due(pg, _cfg(), upload=rec.upload, post=rec.post,
                                today="2026-08-01")
    assert released == [] and rec.uploads == []
    assert pg.execute("SELECT status FROM held_orders").fetchone() == ("held",)


# --- 4. a late answer after the deadline already shipped never doubles -----

def test_a_late_answer_after_the_deadline_already_shipped_does_not_upload_twice(pg, env):
    rec, qid = _hold_one_order(pg, env)
    hold.release_due(pg, _cfg(), upload=rec.upload, post=rec.post, today="2026-08-04")
    assert len(rec.uploads) == 1
    # the answer arrives late — the held row is already 'released', so release_for_question
    # simply finds nothing left to release
    teach.answer(pg, qid, gtin="TOR", card="Torta čokoládová", by="sklad")
    released = hold.release_for_question(pg, _cfg(), qid, upload=rec.upload, post=rec.post)
    assert released == []
    assert len(rec.uploads) == 1, "the deadline shipment already happened — no second one"


def test_the_edi_ledger_itself_refuses_a_repeated_release_not_just_the_status_flag(pg, env):
    """Belt AND braces (#93 asks this to be PROVEN, not assumed): even if `_do_release` were
    somehow invoked twice for the SAME held row — a race, a retried request — it is
    `edi.claim_send`'s content-hash ledger that actually stops the duplicate ORION upload,
    not merely the `status='held'` guard the public release functions rely on."""
    rec, _qid = _hold_one_order(pg, env)
    row = hold.list_held(pg)[0]
    first = hold._do_release(pg, _cfg(), row, "deadline", rec.upload, rec.post,
                             redecide=False)
    assert first["status"] == "partial" and len(rec.uploads) == 1
    # bypass the public status guard on purpose, to prove the LEDGER is the real backstop
    second = hold._do_release(pg, _cfg(), row, "deadline", rec.upload, rec.post,
                              redecide=False)
    assert second["status"] == "ok", "claim_send refused the duplicate content"
    assert len(rec.uploads) == 1, "no second document reached ORION"


# --- the deadline itself ----------------------------------------------------

def test_is_past_deadline():
    assert hold.is_past_deadline("04.08.2026", "2026-08-04") is True
    assert hold.is_past_deadline("04.08.2026", "2026-08-05") is True
    assert hold.is_past_deadline("04.08.2026", "2026-08-03") is False
    assert hold.is_past_deadline("", "2026-08-03") is True, "no date to wait for"
