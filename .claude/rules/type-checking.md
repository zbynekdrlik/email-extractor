---
paths:
  - "email-extractor/pyproject.toml"
  - "email-extractor/requirements-dev.txt"
---

# mypy type-check gate (#270) — how it's configured and how to fix a failure cleanly

A blocking `typecheck` CI job runs `mypy` on every push/PR (`.github/workflows/ci.yml`,
in `build`'s `needs`). Config: `pyproject.toml` `[tool.mypy]`, `mypy==2.3.0` pinned in
`requirements-dev.txt`. Baseline is **pragmatic gradual-typing**: real type errors FAIL,
but `disallow_untyped_defs` stays OFF (legacy `def f(conn, ...)` is allowed). Run it
locally exactly as CI does: `cd email-extractor && .venv/bin/python -m mypy` (no args —
it reads `files = ["app"]` from the config).

## When you touch the mypy config

- **Silence a stub-less third-party lib CHIRURGICALLY, never globally.** The
  `[[tool.mypy.overrides]]` `module = [...]` list is EXACTLY the set of directly-imported
  libs that ship no stubs (`imapclient, odf.*, openpyxl(.*), paramiko(.*), pytesseract,
  waitress(.*), xlrd`). NEVER add `ignore_missing_imports = true` at top level — that would
  also hide a typo in our OWN internal import (`import-not-found`). Add a new dep to the
  list ONLY after confirming app imports it directly AND it lacks `py.typed`
  (`grep -rn 'import <lib>' app/` + check for a `py.typed` in its installed package).
  `python-dateutil`/`lxml` are in `requirements.txt` but app imports NEITHER directly, so
  they are correctly absent from the override — don't add a lib "just because it's a dep".
- `check_untyped_defs = true` adds 0 errors today (≈every signature is already annotated,
  so mypy checks nearly all bodies by default) — it's a FORWARD guard for future
  unannotated functions. Keep it.
- **No env-divergence risk to worry about:** the `.venv` carries no stray `types-*` stub
  packages, so a clean local `mypy` guarantees a clean CI `mypy`.

## Fixing a mypy finding WITHOUT suppression (the #270 playbook — no `# type: ignore`)

Every finding is a real bug OR a legitimately-typed fix. The recurring shapes here:

- **Lost narrowing across a function boundary.** `match.py`/`dl_match.py` hold the invariant
  "`llm_gtin` is truthy ⟹ `llm_card` is not None" (the `unknown_gtin`/`gtin_overflow` blocks
  reset `llm_gtin, llm_card = None, None` together), but mypy can't see it through
  `_weights_disagree`/`_is_unmatched`. Fix with an explicit `assert llm_card is not None`
  that DOCUMENTS the real invariant — never a `# type: ignore`. Before adding an assert,
  PROVE `None` genuinely cannot reach it on any path (else you turn a latent wrong-answer
  into a crash).
- **Too-strict annotation vs the real contract.** Widen to match the body: `snapshot_id: int`
  → `int | None` when the column is nullable and a caller passes `None`; `cnt: int` →
  `int | None` when the body defensively coerces `None`; `value: str` → `str | None` when it
  does `str(value or "")`. The widened contract must be what the body ACTUALLY handles.
- **A re-fetched `.get()` is never narrowed.** `float(llm.get("x") if llm.get("x") is not None
  else ...)` fails because the two `.get("x")` calls are distinct expressions — bind to a
  local first (`raw = llm.get("x"); if raw is None: raw = ...`), then narrowing works.
- **`fetchone()[0]` on a TYPED connection** (`psycopg.connect(...)` context manager, not an
  untyped `conn` param) is `tuple[Any,...] | None` → not indexable. `row = ...fetchone();
  x = row[0] if row else 0` (a `count(*)` always returns a row, so the else is a safe no-op).
- **A dead defensive branch costs coverage.** Prefer `g = grams or 0` over
  `if grams is None: return "?"` — the former keeps the line covered; the latter adds an
  uncovered branch that can push a file under `--cov-fail-under=85`.

## A one-off operator script (coverage-`omit`ed) rots silently — mypy is its only guard

`app/backfill.py` (in `[tool.coverage.run]` `omit`) called `store.save_message(...)` with 6
args after the function dropped to 5 — a guaranteed `TypeError`, swallowed by backfill's own
per-message `try/except`, so it silently processed 0 messages and nobody noticed (no test, no
coverage). mypy now catches this class. When fixing such a script, a genuine RED→GREEN test is
still possible without a live IMAP/DB: `mock.patch("...save_message", autospec=True)` pins the
REAL signature (a wrong-arity call raises `TypeError`), mock the rest of `main()`, and assert a
downstream call (`db.insert_message`) is reached — it fails at the buggy commit, passes fixed.

## A mypy fix can INTRODUCE a ruff error — always re-run `ruff check .` after the mypy fix (#314)

Fixing a mypy "expression has type object" on a reused loop variable by QUOTING a forward-ref
annotation (`unmatched_asks: list[tuple[dict, "dl_memory.Recalled | None", str]]`) satisfies
mypy but trips ruff **UP037** ("Remove quotes from type annotation") — because the module has
`from __future__ import annotations` and imports `dl_memory` at top, so the quotes are
unnecessary (the unquoted `dl_memory.Recalled | None` is the correct form). This slipped
through a LOCAL `ruff check app/ tests/` that ran BEFORE the mypy fix and was never repeated,
then failed CI (CI runs `ruff check .` from `email-extractor/`, the whole tree). Two lessons:
(1) after ANY mypy-driven annotation change, re-run `ruff check .` (the CI form, from
`email-extractor/`) — not just the per-file lint you ran earlier; (2) a reused loop variable
across two loops that mypy types as `object` from a `tuple[..., object, ...]` is better fixed
by the CORRECT element type (`tuple[dict, dl_memory.Recalled | None, str]`, unquoted) than by
`object` + a quote, which fixes mypy but fails ruff. The `test` CI job runs ruff FIRST (before
pytest), so a ruff slip fails the whole job before any test runs — cheap to catch locally,
one wasted CI cycle if not.
