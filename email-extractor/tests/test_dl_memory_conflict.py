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
    # the older roll answer + today's roll answer stay; the fruit misclick is gone
    assert {g for (g,) in live_human} == {G_ROLL}, "the conflicting fruit answer is superseded"
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


# --- review findings (same branch) ------------------------------------------------------

def test_a_conflict_on_a_remembered_non_warehouse_supplier_is_held_not_skipped(pg, tmp_path):
    """Review 🔴: a memory_conflict line positively points at a catalog card — it must NOT
    fall into the #314 'remembered non-warehouse supplier, no catalog match' terminal skip
    (that would silently drop real goods with no question). Held + asked instead."""
    from app.orders import dl_nonwarehouse
    _snapshot(pg)
    _poison_history(pg)
    dl_nonwarehouse.remember(pg, SUPPLIER_EAN, "Pekáreň Lunys", "")
    uploaded = _run(pg, tmp_path, "dl1", _doc("0100000108"), _llm(G_ROLL, 0.81))
    assert uploaded == []
    assert len(_open_question(pg)) == 1, "asked, never skipped as not-warehouse"
    assert pg.execute("SELECT proc_status FROM messages WHERE message_id='dl1'"
                      ).fetchone()[0] != "not_warehouse"


def test_a_conflict_ask_upgrades_an_already_open_plain_question_for_the_wording(pg, tmp_path):
    """Review 🟡: a plain open dl_item question for the same (supplier, wording) already
    exists (dedupe target). The conflict ask must upgrade it — flag it `memory_conflict`
    and put both conflicting cards first — or its answer would neither supersede the
    misclick nor count as the sklad's confirmation."""
    _snapshot(pg)
    _poison_history(pg)
    _msg(pg, mid="dl0")
    # dl0 is an already-processed (held) message — else tick() would claim it before dl1
    pg.execute("UPDATE messages SET processed = true WHERE message_id = 'dl0'")
    qid0 = teach.ask_generic(pg, "dl_item", "dl0", teach.dl_item_key(SUPPLIER_EAN, ROLL), ROLL,
                             [{"value": "9999", "label": "Niečo iné"}], "stará otázka",
                             {"supplier_ean": SUPPLIER_EAN, "supplier_name": "Pekáreň Lunys"})
    _run(pg, tmp_path, "dl1", _doc("0100000109"), _llm(G_ROLL, 0.81))
    rows = _open_question(pg)
    assert [r[0] for r in rows] == [qid0], "deduped onto the existing open question"
    _qid, cands, payload = rows[0]
    assert payload.get("memory_conflict") is True
    assert [c["value"] for c in cands[:2]] == [G_FRUIT, G_ROLL]
    assert "9999" in [c["value"] for c in cands], "the old candidates are kept"


def test_undoing_a_conflict_answer_restores_what_it_superseded(pg, tmp_path, monkeypatch):
    """Review 🟡: undo of a conflict answer must bring back the human answers it superseded
    (never hard-delete them — their Kôš audit rows would point at nothing) and must not
    wipe the OLDER human answers that predate the question."""
    _snapshot(pg)
    _poison_history(pg)
    _run(pg, tmp_path, "dl1", _doc("0100000110"), _llm(G_ROLL, 0.81))
    qid = _open_question(pg)[0][0]
    _answer_through_the_app_path(pg, monkeypatch, qid, G_ROLL)
    teach.KINDS["dl_item"].undo(pg, teach.get(pg, qid))
    live = pg.execute(
        "SELECT gtin, delivered_on::text FROM dl_item_memory WHERE source='human' "
        "AND deleted_at IS NULL ORDER BY delivered_on").fetchall()
    assert live == [(G_ROLL, "2026-09-08"), (G_FRUIT, "2026-09-09")], \
        "the superseded fruit answer is back, the pre-existing roll answer survived"
    assert teach.get(pg, qid)["status"] == "open"


def test_a_conflict_question_offers_the_card_alias_to_the_board(pg, tmp_path):
    """Review 🔵: the board's lexical confirm must see the card's alias too (a wording that
    only matches a card through its doplnok is not a misclick) — the stored candidates carry
    it."""
    _snapshot(pg)
    pg.execute("UPDATE dl_catalog_snapshot SET doplnok = 'jablko pražené balené' "
               "WHERE gtin = %s", (G_FRUIT,))
    _poison_history(pg)
    _run(pg, tmp_path, "dl1", _doc("0100000111"), _llm(G_ROLL, 0.81))
    cands = _open_question(pg)[0][1]
    fruit = next(c for c in cands if c["value"] == G_FRUIT)
    assert fruit.get("alias") == "jablko pražené balené"


def test_an_upgraded_question_shows_the_conflict_reason(pg, tmp_path):
    """Review 🔵: the upgraded (formerly plain) question must carry the conflict explanation,
    not its stale old reason — the sklad needs to see why both cards are offered."""
    _snapshot(pg)
    _poison_history(pg)
    _msg(pg, mid="dl0")
    pg.execute("UPDATE messages SET processed = true WHERE message_id = 'dl0'")
    teach.ask_generic(pg, "dl_item", "dl0", teach.dl_item_key(SUPPLIER_EAN, ROLL), ROLL,
                      [], "stará otázka", {"supplier_ean": SUPPLIER_EAN})
    _run(pg, tmp_path, "dl1", _doc("0100000112"), _llm(G_ROLL, 0.81))
    reason = pg.execute("SELECT reason FROM order_questions WHERE status='open'").fetchone()[0]
    assert "nie je jednoznačná" in reason and reason != "stará otázka"


def test_a_repeated_conflict_undo_restores_cleanly(pg, tmp_path, monkeypatch, caplog):
    """Review 🔵: answer → undo → answer → undo must restore without a spurious ERROR (the
    same superseded row has two delete audit rows by then)."""
    _snapshot(pg)
    _poison_history(pg)
    _run(pg, tmp_path, "dl1", _doc("0100000113"), _llm(G_ROLL, 0.81))
    qid = _open_question(pg)[0][0]
    for _ in range(2):
        _answer_through_the_app_path(pg, monkeypatch, qid, G_ROLL)
        teach.KINDS["dl_item"].undo(pg, teach.get(pg, qid))
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    live = pg.execute("SELECT gtin FROM dl_item_memory WHERE source='human' "
                      "AND deleted_at IS NULL").fetchall()
    assert {g for (g,) in live} == {G_ROLL, G_FRUIT}
