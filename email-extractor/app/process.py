"""Turn one raw email into a structured record: headers + body + extracted attachments."""
from __future__ import annotations

import logging
import re

from . import mailparse, smartattach
from .extract import extract_attachment

log = logging.getLogger("process")

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


def _link_note(name: str, reason: str) -> dict:
    note = f"Prílohu „{name}“ cez odkaz sa nepodarilo stiahnuť: {reason}".encode()
    a = extract_attachment(f"{name}.odkaz.txt", "text/plain", note)
    a.update(method="link", flag="link_failed")
    a["_data"] = note
    return a


def link_attachments(body: str, fetch=smartattach.fetch) -> list[dict]:
    """#506: attachments delivered as an IceWarp SmartAttach LINK, downloaded and extracted
    like a real MIME attachment. A link that cannot be downloaded — for ANY reason — becomes a
    text attachment naming the reason (flag `link_failed`): visible on the dashboard and in the
    text the engines read, never a silent loss and never a crash of the mail's ingest."""
    if not smartattach.has_marker(body):
        return []
    links = smartattach.find_links(body)
    if not links:
        log.warning("smartattach: marker present but no link line recognised")
        return [_link_note("príloha", "v maile som nenašiel rozpoznateľný odkaz na stiahnutie")]
    out = []
    for n, (name, url) in enumerate(links):
        if n >= smartattach.MAX_LINKS:
            out.append(_link_note(name, f"mail má viac ako {smartattach.MAX_LINKS} odkazov"))
            continue
        try:
            got = fetch(url, name)
        except smartattach.FetchError as e:
            log.warning("smartattach: %s not downloaded: %s", name, e)
            out.append(_link_note(name, str(e)))
            continue
        except Exception as e:   # never let one link kill the mail's ingest
            log.exception("smartattach: %s crashed the download", name)
            out.append(_link_note(name, f"neočakávaná chyba ({type(e).__name__}: {e})"))
            continue
        a = extract_attachment(got.filename or name, got.mime, got.data)
        a["_data"] = got.data
        a["_link"] = True          # came from a link, not a MIME part (not persisted)
        out.append(a)
    return out


def process_raw(raw: bytes, fetch=smartattach.fetch) -> dict:
    msg = mailparse.parse_message(raw)
    hdr = mailparse.headers(msg)
    btext, bsrc = mailparse.body_text(msg)
    identity = mailparse.message_identity(msg, raw)

    attachments = []
    for fn, mime, data in mailparse.iter_attachments(msg):
        a = extract_attachment(fn, mime, data)
        a["_data"] = data           # raw bytes for the file store (not persisted in DB JSON)
        attachments.append(a)
    attachments.extend(link_attachments(btext, fetch))

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
