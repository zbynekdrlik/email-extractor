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

import hashlib
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


def _gs1_check_digit(body: str) -> str:
    """The standard GS1 mod-10 check digit for a numeric code BODY (every digit except the
    trailing check digit). Weights alternate 3, 1, 3, 1 … applied from the RIGHTMOST body
    digit leftward — the one algorithm shared by GTIN-8/12/13/14 (only the body length
    differs). `body` must be all-digits; the caller guarantees that."""
    total = sum(int(ch) * (3 if i % 2 == 0 else 1) for i, ch in enumerate(reversed(body)))
    return str((10 - (total % 10)) % 10)


def gtin14_to_gtin13(gtin) -> str | None:
    """The GTIN-13 nested inside a *valid* GTIN-14, or None (#246).

    A GTIN-14 is ``[indicator digit][12-digit item reference][check digit]``. Its nested
    GTIN-13 is those middle 12 digits followed by a **recomputed** check digit — never the
    GTIN-14's own check digit (a different body length yields a different check), and never a
    naive prefix/suffix strip. Returns None unless `gtin` is exactly 14 numeric chars AND is
    itself a check-digit-valid GTIN-14 — so a 14-char code that only LOOKS like a GTIN-14
    (e.g. an internal CODEX identifier whose own check digit does not validate, like the
    warehouse-confirmed 14-digit-only "Korenie čierne mleté") yields None, never a
    plausible-but-false 13-digit code. Deliberately conservative: the result is only ever used
    as a labelled hint for the warehouse to verify against CODEX, never to ship. A returned
    value is always a check-digit-valid GTIN-13, but its EXISTENCE as a real stock card must
    still be confirmed in CODEX before use — the same manual step the owner did for #246's 9
    confirmed products (`.claude/rules/n8n-workflow-edits.md`)."""
    s = str(gtin or "")
    # `.isascii()` guards against Unicode digit look-alikes (superscripts etc.) that pass
    # `.isdigit()` but blow up `int()` in `_gs1_check_digit` — that helper's docstring
    # promises its caller only ever hands it plain ASCII digits.
    if len(s) != 14 or not s.isascii() or not s.isdigit():
        return None
    if _gs1_check_digit(s[:13]) != s[13]:  # not a valid GTIN-14 -> no honest sibling
        return None
    middle12 = s[1:13]
    return middle12 + _gs1_check_digit(middle12)


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

# R84.2 (#366): tonne-unit tokens. 1 ton == 1000 kg. Kept as an EXACT-token set (never
# a substring/prefix match) so a piece unit like "kt" (kart\u00f3n), "ba", or "kus" can never
# be misread as a tonne and multiplied by 1000. Covers Slovak (`ton`/`tona`/`tony`/
# `tonu`), Czech (`tuna`/`tuny`/`tunu`/`tun` \u2014 real DL wording carries Czech spelling,
# e.g. the observed `balen\u00ed` unit) and the English form \u2014 none collide with any observed
# real unit (`ks`/`ba`/`kg`/`kt`/`kus`/`balen\u00ed`/\u2026), so extending the set is collision-free.
_TON_UNITS = {"t", "ton", "tona", "tony", "tonu", "tonne", "tonnes",
              "tuna", "tuny", "tunu", "tun"}


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


