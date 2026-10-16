"""#493 review round 5 — the edges where a shop could still reach the wrong card.

* the name rung must agree on the card with the shop's OWN words, not only with the words left
  after the mail's shared ones are dropped (the town of „Tesco Martin" + „Kaufland Martin");
* orders that name no shop in a mail of several shops are never merged and each is asked on
  its own — an answer about one order never ships another order (of this or another mail);
* a merge of one day's orders keeps the one file that was located;
* the worker sweep uses only the rungs the pipeline used for that question;
* a header's dates / week numbers / order numbers are not part of a shop's identity;
* the CODEX ambiguity alert does not depend on the snapshot rebuild.

Synthetic data only (the repo is public).
"""
from test_orders_store_rules import (
    BRAND_SHOPS,
    CATALOG_CSV,
    SENDER,
    TWO_CARD_CSV,
    _brand_env,
    _cfg,
    _client,
    _env,
    _run,
    _shop_questions,
    _two_card_mail_run,
)

from app.orders import (
    codex_customers,
    hold,
    order_customers,
    snapshot,
    store_key,
    store_rules,
    teach,
)

FAMILY_TOWNS_CSV = (
    "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
    f"Tesco Žilina,2999000000111,Žilina,Hlavná 1,,,{SENDER}\n"
    f"Kaufland Žilina,2999000000112,Žilina,Hlavná 2,,,{SENDER}\n"
    f"Tesco Martin,2999000000113,Martin,Hlavná 3,,,{SENDER}\n"
    "Kaufland Martin,2999000000114,Martin,Hlavná 4,,,\n"
    f"Centrála reťazca,2999000000101,Mestečko,Hlavná 9,,,{SENDER}\n"
)
MARTIN = [("Tesco Martin", "TE", "vianočka 400g", 7, "VIA"),
          ("Kaufland Martin", "KA", "rožok kváskový 70g", 5, "G70")]


def _unnamed(code, qty=10):
    """An order whose header names no shop and whose file name carries no word."""
    return ("Predajňa:", code, "rožok kváskový 70g", qty, "G70")


def _plain_questions(pg):
    return [q for q in teach.open_questions(pg)
            if q["kind"] == "customer" and not q["context"].get("store_key")]


def test_the_name_rung_never_ships_a_shop_by_a_word_its_town_shares(pg):
    """The sender is on Tesco Žilina, Kaufland Žilina, Tesco Martin and the head office; its
    Kaufland Martin card does not carry the address. „Kaufland Martin" minus the town every
    shop of the mail shares is {kaufland} — it named Kaufland Žilina. With its own words
    {kaufland, martin} two cards are hit, so it is asked, never shipped to another town."""
    snapshot.import_snapshot(pg, CATALOG_CSV, FAMILY_TOWNS_CSV)
    result, rec = _run(pg, "m-km", stores=MARTIN)
    shipped = {r["store"]: r["customer_ean"] for r in result["order_results"]
               if r["status"] == "ok"}
    assert "Kaufland Martin" not in shipped
    assert "2999000000112" not in shipped.values()
    assert "Kaufland Martin" in _shop_questions(pg)


def test_two_orders_naming_no_shop_are_never_merged_into_one(pg):
    """Two shops order the same goods and neither the header nor the file says which: two
    held orders, two questions — never one order of 20 under one answer."""
    _env(pg)
    stores = [("BISTRO", "BISTRO", "rožok 50g", 120, "G50"),
              ("Predajňa: Tržnica", "TR", "vianočka 400g", 7, "VIA"),
              _unnamed("A"), _unnamed("B")]
    _run(pg, "m-two", stores=stores)
    rows = pg.execute("SELECT question_ids, order_json FROM held_orders WHERE message_id = "
                      "'m-two' AND store = 'Predajňa:'").fetchall()
    assert len(rows) == 2, "each unnamed order is its own held order"
    assert [sum(i["quantity"] for i in r[1]["items"]) for r in rows] == [10, 10]
    customer_qids = [{q for q in r[0] if teach.get(pg, q)["kind"] == "customer"} for r in rows]
    assert all(customer_qids) and not customer_qids[0] & customer_qids[1]
    plain = _plain_questions(pg)
    assert len(plain) == 2
    assert all("bez označenia predajne" in q["wording"] for q in plain)


