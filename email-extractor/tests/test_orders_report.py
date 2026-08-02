"""Reporting to Odoo + the event timeline (#65, shortened #139).

The Odoo message is deliberately short (#139 — 6 messages for one e-mail was read on the
phone as "a lot of orders failed"): exactly ONE message per processed e-mail, headline
only, and a link to the warehouse's nástenka for anything that needs a human. Two
properties are non-negotiable and both are asserted as properties, not as wording:

1. **Nothing is silently dropped** — every unresolved order/question is still COUNTED in
   the message, and the link routes to where the full detail lives.
2. **No item-level / technical detail leaks into Odoo** — no item names, no tracebacks, no
   run ids, no JSON. `build_summary` cannot even receive those (it only takes aggregate
   counts), so this is structural, not a wording choice.
"""
from app.orders import report


def _order(status="ok", delivery_date="04.08.2026", item_count=2, missing_count=0,
          reject_reason=""):
    return {"status": status, "delivery_date": delivery_date, "item_count": item_count,
            "missing_count": missing_count, "reject_reason": reject_reason}


# --- one message, the headline -------------------------------------------

def test_a_clean_run_is_short_and_carries_no_link():
    html = report.build_summary("Pekáreň Testovacia s.r.o.", [_order()], new_questions=0,
                                link="")
    assert "Pekáreň Testovacia" in html
    assert "nástenke" not in html.lower()
    assert "dashboard" not in html.lower()


def test_a_clean_run_names_what_arrived():
    html = report.build_summary("Pekáreň Testovacia s.r.o.",
                                [_order(delivery_date="04.08.2026", item_count=3)])
    assert "1" in html and "04.08.2026" in html
    assert "3" in html


# --- (c) a link appears whenever anything is unresolved --------------------

def test_a_held_order_gets_the_link():
    html = report.build_summary("Pekáreň X", [_order(status="held", item_count=2)],
                                link="http://46.224.130.35:8099/sklad/abc123")
    assert "http://46.224.130.35:8099/sklad/abc123" in html
    assert "čaká" in html.lower()


def test_a_partial_order_gets_the_link():
    html = report.build_summary("Pekáreň X",
                                [_order(status="partial", item_count=3, missing_count=1)],
                                link="http://x/sklad/k")
    assert "http://x/sklad/k" in html
    assert "neúplných" in html.lower() or "neúplná" in html.lower()


def test_a_partial_order_names_how_many_items_are_missing_in_total():
    """`missing_count` is collected per order — it must actually be used, not just carried
    around unread. Uses distinctive numbers (30 + 7 = 37) so the assertion cannot pass by
    coincidentally matching a digit inside an HTML entity code (e.g. `&#65039;`)."""
    html = report.build_summary(
        "Pekáreň X",
        [_order(status="partial", item_count=50, missing_count=30),
         _order(status="partial", item_count=40, missing_count=7)],
        link="http://x/sklad/k")
    assert "37" in html, "30 + 7 missing items across both partial orders"


def test_new_questions_get_the_link_even_when_every_order_shipped():
    html = report.build_summary("Pekáreň X", [_order(status="ok")], new_questions=2,
                                link="http://x/sklad/k")
    assert "http://x/sklad/k" in html
    assert "2" in html


def test_a_review_or_error_order_gets_the_link():
    html = report.build_summary(
        "Pekáreň X",
        [_order(status="review", item_count=0, reject_reason="Zákazník nebol nájdený")],
        link="http://x/sklad/k")
    assert "http://x/sklad/k" in html
    assert "Zákazník nebol nájdený" in html


def test_unverified_items_are_still_counted_and_get_the_link():
    """The AGEL-incident phantom-item safeguard (extract.py's `unverified`) must survive
    the shortening — a model-claimed item the e-mail text does not prove must remain
    visible, even though the shortened message no longer lists it by name."""
    html = report.build_summary("Pekáreň X", [_order(status="ok")], unverified_count=2,
                                link="http://x/sklad/k")
    assert "http://x/sklad/k" in html
    assert "2" in html


def test_no_unverified_items_never_mentions_them():
    html = report.build_summary("Pekáreň X", [_order(status="ok")], unverified_count=0,
                                link="http://x/sklad/k")
    assert "overiť" not in html.lower()
    assert "http://x/sklad/k" not in html


def test_no_link_configured_still_says_something_is_unresolved():
    """Nothing may be silently hidden even when dashboard_base_url is unset (#139) — the
    message must still say a human is needed, just without a clickable link."""
    html = report.build_summary("Pekáreň X", [_order(status="held")], link="")
    assert "http" not in html
    assert "dashboard" in html.lower() or "otvor" in html.lower()


# --- (b) a clean success never shows the link -------------------------------

def test_a_fully_shipped_multi_order_run_has_no_link():
    html = report.build_summary(
        "Pekáreň X", [_order(status="ok", delivery_date="04.08.2026"),
                      _order(status="ok", delivery_date="05.08.2026")],
        new_questions=0, link="http://x/sklad/k")
    assert "http://x/sklad/k" not in html
    assert "nástenke" not in html.lower()


# --- (d) no item-level / technical detail ever reaches Odoo -----------------

def test_build_summary_cannot_leak_item_names_or_traces():
    """The function only takes aggregate counts — no items list, no trace, no JSON — so
    nothing item-level can appear in the message body by construction."""
    html = report.build_summary(
        "Pekáreň X",
        [_order(status="error", reject_reason="Odoslanie do ORIONu zlyhalo: OSError('x')")])
    assert "Traceback" not in html
    assert '"trace"' not in html
    assert "run_id" not in html.lower()


# --- the warehouse link -----------------------------------------------------

def test_sklad_link_is_empty_when_dashboard_base_url_is_unset():
    class Cfg:
        dashboard_base_url = ""
        secret_key = "s"
        data_dir = "/tmp"
    assert report.sklad_link(Cfg()) == ""


def test_sklad_link_builds_from_dashboard_base_url_not_public_base_url():
    class Cfg:
        dashboard_base_url = "http://46.224.130.35:8099"
        public_base_url = "http://e0ac7775-email-extractor:8099"
        secret_key = "s"
        data_dir = "/tmp"
    link = report.sklad_link(Cfg())
    assert link.startswith("http://46.224.130.35:8099/sklad/")
    assert "e0ac7775" not in link


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
