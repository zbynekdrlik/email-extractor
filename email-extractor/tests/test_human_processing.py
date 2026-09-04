"""#308: nečitateľný scan (OCR ~prázdny, needs_vision=true) končí ticho v
`human_processing` — bez notifikácie, bez spracovania. Dve vrstvy:

- Vrstva 1 (záchrana): vision-asistovaná klasifikácia near-empty-scan správy →
  auto-preklasifikácia na spracovateľskú kategóriu (engine ju vezme).
- Vrstva 2 (povinná sieť): čokoľvek nezachránené → skladu-akčná notifikácia; žiadna
  `human_processing` správa nikdy neskončí bez event/notifikácie.
"""
import os
from datetime import UTC, datetime, timedelta

from app import db
from app.config import Config
from app.orders import human_processing


def _cfg(**kw):
    base = dict(pg_dsn=os.environ.get("PG_TEST_DSN", ""), data_dir="/tmp")
    base.update(kw)
    return Config(**base)


def _hp_msg(pg, mid="hp1", needs_vision=True, has_attachments=True, minutes_old=31,
           created_at=None):
    """A stuck human_processing message. `created_at` (an explicit ISO timestamp) pins
    an exact receive time — used to place a message BEFORE the historical-backlog cutoff;
    otherwise it defaults to `minutes_old` minutes ago (always today, past the cutoff)."""
    if created_at is not None:
        pg.execute(
            """INSERT INTO messages (message_id, category, subject, from_addr,
                                     has_attachments, needs_vision, processed, created_at)
               VALUES (%s, 'human_processing', 'scan', 'tlaciaren@x.sk', %s, %s, false, %s)""",
            (mid, has_attachments, needs_vision, created_at))
    else:
        pg.execute(
            """INSERT INTO messages (message_id, category, subject, from_addr,
                                     has_attachments, needs_vision, processed, created_at)
               VALUES (%s, 'human_processing', 'scan', 'tlaciaren@x.sk', %s, %s, false,
                       now() - make_interval(mins => %s))""",
            (mid, has_attachments, needs_vision, minutes_old))
    return mid


# --- Vrstva 1: vision-asistovaná záchrana ----------------------------------

def test_rescue_reclassifies_a_confident_processor_category(pg):
    _hp_msg(pg, "hp1")
    human_processing.sweep(
        pg, _cfg(),
        classify=lambda cfg, atts: {"category": "dodacie_listy",
                                    "confidence": 0.92, "reason": "dodací list"})
    cat, processed, orig = pg.execute(
        "SELECT category, processed, original_category FROM messages "
        "WHERE message_id='hp1'").fetchone()
    assert cat == "dodacie_listy"        # engine ju teraz uvidí
    assert processed is False            # necháme ju spracovať vlastnou cestou
    assert orig == "human_processing"    # audit: odkiaľ bola zachránená
    # záchrana je zaznamenaná v timeline (event), nie ticho
    assert pg.execute(
        "SELECT count(*) FROM email_events WHERE message_id='hp1'").fetchone()[0] >= 1
    # zachránená správa NEmá skladovú notifikáciu (netreba človeka)
    assert pg.execute(
        "SELECT count(*) FROM pending_alerts WHERE message_id='hp1'").fetchone()[0] == 0


def test_low_confidence_vision_does_not_reclassify_but_notifies(pg):
    _hp_msg(pg, "hp2")
    human_processing.sweep(
        pg, _cfg(),
        classify=lambda cfg, atts: {"category": "dodacie_listy",
                                    "confidence": 0.3, "reason": "neisté"})
    cat = pg.execute(
        "SELECT category FROM messages WHERE message_id='hp2'").fetchone()[0]
    assert cat == "human_processing"     # neisté → nepreklasifikujeme
    alert = pg.execute(
        "SELECT kind FROM pending_alerts WHERE message_id='hp2'").fetchone()
    assert alert is not None and alert[0] == "human_processing_review"


# --- Vrstva 2: povinná sieť (žiadna tichá jama) ----------------------------

