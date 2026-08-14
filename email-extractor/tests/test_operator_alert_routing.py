"""#310: operátorské/diagnostické alerty nesmú chodiť do skladového kanála 243/152.

Kanál 243 (delivery_notes) a 152 (orders) číta sklad/predaj — smú dostávať IBA
správy, na ktoré vie konať sklad/personál. Engine-liveness/staleness (`stuck_
classified_sweep`) a mesačný LLM spend-cap tripwire (`_check_spend_cap`) sú
OPERÁTORSKÉ — musia ísť na `ops_channel_id` (admin kanál), inak do logu +
durable `pending_alerts` — NIKDY na 243/152.
"""
import os

from app.config import Config
from app.orders import dl_worker, report, worker


def _cfg(**kw):
    base = dict(pg_dsn=os.environ.get("PG_TEST_DSN", ""), data_dir="/tmp",
                delivery_notes_engine="n8n", delivery_notes_shadow=False)
    base.update(kw)
    return Config(**base)


def _stuck_dl_msg(pg, mid="dl-op1"):
    """A dodacie_listy message classified but never processed (no order_runs),
    old enough to trip stuck_classified_sweep."""
    pg.execute(
        """INSERT INTO messages (message_id, category, subject, from_addr,
                                 has_attachments, processed, created_at)
           VALUES (%s, 'dodacie_listy', 'DL', 'x@y.sk', true, false,
                   now() - interval '31 minutes')""", (mid,))
    return mid


# --- stuck_classified_sweep (the live #310 incident) -----------------------

def test_stuck_classified_alert_never_targets_warehouse_channel(pg):
    """Default config (ops kanál nenastavený): operátorský staleness alert sa
    NIKDY neenqueuuje na 243/152 — a NIE JE ticho zahodený (durable riadok
    existuje, nech si ho dashboard/log vezme)."""
    _stuck_dl_msg(pg)
    dl_worker.stuck_classified_sweep(pg, _cfg())
    rows = pg.execute(
        "SELECT channel_id FROM pending_alerts WHERE kind='dl_stuck_classified'"
    ).fetchall()
    assert rows, "operátorský alert sa nesmie ticho stratiť"
    for (channel_id,) in rows:
        assert channel_id not in (243, 152), \
            f"operátorský alert skončil na skladovom kanáli {channel_id}"


def test_stuck_classified_alert_routes_to_ops_channel_when_configured(pg):
    _stuck_dl_msg(pg)
    dl_worker.stuck_classified_sweep(pg, _cfg(ops_channel_id=777))
    channel = pg.execute(
        "SELECT channel_id FROM pending_alerts WHERE kind='dl_stuck_classified'"
    ).fetchone()[0]
    assert channel == 777


# --- _check_spend_cap (audit finding, worker.py:161) -----------------------

def _trip_spend(pg, cap_eur=30.0):
    """Seed a month-to-date bill over the cap so cap_tripped() fires once."""
    pg.execute(
        """INSERT INTO order_runs (message_id, shadow, status, cost_usd, finished_at)
           VALUES ('spend-msg', false, 'ok', %s, now())""",
        (float((cap_eur + 10) * 1.10),))


def test_spend_cap_alert_routes_to_ops_channel_not_orders(pg, monkeypatch):
    """Mesačný spend-cap tripwire je operátorský — musí ísť na ops kanál, nie na
    predajný 152 / skladový 243."""
    _trip_spend(pg)
    captured = {}

    def _fake_post(cfg, html, transport=None, channel_id=None):
        captured["channel_id"] = channel_id
        return {"ok": True}

    monkeypatch.setattr(report, "post_from_config", _fake_post)
    worker._check_spend_cap(pg, _cfg(ops_channel_id=777, orders_channel_id=152),
                            shadow=False)
    assert captured.get("channel_id") == 777
    assert captured.get("channel_id") not in (243, 152)
