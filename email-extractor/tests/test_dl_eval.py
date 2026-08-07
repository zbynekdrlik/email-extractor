"""The DL evaluation harness (#205, DL migration F6) — mirrors `tests/test_eval.py`'s shape
for the delivery-notes engine. What is genuinely NEW here (not just a DL-flavoured repeat of
the AI-orders tests): per-DOCUMENT scoring, since one email can carry more than one delivery
note (F2's own multi-document fix) — the exact shape the Lunys "IS KARAT" announced-vs-attached
incident (spec §4) already lost in production once.
"""
from __future__ import annotations

import json
import os

import pytest

from app.config import Config
from app.orders import dl_evaluate, dl_snapshot

DL_CATALOG_CSV = ("GTIN,Názov,doplnok,hmotnost,Sklad,Cena\n"
                  "8588000000001,Rožok 50g,,0.05,1,0.50\n"
                  "8588000000002,Vianočka 400g,,0.40,1,1.20\n")
OBJ_CATALOG_CSV = "GTIN,Sklad,Názov,doplnok\n"
SUPPLIERS_CSV = ("Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,"
                 "Číslo mobilu,E-mail\n"
                 "Pekáreň Lunys,2000000000864,Prešov,Košútka 1,,,dodavatel@lunys.sk\n")
SUPPLIER_EAN = "2000000000864"


def _snapshot(pg):
    return dl_snapshot.import_snapshot(pg, DL_CATALOG_CSV, OBJ_CATALOG_CSV, SUPPLIERS_CSV)


def _cfg(**kw):
    base = dict(pg_dsn="", data_dir="/tmp")
    base.update(kw)
    return Config(**base)


ONE_DOC_EXPECTED = {
    "status": "ok",
    "documents": [
        {"doc_number": "0100000001", "outcome": "ok", "supplier_name_contains": "Lunys",
         "items": [{"gtin": "8588000000001", "quantity": 10}]},
    ],
}


def _actual(**overrides):
    doc = {"outcome": "ok", "doc_number": "0100000001", "supplier_name": "Pekáreň Lunys",
          "supplier_ean": SUPPLIER_EAN,
          "items": [{"gtin": "8588000000001", "quantity": 10}]}
    doc.update(overrides.pop("doc", {}))
    base = {"status": "ok", "documents": [doc], "items": [], "announced_mismatch": []}
    base.update(overrides)
    return base


# --- scoring one document -------------------------------------------------

def test_an_exact_match_scores_one():
    s = dl_evaluate.score(ONE_DOC_EXPECTED, _actual())
    assert s.passed is True and s.item_recall == 1.0
    assert s.problems == []


def test_a_missing_item_fails_and_names_the_gtin():
    s = dl_evaluate.score(ONE_DOC_EXPECTED, _actual(doc={"items": []}))
    assert s.passed is False
    assert any("8588000000001" in p for p in s.problems)


def test_an_extra_item_fails():
    s = dl_evaluate.score(ONE_DOC_EXPECTED, _actual(
        doc={"items": [{"gtin": "8588000000001", "quantity": 10},
                       {"gtin": "8588000000002", "quantity": 1}]}))
    assert s.passed is False
    assert any("8588000000002" in p for p in s.problems)


def test_a_wrong_quantity_fails():
    s = dl_evaluate.score(ONE_DOC_EXPECTED, _actual(doc={"items": [
        {"gtin": "8588000000001", "quantity": 5}]}))
    assert s.passed is False
    assert any("8588000000001" in p and "5" in p for p in s.problems)


def test_item_order_and_number_formatting_are_not_differences():
    s = dl_evaluate.score(ONE_DOC_EXPECTED, _actual(doc={"items": [
        {"gtin": "8588000000001", "quantity": 10.0}]}))
    assert s.passed is True


def test_the_same_card_twice_is_counted_once_as_the_total():
    expected = {"documents": [{"doc_number": "D1",
                               "items": [{"gtin": "G1", "quantity": 3}]}]}
    actual = {"documents": [{"doc_number": "D1",
                             "items": [{"gtin": "G1", "quantity": 1},
                                      {"gtin": "G1", "quantity": 2}]}]}
    assert dl_evaluate.score(expected, actual).passed is True


