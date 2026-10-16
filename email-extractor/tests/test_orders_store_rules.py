"""#493 end to end — a multi-shop customer whose shops come from CODEX, the per-shop question,
the learned shop rule, its undo and its Kôš.

The incident shape with every card present (as after the CODEX import): shops whose header
names ONE card ship to it, a header naming two cards (two shops in one town) and an empty
one („PR", only in the file name) are HELD with one question per shop; the warehouse's answer
ships the held order to the chosen card AND teaches (sender + shop) -> card, so the next mail
of that shop ships with no question. Synthetic data only (the repo is public).
"""
import os

from app.board.services import audit
from app.config import Config
from app.httpapi import create_app
from app.orders import codex_customers, hold, pipeline, snapshot, store_rules, teach

PG_DSN = os.environ.get("PG_TEST_DSN")
SENDER = "nakup@retazec-test.sk"
CATALOG_CSV = (
    "GTIN,Sklad,Názov,doplnok\n"
    "G50,1,Rožok štandart 50g,\n"
    "G70,1,Rožok kváskový 70g,\n"
    "VIA,1,Vianočka 400g,\n"
    "CHL,1,Chlieb kváskový 1kg,\n"
)
BASE_CSV = (
    "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
    "Iný zákazník,2999000000999,Bratislava,Hlavná 9,,,iny@zakaznik-test.sk\n"
)
SHOPS = [
    {"ean_edi": "2999000000202", "name": "Tržnica Mestečko - Riadok 1",
     "trade_name": "Tržnica Mestečko", "city": "Mestečko", "street": "Riadok 1"},
    {"ean_edi": "2999000000203", "name": "Dolina Makovická", "trade_name": "",
     "city": "Dolina", "street": "Makovická"},
    {"ean_edi": "2999000000204", "name": "BISTRO Dolina Krátka 1", "trade_name": "BISTRO",
     "city": "Dolina", "street": "Krátka 1"},
]
# (header, file code, item, qty, gtin)
STORES = (("BISTRO", "BISTRO", "rožok 50g", 120, "G50"),
          ("Predajňa: Tržnica", "TR", "vianočka 400g", 7, "VIA"),
          ("DOLINA", "DO", "rožok kváskový 70g", 5, "G70"),
          ("PR", "PR", "chlieb kváskový", 3, "CHL"))


def _cfg():
    return Config(pg_dsn=PG_DSN, data_dir="/tmp", api_token="tok", dash_password="pw",
                  secret_key="t", orders_shadow=False, odoo_url="", odoo_api_key="",
                  orders_channel_id=0)


class ScriptedClient:
    last_prompt_hash = "testprompt12"

    def __init__(self, answers):
        self.answers = list(answers)

    def json_call(self, system, user, schema, name="result"):
        if not self.answers:
            raise AssertionError(f"unscripted answer: {name}")
        return self.answers.pop(0)


class Recorder:
    def __init__(self):
        self.uploads = []

    def upload(self, cfg, name, content):
        self.uploads.append((name, content))
        return True

    def post(self, cfg, html, transport=None):
        return {"id": 1}


def _mail(mid, stores=STORES):
    parts = ["Subject: objednávka\n\nFrom: " + SENDER + "\n\nBody: Potvrďte príjem.\n\n"
             "Attachments:\n"]
    for header, code, item, qty, _gtin in stores:
        parts.append(f"===== OBJ Pekárová žena {code}.xlsx =====\n[Sheet: H]\n{header}\n"
                     f"na 04.08.2026\n{qty}x {item}\n")
    return {"message_id": mid, "subject": "objednávka", "from_addr": SENDER,
            "from_name": "Nákup", "combined_text": "".join(parts), "today": "2026-07-30"}


def _answers(stores=STORES):
    orders = [{"orderNumber": "", "deliveryDate": "04.08.2026", "recipientGroup": "",
               "store": header, "items": [{"name": item, "quantity": qty, "unit": "ks",
                                           "sourceQuote": f"{qty}x {item}"}]}
              for header, _c, item, qty, _g in stores]
    out = [{"senderName": "Nákup", "senderEmail": SENDER, "companyName": "Reťazec",
            "isChangeRequest": False, "notes": "", "orders": orders},
           # the model is SURE about the BISTRO card — it must never decide a shop
           {"ean_edi": "2999000000204", "confidence": 0.97}]
    out += [{"gtin": g, "confidence": 0.95, "matchedCatalogName": "", "reason": ""}
            for *_r, g in stores]
    return out


