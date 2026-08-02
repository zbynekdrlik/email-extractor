"""Config resolution — bundled-Postgres DSN fallback (add-on mode)."""

import json

from app import config as config_mod
from app.config import Config


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
