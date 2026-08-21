"""DL Odoo message wording + link-only-when-actionable (#229 follow-ups).

`build_success`/`build_review` message wording, verified against real live incidents:

1. **Outcome first, always.** Every DL message opens with a short, unambiguous
   per-document outcome line before any detail — a reader must never be left guessing
   whether the attached DL actually went through (live complaint on run 406: "preco
   nenapisalo do odoo ze dodaci list bol spracovany"). `build_success`'s headline states
   the doc number + item count explicitly.

2. **Dashboard link only when there is something to actually resolve there** -- mirrors
   `report.build_summary`'s own `has_board_item`/`has_other_action` rule for the orders
   pipeline (unified via the shared `report.link_line()` helper, not duplicated): a clean
   "ok" success (even one carrying a purely informational note) gets no link; `partial`
   (real unmatched items -> a genuine open `dl_item` question) and `review` always do.
"""
from app.orders import dl_report, report

# --- build_success: outcome-first headline, exact template ------------------

def test_success_headline_states_doc_number_and_item_count():
    html = dl_report.build_success(
        "Pekáreň Lunys", "0100239749", "10.08.2026", "dodavatel@lunys.sk", "subj",
        [{"name": "Rožok 50g", "quantity": 10, "unit": "ks"},
         {"name": "Chlieb", "quantity": 2, "unit": "ks"}])
    assert "Dodací list 0100239749" in html
    assert "spracovaný a nahratý do ORIONu" in html
    assert "(2 položiek)" in html
    # outcome line must be the FIRST thing in the message
    assert html.index("spracovaný a nahratý do ORIONu") < html.index("Rožok 50g")


def test_partial_success_headline_still_names_the_outcome_first():
    html = dl_report.build_success(
        "Pekáreň Lunys", "0100239749", "10.08.2026", "dodavatel@lunys.sk", "subj",
        [{"name": "Rožok 50g", "quantity": 10, "unit": "ks"}],
        unmatched_items=["Neznámy chlebík (žiadna zhoda)"], partial=True)
    assert "ČIASTOČNE" in html
    assert "nahratý do ORIONu" in html


# --- link only when there is real board action (#229 follow-up 2) -----------

def test_clean_ok_success_carries_no_dashboard_link_even_with_a_note():
    html = dl_report.build_success(
        "Pekáreň Lunys", "0100239749", "10.08.2026", "dodavatel@lunys.sk", "subj",
        [{"name": "Rožok 50g", "quantity": 10, "unit": "ks"}],
        borderline_notes=["Rožok 50g (istota 62 %)"],
        link="http://example.com/sklad/xyz")
    assert "<a href" not in html


def test_partial_success_with_real_unmatched_items_carries_the_link():
    html = dl_report.build_success(
        "Pekáreň Lunys", "0100239749", "10.08.2026", "dodavatel@lunys.sk", "subj",
        [{"name": "Rožok 50g", "quantity": 10, "unit": "ks"}],
        unmatched_items=["Neznámy chlebík (žiadna zhoda)"], partial=True,
        link="http://example.com/sklad/xyz")
    assert "<a href" in html
    assert "http://example.com/sklad/xyz" in html


def test_review_message_always_carries_the_link_when_given():
    html = dl_report.build_review("Zlyhalo párovanie dodávateľa: timeout",
                                  supplier_name="", doc_number="0100239749",
                                  link="http://example.com/sklad/xyz")
    assert "<a href" in html
    assert "http://example.com/sklad/xyz" in html


def test_review_message_with_no_link_renders_without_one():
    html = dl_report.build_review("Zlyhalo párovanie dodávateľa: timeout")
    assert "<a href" not in html


# --- shared helper reused by both notify paths (#229 follow-up 2) -----------

def test_link_line_is_the_single_shared_markup_used_by_orders_too():
    html = report.link_line("http://example.com/sklad/xyz")
    assert "<a href=\"http://example.com/sklad/xyz\"" in html
    assert report.link_line("") == ""
