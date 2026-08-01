"""Turn one raw email into a structured record: headers + body + extracted attachments."""
from __future__ import annotations

import re

from . import mailparse
from .extract import extract_attachment

# Invisible Unicode format chars that a mail client can insert between two tokens (observed:
# a zero-width space glued into "45​ks" — AGEL Levoča incident, 2026-07-09, #41). Plain
# `\s+` regexes downstream (ours and n8n's) do NOT match these, so a line silently fails to
# parse. Mirrors app/orders/extract.py's own ZERO_WIDTH table (kept local, not imported: this
# module is core ingest and orders/ is a feature subpackage built on top of it — core must not
# depend on a feature package). Mapped to a space, not dropped, so "45​ks" becomes
# "45 ks" instead of "45ks".
_INVISIBLE_CODEPOINTS = [
    0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0x2061, 0x2062, 0x2063, 0x2064,
    0xFEFF, 0x00AD, 0x180E,
]
_INVISIBLE_RE = re.compile("[" + "".join(chr(c) for c in _INVISIBLE_CODEPOINTS) + "]")


def _strip_invisible(text: str) -> str:
    return _INVISIBLE_RE.sub(" ", text)


def process_raw(raw: bytes) -> dict:
    msg = mailparse.parse_message(raw)
    hdr = mailparse.headers(msg)
    btext, bsrc = mailparse.body_text(msg)
    identity = mailparse.message_identity(msg, raw)

    attachments = []
    for fn, mime, data in mailparse.iter_attachments(msg):
        a = extract_attachment(fn, mime, data)
        a["_data"] = data           # raw bytes for the file store (not persisted in DB JSON)
        attachments.append(a)

    combined = _combined_text(hdr, btext, attachments)
    return {
        "identity": identity,
        "headers": hdr,
        "body_text": btext,
        "body_source": bsrc,
        "attachments": attachments,
        "combined_text": combined,
        "needs_vision": any(a["needs_vision"] for a in attachments),
        "has_attachments": len(attachments) > 0,
    }


def _combined_text(hdr: dict, body: str, attachments: list[dict]) -> str:
    parts = [
        f"Subject: {hdr.get('subject', '')}",
        f"From: {hdr.get('from_addr', '')}",
        f"Body: {body}",
    ]
    doc_texts = []
    for a in attachments:
        # skip junk and vision-placeholder noise; keep real extracted text
        if a["flag"].startswith("skipped") or a["needs_vision"]:
            continue
        t = (a.get("text") or "").strip()
        if t:
            doc_texts.append(f"===== {a['filename']} =====\n{t}")
    if doc_texts:
        parts.append("Attachments:\n" + "\n\n".join(doc_texts))
    return _strip_invisible("\n\n".join(parts))
