---
paths:
  - "email-extractor/app/orders/dl_worker.py"
  - "email-extractor/app/orders/dl_retry.py"
  - "email-extractor/app/orders/dl_correction.py"
  - "email-extractor/app/orders/dl_matching.py"
  - "email-extractor/app/orders/dl_events.py"
  - "email-extractor/app/orders/dl_document.py"
  - "email-extractor/app/orders/dl_message.py"
  - "email-extractor/app/orders/dl_questions.py"
  - "email-extractor/app/db_schema.py"
  - "email-extractor/app/orders/hold.py"
  - "email-extractor/app/orders/hold_place.py"
  - "email-extractor/app/orders/hold_redecide.py"
  - "email-extractor/app/orders/hold_close.py"
  - "email-extractor/tests/test_dl_worker_public_api.py"
  - "email-extractor/tests/test_hold_split_characterization.py"
---

# Splitting an oversized module by responsibility — the FACADE + byte-exact AST move (#309)

`dl_worker.py` (1911 r.) was split into a thin FACADE + 7 concern modules
(`dl_retry`/`dl_correction`/`dl_matching`/`dl_events`/`dl_document`/`dl_message`/
`dl_questions`), and `db.py` (1364 r.) shed its 1108-line `SCHEMA` into `db_schema.py`.
This is a DIFFERENT split shape than the #268 `httpapi.py` split
(`.claude/rules/httpapi-characterization.md`, a `register(app, deps)`/`Deps`-injection
split) — reuse the shape that matches the module. `dl_worker` had heavy internal
cross-calls + many private helpers referenced by tests as `dl_worker._x`, so a facade
re-export was the right choice; httpapi had route-registration, so `register()` was.

## The FACADE re-export pattern (when a module's `X.name` surface must not change)

Keep the original module as the public entry; move internals to concern modules; the
facade `from .concern import (a, b, c)  # noqa: F401 (re-export ...)` re-exports EVERY
symbol other modules OR tests reach via `module.X`. Then NO caller changes. Two things
this repo proved matter:
- **Grep BOTH shapes before trusting the facade is complete**: `grep -rn "dl_worker\."`
  (attribute access, incl. `monkeypatch.setattr(dl_worker.dl_extract, ...)`) AND
  `grep -rn "from .* import .*dl_worker"` / `from app.orders.dl_worker import`
  (bound-name imports). A missing re-export is a silent runtime `AttributeError`.
- **A monkeypatched SUBMODULE (`dl_worker.dl_extract`) must stay the SAME module
  object** — the facade does `from . import dl_extract`, and the module that actually
  calls it (`dl_message`) calls `dl_extract.extract_email(...)` as `module.attr` (never
  a `from .dl_extract import extract_email` bound name), so `monkeypatch.setattr` on the
  shared singleton reaches the call site. Pin this with the characterization test's
  `assert dl_worker.dl_extract is dl_extract`.
- **Keep `logging.getLogger("orders.dl_worker")` verbatim in every split module** — a
  per-module logger name is an observable log-output change; a zero-behaviour refactor
  keeps the name identical (post-deploy log-watching keys on it).

## The byte-exact AST assembly (never retype 1900 lines)

Drive the move with a scratch `ast` script (kept OUT of git), not by hand:
1. `ast.parse` the original; for each top-level node capture a CONTIGUOUS source chunk
   `lines[prev_end : node.end_lineno]` (leading comments/blanks attach to the FOLLOWING
   node — correct, since each `# comment` block documents the symbol beneath it).
2. Assign each node NAME → target module; emit `chunk` text verbatim per module.
3. **Auto-compute each module's imports from real CODE usage** via `tokenize` (collect
   NAME tokens, which excludes comments/strings — a `worker.tick` mention in a COMMENT
   must not pull in a `worker` import). Decide stdlib/relative/cross-module imports from
   that set; run `ruff check --fix` afterward ONLY for import-sort (I001) — never
   `ruff format` (it would reformat the moved bodies and break byte-exactness).
4. **Verify** every moved function/class: `ast.get_source_segment(orig, onode) ==
   ast.get_source_segment(new_src, nnode)` for all of them (nested closures are captured
   inside the parent segment, so a closure drift IS caught). Then `ruff check .`, an
   import-smoke `from app.orders import dl_worker` (catches cycles), and the FULL suite
   green with ZERO test edits is the behaviour-equivalence proof.
5. Add a characterization test (`test_dl_worker_public_api.py`) pinning the full `X.name`
   surface + the submodule-identity, committed BEFORE the move, and PROVE it can fail
   (delete a symbol at runtime, assert the test's assertion raises) — it must pass
   unchanged before AND after.

The DAG must be acyclic: leaves (retry/correction/matching/events) ← document ← message
← questions; facade → all. Auto-computed imports surface a cycle immediately as an
ImportError on the smoke test.

## #424: hold.py (930 r.) split the same way — one lint gotcha worth pinning

`hold.py` was split into the facade + `hold_place`/`hold_redecide`/`hold_close`
(leaf ← redecide ← close ← facade), and `_classify_manual_target(conn, qid)` was
extracted from `_api_orders_answer_item_manual` (READ-ONLY verdict → thin dispatcher,
messages byte-for-byte). Same AST-byte-exact recipe as #309.

**Lint the WHOLE tree as CI does (`ruff check .` from `email-extractor/`), not just the
files you edited.** Step 3 above says to run `ruff check --fix` for I001 on the moved
modules — but the NEW characterization test file is easy to forget, and CI runs
`ruff check .` over `tests/` too. A batch of `c = _client(); _login(c)` one-liners in
the new test tripped **E702** (multiple-statements-on-one-line) — invisible when you only
`ruff check`ed the `app/` modules, red at CI. After writing any new test file for the
split, `ruff check .` (whole tree) before the push, exactly the command CI uses.
