"""Configuration: read Home Assistant add-on options (/data/options.json) or env."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

OPTIONS_PATH = Path(os.environ.get("ADDON_OPTIONS", "/data/options.json"))


def _opts() -> dict:
    if OPTIONS_PATH.exists():
        try:
            return json.loads(OPTIONS_PATH.read_text())
        except Exception:
            return {}
    return {}


def _get(opts: dict, key: str, env: str, default=None):
    if key in opts and opts[key] not in (None, ""):
        return opts[key]
    return os.environ.get(env, default)


@dataclass
class Config:
    imap_host: str = "imap.m1.websupport.sk"
    imap_port: int = 993
    imap_user: str = ""
    imap_pass: str = ""
    folders: list[str] = field(default_factory=lambda: ["INBOX"])
    poll_interval: int = 60
    pg_dsn: str = ""
    data_dir: str = "/data/store"
    http_port: int = 8099
    api_token: str = ""

    @classmethod
    def load(cls) -> Config:
        o = _opts()
        folders = _get(o, "folders", "FOLDERS", "INBOX")
        if isinstance(folders, str):
            folders = [f.strip() for f in folders.split(",") if f.strip()]
        return cls(
            imap_host=_get(o, "imap_host", "IMAP_HOST", "imap.m1.websupport.sk"),
            imap_port=int(_get(o, "imap_port", "IMAP_PORT", 993)),
            imap_user=_get(o, "imap_user", "IMAP_USER", ""),
            imap_pass=_get(o, "imap_pass", "IMAP_PASS", ""),
            folders=folders or ["INBOX"],
            poll_interval=int(_get(o, "poll_interval", "POLL_INTERVAL", 60)),
            pg_dsn=_get(o, "pg_dsn", "PG_DSN", ""),
            data_dir=_get(o, "data_dir", "DATA_DIR", "/data/store"),
            http_port=int(_get(o, "http_port", "HTTP_PORT", 8099)),
            api_token=_get(o, "api_token", "API_TOKEN", ""),
        )
