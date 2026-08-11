"""Config resolution — bundled-Postgres DSN fallback (add-on mode)."""

import json
import re
from pathlib import Path

from app import config as config_mod
from app.config import Config

CONFIG_YAML = Path(__file__).resolve().parents[1] / "config.yaml"


def _load_with_options(tmp_path, monkeypatch, options: dict) -> Config:
    opts = tmp_path / "options.json"
    opts.write_text(json.dumps(options))
    monkeypatch.setattr(config_mod, "OPTIONS_PATH", opts)
    monkeypatch.delenv("PG_DSN", raising=False)
    monkeypatch.delenv("PG_PASSWORD", raising=False)
    return Config.load()


def test_pg_password_without_dsn_builds_local_bundled_dsn(tmp_path, monkeypatch):
    cfg = _load_with_options(tmp_path, monkeypatch, {"pg_password": "s3cret"})
    assert cfg.pg_dsn == "postgresql://email:s3cret@127.0.0.1:5432/email"


def test_pg_password_is_url_encoded_in_bundled_dsn(tmp_path, monkeypatch):
    cfg = _load_with_options(tmp_path, monkeypatch, {"pg_password": "p@ss/w:rd"})
    assert cfg.pg_dsn == "postgresql://email:p%40ss%2Fw%3Ard@127.0.0.1:5432/email"


def test_explicit_pg_dsn_wins_over_pg_password(tmp_path, monkeypatch):
    cfg = _load_with_options(
        tmp_path,
        monkeypatch,
        {"pg_dsn": "postgresql://email:x@dbhost:5432/email", "pg_password": "ignored"},
    )
    assert cfg.pg_dsn == "postgresql://email:x@dbhost:5432/email"


def test_no_dsn_and_no_password_leaves_dsn_empty(tmp_path, monkeypatch):
    cfg = _load_with_options(tmp_path, monkeypatch, {})
    assert cfg.pg_dsn == ""


