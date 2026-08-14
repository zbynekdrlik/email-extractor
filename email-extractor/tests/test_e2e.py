"""End-to-end browser test (Playwright) — the real user workflow against the app.

login -> list -> search -> open detail -> reclassify -> fix modal, asserting a
clean browser console (zero errors/warnings) and a version label that matches
the backend /version (the mandatory web rules).
"""
from app import db


def _collect_console(page):
    msgs = []
    page.on("console",
            lambda m: msgs.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: msgs.append(f"pageerror: {e}"))
    return msgs


def test_dashboard_user_workflow(live_server, pg, page):
    pg.execute("INSERT INTO messages (message_id, from_addr, subject, category, processed, "
               "proc_status, proc_outcome) VALUES "
               "('e1','kupujuci@x.sk','Objednavka chleba','ai_orders', true, 'ok','EDI nahrate')")
    db.log_event(pg, "e1", "ai_orders", "uploaded_orion", "ok",
                 outcome="EDI nahrate", detail={"edi_file": "ORDER_1.txt"})

    console = _collect_console(page)

    # login
    page.goto(f"{live_server}/login")
    page.fill("input[name=password]", "secret")
    page.click("button[type=submit]")
    page.wait_for_url(f"{live_server}/")

    # version label present and matches the backend
    backend_ver = page.request.get(f"{live_server}/version").text().strip()
    assert backend_ver in page.locator('[data-testid="version"]').inner_text()

    # list shows the seeded mail; search narrows then restores
    page.wait_for_selector("text=Objednavka chleba")
    page.fill("#q", "chleba")
    page.wait_for_timeout(600)               # debounced search (350 ms)
    assert page.locator("text=Objednavka chleba").count() == 1
    page.fill("#q", "neexistujuce_slovo_xyz")
    page.wait_for_timeout(600)
    assert page.locator("text=Objednavka chleba").count() == 0
    page.fill("#q", "")
    page.wait_for_timeout(600)

    # open detail -> the pipeline event shows in the timeline
    page.click("text=Objednavka chleba")
    page.wait_for_selector("text=uploaded_orion")

    # reclassify -> persisted in the DB
    page.select_option("select.act", "invoices")
    page.wait_for_timeout(500)
    assert pg.execute("SELECT category FROM messages WHERE message_id='e1'").fetchone()[0] == "invoices"

    # fix flow -> a fix_requests row is created
    page.click("button:has-text('dať na opravu')")
    page.wait_for_selector("#modal")
    page.fill("#fxdesc", "zle mnozstvo")
    page.click("button:has-text('Odoslať na opravu')")
    page.wait_for_timeout(500)
    assert pg.execute("SELECT count(*) FROM fix_requests WHERE message_id='e1'").fetchone()[0] == 1

    assert console == [], f"browser console not clean: {console}"


def test_unreceived_mails_tab_shows_failed_ingests(live_server, pg, page):
    """#20: an email that never got in has no messages row — this tab is the only
    place a human can see it, so it must actually render in the browser."""
    pg.execute("TRUNCATE imap_failures")
    db.record_uid_failure(pg, "INBOX", 1, 4711, "RuntimeError('OCR out of memory')")
    for _ in range(db.MAX_UID_ATTEMPTS):
        db.record_uid_failure(pg, "INBOX", 1, 4712, "ValueError('broken MIME part')")
    db.mark_uid_skipped(pg, "INBOX", 1, 4712)

    console = _collect_console(page)
    page.goto(f"{live_server}/login")
    page.fill("input[name=password]", "secret")
    page.click("button[type=submit]")
    page.wait_for_url(f"{live_server}/")

    # the red badge on the tab counts them without the user opening anything
    page.wait_for_selector("#imapBadge:text('2')")

    page.click("#tabImap")
    page.wait_for_selector("text=UID 4711")
    body = page.locator("#detail").inner_text()
    assert "skúša sa" in body and "1/5 pokusov" in body
    assert "vzdané" in body and "UID 4712" in body
    assert "OCR out of memory" in body
    assert "broken MIME part" in body

    # and back to the mail list without errors
    page.click("#tabMails")
    page.wait_for_timeout(300)
    assert console == [], f"browser console not clean: {console}"


