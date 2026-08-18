"""Stale-question reminders (#237): #35 (dl_supplier, gnip@hkloan.eu on the live box)
sat open with zero reminder ever sent — nothing in this codebase ever re-visited an
already-open `order_questions` row before this module existed. These tests pin the
cadence (reminder once, escalate once, then silent), the grouping (one Odoo post per
sweep per audience/level, never one per question), the weekday-only gate, the repeat
highlight, and the channel routing.
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import Config
from app.orders import dl_alerts, question_alerts, teach

PG_DSN = os.environ.get("PG_TEST_DSN")
TZ = ZoneInfo("Europe/Bratislava")

# 2026-08-11 = Tuesday, 08-12 = Wednesday, 08-13 = Thursday, 08-14 = Friday,
# 08-15 = Saturday, 08-16 = Sunday, 08-17 = Monday, 08-18 = Tuesday.
TUE = datetime(2026, 8, 11, 17, 42, tzinfo=TZ)
WED = datetime(2026, 8, 12, 18, 13, tzinfo=TZ)
THU = datetime(2026, 8, 13, 11, 0, tzinfo=TZ)
FRI = datetime(2026, 8, 14, 11, 0, tzinfo=TZ)
SAT = datetime(2026, 8, 15, 11, 0, tzinfo=TZ)
NEXT_MON = datetime(2026, 8, 17, 11, 0, tzinfo=TZ)
NEXT_TUE = datetime(2026, 8, 18, 11, 0, tzinfo=TZ)


def _cfg(**kw):
    base = dict(pg_dsn=PG_DSN, data_dir="/tmp", odoo_url="https://erp.example.sk",
               odoo_api_key="k", orders_channel_id=152, delivery_notes_channel_id=243,
               dashboard_base_url="https://dash.example.sk",
               question_stale_working_days=2, question_escalate_working_days=4)
    base.update(kw)
    return Config(**base)


def _ask(pg, kind="item", customer_ean="2000000000001", customer_name="Zákazník A",
         wording="Šiška", item_key="siska", created_at: datetime = TUE,
         context=None, payload=None, message_id="m1"):
    import json
    row = pg.execute(
        """INSERT INTO order_questions
               (message_id, customer_ean, customer_name, wording, item_key, kind,
                candidates, delivery_date, reason, context, payload, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, '[]'::jsonb, '', '', %s::jsonb, %s::jsonb, %s)
           RETURNING id""",
        (message_id, customer_ean, customer_name, wording, item_key, kind,
         json.dumps(context or {}), json.dumps(payload or {}), created_at)).fetchone()
    return int(row[0])


def _answer(pg, qid, answered_at=TUE):
    pg.execute(
        "UPDATE order_questions SET status = 'answered', answered_at = %s WHERE id = %s",
        (answered_at, qid))


def _pending(pg, kind=None):
    if kind:
        return pg.execute(
            "SELECT channel_id, kind, body_html FROM pending_alerts WHERE kind = %s "
            "ORDER BY id", (kind,)).fetchall()
    return pg.execute(
        "SELECT channel_id, kind, body_html FROM pending_alerts ORDER BY id").fetchall()


def _reminder_sent_at(pg, qid):
    return pg.execute(
        "SELECT reminder_sent_at FROM order_questions WHERE id = %s", (qid,)).fetchone()[0]


def _escalated_at(pg, qid):
    return pg.execute(
        "SELECT escalated_at FROM order_questions WHERE id = %s", (qid,)).fetchone()[0]


def _msg(pg, message_id="m1", category="ai_orders"):
    pg.execute(
        "INSERT INTO messages (message_id, category, processed) VALUES (%s, %s, false) "
        "ON CONFLICT (message_id) DO NOTHING", (message_id, category))


def _status(pg, qid):
    return pg.execute(
        "SELECT status FROM order_questions WHERE id = %s", (qid,)).fetchone()[0]


def _mail_rules_count(pg):
    return int(pg.execute("SELECT count(*) FROM mail_rules").fetchone()[0])


def _msg_processed(pg, message_id):
    return pg.execute(
        "SELECT processed FROM messages WHERE message_id = %s", (message_id,)).fetchone()[0]


# --- cadence: reminder --------------------------------------------------------------

def test_a_question_open_less_than_the_threshold_gets_no_reminder(pg):
    qid = _ask(pg, created_at=WED)  # opened the same weekday `now` is evaluated on
    n = question_alerts.sweep(pg, _cfg(), now=WED)
    assert n == 0
    assert _pending(pg) == []
    assert _reminder_sent_at(pg, qid) is None


def test_a_question_that_has_touched_two_working_days_gets_a_reminder(pg):
    """Tue 17:42 -> Wed: touches {Tue, Wed} = 2 working days -> at the default
    threshold (2), this is exactly the #35 live scenario the ticket cites."""
    qid = _ask(pg, kind="dl_supplier", customer_ean="", item_key="dlsupplier:x",
              payload={"sender_email": "supplier@example.com"}, created_at=TUE)
    n = question_alerts.sweep(pg, _cfg(), now=WED)
    assert n == 1
    rows = _pending(pg, "question_reminder")
    assert len(rows) == 1
    channel, kind, html = rows[0]
    assert channel == 243  # dl_supplier -> delivery_notes_channel_id
    assert "supplier@example.com" in html
    assert "dodávateľ" in html.lower()
    assert _reminder_sent_at(pg, qid) is not None


