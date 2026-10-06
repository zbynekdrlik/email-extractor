"""Standalone operational tools (run directly, not imported by the add-on runtime).

The CODEX pushes (`codex_orders_push.py` #342, `codex_cards_push.py` #467,
`codex_receipts_push.py` #485) run on dev2, started by `codex_push_after_etl.py` when the
codex-bridge ETL replaced its DuckDB (#485; units in `systemd/`). Kept a package so their
pure functions are importable + testable in CI without duckdb/requests.
"""
