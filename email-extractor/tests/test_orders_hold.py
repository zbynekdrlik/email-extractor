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
import os

import pytest

from app.config import Config
from app.orders import customer, hold, pipeline, snapshot, teach

PG_DSN = os.environ.get("PG_TEST_DSN")

CATALOG_CSV = (
    "GTIN,Sklad,Názov,doplnok\n"
    "G50,1,Rožok štandart 50g,\n"
    "TOR,1,Torta čokoládová,\n"
    # A second torta so the bare wording "torta" (used throughout this file to exercise the
    # HOLD machinery, never matching accuracy) stays genuinely ambiguous and keeps asking —
    # after #140, match.unique_core_card() no longer requires 2+ core tokens on the card
    # side, so a single-candidate catalog would auto-resolve "torta" via unique_card and the
    # tests below would never see a held question at all.
    "TOR2,1,Torta vanilková,\n"
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
    # #118: hold.release_for_question opens its OWN separate connection (cfg.pg_dsn) to
    # lock the held_orders row — must be a real DSN, not "", or that connect() attempt
    # fails / hits the wrong database.
    base = dict(pg_dsn=PG_DSN, data_dir="/tmp", orders_shadow=False,
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


# --- #118: two near-simultaneous answers must release the order exactly once ---

def test_two_concurrent_answers_to_sibling_questions_release_it_exactly_once(pg, env):
    """Proven with two REAL, separate connections racing on the actual row lock — not a
    mock. Both sibling questions are answered first, then two threads each call
    `hold.release_for_question` for their OWN qid at (as close as possible to) the same
    instant. The FOR UPDATE serialization must let exactly ONE of them actually ship."""
    import threading
    import time

    import psycopg
    from _race import run_racers

    answers = _answers(
        extra_item={"name": "šiška", "quantity": 3, "unit": "ks", "sourceQuote": "3x šiška"},
        extra_answer={"gtin": "NO_MATCH", "confidence": 0.1, "matchedCatalogName": "",
                     "reason": "nič sa nezhoduje"})
    mail = dict(MAIL, combined_text=MAIL["combined_text"] + ", 3x šiška")
    rec = Recorder()
    result = pipeline.run(pg, _cfg(), mail, env, client=ScriptedClient(answers),
                          upload=rec.upload, post=rec.post)
    assert result["status"] == "held"
    qs = {q["wording"]: q["id"] for q in teach.open_questions(pg)}
    assert set(qs) == {"torta", "šiška"}

    teach.answer(pg, qs["torta"], gtin="TOR", card="Torta čokoládová", by="sklad")
    teach.answer(pg, qs["šiška"], gtin="G50", card="Rožok štandart 50g", by="sklad")

    slow_uploads = []
    upload_lock = threading.Lock()

    def slow_upload(cfg, name, content):
        time.sleep(0.3)          # widen the window a missing lock would let a racer exploit
        with upload_lock:
            slow_uploads.append((name, content))
        return True

    barrier = threading.Barrier(2)
    results: dict[str, list] = {}

    def release(key, qid):
        conn = psycopg.connect(PG_DSN, autocommit=True)
        try:
            barrier.wait(timeout=5)
            results[key] = hold.release_for_question(
                conn, _cfg(), qid, upload=slow_upload, post=lambda *a, **k: None)
        finally:
            conn.close()

    t1 = threading.Thread(target=release, args=("a", qs["torta"]), name="release-a")
    t2 = threading.Thread(target=release, args=("b", qs["šiška"]), name="release-b")
    # #291: a hand-rolled join(timeout=15) never kills a genuinely-stalled thread — it
    # left a stray connection holding this order's FOR UPDATE lock open, wedging every
    # later test's schema TRUNCATE. run_racers fails loudly + cleans up the stray
    # backend instead.
    run_racers(pg, [t1, t2], timeout=15, label="release_for_question")

    released_total = len(results.get("a") or []) + len(results.get("b") or [])
    assert released_total == 1, \
        f"exactly one of the two racing answers may release it, got {released_total}"
    assert len(slow_uploads) == 1, "exactly one document must actually reach ORION"
    assert pg.execute("SELECT status FROM held_orders").fetchone() == ("released",)


# --- #117: redecide must also gate on a real "today", not an unfiltered history ---

def test_redecide_passes_as_of_so_future_dated_history_never_decides_it(pg):
    """`hold._redecide` used to call `memory.resolve` with no `as_of` at all — an unanimous
    but FUTURE-dated shipment (one recorded for a delivery date after "today") could
    silently decide a re-check that runs on release, exactly the gap #117 filed one level
    below `pipeline.py`'s own (already-`as_of`-gated) first pass."""
    from app.orders import memory
    from app.orders.match import Decision

    for day in ("2026-08-10", "2026-08-11", "2026-08-12"):
        memory.remember(pg, "2000000000001", "rozok buduci", "G50", "Rožok štandart 50g",
                        delivered_on=day, source="ship")
    d = Decision(item_name="rozok buduci", gtin=None, card="", confidence=0.1,
                rule="unmatched", note="", review=False, trace={}, quantity=5, unit="ks")

    before = hold._redecide(pg, "2000000000001", [d], as_of="2026-08-05")
    assert before[0].rule == "unmatched", \
        "as_of before the shipments — the future-dated history must not decide it"

    after = hold._redecide(pg, "2000000000001", [d], as_of="2026-08-15")
    assert after[0].rule == "history_sure", \
        "once as_of is past the shipments, the same unanimous history may decide it"


# --- the deadline itself ----------------------------------------------------

def test_is_past_deadline():
    assert hold.is_past_deadline("04.08.2026", "2026-08-04") is True
    assert hold.is_past_deadline("04.08.2026", "2026-08-05") is True
    assert hold.is_past_deadline("04.08.2026", "2026-08-03") is False
    assert hold.is_past_deadline("", "2026-08-03") is True, "no date to wait for"


# --- #159: releasing an order held on an UNMATCHED CUSTOMER (kind='customer') -----

def _hold_unmatched_customer(pg, env):
    """Same shape as `_hold_one_order`, but for the customer-unknown branch: place a held
    row + a customer-kind question by hand, exactly what `pipeline._run` now does when
    `matched is None`."""
    from app.orders import teach

    qid = teach.ask_customer(
        pg, message_id="m1", sender_email="zilina@farmeria.sk",
        candidates=[{"ean_edi": "2000000000001", "name": "Pekáreň Testovacia s.r.o.",
                    "city": "Martin", "street": "Košútka 1", "address_match": True}],
        delivery_date="04.08.2026",
        context={"sender_email": "zilina@farmeria.sk", "sender_name": "Sklad",
                "company_name": "", "delivery_address_guess": ""})
    matched = customer.Matched(ean_edi="", name="", confidence=0.0, rule="unmatched", note="")
    order = {"deliveryDate": "04.08.2026", "orderNumber": "", "store": "", "items": [
        {"name": "rožok 50g", "quantity": 120, "unit": "ks"}]}
    from app.orders.match import Decision
    decisions = [Decision(item_name="rožok 50g", gtin="G50", card="Rožok štandart 50g",
                          confidence=0.95, rule="catalog_direct", note="", review=False,
                          trace={}, quantity=120, unit="ks")]
    hid = hold.place(pg, message_id="m1", matched=matched, order=order, decisions=decisions,
                     extracted={"isChangeRequest": False, "unverified": [], "notes": ""},
                     question_ids=[qid])
    return qid, hid


def test_setting_the_customer_updates_every_held_row_waiting_on_the_question(pg, env):
    qid, hid = _hold_unmatched_customer(pg, env)
    hold.set_customer(pg, qid, "2000000000001", "Pekáreň Testovacia s.r.o.")
    row = hold.get(pg, hid)
    assert row["customer_ean"] == "2000000000001"
    assert row["customer_name"] == "Pekáreň Testovacia s.r.o."


def test_answering_with_a_real_customer_then_releasing_ships_it(pg, env):
    qid, hid = _hold_unmatched_customer(pg, env)
    teach.answer_customer(pg, qid, ean_edi="2000000000001",
                          name="Pekáreň Testovacia s.r.o.", by="sklad")
    hold.set_customer(pg, qid, "2000000000001", "Pekáreň Testovacia s.r.o.")
    rec = Recorder()
    released = hold.release_for_question(pg, _cfg(), qid, upload=rec.upload, post=rec.post)
    assert len(released) == 1 and released[0]["status"] == "ok"
    assert len(rec.uploads) == 1
    assert pg.execute("SELECT status FROM held_orders WHERE id=%s", (hid,)).fetchone() \
        == ("released",)


def test_releasing_an_unknown_customer_answer_never_ships_but_becomes_visible(pg, env):
    """'neviem, kto to je' (#159) — the order must never ship with no real customer, but
    must reach a normal, VISIBLE terminal state — never left silently stuck 'held'."""
    qid, hid = _hold_unmatched_customer(pg, env)
    teach.answer_customer(pg, qid, ean_edi="", name="", by="sklad")
    rec = Recorder()
    released = hold.release_unknown_customer(pg, _cfg(), qid, post=rec.post)
    assert len(released) == 1 and released[0]["status"] == "review"
    assert rec.uploads == []
    row = pg.execute(
        "SELECT status, release_reason FROM held_orders WHERE id=%s", (hid,)).fetchone()
    assert row == ("released", "answered")
    assert len(rec.posts) == 1
    assert "nájdený" in rec.posts[0].lower() or "review" in rec.posts[0].lower() \
        or "doriešiť" in rec.posts[0].lower()
    # the message is finally done — never left unprocessed forever
    assert pg.execute(
        "SELECT processed FROM messages WHERE message_id='m1'").fetchone() == (True,)


def test_the_deadline_sweep_never_ships_a_still_unresolved_customer(pg, env):
    """Adversarial review finding on PR #161: `_do_release` used to reconstruct a
    `Matched` straight from `held_orders.customer_ean`/`customer_name` unconditionally —
    a `Matched` instance is ALWAYS truthy even with `ean_edi=""`, so `_ship_one`'s
    `if not matched:` guard never caught a still-unresolved customer reaching the
    deadline unanswered. Left unfixed, the order would ship to ORION with a blank
    customer EAN and a "Zakaznik" placeholder store name — exactly the wrong-customer-
    ship class of incident #159 exists to prevent, just relocated to the deadline sweep.
    It must instead become the SAME visible 'review' outcome an explicit 'neviem, kto to
    je' answer already gets — never shipped, never silently ships to nobody."""
    qid, hid = _hold_unmatched_customer(pg, env)
    rec = Recorder()
    released = hold.release_due(pg, _cfg(), upload=rec.upload, post=rec.post,
                                today="2026-08-04")   # the delivery date itself
    assert len(released) == 1 and released[0]["status"] == "review"
    assert rec.uploads == [], "must never ship to ORION with no real customer"
    row = pg.execute(
        "SELECT status, customer_ean FROM held_orders WHERE id=%s", (hid,)).fetchone()
    assert row == ("released", ""), "stays released, customer never fabricated"
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 0
    # the customer question itself is untouched by the deadline sweep — still open,
    # so a late answer (or a human reading /otazky) can still resolve it
    assert teach.get(pg, qid)["status"] == "open"


# --- #234: a customer added on /znalosti (instead of on the question card) must also
# unstick any order still waiting for it ------------------------------------------------

def test_a_customer_added_in_znalosti_releases_the_held_order(pg, env):
    qid, hid = _hold_unmatched_customer(pg, env)
    snapshot.upsert_customer(
        pg, override_id=None, orig_ean_edi=None, orig_street=None,
        ean_edi="7000000000200", name="Farméria Žilina", emails=["zilina@farmeria.sk"],
        city="Žilina", street="", zip_="")
    snapshot.rebuild_from_overrides(pg)
    rec = Recorder()
    released = hold.retry_unknown_customer_questions(pg, _cfg(), upload=rec.upload,
                                                      post=rec.post)
    assert len(released) == 1 and released[0]["status"] == "ok"
    assert len(rec.uploads) == 1
    assert teach.get(pg, qid)["status"] == "answered"
    assert pg.execute("SELECT status, customer_ean FROM held_orders WHERE id=%s",
                      (hid,)).fetchone() == ("released", "7000000000200")


def test_the_auto_retry_never_guesses_when_the_address_belongs_to_two_customers(pg, env):
    """`customer.resolve`'s addr rung refuses to pick when the SAME address belongs to
    more than one customer and there is no store header to disambiguate (never reached
    here) — the auto-retry must never guess either; the question stays open."""
    qid, hid = _hold_unmatched_customer(pg, env)
    for i, name in enumerate(("Farméria A", "Farméria B")):
        snapshot.upsert_customer(
            pg, override_id=None, orig_ean_edi=None, orig_street=None,
            ean_edi=f"700000000030{i}", name=name, emails=["zilina@farmeria.sk"],
            city="Žilina", street="", zip_="")
    snapshot.rebuild_from_overrides(pg)
    released = hold.retry_unknown_customer_questions(pg, _cfg())
    assert released == []
    assert teach.get(pg, qid)["status"] == "open"
    assert pg.execute(
        "SELECT status FROM held_orders WHERE id=%s", (hid,)).fetchone() == ("held",)


def test_the_auto_retry_ignores_a_customer_without_an_ean(pg, env):
    """A legacy blank-EAN snapshot row (sheet-derived, never possible through
    `upsert_customer` since #234 — but still possible from the sheet import path) must
    never be auto-picked: `matched.ean_edi` must be truthy before the retry acts."""
    qid, hid = _hold_unmatched_customer(pg, env)
    blank_ean_csv = (
        "Názov organizácie,EAN kód EDI,Obec,Ulica,E-mail\n"
        "Farméria Bez EAN,,Žilina,,zilina@farmeria.sk\n"
    )
    snapshot.import_snapshot(pg, CATALOG_CSV, blank_ean_csv)
    released = hold.retry_unknown_customer_questions(pg, _cfg())
    assert released == []
    assert teach.get(pg, qid)["status"] == "open"
    assert pg.execute(
        "SELECT status FROM held_orders WHERE id=%s", (hid,)).fetchone() == ("held",)


# --- #162: a genuinely ambiguous ITEM in a customer-unknown order must be re-asked,
# never silently shipped partial, once the customer is resolved --------------------

def _hold_unmatched_customer_with_ambiguous_item(pg, env):
    """Same as `_hold_unmatched_customer`, but the order's ONLY item is "torta" — with
    two catalog cards (TOR/TOR2) genuinely ambiguous, exactly the shape #162 describes:
    no item question was ever raised on the first pass (the customer was unknown, so
    `pipeline._run`'s `if not shadow and matched and ...` never fired), and the stored
    decision is "unmatched" with no gtin."""
    from app.orders import teach
    from app.orders.match import Decision

    qid = teach.ask_customer(
        pg, message_id="m1", sender_email="zilina@farmeria.sk",
        candidates=[{"ean_edi": "2000000000001", "name": "Pekáreň Testovacia s.r.o.",
                    "city": "Martin", "street": "Košútka 1", "address_match": True}],
        delivery_date="04.08.2026",
        context={"sender_email": "zilina@farmeria.sk", "sender_name": "Sklad",
                "company_name": "", "delivery_address_guess": ""})
    matched = customer.Matched(ean_edi="", name="", confidence=0.0, rule="unmatched", note="")
    order = {"deliveryDate": "04.08.2026", "orderNumber": "", "store": "", "items": [
        {"name": "torta", "quantity": 5, "unit": "ks"}]}
    decisions = [Decision(item_name="torta", gtin=None, card="", confidence=0.1,
                          rule="unmatched", note="nič sa nezhoduje", review=False, trace={},
                          quantity=5, unit="ks")]
    hid = hold.place(pg, message_id="m1", matched=matched, order=order, decisions=decisions,
                     extracted={"isChangeRequest": False, "unverified": [], "notes": ""},
                     question_ids=[qid])
    return qid, hid


def _hold_unmatched_customer_with_two_ambiguous_items(pg, env):
    """Same shape, but TWO independently ambiguous items — "torta" (the catalog's own
    genuine TOR/TOR2 ambiguity) and "neznáma pochúťka" (no catalog card matches it at
    all). Exercises the interaction the fix itself introduces: `_ask_still_ambiguous`
    can raise MORE THAN ONE fresh question for the SAME held row in one redecide, and
    `_release_locked` unions them into `question_ids` — review finding on PR #165."""
    from app.orders import teach
    from app.orders.match import Decision

    qid = teach.ask_customer(
        pg, message_id="m1", sender_email="zilina@farmeria.sk",
        candidates=[{"ean_edi": "2000000000001", "name": "Pekáreň Testovacia s.r.o.",
                    "city": "Martin", "street": "Košútka 1", "address_match": True}],
        delivery_date="04.08.2026",
        context={"sender_email": "zilina@farmeria.sk", "sender_name": "Sklad",
                "company_name": "", "delivery_address_guess": ""})
    matched = customer.Matched(ean_edi="", name="", confidence=0.0, rule="unmatched", note="")
    order = {"deliveryDate": "04.08.2026", "orderNumber": "", "store": "", "items": [
        {"name": "torta", "quantity": 5, "unit": "ks"},
        {"name": "neznáma pochúťka", "quantity": 2, "unit": "ks"}]}
    decisions = [
        Decision(item_name="torta", gtin=None, card="", confidence=0.1, rule="unmatched",
                note="nič sa nezhoduje", review=False, trace={}, quantity=5, unit="ks"),
        Decision(item_name="neznáma pochúťka", gtin=None, card="", confidence=0.1,
                 rule="unmatched", note="nič sa nezhoduje", review=False, trace={},
                 quantity=2, unit="ks"),
    ]
    hid = hold.place(pg, message_id="m1", matched=matched, order=order, decisions=decisions,
                     extracted={"isChangeRequest": False, "unverified": [], "notes": ""},
                     question_ids=[qid])
    return qid, hid


def test_two_fresh_item_questions_from_one_redecide_both_must_be_answered(pg, env):
    """#162 review finding: `_ask_still_ambiguous` can raise MORE THAN ONE fresh question
    in a single redecide. The held row must union both into `question_ids` and stay held
    until EVERY one of them is answered — answering just one must not release it, exactly
    like the pre-existing sibling-question guard for the already-known-customer path."""
    qid, hid = _hold_unmatched_customer_with_two_ambiguous_items(pg, env)
    teach.answer_customer(pg, qid, ean_edi="2000000000001",
                          name="Pekáreň Testovacia s.r.o.", by="sklad")
    hold.set_customer(pg, qid, "2000000000001", "Pekáreň Testovacia s.r.o.")
    rec = Recorder()
    released = hold.release_for_question(pg, _cfg(), qid, upload=rec.upload, post=rec.post)
    assert len(released) == 1 and released[0]["status"] == "held"
    assert rec.uploads == []

    fresh_qs = {q["wording"]: q["id"] for q in teach.open_questions(pg)}
    assert set(fresh_qs) == {"torta", "neznáma pochúťka"}
    row = hold.get(pg, hid)
    assert set(row["question_ids"]) >= set(fresh_qs.values()), \
        "the held row must track BOTH fresh questions, not just one"

    # answer only "torta" — the sibling "neznáma pochúťka" question is still open, so
    # the order must stay held, exactly like the pre-existing sibling-question guard
    teach.answer(pg, fresh_qs["torta"], gtin="TOR", card="Torta čokoládová", by="sklad")
    released = hold.release_for_question(pg, _cfg(), fresh_qs["torta"], upload=rec.upload,
                                         post=rec.post)
    assert released == [], "a sibling item question is still open — must stay held"
    assert rec.uploads == []
    assert hold.get(pg, hid)["status"] == "held"

    # answer the second — NOW it may finally ship, one document, both lines
    teach.answer(pg, fresh_qs["neznáma pochúťka"], gtin="G50", card="Rožok štandart 50g",
                by="sklad")
    released = hold.release_for_question(pg, _cfg(), fresh_qs["neznáma pochúťka"],
                                         upload=rec.upload, post=rec.post)
    assert len(released) == 1 and released[0]["status"] == "ok"
    assert len(rec.uploads) == 1
    assert rec.uploads[0][1].count("LIN") == 2
    assert hold.get(pg, hid)["status"] == "released"


def test_answering_the_customer_re_asks_a_still_ambiguous_item_instead_of_shipping(pg, env):
    """The #162 gap: answering the customer question must NOT ship the order with the
    "torta" line silently dropped — it must raise a FRESH item question and hold the
    order a second time."""
    qid, hid = _hold_unmatched_customer_with_ambiguous_item(pg, env)
    teach.answer_customer(pg, qid, ean_edi="2000000000001",
                          name="Pekáreň Testovacia s.r.o.", by="sklad")
    hold.set_customer(pg, qid, "2000000000001", "Pekáreň Testovacia s.r.o.")
    rec = Recorder()
    released = hold.release_for_question(pg, _cfg(), qid, upload=rec.upload, post=rec.post)

    assert len(released) == 1 and released[0]["status"] == "held", \
        "still ambiguous — must not ship (a 'held' result, never 'ok'/'partial')"
    assert rec.uploads == [], "never ship a line that is still genuinely unresolved"
    row = hold.get(pg, hid)
    assert row["status"] == "held", "stays held on the fresh item question"

    open_qs = teach.open_questions(pg)
    item_qs = [q for q in open_qs if q["wording"] == "torta"]
    assert len(item_qs) == 1, "a fresh, visible item question must exist — never a silent drop"
    assert item_qs[0]["customer_ean"] == "2000000000001"
    assert item_qs[0]["id"] in row["question_ids"], \
        "the held row must now track the new item question, or it can never release again"
    # the notification is visible, not just a log line
    assert len(rec.posts) == 1
    assert "otázk" in rec.posts[0].lower() or "held" in rec.posts[0].lower()


def test_answering_the_re_asked_item_then_finally_ships_the_order(pg, env):
    """Once the fresh item question from #162 is itself answered, the order finally
    ships — with both the customer AND the item resolved, exactly one document."""
    qid, hid = _hold_unmatched_customer_with_ambiguous_item(pg, env)
    teach.answer_customer(pg, qid, ean_edi="2000000000001",
                          name="Pekáreň Testovacia s.r.o.", by="sklad")
    hold.set_customer(pg, qid, "2000000000001", "Pekáreň Testovacia s.r.o.")
    rec = Recorder()
    hold.release_for_question(pg, _cfg(), qid, upload=rec.upload, post=rec.post)
    assert rec.uploads == []

    item_qid = next(q["id"] for q in teach.open_questions(pg) if q["wording"] == "torta")
    teach.answer(pg, item_qid, gtin="TOR", card="Torta čokoládová", by="sklad")
    released = hold.release_for_question(pg, _cfg(), item_qid, upload=rec.upload, post=rec.post)

    assert len(released) == 1 and released[0]["status"] == "ok"
    assert len(rec.uploads) == 1
    assert rec.uploads[0][1].count("LIN") == 1
    assert hold.get(pg, hid)["status"] == "released"
    assert pg.execute(
        "SELECT count(*) FROM item_memory WHERE source='human' AND item_key='torta'"
    ).fetchone()[0] == 1


def test_a_line_that_cannot_even_be_asked_about_is_never_silently_dropped(pg, env):
    """#162 constraint 4: if a still-ambiguous line cannot even be turned into a question
    (here: a wording that folds to an empty memory key — `teach.ask` itself refuses it),
    the order must still surface it visibly (stay held, name it in the Odoo notification)
    rather than silently shipping without it."""
    from app.orders import teach
    from app.orders.match import Decision

    qid = teach.ask_customer(
        pg, message_id="m1", sender_email="zilina@farmeria.sk",
        candidates=[{"ean_edi": "2000000000001", "name": "Pekáreň Testovacia s.r.o.",
                    "city": "Martin", "street": "Košútka 1", "address_match": True}],
        delivery_date="04.08.2026",
        context={"sender_email": "zilina@farmeria.sk", "sender_name": "Sklad",
                "company_name": "", "delivery_address_guess": ""})
    matched = customer.Matched(ean_edi="", name="", confidence=0.0, rule="unmatched", note="")
    order = {"deliveryDate": "04.08.2026", "orderNumber": "", "store": "", "items": [
        {"name": "!!!", "quantity": 1, "unit": "ks"}]}
    decisions = [Decision(item_name="!!!", gtin=None, card="", confidence=0.1, rule="unmatched",
                          note="nič sa nezhoduje", review=False, trace={}, quantity=1, unit="ks")]
    hid = hold.place(pg, message_id="m1", matched=matched, order=order, decisions=decisions,
                     extracted={"isChangeRequest": False, "unverified": [], "notes": ""},
                     question_ids=[qid])

    teach.answer_customer(pg, qid, ean_edi="2000000000001",
                          name="Pekáreň Testovacia s.r.o.", by="sklad")
    hold.set_customer(pg, qid, "2000000000001", "Pekáreň Testovacia s.r.o.")
    rec = Recorder()
    released = hold.release_for_question(pg, _cfg(), qid, upload=rec.upload, post=rec.post)

    assert len(released) == 1 and released[0]["status"] == "held"
    assert rec.uploads == [], "never ship a line nobody could even be asked about"
    assert hold.get(pg, hid)["status"] == "held"
    assert len(rec.posts) == 1
    assert "!!!" in rec.posts[0], "the unaskable item must be named, not silently dropped"


def test_a_customer_unknown_order_with_only_unambiguous_items_still_ships_immediately(pg, env):
    """Regression guard for the live message 5661 case (#159's driving incident): every
    line already resolved at first pass, only the customer was unknown — answering the
    customer question must ship right away, no needless second hold."""
    qid, hid = _hold_unmatched_customer(pg, env)
    teach.answer_customer(pg, qid, ean_edi="2000000000001",
                          name="Pekáreň Testovacia s.r.o.", by="sklad")
    hold.set_customer(pg, qid, "2000000000001", "Pekáreň Testovacia s.r.o.")
    rec = Recorder()
    released = hold.release_for_question(pg, _cfg(), qid, upload=rec.upload, post=rec.post)
    assert len(released) == 1 and released[0]["status"] == "ok"
    assert len(rec.uploads) == 1
    assert hold.get(pg, hid)["status"] == "released"


# --- #164: a held order can wait on TWO INDEPENDENT question kinds at once -------------
#
# The design's own named regression: date-conflict resolution (#164) can hold an order on
# a `date` question AND (independently) an `item` question at the same time. Releasing
# must wait for BOTH — answering only one must not ship a still-guessed value for the
# other — and once both are answered, the EDI must carry the HUMAN-PICKED date, not
# whatever the mail happened to say first.

def test_a_hold_with_date_and_item_questions_ships_only_once_both_are_answered(pg, env):
    from app.orders.match import Decision

    matched = customer.Matched(ean_edi="2000000000001", name="Pekáreň Testovacia s.r.o.",
                               confidence=1.0, rule="llm", note="")
    order = {"deliveryDate": "04.08.2026", "orderNumber": "", "recipientGroup": "",
             "store": "", "items": []}
    date_qid = teach.ask_date(
        pg, message_id="m1", dates=["04.08.2026", "05.08.2026"],
        reason="Predmet hovorí 04.08., objednávka je na 05.08. — dva rôzne dni",
        delivery_date="04.08.2026")
    item_qid = teach.ask(
        pg, message_id="m1", customer_ean="2000000000001",
        customer_name="Pekáreň Testovacia s.r.o.", wording="torta", quantity=5, unit="ks",
        candidates=[{"gtin": "TOR", "name": "Torta čokoládová"}])
    decisions = [Decision(item_name="torta", gtin=None, card="", confidence=0.1,
                          rule="unmatched", note="nič sa nezhoduje", review=False, trace={},
                          quantity=5, unit="ks")]
    hid = hold.place(pg, message_id="m1", matched=matched, order=order, decisions=decisions,
                     extracted={"isChangeRequest": False, "unverified": [], "notes": ""},
                     question_ids=[date_qid, item_qid])
    rec = Recorder()

    # Answer the DATE only — the item question is still open, so the order must stay held.
    hold.set_delivery_date(pg, date_qid, "05.08.2026")
    from psycopg.types.json import Json as _Json
    pg.execute("UPDATE order_questions SET status='answered', answer=%s, answered_by='sklad', "
              "answered_at=now() WHERE id=%s", (_Json({"choice": "05.08.2026"}), date_qid))
    released = hold.release_for_question(pg, _cfg(), date_qid, upload=rec.upload, post=rec.post)
    assert released == [], "the item question is still open — must not release yet"
    assert rec.uploads == []
    assert hold.get(pg, hid)["status"] == "held"
    # the answered date already landed on the row, ready for whenever it DOES release
    row = pg.execute("SELECT delivery_date, order_json->>'deliveryDate' FROM held_orders "
                     "WHERE id=%s", (hid,)).fetchone()
    assert row == ("05.08.2026", "05.08.2026")

    # Now answer the ITEM — every question id is answered, so it ships, WITH the
    # human-picked date (never the original 04.08. the mail's subject/body disagreed on).
    teach.answer(pg, item_qid, gtin="TOR", card="Torta čokoládová", by="sklad")
    released = hold.release_for_question(pg, _cfg(), item_qid, upload=rec.upload, post=rec.post)
    assert len(released) == 1 and released[0]["status"] == "ok"
    assert len(rec.uploads) == 1
    name, _content = rec.uploads[0]
    assert "20260805" in name, "EDI filename must carry the human-picked date (05.08), not 04.08"
    assert hold.get(pg, hid)["status"] == "released"
    assert pg.execute(
        "SELECT count(*) FROM item_memory WHERE source='human' AND gtin='TOR'"
    ).fetchone()[0] == 1


def test_release_due_never_ships_a_still_open_non_shippable_question(pg, env):
    """#164's `deadline_shippable` rule: a hold waiting on a `date` question (unlike an
    `item`-only hold) must NEVER auto-ship at the deadline — that would send an invented
    delivery date nobody confirmed. It converts to 'review' instead, with zero uploads,
    and the question stays open (never silently marked answered)."""
    matched = customer.Matched(ean_edi="2000000000001", name="Pekáreň Testovacia s.r.o.",
                               confidence=1.0, rule="llm", note="")
    order = {"deliveryDate": "05.08.2026", "orderNumber": "", "recipientGroup": "",
             "store": "", "items": []}
    date_qid = teach.ask_date(pg, message_id="m2", dates=["04.08.2026", "05.08.2026"],
                              reason="rozpor dátumu", delivery_date="05.08.2026")
    hid = hold.place(pg, message_id="m2", matched=matched, order=order, decisions=[],
                     extracted={"isChangeRequest": False, "unverified": [], "notes": ""},
                     question_ids=[date_qid])
    rec = Recorder()
    released = hold.release_due(pg, _cfg(), upload=rec.upload, post=rec.post,
                                today="2026-08-06")
    assert len(released) == 1 and released[0]["status"] == "review"
    assert rec.uploads == [], "the deadline must never ship an unconfirmed date"
    assert hold.get(pg, hid)["status"] == "released"
    assert teach.get(pg, date_qid)["status"] == "open", \
        "the deadline sweep must never silently answer the question for the warehouse"


# --- #360: the warehouse-confirmed quantity is what ships -------------------

def test_a_confirmed_quantity_ships_the_human_corrected_value_not_the_extracted_one(pg, env):
    """#360: the sklad can correct a misread quantity on the board before answering. The
    CONFIRMED quantity — not the originally extracted one — is what the released order
    ships, flowing through the SAME answer->release path (never a second ship path)."""
    rec, qid = _hold_one_order(pg, env)   # rožok 120 ships; torta (5) asks, order held
    teach.answer(pg, qid, gtin="TOR", card="Torta čokoládová", by="sklad", quantity=8)
    released = hold.release_for_question(pg, _cfg(), qid, upload=rec.upload, post=rec.post)
    assert len(released) == 1 and released[0]["status"] == "ok"
    assert len(rec.uploads) == 1
    content = rec.uploads[0][1]
    assert content.count("LIN") == 2, "both lines ship together in the ONE document"
    assert "8.000" in content, "the torta LIN carries the confirmed 8"
    assert "5.000" not in content, "never the originally extracted 5"
    assert "120.000" in content, "rožok's own uncorrected quantity is untouched"
    # the confirmed value also persists on the answered question row (audit + display)
    assert float(teach.get(pg, qid)["quantity"]) == 8


def test_no_confirmed_quantity_ships_the_extracted_quantity_unchanged(pg, env):
    """#360 backward-compat: answering WITHOUT correcting the quantity (teach.answer gets no
    quantity, so order_questions.quantity stays the extracted value) ships the ORIGINALLY
    extracted quantity (torta 5) exactly as before."""
    rec, qid = _hold_one_order(pg, env)
    teach.answer(pg, qid, gtin="TOR", card="Torta čokoládová", by="sklad")
    released = hold.release_for_question(pg, _cfg(), qid, upload=rec.upload, post=rec.post)
    assert len(released) == 1 and released[0]["status"] == "ok"
    content = rec.uploads[0][1]
    assert "5.000" in content, "the extracted torta quantity ships unchanged"
    assert "8.000" not in content


def test_a_correction_on_an_earlier_answered_question_of_a_multi_question_hold_still_ships(pg, env):
    """#360 regression (review-caught 🔴): a held order can wait on SEVERAL item questions.
    A quantity corrected on a question answered BEFORE the last one must STILL ship — the
    confirmed value is read back from order_questions.quantity at release for EVERY answered
    question, not only the last one processed. The buggy first cut threaded only the
    last-answered question's value and shipped the earlier line's original extracted qty."""
    mail = dict(MAIL, combined_text="na 04.08.2026 prosím 5x torta, 3x keksík")
    extract_answer = {
        "senderName": "Sklad", "senderEmail": "sklad@pekaren.sk",
        "companyName": "Pekáreň Testovacia s.r.o.", "isChangeRequest": False, "notes": "",
        "orders": [{"orderNumber": "", "deliveryDate": "04.08.2026", "recipientGroup": "",
                    "items": [{"name": "torta", "quantity": 5, "unit": "ks",
                               "sourceQuote": "5x torta"},
                              {"name": "keksík", "quantity": 3, "unit": "ks",
                               "sourceQuote": "3x keksík"}]}],
    }
    answers = [extract_answer, {"ean_edi": "2000000000001", "confidence": 0.95},
              {"gtin": "NO_MATCH", "confidence": 0.1, "matchedCatalogName": "", "reason": ""},
              {"gtin": "NO_MATCH", "confidence": 0.1, "matchedCatalogName": "", "reason": ""}]
    rec = Recorder()
    result = pipeline.run(pg, _cfg(), mail, env, client=ScriptedClient(answers),
                          upload=rec.upload, post=rec.post)
    assert result["status"] == "held"
    assert rec.uploads == []
    qs = {q["wording"]: q["id"] for q in teach.open_questions(pg)}
    assert set(qs) == {"torta", "keksík"}, "the order must hold on BOTH item questions"

    # answer the FIRST question (torta) with a corrected quantity 5 -> 8; order stays held
    teach.answer(pg, qs["torta"], gtin="TOR", card="Torta čokoládová", by="sklad", quantity=8)
    assert hold.release_for_question(pg, _cfg(), qs["torta"], upload=rec.upload,
                                     post=rec.post) == []
    assert rec.uploads == [], "still held while keksík is unanswered"

    # answer the LAST question (keksík); the order ships now — torta's EARLIER correction
    # must be honoured, never the original extracted 5
    teach.answer(pg, qs["keksík"], gtin="TOR2", card="Torta vanilková", by="sklad")
    released = hold.release_for_question(pg, _cfg(), qs["keksík"], upload=rec.upload,
                                         post=rec.post)
    assert len(released) == 1 and released[0]["status"] == "ok"
    content = rec.uploads[0][1]
    assert content.count("LIN") == 2
    assert "8.000" in content, "torta's correction from the EARLIER-answered question ships"
    assert "5.000" not in content, "never the originally extracted torta quantity"
    assert "3.000" in content, "keksík's own (uncorrected) quantity ships unchanged"
