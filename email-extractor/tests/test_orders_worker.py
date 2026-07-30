"""Order worker: claiming, shadow mode and the engine switch (#60).

The worker runs inside the add-on next to the IMAP poller. Until the engine option is
flipped, n8n owns the pipeline — so the default must be provably inert, and shadow mode
must be provably read-only (it may not claim a message n8n still has to process).
"""
import pytest

from app.config import Config
from app.orders import worker


def _msg(pg, mid="m1", category="ai_orders", **kw):
    pg.execute(
        """INSERT INTO messages (message_id, category, subject, combined_text, processed)
           VALUES (%s, %s, %s, %s, false)""",
        (mid, category, kw.get("subject", "Objednávka"), kw.get("text", "10x rožok")))
    return mid


def _cfg(**kw):
    base = dict(pg_dsn="", data_dir="/tmp", ai_orders_engine="n8n", orders_shadow=False)
    base.update(kw)
    return Config(**base)


def _snapshot(pg):
    from app.orders import snapshot
    return snapshot.import_snapshot(
        pg,
        "GTIN,Sklad,Názov,doplnok\n8588001800013,1,Rožok štandart 50g,\n",
        "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
        "Pekáreň s.r.o.,2000000000864,Martin,Košútka 1,,,sklad@pekaren.sk\n")


# --- the default must do nothing at all -----------------------------------

def test_engine_n8n_without_shadow_touches_nothing(pg):
    _msg(pg)
    _snapshot(pg)
    calls = []
    handled = worker.tick(pg, _cfg(), pipeline=lambda *a, **k: calls.append(a))
    assert handled == 0 and calls == []
    row = pg.execute("SELECT processing_at, processed FROM messages").fetchone()
    assert row == (None, False), "n8n still owns this message"
    assert pg.execute("SELECT count(*) FROM order_runs").fetchone()[0] == 0


# --- shadow mode: reads, records, never claims ----------------------------

def test_shadow_records_a_run_without_claiming_or_processing(pg):
    _msg(pg)
    sid = _snapshot(pg)
    seen = {}

    def pipeline(conn, cfg, message, snapshot_id):
        seen.update(message=message["message_id"], snapshot_id=snapshot_id)
        return {"status": "ok", "items": [{"name": "rožok", "quantity": 10, "unit": "ks",
                                          "gtin": "8588001800013", "rule": "llm_sure"}]}

    assert worker.tick(pg, _cfg(orders_shadow=True), pipeline=pipeline) == 1
    assert seen == {"message": "m1", "snapshot_id": sid}
    run = pg.execute(
        "SELECT message_id, snapshot_id, shadow, status FROM order_runs").fetchone()
    assert run == ("m1", sid, True, "ok")
    assert pg.execute("SELECT count(*) FROM order_items").fetchone()[0] == 1
    state = pg.execute("SELECT processing_at, processed, processed_by FROM messages").fetchone()
    assert state == (None, False, None), "shadow must not take the message from n8n"


def test_shadow_does_not_rerun_the_same_message(pg):
    _msg(pg)
    _snapshot(pg)
    cfg = _cfg(orders_shadow=True)
    pipeline = lambda *a, **k: {"status": "ok", "items": []}   # noqa: E731
    assert worker.tick(pg, cfg, pipeline=pipeline) == 1
    assert worker.tick(pg, cfg, pipeline=pipeline) == 0


# --- engine=python: real claim, same protocol the n8n dispatcher uses ------

def test_python_engine_claims_the_message_and_marks_it_processed(pg):
    _msg(pg)
    _snapshot(pg)
    cfg = _cfg(ai_orders_engine="python")
    assert worker.tick(pg, cfg, pipeline=lambda *a, **k: {"status": "ok", "items": []}) == 1
    row = pg.execute(
        "SELECT processed, processed_by, attempts FROM messages").fetchone()
    assert row[0] is True and row[1] == "ai_orders" and row[2] == 1


def test_a_claimed_message_is_not_picked_twice(pg):
    _msg(pg)
    _snapshot(pg)
    cfg = _cfg(ai_orders_engine="python")
    pg.execute("UPDATE messages SET processing_at = now()")   # another worker holds it
    assert worker.tick(pg, cfg, pipeline=lambda *a, **k: {"status": "ok", "items": []}) == 0


