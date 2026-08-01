# Autopilot log

Terse per-ticket record: issue #, commit SHAs, RED→GREEN test names, decisions, shared PR #.

## 2026-08-01 — batch #36 + #41 + #87 (PR #107)

- **#36** (Static generator: add EAN for KOMFOS 'CHLIEB 500g PSEN.RAZNY'): n8n-only, no repo
  diff. Added `'CHLIEB 500g PSEN.RAZNY SLOVNORMAL': '8588001805623'` to `PRODUCT_EAN_BY_NAME`
  in the live `O8IYhUESjaWmPMTI` workflow's `generator` node. Verified via re-fetch +
  Node functional test. Closed directly (not via PR `Closes`).
- **#41** (invisible Unicode chars at ingest, AGEL "45​ks" incident class): two halves.
  (a) n8n-only: `extractor` node in `O8IYhUESjaWmPMTI` gets a `cleanInvisible()` pass over
  `inputText` before any parser runs (same codepoint set as Python's `ZERO_WIDTH`). Verified
  via re-fetch + Node functional test. (b) repo: `app/process.py::_combined_text` strips the
  same invisible-format codepoints. RED `e0ace09` (`test_combined_text_strips_zero_width_space`,
  `tests/test_process.py`) → GREEN `7dd817d`. Closed via PR #107 `Closes #41`.
- **#87** (eval_run --sample N + --live price estimate): `app/orders/eval_run.py` gets
  `select_sample()` (deterministic, one case per distinct manifest `type`, then fills by
  manifest order) and `_estimated_cost_usd()` (scaled from the measured $4.50/30-case
  baseline via `llm.PRICES`). Commits `47bb149` (feature), `2e9b6a1` + `fdbaab4` (deep-review
  fixes: unpriced-model must raise, tempfile write must be inside the cleanup try/finally).
  Tests in `tests/test_eval.py`. Closed via PR #107 `Closes #87`.
- Version bump `bb17403`: 0.9.12 → 0.9.13.
- Shared PR: **#107** — merged `4b2ba23`, main CI green, deployed + verified (dashboard shows
  `v0.9.13`, `/health` reports `{"ok":true,"version":"0.9.13"}`, 0 console errors).
- Filed **#108** (follow-up, out of scope): hardcoded `Authorization` headers on 3 HTTP nodes
  in the same n8n workflow, discovered incidentally while editing it for #36/#41.
- Decision: kept the invisible-char defense LOCAL in `app/process.py` (not imported from
  `app/orders/extract.py`) — core ingest must not depend on the `orders/` feature subpackage.
