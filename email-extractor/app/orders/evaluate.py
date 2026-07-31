"""The evaluation harness (#66): run a corpus of real orders and NOTICE regressions.

This exists for one sentence: *"you improve one order type and break five others"*. So the
harness is built around detection, not around an average:

- every case is scored on the whole outcome — customer, delivery date, every item's card
  and quantity, and whether it should have gone to review at all;
- results aggregate **per order type**, so a gain in one type cannot hide a loss in
  another;
- the baseline is **locked**: a case that once passed and now fails fails the run, no
  matter what the average did.

Two tiers, same harness:

- **offline** — LLM answers come from the on-disk cache (`llm.Client(offline=True)`), so a
  full corpus replays in seconds and exercises the ladder, the EDI writer and the report.
  This is the tier that catches "the Céder fix broke AGEL".
- **live** — the same corpus through real gpt-5.4, which is what refreshes the cache and
  measures the model itself.

The corpus of REAL emails lives on the add-on volume (no customer email in git). The
synthetic manifest committed next to this file is what CI runs, so the harness itself is
regression-tested even where the real corpus is not reachable.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from . import llm, pipeline, report, snapshot
from . import upload as upload_mod

log = logging.getLogger("orders.evaluate")

SYNTHETIC_MANIFEST = Path(__file__).with_name("eval_manifest_synthetic.json")
# The real corpus on the volume; paths, not bytes, are what git ever sees.
VOLUME_MANIFEST = Path("/data/eval/manifest.json")
QUANTITY_TOLERANCE = 0.001


@dataclass
class Score:
    passed: bool
    item_recall: float
    problems: list[str] = field(default_factory=list)


@dataclass
class Result:
    case_id: str
    type: str
    score: Score


def _items_map(items) -> dict[str, float]:
    return {str(i.get("gtin")): float(i.get("quantity") or 0) for i in items or []}


def _compare_items(want: dict, got: dict, label: str, problems: list[str]) -> int:
    """Item-level comparison; `label` names the order the items belong to (or is empty)."""
    where = f" ({label})" if label else ""
    hits = 0
    for gtin, qty in want.items():
        if gtin not in got:
            problems.append(f"chýba karta {gtin} ({qty:g}){where}")
        elif abs(got[gtin] - qty) > QUANTITY_TOLERANCE:
            problems.append(f"iné množstvo pri {gtin}{where}: čakáme {qty:g}, "
                            f"dostali {got[gtin]:g}")
        else:
            hits += 1
    for gtin in got.keys() - want.keys():
        problems.append(f"karta {gtin} navyše ({got[gtin]:g}){where}")
    return hits


def score(expected: dict, actual: dict) -> Score:
    """Compare one case's outcome. Item order and numeric formatting are not differences.

    Two expected shapes, because one email can carry several orders:

    - flat (`delivery_date` + `items`) for the ordinary single-order email;
    - `orders: [{delivery_date, items}]`, matched by delivery date, for an email asking for
      several dates at once. n8n writes one EDI file per date, so scoring a flattened item
      list would silently ignore every order after the first (#78).
    """
    problems: list[str] = []

    if str(expected.get("customer_ean") or "") != str(actual.get("customer_ean") or ""):
        problems.append(f"iný zákazník: čakáme {expected.get('customer_ean')!r}, "
                        f"dostali {actual.get('customer_ean')!r}")

    if expected.get("orders") is not None:
        return _score_per_order(expected, actual, problems)

    if str(expected.get("delivery_date") or "") != str(actual.get("delivery_date") or ""):
        problems.append(f"iný dátum dodania: čakáme {expected.get('delivery_date')!r}, "
                        f"dostali {actual.get('delivery_date')!r}")

    want, got = _items_map(expected.get("items")), _items_map(actual.get("items"))
    hits = _compare_items(want, got, "", problems)

    # The opposite mistake matters as much: shipping what the warehouse had to check.
    if expected.get("should_review") and actual.get("shipped"):
        problems.append("malo ísť na kontrolu, ale odoslalo sa")
    if expected.get("should_ship") and not actual.get("shipped"):
        problems.append("malo sa odoslať, ale skončilo na kontrole")

    recall = (hits / len(want)) if want else (1.0 if not got else 0.0)
    return Score(passed=not problems, item_recall=recall, problems=problems)


def _score_per_order(expected: dict, actual: dict, problems: list[str]) -> Score:
    """Match orders by delivery date, then compare each one's items.

    Sequence is not a difference (the model may emit the dates in any order), but a missing
    date, an invented date, and a wrong item inside the SECOND order all are — exactly the
    failures a flattened comparison could not see.
    """
    want_orders = {str(o.get("delivery_date") or ""): o for o in expected["orders"]}
    got_orders = {str(o.get("delivery_date") or ""): o
                  for o in actual.get("order_results") or []}

    hits = wanted = 0
    for date, order in want_orders.items():
        if date not in got_orders:
            problems.append(f"chýba celá objednávka na {date}")
            continue
        if "items" not in order:
            # The author could not prove this order's items (a weekly order whose per-item
            # ground truth is gone). Asserting guessed GTINs would lock a wrong answer into
            # the baseline, so the date alone is asserted.
            continue
        want = _items_map(order.get("items"))
        wanted += len(want)
        hits += _compare_items(want, _items_map(got_orders[date].get("items")),
                               date, problems)
    for date in got_orders.keys() - want_orders.keys():
        problems.append(f"objednávka na {date} navyše — taký dátum v e-maile nebol")

    if expected.get("should_review") and actual.get("shipped"):
        problems.append("malo ísť na kontrolu, ale odoslalo sa")
    if expected.get("should_ship") and not actual.get("shipped"):
        problems.append("malo sa odoslať, ale skončilo na kontrole")

    recall = (hits / wanted) if wanted else (1.0 if not got_orders else 0.0)
    return Score(passed=not problems, item_recall=recall, problems=problems)


def summarize(results: list[Result]) -> dict:
    by_type: dict[str, dict] = {}
    for r in results:
        bucket = by_type.setdefault(r.type, {"passed": 0, "cases": 0})
        bucket["cases"] += 1
        bucket["passed"] += 1 if r.score.passed else 0
    return {
        "total": {"passed": sum(1 for r in results if r.score.passed), "cases": len(results)},
        "by_type": by_type,
    }


# --- the locked baseline -------------------------------------------------

def regressions(results: list[Result], baseline: dict) -> list[Result]:
    """Cases that USED to pass and now do not. Anything else is not a regression."""
    known = (baseline or {}).get("cases") or {}
    out = [r for r in results
           if not r.score.passed and known.get(r.case_id, {}).get("passed")]
    for r in out:
        log.error("REGRESSION %s (%s): %s", r.case_id, r.type, "; ".join(r.score.problems))
    return out


def new_baseline(results: list[Result]) -> dict:
    return {"cases": {r.case_id: {"passed": r.score.passed, "type": r.type,
                                  "item_recall": round(r.score.item_recall, 3)}
                      for r in results}}


# --- running the corpus --------------------------------------------------

def load_manifest(path) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["cases"]


def _actual_from_run(result: dict) -> dict:
    items = [{"gtin": i["gtin"], "quantity": i["quantity"]}
             for i in result.get("items") or [] if i.get("gtin")]
    # Only orders that would actually be SENT count. The pipeline still extracts and reports
    # an order it refuses to ship, and scoring those would fail every must-review case for
    # doing exactly the right thing.
    orders = [{"delivery_date": o.get("delivery_date", ""), "status": o.get("status", ""),
               "items": [{"gtin": i["gtin"], "quantity": i["quantity"]}
                         for i in o.get("items") or [] if i.get("gtin")]}
              for o in result.get("order_results") or []
              if o.get("status") in ("ok", "partial")]
    return {"customer_ean": result.get("customer_ean", ""),
            "delivery_date": result.get("delivery_date", ""),
            "items": items, "order_results": orders,
            "shipped": result.get("status") in ("ok", "partial")}


def run_case(conn, cfg, case: dict, snapshot_id: int, client=None) -> Result:
    """Run one case through the REAL pipeline, with every side effect disabled.

    Shadow mode is what makes that true — the harness must never upload, post, learn or
    write an event, or an evaluation would ship orders.
    """
    inert = _inert(cfg)
    run = pipeline.run(conn, inert, case["email"], snapshot_id, client=client,
                       upload=_refuse_upload, post=_refuse_post)
    actual = _actual_from_run(run)
    return Result(case_id=case["id"], type=case.get("type", "?"),
                  score=score(case["expected"], actual))


def _inert(cfg):
    """A copy of the config that cannot reach the outside world."""
    from copy import copy
    out = copy(cfg)
    out.orders_shadow = True
    out.odoo_url = ""
    out.orion_host = ""
    return out


def _refuse_upload(cfg, name, content):   # pragma: no cover - guard, never called
    raise AssertionError("an evaluation must never upload to ORION")


def _refuse_post(cfg, html, **kw):        # pragma: no cover - guard, never called
    raise AssertionError("an evaluation must never post to Odoo")


def run_corpus(conn, cfg, manifest_path, offline: bool = True,
               snapshot_id: int | None = None) -> tuple[list[Result], dict]:
    """Run every case; returns (results, summary)."""
    cases = load_manifest(manifest_path)
    sid = snapshot_id or snapshot.latest_snapshot_id(conn)
    if not sid:
        raise RuntimeError("no catalog snapshot — import one before evaluating")
    client = llm.from_config(cfg, offline=offline)
    results = []
    for n, case in enumerate(cases, 1):
        # A live run takes tens of minutes. Announce every case: a run that dies silently
        # must not look like a run that is still working (it did, once, for 40 minutes).
        log.info("case %d/%d %s (%s)", n, len(cases), case.get("id"), case.get("type", "?"))
        try:
            results.append(run_case(conn, cfg, case, sid, client=client))
        except llm.CacheMiss as e:
            log.error("case %s has no cached answer: %s", case["id"], e)
            results.append(Result(case_id=case["id"], type=case.get("type", "?"),
                                  score=Score(False, 0.0, [f"chýba záznam v cache: {e}"])))
    summary = summarize(results)
    log.info("evaluation: %d/%d passed; per type: %s", summary["total"]["passed"],
             summary["total"]["cases"], summary["by_type"])
    return results, summary


# keep the modules the tests patch reachable from here
__all__ = ["Result", "Score", "load_manifest", "new_baseline", "regressions", "report",
           "run_case", "run_corpus", "score", "summarize", "upload_mod",
           "SYNTHETIC_MANIFEST", "VOLUME_MANIFEST"]
