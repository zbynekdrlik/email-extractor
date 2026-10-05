"""The invoice-as-DL duplicate rules (#485) — never ship a DESADV for goods already taken in.

#406/#412 let a flagged supplier's INVOICE stand in for its delivery note
(`dl_supplier_overrides.invoice_is_delivery_note`). Its only dedup was the `desadv_sent` claim
on `(supplier_ean, doc_number)` — but an invoice and the physical delivery note rarely share a
number: the warehouse often types the INVOICE number into CODEX's DL field, and LESAFFRE prints
another DL number on the invoice than CODEX has. Incident (Zeelandia 9.9.): the warehouse took
the delivery in by hand at 13:34, we uploaded a DESADV from the invoice (found in spam) at
19:27. The rules (wired by `dl_invoice` / `dl_document`, all READ-only):

- **Credit note** — the word „dobropis" in an attachment's file name or HEADER
  (`is_credit_note_text`, `CREDIT_HEADER_CHARS`: never the whole invoice text, a footer about
  credit notes must not kill an invoice), in the subject / own text of a single-document mail
  (`dl_invoice.split_credit_notes`, posted for a human), or a negative total
  (`is_credit_note_doc`) — never shipped (a dobropis is no delivery).
- **Three verdicts** per document (`find_duplicate`, same supplier always): a plain DUPLICATE
  (provably the same delivery — not shipped, recorded, nothing posted), a CONFLICT
  (`Duplicate.conflict`: maybe the same delivery, not provable — never shipped, a review tells
  the warehouse), or none (it ships). Erring to "not shipped" is the owner's rule; making the
  unprovable cases VISIBLE is what keeps a genuinely new delivery from being lost silently.
  - **Number** (digits only, ≥ 5 digits, leading zeros ignored): the document's DL / invoice
    number equals a CODEX receipt's DL number / linked invoice number / VS, or one of our
    `desadv_sent` rows' doc / invoice number (our rows of the last `LEDGER_DAYS` — small
    suppliers' counters overlap over months). Our shipment from an EARLIER mail with another
    total / other items is a corrected version → conflict (on the invoice path; on the DL path
    a scan of what the invoice shipped is a plain duplicate). A receipt that IS CODEX's import
    of one of our shipments (its DL number = our doc number) never decides by number — our
    own row, which carries the facts, does.
  - **Date, CODEX**: a receipt within ±1 day (CODEX books the day it is typed) whose total,
    its invoice's total or the sum of the receipts sharing that invoice is within
    max(0.50 €, 1 %) — the SAME day is a duplicate (the incident shape), a neighbouring day a
    conflict (a standing order's previous receipt is no proof). Never a receipt linked to
    ANOTHER invoice than ours, never CODEX's import of our shipment of ANOTHER day.
  - **Date, our rows** (the SAME delivery day only): the same total or the same content
    (`signature`: the [card, quantity] pairs of the generated EDI — what a priceless DL scan
    still has) is a duplicate, a conflict when the two carry different invoice numbers (a
    reissue, or a second delivery that day); totals that cannot be compared (a priceless
    scan) with other content (other units) is a conflict; two known, different totals with
    other content is another delivery.
  - The early gate (before supplier / item matching, `content=None`) settles only what needs no
    content and defers the rest; `dl_invoice.twin_shipped` re-judges everything once the EDI is
    built (before any board question) and again under the per-supplier ship lock right before
    the claim (`dl_invoice.claim_unless_twin`), so two documents of one delivery never both
    pass.
  Never the row of THIS very document (same message + same doc number: its own earlier attempt
  is the claim's `already_shipped_this_run` business, and an orphan claim must stay
  reclaimable), and never a stale orphan claim (unconfirmed past `CLAIM_STALE_MINUTES` — those
  goods are not in ORION); another document of the same mail counts (an invoice PDF and the DL
  PDF of the same goods in one mail are ONE delivery).
- **Newest version wins** is not a rule here: `dl_message._claim_invoice` takes the NEWEST
  waiting invoice first, so an older version of the same invoice then meets the newer's
  `desadv_sent` row by number (a plain duplicate — the right content already went), and a newer
  mail that ships nothing (a reminder, a held version) never blocks the older one. A newer
  version arriving AFTER the older shipped is a conflict (above).

The DL path (`dodacie_listy`) uses only the own-ledger rules, against our invoice-derived rows
(`invoice_only` = rows of `invoices`-category mails).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import NamedTuple

from . import desadv, desadv_edi, dl_match

log = logging.getLogger("orders.invoice_dedup")

MIN_NUMBER_DIGITS = 5            # „123" must never equal another document's „123"
DATE_WINDOW_DAYS = 1            # CODEX books a receipt the day it is typed
LEDGER_DAYS = 60                # = the receipts push window
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
    sorted. Quantities are as `generate()` wrote them — converted to kg where R84 converts
    (kg-tracked cards: kg / tonne / per-piece mass), otherwise the printed count — and the
    unit is deliberately NOT part of the pair: „2 KAR" vs „2 ks" of one card on one day is
    treated as the same goods (erring to "duplicate"), „2 KAR" vs „24 ks" is not recognised."""
    out = []
    for code, qty in desadv_edi.lin_quantities(content):
        q = _num(qty)
        out.append([code, round(q, 3) if q is not None else qty])
    return sorted(out, key=lambda p: (p[0], str(p[1])))