def test_an_unnamed_order_s_question_belongs_to_its_own_mail(pg, monkeypatch):
    """Two mails of several shops each bring an order naming no shop: two questions, and the
    answer for the first mail ships only the first mail's order."""
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    _env(pg)
    stores = [("BISTRO", "BISTRO", "rožok 50g", 120, "G50"),
              ("Predajňa: Tržnica", "TR", "vianočka 400g", 7, "VIA"), _unnamed("A")]
    _run(pg, "m-a", stores=stores)
    _run(pg, "m-b", stores=stores)
    plain = {q["message_id"]: q for q in _plain_questions(pg)}
    assert set(plain) == {"m-a", "m-b"}
    _client().post(f"/api/board/questions/{plain['m-a']['id']}/answer",
                   json={"ean_edi": "2999000000203", "name": "Dolina Makovická"})
    assert pg.execute("SELECT status FROM held_orders WHERE message_id = 'm-b' AND store = "
                      "'Predajňa:'").fetchone() == ("held",)


def test_a_merge_keeps_the_one_file_that_was_located():
    """One order's file could not be located (its lines are in two files): merged with the
    located order of the same day, the merged order keeps the located file."""
    day = {"store": "", "deliveryDate": "04.08.2026", "orderNumber": ""}
    unlocated = dict(day, sourceFile="", items=[{"name": "rožok", "quantity": 3}])
    located = dict(day, sourceFile="Kvetka BISTRO.xlsx", items=[{"name": "chlieb",
                                                                  "quantity": 2}])
    merged = order_customers.merge_by_day([unlocated, located])
    assert len(merged) == 1 and merged[0]["sourceFile"] == "Kvetka BISTRO.xlsx"
    assert [i["name"] for i in merged[0]["items"]] == ["rožok", "chlieb"]
    other = dict(day, sourceFile="Kvetka TR.xlsx", items=[{"name": "vianočka", "quantity": 1}])
    assert order_customers.merge_by_day([located, other])[0]["sourceFile"] == ""


def test_a_rule_senders_merged_order_follows_the_rule_of_its_located_file(pg):
    """The merged order keeps its shop identity, so the learned rule decides it — never the
    sender's address or the model pick."""
    _brand_env(pg)
    known = store_key.for_orders([{"store": "", "sourceFile": "Kvetka BISTRO.xlsx"}])[0]
    store_rules.remember(pg, sender_email=SENDER, key=known, ean_edi="2999000000402",
                         question_id=1, by="sklad")
    day = {"store": "", "deliveryDate": "04.08.2026", "orderNumber": ""}
    orders = [dict(day, sourceFile="", items=[{"name": "rožok 50g", "quantity": 3}]),
              dict(day, sourceFile="Kvetka BISTRO.xlsx",
                   items=[{"name": "vianočka 400g", "quantity": 2}])]
    customers = snapshot.load_customers(pg, snapshot.latest_snapshot_id(pg))
    merged, oc = order_customers.resolve_orders(
        pg, customers, SENDER, {"senderName": "Nákup", "companyName": "Kvetka s.r.o."},
        {"ean_edi": "2999000000401", "confidence": 0.97}, orders)
    assert len(merged) == 1
    assert (oc.matched[0].ean_edi, oc.matched[0].rule) == ("2999000000402", "store_rule")


def test_the_sweep_uses_only_the_rungs_the_pipeline_used(pg):
    """A rule sender's one-shop mail: the file says „Dolina", the delivery address says Hôrka.
    The pipeline asks (no name rung outside a mail of several shops); the worker sweep must
    not then settle it by the file word."""
    snapshot.import_snapshot(pg, CATALOG_CSV, TWO_CARD_CSV)
    other = store_key.for_orders([{"store": "Iná predajňa", "sourceFile": ""}])[0]
    store_rules.remember(pg, sender_email=SENDER, key=other, ean_edi="2999000000301",
                         question_id=1, by="sklad")
    _two_card_mail_run(pg, "m-sw", [("objednavka Dolina", "vianočka 400g", 6)],
                       "Doručiť: Krátka 9, Hôrka")
    qs = [q for q in teach.open_questions(pg) if q["kind"] == "customer"]
    assert len(qs) == 1 and qs[0]["context"].get("store_key") == "dolina"
    assert hold.retry_unknown_customer_questions(pg, _cfg()) == []
    assert teach.get(pg, qs[0]["id"])["status"] == "open"