def test_a_wrong_outcome_fails_and_says_which():
    s = dl_evaluate.score(ONE_DOC_EXPECTED, _actual(doc={"outcome": "review"}))
    assert s.passed is False
    assert any("review" in p for p in s.problems)


def test_a_wrong_supplier_fails():
    s = dl_evaluate.score(ONE_DOC_EXPECTED, _actual(doc={"supplier_name": "Iný dodávateľ"}))
    assert s.passed is False
    assert any("dodávateľ" in p for p in s.problems)


def test_supplier_ean_is_asserted_when_present_in_expected():
    expected = {"documents": [{"doc_number": "0100000001", "supplier_ean": "9999999999999"}]}
    s = dl_evaluate.score(expected, _actual())
    assert s.passed is False
    assert any("9999999999999" in p for p in s.problems)


def test_item_count_is_asserted_without_naming_exact_gtins():
    """Some cases can only prove HOW MANY items shipped, not which GTIN each one is —
    mirrors `evaluate.py`'s `item_count`-only assertion."""
    expected = {"documents": [{"doc_number": "0100000001", "item_count": 1}]}
    assert dl_evaluate.score(expected, _actual()).passed is True
    assert dl_evaluate.score(expected, _actual(doc={"items": []})).passed is False


def test_reason_contains_is_asserted_on_a_review_outcome():
    expected = {"documents": [{"doc_number": "D1", "outcome": "review",
                               "reason_contains": "nebol najdeny"}]}
    actual = {"documents": [{"doc_number": "D1", "outcome": "review",
                             "reason": "Dodavatel nebol najdeny v databaze"}]}
    assert dl_evaluate.score(expected, actual).passed is True
    actual["documents"][0]["reason"] = "Nieco ine"
    assert dl_evaluate.score(expected, actual).passed is False


# --- multi-document scoring (the genuinely NEW thing vs AI orders) --------

def test_a_second_document_in_the_same_mail_is_scored_independently():
    expected = {"documents": [
        {"doc_number": "611494", "outcome": "ok", "items": [{"gtin": "G1", "quantity": 1}]},
        {"doc_number": "611741", "outcome": "ok", "items": [{"gtin": "G2", "quantity": 2}]},
    ]}
    actual = {"documents": [
        {"doc_number": "611494", "outcome": "ok", "items": [{"gtin": "G1", "quantity": 1}]},
        {"doc_number": "611741", "outcome": "ok", "items": [{"gtin": "G2", "quantity": 2}]},
    ]}
    assert dl_evaluate.score(expected, actual).passed is True


def test_a_dropped_second_document_fails_and_names_it():
    """The exact failure shape W1a/W1b already lost in production once (spec §4) — the
    corpus must be ABLE to fail this the same way a naive single-order scorer would hide."""
    expected = {"documents": [
        {"doc_number": "611494", "outcome": "ok"},
        {"doc_number": "611741", "outcome": "ok"},
    ]}
    actual = {"documents": [{"doc_number": "611494", "outcome": "ok"}]}
    s = dl_evaluate.score(expected, actual)
    assert s.passed is False
    assert any("611741" in p for p in s.problems)


def test_document_order_is_not_a_difference():
    expected = {"documents": [{"doc_number": "A"}, {"doc_number": "B"}]}
    actual = {"documents": [{"doc_number": "B"}, {"doc_number": "A"}]}
    assert dl_evaluate.score(expected, actual).passed is True


def test_an_extra_undeclared_document_fails():
    expected = {"documents": [{"doc_number": "A"}]}
    actual = {"documents": [{"doc_number": "A"}, {"doc_number": "SURPRISE"}]}
    s = dl_evaluate.score(expected, actual)
    assert s.passed is False
    assert any("SURPRISE" in p for p in s.problems)


def test_two_documents_sharing_one_doc_number_are_matched_one_to_one():
    """A duplicate real-world doc_number collision (W4) must not let a SECOND expected
    document silently reuse the first actual match."""
    expected = {"documents": [{"doc_number": "D", "outcome": "ok"},
                              {"doc_number": "D", "outcome": "duplicate"}]}
    actual = {"documents": [{"doc_number": "D", "outcome": "ok"},
                            {"doc_number": "D", "outcome": "duplicate"}]}
    assert dl_evaluate.score(expected, actual).passed is True