def _env(pg):
    snapshot.import_snapshot(pg, CATALOG_CSV, BASE_CSV)
    codex_customers.replace_customers(
        pg, [dict(s, emails=[SENDER], zip="", codex_id=s["ean_edi"]) for s in SHOPS])
    return snapshot.rebuild_from_overrides(pg)


def _run(pg, mid, stores=STORES):
    pg.execute("INSERT INTO messages (message_id, category) VALUES (%s, 'ai_orders')", (mid,))
    rec = Recorder()
    result = pipeline.run(pg, _cfg(), _mail(mid, stores), snapshot.latest_snapshot_id(pg),
                          client=ScriptedClient(_answers(stores)), upload=rec.upload,
                          post=rec.post)
    return result, rec


def _shop_questions(pg):
    return {q["context"].get("store"): q for q in teach.open_questions(pg)
            if q["kind"] == "customer"}


def _client():
    app = create_app(_cfg())
    app.testing = True
    c = app.test_client()
    c.post("/login", data={"password": "pw"})
    return c


def test_each_shop_goes_to_its_card_and_the_undecidable_ones_are_asked_once(pg):
    _env(pg)
    result, rec = _run(pg, "m1")
    shipped = {r["store"]: r["customer_ean"] for r in result["order_results"]
               if r["status"] == "ok"}
    assert shipped == {"BISTRO": "2999000000204", "Predajňa: Tržnica": "2999000000202"}
    assert len(rec.uploads) == 2
    qs = _shop_questions(pg)
    assert set(qs) == {"DOLINA", "PR"}
    dolina = qs["DOLINA"]
    assert "predajňa „DOLINA“" in dolina["wording"]
    offered = [c["ean_edi"] for c in dolina["candidates"]]
    assert set(offered[:2]) == {"2999000000203", "2999000000204"}, "the two Dolina shops first"
    held = pg.execute("SELECT store, customer_ean FROM held_orders WHERE message_id = 'm1' "
                      "ORDER BY store").fetchall()
    assert held == [("DOLINA", ""), ("PR", "")]


def test_the_answer_ships_the_held_shop_and_the_next_mail_needs_no_question(pg, monkeypatch):
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    _env(pg)
    _run(pg, "m1")
    q = _shop_questions(pg)["DOLINA"]
    r = _client().post(f"/api/board/questions/{q['id']}/answer",
                       json={"ean_edi": "2999000000203", "name": "Dolina Makovická"})
    assert r.status_code == 200, r.get_json()
    assert pg.execute("SELECT status, customer_ean FROM held_orders WHERE message_id = 'm1' "
                      "AND store = 'DOLINA'").fetchone() == ("released", "2999000000203")
    rule = pg.execute("SELECT sender_email, store_key, ean_edi, question_id, created_by "
                      "FROM customer_store_rules WHERE deleted_at IS NULL").fetchall()
    assert rule == [(SENDER, "dolina", "2999000000203", q["id"], "sklad")]
    assert teach.get(pg, q["id"])["answer"] == {"email_added": False, "store_rule": 1}
    assert pg.execute("SELECT count(*) FROM audit_log WHERE table_name = "
                      "'customer_store_rules' AND action = 'create'").fetchone()[0] == 1
    # next week's mail: the same shop ships to the taught card, no question
    result, rec = _run(pg, "m2", stores=[("DOLINA", "DO", "rožok kváskový 70g", 6, "G70")])
    assert result["status"] == "ok" and len(rec.uploads) == 1
    assert result["order_results"][0]["customer_ean"] == "2999000000203"
    assert "DOLINA" not in _shop_questions(pg)


