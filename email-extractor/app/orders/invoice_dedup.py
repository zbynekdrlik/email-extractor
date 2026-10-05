"""The invoice-as-DL duplicate gate (#485) — never ship a DESADV for goods already taken in.

#406/#412 let a flagged supplier's INVOICE stand in for its delivery note
(`dl_supplier_overrides.invoice_is_delivery_note`). Its only dedup was the `desadv_sent` claim
on `(supplier_ean, doc_number)` — but an invoice and the physical delivery note rarely share a
number: the warehouse often types the INVOICE number into CODEX's DL field, and LESAFFRE prints
another DL number on the invoice than CODEX has. Incident (Zeelandia 9.9.): the warehouse took
the delivery in by hand at 13:34, we uploaded a DESADV from the invoice (found in spam) at
19:27. So before an invoice-derived document may claim, `_process_document` runs, in order:

1. **Credit note** (`is_credit_note_text` before extraction — the word „dobropis" in the
   subject / text / file names; `is_credit_note_doc` after it — a negative total): never
   shipped (a dobropis is no delivery).
2. **Newer version** (`superseded_by`): the SAME sender sent a newer invoice mail carrying this
   invoice's number → this one is skipped and the newest is processed („brať najnovšiu").
   A newer CREDIT NOTE that merely cites the number never supersedes.
3. **Already received / already sent** (`find_duplicate`): same supplier AND
   - a number match — the invoice's DL number or invoice number equals (digits only, ≥ 5
     digits, leading zeros ignored) the receipt's DL number / linked invoice number, or our
     own `desadv_sent` row's doc / invoice number; OR
   - the delivery date within ±1 day of the receipt (`receipt_date`…`receipt_date_to`) / our
     row's `delivery_date`, AND the total within max(0.50 €, 1 %) of the receipt total, the
     linked invoice's total, the sum of the receipts sharing that invoice, or our row's
     `total_amount`.
   against the CODEX receipts (`codex_receipts`, fresh only — stale → the invoice is held,
   fail-closed) and our own `desadv_sent` for that supplier (rows of THIS message excluded:
   its own earlier attempt is the claim's `already_shipped_this_run` business). → no ship, no
   board question, outcome recorded on the run („už prijaté v CODEXe" / „už odoslané").

Everything here only READS. A false "duplicate" costs a hand-entered receipt (the status quo
before #406); a missed duplicate costs a second delivery in ORION — so every rule errs to
"duplicate"/"skip". The DL path (category `dodacie_listy`) never calls this module.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import dl_match

log = logging.getLogger("orders.invoice_dedup")

MIN_NUMBER_DIGITS = 5            # „123" must never equal another document's „123"
DATE_WINDOW_DAYS = 1
TOTAL_TOLERANCE_FLOOR_EUR = 0.50
TOTAL_TOLERANCE_PCT = 0.01
SUPERSEDE_WINDOW_DAYS = 14       # = delivery_notes_max_age_days default: the claim window

_CREDIT_RE = re.compile(r"\bdobropis")      # matched on `dl_match.fold`ed text
_DIGIT_RUN_RE = re.compile(r"\d+")
_DMY_RE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*$")

OUTCOME_CREDIT_NOTE = "credit_note"
OUTCOME_SUPERSEDED = "superseded"
# A duplicate invoice is a "duplicate" document (the W7 outcome every aggregate already knows);
# the source + match ride along in `dedup`.
SOURCE_CODEX = "codex"
SOURCE_DESADV = "desadv"


def digits(value) -> str:
    """A document number reduced to its digits, leading zeros dropped — '' when fewer than
    `MIN_NUMBER_DIGITS` remain (CODEX stores numbers as DOUBLE: no prefix, no leading zero;
    `AVIZO9572455748` / `2610LT…` / `0100237291` must still meet their CODEX twin)."""
    d = re.sub(r"\D", "", str(value or "")).lstrip("0")
    return d if len(d) >= MIN_NUMBER_DIGITS else ""


def numbers_of(*values) -> set[str]:
    return {d for d in (digits(v) for v in values) if d}


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
    """„dobropis" anywhere in the given texts (subject, mail/attachment text, file names) —
    measured 2026-10-05: 23 invoices of the 5 flagged suppliers, the word appears in the 4
    credit notes only (subject or PDF text), never in a regular invoice."""
    return any(_CREDIT_RE.search(dl_match.fold(t)) for t in texts if t)


def is_credit_note_doc(doc: dict) -> bool:
    total = doc_total(doc)
    return total is not None and total < 0


def _mentions(text: str, wanted: set[str]) -> bool:
    return any(run.lstrip("0") in wanted for run in _DIGIT_RUN_RE.findall(text or ""))


def superseded_by(conn, message: dict, numbers: set[str]) -> str | None:
    """The message_id of a NEWER invoice mail from the same sender that carries one of this
    invoice's `numbers` (subject or text) — the same invoice sent again (a corrected version,
    a re-send). None when there is none, when the numbers are unknown, or when the newer mail
    is a credit note (a dobropis cites the invoice it corrects, it never replaces it)."""
    if not numbers or not message.get("from_addr"):
        return None
    rows = conn.execute(
        """SELECT message_id, subject, combined_text FROM messages
            WHERE category = 'invoices' AND lower(from_addr) = lower(%s)
              AND message_id <> %s
              AND created_at > COALESCE((SELECT created_at FROM messages
                                          WHERE message_id = %s), now())
              AND created_at > now() - make_interval(days => %s)
            ORDER BY created_at DESC""",
        (message["from_addr"], message["message_id"], message["message_id"],
         SUPERSEDE_WINDOW_DAYS)).fetchall()
    for message_id, subject, text in rows:
        if is_credit_note_text(subject or "", text or ""):
            continue
        if _mentions(subject or "", numbers) or _mentions(text or "", numbers):
            return message_id
    return None


@dataclass
class Duplicate:
    """Why an invoice is not shipped: `source` codex/desadv, `match` number/date_total."""
    source: str
    match: str
    ref: str
    detail: dict = field(default_factory=dict)

    def reason(self) -> str:
        how = ("rovnaké číslo dokladu" if self.match == "number"
               else "rovnaký dátum dodania (±1 deň) a suma")
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
        hit = ours & numbers_of(r.dl_number, r.invoice_number, r.invoice_vs)
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


def _desadv_duplicate(conn, supplier_ean: str, message_id: str, ours: set[str],
                      day: date | None, total: float | None) -> Duplicate | None:
    rows = conn.execute(
        "SELECT doc_number, invoice_number, delivery_date, total_amount FROM desadv_sent "
        "WHERE supplier_ean = %s AND message_id IS DISTINCT FROM %s ORDER BY id",
        (supplier_ean, message_id)).fetchall()
    for doc_number, invoice_number, _day, _total in rows:
        hit = ours & numbers_of(doc_number, invoice_number)
        if hit:
            return Duplicate(SOURCE_DESADV, "number", doc_number, {"number": sorted(hit)[0]})
    for doc_number, _inv, delivered, amount in rows:
        if _near(day, delivered) and _close(total, (amount,)) is not None:
            return Duplicate(SOURCE_DESADV, "date_total", doc_number,
                             {"delivery_date": delivered.isoformat(), "total": float(amount)})
    return None


def find_duplicate(conn, receipts, supplier_ean: str, doc: dict,
                   message_id: str) -> Duplicate | None:
    """Is this invoice-derived document already received in CODEX (`receipts` — a fresh
    `codex_receipts.Receipts`) or already shipped by us (`desadv_sent`)? See the module
    docstring for the rules. Read-only; logs the verdict."""
    ean = str(supplier_ean or "")
    if not ean:
        return None
    ours = numbers_of(doc.get("docNumber"), doc.get("invoiceNumber"))
    day = parse_day(doc.get("deliveryDate"))
    total = doc_total(doc)
    dup = None
    if receipts is not None:
        dup = _codex_duplicate(receipts.for_supplier(conn, ean), ours, day, total)
    if dup is None:
        dup = _desadv_duplicate(conn, ean, message_id, ours, day, total)
    log.info("invoice dedup %s (supplier %s, numbers %s, date %s, total %s): %s",
             message_id, ean, sorted(ours), day, total,
             dup.as_dict() if dup else "no duplicate")
    return dup