def test_teaching_a_wording_and_taking_it_back_in_the_browser(live_server, pg, page):
    """The teach-once loop through the real UI (#88).

    Live verification of 0.9.6 caught what a unit test could not: the taught list was rendered
    only when open questions existed, so in the NORMAL state (nothing waiting) the "vrátiť"
    button was unreachable and a mis-click stayed permanent. Hence a browser test.
    """
    from app.orders import memory, teach

    ean = "2000000000001"
    qid = teach.ask(pg, message_id="e-teach", customer_ean=ean, customer_name="Zákazník A",
                    wording="testovacia pletenka", quantity=8, unit="ks",
                    candidates=[{"gtin": "AAA", "name": "Karta A"},
                                {"gtin": "BBB", "name": 'Karta B "špeciál"'}],
                    delivery_date="06.08.2026", reason="neznáme znenie")
    assert qid

    console = _collect_console(page)
    page.goto(f"{live_server}/login")
    page.fill("input[name=password]", "secret")
    page.click("button[type=submit]")
    page.wait_for_url(f"{live_server}/")

    page.click("#tabAsk")
    page.wait_for_selector("text=testovacia pletenka")
    # a card name containing a quote must render as a usable button
    page.click('button:has-text("Karta B")')

    # the question leaves the open list, and what was taught is listed WITH its undo
    page.wait_for_selector("text=Naposledy naučené")
    assert memory.resolve(pg, ean, "testovacia pletenka").gtin == "BBB"

    page.click('button:has-text("vrátiť")')
    # wait for something that exists ONLY while the question is open — the wording itself is
    # also in the taught list, so waiting on it would pass before the undo even lands
    page.wait_for_selector('button:has-text("Karta A")')
    pg.rollback()          # this connection's snapshot predates the app's delete
    assert memory.resolve(pg, ean, "testovacia pletenka") is None, "the mapping is gone"
    assert teach.open_questions(pg)[0]["id"] == qid, "and it is asked again"

    assert console == [], f"console must be clean: {console}"


def test_the_questions_view_survives_the_live_refresh_without_duplicating(live_server, pg,
                                                                          page):
    """Seen on the live box: the taught section appeared TWICE.

    The view auto-refreshes every 5 s. A refresh clears the list and re-renders, while the
    PREVIOUS render's taught-list fetch is still in flight — and that late answer then appends
    to the already re-rendered list. So the section (and its undo buttons) doubled on screen.
    """
    from app.orders import teach

    qid = teach.ask(pg, message_id="e-dup", customer_ean="2000000000001",
                    customer_name="Zákazník A", wording="dvojite znenie", quantity=3,
                    unit="ks", candidates=[{"gtin": "AAA", "name": "Karta A"}],
                    delivery_date="06.08.2026", reason="test")
    teach.answer(pg, qid, gtin="AAA", card="Karta A", by="sklad")

    console = _collect_console(page)
    page.goto(f"{live_server}/login")
    page.fill("input[name=password]", "secret")
    page.click("button[type=submit]")
    page.wait_for_url(f"{live_server}/")
    page.click("#tabAsk")
    page.wait_for_selector("text=Naposledy naučené")

    # Force the overlap the live box hit: a click (undo/teach) re-renders while the previous
    # render's taught-list fetch is still in flight. Two renders back to back reproduces it
    # deterministically, where a plain 6 s wait does not.
    page.evaluate("loadAsk(); loadAsk();")
    page.wait_for_timeout(1500)
    assert page.get_by_text("Naposledy naučené").count() == 1, "the section rendered twice"
    assert page.get_by_role("button", name="vrátiť").count() == 1
    assert console == [], f"console must be clean: {console}"


def test_the_warehouse_answers_who_the_customer_is_from_the_link(live_server, pg, page):
    """#159: an unrecognized sender is now a WAREHOUSE QUESTION too, rendered on the same
    /otazky page the product-wording questions already use — through the real browser,
    from the signed link, no login."""
    from app.httpapi import sklad_key
    from app.orders import snapshot, teach

    snapshot.import_snapshot(
        pg, "GTIN,Názov,doplnok\nG1,Rožok štandart 50g,\n",
        "Názov organizácie,EAN kód EDI,Obec,Ulica,E-mail\n"
        "Potraviny nie otraviny Žilina,2000000000861,Žilina,na bráne 4,eva@x.sk\n")
    qid = teach.ask_customer(
        pg, message_id="e-cust", sender_email="cudzi@nikde.sk",
        candidates=[{"ean_edi": "2000000000861", "name": "Potraviny nie otraviny Žilina",
                    "city": "Žilina", "street": "na bráne 4", "address_match": True}],
        delivery_date="08.08.2026",
        context={"sender_email": "cudzi@nikde.sk", "sender_name": "Sklad",
                "company_name": "Neznáma firma s.r.o.",
                "delivery_address_guess": "na bráne 4, Žilina"})
    assert qid

    console = _collect_console(page)
    page.goto(f"{live_server}/sklad/{sklad_key('e2e-secret')}")
    page.wait_for_url(f"{live_server}/otazky")

    page.wait_for_selector("text=cudzi@nikde.sk")
    page.wait_for_selector("text=Potraviny nie otraviny Žilina")
    page.click('button:has-text("Potraviny nie otraviny Žilina")')

    page.wait_for_selector("text=Naposledy naučené")
    q = teach.get(pg, qid)
    assert q["status"] == "answered" and q["answer_gtin"] == "2000000000861"
    row = pg.execute(
        "SELECT emails FROM customer_overrides WHERE ean_edi='2000000000861'").fetchone()
    assert row and "cudzi@nikde.sk" in row[0]

    assert console == [], f"browser console not clean: {console}"


