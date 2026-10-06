"""#485 (reopened 2026-10-06): the dev2 CODEX pushes run when the codex-bridge ETL REPLACES its
DuckDB file, not on a clock that fired ~25 min before the ETL had finished (6.10.: pushes 14:40 /
14:42 / 14:50, file replaced 15:04 — every push sent the previous cycle's data).

The trigger is a systemd `.path` unit (`PathChanged=` on the file); verified on dev2 that it fires
on the ETL's atomic `os.replace(<db>.new, <db>)` but ALSO on every hardlink of the current file
(the codex-bridge MCP server's `.gen-*` pins, odoo_import's `.syncpin`). The round runner's
generation guard makes those extra triggers no-ops — one ETL, one push round.

Synthetic files only (tmp_path) — no DuckDB, no network, no systemd.
"""
import datetime
import os
import pathlib

import pytest

from tools import codex_cards_push, codex_receipts_push
from tools import codex_push_after_etl as rounds

SYSTEMD = pathlib.Path(__file__).resolve().parent.parent / "tools" / "systemd"


def _write(path, text):
    path.write_text(text)
    return path


def _replace_like_the_etl(db, text):
    """What codex-bridge's ETL does at the end of a build: write <db>.new, os.replace onto <db>."""
    new = db.with_name(db.name + ".new")
    new.write_text(text)
    os.replace(new, db)


class Recorder:
    """Stands in for running one push script: records the order, returns an exit code."""

    def __init__(self, codes=None, on_call=None):
        self.calls = []
        self.codes = codes or {}
        self.on_call = on_call

    def __call__(self, script):
        name = pathlib.Path(script).name
        self.calls.append(name)
        if self.on_call:
            self.on_call(name)
        return self.codes.get(name, 0)


def _round(db, state_dir, runner, **kw):
    kw.setdefault("settle", 0)
    kw.setdefault("sleep", lambda s: None)
    return rounds.run_round(str(db), str(state_dir), runner=runner, **kw)


def test_the_first_trigger_pushes_orders_cards_receipts_in_order_and_records_the_generation(
        tmp_path, capsys):
    db = _write(tmp_path / "codex.duckdb", "gen-1")
    runner = Recorder()
    assert _round(db, tmp_path / "state", runner) == 0
    assert runner.calls == ["codex_orders_push.py", "codex_cards_push.py",
                            "codex_receipts_push.py"]
    assert rounds.read_state(str(tmp_path / "state")) == rounds.generation(str(db))
    out = capsys.readouterr().out
    assert "round: generation=" in out and "orders=ok cards=ok receipts=ok" in out
    assert "file_mtime=" in out


def test_a_hardlink_of_the_current_file_is_not_a_new_generation_so_nothing_is_pushed(
        tmp_path, capsys):
    """The MCP server's `.gen-*` / odoo_import's `.syncpin` hardlink fires PathChanged= (IN_ATTRIB
    on the inode) — measured on dev2. Same inode, same mtime -> the round must push nothing."""
    db = _write(tmp_path / "codex.duckdb", "gen-1")
    assert _round(db, tmp_path / "state", Recorder()) == 0
    os.link(db, tmp_path / "codex.duckdb.gen-1")
    capsys.readouterr()
    runner = Recorder()
    assert _round(db, tmp_path / "state", runner) == 0
    assert runner.calls == []
    assert "skip: generation" in capsys.readouterr().out


def test_the_etl_replacing_the_file_is_a_new_generation_and_pushes_again(tmp_path):
    db = _write(tmp_path / "codex.duckdb", "gen-1")
    assert _round(db, tmp_path / "state", Recorder()) == 0
    first = rounds.generation(str(db))
    _replace_like_the_etl(db, "gen-2")
    assert rounds.generation(str(db)) != first
    runner = Recorder()
    assert _round(db, tmp_path / "state", runner) == 0
    assert len(runner.calls) == 3
    assert rounds.read_state(str(tmp_path / "state")) == rounds.generation(str(db))


def test_a_failed_push_still_runs_the_others_and_leaves_the_generation_for_the_next_trigger(
        tmp_path, capsys):
    db = _write(tmp_path / "codex.duckdb", "gen-1")
    runner = Recorder(codes={"codex_cards_push.py": 1})
    assert _round(db, tmp_path / "state", runner) == 1
    assert runner.calls == ["codex_orders_push.py", "codex_cards_push.py",
                            "codex_receipts_push.py"]
    assert rounds.read_state(str(tmp_path / "state")) is None
    assert "cards=exit 1" in capsys.readouterr().out
    # the safety-net timer (or the next path event) retries the whole round
    retry = Recorder()
    assert _round(db, tmp_path / "state", retry) == 0
    assert len(retry.calls) == 3


