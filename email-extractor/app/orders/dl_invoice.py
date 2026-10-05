"""DL worker — the invoice-as-DL gate wiring (#485): what an invoice-derived document must pass
before it may raise a board question or claim a DESADV.

The rules live in `invoice_dedup` (pure reads); this module turns a verdict into the DL
engine's document outcome + event, exactly like the other terminal skips (`_skip_not_
warehouse`): no Odoo post for a skip (the warehouse already has those goods — a post would be
noise), one non-rollup `email_events` row (the invoice flow owns the rollup, #406 F1), and the
document dict `_aggregate_status` folds into the invoice run's outcome. Only a HOLD (the CODEX
receipts are stale while a reprocess reached the gate) posts a review — that one needs a human.
LIVE only — `_process_document` never calls it in shadow, the DL path never calls it at all.
"""
from __future__ import annotations

import logging

from . import codex_receipts, dl_report, invoice_dedup
from .dl_events import _event, _post

log = logging.getLogger("orders.dl_worker")

STAGE = "invoice_dedup"
STALE_REASON = ("Príjemky z CODEXu nie sú aktuálne (starší zoznam než 30 h alebo ešte "
                "neprišiel) — faktúra sa z bezpečnosti NEnahráva do ORIONu ako dodací list, "
                "aby nevznikla duplicita. Skontroluj v CODEXe, či je dodávka prijatá, a v "
                "prípade potreby ju vybav ručne.")


def credit_note_reason(message: dict, attachments: list[dict]) -> str | None:
    """Before extraction: is this invoice mail a credit note (dobropis)? Checks the subject,
    the mail text and every attachment's file name + text — no model call is spent on it."""
    texts = [message.get("subject", ""), message.get("combined_text", "")]
    for att in attachments or []:
        texts += [att.get("filename") or "", att.get("machine_text") or ""]
    if invoice_dedup.is_credit_note_text(*texts):
        return "Dobropis — nie je to dodávka, do ORIONu sa nenahráva."
    return None


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


def gate(conn, cfg, message: dict, doc: dict, supplier_ean: str, *,
         duplicate_only: bool = False, post=None, link: str = "",
         history_link: str = "") -> dict | None:
    """None = the document may go on; else its terminal document dict. `supplier_ean` keys the
    duplicate check ("" = not known yet: the check runs again after the supplier match with
    `duplicate_only=True`)."""
    if not duplicate_only:
        if invoice_dedup.is_credit_note_doc(doc):
            return _skip(conn, message, doc, invoice_dedup.OUTCOME_CREDIT_NOTE,
                         "Dobropis (záporná suma) — nie je to dodávka, do ORIONu sa "
                         "nenahráva.")
        numbers = invoice_dedup.numbers_of(doc.get("invoiceNumber"), doc.get("docNumber"))
        newer = invoice_dedup.superseded_by(conn, message, numbers)
        if newer:
            return _skip(conn, message, doc, invoice_dedup.OUTCOME_SUPERSEDED,
                         "Prišla novšia verzia tej istej faktúry — spracuje sa tá "
                         "najnovšia.", {"newer_message_id": newer})
    receipts = codex_receipts.live(conn)
    if receipts is None:
        # fail-CLOSED: the tick claims no invoice while the receipts are stale, so this is a
        # reprocess (a board answer) or a race — a human decides, never a blind upload
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
                                       message["message_id"])
    if dup is None:
        return None
    return _skip(conn, message, doc, "duplicate", dup.reason() + ".",
                 dup.as_dict())
