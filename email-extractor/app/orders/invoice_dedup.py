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
    `desadv_sent` rows' doc / invoice number (our rows of the last `LEDGER_DAYS` only — small
    suppliers' counters overlap over months); a number match with one of OUR shipments whose
    total / items differ, the document arriving AFTER that shipment's mail, is a CONFLICT (a
    corrected version of what already shipped): never shipped, but a human is told
    (`Duplicate.conflict`). A CODEX receipt the warehouse typed by hand is theirs to correct —
    a number match there is a plain duplicate; OR
  - a DATE match without numbers — a CODEX receipt within ±1 day (`receipt_date`…
    `receipt_date_to`: CODEX books the day it was typed) with the total within
    max(0.50 €, 1 %) of the receipt total, the linked invoice's total or the sum of the
    receipts sharing that invoice; or one of our rows with the SAME delivery date and the same
    total or the SAME content (`signature`: the [card, quantity] pairs of the generated EDI —
    what a priceless DL scan still has). A standing order delivered on two days with the same
    goods is two deliveries (round 2), so: a receipt linked to ANOTHER invoice than ours never
    matches by date, a receipt whose DL number is one of our OTHER shipments is that shipment
    imported (explained — the own-ledger check judges it), and our own rows match by date only
    on the SAME day. A same-day match with one of our rows that carries ANOTHER invoice number
    is ambiguous (a reissued invoice of the same delivery, or a second delivery that day) — a
    conflict for a human, never shipped and never silent.
  Against the CODEX receipts (`codex_receipts`, fresh only) and our own `desadv_sent` for that
  supplier — never the row of THIS very document (same message + same doc number: its own
  earlier attempt is the claim's `already_shipped_this_run` business, and an orphan claim must
  stay reclaimable), but another document of the same mail counts (an invoice PDF and the DL
  PDF of the same goods in one mail are ONE delivery).
- **Newest version wins** is not a rule here: `dl_message._claim_invoice` takes the NEWEST
  waiting invoice first, so an older version of the same invoice then meets the newer's
  `desadv_sent` row by number (never a second DESADV), and a newer mail that ships nothing (a
  reminder, a held version) never blocks the older one. A newer version arriving AFTER the
  older shipped is a conflict (above), never a silent duplicate; an older version met after
  the newer shipped is a plain duplicate (the right content already went).

