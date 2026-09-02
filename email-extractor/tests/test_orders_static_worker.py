"""Static-orders shadow-mode worker (#132): claims nothing, marks nothing, only compares
against what n8n's "Static auto orders" workflow would have produced. All fixtures below are
SYNTHETIC — constructed to match the documented template shapes, never real customer mail
(this repo is public).
"""
import pytest

from app.config import Config
from app.orders import edi, static_edi, static_parse, static_worker, worker
from app.orders import snapshot as snapshot_mod

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

# Same items/dates as KARMEN_TEXT but with NEITHER a "Prev.:N" line NOR a "KARMEN N,"
# location line — the item's own inline EAN still resolves it, but `static_edi.build`'s
# own hard-fail guard (missing prevNumber) must fire.
NO_PREV_TEXT = (
    "Vyšlá objednávka č.: 12345/2026\n"
    "Dátum vystavenia: 01.08.2026\n"
    "Termín dodávky: 03.08.2026\n"
    "Množstvo\n"
    "8588001800013 Rožok štandart 50g 10,000 ks 0,50\n"
    "Nákupná cena spolu\n"
)


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
    return snapshot_mod.import_snapshot(
        pg,
        "GTIN,Sklad,Názov,doplnok\n8588001800013,1,Rožok štandart 50g,\n",
        "Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,Číslo mobilu,E-mail\n"
        "Pekáreň s.r.o.,2000000000864,Martin,Košútka 1,,,sklad@pekaren.sk\n")


def _static_built(pg, sid, text=KARMEN_TEXT):
    """Independently parse + build the SAME order the live/shadow code under test will —
    used to seed a realistic `edi_sent` row (either "n8n already sent this" or "we already
    uploaded this") without going through the worker itself."""
    catalog = snapshot_mod.load_catalog(pg, sid)
    parsed = static_parse.parse_static_order(text)
    built = static_edi.build(parsed, catalog)
    return parsed, built


# --- the default must do nothing at all -----------------------------------

def test_shadow_disabled_touches_nothing(pg):
    _msg(pg)
    _snapshot(pg)
    assert static_worker.tick(pg, _cfg()) == 0
    row = pg.execute("SELECT processing_at, processed FROM messages").fetchone()
    assert row == (None, False), "n8n still owns this message"
    assert pg.execute("SELECT count(*) FROM order_runs").fetchone()[0] == 0


def test_engine_option_only_accepts_known_values():
    with pytest.raises(ValueError):
        static_worker.resolve_engine("postgres-please")
    assert static_worker.resolve_engine("python") == "python"
    assert static_worker.resolve_engine("") == "n8n"


def test_merge_spend_adds_both_sides_together():
    """#133: the extra-content check and a fallback AI-pipeline call can BOTH spend
    money for the same message — spend.record overwrites rather than accumulates, so
    losing either side would silently under-report the run's real cost."""
    a = {"calls": 1, "cached_calls": 0, "tokens_in": 100, "tokens_cached": 0,
         "tokens_out": 20, "tokens_reasoning": 0, "cost_usd": 0.01, "model": "gpt-5.4"}
    b = {"calls": 2, "cached_calls": 1, "tokens_in": 300, "tokens_cached": 50,
         "tokens_out": 60, "tokens_reasoning": 10, "cost_usd": 0.05, "model": "gpt-5.4"}
    merged = static_worker._merge_spend(a, b)
    assert merged["calls"] == 3
    assert merged["cached_calls"] == 1
    assert merged["tokens_in"] == 400
    assert round(merged["cost_usd"], 4) == 0.06
    assert merged["model"] == "gpt-5.4"


def test_merge_spend_handles_either_side_being_absent():
    a = {"calls": 1, "cost_usd": 0.01, "model": "gpt-5.4"}
    assert static_worker._merge_spend(a, None) == a
    assert static_worker._merge_spend(None, a) == a
    assert static_worker._merge_spend(None, None) is None


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


# --- #133: engine=python — real claim, real upload, real fallback ----------

def _python_cfg(**kw):
    return _cfg(static_orders_engine="python", **kw)


def test_python_engine_uploads_and_marks_processed(pg):
    _msg(pg)
    _snapshot(pg)
    uploaded = {}

    def fake_upload(c, name, content):
        uploaded["name"], uploaded["content"] = name, content
        return True

    assert static_worker.tick(pg, _python_cfg(), upload=fake_upload) == 1
    assert uploaded["name"] == "KARMEN_12345_2026_007.txt"
    row = pg.execute("SELECT processed, processed_by, attempts FROM messages").fetchone()
    assert row == (True, "static_orders", 1)
    run = pg.execute("SELECT status, shadow FROM order_runs").fetchone()
    assert run == ("ok", False)
    sent = pg.execute(
        "SELECT customer_ean, delivery_date, filename, uploaded_at IS NOT NULL "
        "FROM edi_sent").fetchone()
    assert sent == ("8589364472007", "03.08.2026", "KARMEN_12345_2026_007.txt", True)
    ev = pg.execute(
        "SELECT workflow, stage, status FROM email_events WHERE message_id='m1'").fetchone()
    assert ev == ("static_orders", "uploaded_orion", "ok")


def test_python_engine_upload_failure_releases_the_claim_and_alerts_odoo(pg):
    _msg(pg)
    _snapshot(pg)
    posted = {}

    def failing_upload(c, name, content):
        raise OSError("connection refused")

    def fake_post(c, html):
        posted["html"] = html
        return {"id": 1}

    assert static_worker.tick(pg, _python_cfg(), upload=failing_upload, post=fake_post) == 1
    assert "zlyhalo" in posted["html"]
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 0, \
        "a failed upload must release its claim, never leave an orphan"
    row = pg.execute("SELECT processed, processed_by FROM messages").fetchone()
    assert row == (True, "static_orders"), \
        "an upload error is reported to Odoo, not silently retried forever"
    run = pg.execute("SELECT status FROM order_runs").fetchone()
    assert run[0] == "error"
    ev = pg.execute("SELECT stage, status FROM email_events WHERE message_id='m1'").fetchone()
    assert ev == ("review", "error")


def test_python_engine_upload_and_odoo_alert_both_fail_still_releases_the_claim(pg):
    """Review finding on PR #181: the nested try/except around the failure-alert post
    had zero test coverage — an Odoo outage must never leave an orphaned edi_sent claim
    or crash the tick, on top of the upload itself already having failed."""
    _msg(pg)
    _snapshot(pg)

    def failing_upload(c, name, content):
        raise OSError("connection refused")

    def failing_post(c, html):
        raise RuntimeError("odoo unreachable")

    assert static_worker.tick(
        pg, _python_cfg(), upload=failing_upload, post=failing_post) == 1
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 0, \
        "the claim must still be released even when the follow-up alert ALSO fails"
    run = pg.execute("SELECT status FROM order_runs").fetchone()
    assert run[0] == "error"
    ev = pg.execute("SELECT stage, status FROM email_events WHERE message_id='m1'").fetchone()
    assert ev == ("review", "error")


def test_python_engine_duplicate_edi_sent_is_skipped_never_reuploaded(pg):
    sid = _snapshot(pg)
    parsed, built = _static_built(pg, sid)
    assert edi.claim_send(pg, built.store_ean, parsed["deliveryDate"], built.content,
                          built.filename)
    edi.confirm_sent(pg, built.store_ean, parsed["deliveryDate"], built.content)
    _msg(pg)

    calls = []
    assert static_worker.tick(
        pg, _python_cfg(), upload=lambda c, n, ct: calls.append(n) or True) == 1
    assert calls == [], "an already-confirmed upload must never be repeated"
    row = pg.execute("SELECT processed FROM messages").fetchone()
    assert row[0] is True
    ev = pg.execute("SELECT stage FROM email_events WHERE message_id='m1'").fetchone()
    assert ev[0] == "duplicate_skip", "must never look like a fresh uploaded_orion event"
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 1


