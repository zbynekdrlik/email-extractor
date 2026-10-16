"""#493 in the real browser: the warehouse SEES which shop a customer question is about, answers
it on the nástenka (which ships the held order and teaches the shop rule), and finds the rule in
the Kôš under its own label, where „Vrátiť" takes it back. Clean console, version label.

Synthetic data only (invented shops, addresses, EANs).
"""
import re

from app.orders import codex_customers, snapshot, store_rules, teach

SENDER = "nakup@retazec-e2e.sk"


def _collect_console(page):
    msgs = []
    page.on("console",
            lambda m: msgs.append(f"{m.type}: {m.text}") if m.type in ("error", "warning")
            else None)
    page.on("pageerror", lambda e: msgs.append(f"pageerror: {e}"))
    return msgs


def _seed_shop_question(pg):
    """Two Dolina shops of one sender (from CODEX) and a held order for „DOLINA", asked per
    shop — exactly what the pipeline leaves behind (`order_customers.ask`)."""
    from psycopg.types.json import Json

    from app.orders import store_key
    snapshot.import_snapshot(pg, "GTIN,Názov,doplnok\nE2EG70,Rožok kváskový 70g,\n",
                             "Názov organizácie,EAN kód EDI,E-mail\nIný E2E,2999000000990,"
                             "iny@e2e.sk\n")
    codex_customers.replace_customers(pg, [
        {"ean_edi": "2999000000501", "name": "Dolina Makovická E2E", "emails": [SENDER],
         "city": "Dolina", "street": "Makovická", "codex_id": "1"},
        {"ean_edi": "2999000000502", "name": "BISTRO Dolina E2E", "emails": [SENDER],
         "city": "Dolina", "street": "Krátka 1", "codex_id": "2"}])
    snapshot.rebuild_from_overrides(pg)
    pg.execute("INSERT INTO messages (message_id, from_addr, from_name, subject) "
               "VALUES ('e2e-shop', %s, 'Nákup', 'objednávka')", (SENDER,))
    key = store_key.for_orders([{"store": "DOLINA",
                                 "sourceFile": "OBJ Pekárová žena ZA.xlsx"}])[0]
    qid = teach.ask_customer(
        pg, message_id="e2e-shop", sender_email=SENDER,
        candidates=[{"ean_edi": "2999000000501", "name": "Dolina Makovická E2E"},
                    {"ean_edi": "2999000000502", "name": "BISTRO Dolina E2E"}],
        delivery_date="04.08.2026", context={"sender_email": SENDER}, key=key)
    pg.execute(
        """INSERT INTO held_orders (message_id, customer_ean, customer_name, delivery_date,
                                    order_number, store, question_ids, order_json,
                                    extracted_json, decisions_json)
           VALUES ('e2e-shop', '', '', '04.08.2026', '', 'DOLINA', %s, %s, %s, %s)""",
        ([qid], Json({"deliveryDate": "04.08.2026", "orderNumber": "", "store": "DOLINA"}),
         Json({"isChangeRequest": False, "unverified": [], "notes": ""}),
         Json([{"item_name": "rožok kváskový 70g", "gtin": "E2EG70",
                "card": "Rožok kváskový 70g", "confidence": 0.95, "rule": "catalog_direct",
                "note": "", "review": False, "trace": {}, "quantity": 5, "unit": "ks"}])))
    return qid


def test_a_shop_question_names_the_shop_and_its_answer_teaches_a_restorable_rule(
        live_server, pg, page, monkeypatch):
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    qid = _seed_shop_question(pg)
    console = _collect_console(page)
    page.on("dialog", lambda d: d.accept())

    page.goto(f"{live_server}/login")
    page.fill("input[name=password]", "secret")
    page.click("button[type=submit]")
    page.goto(f"{live_server}/nastenka/otazky-objednavky")
    backend_ver = page.request.get(f"{live_server}/version").text().strip()
    assert backend_ver in page.locator('[data-testid="version"]').inner_text()

    # the warehouse SEES which shop the question is about, and both Dolina cards
    card = page.locator(f"#q-card-{qid}")
    card.wait_for()
    assert "predajňa „DOLINA“" in card.inner_text()
    assert "OBJ Pekárová žena ZA.xlsx" in card.inner_text()
    card.locator('button:has-text("Dolina Makovická E2E")').click()
    page.wait_for_selector("text=Uložené")

    assert pg.execute("SELECT status, customer_ean FROM held_orders "
                      "WHERE message_id = 'e2e-shop'").fetchone() == ("released",
                                                                     "2999000000501")
    assert store_rules.for_sender(pg, SENDER) == [(frozenset({"dolina", "za"}),
                                                    "2999000000501")]

    # the learned rule sits in the Kôš under its own label; „Vrátiť" takes it back
    page.goto(f"{live_server}/nastenka/kos")
    page.fill("#trash-search", "DOLINA")
    row = page.locator('#trash-rows tr:has-text("Predajňa zákazníka")').first
    row.wait_for()
    row.locator('button:has-text("Vrátiť")').click()
    page.wait_for_selector("text=Vrátené")
    assert store_rules.for_sender(pg, SENDER) == []

    assert console == [], f"browser console not clean: {console}"
    assert re.search(r"\d+\.\d+\.\d+", backend_ver)
