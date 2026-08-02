"""Order-line parsing for the n8n **Static auto orders** workflow's `extractor` node (#68,
workflow `O8IYhUESjaWmPMTI`, node "Order Extractor v2 — NEW combined_text layout").

Same relationship `static_ean.py` has to `generator`'s `getProductEAN()`: n8n's JS Code node
cannot run in this repo's CI, so this module is a hand-kept 1:1 PORT of `extractor`'s
regex-based parsing, and is what CI actually tests. **It is NOT imported by the live
pipeline** — Static auto orders still runs in n8n; this is the first safe, additive-only
layer of the "move onto the Python core" migration (#68), laying groundwork for a future
shadow-mode worker without touching production. Mirror any change here in the n8n Code node
(and vice versa) — same discipline `.claude/rules/orders-corpus.md` already requires for
`static_ean.py`.

The order text is deterministic, template-shaped mail from four partners/families:
KARMEN, KARMEN_CASH, KOMFOS, LABAS — "Vyšlá objednávka" (one line per item) or the BARIS
"ODBERATEĽ OBJEDNÁVKA" tabular layout (two lines per item). No LLM is involved; this is
pure regex parsing, faithfully ported.
"""
from __future__ import annotations

import math
import re

EXCLUDED_PRODUCTS = ["SLIMAK 80g VANILKOVY"]

_ASCII_MAP = {
    "á": "a", "ä": "a", "č": "c", "ď": "d", "é": "e", "í": "i", "ĺ": "l", "ľ": "l",
    "ň": "n", "ó": "o", "ô": "o", "ŕ": "r", "š": "s", "ť": "t", "ú": "u", "ý": "y", "ž": "z",
    "Á": "A", "Ä": "A", "Č": "C", "Ď": "D", "É": "E", "Í": "I", "Ĺ": "L", "Ľ": "L",
    "Ň": "N", "Ó": "O", "Ô": "O", "Ŕ": "R", "Š": "S", "Ť": "T", "Ú": "U", "Ý": "Y", "Ž": "Z",
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2015": "-",
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2026": "...",
    "\u00a0": " ", "\u00b0": "o",
}

# A mail client can glue an invisible Unicode format char between two tokens (observed: a
# zero-width space in "45\u200bks" — AGEL Levoca incident 2026-07-09, #41). Plain \s+
# regexes below do not match these, so a line would silently fail to parse. Cleaned once,
# before any parser runs — same codepoint set the Python ingest side (extract.py) uses.
_INVISIBLE_RE = re.compile(r"[\u200B-\u200F\u2060-\u2064\uFEFF\u00AD\u180E]")


class OrderExtractionError(Exception):
    """The order header is missing something extract_order_data cannot proceed without."""


class MissingInputText(Exception):
    """No order text at all — nothing to parse."""


class PhotoOrderNeedsVision(Exception):
    """A photo-only order (empty OCR body, an attachment present) — needs human/vision
    handling, not this deterministic parser (komfos.67, #47)."""


def to_ascii(s: str) -> str:
    """Fold known Slovak diacritics + smart punctuation to ASCII; drop anything else
    non-ASCII. Mirrors `extractor`'s `toAscii()` exactly (`c => map[c] || ''`)."""
    return "".join(c if ord(c) < 128 else _ASCII_MAP.get(c, "") for c in str(s or ""))


def clean_invisible(s: str) -> str:
    return _INVISIBLE_RE.sub(" ", str(s or ""))


def _round4(n: float) -> float:
    """`Math.round(n*10000)/10000` — JS rounds half AWAY FROM ZERO, not Python's
    round-half-to-even, so this is spelled out rather than using the builtin `round()`."""
    return math.floor(n * 10000 + 0.5) / 10000 if n >= 0 else -math.floor(-n * 10000 + 0.5) / 10000