def test_the_warehouse_can_say_it_does_not_know_the_customer(live_server, pg, page):
    """'neviem, kto to je' (#159) must be reachable from the exact same card — it is the
    only honest answer when none of the candidates fit."""
    from app.httpapi import sklad_key
    from app.orders import teach

    qid = teach.ask_customer(
        pg, message_id="e-cust2", sender_email="dalsi@nikde.sk",
        candidates=[{"ean_edi": "2000000000001", "name": "Iný zákazník", "city": "",
                    "street": "", "address_match": False}],
        delivery_date="08.08.2026",
        context={"sender_email": "dalsi@nikde.sk", "sender_name": "", "company_name": "",
                "delivery_address_guess": ""})
    assert qid

    console = _collect_console(page)
    page.goto(f"{live_server}/sklad/{sklad_key('e2e-secret')}")
    page.wait_for_url(f"{live_server}/otazky")

    page.wait_for_selector("text=dalsi@nikde.sk")
    page.click('button:has-text("Neviem, kto to je")')
    page.wait_for_selector("text=Naposledy naučené")
    q = teach.get(pg, qid)
    assert q["status"] == "answered" and q["answer_gtin"] == ""
    assert pg.execute("SELECT count(*) FROM customer_overrides").fetchone()[0] == 0

    assert console == [], f"browser console not clean: {console}"


def test_the_warehouse_adds_a_brand_new_customer_from_the_card(live_server, pg, page):
    """#234: the dead end this ticket exists to close — a sender absent from
    `customer_snapshot` entirely can now be added right on the question card, prefilled
    from the mail, through the real browser."""
    from app.httpapi import sklad_key
    from app.orders import teach

    qid = teach.ask_customer(
        pg, message_id="e-cust3", sender_email="uplnenovy@nikde.sk",
        candidates=[{"ean_edi": "2000000000001", "name": "Iný zákazník", "city": "",
                    "street": "", "address_match": False}],
        delivery_date="08.08.2026",
        context={"sender_email": "uplnenovy@nikde.sk", "sender_name": "Sklad",
                "company_name": "Celkom Nová Pekáreň s.r.o.",
                "delivery_address_guess": ""})
    assert qid

    console = _collect_console(page)
    page.goto(f"{live_server}/sklad/{sklad_key('e2e-secret')}")
    page.wait_for_url(f"{live_server}/otazky")

    page.wait_for_selector("text=uplnenovy@nikde.sk")
    page.click('button:has-text("Nový zákazník")')
    page.wait_for_selector('input[placeholder="EAN kód EDI *"]')
    # prefilled straight from the mail's own company name
    assert page.locator('input[placeholder="názov firmy *"]').input_value() \
        == "Celkom Nová Pekáreň s.r.o."
    page.fill('input[placeholder="EAN kód EDI *"]', "7000000000321")
    page.click('button:has-text("Uložiť nového zákazníka")')

    page.wait_for_selector("text=Naposledy naučené")
    q = teach.get(pg, qid)
    assert q["status"] == "answered" and q["answer_gtin"] == "7000000000321"
    row = pg.execute(
        "SELECT name FROM customer_overrides WHERE ean_edi='7000000000321'").fetchone()
    assert row == ("Celkom Nová Pekáreň s.r.o.",)

    assert console == [], f"browser console not clean: {console}"


def test_the_warehouse_answers_a_dl_item_question_from_the_link(live_server, pg, page):
    """#202 (DL migration F3), updated for #231: the DL matching ladder's own nástenka
    kind now renders on the SEPARATE `/otazky-dl` page (reached via its own `/sklad-dl/
    <key>` link, never the AI-orders `/sklad/<key>` one — the two boards are now a real
    server-side security boundary, not just a display choice, see `test_api.py`'s
    role/kind tests), through the real browser, proving the teach.py KINDS + httpapi
    dispatch + JS wiring is actually LIVE end-to-end, not just correct when called
    directly."""
    from app.httpapi import dl_key
    from app.orders import dl_memory, teach

    qid = teach.ask_dl_item(
        pg, message_id="e-dl1", supplier_ean="S1", supplier_name="Mlyn Vrbovce s.r.o.",
        wording="Múka hladká T512", quantity=25, unit="kg",
        candidates=[{"gtin": "G1", "name": "Múka hladká T512 25kg"}],
        delivery_date="08.08.2026", reason="neznáme znenie na DL")
    assert qid

    console = _collect_console(page)
    page.goto(f"{live_server}/sklad-dl/{dl_key('e2e-secret')}")
    page.wait_for_url(f"{live_server}/otazky-dl")

    page.wait_for_selector("text=Ktorá karta je táto DL položka?")
    page.wait_for_selector("text=Múka hladká T512")
    page.click('button:has-text("Múka hladká T512 25kg")')

    page.wait_for_selector("text=Naposledy naučené")
    q = teach.get(pg, qid)
    assert q["status"] == "answered"
    assert dl_memory.resolve(pg, "S1", "Múka hladká T512").gtin == "G1"

    assert console == [], f"browser console not clean: {console}"