def test_unrescued_message_is_notified_to_the_ops_channel_never_warehouse(pg):
    """#308 hotfix: 'systém nevie zaradiť' je OPERÁTORSKÝ/triage koncern (nie skladový,
    keďže human_processing je catch-all plný neskladových mailov) → notifikácia ide na
    ops kanál (report.ops_channel), NIKDY na skladový 243/152. Keď ops nie je nastavený,
    je held (channel 0) — nikdy ticho, nikdy na sklad."""
    _hp_msg(pg, "hp3")
    human_processing.sweep(
        pg, _cfg(ops_channel_id=888),
        classify=lambda cfg, atts: {"category": "human_processing",
                                    "confidence": 0.1, "reason": "nečitateľné"})
    channel, kind = pg.execute(
        "SELECT channel_id, kind FROM pending_alerts WHERE message_id='hp3'").fetchone()
    assert kind == "human_processing_review"
    assert channel == 888                       # ops channel
    assert channel not in (243, 152)            # NEVER a warehouse/sales channel


def test_unrescued_message_with_ops_unset_is_held_never_warehouse(pg):
    _hp_msg(pg, "hp3b")
    human_processing.sweep(pg, _cfg(), classify=lambda cfg, atts: None)
    channel = pg.execute(
        "SELECT channel_id FROM pending_alerts WHERE message_id='hp3b'").fetchone()[0]
    assert channel == 0                          # held (ops unset), never 243/152


# --- historical-backlog cutoff (the live incident) -------------------------

def test_a_message_before_the_backlog_cutoff_is_never_rescued_or_notified(pg):
    """THE incident regression: human_processing is a huge catch-all (payslips, job
    replies) accumulated over months. A message from BEFORE the sweep first ran must
    NEVER be rescued (into a live pipeline) OR notified — it belongs at most in a future
    ops digest, not auto-processing."""
    _hp_msg(pg, "old1", created_at="2026-06-25 21:08:10+02")
    called = []
    human_processing.sweep(
        pg, _cfg(ops_channel_id=888),
        classify=lambda cfg, atts: called.append(1) or {"category": "dodacie_listy",
                                                         "confidence": 0.99})
    assert not called, "historickú správu vision NIKDY neklasifikuje (žiaden LLM náklad)"
    cat = pg.execute(
        "SELECT category FROM messages WHERE message_id='old1'").fetchone()[0]
    assert cat == "human_processing"             # never reclassified into a live pipeline
    assert pg.execute(
        "SELECT count(*) FROM pending_alerts WHERE message_id='old1'").fetchone()[0] == 0


def test_a_recently_rescued_message_is_never_re_rescued(pg):
    """Belt-and-suspenders against a re-rescue loop (observed live 2026-08-14): a message
    rescued once, then RETURNED to human_processing (a manual reclassify / an incident
    revert), must NOT be rescued again within the dedup window — otherwise the sweep
    fights the reclassify and re-raises the same downstream question."""
    _hp_msg(pg, "resc1")
    db.log_event(pg, "resc1", "human_processing", "rescued", "ok", rollup=False)
    called = []
    human_processing.sweep(
        pg, _cfg(ops_channel_id=888),
        classify=lambda cfg, atts: called.append(1) or {"category": "dodacie_listy",
                                                         "confidence": 0.99})
    assert not called, "recently-rescued message must not trigger another vision call"
    assert pg.execute(
        "SELECT category FROM messages WHERE message_id='resc1'").fetchone()[0] == "human_processing"
    assert pg.execute(
        "SELECT count(*) FROM pending_alerts WHERE message_id='resc1'").fetchone()[0] == 0


def test_a_message_after_the_cutoff_is_still_handled(pg):
    """Regression guard: the cutoff must not disable the whole feature — a genuinely
    recent stuck message (today) is still rescued/notified normally."""
    _hp_msg(pg, "new1")   # minutes_old=31 -> today, past the cutoff
    human_processing.sweep(
        pg, _cfg(ops_channel_id=888),
        classify=lambda cfg, atts: {"category": "dodacie_listy", "confidence": 0.95})
    assert pg.execute(
        "SELECT category FROM messages WHERE message_id='new1'").fetchone()[0] == "dodacie_listy"


