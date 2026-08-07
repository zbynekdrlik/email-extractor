"""DL evaluation harness (#205, DL migration F6) — the delivery-notes sibling of
`evaluate.py`, same reason for existing: *"you improve one supplier's extraction and break
another's."*

The one structural difference from the AI-orders harness: DL scores per DOCUMENT, not per
delivery date. One email can legitimately carry more than one delivery note across its
attachments (F2's own multi-document fix, `docs/superpowers/specs/
2026-08-07-delivery-notes-python-design.md` W1a/W1b) — scoring a flattened item list the way
`evaluate.py`'s single-order shape does would silently hide a lost SECOND document exactly the
way the Lunys "IS KARAT" announced-vs-attached incident (spec §4) already did in production.

Runs every case through `dl_worker._process_message(..., shadow=True, upload=refuse,
post=refuse)` — the SAME side-effect-free entry point `dl_worker.tick()`'s own shadow branch
already uses in production; this module adds no new pipeline behaviour, only a harness and a
scorer around what F5 already shipped. `_process_message`'s `attachments=` parameter (added by
this same phase) lets a case hand fixture bytes/text directly, so a case needs no real
Postgres `messages`/`attachments` row and no file on the add-on's data volume.

Two tiers, same as `evaluate.py`:

- **offline** — LLM answers replay from the on-disk cache (`llm.Client(offline=True)`); a full
  corpus runs in seconds. This is the tier `e2e-dl` (CI) runs.
- **live** — the same corpus through real gpt-5.4, which is what refreshes the cache. Every
  corpus case in this project's own `~/eval-corpus/email-extractor/dl/` deliberately supplies
  `machine_text` with no `pdf_bytes` needing vision (R42/W13: a digital PDF with real extracted
  text never calls `vision_call` at all) — so `--live` only ever costs `json_call`s (one
  extraction + one supplier match + one call per item), never a vision call.
"""
from __future__ import annotations

import base64
import itertools
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from . import dl_snapshot, dl_worker, llm

log = logging.getLogger("orders.dl_evaluate")

SYNTHETIC_MANIFEST = Path(__file__).with_name("dl_eval_manifest_synthetic.json")
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
    # what the engine actually produced — the corpus cannot be adjudicated without it
    actual: dict | None = None


def _items_map(items) -> dict[str, float]:
    """Cards to total quantity — the same card twice ADDS UP, mirrors `evaluate._items_map`.

    An item with no gtin at all is SKIPPED rather than stringified into a shared `"None"`
    key (review finding, #205): two DIFFERENT no-gtin items would otherwise silently merge
    into one bucket instead of being reported as two separate mismatches. Low risk in
    practice — `_shipped_items`'s own filter already guarantees every `actual` item here
    has a real gtin, and a corpus case should never assert an unmatched item as "shipped"
    — but defended explicitly rather than left to accidental string coercion.
    """
    out: dict[str, float] = {}
    for i in items or []:
        gtin = i.get("gtin")
        if not gtin:
            continue
        out[str(gtin)] = out.get(str(gtin), 0.0) + float(i.get("quantity") or 0)
    return out


def _compare_items(want: dict, got: dict, label: str, problems: list[str]) -> int:
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


def _score_document(expected: dict, actual: dict | None, problems: list[str]) -> tuple[int, int]:
    """One expected document vs its matched actual (`None` when no actual document shares its
    `doc_number`). Returns (hits, wanted) for the item-recall aggregate."""
    label = expected.get("doc_number") or "?"
    if actual is None:
        problems.append(f"chýba celý dokument {label}")
        return 0, len(expected.get("items") or [])

    if "outcome" in expected and expected["outcome"] != actual.get("outcome"):
        problems.append(f"iný výsledok dokumentu {label}: čakáme {expected['outcome']!r}, "
                        f"dostali {actual.get('outcome')!r}")
    if "supplier_name_contains" in expected:
        name = str(actual.get("supplier_name") or "")
        needle = str(expected["supplier_name_contains"])
        if needle.lower() not in name.lower():
            problems.append(f"iný dodávateľ dokumentu {label}: čakáme podreťazec {needle!r}, "
                            f"dostali {name!r}")
    if "supplier_ean" in expected and (
            str(expected["supplier_ean"]) != str(actual.get("supplier_ean") or "")):
        problems.append(f"iný EAN dodávateľa {label}: čakáme {expected['supplier_ean']!r}, "
                        f"dostali {actual.get('supplier_ean')!r}")
    if "item_count" in expected:
        got_n = len(actual.get("items") or [])
        if got_n != int(expected["item_count"]):
            problems.append(f"iný počet položiek na EDI ({label}): čakáme "
                            f"{int(expected['item_count'])}, dostali {got_n}")
    if "reason_contains" in expected:
        reason = str(actual.get("reason") or "")
        if str(expected["reason_contains"]) not in reason:
            problems.append(f"v dôvode chýba „{expected['reason_contains']}“ ({label}): "
                            f"{reason!r}")

    hits = wanted = 0
    if "items" in expected:
        want = _items_map(expected["items"])
        wanted = len(want)
        hits = _compare_items(want, _items_map(actual.get("items")), label, problems)
    return hits, wanted


