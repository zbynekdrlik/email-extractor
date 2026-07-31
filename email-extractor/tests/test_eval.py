"""The evaluation harness (#66).

The point of the harness is the sentence the user actually said: "you improve one order
type and break five others". So what is tested here is not the model's accuracy — it is
that the harness **notices a regression**:

- scoring is per case AND per order type, so a gain in one type cannot hide a loss in
  another;
- a case that once passed and now fails is a hard failure (locked baseline), not a lower
  average;
- offline mode refuses a cache miss instead of silently calling the model.
"""
import json

import pytest

from app.orders import evaluate

EXPECTED = {
    "customer_ean": "2000000000001",
    "delivery_date": "04.08.2026",
    "items": [{"gtin": "G50", "quantity": 120}, {"gtin": "VIA", "quantity": 7}],
}


def _actual(**kw):
    base = {"customer_ean": "2000000000001", "delivery_date": "04.08.2026",
            "items": [{"gtin": "G50", "quantity": 120}, {"gtin": "VIA", "quantity": 7}]}
    base.update(kw)
    return base


# --- scoring one case ----------------------------------------------------

def test_an_exact_match_scores_one():
    score = evaluate.score(EXPECTED, _actual())
    assert score.passed is True and score.item_recall == 1.0
    assert score.problems == []


def test_item_order_and_number_formatting_are_not_differences():
    score = evaluate.score(EXPECTED, _actual(items=[{"gtin": "VIA", "quantity": 7.0},
                                                    {"gtin": "G50", "quantity": 120}]))
    assert score.passed is True


def test_a_missing_item_fails_the_case_and_says_which():
    score = evaluate.score(EXPECTED, _actual(items=[{"gtin": "G50", "quantity": 120}]))
    assert score.passed is False
    assert score.item_recall == 0.5
    assert any("VIA" in p for p in score.problems)


def test_an_invented_item_fails_the_case():
    score = evaluate.score(EXPECTED, _actual(
        items=EXPECTED["items"] + [{"gtin": "G70", "quantity": 3}]))
    assert score.passed is False
    assert any("G70" in p for p in score.problems)


def test_a_wrong_quantity_fails_the_case():
    score = evaluate.score(EXPECTED, _actual(items=[{"gtin": "G50", "quantity": 12},
                                                    {"gtin": "VIA", "quantity": 7}]))
    assert score.passed is False
    assert any("120" in p and "12" in p for p in score.problems)


def test_the_wrong_customer_fails_even_when_every_item_matches():
    score = evaluate.score(EXPECTED, _actual(customer_ean="9999999999999"))
    assert score.passed is False
    assert any("zákazník" in p.lower() for p in score.problems)


def test_a_wrong_delivery_date_fails():
    score = evaluate.score(EXPECTED, _actual(delivery_date="05.08.2026"))
    assert score.passed is False


def test_an_order_that_should_have_gone_to_review_and_shipped_instead_fails():
    """The opposite mistake matters just as much: shipping something the warehouse was
    supposed to check."""
    expected = dict(EXPECTED, should_review=True)
    score = evaluate.score(expected, _actual(shipped=True))
    assert score.passed is False
    assert any("kontrol" in p.lower() for p in score.problems)


# --- aggregation ---------------------------------------------------------

def test_results_are_aggregated_per_type_so_one_type_cannot_hide_another():
    results = [
        evaluate.Result(case_id="a", type="free_text", score=evaluate.score(EXPECTED, _actual())),
        evaluate.Result(case_id="b", type="free_text", score=evaluate.score(EXPECTED, _actual())),
        evaluate.Result(case_id="c", type="price_list",
                        score=evaluate.score(EXPECTED, _actual(items=[]))),
    ]
    summary = evaluate.summarize(results)
    assert summary["total"] == {"passed": 2, "cases": 3}
    assert summary["by_type"]["free_text"] == {"passed": 2, "cases": 2}
    assert summary["by_type"]["price_list"] == {"passed": 0, "cases": 1}


# --- the locked baseline (the whole point) -------------------------------

def test_a_case_that_used_to_pass_and_now_fails_is_a_hard_failure():
    baseline = {"cases": {"a": {"passed": True}, "b": {"passed": False}}}
    results = [
        evaluate.Result(case_id="a", type="free_text",
                        score=evaluate.score(EXPECTED, _actual(items=[]))),
        evaluate.Result(case_id="b", type="free_text",
                        score=evaluate.score(EXPECTED, _actual(items=[]))),
    ]
    regressions = evaluate.regressions(results, baseline)
    assert [r.case_id for r in regressions] == ["a"], \
        "only the case that USED to pass counts as a regression"


def test_a_newly_passing_case_is_not_a_regression_and_updates_the_baseline():
    baseline = {"cases": {"a": {"passed": False}}}
    results = [evaluate.Result(case_id="a", type="free_text",
                               score=evaluate.score(EXPECTED, _actual()))]
    assert evaluate.regressions(results, baseline) == []
    updated = evaluate.new_baseline(results)
    assert updated["cases"]["a"]["passed"] is True


def test_a_case_missing_from_the_baseline_is_recorded_not_ignored():
    updated = evaluate.new_baseline([
        evaluate.Result(case_id="new", type="attachment",
                        score=evaluate.score(EXPECTED, _actual()))])
    assert "new" in updated["cases"] and updated["cases"]["new"]["type"] == "attachment"


