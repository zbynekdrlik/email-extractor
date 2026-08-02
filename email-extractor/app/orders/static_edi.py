"""EDI writer for the n8n **Static auto orders** workflow's `generator` node (#131,
workflow `O8IYhUESjaWmPMTI`, node "generator" — "WINCODEX EDI Generator, store-match
hardened for BARIS lokality").

`app/orders/edi.py` is NOT this node — it is a 1:1 port of a DIFFERENT Code node
("ASSEMBLE AND GENERATE EDI" in the separate "AI auto orders" workflow). Comparing the
two, line by line, found real divergences (see the design comment on #131):

- Header field A dates from the order's own `issueDate`, never `datetime.now()`.
- A long order number is truncated from the FRONT (the generic `pad()`, no special
  case) — `edi.py` special-cases order numbers to keep the LAST 15 chars instead.
- Buyer name (the partner's fixed legal name) and delivery-location name (the specific
  store) are two DISTINCT header fields — `edi.py` writes the same `store` string into
  both slots, which is only correct when buyer == store (true for AI orders' single
  customers, false for a partner like KARMEN with many physical stores).
- The store's own EAN is computed here (`PARTNER_CONFIG` + `LABAS_STORES`/
  `KARMEN_CASH_STORES` token-subset fallback) — `edi.py` takes an opaque `ean` param.
- An unmapped non-ASCII character is DELETED, never NFKD-transliterated.
- The filename convention is entirely different.

`tests/fixtures/static_edi_reference.json` was produced by running the PRODUCTION
`generator` node's VERBATIM JS source (fetched via `get_workflow_details`) under node —
the same technique `edi_reference.json` already uses for the other writer. A REAL-data
byte-parity proof (12 real processed static orders from this add-on's own Postgres,
covering all four partners) lives outside git on dev2 — see `.claude/rules/
orders-corpus.md` and the #131 design comment. Mirror any change here in the n8n
`generator` Code node (same discipline `static_ean.py`/`static_parse.py` already
require), and any change to `static_ean.PRODUCT_EAN_BY_CODE`-equivalent logic in BOTH
places.

This module is NOT imported by the live pipeline (Static auto orders still runs in
n8n) — same additive-only, groundwork status as `static_parse.py`/`static_ean.py`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import static_ean

SUPPLIER_EAN = "8586013743063"
SUPPLIER_NAME = "SLOVNORMAL, s.r.o."
HEADER_WIDTH = 1157

# Mirrors generator.js's PRODUCT_EAN_BY_CODE — KARMEN sends the SAME product under
# different own-codes over time, so this is an override for genuine exceptions only;
# the catalog lookup (static_ean.catalog_match, via resolve_ean) handles the general
# case (#49).
PRODUCT_EAN_BY_CODE = {
    "9485": "8588001805609", "4055": "4055", "9098": "8588001805692",
    "186": "8588001805777", "153": "8588001805630", "7968": "8588001805722",
    "P000072": "8588001805777",
}

# Key ORDER matters — a more specific name (SYROVA) must precede a more general one,
# exactly mirroring generator.js's `Object.entries()` iteration order.
PRODUCT_EAN_BY_NAME = {
    "CHLIEB 500g PSEN.RAZNY SLOVNORMAL": "8588001805623",
    "CHLIEB 800g PSEN.RAZNY SLOVNORMAL": "8588001805609",
    "CEREALNA KOCKA SYROVA 60": "8588001805722",
    "CEREALNA KOCKA 60": "8588001805630",
    "PECIVO TOSKANSKE 60": "8588001805692",
    "TOSKANSKE PECIVO 60": "8588001805692",
    "LUPACKA 75": "8588001805777",
    "ZEMLA 45": "4055",
}

PARTNER_CONFIG = {
    "KARMEN": {"ean_prefix": "8589364472", "buyer_name": "KARMEN - velkoobchod potravin s.r.o."},
    "KOMFOS": {"ean_prefix": "8589364541", "buyer_name": "KOMFOS Presov, s.r.o."},
    "LABAS": {"ean_prefix": "8589361831", "buyer_name": "LABAS s.r.o."},
}
LABAS_STORES = {"GELNICA": "001", "SPISSKA NOVA VES": "002", "SPISSKA": "002"}
KARMEN_CASH_STORES = {"ZEMPLINSKA 1A PRESOV": "111"}

_ASCII_MAP = {
    "á": "a", "ä": "a", "č": "c", "ď": "d", "é": "e", "í": "i", "ĺ": "l", "ľ": "l",
    "ň": "n", "ó": "o", "ô": "o", "ŕ": "r", "š": "s", "ť": "t", "ú": "u", "ý": "y", "ž": "z",
    "Á": "A", "Ä": "A", "Č": "C", "Ď": "D", "É": "E", "Í": "I", "Ĺ": "L", "Ľ": "L",
    "Ň": "N", "Ó": "O", "Ô": "O", "Ŕ": "R", "Š": "S", "Ť": "T", "Ú": "U", "Ý": "Y", "Ž": "Z",
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-",
    "‘": "'", "’": "'", "“": '"', "”": '"', "…": "...",
    " ": " ", "°": "o",
}


@dataclass
class StaticEdi:
    content: str
    filename: str
    store_ean: str
    buyer_name: str
    items_with_ean: int
    items_skipped: int
    skipped_items: list[str]


def _to_ascii(text) -> str:
    """Mirrors `generator.js`'s `toAscii()` EXACTLY: `str.replace(/[^\\x00-\\x7F]/g, c
    => map[c] || '')` — any non-ASCII character NOT in the explicit map is DELETED, not
    transliterated. This is a real divergence from `edi.py`'s `_strip_diacritics`,
    which NFKD-folds an unmapped character instead of dropping it."""
    out = []
    for ch in str(text or ""):
        if ord(ch) < 128:
            out.append(ch)
            continue
        out.append(_ASCII_MAP.get(ch, ""))
    return "".join(out)


def _pad(text, width: int) -> str:
    """Mirrors `generator.js`'s `pad()` — ASCII-folds, then truncates or space-pads to
    EXACTLY `width`, keeping the FRONT of an over-length string (no special-casing,
    unlike `edi.py`'s order-number handling)."""
    s = _to_ascii(text)
    return s[:width] if len(s) >= width else s + " " * (width - len(s))


def _pad_left(text, width: int) -> str:
    """Mirrors `generator.js`'s `padLeft()` — no ASCII folding, right-justified."""
    s = str(text or "")
    return s[:width] if len(s) >= width else " " * (width - len(s)) + s


def _format_date(value) -> str:
    """Mirrors `generator.js`'s `formatDate()` exactly: DD.MM.YYYY only (no ISO
    handling — the Static-orders pipeline never reformats to ISO before this stage,
    unlike AI orders' verify step, which is why `edi.py`'s `_format_date` needs that
    branch and this one does not), no zero-padding of day/month (the upstream
    `static_parse` regexes already enforce 2 digits)."""
    s = str(value or "").strip()
    parts = s.split(".")
    if len(parts) != 3:
        return " " * 8
    year = parts[2].strip()
    if len(year) == 2:
        year = "20" + year
    return year + parts[1].strip() + parts[0].strip()


def _format_qty(qty) -> str:
    try:
        q = float(qty or 0)
    except (TypeError, ValueError):
        q = 0.0
    return _pad_left(f"{q:.3f}", 12)


def _token_subset_match(stores: dict[str, str], location_upper: str) -> str | None:
    for name, code in stores.items():
        tokens = name.split()
        if all(t in location_upper for t in tokens):
            return code
    return None


def get_store_ean(partner: str, prev_number) -> str:
    """Mirrors `generator.js`'s `getStoreEAN()`."""
    config = PARTNER_CONFIG.get(partner, PARTNER_CONFIG["KARMEN"])
    if partner == "KARMEN_CASH":
        karmen = PARTNER_CONFIG["KARMEN"]
        loc = _normalize_location(prev_number)
        if loc in KARMEN_CASH_STORES:
            return karmen["ean_prefix"] + KARMEN_CASH_STORES[loc]
        code = _token_subset_match(KARMEN_CASH_STORES, loc)
        if code:
            return karmen["ean_prefix"] + code
        raise ValueError(f'Neznáma KARMEN_CASH prevádzka: "{prev_number}". '
                          "Pridajte do KARMEN_CASH_STORES.")
    if partner == "LABAS":
        loc = _normalize_location(prev_number)
        if loc in LABAS_STORES:
            return config["ean_prefix"] + LABAS_STORES[loc]
        code = _token_subset_match(LABAS_STORES, loc)
        if code:
            return config["ean_prefix"] + code
        return config["ean_prefix"] + "001"
    return config["ean_prefix"] + str(prev_number or "").rjust(3, "0")


def _normalize_location(prev_number) -> str:
    loc = _to_ascii(str(prev_number or "")).upper()
    loc = re.sub(r"[^A-Z0-9 ]", " ", loc)
    return re.sub(r"\s+", " ", loc).strip()


def get_buyer_name(partner: str) -> str:
    return PARTNER_CONFIG.get(partner, PARTNER_CONFIG["KARMEN"])["buyer_name"]


def _get_product_ean(item: dict, catalog: list[dict]) -> str | None:
    return static_ean.resolve_ean(item, catalog, code_map=PRODUCT_EAN_BY_CODE,
                                   name_map=PRODUCT_EAN_BY_NAME)


def _generate_content(order_data: dict, store_ean: str, buyer_name: str,
                       resolved_eans: list[str | None]) -> str:
    """`resolved_eans` is the per-item EAN, ALREADY resolved once by `build()` — this
    function never re-runs the catalog lookup."""
    store_name = order_data.get("deliveryLocationName") or \
        f"{order_data.get('partner') or 'KARMEN'} {order_data.get('prevNumber') or ''}"
    hdr = ("HDR" + _pad(SUPPLIER_EAN, 13) + "  "
           + _pad(order_data.get("fullOrderNumber") or "", 15) + _pad("", 14)
           + _format_date(order_data.get("issueDate")) + "   "
           + _pad(store_ean, 17) + _pad(buyer_name, 50) + _pad("", 50) + _pad("", 50)
           + _pad("", 30) + _pad("", 15) + _pad("", 14)
           + _pad(store_ean, 17) + _pad(store_name, 50) + _pad("", 129)
           + _pad(store_ean, 17) + _pad("", 50) + _pad("", 159)
           + _pad(SUPPLIER_EAN, 17) + _pad(SUPPLIER_NAME, 50) + _pad("", 159)
           + _pad("", 17) + _pad("", 126)
           + _format_date(order_data.get("deliveryDate")) + _pad("", 4) + _pad("", 70))
    lines = [hdr]
    line_no = 0
    for item, ean in zip(order_data.get("items") or [], resolved_eans, strict=True):
        if not ean:
            continue
        line_no += 1
        lines.append("LIN" + _pad_left(str(line_no), 6) + "   " + _pad(ean, 25)
                     + _format_qty(item.get("quantity")) + _pad("PCE", 3) + _pad("", 12)
                     + _pad(item.get("description") or "", 50) + _pad("", 100))
    lines.append("SUM")
    return "\r\n".join(lines)


def _filename(order_data: dict) -> str:
    """`<PARTNER>_<orderNumber with / -> _>_<prevPart>.txt` — the store's own
    convention, unrelated to `edi.py`'s `ORDER_<ean>_<PO>_<date>_<stamp>.txt`."""
    partner = order_data.get("partner") or "KARMEN"
    full_order_number = order_data.get("fullOrderNumber")
    order_part = full_order_number.replace("/", "_") if full_order_number else "unknown"
    if partner == "KARMEN_CASH":
        prev_part = "CASH"
    else:
        prev_part = str(order_data.get("prevNumber") or "").rjust(3, "0")
    name = _to_ascii(f"{partner}_{order_part}_{prev_part}.txt")
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = re.sub(r"\s+", "_", name)
    return name


def build(order_data: dict, catalog: list[dict]) -> StaticEdi:
    """The fixed-width document + the metadata the n8n node also returns.

    Parameter/field names deliberately mirror `generator.js`'s own so a byte diff
    against `tests/fixtures/static_edi_reference.json` (produced by the real node) is
    unambiguous.
    """
    partner = order_data.get("partner") or "KARMEN"
    prev_number = order_data.get("prevNumber")
    store_ean = get_store_ean(partner, prev_number)
    buyer_name = get_buyer_name(partner)

    items = order_data.get("items") or []
    resolved_eans = [_get_product_ean(it, catalog) for it in items]  # ONE resolve/item
    content = _generate_content(order_data, store_ean, buyer_name, resolved_eans)

    without_ean = [it for it, ean in zip(items, resolved_eans, strict=True) if not ean]

    return StaticEdi(
        content=content,
        filename=_filename(order_data),
        store_ean=store_ean,
        buyer_name=buyer_name,
        items_with_ean=len(items) - len(without_ean),
        items_skipped=len(without_ean),
        skipped_items=[it.get("description") for it in without_ean],
    )
