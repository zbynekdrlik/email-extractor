"""DL worker — message lifecycle: claim/select, attachments, aggregation."""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from .. import store
from . import dl_extract, dl_report, dl_snapshot, report, worker
from .dl_correction import _correction_review_reason, _looks_like_correction, _mail_body_only
from .dl_document import _process_document
from .dl_events import _event, _flag_attachment, _post
from .dl_retry import _check_retry, _RetryLater

log = logging.getLogger("orders.dl_worker")

CATEGORY = "dodacie_listy"
CLAIM_STALE_MINUTES = 30          # R10
MAX_ATTEMPTS = 5                  # R11 (quarantine)
SHADOW_DAYS = 3

# This worker's own scope decision (see module docstring) — a real DL is always a PDF or
# an image; anything else is skipped rather than fed to Vision.
_ATTACHMENT_MIME_RE = re.compile(r"pdf|image", re.IGNORECASE)
_ATTACHMENT_EXT_RE = re.compile(r"\.(pdf|jpe?g|png|tiff?|bmp)$", re.IGNORECASE)

# #297 (ROZHODNUTÉ 2026-08-13): a spreadsheet delivery note (.xls/.xlsx/.xlsm) — the
# extractor already produces `machine_text` for these (app/extract.py), this worker
# previously never looked at them at all (Bardusch, application/vnd.ms-excel, sends
# nothing else). Checked only when the PDF/image filter above did NOT already match
# (see `_read_attachments`), so a file is never both. A spreadsheet is never a "scan"
# and its raw bytes are NEVER handed to `dl_extract` as `pdf_bytes` (forced empty in
# `_read_attachments`, below) — is_scanned()/Vision routing must never see them.
_ATTACHMENT_SPREADSHEET_EXT_RE = re.compile(r"\.(xlsx|xlsm|xls)$", re.IGNORECASE)
_ATTACHMENT_SPREADSHEET_MIME_RE = re.compile(
    r"spreadsheetml|ms-excel|x-msexcel|application/excel", re.IGNORECASE)

# #258: the synthetic "attachment" idx used for the mail's own body text when there is
# no usable real attachment — a real `attachments.idx` is always a non-negative 0-based
# index assigned at ingest (`app/db.py`'s `enumerate()` insert), so -1 can never collide
# with one.
_BODY_TEXT_IDX = -1

# Spec §4: the documented Lunys "IS KARAT" DL-number shape inside a subject line.
_SUBJECT_DOC_RE = re.compile(r"\d{2,}LT\d{4,}", re.IGNORECASE)


# --- catalog refresh (mirrors worker.refresh_due) ---------------------------

def refresh_due(conn, cfg) -> int | None:
    """#129: the DL catalog/supplier sheet (R20/R21) is never read anymore either —
    the snapshot frozen 2026-08-07 (491 catalog rows, 959 suppliers) is now permanent
    and this just reports it. No fetch, no interval, no config gate left to check —
    mirrors `worker.refresh_due`'s own #129 change exactly. `cfg` stays in the
    signature only so `run_forever`'s call site needs no change."""
    return dl_snapshot.latest_snapshot_id(conn)


# --- message selection (R10/R11) --------------------------------------------

def _as_message(row, attempts: int = 0) -> dict | None:
    if not row:
        return None
    return {"message_id": row[0], "subject": row[1] or "", "from_addr": row[2] or "",
            "from_name": row[3] or "", "combined_text": row[4] or row[5] or "",
            "has_attachments": bool(row[6]), "attempts": attempts,
            "today": datetime.now(UTC).date().isoformat()}


def _claim(conn) -> dict | None:
    row = conn.execute(
        f"""UPDATE messages SET processing_at = now(), attempts = COALESCE(attempts, 0) + 1
             WHERE id = (SELECT id FROM messages
                          WHERE category = %s AND processed = false
                            AND COALESCE(attempts, 0) < %s
                            AND (processing_at IS NULL
                                 OR processing_at < now()
                                    - interval '{CLAIM_STALE_MINUTES} minutes')
                          ORDER BY created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED)
         RETURNING message_id, subject, from_addr, from_name, combined_text, body_text,
                   has_attachments, attempts""",
        (CATEGORY, MAX_ATTEMPTS)).fetchone()
    if not row:
        return None
    return _as_message(row[:7], attempts=int(row[7] or 0))


