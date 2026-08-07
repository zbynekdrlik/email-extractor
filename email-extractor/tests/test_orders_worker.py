"""Order worker: claiming, shadow mode and the engine switch (#60).

The worker runs inside the add-on next to the IMAP poller. Until the engine option is
flipped, n8n owns the pipeline — so the default must be provably inert, and shadow mode
must be provably read-only (it may not claim a message n8n still has to process).
"""
from datetime import UTC, datetime

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


def test_refresh_due_never_touches_the_network_even_when_a_sheet_is_configured(pg, monkeypatch):
    """#129: the Google Sheet is permanently disabled — catalog_overrides/
    customer_overrides (#127/#128) are the sole source of truth. This must hold even
    when the add-on's own (now-inert) catalog_sheet_id/gid options are still populated
    (as they are on the live add-on today) and even when the frozen snapshot is old."""
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("must never fetch the sheet (#129)"))
    sid = _snapshot(pg)
    cfg = _cfg(catalog_sheet_id="DOC", catalog_gid="1", customer_gid="2",
               catalog_refresh_minutes=60)
    assert worker.refresh_due(pg, cfg) == sid

    pg.execute("UPDATE order_snapshots SET checked_at = now() - interval '2 hours'")
    assert worker.refresh_due(pg, cfg) == sid


def test_refresh_due_returns_none_when_no_snapshot_exists_yet(pg):
    assert worker.refresh_due(pg, _cfg()) is None


def test_snapshot_module_has_no_network_fetch_code():
    """#129: fetch_csv/refresh/sheet_csv_url must not exist at all — a passing test
    that merely proves they're unused would not catch someone re-adding a call site
    that reintroduces them."""
    assert not hasattr(worker.snapshot, "fetch_csv")
    assert not hasattr(worker.snapshot, "refresh")
    assert not hasattr(worker.snapshot, "sheet_csv_url")


def test_engine_option_only_accepts_known_values():
    with pytest.raises(ValueError):
        worker.resolve_engine("postgres-please")
    assert worker.resolve_engine("python") == "python"
    assert worker.resolve_engine("") == "n8n"


# --- #93: a message with an open question is WAITING, not stuck -----------

def test_a_message_with_an_open_held_order_is_never_reclaimed(pg):
    """A held message's `processing_at` is never reset (it is not marked processed either),
    so once the 30-minute stale window passes it would look re-claimable — except a message
    with an open `held_orders` row must stay out of the queue no matter how stale the claim
    looks, or it would be run through the LLM again for nothing."""
    _msg(pg)
    _snapshot(pg)
    pg.execute("UPDATE messages SET processing_at = now() - interval '31 minutes'")
    pg.execute(
        """INSERT INTO held_orders (message_id, customer_ean, customer_name, delivery_date,
                                    question_ids)
           VALUES ('m1', '2000000000864', 'Pekáreň', '04.08.2026', ARRAY[1]::bigint[])""")
    cfg = _cfg(ai_orders_engine="python")
    assert worker.tick(pg, cfg, pipeline=lambda *a, **k: {"status": "ok", "items": []}) == 0


def test_a_released_held_order_no_longer_blocks_reclaim(pg):
    _msg(pg)
    _snapshot(pg)
    pg.execute("UPDATE messages SET processing_at = now() - interval '31 minutes'")
    pg.execute(
        """INSERT INTO held_orders (message_id, customer_ean, customer_name, delivery_date,
                                    question_ids, status)
           VALUES ('m1', '2000000000864', 'Pekáreň', '04.08.2026', ARRAY[1]::bigint[],
                   'released')""")
    cfg = _cfg(ai_orders_engine="python")
    assert worker.tick(pg, cfg, pipeline=lambda *a, **k: {"status": "ok", "items": []}) == 1