def test_a_reminder_is_never_sent_twice_for_the_same_threshold(pg):
    qid = _ask(pg, created_at=TUE)
    question_alerts.sweep(pg, _cfg(), now=WED)
    first_sent = _reminder_sent_at(pg, qid)
    n2 = question_alerts.sweep(pg, _cfg(), now=THU)
    assert n2 == 0
    assert _reminder_sent_at(pg, qid) == first_sent
    assert len(_pending(pg, "question_reminder")) == 1


# --- cadence: escalate once, then silent --------------------------------------------

def test_escalation_does_not_fire_the_same_day_as_the_reminder(pg):
    """A cold-start sweep on an already very-old question sends the plain reminder
    first (never both levels in one pass) — escalation needs a later calendar day."""
    qid = _ask(pg, created_at=TUE)
    n = question_alerts.sweep(pg, _cfg(), now=NEXT_MON)  # already past escalate_days
    assert n == 1
    assert len(_pending(pg, "question_reminder")) == 1
    assert len(_pending(pg, "question_escalation")) == 0
    assert _escalated_at(pg, qid) is None


def test_escalation_fires_once_after_the_reminder_and_then_stays_silent(pg):
    qid = _ask(pg, created_at=TUE)
    question_alerts.sweep(pg, _cfg(), now=WED)  # reminder (2 working days)
    assert _escalated_at(pg, qid) is None

    n2 = question_alerts.sweep(pg, _cfg(), now=THU)  # 3 working days, not yet escalate
    assert n2 == 0
    assert _escalated_at(pg, qid) is None

    n3 = question_alerts.sweep(pg, _cfg(), now=FRI)  # 4 working days -> escalate
    assert n3 == 1
    esc_rows = _pending(pg, "question_escalation")
    assert len(esc_rows) == 1
    assert _escalated_at(pg, qid) is not None

    # Still open, still stale — no third message ever again.
    n4 = question_alerts.sweep(pg, _cfg(), now=NEXT_MON)
    assert n4 == 0
    assert len(_pending(pg)) == 2  # exactly reminder + escalation, forever


# --- weekday/hour gate (reused from confirm.morning_check_active) -------------------

def test_no_reminder_fires_on_a_skipped_weekend_day(pg):
    _ask(pg, created_at=TUE)
    n = question_alerts.sweep(pg, _cfg(), now=SAT)
    assert n == 0
    assert _pending(pg) == []


