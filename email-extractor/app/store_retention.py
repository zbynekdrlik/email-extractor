"""Delete mail-original FILES older than N days from /data/store (#381).

The HA automatic backup carried the extractor's whole /data/store (7.4 GB of raw .eml +
attachment originals for ~10k messages), which filled the host disk and PANICked the
bundled Postgres (2026-09-04 05:21). Two independent host-side fixes ship together: a
top-level `backup_exclude: ["store"]` in config.yaml so the backup stops carrying those
bytes, and this OPTIONAL retention purge so the store itself does not grow without bound.

This module is filesystem-only — ZERO DB dependencies. It deletes FILES (never directories,
never DB rows): the extracted text lives in Postgres (attachments.machine_text /
extracted_text, messages.raw_eml), and #251 proved a reprocess works fine without the
original PDF bytes. Disabled by default (store_retention_days = 0); the owner opts in with a
day count.

Layout it walks (app/store.py): <data_dir>/<safe_id>/{raw.eml, att<i>__<name>} — depth 2.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger("email-extractor")

# Never sweep more than once per this window, however often the caller invokes maybe_purge().
MIN_INTERVAL_S = 24 * 60 * 60


def _coerce_days(retention_days) -> int:
    """A non-int / unparseable value means DISABLED, never a crash (fail-safe). A negative
    value is returned as-is and treated as disabled by the ``<= 0`` guards below."""
    try:
        return int(retention_days)
    except (TypeError, ValueError):
        return 0


def describe(retention_days) -> str:
    """One-line startup description for the log."""
    days = _coerce_days(retention_days)
    return "DISABLED" if days <= 0 else f"enabled: {days} days"


def purge(data_dir, retention_days, now: float | None = None) -> tuple[int, int]:
    """Delete files older than ``retention_days`` under ``<data_dir>/<safe_id>/``.

    Returns ``(files_deleted, bytes_freed)``. A no-op ``(0, 0)`` when retention is disabled
    (``<= 0`` / unparseable) or the store directory does not exist. ``now`` is a wall-clock
    epoch (defaults to ``time.time()``); a file is removed when its mtime is strictly older
    than ``now - days * 86400``. Only regular files at depth 2 are touched — never a
    directory, never a symlink, never a stray top-level file.
    """
    days = _coerce_days(retention_days)
    if days <= 0:
        return (0, 0)
    root = Path(data_dir)
    if not root.is_dir():
        return (0, 0)
    cutoff = (time.time() if now is None else now) - days * 86400
    files_deleted = 0
    bytes_freed = 0
    for sub in root.iterdir():
        # store.py only ever writes files INSIDE a per-message subdirectory (depth 2); a
        # stray top-level file is not part of that layout, so leave it untouched. A symlinked
        # subdir is skipped too — never traverse out of the store.
        if sub.is_symlink() or not sub.is_dir():
            continue
        for f in sub.iterdir():
            # Never follow or delete a symlink — the store never creates one, and following
            # it could reach bytes outside the store.
            if f.is_symlink() or not f.is_file():
                continue
            try:
                st = f.stat()
                if st.st_mtime < cutoff:
                    size = st.st_size
                    f.unlink()
                    files_deleted += 1
                    bytes_freed += size
            except OSError as e:
                log.warning("store retention: could not delete %s: %s", f, e)
    return (files_deleted, bytes_freed)


def maybe_purge(data_dir, retention_days, last_run: float | None, now_monotonic: float, *,
                interval_s: float = MIN_INTERVAL_S,
                wall_now: float | None = None) -> tuple[float | None, tuple[int, int] | None]:
    """Run :func:`purge` at most once per ``interval_s``. Returns ``(new_last_run, result)``,
    where ``result`` is the ``(files_deleted, bytes_freed)`` tuple when the sweep actually
    ran, or ``None`` when it was skipped (disabled, or not yet due).

    ``last_run`` / ``now_monotonic`` are monotonic timestamps (``time.monotonic()``). The two
    clocks are kept separate on purpose — the cadence gate uses monotonic time (immune to a
    wall-clock jump), while :func:`purge`'s own mtime comparison uses wall time (``wall_now``,
    default ``time.time()``). Disabled retention never runs and never records a run time.
    """
    days = _coerce_days(retention_days)
    if days <= 0:
        return (last_run, None)
    if last_run is not None and now_monotonic - last_run < interval_s:
        return (last_run, None)
    result = purge(data_dir, days, now=wall_now)
    return (now_monotonic, result)