def test_a_generation_that_advanced_during_the_round_is_pushed_again(tmp_path):
    db = _write(tmp_path / "codex.duckdb", "gen-1")
    replaced = []

    def etl_finishes_mid_round(name):
        if name == "codex_cards_push.py" and not replaced:
            replaced.append(True)
            _replace_like_the_etl(db, "gen-2")

    runner = Recorder(on_call=etl_finishes_mid_round)
    assert _round(db, tmp_path / "state", runner) == 0
    assert len(runner.calls) == 6   # the mixed round, then one clean round on gen-2
    assert rounds.read_state(str(tmp_path / "state")) == rounds.generation(str(db))


def test_the_debounce_waits_until_the_file_stopped_changing(tmp_path):
    db = _write(tmp_path / "codex.duckdb", "gen-1")
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            _replace_like_the_etl(db, "gen-2")   # a second replace inside the settle window

    runner = Recorder()
    assert _round(db, tmp_path / "state", runner, settle=30, sleep=sleep) == 0
    assert sleeps == [30, 30]
    assert len(runner.calls) == 3
    assert rounds.read_state(str(tmp_path / "state")) == rounds.generation(str(db))


def test_a_missing_duckdb_file_pushes_nothing_and_fails(tmp_path, capsys):
    runner = Recorder()
    assert _round(tmp_path / "codex.duckdb", tmp_path / "state", runner) == 1
    assert runner.calls == []
    assert "error:" in capsys.readouterr().out


def test_force_pushes_an_already_pushed_generation(tmp_path):
    db = _write(tmp_path / "codex.duckdb", "gen-1")
    assert _round(db, tmp_path / "state", Recorder()) == 0
    runner = Recorder()
    assert _round(db, tmp_path / "state", runner, force=True) == 0
    assert len(runner.calls) == 3


def test_the_scripts_run_are_the_push_tools_next_to_the_round_runner():
    here = pathlib.Path(rounds.__file__).resolve().parent
    assert [pathlib.Path(p) for p in rounds.default_scripts()] == [
        here / "codex_orders_push.py", here / "codex_cards_push.py",
        here / "codex_receipts_push.py"]
    assert all(pathlib.Path(p).is_file() for p in rounds.default_scripts())


def test_main_reads_the_db_and_state_dir_from_the_service_environment(tmp_path, monkeypatch):
    db = _write(tmp_path / "codex.duckdb", "gen-1")
    monkeypatch.setenv("CODEX_DUCKDB_PATH", str(db))
    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_PUSH_SETTLE_SECONDS", "0")
    runner = Recorder()
    monkeypatch.setattr(rounds, "_run_script", runner)
    assert rounds.main([]) == 0
    assert len(runner.calls) == 3
    assert rounds.read_state(str(tmp_path / "state")) == rounds.generation(str(db))


# --- the systemd units (tools/systemd) ---------------------------------------------------------

def _directives(unit):
    out = []
    for line in (SYSTEMD / unit).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "[")) and "=" in line:
            key, _, value = line.partition("=")
            out.append((key.strip(), value.strip()))
    return out


def test_the_path_unit_watches_the_duckdb_file_and_starts_the_guarded_round():
    d = _directives("codex-push-after-etl.path")
    assert ("PathChanged", rounds.DEFAULT_DB_PATH) in d
    assert ("Unit", "codex-push-after-etl.service") in d
    assert ("WantedBy", "paths.target") in d


def test_the_round_service_runs_the_round_runner_as_newlevel_with_the_push_env():
    d = dict(_directives("codex-push-after-etl.service"))
    assert d["Type"] == "oneshot" and d["User"] == "newlevel"
    assert d["EnvironmentFile"] == "/home/newlevel/.secrets/codex-orders-push.env"
    assert d["ExecStart"] == ("/usr/bin/python3 "
                              "/home/newlevel/codex-orders-push/codex_push_after_etl.py")
    assert d["StateDirectory"] == "codex-push-after-etl"


@pytest.mark.parametrize("slot", ["15:30:00", "19:15:00"])
def test_the_safety_net_timer_runs_the_same_guarded_round_after_each_etl(slot):
    d = _directives("codex-push-after-etl.timer")
    assert ("OnCalendar", f"*-*-* {slot} Europe/Prague") in d
    assert ("Unit", "codex-push-after-etl.service") in d
    assert ("Persistent", "true") in d


