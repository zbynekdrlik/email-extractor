"""#493 — one mail, several SHOPS, every order shipped to ONE card (the poistka).

The 2026-10-16 incident: a customer with five shops sends ONE mail with five xlsx files, one
per shop (headers „BISTRO", „Predajňa: Tržnica", „ŽILINA", …). The sender address sat on ONE
card only, so `customer.resolve` returned that card for EVERY order (`llm` / `exact_email` —
rules that never look at the shop) and all of the mail's ORDER files went to ORION under the
one card. Nothing noticed that several DIFFERENT shop headers collapsed onto the same card.

The guard pinned here: a mail with ≥2 orders whose distinct non-empty shop headers resolve
to the SAME card through a rule that does not look at the shop is never shipped silently —
every such order is HELD (on the #431 placeholder customer, never `matched=None`) and the
warehouse gets ONE `customer` question per shop.

All data is synthetic (the repo is public): invented shops, addresses and EANs.
"""
from app.config import Config
from app.orders import pipeline, snapshot, teach

CATALOG_CSV = (
    "GTIN,Sklad,Názov,doplnok\n"
    "G50,1,Rožok štandart 50g,\n"
    "G70,1,Rožok kváskový 70g,\n"
    "VIA,1,Vianočka 400g,\n"
)
SENDER = "nakup@retazec-test.sk"
# The incident's starting point: the sender's address is on ONE card only.
ONE_CARD_CSV = (
    "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
    f"BISTRO Mestečko Hlavná 1,2999000000101,Mestečko,Hlavná 1,,,{SENDER}\n"
    "Tržnica Dolina - Riadok 2,2999000000102,Dolina,Riadok 2,,,\n"
    "Predajňa Mestečko Lipová 3,2999000000103,Mestečko,Lipová 3,,,\n"
)

ATTACHMENTS = (
    "Subject: objednávka\n\nFrom: " + SENDER + "\n\nBody: Dobrý deň, potvrďte príjem "
    "objednávky.\n\nAttachments:\n"
    "===== OBJ Pekárová žena BISTRO.xlsx =====\n[Sheet: Hárok1]\nBISTRO\nna 04.08.2026\n"
    "120x rožok 50g\n"
    "===== OBJ Pekárová žena TR.xlsx =====\n[Sheet: Hárok1]\nPredajňa: Tržnica\n"
    "na 04.08.2026\n7x vianočka 400g\n"
    "===== OBJ Pekárová žena ZA.xlsx =====\n[Sheet: Hárok1]\nŽILINA\nna 04.08.2026\n"
    "5x rožok kváskový 70g\n"
)

MAIL = {"message_id": "m493", "subject": "objednávka", "from_addr": SENDER,
        "from_name": "Nákup", "combined_text": ATTACHMENTS, "today": "2026-07-30"}

STORES = (("BISTRO", "rožok 50g", 120, "G50"),
          ("Predajňa: Tržnica", "vianočka 400g", 7, "VIA"),
          ("ŽILINA", "rožok kváskový 70g", 5, "G70"))


class ScriptedClient:
    """Positional answers: extract, customer, then one per item (no mail-kind call here)."""

    last_prompt_hash = "testprompt12"

    def __init__(self, answers):
        self.answers = list(answers)

    def json_call(self, system, user, schema, name="result"):
        if not self.answers:
            raise AssertionError(f"pipeline asked for an unscripted answer: {name}")
        return self.answers.pop(0)


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


def _cfg(**kw):
    base = dict(pg_dsn="", data_dir="/tmp", orders_shadow=False,
                odoo_url="", odoo_api_key="", orders_channel_id=0)
    base.update(kw)
    return Config(**base)


def _answers(stores=STORES, change=False, date="04.08.2026"):
    orders = [{"orderNumber": "", "deliveryDate": date, "recipientGroup": "", "store": store,
               "items": [{"name": name, "quantity": qty, "unit": "ks",
                          "sourceQuote": f"{qty}x {name}"}]}
              for store, name, qty, _gtin in stores]
    extract = {"senderName": "Nákup", "senderEmail": SENDER, "companyName": "Reťazec test",
               "isChangeRequest": change, "notes": "", "orders": orders}
    out = [extract, {"ean_edi": "2999000000101", "confidence": 0.95}]
    out += [{"gtin": gtin, "confidence": 0.95, "matchedCatalogName": "", "reason": ""}
            for _s, _n, _q, gtin in stores]
    return out