def test_before_the_morning_hour_nothing_fires_even_if_stale(pg):
    _ask(pg, created_at=TUE)
    early = datetime(2026, 8, 13, 7, 0, tzinfo=TZ)  # Thursday, before hour=10 default
    n = question_alerts.sweep(pg, _cfg(), now=early)
    assert n == 0
    assert _pending(pg) == []


# --- grouping: one message, never one per question -----------------------------------

def test_multiple_stale_questions_of_the_same_audience_group_into_one_message(pg):
    _ask(pg, kind="item", customer_ean="2000000000001", wording="Šiška",
        item_key="siska", created_at=TUE, message_id="m1")
    _ask(pg, kind="customer", customer_ean="", wording="a@b.sk", item_key="cust:a@b.sk",
        context={"sender_email": "a@b.sk"}, created_at=TUE, message_id="m2")
    n = question_alerts.sweep(pg, _cfg(), now=WED)
    assert n == 2
    rows = _pending(pg, "question_reminder")
    assert len(rows) == 1  # ONE grouped Odoo post, not two
    channel, _kind, html = rows[0]
    assert channel == 152
    assert "2 otázky" in html or "2 otázok" in html


def test_dl_and_orders_kinds_never_share_one_message(pg):
    _ask(pg, kind="dl_item", customer_ean="", wording="Great", item_key="dlitem:x",
        payload={"supplier_name": "Great s.r.o."}, created_at=TUE, message_id="m1")
    _ask(pg, kind="item", customer_ean="2000000000001", wording="Šiška",
        item_key="siska", created_at=TUE, message_id="m2")
    n = question_alerts.sweep(pg, _cfg(), now=WED)
    assert n == 2
    rows = _pending(pg, "question_reminder")
    channels = sorted(ch for ch, _k, _h in rows)
    assert channels == [152, 243]


# --- repeated occurrence is highlighted ----------------------------------------------

def test_a_recurring_identity_is_highlighted_as_a_repeat(pg):
    old_qid = _ask(pg, kind="dl_supplier", customer_ean="", item_key="dlsupplier:x",
                   payload={"sender_email": "supplier@example.com"}, created_at=TUE,
                   message_id="m-old")
    _answer(pg, old_qid)
    _ask(pg, kind="dl_supplier", customer_ean="", item_key="dlsupplier:x",
        payload={"sender_email": "supplier@example.com"}, created_at=TUE,
        message_id="m-new")
    n = question_alerts.sweep(pg, _cfg(), now=WED)
    assert n == 1
    rows = _pending(pg, "question_reminder")
    html = rows[0][2]
    assert "2" in html and ("opakuje" in html or "×" in html)


def test_a_first_time_question_is_never_reported_as_a_repeat(pg):
    _ask(pg, kind="dl_supplier", customer_ean="", item_key="dlsupplier:y",
        payload={"sender_email": "other@example.com"}, created_at=TUE)
    question_alerts.sweep(pg, _cfg(), now=WED)
    html = _pending(pg, "question_reminder")[0][2]
    assert "opakuje" not in html


# --- delivery reuses the durable dl_alerts outbox, never a direct post --------------

def test_a_failed_post_leaves_the_reminder_pending_for_the_next_flush(pg):
    _ask(pg, created_at=TUE)
    question_alerts.sweep(pg, _cfg(), now=WED)
    assert len(_pending(pg, "question_reminder")) == 1

    n = dl_alerts.flush_pending(pg, cfg=None, post=lambda c, h, **kw: None)
    assert n == 0  # "Odoo not configured" -> stays pending, never lost
    row = pg.execute(
        "SELECT delivered_at FROM pending_alerts WHERE kind = 'question_reminder'"
    ).fetchone()
    assert row[0] is None

    posted = []
    n2 = dl_alerts.flush_pending(
        pg, cfg=None, post=lambda c, h, **kw: posted.append((h, kw.get("channel_id")))
        or {"id": 1})
    assert n2 == 1
    assert len(posted) == 1


