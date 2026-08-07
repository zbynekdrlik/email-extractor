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
    """Cards to total quantity — the same card twice ADDS UP, mirrors `evaluate._items_map`."""
    out: dict[str, float] = {}
    for i in items or []:
        out[str(i.get("gtin"))] = out.get(str(i.get("gtin")), 0.0) + float(i.get("quantity") or 0)
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


def score(expected: dict, actual: dict) -> Score:
    """Compare one case's outcome against `dl_worker._process_message`'s return.

    Documents are matched by `doc_number` — sequence is not a difference (extraction may
    emit attachments' documents in any order), but a missing document, an extra one, and a
    wrong item inside the SECOND document of a multi-document mail all are.
    """
    problems: list[str] = []
    if "status" in expected and expected["status"] != actual.get("status"):
        problems.append(f"iný stav správy: čakáme {expected['status']!r}, "
                        f"dostali {actual.get('status')!r}")

    got_docs = list(actual.get("documents") or [])
    want_docs = expected.get("documents") or []
    matched: set[int] = set()
    hits = wanted = 0
    for wd in want_docs:
        num = wd.get("doc_number", "")
        found = None
        for i, d in enumerate(got_docs):
            if i in matched:
                continue
            if (d.get("doc_number") or "") == num:
                found = d
                matched.add(i)
                break
        h, w = _score_document(wd, found, problems)
        hits += h
        wanted += w

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
    """Run one case through the REAL DL pipeline, with every side effect disabled."""
    catalog = dl_snapshot.load_catalog(conn, snapshot_id)
    suppliers = dl_snapshot.load_suppliers(conn, snapshot_id)
    inert = _inert(cfg)
    message = dict(case["email"])
    message.setdefault("message_id", f"eval-{case['id']}")
    message.setdefault("attempts", 0)
    attachments = [_decode_attachment(a) for a in case.get("attachments") or []]
    result = dl_worker._process_message(       # noqa: SLF001 — the shared shadow-safe entrypoint
        conn, inert, client, message, snapshot_id, catalog, suppliers, shadow=True,
        upload=_refuse_upload, post=_refuse_post, attachments=attachments)
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
    exception that escapes to here. A case scored against a genuinely stale/incomplete
    cache therefore still shows up as a normal scoring FAILURE (`outcome` mismatch),
    not a crash; `--dump` surfaces the swallowed reason for diagnosis.
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
