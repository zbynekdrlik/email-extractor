"""Is-it-an-order? classifier + safe-discard vetoes (#376).

The extractor answers "which items does this mail order?"; it has NO verdict for "is this
even an order at all?". So a Karmen inspector's forwarded infomail (no order in it) hits the
`orders == []` branch and asks the warehouse "is this an order?" — over and over, because the
learned `mail_rules(sender, subject)` rule never generalizes (the subject changes every time).

This module adds the missing verdict as a SEPARATE, gated model call (never touches
`extract.ORDER_SCHEMA`, so the shadow/e2e corpus stays byte-identical). Discarding a mail as
"not an order and not a delivery note" requires TWO independent NOs — the extractor's empty
`orders` AND this classifier's `other` verdict at high confidence — plus a wall of
deterministic vetoes (`veto_reason`). Everything here is a pure function; the pipeline decides
what to do with the result (see `pipeline._mail_kind_discard_reason`). Fail-safe is always to
ASK the warehouse, never to discard.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import dl_match

log = logging.getLogger("orders.mail_kind")

PROMPT_PATH = Path(__file__).with_name("prompts") / "classify_mail_kind.md"

# The confidence floor for an `other` verdict to even be CONSIDERED for a discard. Below it,
# the mail always goes to the warehouse question (fail-safe = ask).
NOT_ORDER_MIN_CONFIDENCE = 0.85

_KINDS = ("order", "delivery_note", "change_request", "other")

MAIL_KIND_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {"type": "string", "enum": list(_KINDS)},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["kind", "confidence"],
}


@dataclass
class MailKind:
    kind: str
    confidence: float
    reason: str = ""
    evidence: list[str] = field(default_factory=list)


def prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _input(subject: str, text: str) -> str:
    return (f"SUBJECT: {subject or ''}\n\n"
            f"--- TELO E-MAILU ---\n{text or ''}\n--- KONIEC ---")


def classify(client, subject: str, text: str) -> MailKind | None:
    """One model verdict: order / delivery_note / change_request / other.

    Returns `None` on ANY failure — an exception, a non-object result, an unknown `kind`, a
    non-numeric confidence — and logs a warning. A `None` verdict is the pipeline's signal to
    fall back to the warehouse question (gate rule 5: never discard on an unreadable verdict).
    Uses the SAME `client` (gpt-5.4) as the rest of the pipeline via `json_call`.
    """
    try:
        raw = client.json_call(prompt(), _input(subject, text), MAIL_KIND_SCHEMA,
                               name="mail_kind")
    except Exception:
        log.warning("mail-kind classifier call failed for subject %r", subject, exc_info=True)
        return None
    if not isinstance(raw, dict):
        log.warning("mail-kind classifier returned a non-object result: %r", type(raw))
        return None
    kind = str(raw.get("kind", "")).strip().lower()
    if kind not in _KINDS:
        log.warning("mail-kind classifier returned an invalid kind %r", kind)
        return None
    try:
        conf = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        log.warning("mail-kind classifier returned a non-numeric confidence %r",
                    raw.get("confidence"))
        return None
    return MailKind(kind=kind, confidence=conf,
                    reason=str(raw.get("reason", "") or ""),
                    evidence=[str(e) for e in (raw.get("evidence") or [])])


# --- deterministic vetoes: any one of these means "do NOT discard, ask the warehouse" ---
#
# Every text stem runs on DIACRITIC-FOLDED text (`dl_match.fold`), never raw: a plain-ASCII
# stem structurally cannot match its own Slovak diacritic forms (`č/ľ/ĺ/ň/í/…`) — the #265
# lesson. So fold FIRST (which lowercases + strips diacritics), then match ASCII patterns.

# Document identifiers — a printed order/DL almost always carries one. `objedn…` needs an
# actual č/no/nr/# + digit AFTER it, so a BARE word "objednávka" in prose is deliberately NOT
# a veto (Karmen "nedodaný tovar z objednávky" must stay discardable — the design's own rule).
_DOC_IDENT_RES = (
    re.compile(r"objedn\w*\s*(?:c|no|nr|#)\.?\s*\d"),   # "objednávka č./no/nr/# <n>"
    re.compile(r"dodac\w*\s+list"),                     # "dodací list" / "dodacích listov"
    re.compile(r"\bdl\s*c"),                            # "DL č."
    re.compile(r"desadv"),
    re.compile(r"av[ií]zo"),                            # "avízo" (í folds to i)
)
# A quantity followed by a warehouse unit. Two or more of these reads like a real order/DL the
# extractor missed — never discard.
_ITEM_LINE_RE = re.compile(r"\d+[\s,.]*\d*\s*(?:ks|kg|bal|kt|t|l)\b")

_STRUCTURED_ATTACHMENT_EXT_RE = re.compile(r"\.(?:xlsx|xls|csv|ods|fods)$", re.IGNORECASE)
_STRUCTURED_ATTACHMENT_MIME_RE = re.compile(
    r"spreadsheet|ms-excel|excel|csv|opendocument\.spreadsheet", re.IGNORECASE)
# A readable-document attachment whose text we depend on having actually parsed.
_DOC_ATTACHMENT_EXT_RE = re.compile(r"\.(?:pdf|docx?|xlsx?|ods|fods)$", re.IGNORECASE)
_DOC_ATTACHMENT_MIME_RE = re.compile(
    r"pdf|word|excel|spreadsheet|officedocument|opendocument", re.IGNORECASE)


def structural_veto(subject: str, text: str) -> str:
    """Non-empty reason when the folded text carries a document identifier OR >= 2 item lines.
    A bare word 'objednávka' in prose is NOT a veto (see `_DOC_IDENT_RES`)."""
    folded = dl_match.fold(f"{subject or ''}\n{text or ''}")
    for rx in _DOC_IDENT_RES:
        if rx.search(folded):
            return f"text obsahuje identifikátor dokladu ({rx.pattern})"
    if len(_ITEM_LINE_RE.findall(folded)) >= 2:
        return "text obsahuje položkové riadky (množstvo + jednotka)"
    return ""


def readability_veto(attachments) -> str:
    """Non-empty reason when ANY attachment was NOT fully read — needs_vision, or a
    pdf/docx/xlsx-shaped attachment with empty `extracted_text`. AI may only discard a mail
    whose attachments it actually SAW."""
    for a in attachments or []:
        filename = str(a.get("filename") or "")
        mime = str(a.get("mime") or "")
        if a.get("needs_vision"):
            return f"príloha {filename or '?'} čaká na AI-Vision (needs_vision)"
        is_doc = bool(_DOC_ATTACHMENT_EXT_RE.search(filename)
                      or _DOC_ATTACHMENT_MIME_RE.search(mime))
        if is_doc and not str(a.get("extracted_text") or "").strip():
            return f"príloha {filename or '?'} sa nedala prečítať (prázdny text)"
    return ""


def structured_attachment_veto(attachments) -> str:
    """Non-empty reason when ANY attachment is a spreadsheet (xlsx/xls/csv/ods/fods) — a grid
    is exactly the shape a genuine order/DL takes; never discard a mail carrying one."""
    for a in attachments or []:
        filename = str(a.get("filename") or "")
        mime = str(a.get("mime") or "")
        if _STRUCTURED_ATTACHMENT_EXT_RE.search(filename) \
                or _STRUCTURED_ATTACHMENT_MIME_RE.search(mime):
            return f"štruktúrovaná príloha ({filename or mime})"
    return ""


def veto_reason(subject: str, text: str, attachments) -> str:
    """The full deterministic wall (design rules 3 + 4). Non-empty ⟹ never discard, ask the
    warehouse. Cheap + deterministic, so the pipeline runs it BEFORE the paid classifier
    call — the gate is an AND, so the order never changes the result, it only saves a call."""
    return (readability_veto(attachments)
            or structured_attachment_veto(attachments)
            or structural_veto(subject, text))
