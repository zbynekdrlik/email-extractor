"""End-to-end browser test (Playwright) — the real user workflow against the app.

login -> list -> search -> open detail -> reclassify -> fix modal, asserting a
clean browser console (zero errors/warnings) and a version label that matches
the backend /version (the mandatory web rules).
"""
import re

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


def test_board_nastenka_skeleton_loads_via_the_sklad_link(live_server, pg, page):
    """#442 lane 1: the signed sklad link lands on the unified nástenka, the tab bar and
    the version label render, and the browser console is clean. #449 lane 8: the old
    /otazky page is RETIRED and now redirects back onto the board for the same session."""
    from app.httpapi import sklad_key

    console = _collect_console(page)
    page.goto(f"{live_server}/sklad/{sklad_key('e2e-secret')}")
    page.wait_for_url(re.compile(r"/nastenka"))

    # the version label is present and matches the backend /version
    backend_ver = page.request.get(f"{live_server}/version").text().strip()
    assert backend_ver in page.locator('[data-testid="version"]').inner_text()

    # the tab bar renders
    for label in ("Otázky sklad", "Produkty objednávky", "Zákazníci", "Kôš"):
        page.wait_for_selector(f"text={label}")

    # the board api ping answers for this session
    ping = page.request.get(f"{live_server}/api/board/ping")
    assert ping.ok

    # #449 lane 8: the retired /otazky page redirects back to the board's orders tab
    page.goto(f"{live_server}/otazky")
    page.wait_for_url(re.compile(r"/nastenka/otazky-objednavky"))
    assert page.locator('[data-testid="version"]').count() >= 1

    assert console == [], f"browser console not clean: {console}"


def test_board_nastenka_reachable_via_the_dl_link_too(live_server, pg, page):
    """The DL key must NOT lose access — it lands on the SAME unified nástenka (#442 §6)."""
    from app.httpapi import dl_key

    console = _collect_console(page)
    page.goto(f"{live_server}/sklad-dl/{dl_key('e2e-secret')}")
    page.wait_for_url(re.compile(r"/nastenka"))
    page.wait_for_selector("text=Otázky sklad")
    page.wait_for_selector("text=História dodacích listov")
    assert console == [], f"browser console not clean: {console}"


def _board_seed_item_question(pg, mid="be2e-1", wording="rožok e2e",
                              gtin="E2EGTIN", name="Karta E2E", status="open"):
    import json
    pg.execute("INSERT INTO messages (message_id, from_addr, from_name, subject) "
               "VALUES (%s, 'cust@e2e.sk', 'Pekáreň E2E', 'Objednávka E2E')", (mid,))
    row = pg.execute(
        """INSERT INTO order_questions
               (message_id, customer_ean, customer_name, wording, item_key, kind,
                candidates, delivery_date, reason, context, payload, status)
           VALUES (%s, '2000000000009', 'Zákazník E2E', %s, %s, 'item',
                   %s::jsonb, '', '', '{}'::jsonb, '{}'::jsonb, %s) RETURNING id""",
        (mid, wording, f"item:{wording}",
         json.dumps([{"gtin": gtin, "name": name}]), status)).fetchone()
    return int(row[0])


def test_board_question_tab_answer_undo_and_reopen_in_the_browser(live_server, pg, page):
    """#443 lane 2: the Otázky objednávky tab — answer an item question with a candidate,
    see it under „Zodpovedané", vrátiť the answer (back to open), and znovu-otvoriť an
    expired one — all through the real browser, from the signed sklad link, no login,
    with a clean console and the version label present."""
    from app.httpapi import sklad_key

    _board_seed_item_question(pg, mid="be2e-open", wording="rožok e2e",
                              gtin="E2EGTIN", name="Karta E2E", status="open")
    exp = _board_seed_item_question(pg, mid="be2e-exp", wording="chlieb e2e",
                                    gtin="E2EGTIN2", name="Karta EXP", status="expired")
    pg.execute("UPDATE order_questions SET answer='{\"expired\": true}'::jsonb, "
               "answered_by='auto-expiry', answered_at=now() WHERE id=%s", (exp,))

    console = _collect_console(page)
    page.goto(f"{live_server}/sklad/{sklad_key('e2e-secret')}")
    page.wait_for_url(re.compile(r"/nastenka"))
    page.goto(f"{live_server}/nastenka/otazky-objednavky")

    # version label matches the backend
    backend_ver = page.request.get(f"{live_server}/version").text().strip()
    assert backend_ver in page.locator('[data-testid="version"]').inner_text()

    # the open item question renders; answer it with its candidate
    page.wait_for_selector("text=Karta E2E")
    page.click('button:has-text("Karta E2E")')
    page.wait_for_selector("text=Uložené")

    # under „Zodpovedané" it shows with a „Vrátiť odpoveď" button
    page.click('button:has-text("Zodpovedané")')
    page.wait_for_selector('button:has-text("Vrátiť odpoveď")')
    page.click('button:has-text("Vrátiť odpoveď")')
    page.wait_for_selector("text=Odpoveď vrátená")

    # back under „Otvorené"
    page.click('button:has-text("Otvorené")')
    page.wait_for_selector("text=Karta E2E")

    # expired filter → „Znovu otvoriť" moves it to open
    page.click('button:has-text("Expirované")')
    page.wait_for_selector("text=chlieb e2e")
    page.click('button:has-text("Znovu otvoriť")')
    page.wait_for_selector("text=Znovu otvorené")

    assert console == [], f"browser console not clean: {console}"


