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


# --- asserting only what the corpus author actually knows ------------------

def test_an_order_without_an_items_key_asserts_only_its_delivery_date():
    """Some real cases are only knowable at the date level — a weekly order whose per-item
    ground truth could not be reconstructed. Asserting invented GTINs would lock a wrong
    answer into the baseline, so an order with NO `items` key asserts the date alone."""
    expected = {"customer_ean": "2000000000001",
                "orders": [{"delivery_date": "03.08.2026"},
                           {"delivery_date": "04.08.2026"}]}
    actual = {"customer_ean": "2000000000001", "shipped": True, "order_results": [
        {"delivery_date": "03.08.2026", "items": [{"gtin": "G50", "quantity": 35}]},
        {"delivery_date": "04.08.2026", "items": [{"gtin": "VIA", "quantity": 3}]}]}
    assert evaluate.score(expected, actual).passed is True


def test_a_date_only_order_still_fails_on_a_missing_or_invented_date():
    expected = {"customer_ean": "2000000000001",
                "orders": [{"delivery_date": "03.08.2026"},
                           {"delivery_date": "04.08.2026"}]}
    missing = {"customer_ean": "2000000000001", "shipped": True, "order_results": [
        {"delivery_date": "03.08.2026", "items": []}]}
    s = evaluate.score(expected, missing)
    assert s.passed is False and any("04.08.2026" in p for p in s.problems)

    invented = {"customer_ean": "2000000000001", "shipped": True, "order_results": [
        {"delivery_date": "03.08.2026", "items": []},
        {"delivery_date": "04.08.2026", "items": []},
        {"delivery_date": "08.08.2026", "items": []}]}
    s = evaluate.score(expected, invented)
    assert s.passed is False and any("08.08.2026" in p for p in s.problems)


def test_an_explicitly_empty_item_list_is_still_asserted():
    """`items: []` means "this order must be empty" — absence of the key is what relaxes."""
    expected = {"customer_ean": "X", "orders": [{"delivery_date": "03.08.2026", "items": []}]}
    actual = {"customer_ean": "X", "shipped": True, "order_results": [
        {"delivery_date": "03.08.2026", "items": [{"gtin": "G50", "quantity": 1}]}]}
    assert evaluate.score(expected, actual).passed is False


def test_an_email_that_must_not_produce_an_order_fails_if_it_produces_one():
    expected = {"customer_ean": "", "orders": [], "should_review": True}
    actual = {"customer_ean": "2000000000001", "shipped": True, "order_results": [
        {"delivery_date": "03.08.2026", "items": [{"gtin": "G50", "quantity": 1}]}]}
    s = evaluate.score(expected, actual)
    assert s.passed is False
    assert any("03.08.2026" in p for p in s.problems)
    assert any("kontrol" in p.lower() for p in s.problems)


# --- the gate command itself ----------------------------------------------

def test_the_gate_command_imports_the_frozen_snapshot_and_runs_offline(
        pg, tmp_path, monkeypatch):
    """`eval_run --catalog --customers --manifest` is the whole CI gate. It must work with
    no add-on options file, no live sheet and no network."""
    from app.orders import eval_run
    cat = tmp_path / "catalog.csv"
    cat.write_text("GTIN,Sklad,Názov,doplnok\nG50,1,Rožok štandart 50g,\n", encoding="utf-8")
    cust = tmp_path / "customers.csv"
    cust.write_text(
        "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
        "Pekáreň s.r.o.,2000000000864,Martin,Košútka 1,,,sklad@pekaren.sk\n",
        encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"cases": []}), encoding="utf-8")

    import os
    monkeypatch.setenv("PG_DSN", os.environ["PG_TEST_DSN"])
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path / "cache"))
    code = eval_run.main(["--manifest", str(manifest), "--catalog", str(cat),
                          "--customers", str(cust)])
    assert code == 0