def test_the_warehouse_answers_a_dl_supplier_question_from_the_link(live_server, pg, page):
    """#202, updated for #231: the "ktorý dodávateľ?" half of the DL nástenka — a
    genuinely different question flow from dl_item (picking a SUPPLIER, not an item
    card), through the real browser, on the SAME separate `/otazky-dl` page."""
    from app.httpapi import dl_key
    from app.orders import dl_supplier_memory as dsm
    from app.orders import teach

    qid = teach.ask_dl_supplier(
        pg, message_id="e-dl2", sender_email="obchod@mlynvrbovce.sk",
        candidates=[{"ean_edi": "S1", "name": "Mlyn Vrbovce s.r.o."}],
        delivery_date="08.08.2026")
    assert qid

    console = _collect_console(page)
    page.goto(f"{live_server}/sklad-dl/{dl_key('e2e-secret')}")
    page.wait_for_url(f"{live_server}/otazky-dl")

    page.wait_for_selector("text=Ktorý dodávateľ?")
    page.wait_for_selector("text=obchod@mlynvrbovce.sk")
    page.click('button:has-text("Mlyn Vrbovce s.r.o.")')

    page.wait_for_selector("text=Naposledy naučené")
    q = teach.get(pg, qid)
    assert q["status"] == "answered"
    assert dsm.resolve(pg, "obchod@mlynvrbovce.sk") == {"ean_edi": "S1",
                                                         "name": "Mlyn Vrbovce s.r.o."}

    assert console == [], f"browser console not clean: {console}"


def test_the_warehouse_adds_a_brand_new_dl_supplier_from_the_card(live_server, pg, page):
    """#235: the dead end this ticket exists to close — HK LOAN (#236) is the concrete
    case. A DL supplier absent from `dl_supplier_snapshot` entirely can now be added
    right on the dl_supplier question card, through the real browser, on her own
    `/otazky-dl` board — mirrors #234's own customer test above."""
    from app.httpapi import dl_key
    from app.orders import dl_supplier_memory as dsm
    from app.orders import teach

    qid = teach.ask_dl_supplier(
        pg, message_id="e-dl3", sender_email="gnip@hkloan.eu", candidates=[],
        delivery_date="11.08.2026")
    assert qid

    console = _collect_console(page)
    page.goto(f"{live_server}/sklad-dl/{dl_key('e2e-secret')}")
    page.wait_for_url(f"{live_server}/otazky-dl")

    page.wait_for_selector("text=gnip@hkloan.eu")
    page.click('button:has-text("Nový dodávateľ")')
    page.wait_for_selector('input[placeholder="EAN kód EDI *"]')
    page.fill('input[placeholder="EAN kód EDI *"]', "2000000000900")
    page.fill('input[placeholder="názov firmy *"]', "HK LOAN s.r.o.")
    page.click('button:has-text("Uložiť nového dodávateľa")')

    page.wait_for_selector("text=Naposledy naučené")
    q = teach.get(pg, qid)
    assert q["status"] == "answered"
    assert dsm.resolve(pg, "gnip@hkloan.eu") == {
        "ean_edi": "2000000000900", "name": "HK LOAN s.r.o."}
    row = pg.execute(
        "SELECT name FROM dl_supplier_overrides WHERE ean_edi='2000000000900'").fetchone()
    assert row == ("HK LOAN s.r.o.",)

    assert console == [], f"browser console not clean: {console}"


def test_the_warehouse_reclaims_an_existing_dl_supplier_after_a_collision(
        live_server, pg, page):
    """Deep-review finding (independent review, same PR as #235's own fixes): the
    server-side EAN-collision check (httpapi.py's `_api_orders_answer_new_dl_supplier`)
    already refuses a duplicate EAN with 409 + the existing supplier's details — this
    proves the FRONTEND actually uses that payload, mirroring `newCustomerForm`'s own
    one-click reclaim button (#234), through the real browser, not just the API
    response shape."""
    from app.httpapi import dl_key
    from app.orders import dl_snapshot, teach
    from app.orders import dl_supplier_memory as dsm

    dl_snapshot.upsert_dl_supplier(
        pg, override_id=None, orig_ean_edi=None, orig_city=None,
        ean_edi="2000000000955", name="Už existujúci s.r.o.", emails=[], city="Trnava")
    qid = teach.ask_dl_supplier(
        pg, message_id="e-dl9", sender_email="iny@x.sk", candidates=[],
        delivery_date="11.08.2026")
    assert qid

    console = _collect_console(page)
    page.goto(f"{live_server}/sklad-dl/{dl_key('e2e-secret')}")
    page.wait_for_url(f"{live_server}/otazky-dl")

    page.wait_for_selector("text=iny@x.sk")
    page.click('button:has-text("Nový dodávateľ")')
    page.wait_for_selector('input[placeholder="EAN kód EDI *"]')
    page.fill('input[placeholder="EAN kód EDI *"]', "2000000000955")
    page.fill('input[placeholder="názov firmy *"]', "Iný názov s.r.o.")
    page.click('button:has-text("Uložiť nového dodávateľa")')

    page.wait_for_selector("text=Už existujúci s.r.o.")
    page.click('button:has-text("Použiť existujúceho Už existujúci s.r.o.")')

    page.wait_for_selector("text=Naposledy naučené")
    q = teach.get(pg, qid)
    assert q["status"] == "answered"
    assert dsm.resolve(pg, "iny@x.sk") == {
        "ean_edi": "2000000000955", "name": "Už existujúci s.r.o."}
    assert pg.execute(
        "SELECT count(*) FROM dl_supplier_overrides WHERE ean_edi='2000000000955'"
    ).fetchone()[0] == 1

    # This is the one test in the file that deliberately triggers a real 409 through a
    # real fetch() (proving the httpapi.py collision check above and the frontend's own
    # use of its `existing` payload). Chromium logs an unavoidable, application-code-free
    # "Failed to load resource: ... 409" console entry for ANY non-2xx fetch() response —
    # that is a browser-native network log, not a JS console.error() call, and every
    # OTHER assertion in this file (real console.error/pageerror calls) still applies in
    # full. Filter ONLY that one expected, intentionally-provoked entry.
    real_errors = [m for m in console if "Failed to load resource" not in m]
    assert real_errors == [], f"browser console not clean: {real_errors}"