def test_a_header_s_dates_and_order_numbers_are_not_the_shop():
    assert store_key.tokens("BISTRO 16.10.", keep_numbers=True) == ["bistro"]
    assert store_key.tokens("BISTRO 16. 10. 2026", keep_numbers=True) == ["bistro"]
    assert store_key.tokens("BISTRO na 16.10", keep_numbers=True) == ["bistro"]
    assert store_key.tokens("Tržnica 42. týždeň", keep_numbers=True) == ["trznica"]
    assert store_key.tokens("Obj. č. 2026104512 BISTRO", keep_numbers=True) == ["bistro"]
    # short standalone numbers stay: „Predajňa 1" / „Predajňa 2", a house number
    assert store_key.tokens("Predajňa 2", keep_numbers=True) == ["2"]
    assert store_key.tokens("ŠKOLA, Lipová 7, MS", keep_numbers=True) == [
        "skola", "lipova", "7", "ms"]


def test_the_ambiguity_alert_never_depends_on_the_snapshot_rebuild(pg, monkeypatch):
    """A failing rebuild must not swallow the ops alert: the next push sees an unchanged set
    and would never alert again."""
    from test_codex_customers import SHOP_A, _base, _push
    from test_codex_customers import _client as codex_client

    _base(pg)

    def boom(conn):
        raise RuntimeError("rebuild failed")
    monkeypatch.setattr("app.orders.snapshot.rebuild_from_overrides", boom)
    amb = [{"ean_edi": "2999000000399", "firms": [{"name": "Predajňa X", "city": "Mestečko"},
                                                  {"name": "Iná firma", "city": "Dolina"}]}]
    assert _push(codex_client(), [SHOP_A], ambiguous=amb).status_code == 200
    assert pg.execute("SELECT count(*) FROM pending_alerts WHERE kind = %s",
                      (codex_customers.AMBIGUOUS_KIND,)).fetchone() == (1,)


def test_brand_family_fixture_is_unchanged():
    """Guard: the round-4 fixture this file reuses still has its head-office card first."""
    assert BRAND_SHOPS[0]["ean_edi"] == "2999000000401"


def test_a_header_s_calendar_week_kt_is_not_the_shop():
    """The incident's own headers carry „43 KT" / „43týždeň" next to the shop."""
    assert store_key.tokens("BISTRO 43 KT", keep_numbers=True) == ["bistro"]
    assert store_key.tokens("Predajňa: Tržnica 43týždeň", keep_numbers=True) == ["trznica"]
    assert store_key.tokens("ŽILINA KT 43", keep_numbers=True) == ["zilina"]


TIED = ("Body: ahoj\n\nAttachments:\n"
        "===== OBJ A.xlsx =====\n[Sheet: H]\nPredajňa: Tržnica\nRožok biely 0.6 20\n"
        "===== OBJ B.xlsx =====\n[Sheet: H]\nBISTRO\nRožok biely 0.6 20\n"
        "===== OBJ C.xlsx =====\n[Sheet: H]\nBISTRO\nVianočka 400 g 3\n")


def test_an_orders_file_is_located_by_its_header_when_its_lines_are_everywhere():
    """The incident's files share most rows („Rožok biely 0.6 …"), so an order's quotes often
    tie and its file stayed unknown — its identity then differed between days of one shop
    ({trznica} vs {tr, trznica}) and every variant was its own question / rule. The block
    header, when it is in exactly ONE attachment, names the file."""
    assert store_key.locate_file(["Rožok biely 0.6 20"], TIED) == "", "a tie on the lines"
    assert store_key.locate_file(["Rožok biely 0.6 20"], TIED,
                                 header="Predajňa: Tržnica") == "OBJ A.xlsx"
    assert store_key.locate_file(["Rožok biely 0.6 20"], TIED, header="PR") == "", \
        "a header in no attachment decides nothing"
    assert store_key.locate_file(["Vianočka 400 g 3"], TIED, header="BISTRO") == "OBJ C.xlsx", \
        "a header in two attachments leaves it to the lines"
    assert store_key.locate_file(["Vianočka 400 g 3"], TIED,
                                 header="Predajňa: Tržnica") == "", \
        "the header and the lines name different files — never guessed"


def test_the_extraction_records_the_file_its_header_names():
    from app.orders import extract
    out = extract.verify({"orders": [
        {"deliveryDate": "04.08.2026", "store": "Predajňa: Tržnica",
         "items": [{"name": "Rožok biely", "quantity": 20, "unit": "ks",
                    "sourceQuote": "Rožok biely 0.6 20"}]}]}, TIED)
    assert [o["sourceFile"] for o in out["orders"]] == ["OBJ A.xlsx"]
