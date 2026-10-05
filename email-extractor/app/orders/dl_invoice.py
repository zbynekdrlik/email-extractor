"""DL worker — the invoice-as-DL gate wiring (#485): what an invoice mail and each document
derived from it must pass before a board question or a DESADV claim.

The rules live in `invoice_dedup` (pure reads); this module turns a verdict into the DL
engine's document outcome + event, exactly like the other terminal skips (`_skip_not_
warehouse`): no Odoo post for a skip (the warehouse already has those goods — a post would be
noise), one non-rollup `email_events` row (the invoice flow owns the rollup, #406 F1), and the
document dict `_aggregate_status` folds into the invoice run's outcome. Only a HOLD (the CODEX
receipts are not fresh while a document reached the gate) and a CONFLICT (the same number
already arrived with another sum / other items — a corrected invoice) post a review: a human
decides, nothing is shipped. LIVE only — `dl_message` / `dl_document` never call it in shadow;
the DL path (`dodacie_listy`) uses only `twin_shipped(invoice_only=True)` (+ `review_conflict`).
"""
from __future__ import annotations

import logging

from . import codex_receipts, desadv_edi, dl_report, invoice_dedup
from .dl_correction import _mail_body_only
from .dl_events import _event, _post

log = logging.getLogger("orders.dl_worker")

STAGE = "invoice_dedup"
CREDIT_REASON = "Dobropis — nie je to dodávka, do ORIONu sa nenahráva."
STALE_REASON = ("Príjemky z CODEXu nie sú aktuálne (starší zoznam než 30 h alebo ešte "
                "neprišiel) — faktúra sa z bezpečnosti NEnahráva do ORIONu ako dodací list, "
                "aby nevznikla duplicita. Skontroluj v CODEXe, či je dodávka prijatá, a v "
                "prípade potreby ju vybav ručne.")


def _has_text(att: dict) -> bool:
    text = (att.get("machine_text") or "").strip()
    return bool(text) and not att.get("needs_vision") and not text.startswith(
        "[needs AI Vision:")


def invoice_sources(attachments: list[dict]) -> list[dict]:
    """The attachments an invoice mail is read from: its TEXT documents (the invoice PDF) when
    it has any — an image riding along (EKVIA's 43 kB marketing banner, flagged `needs_vision`)
    would cost a vision call and, carrying no delivery note, raise a „no DL in this attachment"
    review on the warehouse channel for EVERY invoice (#238). A mail of scans only keeps them
    all (the scan IS the invoice)."""
    textual = [a for a in attachments if _has_text(a)]
    if textual and len(textual) < len(attachments):
        log.info("invoice-as-DL: reading %d text document(s), skipping %d image-only "
                 "attachment(s) %s", len(textual), len(attachments) - len(textual),
                 [a.get("filename") for a in attachments if a not in textual])
    return textual or attachments


def split_credit_notes(message: dict, sources: list[dict]) -> tuple[list[dict], str | None]:
    """Before extraction (no model call is spent on a credit note): one attachment is a credit
    note when its file name or HEADER says „dobropis" (LESAFFRE prints „Faktúra - dobropis" as
    the title) — that attachment is dropped, the invoices of a mixed mail still go on. A mail
    with a SINGLE source is also one when its subject or OWN text says so (a forwarded `Dobropis
    č. …` whose PDF does not repeat the word — Forbak); with several sources the mail's words
    cannot tell which one is meant („Faktúra a dobropis"), so only the per-attachment rule and
    the negative total (`gate`) decide. Returns (the sources to extract, a credit-note reason
    when none is left)."""
    body = _mail_body_only(message.get("combined_text", ""))
    if len(sources) <= 1 and invoice_dedup.is_credit_note_text(message.get("subject", ""),
                                                                 body):
        return [], CREDIT_REASON
    kept = [a for a in sources if not invoice_dedup.is_credit_note_text(
        a.get("filename") or "",
        (a.get("machine_text") or "")[:invoice_dedup.CREDIT_HEADER_CHARS])]
    if len(kept) < len(sources):
        log.info("invoice-as-DL %s: %d credit-note attachment(s) dropped",
                 message.get("message_id"), len(sources) - len(kept))
    return kept, (CREDIT_REASON if sources and not kept else None)


def skip_message(conn, message: dict, outcome: str, reason: str) -> None:
    """The event of a message-level skip (a credit note caught before extraction)."""
    _event(conn, False, message["message_id"], stage=STAGE, status=outcome, outcome=reason,
           detail={"invoice_mode": True}, rollup=False, workflow=dl_report.WORKFLOW)


def _skip(conn, message: dict, doc: dict, outcome: str, reason: str,
          detail: dict | None = None) -> dict:
    doc_number = doc.get("docNumber") or ""
    log.info("invoice-as-DL %s doc %s not shipped (%s): %s", message["message_id"],
             doc_number, outcome, reason)
    _event(conn, False, message["message_id"], stage=STAGE, status=outcome, outcome=reason,
           detail={"doc_number": doc_number,
                   "invoice_number": doc.get("invoiceNumber") or "", **(detail or {})},
           rollup=False, workflow=dl_report.WORKFLOW)
    out = {"outcome": outcome, "doc_number": doc_number,
           "supplier_name": doc.get("supplierName", ""), "reason": reason}
    if detail:
        out["dedup"] = detail
    return out