def test_undo_takes_the_rule_back_and_keeps_the_cards_addresses(pg, monkeypatch):
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    _env(pg)
    _run(pg, "m1")
    q = _shop_questions(pg)["DOLINA"]
    c = _client()
    c.post(f"/api/board/questions/{q['id']}/answer",
           json={"ean_edi": "2999000000203", "name": "Dolina Makovická"})
    assert c.post(f"/api/board/questions/{q['id']}/undo").status_code == 200
    assert store_rules.for_sender(pg, SENDER) == []
    assert pg.execute("SELECT count(*) FROM audit_log WHERE table_name = "
                      "'customer_store_rules' AND action = 'delete'").fetchone()[0] == 1
    cards = {r["ean_edi"]: r for r in snapshot.customers_for_management(pg)}
    assert SENDER in cards["2999000000203"]["emails"], "the CODEX address survives the undo"
    assert teach.get(pg, q["id"])["status"] == "open"


def test_the_kos_can_take_a_rule_back_and_bring_it_back(pg, monkeypatch):
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    _env(pg)
    _run(pg, "m1")
    q = _shop_questions(pg)["PR"]
    _client().post(f"/api/board/questions/{q['id']}/answer",
                   json={"ean_edi": "2999000000202", "name": "Tržnica Mestečko - Riadok 1"})
    created = pg.execute("SELECT id FROM audit_log WHERE table_name = 'customer_store_rules' "
                         "AND action = 'create'").fetchone()[0]
    audit.restore(pg, created)                       # revert the create = soft delete
    assert store_rules.for_sender(pg, SENDER) == []
    deleted = pg.execute("SELECT id FROM audit_log WHERE table_name = 'customer_store_rules' "
                         "AND action = 'restore'").fetchone()[0]
    assert deleted
    pg.execute("UPDATE customer_store_rules SET deleted_at = now()")  # already deleted: no-op
    rid = pg.execute("SELECT row_id FROM audit_log WHERE id = %s", (created,)).fetchone()[0]
    pg.execute("INSERT INTO audit_log (actor, table_name, row_id, action) "
               "VALUES ('admin', 'customer_store_rules', %s, 'delete')", (rid,))
    undel = pg.execute("SELECT max(id) FROM audit_log").fetchone()[0]
    audit.restore(pg, undel)                         # revert a delete = undelete
    assert store_rules.for_sender(pg, SENDER) == [(frozenset({"pr"}), "2999000000202")]


def test_a_different_answer_for_the_same_shop_replaces_the_rule_audited(pg):
    from app.orders import store_key
    key = store_key.for_orders([{"store": "PR", "sourceFile": "OBJ PR.xlsx"}])[0]
    a = store_rules.remember(pg, sender_email=SENDER, key=key, ean_edi="2999000000202",
                             question_id=1, by="sklad")
    assert store_rules.remember(pg, sender_email=SENDER, key=key, ean_edi="2999000000202",
                                question_id=2, by="sklad") == a, "the same answer is a no-op"
    b = store_rules.remember(pg, sender_email=SENDER, key=key, ean_edi="2999000000203",
                             question_id=3, by="sklad")
    assert b != a
    assert store_rules.for_sender(pg, SENDER) == [(frozenset({"pr"}), "2999000000203")]
    notes = [r[0] for r in pg.execute("SELECT note FROM audit_log WHERE table_name = "
                                      "'customer_store_rules' ORDER BY id").fetchall()]
    assert any("nahradené odpoveďou na otázku #3" in n for n in notes)
    assert store_rules.remember(pg, sender_email="", key=key, ean_edi="1", question_id=4,
                                by="sklad") is None


def test_the_sweep_settles_a_shop_question_once_a_card_names_the_shop(pg, monkeypatch):
    """A card corrected in CODEX (the second Dolina card moved to another town) settles the
    open DOLINA question through the SHOP rung — the order ships, and automation teaches no
    rule; „PR" (named by no card) stays a question."""
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    _env(pg)
    _run(pg, "m1")
    fixed = [dict(s, name="BISTRO Hôrka Krátka 1", city="Hôrka")
             if s["ean_edi"] == "2999000000204" else s for s in SHOPS]
    codex_customers.replace_customers(
        pg, [dict(s, emails=[SENDER], zip="", codex_id=s["ean_edi"]) for s in fixed])
    snapshot.rebuild_from_overrides(pg)
    released = hold.retry_unknown_customer_questions(pg, _cfg())
    assert [r["status"] for r in released] == ["ok"]
    assert pg.execute("SELECT status, customer_ean FROM held_orders WHERE message_id = 'm1' "
                      "AND store = 'DOLINA'").fetchone() == ("released", "2999000000203")
    assert store_rules.for_sender(pg, SENDER) == [], "automation never teaches a rule"
    assert set(_shop_questions(pg)) == {"PR"}