def test_python_engine_concurrent_unconfirmed_claim_is_not_reported_as_already_sent(pg):
    """#271: precise code-path demonstration of the "theoretically same gap"
    `.claude/rules/orders-corpus.md` documents for `static_worker.py`'s duplicate-skip
    logging (mirrors the #216 TOCTOU fix `desadv.claim_send_or_identify()` made for the
    DL ledger). `edi.claim_send()` only ever returns a bool — it cannot tell `_ship`
    apart "a CONFIRMED upload already exists" (a genuine duplicate, safe to skip
    silently) from "another claim is merely fresh/UNCONFIRMED" (someone else may be
    mid-upload RIGHT NOW; nothing has actually reached ORION yet). Today `_ship` reports
    BOTH the same way: "EDI už bolo odoslané skôr" ("EDI was already sent before") —
    which is simply FALSE for the second case.

    Simulates the race directly (no threads needed — `edi.claim_send`'s own atomicity
    is already covered by `test_orders_edi.py`; this test is about what `_ship` REPORTS
    once it loses the race, not about the race itself): seed a fresh, UNCONFIRMED claim
    for the exact same document (as a concurrent in-flight run would hold, seconds
    before its own `confirm_sent()`), then let `static_worker.tick` process a message
    that resolves to the SAME document and observe how the resulting skip is logged.
    """
    sid = _snapshot(pg)
    parsed, built = _static_built(pg, sid)
    assert edi.claim_send(pg, built.store_ean, parsed["deliveryDate"], built.content,
                          built.filename)
    assert pg.execute("SELECT uploaded_at FROM edi_sent").fetchone()[0] is None, \
        "the seeded claim must be UNCONFIRMED — this test is the in-flight race, not " \
        "the already-covered confirmed-duplicate case"
    _msg(pg)

    calls = []
    assert static_worker.tick(
        pg, _python_cfg(), upload=lambda c, n, ct: calls.append(n) or True) == 1
    assert calls == [], "must never double-upload while another claim is fresh"

    ev = pg.execute(
        "SELECT stage, outcome, detail FROM email_events WHERE message_id='m1'").fetchone()
    stage, outcome, detail = ev
    assert stage == "duplicate_skip"
    assert "už bolo odoslané" not in outcome, (
        "a fresh, UNCONFIRMED claim held by someone else must never be reported as "
        f"'already sent' when nothing has actually reached ORION yet — got: {outcome!r}")
    assert detail.get("confirmed") is False, (
        "the event must record this was an unconfirmed in-flight claim, not a "
        "genuinely confirmed duplicate")


def test_python_engine_missing_ean_falls_back_to_ai_pipeline(pg):
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
    seen = {}

    def fake_pipeline(conn, cfg, message, snapshot_id):
        seen["message_id"], seen["today"] = message["message_id"], message.get("today")
        return {"status": "ok", "items": []}

    assert static_worker.tick(pg, _python_cfg(), pipeline=fake_pipeline) == 1
    assert seen["message_id"] == "m1"
    assert seen["today"], "the AI pipeline needs a real 'today' for its date fences (#117)"
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 0, \
        "no silent partial upload — the whole order goes to AI, never a skip-and-ship"
    row = pg.execute("SELECT processed, processed_by FROM messages").fetchone()
    assert row == (True, "static_orders")
    fallback_note = pg.execute(
        "SELECT result->>'static_fallback' FROM order_runs").fetchone()[0]
    assert "EAN" in fallback_note


def test_python_engine_unparseable_text_falls_back_to_ai_pipeline(pg):
    _msg(pg, text=UNPARSEABLE_TEXT)
    _snapshot(pg)
    calls = []
    assert static_worker.tick(
        pg, _python_cfg(),
        pipeline=lambda c, cf, m, s: calls.append(m["message_id"])
                                     or {"status": "review", "items": []}) == 1
    assert calls == ["m1"]


def test_python_engine_photo_order_falls_back_to_ai_pipeline(pg):
    _msg(pg, text="krátky text", has_attachments=True)
    _snapshot(pg)
    calls = []
    assert static_worker.tick(
        pg, _python_cfg(),
        pipeline=lambda c, cf, m, s: calls.append(1) or {"status": "review", "items": []}) == 1
    assert calls == [1]


def test_python_engine_missing_prev_number_falls_back_to_ai_pipeline(pg):
    """`static_edi.build`'s own hard-fail guard (missing prevNumber) is a header defect,
    not an unresolved item — it must ALSO fall back, never crash the tick."""
    _msg(pg, text=NO_PREV_TEXT)
    _snapshot(pg)
    calls = []
    assert static_worker.tick(
        pg, _python_cfg(),
        pipeline=lambda c, cf, m, s: calls.append(1) or {"status": "ok", "items": []}) == 1
    assert calls == [1]
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 0


def test_python_engine_empty_order_marks_processed_no_fallback_no_upload(pg):
    _msg(pg, text=EMPTY_ORDER_TEXT)
    _snapshot(pg)
    calls = []
    assert static_worker.tick(
        pg, _python_cfg(),
        pipeline=lambda *a, **k: calls.append("fallback") or {"status": "ok"},
        upload=lambda *a, **k: calls.append("upload") or True) == 1
    assert calls == [], "an empty order (BEZ OBJEDNAVKY) must neither fall back nor upload"
    row = pg.execute("SELECT processed, processed_by FROM messages").fetchone()
    assert row == (True, "static_orders")
    ev = pg.execute(
        "SELECT stage, workflow FROM email_events WHERE message_id='m1'").fetchone()
    assert ev == ("empty_order", "static_orders")


# --- #133 "ZMENA ROZHODNUTIA" / "DOPLNENIE ROZHODNUTIA": extra-content + digest -------

class _FakeLlmClient:
    """Mirrors `llm.Client`'s interface (`json_call`/`spend`) — never a real API call."""

    def __init__(self, actionable=False, reason="", cost=0.02):
        self.actionable = actionable
        self.reason = reason
        self.cost = cost
        self.calls = 0
        self.seen_user = None

    def json_call(self, system, user, schema, name="result"):
        self.calls += 1
        self.seen_user = user
        return {"actionable": self.actionable, "reason": self.reason}

    def spend(self):
        return {"calls": self.calls, "cached_calls": 0, "tokens_in": 100, "tokens_cached": 0,
                "tokens_out": 20, "tokens_reasoning": 0, "cost_usd": self.cost,
                "model": "gpt-5.4"}


def test_python_engine_plain_template_order_costs_no_llm_call_and_joins_the_digest(pg):
    """The pre-filter is what kills the noise: a pure-template mail must never even
    reach the LLM, and a clean upload with nothing extra joins the digest instead of
    posting its own message."""
    _msg(pg)
    _snapshot(pg)
    client = _FakeLlmClient()
    posted = []
    assert static_worker.tick(
        pg, _python_cfg(), upload=lambda *a, **k: True,
        post=lambda c, h: posted.append(h) or {"id": 1}, llm_client=client) == 1
    assert client.calls == 0, "a template-only mail must never trigger the LLM check"
    assert posted == [], "a clean upload with no note must not post its own message"
    pending = pg.execute(
        "SELECT message_id, filename FROM static_order_digest "
        "WHERE flushed_at IS NULL").fetchone()
    assert pending == ("m1", "KARMEN_12345_2026_007.txt")
    run = pg.execute("SELECT calls, cost_usd FROM order_runs").fetchone()
    assert run == (0, 0)