def test_the_warehouse_adds_a_brand_new_dl_product_from_the_card(live_server, pg, page):
    """#235: the dl_item half of the same fix (the #236 "Soľ jedlá..." case — a genuinely
    new catalog card with no GTIN in Codex yet)."""
    from app.httpapi import dl_key
    from app.orders import dl_memory, teach

    qid = teach.ask_dl_item(
        pg, message_id="e-dl4", supplier_ean="S1", supplier_name="Mlyn s.r.o.",
        wording="Soľ jedlá kamenná jódovaná 0,7-0,16 mm", quantity=1000, unit="kg",
        candidates=[], delivery_date="11.08.2026")
    assert qid

    console = _collect_console(page)
    page.goto(f"{live_server}/sklad-dl/{dl_key('e2e-secret')}")
    page.wait_for_url(f"{live_server}/otazky-dl")

    page.wait_for_selector("text=Soľ jedlá kamenná jódovaná")
    page.click('button:has-text("Nový produkt")')
    page.wait_for_selector('input[placeholder="GTIN (EAN kód) *"]')
    # prefilled straight from the DL wording itself
    assert page.locator('input[placeholder="názov produktu *"]').input_value() \
        == "Soľ jedlá kamenná jódovaná 0,7-0,16 mm"
    page.fill('input[placeholder="GTIN (EAN kód) *"]', "4003885181808")
    page.click('button:has-text("Uložiť nový produkt")')

    page.wait_for_selector("text=Naposledy naučené")
    q = teach.get(pg, qid)
    assert q["status"] == "answered"
    assert dl_memory.resolve(
        pg, "S1", "Soľ jedlá kamenná jódovaná 0,7-0,16 mm").gtin == "4003885181808"
    row = pg.execute(
        "SELECT name FROM dl_catalog_overrides WHERE gtin='4003885181808'").fetchone()
    assert row == ("Soľ jedlá kamenná jódovaná 0,7-0,16 mm",)

    assert console == [], f"browser console not clean: {console}"


def test_the_dl_link_never_shows_an_orders_question(live_server, pg, page):
    """#231: the DL nástenka must never render an AI-orders item/customer question, even
    if one happens to be open at the same time — the split is real, not cosmetic."""
    from app.httpapi import dl_key
    from app.orders import teach

    teach.ask(pg, message_id="e-mix1", customer_ean="2000000000001",
              customer_name="Zákazník A", wording="Šiška", quantity=30, unit="ks",
              candidates=[{"gtin": "SLI50", "name": "Šiška džemová 50g"}])
    dl_qid = teach.ask_dl_item(
        pg, message_id="e-mix2", supplier_ean="S1", supplier_name="Mlyn Vrbovce s.r.o.",
        wording="Múka hrubá", quantity=25, unit="kg",
        candidates=[{"gtin": "G3", "name": "Múka hrubá 25kg"}])
    assert dl_qid

    console = _collect_console(page)
    page.goto(f"{live_server}/sklad-dl/{dl_key('e2e-secret')}")
    page.wait_for_url(f"{live_server}/otazky-dl")

    page.wait_for_selector("text=Múka hrubá")
    assert page.locator("text=Šiška").count() == 0, \
        "an AI-orders item question must never render on the DL nástenka"

    assert console == [], f"browser console not clean: {console}"


