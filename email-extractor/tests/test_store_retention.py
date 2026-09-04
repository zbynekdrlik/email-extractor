"""Filesystem-only store-retention purge (#381) — no Postgres, no DB, tmp_path only.

Deletes mail-original FILES older than N days under /data/store/<safe_id>/, keeping the
extracted TEXT the reprocess actually needs (attachments.extracted_text,
messages.body_text/combined_text — #251). The raw .eml + attachment originals have NO DB copy,
so purging them 404s /eml + /files for that mail (the owner's opt-in trade-off).
Default N=0 disables the whole thing, so nothing is ever deleted unless the owner opts in.
"""

import os
from pathlib import Path

from app import store_retention

DAY = 86400.0
NOW = 1_000_000_000.0  # a fixed wall-clock instant for deterministic mtime comparisons


def _mk(root: Path, msg: str, name: str, mtime: float, size: int = 10) -> Path:
    """Create <root>/<msg>/<name> with a given size + mtime (mirrors app/store.py layout)."""
    d = root / msg
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(b"x" * size)
    os.utime(f, (mtime, mtime))
    return f


def test_deletes_files_older_than_cutoff_keeps_recent(tmp_path):
    old_raw = _mk(tmp_path, "msgA-abc123", "raw.eml", NOW - 100 * DAY)
    old_att = _mk(tmp_path, "msgA-abc123", "att0__inv.pdf", NOW - 100 * DAY)
    recent = _mk(tmp_path, "msgB-def456", "raw.eml", NOW - 10 * DAY)
    files, freed = store_retention.purge(tmp_path, 90, now=NOW)
    assert not old_raw.exists()
    assert not old_att.exists()
    assert recent.exists()
    assert files == 2
    assert freed == 20  # two 10-byte files


def test_return_value_is_files_and_bytes(tmp_path):
    d = tmp_path / "m"
    d.mkdir()
    a = d / "raw.eml"
    a.write_bytes(b"a" * 100)
    os.utime(a, (NOW - 200 * DAY, NOW - 200 * DAY))
    b = d / "att0__x.pdf"
    b.write_bytes(b"b" * 250)
    os.utime(b, (NOW - 200 * DAY, NOW - 200 * DAY))
    assert store_retention.purge(tmp_path, 30, now=NOW) == (2, 350)


def test_disabled_zero_deletes_nothing(tmp_path):
    f = _mk(tmp_path, "m", "raw.eml", NOW - 999 * DAY)
    assert store_retention.purge(tmp_path, 0, now=NOW) == (0, 0)
    assert f.exists()


def test_negative_and_unparseable_delete_nothing(tmp_path):
    f = _mk(tmp_path, "m", "raw.eml", NOW - 999 * DAY)
    assert store_retention.purge(tmp_path, -5, now=NOW) == (0, 0)
    assert store_retention.purge(tmp_path, "abc", now=NOW) == (0, 0)
    assert store_retention.purge(tmp_path, None, now=NOW) == (0, 0)
    assert f.exists()


def test_missing_dir_is_noop(tmp_path):
    assert store_retention.purge(tmp_path / "nope", 30, now=NOW) == (0, 0)


def test_symlink_is_never_touched(tmp_path):
    # A symlink inside the store must never be followed or deleted, even if its target is old.
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"keep")
    os.utime(outside, (NOW - 999 * DAY, NOW - 999 * DAY))
    store = tmp_path / "store"
    (store / "m").mkdir(parents=True)
    link = store / "m" / "raw.eml"
    link.symlink_to(outside)
    files, freed = store_retention.purge(store, 30, now=NOW)
    assert (files, freed) == (0, 0)
    assert outside.exists()
    assert link.exists()


def test_symlinked_subdirectory_is_never_traversed(tmp_path):
    # A symlinked SUBDIRECTORY inside the store must not be traversed — otherwise an old file
    # living OUTSIDE the store, reached through the link, would be deleted. Guards the
    # `sub.is_symlink()` check in purge() (distinct from the depth-2 file-symlink test above).
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    old_outside = outside_dir / "raw.eml"
    old_outside.write_bytes(b"keep")
    os.utime(old_outside, (NOW - 999 * DAY, NOW - 999 * DAY))
    store = tmp_path / "store"
    store.mkdir()
    (store / "m").symlink_to(outside_dir, target_is_directory=True)
    files, freed = store_retention.purge(store, 30, now=NOW)
    assert (files, freed) == (0, 0)
    assert old_outside.exists()


def test_unreadable_subdir_does_not_crash_the_sweep(tmp_path):
    # A subdir whose contents cannot be listed must be SKIPPED, not crash the whole sweep —
    # otherwise the escaping OSError leaves maybe_purge()'s cadence unrecorded and main.py
    # retries every poll_interval. The good subdir's old file is still purged.
    good = tmp_path / "good"
    good.mkdir()
    old = good / "raw.eml"
    old.write_bytes(b"x" * 10)
    os.utime(old, (NOW - 999 * DAY, NOW - 999 * DAY))
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "raw.eml").write_bytes(b"y")
    os.chmod(bad, 0o000)
    try:
        files, freed = store_retention.purge(tmp_path, 30, now=NOW)
    finally:
        os.chmod(bad, 0o755)  # restore so pytest's tmp cleanup can remove it
    # purge did not raise; the readable subdir's old file was deleted regardless of whether
    # this process could read `bad` (a non-root run skips it; root would process it, but its
    # file is not old so nothing there is deleted either way).
    assert not old.exists()
    assert files >= 1


def test_top_level_files_are_left_alone(tmp_path):
    # store.py only ever writes files at depth 2 (<data_dir>/<safe_id>/<file>); a stray file
    # sitting directly under data_dir is not part of that layout and must not be deleted.
    stray = tmp_path / "loose.txt"
    stray.write_bytes(b"x")
    os.utime(stray, (NOW - 999 * DAY, NOW - 999 * DAY))
    assert store_retention.purge(tmp_path, 30, now=NOW) == (0, 0)
    assert stray.exists()


def test_maybe_purge_disabled_never_runs(tmp_path):
    f = _mk(tmp_path, "m", "raw.eml", NOW - 999 * DAY)
    last, res = store_retention.maybe_purge(tmp_path, 0, None, 0.0, wall_now=NOW)
    assert res is None
    assert last is None
    assert f.exists()


def test_cadence_no_second_run_within_window(tmp_path):
    f = _mk(tmp_path, "m", "raw.eml", NOW - 999 * DAY)
    # first ever call -> due, runs, records the monotonic timestamp
    last, res = store_retention.maybe_purge(tmp_path, 30, None, 0.0, wall_now=NOW)
    assert res == (1, 10)
    assert last == 0.0
    assert not f.exists()
    # recreate an old file so a second (unwanted) run would be detectable
    f2 = _mk(tmp_path, "m", "raw.eml", NOW - 999 * DAY)
    # 1 hour later (< 24h window) -> NOT due, purge not called
    last2, res2 = store_retention.maybe_purge(tmp_path, 30, last, 3600.0, wall_now=NOW)
    assert res2 is None
    assert last2 == last
    assert f2.exists()
    # > 24h later -> due again
    last3, res3 = store_retention.maybe_purge(tmp_path, 30, last2, 90000.0, wall_now=NOW)
    assert res3 == (1, 10)
    assert last3 == 90000.0
    assert not f2.exists()


def test_describe():
    assert store_retention.describe(0) == "DISABLED"
    assert store_retention.describe(-3) == "DISABLED"
    assert store_retention.describe("bad") == "DISABLED"
    assert store_retention.describe(90) == "enabled: 90 days"