def test_python_engine_actionable_extra_content_posts_immediately_and_skips_the_digest(pg):
    text = KARMEN_TEXT + "\nProsim doructe tento tyzden na inu adresu, sme zatvoreni.\n"
    _msg(pg, text=text)
    _snapshot(pg)
    client = _FakeLlmClient(actionable=True, reason="zmena miesta dodania")
    posted = []
    assert static_worker.tick(
        pg, _python_cfg(), upload=lambda *a, **k: True,
        post=lambda c, h: posted.append(h) or {"id": 1}, llm_client=client) == 1
    assert client.calls == 1
    assert "inu adresu" in client.seen_user.lower() or "doructe" in client.seen_user.lower()
    assert len(posted) == 1, "an actionable note must post its own immediate message"
    assert "dopísal" in posted[0]
    assert "inu adresu" in posted[0].lower() or "doructe" in posted[0].lower()
    # the order itself still ships normally — the notification never blocks it
    row = pg.execute("SELECT processed FROM messages").fetchone()
    assert row[0] is True
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 1
    assert pg.execute(
        "SELECT count(*) FROM static_order_digest").fetchone()[0] == 0, \
        "an order with an actionable note must never join the digest"
    run = pg.execute("SELECT calls, cost_usd FROM order_runs").fetchone()
    assert run[0] == 1 and float(run[1]) == 0.02, \
        "the extra-content LLM call's cost must be recorded"


def test_python_engine_an_undelivered_extra_content_alert_is_marked_error_not_ok(pg):
    """Review finding on PR #182: a failed/undelivered actionable-note alert must never
    be logged as a genuine "ok" success — that would silently hide exactly the miss this
    feature exists to catch. `post` returning None mirrors report.post_from_config's
    "Odoo not configured" contract; a raised exception is the other failure shape."""
    text = KARMEN_TEXT + "\nProsim zmente adresu dodania, sme v inych priestoroch.\n"
    _msg(pg, text=text)
    _snapshot(pg)
    client = _FakeLlmClient(actionable=True, reason="zmena adresy")
    assert static_worker.tick(
        pg, _python_cfg(), upload=lambda *a, **k: True,
        post=lambda c, h: None, llm_client=client) == 1
    ev = pg.execute(
        "SELECT stage, status, detail->>'delivered' FROM email_events "
        "WHERE message_id='m1' AND stage='extra_content'").fetchone()
    assert ev == ("extra_content", "error", "false")
    # the order itself still shipped and is still processed — this is a notification
    # miss, never a reason to hold the order or crash the tick
    row = pg.execute("SELECT processed FROM messages").fetchone()
    assert row[0] is True


def test_python_engine_extra_content_alert_exception_also_marks_error(pg):
    text = KARMEN_TEXT + "\nProsim zmente adresu dodania, sme v inych priestoroch.\n"
    _msg(pg, text=text)
    _snapshot(pg)
    client = _FakeLlmClient(actionable=True, reason="zmena adresy")

    def failing_post(c, h):
        raise RuntimeError("odoo unreachable")

    assert static_worker.tick(
        pg, _python_cfg(), upload=lambda *a, **k: True,
        post=failing_post, llm_client=client) == 1
    ev = pg.execute(
        "SELECT status FROM email_events WHERE message_id='m1' "
        "AND stage='extra_content'").fetchone()
    assert ev == ("error",)


def test_python_engine_non_actionable_residual_is_silent(pg):
    """A stray non-actionable addition still costs the LLM call (residual existed) but
    must NOT post anything and the order still joins the digest normally."""
    text = KARMEN_TEXT + "\nAko obvykle, dakujeme.\n"
    _msg(pg, text=text)
    _snapshot(pg)
    client = _FakeLlmClient(actionable=False, reason="routine")
    posted = []
    assert static_worker.tick(
        pg, _python_cfg(), upload=lambda *a, **k: True,
        post=lambda c, h: posted.append(h) or {"id": 1}, llm_client=client) == 1
    assert client.calls == 1
    assert posted == []
    assert pg.execute(
        "SELECT count(*) FROM static_order_digest WHERE flushed_at IS NULL"
    ).fetchone()[0] == 1


def test_python_engine_extra_content_check_runs_even_when_the_order_falls_back(pg):
    """#133: 'notifikácia je doplnok, nie blokáda' — an actionable note must still be
    reported even when the ORDER itself fails to ship (falls back to AI here)."""
    text = (
        "Vyšlá objednávka č.: 1/2026\n"
        "KARMEN 7, Prešov\n"
        "Prev.:7\n"
        "Dátum vystavenia: 01.08.2026\n"
        "Termín dodávky: 03.08.2026\n"
        "Množstvo\n"
        "Neznamy produkt bez EAN 5,000 ks\n"
        "Nákupná cena spolu\n"
        "\nMimochodom, prosim zmente termin dodania na buduci tyzden.\n"
    )
    _msg(pg, text=text)
    _snapshot(pg)
    client = _FakeLlmClient(actionable=True, reason="zmena termínu")
    posted = []
    fallback_calls = []
    assert static_worker.tick(
        pg, _python_cfg(),
        pipeline=lambda c, cf, m, s: fallback_calls.append(1) or {"status": "review", "items": []},
        post=lambda c, h: posted.append(h) or {"id": 1}, llm_client=client) == 1
    assert fallback_calls == [1], "the order still falls back (unresolved EAN)"
    assert len(posted) == 1, "the actionable note is reported regardless of ship outcome"
    assert "termin" in posted[0].lower() or "termín" in posted[0].lower() \
        or "buduci" in posted[0].lower()


def _karmen_text(order_no: str) -> str:
    """A distinct order number keeps each generated EDI's content (and therefore its
    edi_sent dedup identity) genuinely different from the others."""
    return (
        f"Vyšlá objednávka č.: {order_no}/2026\n"
        "KARMEN 7, Prešov\n"
        "Prev.:7\n"
        "Dátum vystavenia: 01.08.2026\n"
        "Termín dodávky: 03.08.2026\n"
        "Množstvo\n"
        "8588001800013 Rožok štandart 50g 10,000 ks 0,50\n"
        "Nákupná cena spolu\n"
    )


def test_python_engine_digest_batch_flushes_across_ticks(pg):
    _snapshot(pg)
    posted = []
    cfg = _python_cfg(static_digest_batch_size=3)
    for i in range(3):
        _msg(pg, mid=f"m{i}", text=_karmen_text(str(1000 + i)))
        assert static_worker.tick(
            pg, cfg, upload=lambda *a, **k: True,
            post=lambda c, h: posted.append(h) or {"id": 1},
            llm_client=_FakeLlmClient()) == 1
    assert len(posted) == 1, "the batch must flush exactly once, on the 3rd clean upload"
    assert "3 objednávky" in posted[0]
    assert pg.execute(
        "SELECT count(*) FROM static_order_digest WHERE flushed_at IS NULL"
    ).fetchone()[0] == 0