def test_the_warehouse_answers_from_the_link_with_no_login(live_server, pg, page):
    """The user's ask: the warehouse must not type a password (2026-07-31).

    Through the real browser, from the signed link only — no /login visit at all — and the
    page must reach nothing but the questions (a 401 in the console would prove it tried).
    """
    from app.httpapi import sklad_key
    from app.orders import memory, teach

    ean = "2000000000009"
    qid = teach.ask(pg, message_id="e-sklad", customer_ean=ean, customer_name="Zákazník S",
                    wording="šiška bez hesla", quantity=12, unit="ks",
                    candidates=[{"gtin": "AAA", "name": "Karta A"},
                                {"gtin": "BBB", "name": 'Karta B "špeciál"'}],
                    delivery_date="07.08.2026", reason="neznáme znenie")
    assert qid

    console = _collect_console(page)
    page.goto(f"{live_server}/sklad/{sklad_key('e2e-secret')}")
    page.wait_for_url(f"{live_server}/otazky")

    backend_ver = page.request.get(f"{live_server}/version").text().strip()
    assert backend_ver in page.locator('[data-testid="version"]').inner_text()

    page.wait_for_selector("text=šiška bez hesla")
    page.click('button:has-text("Karta B")')
    page.wait_for_selector("text=Naposledy naučené")
    assert memory.resolve(pg, ean, "šiška bez hesla").gtin == "BBB"

    # and it can be taken back from the same page
    page.click('button:has-text("vrátiť")')
    page.wait_for_selector('button:has-text("Karta A")')
    pg.rollback()
    assert memory.resolve(pg, ean, "šiška bez hesla") is None

    # the archive is NOT reachable from this link
    assert page.request.get(f"{live_server}/api/messages").status == 401
    assert console == [], f"browser console not clean: {console}"


def test_the_warehouse_can_search_the_whole_catalog_when_no_candidate_fits(live_server, pg, page):
    """#149: the warehouse's real complaint — none of the 6 offered candidates is the right
    card ("chlebík granč"). The warehouse must be able to search the WHOLE catalog straight
    from the question card and pick any of it; that pick must teach exactly like a candidate
    click (same endpoint, same release, same memory write) — and no /sklad request may ever
    reach the mail archive."""
    from app.httpapi import sklad_key
    from app.orders import memory, snapshot, teach

    snapshot.import_snapshot(
        pg,
        "GTIN,Názov,doplnok\n"
        "G1,Multicereálny kváskový chlieb 500g,\n"
        "G2,Jankové buchty malinové 80g,\n"
        "G3,Chlebík granč 400g,\n",     # the actually-correct card — never offered as a candidate
        "Názov organizácie,EAN kód EDI,E-mail\nVýberofka Levoča,2000000000042,vyber@x.sk\n")
    ean = "2000000000042"
    qid = teach.ask(pg, message_id="e-granc", customer_ean=ean, customer_name="Výberofka Levoča",
                    wording="chlebík granč", quantity=1, unit="ks",
                    candidates=[{"gtin": "G1", "name": "Multicereálny kváskový chlieb 500g"},
                                {"gtin": "G2", "name": "Jankové buchty malinové 80g"}],
                    delivery_date="08.08.2026", reason="istota 57 % je pod hranicou 70 %")
    assert qid

    console = _collect_console(page)
    page.goto(f"{live_server}/sklad/{sklad_key('e2e-secret')}")
    page.wait_for_url(f"{live_server}/otazky")

    backend_ver = page.request.get(f"{live_server}/version").text().strip()
    assert backend_ver in page.locator('[data-testid="version"]').inner_text()

    page.wait_for_selector("text=chlebík granč")
    # neither offered candidate is the answer — the search box finds the real card, with its
    # weight visible in the name so a variant can be told apart from another
    page.fill('input[placeholder^="hľadaj v celom katalógu"]', "granč")
    page.wait_for_selector("text=Chlebík granč 400g")
    page.click("text=Chlebík granč 400g")

    # settled exactly like a candidate click: leaves the open list, taught for this customer
    page.wait_for_selector("text=Naposledy naučené")
    assert memory.resolve(pg, ean, "chlebík granč").gtin == "G3"

    # the search endpoint it used, and the answer endpoint, reach nothing beyond the questions
    assert page.request.get(f"{live_server}/api/messages").status == 401
    assert console == [], f"browser console not clean: {console}"