def should_exclude_product(item: dict) -> bool:
    d = str(item.get("description") or "").upper()
    return any(ex.upper() in d for ex in EXCLUDED_PRODUCTS)


def parse_vysla_items(text: str) -> list[dict]:
    """Family A, "Vyšlá objednávka" — one line per item: `<code?> <ean?> <description>
    <qty>,DDD ks [<unitPrice>]`."""
    items: list[dict] = []
    sec = re.search(r"Mno[žz]stvo\s*\n([\s\S]*?)\n(?:N[áa]kupn[áa] cena spolu)", text)
    body = sec.group(1) if sec else text
    for raw in body.split("\n"):
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^(.*?)\s+(\d+[.,]\d{3})\s+ks(?:\s+([\d.,]+))?\s*$", line, re.I)
        if not m:
            continue
        quantity = float(m.group(2).replace(",", "."))
        unit_price = float(m.group(3).replace(",", ".")) if m.group(3) else None
        head = m.group(1).strip()
        ean = code = description = None
        ean_m = re.search(r"(\d{13})", head)
        if ean_m:
            ean = ean_m.group(1)
            idx = head.index(ean)
            code = head[:idx].strip() or None
            description = head[idx + len(ean):].strip()
        else:
            cm = re.match(r"^P?\s*(\d+)\s+(.+)$", head)
            if cm:
                code, description = cm.group(1), cm.group(2).strip()
            else:
                description = head
        item = {"code": code, "ean": ean, "description": description, "quantity": quantity,
                "unit": "ks", "unitPrice": unit_price,
                "lineTotal": _round4(quantity * unit_price) if unit_price is not None else None}
        if item["description"] and not should_exclude_product(item):
            items.append(item)
    return items


def parse_karmen_cash_items(text: str) -> list[dict]:
    """Family B (BARIS "ODBERATEĽ OBJEDNÁVKA"), KARMEN CASH AND CARRY layout — two
    physical lines per item: a data row, then the description on the NEXT line."""
    items: list[dict] = []
    sec = re.search(r"Katal[óo]gov[ée] [čc][íí]slo[^\n]*\n([\s\S]*?)\n(?:Pozn[áa]mka:|Rekapitul[áa]cia:)",
                    text)
    body = sec.group(1) if sec else text
    lines = [s.strip() for s in body.split("\n") if s.strip()]
    i = 0
    while i < len(lines):
        m = re.match(r"^(\d+)\s+(\S+)\s+(\d+[,.]\d+)\s+KS\s+(?:(.+?)\s+)?(\d+)\s+([\d,.]+)\s+([\d,.]+)$",
                     lines[i], re.I)
        if not m:
            i += 1
            continue
        quantity = float(m.group(3).replace(",", "."))
        ean_match = re.search(r"\d{13}", m.group(4) or "")
        ean = ean_match.group(0) if ean_match else None
        unit_price = float(m.group(6).replace(",", "."))
        code = m.group(2)
        description = (lines[i + 1] if i + 1 < len(lines) else "").strip()
        item = {"code": code, "ean": ean, "description": description, "quantity": quantity,
                "unit": "ks", "unitPrice": unit_price, "lineTotal": _round4(quantity * unit_price)}
        if not should_exclude_product(item):
            items.append(item)
        i += 2
    return items


