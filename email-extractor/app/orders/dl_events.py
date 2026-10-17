"""DL worker — reporting/event helpers (shadow-gated Odoo post + email_events).

#495: every ❗ „Dodací list potrebuje kontrolu" goes through ONE choke point, `_post_review`,
which posts it once per delivery note and reason (`dl_review_posts`) — never `_post` +
`dl_report.build_review` directly (`tests/test_dl_review_once.py` fails on a new direct call).
"""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata

from . import dl_report, invoice_dedup, report

log = logging.getLogger("orders.dl_worker")

# #495: how long a posted review is remembered. A reprint or a reprocess of the same DL comes
# within hours or days; the same supplier + number after a month is a counter reuse — a new DL.
REVIEW_MEMORY_DAYS = 30
# #495: the review-post decisions on the message timeline (`email_events.stage`).
REVIEW_POST_STAGE = "review_post"
# #495: the reason identity of a supplier the model could not match — its review text is the
# model's own free-text `matchReason`, different on every run, while the warehouse's task
# (identify the supplier on the board) is the same.
REVIEW_KEY_SUPPLIER_UNMATCHED = "dodavatel-nesparovany"

_DIGITS_RE = re.compile(r"\d+")
_NON_WORD_RE = re.compile(r"[^a-z#]+")

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


def _review_doc_key(cfg, message: dict, supplier_ean: str, doc_number: str) -> str:
    """#495: WHICH delivery note a review is about — the supplier + the DL number reduced to
    its digits without leading zeros (`invoice_dedup.digits`: a reprint read as „0100…" and
    one read as „100…" are the same DL; < 5 digits is no number). The supplier is the matched
    EAN, else the sender — never a scanner/relay address (#407: it forwards every supplier's
    paper, so it identifies nobody). No number or no supplier identity → the mail itself, so
    such a review goes out once per mail."""
    number = invoice_dedup.digits(doc_number)
    who = str(supplier_ean or "").strip()
    if not who:
        # lazy: dl_questions imports dl_message, which imports this module
        from .dl_questions import is_scanner_sender
        sender = str(message.get("from_addr") or "").strip().lower()
        if sender and not is_scanner_sender(cfg, sender):
            who = sender
    if number and who:
        return f"dl:{who}:{number}"
    return f"mail:{message['message_id']}"


def _review_reason_hash(reason: str) -> str:
    """#495: the identity of a review REASON — what the warehouse is asked to do, not how the
    text reads this time: casefolded, diacritics dropped, EVERY number masked (a reprint read
    with other sums, dates or counts is still „the sum does not add up"), punctuation and
    spacing collapsed, hashed to keep the key short. Item names stay — a hold whose open
    lines change is a different reason and is posted again."""
    text = unicodedata.normalize("NFKD", str(reason or "")).encode("ascii", "ignore")
    norm = _NON_WORD_RE.sub(" ", _DIGITS_RE.sub("#", text.decode().lower())).strip()
    return hashlib.sha256(norm.encode()).hexdigest()


def _claim_review(conn, doc_key: str, reason_hash: str, message_id: str,
                  reason: str) -> int | None:
    """Remember this review as sent BEFORE posting it — the row id, or None when the same DL +
    reason already went out within `REVIEW_MEMORY_DAYS` (an older row is re-claimed in
    place: the UNIQUE key never blocks a genuinely new DL). One atomic statement, so two
    concurrent reprocesses of the same DL can never both post."""
    with conn.transaction():
        row = conn.execute(
            """INSERT INTO dl_review_posts (doc_key, reason_hash, message_id, reason)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (doc_key, reason_hash) DO UPDATE
                  SET message_id = EXCLUDED.message_id, reason = EXCLUDED.reason,
                      created_at = now()
                WHERE dl_review_posts.created_at
                      < now() - make_interval(days => %s)
               RETURNING id""",
            (doc_key, reason_hash, message_id, reason, REVIEW_MEMORY_DAYS)).fetchone()
    return int(row[0]) if row else None


