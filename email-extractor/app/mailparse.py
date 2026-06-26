"""Parse a raw RFC822 message into headers, body text, and attachments."""
from __future__ import annotations

import hashlib
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr


def parse_message(raw: bytes):
    return BytesParser(policy=policy.default).parsebytes(raw)


def _addr_list(value) -> list[str]:
    if not value:
        return []
    return [a for _, a in getaddresses([str(value)]) if a]


def body_text(msg) -> tuple[str, str]:
    """(text, source) preferring text/plain, else stripped text/html."""
    plain, htmltext = "", ""
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.get_content_maintype() == "multipart":
            continue
        cd = (part.get("Content-Disposition") or "").lower()
        if "attachment" in cd:
            continue
        ct = part.get_content_type()
        try:
            payload = part.get_content()
        except Exception:
            continue
        if not isinstance(payload, str):
            continue
        if ct == "text/plain" and not plain:
            plain = payload
        elif ct == "text/html" and not htmltext:
            htmltext = payload
    if plain.strip():
        return plain.strip(), "text/plain"
    if htmltext.strip():
        from bs4 import BeautifulSoup
        return BeautifulSoup(htmltext, "lxml").get_text(" ", strip=True), "text/html"
    return "", "none"


def iter_attachments(msg):
    """Yield (filename, mime, data) for real (non-inline) attachments."""
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        cd = (part.get("Content-Disposition") or "").lower()
        fn = part.get_filename()
        if "attachment" not in cd and not fn:
            continue
        if "inline" in cd and not fn:
            continue
        try:
            data = part.get_payload(decode=True) or b""
        except Exception:
            data = b""
        if data:
            yield fn or "", part.get_content_type(), data


def message_identity(msg, raw: bytes) -> str:
    """Stable dedup id: Message-ID if present, else sha256 of the raw bytes."""
    mid = msg.get("Message-ID")
    if mid:
        return str(mid).strip()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def content_signature(from_addr: str, subject: str, combined_text: str) -> str:
    """Diagnostic content fingerprint (sender+subject+body), stored on each message.

    NOTE: this is NOT used to dedup — ingest dedups ONLY by Message-ID
    (ON CONFLICT in db.insert_message). Content-based dedup was deliberately
    rejected: distinct real orders often share sender+subject+body but carry
    different attachments, so merging them would drop a genuine order. The hash is
    kept purely for diagnostics (spotting suspicious near-duplicates by eye).
    """
    src = "\n".join([
        (from_addr or "").strip().lower(),
        (subject or "").strip(),
        (combined_text or "").strip(),
    ])
    return hashlib.sha256(src.encode("utf-8", "replace")).hexdigest()


def headers(msg) -> dict:
    from_name, from_addr = parseaddr(str(msg.get("From", "")))
    return {
        "message_id": str(msg.get("Message-ID", "")).strip(),
        "from_addr": from_addr,
        "from_name": from_name,
        "to_addrs": _addr_list(msg.get("To")),
        "cc_addrs": _addr_list(msg.get("Cc")),
        "subject": str(msg.get("Subject", "")),
        "date": str(msg.get("Date", "")),
    }
