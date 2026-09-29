"""#342: the dev-box push tool — CI-testable via the DI seam, no duckdb/requests needed."""
import datetime

from tools import codex_orders_push as push

EAN = "2000000000001"


def test_module_imports_without_duckdb_or_requests():
    # duckdb/requests are lazy-imported inside query_duckdb/_requests_post only — importing
    # the module and using its pure functions must never require them.
    assert hasattr(push, "build_orders") and hasattr(push, "run")


def test_build_orders_normalizes_types_and_dates():
    rows = [{
        "order_number": 260051617, "customer_nico": 12345, "customer_ean": EAN,
        "customer_name": "Zákazník A",
        "issue_date": datetime.date(2026, 8, 15),
        "delivery_date": datetime.date(2026, 8, 16), "line_count": 5}]
    out = push.build_orders(rows)
    assert out == [{
        "order_number": 260051617, "customer_nico": 12345, "customer_ean": EAN,
        "customer_name": "Zákazník A", "issue_date": "2026-08-15",
        "delivery_date": "2026-08-16", "line_count": 5}]


def test_build_orders_skips_rows_missing_identity():
    rows = [
        {"order_number": None, "customer_ean": EAN},
        {"order_number": 1, "customer_ean": "  "},
        {"order_number": 2, "customer_ean": EAN, "issue_date": None,
         "delivery_date": None, "line_count": None},
    ]
    out = push.build_orders(rows)
    assert [o["order_number"] for o in out] == [2]
    assert out[0]["issue_date"] is None and out[0]["line_count"] is None


def test_build_orders_truncates_a_long_name():
    out = push.build_orders(
        [{"order_number": 3, "customer_ean": EAN, "customer_name": "X" * 500}])
    assert len(out[0]["customer_name"]) == 200


def test_run_wires_query_to_poster_and_counts_upserts():
    posted = []

    def fake_query():
        return [
            {"order_number": 10, "customer_ean": EAN, "issue_date": datetime.date(2026, 8, 1)},
            {"order_number": 11, "customer_ean": EAN, "issue_date": datetime.date(2026, 8, 2)},
            {"order_number": None, "customer_ean": EAN},   # dropped by build_orders
        ]

    def fake_poster(url, headers, body):
        posted.append((url, headers, body))
        return {"upserted": len(body["orders"])}

    res = push.run("http://addon/api/codex/orders", "tok",
                   query=fake_query, poster=fake_poster)
    assert res == {"fetched": 3, "orders": 2, "upserted": 2}
    assert len(posted) == 1
    url, headers, body = posted[0]
    assert url == "http://addon/api/codex/orders"
    assert headers["X-Token"] == "tok"
    assert [o["order_number"] for o in body["orders"]] == [10, 11]


def test_post_orders_chunks_large_batches():
    calls = []

    def fake_poster(url, headers, body):
        calls.append(len(body["orders"]))
        return {"upserted": len(body["orders"])}

    orders = [{"order_number": i, "customer_ean": EAN} for i in range(1200)]
    total = push.post_orders("u", "t", orders, poster=fake_poster, chunk=500)
    assert calls == [500, 500, 200]
    assert total == 1200


def test_main_requires_url_and_token(capsys):
    assert push.main(["--url", "", "--token", ""]) == 2
    assert "required" in capsys.readouterr().err


def test_the_pushed_line_names_the_target_host_but_never_the_path_or_credentials():
    """#470: the journal line proves WHICH address the push went to (the Cloudflare tunnel
    https://email-pz.newlevel.media vs the raw port) — scheme + host only, never a token."""
    line = push.pushed_line({"fetched": 3, "orders": 2, "upserted": 2},
                            "https://email-pz.newlevel.media/api/codex/orders")
    assert line == ("pushed: fetched=3 orders=2 upserted=2 "
                    "to=https://email-pz.newlevel.media")
    leaky = push.pushed_line({"fetched": 0, "orders": 0, "upserted": 0},
                             "https://user:pw@host.example:8443/api/codex/orders?token=x")
    assert leaky.endswith("to=https://host.example:8443")
    assert "pw" not in leaky and "token" not in leaky


def test_the_pushed_line_keeps_an_ipv6_host_and_never_raises_on_a_bad_port():
    """Review round 2: `urlsplit().hostname` drops IPv6 brackets and `.port` raises on a bad
    port — after a successful POST the tool must still print a sane line, never a traceback."""
    res = {"fetched": 0, "orders": 0, "upserted": 0}
    assert push.pushed_line(res, "http://[::1]:8099/api/codex/orders").endswith(
        "to=http://[::1]:8099")
    assert push.pushed_line(res, "http://host.example:99999/api/codex/orders").endswith(
        "to=http://host.example:99999")


def test_main_prints_the_pushed_line_with_the_target(monkeypatch, capsys):
    """The journal line #470 relies on is what main() actually prints on success. Only the two
    external boundaries are replaced: the codex-bridge DuckDB read and the HTTP POST."""
    rows = [{"order_number": 260051617, "customer_nico": 12345, "customer_ean": EAN,
             "customer_name": "Zákazník A", "issue_date": datetime.date(2026, 9, 1),
             "delivery_date": datetime.date(2026, 9, 2), "line_count": 3}]
    posted = []
    monkeypatch.setattr(push, "query_duckdb", lambda db_path, days: rows)
    monkeypatch.setattr(push, "_requests_post",
                        lambda url, headers, body: posted.append(url)
                        or {"upserted": len(body["orders"])})
    url = "https://email-pz.newlevel.media/api/codex/orders"
    assert push.main(["--url", url, "--token", "tok-not-for-the-log"]) == 0
    out = capsys.readouterr().out.strip()
    assert out == "pushed: fetched=1 orders=1 upserted=1 to=https://email-pz.newlevel.media"
    assert "tok-not-for-the-log" not in out
    assert posted == [url]