def test_require_all_fails_on_any_failing_case_not_only_a_regression(pg, tmp_path,
                                                                    monkeypatch):
    """The user's rule: the corpus must pass on every change, or the change is not applied.
    Gating on regressions alone would let a case that NEVER passed stay broken forever."""
    from app.orders import eval_run
    cases = {"cases": [{"id": "c1", "type": "t", "email": {"combined_text": "x"},
                        "expected": {"customer_ean": "X", "items": []}}]}
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps(cases), encoding="utf-8")
    monkeypatch.setattr(evaluate, "run_corpus", lambda *a, **k: (
        [evaluate.Result("c1", "t", evaluate.Score(False, 0.0, ["nesedí zákazník"]))],
        {"total": {"passed": 0, "cases": 1}, "by_type": {}}))
    import os
    monkeypatch.setenv("PG_DSN", os.environ["PG_TEST_DSN"])
    assert eval_run.main(["--manifest", str(manifest)]) == 0, "no baseline, no regression"
    assert eval_run.main(["--manifest", str(manifest), "--require-all"]) == 1


def test_require_all_refuses_to_pass_on_an_empty_corpus(pg, tmp_path, monkeypatch):
    """A missing/emptied corpus must fail the gate, never pass it silently."""
    from app.orders import eval_run
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"cases": []}), encoding="utf-8")
    import os
    monkeypatch.setenv("PG_DSN", os.environ["PG_TEST_DSN"])
    monkeypatch.setattr(evaluate, "run_corpus", lambda *a, **k: (
        [], {"total": {"passed": 0, "cases": 0}, "by_type": {}}))
    assert eval_run.main(["--manifest", str(manifest), "--require-all"]) == 1


def test_an_order_stopped_at_review_does_not_count_as_a_produced_order():
    """`orders: []` in a case means "nothing may be SENT". The pipeline still extracts and
    reports an order it refuses to ship, so scoring the raw per-order list would fail every
    must-review case for producing exactly what it was supposed to produce: nothing sent."""
    run = {"status": "review", "customer_ean": "2000000000001", "items": [],
           "order_results": [{"delivery_date": "04.08.2026", "status": "review",
                              "items": [{"gtin": "G50", "quantity": 10}]}]}
    actual = evaluate._actual_from_run(run)
    assert actual["order_results"] == []
    assert actual["shipped"] is False
    expected = {"customer_ean": "2000000000001", "orders": [], "should_review": True}
    assert evaluate.score(expected, actual).passed is True


def test_a_partially_shipped_order_still_counts():
    run = {"status": "partial", "customer_ean": "X", "items": [],
           "order_results": [{"delivery_date": "04.08.2026", "status": "partial",
                              "items": [{"gtin": "G50", "quantity": 10}]},
                             {"delivery_date": "05.08.2026", "status": "review",
                              "items": [{"gtin": "VIA", "quantity": 1}]}]}
    actual = evaluate._actual_from_run(run)
    assert [o["delivery_date"] for o in actual["order_results"]] == ["04.08.2026"]


def test_the_corpus_run_logs_progress_per_case(pg, tmp_path, caplog):
    """A 30-case live run takes tens of minutes. With logging only at the END, a run that
    died silently 40 minutes in was indistinguishable from a run still working — that
    happened. Every case must announce itself."""
    from app.config import Config
    from app.orders import snapshot
    snapshot.import_snapshot(
        pg, "GTIN,Sklad,Názov,doplnok\nG50,1,Rožok štandart 50g,\n",
        "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
        "Pekáreň s.r.o.,2000000000864,Martin,Košútka 1,,,sklad@pekaren.sk\n")
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"cases": [
        {"id": "a", "type": "t", "email": {"message_id": "x", "combined_text": "10x rožok"},
         "expected": {"customer_ean": "", "items": []}},
        {"id": "b", "type": "t", "email": {"message_id": "y", "combined_text": "5x rožok"},
         "expected": {"customer_ean": "", "items": []}}]}), encoding="utf-8")
    cfg = Config(pg_dsn="", data_dir="/tmp", llm_cache_dir=str(tmp_path / "cache"))
    with caplog.at_level("INFO"):
        evaluate.run_corpus(pg, cfg, str(manifest), offline=True)
    progress = [r.message for r in caplog.records if "1/2" in r.message or "2/2" in r.message]
    assert len(progress) == 2, f"expected a line per case, got {progress}"
    assert "a" in progress[0] and "b" in progress[1]


# --- comparing dates and customers honestly -------------------------------