@dataclass
class Duplicate:
    """Why a document is not shipped: `source` codex/desadv, `match` number/date_total/
    date_content/date. `conflict` = it is not provably the same delivery (the reason in a few
    words) — never shipped, but a human must look."""
    source: str
    match: str
    ref: str
    detail: dict = field(default_factory=dict)
    conflict: str = ""

    def reason(self) -> str:
        if self.source == SOURCE_CODEX:
            how = {"number": "rovnaké číslo dokladu",
                   "date_total": "dátum príjemky a suma"}[self.match]
            text = f"Už prijaté v CODEXe (príjemka {self.ref}, {how})"
        else:
            how = {"number": "rovnaké číslo dokladu",
                   "date_total": "rovnaký dátum dodania a suma",
                   "date_content": "rovnaký dátum dodania a rovnaké položky",
                   "date": "rovnaký dátum dodania"}[self.match]
            text = f"Už odoslané do ORIONu (dodací list {self.ref}, {how})"
        if self.conflict:
            text += (f" — nie je však isté, že ide o tú istú dodávku ({self.conflict}). Do "
                     f"ORIONu sa NEposiela; over v CODEXe: ak je to iná dodávka, prijmi ju "
                     f"ručne, ak opravená verzia, oprav príjemku")
        return text

    def as_dict(self) -> dict:
        out = {"source": self.source, "match": self.match, "ref": self.ref, **self.detail}
        if self.conflict:
            out["conflict"] = self.conflict
        return out


class _Row(NamedTuple):
    """One of our shipments (`desadv_sent`) with the facts `desadv.record_facts` wrote."""
    doc: str
    invoice: str | None
    delivered: date | None
    amount: float | None
    items: list | None
    shipped_from: datetime | None      # when the mail it shipped from arrived


def _near(day: date | None, start: date | None, end: date | None = None,
          window: int = DATE_WINDOW_DAYS) -> bool:
    if day is None or start is None:
        return False
    end = end or start
    return start - timedelta(days=window) <= day <= end + timedelta(days=window)


def _close(total: float | None, candidates) -> float | None:
    if total is None:
        return None
    for c in candidates:
        if c is not None and abs(float(c) - total) <= tolerance(total):
            return float(c)
    return None


def _conflict(total: float | None, candidates) -> str:
    """'' when the totals agree or cannot be compared; else „ours € oproti theirs €"."""
    known = [float(c) for c in candidates if c is not None]
    if total is None or not known or _close(total, known) is not None:
        return ""
    return f"{total:.2f} € oproti {known[0]:.2f} €"


def _other_invoice(ours: str, theirs) -> bool:
    """Both sides carry an invoice number and they differ — never the same delivery by date."""
    mine = digits(ours)
    other = numbers_of(theirs)
    return bool(mine and other and mine not in other)


