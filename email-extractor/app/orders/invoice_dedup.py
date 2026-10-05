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
    suppliers' counters overlap over months). Between two INVOICES, our shipment from an
    EARLIER mail with another total / other items is a corrected version → conflict; a DL scan
    and the invoice of its DL number (either order — their sums differ by nature: transport,
    prices) are a plain duplicate unless of another day AND other goods. Only the INVOICE number shared while both carry different DL numbers: within
    ONE mail (or on the DL path) a collective invoice's other delivery note — no match, the
    date rules judge it; from another mail it stays a number match (a re-sent or corrected
    version whose DL reference the model read differently). Against a receipt the sum and the
    date decide (the same sum within ±1 day → duplicate — LESAFFRE prints another DL number on
    the invoice than CODEX has; else → conflict). A receipt that IS CODEX's import of one of
    our shipments never decides by its DL number — our own row does — but CODEX's link of it
    to OUR invoice still counts.
  - **Date, CODEX**: a receipt within ±1 day (CODEX books the day it is typed) whose total,
    its invoice's total or the sum of the receipts sharing that invoice is within
    max(0.50 €, 1 %) — the receipt's OWN date (`receipt_date`; a receipt booked over several
    days counts as neighbouring) is a duplicate (the incident shape), a neighbouring day a
    conflict (a standing order's previous receipt is no proof). Never a receipt linked to
    ANOTHER invoice than ours, never CODEX's import of our shipment of ANOTHER day or of
    another invoice; CODEX's import of an old shipment of ours that carries no facts (before
    #485) within ±1 day is a conflict whatever its total (its total came from our catalog
    prices, not the invoice).
  - **Date, our rows** (the SAME delivery day): the same total or the same content
    (`signature`: the [card, quantity, unit] lines of the generated EDI, compared on card +
    quantity — what a priceless DL scan still has) is a duplicate, a conflict when the two
    carry different invoice numbers (a reissue, or a second delivery that day); totals that
    cannot be compared (a priceless scan) with other content (other units) is a conflict; two
    known, different totals with other content is another delivery. A DL scan and an invoice
    of one delivery may carry dates a day apart (LESAFFRE prints the dispatch date): across
    the two sources (our row from a DL mail vs an invoice, or the reverse; also two documents
    of ONE mail) ±1 day with the same total, the same goods, or — sums that cannot be
    compared — the same cards in other units is a conflict; between two invoices of two
    mails (a standing order) it stays the same day. Known residual: a priceless scan missing
    one of the invoice's lines, a day apart, in other units, is not recognised.
  - The early gate (before supplier / item matching, `content=None`) settles only what needs no
    content and defers the rest; `dl_invoice.twin_shipped` re-judges everything once the EDI is
    built (before any board question) and again under the per-supplier ship lock right before
    the claim (`dl_invoice.claim_unless_twin`), so two documents of one delivery never both
    pass.
  Never the row of THIS very document (same message + same doc number: its own earlier attempt
  is the claim's `already_shipped_this_run` business, and an orphan claim must stay
  reclaimable); a match with a stale orphan claim (unconfirmed past `CLAIM_STALE_MINUTES` —
  the bytes may or may not be in ORION) is a conflict; another document of the same mail
  counts (an invoice PDF and the DL PDF of the same goods in one mail are ONE delivery).
  A flagged supplier whose EAN no CODEX receipt carries is not judged at all — its invoices
  wait (`codex_receipts.covered_eans` / `Receipts.covers`, fail-closed like a stale copy).
- **Newest version wins** is not a rule here: `dl_message._claim_invoice` takes the NEWEST
  waiting invoice first, so an older version of the same invoice then meets the newer's
  `desadv_sent` row by its invoice number (a plain duplicate — the right content already went),
  and a newer mail that ships nothing (a reminder, a held version) never blocks the older one.
  A newer version arriving AFTER the older shipped is a conflict (above).

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
    """The content signature of a generated DESADV: its LIN [card code, quantity, unit]
    triples, sorted. Quantities are as `generate()` wrote them — converted to kg where R84
    converts (kg-tracked cards: kg / tonne / per-piece mass), otherwise the printed count. The
    SAME goods (`_same_goods`) compare [card, quantity] only — „2 KAR" vs „2 ks" of one card
    on one day errs to "duplicate"; „1 KAR" vs „100 ks" is not the same goods, but the same
    cards in other units (`_other_units`) mark a scan / invoice pair whose sums cannot be
    compared as a maybe."""
    out = []
    for code, qty, unit in desadv_edi.lin_quantities(content):
        q = _num(qty)
        out.append([code, round(q, 3) if q is not None else qty, unit.lower()])
    return sorted(out, key=lambda p: (p[0], str(p[1]), p[2]))


