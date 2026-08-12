---
paths:
  - "email-extractor/app/httpapi.py"
  - "email-extractor/app/httpapi_*.py"
  - "email-extractor/tests/test_httpapi_characterization.py"
---

# Characterization tests protecting the `app/httpapi.py` split (#268)

`app/httpapi.py` (2693 lines pre-split) is being split into 9 modules across an
11-step chain (full plan: issue #268's own comment thread). Step 1 added
`tests/test_httpapi_characterization.py` — three tests that pin CURRENT behaviour so a
later step's pure-relocation move can be checked byte-for-byte, not just
behaviourally. Read this before touching either the split OR the characterization
tests themselves.

## Checksum a set of derived/formatted string constants, not the raw template too

When several page constants are built from one shared raw template via `.replace()` /
`.format()` (here: `ASK_HTML`/`ASK_DL_HTML` from `_ASK_HTML_TEMPLATE`), hash the
DERIVED constants, not the raw template as a fourth/extra entry. The derived form
strictly contains the template's own content (any template mutation changes every
derived hash too) **and** additionally catches a bug in the formatting chain itself
(a mistyped placeholder that leaves `__TITLE__` literally in the response — the
template-only hash would miss this). Hashing the template on top is pure double-
counting of the same bytes for zero extra protection. If a future split step adds a
new derived-from-shared-template constant, hash the derivative, not the source.

## Route-table characterization via `app.url_map.iter_rules()`

Exclude Flask's built-in `static` endpoint (`r.endpoint == "static"`) — it's registered
regardless of how `create_app()` is written and carries no split-related signal.
Normalize away `HEAD`/`OPTIONS` from `r.methods` (Flask adds them automatically) so the
comparison is stable, not brittle. Recompute the expected list from the LIVE app
(`.venv/bin/python -c "... app.url_map.iter_rules() ..."`) rather than transcribing it
by hand from source — a hand-transcribed list is exactly the kind of thing this test
exists to catch errors in.

## Proving a characterization test can genuinely fail — perturb the SPECIFIC thing it protects

Before trusting a new characterization test, prove it can fail: back up the file
first (`cp app/httpapi.py /tmp/.../httpapi.py.bak`), make the SMALLEST realistic
mutation of the exact thing the test protects (a route path typo like
`dl-products` -> `dl_products`, ONE character in an HTML constant, a swapped
today/yesterday query), run ONLY that one test, capture the failure output, revert the
mutation, then `diff` the file against the backup to confirm a byte-identical revert
BEFORE committing anything. `diff a b && echo IDENTICAL` is cheap and removes any doubt
that the revert left a stray change behind. Do this for every new characterization
test in a future split step — the whole point of these tests is that they can fail;
one that structurally cannot is worthless.

## The `_db_tx`/`_db` occurrence counts in the #268 plan comment are approximate

The plan's own comment states "42× `with _db(`, 8× `with _db_tx(`" — a live `grep -c`
against `bd9e28e`/HEAD found 43/9 (one more of each). Harmless for step 1 (no test uses
a hardcoded count), but re-`grep` before relying on either number in a later step
rather than trusting the plan comment's arithmetic.
