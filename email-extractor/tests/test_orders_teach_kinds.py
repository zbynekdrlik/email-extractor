"""The kind register (#164) — every question kind (item/customer/mail/date/line) shares
the same table/dashboard/dispatch machinery, declared in ONE place (`teach.KINDS`).

Two things this file pins that a new kind added later must not be able to skip:
  * every kind states `learns` (non-empty — enforced at import time too, see teach.py)
  * every kind's `apply`/`undo` actually run without raising for the shapes the dispatch
    endpoint feeds them
"""
import pytest
from psycopg.types.json import Json

from app.config import Config
from app.orders import hold, snapshot, teach

CATALOG_CSV = "GTIN,Názov,doplnok\nSLI50,Šiška džemová 50g,\n"
CUSTOMER_CSV = ("Názov organizácie,EAN kód EDI,Obec,Ulica,E-mail\n"
               "Pekáreň Testovacia s.r.o.,2000000000001,Martin,Košútka 1,sklad@pekaren.sk\n")


def _cfg(**kw):
    import os
    base = dict(pg_dsn=os.environ["PG_TEST_DSN"], data_dir="/tmp", orders_shadow=False,
                odoo_url="", odoo_api_key="", orders_channel_id=0)
    base.update(kw)
    return Config(**base)


# --- the register itself --------------------------------------------------------------

def test_every_kind_declares_a_non_empty_learns_and_an_escape_option():
    assert set(teach.KINDS) == {"item", "customer", "mail", "date", "line",
                                "dl_item", "dl_supplier"}
    for name, kind in teach.KINDS.items():
        assert kind.name == name
        assert kind.learns and kind.learns.strip(), f"{name} must state what it learns"
        assert isinstance(kind.deadline_shippable, bool)
        # every kind must accept a blank "neviem" choice without raising — the universal
        # escape hatch (#164 constraint 5) is structural, not per-kind opt-in
        kind.validate({"id": 0, "candidates": [], "context": {}, "payload": {}}, "", "sklad")


def test_only_item_is_deadline_shippable():
    """The one deliberate carry-over from before #164: an item-only hold still ships what
    matched at the deadline. Every NEW kind (customer/mail/date/line) must NOT — shipping
    an unconfirmed customer/date/line is exactly what this ticket exists to prevent."""
    assert teach.KINDS["item"].deadline_shippable is True
    for name in ("customer", "mail", "date", "line", "dl_item", "dl_supplier"):
        assert teach.KINDS[name].deadline_shippable is False, name


def _ask_one_of_each_kind(pg):
    """One real question per kind, via each kind's own `ask_*` helper (#237 review
    finding) — a dict {kind_name: qid}."""
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('mkX', 'ai_orders')")
    return {
        "item": teach.ask(pg, message_id="mkX", customer_ean="2000000000001",
                          customer_name="Zákazník A", wording="Šiška", quantity=1,
                          unit="ks", candidates=[{"gtin": "SLI50", "name": "Šiška 50g"}]),
        "customer": teach.ask_customer(pg, message_id="mkX", sender_email="a@b.sk",
                                       candidates=[], delivery_date="04.08.2026",
                                       context={}),
        "mail": teach.ask_mail(pg, message_id="mkX", sender_email="c@d.sk",
                               subject="Objednávka"),
        "date": teach.ask_date(pg, message_id="mkX", dates=["04.08.2026"], reason="r"),
        "line": teach.ask_line(pg, message_id="mkX", wording="záhadná položka",
                               quantity=1, unit="ks", reason="r"),
        "dl_item": teach.ask_dl_item(pg, message_id="mkX", supplier_ean="9000000000001",
                                     supplier_name="Dodávateľ X", wording="Great",
                                     quantity=1, unit="ks", candidates=[]),
        "dl_supplier": teach.ask_dl_supplier(pg, message_id="mkX",
                                             sender_email="e@f.sk", candidates=[]),
    }


