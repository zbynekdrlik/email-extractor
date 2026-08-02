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
    clients_box.locator('input[placeholder="EAN kód EDI"]').fill("9998887776")
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
