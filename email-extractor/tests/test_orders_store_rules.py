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