def _peek_for_shadow(conn, days: int = SHADOW_DAYS) -> dict | None:
    row = conn.execute(
        """SELECT m.message_id, m.subject, m.from_addr, m.from_name,
                  m.combined_text, m.body_text, m.has_attachments
             FROM messages m
            WHERE m.category = %s
              AND m.created_at > now() - make_interval(days => %s)
              AND NOT EXISTS (SELECT 1 FROM order_runs r
                               WHERE r.message_id = m.message_id AND r.shadow)
            ORDER BY m.created_at DESC LIMIT 1""",
        (CATEGORY, max(1, int(days or SHADOW_DAYS)))).fetchone()
    return _as_message(row)


# --- attachments (W1a: every attachment, not just the first PDF) ------------

def _read_attachments(cfg, message_id: str, conn) -> list[dict]:
    rows = conn.execute(
        """SELECT idx, filename, mime, extracted_text, method FROM attachments
            WHERE message_id = %s ORDER BY idx""", (message_id,)).fetchall()
    data_dir = getattr(cfg, "data_dir", "") or "/data/store"
    out = []
    for idx, filename, mime, extracted_text, method in rows:
        is_pdf_or_image = bool(_ATTACHMENT_MIME_RE.search(mime or "")
                               or _ATTACHMENT_EXT_RE.search(filename or ""))
        is_spreadsheet = bool(not is_pdf_or_image and (
            _ATTACHMENT_SPREADSHEET_MIME_RE.search(mime or "")
            or _ATTACHMENT_SPREADSHEET_EXT_RE.search(filename or "")))
        if not (is_pdf_or_image or is_spreadsheet):
            continue
        if is_spreadsheet:
            # #297: never read a spreadsheet's real on-disk bytes into `pdf_bytes` —
            # dl_extract's scan-detection/vision routing must never see them, even if
            # they happen to contain a byte sequence that looks like an embedded JPEG
            # (a real risk inside an OLE2/xlsx container, not just theoretical).
            pdf_bytes = b""
        else:
            matches = sorted(store.message_dir(data_dir, message_id).glob(f"att{idx}__*"))
            pdf_bytes = matches[0].read_bytes() if matches else b""
        out.append({"idx": idx, "filename": filename or "", "pdf_bytes": pdf_bytes,
                   "machine_text": extracted_text or "", "method": method or "",
                   "is_spreadsheet": is_spreadsheet})
    return out


# --- announced-vs-attached (spec §4) ----------------------------------------

def _subject_doc_numbers(subject: str) -> list[str]:
    return [dl_extract.strip_lt_prefix(m) for m in _SUBJECT_DOC_RE.findall(subject or "")]


# --- one message (R1-R17) ---------------------------------------------------

def _aggregate_status(documents_out: list[dict]) -> str:
    if not documents_out:
        return "review"
    outcomes = [d.get("outcome") for d in documents_out]
    if all(o == "duplicate" for o in outcomes):
        # Deep-review finding, #204: a message whose documents are ALL duplicates
        # (typically THIS message being reclaimed/retried after already shipping —
        # see the retry/idempotency note above) is not a clean "ok" — nothing was
        # actually sent this run.
        return "duplicate"
    # #314: a message whose documents are all terminally non-warehouse (a remembered
    # non-warehouse supplier, short-circuited before any question/upload) is a CLEAN
    # terminal skip — never "review" (nothing needs a human) and never "ok" (nothing
    # shipped). It flows into _run_and_finish's rollup event as stage/status=
    # 'not_warehouse', the SAME shape #307's manual close produces, so the daily digest
    # counts it exactly like a hand-marked "netýka sa skladu". A mixed message (a real
    # shipment alongside a skip — only possible for a multi-supplier mail, not the single-
    # supplier norm) falls through to the ok/partial branches so the shipment still counts.
    if (all(o in ("not_warehouse", "duplicate") for o in outcomes)
            and "not_warehouse" in outcomes):
        return "not_warehouse"
    # #314 adversarial-review finding: a mixed message (an auto-skipped not_warehouse doc
    # ALONGSIDE a doc that genuinely needs a human) must NOT roll up to "ok" — "review"
    # wins so the digest and every proc_status='review'-keyed sweep still see the human
    # work. `not_warehouse` is added to the review branch's tuple (the all-nw branch above
    # still fires first for a purely non-warehouse message).
    if (all(o in ("review", "duplicate", "not_warehouse") for o in outcomes)
            and "review" in outcomes):
        return "review"
    if any(o == "partial" for o in outcomes) or ("review" in outcomes and
                                                  any(o in ("ok", "partial")
                                                      for o in outcomes)):
        return "partial"
    return "ok"


