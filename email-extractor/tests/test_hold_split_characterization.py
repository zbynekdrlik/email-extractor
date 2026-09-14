"""Characterization test for the #424 refactor — pins the observable surface BEFORE the
code move so the pure, zero-behaviour-change split can be checked at the API level, not
just behaviourally. Must pass UNCHANGED before AND after:

  1. `hold.py` (> 900 r.) is split into a thin FACADE + concern modules
     (`hold_place`/`hold_redecide`/`hold_close`, per `.claude/rules/module-split-refactor.md`).
     Part A below pins every public + test-reached-private name AND its exact signature, so a
     dropped re-export or a signature drift fails HERE, loudly.
  2. `httpapi_orders_questions._api_orders_answer_item_manual` (~170 r.) has its READ-ONLY
     verdict extracted into `_classify_manual_target`; the endpoint becomes a thin dispatcher.
     Part B drives the REAL endpoint for every verdict branch and pins the exact HTTP status
     AND the exact byte-for-byte message — a behaviour drift in the dispatch fails HERE.

Part A is DB-free (namespace inspection only). Part B uses the Flask test client + real
Postgres, mirroring test_httpapi_manual_421.py.
"""
from __future__ import annotations

import inspect
import os
from datetime import datetime

from psycopg.types.json import Json

from app.config import Config
from app.httpapi import create_app
from app.orders import hold, teach

# ---------------------------------------------------------------------------
# Part A — the `hold` public/observable API surface (names + exact signatures)
# ---------------------------------------------------------------------------

# name -> str(inspect.signature(...)) exactly as it is TODAY (the monolith). After the
# split every name must be re-exported by the facade with a byte-identical signature.
EXPECTED_SIGNATURES = {
    # --- read / create a hold + (de)serialization + deadline (-> hold_place) ---
    "is_past_deadline": "(delivery_date: 'str', today: 'str' = '') -> 'bool'",
    "place": "(conn, message_id: 'str', matched, order: 'dict', decisions, extracted: 'dict', question_ids: 'list[int]') -> 'int'",
    "has_open": "(conn, message_id: 'str') -> 'bool'",
    "get": "(conn, held_id: 'int') -> 'dict | None'",
    "list_held": "(conn, limit: 'int' = 200) -> 'list[dict]'",
    "_dump_decisions": "(decisions) -> 'list[dict]'",
    "_load_decisions": "(rows: 'list[dict]')",
    "_apply_confirmed_quantities": "(conn, decisions: 'list', question_ids: 'list') -> 'None'",
    "_row": "(r) -> 'dict | None'",
    "_db_today": "(conn)",
    # --- re-decision + ship helpers (-> hold_redecide) ---
    "_current_catalog": "(conn) -> 'list[dict]'",
    "_redecide": "(conn, customer_ean: 'str', decisions: 'list', as_of: 'str' = '', catalog: 'list[dict] | None' = None, _recalled_cache: 'dict | None' = None) -> 'list'",
    "_ask_still_ambiguous": "(conn, row: 'dict', decisions: 'list', still_asking: 'list', catalog: 'list[dict]', as_of: 'str', _recalled_cache: 'dict | None' = None) -> 'tuple[list[int], list[str]]'",
    "_post_still_held": "(cfg, post, row: 'dict', decisions: 'list', new_qids: 'list[int]', unaskable: 'list[str]') -> 'None'",
    "_ship": "(conn, cfg, row: 'dict', upload, post, redecide: 'bool', as_of: 'str' = '') -> 'tuple[str, dict, str]'",
    "_mark_message_done_if_clear": "(conn, message_id: 'str') -> 'None'",
    # --- release / expire / resolve / customer / date / due (-> hold_close) ---
    "release_to_review": "(conn, cfg, row: 'dict', post, reason: 'str') -> 'dict'",
    "_do_release": "(conn, cfg, row: 'dict', release_reason: 'str', upload, post, redecide: 'bool', as_of: 'str' = '') -> 'dict'",
    "_do_release_locked": "(conn, cfg, hid: 'int', upload, post, as_of: 'str' = '') -> 'dict | None'",
    "release_for_question": "(conn, cfg, qid: 'int', upload=None, post=None) -> 'list[dict]'",
    "_release_locked": "(conn, cfg, hid: 'int', upload, post) -> 'dict | None'",
    "resolve_manually": "(conn, cfg, qid: 'int', post=None) -> 'list[dict]'",
    "_resolve_one_manually": "(conn, cfg, hid: 'int', post) -> 'dict | None'",
    "unresolve_manually": "(conn, qid: 'int') -> 'list[str]'",
    "close_expired_holds": "(conn, cfg, post=None) -> 'list[dict]'",
    "set_customer": "(conn, qid: 'int', ean_edi: 'str', name: 'str') -> 'None'",
    "set_delivery_date": "(conn, qid: 'int', date: 'str') -> 'None'",
    "release_unknown_customer": "(conn, cfg, qid: 'int', post=None) -> 'list[dict]'",
    "retry_unknown_customer_questions": "(conn, cfg, upload=None, post=None) -> 'list[dict]'",
    "_has_non_shippable_open_question": "(conn, question_ids: 'list[int]') -> 'bool'",
    "release_due": "(conn, cfg, upload=None, post=None, today: 'str' = '') -> 'list[dict]'",
}


