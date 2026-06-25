"""Turn one raw email into a structured record: headers + body + extracted attachments."""
from __future__ import annotations

from . import mailparse
from .extract import extract_attachment


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
    return "\n\n".join(parts)