def test_board_dl_question_tab_shows_only_dl_kinds(live_server, pg, page):
    """The Otázky sklad tab is DL-scoped — it shows dl_item/dl_supplier, never the orders
    kinds (spec §6 scope-by-tab). Reachable via the DL key, clean console."""
    import json

    from app.httpapi import dl_key

    pg.execute("INSERT INTO messages (message_id, from_addr, from_name, subject) "
               "VALUES ('be2e-dl', 'dodavatel@e2e.sk', 'Dodávateľ E2E', 'DL E2E')")
    pg.execute(
        """INSERT INTO order_questions
               (message_id, customer_ean, customer_name, wording, item_key, kind,
                candidates, delivery_date, reason, context, payload, status)
           VALUES ('be2e-dl', '', '', 'múka e2e dl', 'dlitem:x:muka', 'dl_item',
                   %s::jsonb, '', '', '{}'::jsonb, '{}'::jsonb, 'open')""",
        (json.dumps([{"value": "DLGTIN", "label": "DL karta"}]),))
    _board_seed_item_question(pg, mid="be2e-ord", wording="rožok orders only",
                              status="open")

    console = _collect_console(page)
    page.goto(f"{live_server}/sklad-dl/{dl_key('e2e-secret')}")
    page.wait_for_url(re.compile(r"/nastenka"))
    page.goto(f"{live_server}/nastenka/otazky-sklad")

    page.wait_for_selector("text=múka e2e dl")
    # the orders-only question must NOT appear on the DL tab
    assert page.get_by_text("rožok orders only").count() == 0
    assert console == [], f"browser console not clean: {console}"


def test_board_products_orders_tab_search_edit_delete_in_the_browser(live_server, pg, page):
    """#445 lane 4: the Produkty objednávky tab — search a card, open it, rename + save
    (toast), then delete (soft) with the „Vrátiť v Koši" toast — all through the real
    browser from the signed sklad link, clean console, version label present."""
    from app.httpapi import sklad_key
    from app.orders import snapshot

    snapshot.upsert_catalog_card(pg, "E2EPROD1", "Rožok e2e produkt")
    snapshot.rebuild_from_overrides(pg)

    console = _collect_console(page)
    page.goto(f"{live_server}/sklad/{sklad_key('e2e-secret')}")
    page.wait_for_url(re.compile(r"/nastenka"))
    page.goto(f"{live_server}/nastenka/produkty-objednavky")

    backend_ver = page.request.get(f"{live_server}/version").text().strip()
    assert backend_ver in page.locator('[data-testid="version"]').inner_text()

    page.wait_for_selector("text=Rožok e2e produkt")
    # wait until the debounced SEARCH reload has actually REBUILT the list (the old row node is
    # detached) — the row text is already on screen from the first load, so waiting for it
    # raced the search reload, which then rebuilt the list under the just-opened editor
    # (flaky „element was detached")
    old_row = page.query_selector('.p-row:has-text("Rožok e2e produkt")')
    page.fill("#p-search", "Rožok e2e")
    page.wait_for_function("el => !el.isConnected", arg=old_row)
    page.wait_for_selector("text=Rožok e2e produkt")

    page.click('.p-row:has-text("Rožok e2e produkt") .p-edit')
    page.wait_for_selector(".p-editor .p-name")
    page.fill(".p-editor .p-name", "Rožok e2e premenovaný")
    page.click(".p-editor .p-save")
    page.wait_for_selector("text=Uložené")
    page.wait_for_selector("text=Rožok e2e premenovaný")

    page.click('.p-row:has-text("Rožok e2e premenovaný") .p-edit')
    page.wait_for_selector(".p-editor .p-del")
    page.click(".p-editor .p-del")
    page.click(".p-editor .p-del-yes")
    page.wait_for_selector("text=Vrátiť v Koši")
    # the reload after a soft delete is async — wait for the row to actually detach rather
    # than snapshot-count immediately after the toast (which races load()'s rebuild).
    page.wait_for_selector('.p-row:has-text("Rožok e2e premenovaný")', state="detached")

    assert console == [], f"browser console not clean: {console}"