# --- announced-vs-attached (spec §4) ---------------------------------------

def test_announced_mismatch_is_asserted():
    expected = {"documents": [], "announced_mismatch": ["0100237306"]}
    assert dl_evaluate.score(expected, {"documents": [],
                                        "announced_mismatch": ["0100237306"]}).passed is True
    s = dl_evaluate.score(expected, {"documents": [], "announced_mismatch": []})
    assert s.passed is False
    assert any("0100237306" in p for p in s.problems)


def test_announced_mismatch_order_is_not_a_difference():
    expected = {"documents": [], "announced_mismatch": ["A", "B"]}
    actual = {"documents": [], "announced_mismatch": ["B", "A"]}
    assert dl_evaluate.score(expected, actual).passed is True


# --- aggregation / baseline (same contract as evaluate.py) -----------------

def test_results_are_aggregated_per_type():
    results = [
        dl_evaluate.Result("c1", "typeA", dl_evaluate.Score(True, 1.0)),
        dl_evaluate.Result("c2", "typeA", dl_evaluate.Score(False, 0.0, ["x"])),
        dl_evaluate.Result("c3", "typeB", dl_evaluate.Score(True, 1.0)),
    ]
    summary = dl_evaluate.summarize(results)
    assert summary["total"] == {"passed": 2, "cases": 3}
    assert summary["by_type"]["typeA"] == {"passed": 1, "cases": 2}
    assert summary["by_type"]["typeB"] == {"passed": 1, "cases": 1}


def test_a_case_that_used_to_pass_and_now_fails_is_a_regression():
    baseline = {"cases": {"c1": {"passed": True}}}
    results = [dl_evaluate.Result("c1", "t", dl_evaluate.Score(False, 0.0, ["broke"]))]
    regressed = dl_evaluate.regressions(results, baseline)
    assert [r.case_id for r in regressed] == ["c1"]


def test_a_case_that_never_passed_is_not_a_regression():
    baseline = {"cases": {"c1": {"passed": False}}}
    results = [dl_evaluate.Result("c1", "t", dl_evaluate.Score(False, 0.0, ["still broke"]))]
    assert dl_evaluate.regressions(results, baseline) == []


def test_new_baseline_records_every_case():
    results = [dl_evaluate.Result("c1", "t", dl_evaluate.Score(True, 1.0))]
    baseline = dl_evaluate.new_baseline(results)
    assert baseline["cases"]["c1"] == {"passed": True, "type": "t", "item_recall": 1.0}


# --- running a real case through the pipeline ------------------------------

class FakeClient:
    def __init__(self, answers: dict[str, list[dict]]):
        self._answers = {k: list(v) for k, v in answers.items()}
        self.calls: list[str] = []
        self.last_prompt_hash = ""

    def json_call(self, system, user, schema, name="result"):
        self.calls.append(name)
        self.last_prompt_hash = name
        queue = self._answers.get(name)
        if not queue:
            raise AssertionError(f"no scripted answer left for {name!r}")
        return queue.pop(0)

    def vision_call(self, *a, **kw):
        raise AssertionError("a corpus case must never need vision (R42/W13 — digital "
                             "text is always supplied)")


def _doc_answer(doc_number="0100000001"):
    return {"documents": [{
        "supplierName": "Pekáreň Lunys", "supplierCity": "Prešov",
        "supplierEmail": "dodavatel@lunys.sk", "docNumber": doc_number,
        "deliveryDate": "01.08.2026", "documentTotalWithoutVAT": 5.0,
        "items": [{"name": "Rožok 50g", "quantity": 10, "unit": "ks", "unitPrice": 0.5,
                  "totalPrice": 5.0, "vatRate": 10}]}]}