def test_a_message_with_an_open_mail_question_is_never_reclaimed(pg):
    """#164: a 'mail' question ("is this even an order?") has NO held_orders row at all —
    there is no order to hold — so `_claim` needs its OWN guard for it, not just the
    held_orders one above."""
    from app.orders import teach
    _msg(pg)
    _snapshot(pg)
    pg.execute("UPDATE messages SET processing_at = now() - interval '31 minutes'")
    teach.ask_mail(pg, message_id="m1", sender_email="x@y.sk", subject="Info",
                   reason="AI nenašla objednávku")
    cfg = _cfg(ai_orders_engine="python")
    assert worker.tick(pg, cfg, pipeline=lambda *a, **k: {"status": "ok", "items": []}) == 0


def test_an_answered_mail_question_no_longer_blocks_reclaim(pg):
    from app.orders import teach
    _msg(pg)
    _snapshot(pg)
    pg.execute("UPDATE messages SET processing_at = now() - interval '31 minutes'")
    qid = teach.ask_mail(pg, message_id="m1", sender_email="x@y.sk", subject="Info",
                         reason="AI nenašla objednávku")
    pg.execute("UPDATE order_questions SET status='answered' WHERE id=%s", (qid,))
    cfg = _cfg(ai_orders_engine="python")
    assert worker.tick(pg, cfg, pipeline=lambda *a, **k: {"status": "ok", "items": []}) == 1


def test_a_run_that_holds_an_order_is_not_marked_processed(pg):
    """The worker itself does not know an order was held — `hold.place` (called inside the
    pipeline) is what leaves the `held_orders` row; the worker only has to notice it before
    marking the message processed."""
    _msg(pg)
    _snapshot(pg)
    cfg = _cfg(ai_orders_engine="python")

    def pipeline(conn, cfg, message, snapshot_id):
        conn.execute(
            """INSERT INTO held_orders (message_id, customer_ean, customer_name,
                                        delivery_date, question_ids)
               VALUES (%s, '2000000000864', 'Pekáreň', '04.08.2026', ARRAY[1]::bigint[])""",
            (message["message_id"],))
        return {"status": "held", "items": []}

    assert worker.tick(pg, cfg, pipeline=pipeline) == 1
    row = pg.execute("SELECT processed, processing_at FROM messages").fetchone()
    assert row[0] is False, "a held order must not mark the message processed"


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


# --- #117: the production claim path must feed a real "today" downstream --

def test_claim_sets_today_to_a_real_date_not_blank(pg):
    """`memory.resolve`'s `as_of` date fence is only real when the worker actually hands it
    a date — this pins the exact production gap #117 filed: `_as_message` used to build the
    message dict WITHOUT a "today" key at all, so `as_of` was always "" downstream."""
    _msg(pg)
    row = worker._claim(pg)
    assert row is not None
    today = datetime.now(UTC).date().isoformat()
    assert row["today"] == today, \
        "the claimed message must carry today's real ISO date, not '' or a stale value"


def test_peek_for_shadow_also_sets_today(pg):
    _snapshot(pg)
    _msg(pg, mid="fresh")
    row = worker._peek_for_shadow(pg)
    assert row is not None
    assert row["today"] == datetime.now(UTC).date().isoformat()


def test_the_production_claim_path_feeds_a_real_today_into_memorys_date_fence(pg):
    """End-to-end proof, not just a field check: a message claimed through the REAL
    production path (`worker._claim`, no test ever hand-setting "today") must, when its
    "today" is passed on to `memory.resolve`, actually exclude a shipment dated in the
    future relative to right now — the exact behaviour #117 says was silently a no-op in
    production because `as_of` was always `""`."""
    from datetime import date, timedelta

    from app.orders import memory

    _msg(pg)
    ean = "2000000000864"
    for i in range(3):
        memory.remember(pg, ean, "rozok buduci", "G1", "Rožok budúci",
                        delivered_on=(date.today() + timedelta(days=1 + i)).isoformat(),
                        source="ship")
    message = worker._claim(pg)
    assert message is not None
    # unfiltered, the future-dated deliveries WOULD decide it
    assert memory.resolve(pg, ean, "rozok buduci").gtin == "G1"
    # gated by the real, production-supplied "today", they must not
    assert memory.resolve(pg, ean, "rozok buduci", as_of=message["today"]) is None, \
        "a shipment dated after today must never decide today's order"
