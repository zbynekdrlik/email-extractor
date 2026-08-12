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
          reject_reason="", change=False):
    return {"status": status, "delivery_date": delivery_date, "item_count": item_count,
            "missing_count": missing_count, "reject_reason": reject_reason, "change": change}


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


# --- #159: the link points at where something is REALLY waiting, never a generic bucket ---

def test_a_plain_review_reason_is_named_but_gets_no_sklad_link():
    """08-03 fix: a "review" status with nothing ever written to `order_questions`/
    `held_orders` (e.g. "no item matched a card") used to get the SAME /sklad link as a
    genuinely held order — pointing the warehouse at an empty board. Now it points at the
    dashboard generically instead, never the sklad key."""
    html = report.build_summary(
        "Pekáreň X",
        [_order(status="review", item_count=0, reject_reason="Zákazník nebol nájdený")],
        link="http://x/sklad/k")
    assert "http://x/sklad/k" not in html
    assert "Zákazník nebol nájdený" in html
    assert "dashboard" in html.lower() or "otvor" in html.lower()


def test_an_error_order_gets_no_sklad_link_either():
    html = report.build_summary(
        "Pekáreň X", [_order(status="error", reject_reason="Odoslanie do ORIONu zlyhalo")],
        link="http://x/sklad/k")
    assert "http://x/sklad/k" not in html
    assert "dashboard" in html.lower() or "otvor" in html.lower()


# "partial" keeps the /sklad link (unchanged, see `test_a_partial_order_gets_the_link`
# above): by construction, a partial ship always has a genuinely open `order_questions`
# row for whatever it could not match — either freshly asked this run, or one left open
# from an earlier hold the deadline sweep just released. Only a status that NEVER
# correlates with a real board item (review/error/unverified-only, and never a
# change-of-order) loses the link.


# --- #159: a change-of-order gets its OWN wording and NEITHER link -----------------

def test_a_change_of_order_gets_its_own_wording_and_no_link_at_all():
    """08-03 CÉDER incident: nothing is EVER queued for a change request (it is always
    resolved by hand in ORION, stated plainly in the reason paragraph already), so the
    message must carry neither the /sklad link nor the generic dashboard hint — and the
    bit must say "žiadosť o zmenu", never the generic "treba zadať ručne"."""
    html = report.build_summary(
        "CÉDER s.r.o.",
        [_order(status="review", item_count=1,
               reject_reason="E-mail je zmena už zadanej objednávky — uprav ju ručne "
                             "v ORIONe (pôvodný súbor začína ORDER_000647_20260808_)",
               change=True)],
        link="http://x/sklad/k")
    assert "http://x/sklad/k" not in html
    assert "nástenke" not in html.lower()
    assert "dashboard" not in html.lower() and "otvor" not in html.lower()
    assert "žiadosť o zmenu" in html.lower()
    assert "treba zadať ručne" not in html.lower()
    assert "ORDER_000647_20260808_" in html, "the useful original-file detail must survive"


def test_a_change_of_order_alongside_a_real_held_order_still_gets_the_link():
    """The exclusion is per-message, not absolute: when something ELSE in the same run IS
    genuinely queued, the link belongs in the message regardless of the change order."""
    html = report.build_summary(
        "Zákazník", [_order(status="held", item_count=2),
                    _order(status="review", change=True,
                          reject_reason="E-mail je zmena už zadanej objednávky")],
        link="http://x/sklad/k")
    assert "http://x/sklad/k" in html
    assert "žiadosť o zmenu" in html.lower()


def test_unverified_items_are_still_counted_and_pointed_at_the_dashboard():
    """The AGEL-incident phantom-item safeguard (extract.py's `unverified`) must survive
    the shortening — a model-claimed item the e-mail text does not prove must remain
    visible, even though the shortened message no longer lists it by name. It is resolved
    from the message detail on the ADMIN dashboard, never from the sklad board (there is
    no order_questions/held_orders row for it at all) — so it gets the dashboard hint, not
    the /sklad link (#159's generalized rule)."""
    html = report.build_summary("Pekáreň X", [_order(status="ok")], unverified_count=2,
                                link="http://x/sklad/k")
    assert "http://x/sklad/k" not in html
    assert "2" in html
    assert "dashboard" in html.lower() or "otvor" in html.lower()


def test_no_unverified_items_never_mentions_them():
    html = report.build_summary("Pekáreň X", [_order(status="ok")], unverified_count=0,
                                link="http://x/sklad/k")
    assert "overiť" not in html.lower()
    assert "http://x/sklad/k" not in html


# --- #187 review finding: the extraction stage's own notice must actually reach Odoo ---

def test_a_note_is_rendered_as_its_own_short_paragraph():
    html = report.build_summary(
        "Pekáreň X", [_order(status="ok")],
        notes="V citovanom texte e-mailu (za '>') je dátum 11.8., ktorý ešte neprešiel.")
    assert "11.8" in html
    assert "citovanom" in html