def test_a_shop_answer_teaches_the_rule_not_the_senders_address(pg, monkeypatch):
    """Review round 1 (#493 F7): adding the sender's address to every answered shop's card
    would make it a family — its mails with no shop clue would then be asked every time.
    The rule is the lasting lesson; the card's addresses stay as they were."""
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    snapshot.import_snapshot(pg, CATALOG_CSV, BASE_CSV)
    codex_customers.replace_customers(
        pg, [dict(s, emails=[SENDER] if s["ean_edi"] == "2999000000204" else [], zip="",
                  codex_id=s["ean_edi"]) for s in SHOPS])
    snapshot.rebuild_from_overrides(pg)
    _run(pg, "m1")
    q = _shop_questions(pg)["Predajňa: Tržnica"]
    r = _client().post(f"/api/board/questions/{q['id']}/answer",
                       json={"ean_edi": "2999000000202", "name": "Tržnica"})
    assert r.status_code == 200, r.get_json()
    cards = {c["ean_edi"]: c for c in snapshot.customers_for_management(pg)}
    assert SENDER not in cards["2999000000202"]["emails"]
    assert teach.get(pg, q["id"])["answer"]["email_added"] is False
    assert store_rules.for_sender(pg, SENDER) == [(frozenset({"tr", "trznica"}),
                                                   "2999000000202")]


# --- review round 2 (#493) --------------------------------------------------------------------

def test_files_without_any_header_or_rule_stay_the_pre_493_single_question(pg):
    """Round 4: with no header block and no learned rules, file names alone never make
    shops (a customer's files per day / per kind of goods are one order, #81.1) — even for a
    sender on several cards. Exactly as before #493: one merged order, one question,
    nothing shipped (never a guess from the first file's name)."""
    _env(pg)
    plain = [("", "BISTRO", "rožok 50g", 120, "G50"), ("", "Tržnica", "vianočka 400g", 7, "VIA"),
             ("", "DOLINA", "rožok kváskový 70g", 5, "G70")]
    result, rec = _run(pg, "m-nohead", stores=plain)
    assert rec.uploads == []
    assert len(result["order_results"]) == 1
    qs = [q for q in teach.open_questions(pg) if q["kind"] == "customer"]
    assert len(qs) == 1 and not qs[0]["context"].get("store_key")


def test_answering_a_conflict_retires_the_other_rule_and_settles_the_shop(pg, monkeypatch):
    """A Kôš restore can leave two live rules for one shop: asked, and the answer retires the
    other one — the next mail of that shop ships with no question."""
    from app.orders import store_key
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    _env(pg)
    key = store_key.for_orders([{"store": "DOLINA",
                                 "sourceFile": "OBJ Pekárová žena DO.xlsx"}])[0]
    store_rules.remember(pg, sender_email=SENDER, key=key, ean_edi="2999000000203",
                         question_id=1, by="sklad")
    pg.execute("INSERT INTO customer_store_rules (sender_email, store_key, ean_edi) "
               "VALUES (%s, %s, '2999000000204')", (SENDER, key.text))
    dolina = [("DOLINA", "DO", "rožok kváskový 70g", 5, "G70")]
    _run(pg, "m-c1", stores=dolina)
    q = _shop_questions(pg)["DOLINA"]
    _client().post(f"/api/board/questions/{q['id']}/answer",
                   json={"ean_edi": "2999000000203", "name": "Dolina Makovická"})
    assert store_rules.for_sender(pg, SENDER) == [(frozenset({"dolina"}), "2999000000203")]
    result, rec = _run(pg, "m-c2", stores=[("DOLINA", "DO", "rožok kváskový 70g", 6, "G70")])
    assert result["status"] == "ok" and len(rec.uploads) == 1


