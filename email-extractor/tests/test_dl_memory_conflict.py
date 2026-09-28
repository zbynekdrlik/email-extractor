"""#465 — the DL memory rescue must not SILENTLY ship a remembered card when the history is
ambiguous (conflicting human answers, a newer contrary ship majority, or zero lexical overlap
item↔card) and the model disagrees. Drives the REAL worker (`dl_worker.tick` /
`release_for_question`) against a real Postgres, scripted model answers only.

All fixtures are SYNTHETIC — the incident SHAPE (a bakery-roll wording taught by one board
misclick onto an unrelated fruit card) with made-up GTINs/EANs; this repo is public.
"""
from __future__ import annotations

from psycopg.types.json import Json
from test_dl_worker import (
    _DL_DELIVERY_DATE,
    SUPPLIER_EAN,
    SUPPLIER_MATCHED,
    SUPPLIERS_CSV,
    FakeClient,
    _attach,
    _cfg,
    _msg,
)

from app.orders import dl_memory, dl_snapshot, dl_worker, teach

G_ROLL = "8588000000101"
G_FRUIT = "8588000000102"
ROLL_CARD = "Rožok štandart 50g"
FRUIT_CARD = "Ovocie - Zlaté jablko pražené"
ROLL = "Rožok oravský bez E 50g"
OIL = "Olej olivový z výliskov 1l"

CATALOG_CSV = ("GTIN,Názov,doplnok,hmotnost,Sklad,Cena\n"
               f"{G_ROLL},{ROLL_CARD},,0.05,1,0.10\n"
               f"{G_FRUIT},{FRUIT_CARD},,,1,2.00\n")


def _snapshot(pg):
    return dl_snapshot.import_snapshot(pg, CATALOG_CSV, "GTIN,Sklad,Názov,doplnok\n",
                                       SUPPLIERS_CSV)


def _doc(doc_number, name=ROLL, qty=10, price=0.1):
    total = round(qty * price, 2)
    return {"documents": [{
        "supplierName": "Pekáreň Lunys", "supplierCity": "Prešov",
        "supplierEmail": "dodavatel@lunys.sk", "docNumber": doc_number,
        "deliveryDate": _DL_DELIVERY_DATE, "documentTotalWithoutVAT": total,
        "items": [{"name": name, "quantity": qty, "unit": "ks", "unitPrice": price,
                   "totalPrice": total, "vatRate": 10}]}]}


def _llm(gtin, conf):
    return {"gtin": gtin, "matchConfidence": conf, "matchReason": "scripted"}


def _poison_history(pg):
    """The incident history: an earlier correct human answer (roll), a later misclick onto
    the fruit card, and ship history AFTER the misclick mostly on the roll card."""
    dl_memory.remember(pg, SUPPLIER_EAN, ROLL, G_ROLL, ROLL_CARD, "2026-09-08", source="human")
    dl_memory.remember(pg, SUPPLIER_EAN, ROLL, G_FRUIT, FRUIT_CARD, "2026-09-09",
                       source="human")
    for day in ("2026-09-10", "2026-09-11", "2026-09-18"):
        dl_memory.remember(pg, SUPPLIER_EAN, ROLL, G_ROLL, ROLL_CARD, day)


def _run(pg, tmp_path, mid, doc, llm_answer):
    _msg(pg, mid=mid)
    _attach(pg, tmp_path, mid)
    uploaded = []
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    dl_worker.tick(pg, cfg, client=FakeClient({"dl_documents": [doc],
                                              "dl_supplier": [SUPPLIER_MATCHED],
                                              "dl_item": [llm_answer]}),
                   upload=lambda c, name, content, dir_override=None: uploaded.append(content))
    return uploaded


def _open_question(pg):
    return pg.execute(
        "SELECT id, candidates, payload FROM order_questions "
        "WHERE kind='dl_item' AND status='open'").fetchall()


def test_the_incident_shape_holds_the_document_and_asks_with_both_cards(pg, tmp_path):
    """#465 core, end-to-end: the model picks the roll (0.81 < sure gate), memory's newest
    human answer says fruit, the older human answer + the newer ship majority say roll. The
    document is HELD (nothing uploaded, no claim) and ONE dl_item question offers BOTH cards,
    the remembered one and the model's, first — never a silent ship of the fruit card."""
    _snapshot(pg)
    _poison_history(pg)
    uploaded = _run(pg, tmp_path, "dl1", _doc("0100000101"), _llm(G_ROLL, 0.81))
    assert uploaded == [], "a conflicted memory must never ship silently"
    assert pg.execute("SELECT count(*) FROM desadv_sent").fetchone()[0] == 0
    rows = _open_question(pg)
    assert len(rows) == 1, "the conflict is asked on the board (not refused as human-taught)"
    _qid, cands, payload = rows[0]
    assert [c["value"] for c in cands[:2]] == [G_FRUIT, G_ROLL]
    assert payload.get("memory_conflict") is True
    row = pg.execute(
        "SELECT processed, proc_status FROM messages WHERE message_id='dl1'").fetchone()
    assert row == (True, "review")
    rule = pg.execute("SELECT rule FROM order_items").fetchone()[0]
    assert rule == "memory_conflict"