def test_message_without_vision_signature_is_notified_never_vision_called(pg):
    """human_processing bez prílohy/needs_vision — vision sa nevolá (žiaden LLM
    náklad), ale povinná sieť ju aj tak notifikuje (žiadna tichá jama)."""
    _hp_msg(pg, "hp4", needs_vision=False, has_attachments=False)
    called = []
    human_processing.sweep(
        pg, _cfg(),
        classify=lambda cfg, atts: called.append(1) or {"category": "dodacie_listy",
                                                         "confidence": 0.99})
    assert not called, "vision sa nesmie volať bez near-empty-scan signatúry"
    kind = pg.execute(
        "SELECT kind FROM pending_alerts WHERE message_id='hp4'").fetchone()[0]
    assert kind == "human_processing_review"


def test_notification_is_deduplicated_across_repeated_sweeps(pg):
    _hp_msg(pg, "hp5", needs_vision=False, has_attachments=False)
    human_processing.sweep(pg, _cfg(), classify=lambda cfg, atts: None)
    human_processing.sweep(pg, _cfg(), classify=lambda cfg, atts: None)
    assert pg.execute(
        "SELECT count(*) FROM pending_alerts WHERE message_id='hp5' "
        "AND kind='human_processing_review'").fetchone()[0] == 1


def test_a_fresh_message_within_threshold_is_left_alone(pg):
    """A message just classified must not be swept before the pipeline could settle."""
    _hp_msg(pg, "hp6", minutes_old=1)
    human_processing.sweep(
        pg, _cfg(),
        classify=lambda cfg, atts: {"category": "dodacie_listy", "confidence": 0.99})
    cat = pg.execute(
        "SELECT category FROM messages WHERE message_id='hp6'").fetchone()[0]
    assert cat == "human_processing"     # nedotknuté
    assert pg.execute(
        "SELECT count(*) FROM pending_alerts WHERE message_id='hp6'").fetchone()[0] == 0


def test_a_processed_human_processing_message_is_ignored(pg):
    """Once a human handled it (processed=true), the net must not re-notify."""
    _hp_msg(pg, "hp7", needs_vision=False, has_attachments=False)
    pg.execute("UPDATE messages SET processed=true WHERE message_id='hp7'")
    human_processing.sweep(pg, _cfg(), classify=lambda cfg, atts: None)
    assert pg.execute(
        "SELECT count(*) FROM pending_alerts WHERE message_id='hp7'").fetchone()[0] == 0


# --- 2-working-day horizon: never chase a mail past the horizon (#385) -------

def test_sweep_does_not_notify_a_message_older_than_the_working_day_horizon(pg):
    """#385 regression: a human_processing mail older than the 2-working-day horizon
    (owner directive `two-workday-horizon`) must NOT be re-notified. The live bug
    re-asked two such mails once per working-day morning for 3 weeks (36 ops-digest
    alerts) because the sweep's candidate query had no upper age bound. A genuinely
    RECENT stuck mail is still surfaced — the horizon must not disable the net."""
    old_ts = (datetime.now(UTC) - timedelta(days=10)).isoformat()   # >> 2 working days
    _hp_msg(pg, "old_horizon", needs_vision=False, has_attachments=False,
            created_at=old_ts)
    _hp_msg(pg, "fresh_horizon", needs_vision=False, has_attachments=False,
            minutes_old=31)   # today, well within the horizon
    human_processing.sweep(pg, _cfg(ops_channel_id=888),
                           classify=lambda cfg, atts: None)
    # past-horizon mail: no ops-digest alert (the 3-week nag stops)
    assert pg.execute(
        "SELECT count(*) FROM pending_alerts WHERE message_id='old_horizon'"
    ).fetchone()[0] == 0
    # recent stuck mail: still surfaced exactly once (net not disabled)
    kind = pg.execute(
        "SELECT kind FROM pending_alerts WHERE message_id='fresh_horizon'").fetchone()
    assert kind is not None and kind[0] == "human_processing_review"
