"""What each run costs (#89).

The user accepted the measured ~€17/month and set a **€30/month tripwire** — not a hard
stop, a signal that something is wrong (a prompt gone verbose, a runaway retry, a mail with
200 items). That cap could not exist, because `llm._http_transport` parsed the answer and
threw the API's own `usage` block away: spend was invisible.

Three things are pinned here: the tally is captured, it is priced by the published table,
and a cache hit costs nothing.
"""
import json

import pytest

from app.orders import llm, spend

# gpt-5.4, per 1M tokens (developers.openai.com/api/docs/pricing, fetched 2026-07-31)
IN, CACHED, OUT = 2.50, 0.25, 15.00


def _usage(inp=1000, cached=0, out=500, reasoning=400):
    return {"input_tokens": inp, "output_tokens": out,
            "input_tokens_details": {"cached_tokens": cached},
            "output_tokens_details": {"reasoning_tokens": reasoning}}


def _client(tmp_path, answers, usages):
    calls = {"n": 0}

    def transport(payload, api_key, timeout):
        i = calls["n"]
        calls["n"] += 1
        return answers[i], usages[i]

    return llm.Client(api_key="k", cache_dir=str(tmp_path), transport=transport)


# --- pricing -------------------------------------------------------------

def test_a_call_is_priced_by_the_published_table():
    got = llm.cost_usd(_usage(inp=1_000_000, cached=0, out=1_000_000))
    assert got == pytest.approx(IN + OUT)


def test_cached_input_is_a_tenth_of_the_price():
    """The whole point of ordering the payload so the static part is a stable prefix."""
    fresh = llm.cost_usd(_usage(inp=1_000_000, cached=0, out=0))
    cached = llm.cost_usd(_usage(inp=1_000_000, cached=1_000_000, out=0))
    assert fresh == pytest.approx(IN)
    assert cached == pytest.approx(CACHED)


def test_an_unpriced_model_is_reported_not_guessed():
    with pytest.raises(llm.LlmError):
        llm.cost_usd(_usage(), model="gpt-9-imaginary")


# --- the tally on the client ---------------------------------------------

def test_the_client_tallies_tokens_and_cost_across_calls(tmp_path):
    c = _client(tmp_path, [{"a": 1}, {"a": 2}], [_usage(), _usage(inp=2000, out=100)])
    c.json_call("s1", "u1", {"type": "object"})
    c.json_call("s2", "u2", {"type": "object"})
    s = c.spend()
    assert s["calls"] == 2
    assert s["tokens_in"] == 3000 and s["tokens_out"] == 600
    assert s["cost_usd"] == pytest.approx(llm.cost_usd(_usage())
                                          + llm.cost_usd(_usage(inp=2000, out=100)))


def test_a_cache_hit_costs_nothing_and_is_counted_as_such(tmp_path):
    c = _client(tmp_path, [{"a": 1}], [_usage()])
    first = c.json_call("s", "u", {"type": "object"})
    paid = c.spend()["cost_usd"]
    again = c.json_call("s", "u", {"type": "object"})     # same key -> from disk
    assert first == again
    s = c.spend()
    assert s["cost_usd"] == pytest.approx(paid), "a replayed answer must cost nothing"
    assert s["cached_calls"] == 1


def test_a_transport_that_reports_no_usage_does_not_break_the_run(tmp_path):
    """An old-style transport (and every test double) returns just the parsed answer."""
    c = llm.Client(api_key="k", transport=lambda p, k, t: {"a": 1})
    assert c.json_call("s", "u", {"type": "object"}) == {"a": 1}
    assert c.spend()["calls"] == 1 and c.spend()["cost_usd"] == 0


# --- persistence + the month's bill --------------------------------------

def _run(pg, cost, shadow=False, rules=()):
    rid = int(pg.execute(
        "INSERT INTO order_runs (message_id, shadow, status) VALUES ('m', %s, 'ok')"
        " RETURNING id", (shadow,)).fetchone()[0])
    spend.record(pg, rid, {"calls": 3, "cached_calls": 1, "tokens_in": 100,
                           "tokens_cached": 10, "tokens_out": 50, "cost_usd": cost})
    for rule in rules:
        pg.execute("INSERT INTO order_items (run_id, name, rule) VALUES (%s, 'x', %s)",
                   (rid, rule))
    return rid


def test_the_run_keeps_what_it_cost(pg):
    rid = _run(pg, 0.1234)
    row = pg.execute("SELECT calls, tokens_in, tokens_out, cost_usd FROM order_runs"
                     " WHERE id = %s", (rid,)).fetchone()
    assert row[0] == 3 and row[1] == 100 and row[2] == 50
    assert float(row[3]) == pytest.approx(0.1234)


def test_the_month_to_date_bill_is_in_both_currencies(pg):
    _run(pg, 1.00)
    _run(pg, 2.00)
    mtd = spend.month_to_date(pg)
    assert mtd["cost_usd"] == pytest.approx(3.00)
    assert mtd["cost_eur"] == pytest.approx(3.00 / spend.USD_PER_EUR)
    assert mtd["runs"] == 2


def test_a_shadow_run_still_costs_real_money_and_is_counted(pg):
    """Shadow means nothing is shipped — the model was still called and billed."""
    _run(pg, 0.5, shadow=True)
    assert spend.month_to_date(pg)["cost_usd"] == pytest.approx(0.5)


def test_the_deterministic_share_is_reported_because_it_should_rise(pg):
    _run(pg, 0.1, rules=("catalog_name", "alias_exact", "history_sure", "llm_sure"))
    share = spend.deterministic_share(pg)
    assert share["free"] == 3 and share["total"] == 4
    assert share["pct"] == pytest.approx(75.0)


def test_the_cap_trips_once_per_month_not_once_per_run(pg):
    _run(pg, 40.0)          # far past a €30 cap
    assert spend.cap_tripped(pg, cap_eur=30) is True
    assert spend.cap_tripped(pg, cap_eur=30) is False, "one message per month, not per run"


def test_under_the_cap_nothing_is_sent(pg):
    _run(pg, 1.0)
    assert spend.cap_tripped(pg, cap_eur=30) is False


def test_the_trip_message_names_the_month_and_the_worst_runs(pg):
    for cost in (5.0, 30.0, 1.0):
        _run(pg, cost)
    text = spend.trip_message(pg, cap_eur=30)
    assert "30" in text and "€" in text
    assert text.count("\n") >= 2, "the three most expensive runs, one per line"
    assert "36" in text or "33" in text, "the month's total, in euro"