def _best_fit_group(wlist: list[dict], glist: list[int],
                    got_docs: list[dict]) -> tuple[list[str], int, int, set[int]]:
    """Best-fit assignment of `glist` (indices into `got_docs`, all sharing ONE
    doc_number) onto `wlist` (expected documents sharing that SAME doc_number).

    Review finding, #205: a naive first-available-index match is ORDER-DEPENDENT — when
    two-or-more expected documents legitimately share a doc_number (W4: a real duplicate-
    DL scenario the corpus explicitly supports), pairing them by encounter order can match
    the WRONG actual document to the wrong expected slot and report spurious problems for
    an objectively-correct result, contradicting `score()`'s own "sequence is not a
    difference" guarantee. Instead: try every injective assignment (which `wlist` slots
    get a match, and which `glist` index each gets) and keep the one with the FEWEST total
    problems — groups are always small in practice (a handful of duplicates at most), so
    the combinatorics (`C(n_w, k) * k!`) stay trivial.
    """
    n_w = len(wlist)
    k = min(n_w, len(glist))
    best: tuple[int, list[str], int, int, set[int]] | None = None
    for subset in itertools.combinations(range(n_w), k):
        for perm in itertools.permutations(glist, k):
            assign = dict(zip(subset, perm, strict=True))
            trial_problems: list[str] = []
            trial_hits = trial_wanted = 0
            used: set[int] = set()
            for wi, wd in enumerate(wlist):
                actual_idx = assign.get(wi)
                actual_doc = got_docs[actual_idx] if actual_idx is not None else None
                if actual_idx is not None:
                    used.add(actual_idx)
                h, w = _score_document(wd, actual_doc, trial_problems)
                trial_hits += h
                trial_wanted += w
            key = len(trial_problems)
            if best is None or key < best[0]:
                best = (key, trial_problems, trial_hits, trial_wanted, used)
    if best is None:      # n_w == 0 — nothing in this doc_number group was ever expected
        return [], 0, 0, set()
    _, problems, hits, wanted, used = best
    return problems, hits, wanted, used


def score(expected: dict, actual: dict) -> Score:
    """Compare one case's outcome against `dl_worker._process_message`'s return.

    Documents are matched by `doc_number` — sequence is not a difference (extraction may
    emit attachments' documents in any order), but a missing document, an extra one, and a
    wrong item inside the SECOND document of a multi-document mail all are. Documents that
    share the SAME doc_number are matched by best-fit CONTENT (`_best_fit_group`), not by
    the order they happen to appear in either list.
    """
    problems: list[str] = []
    if "status" in expected and expected["status"] != actual.get("status"):
        problems.append(f"iný stav správy: čakáme {expected['status']!r}, "
                        f"dostali {actual.get('status')!r}")

    got_docs = list(actual.get("documents") or [])
    want_docs = expected.get("documents") or []

    want_groups: dict[str, list[dict]] = {}
    for wd in want_docs:
        want_groups.setdefault(wd.get("doc_number", "") or "", []).append(wd)
    got_groups: dict[str, list[int]] = {}
    for i, d in enumerate(got_docs):
        got_groups.setdefault(d.get("doc_number") or "", []).append(i)

    matched: set[int] = set()
    hits = wanted = 0
    for num, wlist in want_groups.items():
        group_problems, h, w, used = _best_fit_group(wlist, got_groups.get(num, []),
                                                      got_docs)
        problems.extend(group_problems)
        hits += h
        wanted += w
        matched |= used

    for i, d in enumerate(got_docs):
        if i not in matched:
            problems.append(f"dokument navyše: {d.get('doc_number')!r} "
                            f"({d.get('outcome')!r}) — v e-maile taký nebol")

    if "announced_mismatch" in expected:
        want_mismatch = sorted(str(x) for x in expected["announced_mismatch"])
        got_mismatch = sorted(str(x) for x in actual.get("announced_mismatch") or [])
        if want_mismatch != got_mismatch:
            problems.append(f"iný announced-vs-attached nesúlad: čakáme {want_mismatch}, "
                            f"dostali {got_mismatch}")

    recall = (hits / wanted) if wanted else 1.0
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
    known = (baseline or {}).get("cases") or {}
    out = [r for r in results
           if not r.score.passed and known.get(r.case_id, {}).get("passed")]
    for r in out:
        log.error("DL REGRESSION %s (%s): %s", r.case_id, r.type, "; ".join(r.score.problems))
    return out


def new_baseline(results: list[Result]) -> dict:
    return {"cases": {r.case_id: {"passed": r.score.passed, "type": r.type,
                                  "item_recall": round(r.score.item_recall, 3)}
                      for r in results}}


# --- running the corpus --------------------------------------------------

