"""Reconstruct customer wording -> shipped card for the archived orders (#83).

The archive seeded into `item_memory` via `memory.seed_from_archive` records the CARD we
shipped (from the Odoo delivery confirmation), never the customer's own wording — so a
customer's own nickname for a product ("Šiška", "Vianočka", no weight) can never be resolved
from it, only a fully-specified wording can. This module recovers the missing half.

For an archived order we already trust two independent, previously-verified facts:

1. **The shipped card list, in order** — from the Odoo Discuss channel 152 confirmation
   ("✅ Objednavka vytvorena"), already cross-checked against this add-on's own event log
   (`email_events(stage='uploaded_orion')`) with agreeing item counts.
2. **The customer's own email text** (`messages.combined_text`) — this add-on's own stored
   extraction of what the customer actually wrote, in their own words, in the order they
   wrote it.

Where the two independently-counted lists AGREE on count, positional pairing recovers the
wording -> card link with no guessing. Where they disagree (a dropped line, a dead thread
reply, a dead-end "máte objednávku?" quote — the #80/#82 defects that produced this very
archive), pairing is refused rather than guessed: a wrong pairing baked into history would
silently ship the wrong product forever, which is worse than no history at all.

This module is PURE (no I/O, no DB) so the pairing logic itself is unit-testable with
synthetic fixtures. The one-off script that actually walks the real archive (raw email text,
Odoo channel dump, frozen catalog/customer snapshot — all real customer data, none of it
fit for this public repo) lives outside git, the same way the earlier archive-reconstruction
tooling (`link.py`, `resolve.py`, `odoo_dump.py`) that built the CURRENT `history.json` did.
"""
from __future__ import annotations

import re

# A day-block header: "Na SOBOTU 27.6.2026 poprosím", "na 16.7", "na 21.7. utorok poprosím:".
# Group 1/2/3 = day, month, year (year optional — most weeks omit it and mean "this year").
_HEADER = re.compile(
    r"^na\s+(?:[a-zA-Zá-žÁ-Ž]+\s+)?(\d{1,2})\s*\.\s*(\d{1,2})\.?"
    r"(?:\s*(\d{4}))?\.?\s*(?:[a-zA-Zá-žÁ-Ž]+\s*)?(?:poprosím|poprosim)?\s*:?\s*$",
    re.IGNORECASE)
# An item line: "Chlieb pšenično ražný : 6 x", "Vianočka - 1 x", "Vianočka : 2x".
_ITEM = re.compile(r"^(.+?)\s*[:\-–]\s*(\d+(?:[.,]\d+)?)\s*x\.?\s*$", re.IGNORECASE)
# The start of a quoted reply — everything from here on is an OLDER message, not this one.
_QUOTE_START = re.compile(r"^>|^Dňa\s.+napísal", re.IGNORECASE)


def _fresh_body(text: str) -> str:
    """The message's own text, with the quoted reply chain (and any `Body:` prefix) cut off.

    A dead-end reply that carries NO fresh content of its own (`>> Dobrý deň , >> na 21.7. …`
    — already quoted from line one) correctly yields nothing here, which in turn yields no
    day-blocks below: exactly the outcome a genuine reconstruction needs for that message.
    """
    out = []
    for line in (text or "").splitlines():
        if _QUOTE_START.match(line.strip()):
            break
        out.append(line)
    body = "\n".join(out)
    marker = re.search(r"^Body:\s*", body, re.MULTILINE)
    return body[marker.end():] if marker else body


def extract_day_blocks(text: str) -> list[dict]:
    """The customer's own item lines, grouped by the delivery day they were written under.

    A single-day order with no "na D.M" header at all still yields ONE block (day=month=
    None) — `wordings_for_order` falls back to it when nothing else disambiguates.
    """
    blocks: list[dict] = [{"day": None, "month": None, "items": []}]
    for raw in _fresh_body(text).splitlines():
        line = raw.strip()
        if not line:
            continue
        hm = _HEADER.match(line)
        if hm:
            day, month, _year = hm.groups()
            blocks.append({"day": int(day), "month": int(month), "items": []})
            continue
        im = _ITEM.match(line)
        if im:
            name = im.group(1).strip()
            qty = float(im.group(2).replace(",", "."))
            blocks[-1]["items"].append((name, qty))
    return [b for b in blocks if b["items"]]


def _canon_day_month(value: str) -> tuple[int, int] | None:
    """(day, month) from either `DD.MM.YYYY` or `YYYY-MM-DD` — never guess which is which."""
    s = str(value or "").strip()
    parts = re.findall(r"\d+", s)
    if len(parts) < 2:
        return None
    if re.match(r"^\d{4}\b", s):           # ISO: YYYY-MM-DD
        return (int(parts[2]), int(parts[1])) if len(parts) >= 3 else None
    return int(parts[0]), int(parts[1])    # DD.MM(.YYYY)


def wordings_for_order(combined_text: str, delivery_date: str, item_count: int) -> list[str] | None:
    """The customer's own wording for each line of THIS order, in the Odoo item's order —
    or None when the pairing cannot be trusted.

    Disambiguates a multi-day email (several "na D.M ... poprosím" blocks quoted or written
    in one message) by the block whose OWN stated day matches this order's delivery date;
    only when that still leaves more than one candidate, or no date match at all, does the
    item COUNT alone decide — and only when it decides uniquely. Anything else (no block,
    several equally-plausible blocks, a count that doesn't match) returns None: silence over
    a guess, same principle `memory.resolve` itself follows.
    """
    if item_count <= 0:
        return None
    blocks = extract_day_blocks(combined_text)
    if not blocks:
        return None
    target = _canon_day_month(delivery_date)
    candidates = blocks
    if target:
        dated = [b for b in blocks if (b["day"], b["month"]) == target]
        if dated:
            candidates = dated
    matches = [b for b in candidates if len(b["items"]) == item_count]
    if len(matches) != 1:
        return None
    return [name for name, _qty in matches[0]["items"]]