def test_python_engine_digest_idle_flush_fires_on_a_later_tick(pg):
    """The idle-flush check runs unconditionally at the top of every tick — a queued,
    stale digest flushes even on a tick that finds no new message to process."""
    _snapshot(pg)
    _msg(pg, mid="m0", text=KARMEN_TEXT)
    posted = []
    cfg = _python_cfg()
    assert static_worker.tick(
        pg, cfg, upload=lambda *a, **k: True,
        post=lambda c, h: posted.append(h) or {"id": 1}, llm_client=_FakeLlmClient()) == 1
    assert posted == [], "not stale yet, and below the batch size"
    pg.execute("UPDATE static_order_digest SET created_at = now() - interval '61 minutes'")
    # a later tick with nothing new to claim still flushes the stale digest
    assert static_worker.tick(pg, cfg, upload=lambda *a, **k: True,
                              post=lambda c, h: posted.append(h) or {"id": 1}) == 0
    assert len(posted) == 1
    assert "1 objednávka" in posted[0]


def test_python_engine_duplicate_skip_never_joins_the_digest(pg):
    sid = _snapshot(pg)
    parsed, built = _static_built(pg, sid)
    edi.claim_send(pg, built.store_ean, parsed["deliveryDate"], built.content, built.filename)
    edi.confirm_sent(pg, built.store_ean, parsed["deliveryDate"], built.content)
    _msg(pg)
    posted = []
    assert static_worker.tick(
        pg, _python_cfg(), upload=lambda *a, **k: True,
        post=lambda c, h: posted.append(h) or {"id": 1}, llm_client=_FakeLlmClient()) == 1
    assert posted == []
    assert pg.execute("SELECT count(*) FROM static_order_digest").fetchone()[0] == 0


def test_python_engine_a_crashing_fallback_releases_the_claim(pg):
    _msg(pg, text=UNPARSEABLE_TEXT)
    _snapshot(pg)

    def boom(*a, **k):
        raise RuntimeError("llm exploded")

    assert static_worker.tick(pg, _python_cfg(), pipeline=boom) == 0
    row = pg.execute("SELECT processing_at, processed FROM messages").fetchone()
    assert row == (None, False)
    run = pg.execute("SELECT status, error FROM order_runs").fetchone()
    assert run[0] == "error" and "exploded" in run[1]


def test_python_engine_a_held_order_from_fallback_is_not_marked_processed(pg):
    _msg(pg, text=UNPARSEABLE_TEXT)
    _snapshot(pg)

    def pipeline(conn, cfg, message, snapshot_id):
        conn.execute(
            """INSERT INTO held_orders (message_id, customer_ean, customer_name,
                                        delivery_date, question_ids)
               VALUES (%s, '2000000000864', 'Pekáreň', '04.08.2026', ARRAY[1]::bigint[])""",
            (message["message_id"],))
        return {"status": "held", "items": []}

    assert static_worker.tick(pg, _python_cfg(), pipeline=pipeline) == 1
    row = pg.execute("SELECT processed, processing_at FROM messages").fetchone()
    assert row[0] is False, "a held order must not mark the message processed"


def test_python_engine_fallback_forces_live_cfg_even_when_ai_orders_shadow_is_on(pg):
    """CRITICAL review finding on PR #181: `orders_shadow` is the AI ENGINE's OWN,
    completely unrelated, still-undecided shadow toggle (config.py). Before this fix, an
    unmodified `cfg` handed straight to `pipeline.run()` meant that if an operator ever
    left `orders_shadow=True` (e.g. while shadow-testing the AI engine's own future
    cutover) at the same time `static_orders_engine=python` is live, EVERY static-order
    fallback would silently run the AI pipeline in SHADOW mode — no claim, no hold, no
    teach, no upload, no Odoo post, no event — and the message would still be marked
    processed (nothing was ever held), permanently losing the order with zero trace.

    The fake pipeline below models the real `pipeline._run`'s cfg-sensitivity (shadow=
    True -> writes nothing; shadow=False -> holds for real) so this test actually
    exercises the cfg the fallback constructs, not just a fallback that ignores cfg."""
    _msg(pg, text=UNPARSEABLE_TEXT)
    _snapshot(pg)
    seen = {}

    def fake_pipeline(conn, cfg, message, snapshot_id):
        seen["orders_shadow"] = cfg.orders_shadow
        if cfg.orders_shadow:
            return {"status": "review", "items": [], "shadow": True}
        conn.execute(
            """INSERT INTO held_orders (message_id, customer_ean, customer_name,
                                        delivery_date, question_ids)
               VALUES (%s, '2000000000864', 'Pekáreň', '04.08.2026', ARRAY[1]::bigint[])""",
            (message["message_id"],))
        return {"status": "held", "items": [], "shadow": False}

    cfg = _python_cfg(orders_shadow=True)   # the AI engine's own, unrelated flag left ON
    assert static_worker.tick(pg, cfg, pipeline=fake_pipeline) == 1
    assert seen["orders_shadow"] is False, \
        "the fallback must force orders_shadow=False, never trust the caller's own cfg"
    row = pg.execute("SELECT processed FROM messages").fetchone()
    assert row[0] is False, \
        "a real held order (proof the fallback ran LIVE) must not mark processed"


def test_python_engine_never_reclaims_a_message_with_an_open_held_order(pg):
    """Mirrors worker.py's #93 guard — a fallback that held this exact message on an
    earlier attempt must keep it out of the claim query, however stale the claim looks."""
    _msg(pg)
    _snapshot(pg)
    pg.execute("UPDATE messages SET processing_at = now() - interval '31 minutes'")
    pg.execute(
        """INSERT INTO held_orders (message_id, customer_ean, customer_name, delivery_date,
                                    question_ids)
           VALUES ('m1', '2000000000864', 'Pekáreň', '04.08.2026', ARRAY[1]::bigint[])""")
    assert static_worker.tick(pg, _python_cfg(), upload=lambda *a, **k: True) == 0


def test_python_engine_never_reclaims_a_message_with_an_open_mail_question(pg):
    from app.orders import teach
    _msg(pg, text=UNPARSEABLE_TEXT)
    _snapshot(pg)
    pg.execute("UPDATE messages SET processing_at = now() - interval '31 minutes'")
    teach.ask_mail(pg, message_id="m1", sender_email="x@y.sk", subject="Info",
                   reason="test")
    assert static_worker.tick(pg, _python_cfg(), upload=lambda *a, **k: True) == 0


def test_python_engine_a_stale_claim_is_reclaimed(pg):
    _msg(pg)
    _snapshot(pg)
    pg.execute("UPDATE messages SET processing_at = now() - interval '31 minutes'")
    assert static_worker.tick(pg, _python_cfg(), upload=lambda *a, **k: True) == 1


def test_other_categories_are_never_touched_by_python_engine(pg):
    _msg(pg, mid="ai1", category="ai_orders")
    _snapshot(pg)
    assert static_worker.tick(pg, _python_cfg(), upload=lambda *a, **k: True) == 0


# --- #133: shadow diff against the REAL n8n `edi_sent` row -----------------

def test_shadow_diff_matches_the_real_n8n_upload(pg):
    sid = _snapshot(pg)
    parsed, built = _static_built(pg, sid)
    edi.claim_send(pg, built.store_ean, parsed["deliveryDate"], built.content, built.filename)
    edi.confirm_sent(pg, built.store_ean, parsed["deliveryDate"], built.content)
    _msg(pg)
    assert static_worker.tick(pg, _cfg(static_orders_shadow=True)) == 1
    run = pg.execute(
        "SELECT result->>'shadow_verdict', result->>'shadow_note' FROM order_runs").fetchone()
    assert run == ("match", "")


