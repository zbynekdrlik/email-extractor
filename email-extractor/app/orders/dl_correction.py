"""DL worker — correction/amendment mail detection (#265)."""
from __future__ import annotations

import re

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