def test_undo_clears_the_reminder_cadence_for_every_kind(pg):
    """#237 deep-review finding: `undo()` reopens a question but, before this fix, left
    `reminder_sent_at`/`escalated_at` untouched — a reopened question that had already
    been reminded (or escalated) would then never be reminded again, silently
    reintroducing the exact "stuck open, nobody notified" failure #237 exists to fix.
    Every kind's own undo path (the shared `undo()` for item/customer, `_undo_mail`/
    `_undo_date`/`_undo_line`/`_undo_dl_item`/`_undo_dl_supplier` for the rest) must
    clear both columns on reopen."""
    qids = _ask_one_of_each_kind(pg)
    ids = list(qids.values())
    pg.execute(
        "UPDATE order_questions SET reminder_sent_at = now(), escalated_at = now() "
        "WHERE id = ANY(%s)", (ids,))
    for name, qid in qids.items():
        q = teach.get(pg, qid)
        assert q["status"] == "open"  # none of these were ever answered
        reopened = teach.KINDS[name].undo(pg, q)
        assert reopened["status"] == "open", name
        row = pg.execute(
            "SELECT reminder_sent_at, escalated_at FROM order_questions WHERE id = %s",
            (qid,)).fetchone()
        assert row == (None, None), f"{name}: reminder cadence not cleared on undo"


# --- mail_rules key normalization -------------------------------------------------------

def test_subject_key_folds_away_dates_and_order_numbers():
    a = teach.subject_key("Objednávka č. 4521 na 04.08.2026")
    b = teach.subject_key("Objednávka č. 9910 na 12.09.2027")
    assert a == b, "two order confirmations differing only by date/number are ONE pattern"


def test_sender_norm_is_case_and_whitespace_insensitive():
    assert teach._sender_norm(" Sklad@Pekaren.SK ") == "sklad@pekaren.sk"


# --- date kind ---------------------------------------------------------------------------

def test_date_kind_apply_sets_the_delivery_date_and_releases(pg, monkeypatch):
    from app.orders import customer
    from app.orders.match import Decision

    # `kind.apply` (the real dispatch shape) takes no upload/post override — it always
    # goes through `hold.release_for_question`'s defaults, so a real upload must be faked.
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    sid = snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    assert sid
    matched = customer.Matched(ean_edi="2000000000001", name="Pekáreň Testovacia s.r.o.",
                               confidence=1.0, rule="llm", note="")
    order = {"deliveryDate": "04.08.2026", "orderNumber": "", "recipientGroup": "",
             "store": "", "items": []}
    decisions = [Decision(item_name="rožok", gtin="SLI50", card="Šiška džemová 50g",
                          confidence=1.0, rule="catalog_name", note="", review=False,
                          trace={}, quantity=1, unit="ks")]
    date_qid = teach.ask_date(pg, message_id="mk1", dates=["04.08.2026", "05.08.2026"],
                              reason="rozpor", delivery_date="04.08.2026")
    hold.place(pg, message_id="mk1", matched=matched, order=order, decisions=decisions,
              extracted={"isChangeRequest": False, "unverified": [], "notes": ""},
              question_ids=[date_qid])
    # the real dispatch (httpapi._api_orders_answer_generic) marks the question answered
    # BEFORE calling apply() — release_for_question requires every referenced id answered.
    pg.execute("UPDATE order_questions SET status='answered', answer=%s WHERE id=%s",
              (Json({"choice": "05.08.2026"}), date_qid))
    q = teach.get(pg, date_qid)
    kind = teach.KINDS["date"]
    extra = kind.apply(pg, _cfg(), q, "05.08.2026", "sklad")
    assert extra["released"] and extra["released"][0]["status"] == "ok"
    row = pg.execute("SELECT delivery_date FROM held_orders WHERE message_id='mk1'"
                     ).fetchone()
    assert row == ("05.08.2026",)


def test_date_kind_validate_rejects_a_nonsense_date():
    kind = teach.KINDS["date"]
    with pytest.raises(teach.NotACandidate):
        kind.validate({"id": 1}, "not-a-date", "sklad")
    kind.validate({"id": 1}, "05.08.2026", "sklad")  # a real date is accepted


def test_date_kind_undo_reopens_without_touching_anything_else(pg):
    date_qid = teach.ask_date(pg, message_id="mk2", dates=["04.08.2026"], reason="r")
    pg.execute("UPDATE order_questions SET status='answered', answer=%s WHERE id=%s",
              (Json({"choice": "04.08.2026"}), date_qid))
    q = teach.get(pg, date_qid)
    reopened = teach.KINDS["date"].undo(pg, q)
    assert reopened["status"] == "open"


# --- line kind ---------------------------------------------------------------------------