def test_a_stale_claim_is_reclaimed(pg):
    _msg(pg)
    _snapshot(pg)
    cfg = _cfg(ai_orders_engine="python")
    pg.execute("UPDATE messages SET processing_at = now() - interval '31 minutes'")
    assert worker.tick(pg, cfg, pipeline=lambda *a, **k: {"status": "ok", "items": []}) == 1


def test_a_crashing_pipeline_releases_the_claim_and_does_not_mark_processed(pg):
    """A crash must never leave a message claimed forever, and must never look
    processed — n8n or the next tick has to be able to pick it up again."""
    _msg(pg)
    _snapshot(pg)

    def boom(*a, **k):
        raise RuntimeError("pipeline exploded")

    assert worker.tick(pg, _cfg(ai_orders_engine="python"), pipeline=boom) == 0
    row = pg.execute("SELECT processing_at, processed FROM messages").fetchone()
    assert row == (None, False)
    run = pg.execute("SELECT status, error FROM order_runs").fetchone()
    assert run[0] == "error" and "exploded" in run[1]


def test_other_categories_are_never_touched(pg):
    _msg(pg, mid="static1", category="static_orders")
    _snapshot(pg)
    assert worker.tick(pg, _cfg(ai_orders_engine="python"),
                       pipeline=lambda *a, **k: {"status": "ok", "items": []}) == 0


def test_without_a_snapshot_nothing_runs(pg):
    """Matching against an absent catalog would reject every order, so the worker
    refuses to run instead of producing confident garbage."""
    _msg(pg)
    calls = []
    assert worker.tick(pg, _cfg(ai_orders_engine="python"),
                       pipeline=lambda *a, **k: calls.append(1)) == 0
    assert calls == []


def test_refresh_is_skipped_while_the_snapshot_is_fresh(pg, monkeypatch):
    """The loop calls refresh every tick; it must only hit Google when the newest
    snapshot is older than the configured interval."""
    _snapshot(pg)
    calls = []
    monkeypatch.setattr(worker.snapshot, "refresh",
                        lambda *a, **k: calls.append(a) or 999)
    cfg = _cfg(catalog_sheet_id="DOC", catalog_gid="1", customer_gid="2",
               catalog_refresh_minutes=60)
    assert worker.refresh_due(pg, cfg) == worker.snapshot.latest_snapshot_id(pg)
    assert calls == []

    pg.execute("UPDATE order_snapshots SET checked_at = now() - interval '2 hours'")
    assert worker.refresh_due(pg, cfg) == 999
    assert len(calls) == 1


def test_refresh_without_a_configured_sheet_never_calls_out(pg, monkeypatch):
    monkeypatch.setattr(worker.snapshot, "refresh",
                        lambda *a, **k: pytest.fail("must not fetch without a sheet id"))
    assert worker.refresh_due(pg, _cfg()) is None


def test_engine_option_only_accepts_known_values():
    with pytest.raises(ValueError):
        worker.resolve_engine("postgres-please")
    assert worker.resolve_engine("python") == "python"
    assert worker.resolve_engine("") == "n8n"


# --- shadow must not chew through the whole archive ----------------------

def test_shadow_only_looks_at_recent_mail(pg):
    """Shadow calls the real model, so an unbounded shadow would replay every message
    ever received (195 of them) at top-tier reasoning cost. The window is what keeps it a
    comparison against n8n on CURRENT mail; the historical corpus is a deliberate,
    separately-run evaluation (#66)."""
    _snapshot(pg)
    _msg(pg, mid="fresh")
    _msg(pg, mid="ancient")
    pg.execute("UPDATE messages SET created_at = now() - interval '30 days' "
               "WHERE message_id = 'ancient'")
    cfg = _cfg(orders_shadow=True, orders_shadow_days=3)
    seen = []
    assert worker.tick(pg, cfg, pipeline=lambda c, cf, m, s: seen.append(m["message_id"])
                       or {"status": "ok", "items": []}) == 1
    assert seen == ["fresh"]
    # and the ancient one is never picked up on a later tick either
    assert worker.tick(pg, cfg, pipeline=lambda *a, **k: {"status": "ok", "items": []}) == 0
