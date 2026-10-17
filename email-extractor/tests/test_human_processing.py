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
           created_at=None, from_addr="tlaciaren@x.sk"):
    """A stuck human_processing message. `created_at` (an explicit ISO timestamp) pins
    an exact receive time — used to place a message BEFORE the historical-backlog cutoff;
    otherwise it defaults to `minutes_old` minutes ago (always today, past the cutoff).
    `from_addr` (#436) selects scanner vs non-scanner routing."""
    if created_at is not None:
        pg.execute(
            """INSERT INTO messages (message_id, category, subject, from_addr,
                                     has_attachments, needs_vision, processed, created_at)
               VALUES (%s, 'human_processing', 'scan', %s, %s, %s, false, %s)""",
            (mid, from_addr, has_attachments, needs_vision, created_at))
    else:
        pg.execute(
            """INSERT INTO messages (message_id, category, subject, from_addr,
                                     has_attachments, needs_vision, processed, created_at)
               VALUES (%s, 'human_processing', 'scan', %s, %s, %s, false,
                       now() - make_interval(mins => %s))""",
            (mid, from_addr, has_attachments, needs_vision, minutes_old))
    return mid


# #436: OCR text of a foreign CMR transport form — 3600+ chars of "successful" OCR with
# NO order/DL structure (no EAN, no quantity+unit item lines, no SK DL keyword). This is
# exactly the msg-11503 signature the old `needs_vision`-only gate never sent to vision.
_CMR_OCR = (
    "MEDZINARODNY NAKLADNY LIST\nCMR\n"
    "1. Odosielatel: HK LOAN Sp. z o.o., Katowice, Polsko\n"
    "2. Prijemca: DUOPACK SLOVAKIA s.r.o.\n"
    "3. Miesto nakladky: Jaroslaw, 14.09.\n"
    "4. Miesto vykladky: Bratislava\n"
    "16. Dopravca a jeho adresa ...\n"
    "22. Podpis a peciatka odosielatela\n"
    "23. Podpis a peciatka dopravcu\n"
) * 8   # ~3600 znakov, ako reálny sken


def _hp_attachment(pg, mid, text, filename="scan.pdf", mime="application/pdf",
                   needs_vision=False):
    """One attachment row for a stuck message (#436) — `text` becomes its OCR
    `extracted_text`, which the new `_rescue` gate reads to decide DL-usability."""
    pg.execute(
        """INSERT INTO attachments (message_id, idx, filename, mime, method,
                                    needs_vision, extracted_text)
           VALUES (%s, 0, %s, %s, 'ocr', %s, %s)""",
        (mid, filename, mime, needs_vision, text))


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