def test_line_kind_apply_logs_a_visible_event_and_teaches_nothing(pg):
    pg.execute("INSERT INTO messages (message_id, category) VALUES ('mk3', 'ai_orders')")
    line_qid = teach.ask_line(pg, message_id="mk3", wording="záhadná položka", quantity=3,
                              unit="ks", reason="nenašlo sa v texte")
    q = teach.get(pg, line_qid)
    kind = teach.KINDS["line"]
    kind.apply(pg, _cfg(), q, "drop", "sklad")
    outcome = pg.execute(
        "SELECT outcome FROM email_events WHERE message_id='mk3' ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    assert "nepatril" in outcome
    assert pg.execute("SELECT count(*) FROM item_memory").fetchone()[0] == 0
    assert pg.execute("SELECT count(*) FROM global_item_memory").fetchone()[0] == 0


def test_line_kind_validate_rejects_an_unknown_choice():
    kind = teach.KINDS["line"]
    with pytest.raises(teach.NotACandidate):
        kind.validate({"id": 1}, "maybe", "sklad")


# --- mail kind -----------------------------------------------------------------------------

def test_mail_kind_manual_marks_review_and_teaches_a_rule(pg):
    mq = teach.ask_mail(pg, message_id="mk4", sender_email="dodavatel@example.com",
                        subject="Objednávka 04.08.2026", reason="AI nenašla objednávku")
    q = teach.get(pg, mq)
    teach.KINDS["mail"].apply(pg, _cfg(), q, "manual", "sklad")
    row = pg.execute("SELECT action FROM mail_rules WHERE question_id=%s", (mq,)).fetchone()
    assert row == ("manual",)
    # no `messages` row was inserted for this test message — apply() must not crash
    # trying to mark it processed, only affect it if it actually exists (UPDATE matches 0)
    assert pg.execute("SELECT count(*) FROM messages WHERE message_id='mk4'"
                     ).fetchone()[0] == 0


def test_mail_kind_undo_retracts_only_its_own_rule(pg):
    mq1 = teach.ask_mail(pg, message_id="mk5a", sender_email="a@x.sk", subject="Faktúra",
                         reason="r")
    mq2 = teach.ask_mail(pg, message_id="mk5b", sender_email="a@x.sk", subject="Reklamácia",
                         reason="r")
    teach.KINDS["mail"].apply(pg, _cfg(), teach.get(pg, mq1), "not_order", "sklad")
    teach.KINDS["mail"].apply(pg, _cfg(), teach.get(pg, mq2), "not_order", "sklad")
    assert pg.execute("SELECT count(*) FROM mail_rules").fetchone()[0] == 2
    teach.KINDS["mail"].undo(pg, teach.get(pg, mq1))
    assert pg.execute("SELECT count(*) FROM mail_rules WHERE question_id=%s",
                      (mq1,)).fetchone()[0] == 0
    assert pg.execute("SELECT count(*) FROM mail_rules WHERE question_id=%s",
                      (mq2,)).fetchone()[0] == 1
    assert teach.get(pg, mq1)["status"] == "open"


# --- item / customer kinds — the registry's reference implementation -------------------
#
# httpapi's LIVE dispatch for item/customer keeps its own full-fidelity gtin+card /
# ean_edi+name bodies unchanged (see `_apply_item`'s docstring) — these prove the
# registry entries themselves are correct, not dead code, matching what the "learns +
# escape hatch" test above already exercises for `validate`.

def test_item_kind_present_apply_undo(pg, monkeypatch):
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    qid = teach.ask(pg, message_id="mk6", customer_ean="2000000000001",
                    customer_name="Pekáreň Testovacia s.r.o.", wording="Šiška",
                    quantity=10, unit="ks",
                    candidates=[{"gtin": "SLI50", "name": "Šiška džemová 50g"}])
    q = teach.get(pg, qid)
    kind = teach.KINDS["item"]
    presented = kind.present(q)
    assert presented["kind"] == "item" and presented["options"][0]["value"] == "SLI50"
    extra = kind.apply(pg, _cfg(), q, "SLI50", "sklad")
    assert extra["question"]["status"] == "answered"
    from app.orders import memory
    assert memory.resolve(pg, "2000000000001", "Šiška").gtin == "SLI50"
    kind.undo(pg, teach.get(pg, qid))
    assert memory.resolve(pg, "2000000000001", "Šiška") is None


def test_customer_kind_present_apply_undo(pg, monkeypatch):
    monkeypatch.setattr("app.orders.upload.put", lambda cfg, name, content: True)
    snapshot.import_snapshot(pg, CATALOG_CSV, CUSTOMER_CSV)
    qid = teach.ask_customer(
        pg, message_id="mk7", sender_email="zilina@farmeria.sk",
        candidates=[{"ean_edi": "2000000000001", "name": "Pekáreň Testovacia s.r.o.",
                    "city": "Martin", "street": "Košútka 1", "address_match": False}],
        delivery_date="04.08.2026",
        context={"sender_email": "zilina@farmeria.sk", "sender_name": "Sklad",
                "company_name": "", "delivery_address_guess": ""})
    q = teach.get(pg, qid)
    kind = teach.KINDS["customer"]
    presented = kind.present(q)
    assert presented["kind"] == "customer"
    assert presented["options"][0]["value"] == "2000000000001"
    extra = kind.apply(pg, _cfg(), q, "2000000000001", "sklad")
    assert extra["question"]["status"] == "answered"
    row = pg.execute("SELECT emails FROM customer_overrides WHERE ean_edi='2000000000001'"
                     ).fetchone()
    assert row and "zilina@farmeria.sk" in row[0]
    kind.undo(pg, teach.get(pg, qid))
    row = pg.execute("SELECT emails FROM customer_overrides WHERE ean_edi='2000000000001'"
                     ).fetchone()
    assert row is None or "zilina@farmeria.sk" not in (row[0] or [])


def test_customer_kind_apply_with_blank_choice_is_the_neviem_path(pg):
    qid = teach.ask_customer(
        pg, message_id="mk8", sender_email="neznamy@x.sk",
        candidates=[{"ean_edi": "2000000000001", "name": "Pekáreň", "city": "", "street": "",
                    "address_match": False}],
        delivery_date="04.08.2026",
        context={"sender_email": "neznamy@x.sk", "sender_name": "", "company_name": "",
                "delivery_address_guess": ""})
    q = teach.get(pg, qid)
    extra = teach.KINDS["customer"].apply(pg, _cfg(), q, "", "sklad")
    assert extra["question"]["answer_gtin"] == ""
    assert extra["released"] == []  # no held order exists for this bare question


def test_mail_and_date_kind_present_shapes():
    mail_q = {"id": 1, "kind": "mail", "wording": "Objednávka", "reason": "r",
             "payload": {"sender_email": "a@b.sk"}, "candidates": []}
    date_q = {"id": 2, "kind": "date", "wording": "", "reason": "rozpor", "candidates": []}
    line_q = {"id": 3, "kind": "line", "wording": "x", "reason": "r", "quantity": 1,
             "payload": {"unit": "ks"}, "candidates": []}
    for q, kind_name in ((mail_q, "mail"), (date_q, "date"), (line_q, "line")):
        p = teach.KINDS[kind_name].present(q)
        assert p["qid"] == q["id"] and p["kind"] == kind_name and p["unknown_label"]


# --- dl_item / dl_supplier (#202, DL migration F3) --------------------------------------

def test_dl_item_kind_present_apply_undo(pg):
    from app.orders import dl_memory
    qid = teach.ask_dl_item(
        pg, message_id="dlk1", supplier_ean="S1", supplier_name="Mlyn Vrbovce s.r.o.",
        wording="Múka hladká", quantity=25, unit="kg",
        candidates=[{"gtin": "G1", "name": "Múka hladká T512 25kg"}])
    # The real HTTP dispatch (`_api_orders_answer_generic`) always marks the question
    # 'answered' BEFORE calling apply() — do the same here, or release_for_question's own
    # "every sibling answered?" gate finds THIS question still open and returns early for
    # that reason alone (review finding, #240: an earlier version of this test skipped
    # this step and its own comment below was accordingly wrong).
    pg.execute("UPDATE order_questions SET status='answered' WHERE id=%s", (qid,))
    q = teach.get(pg, qid)
    kind = teach.KINDS["dl_item"]
    presented = kind.present(q)
    assert presented["kind"] == "dl_item" and presented["options"][0]["value"] == "G1"
    extra = kind.apply(pg, _cfg(), q, "G1", "sklad")
    # #240: apply() now also tries to release the document that raised this question —
    # "dlk1" was never inserted into `messages` (this test only exercises teach.py's own
    # dedup/present/undo machinery), so release_for_question's sibling gate passes (no
    # OTHER open dl_item/dl_supplier question on this message) but then finds no
    # `messages` row to reprocess and reports an empty release, same shape item/customer/
    # date's own apply() already returns.
    assert extra == {"released": []}
    assert dl_memory.resolve(pg, "S1", "Múka hladká").gtin == "G1"
    kind.undo(pg, teach.get(pg, qid))
    assert dl_memory.resolve(pg, "S1", "Múka hladká") is None
    assert teach.get(pg, qid)["status"] == "open"


def test_dl_item_kind_apply_with_blank_choice_teaches_nothing(pg):
    """Review finding (#240, this ticket's own second round): `_apply_dl_item`'s new
    blank-choice short-circuit (mirrors `_apply_dl_supplier`'s existing "neviem" guard)
    had zero direct coverage — `_api_orders_answer_generic` already keeps this branch
    dead on the real HTTP path (a blank `choice` short-circuits before `kind.apply` is
    ever called, httpapi.py's own `if not choice: return jsonify(...)`), so only a
    direct `KINDS["dl_item"].apply(..., "", ...)` call like this one actually exercises
    it — mirrors `test_dl_supplier_kind_apply_with_blank_choice_teaches_nothing` above."""
    from app.orders import dl_memory
    qid = teach.ask_dl_item(
        pg, message_id="dlk7", supplier_ean="S1", supplier_name="Mlyn Vrbovce s.r.o.",
        wording="Neznáma múka", quantity=25, unit="kg",
        candidates=[{"gtin": "G1", "name": "Múka hladká T512 25kg"}])
    q = teach.get(pg, qid)
    extra = teach.KINDS["dl_item"].apply(pg, _cfg(), q, "", "sklad")
    assert extra == {}
    assert dl_memory.resolve(pg, "S1", "Neznáma múka") is None
    assert teach.get(pg, qid)["status"] == "open"


def test_ask_dl_item_refuses_with_no_supplier_ean_or_wording(pg):
    assert teach.ask_dl_item(pg, message_id="x", supplier_ean="", supplier_name="",
                             wording="čosi", quantity=1, unit="ks", candidates=[]) is None
    assert teach.ask_dl_item(pg, message_id="x", supplier_ean="S1", supplier_name="",
                             wording="", quantity=1, unit="ks", candidates=[]) is None


def test_ask_dl_supplier_refuses_with_no_message_id_or_sender(pg):
    assert teach.ask_dl_supplier(pg, message_id="", sender_email="a@b.sk",
                                 candidates=[]) is None
    assert teach.ask_dl_supplier(pg, message_id="x", sender_email="", candidates=[]) is None


def test_ask_dl_item_skips_when_the_wording_is_already_human_taught(pg):
    """Review finding (#202 PR): mirrors ask()'s own `recalled.human` pre-check — a future
    caller must not raise a needless duplicate question for a wording dl_memory.resolve()
    would already answer for free."""
    from app.orders import dl_memory
    dl_memory.remember(pg, "S1", "Múka hladká", "G1", "Múka hladká 25kg", "2026-08-01",
                       source="human")
    assert teach.ask_dl_item(pg, message_id="dlk9", supplier_ean="S1", supplier_name="X",
                             wording="Múka hladká", quantity=1, unit="kg",
                             candidates=[{"gtin": "G1", "name": "Múka hladká 25kg"}]) is None
    assert pg.execute("SELECT count(*) FROM order_questions WHERE kind='dl_item'"
                     ).fetchone()[0] == 0


def test_ask_dl_supplier_skips_when_the_address_is_already_taught(pg):
    from app.orders import dl_supplier_memory as dsm
    dsm.remember(pg, "obchod@mlynvrbovce.sk", "S1", "Mlyn Vrbovce s.r.o.")
    assert teach.ask_dl_supplier(
        pg, message_id="dlk10", sender_email="obchod@mlynvrbovce.sk",
        candidates=[{"ean_edi": "S1", "name": "Mlyn Vrbovce s.r.o."}]) is None
    assert pg.execute("SELECT count(*) FROM order_questions WHERE kind='dl_supplier'"
                     ).fetchone()[0] == 0


def test_dl_item_kind_dedupes_per_supplier_and_wording(pg):
    qid1 = teach.ask_dl_item(pg, message_id="dlk2a", supplier_ean="S2",
                             supplier_name="Cukrovar", wording="Cukor", quantity=1, unit="ks",
                             candidates=[{"gtin": "G3", "name": "Cukor kryštálový 25kg"}])
    qid2 = teach.ask_dl_item(pg, message_id="dlk2b", supplier_ean="S2",
                             supplier_name="Cukrovar", wording="Cukor", quantity=1, unit="ks",
                             candidates=[{"gtin": "G3", "name": "Cukor kryštálový 25kg"}])
    assert qid1 == qid2


def test_dl_item_kind_validate_rejects_an_unoffered_gtin(pg):
    qid = teach.ask_dl_item(pg, message_id="dlk3", supplier_ean="S3", supplier_name="X",
                            wording="čosi", quantity=1, unit="ks",
                            candidates=[{"gtin": "G1", "name": "Karta"}])
    q = teach.get(pg, qid)
    with pytest.raises(teach.NotACandidate):
        teach.KINDS["dl_item"].validate(q, "NOT-OFFERED", "sklad")
    teach.KINDS["dl_item"].validate(q, "G1", "sklad")   # the real offered one is fine
    teach.KINDS["dl_item"].validate(q, "", "sklad")     # blank ("neviem") is always fine


def test_dl_supplier_kind_present_apply_undo(pg):
    from app.orders import dl_supplier_memory as dsm
    qid = teach.ask_dl_supplier(
        pg, message_id="dlk4", sender_email="obchod@mlynvrbovce.sk",
        candidates=[{"ean_edi": "S1", "name": "Mlyn Vrbovce s.r.o."}])
    # Same reasoning as test_dl_item_kind_present_apply_undo above — mark it answered
    # first, mirroring the real HTTP dispatch, so release_for_question's sibling gate
    # genuinely passes instead of finding this very question still open.
    pg.execute("UPDATE order_questions SET status='answered' WHERE id=%s", (qid,))
    q = teach.get(pg, qid)
    kind = teach.KINDS["dl_supplier"]
    presented = kind.present(q)
    assert presented["kind"] == "dl_supplier"
    assert presented["options"][0]["value"] == "S1"
    extra = kind.apply(pg, _cfg(), q, "S1", "sklad")
    # #240: same reasoning as test_dl_item_kind_present_apply_undo above — "dlk4" has no
    # `messages` row, so the sibling gate passes but release_for_question finds nothing
    # to reprocess.
    assert extra == {"released": []}
    assert dsm.resolve(pg, "obchod@mlynvrbovce.sk") == {"ean_edi": "S1",
                                                         "name": "Mlyn Vrbovce s.r.o."}
    kind.undo(pg, teach.get(pg, qid))
    assert dsm.resolve(pg, "obchod@mlynvrbovce.sk") is None
    assert teach.get(pg, qid)["status"] == "open"


def test_dl_supplier_kind_apply_with_blank_choice_teaches_nothing(pg):
    from app.orders import dl_supplier_memory as dsm
    qid = teach.ask_dl_supplier(
        pg, message_id="dlk5", sender_email="neznamy@nikde.sk",
        candidates=[{"ean_edi": "S1", "name": "Mlyn Vrbovce s.r.o."}])
    q = teach.get(pg, qid)
    extra = teach.KINDS["dl_supplier"].apply(pg, _cfg(), q, "", "sklad")
    assert extra == {}
    assert dsm.resolve(pg, "neznamy@nikde.sk") is None


def test_dl_supplier_kind_dedupes_per_sender_address(pg):
    qid1 = teach.ask_dl_supplier(pg, message_id="dlk6a", sender_email="a@b.sk",
                                 candidates=[{"ean_edi": "S1", "name": "X"}])
    qid2 = teach.ask_dl_supplier(pg, message_id="dlk6b", sender_email="A@B.SK  ",
                                 candidates=[{"ean_edi": "S1", "name": "X"}])
    assert qid1 == qid2


def test_dl_supplier_kind_validate_rejects_an_unoffered_ean():
    q = {"id": 1, "candidates": [{"value": "S1", "label": "X"}]}
    with pytest.raises(teach.NotACandidate):
        teach.KINDS["dl_supplier"].validate(q, "S9", "sklad")
    teach.KINDS["dl_supplier"].validate(q, "S1", "sklad")
    teach.KINDS["dl_supplier"].validate(q, "", "sklad")
