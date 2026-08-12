"""DESADV (dodací list EDI) document builder — #203, DL migration F4.

Byte-for-byte port of the production "ASSEMBLE AND GENERATE EDI [v1]" Code node
(`sub3_edi_code.js`, v27) from the live "Dodacie Listy EDI" n8n workflow — see
`docs/superpowers/specs/2026-08-07-delivery-notes-python-design.md` §2 R80-R91 + §3.
`tests/fixtures/desadv_reference.json` holds bytes produced by running THAT node under
node against synthetic data (the same technique `edi.py`'s own `edi_reference.json`
fixture uses — see `.claude/rules/orders-corpus.md`'s "run the real node under node"
note), so the low-level `generate()` test is parity with what ORION receives today, not
with a reading of the design doc's prose summary. The original JS source is not
committed here — it lives only in the scratchpad it was extracted from (n8n MCP,
2026-08-07) and carries no real customer data, only business logic.

Two layers, mirroring the exact split the source Code node itself has (port EVERY
hard-fail guard, not just the pure function — `.claude/rules/orders-corpus.md`'s own
#131 gotcha: "A hand-ported n8n Code node has TWO layers of logic — the pure
function(s) AND the node's own top-level MAIN guard clauses"):

- `generate()` — the pure HDR+LIN byte builder (mirrors `generateEDI()`). Given
  already-matched items + sklad/cena lookups, applies R84's quantity/unit conversion
  ladder and R85's price fallback, and writes the fixed-width document. Parameter/key
  names deliberately match the JS variable names (`customerEanEdi`, `supplierName`,
  `unitPrice`, ...) exactly, the same reason `edi.py`'s own `build()` does: a byte
  fixture recorded by running the real node under node can be replayed with
  `**case["input"]` with zero renaming.
- `build()` — orchestration (mirrors the node's top-level MAIN section): R81's
  canCreateEDI gate + reject reasons, R83's docNumber (extracted, else auto-generated
  `DL-<SUPPLIER8>-<MMDD>-<HHMM>`; EDI CONTENT carries a digits-only doc number because
  ORION/EDITEL parses the HDR field as a number, but the human-facing docNumber in
  Odoo/registry/filename keeps the original), R89's filename, and R80's qty==0 filter
  — reported via `items_skipped_zero_qty`, never silently dropped the way the n8n
  version does (W10). Odoo HTML notification building is deliberately OUT of scope
  here (worker/pipeline wiring is #203's own sibling phase, F5) — `build()` only
  returns the structured data (skipped items, price substitutions, partial flag) a
  later phase needs to render one.

`generate()`'s `items` input carries BOTH the matched-catalog fields and the original
extraction fields already merged (mirroring the shape the node's own `allMatched`
already has by the time Sub3 runs): `gtin`, `name` (matched catalog name, used for the
LIN description and the sklad/"vajcia" check), `supplierName` (the ORIGINAL DL wording
— used ONLY for R84.1's liquid-multipack detection, since the multipack token is
usually stripped from the matched catalog name), `quantity`, `unit`, `unitPrice`,
`totalPrice`, `mass`.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime

log = logging.getLogger("orders.desadv_edi")

SUPPLIER_EAN = "8586013743063"
SUPPLIER_NAME = "SLOVNORMAL, s.r.o."
SUPPLIER_ADDR = "Druzstevna 170"
SUPPLIER_CITY = "Grance - Petrovce"
SUPPLIER_ZIP = "053 05"
HEADER_WIDTH = 1157
LIN_MIN_WIDTH = 209
LIN_MAX_WIDTH = 221
# R89: the buyer-EAN tail + the doc-number alnum cap the filename uses.
MAX_DOC_NUMBER_IN_FILENAME = 10
# #245: the DESADV LIN record's GTIN field is a FIXED 13 characters (WINCODEX/CODEX
# spec — docs/superpowers/specs/2026-08-07-delivery-notes-python-design.md §3 "LIN
# GTIN(13)"). `_pad()` below TRUNCATES anything longer instead of erroring — a real
# GTIN-14 catalog code (e.g. a bulk/wholesale trade unit) silently loses its last
# digit if it ever reaches this function. `dl_match.decide_item()` is the actual
# guard (a card whose gtin overflows this width is never allowed to match at all,
# so `generate()` never receives one in production) — this constant is the shared
# source of truth both modules must agree on; `dl_match.py` imports it rather than
# hardcoding its own "13".
GTIN_FIELD_WIDTH = 13

# toWin1250, byte-exact port of the JS map — deliberately includes Czech 'ě'/'Ě' (the
# orders-side edi.py's own table lacks it; real DL supplier/product names do carry
# Czech spelling). Unlike edi.py's `_strip_diacritics`, this does NOT fall back to an
# NFKD unicode-wide fold for characters outside the table — the production node
# doesn't either, and byte parity with what n8n has been shipping to ORION matters more
# here than defensive robustness against an as-yet-unseen character.
_WIN1250 = {
    "á": "a", "ä": "a", "č": "c", "ď": "d", "é": "e", "ě": "e", "í": "i", "ľ": "l",
    "ĺ": "l", "ň": "n", "ó": "o", "ô": "o", "ŕ": "r", "š": "s", "ť": "t", "ú": "u",
    "ý": "y", "ž": "z",
    "Á": "A", "Ä": "A", "Č": "C", "Ď": "D", "É": "E", "Ě": "E", "Í": "I", "Ľ": "L",
    "Ĺ": "L", "Ň": "N", "Ó": "O", "Ô": "O", "Ŕ": "R", "Š": "S", "Ť": "T", "Ú": "U",
    "Ý": "Y", "Ž": "Z",
}

_MULTIPACK_RE = re.compile(r"(\d+)\s*[x\u00d7*]\s*([\d.,]+)\s*(ml|l)\b", re.IGNORECASE)
_MASS_KG_RE = re.compile(r"(\d+)[,.]?(\d*)\s*kg", re.IGNORECASE)
_MASS_G_RE = re.compile(r"(\d+)\s*g(?![a-z])", re.IGNORECASE)


def _to_win1250(text) -> str:
    return "".join(_WIN1250.get(ch, ch) for ch in str(text or ""))


def _pad(text, width: int) -> str:
    s = _to_win1250(text)
    return s[:width] if len(s) >= width else s + " " * (width - len(s))


def _pad_left(text, width: int) -> str:
    s = str(text or "")
    return s[:width] if len(s) >= width else " " * (width - len(s)) + s


def _format_date(value) -> str:
    """YYYYMMDD from `DD.MM.YYYY` — the ONLY shape the production node accepts (W12
    flags this as a weak point: an ISO date silently blanks the field). Also accepts
    ISO defensively — extraction always emits DD.MM.YYYY per R47, so this branch is a
    pure safety net that never changes behavior on input the node actually sees, the
    same reasoning `edi.py`'s own `_format_date` already documents for the orders
    pipeline."""
    s = str(value or "").strip()
    iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if iso:
        return iso.group(1) + iso.group(2) + iso.group(3)
    parts = s.split(".")
    if len(parts) != 3:
        return " " * 8
    year = parts[2].strip()
    if len(year) == 2:
        year = "20" + year
    return year + parts[1].strip().rjust(2, "0") + parts[0].strip().rjust(2, "0")


def _today() -> str:
    return datetime.now(UTC).strftime("%Y%m%d")


def _num(value) -> float:
    try:
        if value is None:
            return 0.0
        f = float(value)
        return f if f == f else 0.0  # filters NaN
    except (TypeError, ValueError):
        return 0.0


def _is_unmatched(gtin) -> bool:
    """No real GTIN — empty/None or the model's literal 'NO_MATCH' sentinel. A single
    shared predicate (review finding on #203: this was written twice, once in
    `generate()` and once in `build()`, with no shared helper — a future new
    "unmatched" sentinel could silently diverge between the two)."""
    return not gtin or str(gtin) == "NO_MATCH"


def _detect_liquid_multipack(name) -> tuple[float, str] | None:
    """R84.1: `N x <size> (ml|l)` on the ORIGINAL supplier wording (never the matched
    catalog name — the token is usually stripped from the card). Liquid only — does
    NOT match a piece/weight multipack like "12x500g". Returns (total_litres, "l") or
    None."""
    m = _MULTIPACK_RE.search(str(name or ""))
    if not m:
        return None
    count = int(m.group(1))
    size_num = float(m.group(2).replace(",", "."))
    if not count or not size_num or count > 1000:
        return None
    unit = m.group(3).lower()
    size_l = size_num / 1000 if unit == "ml" else size_num
    return count * size_l, "L"


def _extract_mass(name) -> float:
    m = _MASS_KG_RE.search(str(name or ""))
    if m:
        return float(m.group(1) + "." + (m.group(2) or "0"))
    g = _MASS_G_RE.search(str(name or ""))
    if g:
        return float(g.group(1)) / 1000
    return 0.0


def _format_price(value) -> str:
    """9-wide, 3-decimal — despite the production node calling its equivalent
    `formatMass()`, its ONLY call site is `formatMass(unitPrice)` (grep-confirmed
    against the real source): the LIN layout has no separate mass field at all — mass
    is input-only, consumed by the R84 conversion math, never written to the document.
    Named for what it actually formats (review finding on #203), not the misleading
    inherited JS name."""
    return _pad_left(f"{_num(value):.3f}", 9)


def _format_qty(value) -> str:
    return _pad_left(f"{_num(value):.3f}", 12)


@dataclass
class Desadv:
    content: str
    line_count: int
    skipped: list[str] = field(default_factory=list)
    substituted: list[str] = field(default_factory=list)


def catalog_lookups(catalog: list[dict]) -> tuple[dict[str, str], dict[str, float]]:
    """R20/R84/R85: `sklad`/`cena` per GTIN, straight off the DL catalog rows. Mirrors
    the node's own top-level `for (const p of catalog)` loop exactly."""
    sklad_by_gtin: dict[str, str] = {}
    cena_by_gtin: dict[str, float] = {}
    for p in catalog or []:
        gtin = p.get("gtin")
        if not gtin:
            continue
        sklad_by_gtin[str(gtin)] = p.get("sklad") or ""
        cena = _num(str(p.get("cena") or "").replace(",", "."))
        if cena > 0:
            cena_by_gtin[str(gtin)] = cena
    return sklad_by_gtin, cena_by_gtin


def generate(data: dict, sklad_by_gtin: dict, cena_by_gtin: dict) -> Desadv:
    """Byte-parity port of `generateEDI(data, skladByGtin, cenaByGtin)`.

    `data` keys match the JS variable names exactly: `customerEanEdi`, `customerName`,
    `docNumber`, `orderNumber`, `deliveryDate`, `items[]` with `gtin`, `name`
    (matched catalog name), `supplierName` (original wording), `quantity`, `mass`,
    `unit`, `totalPrice`, `unitPrice`.
    """
    buyer_ean = data.get("customerEanEdi") or ""
    buyer_name = _to_win1250(data.get("customerName") or "Zakaznik")
    doc_number = data.get("docNumber") or ""
    order_number = data.get("orderNumber") or ""
    deliv_date = _format_date(data.get("deliveryDate"))
    doc_date = deliv_date  # v25: HDR docDate uses the delivery date, not today.

    hdr = "HDR"
    hdr += _pad(doc_number, 15)
    hdr += doc_date
    hdr += deliv_date
    hdr += _pad("", 33)
    hdr += _pad(order_number, 30)
    hdr += _pad("", 24)
    hdr += _pad(buyer_ean, 17)
    hdr += _pad(buyer_ean, 17)
    hdr += _pad(buyer_ean, 17)
    hdr += _pad(SUPPLIER_EAN, 17)
    hdr += _pad("", 21)
    hdr += _pad(buyer_name, 105)
    hdr += _pad("", 105)
    hdr += _pad("", 38)
    hdr += _pad(SUPPLIER_NAME, 105)
    hdr += _pad("", 167)
    hdr += _pad(SUPPLIER_ADDR, 38)
    hdr += _pad(SUPPLIER_CITY, 27)
    hdr += _pad(SUPPLIER_ZIP, 11)
    hdr += _pad("", 160)
    hdr += _pad("", 66)
    if len(hdr) < HEADER_WIDTH:
        hdr += " " * (HEADER_WIDTH - len(hdr))
    else:
        hdr = hdr[:HEADER_WIDTH]

    lines = [hdr]
    skipped: list[str] = []
    substituted: list[str] = []
    line_no = 0
    for item in data.get("items") or []:
        gtin = item.get("gtin")
        if _is_unmatched(gtin):
            skipped.append(item.get("name") or "(unknown)")
            log.info("desadv line skipped (no GTIN): %r", item.get("name"))
            continue
        line_no += 1
        qty = _num(item.get("quantity"))
        mass = _num(item.get("mass"))
        unit = item.get("unit") or "Kus"
        up = _num(item.get("unitPrice"))

        # R84: sklad=100 (kg-tracked), except eggs ("vajcia") -> convert to kg. All
        # other sklad values keep the piece count as printed.
        sklad = sklad_by_gtin.get(str(gtin), "")
        is_kg_tracked = (str(sklad) == "100"
                        and "vajcia" not in str(item.get("name") or "").lower())

        # R84.1: liquid multipack (checked on the ORIGINAL wording) takes precedence
        # over the kg rule for ANY sklad, including 100.
        override_unit = None
        mp = _detect_liquid_multipack(item.get("supplierName"))
        if mp:
            total_l, override_unit = mp
            out_qty = qty * total_l
            unit_price = up / total_l if total_l else up
            log.info("desadv line %r: liquid multipack -> %.3f L @ %.3f", gtin, out_qty,
                     unit_price)
        elif is_kg_tracked:
            if str(unit).strip().lower() == "kg":
                out_qty, unit_price = qty, up
            elif mass > 0:
                out_qty, unit_price = qty * mass, up / mass
                log.info("desadv line %r: kg-tracked conversion %.3f x %.3fkg -> "
                         "%.3fkg @ %.3f", gtin, qty, mass, out_qty, unit_price)
            else:
                out_qty, unit_price = qty, up
                log.warning("desadv line %r: sklad=100 but mass unknown — quantity "
                           "left unconverted", gtin)
        else:
            out_qty, unit_price = qty, up

        # W11 explicit contract: the LIN unit column keeps the item's ORIGINAL unit
        # text unchanged in every branch except the liquid-multipack one, which forces
        # 'L' — ORION keys the import on the GTIN card, not on this text, so it is
        # informational only, but a future reader must not have to reverse-engineer
        # that from the JS the way the original node left it implicit.
        final_unit = override_unit or unit

        # R85 PRICE FALLBACK, after conversion so units line up: catalog `cena` (>0)
        # substitutes when the line has no usable price, or is >=5x / <=1/5x it. Normal
        # price movement (<5x) is never overwritten.
        cat_price = _num(cena_by_gtin.get(str(gtin)))
        if cat_price > 0 and (not (unit_price > 0)
                              or unit_price >= cat_price * 5
                              or unit_price <= cat_price / 5):
            was = f"{unit_price:.3f}" if unit_price > 0 else "bez ceny"
            substituted.append(f"{item.get('name') or '(?)'}: {was} \u2192 "
                               f"{cat_price:.3f} \u20ac/{final_unit}")
            log.info("desadv line %r: price fallback %s -> %.3f (catalog cena)", gtin,
                     was, cat_price)
            unit_price = cat_price

        lin = "LIN"
        lin += _pad_left(str(line_no), 6)
        lin += _pad(str(gtin), GTIN_FIELD_WIDTH)
        lin += _pad_left("0", 14)
        lin += _pad("", 23)
        lin += "Z"
        lin += _pad("", 22)
        lin += _format_price(unit_price)
        lin += _pad_left("5", 5)
        lin += _format_qty(out_qty)
        lin += _pad(str(final_unit)[:3], 3)
        lin += _pad("", 35)
        lin += _pad(order_number, 30)
        lin += _pad("", 45)
        if len(lin) < LIN_MIN_WIDTH:
            lin += " " * (LIN_MIN_WIDTH - len(lin))
        elif len(lin) > LIN_MAX_WIDTH:
            lin = lin[:LIN_MAX_WIDTH]
        lines.append(lin)

    return Desadv(content="\r\n".join(lines), line_count=line_no, skipped=skipped,
                 substituted=substituted)


# --- naming (R89) ----------------------------------------------------------

def _generate_doc_number(customer_name: str) -> str:
    """R83's fallback when extraction found no docNumber at all:
    `DL-<SUPPLIER8>-<MMDD>-<HHMM>`, mirrors `generateDocNumber()`."""
    now = datetime.now()
    mmdd = now.strftime("%m%d")
    hhmm = now.strftime("%H%M")
    first_word = re.split(r"[\s,]+", str(customer_name or "UNKNOWN"))[0]
    nfd = unicodedata.normalize("NFD", first_word)
    stripped = "".join(c for c in nfd if not unicodedata.combining(c))
    supplier = re.sub(r"[^A-Z]", "", stripped.upper())[:8]
    return f"DL-{supplier}-{mmdd}-{hhmm}"


def _date_stamp(delivery_date) -> str:
    stamp = _format_date(delivery_date)
    return stamp if stamp.strip() else _today()


def _doc_part(doc_number) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]", "", str(doc_number or ""))[:MAX_DOC_NUMBER_IN_FILENAME]
    return f"_{cleaned}" if cleaned else ""


