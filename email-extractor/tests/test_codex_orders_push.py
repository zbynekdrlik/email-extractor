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
