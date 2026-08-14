"""Characterization test for `dl_worker`'s public/observable API surface (#309).

`dl_worker.py` is being split along responsibility boundaries into several sibling
modules (`dl_events`/`dl_retry`/`dl_correction`/`dl_matching`/`dl_document`/
`dl_message`/`dl_questions`), with `dl_worker.py` kept as a thin FACADE that
re-exports every symbol other modules and the test suite reach via `dl_worker.X`.

This test PINS that observable surface so the pure code-move can be checked
byte-for-byte at the API level, not just behaviourally — exactly the #268
`test_httpapi_characterization.py` role for the httpapi split. It must pass
UNCHANGED before AND after the split: before, every name is defined directly in the
monolith; after, every name is re-exported from its new home. A split step that
forgets to re-export a symbol (or breaks the `dl_worker.dl_extract` monkeypatch
reach-through the DL tests rely on) fails HERE, loudly, instead of surfacing as a
confusing `AttributeError` deep inside an unrelated test.

Deliberately DB-free and side-effect-free: it only imports the module and inspects
its namespace, so it adds no fixtures and touches no Postgres.
"""
from __future__ import annotations

import re

from app.orders import dl_extract, dl_worker

# Every callable the rest of the app OR the test suite reaches through `dl_worker.X`
# (public entry points + private helpers referenced by name in tests/other modules),
# plus the full set of internal helpers, so a dropped re-export is caught immediately.
EXPECTED_CALLABLES = [
    # public entry points (worker loop, httpapi, teach, human_processing, reliability)
    "tick",
    "refresh_due",
    "stuck_classified_sweep",
    "release_for_question",
    "close_message_not_warehouse",
    "close_message_sklad_unknown",
    "resolve_engine",
    "_read_attachments",
    # correction detection (#265) — referenced directly by tests
    "_looks_like_correction",
    "_correction_review_reason",
    "_mail_body_only",
    # message lifecycle / selection / aggregation
    "_as_message",
    "_claim",
    "_peek_for_shadow",
    "_subject_doc_numbers",
    "_aggregate_status",
    "_summary_outcome",
    "_process_message",
    "_run_and_finish",
    # per-document pipeline
    "_process_document",
    "_shipped_items",
    "_document_has_catalog_match",
    "_skip_not_warehouse",
    # matching glue
    "_match_supplier",
    "_match_item",
    "_supplier_prompt",
    "_item_prompt",
    "_supplier_input",
    "_item_input",
    # reporting / event helpers
    "_event",
    "_post",
    "_num",
    "_flag_attachment",
    # retry / landed (#239)
    "_is_transient",
    "_check_retry",
    "_check_landed",
    # sibling release (#265)
    "_release_stuck_siblings",
]

# Non-callable module-level symbols reached through `dl_worker.X` (constants, schemas,
# compiled regexes, the internal exception class).
EXPECTED_ATTRS = [
    "CATEGORY",
    "CLAIM_STALE_MINUTES",
    "MAX_ATTEMPTS",
    "TRANSIENT_RETRY_LIMIT",
    "SHADOW_DAYS",
    "STUCK_CLASSIFIED_MINUTES",
    "_STUCK_SIBLING_LIMIT",
    "_BODY_TEXT_IDX",
    "_CORRECTION_EXCERPT_LIMIT",
    "_COMBINED_TEXT_ATTACHMENTS_MARKER",
    "PROMPTS_DIR",
    "TRANSIENT_RE",
    "SUPPLIER_SCHEMA",
    "ITEM_SCHEMA",
    "_RetryLater",
    "_SUBJECT_DOC_RE",
    "_CORRECTION_STRONG_RE",
    "_CORRECTION_DOPLN_SUBJECT_RE",
    "_ATTACHMENT_MIME_RE",
    "_ATTACHMENT_EXT_RE",
    "_ATTACHMENT_SPREADSHEET_EXT_RE",
    "_ATTACHMENT_SPREADSHEET_MIME_RE",
]


def test_every_callable_is_reachable_and_callable():
    for name in EXPECTED_CALLABLES:
        assert hasattr(dl_worker, name), f"dl_worker.{name} is missing from the facade"
        assert callable(getattr(dl_worker, name)), f"dl_worker.{name} is not callable"


def test_every_module_level_attr_is_reachable():
    for name in EXPECTED_ATTRS:
        assert hasattr(dl_worker, name), f"dl_worker.{name} is missing from the facade"


def test_dl_extract_monkeypatch_reach_through_is_preserved():
    # The one attribute the DL tests monkeypatch is `dl_worker.dl_extract.extract_email`
    # (test_dl_worker.py). That only works because `dl_worker.dl_extract` IS the shared
    # `app.orders.dl_extract` module object — patching the attribute on it reaches every
    # split module that calls `dl_extract.extract_email(...)`. Pin that identity so the
    # split can never quietly rebind the name to something else.
    assert dl_worker.dl_extract is dl_extract


def test_RetryLater_is_an_exception_subclass():
    assert issubclass(dl_worker._RetryLater, Exception)


def test_behaviour_relevant_constants_have_their_exact_values():
    # These constants encode real behaviour (R10/R11/R17 windows, ORION category, the
    # #258 synthetic body-text idx). Pinning their exact values proves the move carried
    # them byte-for-byte, not just that a same-named symbol exists.
    assert dl_worker.CATEGORY == "dodacie_listy"
    assert dl_worker.CLAIM_STALE_MINUTES == 30
    assert dl_worker.MAX_ATTEMPTS == 5
    assert dl_worker.TRANSIENT_RETRY_LIMIT == 3
    assert dl_worker.SHADOW_DAYS == 3
    assert dl_worker.STUCK_CLASSIFIED_MINUTES == 30
    assert dl_worker._STUCK_SIBLING_LIMIT == 20
    assert dl_worker._BODY_TEXT_IDX == -1
    assert dl_worker._CORRECTION_EXCERPT_LIMIT == 500
    assert dl_worker._COMBINED_TEXT_ATTACHMENTS_MARKER == "\n\nAttachments:\n"
    assert dl_worker.resolve_engine("python") == "python"


def test_correction_and_attachment_regexes_still_compile_and_match():
    # Cheap sanity that the compiled patterns survived the move intact (a subtle
    # copy error in a regex string would slip past a mere `hasattr` check).
    assert isinstance(dl_worker.TRANSIENT_RE, re.Pattern)
    assert dl_worker._CORRECTION_STRONG_RE.search("OPRAVA HMOTNOSTI")
    assert dl_worker._CORRECTION_DOPLN_SUBJECT_RE.search("DOPLŇUJÚCE")
    assert dl_worker._ATTACHMENT_EXT_RE.search("dodaci_list.pdf")
    assert dl_worker._SUBJECT_DOC_RE.search("2610LT0100000001")