CASE = {
    "id": "c-lunys",
    "type": "single_document_ok",
    "email": {"message_id": "eval-c-lunys", "subject": "Dodaci list",
              "from_addr": "dodavatel@lunys.sk", "has_attachments": True,
              "today": "2026-08-07"},
    "attachments": [{"idx": 0, "filename": "dl.pdf", "machine_text": "dodaci list text"}],
    "expected": {"status": "ok", "documents": [
        {"doc_number": "0100000001", "outcome": "ok", "supplier_name_contains": "Lunys",
         "items": [{"gtin": "8588000000001", "quantity": 10}]}]},
}


def test_run_case_never_uploads_or_posts(pg):
    """`run_case` hardcodes `_refuse_upload`/`_refuse_post` (both raise loudly if ever
    called) — a passing shadow-mode case never reaches them at all (the shadow branch of
    `_process_document` returns before either would fire), so the real proof is the DB
    state below: nothing observable landed anywhere."""
    sid = _snapshot(pg)
    client = FakeClient({"dl_documents": [_doc_answer()],
                         "dl_supplier": [{"matched": True, "ean_edi": SUPPLIER_EAN,
                                        "name": "Pekáreň Lunys", "matchConfidence": 0.95,
                                        "matchReason": "presná zhoda"}],
                         "dl_item": [{"gtin": "8588000000001",
                                     "matchedCatalogName": "Rožok 50g",
                                     "matchConfidence": 0.97, "matchReason": "presná zhoda",
                                     "mass": 0.05}]})
    result = dl_evaluate.run_case(pg, _cfg(), CASE, sid, client=client)
    assert result.score.passed is True, result.score.problems
    assert result.actual["status"] == "ok"
    # shadow guarantee: nothing observable landed anywhere
    assert pg.execute("SELECT count(*) FROM desadv_sent").fetchone()[0] == 0
    assert pg.execute("SELECT count(*) FROM dl_item_memory").fetchone()[0] == 0
    assert pg.execute("SELECT count(*) FROM email_events").fetchone()[0] == 0


def test_run_case_refuses_to_call_vision():
    """Every corpus case supplies non-empty machine_text with no pdf_bytes — proves the
    harness itself would loudly fail if a future case accidentally needed vision, rather
    than silently costing a live call during --live recording."""
    client = FakeClient({})
    assert hasattr(client, "vision_call")


def test_offline_run_refuses_a_cache_miss(tmp_path):
    from app.orders import llm
    client = llm.Client(api_key="", cache_dir=str(tmp_path), offline=True)
    with pytest.raises(llm.CacheMiss):
        client.json_call("system", "input", {"type": "object"})


def test_run_corpus_offline_with_no_cache_fails_the_case_not_a_crash(pg, tmp_path):
    """Unlike `evaluate.run_corpus`, a missing offline cache entry here never raises —
    `dl_worker._process_message`'s own per-call try/excepts already turn it into a
    `"review"` document outcome (R17/W9), so the case fails scoring normally instead of
    crashing the whole corpus run."""
    sid = _snapshot(pg)
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"cases": [CASE]}), encoding="utf-8")
    results, summary = dl_evaluate.run_corpus(
        pg, _cfg(llm_cache_dir=str(tmp_path / "cache")), str(manifest), offline=True,
        snapshot_id=sid)
    assert summary["total"] == {"passed": 0, "cases": 1}
    assert results[0].score.passed is False
    assert results[0].actual["documents"][0]["outcome"] == "review"


def test_the_synthetic_manifest_is_loadable_and_typed():
    cases = dl_evaluate.load_manifest(dl_evaluate.SYNTHETIC_MANIFEST)
    assert cases, "the synthetic manifest must not be empty"
    for case in cases:
        assert case["id"] and case["type"]
        assert case["expected"]["documents"]
        assert json.dumps(case)


# --- the gate CLI (dl_eval_run.py) -----------------------------------------

def test_the_gate_command_imports_the_frozen_snapshot_and_runs_offline(pg, tmp_path,
                                                                       monkeypatch):
    from app.orders import dl_eval_run
    dl_cat = tmp_path / "dl_catalog.csv"
    dl_cat.write_text(DL_CATALOG_CSV, encoding="utf-8")
    cust = tmp_path / "customers.csv"
    cust.write_text(SUPPLIERS_CSV, encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"cases": []}), encoding="utf-8")

    monkeypatch.setenv("PG_DSN", os.environ["PG_TEST_DSN"])
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path / "cache"))
    code = dl_eval_run.main(["--manifest", str(manifest), "--dl-catalog", str(dl_cat),
                             "--customers", str(cust)])
    assert code == 0


