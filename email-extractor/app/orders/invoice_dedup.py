"""The invoice-as-DL duplicate rules (#485) — never ship a DESADV for goods already taken in.

#406/#412 let a flagged supplier's INVOICE stand in for its delivery note
(`dl_supplier_overrides.invoice_is_delivery_note`). Its only dedup was the `desadv_sent` claim
on `(supplier_ean, doc_number)` — but an invoice and the physical delivery note rarely share a
number: the warehouse often types the INVOICE number into CODEX's DL field, and LESAFFRE prints
another DL number on the invoice than CODEX has. Incident (Zeelandia 9.9.): the warehouse took
the delivery in by hand at 13:34, we uploaded a DESADV from the invoice (found in spam) at
19:27. The rules (wired by `dl_invoice` / `dl_document`, all READ-only):

- **Credit note** — the word „dobropis" in the mail's subject or own text, or in an
  attachment's file name or HEADER (`is_credit_note_text`, `CREDIT_HEADER_CHARS`: never the
  whole invoice text, a footer about credit notes must not kill an invoice), or a negative
  total (`is_credit_note_doc`) — never shipped (a dobropis is no delivery).
- **Already received / already sent** (`find_duplicate`): same supplier AND
  - a number match — the document's DL number or invoice number equals (digits only, ≥ 5
    digits, leading zeros ignored) a receipt's DL number / linked invoice number, or one of our
    `desadv_sent` rows' doc / invoice number; OR
  - the delivery date within ±1 day of the receipt (`receipt_date`…`receipt_date_to`) / our
    row's `delivery_date`, AND the total within max(0.50 €, 1 %) of the receipt total, the
    linked invoice's total, the sum of the receipts sharing that invoice, or our row's
    `total_amount` — or, against our own row, the SAME content (`signature`: the [card,
    quantity] pairs of the generated EDI — what a priceless DL scan still has).
  Against the CODEX receipts (`codex_receipts`, fresh only) and our own `desadv_sent` for that
  supplier — never the row of THIS very document (same message + same doc number: its own
  earlier attempt is the claim's `already_shipped_this_run` business, and an orphan claim must
  stay reclaimable), but another document of the same mail counts (an invoice PDF and the DL
  PDF of the same goods in one mail are ONE delivery).
- **Newest version wins** is not a rule here: `dl_message._claim_invoice` takes the NEWEST
  waiting invoice first, so an older version of the same invoice then meets the newer's
  `desadv_sent` row by number (never a second DESADV), and a newer mail that ships nothing (a
  reminder, a held version) never blocks the older one.

A false "duplicate" costs a hand-entered receipt (the status quo before #406); a missed one a
second delivery in ORION — so every rule errs to "duplicate". The DL path (`dodacie_listy`)
uses only the last check, against our invoice-derived rows (`invoice_only`).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import desadv_edi, dl_match

log = logging.getLogger("orders.invoice_dedup")

MIN_NUMBER_DIGITS = 5            # „123" must never equal another document's „123"
DATE_WINDOW_DAYS = 1
TOTAL_TOLERANCE_FLOOR_EUR = 0.50
TOTAL_TOLERANCE_PCT = 0.01
CREDIT_HEADER_CHARS = 300        # LESAFFRE prints „Faktúra - dobropis" as the title (pos 11)

_CREDIT_RE = re.compile(r"\bdobropis")      # matched on `dl_match.fold`ed text
_DMY_RE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*$")

OUTCOME_CREDIT_NOTE = "credit_note"
SOURCE_CODEX = "codex"
SOURCE_DESADV = "desadv"


def digits(value) -> str:
    """A document number reduced to its digits, leading zeros dropped — '' when fewer than
    `MIN_NUMBER_DIGITS` remain (CODEX stores numbers as DOUBLE: no prefix, no leading zero;
    `AVIZO9572455748` / `2610LT…` / `0100237291` must still meet their CODEX twin)."""
    d = re.sub(r"\D", "", str(value or "")).lstrip("0")
    return d if len(d) >= MIN_NUMBER_DIGITS else ""


def numbers_of(*values) -> set[str]:
    out: set[str] = set()
    for v in values:
        for item in (v if isinstance(v, (list, tuple)) else [v]):
            d = digits(item)
            if d:
                out.add(d)
    return out


def parse_day(value) -> date | None:
    """`DD.MM.YYYY` (the extraction's normalized form) or ISO `YYYY-MM-DD` → a date."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    m = _DMY_RE.match(s)
    try:
        if m:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _num(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def doc_total(doc: dict) -> float | None:
    """The document total without VAT: the printed `documentTotalWithoutVAT` when set (a
    credit note's is negative), else Σ line `totalPrice` (None when no line has one)."""
    printed = _num(doc.get("documentTotalWithoutVAT"))
    if printed:
        return round(printed, 2)
    lines: list[float] = [v for v in (_num(i.get("totalPrice"))
                                      for i in (doc.get("items") or [])) if v]
    return round(sum(lines), 2) if lines else None


def tolerance(total: float) -> float:
    return max(TOTAL_TOLERANCE_FLOOR_EUR, TOTAL_TOLERANCE_PCT * abs(total))


def is_credit_note_text(*texts: str) -> bool:
    """„dobropis" in any of the given SHORT texts (a subject, a mail's own text, a file name, a
    document header) — measured 2026-10-05 on the 23 invoices of the 5 flagged suppliers: the
    word appears in the credit notes only, never in a regular invoice."""
    return any(_CREDIT_RE.search(dl_match.fold(t)) for t in texts if t)


def is_credit_note_doc(doc: dict) -> bool:
    total = doc_total(doc)
    return total is not None and total < 0


def signature(content: str) -> list[list]:
    """The content signature of a generated DESADV: its LIN [card code, quantity] pairs,
    sorted — quantities as `generate()` normalized them to the card's unit (kg / pieces), so a
    DL scan and the invoice of the same goods yield the same pairs whatever units they print."""
    out = []
    for code, qty in desadv_edi.lin_quantities(content):
        q = _num(qty)
        out.append([code, round(q, 3) if q is not None else qty])
    return sorted(out, key=lambda p: (p[0], str(p[1])))


@dataclass
class Duplicate:
    """Why a document is not shipped: `source` codex/desadv, `match` number/date_total/
    date_content."""
    source: str
    match: str
    ref: str
    detail: dict = field(default_factory=dict)

    def reason(self) -> str:
        how = {"number": "rovnaké číslo dokladu",
               "date_total": "rovnaký dátum dodania (±1 deň) a suma",
               "date_content": "rovnaký dátum dodania (±1 deň) a rovnaké položky"}[self.match]
        if self.source == SOURCE_CODEX:
            return f"Už prijaté v CODEXe (príjemka {self.ref}, {how})"
        return f"Už odoslané do ORIONu (dodací list {self.ref}, {how})"

    def as_dict(self) -> dict:
        return {"source": self.source, "match": self.match, "ref": self.ref, **self.detail}


def _near(day: date | None, start: date | None, end: date | None = None) -> bool:
    if day is None or start is None:
        return False
    end = end or start
    return (start - timedelta(days=DATE_WINDOW_DAYS) <= day
            <= end + timedelta(days=DATE_WINDOW_DAYS))


def _close(total: float | None, candidates) -> float | None:
    if total is None:
        return None
    for c in candidates:
        if c is not None and abs(float(c) - total) <= tolerance(total):
            return float(c)
    return None


def _codex_duplicate(receipts: list, ours: set[str], day: date | None,
                     total: float | None) -> Duplicate | None:
    by_invoice: dict[str, float] = {}
    for r in receipts:
        key = digits(r.invoice_number)
        if key and r.total is not None:
            by_invoice[key] = by_invoice.get(key, 0.0) + r.total
    for r in receipts:
        hit = ours & numbers_of(r.dl_numbers, r.invoice_number, r.invoice_vs)
        if hit:
            return Duplicate(SOURCE_CODEX, "number", r.receipt_number,
                             {"number": sorted(hit)[0]})
    for r in receipts:
        if not _near(day, r.receipt_date, r.receipt_date_to):
            continue
        same = _close(total, (r.total, r.invoice_total,
                              by_invoice.get(digits(r.invoice_number))))
        if same is not None:
            return Duplicate(SOURCE_CODEX, "date_total", r.receipt_number,
                             {"receipt_date": r.receipt_date.isoformat(), "total": same})
    return None


def _items(value) -> list | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return [list(p) for p in value] if isinstance(value, list) and value else None


def _desadv_duplicate(conn, supplier_ean: str, message_id: str, doc_number: str,
                      ours: set[str], day: date | None, total: float | None,
                      content: list | None, invoice_only: bool) -> Duplicate | None:
    rows = conn.execute(
        "SELECT doc_number, invoice_number, delivery_date, total_amount, items FROM desadv_sent "
        "WHERE supplier_ean = %s "
        "AND NOT (message_id IS NOT DISTINCT FROM %s AND doc_number = %s)"
        + (" AND invoice_number IS NOT NULL" if invoice_only else "") + " ORDER BY id",
        (supplier_ean, message_id, doc_number)).fetchall()
    for row_doc, row_invoice, *_rest in rows:
        hit = ours & numbers_of(row_doc, row_invoice)
        if hit:
            return Duplicate(SOURCE_DESADV, "number", row_doc, {"number": sorted(hit)[0]})
    for row_doc, _inv, delivered, amount, items in rows:
        if not _near(day, delivered):
            continue
        if _close(total, (amount,)) is not None:
            return Duplicate(SOURCE_DESADV, "date_total", row_doc,
                             {"delivery_date": delivered.isoformat(), "total": float(amount)})
        if content and _items(items) == content:
            return Duplicate(SOURCE_DESADV, "date_content", row_doc,
                             {"delivery_date": delivered.isoformat()})
    return None


def find_duplicate(conn, receipts, supplier_ean: str, doc: dict, message_id: str, *,
                   doc_number: str, content: list | None = None,
                   invoice_only: bool = False) -> Duplicate | None:
    """Is this document already received in CODEX (`receipts` — a fresh
    `codex_receipts.Receipts`, None = skip that side) or already shipped by us (`desadv_sent`;
    `invoice_only` = only our invoice-derived rows, the DL path's check)? `doc_number` = the
    number this document claims under (its own row is never a twin); `content` = its EDI
    `signature` when already built. Read-only; logs the verdict."""
    ean = str(supplier_ean or "")
    if not ean:
        return None
    ours = numbers_of(doc.get("docNumber"), doc.get("invoiceNumber"), doc_number)
    day = parse_day(doc.get("deliveryDate"))
    total = doc_total(doc)
    dup = None
    if receipts is not None:
        dup = _codex_duplicate(receipts.for_supplier(conn, ean), ours, day, total)
    if dup is None:
        dup = _desadv_duplicate(conn, ean, message_id, doc_number, ours, day, total, content,
                                invoice_only)
    log.info("dedup %s doc %s (supplier %s, numbers %s, date %s, total %s, items %s, "
             "invoice_only=%s): %s", message_id, doc_number, ean, sorted(ours), day, total,
             len(content) if content else 0, invoice_only,
             dup.as_dict() if dup else "no duplicate")
    return dup