def test_a_lexically_unrelated_single_human_answer_with_no_model_match_is_held(pg, tmp_path):
    """The olej-olivový shape: ONE misclicked human answer onto the fruit card, the model
    finds nothing. Zero shared word → held + question, not a silent fruit line."""
    _snapshot(pg)
    dl_memory.remember(pg, SUPPLIER_EAN, OIL, G_FRUIT, FRUIT_CARD, "2026-09-09",
                       source="human")
    uploaded = _run(pg, tmp_path, "dl1", _doc("0100000102", name=OIL, qty=4, price=5.0),
                    _llm("NO_MATCH", 0.74))
    assert uploaded == []
    rows = _open_question(pg)
    assert len(rows) == 1 and [c["value"] for c in rows[0][1][:1]] == [G_FRUIT]


def test_a_plausible_single_human_answer_still_rescues_silently(pg, tmp_path):
    """No regression: one human answer, lexically plausible, no contrary newer majority —
    the rescue ships exactly as before, no question."""
    _snapshot(pg)
    dl_memory.remember(pg, SUPPLIER_EAN, ROLL, G_ROLL, ROLL_CARD, "2026-09-08", source="human")
    uploaded = _run(pg, tmp_path, "dl1", _doc("0100000103"), _llm("NO_MATCH", 0.3))
    assert len(uploaded) == 1 and G_ROLL in uploaded[0]
    assert _open_question(pg) == []


def _answer_through_the_app_path(pg, monkeypatch, qid, choice):
    """The real board answer tail (`_api_orders_answer_generic`): mark answered, then
    `teach.KINDS['dl_item'].apply`. Its reprocess (`release_for_question`, which would build
    a REAL model client) is captured here and driven by the test with a FakeClient."""
    released = []
    monkeypatch.setattr(dl_worker, "release_for_question",
                        lambda conn, cfg, q, **kw: released.append(q) or [])
    pg.execute("UPDATE order_questions SET status='answered', answer=%s, answered_by='sklad',"
               " answered_at=now() WHERE id=%s", (Json({"choice": choice}), qid))
    q = teach.get(pg, qid)
    teach.KINDS["dl_item"].apply(pg, _cfg(), q, choice, "sklad")
    monkeypatch.undo()
    assert released == [qid]


def _release(pg, tmp_path, qid, doc, llm_answer):
    uploaded = []
    dl_worker.release_for_question(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), qid,
        client=FakeClient({"dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
                           "dl_item": [llm_answer]}),
        upload=lambda c, name, content, dir_override=None: uploaded.append(content))
    return uploaded


def test_resolving_the_conflict_ships_the_pick_and_never_asks_again(pg, tmp_path, monkeypatch):
    """The sklad picks the ROLL on the conflict question: the wrong fruit answer is
    superseded (soft-deleted, recoverable), the held document ships the roll, and the NEXT
    delivery of the same wording ships silently — no loop of questions."""
    _snapshot(pg)
    _poison_history(pg)
    _run(pg, tmp_path, "dl1", _doc("0100000104"), _llm(G_ROLL, 0.81))
    qid = _open_question(pg)[0][0]
    _answer_through_the_app_path(pg, monkeypatch, qid, G_ROLL)
    live_human = pg.execute(
        "SELECT gtin FROM dl_item_memory WHERE source='human' AND deleted_at IS NULL"
    ).fetchall()
    assert live_human == [(G_ROLL,)], "the conflicting fruit answer is superseded"
    shipped = _release(pg, tmp_path, qid, _doc("0100000104"), _llm(G_ROLL, 0.81))
    assert len(shipped) == 1 and G_ROLL in shipped[0] and G_FRUIT not in shipped[0]
    nxt = _run(pg, tmp_path, "dl2", _doc("0100000105"), _llm("NO_MATCH", 0.5))
    assert len(nxt) == 1 and G_ROLL in nxt[0]
    assert _open_question(pg) == [], "no re-ask on the next delivery"


def test_confirming_the_remembered_card_sticks_no_loop(pg, tmp_path, monkeypatch):
    """The sklad deliberately CONFIRMS the lexically-unrelated remembered card on the
    conflict question — that decision is honoured on every later delivery (silent rescue),
    never re-asked, even though the model keeps disagreeing."""
    _snapshot(pg)
    dl_memory.remember(pg, SUPPLIER_EAN, OIL, G_FRUIT, FRUIT_CARD, "2026-09-09",
                       source="human")
    oil_doc = _doc("0100000106", name=OIL, qty=4, price=5.0)
    _run(pg, tmp_path, "dl1", oil_doc, _llm("NO_MATCH", 0.74))
    qid = _open_question(pg)[0][0]
    _answer_through_the_app_path(pg, monkeypatch, qid, G_FRUIT)
    shipped = _release(pg, tmp_path, qid, oil_doc, _llm("NO_MATCH", 0.74))
    assert len(shipped) == 1 and G_FRUIT in shipped[0]
    nxt = _run(pg, tmp_path, "dl2", _doc("0100000107", name=OIL, qty=4, price=5.0),
               _llm(G_ROLL, 0.6))
    assert len(nxt) == 1 and G_FRUIT in nxt[0]
    assert _open_question(pg) == []