def test_a_new_customer_created_for_a_shop_never_gets_the_senders_address(pg, monkeypatch):
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    _env(pg)
    _run(pg, "m-new")
    q = _shop_questions(pg)["PR"]
    r = _client().post(f"/api/board/questions/{q['id']}/answer", json={"new_customer": {
        "ean_edi": "2999000000209", "name": "Predajňa PR Lipová", "city": "Mestečko"}})
    assert r.status_code == 200, r.get_json()
    cards = {c["ean_edi"]: c for c in snapshot.customers_for_management(pg)}
    assert SENDER not in cards["2999000000209"]["emails"]
    assert store_rules.for_sender(pg, SENDER) == [(frozenset({"pr"}), "2999000000209")]


# --- review round 3 (#493) --------------------------------------------------------------------

TOWNS_CSV = (
    "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
    f"Tesco Žilina,2999000000111,Žilina,Hlavná 1,,,\n"
    f"Kaufland Žilina,2999000000112,Žilina,Hlavná 2,,,\n"
    f"Tesco Martin,2999000000113,Martin,Hlavná 3,,,\n"
    f"Kaufland Martin,2999000000114,Martin,Hlavná 4,,,\n"
    f"Centrála reťazca,2999000000101,Mestečko,Hlavná 9,,,{SENDER}\n"
)


def _towns(pg):
    snapshot.import_snapshot(pg, CATALOG_CSV, TOWNS_CSV)


def _town_stores(town, qty=7):
    """The file names carry no town — only the headers do (the mail-relative words of both
    towns' mails are then the same: {tesco} / {kaufland})."""
    return [(f"Tesco {town}", "TE", "vianočka 400g", qty, "VIA"),
            (f"Kaufland {town}", "KA", "rožok kváskový 70g", qty - 2, "G70")]


def test_a_rule_never_captures_the_same_chain_in_another_town(pg, monkeypatch):
    """„Tesco Žilina" + „Kaufland Žilina" and „Tesco Martin" + „Kaufland Martin" reduce to
    the same mail-relative words ({tesco}/{kaufland}); a rule is keyed on the shop's WHOLE
    identity, so Martin is asked, never shipped to the Žilina cards — also while the Žilina
    questions are still open (each shop its own question)."""
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    _towns(pg)
    _run(pg, "m-za", stores=_town_stores("Žilina"))
    _run(pg, "m-mt", stores=_town_stores("Martin"))
    qs = _shop_questions(pg)
    assert set(qs) == {"Tesco Žilina", "Kaufland Žilina", "Tesco Martin", "Kaufland Martin"}
    c = _client()
    c.post(f"/api/board/questions/{qs['Tesco Žilina']['id']}/answer",
           json={"ean_edi": "2999000000111", "name": "Tesco Žilina"})
    assert pg.execute("SELECT message_id, status FROM held_orders WHERE store = "
                      "'Tesco Martin'").fetchone() == ("m-mt", "held")
    c.post(f"/api/board/questions/{qs['Kaufland Žilina']['id']}/answer",
           json={"ean_edi": "2999000000112", "name": "Kaufland Žilina"})
    result, rec = _run(pg, "m-mt2", stores=_town_stores("Martin", qty=8))
    assert rec.uploads == [], "the Martin shops are never shipped to the Žilina cards"
    again, rec2 = _run(pg, "m-za2", stores=_town_stores("Žilina", qty=9))
    assert {r["customer_ean"] for r in again["order_results"]} == {"2999000000111",
                                                                 "2999000000112"}
    assert len(rec2.uploads) == 2


def test_a_rule_sender_s_unknown_shop_is_asked_even_alone(pg, monkeypatch):
    """A sender with learned shop rules sends ONE order of a shop no rule knows — asked once
    (owner decision 2), never shipped to the sender's one card by the model pick."""
    from app.orders import store_key
    _towns(pg)
    za = store_key.for_orders([{"store": "Tesco Žilina",
                                "sourceFile": "OBJ Pekárová žena TE.xlsx"}])[0]
    store_rules.remember(pg, sender_email=SENDER, key=za, ean_edi="2999000000111",
                         question_id=1, by="sklad")
    result, rec = _run(pg, "m-lone", stores=[_town_stores("Martin")[0]])
    assert rec.uploads == []
    assert set(_shop_questions(pg)) == {"Tesco Martin"}