def test_the_same_day_written_two_ways_is_the_same_day():
    """The archive stores delivery dates both as 2026-07-01 and as 01.07.2026, depending on
    which n8n node wrote them. Treating those as different dates reported a missing order
    and an extra order for one and the same delivery."""
    expected = {"orders": [{"delivery_date": "2026-07-01",
                            "items": [{"gtin": "G50", "quantity": 10}]}]}
    actual = {"shipped": True, "order_results": [
        {"delivery_date": "01.07.2026", "status": "ok",
         "items": [{"gtin": "G50", "quantity": 10}]}]}
    assert evaluate.score(expected, actual).passed is True
    assert evaluate.score({"delivery_date": "1.7.2026", "items": []},
                          {"delivery_date": "01.07.2026", "items": []}).passed is True


def test_a_different_day_is_still_a_difference():
    expected = {"orders": [{"delivery_date": "2026-07-01", "items": []}]}
    actual = {"shipped": True, "order_results": [
        {"delivery_date": "02.07.2026", "status": "ok", "items": []}]}
    assert evaluate.score(expected, actual).passed is False


def test_the_customer_is_compared_only_when_the_case_states_one():
    """A must-review case cares that nothing SHIPS, not who the sender turned out to be —
    identifying the customer correctly and then refusing to ship is right, not a failure."""
    expected = {"orders": [], "should_review": True}
    actual = {"customer_ean": "2000000000354", "shipped": False, "order_results": []}
    assert evaluate.score(expected, actual).passed is True
    # but a case that DOES name a customer still enforces it
    assert evaluate.score(dict(expected, customer_ean="2000000000001"), actual).passed is False


def test_an_order_can_assert_how_many_item_lines_it_must_have():
    """Which card each wording maps to is sometimes unprovable, but HOW MANY lines the email
    asks for always is. One real email listed 15 items and n8n's EDI carried 1 — a
    dates-only assertion would have called that a pass."""
    expected = {"orders": [{"delivery_date": "21.07.2026", "item_count": 3}]}
    ok = {"shipped": True, "order_results": [
        {"delivery_date": "21.07.2026", "status": "ok", "items": [
            {"gtin": "A", "quantity": 1}, {"gtin": "B", "quantity": 2},
            {"gtin": "C", "quantity": 3}]}]}
    assert evaluate.score(expected, ok).passed is True

    short = {"shipped": True, "order_results": [
        {"delivery_date": "21.07.2026", "status": "ok",
         "items": [{"gtin": "A", "quantity": 1}]}]}
    s = evaluate.score(expected, short)
    assert s.passed is False
    assert any("3" in p and "1" in p for p in s.problems)


def test_the_runner_can_dump_what_actually_happened(pg, tmp_path, monkeypatch):
    """Adjudicating a corpus means comparing expected against what the engine really
    produced, so the run has to be able to hand that over."""
    from app.orders import eval_run
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"cases": []}), encoding="utf-8")
    out = tmp_path / "actuals.json"
    monkeypatch.setattr(evaluate, "run_corpus", lambda *a, **k: (
        [evaluate.Result("c1", "t", evaluate.Score(True, 1.0, []))],
        {"total": {"passed": 1, "cases": 1}, "by_type": {}}))
    import os
    monkeypatch.setenv("PG_DSN", os.environ["PG_TEST_DSN"])
    eval_run.main(["--manifest", str(manifest), "--dump", str(out)])
    dumped = json.loads(out.read_text(encoding="utf-8"))
    assert dumped[0]["case_id"] == "c1" and dumped[0]["passed"] is True


def test_two_orders_for_the_same_delivery_date_is_a_failure():
    """Two EDI files for one delivery date are two orders in ORION for one day. Keying the
    result by date silently kept the last of them and reported a quantity mismatch instead of
    the real defect."""
    expected = {"orders": [{"delivery_date": "30.06.2026",
                            "items": [{"gtin": "A", "quantity": 50}]}]}
    actual = {"shipped": True, "order_results": [
        {"delivery_date": "30.06.2026", "status": "ok", "items": [{"gtin": "A", "quantity": 40}]},
        {"delivery_date": "30.06.2026", "status": "ok", "items": [{"gtin": "A", "quantity": 10}]}]}
    s = evaluate.score(expected, actual)
    assert s.passed is False
    assert any("dvakrát" in p or "dve objednávky" in p for p in s.problems), s.problems