def _env(pg):
    sid = snapshot.import_snapshot(pg, CATALOG_CSV, ONE_CARD_CSV)
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('m493', 'ai_orders')")
    return sid


def _customer_questions(pg):
    return [q for q in teach.open_questions(pg) if q["kind"] == "customer"]


def test_several_shops_collapsing_onto_one_card_are_held_and_asked_per_shop(pg):
    """The incident shape: 3 shop headers, all resolved to the sender's ONE card by the model
    pick — nothing may ship; each order is held and each shop gets its own question."""
    sid = _env(pg)
    rec = Recorder()
    result = pipeline.run(pg, _cfg(), MAIL, sid, client=ScriptedClient(_answers()),
                          upload=rec.upload, post=rec.post)
    assert rec.uploads == [], "no ORDER file may go to ORION for a collapsed multi-shop mail"
    assert result["status"] == "held"
    held = pg.execute("SELECT customer_ean, store, question_ids FROM held_orders "
                      "WHERE message_id = 'm493' ORDER BY id").fetchall()
    assert len(held) == 3
    assert {h[0] for h in held} == {""}, "held on the #431 placeholder, never the one card"
    qs = _customer_questions(pg)
    assert len(qs) == 3, "one customer question PER SHOP, never one for the whole sender"
    for store, *_rest in STORES:
        assert any(store in q["wording"] for q in qs), f"the question names the shop {store!r}"
    # every held order waits on exactly its own shop's question
    assert len({tuple(h[2]) for h in held}) == 3


def test_a_single_shop_mail_from_the_same_sender_still_ships(pg):
    """Regression guard: one shop (two dates) is not a collapse — it ships as before."""
    sid = _env(pg)
    rec = Recorder()
    stores = (("BISTRO", "rožok 50g", 120, "G50"),)
    result = pipeline.run(pg, _cfg(), MAIL, sid,
                          client=ScriptedClient(_answers(stores=stores)),
                          upload=rec.upload, post=rec.post)
    assert result["status"] == "ok"
    assert len(rec.uploads) == 1
    assert _customer_questions(pg) == []


def test_a_change_request_from_several_shops_never_ships_and_never_crashes(pg):
    """#431 + #421: a change request gets no board question, but the collapsed shops must
    not ship either (and never crash on matched=None)."""
    sid = _env(pg)
    rec = Recorder()
    pipeline.run(pg, _cfg(), MAIL, sid, client=ScriptedClient(_answers(change=True)),
                 upload=rec.upload, post=rec.post)
    assert rec.uploads == []
    assert _customer_questions(pg) == []


def test_a_date_conflict_does_not_send_all_shops_to_the_mail_level_card(pg):
    """The date-conflict branch held every order on the MAIL-level customer, so answering the
    date question would ship every shop to the one card. Each shop needs its own customer
    question next to the date question."""
    sid = _env(pg)
    rec = Recorder()
    mail = dict(MAIL, subject="objednávka 29.06.2026")
    pipeline.run(pg, _cfg(), mail, sid, client=ScriptedClient(_answers()),
                 upload=rec.upload, post=rec.post)
    assert rec.uploads == []
    held = pg.execute("SELECT customer_ean, question_ids FROM held_orders "
                      "WHERE message_id = 'm493' ORDER BY id").fetchall()
    assert len(held) == 3
    assert {h[0] for h in held} == {""}
    kinds = sorted(q["kind"] for q in teach.open_questions(pg))
    assert kinds.count("date") == 1 and kinds.count("customer") == 3
    assert all(len(h[1]) == 2 for h in held), "date question + the shop's own question"


def test_the_worker_sweep_never_answers_a_shop_question_with_the_senders_one_card(pg):
    """`hold.retry_unknown_customer_questions` auto-answers an open customer question when the
    sender resolves by `exact_email` — for a per-shop question that is exactly the collapse
    the poistka stopped (the sender's ONE card says nothing about the shop)."""
    import os

    from app.orders import hold
    sid = _env(pg)
    rec = Recorder()
    pipeline.run(pg, _cfg(), MAIL, sid, client=ScriptedClient(_answers()),
                 upload=rec.upload, post=rec.post)
    assert len(_customer_questions(pg)) == 3
    released = hold.retry_unknown_customer_questions(
        pg, _cfg(pg_dsn=os.environ["PG_TEST_DSN"]), upload=rec.upload, post=rec.post)
    assert released == []
    assert rec.uploads == []
    assert len(_customer_questions(pg)) == 3, "every shop question stays open for a human"