def _review_event(conn, message_id: str, status: str, outcome: str, detail: dict) -> None:
    """The review-post decision on the message timeline (non-rollup — never proc_status). A
    failing write only logs: it must never block the processing that called it (R97)."""
    try:
        report.log_event(conn, message_id, stage=REVIEW_POST_STAGE, status=status,
                         outcome=outcome, detail=detail, rollup=False,
                         workflow=dl_report.WORKFLOW)
    except Exception:
        log.exception("logging the DL review-post decision failed for %s", message_id)


def _post_review(conn, cfg, shadow: bool, message: dict, reason: str, *,
                 supplier_ean: str = "", supplier_name: str = "", doc_number: str = "",
                 delivery_date: str = "", link: str = "", history_link: str = "",
                 cmr: bool = False, reason_key: str | None = None, post=None) -> bool:
    """#495: THE one way a ❗ „Dodací list potrebuje kontrolu" (`dl_report.build_review`)
    reaches Odoo. It goes out ONCE per delivery note (`_review_doc_key`) and reason
    (`_review_reason_hash` of `reason_key`, else of the `reason` text — a caller whose text is
    the model's own words passes a stable `reason_key`): every print and every reprocess of
    the same DL with the same reason posted again before (live: the same DL up to 8x). A
    DIFFERENT reason is posted. Shadow posts and remembers nothing. A post Odoo never got
    (an exception or `None`) is not remembered — the next processing tries again. Every
    decision is logged on the timeline (`review_post`: posted / suppressed / post_failed).
    Never raises (R97): a failing memory lookup posts anyway — a repeat beats a lost review.
    Returns True when the review was delivered."""
    if shadow:
        return False
    mid = message["message_id"]
    doc_key = _review_doc_key(cfg, message, supplier_ean, doc_number)
    reason_hash = _review_reason_hash(reason_key or reason)
    detail = {"doc_key": doc_key, "reason_hash": reason_hash[:16]}
    try:
        claim_id = _claim_review(conn, doc_key, reason_hash, mid, reason or "")
        if claim_id is None:
            first = conn.execute(
                "SELECT message_id, created_at FROM dl_review_posts "
                "WHERE doc_key = %s AND reason_hash = %s", (doc_key, reason_hash)).fetchone()
            if first:
                detail["first_message_id"] = first[0]
                detail["first_posted_at"] = first[1].isoformat()
            log.info("DL review for %s (message %s) already posted with the same reason — "
                     "not repeated", doc_key, mid)
            _review_event(conn, mid, "suppressed",
                          "kontrola tohto dodacieho listu s tým istým dôvodom už bola "
                          "odoslaná do Odoo — neopakuje sa", detail)
            return False
    except Exception:
        log.exception("DL review memory failed for %s — posting anyway", mid)
        claim_id = None
    post = post or dl_report.post   # dl_report.post already never raises
    try:
        result = post(cfg, dl_report.build_review(
            reason, supplier_name, doc_number, delivery_date, message.get("from_addr", ""),
            message.get("subject", ""), link=link, cmr=cmr, history_link=history_link))
    except Exception:
        log.exception("posting a DL review to Odoo failed for %s", mid)
        result = None
    if result is None:
        if claim_id is not None:
            try:
                conn.execute("DELETE FROM dl_review_posts WHERE id = %s", (claim_id,))
            except Exception:
                log.exception("releasing the DL review memory failed for %s", mid)
        _review_event(conn, mid, "post_failed",
                      "kontrolu dodacieho listu sa nepodarilo odoslať do Odoo — pri ďalšom "
                      "spracovaní sa pošle znova", detail)
        return False
    _review_event(conn, mid, "posted", "kontrola dodacieho listu odoslaná do Odoo", detail)
    return True


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
    _post_review(conn, cfg, shadow, message, reason, link=link, history_link=history_link,
                 post=post)
    _event(conn, shadow, message["message_id"], stage="review", status=status,
          outcome=reason, detail={"idx": att.get("idx")}, rollup=False,
          workflow=dl_report.WORKFLOW)
    out = {"outcome": "review", "reason": reason, "attachment_idx": att.get("idx")}
    if synthetic:
        out["synthetic"] = True
    return out