def stable_prefix(ean_edi: str, doc_number: str) -> str:
    """#239 finding 6: the part of `filename()`'s output that stays IDENTICAL across
    every retry of the SAME document — everything up to (not including) the
    `_<YYYYMMDD>_<HHMMSSmmm>.txt` suffix, which changes on every attempt (a fresh
    timestamp, R89). A safe presence/absence check on ORION must match on THIS, never
    on a full filename — a full filename built for a retry can never equal an earlier
    attempt's own name, so filename equality can never answer "did an earlier attempt
    already land this document." See `already_landed()` below.

    Review finding: `_doc_part()`'s existing `MAX_DOC_NUMBER_IN_FILENAME=10`
    truncation (needed for the filename itself, R89) is REUSED here as a document-
    IDENTITY key — two genuinely different doc numbers that happen to share their
    first 10 alnum characters would collide onto the same stable prefix. Harmless
    today: `stable_prefix()`/`already_landed()` are not yet wired into any decision
    (deliberately deferred, see the design comment on #239) — but this collision risk
    MUST be re-examined before either function becomes load-bearing for an actual
    safe-retry decision."""
    ean_short = str(ean_edi or "")[-6:] or "000000"
    return f"DESADV_{ean_short}{_doc_part(doc_number)}_"


def filename(ean_edi: str, delivery_date: str, doc_number: str, stamp: str = "") -> str:
    """R89: `DESADV_<last 6 of buyer EAN>_<docNumber alnum, max 10>_<YYYYMMDD from
    deliveryDate>_<HHMMSSmmm>.txt` — mirrors `generateUniqueFilename()` (`orderIndex`
    is always 0 in production, "never used" per the design doc, so its `_N` suffix is
    not ported). `stamp` is injectable so a test does not depend on the clock, same as
    `edi.py`'s own `filename()`."""
    if not stamp:
        now = datetime.now()
        stamp = now.strftime("%H%M%S") + f"{now.microsecond // 1000:03d}"
    return f"{stable_prefix(ean_edi, doc_number)}{_date_stamp(delivery_date)}_{stamp}.txt"