def _items(value) -> list | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return [list(p) for p in value] if isinstance(value, list) and value else None


def _ledger(conn, supplier_ean: str, message_id: str, doc_number: str,
            invoice_only: bool) -> list[_Row]:
    """Our shipments of this supplier in the last `LEDGER_DAYS` — confirmed, or claimed and
    still fresh (mid-upload) — except THIS very document's own row; `invoice_only` = only those
    shipped from an invoice mail."""
    rows = conn.execute(
        "SELECT d.doc_number, d.invoice_number, d.delivery_date, d.total_amount, d.items, "
        "(SELECT m.created_at FROM messages m WHERE m.message_id = d.message_id) "
        "FROM desadv_sent d WHERE d.supplier_ean = %s "
        "AND d.sent_at > now() - make_interval(days => %s) "
        "AND (d.uploaded_at IS NOT NULL OR d.sent_at > now() - make_interval(mins => %s)) "
        "AND NOT (d.message_id IS NOT DISTINCT FROM %s AND d.doc_number = %s)"
        + (" AND EXISTS (SELECT 1 FROM messages m WHERE m.message_id = d.message_id "
           "AND m.category = 'invoices')" if invoice_only else "")
        + " ORDER BY d.id",
        (supplier_ean, LEDGER_DAYS, desadv.CLAIM_STALE_MINUTES, message_id,
         doc_number)).fetchall()
    return [_Row(str(r[0]), r[1], r[2], float(r[3]) if r[3] is not None else None,
                 _items(r[4]), r[5]) for r in rows]


def _later(received_at, shipped_from) -> bool:
    return received_at is not None and shipped_from is not None and received_at > shipped_from


def _explaining(receipt, rows: list[_Row]) -> list[_Row]:
    """Our shipments this CODEX receipt is the import of (its DL number = our doc number)."""
    nums = numbers_of(receipt.dl_numbers)
    return [r for r in rows if r.delivered is not None and digits(r.doc) in nums]


def _own_number(rows: list[_Row], ours: set[str], total: float | None,
                content: list | None, received_at, invoice_only: bool
                ) -> tuple[Duplicate | None, bool]:
    """(verdict, deferred): deferred = only the EDI's content can tell (early gate)."""
    for row in rows:
        hit = ours & numbers_of(row.doc, row.invoice)
        if not hit:
            continue
        dup = Duplicate(SOURCE_DESADV, "number", row.doc, {"number": sorted(hit)[0]})
        if invoice_only or not _later(received_at, row.shipped_from):
            return dup, False
        dup.conflict = _conflict(total, (row.amount,))
        if not dup.conflict and row.items:
            if content is None:
                return None, True
            if content != row.items:
                dup.conflict = "iné položky"
        return dup, False
    return None, False


def _codex_number(receipts: list, ours: set[str], rows: list[_Row]) -> Duplicate | None:
    for r in receipts:
        if _explaining(r, rows):
            continue        # our own shipment, imported — our row decides
        hit = ours & numbers_of(r.dl_numbers, r.invoice_number, r.invoice_vs)
        if hit:
            return Duplicate(SOURCE_CODEX, "number", r.receipt_number,
                             {"number": sorted(hit)[0]})
    return None


def _codex_date(receipts: list, invoice: str, day: date | None, total: float | None,
                rows: list[_Row]) -> Duplicate | None:
    by_invoice: dict[str, float] = {}
    for r in receipts:
        key = digits(r.invoice_number)
        if key and r.total is not None:
            by_invoice[key] = by_invoice.get(key, 0.0) + r.total
    neighbour = None
    for r in receipts:
        if not _near(day, r.receipt_date, r.receipt_date_to):
            continue
        if _other_invoice(invoice, [r.invoice_number, r.invoice_vs]):
            continue
        explaining = _explaining(r, rows)
        if explaining and all(x.delivered != day for x in explaining):
            continue        # CODEX's copy of our shipment of another day
        same = _close(total, (r.total, r.invoice_total,
                              by_invoice.get(digits(r.invoice_number))))
        if same is None:
            continue
        dup = Duplicate(SOURCE_CODEX, "date_total", r.receipt_number,
                        {"receipt_date": r.receipt_date.isoformat(), "total": same})
        if _near(day, r.receipt_date, r.receipt_date_to, window=0):
            return dup
        dup.conflict = (f"príjemka je z {r.receipt_date:%d.%m.}, dodávka z {day:%d.%m.}"
                        if day else "iný deň")
        neighbour = neighbour or dup
    return neighbour


