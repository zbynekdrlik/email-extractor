"""Extra-content detection for static orders (#133 "ZMENA ROZHODNUTIA", 2026-08-05).

n8n's original LLM branch was USEFUL — it caught cases where a customer wrote something
extra into a template order mail (a different delivery place, a date/quantity change, a
question, a complaint). The defect was calibration, not the idea: it fired an Odoo
notification on almost every mail. This module fixes the calibration, in two stages:

1. **Deterministic pre-filter** (`residual_text`): once the recognized template — the
   header fields and the item block `static_parse.py`'s own parsers already extract — is
   subtracted from the raw mail text, whatever remains is candidate "extra" content. A
   template-only mail (the vast majority) leaves nothing, or only boilerplate
   (signature/contact/disclaimer lines), and costs NO LLM call at all.
2. **One LLM call, only when residual text exists** (the caller wires this up): the
   model judges whether the residue is an ACTIONABLE instruction or routine noise. Only
   an actionable note reaches Odoo, quoting the residue itself (not a paraphrase).

The header/item-block regexes below are a deliberate, documented DUPLICATE of the ones
`static_parse.py`'s own parse functions use — NOT a shared refactor. Reusing this
module's own copies (rather than reaching into `static_parse.py`'s function-local regex
literals) keeps this module simple and keeps the parity-critical parser untouched;
the tradeoff is the SAME "mirror and keep in sync" discipline `static_ean.py` already
uses for its n8n JS twin. **Any change to a header/item-block pattern in
`static_parse.py` must be mirrored here too**, or a genuinely new header field would be
wrongly treated as "extra content" and needlessly trigger the LLM check.
"""
from __future__ import annotations

import re
from pathlib import Path

PROMPTS = Path(__file__).with_name("prompts")

# Minimum residual length (stripped, whitespace-collapsed) worth a paid LLM call. A
# stray leftover word or punctuation fragment from an imperfect boilerplate strip is
# noise, not a customer's genuine addition.
MIN_RESIDUAL_CHARS = 8