def test_the_warehouse_link_can_reach_the_knowledge_base_and_teach_a_wording(live_server, pg, page):
    """#104: /znalosti is reachable from the same signed link as /otazky and lets the
    warehouse teach a wording->card assignment DIRECTLY, without a pending question."""
    from app.httpapi import sklad_key
    from app.orders import memory, snapshot, teach

    snapshot.import_snapshot(
        pg,
        "GTIN,Názov,doplnok\nG1,Rožok štandart 50g,\n",
        "Názov organizácie,EAN kód EDI,E-mail\nPekáreň Rožok,2000000000777,pekaren@x.sk\n")
    teach.ask(pg, message_id="e-kb", customer_ean="2000000000777", customer_name="Pekáreň Rožok",
             wording="šiška", quantity=1, unit="ks", candidates=[{"gtin": "G1", "name": "x"}])

    console = _collect_console(page)
    page.goto(f"{live_server}/sklad/{sklad_key('e2e-secret')}")
    page.wait_for_url(f"{live_server}/otazky")

    # reached from the questions page, not typed by hand — pre-filled with the customer
    # and wording the open question was about
    page.click("a.kb")
    page.wait_for_url(f"{live_server}/znalosti/2000000000777*")
    page.wait_for_selector("text=Pekáreň Rožok")

    backend_ver = page.request.get(f"{live_server}/version").text().strip()
    assert backend_ver in page.locator('[data-testid="version"]').inner_text()

    page.fill('input[placeholder^="znenie"]', "domáci rožtek")
    page.fill('input[placeholder^="hľadaj kartu"]', "rožok")
    page.wait_for_selector("text=Rožok štandart 50g")
    page.click("text=Rožok štandart 50g")
    # scoped to the alias form specifically — #127/#128 added a customer-data edit box
    # ABOVE it on this same page with its own "Uložiť zmeny" button (substring overlap)
    page.click('.box:has-text("Pridať priradenie") button:has-text("Uložiť")')

    page.wait_for_selector("text=domáci rožtek")
    assert memory.resolve(pg, "2000000000777", "domáci rožtek").gtin == "G1"

    # curated -> deletable, and gone from both the page and match resolution
    page.click('.row:has-text("domáci rožtek") button:has-text("zmazať")')
    # "Zatiaľ nič." also appears (correctly) in the always-empty global section below, so
    # waiting for it would resolve on that pre-existing match instead of this delete's
    # re-render — wait for the specific row to actually disappear instead.
    page.wait_for_selector("text=domáci rožtek", state="detached")
    assert memory.resolve(pg, "2000000000777", "domáci rožtek") is None

    # still bounded by the same security boundary as /otazky
    assert page.request.get(f"{live_server}/api/messages").status == 401
    assert console == [], f"browser console not clean: {console}"


def test_znalosti_lets_the_warehouse_curate_products_and_customers_directly(
        live_server, pg, page):
    """#127+#128: add/edit/retire a product card and a customer straight from the page —
    no waiting for the hourly sheet refresh, no order_questions row needed first."""
    from app.httpapi import sklad_key
    from app.orders import customer as customer_mod
    from app.orders import snapshot

    snapshot.import_snapshot(
        pg,
        "GTIN,Názov,doplnok\nG1,Rožok štandart 50g,\n",
        "Názov organizácie,EAN kód EDI,E-mail\nPekáreň Rožok,2000000000777,pekaren@x.sk\n")

    console = _collect_console(page)
    page.goto(f"{live_server}/sklad/{sklad_key('e2e-secret')}")
    page.wait_for_url(f"{live_server}/otazky")
    page.goto(f"{live_server}/znalosti")
    page.wait_for_selector("text=Karty výrobkov")

    # --- product card: add, then edit the SAME gtin, then retire it -----------------
    page.fill('input[placeholder="GTIN"]', "NOVY1")
    page.fill('input[placeholder="názov karty"]', "Chlieb domáci 1kg")
    page.click('button:has-text("Uložiť (nový GTIN")')
    page.wait_for_selector("text=Chlieb domáci 1kg")

    page.fill('input[placeholder="GTIN"]', "NOVY1")
    page.fill('input[placeholder="názov karty"]', "Chlieb domáci 1kg OPRAVA")
    page.click('button:has-text("Uložiť (nový GTIN")')
    page.wait_for_selector("text=Chlieb domáci 1kg OPRAVA")

    page.once("dialog", lambda d: d.accept())
    page.click('button:has-text("Vyradiť kartu")')
    page.wait_for_selector("text=Chlieb domáci 1kg OPRAVA", state="detached")
    sid = snapshot.latest_snapshot_id(pg)
    assert "NOVY1" not in {r["gtin"] for r in snapshot.load_catalog(pg, sid)}

    # --- customer: add a brand-new one, verify it actually matches by e-mail --------
    clients_box = page.locator('#wrap .box:has-text("Odberatelia")')
    # #234: the clientsBox() EAN field is now marked required in its own placeholder
    clients_box.locator('input[placeholder="EAN kód EDI *"]').fill("9998887776")
    clients_box.locator('input[placeholder="názov firmy"]').fill("Nový odberateľ s.r.o.")
    clients_box.locator('input[placeholder="e-maily (čiarkou oddelené)"]').fill("novy@odber.sk")
    clients_box.locator('button:has-text("Uložiť")').click()
    page.wait_for_selector("text=Nový odberateľ s.r.o.")

    sid2 = snapshot.latest_snapshot_id(pg)
    customers = snapshot.load_customers(pg, sid2)
    hit = customer_mod.resolve(customers, "novy@odber.sk", "", "")
    assert hit is not None and hit.ean_edi == "9998887776"

    # --- customer: edit the EXISTING seeded one from its own detail page ------------
    page.goto(f"{live_server}/znalosti/2000000000777")
    page.wait_for_selector("text=Upraviť údaje zákazníka")
    ean_field = page.locator('.box:has-text("Upraviť údaje zákazníka") '
                             'input[placeholder="EAN kód EDI"]')
    assert ean_field.input_value() == "2000000000777"
    name_field = page.locator('.box:has-text("Upraviť údaje zákazníka") '
                              'input[placeholder="názov firmy"]')
    assert name_field.input_value() == "Pekáreň Rožok"
    name_field.fill("Pekáreň Rožok OPRAVENÉ")
    page.once("dialog", lambda d: d.accept())
    page.click('.box:has-text("Upraviť údaje zákazníka") button:has-text("Uložiť zmeny")')
    page.wait_for_url(f"{live_server}/znalosti/2000000000777")
    page.wait_for_selector("text=Pekáreň Rožok OPRAVENÉ")

    assert page.request.get(f"{live_server}/api/messages").status == 401
    assert console == [], f"browser console not clean: {console}"


