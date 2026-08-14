"""#308: nečitateľný scan (OCR ~prázdny, needs_vision=true) končí ticho v
`human_processing` — bez notifikácie, bez spracovania. Dve vrstvy:

- Vrstva 1 (záchrana): vision-asistovaná klasifikácia near-empty-scan správy →
  auto-preklasifikácia na spracovateľskú kategóriu (engine ju vezme).
- Vrstva 2 (povinná sieť): čokoľvek nezachránené → skladu-akčná notifikácia; žiadna
  `human_processing` správa nikdy neskončí bez event/notifikácie.
"""
import os

from app.config import Config
from app.orders import human_processing


def _cfg(**kw):
    base = dict(pg_dsn=os.environ.get("PG_TEST_DSN", ""), data_dir="/tmp")
    base.update(kw)
    return Config(**base)


def _hp_msg(pg, mid="hp1", needs_vision=True, has_attachments=True, minutes_old=31):
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

def test_unrescued_message_gets_a_warehouse_notification(pg):
    """Vision povie 'human_processing' (nevie zaradiť) → povinná notifikácia na
    skladu-akčný kanál (243), nikdy ticho."""
    _hp_msg(pg, "hp3")
    human_processing.sweep(
        pg, _cfg(),
        classify=lambda cfg, atts: {"category": "human_processing",
                                    "confidence": 0.1, "reason": "nečitateľné"})
    channel, kind = pg.execute(
        "SELECT channel_id, kind FROM pending_alerts WHERE message_id='hp3'").fetchone()
    assert kind == "human_processing_review"
    assert channel == _cfg().delivery_notes_channel_id   # 243, skladu-akčné


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