def test_board_products_sklad_tab_renders_dl_cards_for_the_dl_key(live_server, pg, page):
    """The Produkty sklad tab lists DL catalog cards (admin-only before lane 4), reachable
    via the DL key, clean console, version label present."""
    from app.httpapi import dl_key
    from app.orders import dl_snapshot

    dl_snapshot.upsert_dl_catalog_card(pg, "E2EDL1", "Múka e2e dl", doplnok="muka",
                                       mass=1.0, sklad="100", cena=0.4)
    dl_snapshot.dl_rebuild_from_overrides(pg)

    console = _collect_console(page)
    page.goto(f"{live_server}/sklad-dl/{dl_key('e2e-secret')}")
    page.wait_for_url(re.compile(r"/nastenka"))
    page.goto(f"{live_server}/nastenka/produkty-sklad")

    backend_ver = page.request.get(f"{live_server}/version").text().strip()
    assert backend_ver in page.locator('[data-testid="version"]').inner_text()

    page.wait_for_selector("text=Múka e2e dl")
    assert console == [], f"browser console not clean: {console}"
def test_board_kos_delete_a_card_then_restore_it_in_the_browser(live_server, pg, page):
    """#444 lane 3: an admin deletes a product card via the existing /znalosti API, the delete
    shows up in the Kôš tab, „Vrátiť" restores it (through the real confirm dialog), and the
    card is back in /api/znalosti/catalog — all in the real browser, clean console, version
    label matching the backend."""
    from app.orders import snapshot

    # a real base snapshot so the card shows in /api/znalosti/catalog (the search endpoint,
    # which reads the frozen snapshot — an override-only card would never appear there)
    snapshot.import_snapshot(
        pg,
        "GTIN,Názov,doplnok\nE2EKOS,Karta Kôš E2E,\n",
        "Názov organizácie,EAN kód EDI,E-mail\nZákazník E2E,2000000000042,z@e2e.sk\n")

    console = _collect_console(page)
    page.on("dialog", lambda d: d.accept())   # accept the „Vrátiť" confirm() prompt

    # admin login — reaches BOTH the /znalosti delete API and the Kôš tab
    page.goto(f"{live_server}/login")
    page.fill("input[name=password]", "secret")
    page.click("button[type=submit]")

    # delete the card via the existing /znalosti API (same session cookie) → soft delete + audit
    resp = page.request.delete(f"{live_server}/api/znalosti/products/E2EKOS")
    assert resp.ok, resp.status
    assert not any(it["gtin"] == "E2EKOS"
                   for it in page.request.get(f"{live_server}/api/znalosti/catalog?q=E2EKOS")
                   .json()["items"]), "card should be gone from the catalog after delete"

    # it shows up in the Kôš tab; the version label matches the backend
    page.goto(f"{live_server}/nastenka/kos")
    backend_ver = page.request.get(f"{live_server}/version").text().strip()
    assert backend_ver in page.locator('[data-testid="version"]').inner_text()
    page.fill("#trash-search", "E2EKOS")
    page.wait_for_selector('#trash-rows tr:has-text("E2EKOS")')

    # „Vrátiť" restores it
    page.click('button:has-text("Vrátiť")')
    page.wait_for_selector("text=Vrátené")

    # the card is back in the effective catalog
    back = page.request.get(f"{live_server}/api/znalosti/catalog?q=E2EKOS").json()["items"]
    assert any(it["gtin"] == "E2EKOS" for it in back), "card was not restored to the catalog"

    assert console == [], f"browser console not clean: {console}"