def test_the_dl_board_shows_a_prominent_alert_banner_for_the_reliability_gauges(
        live_server, pg, page):
    """#239 reopened, finding 5: the three current-state gauges (quarantined / pending
    alerts / open import incidents) used to be three words silently appended to the
    small `#dlStats` header strip — visually indistinguishable from ordinary text, and
    on a quiet day nothing rendered at all so the feature was easy to miss entirely.
    They now render in their own prominent, plain-Slovak banner."""
    from app.httpapi import dl_key
    from app.orders import dl_alerts

    pg.execute(
        "INSERT INTO messages (message_id, category, processed, attempts) "
        "VALUES ('e-dl-stuck', 'dodacie_listy', false, 5)")
    dl_alerts.enqueue(pg, 243, "dl_stuck_classified", "<p>x</p>",
                      message_id="e-dl-stuck2")

    console = _collect_console(page)
    page.goto(f"{live_server}/sklad-dl/{dl_key('e2e-secret')}")
    page.wait_for_url(f"{live_server}/otazky-dl")

    page.wait_for_selector("#dlAlertBanner:visible")
    banner_text = page.locator("#dlAlertBanner").inner_text()
    assert "1 dodací(ch) list(ov)" in banner_text
    assert "1 upozornenie/upozornení" in banner_text
    # the plain today/yesterday summary strip stays separate and unaffected
    assert "zaseknutých" not in page.locator("#dlStats").inner_text()

    assert console == [], f"browser console not clean: {console}"


def test_the_dl_alert_banner_stays_hidden_on_a_quiet_day(live_server, pg, page):
    from app.httpapi import dl_key

    console = _collect_console(page)
    page.goto(f"{live_server}/sklad-dl/{dl_key('e2e-secret')}")
    page.wait_for_url(f"{live_server}/otazky-dl")
    page.wait_for_selector("#dlStats")
    # `state="attached"` proves the banner element genuinely EXISTS in the DOM on a
    # quiet day (not just "no such element", which would also satisfy a bare
    # is_hidden() check and prove nothing about the fix).
    page.wait_for_selector("#dlAlertBanner", state="attached")
    assert page.locator("#dlAlertBanner").is_hidden()
    assert console == [], f"browser console not clean: {console}"


def test_dl_new_product_form_survives_the_5s_auto_refresh(live_server, pg, page):
    """#306: the board runs `setInterval(load, 5000)` and `load()` wipes `#wrap`
    (`textContent=''`) to rebuild every card from scratch. While the skladníčka is
    typing into the "➕ Nový produkt" form, that periodic rebuild destroys her
    half-filled form mid-entry — the reported "stále ma to vyhodí, vôbec nejde
    doplniť". This proves the in-progress form + typed value survive one full
    auto-refresh cycle. RED before the fix (wiped), GREEN after (skipped while busy)."""
    from app.httpapi import dl_key
    from app.orders import teach

    qid = teach.ask_dl_item(
        pg, message_id="e-dl306", supplier_ean="S1", supplier_name="HK LOAN",
        wording="Soľ jedlá", quantity=1, unit="ks",
        candidates=[{"gtin": "G1", "name": "Múka pšeničná"}])
    assert qid

    console = _collect_console(page)
    page.goto(f"{live_server}/sklad-dl/{dl_key('e2e-secret')}")
    page.wait_for_url(f"{live_server}/otazky-dl")

    page.wait_for_selector("text=Soľ jedlá")
    page.click('button:has-text("Nový produkt")')
    gtin = page.locator('input[placeholder^="GTIN"]')
    gtin.fill("8588888888882")
    assert gtin.input_value() == "8588888888882"      # sanity: typed before the refresh

    # wait past ONE full 5s auto-refresh cycle (its own two fetches finish well within)
    page.wait_for_timeout(6000)

    # the form must still be OPEN and the typed GTIN must still be there
    assert gtin.is_visible(), "the '➕ Nový produkt' form was wiped by the 5s auto-refresh"
    assert gtin.input_value() == "8588888888882", \
        "the typed GTIN was wiped by the 5s auto-refresh"
    assert console == [], f"browser console not clean: {console}"
