"""Standalone operational tools (run directly, not imported by the add-on runtime).

`codex_orders_push.py` runs on the dev/ERP box via its own systemd timer (#342). Kept a
package so its pure functions are importable + testable in CI without duckdb/requests.
"""
