"""CI gate: `static_edi.build()` must byte-match REAL static-order EDI output (#131).

Mirrors the AI-orders `eval_run.py` gate in spirit (same `.claude/rules/
orders-corpus.md` "the corpus is the gate" discipline) but is much simpler — no LLM,
no cache, just a pure-function byte comparison against a fixed corpus of real processed
static orders (partner, prevNumber, dates, items, catalog snapshot at the time) paired
with the byte-exact output of the REAL production `generator` n8n node (captured by
running its verbatim JS source under node — see the #131 design comment). The corpus
lives outside git (real customer order data, this repo is public) at
`$STATIC_EDI_CORPUS/cases/*.json`, one file per case:
`{"name": ..., "orderData": {...}, "catalog": [...], "expected": {...}}`.

A missing or empty corpus FAILS this check — it never silently skips (`test-strictness.md`).

Usage: `python -m app.orders.static_edi_corpus_check --corpus-dir <dir>`
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import static_edi


def check_case(case: dict) -> list[str]:
    """Returns a list of mismatch descriptions; empty means the case passed."""
    problems = []
    got = static_edi.build(case["orderData"], case["catalog"])
    exp = case["expected"]
    if got.content != exp["content"]:
        problems.append("content differs")
    if got.filename != exp["filename"]:
        problems.append(f"filename: got {got.filename!r} expected {exp['filename']!r}")
    if got.store_ean != exp["storeEAN"]:
        problems.append(f"store_ean: got {got.store_ean!r} expected {exp['storeEAN']!r}")
    if got.buyer_name != exp["buyerName"]:
        problems.append(f"buyer_name: got {got.buyer_name!r} expected {exp['buyerName']!r}")
    if got.items_with_ean != exp["itemsWithEAN"]:
        problems.append("itemsWithEAN differs")
    if got.items_skipped != exp["itemsSkipped"]:
        problems.append("itemsSkipped differs")
    if got.skipped_items != exp["skippedItems"]:
        problems.append("skippedItems differs")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus-dir", required=True)
    args = parser.parse_args(argv)

    cases_dir = Path(args.corpus_dir) / "cases"
    if not cases_dir.is_dir():
        print(f"corpus missing: {cases_dir} is not a directory", file=sys.stderr)
        return 1
    files = sorted(cases_dir.glob("*.json"))
    if not files:
        print(f"corpus empty: no *.json files under {cases_dir}", file=sys.stderr)
        return 1

    failed = 0
    for f in files:
        case = json.loads(f.read_text(encoding="utf-8"))
        problems = check_case(case)
        if problems:
            failed += 1
            print(f"FAIL {case.get('name', f.name)}: {'; '.join(problems)}", file=sys.stderr)
    total = len(files)
    print(f"{total - failed}/{total} static-edi corpus cases byte-match the real generator output")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
