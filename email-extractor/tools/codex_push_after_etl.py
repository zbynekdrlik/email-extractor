#!/usr/bin/env python3
"""Run the three CODEX pushes once per codex-bridge ETL generation (#485, reopened 2026-10-06).

Why: the pushes used to run on their own clocks (orders 14:40 / 18:25, cards 14:42 / 18:27,
receipts 14:50 / 18:35) while the codex-bridge ETL starts at 14:15 / 18:00 and replaces its
DuckDB file only when the ~45-50 min build is done (6.10.: 15:04:56) — every push sent the
PREVIOUS cycle's data, so the add-on's invoice-as-DL gate waited a whole cycle longer and one
failed push aged the receipts copy past its 30 h fail-closed limit.

How it is triggered on dev2 (units in `tools/systemd/`, installed like the other push units):
  * `codex-push-after-etl.path` — `PathChanged=` on the DuckDB file. Measured on dev2 (systemd
    255): it fires on the ETL's atomic `os.replace(<db>.new, <db>)`, NOT on the `.new` build or
    a read-only DuckDB open — but ALSO on every hardlink / unlink of the CURRENT file (the
    codex-bridge MCP server's `.gen-<ino>-<mtime_ns>` pins, odoo_import's `.syncpin` right after
    each ETL and again when its run ends), since a link-count change is an IN_ATTRIB on the inode.
  * `codex-push-after-etl.timer` — 15:30 / 19:15, a same-day safety net for a missed path event.

The GENERATION GUARD makes every extra trigger a logged no-op: a generation is the file's
(inode, mtime_ns) — a hardlink changes neither, the ETL's replacement changes both — and a round
whose generation was already pushed successfully pushes nothing (`skip:` line). So one ETL is
one push round however many times the path unit fires. A short debounce first waits until the
generation has not changed for `settle` seconds. A failed push does not stop the others; the
generation is then NOT recorded, so the next trigger (path event or the safety-net timer)
repeats the whole round — every push is idempotent (orders upsert, cards / receipts replace).
A generation that changed DURING the round (a second ETL replace) is pushed again at once.

The pushes run as child processes of this one (same interpreter, same EnvironmentFile), so their
own `pushed: …` lines land in this unit's journal, between the `trigger:` and `round:` lines.
The three `codex-*-push.service` units stay for a manual one-off push (no guard there).

Config (environment, from the push EnvironmentFile + systemd):
  CODEX_DUCKDB_PATH          default /var/lib/codex-bridge/codex.duckdb (must equal the path
                             the .path unit watches)
  STATE_DIRECTORY            set by systemd `StateDirectory=` — holds `last-generation`
  CODEX_PUSH_SETTLE_SECONDS  debounce, default 30
"""
from __future__ import annotations

import argparse
import datetime
import os
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_DB_PATH = "/var/lib/codex-bridge/codex.duckdb"
DEFAULT_SETTLE_SECONDS = 30
STATE_FILE = "last-generation"
PUSH_SCRIPTS = ("codex_orders_push.py", "codex_cards_push.py", "codex_receipts_push.py")
PUSH_TIMEOUT_SECONDS = 300   # what each push unit's TimeoutStartSec allowed
MAX_ROUNDS = 3               # a generation changing under a round more often than this = broken
MAX_SETTLE_CHECKS = 10


def _say(line: str) -> None:
    # stdout -> the unit's journal; flush so it orders correctly with the children's lines
    print(line, flush=True)


def generation(db_path: str) -> str:
    """The ETL generation of the DuckDB file: `<inode>-<mtime_ns>` (the shape codex-bridge's own
    `.gen-*` pins use). A hardlink / unlink of the file changes neither part; the ETL's
    `os.replace` of a freshly built file changes both."""
    st = os.stat(db_path)
    return f"{st.st_ino}-{st.st_mtime_ns}"


def file_mtime(db_path: str) -> str:
    """The file's mtime as local ISO time — when the ETL finished writing this generation."""
    ts = os.stat(db_path).st_mtime
    return datetime.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def read_state(state_dir: str) -> str | None:
    """The last generation a round pushed successfully (None = never)."""
    try:
        text = (Path(state_dir) / STATE_FILE).read_text().strip()
    except FileNotFoundError:
        return None
    return text.split()[0] if text else None