def _summary_outcome(result: dict) -> str:
    # #238 review: `synthetic` entries (a missing/unattached document that was NEVER
    # actually processed) are not real documents this run touched — counting them here
    # would make a mail that carried ONE real document read as "2 dokument(y)".
    docs = [d for d in (result.get("documents") or []) if not d.get("synthetic")]
    if not docs:
        return "žiadny dodací list sa nepodarilo rozpoznať"
    counts: dict[str, int] = {}
    for d in docs:
        counts[d.get("outcome", "review")] = counts.get(d.get("outcome", "review"), 0) + 1
    bits = ", ".join(f"{v}x {k}" for k, v in counts.items())
    return f"{len(docs)} dokument(y): {bits}"


def _process_message(conn, cfg, client, message: dict, snapshot_id: int | None,
                     catalog: list[dict], suppliers: list[dict], shadow: bool,
                     upload=None, post=None, attachments: list[dict] | None = None,
                     list_dirs=None) -> dict:
    # `attachments` injection (#205, DL migration F6 eval harness): mirrors the existing
    # `upload=`/`post=` DI seam. `None` (every real call site, incl. `tick()` below) keeps
    # reading `messages`/`attachments`/disk exactly as before; the eval harness passes a
    # fixture list directly so a corpus case never needs a real Postgres row or a file on
    # the add-on's data volume.
    if attachments is None:
        attachments = ([] if not message.get("has_attachments")
                       else _read_attachments(cfg, message["message_id"], conn))
    # #229 follow-up 2: computed once, reused by every build_review/build_announced_
    # mismatch call in this function (mirrors the same pattern in _process_document).
    # #231: the DL-only nástenka link, never the mixed AI-orders `sklad_link`.
    link = report.dl_sklad_link(cfg)

    # #339: age cutoff. An OLD stuck delivery note that becomes claimable again (a fresh
    # _claim, a _release_stuck_siblings reset, or a release_for_question reprocess — ALL
    # three re-entry paths converge HERE on _process_message, so this ONE guard covers
    # every one) must NEVER auto-upload a months-old document to ORION: the goods almost
    # certainly already arrived and were handled by hand, so an automatic upload is a real
    # duplicate-delivery risk (#338: 3 DLs from 7.7/17.7 auto-uploaded 15.8). The DL engine
    # had NO age guard at all, unlike the orders engine's human_processing.BACKLOG_CUTOFF.
    # Route straight to manual review with an honest reason, BEFORE extraction (no model
    # call, no supplier/item match, no claim, no upload — exactly the #265 correction-mail
    # gate's shape), marked processed so it never loops. Gated `not shadow` for the same
    # reason the #265 gate is: a shadow run uploads nothing anyway and must keep measuring
    # the full pipeline (the corpus/eval harness always runs shadow, so it never sees this
    # branch). `created_at` is read here from `messages` rather than carried on the message
    # dict so the ONE guard covers every re-entry path uniformly (release_for_question
    # builds its own message dict from a SELECT that never carried created_at either).
    # delivery_notes_max_age_days <= 0 disables the guard.
    max_age_days = int(getattr(cfg, "delivery_notes_max_age_days", 14) or 0)
    if max_age_days > 0 and not shadow:
        agerow = conn.execute(
            "SELECT created_at, created_at < now() - make_interval(days => %s) "
            "FROM messages WHERE message_id = %s",
            (max_age_days, message["message_id"])).fetchone()
        if agerow and agerow[1]:
            received = agerow[0]
            reason = (
                f"Tento dodací list je starší ako {max_age_days} dní (prijatý "
                f"{received:%d.%m.%Y}) — z bezpečnosti sa NEnahráva automaticky do ORIONu, "
                f"aby sa nezopakovala už raz vybavená dodávka. Skontroluj ho a v prípade "
                f"potreby ho nahraj / vybav ručne.")
            _post(cfg, shadow, lambda: dl_report.build_review(
                reason, from_addr=message.get("from_addr", ""),
                subject=message.get("subject", ""), link=link), post=post)
            _event(conn, shadow, message["message_id"], stage="review", status="review",
                  outcome=reason, rollup=True, workflow=dl_report.WORKFLOW)
            return {"kind": "dl", "dl_snapshot_id": snapshot_id, "status": "review",
                   "documents": [{"outcome": "review", "reason": reason,
                                  "over_age_cutoff": True}], "items": []}

    # #247: a decorative/tiny/junk attachment (`app/extract.py`'s own ingest-time
    # `method='skipped'` classification, e.g. `flag='skipped_tiny_image'` for a
    # signature logo) must never be handed to `dl_extract` at all — it has no way to
    # tell "a real tiny scan" from "a decorative image" and, for one with no
    # `machine_text`, falls into the digital-PDF-no-text vision fallback and sends the
    # raw image bytes to OpenAI labelled as a PDF file, which is rejected with a 400
    # (the HK LOAN incident this fixes). Reuses the EXISTING classification already
    # computed at ingest and threaded through `_read_attachments()` — never a second,
    # parallel decorative-image decision. An eval-harness fixture (`dl_evaluate.
    # _decode_attachment`) never sets `method` at all, so it is always treated as
    # usable — unaffected by this filter.
    usable_attachments = [a for a in attachments if (a.get("method") or "") != "skipped"]

    documents_out: list[dict] = []

    # #297: a spreadsheet attachment (.xls/.xlsx) whose `machine_text` came back
    # genuinely empty (native extraction failed outright, or the sheet itself has no
    # text) must NEVER reach `dl_extract` — a spreadsheet is never a scan and its
    # `pdf_bytes` are ALWAYS forced empty (see `_read_attachments`), so the "digital
    # source, no text -> Vision fallback" branch would call Vision with nothing to look
    # at, either erroring outright or wasting a call for a guaranteed-empty answer.
    # Flag it directly with an honest, spreadsheet-specific reason instead — mirrors
    # the existing decorative-attachment (#247) and "processed but nothing found"
    # (#238) flagging shape. Excluded from `sources` below, so it can never ALSO be
    # flagged a second time by the #238 completeness loop further down (that loop only
    # sees what was actually sent to `dl_extract`).
    empty_spreadsheets = [a for a in usable_attachments
                          if a.get("is_spreadsheet")
                          and not (a.get("machine_text") or "").strip()]
    for att in empty_spreadsheets:
        reason = (f"Príloha {att.get('filename') or att.get('idx')} (Excel/xls) sa "
                  f"nepodarilo prečítať alebo neobsahuje žiadny text — over ručne, "
                  f"či neobsahuje dodací list")
        documents_out.append(_flag_attachment(
            conn, cfg, shadow, message, link, att, reason, status="review",
            synthetic=True, post=post))
    # Review finding: exclude by `idx` (a real `attachments.idx` is always a unique,
    # non-negative 0-based index per message, see the `_BODY_TEXT_IDX` comment above)
    # rather than by dict-value equality — safety here should never depend on two
    # attachment dicts happening to differ in content.
    empty_spreadsheet_idxs = {a.get("idx") for a in empty_spreadsheets}
    extractable_attachments = [a for a in usable_attachments
                               if a.get("idx") not in empty_spreadsheet_idxs]

    # #258: some suppliers (HK LOAN, gnip@hkloan.eu — verified live, STEP 0 evidence on
    # the ticket) never attach a real document at all; the delivery note is written
    # directly in the mail's own BODY TEXT. When there is no usable attachment, try the
    # message's own body text (subject+From+body ONLY, via `_mail_body_only` — see its
    # own docstring for why NOT raw `combined_text`) as a document SOURCE through the
    # exact same extraction call an attachment goes through — `dl_extract.
    # extract_attachment`'s own W13/R42 routing already skips vision whenever
    # `machine_text` is non-empty and `pdf_bytes` is empty (`is_scanned()` is False with
    # no embedded JPEG bytes to find), so this adds no vision call, only one extra
    # multi-document extraction call over plain text. Everything downstream (item
    # matching, EDI build, ORION upload, the desadv_sent ledger, board questions) is the
    # SAME pipeline every attachment-sourced document already goes through — a document
    # cannot tell whether it came from an attachment or the mail body.
    sources = extractable_attachments
    used_body_text = False
    # Deep-review finding on this ticket's own PR (#265): initialised here, not just
    # inside the `if not sources:` branch below — `body_text` is read again further
    # down (the #265 correction-detection gate), and correctness there depends
    # entirely on `used_body_text and ...`'s short-circuit never evaluating `body_text`
    # while it's unbound. Defining it unconditionally means a future reorder of that
    # gate can never turn this into a `NameError` on the normal attachment path.
    body_text = ""
    if not sources:
        body_text = _mail_body_only(message.get("combined_text", "")).strip()
        if body_text:
            log.info("DL message %s: no usable attachment — trying %d char(s) of mail "
                     "body text as the document source (#258)",
                     message.get("message_id"), len(body_text))
            sources = [{"idx": _BODY_TEXT_IDX, "filename": "text e-mailu",
                       "pdf_bytes": b"", "machine_text": body_text}]
            used_body_text = True

    if not sources:
        if documents_out:
            # #297: every attachment was either decorative or an unreadable
            # spreadsheet, already individually flagged above — nothing further to
            # add, and the generic "no attachment" wording below would be dishonest
            # here (there WAS an attachment, it just had nothing to read).
            return {"kind": "dl", "dl_snapshot_id": snapshot_id,
                   "status": _aggregate_status(documents_out),
                   "documents": documents_out, "items": []}
        if attachments:
            # Attachment(s) existed but were ALL decorative/junk, and the mail's own
            # body text is empty too — distinct, more actionable wording than "no
            # attachment at all" (still posted to Odoo review + the /sklad-dl board,
            # never silent — R15's own visibility path).
            reason = ("Príloha/y sú len drobný/nepoužiteľný obrázok (napr. podpis "
                      "alebo logo) a text e-mailu je prázdny — žiadny skutočný "
                      "dodací list sa nenašiel; treba ho spracovať ručne")
        else:
            # R15: no attachment (or nothing PDF/image-shaped) is NOT an error.
            reason = "Email bez prílohy a bez textu — pravdepodobne bežná správa"
        _post(cfg, shadow, lambda: dl_report.build_review(
            reason, from_addr=message.get("from_addr", ""),
            subject=message.get("subject", ""), link=link), post=post)
        _event(conn, shadow, message["message_id"], stage="review", status="review",
              outcome=reason, rollup=True, workflow=dl_report.WORKFLOW)
        return {"kind": "dl", "dl_snapshot_id": snapshot_id, "status": "review",
               "documents": [{"outcome": "review", "reason": reason}], "items": []}

    # #265: a mail-body-sourced (#258) document whose OWN mail reads as a correction/
    # amendment NEVER auto-ships — see the module docstring's own #265 paragraph.
    # Checked BEFORE extraction (never after): the model is not even called, so there
    # is no world in which this could accidentally match/claim/upload anything.
    #
    # Deep-review finding on this ticket's own PR (#265): gated `not shadow`, matching
    # this project's own documented rule (`.claude/rules/orders-corpus.md`: "Any
    # FUTURE short-circuit that skips calling the model needs the same `not shadow`
    # gate", precedent `pipeline._mail_rule`). The module's own docstring promises
    # shadow "runs the FULL pipeline (extraction, matching, EDI build) for comparison
    # only" — a correction mail skipping extraction even in shadow would silently
    # narrow what shadow actually measures. `_post`/`_event` are already gated on
    # `not shadow` independently (no observable effect either way); this is ONLY about
    # whether extraction itself runs, never about claiming/uploading/teaching (those
    # stay impossible in shadow regardless, via `_process_document`'s own `if shadow:`
    # branch).
    if (used_body_text and not shadow
            and _looks_like_correction(message.get("subject", ""), body_text)):
        reason = _correction_review_reason(body_text)
        _post(cfg, shadow, lambda: dl_report.build_review(
            reason, from_addr=message.get("from_addr", ""),
            subject=message.get("subject", ""), link=link), post=post)
        _event(conn, shadow, message["message_id"], stage="review", status="review",
              outcome=reason, rollup=True, workflow=dl_report.WORKFLOW)
        # #297 review finding: merge with `documents_out` (never overwrite it) — it
        # may already hold empty-spreadsheet review flags from earlier in this
        # function (reachable when a message has an unreadable/empty .xls attachment
        # AND falls back to mail-body text that itself reads as a correction). Losing
        # those entries here would undercount `order_runs.result["documents"]` even
        # though their own Odoo post/event already fired via `_flag_attachment`.
        this_doc = {"outcome": "review", "reason": reason, "correction_detected": True}
        return {"kind": "dl", "dl_snapshot_id": snapshot_id,
               "status": _aggregate_status(documents_out + [this_doc]),
               "documents": documents_out + [this_doc], "items": []}

    extraction = dl_extract.extract_email(client, sources)

    # #297: `documents_out` may already carry entries from the empty-spreadsheet
    # flagging above — accumulate into the SAME list, never reset it here.
    all_items: list[dict] = []
    extracted_doc_numbers: list[str] = []

    for att in extraction["attachments"]:
        if att.get("error"):
            _check_retry(message.get("attempts", 0), att["error"])
            # #312: the raw extraction error (str(e) from dl_extract) must NOT reach the
            # warehouse channel (243) — a clean sentence goes there, the technical detail
            # only to the log.
            log.warning("DL extraction failed for message %s idx %s: %s",
                        message["message_id"], att.get("idx"), att["error"])
            # #258 deep-review finding: the body-text pseudo-source is NOT a "príloha"
            # (attachment) — calling it one in a message a human reads is exactly the
            # category confusion this ticket exists to eliminate.
            if att.get("idx") == _BODY_TEXT_IDX:
                reason = "Text e-mailu sa nepodarilo spracovať — over ho ručne."
            else:
                reason = (f"Prílohu {att.get('filename') or att.get('idx')} sa nepodarilo "
                          f"spracovať — over ju ručne.")
            documents_out.append(_flag_attachment(
                conn, cfg, shadow, message, link, att, reason, status="error", post=post))

    for doc in extraction["documents"]:
        extracted_doc_numbers.append(doc.get("docNumber") or "")
        documents_out.append(_process_document(conn, cfg, client, message, doc, catalog,
                                                suppliers, shadow, all_items,
                                                upload=upload, post=post,
                                                list_dirs=list_dirs))

    if not documents_out:
        # #258: the text of the failure must say where it actually looked — a text-
        # sourced attempt that found nothing did not fail to read "prílohy" (there
        # were none to read).
        reason = ("Nepodarilo sa rozpoznať žiadny dodací list v texte e-mailu"
                  if used_body_text else
                  "Nepodarilo sa rozpoznať žiadny dodací list v prílohách")
        _post(cfg, shadow, lambda: dl_report.build_review(
            reason, from_addr=message.get("from_addr", ""),
            subject=message.get("subject", ""), link=link), post=post)
        _event(conn, shadow, message["message_id"], stage="review", status="review",
              outcome=reason, rollup=True, workflow=dl_report.WORKFLOW)
        documents_out.append({"outcome": "review", "reason": reason})
    else:
        # #238: a UNIVERSAL, supplier-format-independent completeness check —
        # replaces relying on the Lunys-only subject check alone. A successfully-read
        # attachment (`error is None`) that contributed ZERO documents to
        # `extraction["documents"]` is the CURRENT engine's own analogue of the old
        # n8n W1a loss: an LLM/vision extraction call can omit a genuine document with
        # no exception at all, so nothing upstream ever learns it was missed. Marked
        # `synthetic` — this is a MISSING document, never a processed one (review
        # finding: `dl_evaluate.score()`/`_summary_outcome`/the rollup detail count
        # must not treat it as an extra REAL document).
        #
        # A decorative/junk attachment (`extract.py`'s own `method='skipped'` —
        # a logo, a signature image, a banner) is excluded: it was never expected to
        # carry a delivery note in the first place (review finding — without this, a
        # signature image attached alongside a real DL would falsely demote a clean
        # run to "partial" and spam the warehouse channel with a false alert, the
        # exact false-alarm class already documented in #133/#151).
        skip_idxs = {a.get("idx") for a in attachments if (a.get("method") or "") == "skipped"}
        found_idxs = {d.get("source_attachment_idx") for d in extraction["documents"]}
        for att in extraction["attachments"]:
            idx = att.get("idx")
            if att.get("error") or idx in found_idxs or idx in skip_idxs:
                continue
            reason = (f"Príloha {att.get('filename') or idx} bola spracovaná, ale "
                      f"nenašiel sa v nej žiadny dodací list — over ručne, či naozaj "
                      f"neobsahuje ďalší doklad")
            documents_out.append(_flag_attachment(
                conn, cfg, shadow, message, link, att, reason, status="review",
                synthetic=True, post=post))

    # spec §4: announced-vs-attached (Lunys subject shape only — a real, still-useful
    # signal for that supplier, kept as-is). Shadow guarantees nothing observable
    # leaves the process, so neither the event log nor the Odoo post fire while
    # shadowing (deep-review finding, #204 — the mismatch count must not be inflated
    # by shadow runs). `dict.fromkeys` dedupes while keeping order — a subject naming
    # the same DL number twice must not produce two identical synthetic entries.
    announced = _subject_doc_numbers(message.get("subject", ""))
    missing = list(dict.fromkeys(a for a in announced if a not in extracted_doc_numbers))
    if missing and not shadow:
        dl_report.log_announced_mismatch(conn, message["message_id"],
                                         message.get("subject", ""), missing,
                                         extracted_doc_numbers)
        _post(cfg, shadow, lambda: dl_report.build_announced_mismatch(
            message.get("subject", ""), message.get("from_addr", ""), missing,
            extracted_doc_numbers, documents=documents_out, link=link), post=post)
    if missing:
        # #238 requirement #2: fed into the AGGREGATE (`_aggregate_status` below) —
        # AFTER the Odoo post/event above so `build_announced_mismatch`'s own
        # per-document rendering never doubles up with these synthetic entries — so
        # `messages.proc_status` itself is never "ok" while a document the mail's own
        # subject announces is genuinely missing, not just alerted separately.
        for num in missing:
            documents_out.append({
                "outcome": "review", "doc_number": num, "synthetic": True,
                "reason": f"V predmete e-mailu je ohlásený dodací list {num}, ale "
                         f"nebol nájdený v žiadnej prílohe"})

    return {"kind": "dl", "dl_snapshot_id": snapshot_id,
           "status": _aggregate_status(documents_out), "documents": documents_out,
           "items": all_items, "announced_mismatch": missing}