def test_shadow_diff_flags_a_real_mismatch(pg):
    sid = _snapshot(pg)
    parsed, built = _static_built(pg, sid)
    edi.claim_send(pg, built.store_ean, parsed["deliveryDate"], "INY OBSAH", "other.txt")
    edi.confirm_sent(pg, built.store_ean, parsed["deliveryDate"], "INY OBSAH")
    _msg(pg)
    assert static_worker.tick(pg, _cfg(static_orders_shadow=True)) == 1
    verdict = pg.execute("SELECT result->>'shadow_verdict' FROM order_runs").fetchone()[0]
    assert verdict == "mismatch"


def test_shadow_diff_with_no_n8n_output_yet_is_inconclusive_not_a_mismatch(pg):
    _snapshot(pg)
    _msg(pg)
    assert static_worker.tick(pg, _cfg(static_orders_shadow=True)) == 1
    verdict = pg.execute("SELECT result->>'shadow_verdict' FROM order_runs").fetchone()[0]
    assert verdict == "no_n8n_output"


def test_shadow_diff_a_partial_ean_miss_is_would_fallback_not_diffed(pg):
    text = (
        "Vyšlá objednávka č.: 2/2026\n"
        "KARMEN 7, Prešov\n"
        "Prev.:7\n"
        "Dátum vystavenia: 01.08.2026\n"
        "Termín dodávky: 03.08.2026\n"
        "Množstvo\n"
        "8588001800013 Rožok štandart 50g 10,000 ks 0,50\n"
        "Neznamy produkt 5,000 ks\n"
        "Nákupná cena spolu\n"
    )
    _msg(pg, text=text)
    _snapshot(pg)
    assert static_worker.tick(pg, _cfg(static_orders_shadow=True)) == 1
    run = pg.execute("SELECT status, result->>'shadow_verdict' FROM order_runs").fetchone()
    assert run == ("partial", "would_fallback")


def test_shadow_diff_a_full_parse_failure_is_would_fallback(pg):
    _msg(pg, text=UNPARSEABLE_TEXT)
    _snapshot(pg)
    assert static_worker.tick(pg, _cfg(static_orders_shadow=True)) == 1
    verdict = pg.execute("SELECT result->>'shadow_verdict' FROM order_runs").fetchone()[0]
    assert verdict == "would_fallback"


def test_shadow_diff_empty_order_has_its_own_verdict(pg):
    _msg(pg, text=EMPTY_ORDER_TEXT)
    _snapshot(pg)
    assert static_worker.tick(pg, _cfg(static_orders_shadow=True)) == 1
    verdict = pg.execute("SELECT result->>'shadow_verdict' FROM order_runs").fetchone()[0]
    assert verdict == "empty_order"


def test_shadow_never_claims_even_when_a_real_n8n_row_already_exists(pg):
    """The load-bearing shadow guarantee (#132) must still hold once the diff reads
    `edi_sent` — reading that table must never turn into claiming the message."""
    sid = _snapshot(pg)
    parsed, built = _static_built(pg, sid)
    edi.claim_send(pg, built.store_ean, parsed["deliveryDate"], built.content, built.filename)
    edi.confirm_sent(pg, built.store_ean, parsed["deliveryDate"], built.content)
    _msg(pg)
    assert static_worker.tick(pg, _cfg(static_orders_shadow=True)) == 1
    state = pg.execute(
        "SELECT processing_at, processed, processed_by FROM messages").fetchone()
    assert state == (None, False, None)
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 1, \
        "shadow must not add a second edi_sent row of its own"


def test_python_engine_crash_on_final_attempt_surfaces_a_real_diagnostic(pg, monkeypatch):
    """#330: a static order whose live run crashes with an UNEXPECTED exception on its
    FINAL allowed attempt must surface a real, diagnosable error carrying the exception
    TYPE + MESSAGE + stage — never vanish silently the way the pre-fix engine did (it
    only wrote `order_runs.error` + a log line, then `_claim`'s `attempts < MAX_ATTEMPTS`
    guard quietly stopped reprocessing it, with nothing on `proc_status`/`email_events`).
    This is the Python-engine analog of the n8n "Chyba: neznáma [line 150]" class of
    undiagnosable stuck error the ticket is about."""
    _msg(pg)
    _snapshot(pg)
    # attempts 4 -> `_claim` increments to 5 == MAX_ATTEMPTS, so after this crash the
    # message can never be re-claimed; it must NOT disappear with no human diagnostic.
    pg.execute("UPDATE messages SET attempts = %s WHERE message_id = 'm1'",
               (worker.MAX_ATTEMPTS - 1,))

    def boom(*a, **k):
        raise RuntimeError("catalog snapshot vanished mid-run")

    monkeypatch.setattr(static_parse, "parse_static_order", boom)

    assert static_worker.tick(pg, _python_cfg()) == 0
    ev = pg.execute(
        "SELECT status, outcome, detail FROM email_events "
        "WHERE message_id = 'm1' AND status = 'error'").fetchone()
    assert ev is not None, (
        "a static order crashing on its final attempt must surface a real error event, "
        "not vanish silently")
    status, outcome, detail = ev
    assert "RuntimeError" in outcome, outcome
    assert "catalog snapshot vanished mid-run" in outcome, outcome
    assert "neznáma" not in outcome.lower(), outcome
    assert "RuntimeError" in (detail.get("error") or ""), detail
    assert pg.execute(
        "SELECT proc_status FROM messages WHERE message_id = 'm1'").fetchone()[0] == "error"


# --- #372: safe automatic ORION upload retry for the static engine (port of #239) -----
#
# The static EDI filename (static_edi._filename) is `KARMEN_12345_2026_007.txt` for
# KARMEN_TEXT — fully deterministic, NO per-attempt timestamp — so it is the document's
# stable identity on ORION. These tests drive the SAME injected seams the DL retry tests
# use (`upload`/`post`/`list_dirs`), mocking ONLY the SFTP boundary; everything else is
# real code, real Postgres (`edi_sent` ledger, `email_events`, `messages`).

_STATIC_FILENAME = "KARMEN_12345_2026_007.txt"


def _orion_dirs(*, in_=(), arch=(), unconfirmed=(), present=None):
    """`upload.list_dirs()`'s return shape. Static orders upload to `in` (never `in_DL`);
    once imported they move to the shared `archCodex` (possibly Z-/Z-Z--renamed); a failed
    import lands in `unconfirmed`. Each folder can be seeded independently (to isolate which
    folder a presence match came from); `present=[...]` is a convenience that seeds BOTH
    `in` and `archCodex` (the common "somewhere on ORION" case)."""
    if present is not None:
        in_, arch = present, present
    return {"in": set(in_), "in_DL": set(), "archCodex": set(arch),
            "unconfirmed": set(unconfirmed)}


