"""CLI for the evaluation harness (#66).

    python -m app.orders.eval_run                        # offline, synthetic corpus (CI)
    python -m app.orders.eval_run --manifest /data/eval/manifest.json
    python -m app.orders.eval_run --manifest ... --live   # real gpt-5.4, refreshes the cache
    python -m app.orders.eval_run --manifest ... --update-baseline
    python -m app.orders.eval_run --manifest ... --live --sample 5   # cheap iteration (#87):
                                                            # deterministic 5-case subset, one
                                                            # per case type; prints an estimated
                                                            # price before the run starts

Exit code 1 on a REGRESSION (a case that used to pass and no longer does), so it can gate
CI and a nightly run alike.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

from .. import config, db
from . import evaluate, llm

# Measured average cost per case for the FULL 30-case corpus at gpt-5.4 rates (2026-07-31
# live re-record: ~$4.50 total, #87) — scaled by the model's PRICES entry so the estimate
# tracks price changes instead of going stale. This is a ROUGH up-front estimate to avoid a
# cost surprise; the API's own token counts (via llm.cost_usd) are what actually bills.
_MEASURED_AVG_COST_PER_CASE_USD = 4.50 / 30
_MEASURED_AVG_COST_MODEL = "gpt-5.4"


def _estimated_cost_usd(n_cases: int, model: str) -> float:
    base = sum(llm.PRICES.get(_MEASURED_AVG_COST_MODEL, llm.PRICES[llm.DEFAULT_MODEL]))
    this = sum(llm.PRICES.get(model, llm.PRICES[llm.DEFAULT_MODEL]))
    scale = (this / base) if base else 1.0
    return n_cases * _MEASURED_AVG_COST_PER_CASE_USD * scale


def select_sample(cases: list[dict], n: int) -> list[dict]:
    """Deterministic N-case subset — NEVER random (#87): a gate that draws different cases
    each run can pass a change because the broken order type wasn't drawn, which is exactly
    the "fix one type, break five" failure the corpus exists to catch.

    Picks one case per DISTINCT `type`, in first-seen manifest order, up to N types; if N
    exceeds the number of distinct types, fills the remaining slots with the next
    not-yet-picked cases in manifest order.
    """
    if n <= 0 or n >= len(cases):
        return cases
    picked: list[dict] = []
    picked_ids: set[str] = set()
    seen_types: set[str] = set()
    for c in cases:
        t = c.get("type", "?")
        if t not in seen_types and len(picked) < n:
            seen_types.add(t)
            picked.append(c)
            picked_ids.add(c["id"])
    if len(picked) < n:
        for c in cases:
            if len(picked) >= n:
                break
            if c["id"] not in picked_ids:
                picked.append(c)
                picked_ids.add(c["id"])
    return picked


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=str(evaluate.SYNTHETIC_MANIFEST))
    ap.add_argument("--baseline", default="")
    ap.add_argument("--live", action="store_true", help="call the real model")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--catalog", default="", help="frozen catalog CSV to import first")
    ap.add_argument("--customers", default="", help="frozen customer CSV to import first")
    ap.add_argument("--require-all", action="store_true",
                    help="fail unless EVERY case passes, not just on a regression")
    ap.add_argument("--history", default="",
                    help="archived deliveries to seed the item history with")
    ap.add_argument("--dump", default="",
                    help="write per-case results here, for adjudicating the corpus")
    ap.add_argument("--sample", type=int, default=0,
                    help="deterministic subset of N cases (one per case type, then fill in "
                         "manifest order) for cheap prompt-iteration runs — never random. "
                         "The full corpus stays the merge gate.")
    args = ap.parse_args(argv)

    cfg = config.Config.load()
    conn = db.connect(cfg.pg_dsn)
    db.init_schema(conn)
    if args.catalog and args.customers:
        # The corpus is scored against the catalog it was WRITTEN against, never against
        # whatever the live sheet says today (#79).
        from . import snapshot
        sid = snapshot.import_files(conn, args.catalog, args.customers)
        print(f"frozen snapshot imported: {sid}")
    if args.history:
        # Cases are scored against the history as it stood on the day of the email, so the
        # bundle carries that history and the gate loads it.
        from . import memory
        added = memory.seed_from_archive(
            conn, json.loads(Path(args.history).read_text(encoding="utf-8")))
        print(f"delivery history seeded: {added} new lines")

    manifest_path = args.manifest
    sample_tmp: Path | None = None
    if args.sample:
        all_cases = evaluate.load_manifest(args.manifest)
        sampled = select_sample(all_cases, args.sample)
        print(f"sampled {len(sampled)}/{len(all_cases)} case(s): "
              f"{', '.join(c['id'] for c in sampled)}")
        fd, tmp_name = tempfile.mkstemp(suffix=".json", prefix="eval-sample-")
        sample_tmp = Path(tmp_name)
        sample_tmp.write_text(json.dumps({"cases": sampled}, ensure_ascii=False),
                               encoding="utf-8")
        os.close(fd)
        manifest_path = str(sample_tmp)

    if args.live:
        n_cases = len(evaluate.load_manifest(manifest_path))
        model = getattr(cfg, "orders_model", llm.DEFAULT_MODEL) or llm.DEFAULT_MODEL
        est = _estimated_cost_usd(n_cases, model)
        print(f"--live: estimated cost ~${est:.2f} for {n_cases} case(s) at {model} "
              f"(rough — measured avg $"
              f"{_MEASURED_AVG_COST_PER_CASE_USD:.3f}/case at {_MEASURED_AVG_COST_MODEL}, "
              "scaled by model pricing; real cost is billed by the API's own token counts)")

    try:
        results, summary = evaluate.run_corpus(conn, cfg, manifest_path, offline=not args.live)
    finally:
        if sample_tmp is not None:
            sample_tmp.unlink(missing_ok=True)

    print(json.dumps(summary, ensure_ascii=False, indent=1))
    for r in results:
        if not r.score.passed:
            print(f"FAIL {r.case_id} ({r.type}): {'; '.join(r.score.problems)}")

    if args.dump:
        Path(args.dump).write_text(json.dumps(
            [{"case_id": r.case_id, "type": r.type, "passed": r.score.passed,
              "item_recall": r.score.item_recall, "problems": r.score.problems,
              "actual": getattr(r, "actual", None)} for r in results],
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"actuals written: {args.dump}")

    baseline_path = Path(args.baseline) if args.baseline else None
    baseline = {}
    if baseline_path and baseline_path.exists():
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    regressed = evaluate.regressions(results, baseline)
    for r in regressed:
        print(f"REGRESSION {r.case_id} ({r.type}): {'; '.join(r.score.problems)}")

    if args.update_baseline and baseline_path:
        baseline_path.write_text(
            json.dumps(evaluate.new_baseline(results), ensure_ascii=False, indent=1),
            encoding="utf-8")
        print(f"baseline updated: {baseline_path}")
    # Cases pinning behaviour the engine does not have yet. Printed with their ticket every
    # run — never silently dropped — but they do not block the other cases.
    known = {c["id"]: c["known_defect"] for c in evaluate.load_manifest(args.manifest)
             if c.get("known_defect")}
    failed = [r for r in results if not r.score.passed]
    for r in failed:
        if r.case_id in known:
            print(f"KNOWN DEFECT {known[r.case_id]} {r.case_id} ({r.type}): "
                  f"{'; '.join(r.score.problems)}")
    if known:
        print(f"{len(known)} case(s) excluded from the hard gate as known defects: "
              f"{', '.join(sorted(known))}")
    blocking = [r for r in failed if r.case_id not in known]
    if args.require_all and blocking:
        print(f"GATE FAILED: {len(blocking)} of {len(results)} cases do not pass")
        return 1
    if args.require_all and not results:
        print("GATE FAILED: the corpus is empty — the gate must never pass vacuously")
        return 1
    return 1 if regressed else 0


if __name__ == "__main__":      # pragma: no cover
    sys.exit(main())