def test_public_base_url_has_no_localhost_default(monkeypatch):
    """#22: baked into file_url and fetched from ANOTHER container, so localhost
    resolved to n8n itself and AI-Vision fetches failed. Better empty than wrong."""
    from app import config
    for k in ("PUBLIC_BASE_URL", "IMAP_HOST", "IMAP_USER", "IMAP_PASS", "PG_DSN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(config, "OPTIONS_PATH", config.Path("/nonexistent/options.json"))
    assert config.Config.load().public_base_url == ""


def test_public_base_url_is_kept_when_configured(monkeypatch):
    from app import config
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://e0ac7775-email-extractor:8099")
    monkeypatch.setattr(config, "OPTIONS_PATH", config.Path("/nonexistent/options.json"))
    assert config.Config.load().public_base_url == "http://e0ac7775-email-extractor:8099"


def test_dashboard_base_url_defaults_empty(monkeypatch):
    """#139: distinct from public_base_url (the MACHINE address) — a human-facing base URL
    for links posted from the order worker, outside any HTTP request. Better empty than
    silently reusing the machine address again (that was the 0.9.10 bug)."""
    from app import config
    monkeypatch.delenv("DASHBOARD_BASE_URL", raising=False)
    monkeypatch.setattr(config, "OPTIONS_PATH", config.Path("/nonexistent/options.json"))
    assert config.Config.load().dashboard_base_url == ""


def test_dashboard_base_url_is_kept_when_configured(monkeypatch):
    from app import config
    monkeypatch.setenv("DASHBOARD_BASE_URL", "http://46.224.130.35:8099")
    monkeypatch.setattr(config, "OPTIONS_PATH", config.Path("/nonexistent/options.json"))
    assert config.Config.load().dashboard_base_url == "http://46.224.130.35:8099"


# --- #200 F1: delivery-notes (DL) engine trio — same shape as ai_orders_engine/
# static_orders_engine, defaults must keep the live n8n DL workflow completely
# untouched until a later phase deliberately flips this. ---

def test_delivery_notes_engine_defaults_to_n8n_inert(tmp_path, monkeypatch):
    cfg = _load_with_options(tmp_path, monkeypatch, {})
    assert cfg.delivery_notes_engine == "n8n"
    assert cfg.delivery_notes_shadow is False
    assert cfg.delivery_notes_shadow_days == 3


def test_delivery_notes_engine_reads_from_options(tmp_path, monkeypatch):
    cfg = _load_with_options(tmp_path, monkeypatch, {
        "delivery_notes_engine": "python",
        "delivery_notes_shadow": True,
        "delivery_notes_shadow_days": 7,
    })
    assert cfg.delivery_notes_engine == "python"
    assert cfg.delivery_notes_shadow is True
    assert cfg.delivery_notes_shadow_days == 7


_DEAD_SHEET_OPTIONS = ("catalog_sheet_id", "catalog_gid", "customer_gid",
                      "catalog_refresh_minutes", "dl_catalog_gid")


def test_dead_sheet_options_stay_declared_but_are_unread(tmp_path, monkeypatch):
    """Deep-review finding on #235's own first draft, which REMOVED these fields
    entirely: #129 already permanently disabled the Sheet reads catalog_sheet_id/
    catalog_gid/customer_gid/catalog_refresh_minutes/dl_catalog_gid used to configure —
    but the live add-on's own /data/options.json still has all five keys SET with
    real-looking values. `.claude/rules/deploy.md` records exactly this precedent from
    #129 itself: removing a still-configured option from config.yaml's schema risks the
    REAL HA Supervisor rejecting/warning on the next options validation — a risk this
    Python-only test cannot rule out either way, which is why the fields stay. So:
    still declared on `Config`, still parsed from options.json (a value present there
    is accepted, not just silently tolerated), but consumed by NOTHING downstream — see
    the field's own comment in app/config.py."""
    cfg = _load_with_options(tmp_path, monkeypatch, {
        "catalog_sheet_id": "DOC", "catalog_gid": "1", "customer_gid": "2",
        "catalog_refresh_minutes": 90, "dl_catalog_gid": "1437442607"})
    assert cfg.catalog_sheet_id == "DOC"
    assert cfg.catalog_gid == "1"
    assert cfg.customer_gid == "2"
    assert cfg.catalog_refresh_minutes == 90
    assert cfg.dl_catalog_gid == "1437442607"
    for dead in _DEAD_SHEET_OPTIONS:
        assert dead in Config.__dataclass_fields__, f"{dead} must stay declared on Config"
    assert cfg.delivery_notes_engine == "n8n"  # the rest of Config still loads fine


def test_dead_sheet_options_default_harmlessly_when_absent(tmp_path, monkeypatch):
    """The live add-on always has these set today, but a fresh/test install with none
    of the 5 keys present must still load cleanly (same `_get()` fallback every other
    option here already relies on)."""
    cfg = _load_with_options(tmp_path, monkeypatch, {})
    assert cfg.catalog_sheet_id == ""
    assert cfg.catalog_gid == ""
    assert cfg.customer_gid == ""
    assert cfg.catalog_refresh_minutes == 60
    assert cfg.dl_catalog_gid == ""


def test_config_yaml_schema_still_declares_the_dead_sheet_options():
    """A future cleanup pass must not silently re-break the #129 precedent above — pin
    config.yaml's own `options:`/`schema:` blocks textually (this project has no PyYAML
    dependency, same reason `test_version.py` regexes config.yaml instead of parsing
    it), not just the `Config` dataclass fields the test above already pins."""
    text = CONFIG_YAML.read_text()
    options_block, sep, schema_block = text.partition("\nschema:\n")
    assert sep, "config.yaml has no schema: block"
    for dead in _DEAD_SHEET_OPTIONS:
        assert re.search(rf"^\s+{dead}:", options_block, re.M), \
            f"{dead} missing from config.yaml options: block"
        assert re.search(rf"^\s+{dead}:", schema_block, re.M), \
            f"{dead} missing from config.yaml schema: block"


def test_orion_dl_dir_defaults_to_a_different_folder_than_orders(tmp_path, monkeypatch):
    """#200: DL uploads must never land in the orders folder — the two pipelines'
    uploads must be trivially distinguishable in ORION even before either engine
    is flipped to python."""
    cfg = _load_with_options(tmp_path, monkeypatch, {})
    assert cfg.orion_dl_dir == "C:\\ORION\\COMMUNICATOR\\data\\in_DL"
    assert cfg.orion_dl_dir != cfg.orion_dir


def test_orion_dl_dir_reads_from_options(tmp_path, monkeypatch):
    cfg = _load_with_options(tmp_path, monkeypatch, {"orion_dl_dir": "C:\\custom\\in_DL"})
    assert cfg.orion_dl_dir == "C:\\custom\\in_DL"