def test_python_engine_transient_upload_confirms_when_already_landed(pg):
    """#372 branch 1 (bytes-landed-reply-lost): a TRANSIENT SFTP failure whose presence
    check proves the exact static filename is ALREADY on ORION (under a Z- archCodex
    rename) must CONFIRM the same claim, never re-upload — the v0.9.70 duplicate-delivery
    class this whole port exists to prevent."""
    _msg(pg)
    _snapshot(pg)
    tries, list_calls, posted = [], [], []

    def timed_out_upload(c, name, content):
        tries.append(name)
        raise OSError("connection timed out")

    def fake_list_dirs(cfg):
        list_calls.append(cfg)
        return _orion_dirs(present=[f"Z-{_STATIC_FILENAME}"])

    assert static_worker.tick(
        pg, _python_cfg(), upload=timed_out_upload,
        post=lambda c, h: posted.append(h) or {"id": 1},
        list_dirs=fake_list_dirs, llm_client=_FakeLlmClient()) == 1
    assert len(tries) == 1, "a landed document must NEVER be re-uploaded"
    assert len(list_calls) == 1, "the presence check runs exactly once"
    sent = pg.execute(
        "SELECT filename, uploaded_at IS NOT NULL FROM edi_sent").fetchone()
    assert sent == (_STATIC_FILENAME, True), "the SAME claim must be CONFIRMED, not released"
    assert posted == [], "a genuine (reply-lost) success must not post an upload-failure alert"
    row = pg.execute("SELECT processed, processed_by FROM messages").fetchone()
    assert row == (True, "static_orders")
    ev = pg.execute(
        "SELECT stage, status FROM email_events WHERE message_id='m1' "
        "AND stage='uploaded_orion'").fetchone()
    assert ev == ("uploaded_orion", "ok")


def test_python_engine_transient_upload_retries_once_when_absent(pg):
    """#372 branch 2 (absent → safe retry): the presence check proves the document is
    genuinely NOT on ORION → exactly ONE bounded retry with the SAME claim held throughout
    (never release-then-reclaim, which is what removed the anti-duplicate protection)."""
    _msg(pg)
    _snapshot(pg)
    tries, list_calls, posted = [], [], []

    def flaky_upload(c, name, content):
        tries.append(name)
        if len(tries) == 1:
            raise OSError("connection timed out")
        return True

    def fake_list_dirs(cfg):
        list_calls.append(cfg)
        return _orion_dirs()

    assert static_worker.tick(
        pg, _python_cfg(), upload=flaky_upload,
        post=lambda c, h: posted.append(h) or {"id": 1},
        list_dirs=fake_list_dirs, llm_client=_FakeLlmClient()) == 1
    assert len(tries) == 2, "exactly one retry, never a loop"
    assert len(list_calls) == 1, "the presence check runs once, before the retry"
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 1, "one claim row"
    sent = pg.execute("SELECT uploaded_at IS NOT NULL FROM edi_sent").fetchone()
    assert sent == (True,), "the SAME claim survived (never released) and got confirmed"
    assert posted == []
    assert pg.execute("SELECT processed FROM messages").fetchone()[0] is True


def test_python_engine_transient_upload_retry_also_fails_alerts_and_releases(pg):
    """#372 branch 3 (retry also fails): the single retry is BOUNDED — when it also
    fails, fall back to today's release + Odoo alert + error-event path, unchanged."""
    _msg(pg)
    _snapshot(pg)
    tries, posted = [], []

    def always_fail(c, name, content):
        tries.append(name)
        raise OSError("connection timed out")

    assert static_worker.tick(
        pg, _python_cfg(), upload=always_fail,
        post=lambda c, h: posted.append(h) or {"id": 1},
        list_dirs=lambda cfg: _orion_dirs(), llm_client=_FakeLlmClient()) == 1
    assert len(tries) == 2, "the original attempt plus exactly one bounded retry"
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 0, \
        "a genuinely failed retry releases the claim, same as today's no-retry path"
    assert len(posted) == 1 and "zlyhalo" in posted[0]
    assert pg.execute("SELECT status FROM order_runs").fetchone()[0] == "error"
    ev = pg.execute(
        "SELECT stage, status FROM email_events WHERE message_id='m1' "
        "AND stage='review'").fetchone()
    assert ev == ("review", "error")


def test_python_engine_non_transient_upload_never_retries(pg):
    """#372 branch 4 (non-transient): a `PermissionError` is a genuine ORION-side refusal,
    never retryable — it keeps today's behaviour byte-for-byte, and must NOT even consult
    ORION presence."""
    _msg(pg)
    _snapshot(pg)
    tries, list_calls, posted = [], [], []

    def perm_fail(c, name, content):
        tries.append(name)
        raise PermissionError("Access is denied")

    def fake_list_dirs(cfg):
        list_calls.append(cfg)
        return _orion_dirs()

    assert static_worker.tick(
        pg, _python_cfg(), upload=perm_fail,
        post=lambda c, h: posted.append(h) or {"id": 1},
        list_dirs=fake_list_dirs, llm_client=_FakeLlmClient()) == 1
    assert len(tries) == 1, "a non-transient failure must never retry"
    assert list_calls == [], "a non-transient failure must never even check ORION presence"
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 0
    assert len(posted) == 1 and "zlyhalo" in posted[0]
    ev = pg.execute(
        "SELECT stage, status FROM email_events WHERE message_id='m1' "
        "AND stage='review'").fetchone()
    assert ev == ("review", "error")


def test_python_engine_bare_eoferror_is_treated_transient_the_msg_8447_incident(pg):
    """#372 THE LIVE INCIDENT (msg 8447): the failure was a BARE `EOFError()` whose
    `str()` is empty — `dl_retry.TRANSIENT_RE` structurally CANNOT match it. It MUST still
    be classified transient (by exception TYPE), or the fix misses the very incident it
    exists for. Here the reply was lost but the bytes landed → confirm, never re-upload."""
    _msg(pg)
    _snapshot(pg)
    tries, list_calls, posted = [], [], []

    def eof_upload(c, name, content):
        tries.append(name)
        raise EOFError()

    def fake_list_dirs(cfg):
        list_calls.append(cfg)
        return _orion_dirs(present=[f"Z-{_STATIC_FILENAME}"])

    assert static_worker.tick(
        pg, _python_cfg(), upload=eof_upload,
        post=lambda c, h: posted.append(h) or {"id": 1},
        list_dirs=fake_list_dirs, llm_client=_FakeLlmClient()) == 1
    assert len(list_calls) == 1, \
        "a bare EOFError() must be transient → the presence check must run"
    assert len(tries) == 1, "the landed bytes must be confirmed, never re-uploaded"
    sent = pg.execute("SELECT uploaded_at IS NOT NULL FROM edi_sent").fetchone()
    assert sent == (True,), "the reply-lost EOFError bytes-landed case must CONFIRM the claim"
    assert posted == []
    assert pg.execute("SELECT processed FROM messages").fetchone()[0] is True


def test_python_engine_transient_upload_never_trusts_a_filename_collision(pg):
    """#372 collision guard (the #239 mirror — silent LOSS, not duplication): the static
    filename encodes partner/order/store, NOT content or delivery date, so a same-order-
    number correction can produce the SAME filename with DIFFERENT content. A presence
    match on that name must NOT be trusted to confirm OUR (different) order — that would
    silently drop it. Seed a DIFFERENT already-confirmed order under the same filename,
    then a transient failure on our order must release+alert, never confirm off the
    ambiguous name."""
    sid = _snapshot(pg)
    parsed, built = _static_built(pg, sid)
    assert built.filename == _STATIC_FILENAME
    # a genuinely DIFFERENT order (different delivery date + content) already shipped under
    # the SAME filename our order will produce
    edi.claim_send(pg, built.store_ean, "99.99.9999", "INY OBSAH", built.filename)
    edi.confirm_sent(pg, built.store_ean, "99.99.9999", "INY OBSAH")
    _msg(pg)
    tries, list_calls, posted = [], [], []

    def timed_out_upload(c, name, content):
        tries.append(name)
        raise OSError("connection timed out")

    def fake_list_dirs(cfg):
        list_calls.append(cfg)
        return _orion_dirs(present=[f"Z-{built.filename}"])

    assert static_worker.tick(
        pg, _python_cfg(), upload=timed_out_upload,
        post=lambda c, h: posted.append(h) or {"id": 1},
        list_dirs=fake_list_dirs, llm_client=_FakeLlmClient()) == 1
    assert list_calls != [], "a transient failure must consult ORION presence"
    assert len(tries) == 1, "never a blind re-upload when the presence match is ambiguous"
    ours = edi.content_hash(built.content)
    assert pg.execute(
        "SELECT count(*) FROM edi_sent WHERE content_sha256 = %s", (ours,)).fetchone()[0] == 0, \
        "our different-content order must be RELEASED, never confirmed off a filename collision"
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 1, \
        "only the pre-existing DIFFERENT order's confirmed row remains"
    assert len(posted) == 1 and "zlyhalo" in posted[0]
    assert pg.execute("SELECT status FROM order_runs").fetchone()[0] == "error"