def test_the_same_card_twice_in_one_order_is_counted_once_as_the_total():
    """One card must reach ORION as ONE line. If it appears twice the quantities add up —
    silently keeping the last one hid a lost quantity."""
    expected = {"orders": [{"delivery_date": "30.06.2026",
                            "items": [{"gtin": "A", "quantity": 50}]}]}
    actual = {"shipped": True, "order_results": [
        {"delivery_date": "30.06.2026", "status": "ok",
         "items": [{"gtin": "A", "quantity": 40}, {"gtin": "A", "quantity": 10}]}]}
    assert evaluate.score(expected, actual).passed is True


def test_a_case_marked_as_a_known_defect_is_reported_loudly_but_does_not_block(pg, tmp_path,
                                                                              monkeypatch):
    """Some corpus cases pin behaviour the engine does not have yet. Blocking every change on
    them would make the gate useless for the other 26; hiding them would be worse. So they
    are printed with their ticket and excluded from the hard gate — and NOTHING else is."""
    from app.orders import eval_run
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"cases": [
        {"id": "known", "type": "t", "known_defect": "#81.2",
         "email": {"combined_text": "x"}, "expected": {"items": []}},
        {"id": "other", "type": "t", "email": {"combined_text": "y"},
         "expected": {"items": []}}]}), encoding="utf-8")
    monkeypatch.setattr(evaluate, "run_corpus", lambda *a, **k: (
        [evaluate.Result("known", "t", evaluate.Score(False, 0.0, ["zlá karta"])),
         evaluate.Result("other", "t", evaluate.Score(True, 1.0, []))],
        {"total": {"passed": 1, "cases": 2}, "by_type": {}}))
    import os
    monkeypatch.setenv("PG_DSN", os.environ["PG_TEST_DSN"])
    assert eval_run.main(["--manifest", str(manifest), "--require-all"]) == 0

    # and a NON-marked failure still blocks
    monkeypatch.setattr(evaluate, "run_corpus", lambda *a, **k: (
        [evaluate.Result("known", "t", evaluate.Score(False, 0.0, ["zlá karta"])),
         evaluate.Result("other", "t", evaluate.Score(False, 0.0, ["zlý zákazník"]))],
        {"total": {"passed": 0, "cases": 2}, "by_type": {}}))
    assert eval_run.main(["--manifest", str(manifest), "--require-all"]) == 1


# --- cheap iteration: --sample and the price estimate (#87) ---------------

def test_select_sample_picks_one_case_per_type_first():
    """A prompt edit invalidates every cache key, so cheap iteration needs a SMALL subset
    that still catches "fix one type, break five" — that means type coverage, not just the
    first N cases in the file."""
    from app.orders import eval_run
    cases = [{"id": "a1", "type": "free_text"}, {"id": "a2", "type": "free_text"},
              {"id": "b1", "type": "price_list"}, {"id": "c1", "type": "attachment"}]
    sampled = eval_run.select_sample(cases, 2)
    assert [c["id"] for c in sampled] == ["a1", "b1"], "one per distinct type, in file order"


def test_select_sample_fills_remaining_slots_after_types_are_covered():
    from app.orders import eval_run
    cases = [{"id": "a1", "type": "free_text"}, {"id": "b1", "type": "price_list"},
              {"id": "a2", "type": "free_text"}, {"id": "b2", "type": "price_list"}]
    sampled = eval_run.select_sample(cases, 3)
    # a1, b1 cover both types; the 3rd slot is the next not-yet-picked case in file order
    assert [c["id"] for c in sampled] == ["a1", "b1", "a2"]


def test_select_sample_is_deterministic_across_repeated_calls():
    from app.orders import eval_run
    cases = [{"id": f"c{i}", "type": f"t{i % 3}"} for i in range(10)]
    first = [c["id"] for c in eval_run.select_sample(cases, 4)]
    second = [c["id"] for c in eval_run.select_sample(cases, 4)]
    assert first == second


def test_select_sample_n_at_or_above_total_returns_everything():
    from app.orders import eval_run
    cases = [{"id": "a", "type": "t"}, {"id": "b", "type": "t"}]
    assert eval_run.select_sample(cases, 5) == cases
    assert eval_run.select_sample(cases, 0) == cases