def test_no_note_never_adds_an_empty_paragraph():
    html_with = report.build_summary("Pekáreň X", [_order(status="ok")], notes="niečo")
    html_without = report.build_summary("Pekáreň X", [_order(status="ok")], notes="")
    assert "niečo" in html_with
    assert "niečo" not in html_without


def test_a_note_is_escaped_like_reject_reason():
    html = report.build_summary("Pekáreň X", [_order(status="ok")],
                                notes="<script>alert(1)</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


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


# --- #231: the DL-only nástenka link is a genuinely separate one ------------

def test_dl_sklad_link_is_empty_when_dashboard_base_url_is_unset():
    class Cfg:
        dashboard_base_url = ""
        secret_key = "s"
        data_dir = "/tmp"
    assert report.dl_sklad_link(Cfg()) == ""


def test_dl_sklad_link_points_at_sklad_dl_not_sklad():
    class Cfg:
        dashboard_base_url = "http://46.224.130.35:8099"
        public_base_url = "http://e0ac7775-email-extractor:8099"
        secret_key = "s"
        data_dir = "/tmp"
    link = report.dl_sklad_link(Cfg())
    assert link.startswith("http://46.224.130.35:8099/sklad-dl/")
    assert "e0ac7775" not in link
    # a genuinely different key from the orders link for the SAME secret (#231)
    assert link != report.sklad_link(Cfg())


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


def test_post_from_config_channel_id_overrides_the_configured_orders_channel():
    """#151: a delivery-note import alert must reach the delivery-notes channel, not the
    orders one, without touching every other caller's default behaviour."""
    sent = {}

    def transport(url, headers, payload):
        sent.update(payload=payload)
        return {"id": 1}

    class Cfg:
        odoo_url = "https://erp.example.sk"
        odoo_api_key = "k"
        odoo_db = "odoo"
        orders_channel_id = 152

    report.post_from_config(Cfg(), "<p>x</p>", transport=transport, channel_id=243)
    assert sent["payload"]["ids"] == [243]


def test_post_from_config_falls_back_to_orders_channel_when_no_channel_id_given():
    sent = {}

    def transport(url, headers, payload):
        sent.update(payload=payload)
        return {"id": 1}

    class Cfg:
        odoo_url = "https://erp.example.sk"
        odoo_api_key = "k"
        odoo_db = "odoo"
        orders_channel_id = 152

    report.post_from_config(Cfg(), "<p>x</p>", transport=transport)
    assert sent["payload"]["ids"] == [152]


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


def test_workflow_can_be_overridden_so_static_orders_are_never_mislabeled_ai_orders(pg):
    """#133: the static-orders engine shares this same timeline — an event it logs must
    never look, on the admin dashboard, like it came from the AI pipeline."""
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('m3', 'static_orders')")
    report.log_event(pg, "m3", stage="uploaded_orion", status="ok",
                     outcome="EDI vytvorené: KARMEN_1_007.txt", workflow="static_orders")
    row = pg.execute(
        "SELECT workflow, stage, status FROM email_events WHERE message_id='m3'").fetchone()
    assert row == ("static_orders", "uploaded_orion", "ok")


def test_workflow_defaults_to_ai_orders_when_not_given(pg):
    """Every pre-existing caller must keep its old behaviour unchanged."""
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('m4', 'ai_orders')")
    report.log_event(pg, "m4", stage="review", status="review", outcome="x")
    row = pg.execute("SELECT workflow FROM email_events WHERE message_id='m4'").fetchone()
    assert row[0] == "ai_orders"


# --- #196: the daily match-provenance digest ------------------------------

def _stats(**over):
    base = {"day": "2026-08-05", "runs": 10, "orders": 12, "errors": 0,
           "items": 100, "deterministic": 70, "llm": 20, "review": 10}
    base.update(over)
    return base


def test_the_digest_names_the_day_and_the_headline_counts():
    html = report.build_daily_digest(_stats(), days_since_incident=3)
    assert "2026-08-05" in html
    assert "10" in html and "12" in html and "100" in html


def test_the_digest_shows_all_three_provenance_buckets_with_percentages():
    html = report.build_daily_digest(_stats(), days_since_incident=3)
    assert "70" in html and "70 %" in html
    assert "20" in html and "20 %" in html
    assert "10" in html and "10 %" in html


def test_errors_are_only_mentioned_when_there_are_any():
    clean = report.build_daily_digest(_stats(errors=0), days_since_incident=3)
    assert "zlyhan" not in clean.lower()
    dirty = report.build_daily_digest(_stats(errors=2), days_since_incident=3)
    assert "2" in dirty and "zlyhan" in dirty.lower()


def test_no_incident_ever_recorded_is_rendered_honestly_not_as_zero():
    html = report.build_daily_digest(_stats(), days_since_incident=None)
    assert "žiadny potvrdený incident" in html.lower()
    assert "0 d" not in html.lower()


def test_days_since_incident_is_named_in_slovak_plural_form():
    one = report.build_daily_digest(_stats(), days_since_incident=1)
    assert "1 deň" in one
    few = report.build_daily_digest(_stats(), days_since_incident=3)
    assert "3 dni" in few
    many = report.build_daily_digest(_stats(), days_since_incident=11)
    assert "11 dní" in many


def test_a_nothing_processed_day_still_renders_cleanly():
    html = report.build_daily_digest(
        {"day": "2026-08-05", "runs": 0, "orders": 0, "errors": 0, "items": 0,
         "deterministic": 0, "llm": 0, "review": 0}, days_since_incident=5)
    assert "2026-08-05" in html
    assert "%" not in html   # no percentages computed against zero items


def test_the_link_is_only_rendered_when_given():
    no_link = report.build_daily_digest(_stats(), days_since_incident=3, link="")
    assert "<a href" not in no_link
    with_link = report.build_daily_digest(_stats(), days_since_incident=3,
                                          link="http://x/sklad/k")
    assert 'href="http://x/sklad/k"' in with_link


# --- #239 reopened, finding 2: the DL digest is its own STANDALONE message -------
#
# Was an optional section embedded in build_daily_digest() (#204) — that made the
# WHOLE combined message (orders + DL) post to orders_channel_id, so every DL notice
# landed with the wrong (sales) audience. build_dl_digest() is now independent, so the
# caller (reliability.maybe_post_daily_digest) can route it to delivery_notes_channel_id.

def _dl_stats(**over):
    base = {"day": "2026-08-05", "runs": 0, "errors": 0, "items": 0, "deterministic": 0,
           "llm": 0, "review": 0, "duplicates": 0, "announced_mismatch": 0}
    base.update(over)
    return base


def test_build_daily_digest_never_mentions_delivery_notes():
    """The orders-only digest must stay orders-only — DL content belongs exclusively
    in build_dl_digest()'s own separate message."""
    html = report.build_daily_digest(_stats(), days_since_incident=3)
    assert "Dodacie listy" not in html
    assert "dodacích listov" not in html.lower()


def test_a_quiet_dl_day_renders_nothing():
    assert report.build_dl_digest(_dl_stats()) == ""
    assert report.build_dl_digest(None) == ""


def test_dl_activity_renders_its_own_message():
    html = report.build_dl_digest(
        _dl_stats(runs=5, items=8, errors=1, duplicates=2, announced_mismatch=1))
    assert html != ""
    assert "Denný prehľad dodacích listov" in html
    assert "5" in html and "8" in html
    assert "duplicitn" in html.lower()
    assert "ohlásil dodací list" in html


def test_dl_duplicates_alone_still_render_the_message():
    """Runs can be 0 (nothing new the DL worker touched) while a duplicate/mismatch
    was still found — the message must appear either way, never gated on `runs` alone."""
    html = report.build_dl_digest(_dl_stats(duplicates=1))
    assert html != ""
    assert "1" in html


# --- #239: three current-state gauges — a silent backlog must never hide -----------

def test_quarantined_alone_triggers_the_message_even_with_zero_runs():
    """A day with no NEW DL activity but an EXISTING quarantined backlog must still be
    mentioned — the whole point of #239 is that a silent backlog is invisible."""
    html = report.build_dl_digest(_dl_stats(quarantined=2))
    assert html != ""
    assert "2" in html
    assert "vzdal" in html.lower()


def test_pending_alerts_alone_triggers_the_message():
    html = report.build_dl_digest(_dl_stats(pending_alerts=3))
    assert html != ""
    assert "3" in html
    assert "čaká na odoslanie" in html.lower() or "čakajú na odoslanie" in html.lower()


def test_open_import_incidents_alone_triggers_the_message():
    html = report.build_dl_digest(_dl_stats(open_import_incidents=1))
    assert html != ""
    assert "problém s importom" in html.lower()


def test_a_genuinely_quiet_dl_day_still_renders_nothing_with_gauges_present():
    """The three new gauges default to 0 via `_dl_stats()` — a quiet day must stay
    quiet even though the digest now checks three more fields."""
    assert report.build_dl_digest(_dl_stats()) == ""


def test_dl_digest_link_is_only_rendered_when_given():
    no_link = report.build_dl_digest(_dl_stats(runs=1), link="")
    assert "<a href" not in no_link
    with_link = report.build_dl_digest(_dl_stats(runs=1), link="http://x/sklad-dl/k")
    assert 'href="http://x/sklad-dl/k"' in with_link
