"""CLI for the evaluation harness (#66).

    python -m app.orders.eval_run                        # offline, synthetic corpus (CI)
    python -m app.orders.eval_run --manifest /data/eval/manifest.json
    python -m app.orders.eval_run --manifest ... --live   # real gpt-5.4, refreshes the cache
    python -m app.orders.eval_run --manifest ... --update-baseline

Exit code 1 on a REGRESSION (a case that used to pass and no longer does), so it can gate
CI and a nightly run alike.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .. import config, db
from . import evaluate


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
    results, summary = evaluate.run_corpus(conn, cfg, args.manifest, offline=not args.live)

    print(json.dumps(summary, ensure_ascii=False, indent=1))
    for r in results:
        if not r.score.passed:
            print(f"FAIL {r.case_id} ({r.type}): {'; '.join(r.score.problems)}")

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
    failed = [r for r in results if not r.score.passed]
    if args.require_all and failed:
        print(f"GATE FAILED: {len(failed)} of {len(results)} cases do not pass")
        return 1
    if args.require_all and not results:
        print("GATE FAILED: the corpus is empty — the gate must never pass vacuously")
        return 1
    return 1 if regressed else 0


if __name__ == "__main__":      # pragma: no cover
    sys.exit(main())