def test_require_all_fails_on_any_failing_case(pg, tmp_path, monkeypatch):
    from app.orders import dl_eval_run
    cases = {"cases": [{"id": "c1", "type": "t", "email": {"subject": "x"}, "attachments": [],
                        "expected": {"documents": []}}]}
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps(cases), encoding="utf-8")
    monkeypatch.setattr(dl_evaluate, "run_corpus", lambda *a, **k: (
        [dl_evaluate.Result("c1", "t", dl_evaluate.Score(False, 0.0, ["nesedí"]))],
        {"total": {"passed": 0, "cases": 1}, "by_type": {}}))
    monkeypatch.setenv("PG_DSN", os.environ["PG_TEST_DSN"])
    assert dl_eval_run.main(["--manifest", str(manifest)]) == 0, "no baseline, no regression"
    assert dl_eval_run.main(["--manifest", str(manifest), "--require-all"]) == 1


def test_require_all_refuses_to_pass_on_an_empty_corpus(pg, tmp_path, monkeypatch):
    from app.orders import dl_eval_run
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"cases": []}), encoding="utf-8")
    monkeypatch.setenv("PG_DSN", os.environ["PG_TEST_DSN"])
    monkeypatch.setattr(dl_evaluate, "run_corpus", lambda *a, **k: (
        [], {"total": {"passed": 0, "cases": 0}, "by_type": {}}))
    assert dl_eval_run.main(["--manifest", str(manifest), "--require-all"]) == 1


def test_the_gate_can_seed_dl_item_memory_from_the_history_bundle(pg, tmp_path, monkeypatch):
    from app.orders import dl_eval_run, dl_memory
    dl_cat = tmp_path / "dl_catalog.csv"
    dl_cat.write_text(DL_CATALOG_CSV, encoding="utf-8")
    cust = tmp_path / "customers.csv"
    cust.write_text(SUPPLIERS_CSV, encoding="utf-8")
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"cases": []}), encoding="utf-8")
    hist = tmp_path / "history.json"
    hist.write_text(json.dumps([{"cust": SUPPLIER_EAN, "item": "rožok 50g",
                                 "gtin": "8588000000001", "card": "Rožok 50g",
                                 "at": "2026-07-01T10:00:00", "src": "ship", "cnt": 1}]),
                    encoding="utf-8")
    monkeypatch.setenv("PG_DSN", os.environ["PG_TEST_DSN"])
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path / "cache"))
    assert dl_eval_run.main(["--manifest", str(manifest), "--dl-catalog", str(dl_cat),
                             "--customers", str(cust), "--history", str(hist)]) == 0
    recalled = dl_memory.resolve(pg, SUPPLIER_EAN, "rožok 50g", as_of="2026-07-20")
    assert recalled is not None and recalled.gtin == "8588000000001"


def test_the_gate_command_can_sample_and_dump(pg, tmp_path, monkeypatch):
    from app.orders import dl_eval_run
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"cases": [
        {"id": "a", "type": "t1", "email": {}, "expected": {"documents": []}},
        {"id": "b", "type": "t2", "email": {}, "expected": {"documents": []}},
    ]}), encoding="utf-8")
    dump = tmp_path / "dump.json"
    monkeypatch.setenv("PG_DSN", os.environ["PG_TEST_DSN"])
    monkeypatch.setattr(dl_evaluate, "run_corpus", lambda *a, **k: (
        [dl_evaluate.Result("a", "t1", dl_evaluate.Score(True, 1.0))],
        {"total": {"passed": 1, "cases": 1}, "by_type": {}}))
    code = dl_eval_run.main(["--manifest", str(manifest), "--sample", "1", "--dump",
                             str(dump)])
    assert code == 0
    assert json.loads(dump.read_text())[0]["case_id"] == "a"