def test_a_deep_link_from_an_odoo_message_opens_the_right_tab_and_highlights_the_question(
        live_server, pg, page):
    """#459: the new warehouse link `.../sklad/<k>?next=/nastenka/otazky-objednavky?q=<id>`
    (the exact `board_link` shape) lands on the Otázky objednávky tab AND scrolls to /
    highlights that one question — through the real browser, no login, clean console."""
    from app.httpapi import sklad_key

    qid = _board_seed_item_question(pg, mid="be2e-deep", wording="deep-link rožok",
                                    gtin="E2EDEEP", name="Karta DEEP", status="open")
    # a second open question so the highlight is provably a SELECTION, not the only card
    _board_seed_item_question(pg, mid="be2e-deep2", wording="iný rožok",
                              gtin="E2EDEEP2", name="Karta INÁ", status="open")

    console = _collect_console(page)
    # the encoded shape board_link emits (%3F/%3D keep `?q=` inside the `next` VALUE)
    page.goto(f"{live_server}/sklad/{sklad_key('e2e-secret')}"
              f"?next=/nastenka/otazky-objednavky%3Fq%3D{qid}")
    page.wait_for_url(re.compile(r"/nastenka/otazky-objednavky"))

    # the target question card is highlighted (the deep-link focus)
    page.wait_for_selector(f"#q-card-{qid}.q-card--focus")
    # both open questions still render — the deep link focuses, it does not filter the list away
    page.wait_for_selector("text=Karta DEEP")
    page.wait_for_selector("text=Karta INÁ")

    assert console == [], f"browser console not clean: {console}"


def _history_seed(pg, mid, *, category, subject, outcome):
    pg.execute("INSERT INTO messages (message_id, category, from_addr, from_name, subject, "
               "processed, proc_status, proc_outcome) VALUES (%s, %s, 'sklad@e2e.sk', "
               "'Zákazník E2E', %s, true, 'review', %s)", (mid, category, subject, outcome))
    db.log_event(pg, mid, category, "review", "review", outcome=outcome)


class _LinkCfg:
    """The Odoo-message builder's view of the live server: the SAME `board_link` code the
    worker thread uses, pointed at the test app (its `secret_key` = the live_server's)."""
    secret_key = "e2e-secret"
    data_dir = "/tmp"

    def __init__(self, base):
        self.dashboard_base_url = base


def test_an_odoo_treba_doriesit_link_opens_that_mail_in_the_orders_history_without_login(
        live_server, pg, page):
    """#473 — the owner's incident, through the real browser: the "📋 Treba doriešiť na
    nástenke" link an Odoo orders summary now carries (built by the REAL `report.
    history_link`) opens — with NO password, from a fresh browser — the História objednávok
    tab with THAT mail's detail already open (not the admin /login, not the list of some
    other tab). The Message-ID carries `!&$+/@<>` like a real Outlook one — the `/` is the
    #473 review finding: the detail route must still resolve it (`<path:message_id>`). Clean
    console (a 404 on the detail fetch would log a console error)."""
    from app.orders import report

    mid = "<!&!e2e473$aa+AA/bb@example-pekaren.test>"   # SYNTHETIC
    _history_seed(pg, mid, category="ai_orders", subject="RE: OBJEDNAVKA E2E 473",
                  outcome="AI nenašla v e-maile žiadnu objednávku")
    # a second mail so the opened detail is provably a SELECTION, not the only row
    _history_seed(pg, "<e2e473-other@example.test>", category="ai_orders",
                  subject="Iná objednávka E2E", outcome="EDI nahraté")

    link = report.history_link(_LinkCfg(live_server), mid)
    assert "/sklad/" in link and "historia-objednavok" in link

    console = _collect_console(page)
    page.goto(link)
    page.wait_for_url(re.compile(r"/nastenka/historia-objednavok"))
    # the detail drawer of THAT mail is open on arrival — no click needed
    page.wait_for_selector("#h-drawer:not([hidden])")
    assert page.locator(".h-detail-title").inner_text() == "RE: OBJEDNAVKA E2E 473"
    assert "AI nenašla v e-maile žiadnu objednávku" in page.locator("#h-detail").inner_text()
    # the list itself still renders both mails (the deep link opens, it does not filter)
    page.wait_for_selector("text=Iná objednávka E2E")
    # the version label matches the backend (a genuinely loaded board page, not /login)
    backend_ver = page.request.get(f"{live_server}/version").text().strip()
    assert backend_ver in page.locator('[data-testid="version"]').inner_text()

    assert console == [], f"browser console not clean: {console}"