def test_every_hold_name_is_reachable_callable_and_signature_stable():
    for name, sig in EXPECTED_SIGNATURES.items():
        assert hasattr(hold, name), f"hold.{name} is missing from the facade"
        obj = getattr(hold, name)
        assert callable(obj), f"hold.{name} is not callable"
        assert str(inspect.signature(obj)) == sig, (
            f"hold.{name} signature drifted: {inspect.signature(obj)!s} != {sig}")


def test_hold_logger_name_is_preserved_verbatim():
    # A per-module logger name is an observable log-output change; the split keeps it identical
    # (post-deploy log-watching keys on "orders.hold").
    assert hold.log.name == "orders.hold"


def test_hold_cols_constant_is_byte_stable():
    assert hold._COLS == (
        "id, message_id, customer_ean, customer_name, delivery_date, order_number, "
        "store, recipient_group, question_ids, order_json, extracted_json, "
        "decisions_json, status, release_reason, created_at, released_at")


# ---------------------------------------------------------------------------
# Part B — the manual endpoint verdict dispatch (exact status + exact message)
# ---------------------------------------------------------------------------

PG_DSN = os.environ.get("PG_TEST_DSN")

_RELEASED_PREFIX = "Na túto otázku už nečaká žiadna objednávka — "
_RELEASED_SUFFIX = "; mohla sa medzitým odoslať do ORIONu, skontroluj dashboard."
_ALREADY_SHIPPED = ("Na túto otázku nečakala žiadna objednávka, ale mail už "
                    "(čiastočne) odišiel do ORIONu — ručné zadanie by bol duplikát; "
                    "skontroluj dashboard.")
_MULTI_MESSAGE = ("Táto otázka blokuje objednávky z viacerých mailov — nedá sa hromadne "
                  "vyriešiť ručne. Vyrieš ju bežnou odpoveďou (vyber kartu).")


def _client():
    cfg = Config(pg_dsn=PG_DSN, data_dir="/tmp", api_token="tok",
                 dash_password="secret", secret_key="test-secret", orders_channel_id=0)
    app = create_app(cfg)
    app.testing = True
    return app.test_client()


def _login(c):
    c.post("/login", data={"password": "secret"})


def _no_upload(monkeypatch, sink):
    monkeypatch.setattr("app.orders.upload.put",
                        lambda cfg, name, content: sink.append((name, content)) or True)


def _no_post(monkeypatch):
    monkeypatch.setattr("app.orders.report.post_from_config", lambda *a, **k: None)


def _msg(pg, mid):
    pg.execute("INSERT INTO messages (message_id, category) VALUES (%s, 'ai_orders')", (mid,))


def _item_question(pg, mid, key="croissant"):
    return pg.execute(
        """INSERT INTO order_questions (message_id, customer_ean, customer_name, wording,
                                        item_key, quantity, unit, candidates, delivery_date,
                                        reason)
           VALUES (%s, '2000000000864', 'Vzorky', %s, %s, 5, 'ks', %s,
                   '10.09.2026', 'test')
           RETURNING id""",
        (mid, key, key, Json([{"gtin": "CR", "name": "Croissant 80g"}]))).fetchone()[0]


def _held_row(pg, mid, qid, status="held", reason=None):
    released_at = None if status == "held" else datetime.now()
    pg.execute(
        """INSERT INTO held_orders (message_id, customer_ean, customer_name, delivery_date,
                                    order_number, question_ids, order_json, extracted_json,
                                    decisions_json, status, release_reason, released_at)
           VALUES (%s, '2000000000864', 'Vzorky', '10.09.2026', '', %s, %s, %s, %s, %s, %s,
                   %s)""",
        (mid, [qid], Json({"deliveryDate": "10.09.2026", "orderNumber": ""}),
         Json({"isChangeRequest": False, "unverified": [], "notes": ""}), Json([]),
         status, reason, released_at))


def _post_manual(c, qid):
    return c.post(f"/api/orders/question/{qid}/answer", json={"manual": True})