def _matches_stable_prefix(name: str, prefix: str) -> bool:
    """Tolerates R89's own upload-time `Z-` wire prefix, PLUS Communicator's separate,
    uncontrolled archCodex rename job's OWN extra `Z-` on top of that — mirrors
    `confirm.py`'s own `_decide()` tolerance EXACTLY (`wire_name in archCodex or
    f"Z-{wire_name}" in archCodex`, `wire_name` already carrying ONE `Z-`): a name with
    no `Z-`, exactly one, or exactly two leading `Z-`s all match. Review finding: an
    earlier draft stripped an UNBOUNDED number of leading `Z-`, which is more
    permissive than confirm.py's own check despite the docstring claiming parity —
    fixed to the exact same three-way check."""
    return (name.startswith(prefix)
           or name.startswith(f"Z-{prefix}")
           or name.startswith(f"Z-Z-{prefix}"))


def already_landed(dirs: dict, ean_edi: str, doc_number: str) -> bool:
    """#239 finding 6: has a document with THIS identity (buyer/supplier EAN + doc
    number) already reached ORION under ANY prior attempt's filename? `dirs` is
    `upload.list_dirs(cfg)`'s return shape (`in_DL`/`archCodex`/`unconfirmed` name
    sets). Matches the STABLE prefix `stable_prefix()` builds — the trailing
    date/timestamp is the only part that differs between attempts, so a prefix match
    is genuine document identity, never a guess.

    Checked against all three folders a DESADV upload can legitimately be found in:
    `in_DL` (still queued for her morning import), `archCodex` (imported), `unconfirmed`
    (import FAILED — but the UPLOAD itself still succeeded, so retrying now would still
    be a duplicate upload, just of a document CODEX later rejected)."""
    prefix = stable_prefix(ean_edi, doc_number)
    for folder in ("in_DL", "archCodex", "unconfirmed"):
        for name in (dirs or {}).get(folder) or ():
            if _matches_stable_prefix(name, prefix):
                return True
    return False