def test_the_sweep_never_answers_a_per_sender_question_of_a_shop_mail(pg):
    """An order naming no shop (an empty header, a file name with no word) in a mail of
    several shops is asked per sender with `no_address`; the worker sweep must not settle
    it with the sender's one card."""
    from app.orders import hold
    _towns(pg)
    stores = _town_stores("Žilina") + [("Predajňa:", "3", "rožok 50g", 120, "G50")]
    _run(pg, "m-sweep", stores=stores)
    plain = [q for q in teach.open_questions(pg) if q["kind"] == "customer"
             and not q["context"].get("store_key")]
    assert len(plain) == 1 and plain[0]["context"].get("no_address") is True
    assert hold.retry_unknown_customer_questions(pg, _cfg()) == []
    assert teach.get(pg, plain[0]["id"])["status"] == "open"


TWO_CARD_CSV = (
    "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
    f"Rodina pobočka A,2999000000301,Hôrka,Krátka 9,,,{SENDER}\n"
    f"Rodina pobočka B,2999000000302,Dolina,Dlhá 5,,,{SENDER}\n"
)


def _two_card_mail_run(pg, mid, files, notes):
    pg.execute("INSERT INTO messages (message_id, category) VALUES (%s, 'ai_orders')", (mid,))
    parts = ["Subject: objednávka\n\nAttachments:\n"]
    for name, item, qty in files:
        parts.append(f"===== {name}.xlsx =====\n[Sheet: H]\nna 04.08.2026\n{qty}x {item}\n")
    orders = [{"orderNumber": "", "deliveryDate": "04.08.2026", "recipientGroup": "",
               "store": "", "items": [{"name": item, "quantity": qty, "unit": "ks",
                                       "sourceQuote": f"{qty}x {item}"}]}
              for _n, item, qty in files]
    answers = [{"senderName": "N", "senderEmail": SENDER, "companyName": "Rodina",
                "isChangeRequest": False, "notes": notes, "orders": orders},
               {"ean_edi": "", "confidence": 0.2}]
    answers += [{"gtin": g, "confidence": 0.95, "matchedCatalogName": "", "reason": ""}
                for g in ("VIA", "G70")[:len(files)]]
    rec = Recorder()
    mail = {"message_id": mid, "subject": "objednávka", "from_addr": SENDER, "from_name": "N",
            "combined_text": "".join(parts), "today": "2026-07-30"}
    result = pipeline.run(pg, _cfg(), mail, snapshot.latest_snapshot_id(pg),
                          client=ScriptedClient(answers), upload=rec.upload, post=rec.post)
    return result, rec


def test_a_two_card_sender_keeps_the_delivery_address_and_learns_no_file_rule(pg, monkeypatch):
    """#418 unchanged: a 2-card sender's ordinary mail with a constant template file name.
    One without a delivery address is asked per SENDER (as before #493) and its answer
    teaches NO file-name rule — the next mails are decided by their delivery address."""
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    snapshot.import_snapshot(pg, CATALOG_CSV, TWO_CARD_CSV)
    _two_card_mail_run(pg, "m-418q", [("rodina tovar", "vianočka 400g", 6)], "")
    qs = [q for q in teach.open_questions(pg) if q["kind"] == "customer"]
    assert len(qs) == 1 and not qs[0]["context"].get("store_key")
    _client().post(f"/api/board/questions/{qs[0]['id']}/answer",
                   json={"ean_edi": "2999000000301", "name": "Rodina pobočka A"})
    assert store_rules.for_sender(pg, SENDER) == []
    r1, rec1 = _two_card_mail_run(pg, "m-418a", [("rodina tovar", "vianočka 400g", 7)],
                                  "Doručiť: Dlhá 5, Dolina")
    r2, rec2 = _two_card_mail_run(pg, "m-418b", [("rodina tovar", "vianočka 400g", 8)],
                                  "Doručiť: Krátka 9, Hôrka")
    assert [r["customer_ean"] for r in r1["order_results"]] == ["2999000000302"]
    assert [r["customer_ean"] for r in r2["order_results"]] == ["2999000000301"]
    assert len(rec1.uploads) == len(rec2.uploads) == 1


