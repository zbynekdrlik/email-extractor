"""DL Odoo message wording + link-only-when-actionable (#229 follow-ups).

Two user-reported gaps in the DL notify messages, both traced to real live incidents:

1. **Outcome first, always.** `build_announced_mismatch` is posted as its OWN, separate
   Odoo message (never merged with `build_success` -- one mail can carry several
   documents, and the mismatch is a property of the whole message, not one document) --
   but it never said whether the attached DL was actually processed, only that a SECOND
   number was announced-but-missing. A reader who only sees this second message cannot
   tell whether the DL went through (live complaint on run 406: "preco nenapisalo do
   odoo ze dodaci list bol spracovany"). Every DL message must now open with a short,
   unambiguous per-document outcome line before any warning.

2. **Dashboard link only when there is something to actually resolve there** -- mirrors
   `report.build_summary`'s own `has_board_item`/`has_other_action` rule for the orders
   pipeline (unified via the shared `report.link_line()` helper, not duplicated): a clean
   "ok" success (even one carrying a purely informational note) gets no link; `partial`
   (real unmatched items -> a genuine open `dl_item` question) and `review` always do.
"""
from app.orders import dl_report, report


def _doc(outcome="ok", doc_number="0100000001", items=None, reason=""):
    d = {"outcome": outcome, "doc_number": doc_number, "supplier_name": "Pekáreň Lunys"}
    if items is not None:
        d["items"] = items
    if reason:
        d["reason"] = reason
    return d


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


# --- build_announced_mismatch: outcome-first + reworded warning + link rule -

def test_mismatch_message_states_the_document_outcome_before_the_warning():
    html = dl_report.build_announced_mismatch(
        "subj", "dodavatel@lunys.sk", ["0100239776"], ["0100239749"],
        documents=[_doc(outcome="ok", doc_number="0100239749",
                        items=[{"name": "Rožok 50g"}] * 10)])
    assert html.index("Dodací list 0100239749") < html.index("ohlásený aj doklad")
    assert "spracovaný a nahratý do ORIONu" in html
    assert "(10 položiek)" in html


def test_mismatch_warning_is_reworded_per_the_user_request():
    html = dl_report.build_announced_mismatch(
        "subj", "dodavatel@lunys.sk", ["0100239776"], ["0100239749"])
    assert "V predmete e-mailu bol ohlásený aj doklad" in html
    assert "0100239776" in html
    assert "ktorý v prílohe nebol" in html
    assert "vyžiadať od dodávateľa" in html
    # the old, unclear header wording is gone
    assert "ohlásil viac dodacích listov" not in html


def test_mismatch_with_no_documents_still_renders_gracefully():
    html = dl_report.build_announced_mismatch(
        "subj", "dodavatel@lunys.sk", ["0100239776"], [])
    assert "V predmete e-mailu bol ohlásený aj doklad" in html


def test_mismatch_with_a_clean_ok_document_carries_no_link():
    html = dl_report.build_announced_mismatch(
        "subj", "dodavatel@lunys.sk", ["0100239776"], ["0100239749"],
        documents=[_doc(outcome="ok", items=[{"name": "x"}])],
        link="http://example.com/sklad/xyz")
    assert "<a href" not in html


def test_mismatch_with_a_review_document_carries_the_link():
    html = dl_report.build_announced_mismatch(
        "subj", "dodavatel@lunys.sk", ["0100239776"], ["0100239749"],
        documents=[_doc(outcome="review", reason="Zlyhalo párovanie dodávateľa")],
        link="http://example.com/sklad/xyz")
    assert "<a href" in html


# --- shared helper reused by both notify paths (#229 follow-up 2) -----------

def test_link_line_is_the_single_shared_markup_used_by_orders_too():
    html = report.link_line("http://example.com/sklad/xyz")
    assert "<a href=\"http://example.com/sklad/xyz\"" in html
    assert report.link_line("") == ""
