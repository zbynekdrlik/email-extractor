"""DL worker — reporting/event helpers (shadow-gated Odoo post + email_events)."""
from __future__ import annotations

import logging

from . import dl_report, report

log = logging.getLogger("orders.dl_worker")

# --- Odoo + event-log helpers -----------------------------------------------
#
# Deep-review findings on #204's own PR: (1) EVERY email_events write in this module
# must be gated on `not shadow` — shadow's whole guarantee is that nothing observable
# leaves the process, and `email_events` is observable even with `rollup=False` (it
# still appears on the message's own admin timeline); the two `rollup=True` writes are
# actively destructive (the DB trigger overwrites `messages.proc_status/proc_outcome`
# — a shadow tick was reproduced silently corrupting the dashboard state of a message
# n8n had ALREADY finished, mirroring `.claude/rules/n8n-workflow-edits.md` §3's own
# "a skip/duplicate branch must never chain into the success logger" incident, just via
# a different mechanism). (2) R97's "a posting failure never blocks Mark Processed"
# must also cover a BUILDER exception (`dl_report.build_*`), not only the network post
# — `_post` now takes a zero-arg callable so the HTML is built INSIDE the same
# try/except, never evaluated as a bare argument before the guard runs.

def _event(conn, shadow: bool, message_id: str, **kwargs) -> None:
    if shadow:
        return
    report.log_event(conn, message_id, **kwargs)


def _dl_history_link(conn, cfg, message_id: str) -> str:
    """#473: THIS mail's password-free História dodacích listov deep link for a review with
    NO board question — but only when that tab really lists the mail (the SAME
    `is_history_document` guard its detail route applies). Anything else — an invoice-as-DL
    mail (`category='invoices'`), including one `release_for_question` reprocesses without
    its invoice flag — gets "" so `build_review` keeps the questions link rather than a deep
    link that 404s. "" too with no `dashboard_base_url` (no DB read then)."""
    link = report.dl_history_link(cfg, message_id)
    if not link:
        return ""
    from ..board.services.history import is_history_document
    return link if is_history_document(conn, message_id, "dl") else ""


def _post(cfg, shadow: bool, build, post=None) -> None:
    if shadow:
        return
    post = post or dl_report.post   # dl_report.post already never raises
    try:
        html = build()
        post(cfg, html)
    except Exception:
        log.exception("posting a DL Odoo message failed")


def _flag_attachment(conn, cfg, shadow: bool, message: dict, link: str,
                     att: dict, reason: str, status: str, synthetic: bool = False,
                     post=None, history_link: str = "") -> dict:
    """Shared shape for "this attachment needs a human to look at it" — posts a review
    message, logs a non-rollup event, and returns the `documents_out` entry. Used both
    by the pre-existing attachment-extraction-error path and #238's own completeness
    check (an attachment that read fine but contributed zero documents) — the only
    difference is the event `status` and whether the entry is marked `synthetic`
    (never a REAL document, so callers that count "documents" — `_summary_outcome`,
    the rollup detail, `dl_evaluate.score()` — must exclude it, per #238's own review).
    #473: neither case raises a board question, so the caller passes `history_link` (the
    mail's História dodacích listov detail); `link` stays the fallback."""
    _post(cfg, shadow, lambda: dl_report.build_review(
        reason, from_addr=message.get("from_addr", ""),
        subject=message.get("subject", ""), link=link, history_link=history_link),
        post=post)
    _event(conn, shadow, message["message_id"], stage="review", status=status,
          outcome=reason, detail={"idx": att.get("idx")}, rollup=False,
          workflow=dl_report.WORKFLOW)
    out = {"outcome": "review", "reason": reason, "attachment_idx": att.get("idx")}
    if synthetic:
        out["synthetic"] = True
    return out