def test_a_two_card_sender_s_two_files_of_one_day_stay_one_order(pg):
    """#81.1 unchanged: two files of one day (no shop header, no shop named) are ONE order."""
    snapshot.import_snapshot(pg, CATALOG_CSV, TWO_CARD_CSV)
    result, rec = _two_card_mail_run(
        pg, "m-811", [("objednavka pecivo", "vianočka 400g", 7),
                      ("objednavka kolace", "rožok kváskový 70g", 5)],
        "Doručiť: Dlhá 5, Dolina")
    assert len(result["order_results"]) == 1
    assert len(rec.uploads) == 1
    assert result["order_results"][0]["customer_ean"] == "2999000000302"


# --- review round 4 (#493) --------------------------------------------------------------------

BRAND_SHOPS = [
    {"ean_edi": "2999000000401", "name": "Kvetka s.r.o.", "trade_name": "Kvetka",
     "city": "Mestečko", "street": "Centrálna 1"},
    {"ean_edi": "2999000000402", "name": "BISTRO Mestečko", "trade_name": "",
     "city": "Mestečko", "street": "Krátka 1"},
    {"ean_edi": "2999000000403", "name": "Tržnica Mestečko", "trade_name": "",
     "city": "Mestečko", "street": "Riadok 1"},
    {"ean_edi": "2999000000404", "name": "Predajňa Lipová", "trade_name": "",
     "city": "Mestečko", "street": "Lipová 6"},
]


def _brand_env(pg):
    snapshot.import_snapshot(pg, CATALOG_CSV, BASE_CSV)
    codex_customers.replace_customers(
        pg, [dict(s, emails=[SENDER], zip="", codex_id=s["ean_edi"]) for s in BRAND_SHOPS])
    snapshot.rebuild_from_overrides(pg)


def _brand_run(pg, mid, stores, company="Kvetka s.r.o."):
    pg.execute("INSERT INTO messages (message_id, category) VALUES (%s, 'ai_orders')", (mid,))
    parts = ["Subject: objednávka\n\nAttachments:\n"]
    for header, fname, item, qty, _g in stores:
        parts.append(f"===== {fname}.xlsx =====\n[Sheet: H]\n{header}\nna 04.08.2026\n"
                     f"{qty}x {item}\n")
    orders = [{"orderNumber": "", "deliveryDate": "04.08.2026", "recipientGroup": "",
               "store": header, "items": [{"name": item, "quantity": qty, "unit": "ks",
                                           "sourceQuote": f"{qty}x {item}"}]}
              for header, _f, item, qty, _g in stores]
    answers = [{"senderName": "Nákup", "senderEmail": SENDER, "companyName": company,
                "isChangeRequest": False, "notes": "", "orders": orders},
               {"ean_edi": "2999000000401", "confidence": 0.97}]
    answers += [{"gtin": g, "confidence": 0.95, "matchedCatalogName": "", "reason": ""}
                for *_r, g in stores]
    rec = Recorder()
    mail = {"message_id": mid, "subject": "objednávka", "from_addr": SENDER,
            "from_name": "Nákup", "combined_text": "".join(parts), "today": "2026-07-30"}
    result = pipeline.run(pg, _cfg(), mail, snapshot.latest_snapshot_id(pg),
                          client=ScriptedClient(answers), upload=rec.upload, post=rec.post)
    return result, rec


BRAND_STORES = [("BISTRO", "Kvetka BISTRO", "rožok 50g", 120, "G50"),
                ("Predajňa: Tržnica", "Kvetka TR", "vianočka 400g", 7, "VIA"),
                ("Predajňa:", "Kvetka PR", "rožok kváskový 70g", 5, "G70")]