def test_horizon_cutoff_counts_working_days_and_skips_weekends():
    """#385: the horizon never counts Saturday/Sunday. Thu - 2 working days = Tue;
    Mon - 2 working days = the previous Thu (the weekend is skipped, so a Friday mail is
    still within a 2-working-day horizon on Monday). The cutoff is local midnight."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Bratislava")
    thu = datetime(2026, 9, 3, 12, 0, tzinfo=tz)          # a Thursday
    cut = human_processing._horizon_cutoff(thu.astimezone(UTC), 2).astimezone(tz)
    assert cut.date().isoformat() == "2026-09-01"          # Tue (Wed=1, Tue=2)
    assert (cut.hour, cut.minute, cut.second) == (0, 0, 0)  # local midnight
    mon = datetime(2026, 9, 7, 9, 0, tzinfo=tz)           # a Monday
    cut2 = human_processing._horizon_cutoff(mon.astimezone(UTC), 2).astimezone(tz)
    assert cut2.date().isoformat() == "2026-09-03"         # Thu (Fri=1, Thu=2; weekend skipped)
    # working_days<=0 → today's local midnight (only today's mail stays)
    cut0 = human_processing._horizon_cutoff(thu.astimezone(UTC), 0).astimezone(tz)
    assert cut0.date().isoformat() == "2026-09-03"


# --- #390: FIFO starvation — LIMIT 10 vs suppressed backlog ----------------

def test_sweep_processes_new_message_despite_suppressed_backlog(pg):
    """#390 RED regression: 11 old already-notified (reminder_suppressed) messages fill
    every LIMIT 10 slot, so a BRAND-NEW stuck message never gets its Layer-1 vision
    attempt or Layer-2 notification. Pre-fix, the sweep returns handled=0 every tick and
    the new message ages past the 2-working-day horizon — permanently lost.

    Setup: 11 old messages with delivered pending_alerts (suppressed until morning) +
    1 new message with no alerts. The sweep MUST process the new one in the first tick."""
    from zoneinfo import ZoneInfo

    # Use a fixed "now" that is afternoon on a weekday — NOT morning-check time,
    # so delivered alerts are suppressed by reminder_suppressed.
    tz = ZoneInfo("Europe/Bratislava")
    now = datetime(2026, 9, 7, 14, 30, tzinfo=tz).astimezone(UTC)

    # 11 old already-notified messages (created ~20 hours ago, within the horizon)
    for i in range(11):
        mid = f"old_suppressed_{i:02d}"
        old_ts = (now - timedelta(hours=20 + i)).isoformat()
        _hp_msg(pg, mid, needs_vision=True, has_attachments=True, created_at=old_ts)
        # Each has a delivered pending_alert from earlier today -> reminder_suppressed
        pg.execute(
            """INSERT INTO pending_alerts (channel_id, kind, body_html, message_id,
                                           created_at, delivered_at)
               VALUES (888, %s, 'test', %s, %s, %s)""",
            (human_processing.ALERT_KIND, mid,
             (now - timedelta(hours=3)).isoformat(),
             (now - timedelta(hours=2)).isoformat()))

    # 1 brand-new message — no pending_alerts, no events, just stuck
    new_ts = (now - timedelta(minutes=20)).isoformat()
    _hp_msg(pg, "new_stuck_390", needs_vision=True, has_attachments=True,
            created_at=new_ts)

    # The classify mock returns a confident rescue for any message
    rescued = []

    def mock_classify(cfg, atts):
        rescued.append(1)
        return {"category": "dodacie_listy", "confidence": 0.95, "reason": "test"}

    handled = human_processing.sweep(pg, _cfg(ops_channel_id=888),
                                     classify=mock_classify, now=now)

    # The new message MUST have been rescued (Layer-1 vision attempt made)
    new_cat = pg.execute(
        "SELECT category FROM messages WHERE message_id='new_stuck_390'").fetchone()[0]
    assert new_cat == "dodacie_listy", (
        f"new message was NOT rescued (still {new_cat!r}) — FIFO starvation bug #390")
    assert handled >= 1, "sweep must handle at least the new message"
    assert len(rescued) >= 1, "vision classify must have been called for the new message"


# --- #436: „úspešný" OCR (CMR/iný papier) tiež dostane vision + scanner routing ---

def test_scanner_scan_with_usable_looking_but_unusable_ocr_still_gets_vision(pg):
    """#436 root cause 1: sken z tlačiarne s „úspešným" OCR (CMR, ~3600 zn., bez
    položiek/EAN/SK-DL kľúčových slov) mal `needs_vision=False`, takže stará brána
    `needs_vision and has_attachments` vision NIKDY nespustila. Nová brána spustí vision
    pre ĽUBOVOĽNÝ mail s prílohou, ktorého OCR je pre DL/objednávku nepoužiteľné."""
    _hp_msg(pg, "cmr1", needs_vision=False, has_attachments=True)
    _hp_attachment(pg, "cmr1", _CMR_OCR)
    called = []

    def classify(cfg, atts):
        called.append(atts)
        return {"category": "no_processing", "confidence": 0.2,
                "reason": "prepravný list", "doc_type": "CMR"}

    human_processing.sweep(
        pg, _cfg(ops_channel_id=888, delivery_notes_channel_id=243,
                 delivery_notes_scanner_senders="tlaciaren@x.sk"),
        classify=classify)
    assert called, "vision sa MUSÍ pokúsiť aj pri needs_vision=False, keď je OCR nepoužiteľné"


def test_scanner_alert_goes_to_delivery_notes_channel_with_doc_type(pg):
    """#436 root cause 2: pre scanner odosielateľa (`delivery_notes_scanner_senders`) musí
    Layer-2 alert ísť na SKLADOVÝ kanál 243 (`delivery_notes_channel_id`), nie ops 592, a
    niesť rozpoznaný typ dokladu z vision klasifikátora — aby sklad vedel, že jeho sken
    nebol dodací list."""
    _hp_msg(pg, "cmr2", needs_vision=False, has_attachments=True)
    _hp_attachment(pg, "cmr2", _CMR_OCR)
    human_processing.sweep(
        pg, _cfg(ops_channel_id=592, delivery_notes_channel_id=243,
                 delivery_notes_scanner_senders="tlaciaren@x.sk"),
        classify=lambda cfg, atts: {"category": "no_processing", "confidence": 0.2,
                                    "reason": "prepravný list", "doc_type": "CMR"})
    channel, kind, body = pg.execute(
        "SELECT channel_id, kind, body_html FROM pending_alerts "
        "WHERE message_id='cmr2'").fetchone()
    assert channel == 243                    # skladový kanál, NIE ops
    assert channel not in (592, 0)           # nie ops, nie held
    assert kind == "scanner_not_dl"
    assert "CMR" in body                     # rozpoznaný typ dokladu je v texte


def test_non_scanner_unusable_ocr_routes_to_ops_unchanged(pg):
    """#436: ne-skenerový odosielateľ s nepoužiteľným OCR ostáva na starom ops routingu
    (#308/#310 nezmenené) — kanál 243 sa týka LEN skenerov."""
    _hp_msg(pg, "cmr3", from_addr="uctaren@firma.sk", needs_vision=False,
            has_attachments=True)
    _hp_attachment(pg, "cmr3", _CMR_OCR)
    human_processing.sweep(
        pg, _cfg(ops_channel_id=888, delivery_notes_channel_id=243,
                 delivery_notes_scanner_senders="tlaciaren@x.sk"),
        classify=lambda cfg, atts: {"category": "human_processing", "confidence": 0.1,
                                    "doc_type": "výplatná páska"})
    channel, kind = pg.execute(
        "SELECT channel_id, kind FROM pending_alerts WHERE message_id='cmr3'").fetchone()
    assert channel == 888                    # ops, nezmenené
    assert kind == "human_processing_review"
    assert channel not in (243, 152)         # NIKDY skladový/predajný kanál


def test_usable_dl_ocr_skips_vision(pg):
    """#436: ak OCR má jasnú DL štruktúru (produktové EAN-13 kódy + položkové riadky),
    klasifikátor už mal dobrý vstup — vision sa nevolá (šetríme jedno LLM volanie),
    správa ide rovno do Layer-2 siete."""
    usable = ("Dodaci list c. 12345\n"
              "8586001112223 Mlieko polotucne 5 ks\n"
              "8586004445556 Maslo 250g 3 ks\n"
              "8586007778889 Jogurt biely 12 ks\n")
    _hp_msg(pg, "usable1", needs_vision=False, has_attachments=True)
    _hp_attachment(pg, "usable1", usable)
    called = []
    human_processing.sweep(
        pg, _cfg(ops_channel_id=888,
                 delivery_notes_scanner_senders="tlaciaren@x.sk"),
        classify=lambda cfg, atts: called.append(1) or None)
    assert not called, "pri použiteľnom DL OCR (EAN + položky) sa vision nevolá"
    # sieť aj tak notifikuje (žiadna tichá jama) — skener → 243
    kind = pg.execute(
        "SELECT kind FROM pending_alerts WHERE message_id='usable1'").fetchone()[0]
    assert kind == "scanner_not_dl"


def test_scanner_dedupe_is_kept_one_alert_per_message(pg):
    """#436: dedupe ostáva — opakované sweepy nesmú enqueovať druhý scanner alert, ANI
    minúť druhé vision volanie (once-per-message rozpočet)."""
    _hp_msg(pg, "cmr5", needs_vision=False, has_attachments=True)
    _hp_attachment(pg, "cmr5", _CMR_OCR)
    cfg = _cfg(delivery_notes_channel_id=243,
               delivery_notes_scanner_senders="tlaciaren@x.sk")
    calls = []

    def classify(c, a):
        calls.append(1)
        return {"category": "no_processing", "confidence": 0.2, "doc_type": "CMR"}

    human_processing.sweep(pg, cfg, classify=classify)
    human_processing.sweep(pg, cfg, classify=classify)
    assert pg.execute(
        "SELECT count(*) FROM pending_alerts WHERE message_id='cmr5' "
        "AND kind='scanner_not_dl'").fetchone()[0] == 1
    assert len(calls) == 1, "vision sa smie minúť najviac RAZ na správu (rozpočet)"


def test_scanner_doc_type_falls_back_to_category_label_then_neutral():
    """#436: an older/degraded verdict without a free-text `doc_type` names the type from a
    Slovak category label; a missing verdict degrades to a neutral 'neznámy typ' rather than
    crashing the notify path."""
    assert human_processing._scanner_doc_type({"doc_type": "CMR"}) == "CMR"
    assert human_processing._scanner_doc_type({"category": "invoices"}) == "faktúra"
    assert human_processing._scanner_doc_type({"category": "dodacie_listy"}) == "dodací list"
    assert human_processing._scanner_doc_type({"category": "zzz"}) == "neznámy typ"
    assert human_processing._scanner_doc_type(None) == "neznámy typ"


def test_ocr_unusable_for_dl_signals():
    """#436: OCR is 'usable' (skip vision) ONLY on a STRONG order/DL-specific signal — ≥2
    DISTINCT product EAN-13 codes, or an SK DL/order keyword. Everything else (empty OCR, a
    foreign CMR, a single reference number, weight-only lines) is 'unusable' → vision."""
    assert human_processing._ocr_unusable_for_dl("") is True
    assert human_processing._ocr_unusable_for_dl(_CMR_OCR) is True
    assert human_processing._ocr_unusable_for_dl(
        "8586001112223 Mlieko\n8586004445556 Maslo") is False       # ≥2 distinct EANs
    assert human_processing._ocr_unusable_for_dl("Dodaci list c.5") is False        # keyword
    # a SINGLE 13-digit run is NOT enough (a CMR consignment number is ~13 digits) — #436
    # review finding: a lone number must never flip a CMR to 'usable' and skip vision
    assert human_processing._ocr_unusable_for_dl("8586001112223 iba jeden kod") is True
    # weight-only lines (kg) are NOT order structure — a CMR carries gross/net weight in kg
    assert human_processing._ocr_unusable_for_dl("Hrubá hmotnosť 1500 kg\nČistá 1480 kg") is True
    # #436-review realistic CMR: 2+ kg weight lines AND a 13-digit consignment number → the
    # exact document class this ticket targets; MUST be unusable (→ vision), not skipped
    assert human_processing._ocr_unusable_for_dl(
        "CMR 1234567890123\nBrutto 2100 kg\nNetto 2050 kg\nprzewozowy Katowice") is True
    # a Polish CMR mentioning "Faktura nr" (no diacritic) must NOT be marked usable
    assert human_processing._ocr_unusable_for_dl("Faktura nr 123/2026 przewozowy") is True


# --- #437: a scanner CMR verdict is rescued into dodacie_listy (never a dead end) -------

def test_scanner_cmr_verdict_is_rescued_to_dodacie_listy(pg):
    """#437: the vision classifier can now recognise a `cmr` (medzinarodny nakladny list).
    For a SCANNER sender (warehouse paper) that verdict reclassifies the message to
    `dodacie_listy` — the SAME rescue shape a processor category takes — so the DL engine
    picks it up instead of the message dead-ending as a 243 alert. RED before #437: `cmr`
    is not a rescuable category, so the message stays in human_processing."""
    _hp_msg(pg, "cmr1", from_addr="tlaciaren@slovnormal.sk")
    _hp_attachment(pg, "cmr1", _CMR_OCR)
    human_processing.sweep(
        pg, _cfg(delivery_notes_scanner_senders="tlaciaren@slovnormal.sk"),
        classify=lambda cfg, atts: {"category": "cmr", "confidence": 0.92,
                                    "reason": "medzinarodny nakladny list",
                                    "doc_type": "CMR"})
    cat, processed, orig = pg.execute(
        "SELECT category, processed, original_category FROM messages "
        "WHERE message_id='cmr1'").fetchone()
    assert cat == "dodacie_listy"        # the DL engine will now see it
    assert processed is False            # let it flow through the DL engine's own claim
    assert orig == "human_processing"    # audit: where it was rescued from
    # a rescued message needs NO warehouse notification (no human burden)
    assert pg.execute(
        "SELECT count(*) FROM pending_alerts WHERE message_id='cmr1'").fetchone()[0] == 0
    # the rescue is recorded in the timeline (event), never silent
    assert pg.execute(
        "SELECT count(*) FROM email_events WHERE message_id='cmr1' AND stage='rescued'"
        ).fetchone()[0] == 1


def test_non_scanner_cmr_verdict_is_not_rescued(pg):
    """#437: a `cmr` verdict rescues ONLY a scanner sender (a CMR is warehouse paper that
    arrives through the scanner). A CMR-looking verdict from a NON-scanner sender is NOT
    auto-reclassified — it falls to the Layer-2 ops net exactly like any unclassifiable
    catch-all mail, never onto the warehouse 243 channel via a spurious reclassify."""
    _hp_msg(pg, "cmr2", from_addr="ktosi@dodavatel.sk")
    _hp_attachment(pg, "cmr2", _CMR_OCR)
    human_processing.sweep(
        pg, _cfg(ops_channel_id=888),   # no scanner senders configured
        classify=lambda cfg, atts: {"category": "cmr", "confidence": 0.92,
                                    "doc_type": "CMR"})
    cat = pg.execute(
        "SELECT category FROM messages WHERE message_id='cmr2'").fetchone()[0]
    assert cat == "human_processing"     # cmr rescues a SCANNER sender only
    channel, kind = pg.execute(
        "SELECT channel_id, kind FROM pending_alerts WHERE message_id='cmr2'").fetchone()
    assert kind == "human_processing_review"
    assert channel == 888                # ops, never the warehouse 243


# --- #495: a scanner scan reaches the warehouse thread ONLY when it MAY be a DL, once ---
#
# 30 days of channel 243 (16.9.-16.10.): 73 "Skeny z tlačiarne" posts about only 40 scans,
# nearly all personal/HR papers (a health card, an employment contract, …), re-reminded
# every working-day morning.
# A scan the vision verdict CLEARLY places elsewhere is posted NOWHERE (it stays on the
# dashboard in human_processing); a scan that may be a DL (no verdict, a DL-like category,
# low confidence) is posted ONCE and never re-reminded. The verdict + the routing decision
# land in email_events. Synthetic fixtures only — never a real employee document.

_SCANNER_CFG = dict(ops_channel_id=592, delivery_notes_channel_id=243,
                    delivery_notes_scanner_senders="tlaciaren@x.sk")
# A synthetic internal form: no EAN, no quantity lines, no SK DL/order keyword.
_PAPER_OCR = "Interny formular\nMeno: Vzor Vzorovy\nDatum: 1.1.2026\nPodpis\n" * 20
_TUE_MORNING = datetime(2026, 10, 13, 10, 30, tzinfo=UTC)   # a Tuesday, past 10:00 local


def _notify_events(pg, mid):
    return pg.execute(
        "SELECT stage, status, detail FROM email_events WHERE message_id = %s "
        "AND workflow = 'human_processing' AND stage = 'notify' ORDER BY id",
        (mid,)).fetchall()


def test_a_clear_non_dl_scanner_scan_is_posted_nowhere_and_its_verdict_is_logged(pg):
    _hp_msg(pg, "hr1", needs_vision=False, has_attachments=True)
    _hp_attachment(pg, "hr1", _PAPER_OCR)
    calls = []

    def classify(cfg, atts):
        calls.append(1)
        return {"category": "no_processing", "confidence": 0.9,
                "reason": "osobný doklad", "doc_type": "pracovná zmluva"}

    cfg = _cfg(**_SCANNER_CFG)
    human_processing.sweep(pg, cfg, classify=classify)
    human_processing.sweep(pg, cfg, classify=classify)   # a later tick
    assert pg.execute("SELECT count(*) FROM pending_alerts WHERE message_id='hr1'"
                      ).fetchone()[0] == 0, "a clear non-DL scan is posted nowhere"
    cat, processed = pg.execute(
        "SELECT category, processed FROM messages WHERE message_id='hr1'").fetchone()
    assert (cat, processed) == ("human_processing", False)   # stays on the dashboard
    events = _notify_events(pg, "hr1")
    assert len(events) == 1, "the routing decision is logged once"
    _, status, detail = events[0]
    assert status == "skipped"
    assert detail["route"] == "none"
    assert detail["verdict"]["category"] == "no_processing"
    assert detail["verdict"]["confidence"] == 0.9
    assert len(calls) == 1, "a decided scan never costs a second vision call"


def test_an_unclear_scanner_scan_is_posted_once_and_never_reminded(pg):
    _hp_msg(pg, "unc1", needs_vision=False, has_attachments=True)
    _hp_attachment(pg, "unc1", _PAPER_OCR)
    calls = []

    def classify(cfg, atts):
        calls.append(1)
        return {"category": "human_processing", "confidence": 0.3, "doc_type": "neznámy"}

    cfg = _cfg(**_SCANNER_CFG)
    human_processing.sweep(pg, cfg, classify=classify)
    rows = pg.execute("SELECT channel_id, kind FROM pending_alerts WHERE message_id='unc1'"
                      ).fetchall()
    assert rows == [(243, "scanner_not_dl")]
    # delivered on an earlier working day; the next working-day morning must not remind
    pg.execute("UPDATE pending_alerts SET delivered_at = %s WHERE message_id = 'unc1'",
               (_TUE_MORNING - timedelta(days=1),))
    human_processing.sweep(pg, cfg, classify=classify, now=_TUE_MORNING)
    assert pg.execute("SELECT count(*) FROM pending_alerts WHERE message_id='unc1'"
                      ).fetchone()[0] == 1, "a scanner alert is sent once, never reminded"
    assert len(calls) == 1
    events = _notify_events(pg, "unc1")
    assert len(events) == 1
    _, status, detail = events[0]
    assert status == "enqueued"
    assert detail["route"] == "warehouse" and detail["channel"] == 243
    assert detail["verdict"]["category"] == "human_processing"


def test_a_scanner_scan_with_no_verdict_is_posted_as_a_maybe_dl(pg):
    """Unreadable (vision returned nothing) = it MAY be a DL → the warehouse hears once."""
    _hp_msg(pg, "nov1", needs_vision=False, has_attachments=True)
    _hp_attachment(pg, "nov1", _PAPER_OCR)
    human_processing.sweep(pg, _cfg(**_SCANNER_CFG), classify=lambda c, a: None)
    assert pg.execute("SELECT channel_id, kind FROM pending_alerts WHERE message_id='nov1'"
                      ).fetchall() == [(243, "scanner_not_dl")]
    _, status, detail = _notify_events(pg, "nov1")[0]
    assert status == "enqueued" and detail["verdict"] is None


def test_scanner_maybe_dl_verdict_table():
    maybe = human_processing._scanner_maybe_dl
    assert maybe(None) is True                                         # unreadable
    for cat in ("dodacie_listy", "invoices", "cmr", "human_processing"):
        assert maybe({"category": cat, "confidence": 0.95}) is True    # DL-like
    assert maybe({"category": "no_processing", "confidence": 0.59}) is True   # unsure
    assert maybe({"category": "reklamacie", "confidence": 0.4}) is True       # unsure
    assert maybe({"category": "no_processing", "confidence": 0.6}) is False   # clear
    assert maybe({"category": "no_processing", "confidence": 0.9}) is False
    assert maybe({"category": "no_processing"}) is True        # no confidence = unsure
    assert maybe({"category": "no_processing", "confidence": "vysoká"}) is True


def test_a_non_scanner_notify_logs_its_ops_routing(pg):
    _hp_msg(pg, "ops1", from_addr="uctaren@firma.sk", needs_vision=False,
            has_attachments=True)
    _hp_attachment(pg, "ops1", _PAPER_OCR)
    human_processing.sweep(
        pg, _cfg(**_SCANNER_CFG),
        classify=lambda c, a: {"category": "no_processing", "confidence": 0.9})
    assert pg.execute("SELECT channel_id, kind FROM pending_alerts WHERE message_id='ops1'"
                      ).fetchall() == [(592, "human_processing_review")]   # unchanged
    _, status, detail = _notify_events(pg, "ops1")[0]
    assert status == "enqueued" and detail["route"] == "ops" and detail["channel"] == 592


def test_reminder_suppressed_never_re_reminds_a_one_shot_scanner_alert(pg):
    from zoneinfo import ZoneInfo

    from app.orders import dl_alerts
    tz = ZoneInfo("Europe/Bratislava")
    cfg = _cfg()
    mon = datetime(2026, 8, 17, 9, 0, tzinfo=tz)
    tue = datetime(2026, 8, 18, 11, 0, tzinfo=tz)
    for kind, mid in (("scanner_not_dl", "s1"), ("human_processing_review", "o1")):
        dl_alerts.enqueue(pg, 243, kind, "<p>l</p>", message_id=mid)
        pg.execute("UPDATE pending_alerts SET delivered_at = %s WHERE message_id = %s",
                   (mon, mid))
    assert dl_alerts.reminder_suppressed(pg, cfg, "scanner_not_dl", "s1", now=tue) is True
    # the ops net keeps its once-per-morning reminder (#336)
    assert dl_alerts.reminder_suppressed(
        pg, cfg, "human_processing_review", "o1", now=tue) is False


def test_an_unparseable_or_nan_confidence_never_breaks_the_sweep_nor_rescues(pg):
    """Review round 1: `_apply_rescue` read `float(confidence)` raw — „vysoká" raised (no
    alert, a new vision call on every tick, forever) and NaN passed `< 0.6` as False, so a
    NaN-confident processor verdict was RESCUED. Both are „unsure": never rescued, posted once."""
    cfg = _cfg(**_SCANNER_CFG)
    for mid, conf in (("odd1", "vysoká"), ("odd2", float("nan"))):
        _hp_msg(pg, mid, needs_vision=False, has_attachments=True)
        _hp_attachment(pg, mid, _PAPER_OCR)
        calls = []

        def classify(c, a, conf=conf, calls=calls):
            calls.append(1)
            return {"category": "dodacie_listy", "confidence": conf, "doc_type": "?"}

        human_processing.sweep(pg, cfg, classify=classify)
        human_processing.sweep(pg, cfg, classify=classify)
        cat = pg.execute("SELECT category FROM messages WHERE message_id = %s",
                         (mid,)).fetchone()[0]
        assert cat == "human_processing", f"{conf!r} must never rescue"
        assert pg.execute("SELECT channel_id, kind FROM pending_alerts WHERE message_id = %s",
                          (mid,)).fetchall() == [(243, "scanner_not_dl")]
        assert len(calls) == 1, f"{conf!r}: one vision call, not one per tick"


def test_an_out_of_vocabulary_category_is_a_maybe_dl():
    """Review round 1: only a KNOWN non-DL verdict hides a scan — a category the classifier
    should not return (upper case, a typo, an invented one) is „unsure", the safe side."""
    maybe = human_processing._scanner_maybe_dl
    assert maybe({"category": "DODACIE_LISTY", "confidence": 0.9}) is True
    assert maybe({"category": "dodaci_list", "confidence": 0.9}) is True
    assert maybe({"category": "vyplatna_paska", "confidence": 0.95}) is True
    assert maybe({"category": " No_Processing ", "confidence": 0.9}) is False


def test_the_sweep_sql_alone_keeps_an_alerted_scanner_scan_out(pg, monkeypatch):
    """The one-shot exclusion is in the candidate SQL (#390: before the LIMIT), not only in
    the Python `reminder_suppressed` — pinned with the Python layer switched off."""
    from app.orders import dl_alerts
    monkeypatch.setattr(dl_alerts, "reminder_suppressed", lambda *a, **k: False)
    _hp_msg(pg, "sql1", needs_vision=False, has_attachments=True)
    _hp_attachment(pg, "sql1", _PAPER_OCR)
    dl_alerts.enqueue(pg, 243, "scanner_not_dl", "<p>l</p>", message_id="sql1")
    pg.execute("UPDATE pending_alerts SET delivered_at = now() - interval '3 days'")
    calls = []
    human_processing.sweep(pg, _cfg(**_SCANNER_CFG),
                           classify=lambda c, a: calls.append(1) or None)
    assert calls == [], "never classified again"
    assert pg.execute("SELECT count(*) FROM pending_alerts WHERE message_id='sql1'"
                      ).fetchone()[0] == 1
