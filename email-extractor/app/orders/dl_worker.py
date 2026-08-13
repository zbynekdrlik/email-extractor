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

**A mail-body-sourced (#258) document whose OWN mail reads as a CORRECTION/AMENDMENT
never auto-ships (#265).** HK LOAN (`gnip@hkloan.eu`) writes delivery notes directly
into the mail body and routinely follows up with a short mail that only restates ONE
changed line of an earlier, separately-sent full delivery ("Zvyšok dodania bez zmien" —
"rest unchanged"). This worker has zero cross-message memory — extracting such a
follow-up ALONE would silently produce a document missing every item the follow-up
never repeats. Per the owner's binding decision on #265 (2026-08-13, "možnosť 1"):
`_looks_like_correction` detects this from the subject/body (gated `not shadow` — a
shadow run still extracts a correction mail for comparison, per this module's own
"shadow runs the FULL pipeline" contract; it never claims/uploads/teaches regardless,
via `_process_document`'s own shadow branch) BEFORE `dl_extract.extract_email` is ever
called for a LIVE run, and routes straight to `review` — no model call, no
supplier/item matching, no `dl_item`/`dl_supplier` question, no claim, no upload, ever.
The review text explicitly tells the warehouse that if the original document was
already imported into CODEX, the fix has to happen there BY HAND — this engine cannot
amend an already-imported document and must never try (there is currently no
system-side way to do it). See the #265 design comment (`gh issue comment`) for the
full rejected-alternative reasoning behind the exact trigger-word set.

**`release_for_question` also releases orphaned same-sender siblings for a `dl_
supplier` answer (#265 gap 2).** `teach.ask_dl_supplier`'s per-sender dedupe means only
the FIRST stuck message from a still-unregistered sender ever gets its own `order_
questions` row — every later message from that sender is left `processed=true`/`proc_
status='review'` forever, tied to nothing. Once the sender is finally taught, `_release_
stuck_siblings` resets every OTHER orphaned same-sender message back into the normal
`_claim()` pool (bounded, never a synchronous storm) instead of leaving them stuck.
Deliberately scoped to `dl_supplier` only — see that function's own docstring for why.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from html import escape
from pathlib import Path

import psycopg

from .. import store
from . import (
    desadv,
    desadv_edi,
    dl_alerts,
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

# #258: the synthetic "attachment" idx used for the mail's own body text when there is
# no usable real attachment — a real `attachments.idx` is always a non-negative 0-based
# index assigned at ingest (`app/db.py`'s `enumerate()` insert), so -1 can never collide
# with one.
_BODY_TEXT_IDX = -1

# #258 deep-review finding: `messages.combined_text` (built by `app/process.py`'s
# `_combined_text`) is Subject + From + Body, and then — ONLY when at least one
# attachment was successfully read as real text (not skipped, not vision-only) — an
# "Attachments:\n===== <filename> =====\n<text>" block appended after a blank line. A
# message whose only attachment is a non-PDF/image type (.docx/.xlsx/.csv/...) is
# entirely invisible to `_read_attachments` (its MIME/ext filter is PDF/image only, see
# `_ATTACHMENT_MIME_RE`/`_ATTACHMENT_EXT_RE` above) — so `usable_attachments` is empty
# for it too, and WITHOUT this marker the body-text fallback below would silently start
# reading that attachment's own extracted text as if it were mail prose. That directly
# contradicts this module's own documented scope decision ("anything else — a .docx,
# ... — is skipped rather than fed to Vision"): a .docx/.xlsx delivery note should stay
# out of scope here, exactly as before this fix, not sneak back in through the body-text
# side door. `_combined_text` always inserts this exact marker (a literal, ASCII-only
# string the `_strip_invisible` pass never touches) as the LAST part it joins, so
# truncating the first occurrence is safe and exact.
_COMBINED_TEXT_ATTACHMENTS_MARKER = "\n\nAttachments:\n"


def _mail_body_only(combined_text: str) -> str:
    """Subject + From + Body ONLY — strips `_combined_text`'s own attachment-text block,
    if present, so the #258 body-text fallback below can never accidentally read a
    non-PDF/image attachment's extracted text instead of the mail's own prose."""
    return (combined_text or "").split(_COMBINED_TEXT_ATTACHMENTS_MARKER, 1)[0]

# Spec §4: the documented Lunys "IS KARAT" DL-number shape inside a subject line.
_SUBJECT_DOC_RE = re.compile(r"\d{2,}LT\d{4,}", re.IGNORECASE)

# #265: Slovak correction/amendment word stems — see the module docstring's own #265
# paragraph and the design comment on the ticket (`gh issue comment`) for the full
# evidence + rejected-alternative reasoning. "oprav"/"korek" are strong, low-ambiguity
# signals in a delivery-note mailbox (both real HK LOAN incidents quoted on the ticket
# trip on "oprav" alone — mail 6389 "OPRAVA HMOTNOSTI", mail 4417 "... + oprava v
# dátume dodania"), checked in BOTH subject and body. "zmena" was considered and
# deliberately EXCLUDED — too common in unrelated administrative mail ("zmena adresy",
# "zmena banky") to be a safe standalone trigger; see `test_innocent_zmena_wording_
# does_not_trip_the_correction_detector` for the negative case this is verified against.
#
# Deep-review finding on this ticket's own PR (#265): the FIRST cut of `_CORRECTION_
# STRONG_RE` used the plain ASCII stem "korekci" only, which misses "korektúra"/
# "korektúru" (a genuine Slovak synonym for "correction") — `korek(?:ci|t[uú]r)` covers
# both. The Slovak alphabet's own case-folding correctly matches diacritic forms of
# "oprav"/"korek" with plain `re.IGNORECASE` (verified: no combining-mark stripping
# needed for THIS word family — unlike "dopln" below, neither stem's own letters carry
# a diacritic in their base ASCII form).
_CORRECTION_STRONG_RE = re.compile(r"\b(?:oprav|korek(?:ci|t[uú]r))\w*", re.IGNORECASE)

# Deep-review finding on this ticket's own PR (#265): the FIRST cut of this stem was
# the plain ASCII "dopln" — which structurally CANNOT match its own most natural
# Slovak forms ("DOPLŇUJÚCE", "doplňujúce", "dopĺňame", "doplňte" all replace the
# plain "l"/"n" with the diacritic letters ľ/ĺ/ň) even though the ticket's own
# description explicitly frames the risk class as "DOPLŇUJÚCE/OPRAVNÉ maily" — a real
# false-NEGATIVE bug that would have silently auto-shipped exactly the incomplete-
# delivery mail this whole ticket exists to catch. `dop(?:ln|lň|ĺň)` covers the plain
# form plus both diacritic variants (verified against all four cited forms).
#
# Deliberately checked ONLY in the SUBJECT, never the body — "dopln"/"doplnok" is
# ordinary Slovak vocabulary ("doplnok stravy" = dietary supplement, a real product
# category) that can legitimately appear as a delivered ITEM's own name inside a
# mail-body-sourced (#258) delivery note's body text; scanning the body too would
# risk permanently misrouting a genuine supplier who happens to sell such products.
# The subject alone is where BOTH real HK LOAN incidents' own signal actually lives —
# no live evidence needs "dopln" in the body specifically.
_CORRECTION_DOPLN_SUBJECT_RE = re.compile(r"\bdop(?:ln|lň|ĺň)\w*", re.IGNORECASE)

_CORRECTION_EXCERPT_LIMIT = 500


def _looks_like_correction(subject: str, body_text: str) -> bool:
    """#265: true when the subject OR the mail's own body text (Subject+From+Body, via
    `_mail_body_only` — never an attachment's own text) carries a correction/amendment
    stem. ONLY ever consulted for the #258 mail-body-sourced path (a real PDF/image
    attachment is out of scope — see the module docstring). See the two regexes above
    for why "dopln" is subject-only while "oprav"/"korek" cover subject AND body."""
    subject, body_text = subject or "", body_text or ""
    if _CORRECTION_STRONG_RE.search(subject) or _CORRECTION_STRONG_RE.search(body_text):
        return True
    return bool(_CORRECTION_DOPLN_SUBJECT_RE.search(subject))


def _correction_review_reason(body_text: str) -> str:
    """The mandated wording (owner's binding #265 decision, 2026-08-13): NEVER auto-
    ship, always manual review, and — because there is currently no way to amend a
    document already imported into CODEX — explicitly say the fix may have to happen
    there BY HAND. One honest wording deliberately covers BOTH "not yet imported" and
    "already imported": a best-effort `desadv_sent` lookup was considered and rejected
    (see the design comment) — a correction mail almost never carries its own doc
    number, so there is no reliable key to look the earlier document up by, and a wrong
    "not yet imported" claim would be worse than no claim at all. The mail's own text is
    quoted verbatim (never an AI interpretation of it) so the warehouse reads exactly
    what the supplier wrote.

    Deep-review finding on this ticket's own PR (#265): `build_review` wraps `reason`
    in a single `<p>` with no `nl2br` — a multi-line excerpt embedded with its own raw
    newlines rendered as one visually run-together paragraph in Odoo. Collapsing
    whitespace here (never truncating meaning, just normalizing layout) keeps the
    quoted text readable without needing any HTML change downstream."""
    excerpt = " ".join((body_text or "").split())
    if len(excerpt) > _CORRECTION_EXCERPT_LIMIT:
        excerpt = excerpt[:_CORRECTION_EXCERPT_LIMIT].rstrip() + " (...)"
    return (
        "Tento e-mail vyzerá ako OPRAVA/DOPLNOK k dodaciemu listu poslanému skôr "
        "samostatným mailom, nie je to kompletný nový doklad — preto sa NESPRACOVAL "
        "automaticky. Skontroluj ho ručne oproti pôvodnému mailu od rovnakého "
        "dodávateľa — táto správa môže meniť len jednu položku, ostatné položky "
        "pôvodného dodania v nej môžu úplne chýbať. Ak bol pôvodný dodací list už "
        "nahratý/naimportovaný do systému ORION (priečinok archCodex v CODEXe), "
        "TÚTO OPRAVU TREBA UROBIŤ RUČNE PRIAMO V CODEXe — systém naimportovaný "
        "doklad upraviť nevie, nesmie sa o to ani pokúšať a nikdy ho nenahráva do "
        f"ORIONu znova. Text e-mailu: {excerpt}"
    )


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
        """SELECT idx, filename, mime, extracted_text, method FROM attachments
            WHERE message_id = %s ORDER BY idx""", (message_id,)).fetchall()
    data_dir = getattr(cfg, "data_dir", "") or "/data/store"
    out = []
    for idx, filename, mime, extracted_text, method in rows:
        if not (_ATTACHMENT_MIME_RE.search(mime or "")
               or _ATTACHMENT_EXT_RE.search(filename or "")):
            continue
        matches = sorted(store.message_dir(data_dir, message_id).glob(f"att{idx}__*"))
        pdf_bytes = matches[0].read_bytes() if matches else b""
        out.append({"idx": idx, "filename": filename or "", "pdf_bytes": pdf_bytes,
                   "machine_text": extracted_text or "", "method": method or ""})
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


def _flag_attachment(conn, cfg, shadow: bool, message: dict, link: str,
                     att: dict, reason: str, status: str, synthetic: bool = False,
                     post=None) -> dict:
    """Shared shape for "this attachment needs a human to look at it" — posts a review
    message, logs a non-rollup event, and returns the `documents_out` entry. Used both
    by the pre-existing attachment-extraction-error path and #238's own completeness
    check (an attachment that read fine but contributed zero documents) — the only
    difference is the event `status` and whether the entry is marked `synthetic`
    (never a REAL document, so callers that count "documents" — `_summary_outcome`,
    the rollup detail, `dl_evaluate.score()` — must exclude it, per #238's own review)."""
    _post(cfg, shadow, lambda: dl_report.build_review(
        reason, from_addr=message.get("from_addr", ""),
        subject=message.get("subject", ""), link=link), post=post)
    _event(conn, shadow, message["message_id"], stage="review", status=status,
          outcome=reason, detail={"idx": att.get("idx")}, rollup=False,
          workflow=dl_report.WORKFLOW)
    out = {"outcome": "review", "reason": reason, "attachment_idx": att.get("idx")}
    if synthetic:
        out["synthetic"] = True
    return out


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
                              delivery_date=delivery_date, reason=decision.note,
                              catalog_gtins=catalog_gtins)

    header = {"customerName": supplier_decision.name,
             "customerEanEdi": supplier_decision.ean_edi}
    # #262: an informal delivery announcement (mail body text, no printed document)
    # extracts with NO docNumber at all — synthesize a STABLE identity here, keyed on
    # the message itself, BEFORE build() ever sees an empty docNumber. This is the
    # ONLY call site build() has, so its own `extraction_doc_number or
    # _generate_doc_number(...)` wall-clock fallback (R83) is never reached from the
    # live worker any more — see `desadv_edi.generate_stable_doc_number()`'s own
    # docstring for why a wall-clock value is unsafe here (a stale-claim reclaim or
    # an R17 retry would change the desadv_sent dedup key on every attempt).
    stable_doc_number = doc_number or desadv_edi.generate_stable_doc_number(
        message["message_id"])
    extraction = {"docNumber": stable_doc_number, "deliveryDate": delivery_date}
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
        # #239: an upload failure is NEVER auto-retried here. The removed code called
        # `_check_retry(...)` at this point, reasoning that "the claim was just released
        # above, so a retry can safely re-claim and re-upload without ever risking a
        # duplicate" — backwards: releasing the claim is exactly what REMOVES the
        # protection. `upload_mod.put()` writes straight to the FINAL `in_DL\<name>` with
        # no temp-write + rename, `desadv_edi.filename()` stamps a retry with a fresh
        # `HHMMSSmmm` (so it cannot collide), `release_send()` above DELETED the ledger
        # row (so `claim_send_or_identify()`, the one atomic anti-double-upload backstop,
        # has nothing left to guard and `confirm.py` never sees the orphan), and
        # `TRANSIENT_RE` matches `timed out` — the exact shape of "the bytes DID land,
        # only the reply was lost". Composed: two copies of one document in `in_DL`, both
        # taken in at the warehouse's next manual morning import. R17's retry semantics
        # for LLM/vision failures are untouched; only this upload call site is affected.
        # Re-enabling a retry needs an absence proof (by doc_number + supplier, never by
        # filename, which changes between attempts) plus a temp-write+rename upload —
        # tracked on #239. The durable alert below is kept: that half is correct, and is
        # what makes this failure visible instead of silent.
        log.exception("DL upload of %s failed", built.filename)
        note = ("Odoslanie dodacieho listu do ORIONu zlyhalo — skús znova alebo "
               "nahlás administrátorovi")
        # `e` is captured into a plain string BEFORE any closure, never referenced
        # inside one — `except ... as e` implicitly deletes `e` at the end of this
        # block, which would otherwise make a closure over it fragile (it happens to
        # run early enough today, but a later failure here would silently swallow a
        # future NameError and degrade the alert to a bare generic message — the
        # exact silent-loss failure mode fix #5 exists to prevent).
        detail_text = f"{note} ({built.filename}): {e}"
        # #239 class 2, requirement 3: NEVER a fire-and-forget best-effort post here —
        # a lost upload-failure alert is exactly the "invisible one layer up" failure
        # this ticket exists to close (Odoo is down right now — a bare `_post()` at
        # this exact moment would vanish with no trace, ever). Durably enqueued
        # instead; `dl_alerts.flush_pending` (run on the same ~15s tick as
        # confirm.sweep, see worker.run_forever) retries delivery until Odoo genuinely
        # confirms it, grouped with any other pending alert of the same kind. A
        # builder exception (dl_report.build_review) must not blow up processing
        # either — same discipline `_post`'s own docstring already established.
        # Deep-review finding on this ticket's own PR: `stuck_classified_sweep` below
        # already bails out when `delivery_notes_channel_id` resolves to 0 (unset) —
        # this call site lacked the same guard, so an unset channel would enqueue a
        # `pending_alerts` row that can NEVER be delivered (nothing posts to channel 0)
        # and would sit pending forever, growing the "stuck backlog" gauge with no way
        # to resolve it. Match the same guard here.
        channel = int(getattr(cfg, "delivery_notes_channel_id", 0) or 0)
        if channel:
            try:
                html = dl_report.build_review(
                    detail_text, supplier_decision.name, built.doc_number,
                    delivery_date, from_addr, subject, link=link)
                dl_alerts.enqueue(conn, channel, "dl_upload_failed", html,
                                  message_id=message["message_id"])
            except Exception:
                log.exception("failed to enqueue the DL upload-failure alert for %s",
                              built.filename)
        else:
            log.warning("delivery_notes_channel_id is unset — the upload-failure "
                       "alert for %s could not be enqueued", built.filename)
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
    sources = usable_attachments
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
        return {"kind": "dl", "dl_snapshot_id": snapshot_id, "status": "review",
               "documents": [{"outcome": "review", "reason": reason,
                             "correction_detected": True}], "items": []}

    extraction = dl_extract.extract_email(client, sources)

    documents_out: list[dict] = []
    all_items: list[dict] = []
    extracted_doc_numbers: list[str] = []

    for att in extraction["attachments"]:
        if att.get("error"):
            _check_retry(message.get("attempts", 0), att["error"])
            # #258 deep-review finding: the body-text pseudo-source is NOT a "príloha"
            # (attachment) — calling it one in a message a human reads is exactly the
            # category confusion this ticket exists to eliminate.
            if att.get("idx") == _BODY_TEXT_IDX:
                reason = f"Text e-mailu sa nepodarilo spracovať: {att['error']}"
            else:
                reason = (f"Príloha {att.get('filename') or att.get('idx')} sa nepodarilo "
                          f"spracovať: {att['error']}")
            documents_out.append(_flag_attachment(
                conn, cfg, shadow, message, link, att, reason, status="error", post=post))

    for doc in extraction["documents"]:
        extracted_doc_numbers.append(doc.get("docNumber") or "")
        documents_out.append(_process_document(conn, cfg, client, message, doc, catalog,
                                                suppliers, shadow, all_items,
                                                upload=upload, post=post))

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
    (the message row is gone, or no catalog snapshot is loaded).

    Deep-review finding on this ticket's own PR (#240): the "every sibling answered?"
    check below has no lock of its own — two near-simultaneous answers to SIBLING
    questions of the SAME message could each independently observe "every question
    answered" under READ COMMITTED and both dispatch a reprocess (the exact race
    `hold._release_locked`'s own docstring already documents and fixes for AI-orders,
    #118). Bounded to WASTED work, never corruption (`desadv.claim_send_or_identify` is
    still the real, atomic backstop against a double-upload) — but a doubled LLM call, a
    duplicate `order_runs` row and duplicate digest noise per collision is real, avoidable
    waste. `pg_advisory_xact_lock(hashtext(message_id))`, taken on a SEPARATE connection
    and held for the WHOLE check-through-reprocess decision (mirrors the `db.py`
    migration-lock idiom this codebase already uses, and `hold._release_locked`'s own
    "separate connection, held for the whole decision" shape — DL has no natural row to
    `FOR UPDATE` the way `held_orders` gives AI-orders, so a plain advisory lock keyed on
    the message id is the closest honest equivalent).

    Correction (review of THIS lock's own first draft, same PR): the SECOND of two racing
    answers does NOT "no-op" once it acquires the lock — the sibling questions it re-reads
    are the SAME `status='answered'` rows the first racer already saw (answering a
    question doesn't reopen it), so the second racer still runs its own full
    `_run_and_finish` pass (a second LLM call, a second `order_runs` row) — the lock only
    SERIALIZES the two attempts instead of letting them run truly concurrently; it does
    not skip the second one. What actually stops the waste from becoming a real
    duplicate DELIVERY is the same `desadv.claim_send_or_identify` claim two paragraphs
    up — the second pass's own upload attempt sees the first pass's already-confirmed
    claim and refuses to re-ship. The lock's whole value is narrower than "no-op": it
    guarantees the two attempts never touch `desadv.claim_send_or_identify` at the same
    instant (no TOCTOU on the claim check itself), and it keeps the SQL cheap
    (no genuinely parallel Postgres work on the same message) — it does not, by itself,
    prevent the wasted second LLM call and `order_runs` row this docstring already
    called out above as the acceptable cost.

    #265 gap 2: when the just-answered question is `dl_supplier`, this ALSO releases
    every OTHER same-sender `dodacie_listy` message stuck in `review` with no `order_
    questions` row of its own — see `_release_stuck_siblings`'s own docstring for the
    full evidence and why this is scoped to `dl_supplier` only.

    Deep-review finding on this ticket's own PR (#265): the sibling widening keys on
    the TIED message's own `messages.from_addr` (the raw envelope address, read below
    via `message = _as_message(msg_row)`), NOT `order_questions.payload['sender_
    email']` — `_process_document` sets that payload field to `doc.get('supplierEmail')
    or from_addr`, the DOCUMENT-extracted address, which can genuinely differ from the
    envelope address (a 3PL/warehouse operator's own contact email inside the mail
    body, for example). `_release_stuck_siblings`'s own query matches candidate
    siblings by `messages.from_addr` — using anything else as the search key here would
    silently no-op (or worse, match a DIFFERENT sender's messages) whenever the two
    addresses diverge."""
    qrow = conn.execute(
        "SELECT message_id, kind FROM order_questions WHERE id = %s", (qid,)).fetchone()
    if not qrow:
        return []
    message_id, kind = qrow[0], qrow[1]
    with psycopg.connect(cfg.pg_dsn) as lock_tx:
        lock_tx.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (message_id,))
        still_open = conn.execute(
            """SELECT 1 FROM order_questions
                WHERE message_id = %s AND kind IN ('dl_item', 'dl_supplier')
                  AND status = 'open' LIMIT 1""", (message_id,)).fetchone()
        if still_open:
            return []
        msg_row = conn.execute(
            """SELECT message_id, subject, from_addr, from_name, combined_text,
                      body_text, has_attachments FROM messages
                WHERE message_id = %s""", (message_id,)).fetchone()
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
        result = _run_and_finish(conn, cfg, client, message, snapshot_id, catalog,
                                 suppliers, upload=upload, post=post)
        if kind == "dl_supplier":
            _release_stuck_siblings(conn, message_id, message.get("from_addr", ""))
        return (result or {}).get("documents", [])


# #265 gap 2: `ask_dl_supplier`'s per-sender dedupe (`ON CONFLICT ... DO NOTHING`, the
# SAME `order_questions(customer_ean, item_key) WHERE status='open'` index every other
# kind shares) means only the FIRST `dodacie_listy` message from a still-unregistered
# sender ever gets a row of its OWN — every LATER message from that sender is left
# `processed=true`/`proc_status='review'` forever, with NO `order_questions` row
# referencing it at all (live evidence on #265: HK LOAN had 5 such messages against
# exactly 1 tied question). Answering the one open question only ever gave that FIRST
# message a second chance (`release_for_question` above) — the other 4 stayed stuck.
#
# This closes the gap WITHOUT reprocessing every sibling synchronously inside the
# request that just answered the nástenka question (unbounded cost for a sender with
# many stuck messages, and this whole function runs under the advisory lock the caller
# already holds) — it just resets them back into the SAME pool `_claim()` already
# rate-limits to one message per ~15s tick; the actual reprocessing reuses that
# existing, already-tested machinery unchanged. `_STUCK_SIBLING_LIMIT` bounds how many
# rows get flipped per call — a sender with more than that many still gets the rest on
# the NEXT answered question for it (or the existing `stuck_classified_sweep`/hourly
# n8n watchdog eventually surfaces anything that never gets picked up).
#
# Deliberately scoped to `dl_supplier` only (see the #265 design comment for the full
# rejected-alternative reasoning) — the only identity that can correlate an
# UNPROCESSED sibling message with the just-answered question, without extracting it
# first, is the raw envelope `from_addr`, and that matches `ask_dl_supplier`'s own
# dedupe key exactly (keyed on the sender ADDRESS). A `dl_item` question's dedupe key is
# (supplier_ean, wording) instead — by the time one exists the supplier is already
# matched, but there is no similarly reliable way to find OTHER not-yet-extracted
# messages sharing that exact wording without extracting them first. No live evidence
# of the `dl_item` version of this gap has been observed; it needs its own design if it
# ever is, not a copy of this one.
#
# Deep-review finding on this ticket's own PR (#265) — a REAL, proven safety bug in
# the first cut: `processed=true AND proc_status='review' AND no order_questions row`
# ALSO matches a message whose upload to ORION genuinely FAILED (a timeout, a network
# error) — `_process_document`'s upload-except branch calls `desadv.release_send()`
# (deleting the claim) and never raises a `dl_item`/`dl_supplier` question, so that
# message is indistinguishable from a genuine unmatched-supplier orphan by THAT
# predicate alone. Resetting it here would re-enable exactly the automatic upload
# retry #239 deliberately REMOVED (a released claim + a fresh per-attempt filename
# means a second attempt can upload a genuine SECOND copy of an already-landed
# document — see `.claude/rules/n8n-workflow-edits.md`'s "#239" section). Every
# upload-failure AND every other genuine processing exception in this module logs its
# own review event with `status="error"` (`_event(..., status="error", ...)` at the
# supplier-match-exception, upload-exception, and attachment-extraction-error call
# sites) — `status="review"` is reserved for the plain "nothing matched, nothing
# failed" outcomes (an unmatched supplier, an unmatched item, a correction mail). The
# `NOT EXISTS ... status = 'error'` clause below is what makes that distinction real:
# it excludes ANY message with so much as one logged failure from ever being reset by
# this widening, at the cost of leaving a message that BOTH failed AND is a genuine
# orphan stuck (safe default — `stuck_classified_sweep`/the hourly n8n watchdog still
# surface it).
_STUCK_SIBLING_LIMIT = 20


def _release_stuck_siblings(conn, exclude_message_id: str, sender_email: str) -> int:
    """Resets up to `_STUCK_SIBLING_LIMIT` orphaned same-sender `dodacie_listy`
    messages (`processed=true`, `proc_status='review'`, no `order_questions` row of
    their own, and no `status='error'` event ever logged for it — see the section
    comment above for why that last exclusion is load-bearing, not decorative) back
    into the normal claim pool. Returns how many were reset — `0` when `sender_email`
    is blank or nothing matched, never raises.

    Known, accepted residual (deep-review finding on this ticket's own PR, #265): a
    released sibling that reprocesses back to a permanent, question-less `review` (a
    #265 correction mail, or a mail with no usable source) stays eligible for a
    SECOND widening if the SAME sender's `dl_supplier` question is ever reopened and
    re-answered — bounded in practice: once taught, `dl_supplier_memory`'s
    memory-rescue rung means no NEW `dl_supplier` question is ever raised for that
    sender again unless its taught card is later retired from the catalog (rare). A
    `released_once` tracking column would close this fully but needs a schema
    migration — deferred as a documented, low-frequency, non-safety limitation rather
    than blocking this fix on it."""
    sender_email = (sender_email or "").strip()
    if not sender_email:
        return 0
    rows = conn.execute(
        """SELECT message_id FROM messages
            WHERE category = %s AND processed = true AND proc_status = 'review'
              AND message_id <> %s AND lower(from_addr) = lower(%s)
              AND NOT EXISTS (SELECT 1 FROM order_questions oq
                              WHERE oq.message_id = messages.message_id)
              AND NOT EXISTS (SELECT 1 FROM email_events e
                              WHERE e.message_id = messages.message_id
                                AND e.status = 'error')
            ORDER BY created_at ASC LIMIT %s""",
        (CATEGORY, exclude_message_id, sender_email, _STUCK_SIBLING_LIMIT)).fetchall()
    if not rows:
        return 0
    ids = [r[0] for r in rows]
    conn.execute(
        """UPDATE messages SET processed = false, processing_at = NULL, attempts = 0
            WHERE message_id = ANY(%s)""", (ids,))
    log.info("dl release_for_question: released %d orphaned same-sender sibling "
             "message(s) for %r back into the claim pool", len(ids), sender_email)
    return len(ids)


# --- #239 class 3: classified as DL but never even attempted ----------------

# Deliberately generous — `worker.run_forever`'s tick claims a message within ~15s
# under normal operation, so this many minutes with ZERO order_runs rows is already a
# strong anomaly (delivery_notes_engine misconfigured, or the worker thread died before
# its first claim), not routine backlog. Matches CLAIM_STALE_MINUTES for consistency.
STUCK_CLASSIFIED_MINUTES = 30


def stuck_classified_sweep(conn, cfg, threshold_minutes: int = STUCK_CLASSIFIED_MINUTES) -> int:
    """A message classified `dodacie_listy` that never got a first processing attempt
    AT ALL (zero `order_runs` rows) within a generous threshold — the ONE class none of
    the existing safety nets cover: the hourly n8n "Stuck message watchdog"
    (`EPe5WWMVZR0lzUld`, active, alerts channel 243 for this category) only fires once
    `attempts>=3`, which a message that was NEVER claimed (engine misconfigured, the
    worker thread died before its first claim) never reaches — `attempts` stays 0
    forever.

    Deduped via `dl_alerts.already_pending` keyed on `message_id` — deliberately NOT
    `messages.alerted_stuck` (see the design comment on #239 for why: that flag belongs
    to the n8n watchdog's own dedup, and setting it here would silently suppress that
    watchdog's own future alert if the message later starts retrying and crosses
    `attempts>=3`). Returns how many NEW messages were enqueued this pass (0 on a clean
    sweep, or when every candidate was already alerted)."""
    channel = int(getattr(cfg, "delivery_notes_channel_id", 0) or 0)
    if not channel:
        return 0
    rows = conn.execute(
        """SELECT m.message_id, m.subject, m.from_addr, m.created_at
             FROM messages m
            WHERE m.category = %s AND m.processed = false
              AND m.created_at < now() - make_interval(mins => %s)
              AND NOT EXISTS (SELECT 1 FROM order_runs r
                              WHERE r.message_id = m.message_id)
            ORDER BY m.created_at ASC LIMIT 20""",
        (CATEGORY, max(1, int(threshold_minutes)))).fetchall()
    # Deep-review finding on this ticket's own PR: `flush_pending` may deliver this
    # alert well after it was detected here (a queued Odoo outage, a busy sweep) — by
    # then the message may already have been claimed and finished normally, which
    # would make a reader think it "never started" when it since has. Stamping the
    # DETECTION time (read once, shared by every row this pass) alongside the
    # message's own received time lets a reader judge staleness for themselves;
    # `created_at` alone cannot distinguish "just detected" from "detected an hour
    # ago, still undelivered".
    detected_at = conn.execute("SELECT now()").fetchone()[0]
    n = 0
    for message_id, subject, from_addr, created_at in rows:
        if dl_alerts.already_pending(conn, "dl_stuck_classified", message_id):
            continue
        html = (
            "<p>&#9888;&#65039; E-mail bol zaradený ako dodací list, ale spracovanie "
            "sa vôbec nezačalo &mdash; over, či beží spracovanie dodacích listov.</p>"
            f"<p>Od: {escape(from_addr or '-')} / Predmet: {escape(subject or '-')} / "
            f"prijaté: {escape(str(created_at))} / "
            f"zistené: {escape(str(detected_at))}</p>")
        dl_alerts.enqueue(conn, channel, "dl_stuck_classified", html,
                          message_id=message_id)
        n += 1
    if n:
        log.warning("dl stuck-classified: %d message(s) never got a first attempt", n)
    return n


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
