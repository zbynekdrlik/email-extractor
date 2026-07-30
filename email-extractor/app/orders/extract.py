"""Extraction: email text -> orders (#61).

Authority order, deliberately:

1. **A recognized tabular attachment is parsed by CODE and overrides the model.** A grid
   is deterministic; the model reading a grid is not. The live pipeline already does this
   (buried inside its citation validator) — here it is the first thing that happens.
2. **Whatever the model returns is citation-checked** against the source text, so an
   invented item cannot reach ORION.
3. **An unverifiable item is reported, never dropped.** The warehouse adds it by hand.

The prompt lives in `prompts/extract.md` so a prompt change shows up as a diff and as a
cache miss; its hash is recorded on the run.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path

log = logging.getLogger("orders.extract")

PROMPT_PATH = Path(__file__).with_name("prompts") / "extract.md"
# A template read as an order shows up as many items all with quantity 1.
PRICELIST_MIN_ITEMS = 10
ZERO_WIDTH = dict.fromkeys(
    [0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0x2061, 0x2062, 0x2063, 0x2064,
     0xFEFF, 0x00AD, 0x180E], " ")

ORDER_SCHEMA = {
    "type": "object",
    "properties": {
        "senderName": {"type": "string"},
        "senderEmail": {"type": "string"},
        "companyName": {"type": "string"},
        "isChangeRequest": {"type": "boolean"},
        "notes": {"type": "string"},
        "orders": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "orderNumber": {"type": "string"},
                "deliveryDate": {"type": "string"},
                "recipientGroup": {"type": "string"},
                "items": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "quantity": {"type": "number"},
                        "unit": {"type": "string"},
                        "sourceQuote": {"type": "string"},
                    },
                    "required": ["name", "quantity", "unit", "sourceQuote"],
                }},
            },
            "required": ["deliveryDate", "items"],
        }},
    },
    "required": ["orders"],
}


def clean_text(text: str) -> str:
    """Strip invisible format characters.

    Some mail clients send a zero-width space inside "45​ks"; the model, the citation
    check and the parser must all see the same string or a real item silently becomes
    unverified (#41, the AGEL incident).
    """
    return str(text or "").translate(ZERO_WIDTH)


def prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


DAYS_SK = ("pondelok", "utorok", "streda", "štvrtok", "piatok", "sobota", "nedeľa")


def today_header(today=None) -> str:
    """The date line the prompt counts relative dates from.

    Without it the model dates "zajtra" / "pondelok" from its own training cutoff, which
    is a silently wrong delivery date — found in the first live run of this stage.
    Passing `today` in makes a golden-corpus case reproducible years later.
    """
    import datetime
    if isinstance(today, str) and today:
        day = datetime.date.fromisoformat(today)
    elif isinstance(today, datetime.date):
        day = today
    else:
        day = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=2))).date()
    return (f"Dnešný dátum: {day.strftime('%d.%m.%Y')}, deň: {DAYS_SK[day.weekday()]}, "
            f"rok: {day.year}. Relatívne aj predvolené dátumy (zajtra, pondelok, "
            f"„na 26.6\", default tomorrow) počítaj VŽDY od tohto dňa; nikdy nehádaj.")


# --- 1) deterministic table parsing --------------------------------------

def _attachment_block(text: str) -> list[str] | None:
    idx = text.find("Attachments:")
    if idx == -1:
        return None
    return text[idx:].splitlines()


def parse_table(text: str) -> list[dict] | None:
    """Items from a recognized tabular attachment, or None when there is no such table."""
    lines = _attachment_block(clean_text(text))
    if not lines:
        return None
    for i, line in enumerate(lines):
        if "Č.mat.dodavat." in line or ("EAN kus" in line and "Název" in line):
            return _parse_kosik(lines, i)
        if "%DPH" in line and ("VO bez DPH" in line or "VO s DPH" in line):
            return _parse_pricelist(lines, i)
    return None


def _rows_after(lines: list[str], header_idx: int):
    for line in lines[header_idx + 1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("====="):
            break
        yield stripped


def _parse_kosik(lines: list[str], header_idx: int) -> list[dict] | None:
    header = [h.strip() for h in lines[header_idx].split(",")]
    try:
        name_i, qty_i = header.index("Název"), header.index("Množství")
    except ValueError:
        return None
    items = []
    for row in _rows_after(lines, header_idx):
        cols = row.split(",")
        if len(cols) <= max(name_i, qty_i):
            continue
        name = cols[name_i].strip().strip('"')
        qty = _number(cols[qty_i])
        if name and qty and qty > 0:
            items.append({"name": name, "quantity": round(qty), "unit": "ks"})
    return items or None


def _parse_pricelist(lines: list[str], header_idx: int) -> list[dict] | None:
    """The order quantity is the column AFTER the last labelled header column.

    Templates differ in how many price columns they carry (5 or 7), so the header row
    decides — a fixed index reads a price as a quantity, which is how a 1.50 € price
    once became "1.5 pieces".
    """
    header = lines[header_idx].split("\t")
    last_labelled = max((i for i, h in enumerate(header) if h.strip()), default=0)
    qty_i = last_labelled + 1
    items = []
    for row in _rows_after(lines, header_idx):
        cols = row.split("\t")
        name = (cols[0] if cols else "").strip().strip('"')
        if not name:
            continue
        # A product row carries prices; a note or a section title does not.
        if not any((_number(c) or 0) > 0 for c in cols[1:last_labelled + 1]):
            continue
        qty = _number(cols[qty_i]) if len(cols) > qty_i else None
        if qty and qty > 0:
            items.append({"name": name, "quantity": round(qty), "unit": "ks"})
    return items or None


def _number(value) -> float | None:
    try:
        return float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


# --- 2) citation checking ------------------------------------------------

def _fold(text: str) -> str:
    s = unicodedata.normalize("NFD", clean_text(text).lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip()


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _fold(text))


def quote_in_source(quote: str, source: str) -> bool:
    if not quote or len(quote) < 5:
        return False
    folded_source = _fold(source)
    if _fold(quote) in folded_source:
        return True
    squashed = _squash(quote)
    if len(squashed) >= 8 and squashed in _squash(source):
        return True
    # The model sometimes quotes a whole table row; half its cells matching the source is
    # enough proof that the row exists.
    parts = [p.strip() for p in re.split(r"[\t,;|]+", _fold(quote)) if len(p.strip()) > 3]
    if not parts:
        return False
    hits = sum(1 for p in parts if p in folded_source)
    return hits >= -(-len(parts) // 2)


def item_in_source(item: dict, source: str) -> bool:
    """The item LINE proves the item even when the quote does not exist contiguously.

    VERIFY sometimes glues a section header onto the item line; ČSB (2026-07-23) shipped
    1 of 9 items because of it. A phantom item still fails — it is nowhere in the source.
    """
    name = _fold(item.get("name", ""))
    qty = _number(item.get("quantity"))
    if len(name) < 4 or not qty or qty <= 0:
        return False
    folded_source = _fold(source)
    n = str(round(qty))
    for candidate in (f"{n}x {name}", f"{n} x {name}", f"{n}x{name}",
                      f"{n}ks {name}", f"{n} ks {name}", f"{name} {n}"):
        if candidate in folded_source:
            return True
    squashed = _squash(f"{n}x{name}")
    return len(squashed) >= 8 and squashed in _squash(source)


def verify(extracted: dict, source: str) -> dict:
    """Keep only items provable from the source; report the rest.

    An order left with no provable item is dropped entirely — there is nothing to ship —
    but its items still appear in `unverified`, so nothing disappears quietly.
    """
    orders, unverified = [], []
    for order in extracted.get("orders", []) or []:
        kept = []
        for item in (order.get("items") or []) + (order.get("missedItems") or []):
            if str(item.get("status", "")).upper() == "PHANTOM":
                unverified.append(_plain(item))
                continue
            if quote_in_source(item.get("sourceQuote", ""), source) or \
                    item_in_source(item, source):
                kept.append(_plain(item))
            else:
                unverified.append(_plain(item))
        if kept:
            orders.append({"orderNumber": order.get("orderNumber", "") or "",
                           "deliveryDate": order.get("deliveryDate", "") or "",
                           "recipientGroup": order.get("recipientGroup", "") or "",
                           "items": kept})
    if unverified:
        log.warning("%d item(s) could not be verified against the email: %s",
                    len(unverified), [i["name"] for i in unverified])
    return {
        "senderName": extracted.get("senderName", "") or "",
        "senderEmail": extracted.get("senderEmail", "") or "",
        "companyName": extracted.get("companyName", "") or "",
        "isChangeRequest": bool(extracted.get("isChangeRequest")),
        "notes": extracted.get("notes", "") or "",
        "orders": orders,
        "unverified": unverified,
    }


def _plain(item: dict) -> dict:
    return {"name": item.get("name", ""), "quantity": item.get("quantity"),
            "unit": item.get("unit", "ks") or "ks"}


# --- 3) sanity guard -----------------------------------------------------

def sanity(extracted: dict) -> str | None:
    """A reason to refuse the whole extraction, or None when it looks like an order."""
    items = [i for o in extracted.get("orders", []) or [] for i in (o.get("items") or [])]
    if len(items) > PRICELIST_MIN_ITEMS and all(
            _number(i.get("quantity")) == 1 for i in items):
        return (f"Podozrenie na cenník — všetky položky ({len(items)}) majú množstvo 1. "
                "Pravdepodobne sa spracovala šablóna cenníka namiesto objednávky.")
    return None


# --- the stage -----------------------------------------------------------

def run(client, email: dict) -> dict:
    """Extract orders from one email. `client` is an `llm.Client`.

    A recognized table replaces the model's item list but keeps its header fields (dates,
    PO number, sender) — those are not in the grid.
    """
    source = clean_text(email.get("combined_text") or "")
    user = (f"{today_header(email.get('today'))}\n\n"
            f"FROM: {email.get('from_name', '')} <{email.get('from_addr', '')}>\n"
            f"SUBJECT: {email.get('subject', '')}\n\n--- ORDER EMAIL ---\n{source}\n--- END ---")
    extracted = client.json_call(prompt(), user, ORDER_SCHEMA, name="orders")

    refusal = sanity(extracted)
    if refusal:
        return {"orders": [], "unverified": [], "refusal": refusal,
                "isChangeRequest": bool(extracted.get("isChangeRequest")),
                "notes": extracted.get("notes", "") or "",
                "senderName": extracted.get("senderName", "") or "",
                "senderEmail": extracted.get("senderEmail", "") or "",
                "companyName": extracted.get("companyName", "") or "",
                "prompt_hash": client.last_prompt_hash}

    table = parse_table(source)
    if table:
        head = (extracted.get("orders") or [{}])[0]
        log.info("tabular attachment recognized: %d item(s) parsed by code (model said %d)",
                 len(table), len(head.get("items") or []))
        result = {
            "senderName": extracted.get("senderName", "") or "",
            "senderEmail": extracted.get("senderEmail", "") or "",
            "companyName": extracted.get("companyName", "") or "",
            "isChangeRequest": bool(extracted.get("isChangeRequest")),
            "notes": extracted.get("notes", "") or "",
            "orders": [{"orderNumber": head.get("orderNumber", "") or "",
                        "deliveryDate": head.get("deliveryDate", "") or "",
                        "recipientGroup": head.get("recipientGroup", "") or "",
                        "items": table}],
            "unverified": [],
            "source": "table",
        }
    else:
        result = dict(verify(extracted, source), source="model")
    result["prompt_hash"] = client.last_prompt_hash
    return result
