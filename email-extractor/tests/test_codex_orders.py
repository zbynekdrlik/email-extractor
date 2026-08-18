"""#342: CODEX order evidence + the auto-resolve sweep of `mail`-kind board questions."""
from app.config import Config
from app.orders import codex_orders, snapshot, teach

EAN = "2000000000001"
OTHER = "2000000000002"

_CATALOG = "GTIN,Sklad,Názov,doplnok\nSLI50,1,Šiška 50g,\n"


def _seed_customer(pg, email="sklad@a.sk", ean=EAN, name="Zákazník A"):
    """Freeze a snapshot with ONE customer card carrying `email` + `ean`."""
    return snapshot.import_snapshot(
        pg, _CATALOG,
        "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
        f"{name},{ean},Martin,U 1,,,{email}\n")


def _mail_question(pg, mid="mq", sender="sklad@a.sk", mail_date="2026-08-14"):
    """Insert a message received on `mail_date` and raise its open `mail` question."""
    pg.execute(
        "INSERT INTO messages (message_id, category, created_at) VALUES (%s,'ai_orders',%s)",
        (mid, mail_date))
    qid = teach.ask_mail(pg, message_id=mid, sender_email=sender, subject="Re: objednávka")
    assert qid
    return qid


def _cfg():
    return Config(pg_dsn="", data_dir="/tmp", ai_orders_engine="python")


def _mail_rules_count(pg):
    return pg.execute("SELECT count(*) FROM mail_rules").fetchone()[0]


# --- upsert idempotency ------------------------------------------------------

def test_upsert_is_idempotent_on_order_number(pg):
    codex_orders.upsert_orders(pg, [
        {"order_number": 111, "customer_ean": EAN, "customer_name": "A",
         "issue_date": "2026-08-15", "delivery_date": "2026-08-16", "line_count": 3}])
    codex_orders.upsert_orders(pg, [
        {"order_number": 111, "customer_ean": EAN, "customer_name": "A (renamed)",
         "issue_date": "2026-08-17", "delivery_date": "2026-08-18", "line_count": 5}])
    rows = pg.execute(
        "SELECT customer_name, issue_date::text, line_count FROM codex_orders "
        "WHERE order_number = 111").fetchall()
    assert len(rows) == 1, "one row per order number"
    assert rows[0] == ("A (renamed)", "2026-08-17", 5), "the second push wins"


def test_upsert_skips_rows_missing_identity(pg):
    n = codex_orders.upsert_orders(pg, [
        {"order_number": None, "customer_ean": EAN},        # no order number
        {"order_number": 222, "customer_ean": ""},          # no EAN
        {"order_number": 333, "customer_ean": EAN, "issue_date": "2026-08-15"}])
    assert n == 1
    assert pg.execute("SELECT count(*) FROM codex_orders").fetchone()[0] == 1


# --- the auto-resolve sweep: positive case -----------------------------------

def test_sweep_closes_mail_question_from_a_matching_codex_order(pg):
    _seed_customer(pg)
    qid = _mail_question(pg, mail_date="2026-08-14")
    codex_orders.upsert_orders(pg, [
        {"order_number": 555, "customer_ean": EAN, "customer_name": "Zákazník A",
         "issue_date": "2026-08-15", "line_count": 4}])
    before_rules = _mail_rules_count(pg)

    closed = codex_orders.resolve_mail_questions(pg, _cfg())

    assert closed == 1
    q = teach.get(pg, qid)
    assert q["status"] == "answered"
    assert q["answered_by"] == "codex-auto"
    assert q["answer"].get("kind") == "codex_handled"
    assert q["answer"].get("order_number") == 555
    # The message is marked processed so it never re-asks (#307).
    processed = pg.execute(
        "SELECT processed, processed_by FROM messages WHERE message_id = 'mq'").fetchone()
    assert processed == (True, "codex-auto")
    # An honest review event was logged (never an ok/upload event).
    ev = pg.execute(
        "SELECT status, outcome FROM email_events WHERE message_id = 'mq' "
        "AND status = 'review' ORDER BY id DESC LIMIT 1").fetchone()
    assert ev and "CODEX" in ev[1]
    # CRITICAL (#341): the neutral close writes ZERO durable rules.
    assert _mail_rules_count(pg) == before_rules == 0


