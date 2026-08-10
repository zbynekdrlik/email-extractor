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
    # The human-facing base URL for links posted OUTSIDE an HTTP request (#139) — the
    # order worker's Odoo summary needs a clickable /sklad/<key> link, but it runs on a
    # background thread with no Flask `request.host_url` to read the operator's address
    # from. Deliberately separate from public_base_url, which is the MACHINE address n8n
    # uses over the docker network (see `linkutil.sklad_url`'s docstring — 0.9.10 fixed
    # exactly this confusion once already, never repeat it).
    dashboard_base_url: str = ""
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
    # Static orders (#132 shadow-mode groundwork, #133 the real cutover): same naming
    # pattern as the ai_orders_engine/orders_shadow trio above. "python" claims the
    # message for real (static_worker.tick) — flipping this to "python" in production is
    # a deliberate, separate operator decision (default stays "n8n" until the user acts).
    static_orders_engine: str = "n8n"
    static_orders_shadow: bool = False
    static_orders_shadow_days: int = 3
    # The monthly tripwire (#89): warn once past this, never stop shipping orders.
    orders_spend_cap_eur: float = 30.0
    openai_api_key: str = ""
    orders_model: str = "gpt-5.4"
    orders_reasoning_effort: str = "high"
    llm_cache_dir: str = "/data/llm-cache"
    odoo_url: str = ""
    odoo_api_key: str = ""
    odoo_db: str = "odoo"
    orders_channel_id: int = 0
    # #151: the delivery-note (DESADV_*) counterpart of orders_channel_id — see
    # orders/confirm.py's _channel_for. Falls back to orders_channel_id when unset.
    # #229: defaults to 243 ("AI dodacie listy", the warehouse) — a genuinely UNSET (0)
    # value used to silently fall back to orders_channel_id (152, "objednávky", the sales
    # desk), routing every DL review/success/announced-mismatch message to the wrong
    # audience. Still fully overridable per-install; this only fixes the DEFAULT.
    delivery_notes_channel_id: int = 243
    orion_host: str = ""
    orion_port: int = 22
    orion_user: str = ""
    orion_pass: str = ""
    orion_dir: str = "C:\\ORION\\COMMUNICATOR\\data\\in"
    # #200 F1: the delivery-notes (dodacie listy) engine trio — SAME naming/default
    # pattern as ai_orders_engine/orders_shadow and static_orders_engine/
    # static_orders_shadow above. "n8n" (default) is completely inert for THIS engine;
    # the live n8n "Dodacie Listy EDI" workflow keeps running unchanged until a later
    # phase deliberately flips this. "shadow" runs the Python pipeline for comparison
    # only once it exists (#200 itself only lands schema/config — no pipeline yet).
    delivery_notes_engine: str = "n8n"
    delivery_notes_shadow: bool = False
    delivery_notes_shadow_days: int = 3
    # #200: the "produkty dodacie listy" tab (gid 1437442607 in the live sheet) — the
    # DL catalog is the UNION of this tab with the existing catalog_gid tab ("produkty
    # objednavky"), never a replacement of it. See app/orders/dl_snapshot.py.
    dl_catalog_gid: str = ""
    # #200: DL uploads land in a DIFFERENT ORION folder than orders (in_DL, not in) —
    # same "not exposed as an add-on option" precedent as orion_dir above (an internal
    # convention path, not something an operator tunes).
    orion_dl_dir: str = "C:\\ORION\\COMMUNICATOR\\data\\in_DL"
    # #151: import-confirmation sweep (orders/confirm.py). 5 minutes keeps the SFTP
    # `listdir` load on the ORION box low while a file is still legitimately waiting.
    # (2026-08-05 #133 correction: the old `import_confirm_timeout_minutes` — a
    # ~60-minute "Communicator will pick it up automatically" alert — is REMOVED
    # entirely; import is a MANUAL morning click, not an automatic sweep. See
    # `import_morning_check_hour`/`import_morning_check_skip_saturday`/
    # `import_morning_check_skip_sunday`/`import_alert_reminder_hours` below.)
    import_confirm_interval_minutes: int = 5
    # #133 (2026-08-05 correction): once past this LOCAL (Europe/Bratislava) hour, a file
    # still sitting in ORION's `in/` from a PRIOR day is a genuine carryover worth
    # alerting on (grouped, deduped per incident) — the warehouse's own daily "prijať
    # objednávky z ORIONu" click hasn't happened yet that day. Skips Saturday/Sunday by
    # default (the warehouse doesn't work weekends) — a Friday-evening/weekend upload is
    # first checked the following Monday.
    import_morning_check_hour: int = 10
    import_morning_check_skip_saturday: bool = True
    import_morning_check_skip_sunday: bool = True
    # #133: while an import-alert incident (carryover/failed/unknown) stays open, at most
    # one reminder is sent after this many hours — never a repeat per file.
    import_alert_reminder_hours: int = 4
    # #133 "DOPLNENIE ROZHODNUTIA": grouped Odoo digest for cleanly-uploaded static
    # orders (see static_digest.py) — batch-size and idle-timeout triggers, tunable
    # without a code change.
    static_digest_batch_size: int = 30
    static_digest_idle_minutes: int = 60

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
        dashboard_base = _get(o, "dashboard_base_url", "DASHBOARD_BASE_URL", "") or ""
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
            dashboard_base_url=dashboard_base,
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
            static_orders_engine=str(
                _get(o, "static_orders_engine", "STATIC_ORDERS_ENGINE", "n8n") or "n8n"),
            static_orders_shadow=str(
                _get(o, "static_orders_shadow", "STATIC_ORDERS_SHADOW", "false")).lower() in (
                    "1", "true", "yes", "on"),
            static_orders_shadow_days=int(
                _get(o, "static_orders_shadow_days", "STATIC_ORDERS_SHADOW_DAYS", 3) or 3),
            orders_spend_cap_eur=float(
                _get(o, "orders_spend_cap_eur", "ORDERS_SPEND_CAP_EUR", 30) or 0),
            openai_api_key=_get(o, "openai_api_key", "OPENAI_API_KEY", "") or "",
            orders_model=_get(o, "orders_model", "ORDERS_MODEL", "gpt-5.4") or "gpt-5.4",
            orders_reasoning_effort=_get(o, "orders_reasoning_effort",
                                         "ORDERS_REASONING_EFFORT", "high") or "high",
            llm_cache_dir=_get(o, "llm_cache_dir", "LLM_CACHE_DIR", "/data/llm-cache"),
            odoo_url=_get(o, "odoo_url", "ODOO_URL", "") or "",
            odoo_api_key=_get(o, "odoo_api_key", "ODOO_API_KEY", "") or "",
            odoo_db=_get(o, "odoo_db", "ODOO_DB", "odoo") or "odoo",
            orders_channel_id=int(_get(o, "orders_channel_id", "ORDERS_CHANNEL_ID", 0) or 0),
            # #229: the trailing `or 243` (same idiom every other int option here uses)
            # means an options.json that still carries the OLD explicit `0` (not just a
            # genuinely absent key) also resolves to 243 -- deliberate, since 0 was never
            # a real Odoo channel id, only ever the "unset, fall back" sentinel this whole
            # ticket exists to stop relying on.
            delivery_notes_channel_id=int(
                _get(o, "delivery_notes_channel_id", "DELIVERY_NOTES_CHANNEL_ID", 243)
                or 243),
            orion_host=_get(o, "orion_host", "ORION_HOST", "") or "",
            orion_port=int(_get(o, "orion_port", "ORION_PORT", 22) or 22),
            orion_user=_get(o, "orion_user", "ORION_USER", "") or "",
            orion_pass=_get(o, "orion_pass", "ORION_PASS", "") or "",
            orion_dir=_get(o, "orion_dir", "ORION_DIR",
                           "C:\\ORION\\COMMUNICATOR\\data\\in"),
            delivery_notes_engine=str(
                _get(o, "delivery_notes_engine", "DELIVERY_NOTES_ENGINE", "n8n") or "n8n"),
            delivery_notes_shadow=str(
                _get(o, "delivery_notes_shadow", "DELIVERY_NOTES_SHADOW", "false")).lower() in (
                    "1", "true", "yes", "on"),
            delivery_notes_shadow_days=int(
                _get(o, "delivery_notes_shadow_days", "DELIVERY_NOTES_SHADOW_DAYS", 3) or 3),
            dl_catalog_gid=str(_get(o, "dl_catalog_gid", "DL_CATALOG_GID", "") or ""),
            orion_dl_dir=_get(o, "orion_dl_dir", "ORION_DL_DIR",
                              "C:\\ORION\\COMMUNICATOR\\data\\in_DL"),
            import_confirm_interval_minutes=int(
                _get(o, "import_confirm_interval_minutes",
                     "IMPORT_CONFIRM_INTERVAL_MINUTES", 5) or 5),
            import_morning_check_hour=int(
                _get(o, "import_morning_check_hour", "IMPORT_MORNING_CHECK_HOUR", 10) or 10),
            import_morning_check_skip_saturday=str(
                _get(o, "import_morning_check_skip_saturday",
                     "IMPORT_MORNING_CHECK_SKIP_SATURDAY", "true")).lower() in (
                    "1", "true", "yes", "on"),
            import_morning_check_skip_sunday=str(
                _get(o, "import_morning_check_skip_sunday",
                     "IMPORT_MORNING_CHECK_SKIP_SUNDAY", "true")).lower() in (
                    "1", "true", "yes", "on"),
            import_alert_reminder_hours=int(
                _get(o, "import_alert_reminder_hours", "IMPORT_ALERT_REMINDER_HOURS", 4)
                or 4),
            static_digest_batch_size=int(
                _get(o, "static_digest_batch_size", "STATIC_DIGEST_BATCH_SIZE", 30) or 30),
            static_digest_idle_minutes=int(
                _get(o, "static_digest_idle_minutes", "STATIC_DIGEST_IDLE_MINUTES", 60) or 60),
        )
