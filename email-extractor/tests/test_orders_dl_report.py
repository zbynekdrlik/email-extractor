"""DL Odoo channel routing (#229) — a delivery-note message must reach the DEDICATED
delivery-notes Discuss channel (243, "AI dodacie listy" — the warehouse), never the
sales-orders channel (152, "objednávky") that a misconfigured/defaulted
`delivery_notes_channel_id` silently falls back to.

Root cause (see #229's design comment): `delivery_notes_channel_id` defaulted to `0`
everywhere (`Config` dataclass, `Config.load()`, `config.yaml`) — and `dl_report._channel()`
treats `0` as "unset", so `report.post_from_config()`'s own `channel_id` fallback (shared
with `orders/confirm.py`, tested separately and NOT touched here) silently reused
`orders_channel_id` instead. These tests pin the fix: a DEFAULT `Config` (no explicit
`delivery_notes_channel_id` override — the exact shape a fresh/reset add-on install has)
must route a DL message to 243, while an orders-pipeline message (no channel_id override at
all, same call shape `pipeline.py` uses) must keep routing to `orders_channel_id` unchanged.
"""
from app.config import Config
from app.orders import dl_report, report


def _post_recorder():
    sent = {}

    def transport(url, headers, payload):
        sent["channel"] = payload["ids"][0]
        return {"id": 1}

    return sent, transport


def test_dl_message_routes_to_the_dedicated_dl_channel_by_default():
    """A fresh/default Config (delivery_notes_channel_id left at its own default, exactly
    like the live add-on before this ticket's options fix) must NOT silently fall back to
    orders_channel_id."""
    cfg = Config(odoo_url="https://erp.example.sk", odoo_api_key="k",
                orders_channel_id=152)
    sent, transport = _post_recorder()

    dl_report.post(cfg, "<p>dodaci list</p>", transport=transport)

    assert sent["channel"] == 243


def test_orders_pipeline_message_still_routes_to_the_orders_channel():
    """The other direction must not break: an orders-pipeline post with no channel_id
    override (the exact call shape `pipeline.py`'s own Odoo summary uses) keeps going to
    orders_channel_id, unaffected by the DL default change."""
    cfg = Config(odoo_url="https://erp.example.sk", odoo_api_key="k",
                orders_channel_id=152)
    sent, transport = _post_recorder()

    report.post_from_config(cfg, "<p>objednavka</p>", transport=transport)

    assert sent["channel"] == 152


def test_delivery_notes_channel_id_still_overridable():
    """An operator can still point DL messages at a different channel explicitly — the
    new default must not become a hardcoded value."""
    cfg = Config(odoo_url="https://erp.example.sk", odoo_api_key="k",
                orders_channel_id=152, delivery_notes_channel_id=999)
    sent, transport = _post_recorder()

    dl_report.post(cfg, "<p>dodaci list</p>", transport=transport)

    assert sent["channel"] == 999