def test_sweep_never_writes_a_mail_rule(pg):
    """The whole point of the neutral path: unlike not_order/manual, it must not teach a
    permanent ignore/manual rule for the sender (#341 finding)."""
    _seed_customer(pg)
    _mail_question(pg)
    codex_orders.upsert_orders(pg, [
        {"order_number": 556, "customer_ean": EAN, "issue_date": "2026-08-15"}])
    codex_orders.resolve_mail_questions(pg, _cfg())
    assert _mail_rules_count(pg) == 0


# --- the sweep: negative cases (never auto-close) ----------------------------

def test_sweep_leaves_question_open_when_codex_has_no_order(pg):
    _seed_customer(pg)
    qid = _mail_question(pg)
    # no codex_orders rows at all
    closed = codex_orders.resolve_mail_questions(pg, _cfg())
    assert closed == 0
    assert teach.get(pg, qid)["status"] == "open"
    assert pg.execute(
        "SELECT processed FROM messages WHERE message_id = 'mq'").fetchone()[0] is False


def test_sweep_leaves_question_open_when_the_codex_order_predates_the_mail(pg):
    """DATVYST < mail's date means the order was issued BEFORE this mail arrived — it is a
    different (older) order, not this mail being handled. Must not close."""
    _seed_customer(pg)
    qid = _mail_question(pg, mail_date="2026-08-14")
    codex_orders.upsert_orders(pg, [
        {"order_number": 557, "customer_ean": EAN, "issue_date": "2026-08-13"}])
    assert codex_orders.resolve_mail_questions(pg, _cfg()) == 0
    assert teach.get(pg, qid)["status"] == "open"


def test_sweep_does_not_match_a_different_customers_order(pg):
    _seed_customer(pg, email="sklad@a.sk", ean=EAN)
    qid = _mail_question(pg, sender="sklad@a.sk")
    codex_orders.upsert_orders(pg, [
        {"order_number": 558, "customer_ean": OTHER, "issue_date": "2026-08-15"}])
    assert codex_orders.resolve_mail_questions(pg, _cfg()) == 0
    assert teach.get(pg, qid)["status"] == "open"


def test_sweep_does_not_match_an_ambiguous_sender(pg):
    """The address is written in TWO customer cards with different EANs — unresolvable, so
    the question must stay open (never closed for the wrong customer)."""
    snapshot.import_snapshot(
        pg, _CATALOG,
        "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
        f"Zákazník A,{EAN},Martin,U 1,,,shared@x.sk\n"
        f"Zákazník B,{OTHER},Žilina,U 2,,,shared@x.sk\n")
    qid = _mail_question(pg, sender="shared@x.sk")
    codex_orders.upsert_orders(pg, [
        {"order_number": 559, "customer_ean": EAN, "issue_date": "2026-08-15"}])
    assert codex_orders.resolve_mail_questions(pg, _cfg()) == 0
    assert teach.get(pg, qid)["status"] == "open"


# --- the guarded write: a concurrent human answer always wins ----------------

def test_close_is_a_no_op_when_the_question_is_no_longer_open(pg):
    """The guarded `status='open'` UPDATE: if a human already answered (status != 'open'),
    the auto-close must NOT overwrite their answer (#323 pattern). The httpapi answer path
    writes this exact status transition via its own guarded UPDATE — simulate it directly."""
    _seed_customer(pg)
    qid = _mail_question(pg)
    # Simulate a concurrent human answer (the status write the /otazky answer path makes).
    pg.execute("UPDATE order_questions SET status='answered', answered_by='sklad', "
               "answered_at=now() WHERE id=%s", (qid,))

    did = codex_orders._close_mail_question(
        pg, teach.get(pg, qid), {"order_number": 560, "issue_date": "2026-08-15"})
    assert did is False, "the guarded UPDATE affected 0 rows"
    # The human's answer is intact — not overwritten by codex-auto.
    assert teach.get(pg, qid)["answered_by"] == "sklad"


def test_sweep_only_touches_mail_kind_questions(pg):
    """An open item/customer/dl question must be invisible to this sweep."""
    _seed_customer(pg)
    # a customer-kind question for the same sender
    teach.ask_customer(pg, message_id="cq", sender_email="sklad@a.sk", candidates=[],
                       delivery_date="", context={"sender_email": "sklad@a.sk"})
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('cq', 'ai_orders')")
    codex_orders.upsert_orders(pg, [
        {"order_number": 561, "customer_ean": EAN, "issue_date": "2026-08-15"}])
    assert codex_orders.resolve_mail_questions(pg, _cfg()) == 0