def test_matches_wire_prefix_is_byte_identical_to_the_former_private_helper():
    """#372: the Z-/Z-Z- tolerance was promoted from `desadv_edi._matches_stable_prefix`
    to the public `desadv_edi.matches_wire_prefix` so the static presence check can reuse
    it (never a second copy). The DL side must be byte-identical: the private name stays a
    working alias, and both accept name / Z-name / Z-Z-name but reject a 3rd Z-."""
    from app.orders import desadv_edi
    assert desadv_edi._matches_stable_prefix is desadv_edi.matches_wire_prefix
    base = "DESADV_743063_0100000001_"
    assert desadv_edi.matches_wire_prefix(base + "x", base)
    assert desadv_edi.matches_wire_prefix("Z-" + base + "x", base)
    assert desadv_edi.matches_wire_prefix("Z-Z-" + base + "x", base)
    assert not desadv_edi.matches_wire_prefix("Z-Z-Z-" + base + "x", base)
    assert not desadv_edi.matches_wire_prefix("something-else", base)


def test_matches_wire_name_is_exact_not_a_prefix():
    """#372 review 🔵-4: the static presence check needs EXACT-name tolerance (the whole
    static filename is the identity), not `startswith` — a `…_007.txt.bak` artifact from
    manual ORION ops must NOT count as a match (a static false-positive silently confirms
    and drops the order). Same Z-/Z-Z- tolerance as `matches_wire_prefix`, exact otherwise."""
    from app.orders import desadv_edi
    fn = "KARMEN_12345_2026_007.txt"
    assert desadv_edi.matches_wire_name(fn, fn)
    assert desadv_edi.matches_wire_name(f"Z-{fn}", fn)
    assert desadv_edi.matches_wire_name(f"Z-Z-{fn}", fn)
    assert not desadv_edi.matches_wire_name(f"Z-Z-Z-{fn}", fn)
    assert not desadv_edi.matches_wire_name(f"{fn}.bak", fn), "a longer name must NOT match"
    assert not desadv_edi.matches_wire_name(f"Z-{fn}.part", fn)
    assert not desadv_edi.matches_wire_name("something-else.txt", fn)


def test_present_on_orion_checks_each_static_folder_and_rejects_a_longer_name():
    """#372 review 🔵-5 (isolate each folder) + 🔵-4 (exact match). Static orders live in
    `in`/`archCodex`/`unconfirmed` (NEVER `in_DL`); a presence match in ANY ONE of them
    counts, and a longer/artifact name never false-positives."""
    from app.orders import static_retry
    fn = _STATIC_FILENAME
    for folder in ("in", "archCodex", "unconfirmed"):
        dirs = {"in": set(), "in_DL": set(), "archCodex": set(), "unconfirmed": set()}
        dirs[folder] = {f"Z-{fn}"}
        assert static_retry._present_on_orion(dirs, fn), f"must match a file sitting in {folder}"
    assert not static_retry._present_on_orion(
        {"in": set(), "in_DL": {fn}, "archCodex": set(), "unconfirmed": set()}, fn), \
        "in_DL is a DESADV folder, never a static-order location"
    assert not static_retry._present_on_orion(
        {"in": {f"{fn}.bak"}, "in_DL": set(), "archCodex": set(), "unconfirmed": set()}, fn), \
        "a longer artifact name must never count as a presence match"


def test_python_engine_transient_upload_distrusts_an_unconfirmed_name_collision(pg):
    """#372 review 🟡-1: an UNCONFIRMED different-content `edi_sent` row occupying the same
    filename (a run that crashed BETWEEN sftp.rename and confirm_sent — its bytes ARE on
    ORION under that name, its claim row still `uploaded_at NULL`) must ALSO be treated as
    an ambiguous collision → None → release, never confirmed off those bytes. The pre-fix
    guard filtered `uploaded_at IS NOT NULL` and would have confirmed our order off the
    stranger's bytes → silent loss."""
    sid = _snapshot(pg)
    parsed, built = _static_built(pg, sid)
    # a genuinely DIFFERENT-content claim under the SAME filename, left UNCONFIRMED
    edi.claim_send(pg, built.store_ean, "99.99.9999", "INY OBSAH", built.filename)
    assert pg.execute("SELECT uploaded_at FROM edi_sent").fetchone()[0] is None, \
        "the seeded occupant must be UNCONFIRMED — that is the case the fix adds"
    _msg(pg)
    tries, posted = [], []

    def timed_out_upload(c, name, content):
        tries.append(name)
        raise OSError("connection timed out")

    assert static_worker.tick(
        pg, _python_cfg(), upload=timed_out_upload,
        post=lambda c, h: posted.append(h) or {"id": 1},
        list_dirs=lambda cfg: _orion_dirs(present=[f"Z-{built.filename}"]),
        llm_client=_FakeLlmClient()) == 1
    assert len(tries) == 1, "an ambiguous name collision must never re-upload"
    ours = edi.content_hash(built.content)
    assert pg.execute(
        "SELECT count(*) FROM edi_sent WHERE content_sha256 = %s", (ours,)).fetchone()[0] == 0, \
        "our order must be RELEASED, never confirmed off a DIFFERENT (even unconfirmed) occupant"
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 1, \
        "only the pre-existing occupant's row remains"
    assert len(posted) == 1 and "zlyhalo" in posted[0]
    assert pg.execute("SELECT status FROM order_runs").fetchone()[0] == "error"


def test_python_engine_presence_check_failure_alerts_without_retry(pg):
    """#372 review 🟡-2 (the DL 'presence-check-unavailable' branch, ported): when the ORION
    listing itself raises — the SFTP connection that just failed the upload is very likely
    down for the follow-up listing too — NO retry is attempted (a blind retry with no
    absence proof is the v0.9.70 duplicate-delivery bug), falling back to today's
    release + alert + error-event path. `check_landed` returns None on this branch, so the
    claim must NOT be left held-without-an-alert."""
    _msg(pg)
    _snapshot(pg)
    tries, posted = [], []

    def timed_out_upload(c, name, content):
        tries.append(name)
        raise OSError("connection timed out")

    def broken_list_dirs(cfg):
        raise OSError("SFTP unavailable")

    assert static_worker.tick(
        pg, _python_cfg(), upload=timed_out_upload,
        post=lambda c, h: posted.append(h) or {"id": 1},
        list_dirs=broken_list_dirs, llm_client=_FakeLlmClient()) == 1
    assert len(tries) == 1, "no retry is safe without an absence proof"
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 0, \
        "the claim must be released, never left held with no alert"
    assert len(posted) == 1 and "zlyhalo" in posted[0]
    ev = pg.execute(
        "SELECT stage, status FROM email_events WHERE message_id='m1' "
        "AND stage='review'").fetchone()
    assert ev == ("review", "error")


