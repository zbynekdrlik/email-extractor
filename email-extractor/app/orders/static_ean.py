"""EAN resolution for the n8n **Static auto orders** workflow's `generator` node (#49).

The production logic lives in n8n (JS, workflow `O8IYhUESjaWmPMTI`, node `generator`)
— n8n's JS Code node cannot run in this repo's CI, so this module is a hand-kept 1:1
PORT of that node's `getProductEAN()` catalog-lookup half, and is what CI actually
tests. Mirror any change here in the n8n Code node (and vice versa).

Before this module, an unresolved item fell back to a hand-maintained name list
(`PRODUCT_EAN_BY_NAME` in generator.js) that had to be edited by hand for every new
wording — the 2026-07-28 KARMEN incident (order 020260851324, code P000534 = "Pečivo
toskánske 60g Granč") was the same product already in the list under code 9098, just
a new own-code. This module instead resolves against the catalog snapshot
(`catalog_snapshot` table, the same source `AI auto orders` uses — see
`app.orders.snapshot`, #59) that used to be the only fallback. The manual maps stay as
an OVERRIDE for genuine exceptions and are still checked first (see `resolve_ean`).

Matching mirrors `app.orders.match`'s "gramáž ako identita" rule (a differing weight is
a different product — never guessed) rather than the full scored ladder: this is a
single, unambiguous lookup with no model call, not a scored ranking. Anything the
lookup cannot decide without guessing returns None; the caller (generator.js) already
reports that via `itemsSkipped` -> Odoo (`Has skipped items?` -> `Odoo Skip Alert`), so
a line this module cannot resolve is surfaced, never silently dropped.
"""
from __future__ import annotations

import re
import unicodedata

# Same tolerance as app.orders.match.WEIGHT_TOLERANCE — kept as its own constant since
# this module has no dependency on match.py (the n8n JS mirror has none either).
WEIGHT_TOLERANCE = 0.1


def normalize_name(text: str) -> str:
    """Fold diacritics, uppercase, collapse whitespace.

    Mirrors generator.js's `toAscii(x).toUpperCase().replace(/\\s+/g, ' ')`.
    """
    s = unicodedata.normalize("NFD", str(text or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s.upper()).strip()


def weight_grams(name: str) -> float | None:
    """Weight stated in a name, in grams. A multipack ("6x1l") is not a unit weight."""
    s = str(name or "")
    if re.search(r"\d+\s*[x×*]\s*[\d.,]+\s*(kg|g|l|ml)\b", s, re.I):
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(kg|gr|g)\b", s, re.I)
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    return value * 1000 if m.group(2).lower() == "kg" else value


def _weights_agree(a: float | None, b: float | None) -> bool:
    """No stated weight on either side is missing information, not a disagreement."""
    if not a or not b:
        return True
    ratio = a / b
    return 1 - WEIGHT_TOLERANCE <= ratio <= 1 + WEIGHT_TOLERANCE


def _alias_parts(alias: str) -> list[str]:
    return [normalize_name(p) for p in re.split(r"[,;/]+", str(alias or "")) if p.strip()]


def _core_tokens(name: str) -> set[str]:
    """Words that make up a name once the weight and short filler words are stripped —
    order-independent, so "Toskánske pečivo" and "Pečivo toskánske" are the same core.
    Mirrors `app.orders.match._core_tokens`."""
    folded = normalize_name(name)
    stripped = re.sub(r"\d+(?:[.,]\d+)?\s*(KG|GR|G|ML|L)\b", " ", folded)
    return {w for w in re.split(r"[^A-Z0-9]+", stripped) if len(w) >= 3}


def catalog_match(description: str, catalog: list[dict]) -> dict | None:
    """The single catalog card `description` names, or None when it cannot be decided
    without guessing.

    Rungs, each requiring a UNIQUE hit — an ambiguous match (several cards fit) is
    reported as None, exactly like no hit at all, never resolved by picking one:

      1. exact catalog name match (diacritics/case folded)
      2. exact alias match (the `alias` column is a comma/semicolon list of curated
         customer wordings for that card — same shape as `app.orders.match.alias_parts`)
      3. unambiguous CORE-TOKEN match (order-independent word-set containment — a
         partner may write "Pečivo toskánske" for our "Toskánske pečivo") whose stated
         weight agrees with the ordered wording's weight. Needs 2+ core tokens in the
         ordered wording — a single generic word ("croissant") is not specific enough
         to resolve without guessing which flavour, so it is left unmatched instead.
    """
    if not description or not catalog:
        return None
    want = normalize_name(description)
    if not want:
        return None
    want_w = weight_grams(description)

    exact = [c for c in catalog if normalize_name(c.get("name", "")) == want]
    if len(exact) == 1:
        return exact[0]

    alias_hits = [c for c in catalog if want in _alias_parts(c.get("alias", ""))]
    if len(alias_hits) == 1:
        return alias_hits[0]

    want_tokens = _core_tokens(description)
    if len(want_tokens) >= 2:
        contained = []
        for c in catalog:
            card_tokens = _core_tokens(c.get("name", ""))
            if not card_tokens:
                continue
            if card_tokens <= want_tokens or want_tokens <= card_tokens:
                if _weights_agree(want_w, weight_grams(c.get("name", ""))):
                    contained.append(c)
        if len(contained) == 1:
            return contained[0]

    return None


def resolve_ean(item: dict, catalog: list[dict],
                 code_map: dict[str, str] | None = None,
                 name_map: dict[str, str] | None = None) -> str | None:
    """Resolve one order item to a GTIN/EAN.

    Same precedence as generator.js's `getProductEAN`: EAN on the document wins over
    everything, then the manual code map, then the manual name map — both are the
    explicit OVERRIDE for genuine exceptions (#49) — and only then the catalog lookup.
    Returns None when nothing above resolves it; the caller must report that, never
    drop the line silently.
    """
    if item.get("ean"):
        return item["ean"]

    code_map = code_map or {}
    name_map = name_map or {}

    code = item.get("code")
    if code and code in code_map:
        return code_map[code]

    description = item.get("description") or ""
    if description:
        d = normalize_name(description)
        for name, ean in name_map.items():
            if normalize_name(name) in d:
                return ean

        hit = catalog_match(description, catalog)
        if hit:
            return hit.get("gtin")

    return None