# --- one full pipeline pass, finished off exactly like the live claim branch -------

def _run_and_finish(conn, cfg, client, message: dict, snapshot_id: int | None,
                    catalog: list[dict], suppliers: list[dict], upload=None,
                    post=None, list_dirs=None) -> dict | None:
    """One full `_process_message` pass for `message`, finished off exactly like the
    live `engine=python` claim branch of `tick()` always has: an `order_runs` row,
    `messages` marked processed, and the rollup summary event. Returns the result dict
    on a completed pass, `None` on a transient retry or a hard failure (both already
    logged/recorded here — the caller has nothing further to do in either case).

    Shared by `tick()`'s own claim branch AND `release_for_question`'s reprocess-on-
    answer (#240) — the SAME safe, already-tested shape either way: `_process_message`
    always calls `_process_document`, which always claims through `desadv.
    claim_send_or_identify` before any upload, so an ALREADY-SHIPPED document inside
    `message` is never re-uploaded here, no matter which caller triggered this pass."""
    run_id = worker._start_run(conn, message["message_id"], None, shadow=False)
    try:
        result = _process_message(conn, cfg, client, message, snapshot_id, catalog,
                                  suppliers, shadow=False, upload=upload, post=post,
                                  list_dirs=list_dirs)
    except _RetryLater as e:
        log.info("DL message %s: transient failure (attempts=%s) — leaving for the "
                 "30-min stale reclaim: %s", message["message_id"],
                 message.get("attempts"), e)
        worker._finish_run(conn, run_id, "retry",
                           {"kind": "dl", "dl_snapshot_id": snapshot_id, "reason": str(e)},
                           error=str(e))
        # Deep-review finding on this ticket's own PR (#240): `tick()`'s claim branch
        # relies on `_claim()` having already set `processing_at = now()` and `processed
        # = false` — leaving both UNTOUCHED here is what lets R10's 30-minute stale
        # window reclaim the message later. `release_for_question`'s reprocess call
        # never went through `_claim()` at all: the message arrives here with `processed
        # = true` (its earlier, successful pass already set that) and `processing_at =
        # NULL` — leaving BOTH untouched would permanently strand the message outside
        # `_claim()`'s own `WHERE processed = false` filter, with no path back into the
        # normal retry cycle at all (a human's answer recorded, but the document never
        # gets the "second chance" this whole ticket exists to give it). Explicitly
        # re-arming both columns here makes the message reclaimable by the SAME stale
        # window either way — a genuine no-op for the tick()-claim path (both columns
        # already held these values moments earlier) and the actual fix for the
        # release_for_question-reprocess path.
        conn.execute(
            """UPDATE messages SET processed = false, processing_at = now()
                WHERE message_id = %s""", (message["message_id"],))
        report.log_event(conn, message["message_id"], stage="retry", status="retry",
                         outcome=str(e)[:500], rollup=False, workflow=dl_report.WORKFLOW)
        return None
    except Exception as e:
        log.exception("DL pipeline failed for %s", message["message_id"])
        # Deep-review finding, #204: `result=None` leaves `result->>'kind'` NULL, which
        # `reliability.provenance_stats_for_day`'s own `IS DISTINCT FROM 'dl'` exclusion
        # treats as an ORDERS run (NULL is distinct from 'dl') — exactly the rows the
        # DL/orders split exists to keep apart end up miscounted on the busiest signal
        # (a hard failure). Always tag `kind`.
        worker._finish_run(conn, run_id, "error",
                           {"kind": "dl", "dl_snapshot_id": snapshot_id}, error=repr(e))
        # Deep-review finding on this ticket's own PR (#240), same reasoning as the
        # `_RetryLater` branch above: a hard failure during a `release_for_question`
        # reprocess also arrives here with `processed = true` (its earlier, successful
        # pass already set that) — `processing_at = NULL` alone would leave `processed`
        # untouched and permanently strand the message outside `_claim()`'s own `WHERE
        # processed = false` filter, exactly like the retry case, just with no 30-minute
        # stale window at all (a hard failure has always been immediately reclaimable —
        # `processing_at = NULL` puts it straight back in `_claim()`'s pool, matching the
        # existing tick()-claim behaviour this line already had). Re-arming `processed`
        # too is a genuine no-op for the tick()-claim path (already `false`) and the
        # actual fix for the reprocess path.
        conn.execute(
            "UPDATE messages SET processed = false, processing_at = NULL "
            "WHERE message_id = %s", (message["message_id"],))
        return None
    worker._finish_run(conn, run_id, result.get("status", "ok"), result)
    conn.execute(
        """UPDATE messages
              SET processed = true, processed_at = now(), processed_by = %s,
                  processing_at = NULL
            WHERE message_id = %s""", (CATEGORY, message["message_id"]))
    real_docs = [d for d in result.get("documents", []) if not d.get("synthetic")]
    report.log_event(conn, message["message_id"], stage=result.get("status", "ok"),
                     status=result.get("status", "ok"),
                     outcome=_summary_outcome(result),
                     detail={"documents": len(real_docs)},
                     rollup=True, workflow=dl_report.WORKFLOW)
    return result