def _goods(items) -> list:
    return sorted([[p[0], p[1]] for p in items], key=lambda p: (p[0], str(p[1])))


def _same_goods(a, b) -> bool:
    """The same [card, quantity] lines (the unit aside — older rows stored pairs)."""
    return bool(a) and bool(b) and _goods(a) == _goods(b)


def _other_units(a, b) -> bool:
    """The same cards, but some printed in another unit (a scan in ks, its invoice in KAR)."""
    if not a or not b or {p[0] for p in a} != {p[0] for p in b}:
        return False
    ua = {(p[0], p[2]) for p in a if len(p) > 2}
    ub = {(p[0], p[2]) for p in b if len(p) > 2}
    return bool(ua) and bool(ub) and ua != ub


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
        if self.match == "unverifiable":
            return ("Duplicitu voči CODEXu sa nedá overiť (" + self.conflict + ") — faktúra sa "
                    "z bezpečnosti NEnahráva do ORIONu ako dodací list; skontroluj v CODEXe, či "
                    "je dodávka prijatá, a v prípade potreby ju vybav ručne")
        if self.source == SOURCE_CODEX:
            how = {"number": "rovnaké číslo dokladu",
                   "date_total": "dátum príjemky a suma",
                   "import": "import nášho staršieho dodacieho listu"}[self.match]
            text = f"Už prijaté v CODEXe (príjemka {self.ref}, {how})"
        else:
            how = {"number": "rovnaké číslo dokladu",
                   "date_total": "rovnaký dátum dodania a suma",
                   "date_content": "rovnaký dátum dodania a rovnaké položky",
                   "date": "rovnaký dátum dodania",
                   "near_day": "dátum dodania o deň inak"}[self.match]
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
    message_id: str
    doc: str
    invoice: str | None
    delivered: date | None
    amount: float | None
    items: list | None
    shipped_from: datetime | None      # when the mail it shipped from arrived
    unsure: bool                       # never confirmed, past the stale window: maybe in ORION
    from_invoice: bool                 # shipped from an invoice mail (else a DL mail)


UNSURE = "odoslanie do ORIONu nebolo potvrdené"


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
    """Our shipments of this supplier in the last `LEDGER_DAYS` (confirmed, mid-upload, or a
    stale orphan claim — `unsure`) except THIS very document's own row; `invoice_only` = only
    those shipped from an invoice mail."""
    rows = conn.execute(
        "SELECT d.message_id, d.doc_number, d.invoice_number, d.delivery_date, "
        "d.total_amount, d.items, m.created_at, "
        "(d.uploaded_at IS NULL AND d.sent_at <= now() - make_interval(mins => %s)), "
        "COALESCE(m.category = 'invoices', false) "
        "FROM desadv_sent d LEFT JOIN messages m ON m.message_id = d.message_id "
        "WHERE d.supplier_ean = %s "
        "AND d.sent_at > now() - make_interval(days => %s) "
        "AND NOT (d.message_id IS NOT DISTINCT FROM %s AND d.doc_number = %s)"
        + (" AND m.category = 'invoices'" if invoice_only else "")
        + " ORDER BY d.id",
        (desadv.CLAIM_STALE_MINUTES, supplier_ean, LEDGER_DAYS, message_id,
         doc_number)).fetchall()
    return [_Row(str(r[0] or ""), str(r[1]), r[2], r[3],
                 float(r[4]) if r[4] is not None else None, _items(r[5]), r[6], bool(r[7]),
                 bool(r[8]))
            for r in rows]


def _later(received_at, shipped_from) -> bool:
    return received_at is not None and shipped_from is not None and received_at > shipped_from


def _explaining(receipt, rows: list[_Row]) -> list[_Row]:
    """Our shipments this CODEX receipt is the import of (its DL number = our doc number) —
    also an old one without facts (`delivered` None)."""
    nums = numbers_of(receipt.dl_numbers)
    return [r for r in rows if digits(r.doc) in nums]


