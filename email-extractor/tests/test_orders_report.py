"""Reporting to Odoo + the event timeline (#65).

The report is the warehouse's only view of what the automat did. Two properties are
non-negotiable and both are asserted as properties, not as wording:

1. **Nothing is silently dropped** — every item that did NOT reach the EDI file appears
   in the message, and every item that passed on a weaker rule is named with its reason.
2. **A partial order says so in the HEADER** — an incomplete order still ships (user
   decision 2026-07-30, after the warehouse retyped 11-14 items because of one), so the
   missing items must be impossible to overlook.
"""
from app.orders import report


def _decision(name, gtin, card="Karta", rule="llm_sure", review=False, note=""):
    return {"name": name, "quantity": 10, "unit": "ks", "gtin": gtin, "card": card,
            "rule": rule, "review": review, "note": note or f"rozhodlo {rule}"}


def _result(**kw):
    base = {
        "customer": {"name": "Pekáreň Testovacia s.r.o.", "ean_edi": "2000000000001"},
        "delivery_date": "04.08.2026",
        "order_number": "",
        "items": [_decision("rožok", "G1", "Rožok štandart 50g")],
        "unverified": [],
        "is_change_request": False,
        "notes": "",
        "edi_filename": "ORDER_000001_20260804_120000000.txt",
        "shipped": True,
    }
    base.update(kw)
    return base


# --- nothing is dropped ---------------------------------------------------

def test_every_item_that_did_not_ship_appears_in_the_message():
    html = report.build(_result(items=[
        _decision("rožok", "G1"),
        _decision("torta", None, card="", rule="unmatched", note="istota 41 % je pod 70 %"),
        _decision("chlieb", None, card="", rule="unmatched", note="gramáž nesúhlasí"),
    ]))
    assert "torta" in html and "chlieb" in html
    assert "41" in html and "gramáž" in html, "the REASON must travel with the item"


def test_a_partial_order_names_the_missing_items_in_the_header():
    """One unmatched item used to kill the whole document; now the order ships and the
    gap must be at the very top of the message, not at the bottom."""
    html = report.build(_result(items=[
        _decision("rožok", "G1"),
        _decision("torta", None, card="", rule="unmatched", note="nie je v katalógu"),
    ]))
    head, tail = html[:len(html) // 2], html[len(html) // 2:]
    assert "torta" in head, "the missing item must be in the first half of the message"
    assert "NEÚPLNÁ" in html.upper()
    assert "rožok" in tail or "rožok" in head


def test_items_that_passed_on_a_weaker_rule_are_each_named_with_the_reason():
    html = report.build(_result(items=[
        _decision("vianočka", "G2", rule="llm_borderline", review=True,
                  note="istota 73 % (pod 85 %)"),
        _decision("slimák 130g", "G3", rule="unique_card", review=True,
                  note="gramáž objednávky 130 g vs karta 90 g"),
        _decision("dánske", "G4", rule="history_weight", review=True,
                  note="odoslané podľa histórie (4x)"),
        _decision("rožok", "G5", rule="sibling", review=True,
                  note="tá istá položka bola inde spárovaná"),
    ]))
    for wording in ("vianočka", "slimák 130g", "dánske", "rožok"):
        assert wording in html
    for reason in ("73", "130 g", "histórie", "inde spárovaná"):
        assert reason in html


def test_unverified_items_are_reported_too():
    """An item the model claimed but the email does not prove never ships — the warehouse
    adds it by hand, so it must be in the message."""
    html = report.build(_result(unverified=[{"name": "zákusok", "quantity": 3, "unit": "ks"}]))
    assert "zákusok" in html


def test_a_clean_order_does_not_cry_wolf():
    html = report.build(_result())
    assert "NEÚPLNÁ" not in html.upper()
    assert "prekontroluj" not in html.lower()


# --- refusals ------------------------------------------------------------

def test_a_change_request_explains_which_orion_order_to_fix_by_hand():
    """Deliberately not automated (decision 2026-07-29): the automat must not create a
    second order, so it tells the warehouse exactly which file to look for."""
    html = report.build(_result(shipped=False, is_change_request=True,
                                reject_reason="Email je zmena už zadanej objednávky",
                                change_prefix="ORDER_000001_20260804_"))
    assert "ORDER_000001_20260804_" in html
    assert "ručne" in html.lower()


def test_an_unknown_customer_is_stated_plainly():
    html = report.build(_result(shipped=False, customer={"name": "", "ean_edi": ""},
                                reject_reason="Zákazník nebol nájdený"))
    assert "nebol nájdený" in html.lower()


def test_the_notes_from_the_email_are_carried_over():
    html = report.build(_result(notes="šofér vyzdvihne vo štvrtok ráno"))
    assert "šofér vyzdvihne" in html


# --- delivery ------------------------------------------------------------

def test_posting_uses_message_post_with_html_enabled():
    """`mail.message/create` would post silently with no notification; the channel needs
    `message_post` and `body_is_html`."""
    sent = {}

    def transport(url, headers, payload):
        sent.update(url=url, headers=headers, payload=payload)
        return {"id": 42}

    report.post(url="https://erp.example.sk", api_key="k", db="odoo", channel_id=152,
                html="<p>ahoj</p>", transport=transport)
    assert sent["url"].endswith("/json/2/discuss.channel/message_post")
    assert sent["payload"]["ids"] == [152]
    assert sent["payload"]["body_is_html"] is True
    assert sent["payload"]["subtype_xmlid"] == "mail.mt_comment"
    assert sent["headers"]["X-Odoo-Database"] == "odoo"
    assert "Bearer k" == sent["headers"]["Authorization"]


def test_an_event_is_written_for_the_timeline(pg):
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('m1', 'ai_orders')")
    report.log_event(pg, "m1", stage="uploaded_orion", status="ok",
                     outcome="EDI vytvorené: ORDER_x.txt", detail={"edi_file": "ORDER_x.txt"})
    row = pg.execute(
        "SELECT workflow, stage, status, outcome FROM email_events WHERE message_id='m1'"
    ).fetchone()
    assert row == ("ai_orders", "uploaded_orion", "ok", "EDI vytvorené: ORDER_x.txt")
    # the existing rollup trigger must pick it up, so the dashboard shows the state
    state = pg.execute("SELECT proc_stage, proc_status, edi_file FROM messages").fetchone()
    assert state == ("uploaded_orion", "ok", "ORDER_x.txt")


def test_a_review_event_marks_the_message_for_the_warehouse(pg):
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('m2', 'ai_orders')")
    report.log_event(pg, "m2", stage="review", status="review",
                     outcome="Odoo kontrola (AI orders)")
    row = pg.execute("SELECT proc_status FROM messages WHERE message_id='m2'").fetchone()
    assert row[0] == "review"
