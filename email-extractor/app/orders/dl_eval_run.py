"""CLI for the DL evaluation harness (#205, DL migration F6) — mirrors `eval_run.py`
one-for-one, adapted for `dl_evaluate`'s per-document scoring and DL's two-tab catalog
union (R20: `produkty dodacie listy` + `produkty objednavky`).

    python -m app.orders.dl_eval_run                          # offline, synthetic corpus
    python -m app.orders.dl_eval_run --manifest /path/to/dl/manifest.json \\
        --dl-catalog dl_catalog.csv --objednavky-catalog objednavky_catalog.csv \\
        --customers customers.csv --baseline baseline.json --require-all
    python -m app.orders.dl_eval_run --manifest ... --live      # real gpt-5.4, refreshes cache
    python -m app.orders.dl_eval_run --manifest ... --live --sample 3   # cheap iteration

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
from . import dl_evaluate, dl_memory, dl_snapshot, llm
from .eval_run import _estimated_cost_usd, select_sample


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=str(dl_evaluate.SYNTHETIC_MANIFEST))
    ap.add_argument("--baseline", default="")
    ap.add_argument("--live", action="store_true", help="call the real model")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--dl-catalog", default="",
                    help="frozen 'produkty dodacie listy' tab CSV to import first")
    ap.add_argument("--objednavky-catalog", default="",
                    help="frozen 'produkty objednavky' tab CSV (R20's union side, optional)")
    ap.add_argument("--customers", default="", help="frozen suppliers CSV to import first")
    ap.add_argument("--require-all", action="store_true",
                    help="fail unless EVERY case passes, not just on a regression")
    ap.add_argument("--history", default="",
                    help="archived dl_item_memory rows (n8n-import shape) to seed before running")
    ap.add_argument("--dump", default="", help="write per-case results here")
    ap.add_argument("--sample", type=int, default=0,
                    help="deterministic subset of N cases (one per case type, then fill in "
                         "manifest order) for cheap prompt-iteration runs — never random.")
    args = ap.parse_args(argv)

    cfg = config.Config.load()
    conn = db.connect(cfg.pg_dsn)
    db.init_schema(conn)
    if args.dl_catalog and args.customers:
        # The corpus is scored against the catalog it was WRITTEN against, never against
        # whatever the live sheet says today — mirrors eval_run.py's own --catalog reasoning.
        sid = dl_snapshot.import_snapshot(
            conn, Path(args.dl_catalog).read_text(encoding="utf-8"),
            Path(args.objednavky_catalog).read_text(encoding="utf-8")
            if args.objednavky_catalog else "",
            Path(args.customers).read_text(encoding="utf-8"))
        print(f"DL frozen snapshot imported: {sid}")
    if args.history:
        added = dl_memory.import_n8n_rows(
            conn, json.loads(Path(args.history).read_text(encoding="utf-8")))
        print(f"DL item memory seeded: {added} new rows")

    manifest_path = args.manifest
    sample_tmp: Path | None = None
    try:
        if args.sample:
            all_cases = dl_evaluate.load_manifest(args.manifest)
            sampled = select_sample(all_cases, args.sample)
            print(f"sampled {len(sampled)}/{len(all_cases)} case(s): "
                  f"{', '.join(c['id'] for c in sampled)}")
            fd, tmp_name = tempfile.mkstemp(suffix=".json", prefix="dl-eval-sample-")
            sample_tmp = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"cases": sampled}, f, ensure_ascii=False)
            manifest_path = str(sample_tmp)

        if args.live:
            n_cases = len(dl_evaluate.load_manifest(manifest_path))
            model = getattr(cfg, "orders_model", llm.DEFAULT_MODEL) or llm.DEFAULT_MODEL
            est = _estimated_cost_usd(n_cases, model)
            print(f"--live: estimated cost ~${est:.2f} for {n_cases} case(s) at {model} "
                  "(rough — DL cases have no vision call, so the real cost per case is "
                  "usually LOWER than this AI-orders-derived estimate)")

        results, summary = dl_evaluate.run_corpus(conn, cfg, manifest_path, offline=not args.live)
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
    regressed = dl_evaluate.regressions(results, baseline)
    for r in regressed:
        print(f"REGRESSION {r.case_id} ({r.type}): {'; '.join(r.score.problems)}")

    if args.update_baseline and baseline_path:
        baseline_path.write_text(
            json.dumps(dl_evaluate.new_baseline(results), ensure_ascii=False, indent=1),
            encoding="utf-8")
        print(f"baseline updated: {baseline_path}")
    known = {c["id"]: c["known_defect"] for c in dl_evaluate.load_manifest(args.manifest)
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
