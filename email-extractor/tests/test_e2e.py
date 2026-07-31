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
    page.wait_for_selector("text=testovacia pletenka")
    assert memory.resolve(pg, ean, "testovacia pletenka") is None, "the mapping is gone"
    assert teach.open_questions(pg)[0]["id"] == qid, "and it is asked again"

    assert console == [], f"console must be clean: {console}"