def test_no_push_has_its_own_clock_before_the_etl_finished_any_more():
    """The per-push timers (14:40-14:50 / 18:25-18:35) fired before the ETL replaced the file;
    only the guarded round's safety-net timer remains."""
    assert sorted(p.name for p in SYSTEMD.glob("*.timer")) == ["codex-push-after-etl.timer"]


# --- the journal line names the ETL time the push sent --------------------------------------

def test_the_cards_pushed_line_names_the_etl_time_it_sent():
    res = codex_cards_push.run(
        "https://email-pz.newlevel.media/api/codex/cards", "tok",
        query=lambda: [{"code": 9990000000017.0, "card_code": "27", "name": "A",
                        "stredisko": 1, "sklad": 1}],
        as_of=lambda: datetime.datetime(2026, 10, 6, 12, 15, 3),
        poster=lambda url, headers, body: {"rows": 1, "codes": 1})
    line = codex_cards_push.pushed_line(res, "https://email-pz.newlevel.media/api/codex/cards")
    assert line == ("pushed: fetched=1 cards=1 rows=1 codes=1 "
                    "source_as_of=2026-10-06T12:15:03+00:00 to=https://email-pz.newlevel.media")


def test_the_receipts_pushed_line_names_the_etl_time_it_sent():
    row = {"receipt_number": 261004409.0, "supplier_ico": 12345678.0,
           "supplier_eans": ["2000000000991"], "supplier_name": "Testovací dodávateľ",
           "receipt_date": datetime.date(2026, 9, 9), "receipt_date_to": None,
           "dl_numbers": [526013012], "invoice_number": None, "invoice_vs": None,
           "total": 10.0, "invoice_total": None, "line_count": 1, "entered_at": None,
           "sdpoh": 10}
    res = codex_receipts_push.run(
        "https://email-pz.newlevel.media/api/codex/receipts", "tok", query=lambda: [row],
        as_of=lambda: datetime.datetime(2026, 10, 6, 12, 15, 3),
        poster=lambda url, headers, body: {"stored": 1})
    line = codex_receipts_push.pushed_line(
        res, "https://email-pz.newlevel.media/api/codex/receipts")
    assert line == ("pushed: fetched=1 receipts=1 stored=1 "
                    "source_as_of=2026-10-06T12:15:03+00:00 to=https://email-pz.newlevel.media")


# --- review round 1: real child processes, exhaustion paths, serialisation, budget -------------

def _child(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body)
    return str(path)


def test_real_child_pushes_report_ok_crash_and_timeout_and_the_round_is_not_recorded(
        tmp_path, monkeypatch, capsys):
    """The real `_run_script` (no runner injected): a crashing push and a hanging one (killed at
    the per-push timeout) are reported by name; the other pushes still ran."""
    monkeypatch.setattr(rounds, "PUSH_TIMEOUT_SECONDS", 1)
    db = _write(tmp_path / "codex.duckdb", "gen-1")
    marker = tmp_path / "ran.txt"
    scripts = [
        _child(tmp_path, "codex_orders_push.py",
               f"open({str(marker)!r}, 'a').write('orders\\n')\n"),
        _child(tmp_path, "codex_cards_push.py", "raise SystemExit(3)\n"),
        _child(tmp_path, "codex_receipts_push.py", "import time\ntime.sleep(30)\n"),
    ]
    assert rounds.run_round(str(db), str(tmp_path / "state"), scripts=scripts, settle=0) == 1
    out = capsys.readouterr().out
    assert "orders=ok cards=exit 3 receipts=exit 124" in out
    assert "killed" in out
    assert marker.read_text() == "orders\n"
    assert rounds.read_state(str(tmp_path / "state")) is None


def test_a_missing_interpreter_fails_that_push_not_the_whole_round_as_a_missing_duckdb(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rounds.sys, "executable", str(tmp_path / "no-such-python"))
    db = _write(tmp_path / "codex.duckdb", "gen-1")
    script = _child(tmp_path, "codex_orders_push.py", "")
    assert rounds.run_round(str(db), str(tmp_path / "state"), scripts=[script], settle=0) == 1
    out = capsys.readouterr().out
    assert "orders=exit 127" in out
    assert "DuckDB file is missing" not in out


def test_the_duckdb_vanishing_after_the_pushes_is_not_reported_as_nothing_pushed(
        tmp_path, capsys):
    db = _write(tmp_path / "codex.duckdb", "gen-1")

    def vanish(name):
        if name == "codex_receipts_push.py":
            db.unlink()

    assert _round(db, tmp_path / "state", Recorder(on_call=vanish)) == 1
    out = capsys.readouterr().out
    assert "nothing pushed" not in out and "vanished" in out
    assert rounds.read_state(str(tmp_path / "state")) is None


