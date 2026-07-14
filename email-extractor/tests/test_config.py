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