def _own_date(rows: list[_Row], invoice: str, day: date | None, total: float | None,
              content: list | None) -> tuple[Duplicate | None, bool]:
    """(verdict, deferred) for our rows of the SAME delivery day."""
    ambiguous, deferred = None, False
    for row in rows:
        if day is None or row.delivered != day:
            continue
        both = total is not None and row.amount is not None
        iso = row.delivered.isoformat()
        if both and _close(total, (row.amount,)) is not None:
            dup = Duplicate(SOURCE_DESADV, "date_total", row.doc,
                            {"delivery_date": iso, "total": row.amount})
        elif content is not None and row.items and row.items == content:
            dup = Duplicate(SOURCE_DESADV, "date_content", row.doc, {"delivery_date": iso})
        elif content is None:
            deferred = True     # the content (once the EDI is built) may still match
            continue
        elif both:
            continue            # two different sums and other goods: another delivery
        else:
            ambiguous = ambiguous or Duplicate(
                SOURCE_DESADV, "date", row.doc, {"delivery_date": iso},
                "sumy sa nedajú porovnať a položky sa líšia")
            continue
        if _other_invoice(invoice, row.invoice):
            dup.conflict = f"iné číslo faktúry: {invoice} oproti {row.invoice}"
            ambiguous = ambiguous or dup
            continue
        return dup, False
    return ambiguous, deferred


def find_duplicate(conn, receipts, supplier_ean: str, doc: dict, message_id: str, *,
                   doc_number: str, content: list | None = None,
                   invoice_only: bool = False, received_at=None) -> Duplicate | None:
    """Is this document already received in CODEX (`receipts` — a fresh
    `codex_receipts.Receipts`, None = skip that side) or already shipped by us (`desadv_sent`;
    `invoice_only` = only our invoice-derived rows, the DL path's check)? `doc_number` = the
    number this document claims under (its own row is never a twin); `content` = its EDI
    `signature` once built (None at the early gate: what only the content can decide waits);
    `received_at` = when its mail arrived (a number match with a shipment of an EARLIER mail
    and other content is a conflict). Read-only; logs the verdict."""
    ean = str(supplier_ean or "")
    if not ean:
        return None
    ours = numbers_of(doc.get("docNumber"), doc.get("invoiceNumber"), doc_number)
    invoice = str(doc.get("invoiceNumber") or "")
    day = parse_day(doc.get("deliveryDate"))
    total = doc_total(doc)
    rows = _ledger(conn, ean, message_id, doc_number, invoice_only)
    codex = receipts.for_supplier(conn, ean) if receipts is not None else []
    dup, deferred = _own_number(rows, ours, total, content, received_at, invoice_only)
    if dup is None and not deferred:
        dup = _codex_number(codex, ours, rows)
    if dup is None and not deferred:
        own, deferred = _own_date(rows, invoice, day, total, content)
        found = [d for d in (own, _codex_date(codex, invoice, day, total, rows)) if d]
        plain = [d for d in found if not d.conflict]
        if plain:
            dup = plain[0]
        elif found and not deferred:
            dup = found[0]
    log.info("dedup %s doc %s (supplier %s, numbers %s, date %s, total %s, items %s, "
             "invoice_only=%s): %s", message_id, doc_number, ean, sorted(ours), day, total,
             "?" if content is None else len(content), invoice_only,
             dup.as_dict() if dup else ("deferred to the built EDI" if deferred
                                        else "no duplicate"))
    return dup