def test_sample_flag_restricts_which_cases_the_corpus_actually_runs(pg, tmp_path,
                                                                    monkeypatch):
    """--sample must actually narrow what evaluate.run_corpus is given, not just print a
    line — otherwise the flag would claim savings it doesn't deliver."""
    from app.orders import eval_run
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"cases": [
        {"id": "a", "type": "free_text", "email": {"combined_text": "x"},
         "expected": {"items": []}},
        {"id": "b", "type": "price_list", "email": {"combined_text": "y"},
         "expected": {"items": []}},
        {"id": "c", "type": "attachment", "email": {"combined_text": "z"},
         "expected": {"items": []}}]}), encoding="utf-8")
    seen = {}

    def fake_run_corpus(conn, cfg, manifest_path, offline=True, snapshot_id=None):
        seen["cases"] = evaluate.load_manifest(manifest_path)
        return [], {"total": {"passed": 0, "cases": 0}, "by_type": {}}

    monkeypatch.setattr(evaluate, "run_corpus", fake_run_corpus)
    import os
    monkeypatch.setenv("PG_DSN", os.environ["PG_TEST_DSN"])
    eval_run.main(["--manifest", str(manifest), "--sample", "2"])
    assert [c["id"] for c in seen["cases"]] == ["a", "b"]


def test_sample_tempfile_is_cleaned_up_after_the_run(pg, tmp_path, monkeypatch):
    from app.orders import eval_run
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"cases": [
        {"id": "a", "type": "t", "email": {"combined_text": "x"},
         "expected": {"items": []}}]}), encoding="utf-8")
    seen_path = {}

    def fake_run_corpus(conn, cfg, manifest_path, offline=True, snapshot_id=None):
        seen_path["path"] = manifest_path
        return [], {"total": {"passed": 0, "cases": 0}, "by_type": {}}

    monkeypatch.setattr(evaluate, "run_corpus", fake_run_corpus)
    import os
    monkeypatch.setenv("PG_DSN", os.environ["PG_TEST_DSN"])
    eval_run.main(["--manifest", str(manifest), "--sample", "1"])
    from pathlib import Path
    assert not Path(seen_path["path"]).exists()


def test_live_prints_an_estimated_price_before_the_run(pg, tmp_path, monkeypatch, capsys):
    """The cost must never be a surprise: printed BEFORE the run starts, not prompted for
    (these runs are non-interactive)."""
    from app.orders import eval_run
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"cases": [
        {"id": "a", "type": "t", "email": {"combined_text": "x"},
         "expected": {"items": []}}]}), encoding="utf-8")
    monkeypatch.setattr(evaluate, "run_corpus", lambda *a, **k: (
        [], {"total": {"passed": 0, "cases": 0}, "by_type": {}}))
    import os
    monkeypatch.setenv("PG_DSN", os.environ["PG_TEST_DSN"])
    eval_run.main(["--manifest", str(manifest), "--live"])
    out = capsys.readouterr().out
    assert "estimated cost" in out and "$" in out


def test_offline_run_prints_no_price_line(pg, tmp_path, monkeypatch, capsys):
    from app.orders import eval_run
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"cases": []}), encoding="utf-8")
    monkeypatch.setattr(evaluate, "run_corpus", lambda *a, **k: (
        [], {"total": {"passed": 0, "cases": 0}, "by_type": {}}))
    import os
    monkeypatch.setenv("PG_DSN", os.environ["PG_TEST_DSN"])
    eval_run.main(["--manifest", str(manifest)])
    out = capsys.readouterr().out
    assert "estimated cost" not in out


def test_estimated_cost_scales_with_case_count_and_model_price():
    from app.orders import eval_run
    base = eval_run._estimated_cost_usd(30, "gpt-5.4")
    assert base == pytest.approx(4.50, abs=0.01), "30 cases at the measured model must ~match"
    assert eval_run._estimated_cost_usd(5, "gpt-5.4") == pytest.approx(base / 6, abs=0.01)
    # gpt-5.4-mini is cheaper per the PRICES table, so its estimate must be lower
    mini = eval_run._estimated_cost_usd(30, "gpt-5.4-mini")
    assert mini < base


def test_estimated_cost_raises_on_an_unpriced_model_instead_of_guessing():
    """llm.cost_usd() already refuses to silently price an unlisted model at gpt-5.4 rates
    (llm.py's own €30/month-tripwire comment); the estimate must not contradict that policy
    by quietly falling back — a wrong-but-confident number defeats "cost is never a
    surprise"."""
    from app.orders import eval_run, llm
    with pytest.raises(llm.LlmError):
        eval_run._estimated_cost_usd(10, "some-future-model-not-in-prices")


