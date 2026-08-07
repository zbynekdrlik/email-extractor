"""Delivery-notes (DL) extraction: Vision + multi-document schema (#201, DL migration F2).

Ports the live n8n "Dodacie Listy EDI" / "DL — Vision + Extract" sub-workflow's extraction
stage into Python, fixing four structural losses the binding spec documents
(docs/superpowers/specs/2026-08-07-delivery-notes-python-design.md, R40-R52, W1a-W1c/W13):

- **W1a** every attachment of the mail is processed (`extract_email`), not just the first
  PDF the n8n `Get Attachment Meta ... LIMIT 1` picked.
- **W1b** one attachment can carry MORE THAN ONE delivery note — the schema returns a LIST
  of documents (`{"documents": [...]}`), never a single-document contract.
- **W1c** a multi-page scan transcribes EVERY embedded page image, not just the largest one
  (scan CLASSIFICATION still keys on the largest page per R40 — only the transcription
  scope widened).
- **W13** a digital PDF whose extractor already produced real text never pays for a vision
  call at all (R42: raw extractor text wins; vision is a fallback only).

This module is deliberately standalone from `app/orders/extract.py` (the ai_orders engine's
own extraction stage) — see the design comment on #201 for why: DL validates against a
numeric money-gate/quantity-self-correction model (printed totals on a structured supplier
document), not `extract.py`'s citation-based verification against free-text customer email
prose. Conflating the two would make every future change to either reason about the other's
unrelated rules.

No DB, no worker/claim wiring, no matching (Sub2) or EDI assembly (Sub3) — the worker/shadow
integration is #204; matching and EDI-writing are later phases too. Everything here is pure
functions plus the `llm.Client` (json_call + the new vision_call, #201) — fully testable
offline with a scripted fake client, no network.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger("orders.dl_extract")

PROMPTS_DIR = Path(__file__).with_name("prompts")
EXTRACT_PROMPT_PATH = PROMPTS_DIR / "dl_extract.md"
VISION_PROMPT_PATH = PROMPTS_DIR / "dl_vision.md"

# R40: the largest embedded JPEG over this size classifies the PDF as a scan.
SCAN_JPEG_THRESHOLD_BYTES = 20_000

JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"

# R51: the EDI is built from line items, so a misread SUMMARY digit must not block — but a
# missing/extra/mistranscribed LINE must.
MONEY_GATE_TOLERANCE_EUR = 0.50

# R50: how far a derived quantity (totalPrice / unitPrice) may drift from the read one
# before it is even considered a misread, split by unit class.
QTY_TOLERANCE_PIECES = 0.5
QTY_TOLERANCE_KG = 0.005
PIECE_UNITS = {"ks", "ba", "kt", ""}

# R47: only the documented '<digits>LT<digits>' shape (e.g. '2610LT9999999999') is a
# genuine Lunys-style prefix — anchored to the WHOLE string, never a bare substring
# search, or a doc number that merely CONTAINS "lt" ('SALT-2026-0001', 'MULTI2026') gets
# silently corrupted (deep-review finding, #201).
_LT_PREFIX = re.compile(r"^\d*LT(\d+)$", re.IGNORECASE)
_DMY_DATE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

# The multi-document extraction schema (fixes W1b) — one entry per SEPARATE delivery note
# found in the (possibly cross-checked, R43) source text.
DL_SCHEMA = {
    "type": "object",
    "properties": {
        "documents": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "supplierName": {"type": "string"},
                "supplierCity": {"type": "string"},
                "supplierEmail": {"type": "string"},
                "docNumber": {"type": "string"},
                "deliveryDate": {"type": "string"},
                "deliveryTime": {"type": "string"},
                "documentTotalWithoutVAT": {"type": "number"},
                "items": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "quantity": {"type": "number"},
                        "unit": {"type": "string"},
                        "unitPrice": {"type": "number"},
                        "totalPrice": {"type": "number"},
                        "vatRate": {"type": "number"},
                    },
                    "required": ["name", "quantity", "unit"],
                }},
            },
            "required": ["items"],
        }},
    },
    "required": ["documents"],
}


def extract_prompt() -> str:
    return EXTRACT_PROMPT_PATH.read_text(encoding="utf-8")


def vision_prompt() -> str:
    return VISION_PROMPT_PATH.read_text(encoding="utf-8")


# --- 1) scan detection (R40, fixes W1c) -------------------------------------

def extract_embedded_jpegs(pdf_bytes: bytes | None) -> list[bytes]:
    """Every embedded JPEG image in the raw PDF bytes, in page order.

    Mirrors the n8n `Detect scan image` node's own heuristic exactly: scan the raw bytes
    for JPEG SOI (`\\xff\\xd8`) ... EOI (`\\xff\\xd9`) marker pairs. No real PDF/image codec
    is needed — a genuine JPEG stream embedded in a PDF always carries both markers, and a
    false positive here only widens the scan classification, never corrupts extraction (the
    bytes are sent straight to Vision, never decoded locally).
    """
    data = pdf_bytes or b""
    jpegs: list[bytes] = []
    pos = 0
    while True:
        start = data.find(JPEG_SOI, pos)
        if start == -1:
            break
        end = data.find(JPEG_EOI, start + len(JPEG_SOI))
        if end == -1:
            break
        end += len(JPEG_EOI)
        jpegs.append(data[start:end])
        pos = end
    return jpegs


def is_scanned(jpegs: list[bytes]) -> bool:
    """R40: the LARGEST embedded JPEG over 20kB classifies the document as a scan.

    Classification stays keyed on the largest page only (matching n8n's own threshold
    check) — it is the TRANSCRIPTION scope that widens to every page (W1c), via
    `extract_embedded_jpegs` already returning all of them.
    """
    return bool(jpegs) and max(len(j) for j in jpegs) > SCAN_JPEG_THRESHOLD_BYTES


# --- 2) text-source priority (R42, fixes W13) -------------------------------

def choose_source_text(scanned: bool, machine_text: str, vision_primary: str | None) -> str:
    """R42: a scan prefers its vision transcript (falling back to machine OCR only if
    vision failed outright); a digital PDF prefers its own raw extracted text, and only
    falls back to vision when that text is empty — never when it merely disagrees with
    vision. This is the priority rule that makes W13 possible: a digital PDF with real
    text simply never reaches vision at all (see `extract_attachment`)."""
    machine_text = machine_text or ""
    if scanned:
        return vision_primary or machine_text
    return machine_text or (vision_primary or "")


# --- 3) up-to-three-way cross-check (R43) -----------------------------------

def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def combine_transcripts(machine_text: str, primary: str, secondary: str | None) -> str:
    """R43: up to three transcripts of the SAME document, for cross-checking a silent
    digit misread — never new items, only alternative readings of the one document.

    `secondary` (the vision call's second independent sample) and `machine_text` (the
    extractor's own machine-OCR text) are each appended, clearly labelled, only when they
    actually differ from `primary` — an identical transcript would just double the token
    cost for zero cross-check value. The comparison is over the FULL normalized text, not
    a short prefix (deep-review finding, #201): a real delivery note's header — supplier,
    city, doc number, date — fills roughly the first 80 characters on its own, so an
    early version of this function that only compared a truncated head silently dropped
    the whole secondary transcript whenever two samples agreed on the header but disagreed
    on an ITEM further down — exactly the digit misread this cross-check exists to catch.
    """
    primary = primary or ""
    parts = [primary]
    primary_norm = _normalized(primary)
    if secondary and _normalized(secondary) != primary_norm:
        parts.append(
            "--- DRUHY NEZAVISLY VISION PREPIS (na krížovú kontrolu, "
            "NIE nový dokument) ---\n" + secondary)
    if machine_text and _normalized(machine_text) != primary_norm:
        parts.append(
            "--- ALTERNATIVNY STROJOVY OCR PREPIS (na krížovú kontrolu, "
            "NIE nový dokument) ---\n" + machine_text)
    return "\n\n".join(parts)


# --- 4) date normalization (fixes W12) --------------------------------------

def normalize_delivery_date(raw: str) -> str | None:
    """DD.MM.YYYY (zero-padded) from either a DD.MM.YYYY or an ISO YYYY-MM-DD input.

    Unparseable input returns None rather than silently producing a blank EDI date field
    (W12 — the orders pipeline had exactly this bug once already for `formatDate`)."""
    s = str(raw or "").strip()
    m = _DMY_DATE.match(s)
    if m:
        d, mo, y = m.groups()
        return f"{int(d):02d}.{int(mo):02d}.{y}"
    m = _ISO_DATE.match(s)
    if m:
        y, mo, d = m.groups()
        return f"{int(d):02d}.{int(mo):02d}.{y}"
    return None


# --- 5) doc number LT-prefix strip (R47) ------------------------------------

def strip_lt_prefix(doc_number: str) -> str:
    """'2610LT9999999999' -> '9999999999': ORION parses the EDI's NCDLIST as a float, so a
    letter prefix crashes the import — the leading digits (if any) + 'LT' are dropped,
    keeping only the digits after it. Anchored to the whole string (R47's documented
    shape only), so a doc number that merely CONTAINS 'lt'/'LT' without being that exact
    shape is left untouched."""
    s = str(doc_number or "")
    m = _LT_PREFIX.match(s)
    return m.group(1) if m else s


# --- 6) validation (R50-R52) -------------------------------------------------

def _num(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_qty(value: float, unit: str) -> float:
    u = (unit or "").strip().lower()
    return round(value) if u in PIECE_UNITS else round(value, 3)


def self_correct_quantity(item: dict) -> dict:
    """R50: replace a misread quantity with totalPrice/unitPrice when the derived value
    BOTH differs meaningfully from what was read AND itself satisfies the line equation —
    a quantity that already reconciles, or a derived value that reconciles with nothing,
    is left exactly as read. The original value is kept in `_qtyOcr` for traceability."""
    unit_price = _num(item.get("unitPrice"))
    total_price = _num(item.get("totalPrice"))
    read_qty = _num(item.get("quantity"))
    if not unit_price or not total_price or read_qty is None:
        return dict(item)
    unit = item.get("unit", "") or ""
    derived = _round_qty(total_price / unit_price, unit)
    tolerance = QTY_TOLERANCE_PIECES if unit.strip().lower() in PIECE_UNITS else QTY_TOLERANCE_KG
    if abs(derived - read_qty) <= tolerance:
        return dict(item)
    line_tolerance = max(0.02, 0.02 * total_price)
    if abs(derived * unit_price - total_price) > line_tolerance:
        return dict(item)
    out = dict(item)
    out["_qtyOcr"] = read_qty
    out["quantity"] = derived
    return out


def money_gate(document: dict) -> str | None:
    """R51: |Σ line totalPrice - documentTotalWithoutVAT| <= 0.50 EUR, only when a real
    (> 0) document total was actually read — a no-price scan has no summary to check
    against, and that is fine (the dual transcript + a later match gate cover it)."""
    doc_total = _num(document.get("documentTotalWithoutVAT"))
    if not doc_total or doc_total <= 0:
        return None
    items_total = sum(_num(i.get("totalPrice")) or 0.0 for i in document.get("items") or [])
    diff = abs(items_total - doc_total)
    if diff > MONEY_GATE_TOLERANCE_EUR:
        return (f"Súčet riadkov ({items_total:.2f} €) sa nezhoduje s dokladom "
                f"({doc_total:.2f} €), rozdiel {diff:.2f} € presahuje toleranciu "
                f"{MONEY_GATE_TOLERANCE_EUR:.2f} €")
    return None


def validate_document(document: dict) -> dict:
    """R50-R52 applied to one document: quantity self-correction on every item, then the
    zero-items and money-gate review gates. Never raises — a breach is reported on the
    document (`status`/`reviewReason`), never thrown away."""
    doc = dict(document)
    doc["items"] = [self_correct_quantity(i) for i in (document.get("items") or [])]
    if not doc["items"]:
        doc["status"] = "needsReview"
        doc["reviewReason"] = "Dokument neobsahuje žiadne položky"
        return doc
    reason = money_gate(doc)
    if reason:
        doc["status"] = "needsReview"
        doc["reviewReason"] = reason
        return doc
    doc["status"] = "valid"
    doc["reviewReason"] = ""
    return doc


# --- 7) the LLM extraction call (multi-document schema, fixes W1b) ---------

def run_extraction(client, source_text: str) -> dict:
    """One multi-document extraction call over `source_text` (already cross-checked by
    `combine_transcripts`, if applicable). `client` is an `llm.Client` (or any object
    exposing `json_call`)."""
    user = f"--- DELIVERY NOTE TEXT ---\n{source_text}\n--- END ---"
    extracted = client.json_call(extract_prompt(), user, DL_SCHEMA, name="dl_documents")
    documents = []
    for raw_doc in extracted.get("documents") or []:
        doc = dict(raw_doc)
        doc["deliveryDate"] = normalize_delivery_date(doc.get("deliveryDate", "")) or ""
        doc["docNumber"] = strip_lt_prefix(doc.get("docNumber", ""))
        documents.append(validate_document(doc))
    return {"documents": documents, "prompt_hash": client.last_prompt_hash}


# --- 8) one attachment end-to-end (vision routing) --------------------------

def extract_attachment(client, pdf_bytes: bytes, machine_text: str = "") -> dict:
    """Scan detection -> vision (only when actually needed, R42/W13) -> R43 cross-check
    -> multi-document extraction -> R50-R52 validation, for ONE attachment.

    `client` needs `vision_call` (llm.Client, #201) and `json_call`.
    """
    jpegs = extract_embedded_jpegs(pdf_bytes)
    scanned = is_scanned(jpegs)
    machine_text = machine_text or ""

    vision_used = False
    vision_primary: str | None = None
    vision_secondary: str | None = None
    if scanned:
        transcripts = client.vision_call(vision_prompt(), images=jpegs)
        vision_used = True
    elif not machine_text.strip():
        transcripts = client.vision_call(vision_prompt(), pdf_bytes=pdf_bytes)
        vision_used = True
    else:
        transcripts = []
    if transcripts:
        vision_primary = transcripts[0] if len(transcripts) > 0 else None
        vision_secondary = transcripts[1] if len(transcripts) > 1 else None

    primary_text = choose_source_text(scanned, machine_text, vision_primary)
    source_text = combine_transcripts(machine_text, primary_text, vision_secondary)

    extraction = run_extraction(client, source_text)
    return {"documents": extraction["documents"], "scanned": scanned,
            "vision_used": vision_used, "prompt_hash": extraction["prompt_hash"],
            "source_text": source_text}


# --- 9) every attachment of the mail (fixes W1a) ----------------------------

def extract_email(client, attachments: list[dict]) -> dict:
    """Every attachment of the message (fixes W1a — n8n's own `LIMIT 1` attachment pick).
    Each attachment can itself carry more than one delivery note (fixes W1b, via
    `extract_attachment`'s multi-document schema); every returned document is tagged with
    which attachment it came from, so a later phase can still act per-attachment (one
    attachment's needsReview must not block another's upload).

    `attachments`: list of `{"idx", "filename", "pdf_bytes", "machine_text"}`.
    """
    documents: list[dict] = []
    per_attachment: list[dict] = []
    for i, att in enumerate(attachments):
        idx = att.get("idx", i)
        filename = att.get("filename", "")
        result = extract_attachment(client, att.get("pdf_bytes") or b"",
                                    att.get("machine_text") or "")
        for doc in result["documents"]:
            documents.append(dict(doc, source_attachment_idx=idx,
                                  source_attachment_filename=filename))
        per_attachment.append({"idx": idx, "filename": filename,
                               "scanned": result["scanned"], "vision_used": result["vision_used"]})
    return {"documents": documents, "attachments": per_attachment}