# --- running a case ------------------------------------------------------

def test_running_a_case_never_uploads_or_posts(pg, monkeypatch):
    """The harness runs the real pipeline; it must be as inert as shadow mode or an
    evaluation would ship orders."""
    from app.config import Config
    from app.orders import snapshot
    sid = snapshot.import_snapshot(
        pg, "GTIN,Sklad,Názov,doplnok\nG50,1,Rožok štandart 50g,\n",
        "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
        "Pekáreň Testovacia s.r.o.,2000000000001,Martin,Košútka 1,,,sklad@pekaren.sk\n")
    calls = []
    monkeypatch.setattr(evaluate.upload_mod, "put",
                        lambda *a, **k: calls.append("upload"))
    monkeypatch.setattr(evaluate.report, "post_from_config",
                        lambda *a, **k: calls.append("post"))

    class Client:
        last_prompt_hash = "p"

        def json_call(self, system, user, schema, name="result"):
            if name == "orders":
                return {"senderEmail": "sklad@pekaren.sk", "orders": [
                    {"deliveryDate": "04.08.2026", "items": [
                        {"name": "rožok", "quantity": 10, "unit": "ks",
                         "sourceQuote": "10x rožok"}]}]}
            if name == "customer":
                return {"ean_edi": "2000000000001", "confidence": 0.95}
            return {"gtin": "G50", "confidence": 0.95}

    case = {"id": "c1", "type": "free_text",
            "email": {"message_id": "x", "combined_text": "10x rožok", "today": "2026-07-30"},
            "expected": {"customer_ean": "2000000000001", "delivery_date": "04.08.2026",
                         "items": [{"gtin": "G50", "quantity": 10}]}}
    result = evaluate.run_case(pg, Config(pg_dsn="", data_dir="/tmp"), case, sid,
                               client=Client())
    assert calls == [], "an evaluation must never touch ORION or Odoo"
    assert result.score.passed is True
    assert pg.execute("SELECT count(*) FROM item_memory").fetchone()[0] == 0


def test_offline_mode_refuses_a_cache_miss(tmp_path):
    from app.orders import llm
    client = llm.Client(api_key="", cache_dir=str(tmp_path), offline=True)
    with pytest.raises(llm.CacheMiss):
        client.json_call("system", "input", {"type": "object"})


def test_the_manifest_shipped_with_the_repo_is_loadable_and_typed():
    """The committed manifest is the synthetic one used by CI; the real 30-email corpus
    lives on the add-on volume (no customer email in git)."""
    cases = evaluate.load_manifest(evaluate.SYNTHETIC_MANIFEST)
    assert cases, "the synthetic manifest must not be empty"
    for case in cases:
        assert case["id"] and case["type"]
        assert case["expected"]["items"] is not None
        assert json.dumps(case)      # serializable, no surprises


# --- one email, several orders (#78) -------------------------------------

MULTI = {
    "customer_ean": "2000000000001",
    "orders": [
        {"delivery_date": "04.08.2026", "items": [{"gtin": "G50", "quantity": 120}]},
        {"delivery_date": "05.08.2026", "items": [{"gtin": "VIA", "quantity": 7}]},
    ],
}


def _multi_actual(orders=None):
    return {"customer_ean": "2000000000001", "shipped": True,
            "order_results": orders if orders is not None else [
                {"delivery_date": "04.08.2026", "status": "ok",
                 "items": [{"gtin": "G50", "quantity": 120}]},
                {"delivery_date": "05.08.2026", "status": "ok",
                 "items": [{"gtin": "VIA", "quantity": 7}]}]}


def test_a_multi_order_email_is_scored_per_delivery_date():
    assert evaluate.score(MULTI, _multi_actual()).passed is True


def test_order_sequence_is_not_a_difference():
    """The model may emit the dates in either order; only their content matters."""
    reversed_orders = list(reversed(_multi_actual()["order_results"]))
    assert evaluate.score(MULTI, _multi_actual(reversed_orders)).passed is True


def test_a_dropped_second_order_fails_and_names_the_date():
    """The failure the flattened result used to hide completely."""
    only_first = _multi_actual()["order_results"][:1]
    s = evaluate.score(MULTI, _multi_actual(only_first))
    assert s.passed is False
    assert any("05.08.2026" in p for p in s.problems)


def test_a_wrong_item_inside_the_second_order_fails():
    orders = _multi_actual()["order_results"]
    orders[1] = dict(orders[1], items=[{"gtin": "G70", "quantity": 7}])
    s = evaluate.score(MULTI, _multi_actual(orders))
    assert s.passed is False
    assert any("05.08.2026" in p and ("G70" in p or "VIA" in p) for p in s.problems)


def test_an_invented_extra_delivery_date_fails():
    orders = _multi_actual()["order_results"] + [
        {"delivery_date": "06.08.2026", "status": "ok",
         "items": [{"gtin": "G50", "quantity": 1}]}]
    s = evaluate.score(MULTI, _multi_actual(orders))
    assert s.passed is False
    assert any("06.08.2026" in p for p in s.problems)


def test_the_single_order_shape_still_works():
    """The old flat shape stays valid — most emails are one order."""
    assert evaluate.score(EXPECTED, _actual()).passed is True