def _is_ton_unit(unit) -> bool:
    """R84.2 (#366): a tonne unit on a line (t / ton / tona / tonne / …). Diacritics are
    folded (`tón` -> `ton`), a trailing dot is stripped (`ton.` -> `ton`), and the
    result must EXACTLY equal one of `_TON_UNITS` — never a substring, so `kt`/`ba`/`kus`
    can never match."""
    u = _to_win1250(str(unit or "")).strip().lower().rstrip(".")
    return u in _TON_UNITS


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
        elif is_kg_tracked and _is_ton_unit(unit):
            # R84.2 (#366): a kg-tracked card receiving a tonne-unit delivery — 1 ton ==
            # 1000 kg. ORION keys the card in kg, so the LIN quantity AND the per-line
            # price must be in kg, and the unit label becomes kg. Checked BEFORE the
            # kg/mass rungs — a tonne is 1000 kg regardless of any per-piece `mass`.
            # Without this a "2 ton" line shipped as qty 2 and ORION imported 2 kg
            # instead of 2000 kg (warehouse-reported on message 8700). Dividing the
            # per-line price by 1000 (€/ton -> €/kg) keeps R85's fallback comparison
            # against the catalog cena (already €/kg) apples-to-apples; an empty line
            # price stays 0 and R85 fills the €/kg cena, now correctly labelled kg.
            out_qty, unit_price, override_unit = qty * 1000, up / 1000, "kg"
            log.info("desadv line %r: tonne conversion %.3f ton -> %.3f kg @ %.3f", gtin,
                     qty, out_qty, unit_price)
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
        # text unchanged EXCEPT in the two branches that set `override_unit` — the
        # liquid-multipack branch (forces 'L') and the R84.2 tonne branch (forces 'kg',
        # since the quantity was converted to kg). ORION keys the import on the GTIN
        # card, not on this text, so it is informational only, but a future reader must
        # not have to reverse-engineer that from the JS the way the original node left
        # it implicit.
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

def generate_stable_doc_number(message_id: str) -> str:
    """#262: a STABLE fallback identity for a document whose extraction found NO
    doc number at all and never will (an informal delivery announcement written
    directly in a mail's body text — no printed document to carry a number) — keyed
    on the ORIGINATING MESSAGE, never wall-clock time, so every reprocessing attempt
    of the SAME message (a stale-claim reclaim, R10; an R17 transient retry; a
    quarantine attempt) produces the IDENTICAL string.

    `_generate_doc_number()` below (R83's byte-for-byte port of the production
    `generateDocNumber()`) is wall-clock-based BY DESIGN — it stays exactly that way,
    unchanged, for parity — and is deliberately NOT reused here: a document with no
    real doc number has NOTHING but `desadv_sent`'s (supplier_ean, doc_number) row to
    protect it from a duplicate ORION upload (`desadv.claim_send_or_identify()`), so
    a fallback whose VALUE changes between retries would defeat that protection
    outright — the exact class of bug #239 fixed one layer up, for the upload-retry
    path itself; this is the identical fix one layer earlier, deciding the
    document's identity before the first claim attempt is even made. Callers with a
    genuine extracted doc number never reach this function — `build()`'s own
    `extraction_doc_number or ...` fallback wins first, and the caller (`dl_worker.
    _process_document`) only reaches THIS function when extraction produced none.

    Prefixed `AVIZO` ("avízo" = a delivery advice note in Slovak logistics usage,
    matching how #262's own ticket already names this class of mail) — unmistakably
    synthetic, never confusable with a real printed doc number's shape (plain digits,
    or an LT-prefixed code, per R47). The numeric suffix is guaranteed non-empty
    (`build()`'s own `re.sub(r"[^0-9]", "", doc_number)` needs real digits — ORION/
    EDITEL parses the EDI HDR field as a NUMBER; an all-non-digit fallback would fall
    back to the raw string and risk the exact import crash R83 exists to prevent).

    NOT a general substitute for `_generate_doc_number()`: it protects every RETRY of
    ONE message, never a genuine resend of the same physical delivery under a
    DIFFERENT message — there is no printed number to recognize that case by, so a
    supplier's own resend of a numberless announcement is (correctly) accepted as a
    new document, same as a warehouse worker reading two separate emails with no
    doc number to compare would do."""
    digest = hashlib.sha256((message_id or "").encode("utf-8", "surrogatepass")).hexdigest()
    numeric = str(int(digest[:16], 16))[-10:].zfill(10)
    return f"AVIZO{numeric}"


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
    name = filename(customer_ean_edi, extraction.get("deliveryDate") or "", doc_number)
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
