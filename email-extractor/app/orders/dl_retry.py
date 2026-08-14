"""DL worker — transient-failure classification + ORION-landed check (#239)."""
from __future__ import annotations

import logging
import re

from . import desadv, desadv_edi

log = logging.getLogger("orders.dl_worker")

TRANSIENT_RETRY_LIMIT = 3         # R17/W9: attempts 1-2 retry, 3-4 go to review

# R17: the SAME transient-failure phrase list n8n's own "Retry transient?" node uses —
# deliberately no bare digits (a reason routinely carries a money amount, e.g. the R51
# money-gate breach text, which must never be misread as "transient").
TRANSIENT_RE = re.compile(
    r"service failed to process|timed out|timeout|rate limit|too many requests|"
    r"overloaded|do[cč]asn[yý] v[yý]padok|service unavailable|internal server error|"
    r"bad gateway|econnreset|socket hang up", re.IGNORECASE)


class _RetryLater(Exception):
    """Internal signal only — a transient LLM/vision failure within R17's retry window
    (attempts < 3). Caught by the live-engine branch of `tick()`; never escapes this
    module."""


def _is_transient(message: str) -> bool:
    return bool(TRANSIENT_RE.search(message or ""))


def _check_retry(attempts: int, error: str) -> None:
    """Raises `_RetryLater` when this failure is transient AND still within its retry
    window — the caller's `except` blocks let it propagate all the way up to `tick()`,
    aborting the rest of THIS message's processing (matches n8n's own retry granularity:
    `Retry transient?` operates on the whole claimed message, not a sub-document)."""
    if _is_transient(error) and int(attempts or 0) < TRANSIENT_RETRY_LIMIT:
        raise _RetryLater(error)


def _check_landed(conn, cfg, list_dirs, ean_edi: str, doc_number: str) -> bool | None:
    """#239 finding 6 (remainder): after a TRANSIENT upload failure, is the document
    already on ORION under an EARLIER attempt's name — the "bytes landed, only the
    reply was lost" case `upload.put()`'s temp-write+rename makes provable
    (`desadv_edi.already_landed()`, keyed on the document's STABLE identity, never a
    filename)? Returns `True`/`False` when the check itself succeeded AND (review
    finding on this ticket's own PR) is genuinely trustworthy, `None` when it could not
    even be attempted — most likely the SAME SFTP connection that just failed the
    upload is down too, right after failing on it — OR when a presence match was found
    but is NOT trustworthy (`desadv.has_confirmed_collision`: a different, already-
    confirmed document from the same supplier shares the same truncated stable prefix,
    see that function's own docstring). The caller treats `None` exactly like the
    pre-finding-6 behaviour: no retry, straight to the durable alert — a blind retry
    (or a blindly-trusted false-positive presence match) is exactly the v0.9.70
    duplicate-delivery incident this whole ticket exists to prevent, just possibly in
    the opposite direction (silent loss instead of silent duplication).

    Review finding on this ticket's own PR: `already_landed()` itself must be inside
    the SAME try/except as `list_dirs(cfg)` — an earlier draft only guarded the SFTP
    listing call, so an exception from `already_landed()` (e.g. a malformed `dirs`
    shape) would propagate uncaught out of the whole upload except-block in
    `_process_document`, skipping `_alert_and_release` entirely and leaving the claim
    held with no alert ever raised."""
    try:
        dirs = list_dirs(cfg)
        landed = desadv_edi.already_landed(dirs, ean_edi, doc_number)
    except Exception:
        log.warning("DL upload retry: could not check ORION presence for supplier=%s "
                   "doc=%s — no safe retry possible", ean_edi, doc_number, exc_info=True)
        return None
    if landed and desadv.has_confirmed_collision(conn, ean_edi, doc_number):
        log.warning(
            "DL upload retry: stable-identity presence match for supplier=%s doc=%s "
            "collided with a DIFFERENT already-confirmed document sharing the same "
            "10-char prefix — refusing to trust it, falling back to the safe alert "
            "path instead of confirming the wrong document", ean_edi, doc_number)
        return None
    return landed
