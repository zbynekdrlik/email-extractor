"""#459: the ONE warehouse-board link builder + the `?next` open-redirect guard on the
signed key routes.

Every Odoo message / reminder / DL post now routes the warehouse straight to the correct
unified-nástenka tab (orders → Otázky objednávky, DL → Otázky sklad), optionally
deep-linking one question (`?q=<id>`). These tests pin:
  * `board_link` output shape (per kind, with/without a question id, key + base reuse);
  * `safe_next` open-redirect guard;
  * the `/sklad/<k>` + `/sklad-dl/<k>` routes honour a valid `?next` and default by key kind;
  * every warehouse-link GENERATOR now emits the new `?next=/nastenka/<slug>` shape.
"""
from datetime import UTC, datetime

import pytest

from app import linkutil
from app.board import links as board_links
from app.config import Config
from app.httpapi import create_app, dl_key, sklad_key
from app.orders import dl_report, question_alerts, report

ORDERS_NEXT = "?next=/nastenka/otazky-objednavky"
DL_NEXT = "?next=/nastenka/otazky-sklad"


class Cfg:
    dashboard_base_url = "https://ex.sk"
    secret_key = "s"
    data_dir = "/tmp"


def _cfg():
    return Cfg()


# --- board_link ------------------------------------------------------------------------

def test_board_link_orders_lands_on_the_orders_questions_tab():
    url = board_links.board_link(_cfg(), "orders")
    key = linkutil.sklad_key("s")
    assert url == f"https://ex.sk/sklad/{key}{ORDERS_NEXT}"


def test_board_link_dl_lands_on_the_dl_questions_tab():
    url = board_links.board_link(_cfg(), "dl")
    key = linkutil.dl_key("s")
    assert url == f"https://ex.sk/sklad-dl/{key}{DL_NEXT}"


def test_board_link_deep_links_one_question_with_encoded_q():
    url = board_links.board_link(_cfg(), "orders", question_id=7)
    # the inner `?q=` must be URL-encoded so it stays part of the `next` VALUE
    assert url.endswith("?next=/nastenka/otazky-objednavky%3Fq%3D7")


def test_board_link_dl_deep_links_one_question():
    url = board_links.board_link(_cfg(), "dl", question_id=42)
    assert url.endswith("?next=/nastenka/otazky-sklad%3Fq%3D42")


def test_board_link_uses_the_linkutil_key_never_public_base_url():
    cfg = _cfg()
    cfg.public_base_url = "http://machine:8080"   # the 0.9.10 trap — must be ignored
    url = board_links.board_link(cfg, "orders")
    assert url.startswith("https://ex.sk/sklad/")
    assert "machine:8080" not in url


def test_board_link_is_empty_without_a_human_base_url():
    cfg = _cfg()
    cfg.dashboard_base_url = ""
    assert board_links.board_link(cfg, "orders") == ""


def test_board_link_rejects_an_unknown_kind():
    with pytest.raises(ValueError):
        board_links.board_link(_cfg(), "nonsense")


# --- safe_next -------------------------------------------------------------------------

@pytest.mark.parametrize("nxt", [
    "/nastenka",
    "/nastenka/otazky-sklad",
    "/nastenka/otazky-objednavky?q=5",
])
def test_safe_next_accepts_internal_board_paths(nxt):
    assert board_links.safe_next(nxt) == nxt


@pytest.mark.parametrize("nxt", [
    None, "", "/otazky", "/", "//evil.com", "https://evil.com",
    "/nastenkaX", "\\\\evil", "/nastenka\\x",
])
def test_safe_next_rejects_everything_else(nxt):
    assert board_links.safe_next(nxt) is None


# --- the signed key routes -------------------------------------------------------------

def _client():
    cfg = Config(pg_dsn="postgresql://unused", data_dir="/tmp", api_token="tok",
                 dash_password="secret", secret_key="test-secret")
    app = create_app(cfg)
    app.testing = True
    return app.test_client()


def test_orders_key_defaults_to_the_orders_tab():
    r = _client().get("/sklad/" + sklad_key("test-secret"))
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/nastenka/otazky-objednavky")