def parse_labas_items(text: str) -> list[dict]:
    """Family B, LABAS layout — a data row, then an OPTIONAL "Kusový EAN kód:" line."""
    items: list[dict] = []
    sec = re.search(r"Celkom\s*\n([\s\S]*?)\n(?:Celkov[áa] hmotnos|Celkov[áa] suma)", text)
    body = sec.group(1) if sec else text
    lines = [s.strip() for s in body.split("\n") if s.strip()]
    i = 0
    while i < len(lines):
        m = re.match(
            r"^(\d+)\s+(\d+)\s+(\S+)\s+(.+?)\s+(\d+)\s+(\d+[,.]\d+)\s+ks\s+([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)$",
            lines[i], re.I)
        if not m:
            i += 1
            continue
        quantity = float(m.group(6).replace(",", "."))
        unit_price = float(m.group(8).replace(",", "."))
        code = m.group(2)
        description = m.group(4).strip()
        ean = None
        next_line = lines[i + 1] if i + 1 < len(lines) else ""
        em = re.search(r"Kusov[ýy] EAN k[óo]d:\s*(\d{13})", next_line)
        if em:
            ean = em.group(1)
            i += 1
        item = {"code": code, "ean": ean, "description": description, "quantity": quantity,
                "unit": "ks", "unitPrice": unit_price, "lineTotal": _round4(quantity * unit_price)}
        if not should_exclude_product(item):
            items.append(item)
        i += 1
    return items


def detect_partner(text: str) -> str:
    if re.search(r"CASH\s+AND\s+CARRY", text, re.I):
        return "KARMEN_CASH"
    if re.search(r"KOMFOS", text, re.I):
        return "KOMFOS"
    if re.search(r"LABA[ŠS]", text, re.I):
        return "LABAS"
    if re.search(r"KARMEN", text, re.I):
        return "KARMEN"
    return "UNKNOWN"


def parse_order_number(text: str) -> dict:
    m = re.search(r"Vy[šs]l[áa] objedn[áa]vka [čc]\.:\s*(\d+)\s*/\s*(\d+)", text)
    if m:
        return {"fullOrderNumber": f"{m.group(1)}/{m.group(2)}",
                "orderNumber": m.group(1), "orderYear": m.group(2)}
    m = re.search(r"OBJEDN[ÁA]VKA [čc]\.:\s*(\d+)", text, re.I)
    if m:
        return {"fullOrderNumber": m.group(1), "orderNumber": m.group(1),
                "orderYear": m.group(1)[:4]}
    return {"fullOrderNumber": None, "orderNumber": None, "orderYear": None}


def parse_delivery_date(text: str) -> str | None:
    for pattern in (r"Term[íi]n dod[áa]vky:\s*(\d{2}\.\d{2}\.\d{2,4})",
                    r"Term[íi]n dodania:\s*(\d{2}\.\d{2}\.\d{2,4})",
                    r"D[áa]tum dodania tovaru\s*:?\s*(\d{2}\.\d{2}\.\d{2,4})"):
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    return None


def parse_issue_date(text: str) -> str | None:
    m = re.search(r"D[áa]tum vystavenia:\s*(\d{2}\.\d{2}\.\d{2,4})", text)
    return m.group(1) if m else None


def parse_location(text: str, partner: str) -> dict:
    out: dict = {"prevNumber": None, "deliveryLocationName": None}
    if partner == "KARMEN":
        pm = re.search(r"Prev\.:(\d+)", text)
        if pm:
            out["prevNumber"] = pm.group(1)
        lm = re.search(r"KARMEN\s+(\d+),\s*([^\n]+)", text)
        if lm:
            out["deliveryLocationName"] = f"KARMEN {lm.group(1)}, {lm.group(2).strip()}"
            if not out["prevNumber"]:
                out["prevNumber"] = lm.group(1)
    elif partner == "KOMFOS":
        pm = re.search(r"Prev\.:(\d+)", text)
        if pm:
            out["prevNumber"] = pm.group(1)
        lm = re.search(r"Term[íi]n dod[áa]vky:[^\n]*\n(KOMFOS[^\n]+)", text)
        if lm:
            out["deliveryLocationName"] = lm.group(1).strip()
    elif partner == "KARMEN_CASH":
        cand = None
        bm = re.search(r"prev[áa]dzka:\s*KARMEN CASH AND CARRY\s+([^\n]+)", text, re.I)
        if bm:
            cand = bm.group(1)
        if not cand:
            pm = re.search(r"KARMEN CASH AND CARRY[^\n]*\n([^\n]+)\n([^\n]+)", text, re.I)
            if pm:
                cand = f"{pm.group(1)} {pm.group(2)}"
        out["deliveryLocationName"] = "KARMEN CASH AND CARRY" + (f", {cand.strip()}" if cand else "")
        out["prevNumber"] = cand.strip() if cand else "KARMEN CASH AND CARRY"
    elif partner == "LABAS":
        lm = (re.search(r"LABA[ŠS]\s+s\.r\.o\.\s+KS/OC\s+(.+?)\s+(?:MOBIL|E[\u2010\-]?mail|TEL)",
                        text, re.I)
              or re.search(r"LABA[ŠS]\s+s\.r\.o\.\s+KS/OC\s+([^\n]+)", text, re.I))
        if lm:
            out["prevNumber"] = lm.group(1).strip()
            out["deliveryLocationName"] = f"LABAS s.r.o. KS/OC {lm.group(1).strip()}"
    return out


