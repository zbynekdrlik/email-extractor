"""#485: the dev2 CODEX supplier-receipts push tool — CI-testable via the DI seam, no duckdb.

Synthetic rows only (made-up suppliers, numbers) — this repo is public.
"""
import datetime

from tools import codex_receipts_push as push


def _row(**kw):
    base = {"receipt_number": 261004409.0, "supplier_ico": 12345678.0,
            "supplier_ean": " 2000000000991 ", "supplier_name": " Testovací dodávateľ ",
            "receipt_date": datetime.date(2026, 9, 9),
            "receipt_date_to": datetime.date(2026, 9, 10),
            "dl_number": 526013012, "invoice_number": "526013012",
            "invoice_vs": "0526013012", "total": 191.7000001, "invoice_total": 191.7,
            "line_count": 2, "entered_at": datetime.datetime(2026, 9, 11, 14, 38, 20),
            "sdpoh": 10}
    base.update(kw)
    return base


def test_module_imports_without_duckdb_or_requests():
    assert hasattr(push, "build_receipts") and hasattr(push, "run")


def test_build_receipts_normalizes_codex_doubles_dates_and_money():
    out = push.build_receipts([_row()])
    assert out == [{
        "receipt_number": "261004409", "supplier_ico": "12345678",
        "supplier_ean": "2000000000991", "supplier_name": "Testovací dodávateľ",
        "receipt_date": "2026-09-09", "receipt_date_to": "2026-09-10",
        "dl_number": "526013012", "invoice_number": "526013012",
        "invoice_vs": "0526013012", "total": 191.7, "invoice_total": 191.7,
        "line_count": 2, "entered_at": "2026-09-11T14:38:20+02:00", "sdpoh": 10}]


def test_build_receipts_keeps_missing_links_empty_and_skips_unusable_rows():
    out = push.build_receipts([
        _row(dl_number=None, invoice_number=None, invoice_vs=None, total=None,
             invoice_total=None, receipt_date_to=None, entered_at=None),
        _row(receipt_number=None),
        _row(receipt_date=None),
    ])
    assert len(out) == 1
    r = out[0]
    assert (r["dl_number"], r["invoice_number"], r["invoice_vs"]) == ("", "", "")
    assert r["total"] is None and r["invoice_total"] is None
    assert r["receipt_date_to"] == r["receipt_date"] and r["entered_at"] is None


def test_receipts_url_is_derived_from_the_orders_push_url():
    assert (push.receipts_url("https://addon.example/api/codex/orders")
            == "https://addon.example/api/codex/receipts")


def test_run_posts_the_whole_window_once_with_the_source_time_and_days():
    posted = []

    def fake_poster(url, headers, body):
        posted.append((url, headers, body))
        return {"stored": len(body["receipts"])}

    res = push.run("https://addon/api/codex/receipts", "tok", days=60,
                   query=lambda: [_row(), _row(receipt_number=261004410.0),
                                  _row(receipt_number=None)],
                   as_of=lambda: datetime.datetime(2026, 10, 4, 16, 19, 49),
                   poster=fake_poster)
    assert res == {"fetched": 3, "receipts": 2, "stored": 2}
    assert len(posted) == 1, "one POST — the add-on replaces its copy atomically"
    url, headers, body = posted[0]
    assert url == "https://addon/api/codex/receipts" and headers["X-Token"] == "tok"
    assert body["source_as_of"] == "2026-10-04T16:19:49+00:00" and body["days"] == 60
    assert [r["receipt_number"] for r in body["receipts"]] == ["261004409", "261004410"]


def test_run_refuses_to_post_an_empty_window():
    posted = []
    res = push.run("u", "t", query=lambda: [], as_of=lambda: None,
                   poster=lambda *a: posted.append(a))
    assert posted == [] and res["receipts"] == 0 and "nothing posted" in res["error"]


def test_main_without_url_or_token_is_a_usage_error(monkeypatch):
    monkeypatch.delenv("CODEX_PUSH_URL", raising=False)
    monkeypatch.delenv("CODEX_RECEIPTS_PUSH_URL", raising=False)
    monkeypatch.delenv("CODEX_PUSH_TOKEN", raising=False)
    assert push.main([]) == 2


def test_the_pushed_line_names_the_target_host_but_never_the_path_or_credentials():
    line = push.pushed_line({"fetched": 3, "receipts": 2, "stored": 2},
                            "https://user:secret@addon.example/api/codex/receipts?token=x")
    assert line == "pushed: fetched=3 receipts=2 stored=2 to=https://addon.example"


def test_main_prints_the_pushed_line(monkeypatch, capsys):
    monkeypatch.setenv("CODEX_PUSH_URL", "https://addon.example/api/codex/orders")
    monkeypatch.setenv("CODEX_PUSH_TOKEN", "tok")
    seen = {}

    def fake_run(url, token, db_path, days):
        seen.update(url=url, token=token, days=days)
        return {"fetched": 1, "receipts": 1, "stored": 1}

    monkeypatch.setattr(push, "run", fake_run)
    assert push.main(["--days", "30"]) == 0
    assert seen == {"url": "https://addon.example/api/codex/receipts", "token": "tok",
                    "days": 30}
    assert capsys.readouterr().out.strip() == (
        "pushed: fetched=1 receipts=1 stored=1 to=https://addon.example")


def test_the_query_dedups_firma_and_faktury_before_joining():
    """The fan-out trap (codex-orders.md): raw.firma NICO and raw.faktury (NICO, SROK,
    IPORCFAKT) carry duplicate rows — both are grouped BEFORE the join, and the receipt lines
    are grouped per (NCD, NICO) so one receipt is one row; never the SLOVNORMAL own IČO."""
    sql = push._SQL
    assert "FROM raw.firma GROUP BY NICO" in sql
    assert "GROUP BY NICO, SROK, IPORCFAKT" in sql
    assert "GROUP BY NCD, NICO" in sql
    assert f"NICO <> {push.OWN_ICO}" in sql
    assert "NMNOZ" not in sql, "NMNOZ is mis-decoded in CODEX — never read it"