def upload_name(name: str) -> str:
    """R89: uploaded onto ORION as `Z-<filename>` (verified live 2026-08-07: `in_DL`
    and `in\\archCodex` both hold the file under its Z-prefixed name — the LEDGER keeps
    the base name per R83's "human-facing name in the registry", this is only the
    on-wire transform applied at upload time). The target directory itself is NOT
    duplicated here — `upload.DL_DIR`/`cfg.orion_dl_dir` (config.py) are the two
    existing sources of truth for that path (mirroring the orders side's own
    `edi.ORION_DIR`/`cfg.orion_dir` split); a third copy here was removed (review
    finding on #203) rather than adding a fourth place a path change would need to
    touch."""
    return f"Z-{name}"


# --- orchestration (R80-R83, R89) -------------------------------------------

@dataclass
class DesadvResult:
    can_create: bool
    reject_reason: str
    doc_number: str                    # human-facing (original, letter prefix kept)
    doc_number_auto_generated: bool
    filename: str                      # "" when can_create is False
    content: str                       # "" when can_create is False
    line_count: int
    items_total: int                   # non-zero-qty items considered
    items_skipped_zero_qty: list[str]  # W10: reported, never silently dropped
    items_skipped_no_match: list[str]  # R81: partial EDI — surfaced, not lost
    price_substitutions: list[str]
    partial: bool
    customer_name: str
    customer_ean_edi: str
    delivery_date: str