def test_verdict_noop_already_answered_manual_nothing_held_returns_200(pg, monkeypatch):
    _no_upload(monkeypatch, [])
    _msg(pg, "noop1")
    qid = _item_question(pg, "noop1")
    pg.execute(
        "UPDATE order_questions SET status='answered', answer=%s WHERE id=%s",
        (Json({"choice": teach.ITEM_MANUAL}), qid))
    c = _client(); _login(c)
    r = _post_manual(c, qid)
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["ok"] and body["resolved_manually"] == [] and body["released"] == []


def test_verdict_never_held_answers_manual_returns_200(pg, monkeypatch):
    uploads = []
    _no_upload(monkeypatch, uploads)
    _msg(pg, "nh1")
    qid = _item_question(pg, "nh1")
    c = _client(); _login(c)
    r = _post_manual(c, qid)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert uploads == []
    choice = pg.execute("SELECT answer->>'choice' FROM order_questions WHERE id=%s",
                        (qid,)).fetchone()[0]
    assert choice == teach.ITEM_MANUAL


def test_verdict_already_shipped_refuses_409_exact_message(pg, monkeypatch):
    _no_upload(monkeypatch, [])
    _msg(pg, "sh1")
    qid = _item_question(pg, "sh1")
    pg.execute(
        """INSERT INTO email_events (message_id, workflow, stage, status, outcome, detail)
           VALUES (%s, 'orders', 'uploaded_orion', 'ok', 'EDI', %s)""",
        ("sh1", Json({"question_ids": [qid]})))
    c = _client(); _login(c)
    r = _post_manual(c, qid)
    assert r.status_code == 409
    assert r.get_json()["error"] == _ALREADY_SHIPPED
    assert pg.execute("SELECT status FROM order_questions WHERE id=%s",
                      (qid,)).fetchone() == ("open",)


def test_verdict_released_reasons_each_refuse_409_with_exact_message(pg, monkeypatch):
    _no_upload(monkeypatch, [])
    # release_reason is DB-constrained to answered/deadline/manual/expired (or NULL). The
    # else-branch fallback fires when a released row carries NO recognised reason — modelled
    # here by a released row with release_reason=NULL.
    cases = [
        ("deadline", "objednávka už odišla v termíne dodania (deadline)"),
        ("manual", "objednávka už bola vyriešená ručne"),
        ("answered", "objednávka už bola odoslaná po odpovedi na otázku"),
        ("expired", "otázka expirovala a objednávka je na ručné vybavenie"),
        (None, "objednávka už bola uvoľnená"),  # the else-branch fallback (reason NULL)
    ]
    for i, (reason, why) in enumerate(cases):
        mid = f"rel{i}"
        _msg(pg, mid)
        qid = _item_question(pg, mid, key=f"item{i}")  # distinct key: idx_order_questions_open
        _held_row(pg, mid, qid, status="released", reason=reason)
        c = _client(); _login(c)
        r = _post_manual(c, qid)
        assert r.status_code == 409, f"{reason}: {r.get_data(as_text=True)}"
        assert r.get_json()["error"] == _RELEASED_PREFIX + why + _RELEASED_SUFFIX, reason
        assert pg.execute("SELECT status FROM order_questions WHERE id=%s",
                          (qid,)).fetchone() == ("open",)


def test_verdict_multi_message_refuses_409_exact_message(pg, monkeypatch):
    _no_upload(monkeypatch, [])
    _msg(pg, "mm_a")
    qid = _item_question(pg, "mm_a")
    _held_row(pg, "mm_a", qid, status="held")
    _msg(pg, "mm_b")
    _held_row(pg, "mm_b", qid, status="held")  # SAME qid, DIFFERENT message -> >1 mail
    c = _client(); _login(c)
    r = _post_manual(c, qid)
    assert r.status_code == 409
    assert r.get_json()["error"] == _MULTI_MESSAGE
    assert pg.execute("SELECT status FROM order_questions WHERE id=%s",
                      (qid,)).fetchone() == ("open",)


def test_verdict_held_single_mail_resolves_manually_returns_200(pg, monkeypatch):
    uploads = []
    _no_upload(monkeypatch, uploads)
    _no_post(monkeypatch)
    _msg(pg, "held1")
    qid = _item_question(pg, "held1")
    _held_row(pg, "held1", qid, status="held")
    c = _client(); _login(c)
    r = _post_manual(c, qid)
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["ok"] and body["released"] == []
    assert [x["status"] for x in body["resolved_manually"]] == ["manual"]
    assert uploads == [], "the manual (no-upload) path never ships"
    assert pg.execute(
        "SELECT status, release_reason FROM held_orders WHERE %s = ANY(question_ids)",
        (qid,)).fetchone() == ("released", "manual")
    choice = pg.execute("SELECT answer->>'choice' FROM order_questions WHERE id=%s",
                        (qid,)).fetchone()[0]
    assert choice == teach.ITEM_MANUAL