def load_manifest(path) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["cases"]


def _decode_attachment(a: dict) -> dict:
    raw = a.get("pdf_bytes_b64") or ""
    return {"idx": a.get("idx", 0), "filename": a.get("filename", ""),
           "pdf_bytes": base64.b64decode(raw) if raw else b"",
           "machine_text": a.get("machine_text", "")}


def _inert(cfg):
    """A copy of the config that cannot reach the outside world — mirrors
    `evaluate._inert`. `delivery_notes_shadow`/`delivery_notes_engine` are irrelevant here:
    `run_case` always calls `_process_message` with `shadow=True` explicitly, never through
    `tick()`'s own engine/shadow resolution."""
    from copy import copy
    out = copy(cfg)
    out.odoo_url = ""
    out.orion_host = ""
    return out


def _refuse_upload(cfg, name, content, dir_override=None):   # pragma: no cover - guard
    raise AssertionError("a DL evaluation must never upload to ORION")


def _refuse_post(cfg, html, **kw):                            # pragma: no cover - guard
    raise AssertionError("a DL evaluation must never post to Odoo")


def run_case(conn, cfg, case: dict, snapshot_id: int, client=None) -> Result:
    """Run one case through the REAL DL pipeline, with every side effect disabled.

    Review finding, #205: `dl_worker._process_message` swallows an LLM failure into a
    `"review"` document outcome (R17/W9) EXCEPT one shape — `_RetryLater`, raised by
    `_check_retry` when the failure text matches the transient-error regex AND
    `attempts < 3` (the state every corpus case starts in, via `message.setdefault
    ("attempts", 0)` below). That is a genuine `--live` possibility (an OpenAI
    timeout/rate-limit), not just a theoretical one, so it is caught HERE and turned
    into a failing `Result` — the same "never crash the whole corpus over one case"
    guarantee `run_corpus` already gives `llm.CacheMiss` for the AI-orders harness.
    """
    catalog = dl_snapshot.load_catalog(conn, snapshot_id)
    suppliers = dl_snapshot.load_suppliers(conn, snapshot_id)
    inert = _inert(cfg)
    message = dict(case["email"])
    message.setdefault("message_id", f"eval-{case['id']}")
    message.setdefault("attempts", 0)
    attachments = [_decode_attachment(a) for a in case.get("attachments") or []]
    try:
        result = dl_worker._process_message(   # noqa: SLF001 — the shared shadow-safe entrypoint
            conn, inert, client, message, snapshot_id, catalog, suppliers, shadow=True,
            upload=_refuse_upload, post=_refuse_post, attachments=attachments)
    except dl_worker._RetryLater as e:          # noqa: SLF001 — see docstring above
        return Result(case_id=case["id"], type=case.get("type", "?"),
                     score=Score(False, 0.0, [f"prechodná chyba počas behu: {e}"]))
    return Result(case_id=case["id"], type=case.get("type", "?"),
                 score=score(case["expected"], result), actual=result)


def run_corpus(conn, cfg, manifest_path, offline: bool = True,
               snapshot_id: int | None = None) -> tuple[list[Result], dict]:
    """Run every case; returns (results, summary).

    Unlike `evaluate.run_corpus`, this never needs to catch `llm.CacheMiss` around
    `run_case` itself: every LLM call inside `dl_worker._process_message` already runs
    behind its OWN try/except (R17/W9's retry/review handling, and `dl_extract.
    extract_email`'s per-attachment guard) that turns a missing cache entry into a
    `"review"` document outcome with the miss's own text in `reason` — never an
    exception that escapes to here (a `CacheMiss` message never matches the transient-
    error regex, so `_check_retry` never re-raises it as `_RetryLater`). A case scored
    against a genuinely stale/incomplete OFFLINE cache therefore still shows up as a
    normal scoring FAILURE (`outcome` mismatch), not a crash; `--dump` surfaces the
    swallowed reason for diagnosis. A genuine TRANSIENT failure (realistic during
    `--live`) is a different exception shape (`_RetryLater`) and is caught inside
    `run_case` itself — see its own docstring.
    """
    cases = load_manifest(manifest_path)
    sid = snapshot_id or dl_snapshot.latest_snapshot_id(conn)
    if not sid:
        raise RuntimeError("no DL catalog snapshot — import one before evaluating")
    client = llm.from_config(cfg, offline=offline)
    results = []
    for n, case in enumerate(cases, 1):
        log.info("dl case %d/%d %s (%s)", n, len(cases), case.get("id"), case.get("type", "?"))
        results.append(run_case(conn, cfg, case, sid, client=client))
    summary = summarize(results)
    log.info("dl evaluation: %d/%d passed; per type: %s", summary["total"]["passed"],
             summary["total"]["cases"], summary["by_type"])
    return results, summary


__all__ = ["Result", "Score", "load_manifest", "new_baseline", "regressions", "run_case",
           "run_corpus", "score", "summarize", "SYNTHETIC_MANIFEST"]