def test_sample_write_failure_still_cleans_up_the_tempfile(pg, tmp_path, monkeypatch):
    """A write failure while building the --sample manifest must not leak the tempfile."""
    from app.orders import eval_run
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"cases": [
        {"id": "a", "type": "t", "email": {"combined_text": "x"},
         "expected": {"items": []}}]}), encoding="utf-8")
    created = {}
    real_mkstemp = eval_run.tempfile.mkstemp

    def spying_mkstemp(*a, **k):
        fd, name = real_mkstemp(*a, **k)
        created["path"] = name
        return fd, name

    monkeypatch.setattr(eval_run.tempfile, "mkstemp", spying_mkstemp)
    monkeypatch.setattr(eval_run.json, "dump",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    import os
    monkeypatch.setenv("PG_DSN", os.environ["PG_TEST_DSN"])
    with pytest.raises(OSError):
        eval_run.main(["--manifest", str(manifest), "--sample", "1"])
    from pathlib import Path
    assert not Path(created["path"]).exists(), "tempfile must be cleaned up even on write failure"


def test_the_gate_can_seed_the_delivery_history_from_the_corpus_bundle(pg, tmp_path,
                                                                      monkeypatch):
    """The corpus scores against the history as it stood, so the bundle carries it and the
    gate loads it — otherwise every history-driven case would be judged with no history."""
    from app.orders import eval_run, memory
    cat = tmp_path / "catalog.csv"
    cat.write_text("GTIN,Sklad,Názov,doplnok\nG50,1,Rožok štandart 50g,\n", encoding="utf-8")
    cust = tmp_path / "customers.csv"
    cust.write_text(
        "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
        "Pekáreň s.r.o.,2000000000864,Martin,Košútka 1,,,sklad@pekaren.sk\n",
        encoding="utf-8")
    hist = tmp_path / "history.json"
    hist.write_text(json.dumps([{"customer_ean": "2000000000864",
                                 "delivered_on": "2026-07-01",
                                 "items": [{"name": "rožok", "card": "Rožok štandart 50g",
                                            "gtin": "G50"}]}]), encoding="utf-8")
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"cases": []}), encoding="utf-8")
    import os
    monkeypatch.setenv("PG_DSN", os.environ["PG_TEST_DSN"])
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path / "cache"))
    assert eval_run.main(["--manifest", str(manifest), "--catalog", str(cat),
                          "--customers", str(cust), "--history", str(hist)]) == 0
    assert memory.resolve(pg, "2000000000864", "rožok", as_of="2026-07-20") is not None


def test_the_gate_can_seed_taught_mappings_from_the_corpus_bundle(pg, tmp_path, monkeypatch):
    """#102: a corpus case proving the global/per-customer teach precedence needs the taught
    state to already exist before the case runs — the harness forces shadow mode, so the
    real ask/answer flow (covered by test_orders_teach.py) never fires during a replay."""
    from app.orders import eval_run, memory
    cat = tmp_path / "catalog.csv"
    cat.write_text("GTIN,Sklad,Názov,doplnok\nVIA,1,Vianočka 400g,\n", encoding="utf-8")
    cust = tmp_path / "customers.csv"
    cust.write_text(
        "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
        "Pekáreň s.r.o.,2000000000864,Martin,Košútka 1,,,sklad@pekaren.sk\n",
        encoding="utf-8")
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"cases": []}), encoding="utf-8")
    taught = tmp_path / "taught.json"
    taught.write_text(json.dumps([
        {"scope": "global", "wording": "Twister", "gtin": "VIA", "card": "Vianočka 400g"},
        {"scope": "customer", "customer_ean": "2000000000864", "wording": "Twister",
         "gtin": "SLI50", "card": "Šiška džemová 50g"},
    ]), encoding="utf-8")
    import os
    monkeypatch.setenv("PG_DSN", os.environ["PG_TEST_DSN"])
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path / "cache"))
    assert eval_run.main(["--manifest", str(manifest), "--catalog", str(cat),
                          "--customers", str(cust), "--taught", str(taught)]) == 0
    assert memory.resolve_global(pg, "Twister").gtin == "VIA"
    assert memory.resolve(pg, "2000000000864", "Twister").gtin == "SLI50"
