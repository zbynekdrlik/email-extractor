"""Delivery-notes (DL) worker — #204, DL migration F5.

Wires F2 (`dl_extract`), F3 (`dl_match`/`dl_memory`/`dl_supplier_memory`/`teach`'s
`dl_item`/`dl_supplier` kinds) and F4 (`desadv_edi`/`desadv`/`upload`) into ONE worker
loop, per the binding spec (`docs/superpowers/specs/2026-08-07-delivery-notes-python-
design.md`, R1-R17, R60-R97, W1-W16, §4). Three modes, the SAME shape `static_worker.py`
already uses (see that module's own docstring):

- `delivery_notes_engine=n8n`, `delivery_notes_shadow=false` (DEFAULT) — completely
  inert. The live n8n "Dodacie Listy EDI" workflow keeps running unchanged.
- `delivery_notes_engine=n8n`, `delivery_notes_shadow=true` — runs the FULL pipeline
  (extraction, matching, EDI build) for comparison only: **claims nothing, uploads
  nothing, marks nothing, teaches nothing** — every DB write in this module is gated on
  `not shadow`. A shadow "duplicate" verdict is read via `desadv.already_sent()`
  (read-only), never `desadv.claim_send()` (which always writes).
- `delivery_notes_engine=python` — claims per MESSAGE (R10: atomic claim, 30-min stale
  reclaim, R11: quarantine at 5 attempts) but decides PER DOCUMENT (R16/W1a fixed by F2:
  every attachment is processed, and one attachment can carry more than one delivery
  note). A message is marked `processed` only once every document extracted from it has
  reached a terminal per-document outcome (ok/partial/review/duplicate) — which, because
  this worker processes every document of a claimed message in ONE synchronous pass (not
  across several ticks), is naturally true every time EXCEPT the R17 transient-retry
  path (`_RetryLater`, below), which leaves the message unmarked for the 30-min reclaim.

**R17/W9 retry semantics are implemented explicitly here, NOT via `worker.tick`'s generic
exception-retry** (which retries ANY exception up to `attempts=5` with no
transient/non-transient distinction — that does not match R17). `_check_retry` classifies
a caught LLM/vision failure against `TRANSIENT_RE` (the same phrase list n8n's own
"Retry transient?" node uses, with NO bare digits — a reason routinely carries money
amounts) and `message["attempts"] < TRANSIENT_RETRY_LIMIT` (3, matching W9's own
"attempts 1-2 retry, attempts 3-4 review, quarantine at 5" reading of the claim's
already-incremented `attempts`). Within that window it raises `_RetryLater`, caught ONLY
by the live-engine branch of `tick()`, which leaves `processing_at` untouched (R10's own
30-minute stale window is what makes it reclaimable) and marks NOTHING. Outside that
window — a non-transient failure, or a transient one that has already exhausted its
retries — the SAME failure becomes a per-document/per-attachment "review" outcome
(`dl_report.build_review`) and processing continues with the rest of the message; the
message is then marked processed as normal (this is R17's own "Attempts 3-4 or
non-transient reason -> Odoo review" — mirrored, not the "retry forever" a bare
`try/except` around the whole tick would give).

**`order_runs`/`order_items` are reused UNMODIFIED (F1's #200 design decision)**, with one
technical resolution F1 itself did not need to make: `order_runs.snapshot_id` has a FK to
`order_snapshots(id)`, NOT `dl_snapshots(id)` — a DL run passes `snapshot_id=None`
(the column is nullable) to `worker._start_run`/`_finish_run` and stashes the REAL DL
snapshot id inside `result["dl_snapshot_id"]` instead. `result["kind"] = "dl"` is the
only discriminator a later reader needs (`reliability.py`'s own `#204` section explains
why every AI-orders query now excludes it explicitly).

**Duplicate documents (W7) and an announced-vs-attached mismatch (spec §4) are NEVER
posted as their own immediate Odoo message** — both are logged to `email_events`
(`dl_report.log_duplicate`/`log_announced_mismatch`) and surfaced in the DAILY digest
(`reliability.dl_provenance_stats_for_day`) instead, per the spec's explicit "hlásenie v
dennom sumári (nie ticho)" for W7. The announced-vs-attached scan (`_subject_doc_numbers`)
deliberately covers ONLY the documented Lunys "IS KARAT" subject shape
(`<digits>LT<digits>`, e.g. "2610LT0100000001") — a supplier with a different subject
convention simply has nothing to compare against (a safe default: no false positives,
never a false "you're missing a DL" alert for a subject shape nobody has verified yet).

**A refused DESADV claim is NOT always a genuine W7 duplicate (#216).** R17's retry
re-processes the WHOLE message on its next tick — including a document that already
shipped successfully in an earlier, partially-failed attempt of THIS SAME message.
`desadv_sent.message_id` records who currently holds the claim; `_process_document`
calls `desadv.claim_send_or_identify()` (the claim decision AND the current claimant's
read, ATOMICALLY in one round trip — never `claim_send()` followed by a separate
`claimed_by()` read, which would leave a TOCTOU gap) to tell "this message is
retrying itself" (logged as `already_shipped_this_run`, excluded from the digest's
`duplicates` count) apart from "a different message really duplicated this document"
(`duplicate_skip`, counted).

**Attachment selection is this worker's OWN scope decision** (`dl_extract.py`'s own
docstring explicitly leaves worker/claim wiring to this phase): only attachments whose
`mime`/filename look like a PDF or an image are read — a real delivery note is always one
of those two; anything else (a signature image, a `.docx`, ...) is skipped rather than fed
to Vision, which n8n's own `Get Attachment Meta ... LIMIT 1` query implicitly also
preferred (R16) before F2 widened it to every PDF-shaped attachment (W1a).
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from .. import store
from . import (
    desadv,
    desadv_edi,
    dl_extract,
    dl_match,
    dl_memory,
    dl_report,
    dl_snapshot,
    dl_supplier_memory,
    llm,
    report,
    teach,
    worker,
)
from . import upload as upload_mod

log = logging.getLogger("orders.dl_worker")

CATEGORY = "dodacie_listy"
CLAIM_STALE_MINUTES = 30          # R10
MAX_ATTEMPTS = 5                  # R11 (quarantine)
TRANSIENT_RETRY_LIMIT = 3         # R17/W9: attempts 1-2 retry, 3-4 go to review
SHADOW_DAYS = 3

PROMPTS_DIR = Path(__file__).with_name("prompts")

# R17: the SAME transient-failure phrase list n8n's own "Retry transient?" node uses —
# deliberately no bare digits (a reason routinely carries a money amount, e.g. the R51
# money-gate breach text, which must never be misread as "transient").
TRANSIENT_RE = re.compile(
    r"service failed to process|timed out|timeout|rate limit|too many requests|"
    r"overloaded|do[cč]asn[yý] v[yý]padok|service unavailable|internal server error|"
    r"bad gateway|econnreset|socket hang up", re.IGNORECASE)

# This worker's own scope decision (see module docstring) — a real DL is always a PDF or
# an image; anything else is skipped rather than fed to Vision.
_ATTACHMENT_MIME_RE = re.compile(r"pdf|image", re.IGNORECASE)
_ATTACHMENT_EXT_RE = re.compile(r"\.(pdf|jpe?g|png|tiff?|bmp)$", re.IGNORECASE)

# Spec §4: the documented Lunys "IS KARAT" DL-number shape inside a subject line.
_SUBJECT_DOC_RE = re.compile(r"\d{2,}LT\d{4,}", re.IGNORECASE)

resolve_engine = worker.resolve_engine


class _RetryLater(Exception):
    """Internal signal only — a transient LLM/vision failure within R17's retry window
    (attempts < 3). Caught by the live-engine branch of `tick()`; never escapes this
    module."""


def _is_transient(message: str) -> bool:
    return bool(TRANSIENT_RE.search(message or ""))


def _check_retry(attempts: int, error: str) -> None:
    """Raises `_RetryLater` when this failure is transient AND still within its retry
    window — the caller's `except` blocks let it propagate all the way up to `tick()`,
    aborting the rest of THIS message's processing (matches n8n's own retry granularity:
    `Retry transient?` operates on the whole claimed message, not a sub-document)."""
    if _is_transient(error) and int(attempts or 0) < TRANSIENT_RETRY_LIMIT:
        raise _RetryLater(error)


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
        """SELECT idx, filename, mime, extracted_text FROM attachments
            WHERE message_id = %s ORDER BY idx""", (message_id,)).fetchall()
    data_dir = getattr(cfg, "data_dir", "") or "/data/store"
    out = []
    for idx, filename, mime, extracted_text in rows:
        if not (_ATTACHMENT_MIME_RE.search(mime or "")
               or _ATTACHMENT_EXT_RE.search(filename or "")):
            continue
        matches = sorted(store.message_dir(data_dir, message_id).glob(f"att{idx}__*"))
        pdf_bytes = matches[0].read_bytes() if matches else b""
        out.append({"idx": idx, "filename": filename or "", "pdf_bytes": pdf_bytes,
                   "machine_text": extracted_text or ""})
    return out


# --- announced-vs-attached (spec §4) ----------------------------------------

def _subject_doc_numbers(subject: str) -> list[str]:
    return [dl_extract.strip_lt_prefix(m) for m in _SUBJECT_DOC_RE.findall(subject or "")]


# --- LLM matching glue (deferred by dl_match.py's own docstring to this phase) ----

SUPPLIER_SCHEMA = {
    "type": "object",
    "properties": {
        "matched": {"type": "boolean"},
        "ean_edi": {"type": "string"},
        "name": {"type": "string"},
        "matchConfidence": {"type": "number"},
        "matchReason": {"type": "string"},
    },
    "required": ["matched"],
}

ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "gtin": {"type": "string"},
        "matchedCatalogName": {"type": "string"},
        "matchConfidence": {"type": "number"},
        "matchReason": {"type": "string"},
        "mass": {"type": "number"},
    },
    "required": ["gtin"],
}


def _supplier_prompt() -> str:
    return (PROMPTS_DIR / "dl_match_supplier.md").read_text(encoding="utf-8")


def _item_prompt() -> str:
    return (PROMPTS_DIR / "dl_match_item.md").read_text(encoding="utf-8")


def _supplier_input(doc: dict, cands: list[dict]) -> str:
    lines = [f"Dodávateľ na dokumente: {doc.get('supplierName', '')} / mesto "
             f"{doc.get('supplierCity', '')} / e-mail {doc.get('supplierEmail', '')}",
             "", "Kandidáti:"]
    for c in cands:
        lines.append(f"- EAN {c.get('ean_edi')}: {c.get('name')} ({c.get('city', '')}, "
                     f"{', '.join(c.get('emails') or [])})")
    return "\n".join(lines)


def _item_input(item: dict, cands: list[dict], partner_name: str) -> str:
    lines = [f"Položka: {item.get('name', '')} ({item.get('quantity')} "
             f"{item.get('unit', '')})",
             f"Dodávateľ tohto dokumentu: {partner_name}", "", "Kandidáti:"]
    for c in cands:
        lines.append(f"- GTIN {c.get('gtin')}: {c.get('name')} "
                     f"(alias: {c.get('doplnok') or '-'})")
    return "\n".join(lines)


def _match_supplier(conn, client, doc: dict, suppliers: list[dict],
                    sender_email: str = "") -> dl_match.SupplierDecision:
    """#240: a HUMAN-taught address (`dl_supplier_memory`, written the moment a
    `dl_supplier` nástenka question is answered) is checked FIRST, before the model is
    ever asked — mirrors AI-orders' own `human_taught` rung sitting above the whole
    `decide_without_model` ladder (`match.py`). Without this, `release_for_question`'s
    reprocess-on-answer would just ask the model again and get the exact same "not
    matched" verdict it got the first time (the model has no way to know a human already
    resolved this address) — the document would never actually finish. A taught EAN that
    no longer exists in the CURRENT supplier snapshot (a retired card) falls through to
    the model exactly as before, the same defensive shape `decide_item`'s own
    `unknown_gtin` guard already uses."""
    if sender_email:
        recalled = dl_supplier_memory.resolve(conn, sender_email)
        if recalled:
            row = next((s for s in suppliers
                       if str(s.get("ean_edi")) == str(recalled["ean_edi"])), None)
            if row:
                log.info("dl supplier memory rescue: %r -> %s (%s)", sender_email,
                         recalled["ean_edi"], recalled.get("name"))
                return dl_match.SupplierDecision(
                    matched=True, ean_edi=row["ean_edi"], name=row.get("name", ""),
                    confidence=0.95,
                    note="Potvrdené naučenou adresou — sklad ju už raz priradil.")
    cands = dl_match.supplier_candidates(doc.get("supplierName", ""),
                                         doc.get("supplierEmail", ""),
                                         doc.get("supplierCity", ""), suppliers)
    answer = client.json_call(_supplier_prompt(), _supplier_input(doc, cands),
                              SUPPLIER_SCHEMA, name="dl_supplier")
    return dl_match.decide_supplier(answer, suppliers)


def _match_item(client, item: dict, catalog: list[dict], recalled,
                partner_name: str) -> dl_match.Decision:
    cands = dl_match.candidates(item.get("name", ""), catalog,
                                memory_gtin=(recalled.gtin if recalled else ""))
    answer = client.json_call(_item_prompt(), _item_input(item, cands, partner_name),
                              ITEM_SCHEMA, name="dl_item")
    return dl_match.decide_item(item.get("name", ""), answer, catalog, recalled=recalled,
                                partner_name=partner_name)


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


def _post(cfg, shadow: bool, build, post=None) -> None:
    if shadow:
        return
    post = post or dl_report.post   # dl_report.post already never raises
    try:
        html = build()
        post(cfg, html)
    except Exception:
        log.exception("posting a DL Odoo message failed")


def _num(value) -> float:
    try:
        f = float(value)
        return f if f == f else 0.0   # filters NaN
    except (TypeError, ValueError):
        return 0.0


def _shipped_items(decisions: list[tuple[dict, dl_match.Decision]]) -> list[dict]:
    """The gtin/quantity of every item that actually ends up ON THE EDI — same filter
    `desadv_edi.build()` itself applies (real gtin via `desadv_edi._is_unmatched`,
    non-zero quantity — review finding, #205: routed through the SAME shared predicate
    `generate()`/`build()` already share per #203, rather than a third ad-hoc `gtin`
    truthy check that could silently diverge from it). Exposed on `_process_document`'s
    returned dict (#205, DL migration F6) so a caller — the eval harness's own
    per-document scoring, or a future debugging/digest read — never has to re-derive it
    from the aggregate `all_items` list, which carries no per-document tag and cannot be
    split back apart for a multi-document message."""
    return [{"gtin": d.gtin, "quantity": item.get("quantity")}
           for item, d in decisions
           if not desadv_edi._is_unmatched(d.gtin) and _num(item.get("quantity")) != 0]


# --- one document (R60-R97) -------------------------------------------------

def _process_document(conn, cfg, client, message: dict, doc: dict, catalog: list[dict],
                      suppliers: list[dict], shadow: bool, all_items: list[dict],
                      upload=None, post=None) -> dict:
    subject, from_addr = message.get("subject", ""), message.get("from_addr", "")
    doc_number = doc.get("docNumber") or ""
    delivery_date = doc.get("deliveryDate", "")
    # #240: computed once, reused both for the memory-rescue lookup in `_match_supplier`
    # and for the `teach.ask_dl_supplier` call below — the SAME address must key both,
    # or a taught mapping would never actually match what a later ask/resolve looks up.
    sender_email = doc.get("supplierEmail") or from_addr
    upload = upload or (lambda c, name, content, dir_override=None:
                        upload_mod.put(c, name, content, dir_override=dir_override))
    # #229 follow-up 2: computed once, reused by every build_review/build_success call
    # below — each function decides for ITSELF whether this specific message actually
    # needs the link (review always does; success only when it raised a real question).
    # #231: the DL-only nástenka link, never the mixed AI-orders `sklad_link`.
    link = report.dl_sklad_link(cfg)

    if doc.get("status") == "needsReview":
        reason = doc.get("reviewReason") or "Dokument potrebuje kontrolu"
        _post(cfg, shadow, lambda: dl_report.build_review(
            reason, doc.get("supplierName", ""), doc_number, delivery_date, from_addr,
            subject, link=link), post=post)
        _event(conn, shadow, message["message_id"], stage="review", status="review",
              outcome=reason, detail={"doc_number": doc_number}, rollup=False,
              workflow=dl_report.WORKFLOW)
        return {"outcome": "review", "doc_number": doc_number,
               "supplier_name": doc.get("supplierName", ""), "reason": reason}

    try:
        supplier_decision = _match_supplier(conn, client, doc, suppliers, sender_email)
    except Exception as e:
        _check_retry(message.get("attempts", 0), str(e))
        reason = f"Zlyhalo párovanie dodávateľa: {e}"
        _post(cfg, shadow, lambda: dl_report.build_review(
            reason, "", doc_number, delivery_date, from_addr, subject, link=link),
            post=post)
        _event(conn, shadow, message["message_id"], stage="review", status="error",
              outcome=reason, detail={"doc_number": doc_number}, rollup=False,
              workflow=dl_report.WORKFLOW)
        return {"outcome": "review", "doc_number": doc_number, "supplier_name": "",
               "reason": reason}

    if not supplier_decision.matched:
        if not shadow:
            cands = dl_match.supplier_candidates(
                doc.get("supplierName", ""), doc.get("supplierEmail", ""),
                doc.get("supplierCity", ""), suppliers)
            teach.ask_dl_supplier(conn, message["message_id"], sender_email, cands,
                                  delivery_date=delivery_date)
        _post(cfg, shadow, lambda: dl_report.build_review(
            supplier_decision.note, "", doc_number, delivery_date, from_addr, subject,
            link=link), post=post)
        _event(conn, shadow, message["message_id"], stage="review", status="review",
              outcome=supplier_decision.note, detail={"doc_number": doc_number},
              rollup=False, workflow=dl_report.WORKFLOW)
        return {"outcome": "review", "doc_number": doc_number, "supplier_name": "",
               "reason": supplier_decision.note}

    matched_items: list[dict] = []
    decisions: list[tuple[dict, dl_match.Decision]] = []
    catalog_gtins = {str(c.get("gtin")) for c in catalog}
    for item in doc.get("items") or []:
        recalled = dl_memory.resolve(conn, supplier_decision.ean_edi, item.get("name", ""),
                                     catalog_gtins=catalog_gtins,
                                     as_of=message.get("today", ""))
        try:
            decision = _match_item(client, item, catalog, recalled, supplier_decision.name)
        except Exception as e:
            _check_retry(message.get("attempts", 0), str(e))
            decision = dl_match.Decision(
                item_name=item.get("name", ""), gtin=None, card="", mass=0.0,
                confidence=0.0, rule="match_failed",
                note=f"Zlyhalo párovanie položky: {e}", review=True)
        decisions.append((item, decision))
        matched_items.append({
            "gtin": decision.gtin, "name": decision.item_name,
            "matchedCatalogName": decision.card, "quantity": item.get("quantity"),
            "unit": item.get("unit"), "unitPrice": item.get("unitPrice"),
            "totalPrice": item.get("totalPrice"), "mass": decision.mass})
        all_items.append({
            "name": decision.item_name, "quantity": item.get("quantity"),
            "unit": item.get("unit"), "gtin": decision.gtin, "card": decision.card,
            "confidence": decision.confidence, "rule": decision.rule,
            "trace": decision.trace})
        # Deep-review finding on #204's own PR: gate on `not decision.gtin` — the item
        # is genuinely EXCLUDED from the EDI whenever `desadv_edi._is_unmatched(gtin)`
        # would fire (i.e. `gtin` is falsy), which is EVERY rule that ends up here with
        # no card (`unmatched`, the R75 lexical-gap tripwire `llm_sure_lexical_gap`,
        # and this function's own `match_failed` fallback) — not only the literal
        # string `"unmatched"`. Keying on the rule NAME instead of the actual EDI
        # exclusion left those other two rules silently invisible: no nástenka
        # question, no line in the Odoo message, nothing — exactly the loss class
        # this migration exists to remove.
        if not decision.gtin and not shadow:
            cands = dl_match.candidates(item.get("name", ""), catalog,
                                        memory_gtin=(recalled.gtin if recalled else ""))
            teach.ask_dl_item(conn, message["message_id"], supplier_decision.ean_edi,
                              supplier_decision.name, item.get("name", ""),
                              item.get("quantity"), item.get("unit", ""), cands,
                              delivery_date=delivery_date, reason=decision.note)

    header = {"customerName": supplier_decision.name,
             "customerEanEdi": supplier_decision.ean_edi}
    extraction = {"docNumber": doc_number, "deliveryDate": delivery_date}
    built = desadv_edi.build(header, extraction, matched_items, catalog)

    if not built.can_create:
        _post(cfg, shadow, lambda: dl_report.build_review(
            built.reject_reason, supplier_decision.name, built.doc_number, delivery_date,
            from_addr, subject, link=link), post=post)
        _event(conn, shadow, message["message_id"], stage="review", status="review",
              outcome=built.reject_reason, detail={"doc_number": built.doc_number},
              rollup=False, workflow=dl_report.WORKFLOW)
        return {"outcome": "review", "doc_number": built.doc_number,
               "supplier_name": supplier_decision.name, "reason": built.reject_reason}

    if shadow:
        is_dup = desadv.already_sent(conn, supplier_decision.ean_edi, built.doc_number)
        outcome = "duplicate" if is_dup else ("partial" if built.partial else "ok")
        return {"outcome": outcome, "doc_number": built.doc_number,
               "supplier_name": supplier_decision.name,
               "supplier_ean": supplier_decision.ean_edi, "filename": built.filename,
               "line_count": built.line_count,
               "items_skipped_no_match": built.items_skipped_no_match,
               "price_substitutions": built.price_substitutions,
               "items": _shipped_items(decisions)}

    claimed, holder = desadv.claim_send_or_identify(
        conn, supplier_decision.ean_edi, built.doc_number, built.filename,
        message_id=message["message_id"])
    if not claimed:
        # #216: a claim refusal has TWO different causes, and only one of them is a
        # genuine W7 duplicate. R17's transient retry re-processes the WHOLE message
        # on the next tick, including any document that already shipped earlier in a
        # prior (partially-failed) attempt of THIS SAME message — that self-caused
        # re-skip must not be counted the same as a real different message (e.g. a
        # supplier's genuine re-announcement) having already sent this exact document.
        # Compare the CURRENT claimant (read atomically alongside the claim decision
        # itself, `claim_send_or_identify` — never a separate follow-up read, review
        # finding on this ticket's own PR) against the message being processed now.
        if holder == message["message_id"]:
            dl_report.log_already_shipped_this_run(
                conn, message["message_id"], built.doc_number, supplier_decision.ean_edi)
        else:
            # W7 fix: visible in the daily digest, never a silent skip (R32's "quiet by
            # design" is exactly what lost the Lunys X1/X2 pair in the incident spec §4
            # documents).
            dl_report.log_duplicate(conn, message["message_id"], built.doc_number,
                                    supplier_decision.ean_edi)
        return {"outcome": "duplicate", "doc_number": built.doc_number,
               "supplier_name": supplier_decision.name}

    try:
        upload(cfg, desadv_edi.upload_name(built.filename), built.content,
              dir_override=getattr(cfg, "orion_dl_dir", upload_mod.DL_DIR))
    except Exception as e:
        desadv.release_send(conn, supplier_decision.ean_edi, built.doc_number)
        log.exception("DL upload of %s failed", built.filename)
        note = ("Odoslanie dodacieho listu do ORIONu zlyhalo — skús znova alebo "
               "nahlás administrátorovi")
        # `e` is captured into a plain string BEFORE the lambda, never referenced
        # inside it — `except ... as e` implicitly deletes `e` at the end of this
        # block, which would otherwise make a closure over it fragile (it happens to
        # run early enough today, but `_post`'s own try/except would silently
        # swallow a future NameError here and degrade the alert to a bare generic
        # message — the exact silent-loss failure mode fix #5 exists to prevent).
        detail_text = f"{note} ({built.filename}): {e}"
        _post(cfg, shadow, lambda: dl_report.build_review(
            detail_text, supplier_decision.name, built.doc_number, delivery_date,
            from_addr, subject, link=link), post=post)
        _event(conn, shadow, message["message_id"], stage="review", status="error",
              outcome=note, detail={"error": repr(e), "filename": built.filename},
              rollup=False, workflow=dl_report.WORKFLOW)
        return {"outcome": "review", "doc_number": built.doc_number,
               "supplier_name": supplier_decision.name, "reason": note}

    desadv.confirm_sent(conn, supplier_decision.ean_edi, built.doc_number,
                        pg_dsn=getattr(cfg, "pg_dsn", ""))

    today = message.get("today") or datetime.now(UTC).date().isoformat()
    for _item, decision in decisions:
        if not desadv_edi._is_unmatched(decision.gtin):
            # R91: item-history write, ONLY for what actually shipped.
            dl_memory.remember(conn, supplier_decision.ean_edi, decision.item_name,
                              decision.gtin, decision.card, today, source="ship")

    shipped, unmatched_notes, borderline_notes, history_notes = [], [], [], []
    for item, decision in decisions:
        if not desadv_edi._is_unmatched(decision.gtin) and _num(item.get("quantity")) != 0:
            shipped.append({"name": decision.card or decision.item_name,
                           "quantity": item.get("quantity"), "unit": item.get("unit")})
            if decision.rule == "llm_borderline":
                borderline_notes.append(
                    f"{decision.item_name} ({decision.card}, istota "
                    f"{round(decision.confidence * 100)} %)")
            elif decision.rule == "weight_override":
                history_notes.append(f"{decision.item_name} -> {decision.card} "
                                     f"({decision.note})")
        elif not decision.gtin:
            # Same fix as the nástenka gate above: any decision with no real gtin was
            # excluded from the EDI, regardless of which rule produced it.
            unmatched_notes.append(f"{decision.item_name} ({decision.note})")

    outcome = "partial" if built.partial else "ok"
    _post(cfg, shadow, lambda: dl_report.build_success(
        supplier_decision.name, built.doc_number, delivery_date, from_addr, subject,
        shipped, unmatched_items=unmatched_notes, borderline_notes=borderline_notes,
        history_notes=history_notes, price_substitutions=built.price_substitutions,
        filename=built.filename, partial=built.partial, link=link),
        post=post)
    _event(conn, shadow, message["message_id"], stage="uploaded_orion", status="ok",
          outcome=f"EDI vytvorené: {built.filename}",
          detail={"doc_number": built.doc_number, "edi_file": built.filename},
          rollup=False, workflow=dl_report.WORKFLOW)
    return {"outcome": outcome, "doc_number": built.doc_number,
           "supplier_name": supplier_decision.name,
           "supplier_ean": supplier_decision.ean_edi, "filename": built.filename,
           "line_count": built.line_count,
           "items_skipped_no_match": built.items_skipped_no_match,
           "items_skipped_zero_qty": built.items_skipped_zero_qty,
           "price_substitutions": built.price_substitutions,
           # #229 follow-up 2, review finding: `outcome`/`built.partial` alone is NOT a
           # precise "did this raise a real dl_item board question" signal --
           # desadv_edi.build() excludes a zero-quantity item from its own no_match/
           # partial computation even when unmatched, but dl_worker's own teach.ask_
           # dl_item call above fires for ANY unmatched item regardless of quantity.
           # `unmatched_notes` is that exact, precise signal (same list build_success's
           # own link condition already reads) -- carry it through so
           # dl_report._outcome_needs_link can check it directly instead of
           # re-deriving an imprecise proxy from `outcome`.
           "unmatched_items": unmatched_notes,
           "items": _shipped_items(decisions)}


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
    if all(o in ("review", "duplicate") for o in outcomes) and "review" in outcomes:
        return "review"
    if any(o == "partial" for o in outcomes) or ("review" in outcomes and
                                                  any(o in ("ok", "partial")
                                                      for o in outcomes)):
        return "partial"
    return "ok"


def _summary_outcome(result: dict) -> str:
    docs = result.get("documents") or []
    if not docs:
        return "žiadny dodací list sa nepodarilo rozpoznať"
    counts: dict[str, int] = {}
    for d in docs:
        counts[d.get("outcome", "review")] = counts.get(d.get("outcome", "review"), 0) + 1
    bits = ", ".join(f"{v}x {k}" for k, v in counts.items())
    return f"{len(docs)} dokument(y): {bits}"


def _process_message(conn, cfg, client, message: dict, snapshot_id: int | None,
                     catalog: list[dict], suppliers: list[dict], shadow: bool,
                     upload=None, post=None, attachments: list[dict] | None = None) -> dict:
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

    if not attachments:
        # R15: no attachment (or nothing PDF/image-shaped) is NOT an error.
        reason = "Email bez prílohy — pravdepodobne bežná správa"
        _post(cfg, shadow, lambda: dl_report.build_review(
            reason, from_addr=message.get("from_addr", ""),
            subject=message.get("subject", ""), link=link), post=post)
        _event(conn, shadow, message["message_id"], stage="review", status="review",
              outcome=reason, rollup=True, workflow=dl_report.WORKFLOW)
        return {"kind": "dl", "dl_snapshot_id": snapshot_id, "status": "review",
               "documents": [{"outcome": "review", "reason": reason}], "items": []}

    extraction = dl_extract.extract_email(client, attachments)

    documents_out: list[dict] = []
    all_items: list[dict] = []
    extracted_doc_numbers: list[str] = []

    for att in extraction["attachments"]:
        if att.get("error"):
            _check_retry(message.get("attempts", 0), att["error"])
            reason = (f"Príloha {att.get('filename') or att.get('idx')} sa nepodarilo "
                      f"spracovať: {att['error']}")
            _post(cfg, shadow, lambda reason=reason: dl_report.build_review(
                reason, from_addr=message.get("from_addr", ""),
                subject=message.get("subject", ""), link=link), post=post)
            _event(conn, shadow, message["message_id"], stage="review", status="error",
                  outcome=reason, detail={"idx": att.get("idx")}, rollup=False,
                  workflow=dl_report.WORKFLOW)
            documents_out.append({"outcome": "review", "reason": reason,
                                  "attachment_idx": att.get("idx")})

    for doc in extraction["documents"]:
        extracted_doc_numbers.append(doc.get("docNumber") or "")
        documents_out.append(_process_document(conn, cfg, client, message, doc, catalog,
                                                suppliers, shadow, all_items,
                                                upload=upload, post=post))

    if not documents_out:
        reason = "Nepodarilo sa rozpoznať žiadny dodací list v prílohách"
        _post(cfg, shadow, lambda: dl_report.build_review(
            reason, from_addr=message.get("from_addr", ""),
            subject=message.get("subject", ""), link=link), post=post)
        _event(conn, shadow, message["message_id"], stage="review", status="review",
              outcome=reason, rollup=True, workflow=dl_report.WORKFLOW)
        documents_out.append({"outcome": "review", "reason": reason})

    # spec §4: announced-vs-attached. Shadow guarantees nothing observable leaves the
    # process, so neither the event log nor the Odoo post fire while shadowing (deep-
    # review finding, #204 — the mismatch count must not be inflated by shadow runs).
    announced = _subject_doc_numbers(message.get("subject", ""))
    missing = [a for a in announced if a not in extracted_doc_numbers]
    if missing and not shadow:
        dl_report.log_announced_mismatch(conn, message["message_id"],
                                         message.get("subject", ""), missing,
                                         extracted_doc_numbers)
        _post(cfg, shadow, lambda: dl_report.build_announced_mismatch(
            message.get("subject", ""), message.get("from_addr", ""), missing,
            extracted_doc_numbers, documents=documents_out, link=link), post=post)

    return {"kind": "dl", "dl_snapshot_id": snapshot_id,
           "status": _aggregate_status(documents_out), "documents": documents_out,
           "items": all_items, "announced_mismatch": missing}


# --- one full pipeline pass, finished off exactly like the live claim branch -------

def _run_and_finish(conn, cfg, client, message: dict, snapshot_id: int | None,
                    catalog: list[dict], suppliers: list[dict], upload=None,
                    post=None) -> dict | None:
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
                                  suppliers, shadow=False, upload=upload, post=post)
    except _RetryLater as e:
        log.info("DL message %s: transient failure (attempts=%s) — leaving for the "
                 "30-min stale reclaim: %s", message["message_id"],
                 message.get("attempts"), e)
        worker._finish_run(conn, run_id, "retry",
                           {"kind": "dl", "dl_snapshot_id": snapshot_id, "reason": str(e)},
                           error=str(e))
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
        conn.execute(
            "UPDATE messages SET processing_at = NULL WHERE message_id = %s",
            (message["message_id"],))
        return None
    worker._finish_run(conn, run_id, result.get("status", "ok"), result)
    conn.execute(
        """UPDATE messages
              SET processed = true, processed_at = now(), processed_by = %s,
                  processing_at = NULL
            WHERE message_id = %s""", (CATEGORY, message["message_id"]))
    report.log_event(conn, message["message_id"], stage=result.get("status", "ok"),
                     status=result.get("status", "ok"),
                     outcome=_summary_outcome(result),
                     detail={"documents": len(result.get("documents", []))},
                     rollup=True, workflow=dl_report.WORKFLOW)
    return result


# --- #240: an answered dl_item/dl_supplier question gives its document another chance --

def release_for_question(conn, cfg, qid: int, client=None, upload=None,
                         post=None) -> list[dict]:
    """Once every `dl_item`/`dl_supplier` nástenka question raised for ONE message has
    been answered, that message is given a genuine second chance to finish — the exact
    thing that was missing before this ticket (`teach._apply_dl_item`/`_apply_dl_supplier`
    used to just write memory and stop, so an answered question never unstuck the
    document that raised it, per the design comment on #240).

    Waits for EVERY sibling `dl_item`/`dl_supplier` question of the SAME message before
    reprocessing (mirrors `hold.release_for_question`'s own "every question_id answered"
    gate) — reprocessing after only SOME of them are answered would just re-raise the
    exact same still-open questions for nothing. The caller (`teach._apply_dl_item`/
    `_apply_dl_supplier`) already marks `qid` itself 'answered' BEFORE calling this, so
    it never counts itself as still-open.

    Re-runs the WHOLE message through the ordinary, already-tested `_process_message`
    pipeline (`_run_and_finish`) rather than a bespoke "redecide without a model call"
    path (the design AI-orders' `hold.py` uses): unlike AI-orders' hold, a DL document
    blocked on `dl_supplier` never even reached item matching the first time (the item
    loop only runs after a successful supplier match) — there is no earlier decision to
    redecide, the model genuinely has to be asked. Safety against re-uploading an
    ALREADY-shipped document (e.g. one that partially shipped, then later got its
    excluded item's `dl_item` question answered) is NOT new code here — it is the SAME
    `desadv.claim_send_or_identify` claim every `_process_document` call already makes
    before any upload; reprocessing this message again, the ledger reports the confirmed
    claim exactly like a genuine R17 self-retry and refuses the upload (see
    `_process_document`'s own `already_shipped_this_run` handling) — see
    `test_release_for_question_never_reuploads_an_already_partially_shipped_document`.

    Returns the reprocessed message's own `documents` list (mirrors AI-orders'
    `hold.release_for_question`'s `released` list) — `[]` when nothing happened yet (a
    sibling question is still open), or when there is genuinely nothing to reprocess
    (the message row is gone, or no catalog snapshot is loaded)."""
    still_open = conn.execute(
        """SELECT 1 FROM order_questions
            WHERE message_id = (SELECT message_id FROM order_questions WHERE id = %s)
              AND kind IN ('dl_item', 'dl_supplier') AND status = 'open' LIMIT 1""",
        (qid,)).fetchone()
    if still_open:
        return []
    qrow = conn.execute(
        "SELECT message_id FROM order_questions WHERE id = %s", (qid,)).fetchone()
    if not qrow:
        return []
    message_id = qrow[0]
    msg_row = conn.execute(
        """SELECT message_id, subject, from_addr, from_name, combined_text, body_text,
                  has_attachments FROM messages WHERE message_id = %s""",
        (message_id,)).fetchone()
    if not msg_row:
        return []
    message = _as_message(msg_row)
    snapshot_id = dl_snapshot.latest_snapshot_id(conn)
    if not snapshot_id:
        log.warning("release_for_question(%s): no DL catalog snapshot yet — cannot "
                    "reprocess %s", qid, message_id)
        return []
    catalog = dl_snapshot.load_catalog(conn, snapshot_id)
    suppliers = dl_snapshot.load_suppliers(conn, snapshot_id)
    if client is None:
        client = llm.from_config(cfg)
    result = _run_and_finish(conn, cfg, client, message, snapshot_id, catalog, suppliers,
                             upload=upload, post=post)
    return (result or {}).get("documents", [])


# --- one tick ----------------------------------------------------------------

def tick(conn, cfg, client=None, upload=None, post=None) -> int:
    """Process at most one `dodacie_listy` message. Returns 0 or 1 (whether a MESSAGE
    reached a terminal outcome this tick — a transient retry, per R17, returns 0: nothing
    was actually completed).

    `client`/`upload`/`post` are injected so the worker's claim/shadow/retry/upload
    guarantees can be tested without the LLM stack, a real ORION host, or Odoo — same
    convention `static_worker.tick` already uses. Production passes the real
    `llm.from_config(cfg)` and leaves `upload`/`post` at their defaults
    (`upload_mod.put`/`dl_report.post`).
    """
    engine = resolve_engine(getattr(cfg, "delivery_notes_engine", "n8n"))
    shadow = bool(getattr(cfg, "delivery_notes_shadow", False))
    if engine != "python" and not shadow:
        return 0

    snapshot_id = dl_snapshot.latest_snapshot_id(conn)
    if not snapshot_id:
        log.warning("no DL catalog snapshot yet — DL worker idle")
        return 0
    catalog = dl_snapshot.load_catalog(conn, snapshot_id)
    suppliers = dl_snapshot.load_suppliers(conn, snapshot_id)
    if client is None:
        client = llm.from_config(cfg)

    if engine == "python":
        message = _claim(conn)
        if not message:
            return 0
        result = _run_and_finish(conn, cfg, client, message, snapshot_id, catalog,
                                 suppliers, upload=upload, post=post)
        return 1 if result is not None else 0

    message = _peek_for_shadow(conn, getattr(cfg, "delivery_notes_shadow_days",
                                             SHADOW_DAYS))
    if not message:
        return 0
    run_id = worker._start_run(conn, message["message_id"], None, shadow=True)
    try:
        result = _process_message(conn, cfg, client, message, snapshot_id, catalog,
                                  suppliers, shadow=True, upload=upload, post=post)
    except _RetryLater as e:
        # Deep-review finding, #204: a shadow peek is not a claim — there is nothing
        # to "retry", but a routine transient LLM hiccup is not an ERROR either; log
        # it at info (not a full traceback) so it doesn't read as a genuine failure.
        log.info("DL shadow pipeline hit a transient failure for %s (no observable "
                 "effect — shadow claims/marks/writes nothing): %s",
                 message["message_id"], e)
        worker._finish_run(conn, run_id, "error",
                           {"kind": "dl", "dl_snapshot_id": snapshot_id, "reason": str(e)},
                           error=str(e))
        return 0
    except Exception as e:
        log.exception("DL shadow pipeline failed for %s", message["message_id"])
        worker._finish_run(conn, run_id, "error",
                           {"kind": "dl", "dl_snapshot_id": snapshot_id}, error=repr(e))
        return 0
    worker._finish_run(conn, run_id, result.get("status", "ok"), result)
    return 1
