"""#421: „Vyriešené ručne" must distinguish a NEVER-held question (safe to close as
`manual`, 200) from a question whose held order existed but is released/deadline (a
genuine 409). The change-request orphan class (an item question raised with no hold ever
placed — the 215/216 incident) is exactly the never-held case; before #421 the endpoint
refused it with the ORION-warning 409, leaving the sklad unable to clear a board card by
hand for an order that never produced an EDI.

Flask test client + real Postgres, mirroring test_api.py's own manual-resolve tests.
"""
import os

from psycopg.types.json import Json

from app.config import Config
from app.httpapi import create_app
from app.orders import teach

PG_DSN = os.environ.get("PG_TEST_DSN")


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


def _orphan_item_question(pg, mid):
    """An item question with NO held_orders row — the change-request orphan class."""
    pg.execute("INSERT INTO messages (message_id, category) VALUES (%s, 'ai_orders')", (mid,))
    qid = pg.execute(
        """INSERT INTO order_questions (message_id, customer_ean, customer_name, wording,
                                        item_key, quantity, unit, candidates, delivery_date,
                                        reason)
           VALUES (%s, '2000000000864', 'Vzorky', 'croissant', 'croissant', 5, 'ks', %s,
                   '10.09.2026', 'test')
           RETURNING id""",
        (mid, Json([{"gtin": "CR", "name": "Croissant 80g"}]))).fetchone()[0]
    return qid


def _released_item_hold(pg, mid, reason="deadline"):
    """An item question whose held_orders row EXISTED but is already released — the
    genuine 409 case (the order may already sit in ORION via the deadline sweep)."""
    pg.execute("INSERT INTO messages (message_id, category) VALUES (%s, 'ai_orders')", (mid,))
    qid = pg.execute(
        """INSERT INTO order_questions (message_id, customer_ean, customer_name, wording,
                                        item_key, quantity, unit, candidates, delivery_date,
                                        reason)
           VALUES (%s, '2000000000864', 'Pekáreň', 'rožky', 'rožky', 105, 'ks', %s,
                   '04.09.2026', 'test')
           RETURNING id""",
        (mid, Json([{"gtin": "BAG", "name": "Bageta rožková 200g"}]))).fetchone()[0]
    pg.execute(
        """INSERT INTO held_orders (message_id, customer_ean, customer_name, delivery_date,
                                    order_number, question_ids, order_json, extracted_json,
                                    decisions_json, status, release_reason, released_at)
           VALUES (%s, '2000000000864', 'Pekáreň', '04.09.2026', '', %s, %s, %s, %s,
                   'released', %s, now())""",
        (mid, [qid], Json({"deliveryDate": "04.09.2026", "orderNumber": ""}),
         Json({"isChangeRequest": False, "unverified": [], "notes": ""}), Json([]), reason))
    return qid


def test_manual_on_a_never_held_question_answers_it_without_shipping(pg, monkeypatch):
    """#421 (b): NEVER-held → closed as `manual`, 200, nothing shipped, reminders stop."""
    uploads = []
    _no_upload(monkeypatch, uploads)
    qid = _orphan_item_question(pg, "orph1")
    assert pg.execute("SELECT count(*) FROM held_orders").fetchone()[0] == 0
    c = _client()
    _login(c)
    r = c.post(f"/api/orders/question/{qid}/answer", json={"manual": True})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["ok"] and body["resolved_manually"] == []
    assert uploads == [], "nothing ever ships for a never-held question"
    assert pg.execute("SELECT count(*) FROM edi_sent").fetchone()[0] == 0
    status, choice, by = pg.execute(
        "SELECT status, answer->>'choice', answered_by FROM order_questions WHERE id=%s",
        (qid,)).fetchone()
    assert status == "answered" and choice == "manual"
    assert by, "answered_by is recorded (the session role, or the admin fallback)"
    assert qid not in [q["id"] for q in teach.open_questions(pg)], "leaves the open list"


def _shipped_orphan_item_question(pg, mid):
    """An item question with NO held_orders row, but whose order ALREADY shipped to ORION
    (a same-day / undated matched order with an unmatched line: pipeline.py:470 asks, the
    hold gate at :559 is past-deadline so no hold, and _ship_one uploads a partial EDI
    logging an `uploaded_orion` event carrying the question id). Manual close here would
    invite a duplicate physical delivery — must 409."""
    pg.execute("INSERT INTO messages (message_id, category) VALUES (%s, 'ai_orders')", (mid,))
    qid = pg.execute(
        """INSERT INTO order_questions (message_id, customer_ean, customer_name, wording,
                                        item_key, quantity, unit, candidates, delivery_date,
                                        reason)
           VALUES (%s, '2000000000864', 'Pekáreň', 'torta', 'torta', 5, 'ks', %s,
                   '10.09.2026', 'test')
           RETURNING id""",
        (mid, Json([{"gtin": "TOR", "name": "Torta 1kg"}]))).fetchone()[0]
    # the real upload event _finish logs on a shipped/partial order, carrying question_ids
    pg.execute(
        """INSERT INTO email_events (message_id, workflow, stage, status, outcome, detail)
           VALUES (%s, 'orders', 'uploaded_orion', 'ok', 'EDI', %s)""",
        (mid, Json({"question_ids": [qid]})))
    return qid


def test_manual_on_a_never_held_but_already_shipped_question_refuses_409(pg, monkeypatch):
    """#421 F1: never-held ≠ never-shipped. A same-day/undated order can ship a PARTIAL EDI
    while its unmatched line's question stays open with no hold. Closing it manually would
    duplicate the delivery — refuse with 409, leave the question open."""
    uploads = []
    _no_upload(monkeypatch, uploads)
    qid = _shipped_orphan_item_question(pg, "ship1")
    assert pg.execute("SELECT count(*) FROM held_orders").fetchone()[0] == 0
    c = _client()
    _login(c)
    r = c.post(f"/api/orders/question/{qid}/answer", json={"manual": True})
    assert r.status_code == 409, r.get_data(as_text=True)
    assert pg.execute(
        "SELECT status FROM order_questions WHERE id=%s", (qid,)).fetchone() == ("open",)
    assert uploads == []


def test_manual_on_a_released_hold_still_refuses_with_409(pg, monkeypatch):
    """#421 (c): a held order that EXISTED and is released/deadline keeps the loud 409 —
    the order may already sit in ORION, so a hand-entry would duplicate a physical
    delivery. The question stays open so the sklad checks the dashboard."""
    uploads = []
    _no_upload(monkeypatch, uploads)
    qid = _released_item_hold(pg, "rel1", reason="deadline")
    c = _client()
    _login(c)
    r = c.post(f"/api/orders/question/{qid}/answer", json={"manual": True})
    assert r.status_code == 409
    assert pg.execute(
        "SELECT status FROM order_questions WHERE id=%s", (qid,)).fetchone() == ("open",)
    assert uploads == []