def claim_number(message: dict, doc: dict) -> str:
    """The number this document will claim under — `_process_document`'s own rule (#262)."""
    return doc.get("docNumber") or desadv_edi.generate_stable_doc_number(message["message_id"])


def gate(conn, cfg, message: dict, doc: dict, supplier_ean: str, *,
         duplicate_only: bool = False, post=None, link: str = "",
         history_link: str = "") -> dict | None:
    """None = the document may go on; else its terminal document dict. `supplier_ean` keys the
    duplicate check ("" = not known yet: the check runs again after the supplier match with
    `duplicate_only=True`)."""
    if not duplicate_only and invoice_dedup.is_credit_note_doc(doc):
        return _skip(conn, message, doc, invoice_dedup.OUTCOME_CREDIT_NOTE,
                     "Dobropis (záporná suma) — nie je to dodávka, do ORIONu sa nenahráva.")
    receipts = codex_receipts.live(conn)
    if receipts is None:
        # fail-CLOSED: the tick claims no invoice while the receipts are stale, so this is a
        # race with a stopped push — a human decides, never a blind upload
        _post(cfg, False, lambda: dl_report.build_review(
            STALE_REASON, doc.get("supplierName", ""), doc.get("docNumber") or "",
            doc.get("deliveryDate", ""), message.get("from_addr", ""),
            message.get("subject", ""), link=link, history_link=history_link), post=post)
        _event(conn, False, message["message_id"], stage="review", status="review",
               outcome=STALE_REASON, detail={"doc_number": doc.get("docNumber") or "",
                                             "held": True, "invoice_mode": True},
               rollup=False, workflow=dl_report.WORKFLOW)
        return {"outcome": "review", "doc_number": doc.get("docNumber") or "",
                "supplier_name": doc.get("supplierName", ""), "reason": STALE_REASON,
                "held": True}
    if not supplier_ean:
        return None
    dup = invoice_dedup.find_duplicate(conn, receipts, supplier_ean, doc,
                                       message["message_id"],
                                       doc_number=claim_number(message, doc),
                                       received_at=message.get("created_at"))
    if dup is None:
        return None
    if dup.conflict:
        return review_conflict(conn, cfg, message, doc, dup, post=post, link=link,
                               history_link=history_link)
    return _skip(conn, message, doc, "duplicate", dup.reason() + ".", dup.as_dict())


def review_conflict(conn, cfg, message: dict, doc: dict, dup: invoice_dedup.Duplicate, *,
                    post=None, link: str = "", history_link: str = "") -> dict:
    """Not provably the same document as one of our shipments (`Duplicate.conflict`: the same
    number from a later mail with another sum / other items — a CORRECTED version; the same
    day with another invoice number — a reissue or a second delivery). Never shipped (a second
    DESADV of one delivery is the harm #485 exists to prevent), never silent either: the
    warehouse is told to check CODEX by hand. No board question — there is nothing to pick."""
    reason = dup.reason() + "."
    doc_number = doc.get("docNumber") or ""
    log.warning("invoice/DL %s doc %s: number already arrived with different content (%s) — "
                "not shipped, review posted", message["message_id"], doc_number,
                dup.as_dict())
    _post(cfg, False, lambda: dl_report.build_review(
        reason, doc.get("supplierName", ""), doc_number, doc.get("deliveryDate", ""),
        message.get("from_addr", ""), message.get("subject", ""), link=link,
        history_link=history_link), post=post)
    _event(conn, False, message["message_id"], stage=STAGE, status="review", outcome=reason,
           detail={"doc_number": doc_number, "invoice_number": doc.get("invoiceNumber") or "",
                   "dedup": dup.as_dict()},
           rollup=False, workflow=dl_report.WORKFLOW)
    return {"outcome": "review", "doc_number": doc_number,
            "supplier_name": doc.get("supplierName", ""), "reason": reason,
            "dedup": dup.as_dict()}


def twin_shipped(conn, message: dict, doc: dict, supplier_ean: str, built, *,
                 invoice_only: bool) -> invoice_dedup.Duplicate | None:
    """Right before the claim, with the EDI built: does one of OUR shipments already carry
    these goods — by number, date + total, or date + the SAME [card, quantity] content (a DL
    scan without prices)? The invoice path asks against every row of the supplier (and closes
    the window between the early `gate` and the claim, where item matching runs); the DL path
    only against our INVOICE-derived rows (an invoice shipped first, its DL scan arriving
    later — LESAFFRE sends both)."""
    return invoice_dedup.find_duplicate(conn, None, supplier_ean, doc, message["message_id"],
                                        doc_number=built.doc_number,
                                        content=invoice_dedup.signature(built.content),
                                        invoice_only=invoice_only,
                                        received_at=message.get("created_at"))


def skip_twin(conn, message: dict, doc: dict, dup: invoice_dedup.Duplicate) -> dict:
    """The invoice path's late-check skip (same shape as the early gate's)."""
    return _skip(conn, message, doc, "duplicate", dup.reason() + ".", dup.as_dict())