def test_the_sweep_never_settles_a_shop_by_the_senders_own_brand_word(pg, monkeypatch):
    """The file names carry the customer's own brand („Kvetka …") that only its head-office
    card bears: the pipeline asks „PR"; the worker sweep must use the SAME shop words and
    never answer it with the head-office card."""
    from app.orders import hold
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    _brand_env(pg)
    result, rec = _brand_run(pg, "m-brand", BRAND_STORES)
    shipped = {r["store"]: r["customer_ean"] for r in result["order_results"]
               if r["status"] == "ok"}
    assert shipped == {"BISTRO": "2999000000402", "Predajňa: Tržnica": "2999000000403"}
    assert hold.retry_unknown_customer_questions(pg, _cfg()) == []
    assert pg.execute("SELECT status FROM held_orders WHERE message_id = 'm-brand'"
                      ).fetchall() == [("held",)]


def test_a_rule_senders_one_shop_mail_is_never_decided_by_its_brand_word(pg):
    """A sender with learned rules sends ONE order „Kvetka PR" no rule knows: asked (the
    poistka), never shipped to the head-office card by the brand word."""
    from app.orders import store_key
    _brand_env(pg)
    known = store_key.for_orders([{"store": "BISTRO", "sourceFile": "Kvetka BISTRO.xlsx"}])[0]
    store_rules.remember(pg, sender_email=SENDER, key=known, ean_edi="2999000000402",
                         question_id=1, by="sklad")
    result, rec = _brand_run(pg, "m-one", [BRAND_STORES[2]])
    assert rec.uploads == []
    assert set(_shop_questions(pg)) == {"Predajňa:"}


def test_two_files_of_one_taught_shop_still_follow_its_rule(pg):
    """„… BISTRO.xlsx" + „… BISTRO (1).xlsx" (one shop, one day) are merged and keep the
    shop's words — the rule decides, never the model pick of the head-office card."""
    from app.orders import store_key
    _brand_env(pg)
    known = store_key.for_orders([{"store": "", "sourceFile": "Kvetka BISTRO.xlsx"}])[0]
    store_rules.remember(pg, sender_email=SENDER, key=known, ean_edi="2999000000402",
                         question_id=1, by="sklad")
    result, rec = _brand_run(pg, "m-dup", [("", "Kvetka BISTRO", "rožok 50g", 120, "G50"),
                                           ("", "Kvetka BISTRO (1)", "vianočka 400g", 7, "VIA")])
    assert [r["customer_ean"] for r in result["order_results"]] == ["2999000000402"]
    assert len(rec.uploads) == 1


def test_a_shop_mails_per_sender_question_never_merges_into_a_plain_one(pg):
    """An older plain question of the sender is open; a mail of several shops brings an
    order that names no shop — its own `no_address` question, never folded into the plain
    one (whose answer teaches the address and which the sweep may settle by the address)."""
    _towns(pg)
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('m-plain', 'ai_orders')")
    teach.ask_customer(pg, message_id="m-plain", sender_email=SENDER,
                       candidates=[{"ean_edi": "2999000000101", "name": "Centrála"}],
                       delivery_date="04.08.2026", context={"sender_email": SENDER})
    _run(pg, "m-mix", stores=_town_stores("Žilina") + [("Predajňa:", "3", "rožok 50g", 120,
                                                         "G50")])
    plain = [q for q in teach.open_questions(pg) if q["kind"] == "customer"
             and not q["context"].get("store_key")]
    assert len(plain) == 2
    assert sorted(bool(q["context"].get("no_address")) for q in plain) == [False, True]


def test_a_two_card_senders_similar_file_names_stay_one_order(pg):
    """#81.1 as on main: „Objednávka Prešov" + „Objednávka Prešov pečivo" (no header, no
    rules) are ONE order — never split into a shop question, even when one of the sender's
    cards IS in Prešov (round 4: the shared word looked like a shop name)."""
    snapshot.import_snapshot(pg, CATALOG_CSV, TWO_CARD_CSV.replace(
        "Rodina pobočka B,2999000000302,Dolina", "Rodina pobočka B,2999000000302,Prešov"))
    result, rec = _two_card_mail_run(
        pg, "m-pre", [("Objednávka Prešov", "vianočka 400g", 7),
                      ("Objednávka Prešov pečivo", "rožok kváskový 70g", 5)],
        "Doručiť: Dlhá 5, Prešov")
    assert len(result["order_results"]) == 1 and len(rec.uploads) == 1
    assert _shop_questions(pg) == {}