def test_dl_key_defaults_to_the_dl_tab():
    r = _client().get("/sklad-dl/" + dl_key("test-secret"))
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/nastenka/otazky-sklad")


def test_a_valid_internal_next_is_honoured():
    r = _client().get("/sklad/" + sklad_key("test-secret"),
                      query_string={"next": "/nastenka/otazky-sklad?q=3"})
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/nastenka/otazky-sklad?q=3")


def test_a_next_outside_nastenka_is_ignored_and_falls_back_to_the_default():
    r = _client().get("/sklad/" + sklad_key("test-secret"),
                      query_string={"next": "https://evil.com/steal"})
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/nastenka/otazky-objednavky")


def test_the_hmac_check_is_unchanged_by_the_next_support():
    r = _client().get("/sklad/" + "0" * 32,
                      query_string={"next": "/nastenka/otazky-sklad"})
    assert r.status_code == 403


# --- every warehouse-link GENERATOR now emits the new shape ----------------------------

def test_report_sklad_link_generator_emits_the_orders_tab_shape():
    link = report.sklad_link(_cfg())
    assert "/sklad/" in link and ORDERS_NEXT in link


def test_report_dl_sklad_link_generator_emits_the_dl_tab_shape():
    link = report.dl_sklad_link(_cfg())
    assert "/sklad-dl/" in link and DL_NEXT in link


def test_link_line_keeps_the_text_and_carries_the_new_link():
    html = report.link_line(report.sklad_link(_cfg()))
    assert "Rieš na nástenke" in html
    assert ORDERS_NEXT in html


def test_build_summary_orders_message_carries_the_new_next_shape():
    html = report.build_summary(
        customer_name="Zákazník", orders=[{
            "status": "held", "delivery_date": "2026-09-20", "item_count": 1,
            "missing_count": 0, "reject_reason": ""}],
        new_questions=1, link=report.sklad_link(_cfg()), cfg=_cfg())
    assert "Rieš na nástenke" in html
    assert ORDERS_NEXT in html


def test_dl_review_message_carries_the_new_dl_next_shape():
    html = dl_report.build_review("Treba skontrolovať", supplier_name="Dodávateľ",
                                  link=report.dl_sklad_link(_cfg()))
    assert "Rieš na nástenke" in html
    assert DL_NEXT in html


def test_question_reminder_group_carries_the_new_next_shape():
    row = {"id": 1, "kind": "dl_supplier", "customer_ean": "", "customer_name": "",
           "wording": "gnip@hkloan.eu", "item_key": "", "context": {}, "payload": {},
           "created_at": datetime.now(UTC), "reminder_sent_at": None}
    html = question_alerts._group_html([row], {1: 1}, 2, report.dl_sklad_link(_cfg()))
    assert "Rieš na nástenke" in html
    assert DL_NEXT in html


# --- #473: the password-free History deep link (one mail's detail on the board) ---------
#
# An Odoo "treba doriešiť" message about a review / error mail (nothing waiting as a board
# question) must land the warehouse — without a password — on the História tab with THAT
# mail's detail open. The mail id rides inside `next` as `?q=<message_id>`, URL-encoded
# TWICE (once as the q value, once as part of the whole `next` value) so real RFC Message-IDs
# (`<…$…@…>`, Gmail's `+`, a stray `/`) survive the signed-key redirect intact.

ORDERS_HISTORY_NEXT = "?next=/nastenka/historia-objednavok"
DL_HISTORY_NEXT = "?next=/nastenka/historia-dl"
# SYNTHETIC Message-ID carrying every character class a real one uses (Outlook `<!&!…>`,
# `$`, Gmail `+`, a `/`) — never real mail.
TRICKY_MID = "<!&!001101dd0000$aa+bb/cc$@example-pekaren.test>"


def test_board_link_orders_history_lands_on_the_orders_history_tab():
    url = board_links.board_link(_cfg(), "orders_history")
    assert url == f"https://ex.sk/sklad/{linkutil.sklad_key('s')}{ORDERS_HISTORY_NEXT}"