def _own_number(rows: list[_Row], ours: set[str], dl_number: str, day: date | None,
                total: float | None, content: list | None, received_at, invoice_only: bool,
                message_id: str) -> tuple[Duplicate | None, bool]:
    """(verdict, deferred): deferred = only the EDI's content can tell (early gate). Rows whose
    DL number matches come first (a resent collective invoice's DL2 meets its own twin, not
    DL1 through the shared invoice number)."""
    for row in sorted(rows, key=lambda r: digits(r.doc) not in ours):
        hit = ours & numbers_of(row.doc, row.invoice)
        if not hit:
            continue
        row_dl = digits(row.doc)
        if (dl_number and row_dl and dl_number != row_dl and row_dl not in hit
                and (invoice_only or row.message_id == message_id)):
            continue        # only the invoice number: another DL of a collective invoice
        dup = Duplicate(SOURCE_DESADV, "number", row.doc, {"number": sorted(hit)[0]})
        if invoice_only or not row.from_invoice:
            # a DL scan vs the invoice of its DL number (either order): the same document by
            # number — their sums differ by nature (transport, prices), only another day AND
            # other goods make it a maybe
            if (row.delivered and day and row.delivered != day and content is not None
                    and row.items and not _same_goods(content, row.items)):
                dup.conflict = "iný deň dodania a iné položky"
        elif _later(received_at, row.shipped_from):
            dup.conflict = _conflict(total, (row.amount,))
            if not dup.conflict and row.items:
                if content is None:
                    return None, True
                if not _same_goods(content, row.items):
                    dup.conflict = "iné položky"
        if not dup.conflict and row.unsure:
            dup.conflict = UNSURE
        return dup, False
    return None, False


def _codex_number(receipts: list, ours: set[str], dl_number: str, day: date | None,
                  total: float | None, rows: list[_Row]) -> Duplicate | None:
    by_invoice = _invoice_sums(receipts)
    linked = None
    for r in receipts:
        explained = bool(_explaining(r, rows))
        by_dl = set() if explained else ours & numbers_of(r.dl_numbers)
        if by_dl:
            return Duplicate(SOURCE_CODEX, "number", r.receipt_number,
                             {"number": sorted(by_dl)[0]})
        by_link = ours & numbers_of(r.invoice_number, r.invoice_vs)
        if not by_link:
            continue
        dup = Duplicate(SOURCE_CODEX, "number", r.receipt_number, {"number": sorted(by_link)[0]})
        receipt_dls = numbers_of(r.dl_numbers)
        if dl_number and receipt_dls and dl_number not in receipt_dls and (
                not _near(day, r.receipt_date, r.receipt_date_to)
                or _close(total, (r.total, r.invoice_total,
                                  by_invoice.get(digits(r.invoice_number)))) is None):
            # the same invoice, another DL and another sum or day: a collective invoice's other
            # delivery — or not; a human decides
            dup.conflict = "príjemka patrí k tej istej faktúre, ale k inému dodaciemu listu"
            linked = linked or dup
            continue
        return dup
    return linked


def _invoice_sums(receipts: list) -> dict[str, float]:
    sums: dict[str, float] = {}
    for r in receipts:
        key = digits(r.invoice_number)
        if key and r.total is not None:
            sums[key] = sums.get(key, 0.0) + r.total
    return sums


def _codex_date(receipts: list, invoice: str, day: date | None, total: float | None,
                rows: list[_Row]) -> Duplicate | None:
    by_invoice = _invoice_sums(receipts)
    neighbour = None
    for r in receipts:
        if not _near(day, r.receipt_date, r.receipt_date_to):
            continue
        if _other_invoice(invoice, [r.invoice_number, r.invoice_vs]):
            continue
        explaining = _explaining(r, rows)
        factless = any(x.delivered is None for x in explaining)
        if explaining and not factless and (
                all(x.delivered != day for x in explaining)
                or any(_other_invoice(invoice, x.invoice) for x in explaining)):
            continue        # CODEX's copy of our shipment of another day / another invoice
        same = _close(total, (r.total, r.invoice_total,
                              by_invoice.get(digits(r.invoice_number))))
        if same is None and not factless:
            continue
        dup = Duplicate(SOURCE_CODEX, "import" if factless else "date_total",
                        r.receipt_number, {"receipt_date": r.receipt_date.isoformat(),
                                           "total": same if same is not None else r.total})
        if factless:
            dup.conflict = (f"príjemka z {r.receipt_date:%d.%m.} je bez údajov o faktúre — "
                            f"sumu ani položky nemožno porovnať")
        elif day == r.receipt_date:
            return dup          # the incident shape: typed by hand the day of the delivery
        else:
            dup.conflict = (f"príjemka je z {r.receipt_date:%d.%m.}, dodávka z {day:%d.%m.}"
                            if day else "iný deň")
        neighbour = neighbour or dup
    return neighbour