def test_a_dl_review_link_opens_that_delivery_note_in_the_dl_history_for_the_dl_key(
        live_server, pg, page):
    """#473, DL side: the /sklad-dl key link a no-question DL review message carries lands
    on História dodacích listov with that delivery note's detail open. Clean console."""
    from app.orders import report

    mid = "<e2e473-dl$1@example-dodavatel.test>"   # SYNTHETIC
    _history_seed(pg, mid, category="dodacie_listy", subject="Dodací list E2E 473",
                  outcome="Email bez prílohy a bez textu")
    link = report.dl_history_link(_LinkCfg(live_server), mid)
    assert "/sklad-dl/" in link and "historia-dl" in link

    console = _collect_console(page)
    page.goto(link)
    page.wait_for_url(re.compile(r"/nastenka/historia-dl"))
    page.wait_for_selector("#h-drawer:not([hidden])")
    assert page.locator(".h-detail-title").inner_text() == "Dodací list E2E 473"
    assert console == [], f"browser console not clean: {console}"


def test_the_retired_znalosti_ean_link_seeds_the_customers_tab_search(live_server, pg, page):
    """#449 lane 8: the retired /znalosti/<ean> page now redirects to
    /nastenka/zakaznici?q=<ean>, and the Zákazníci tab seeds its search box + filters
    to that customer — through the real browser, from the signed sklad link, clean
    console. Proves the ?q= deep-link is FUNCTIONAL (not just present in the URL)."""
    from app.httpapi import sklad_key
    from app.orders import snapshot

    ean = "8590000000449"
    snapshot.upsert_customer(
        pg, override_id=None, orig_ean_edi=None, orig_street=None,
        ean_edi=ean, name="Pekáreň Lane8 E2E", emails=["l8@e2e.sk"],
        city="Košice", street="", zip_="")
    snapshot.rebuild_from_overrides(pg)
    # a second customer so the seed is provably a FILTER, not the only row
    snapshot.upsert_customer(
        pg, override_id=None, orig_ean_edi=None, orig_street=None,
        ean_edi="8590000000998", name="Iná Firma E2E", emails=[],
        city="Žilina", street="", zip_="")
    snapshot.rebuild_from_overrides(pg)

    console = _collect_console(page)
    # arrive through the retired page so the whole redirect chain is exercised
    page.goto(f"{live_server}/sklad/{sklad_key('e2e-secret')}")
    page.wait_for_url(re.compile(r"/nastenka"))
    page.goto(f"{live_server}/znalosti/{ean}")
    page.wait_for_url(re.compile(r"/nastenka/zakaznici"))

    # the search box is pre-seeded with the EAN and the list is filtered to that customer
    assert page.locator("#p-search").input_value() == ean
    page.wait_for_selector("text=Pekáreň Lane8 E2E")
    assert page.get_by_text("Iná Firma E2E").count() == 0

    assert console == [], f"browser console not clean: {console}"


def _board_seed_dl_item_question(pg, mid, wording, cands):
    import json
    pg.execute("INSERT INTO messages (message_id, from_addr, from_name, subject) "
               "VALUES (%s, 'dodavatel@e2e.sk', 'Dodávateľ E2E', 'DL E2E')", (mid,))
    return int(pg.execute(
        """INSERT INTO order_questions
               (message_id, customer_ean, customer_name, wording, item_key, kind,
                candidates, delivery_date, reason, context, payload, status)
           VALUES (%s, '', '', %s, %s, 'dl_item', %s::jsonb, '', '', '{}'::jsonb,
                   '{"supplier_ean": "2000000000009"}'::jsonb, 'open') RETURNING id""",
        (mid, wording, f"dlitem:2000000000009:{mid}",
         json.dumps([{"value": v, "label": lbl} for v, lbl in cands]))).fetchone()[0])


