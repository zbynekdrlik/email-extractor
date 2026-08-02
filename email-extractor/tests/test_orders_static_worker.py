"""Static-orders shadow-mode worker (#132): claims nothing, marks nothing, only compares
against what n8n's "Static auto orders" workflow would have produced. All fixtures below are
SYNTHETIC — constructed to match the documented template shapes, never real customer mail
(this repo is public).
"""
import pytest

from app.config import Config
from app.orders import static_worker

KARMEN_TEXT = (
    "Vyšlá objednávka č.: 12345/2026\n"
    "KARMEN 7, Prešov\n"
    "Prev.:7\n"
    "Dátum vystavenia: 01.08.2026\n"
    "Termín dodávky: 03.08.2026\n"
    "Množstvo\n"
    "8588001800013 Rožok štandart 50g 10,000 ks 0,50\n"
    "Nákupná cena spolu\n"
)

# Same header as KARMEN_TEXT but with the item section stripped — a valid header, zero item
# lines (KARMEN and others occasionally send this — "BEZ OBJEDNAVKY").
EMPTY_ORDER_TEXT = (
    "Vyšlá objednávka č.: 12345/2026\n"
    "KARMEN 7, Prešov\n"
    "Prev.:7\n"
    "Dátum vystavenia: 01.08.2026\n"
    "Termín dodávky: 03.08.2026\n"
    "Množstvo\n"
    "Nákupná cena spolu\n"
)

# No delivery/issue date at all — extract_order_data must refuse.
UNPARSEABLE_TEXT = "KARMEN 7, Prešov\nniečo úplne iné, žiadna objednávka tu nie je"


def _msg(pg, mid="m1", category="static_orders", text=KARMEN_TEXT, has_attachments=False):
    pg.execute(
        """INSERT INTO messages (message_id, category, subject, combined_text, has_attachments,
                                 processed)
           VALUES (%s, %s, %s, %s, %s, false)""",
        (mid, category, "Vyšlá objednávka", text, has_attachments))
    return mid


def _cfg(**kw):
    base = dict(pg_dsn="", data_dir="/tmp", static_orders_engine="n8n",
                static_orders_shadow=False)
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

def test_shadow_disabled_touches_nothing(pg):
    _msg(pg)
    _snapshot(pg)
    assert static_worker.tick(pg, _cfg()) == 0
    row = pg.execute("SELECT processing_at, processed FROM messages").fetchone()
    assert row == (None, False), "n8n still owns this message"
    assert pg.execute("SELECT count(*) FROM order_runs").fetchone()[0] == 0


def test_engine_python_is_not_implemented_and_does_nothing(pg):
    """#133 (the real cutover) is separate and not yet built — flipping the engine must
    fail loudly in the log, never crash, never touch the message or write a run."""
    _msg(pg)
    _snapshot(pg)
    cfg = _cfg(static_orders_engine="python", static_orders_shadow=True)
    assert static_worker.tick(pg, cfg) == 0
    row = pg.execute("SELECT processing_at, processed FROM messages").fetchone()
    assert row == (None, False)
    assert pg.execute("SELECT count(*) FROM order_runs").fetchone()[0] == 0


def test_engine_option_only_accepts_known_values():
    with pytest.raises(ValueError):
        static_worker.resolve_engine("postgres-please")
    assert static_worker.resolve_engine("python") == "python"
    assert static_worker.resolve_engine("") == "n8n"


def test_other_categories_are_never_touched(pg):
    _msg(pg, mid="ai1", category="ai_orders")
    _snapshot(pg)
    assert static_worker.tick(pg, _cfg(static_orders_shadow=True)) == 0


def test_without_a_snapshot_nothing_runs(pg):
    _msg(pg)
    assert static_worker.tick(pg, _cfg(static_orders_shadow=True)) == 0
    assert pg.execute("SELECT count(*) FROM order_runs").fetchone()[0] == 0


# --- shadow mode: reads, records, never claims -----------------------------

def test_shadow_claims_nothing_row_unchanged_after_a_tick(pg):
    """The load-bearing guarantee (#132): n8n must find the message exactly as it left it."""
    _msg(pg)
    _snapshot(pg)
    assert static_worker.tick(pg, _cfg(static_orders_shadow=True)) == 1
    state = pg.execute(
        "SELECT processing_at, processed, processed_by, attempts FROM messages").fetchone()
    assert state == (None, False, None, 0), "shadow must not take the message from n8n"