def _own_date(rows: list[_Row], invoice: str, day: date | None, total: float | None,
              content: list | None, doc_from_invoice: bool,
              message_id: str = "") -> tuple[Duplicate | None, bool]:
    """(verdict, deferred) for our rows of the SAME delivery day — and, across a DL scan and an
    invoice, of the day before / after (the same total, the same goods, or — sums that cannot
    be compared — the same cards in other units: a conflict). Two documents of ONE mail a day
    apart count as the two sources too (an invoice PDF and its DL PDF; the model may fill an
    invoice number on both)."""
    ambiguous, deferred = None, False
    for row in rows:
        if day is None or row.delivered is None:
            continue
        if row.delivered != day:
            cross = row.from_invoice != doc_from_invoice or row.message_id == message_id
            if not cross or not _near(day, row.delivered):
                continue
            # a DL scan and an invoice of one delivery a day apart (dispatch vs delivery date)
            both = total is not None and row.amount is not None
            if (both and _close(total, (row.amount,)) is not None) or (
                    content is not None and _same_goods(content, row.items)) or (
                    not both and content is not None and _other_units(content, row.items)):
                ambiguous = ambiguous or Duplicate(
                    SOURCE_DESADV, "near_day", row.doc,
                    {"delivery_date": row.delivered.isoformat()},
                    "dodací list a faktúra tej istej dodávky môžu niesť dátumy o deň inak")
            elif content is None and not both:
                deferred = True
            continue
        both = total is not None and row.amount is not None
        iso = row.delivered.isoformat()
        if both and _close(total, (row.amount,)) is not None:
            dup = Duplicate(SOURCE_DESADV, "date_total", row.doc,
                            {"delivery_date": iso, "total": row.amount})
        elif content is not None and _same_goods(content, row.items):
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
        elif row.unsure:
            dup.conflict = UNSURE
        if dup.conflict:
            ambiguous = ambiguous or dup
            continue
        return dup, False
    return ambiguous, deferred


def unverifiable(why: str) -> Duplicate:
    """The verdict when the CODEX side cannot be judged at all (a stale copy at the late
    check, a supplier no receipt carries): never shipped, a human decides."""
    return Duplicate(SOURCE_CODEX, "unverifiable", "", conflict=why)


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
    dl_number = digits(doc.get("docNumber"))
    invoice = str(doc.get("invoiceNumber") or "")
    day = parse_day(doc.get("deliveryDate"))
    total = doc_total(doc)
    rows = _ledger(conn, ean, message_id, doc_number, invoice_only)
    codex = receipts.for_supplier(conn, ean) if receipts is not None else []
    dup, deferred = _own_number(rows, ours, dl_number, day, total, content, received_at,
                                invoice_only, message_id)
    conflicts: list[Duplicate] = []
    if dup is None and not deferred:
        linked = _codex_number(codex, ours, dl_number, day, total, rows)
        if linked is not None and not linked.conflict:
            dup = linked
        elif linked is not None:
            conflicts.append(linked)
    if dup is None and not deferred:
        own, deferred = _own_date(rows, invoice, day, total, content, not invoice_only,
                                  message_id)
        found = [d for d in (own, _codex_date(codex, invoice, day, total, rows)) if d]
        plain = [d for d in found if not d.conflict]
        conflicts += [d for d in found if d.conflict]
        if plain:
            dup = plain[0]
        elif conflicts and not deferred:
            dup = conflicts[0]
    log.info("dedup %s doc %s (supplier %s, numbers %s, date %s, total %s, items %s, "
             "invoice_only=%s): %s", message_id, doc_number, ean, sorted(ours), day, total,
             "?" if content is None else len(content), invoice_only,
             dup.as_dict() if dup else ("deferred to the built EDI" if deferred
                                        else "no duplicate"))
    return dup