def test_board_dl_item_answer_unrelated_to_the_wording_asks_for_confirmation(
        live_server, pg, page):
    """#465: on Otázky sklad, picking a card that shares NO word with the delivery-note line
    (the 'rožok' answered as 'jablko pražené' misclick) pops a confirmation first — cancel
    keeps the question open, confirm answers it. A lexically plausible pick answers straight
    away with no dialog. Clean console."""
    from app.httpapi import dl_key

    roll = _board_seed_dl_item_question(
        pg, "be2e-465a", "Rožok oravský bez E 50g",
        [("E2EFRUIT", "Ovocie - Zlaté jablko pražené"), ("E2EROLL", "Rožok štandart 50g")])
    oil = _board_seed_dl_item_question(
        pg, "be2e-465b", "Olej olivový z výliskov 1l",
        [("E2EFRUIT", "Ovocie - Zlaté jablko pražené")])

    dialogs, mode = [], {"accept": False}

    def _on_dialog(d):
        dialogs.append(d.message)
        if mode["accept"]:
            d.accept()
        else:
            d.dismiss()

    page.on("dialog", _on_dialog)
    console = _collect_console(page)
    page.goto(f"{live_server}/sklad-dl/{dl_key('e2e-secret')}")
    page.wait_for_url(re.compile(r"/nastenka"))
    page.goto(f"{live_server}/nastenka/otazky-sklad")
    page.wait_for_selector("text=Rožok oravský bez E 50g")

    def _status(qid):
        return pg.execute("SELECT status FROM order_questions WHERE id=%s",
                          (qid,)).fetchone()[0]

    def _wait_answered(qid):
        for _ in range(50):
            if _status(qid) == "answered":
                return True
            page.wait_for_timeout(100)
        return False

    roll_card = page.locator(f"#q-card-{roll}")
    # unrelated pick → confirmation; cancelled → nothing answered
    roll_card.locator('button:has-text("Ovocie - Zlaté jablko pražené")').click()
    page.wait_for_timeout(500)
    assert len(dialogs) == 1, "an unrelated pick must ask for confirmation first"
    assert "Rožok oravský bez E 50g" in dialogs[0]
    assert "Ovocie - Zlaté jablko pražené" in dialogs[0]
    assert _status(roll) == "open", "a cancelled confirmation must not answer the question"

    # plausible pick (shares 'rožok') → answered straight away, no second dialog
    roll_card.locator('button:has-text("Rožok štandart 50g")').click()
    assert _wait_answered(roll)
    assert len(dialogs) == 1

    # unrelated pick confirmed → answered
    mode["accept"] = True
    page.locator(f"#q-card-{oil}").locator(
        'button:has-text("Ovocie - Zlaté jablko pražené")').click()
    assert _wait_answered(oil)
    assert len(dialogs) == 2

    # a card whose NAME shares no word but whose ALIAS does is not a misclick — no dialog
    mode["accept"] = False
    yeast = _board_seed_dl_item_question(pg, "be2e-465c", "Rekord 1 kg, drevo", [])
    pg.execute("UPDATE order_questions SET candidates = %s::jsonb WHERE id = %s",
               ('[{"value": "E2EYEAST", "label": "Droždie", '
                '"alias": "Rekord 10 kg, drevo"}]', yeast))
    page.reload()
    page.locator(f"#q-card-{yeast}").locator('button:has-text("Droždie")').click()
    assert _wait_answered(yeast)
    assert len(dialogs) == 2, "an alias-backed pick must not ask for confirmation"

    assert console == [], f"browser console not clean: {console}"


# --- #467: the board refuses a card number CODEX has no stock card for ------------------

_E2E_CODEX = [
    {"code": "9990000000017", "card_code": "27", "stredisko": 1, "sklad": 1,
     "name": "Rožok so slaninou a syrom 70g"},
    {"code": "9990000000093", "card_code": "93", "stredisko": 1, "sklad": 100,
     "name": "Mak modrý mletý e2e"},
]


def _e2e_codex_and_catalog(pg):
    from app.orders import codex_cards, dl_snapshot
    codex_cards.replace_cards(pg, _E2E_CODEX)
    dl_snapshot._freeze(pg, [{"gtin": "DBASE0", "name": "Base", "doplnok": "", "mass": None,
                              "sklad": "", "cena": None}], [])
    # our card for the CODEX code still carries an OLD, unrelated name (the incident)
    dl_snapshot.upsert_dl_catalog_card(pg, "9990000000017", "Bagetka s kečupom a syrom 80 gr",
                                       sklad="1")
    dl_snapshot.dl_rebuild_from_overrides(pg)