def extract_order_data(text: str) -> dict:
    """The `extractor` node's `extractOrderData()` — orchestrates partner detection, header
    fields and the item-family dispatch. Raises `OrderExtractionError` when a delivery or
    issue date cannot be found (the node's own hard requirement); an order with a valid
    header but zero item lines is a SKIP, not an error (KARMEN and others occasionally send
    a PDF with no order lines, "BEZ OBJEDNAVKY")."""
    partner = detect_partner(text)
    on = parse_order_number(text)
    loc = parse_location(text, partner)
    result: dict = {
        "partner": partner, "orderNumber": on["orderNumber"], "orderYear": on["orderYear"],
        "fullOrderNumber": on["fullOrderNumber"], "prevNumber": loc["prevNumber"],
        "deliveryLocationName": loc["deliveryLocationName"],
        "deliveryDate": parse_delivery_date(text), "issueDate": parse_issue_date(text),
        "items": [], "itemCount": 0, "totalQuantity": 0,
    }
    if not result["deliveryDate"]:
        raise OrderExtractionError(
            f"Nepodarilo sa extrahovať dátum dodania! Partner: {partner}, "
            f"Objednávka: {result['fullOrderNumber'] or 'neznáma'}")
    if not result["issueDate"]:
        raise OrderExtractionError(
            f"Nepodarilo sa extrahovať dátum vystavenia! Partner: {partner}, "
            f"Objednávka: {result['fullOrderNumber'] or 'neznáma'}")
    if re.search(r"Kusov[ýy] EAN k[óo]d:", text):
        result["items"] = parse_labas_items(text)
    elif re.search(r"Int\. k[óo]d a n[áa]zov tovaru", text):
        result["items"] = parse_karmen_cash_items(text)
    else:
        result["items"] = parse_vysla_items(text)
    if not result["items"]:
        result["skip"] = True
        result["skipReason"] = "empty_order"
        return result
    result["itemCount"] = len(result["items"])
    result["totalQuantity"] = sum(it.get("quantity") or 0 for it in result["items"])
    return result


def parse_static_order(text: str, has_attachments: bool = False) -> dict:
    """The n8n MAIN section: clean invisible chars, guard against a photo-only order with
    no OCR'able body, then run the real extraction. `has_attachments` mirrors
    `$('Normalize').first().json.hasAttachments` in the n8n node."""
    input_text = clean_invisible(text)
    if not input_text:
        raise MissingInputText('Chýba vstupný text! Očakáva sa pole "text" s objednávkou.')
    bare = re.sub(r"Subject:|From:[^\n]*|Body:", "", input_text).strip()
    if has_attachments and len(bare) < 30:
        raise PhotoOrderNeedsVision(
            "Príloha je FOTKA — automat ju nevie prečítať. Môže to byť objednávka aj "
            "vratka/doklad: otvor dashboard (46.224.130.35:8099), pozri fotku a vybav ručne.")
    return extract_order_data(input_text)
