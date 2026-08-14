"""#310: operátorské/diagnostické alerty nesmú chodiť do skladového kanála 243/152.

Kanál 243 (delivery_notes) a 152 (orders) číta sklad/predaj — smú dostávať IBA
správy, na ktoré vie konať sklad/personál. Engine-liveness/staleness (`stuck_
classified_sweep`) a mesačný LLM spend-cap tripwire (`_check_spend_cap`) sú
OPERÁTORSKÉ — idú cez durable `dl_alerts` outbox na `ops_channel_id` (admin kanál).
Keď ops kanál nie je nastavený (0), alert je HELD v `pending_alerts` (počítaný,
nedoručený) — NIKDY sa nedoručí na 243/152 (kľúčové: `post_from_config` s 0 by
inak spadol späť na `orders_channel_id` 152).
"""
import os

from app.config import Config
from app.orders import dl_alerts, dl_worker, report, worker


def _cfg(**kw):
    base = dict(pg_dsn=os.environ.get("PG_TEST_DSN", ""), data_dir="/tmp",
                delivery_notes_engine="n8n", delivery_notes_shadow=False)
    base.update(kw)
    return Config(**base)


def _odoo_cfg(**kw):
    """A production-like config: Odoo configured + the real warehouse/sales channels,
    so a delivery attempt actually resolves a channel (and would misroute a channel-0
    alert to 152 without the fix)."""
    return _cfg(odoo_url="http://odoo.test", odoo_api_key="k", odoo_db="odoo",
                orders_channel_id=152, delivery_notes_channel_id=243, **kw)


def _stuck_dl_msg(pg, mid="dl-op1"):
    pg.execute(
        """INSERT INTO messages (message_id, category, subject, from_addr,
                                 has_attachments, processed, created_at)
           VALUES (%s, 'dodacie_listy', 'DL', 'x@y.sk', true, false,
                   now() - interval '31 minutes')""", (mid,))
    return mid


def _trip_spend(pg, cap_eur=30.0):
    pg.execute(
        """INSERT INTO order_runs (message_id, shadow, status, cost_usd, finished_at)
           VALUES ('spend-msg', false, 'ok', %s, now())""",
        (float((cap_eur + 10) * 1.10),))


def _capturing_flush(pg, cfg):
    """Run flush_pending through the REAL post_from_config with a fake transport that
    records the RESOLVED channel each alert is actually delivered to. Returns the list
    of delivered channel ids (empty when everything was held)."""
    delivered = []

    def _transport(endpoint, headers, payload):
        delivered.append(payload["ids"][0])
        return {"ok": True}

    def _post(c, html, **kw):
        return report.post_from_config(c, html, channel_id=kw.get("channel_id"),
                                       transport=_transport)

    dl_alerts.flush_pending(pg, cfg, post=_post)
    return delivered


# --- stuck_classified_sweep: enqueue routing --------------------------------

def test_stuck_classified_alert_never_targets_warehouse_channel(pg):
    """Default config (ops nenastavený): staleness alert sa NIKDY neenqueuuje na
    243/152 — a NIE JE ticho zahodený (durable riadok existuje)."""
    _stuck_dl_msg(pg)
    dl_worker.stuck_classified_sweep(pg, _cfg())
    rows = pg.execute(
        "SELECT channel_id FROM pending_alerts WHERE kind='dl_stuck_classified'"
    ).fetchall()
    assert rows, "operátorský alert sa nesmie ticho stratiť"
    for (channel_id,) in rows:
        assert channel_id not in (243, 152)


def test_stuck_classified_alert_routes_to_ops_channel_when_configured(pg):
    _stuck_dl_msg(pg)
    dl_worker.stuck_classified_sweep(pg, _cfg(ops_channel_id=777))
    channel = pg.execute(
        "SELECT channel_id FROM pending_alerts WHERE kind='dl_stuck_classified'"
    ).fetchone()[0]
    assert channel == 777


# --- the DELIVERY path (the gap: 0 must be HELD, never resolved to 152) ------

def test_flush_holds_operator_alert_when_ops_unset_never_delivers_to_warehouse(pg):
    """THE regression: with Odoo configured and ops unset, the channel-0 operator alert
    must be HELD by flush_pending — never delivered to 152/243. Without the fix,
    post_from_config resolves channel_id=0 to orders_channel_id (152)."""
    _stuck_dl_msg(pg)
    cfg = _odoo_cfg(ops_channel_id=0)
    dl_worker.stuck_classified_sweep(pg, cfg)
    delivered = _capturing_flush(pg, cfg)
    assert 152 not in delivered and 243 not in delivered, \
        f"operátorský alert doručený na skladový/predajný kanál: {delivered}"
    assert delivered == []            # genuinely held, nothing delivered
    # …and still on record (never lost), still counted as pending
    assert dl_alerts.pending_count(pg) >= 1


def test_flush_delivers_operator_alert_to_the_ops_channel_when_configured(pg):
    _stuck_dl_msg(pg)
    cfg = _odoo_cfg(ops_channel_id=777)
    dl_worker.stuck_classified_sweep(pg, cfg)
    delivered = _capturing_flush(pg, cfg)
    assert delivered == [777]


# --- _check_spend_cap: routing + delivery -----------------------------------

def test_spend_cap_enqueues_to_ops_channel_not_orders(pg):
    _trip_spend(pg)
    worker._check_spend_cap(pg, _cfg(ops_channel_id=777, orders_channel_id=152),
                            shadow=False)
    channel, kind = pg.execute(
        "SELECT channel_id, kind FROM pending_alerts WHERE kind='spend_cap'").fetchone()
    assert kind == "spend_cap"
    assert channel == 777


def test_spend_cap_with_ops_unset_is_held_never_delivered_to_152(pg):
    _trip_spend(pg)
    cfg = _odoo_cfg(ops_channel_id=0)
    worker._check_spend_cap(pg, cfg, shadow=False)
    # enqueued channel-0 (never the sales channel), and held on flush (not delivered)
    channel = pg.execute(
        "SELECT channel_id FROM pending_alerts WHERE kind='spend_cap'").fetchone()[0]
    assert channel == 0
    delivered = _capturing_flush(pg, cfg)
    assert 152 not in delivered and delivered == []


def test_spend_cap_in_shadow_mode_never_enqueues(pg):
    _trip_spend(pg)
    worker._check_spend_cap(pg, _cfg(ops_channel_id=777), shadow=True)
    assert pg.execute(
        "SELECT count(*) FROM pending_alerts WHERE kind='spend_cap'").fetchone()[0] == 0