def test_shadow_records_a_successful_run_with_shadow_true(pg):
    sid = _snapshot(pg)
    _msg(pg)
    assert static_worker.tick(pg, _cfg(static_orders_shadow=True)) == 1
    run = pg.execute(
        "SELECT message_id, snapshot_id, shadow, status FROM order_runs").fetchone()
    assert run == ("m1", sid, True, "ok")
    item = pg.execute("SELECT name, quantity, gtin FROM order_items").fetchone()
    assert item == ("Rožok štandart 50g", 10, "8588001800013")


def test_shadow_does_not_rerun_the_same_message(pg):
    _msg(pg)
    _snapshot(pg)
    cfg = _cfg(static_orders_shadow=True)
    assert static_worker.tick(pg, cfg) == 1
    assert static_worker.tick(pg, cfg) == 0


# --- parse/build failures must record a reviewable run, never crash -------

def test_a_message_with_no_dates_records_review_not_a_crash(pg):
    _msg(pg, text=UNPARSEABLE_TEXT)
    _snapshot(pg)
    assert static_worker.tick(pg, _cfg(static_orders_shadow=True)) == 1
    run = pg.execute("SELECT status, result->>'reject_reason' FROM order_runs").fetchone()
    assert run[0] == "review"
    assert "dodania" in run[1] or "dátum" in run[1].lower()
    state = pg.execute("SELECT processing_at, processed FROM messages").fetchone()
    assert state == (None, False)


def test_an_order_with_a_valid_header_but_no_items_records_review(pg):
    _msg(pg, text=EMPTY_ORDER_TEXT)
    _snapshot(pg)
    assert static_worker.tick(pg, _cfg(static_orders_shadow=True)) == 1
    run = pg.execute("SELECT status, result->>'reject_reason' FROM order_runs").fetchone()
    assert run == ("review", "empty_order")


def test_a_photo_only_order_records_review(pg):
    """A photo-only order (empty OCR body, an attachment present) needs vision handling,
    not this deterministic parser — same guard `static_parse` already raises for."""
    _msg(pg, text="krátky text", has_attachments=True)
    _snapshot(pg)
    assert static_worker.tick(pg, _cfg(static_orders_shadow=True)) == 1
    run = pg.execute("SELECT status, result->>'reject_reason' FROM order_runs").fetchone()
    assert run[0] == "review"
    assert "FOTKA" in run[1]


def test_a_missing_ean_on_every_item_records_review(pg):
    """`static_edi.build`'s own hard-fail guard (no item resolved an EAN) must surface as a
    reviewable shadow run, never an uncaught exception."""
    text = (
        "Vyšlá objednávka č.: 1/2026\n"
        "KARMEN 7, Prešov\n"
        "Prev.:7\n"
        "Dátum vystavenia: 01.08.2026\n"
        "Termín dodávky: 03.08.2026\n"
        "Množstvo\n"
        "Neznamy produkt bez EAN 5,000 ks\n"
        "Nákupná cena spolu\n"
    )
    _msg(pg, text=text)
    _snapshot(pg)
    assert static_worker.tick(pg, _cfg(static_orders_shadow=True)) == 1
    run = pg.execute("SELECT status, result->>'reject_reason' FROM order_runs").fetchone()
    assert run[0] == "review"
    assert "EAN" in run[1]


# --- shadow must not chew through the whole archive ------------------------

def test_the_day_bound_is_honoured(pg):
    _snapshot(pg)
    _msg(pg, mid="fresh")
    _msg(pg, mid="ancient")
    pg.execute("UPDATE messages SET created_at = now() - interval '30 days' "
               "WHERE message_id = 'ancient'")
    cfg = _cfg(static_orders_shadow=True, static_orders_shadow_days=3)
    assert static_worker.tick(pg, cfg) == 1
    seen = pg.execute("SELECT message_id FROM order_runs").fetchone()[0]
    assert seen == "fresh"
    # and the ancient one is never picked up on a later tick either
    assert static_worker.tick(pg, cfg) == 0