def test_board_products_sklad_refuses_a_code_codex_lacks_and_offers_the_codex_card(
        live_server, pg, page):
    """Produkty sklad: the stale-named card shows its CODEX name; a „Nová karta" with a code
    CODEX lacks is NOT saved — the editor lists CODEX cards with a similar name + code, one
    click puts the right code in, and the save goes through. Clean console, version label."""
    from app.httpapi import dl_key

    _e2e_codex_and_catalog(pg)
    console = _collect_console(page)
    page.goto(f"{live_server}/sklad-dl/{dl_key('e2e-secret')}")
    page.wait_for_url(re.compile(r"/nastenka"))
    page.goto(f"{live_server}/nastenka/produkty-sklad")
    backend_ver = page.request.get(f"{live_server}/version").text().strip()
    assert backend_ver in page.locator('[data-testid="version"]').inner_text()

    # name drift is visible on the row, and the CODEX status line is shown
    row = page.locator('.p-row[data-gtin="9990000000017"]')
    row.wait_for()
    assert "Rožok so slaninou a syrom 70g" in row.locator(".p-codex").inner_text()
    assert "CODEX" in page.locator("#p-codex-status").inner_text()

    # new card with a code CODEX does not have → refused, nothing saved, suggestions shown
    page.click("#p-new")
    page.fill(".p-new-editor .p-gtin", "3698")
    page.fill(".p-new-editor .p-name", "Mak modrý mletý e2e")
    page.click(".p-new-editor .p-save")
    hint = page.locator(".p-new-editor .p-codex-hint")
    hint.wait_for()
    assert "3698" in hint.inner_text() and "CODEX" in hint.inner_text()
    assert pg.execute("SELECT count(*) FROM dl_catalog_overrides WHERE gtin='3698'"
                      ).fetchone()[0] == 0

    # one click takes the CODEX card's code → save succeeds
    hint.locator('.p-codex-use[data-code="9990000000093"]').click()
    assert page.locator(".p-new-editor .p-gtin").input_value() == "9990000000093"
    page.click(".p-new-editor .p-save")
    page.wait_for_selector('.p-row[data-gtin="9990000000093"]')
    assert pg.execute("SELECT name FROM dl_catalog_overrides WHERE gtin='9990000000093'"
                      ).fetchone()[0] == "Mak modrý mletý e2e"

    # a „Nová karta" with a number we ALREADY have is refused (never overwritten) — one click
    # finds that card in the list instead
    page.click("#p-new")
    page.fill(".p-new-editor .p-gtin", "9990000000017")
    page.fill(".p-new-editor .p-name", "Iný názov e2e")
    page.click(".p-new-editor .p-save")
    page.locator('.p-new-editor .p-codex-find[data-code="9990000000017"]').click()
    page.wait_for_selector('.p-row[data-gtin="9990000000093"]', state="detached")
    assert page.locator("#p-search").input_value() == "9990000000017"
    assert pg.execute("SELECT name FROM dl_catalog_overrides WHERE gtin='9990000000017'"
                      ).fetchone()[0] == "Bagetka s kečupom a syrom 80 gr"
    page.fill("#p-search", "")
    page.wait_for_selector('.p-row[data-gtin="9990000000093"]')

    # the drifted card's editor offers „Prevziať názov z CODEXu"
    row.locator(".p-edit").click()
    page.locator('.p-row[data-gtin="9990000000017"] .p-codex-take').click()
    assert page.locator('.p-row[data-gtin="9990000000017"] .p-editor .p-name').input_value() \
        == "Rožok so slaninou a syrom 70g"
    row.locator(".p-edit").click()   # close the editor again (refresh-safety)

    # „Len rozdiely s CODEXom" keeps only the drifted / CODEX-missing cards
    page.check("#p-codex-issues")
    page.wait_for_selector('.p-row[data-gtin="9990000000093"]', state="detached")
    gtins = page.locator(".p-row").evaluate_all("rs => rs.map(r => r.dataset.gtin)")
    assert "9990000000017" in gtins and "9990000000093" not in gtins

    # the refusals ARE deliberate 409s — Chromium logs every non-2xx fetch as "Failed to load
    # resource" (no app console.error); tolerate exactly those TWO entries, nothing else (#235)
    tolerated = [m for m in console if "Failed to load resource" in m and "status of 409" in m]
    assert len(tolerated) == 2, f"exactly the two deliberate refusals: {console}"
    real_errors = [m for m in console if m not in tolerated]
    assert real_errors == [], f"browser console not clean: {real_errors}"