def build(header: dict, extraction: dict, matched_items: list[dict],
         catalog: list[dict]) -> DesadvResult:
    """Orchestration mirroring the Code node's top-level MAIN section.

    `header` — `{"customerName": ..., "customerEanEdi": ...}` (Sub2's ASSEMBLE OUTPUT
    contract carries only `customerEanEdi` as the supplier-match signal — "has EAN" IS
    "matched", the same simplification the production node's own comment documents:
    the original used a separate `customerMatch.matched` flag, but the header contract
    only ever carries the EAN).
    `extraction` — `{"docNumber": ..., "deliveryDate": ...}`.
    `matched_items` — each item already merged (extraction fields + the match decision):
    `gtin`, `name` (original DL wording), `matchedCatalogName` (matched card name, ""
    or absent when unmatched), `quantity`, `unit`, `unitPrice`, `totalPrice`, `mass`.
    `catalog` — the DL catalog rows (R20 shape: name/gtin/mass/doplnok/sklad/cena).
    """
    customer_name = str(header.get("customerName") or "")
    customer_ean_edi = str(header.get("customerEanEdi") or "")
    supplier_matched = bool(customer_ean_edi)

    all_items = matched_items or []
    zero_qty = [i for i in all_items if _num(i.get("quantity")) == 0]
    items = [i for i in all_items if _num(i.get("quantity")) != 0]
    no_match = [i for i in items if _is_unmatched(i.get("gtin"))]
    real_matched = [i for i in items if not _is_unmatched(i.get("gtin"))]
    can_create = supplier_matched and bool(real_matched)

    zero_qty_names = [i.get("name") or "(unknown)" for i in zero_qty]
    if zero_qty_names:
        # W10: the n8n version drops these with zero trace anywhere in its output —
        # logged AND returned on `items_skipped_zero_qty`, never silent.
        log.info("desadv: %d item(s) with zero quantity dropped from the EDI: %r",
                 len(zero_qty_names), zero_qty_names)
    extraction_doc_number = str(extraction.get("docNumber") or "")
    delivery_date = str(extraction.get("deliveryDate") or "")

    if not can_create:
        if not supplier_matched:
            reason = "Dodavatel nebol najdeny v databaze"
        elif not real_matched:
            reason = f"Ziadne polozky s GTIN: 0 z {len(items)}"
        else:
            reason = "Ziadne polozky s nenulovym mnozstvom"
        log.warning("desadv: cannot create EDI for supplier=%r doc=%r — %s",
                   customer_name, extraction_doc_number, reason)
        return DesadvResult(
            can_create=False, reject_reason=reason, doc_number=extraction_doc_number,
            doc_number_auto_generated=False, filename="", content="", line_count=0,
            items_total=len(items), items_skipped_zero_qty=zero_qty_names,
            items_skipped_no_match=[i.get("name") or "(unknown)" for i in no_match],
            price_substitutions=[], partial=bool(no_match),
            customer_name=customer_name, customer_ean_edi=customer_ean_edi,
            delivery_date=delivery_date)

    # R83: extraction's own docNumber wins; else auto-generate. EDI CONTENT strips to
    # digits-only (ORION/EDITEL parses the HDR field as a number — a letter prefix
    # crashes the import); the human-facing doc_number (Odoo, registry, filename) keeps
    # whatever extraction/auto-generation produced.
    #
    # `or doc_number` is a FAITHFUL port of the production node's own fallback
    # (`String(docNumber).replace(/[^0-9]/g, '') || String(docNumber)`), not a Python
    # addition — verified against the real sub3_edi_code.js source (review finding on
    # #203). It is a genuine INHERITED weak point, same class as W12: a docNumber with
    # ZERO digit characters anywhere (e.g. extraction misreads a purely-alphabetic
    # value) falls back to the un-stripped original, which can still crash ORION's
    # import exactly the way R83 exists to prevent. `_generate_doc_number()`'s own
    # fallback always contains digits (MMDD/HHMM), so this only bites when EXTRACTION
    # itself hands back an all-non-digit docNumber — never observed in practice, but
    # deliberately NOT "fixed" here (dropping the fallback would diverge from the byte
    # parity this module exists to guarantee); pinned by
    # `test_doc_number_with_zero_digits_falls_back_to_the_raw_value_matching_production`
    # so the behavior is visible and provably intentional, not silently unverified.
    doc_number = extraction_doc_number or _generate_doc_number(customer_name)
    auto_generated = not extraction_doc_number
    edi_doc_number = re.sub(r"[^0-9]", "", doc_number) or doc_number

    sklad_by_gtin, cena_by_gtin = catalog_lookups(catalog)
    data = {
        "customerEanEdi": customer_ean_edi,
        "customerName": customer_name,
        "orderNumber": edi_doc_number,
        "docNumber": edi_doc_number,
        "deliveryDate": extraction.get("deliveryDate"),
        "items": [
            {
                "gtin": i.get("gtin"),
                "name": i.get("matchedCatalogName") or i.get("name"),
                "supplierName": i.get("name"),
                "quantity": i.get("quantity"),
                "mass": _num(i.get("mass")) or _extract_mass(i.get("name")) or 0,
                "unit": i.get("unit") or "Kus",
                "totalPrice": i.get("totalPrice") or 0,
                "unitPrice": i.get("unitPrice") or 0,
            }
            for i in items
        ],
    }
    edi = generate(data, sklad_by_gtin, cena_by_gtin)
    name = filename(customer_ean_edi, extraction.get("deliveryDate"), doc_number)
    log.info("desadv built: doc=%r supplier=%r lines=%d skipped=%d substitutions=%d "
             "-> %s", doc_number, customer_name, edi.line_count, len(edi.skipped),
             len(edi.substituted), name)

    return DesadvResult(
        can_create=True, reject_reason="", doc_number=doc_number,
        doc_number_auto_generated=auto_generated, filename=name, content=edi.content,
        line_count=edi.line_count, items_total=len(items),
        items_skipped_zero_qty=zero_qty_names, items_skipped_no_match=edi.skipped,
        price_substitutions=edi.substituted, partial=bool(edi.skipped),
        customer_name=customer_name, customer_ean_edi=customer_ean_edi,
        delivery_date=delivery_date)