def test_board_link_dl_history_lands_on_the_dl_history_tab_with_the_dl_key():
    url = board_links.board_link(_cfg(), "dl_history")
    assert url == f"https://ex.sk/sklad-dl/{linkutil.dl_key('s')}{DL_HISTORY_NEXT}"


def test_board_link_history_deep_links_one_mail_double_encoded():
    url = board_links.board_link(_cfg(), "orders_history", message_id="<m1@x.test>")
    # `<`/`@`/`>` are encoded once as the q value (%3C/%40/%3E) and once more as part of
    # the `next` value (%25…), and `?q=` itself is %3F/%3D — so q stays INSIDE `next`.
    assert url.endswith(f"{ORDERS_HISTORY_NEXT}%3Fq%3D%253Cm1%2540x.test%253E")


def test_the_history_tab_slugs_are_real_board_tabs():
    """A deep link to a slug the board does not know hard-404s (the lane-6 `otazky-dl`
    trap) — the history targets must be real `TABS` slugs."""
    from app.board import TABS
    slugs = {s for s, _ in TABS}
    assert board_links.ORDERS_HISTORY_TAB.rsplit("/", 1)[1] in slugs
    assert board_links.DL_HISTORY_TAB.rsplit("/", 1)[1] in slugs


def test_board_link_refuses_a_message_seed_on_a_question_kind_and_vice_versa():
    with pytest.raises(ValueError):
        board_links.board_link(_cfg(), "orders", message_id="<m1@x.test>")
    with pytest.raises(ValueError):
        board_links.board_link(_cfg(), "dl_history", question_id=5)


def test_board_link_history_is_empty_without_a_human_base_url():
    cfg = _cfg()
    cfg.dashboard_base_url = ""
    assert board_links.board_link(cfg, "orders_history", message_id="<m1@x.test>") == ""


@pytest.mark.parametrize("kind,route", [("orders_history", "/sklad/"),
                                        ("dl_history", "/sklad-dl/")])
def test_the_history_link_round_trips_the_message_id_through_the_signed_key_redirect(
        kind, route):
    """The server-side half of a warehouse click: the built link → the signed key route →
    `safe_next` → 302 to the History tab whose `?q` decodes back to the EXACT Message-ID
    (a `+` must not turn into a space, a `/`, `&` or `$` must survive). The browser half
    (the tab opening that mail's detail) is the Playwright test in test_e2e.py; the detail
    route resolving a `/` id is test_board_history.py's `…with_a_slash…` test."""
    from urllib.parse import parse_qs, urlsplit

    class C:
        dashboard_base_url = "http://localhost"
        secret_key = "test-secret"
        data_dir = "/tmp"
    url = board_links.board_link(C(), kind, message_id=TRICKY_MID)
    assert route in url
    parts = urlsplit(url)
    r = _client().get(parts.path + "?" + parts.query)
    assert r.status_code == 302
    loc = urlsplit(r.headers["Location"])
    tab = "historia-objednavok" if kind == "orders_history" else "historia-dl"
    assert loc.path.endswith(f"/nastenka/{tab}")
    assert parse_qs(loc.query)["q"] == [TRICKY_MID]


def test_report_history_link_generators_emit_the_history_shape():
    assert ORDERS_HISTORY_NEXT in report.history_link(_cfg(), "<m1@x.test>")
    assert "/sklad/" in report.history_link(_cfg(), "<m1@x.test>")
    assert DL_HISTORY_NEXT in report.dl_history_link(_cfg(), "<m1@x.test>")
    assert "/sklad-dl/" in report.dl_history_link(_cfg(), "<m1@x.test>")
    # no message id → the tab itself (still password-free), never the admin dashboard
    assert report.history_link(_cfg(), "").endswith(ORDERS_HISTORY_NEXT)


def test_history_line_wording_and_link():
    html = report.history_line(report.history_link(_cfg(), "<m1@x.test>"))
    assert "Treba doriešiť na nástenke" in html
    assert ORDERS_HISTORY_NEXT in html
    assert report.history_line("") == ""
