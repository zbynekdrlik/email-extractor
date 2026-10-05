"""DL worker — the invoice-as-DL gate wiring (#485): what an invoice mail and each document
derived from it must pass before a board question or a DESADV claim.

The rules live in `invoice_dedup` (pure reads); this module turns a verdict into the DL
engine's document outcome + event, exactly like the other terminal skips (`_skip_not_
warehouse`): no Odoo post for a skip (the warehouse already has those goods — a post would be
noise), one non-rollup `email_events` row (the invoice flow owns the rollup, #406 F1), and the
document dict `_aggregate_status` folds into the invoice run's outcome. A HOLD (the CODEX
receipts are not fresh, or the supplier's EAN is on no receipt, while a document reached the
gate) and a CONFLICT (`invoice_dedup.Duplicate.conflict`: maybe the same delivery, not
provable) post a review: a human decides, nothing is shipped. A mail whose only hint of a
credit note is its own words is posted too (`MAIL_CREDIT_REASON`). LIVE only — `dl_message` /
`dl_document` never call it in shadow; the DL path (`dodacie_listy`) uses `twin_shipped` /
`claim_unless_twin` with `invoice_only=True` (+ `review_conflict`).
"""
from __future__ import annotations

import logging

from psycopg.pq import TransactionStatus

from . import codex_receipts, desadv, desadv_edi, dl_report, invoice_dedup
from .dl_correction import _mail_body_only
from .dl_events import _event, _post

log = logging.getLogger("orders.dl_worker")

STAGE = "invoice_dedup"
CREDIT_REASON = "Dobropis — nie je to dodávka, do ORIONu sa nenahráva."
MAIL_CREDIT_REASON = ("E-mail hovorí o dobropise — doklad sa ako dobropis do ORIONu NEnahráva. "
                      "Ak je to v skutočnosti faktúra za dodávku, prijmi ju v CODEXe ručne.")
SHIP_LOCK = "desadv-ship:"      # + supplier EAN: twin check → claim → facts, one at a time
UNCOVERED_REASON = "dodávateľ nemá v príjemkách z CODEXu ani jednu príjemku pod svojím EAN"
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
    the title) — that attachment is dropped, the invoices of a mixed mail still go on
    (`CREDIT_REASON` when none is left). A mail with a SINGLE source whose own words say so (a
    forwarded `Dobropis č. …` whose PDF does not repeat the word — Forbak) is one too, but only
    the mail's words decide there — a „Re: dobropis" thread or a „dobropis pošleme zvlášť"
    line may sit on a real invoice — so it returns `MAIL_CREDIT_REASON`, which the caller posts
    for a human (never a silent loss). With several sources the mail's words cannot tell which
    one is meant („Faktúra a dobropis"): only the per-attachment rule and the negative total
    (`gate`) decide. Returns (the sources to extract, the credit-note reason when none is
    left)."""
    kept = [a for a in sources if not invoice_dedup.is_credit_note_text(
        a.get("filename") or "",
        (a.get("machine_text") or "")[:invoice_dedup.CREDIT_HEADER_CHARS])]
    if len(kept) < len(sources):
        log.info("invoice-as-DL %s: %d credit-note attachment(s) dropped",
                 message.get("message_id"), len(sources) - len(kept))
    if sources and not kept:
        return [], CREDIT_REASON
    body = _mail_body_only(message.get("combined_text", ""))
    if len(sources) <= 1 and invoice_dedup.is_credit_note_text(message.get("subject", ""),
                                                                 body):
        return [], MAIL_CREDIT_REASON
    return kept, None


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
    if not receipts.covers(conn, supplier_ean):
        # the claim waits for an uncovered flagged supplier; this is the supplier the
        # document RESOLVED to — its CODEX receipts are invisible to the gate: a human decides
        return review_conflict(conn, cfg, message, doc,
                               invoice_dedup.unverifiable(UNCOVERED_REASON), post=post,
                               link=link, history_link=history_link)
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
    log.warning("invoice/DL %s doc %s: maybe already received / shipped, not provable (%s) — "
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
    """With the EDI built: is this document already received / shipped — the full rules, now
    with its [card, quantity] content (a DL scan without prices still has it)? The invoice path
    judges against the CODEX receipts and every row of the supplier (what the early `gate`
    deferred is decided here); the DL path only against our INVOICE-derived rows (an invoice
    shipped first, its DL scan arriving later — LESAFFRE sends both)."""
    receipts = None
    if not invoice_only:
        receipts = codex_receipts.live(conn)
        if receipts is None:
            # the copy went stale while this invoice was being processed: never a blind ship
            return invoice_dedup.unverifiable("príjemky z CODEXu nie sú aktuálne")
        if not receipts.covers(conn, supplier_ean):
            return invoice_dedup.unverifiable(UNCOVERED_REASON)
    return invoice_dedup.find_duplicate(conn, receipts, supplier_ean, doc,
                                        message["message_id"], doc_number=built.doc_number,
                                        content=invoice_dedup.signature(built.content),
                                        invoice_only=invoice_only,
                                        received_at=message.get("created_at"))


def claim_unless_twin(conn, message: dict, doc: dict, supplier_ean: str, built, *,
                      invoice_only: bool, facts: dict
                      ) -> tuple[invoice_dedup.Duplicate | None, bool, str]:
    """The twin check, the DESADV claim and our row's facts as ONE step per supplier: a
    transaction holding an advisory lock on the supplier, so two documents of one delivery
    processed at the same moment (the worker's invoice and a board-answer DL reprocess) never
    both see "no twin" — the second one waits and then sees the first one's row WITH its facts.
    Returns (twin, claimed, holder); `facts` = `desadv.record_facts`' keyword arguments.

    The claim must be COMMITTED before the upload (`claim_send_or_identify`'s two-phase
    contract), so the caller's connection must not already be inside a transaction — the lock
    and the claim would otherwise stay open across the upload. Refused loudly."""
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise RuntimeError("claim_unless_twin needs an autocommit connection outside a "
                           "transaction (the claim must commit before the upload)")
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                     (SHIP_LOCK + str(supplier_ean or ""),))
        twin = twin_shipped(conn, message, doc, supplier_ean, built, invoice_only=invoice_only)
        if twin is not None:
            return twin, False, ""
        claimed, holder = desadv.claim_send_or_identify(
            conn, supplier_ean, built.doc_number, built.filename,
            message_id=message["message_id"])
        if claimed:
            desadv.record_facts(conn, supplier_ean, built.doc_number,
                                message_id=message["message_id"], **facts)
    return None, claimed, holder


def skip_twin(conn, message: dict, doc: dict, dup: invoice_dedup.Duplicate) -> dict:
    """The invoice path's late-check skip (same shape as the early gate's)."""
    return _skip(conn, message, doc, "duplicate", dup.reason() + ".", dup.as_dict())