SCHEMA = {
    "type": "object",
    "properties": {
        "actionable": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["actionable"],
}

# Mirrors the header-field regexes `static_parse.py`'s own functions use — see this
# module's docstring for why these are a deliberate, documented duplicate.
_HEADER_PATTERNS = [
    re.compile(r"Vy[šs]l[áa] objedn[áa]vka [čc]\.:\s*\d+\s*/\s*\d+"),
    re.compile(r"OBJEDN[ÁA]VKA [čc]\.:\s*\d+", re.I),
    re.compile(r"Term[íi]n dod[áa]vky:\s*\d{2}\.\d{2}\.\d{2,4}"),
    re.compile(r"Term[íi]n dodania:\s*\d{2}\.\d{2}\.\d{2,4}"),
    re.compile(r"D[áa]tum dodania tovaru\s*:?\s*\d{2}\.\d{2}\.\d{2,4}"),
    re.compile(r"D[áa]tum vystavenia:\s*\d{2}\.\d{2}\.\d{2,4}"),
    re.compile(r"Prev\.:\s*\d+"),
    re.compile(r"KARMEN\s+\d+,\s*[^\n]+"),
    re.compile(r"Term[íi]n dod[áa]vky:[^\n]*\n(?:KOMFOS[^\n]+)"),
    re.compile(r"prev[áa]dzka:\s*KARMEN CASH AND CARRY\s+[^\n]+", re.I),
    re.compile(r"KARMEN CASH AND CARRY[^\n]*\n[^\n]+\n[^\n]+", re.I),
    re.compile(r"LABA[ŠS]\s+s\.r\.o\.\s+KS/OC\s+.+?(?:MOBIL|E[‐\-]?mail|TEL)", re.I),
    re.compile(r"LABA[ŠS]\s+s\.r\.o\.\s+KS/OC\s+[^\n]+", re.I),
]

# Mirrors the item-block section bookends `parse_vysla_items`/`parse_karmen_cash_items`/
# `parse_labas_items` search for — the WHOLE matched span (bookends + every item/
# description line in between) is consumed as one blob, which is what correctly handles
# the two-physical-lines-per-item KARMEN_CASH/LABAS layouts without needing a separate
# per-line item classifier.
_ITEM_BLOCK_PATTERNS = [
    re.compile(r"Mno[žz]stvo\s*\n[\s\S]*?\n(?:N[áa]kupn[áa] cena spolu)"),
    re.compile(r"Katal[óo]gov[ée] [čc][íí]slo[^\n]*\n[\s\S]*?\n"
              r"(?:Pozn[áa]mka:|Rekapitul[áa]cia:)"),
    re.compile(r"Celkom\s*\n[\s\S]*?\n(?:Celkov[áa] hmotnos|Celkov[áa] suma)"),
]

# Boilerplate LINES (signature, contact, legal disclaimer, decorative separators) that
# survive outside the header/item spans above — stripped so a routine footer never looks
# like a customer addition.
_BOILERPLATE_LINE_PATTERNS = [
    re.compile(r"^S\s+pozdravom\b", re.I),
    re.compile(r"^Vaš[a]\b", re.I),
    re.compile(r"tel\.?:?\s*[+\d]", re.I),
    re.compile(r"e-?mail:?\s*\S+@\S+", re.I),
    re.compile(r"^-{2,}\s*$"),
    re.compile(r"T[áa]to\s+spr[áa]va\s+bola\s+vygenerovan", re.I),
    re.compile(r"^I[ČC]O:?", re.I),
    re.compile(r"^DI[ČC]:?", re.I),
    re.compile(r"^I[ČC]\s+DPH:?", re.I),
    re.compile(r"^www\.", re.I),
    re.compile(r"^https?://", re.I),
]

# A sign-off marker starts the SIGNATURE BLOCK — everything from here to the end of the
# mail (sender's name, company name repeated, contact details) is boilerplate as a whole,
# not just the marker line itself. Truncating here BEFORE the per-line filter below is
# what correctly drops a signer's bare name or a repeated company name — neither of those
# lines matches any single boilerplate pattern on its own.
_SIGNOFF_PATTERNS = [
    re.compile(r"^S\s+pozdravom\b", re.I | re.M),
    re.compile(r"^S\s+úctou\b", re.I | re.M),
    re.compile(r"^[ĎD]akujem(?:e)?\b", re.I | re.M),
]


def _truncate_at_signoff(text: str) -> str:
    for pattern in _SIGNOFF_PATTERNS:
        m = pattern.search(text)
        if m:
            text = text[:m.start()]
    return text


def _strip_consumed(text: str) -> str:
    spans: list[tuple[int, int]] = []
    for pattern in (*_HEADER_PATTERNS, *_ITEM_BLOCK_PATTERNS):
        m = pattern.search(text)
        if m:
            spans.append(m.span())
    if not spans:
        return text
    spans.sort()
    out = []
    cursor = 0
    for start, end in spans:
        if start > cursor:
            out.append(text[cursor:start])
        cursor = max(cursor, end)
    out.append(text[cursor:])
    return "".join(out)


def residual_text(text: str) -> str:
    """Whatever remains of `text` once the recognized template (header fields + the
    item block), the trailing signature block, and common boilerplate lines are removed.
    "" when nothing meaningful is left — the common case for a template-only mail."""
    remainder = _strip_consumed(_truncate_at_signoff(text or ""))
    kept = []
    for raw in remainder.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if any(p.search(line) for p in _BOILERPLATE_LINE_PATTERNS):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def has_meaningful_residual(residual: str) -> bool:
    stripped = re.sub(r"\s+", "", residual or "")
    return len(stripped) >= MIN_RESIDUAL_CHARS


def prompt() -> str:
    return (PROMPTS / "static_extra_content.md").read_text(encoding="utf-8")
