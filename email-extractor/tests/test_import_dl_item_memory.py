"""scripts/import_dl_item_memory.py (#200 F1) — verifies the one-shot n8n import
script's actual SQL against a local/test Postgres, per the ticket's instruction to
verify the script before it is ever run against live (the real run happens later,
at DL cutover — this test never touches production).
"""
import json
import os

from app import config as config_mod
from scripts import import_dl_item_memory as script


def _run(pg, monkeypatch, tmp_path, rows):
    dsn = os.environ.get("PG_TEST_DSN")
    monkeypatch.setenv("PG_DSN", dsn)
    monkeypatch.setattr(config_mod, "OPTIONS_PATH",
                        config_mod.Path("/nonexistent/options.json"))
    path = tmp_path / "export.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return script.main([str(path)])


def test_imports_rows_and_prints_the_stored_count(pg, monkeypatch, tmp_path, capsys):
    rows = [
        {"cust": "999", "item": "repka olej", "gtin": "G9", "card": "Olej repkový 10l",
         "at": "2026-04-01T09:00:00Z", "src": "n8n", "cnt": 3},
    ]
    rc = _run(pg, monkeypatch, tmp_path, rows)
    assert rc == 0
    out = capsys.readouterr().out
    assert "imported 1 new row(s)" in out
    row = pg.execute(
        "SELECT gtin, cnt FROM dl_item_memory WHERE supplier_ean = '999'").fetchone()
    assert row == ("G9", 3)


def test_a_second_run_of_the_same_export_is_a_noop(pg, monkeypatch, tmp_path, capsys):
    rows = [
        {"cust": "998", "item": "vanilka", "gtin": "G10", "card": "Vanilkový cukor",
         "at": "2026-04-02T09:00:00Z", "src": "n8n", "cnt": 1},
    ]
    _run(pg, monkeypatch, tmp_path, rows)
    rc = _run(pg, monkeypatch, tmp_path, rows)
    assert rc == 0
    out = capsys.readouterr().out
    assert "imported 0 new row(s)" in out
    total = pg.execute(
        "SELECT count(*) FROM dl_item_memory WHERE supplier_ean = '998'").fetchone()[0]
    assert total == 1


def test_missing_argument_prints_usage_and_exits_nonzero(capsys):
    rc = script.main([])
    assert rc == 2
    assert "dodacie_pamat_poloziek" in capsys.readouterr().out


def test_refuses_to_run_with_no_postgres_dsn_configured(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("PG_DSN", raising=False)
    monkeypatch.delenv("PG_PASSWORD", raising=False)
    monkeypatch.setattr(config_mod, "OPTIONS_PATH",
                        config_mod.Path("/nonexistent/options.json"))
    path = tmp_path / "export.json"
    path.write_text("[]", encoding="utf-8")
    rc = script.main([str(path)])
    assert rc == 2
