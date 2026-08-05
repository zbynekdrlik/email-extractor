"""Grouped Odoo digest for cleanly-uploaded static orders (#133 "DOPLNENIE ROZHODNUTIA",
2026-08-05). Every function reads its state straight from `static_order_digest` — no
in-memory state anywhere — so a "durable / survives restart" test is just: insert rows
via one call, read them back via a completely separate call, with nothing shared between
the two but the DB itself.
"""
from app.config import Config
from app.orders import static_digest


def _cfg(**kw):
    base = dict(pg_dsn="", data_dir="/tmp")
    base.update(kw)
    return Config(**base)


def _pending(pg):
    return pg.execute(
        "SELECT count(*) FROM static_order_digest WHERE flushed_at IS NULL").fetchone()[0]


def test_queue_inserts_a_pending_row(pg):
    static_digest.queue(pg, "m1", "KARMEN_1_007.txt")
    assert _pending(pg) == 1


def test_maybe_flush_batch_does_nothing_below_the_batch_size(pg):
    for i in range(5):
        static_digest.queue(pg, f"m{i}", f"f{i}.txt")
    posted = []
    n = static_digest.maybe_flush_batch(
        pg, _cfg(), batch_size=30, post=lambda c, h: posted.append(h) or {"id": 1})
    assert n == 0
    assert posted == []
    assert _pending(pg) == 5


def test_maybe_flush_batch_posts_one_grouped_message_once_the_batch_fills(pg):
    for i in range(30):
        static_digest.queue(pg, f"m{i}", f"f{i}.txt")
    posted = []
    n = static_digest.maybe_flush_batch(
        pg, _cfg(), batch_size=30, post=lambda c, h: posted.append(h) or {"id": 1})
    assert n == 30
    assert len(posted) == 1
    assert "30" in posted[0] and "žiadna nemá doplňujúcu správu" in posted[0]
    assert _pending(pg) == 0
    all_flushed = pg.execute(
        "SELECT count(*) FROM static_order_digest WHERE flushed_at IS NOT NULL").fetchone()[0]
    assert all_flushed == 30


def test_maybe_flush_batch_uses_the_configured_batch_size_when_not_overridden(pg):
    for i in range(3):
        static_digest.queue(pg, f"m{i}", f"f{i}.txt")
    posted = []
    cfg = _cfg(static_digest_batch_size=3)
    n = static_digest.maybe_flush_batch(pg, cfg, post=lambda c, h: posted.append(h) or {"id": 1})
    assert n == 3
    assert len(posted) == 1


def test_flush_idle_does_nothing_while_the_queue_is_fresh(pg):
    static_digest.queue(pg, "m1", "f1.txt")
    posted = []
    n = static_digest.flush_idle(pg, _cfg(), idle_minutes=60,
                                 post=lambda c, h: posted.append(h) or {"id": 1})
    assert n == 0
    assert posted == []
    assert _pending(pg) == 1


def test_flush_idle_flushes_once_the_newest_pending_row_is_stale(pg):
    """1 hour since the LAST uploaded order, not since the oldest — so a steady trickle
    of new orders keeps resetting the clock."""
    static_digest.queue(pg, "m1", "f1.txt")
    static_digest.queue(pg, "m2", "f2.txt")
    pg.execute("UPDATE static_order_digest SET created_at = now() - interval '61 minutes'")
    posted = []
    n = static_digest.flush_idle(pg, _cfg(), idle_minutes=60,
                                 post=lambda c, h: posted.append(h) or {"id": 1})
    assert n == 2
    assert "2" in posted[0]
    assert _pending(pg) == 0


def test_flush_idle_is_not_reset_by_an_empty_queue_check(pg):
    n = static_digest.flush_idle(pg, _cfg(), idle_minutes=60,
                                 post=lambda c, h: (_ for _ in ()).throw(
                                     AssertionError("must not post with an empty queue")))
    assert n == 0


def test_the_queue_survives_a_simulated_restart(pg):
    """No in-memory state anywhere in this module — every call reads straight from the
    DB, so state genuinely survives an add-on restart by construction. Simulate one by
    calling `queue()` then reading it back through a COMPLETELY FRESH call sequence with
    nothing shared but the connection (which is exactly what a real restart preserves —
    the DB, never the Python process)."""
    static_digest.queue(pg, "m1", "f1.txt")
    static_digest.queue(pg, "m2", "f2.txt")
    # "restart" — a brand-new call sequence, no shared Python state with the calls above
    assert static_digest._pending_count(pg) == 2
    pg.execute("UPDATE static_order_digest SET created_at = now() - interval '90 minutes'")
    posted = []
    n = static_digest.flush_idle(pg, _cfg(),
                                 post=lambda c, h: posted.append(h) or {"id": 1})
    assert n == 2
    assert static_digest._pending_count(pg) == 0


def test_a_failed_post_leaves_the_batch_pending_for_retry(pg):
    for i in range(30):
        static_digest.queue(pg, f"m{i}", f"f{i}.txt")

    def failing_post(c, h):
        raise RuntimeError("odoo unreachable")

    n = static_digest.maybe_flush_batch(pg, _cfg(), batch_size=30, post=failing_post)
    assert n == 0
    assert _pending(pg) == 30, "an undelivered digest must stay pending, never silently lost"


def test_odoo_not_configured_leaves_the_batch_pending(pg):
    """`report.post_from_config` returns None (not an exception) when Odoo isn't
    configured — the digest must treat that the same as a failed delivery."""
    for i in range(30):
        static_digest.queue(pg, f"m{i}", f"f{i}.txt")
    n = static_digest.maybe_flush_batch(pg, _cfg(), batch_size=30, post=lambda c, h: None)
    assert n == 0
    assert _pending(pg) == 30


def test_singular_and_plural_wording(pg):
    static_digest.queue(pg, "m1", "f1.txt")
    posted = []
    static_digest.maybe_flush_batch(
        pg, _cfg(), batch_size=1, post=lambda c, h: posted.append(h) or {"id": 1})
    assert "1 objednávka" in posted[0]

    for i in range(2, 4):
        static_digest.queue(pg, f"m{i}", f"f{i}.txt")
    posted2 = []
    static_digest.maybe_flush_batch(
        pg, _cfg(), batch_size=2, post=lambda c, h: posted2.append(h) or {"id": 1})
    assert "2 objednávky" in posted2[0]
