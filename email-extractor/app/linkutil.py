"""The warehouse's signed `/sklad/<key>` link (#93/#139): derivation shared by two callers.

`httpapi.py` mints this link inside an HTTP request (the operator's own dashboard page,
`request.host_url`). `orders/report.py` mints the SAME link from the order worker's
background thread, which has no Flask request at all — so the key derivation (and the
secret it is built from) must live in exactly ONE place, or the two could silently drift
apart and hand out two different, both-wrong keys.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path


def persistent_secret(data_dir: Path) -> bytes:
    """Stable secret when `secret_key` is unset: persisted on the data volume so it
    survives restarts, and is identical for the HTTP server and the worker thread —
    both run in the same process, sharing the same `data_dir` (see `main.py`)."""
    f = data_dir / ".session_secret"
    try:
        if f.exists():
            return f.read_bytes()
        data_dir.mkdir(parents=True, exist_ok=True)
        s = os.urandom(32)
        f.write_bytes(s)
        return s
    except OSError:
        return os.urandom(32)   # read-only fs fallback: ephemeral key


def resolve_secret(cfg) -> bytes:
    """The same secret `httpapi.create_app` uses for `app.secret_key`."""
    secret = getattr(cfg, "secret_key", "") or ""
    if secret:
        return secret.encode() if isinstance(secret, str) else secret
    return persistent_secret(Path(getattr(cfg, "data_dir", "") or "/data/store"))


def sklad_key(secret) -> str:
    """The warehouse link's key: derived from the install's session secret, so it is
    stable across restarts (the link can be bookmarked) and different on every install."""
    if isinstance(secret, str):
        secret = secret.encode()
    return hmac.new(secret, b"sklad-link-v1", hashlib.sha256).hexdigest()[:32]


def sklad_url(cfg) -> str:
    """The full, human-clickable `/sklad/<key>` link — or `""` when no human-facing base
    URL is configured (`dashboard_base_url`).

    Deliberately NOT `cfg.public_base_url`: that one is the MACHINE address n8n uses to
    fetch `/files` over the docker network and is unopenable in a browser (the exact bug
    fixed in 0.9.10 — never repeat it here). A message posted from the order worker has no
    Flask `request` to read the operator's own address from, so it needs its own,
    explicitly human-facing option.
    """
    base = (getattr(cfg, "dashboard_base_url", "") or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/sklad/{sklad_key(resolve_secret(cfg))}"
