"""#467: the dev-box CODEX stock-card push tool — CI-testable via the DI seam, no duckdb/requests.

Synthetic rows only (made-up codes/names) — this repo is public.
"""
import datetime

from tools import codex_cards_push as push


def test_module_imports_without_duckdb_or_requests():
    assert hasattr(push, "build_cards") and hasattr(push, "run")


def test_build_cards_turns_the_neankod_double_into_our_gtin_text():
    rows = [{"code": 9990000000017.0, "card_code": " 27 ", "stredisko": 1, "sklad": 1,
             "name": " Rožok so slaninou a syrom 70g ", "inactive": None,
             "changed_at": datetime.datetime(2026, 9, 28, 9, 0, 48)}]
    out = push.build_cards(rows)
    assert out == [{"code": "9990000000017", "card_code": "27", "stredisko": 1, "sklad": 1,
                    "name": "Rožok so slaninou a syrom 70g", "inactive": False,
                    "changed_at": "2026-09-28T09:00:48+02:00"}]


def test_build_cards_skips_rows_without_a_usable_code():
    rows = [{"code": None, "name": "a"}, {"code": 0, "name": "b"}, {"code": 12.5, "name": "c"},
            {"code": "3698", "name": "d", "stredisko": None, "sklad": None}]
    out = push.build_cards(rows)
    assert [c["code"] for c in out] == ["3698"]
    assert out[0]["stredisko"] == 0 and out[0]["sklad"] == 0 and out[0]["changed_at"] is None


def test_cards_url_is_derived_from_the_orders_push_url():
    assert (push.cards_url("http://addon:8099/api/codex/orders")
            == "http://addon:8099/api/codex/cards")
    assert push.cards_url("http://addon:8099/api/codex/cards") == \
        "http://addon:8099/api/codex/cards"


def test_run_posts_the_whole_list_once_with_the_source_time():
    posted = []

    def fake_query():
        return [{"code": 9990000000017.0, "name": "A", "stredisko": 1, "sklad": 1},
                {"code": 9990000000031.0, "name": "B", "stredisko": 1, "sklad": 100},
                {"code": None, "name": "dropped"}]

    def fake_as_of():
        return datetime.datetime(2026, 9, 29, 12, 23, 34)   # meta.etl_runs is naive UTC

    def fake_poster(url, headers, body):
        posted.append((url, headers, body))
        return {"rows": len(body["cards"]), "codes": len(body["cards"])}

    res = push.run("http://addon/api/codex/cards", "tok", query=fake_query,
                   as_of=fake_as_of, poster=fake_poster)
    assert res == {"fetched": 3, "cards": 2, "rows": 2, "codes": 2}
    assert len(posted) == 1, "one POST — the add-on replaces the list atomically"
    url, headers, body = posted[0]
    assert url == "http://addon/api/codex/cards" and headers["X-Token"] == "tok"
    assert body["source_as_of"] == "2026-09-29T12:23:34+00:00"
    assert [c["code"] for c in body["cards"]] == ["9990000000017", "9990000000031"]


def test_run_refuses_to_post_an_empty_list():
    posted = []
    res = push.run("u", "t", query=lambda: [], as_of=lambda: None,
                   poster=lambda *a: posted.append(a) or {})
    assert posted == [] and res["cards"] == 0 and res["error"]


def test_main_without_url_or_token_is_a_usage_error(monkeypatch):
    monkeypatch.delenv("CODEX_PUSH_URL", raising=False)
    monkeypatch.delenv("CODEX_CARDS_PUSH_URL", raising=False)
    monkeypatch.delenv("CODEX_PUSH_TOKEN", raising=False)
    assert push.main([]) == 2