def write_state(state_dir: str, gen: str, mtime: str) -> None:
    """Record a successfully pushed generation — atomically (tmp + rename in the same dir)."""
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (STATE_FILE + ".tmp")
    pushed_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    tmp.write_text(f"{gen} file_mtime={mtime} pushed_at={pushed_at}\n")
    os.replace(tmp, d / STATE_FILE)


def default_scripts() -> list[str]:
    """The push tools, installed next to this file (/home/newlevel/codex-orders-push/)."""
    here = Path(__file__).resolve().parent
    return [str(here / name) for name in PUSH_SCRIPTS]


def _run_script(script: str) -> int:
    """Run one push tool as a child process; its output goes straight to our journal."""
    try:
        return subprocess.run([sys.executable, script], check=False,
                              timeout=PUSH_TIMEOUT_SECONDS).returncode
    except subprocess.TimeoutExpired:
        _say(f"error: {Path(script).name} still running after {PUSH_TIMEOUT_SECONDS}s — killed")
        return 124


def settled_generation(db_path: str, settle: float, sleep) -> str:
    """Debounce: wait until the generation has not changed for `settle` seconds. Bounded — after
    MAX_SETTLE_CHECKS the current generation is returned (the round's own after-check catches a
    file that is still moving)."""
    gen = generation(db_path)
    if settle <= 0:
        return gen
    for _ in range(MAX_SETTLE_CHECKS):
        sleep(settle)
        now = generation(db_path)
        if now == gen:
            return gen
        _say(f"settle: generation {gen} -> {now}, waiting again")
        gen = now
    return gen


def _label(script: str) -> str:
    return Path(script).name.removeprefix("codex_").removesuffix("_push.py")


def run_round(db_path: str, state_dir: str, *, scripts=None, runner=None,
              settle: float = DEFAULT_SETTLE_SECONDS, sleep=time.sleep,
              force: bool = False) -> int:
    """One guarded push round. Returns the process exit code (0 = pushed or nothing to do)."""
    scripts = list(scripts or default_scripts())
    runner = runner or _run_script
    try:
        for _ in range(MAX_ROUNDS):
            gen = settled_generation(db_path, settle, sleep)
            mtime = file_mtime(db_path)
            if not force and gen == read_state(state_dir):
                _say(f"skip: generation {gen} (file_mtime={mtime}) already pushed")
                return 0
            force = False
            _say(f"trigger: generation={gen} file_mtime={mtime}")
            results = {_label(s): runner(s) for s in scripts}
            summary = " ".join(f"{k}={'ok' if rc == 0 else f'exit {rc}'}"
                               for k, rc in results.items())
            if any(rc != 0 for rc in results.values()):
                _say(f"round: generation={gen} file_mtime={mtime} FAILED {summary} — "
                     "not recorded, the next trigger repeats the round")
                return 1
            after = generation(db_path)
            if after == gen:
                write_state(state_dir, gen, mtime)
                _say(f"round: generation={gen} file_mtime={mtime} {summary}")
                return 0
            _say(f"round: generation advanced during the round ({gen} -> {after}) — "
                 "pushing the new one")
    except FileNotFoundError as e:
        _say(f"error: the CODEX DuckDB file is missing ({e.filename}) — nothing pushed")
        return 1
    _say(f"error: the DuckDB file kept changing for {MAX_ROUNDS} rounds — not recorded")
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Push CODEX orders, cards and receipts once per codex-bridge ETL (#485)")
    ap.add_argument("--db", default=os.environ.get("CODEX_DUCKDB_PATH", DEFAULT_DB_PATH))
    ap.add_argument("--state-dir", default=os.environ.get("STATE_DIRECTORY") or str(
        Path.home() / ".local" / "state" / "codex-push-after-etl"))
    ap.add_argument("--settle", type=float, default=float(
        os.environ.get("CODEX_PUSH_SETTLE_SECONDS", DEFAULT_SETTLE_SECONDS)))
    ap.add_argument("--force", action="store_true",
                    help="push even if this generation was already pushed")
    args = ap.parse_args(argv)
    return run_round(args.db, args.state_dir, runner=_run_script, settle=args.settle,
                     force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