# --- #373: the SINGLE retry can ITSELF end "bytes landed, reply lost" — presence-check
# --- ONCE MORE before releasing the claim, never a THIRD upload ----------------------

def test_python_engine_transient_retry_that_fails_but_landed_confirms(pg):
    """#373 (static side of the shared cross-cutting fix): the single bounded retry has the
    SAME failure mode as the first attempt — its own temp-write+rename can land the bytes on
    ORION while only the confirming reply is lost. Before #373 the retry-fail branch released
    the claim WITHOUT a second presence check, so a later manual reprocess could upload a
    SECOND copy. Now a SECOND presence check runs before releasing: found (this exact stable
    filename) → CONFIRM the SAME claim, never a third upload, never a release, never an
    alert."""
    _msg(pg)
    _snapshot(pg)
    tries, list_calls, posted = [], [], []

    def always_fail(c, name, content):
        tries.append(name)
        raise OSError("connection timed out")

    def fake_list_dirs(cfg):
        list_calls.append(cfg)
        # absent before the retry → a retry is attempted; present (retry's own bytes landed,
        # reply lost) on the SECOND check → confirm, never re-upload.
        if len(list_calls) == 1:
            return _orion_dirs()
        return _orion_dirs(present=[f"Z-{_STATIC_FILENAME}"])

    assert static_worker.tick(
        pg, _python_cfg(), upload=always_fail,
        post=lambda c, h: posted.append(h) or {"id": 1},
        list_dirs=fake_list_dirs, llm_client=_FakeLlmClient()) == 1
    assert len(tries) == 2, "the original attempt + exactly one bounded retry — never a third"
    assert len(list_calls) == 2, "presence is checked once BEFORE and once AFTER the failed retry"
    sent = pg.execute("SELECT filename, uploaded_at IS NOT NULL FROM edi_sent").fetchone()
    assert sent == (_STATIC_FILENAME, True), \
        "the retry's landed bytes CONFIRM the SAME claim, never released"
    assert posted == [], "a reply-lost success must not post an upload-failure alert"
    assert pg.execute("SELECT processed FROM messages").fetchone()[0] is True
    ev = pg.execute(
        "SELECT stage, status FROM email_events WHERE message_id='m1' "
        "AND stage='uploaded_orion'").fetchone()
    assert ev == ("uploaded_orion", "ok")


def test_python_engine_transient_retry_that_fails_landed_but_name_collision_releases(pg):
    """#373 collision guard still applies on the retry-fail path (the #239 mirror — silent
    LOSS, not duplication): the static filename encodes partner/order/store, NOT content, so
    a presence match on the SECOND check could be a DIFFERENT-content order's bytes. Seed a
    different already-confirmed order under the same filename; a transient retry that fails
    and finds that name occupied must RELEASE + alert, never confirm OUR order off the
    stranger's bytes."""
    sid = _snapshot(pg)
    parsed, built = _static_built(pg, sid)
    assert built.filename == _STATIC_FILENAME
    # a genuinely DIFFERENT-content order already shipped under the SAME filename
    edi.claim_send(pg, built.store_ean, "99.99.9999", "INY OBSAH", built.filename)
    edi.confirm_sent(pg, built.store_ean, "99.99.9999", "INY OBSAH")
    _msg(pg)
    tries, list_calls, posted = [], [], []

    def always_fail(c, name, content):
        tries.append(name)
        raise OSError("connection timed out")

    def fake_list_dirs(cfg):
        list_calls.append(cfg)
        if len(list_calls) == 1:
            return _orion_dirs()                          # absent → retry
        return _orion_dirs(present=[f"Z-{built.filename}"])  # occupied by the STRANGER

    assert static_worker.tick(
        pg, _python_cfg(), upload=always_fail,
        post=lambda c, h: posted.append(h) or {"id": 1},
        list_dirs=fake_list_dirs, llm_client=_FakeLlmClient()) == 1
    assert len(tries) == 2, "an ambiguous post-retry collision must never re-upload a third time"
    assert len(list_calls) == 2, "the retry-fail path checks presence before releasing"
    ours = edi.content_hash(built.content)
    assert pg.execute(
        "SELECT count(*) FROM edi_sent WHERE content_sha256 = %s", (ours,)).fetchone()[0] == 0, \
        "our different-content order must be RELEASED, never confirmed off a filename collision"
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 1, \
        "only the pre-existing DIFFERENT order's confirmed row remains"
    assert len(posted) == 1 and "zlyhalo" in posted[0]
    assert pg.execute("SELECT status FROM order_runs").fetchone()[0] == "error"


def test_python_engine_transient_retry_that_fails_and_still_absent_releases(pg):
    """#373 (retry-fail, still absent): the SECOND presence check proves the document is
    genuinely NOT on ORION after the failed retry → today's release + Odoo alert + error-
    event path, unchanged. The bound stays "exactly one retry" (never a third upload), and
    the second check IS attempted before releasing."""
    _msg(pg)
    _snapshot(pg)
    tries, list_calls, posted = [], [], []

    def always_fail(c, name, content):
        tries.append(name)
        raise OSError("connection timed out")

    def fake_list_dirs(cfg):
        list_calls.append(cfg)
        return _orion_dirs()   # absent BEFORE and AFTER the retry

    assert static_worker.tick(
        pg, _python_cfg(), upload=always_fail,
        post=lambda c, h: posted.append(h) or {"id": 1},
        list_dirs=fake_list_dirs, llm_client=_FakeLlmClient()) == 1
    assert len(tries) == 2, "the original attempt + exactly one bounded retry — never a third"
    assert len(list_calls) == 2, \
        "#373: the retry-fail path presence-checks ONCE MORE before releasing the claim"
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 0, \
        "a genuinely-absent failed retry releases the claim, same as today's no-retry path"
    assert len(posted) == 1 and "zlyhalo" in posted[0]
    assert pg.execute("SELECT status FROM order_runs").fetchone()[0] == "error"


def test_python_engine_transient_retry_that_fails_then_broken_presence_check_never_reuploads(pg):
    """#373 (retry-fail, then the post-retry check itself raises): the SFTP connection that
    just failed the retry is very likely down for the follow-up listing too. The second
    presence check IS attempted, `check_landed` returns None on its own failure, and this
    falls back to release + alert — NEVER a third upload."""
    _msg(pg)
    _snapshot(pg)
    tries, list_calls, posted = [], [], []

    def always_fail(c, name, content):
        tries.append(name)
        raise OSError("connection timed out")

    def fake_list_dirs(cfg):
        list_calls.append(cfg)
        if len(list_calls) == 1:
            return _orion_dirs()                    # first check: absent → retry
        raise OSError("SFTP unavailable")           # second check: connection now down

    assert static_worker.tick(
        pg, _python_cfg(), upload=always_fail,
        post=lambda c, h: posted.append(h) or {"id": 1},
        list_dirs=fake_list_dirs, llm_client=_FakeLlmClient()) == 1
    assert len(tries) == 2, "never a THIRD upload even when the post-retry check is unavailable"
    assert len(list_calls) == 2, "the second presence check was attempted before releasing"
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 0, \
        "the claim is released, never left held with no alert"
    assert len(posted) == 1 and "zlyhalo" in posted[0]
    assert pg.execute("SELECT status FROM order_runs").fetchone()[0] == "error"