def test_board_dl_item_new_card_refuses_a_code_codex_lacks_and_answers_with_the_codex_card(
        live_server, pg, page):
    """Otázky sklad, the incident's entry point: „➕ Nová karta" with 3698 is refused, the card
    shows which CODEX card has that name (our card under its old name) — one click answers the
    question with it. Nothing is written for 3698. Clean console."""
    from app.httpapi import dl_key

    _e2e_codex_and_catalog(pg)
    qid = _board_seed_dl_item_question(pg, "be2e-467", "Rožok so slaninou a syrom 70g", [])
    console = _collect_console(page)
    page.goto(f"{live_server}/sklad-dl/{dl_key('e2e-secret')}")
    page.wait_for_url(re.compile(r"/nastenka"))
    page.goto(f"{live_server}/nastenka/otazky-sklad")
    card = page.locator(f"#q-card-{qid}")
    card.wait_for()

    card.locator('button:has-text("➕ Nová karta")').click()
    card.locator(".q-in-gtin").fill("3698")
    card.locator(".q-in-name").fill("Rožok so slaninou a syrom 70g")
    card.locator('.q-inline-form button:has-text("Uložiť")').click()
    hint = card.locator(".q-codex-hint")
    hint.wait_for()
    assert "3698" in hint.inner_text() and "CODEX" in hint.inner_text()
    assert pg.execute("SELECT status FROM order_questions WHERE id=%s",
                      (qid,)).fetchone()[0] == "open"
    assert pg.execute("SELECT count(*) FROM dl_catalog_overrides WHERE gtin='3698'"
                      ).fetchone()[0] == 0

    # the hint does NOT freeze the board: close the form, let the 8 s refresh run (a question
    # added meanwhile appears) — the hint is re-rendered on its card, not wiped. The new
    # question's candidate is a card whose OUR name drifted: its button shows the CODEX name.
    drift_q = _board_seed_dl_item_question(pg, "be2e-467b", "Rožok so slaninou 70g", [])
    pg.execute("UPDATE order_questions SET candidates = %s::jsonb WHERE id = %s",
               ('[{"value": "9990000000017", "label": "Bagetka s kečupom a syrom 80 gr", '
                '"codex_name": "Rožok so slaninou a syrom 70g"}]', drift_q))
    card.locator('.q-inline-form button:has-text("Zrušiť")').click()
    btn = page.locator(f"#q-card-{drift_q} .q-btn--cand")
    btn.wait_for(timeout=15000)
    assert "CODEX: Rožok so slaninou a syrom 70g" in btn.inner_text()
    assert card.locator(".q-codex-hint").count() == 1

    card.locator('.q-codex-use[data-code="9990000000017"]').click()
    for _ in range(50):
        row = pg.execute("SELECT status, answer->>'choice' FROM order_questions WHERE id=%s",
                         (qid,)).fetchone()
        if row[0] == "answered":
            break
        page.wait_for_timeout(100)
    assert row == ("answered", "9990000000017")

    # „Nová karta" with a number we ALREADY have → refused (never overwritten), one click answers
    # the question with that existing card
    taken_q = _board_seed_dl_item_question(pg, "be2e-467c", "Bagetka so syrom 80g", [])
    tcard = page.locator(f"#q-card-{taken_q}")
    tcard.wait_for(timeout=15000)
    tcard.locator('button:has-text("➕ Nová karta")').click()
    tcard.locator(".q-in-gtin").fill("9990000000017")
    tcard.locator(".q-in-name").fill("Bagetka so syrom 80g")
    tcard.locator('.q-inline-form button:has-text("Uložiť")').click()
    tcard.locator('.q-codex-use[data-code="9990000000017"]').click()
    for _ in range(50):
        row = pg.execute("SELECT status, answer->>'choice' FROM order_questions WHERE id=%s",
                         (taken_q,)).fetchone()
        if row[0] == "answered":
            break
        page.wait_for_timeout(100)
    assert row == ("answered", "9990000000017")
    assert pg.execute("SELECT name FROM dl_catalog_overrides WHERE gtin='9990000000017'"
                      ).fetchone()[0] == "Bagetka s kečupom a syrom 80 gr"

    # the refusals ARE deliberate 409s — Chromium logs every non-2xx fetch as "Failed to load
    # resource" (no app console.error); tolerate exactly those TWO entries, nothing else (#235)
    tolerated = [m for m in console if "Failed to load resource" in m and "status of 409" in m]
    assert len(tolerated) == 2, f"exactly the two deliberate refusals: {console}"
    real_errors = [m for m in console if m not in tolerated]
    assert real_errors == [], f"browser console not clean: {real_errors}"
