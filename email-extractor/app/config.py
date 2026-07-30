"""Configuration: read Home Assistant add-on options (/data/options.json) or env."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus

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
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_pass: str = ""
    folders: list[str] = field(default_factory=lambda: ["INBOX"])
    poll_interval: int = 60
    pg_dsn: str = ""
    data_dir: str = "/data/store"
    http_port: int = 8099
    api_token: str = ""
    dash_password: str = ""
    secret_key: str = ""
    public_base_url: str = ""
    # --- order pipeline (#59): the catalog/customer sheet is fetched as CSV and frozen
    # into Postgres. The document id is an option, never committed. ---
    catalog_sheet_id: str = ""
    catalog_gid: str = ""
    customer_gid: str = ""
    catalog_refresh_minutes: int = 60
    # n8n owns the live pipeline until this is flipped to "python"; "shadow" runs the
    # Python pipeline for comparison only (claims nothing, uploads nothing).
    ai_orders_engine: str = "n8n"
    orders_shadow: bool = False
    orders_shadow_days: int = 3
    openai_api_key: str = ""
    orders_model: str = "gpt-5.4"
    orders_reasoning_effort: str = "high"
    llm_cache_dir: str = "/data/llm-cache"
    odoo_url: str = ""
    odoo_api_key: str = ""
    odoo_db: str = "odoo"
    orders_channel_id: int = 0
    orion_host: str = ""
    orion_port: int = 22
    orion_user: str = ""
    orion_pass: str = ""
    orion_dir: str = "C:\\ORION\\COMMUNICATOR\\data\\in"

    @classmethod
    def load(cls) -> Config:
        o = _opts()
        folders = _get(o, "folders", "FOLDERS", "INBOX")
        if isinstance(folders, str):
            folders = [f.strip() for f in folders.split(",") if f.strip()]
        http_port = int(_get(o, "http_port", "HTTP_PORT", 8099))
        pg_dsn = _get(o, "pg_dsn", "PG_DSN", "")
        pg_password = _get(o, "pg_password", "PG_PASSWORD", "")
        if not pg_dsn and pg_password:
            # Bundled-Postgres mode: run.sh starts a local cluster inside the
            # add-on container and creates role/db "email" with pg_password.
            pg_dsn = f"postgresql://email:{quote_plus(pg_password)}@127.0.0.1:5432/email"
        # No localhost fallback: this value is fetched from another container (#22).
        base = _get(o, "public_base_url", "PUBLIC_BASE_URL", "") or ""
        return cls(
            imap_host=_get(o, "imap_host", "IMAP_HOST", ""),
            imap_port=int(_get(o, "imap_port", "IMAP_PORT", 993)),
            imap_user=_get(o, "imap_user", "IMAP_USER", ""),
            imap_pass=_get(o, "imap_pass", "IMAP_PASS", ""),
            folders=folders or ["INBOX"],
            poll_interval=int(_get(o, "poll_interval", "POLL_INTERVAL", 60)),
            pg_dsn=pg_dsn,
            data_dir=_get(o, "data_dir", "DATA_DIR", "/data/store"),
            http_port=http_port,
            api_token=_get(o, "api_token", "API_TOKEN", ""),
            dash_password=_get(o, "dash_password", "DASH_PASSWORD", ""),
            secret_key=_get(o, "secret_key", "SECRET_KEY", ""),
            public_base_url=base,
            catalog_sheet_id=_get(o, "catalog_sheet_id", "CATALOG_SHEET_ID", "") or "",
            catalog_gid=str(_get(o, "catalog_gid", "CATALOG_GID", "") or ""),
            customer_gid=str(_get(o, "customer_gid", "CUSTOMER_GID", "") or ""),
            catalog_refresh_minutes=int(
                _get(o, "catalog_refresh_minutes", "CATALOG_REFRESH_MINUTES", 60)),
            ai_orders_engine=str(_get(o, "ai_orders_engine", "AI_ORDERS_ENGINE", "n8n")
                                 or "n8n"),
            orders_shadow=str(
                _get(o, "orders_shadow", "ORDERS_SHADOW", "false")).lower() in (
                    "1", "true", "yes", "on"),
            orders_shadow_days=int(
                _get(o, "orders_shadow_days", "ORDERS_SHADOW_DAYS", 3) or 3),
            openai_api_key=_get(o, "openai_api_key", "OPENAI_API_KEY", "") or "",
            orders_model=_get(o, "orders_model", "ORDERS_MODEL", "gpt-5.4") or "gpt-5.4",
            orders_reasoning_effort=_get(o, "orders_reasoning_effort",
                                         "ORDERS_REASONING_EFFORT", "high") or "high",
            llm_cache_dir=_get(o, "llm_cache_dir", "LLM_CACHE_DIR", "/data/llm-cache"),
            odoo_url=_get(o, "odoo_url", "ODOO_URL", "") or "",
            odoo_api_key=_get(o, "odoo_api_key", "ODOO_API_KEY", "") or "",
            odoo_db=_get(o, "odoo_db", "ODOO_DB", "odoo") or "odoo",
            orders_channel_id=int(_get(o, "orders_channel_id", "ORDERS_CHANNEL_ID", 0) or 0),
            orion_host=_get(o, "orion_host", "ORION_HOST", "") or "",
            orion_port=int(_get(o, "orion_port", "ORION_PORT", 22) or 22),
            orion_user=_get(o, "orion_user", "ORION_USER", "") or "",
            orion_pass=_get(o, "orion_pass", "ORION_PASS", "") or "",
            orion_dir=_get(o, "orion_dir", "ORION_DIR",
                           "C:\\ORION\\COMMUNICATOR\\data\\in"),
        )
