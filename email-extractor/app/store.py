"""Persist the raw .eml + attachment files on the add-on volume; build fetch URLs."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path


def safe_id(message_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", message_id or "")[:120] or "noid"


def _safe_name(name: str, idx: int) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name or "")[:80] or f"att{idx}"


def save_message(data_dir: str, identity: str, raw: bytes,
                 attachments: list[dict], base_url: str, token: str) -> tuple[str, list[dict]]:
    """Write raw.eml + each attachment under <data_dir>/<safe_id>/; return (raw_path, file_infos)."""
    mid = safe_id(identity)
    d = Path(data_dir) / mid
    d.mkdir(parents=True, exist_ok=True)
    raw_path = d / "raw.eml"
    raw_path.write_bytes(raw)
    q = f"?token={token}" if token else ""
    files = []
    for i, a in enumerate(attachments):
        data = a.get("_data") or b""
        path = d / f"att{i}__{_safe_name(a.get('filename', ''), i)}"
        path.write_bytes(data)
        files.append({
            "idx": i,
            "sha256": hashlib.sha256(data).hexdigest(),
            "path": str(path),
            "url": f"{base_url}/files/{mid}/{i}{q}",
        })
    return str(raw_path), files