# --- #341: auto-expiry of questions older than 2 WORKING days -----------------------

def test_expire_over_age_mail_question_learns_nothing_and_routes_to_manual_review(pg):
    """A `mail`-kind question open across MORE than 2 working days is neutrally expired:
    status 'expired', ZERO `mail_rules` written (the whole danger — the two answer paths
    would each teach a durable ignore/manual rule keyed on a generic subject from a real
    customer), message routed honestly to manual review (`processed=true`), and dropped
    from the open list so it is never reminded again."""
    _msg(pg, "m1")
    qid = _ask(pg, kind="mail", customer_ean="", wording="Re: objednávka",
               item_key="mail:m1", message_id="m1", created_at=TUE,
               payload={"sender_norm": "z@x.sk", "subject_key": "re objednavka",
                        "sender_email": "z@x.sk", "subject": "Re: objednávka"})
    n = question_alerts.expire_stale(pg, _cfg(), now=THU)  # Tue->Thu = 3 working days > 2
    assert n == 1
    assert _status(pg, qid) == "expired"
    assert _mail_rules_count(pg) == 0  # never taught anything
    assert _msg_processed(pg, "m1") is True
    open_ids = [q["id"] for q in teach.open_questions(pg)]
    assert qid not in open_ids


def test_a_question_at_exactly_two_working_days_is_not_yet_expired(pg):
    """Expiry is STRICTLY more than the threshold (the reminder fires AT 2 working days;
    expiry only past it), so a question is never expired before it has had its full 2
    working days open."""
    _msg(pg, "m1")
    qid = _ask(pg, created_at=TUE, message_id="m1")
    n = question_alerts.expire_stale(pg, _cfg(), now=WED)  # Tue->Wed = 2 working days, not > 2
    assert n == 0
    assert _status(pg, qid) == "open"


def test_weekend_arithmetic_friday_question_not_expired_monday_but_expired_tuesday(pg):
    """Sat/Sun don't count: a Friday-opened question has touched only {Fri, Mon} = 2
    working days on Monday morning (not expired), and {Fri, Mon, Tue} = 3 on Tuesday
    (expired)."""
    _msg(pg, "m1")
    qid = _ask(pg, created_at=FRI, message_id="m1")
    assert question_alerts.expire_stale(pg, _cfg(), now=NEXT_MON) == 0
    assert _status(pg, qid) == "open"
    assert question_alerts.expire_stale(pg, _cfg(), now=NEXT_TUE) == 1
    assert _status(pg, qid) == "expired"


def test_an_expired_question_is_never_reminded_again_by_the_sweep(pg):
    """Once expired, the #237 reminder sweep (status='open' only) must never touch it."""
    _msg(pg, "m1")
    qid = _ask(pg, kind="dl_supplier", customer_ean="", item_key="dlsupplier:x",
               message_id="m1", created_at=TUE,
               payload={"sender_email": "s@x.sk"})
    assert question_alerts.expire_stale(pg, _cfg(), now=THU) == 1
    assert _status(pg, qid) == "expired"
    n = question_alerts.sweep(pg, _cfg(), now=THU)
    assert n == 0
    assert _pending(pg) == []


def test_expiry_writes_no_item_or_dl_memory(pg):
    """Expiring item/dl_item questions must NOT teach any card memory (no answer path)."""
    _msg(pg, "m1")
    _ask(pg, kind="item", customer_ean="2000000000001", wording="Šiška", item_key="siska",
         message_id="m1", created_at=TUE)
    _ask(pg, kind="dl_item", customer_ean="", wording="Great", item_key="dlitem:x",
         message_id="m1", created_at=TUE, payload={"supplier_ean": "111"})
    question_alerts.expire_stale(pg, _cfg(), now=THU)
    assert int(pg.execute("SELECT count(*) FROM item_memory").fetchone()[0]) == 0
    assert int(pg.execute("SELECT count(*) FROM dl_item_memory").fetchone()[0]) == 0
