"""#485: the dev2 CODEX supplier-receipts push tool — CI-testable via the DI seam, and its SQL
run for real against a synthetic DuckDB file (duckdb is a dev dependency; the tool itself
imports it lazily so the add-on image never needs it).

Synthetic rows only (made-up suppliers, numbers) — this repo is public.
"""
import datetime

import duckdb

from tools import codex_receipts_push as push


def _row(**kw):
    base = {"receipt_number": 261004409.0, "supplier_ico": 12345678.0,
            "supplier_eans": [" 2000000000991 ", "2000000000777", ""],
            "supplier_name": " Testovací dodávateľ ",
            "receipt_date": datetime.date(2026, 9, 9),
            "receipt_date_to": datetime.date(2026, 9, 10),
            "dl_numbers": [526013012, 1144195], "invoice_number": "526013012",
            "invoice_vs": "0526013012", "total": 191.7000001, "invoice_total": 191.7,
            "line_count": 2, "entered_at": datetime.datetime(2026, 9, 11, 14, 38, 20),
            "sdpoh": 10}
    base.update(kw)
    return base


def test_module_imports_without_requests():
    assert hasattr(push, "build_receipts") and hasattr(push, "run")


def test_build_receipts_normalizes_codex_doubles_lists_dates_and_money():
    out = push.build_receipts([_row()])
    assert out == [{
        "receipt_number": "261004409", "supplier_ico": "12345678",
        "supplier_eans": ["2000000000777", "2000000000991"],
        "supplier_name": "Testovací dodávateľ",
        "receipt_date": "2026-09-09", "receipt_date_to": "2026-09-10",
        "dl_numbers": ["1144195", "526013012"], "invoice_number": "526013012",
        "invoice_vs": "0526013012", "total": 191.7, "invoice_total": 191.7,
        "line_count": 2, "entered_at": "2026-09-11T14:38:20+02:00", "sdpoh": 10}]


def test_build_receipts_keeps_missing_links_empty_and_skips_unusable_rows():
    out = push.build_receipts([
        _row(supplier_eans=None, dl_numbers=None, invoice_number=None, invoice_vs=None,
             total=None, invoice_total=None, receipt_date_to=None, entered_at=None),
        _row(receipt_number=None),
        _row(receipt_date=None),
    ])
    assert len(out) == 1
    r = out[0]
    assert (r["supplier_eans"], r["dl_numbers"], r["invoice_number"], r["invoice_vs"]) == (
        [], [], "", "")
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


def test_run_never_posts_a_copy_of_unknown_age():
    """No finished sp001 load in meta.etl_runs: the add-on could not tell how far CODEX's data
    reaches (it would pass as fresh) — nothing is posted."""
    posted = []
    res = push.run("u", "t", query=lambda: [_row()], as_of=lambda: None,
                   poster=lambda *a: posted.append(a))
    assert posted == [] and res["stored"] == 0 and "etl_runs" in res["error"]


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


# --- the SQL itself, against a synthetic codex-bridge DuckDB ------------------------------

def _codex_db(path):
    """The columns the push reads, with the traps CODEX really has: duplicate raw.firma rows
    per NICO (two EDI EANs), duplicate raw.faktury rows, two lines per receipt (one dated a day
    later), an own-IČO transfer, a garbage NCDLIST, a receipt whose invoice is not booked yet,
    and one older than the window."""
    today = datetime.date.today()
    d = today - datetime.timedelta(days=5)
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA raw")
    con.execute("CREATE SCHEMA meta")
    con.execute("""CREATE TABLE raw.sp001 (NCD DOUBLE, ICPOL BIGINT, NICO DOUBLE,
                   NCDLIST DOUBLE, DUCTOBD DATE, NSUMAP DOUBLE, SDRUHFAKT BIGINT, SROK BIGINT,
                   IPORCFAKT BIGINT, UDATUMAKT TIMESTAMP, SDPOH BIGINT)""")
    con.execute("CREATE TABLE raw.firma (NICO DOUBLE, AEDIEAN VARCHAR, ANAZORG VARCHAR)")
    con.execute("""CREATE TABLE raw.faktury (NICO DOUBLE, SDRUHFAKT BIGINT, SROK BIGINT,
                   IPORCFAKT BIGINT, ACFAKTDPH VARCHAR, AVSYMB VARCHAR, NVYMZAK1 DOUBLE,
                   NVYMZAK2 DOUBLE, NVYMZAK3 DOUBLE, NVYMZAK4 DOUBLE)""")
    con.execute("""CREATE TABLE meta.etl_runs (table_name VARCHAR, status VARCHAR,
                   started_at TIMESTAMP, finished_at TIMESTAMP)""")
    ts = datetime.datetime.combine(d, datetime.time(9, 0))
    rows = [  # NCD, ICPOL, NICO, NCDLIST, DUCTOBD, NSUMAP, SDRUHFAKT, SROK, IPORC, UDATUMAKT, SDPOH
        (261000001, 1, 12345678, 526013012, d, 100.0, 1, 2026, 26100001, ts, 10),
        (261000001, 2, 12345678, 526013012, d + datetime.timedelta(days=1), 56.7, 1, 2026,
         26100001, ts, 10),
        (261000002, 1, 12345678, 1e30, d, 10.0, None, None, None, ts, 10),
        (261000003, 1, 31697143, 77, d, 999.0, None, None, None, ts, 10),
        (261000004, 1, 12345678, 5, today - datetime.timedelta(days=90), 1.0, None, None,
         None, ts, 10),
        (261000005, 1, 12345678, 6, d, 1.0, None, None, None, ts, 50),
    ]
    con.executemany("INSERT INTO raw.sp001 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    con.executemany("INSERT INTO raw.firma VALUES (?, ?, ?)", [
        (12345678, "2000000000991", "Dodávateľ"), (12345678, "2000000000777", "Dodávateľ"),
        (12345678, None, "Dodávateľ")])
    con.executemany("INSERT INTO raw.faktury VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
        (12345678, 1, 2026, 26100001, "526013012", "526013012", None, 150.0, 6.7, None),
        (12345678, 1, 2026, 26100001, "526013012", "526013012", None, 150.0, 6.7, None)])
    con.execute("INSERT INTO meta.etl_runs VALUES ('sp001', 'ok', TIMESTAMP '2026-10-04 "
                "16:04:12', TIMESTAMP '2026-10-04 16:19:49')")
    con.close()


def test_the_sql_groups_one_receipt_per_ncd_without_fanout(tmp_path):
    path = tmp_path / "codex.duckdb"
    _codex_db(path)
    out = push.build_receipts(push.query_duckdb(str(path), 60))
    by_number = {r["receipt_number"]: r for r in out}
    assert set(by_number) == {"261000001", "261000002"}, \
        "own-IČO transfers, out-of-window receipts and non-purchase moves are excluded"
    r = by_number["261000001"]
    assert r["total"] == 156.7, "lines summed once each — no fan-out from duplicate rows"
    assert r["line_count"] == 2
    assert r["receipt_date_to"] > r["receipt_date"]
    assert r["supplier_eans"] == ["2000000000777", "2000000000991"]
    assert r["dl_numbers"] == ["526013012"]
    assert (r["invoice_number"], r["invoice_total"]) == ("526013012", 156.7)
    garbage = by_number["261000002"]
    assert garbage["dl_numbers"] == [] and garbage["invoice_number"] == ""
    # the load's START — a receipt typed during the ~15-min load may be missing from it
    assert push.query_as_of(str(path)) == datetime.datetime(2026, 10, 4, 16, 4, 12)