A false "duplicate" costs a hand-entered receipt (the status quo before #406); a missed one a
second delivery in ORION — so every rule errs to "duplicate". The DL path (`dodacie_listy`)
uses only the own-ledger check, against our invoice-derived rows (`invoice_only` = rows
of `invoices`-category mails).
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
    date_content. `conflict` = it is not provably the SAME document as our shipment (a number
    match with another total / items from a later mail — a corrected version; a same-day match
    with another invoice number — a reissue or a second delivery): a human must look."""
    source: str
    match: str
    ref: str
    detail: dict = field(default_factory=dict)
    conflict: str = ""

    def reason(self) -> str:
        how = {"number": "rovnaké číslo dokladu",
               "date_total": "rovnaký dátum dodania a suma",
               "date_content": "rovnaký dátum dodania a rovnaké položky"}[self.match]
        if self.source == SOURCE_CODEX:
            text = f"Už prijaté v CODEXe (príjemka {self.ref}, {how})"
        else:
            text = f"Už odoslané do ORIONu (dodací list {self.ref}, {how})"
        if self.conflict:
            text += (f" — ale tento doklad sa od odoslaného líši ({self.conflict}): môže to "
                     f"byť OPRAVENÁ verzia alebo iná dodávka. Do ORIONu sa NEposiela; over v "
                     f"CODEXe a v prípade potreby príjemku oprav alebo dodávku prijmi ručne")
        return text

    def as_dict(self) -> dict:
        out = {"source": self.source, "match": self.match, "ref": self.ref, **self.detail}
        if self.conflict:
            out["conflict"] = self.conflict
        return out


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


def _codex_duplicate(receipts: list, ours: set[str], invoice: str, day: date | None,
                     total: float | None, shipped: set[str]) -> Duplicate | None:
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
        if _other_invoice(invoice, [r.invoice_number, r.invoice_vs]):
            continue
        if numbers_of(r.dl_numbers) & shipped:
            continue      # CODEX's import of another of our shipments — already explained
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


def _ledger(conn, supplier_ean: str, message_id: str, doc_number: str,
            invoice_only: bool) -> list:
    """Our shipments of this supplier in the last `LEDGER_DAYS`, except THIS very document's
    own row; `invoice_only` = only those shipped from an invoice mail. Each row ends with the
    received time of the mail it shipped from (None when that mail is gone)."""
    return conn.execute(
        "SELECT d.doc_number, d.invoice_number, d.delivery_date, d.total_amount, d.items, "
        "(SELECT m.created_at FROM messages m WHERE m.message_id = d.message_id) "
        "FROM desadv_sent d WHERE d.supplier_ean = %s "
        "AND d.sent_at > now() - make_interval(days => %s) "
        "AND NOT (d.message_id IS NOT DISTINCT FROM %s AND d.doc_number = %s)"
        + (" AND EXISTS (SELECT 1 FROM messages m WHERE m.message_id = d.message_id "
           "AND m.category = 'invoices')" if invoice_only else "")
        + " ORDER BY d.id",
        (supplier_ean, LEDGER_DAYS, message_id, doc_number)).fetchall()


def _later(received_at, shipped_from) -> bool:
    return received_at is not None and shipped_from is not None and received_at > shipped_from


def _desadv_duplicate(rows: list, ours: set[str], invoice: str, day: date | None,
                      total: float | None, content: list | None,
                      received_at=None) -> Duplicate | None:
    for row_doc, row_invoice, _day, amount, items, shipped_from in rows:
        hit = ours & numbers_of(row_doc, row_invoice)
        if hit:
            conflict = ""
            if _later(received_at, shipped_from):
                conflict = _conflict(total, (amount,))
                row_items = _items(items)
                if not conflict and content and row_items and row_items != content:
                    conflict = "iné položky"
            return Duplicate(SOURCE_DESADV, "number", row_doc, {"number": sorted(hit)[0]},
                             conflict)
    ambiguous = None
    for row_doc, row_invoice, delivered, amount, items, _from in rows:
        if not _near(day, delivered, window=0):
            continue
        if _close(total, (amount,)) is not None:
            dup = Duplicate(SOURCE_DESADV, "date_total", row_doc,
                            {"delivery_date": delivered.isoformat(), "total": float(amount)})
        elif content and _items(items) == content:
            dup = Duplicate(SOURCE_DESADV, "date_content", row_doc,
                            {"delivery_date": delivered.isoformat()})
        else:
            continue
        if not _other_invoice(invoice, row_invoice):
            return dup
        dup.conflict = f"iné číslo faktúry: {invoice} oproti {row_invoice}"
        ambiguous = ambiguous or dup
    return ambiguous


def find_duplicate(conn, receipts, supplier_ean: str, doc: dict, message_id: str, *,
                   doc_number: str, content: list | None = None,
                   invoice_only: bool = False, received_at=None) -> Duplicate | None:
    """Is this document already received in CODEX (`receipts` — a fresh
    `codex_receipts.Receipts`, None = skip that side) or already shipped by us (`desadv_sent`;
    `invoice_only` = only our invoice-derived rows, the DL path's check)? `doc_number` = the
    number this document claims under (its own row is never a twin); `content` = its EDI
    `signature` when already built; `received_at` = when its mail arrived (a number match
    with a shipment of an EARLIER mail and other content is a conflict). Read-only; logs the
    verdict."""
    ean = str(supplier_ean or "")
    if not ean:
        return None
    ours = numbers_of(doc.get("docNumber"), doc.get("invoiceNumber"), doc_number)
    invoice = str(doc.get("invoiceNumber") or "")
    day = parse_day(doc.get("deliveryDate"))
    total = doc_total(doc)
    rows = _ledger(conn, ean, message_id, doc_number, invoice_only)
    dup = None
    if receipts is not None:
        # a receipt that IS one of our other shipments (imported) is explained only when that
        # shipment carries its facts — then the own-ledger check below judges it; a pre-#485
        # row without them leaves the receipt in play
        shipped = numbers_of(*[r[0] for r in rows if r[2] is not None])
        dup = _codex_duplicate(receipts.for_supplier(conn, ean), ours, invoice, day, total,
                               shipped)
    if dup is None:
        dup = _desadv_duplicate(rows, ours, invoice, day, total, content, received_at)
    log.info("dedup %s doc %s (supplier %s, numbers %s, date %s, total %s, items %s, "
             "invoice_only=%s): %s", message_id, doc_number, ean, sorted(ours), day, total,
             len(content) if content else 0, invoice_only,
             dup.as_dict() if dup else "no duplicate")
    return dup