def test_a_file_that_never_settles_is_pushed_after_the_bounded_debounce(tmp_path):
    db = _write(tmp_path / "codex.duckdb", "gen-0")
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        _replace_like_the_etl(db, f"gen-{len(sleeps)}")

    runner = Recorder()
    assert _round(db, tmp_path / "state", runner, settle=5, sleep=sleep) == 0
    assert len(sleeps) == rounds.MAX_SETTLE_CHECKS
    assert len(runner.calls) == 3
    assert rounds.read_state(str(tmp_path / "state")) == rounds.generation(str(db))


def test_a_file_that_changes_under_every_round_gives_up_unrecorded(tmp_path, capsys):
    db = _write(tmp_path / "codex.duckdb", "gen-0")
    count = []

    def etl_again(name):
        if name == "codex_orders_push.py":
            count.append(1)
            _replace_like_the_etl(db, f"gen-{len(count)}")

    runner = Recorder(on_call=etl_again)
    assert _round(db, tmp_path / "state", runner) == 1
    assert len(runner.calls) == 3 * rounds.MAX_ROUNDS
    assert rounds.read_state(str(tmp_path / "state")) is None
    assert "kept changing" in capsys.readouterr().out


def test_a_second_round_waits_for_the_running_one_instead_of_pushing_alongside(tmp_path):
    """A manual run next to the service's round must not push the same data twice in
    parallel — the round holds a lock in the state directory."""
    import fcntl
    import threading

    db = _write(tmp_path / "codex.duckdb", "gen-1")
    state = tmp_path / "state"
    state.mkdir()
    runner = Recorder()
    with open(state / rounds.LOCK_FILE, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        t = threading.Thread(target=_round, args=(db, state, runner), daemon=True)
        t.start()
        t.join(timeout=0.5)
        assert t.is_alive() and runner.calls == [], "pushed while another round held the lock"
    t.join(timeout=5)
    assert not t.is_alive() and len(runner.calls) == 3


def test_a_manual_run_without_systemd_uses_the_services_state_directory(tmp_path, monkeypatch):
    db = _write(tmp_path / "codex.duckdb", "gen-1")
    monkeypatch.delenv("STATE_DIRECTORY", raising=False)
    monkeypatch.setenv("CODEX_DUCKDB_PATH", str(db))
    monkeypatch.setenv("CODEX_PUSH_SETTLE_SECONDS", "0")
    monkeypatch.setattr(rounds, "DEFAULT_STATE_DIR", str(tmp_path / "var-lib-state"))
    monkeypatch.setattr(rounds, "_run_script", Recorder())
    assert rounds.main([]) == 0
    assert rounds.read_state(str(tmp_path / "var-lib-state")) == rounds.generation(str(db))


def test_the_service_timeout_covers_the_rounds_worst_case():
    """systemd must never SIGTERM a round the code still considers running."""
    d = dict(_directives("codex-push-after-etl.service"))
    worst = rounds.MAX_ROUNDS * (rounds.MAX_SETTLE_CHECKS * rounds.DEFAULT_SETTLE_SECONDS
                                 + len(rounds.PUSH_SCRIPTS) * rounds.PUSH_TIMEOUT_SECONDS)
    assert int(d["TimeoutStartSec"]) >= worst


# --- the orders push names its ETL time too ------------------------------------------------------

def test_the_orders_push_reads_the_etl_time_of_its_table(tmp_path):
    import duckdb

    from tools import codex_orders_push

    path = tmp_path / "codex.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA meta")
    con.execute("CREATE TABLE meta.etl_runs (table_name VARCHAR, status VARCHAR, "
                "started_at TIMESTAMP, finished_at TIMESTAMP)")
    con.execute("INSERT INTO meta.etl_runs VALUES "
                "('sp002', 'ok', TIMESTAMP '2026-10-06 12:39:58', TIMESTAMP '2026-10-06 12:41:09'),"
                "('sp002', 'error', TIMESTAMP '2026-10-06 16:39:58', TIMESTAMP '2026-10-06 16:41:09'),"
                "('sm002', 'ok', TIMESTAMP '2026-10-06 17:00:00', TIMESTAMP '2026-10-06 17:01:00')")
    con.close()
    assert codex_orders_push.query_as_of(str(path)) == datetime.datetime(2026, 10, 6, 12, 41, 9)
    res = codex_orders_push.run(
        "https://addon/api/codex/orders", "tok", query=lambda: [],
        as_of=lambda: datetime.datetime(2026, 10, 6, 12, 41, 9),
        poster=lambda url, headers, body: {"upserted": 0})
    assert res["source_as_of"] == "2026-10-06T12:41:09+00:00"
