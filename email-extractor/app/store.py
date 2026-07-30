"""Persist the raw .eml + attachment files on the add-on volume; build fetch URLs."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path


def _sanitize(message_id: str, limit: int) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", message_id or "")[:limit] or "noid"


def legacy_safe_id(message_id: str) -> str:
    """The pre-0.4.0 dir name (plain 120-char cut). Read-only: live data uses it."""
    return _sanitize(message_id, 120)


def safe_id(message_id: str) -> str:
    """Directory name for one email — unambiguous per Message-ID (#21).

    A truncated prefix alone collided: two long auto-generated IDs sharing their
    first 120 chars landed in one directory and overwrote each other's originals.
    The appended hash is taken from the FULL id, so distinct ids always differ.
    """
    digest = hashlib.sha256((message_id or "").encode("utf-8", "surrogatepass")).hexdigest()
    return f"{_sanitize(message_id, 100)}-{digest[:12]}"


def message_dir(data_dir: str, key: str) -> Path:
    """Resolve one email's storage dir from a URL segment or a raw Message-ID.

    Accepts all three forms that exist in the wild, newest first:
      * a raw Message-ID (what n8n passes: /files/<message_id>/<idx>),
      * a current dir name (`<prefix>-<hash>`, what save_message puts in file_url),
      * a legacy 120-char dir name (emails ingested before 0.4.0).
    Falls back to the current name for this key when nothing exists yet, so a
    missing file 404s instead of leaking another email's directory.
    """
    root = Path(data_dir)
    current = root / safe_id(key)
    for cand in (current, root / legacy_safe_id(key), root / _sanitize(key, 200)):
        if _within(root, cand) and cand.exists():
            return cand
    return current


def _within(root: Path, path: Path) -> bool:
    """Guard against a crafted ../ segment escaping the store."""
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _safe_name(name: str, idx: int) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name or "")[:80] or f"att{idx}"


def save_message(data_dir: str, identity: str, raw: bytes,
                 attachments: list[dict], base_url: str) -> tuple[str, list[dict]]:
    """Write raw.eml + each attachment under <data_dir>/<safe_id>/; return (raw_path, file_infos)."""
    mid = safe_id(identity)
    d = Path(data_dir) / mid
    d.mkdir(parents=True, exist_ok=True)
    raw_path = d / "raw.eml"
    raw_path.write_bytes(raw)
    files = []
    for i, a in enumerate(attachments):
        data = a.get("_data") or b""
        path = d / f"att{i}__{_safe_name(a.get('filename', ''), i)}"
        path.write_bytes(data)
        files.append({
            "idx": i,
            "sha256": hashlib.sha256(data).hexdigest(),
            "path": str(path),
            # No token in the URL: it is persisted in Postgres, so a dump would leak
            # the secret and rotating it would break every stored URL (#22). The
            # fetcher authenticates with the X-Token header instead.
            "url": f"{base_url}/files/{mid}/{i}",
        })
    return str(raw_path), files
