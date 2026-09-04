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
    # #129/#235: catalog_sheet_id/catalog_gid/customer_gid/catalog_refresh_minutes (the
    # AI-orders sheet) and dl_catalog_gid (the DL sheet, below) are UNREAD — #129
    # (2026-08-07) permanently stopped reading either Sheet; Postgres overrides are now
    # the sole source of truth for both catalogs (snapshot.py/dl_snapshot.py). #235's
    # own first draft REMOVED these fields entirely — deep review reverted that: per
    # .claude/rules/deploy.md's own recorded #129 incident, the live add-on's
    # /data/options.json still has these 5 keys set with real-looking values, and
    # dropping them from the store-published schema risks the Supervisor rejecting/
    # warning on the next options validation — a risk nothing in THIS repo (a
    # Python-only test) can prove is safe, since it can't simulate the real HA
    # Supervisor's own schema validation. Kept declared + parsed here (matching
    # config.yaml's schema, see test_config.py's own parity test), just never consumed
    # by any downstream code — that's what "unread" means; do not "clean this up" again
    # without first verifying live against the real Supervisor.
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
    # #376: the AI-not-order safe-discard rollout switch. Default FALSE = DRY-RUN — the whole
    # classifier gate still runs on every no-order mail (so its would-be verdict is recorded
    # in the review event's outcome, "AI by zahodilo (...)"), but the mail STILL goes to the
    # warehouse question. Flip to true (a deliberate operator decision, after ~a week of
    # comparing the dry-run verdicts against the sklad's real answers) to actually discard.
    ai_not_order_discard: bool = False
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
    # #310: the OPERATOR/admin Odoo channel — engine-liveness/staleness alerts, the
    # monthly spend-cap tripwire and any other diagnostic hlásenie route HERE, never to
    # the warehouse (243) / sales (152) channels a real person reads and cannot act on.
    # Default UNSET (0): no such channel exists in prod today, so when unset an operator
    # alert stays in the app log + durable `pending_alerts` (dashboard gauge) — never
    # lost, never on 243/152. Set it to an admin channel id to also deliver in Odoo.
    ops_channel_id: int = 0
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
    # #339: safety age cutoff for the DL engine. A `dodacie_listy` message older than this
    # many days that becomes claimable again (a fresh claim, a stuck-sibling release, an
    # answered-question reprocess) routes to MANUAL REVIEW instead of auto-uploading a
    # months-old delivery note to ORION — the goods almost certainly already arrived and
    # were handled by hand, so an automatic upload is a real duplicate-delivery risk
    # (#338). A ROLLING window (not a fixed date like human_processing.BACKLOG_CUTOFF): the
    # risk is the AGE of the document, so it catches a stuck DL whenever it re-enters. 0
    # disables the guard. Loaded WITHOUT the `or N` idiom so an explicit 0 truly disables
    # it (unlike delivery_notes_shadow_days, where 0 is meaningless — see #229 on the trap).
    delivery_notes_max_age_days: int = 14
    # #129/#235: the DL-sheet counterpart of catalog_sheet_id above — same "unread,
    # never removed" precedent, see that field's own comment.
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
    # #255: the evening/same-day check (confirm.py's evening_check_active) — mirrors
    # import_morning_check_hour/_skip_saturday/_skip_sunday above, same shape, own knobs.
    # Default hour 18:00 is well past a normal working day so a slow-completing import
    # pass is never mistaken for a genuine same-day CODEX rejection.
    import_evening_check_hour: int = 18
    import_evening_check_skip_saturday: bool = True
    import_evening_check_skip_sunday: bool = True
    # #133 "DOPLNENIE ROZHODNUTIA": grouped Odoo digest for cleanly-uploaded static
    # orders (see static_digest.py) — batch-size and idle-timeout triggers, tunable
    # without a code change.
    static_digest_batch_size: int = 30
    static_digest_idle_minutes: int = 60
    # #237: stale-question reminder sweep (app/orders/question_alerts.py). "Working
    # days" = distinct Mon-Fri calendar dates the question has been open across,
    # inclusive of both its creation date and today. ONE reminder at
    # question_stale_working_days, then silent until the question is answered or
    # auto-expired (#341) — see that module's own docstring + the #237 design comment
    # for the full reasoning.
    question_stale_working_days: int = 2
    # #349 removed the second, escalation-level reminder (dead code under the default
    # config — expiry closes a question at touched=3 before it can reach the escalation
    # threshold of touched=4, so the 🚨 branch never rendered a single message). This
    # option is now DEAD-BUT-DECLARED, consumed by NOTHING — kept for exactly the same
    # #129 reason as the dead sheet options above: the live add-on's /data/options.json
    # still has it SET, so dropping it from config.yaml's schema would risk the HA
    # Supervisor rejecting the next options validation (see deploy.md + test_config.py's
    # question_escalate pin).
    question_escalate_working_days: int = 4
    # #341: a board question open across MORE than this many WORKING days is auto-expired
    # (neutral terminal state, teaches nothing) — the warehouse handles the underlying
    # mail manually daily, so a stale question is bezpredmetná. Strictly greater than the
    # threshold, so a question always gets its full N working days before expiring.
    question_expire_working_days: int = 2
    # #381: optional retention for /data/store mail originals — delete files older than this
    # many days on a daily sweep (app/store_retention.py, driven from main.py's IMAP loop).
    # 0 (default) = DISABLED, nothing is ever deleted; the DB keeps the extracted text so a
    # reprocess still works without the originals (#251). Loaded WITHOUT the `or N` idiom so
    # an explicit 0 truly disables it (the #229 falsy-override trap — same as
    # delivery_notes_max_age_days above).
    store_retention_days: int = 0

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
            # #129/#235: parsed (matching config.yaml's still-declared schema) but
            # deliberately UNREAD by any downstream code — see the field comment above.
            catalog_sheet_id=_get(o, "catalog_sheet_id", "CATALOG_SHEET_ID", "") or "",
            catalog_gid=str(_get(o, "catalog_gid", "CATALOG_GID", "") or ""),
            customer_gid=str(_get(o, "customer_gid", "CUSTOMER_GID", "") or ""),
            catalog_refresh_minutes=int(
                _get(o, "catalog_refresh_minutes", "CATALOG_REFRESH_MINUTES", 60) or 60),
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
            ai_not_order_discard=str(
                _get(o, "ai_not_order_discard", "AI_NOT_ORDER_DISCARD", "false")).lower() in (
                    "1", "true", "yes", "on"),
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
            # #310: operator/admin channel — 0 (unset) is a REAL value here, not a
            # "fall back to a warehouse channel" sentinel, so NO trailing `or N`.
            ops_channel_id=int(_get(o, "ops_channel_id", "OPS_CHANNEL_ID", 0) or 0),
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
            # #339: NO trailing `or 14` — an explicit 0 in options.json must DISABLE the
            # guard, and `0 or 14` would silently re-enable it (the #229 falsy-override trap).
            delivery_notes_max_age_days=int(
                _get(o, "delivery_notes_max_age_days", "DELIVERY_NOTES_MAX_AGE_DAYS", 14)),
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
            import_evening_check_hour=int(
                _get(o, "import_evening_check_hour", "IMPORT_EVENING_CHECK_HOUR", 18) or 18),
            import_evening_check_skip_saturday=str(
                _get(o, "import_evening_check_skip_saturday",
                     "IMPORT_EVENING_CHECK_SKIP_SATURDAY", "true")).lower() in (
                    "1", "true", "yes", "on"),
            import_evening_check_skip_sunday=str(
                _get(o, "import_evening_check_skip_sunday",
                     "IMPORT_EVENING_CHECK_SKIP_SUNDAY", "true")).lower() in (
                    "1", "true", "yes", "on"),
            static_digest_batch_size=int(
                _get(o, "static_digest_batch_size", "STATIC_DIGEST_BATCH_SIZE", 30) or 30),
            static_digest_idle_minutes=int(
                _get(o, "static_digest_idle_minutes", "STATIC_DIGEST_IDLE_MINUTES", 60) or 60),
            question_stale_working_days=int(
                _get(o, "question_stale_working_days", "QUESTION_STALE_WORKING_DAYS", 2)
                or 2),
            # #349: dead-but-declared (see the field's own comment above) — still parsed
            # so a value present in options.json is accepted, never consumed downstream.
            question_escalate_working_days=int(
                _get(o, "question_escalate_working_days",
                     "QUESTION_ESCALATE_WORKING_DAYS", 4) or 4),
            question_expire_working_days=int(
                _get(o, "question_expire_working_days",
                     "QUESTION_EXPIRE_WORKING_DAYS", 2) or 2),
            # #381: NO trailing `or N` — an explicit 0 in options.json must DISABLE the
            # purge, and `0 or 90` would silently re-enable it (the #229 trap; same shape
            # as delivery_notes_max_age_days).
            store_retention_days=int(
                _get(o, "store_retention_days", "STORE_RETENTION_DAYS", 0)),
        )
