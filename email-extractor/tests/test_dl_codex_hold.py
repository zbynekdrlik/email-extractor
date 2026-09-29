"""#467 — a DL line matched to a card whose code CODEX does not have must NEVER ship: CODEX
rejects the WHOLE delivery-note import for one unknown EAN kód (DL 126049732 stuck in in_DL with
code 3698). Live path: the line is left without a card (the #245 "cannot ship this code" shape),
the document is HELD (#365) and ONE dl_item question offers only cards CODEX has — ranked by the
CODEX name too, so a card whose OUR name went stale still surfaces. Answering it ships the right
card on the reprocess and on every later delivery. A missing/stale CODEX list FAILS OPEN (ships
as before). Shadow (the e2e-dl corpus) is untouched.

Drives the REAL worker (`dl_worker.tick` / `release_for_question`) against a real Postgres,
scripted model answers only. All fixtures SYNTHETIC (made-up codes/names) — public repo.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

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

from app.orders import codex_cards, dl_memory, dl_snapshot, dl_worker, teach

G_BAD = "3698"                 # our card, but CODEX has no stock card with this code
G_GOOD = "9990000000017"       # CODEX calls it „Rožok so slaninou…", we still „Bagetka…"
G_BREAD = "9990000000024"
BAD_CARD = "Rožok so slaninou a syrom 70g"
GOOD_CARD_OURS = "Bagetka s kečupom a syrom 80 gr"
GOOD_CARD_CODEX = "Rožok so slaninou a syrom 70g"
BREAD_CARD = "Chlieb biely 500g"
ROLL = "Rožok so slaninou a syrom 70g"
BREAD = "Chlieb biely 500g"

CATALOG_CSV = ("GTIN,Názov,doplnok,hmotnost,Sklad,Cena\n"
               f"{G_BAD},{BAD_CARD},,0.07,1,0.30\n"
               f"{G_GOOD},{GOOD_CARD_OURS},,0.08,1,0.30\n"
               f"{G_BREAD},{BREAD_CARD},,0.5,1,0.80\n")

CODEX = [
    {"code": G_GOOD, "card_code": "27", "stredisko": 1, "sklad": 1,
     "name": GOOD_CARD_CODEX, "inactive": False, "changed_at": None},
    {"code": G_BREAD, "card_code": "31", "stredisko": 1, "sklad": 1,
     "name": "Chlieb biely 500g", "inactive": False, "changed_at": None},
]


def _snapshot(pg):
    return dl_snapshot.import_snapshot(pg, CATALOG_CSV, "GTIN,Sklad,Názov,doplnok\n",
                                       SUPPLIERS_CSV)


def _codex(pg, hours_old=1):
    codex_cards.replace_cards(pg, CODEX,
                              source_as_of=datetime.now(UTC) - timedelta(hours=hours_old))


def _doc(doc_number):
    items = [(ROLL, 20, 0.3), (BREAD, 5, 0.8)]
    lines = [{"name": n, "quantity": q, "unit": "ks", "unitPrice": p,
              "totalPrice": round(q * p, 2), "vatRate": 10} for n, q, p in items]
    return {"documents": [{
        "supplierName": "Pekáreň Lunys", "supplierCity": "Prešov",
        "supplierEmail": "dodavatel@lunys.sk", "docNumber": doc_number,
        "deliveryDate": _DL_DELIVERY_DATE,
        "documentTotalWithoutVAT": round(sum(x["totalPrice"] for x in lines), 2),
        "items": lines}]}


def _llm(gtin, conf=0.95):
    return {"gtin": gtin, "matchConfidence": conf, "matchReason": "scripted"}


def _client(doc, roll_pick=G_BAD):
    return FakeClient({"dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
                       "dl_item": [_llm(roll_pick), _llm(G_BREAD, 0.97)]})


def _run(pg, tmp_path, mid, doc, roll_pick=G_BAD):
    _msg(pg, mid=mid)
    _attach(pg, tmp_path, mid)
    uploaded = []
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    dl_worker.tick(pg, cfg, client=_client(doc, roll_pick),
                   upload=lambda c, name, content, dir_override=None: uploaded.append(content))
    return uploaded


def _open_questions(pg):
    return pg.execute(
        "SELECT id, wording, candidates, reason FROM order_questions "
        "WHERE kind='dl_item' AND status='open'").fetchall()


def test_a_line_on_a_card_codex_lacks_holds_the_document_and_asks(pg, tmp_path):
    """The incident, end-to-end: the model is SURE the roll is card 3698 — a code CODEX has
    no stock card for. Nothing is uploaded, nothing is claimed, the document is held, and ONE
    question offers the CODEX-valid card first (found through its CODEX name) and never 3698."""
    _snapshot(pg)
    _codex(pg)
    uploaded = _run(pg, tmp_path, "dl1", _doc("0100000201"))
    assert uploaded == [], "a file CODEX will reject must never reach ORION"
    assert pg.execute("SELECT count(*) FROM desadv_sent").fetchone()[0] == 0
    rows = _open_questions(pg)
    assert len(rows) == 1
    _qid, wording, cands, reason = rows[0]
    assert wording == ROLL
    assert G_BAD in reason and "CODEX" in reason
    values = [c["value"] for c in cands]
    assert values[0] == G_GOOD and G_BAD not in values
    assert cands[0].get("codex_name") == GOOD_CARD_CODEX
    assert pg.execute("SELECT processed, proc_status FROM messages WHERE message_id='dl1'"
                      ).fetchone() == (True, "review")
    rules = dict(pg.execute("SELECT name, rule FROM order_items").fetchall())
    assert rules[ROLL] == "codex_missing" and rules[BREAD] == "llm_sure"
    outcome = pg.execute("SELECT outcome FROM email_events WHERE message_id='dl1' "
                         "AND detail->>'held' = 'true'").fetchone()[0]
    assert "CODEX" in outcome and ROLL in outcome


def _answer_through_the_app_path(pg, monkeypatch, qid, choice):
    """The real board answer tail (`_api_orders_answer_generic`): mark answered, then
    `teach.KINDS['dl_item'].apply`. Its reprocess is captured and driven by the test."""
    released = []
    monkeypatch.setattr(dl_worker, "release_for_question",
                        lambda conn, cfg, q, **kw: released.append(q) or [])
    pg.execute("UPDATE order_questions SET status='answered', answer=%s, answered_by='sklad',"
               " answered_at=now() WHERE id=%s", (Json({"choice": choice}), qid))
    teach.KINDS["dl_item"].apply(pg, _cfg(), teach.get(pg, qid), choice, "sklad")
    monkeypatch.undo()
    assert released == [qid]


def _release(pg, tmp_path, qid, doc):
    uploaded = []
    dl_worker.release_for_question(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), qid,
        client=_client(doc),
        upload=lambda c, name, content, dir_override=None: uploaded.append(content))
    return uploaded


def test_answering_ships_the_valid_card_now_and_on_every_later_delivery(pg, tmp_path,
                                                                       monkeypatch):
    """No loop: the model keeps saying 3698, but once the sklad picked the CODEX-valid card the
    reprocess ships it, and the NEXT delivery ships it silently — never the invalid code."""
    _snapshot(pg)
    _codex(pg)
    _run(pg, tmp_path, "dl1", _doc("0100000202"))
    qid = _open_questions(pg)[0][0]
    _answer_through_the_app_path(pg, monkeypatch, qid, G_GOOD)
    shipped = _release(pg, tmp_path, qid, _doc("0100000202"))
    assert len(shipped) == 1
    assert G_GOOD in shipped[0] and G_BAD not in shipped[0]
    nxt = _run(pg, tmp_path, "dl2", _doc("0100000203"))
    assert len(nxt) == 1 and G_GOOD in nxt[0] and G_BAD not in nxt[0]
    assert _open_questions(pg) == [], "asked once, never again"


def test_a_human_answer_that_taught_the_invalid_code_does_not_block_the_question(pg, tmp_path):
    """The exact incident history: the wording was TAUGHT to 3698 through the board's
    „Nová karta" (a human answer). A human-taught wording is normally never re-asked — but a
    mapping to a code CODEX lacks can never resolve, so it must not count; the line is asked."""
    _snapshot(pg)
    _codex(pg)
    dl_memory.remember(pg, SUPPLIER_EAN, ROLL, G_BAD, BAD_CARD, "2026-09-24", source="human")
    uploaded = _run(pg, tmp_path, "dl1", _doc("0100000204"))
    assert uploaded == []
    rows = _open_questions(pg)
    assert len(rows) == 1 and G_BAD not in [c["value"] for c in rows[0][2]]


def test_a_stale_codex_list_fails_open_and_ships_as_before(pg, tmp_path):
    """Never block every DL because the push stopped: past the staleness threshold the check
    is OFF — the document ships exactly like before this ticket (the ops alert, not a hold, is
    what surfaces the stale list)."""
    _snapshot(pg)
    _codex(pg, hours_old=codex_cards.STALE_HOURS + 10)
    uploaded = _run(pg, tmp_path, "dl1", _doc("0100000205"))
    assert len(uploaded) == 1 and G_BAD in uploaded[0]
    assert _open_questions(pg) == []


def test_no_codex_list_at_all_ships_as_before(pg, tmp_path):
    _snapshot(pg)
    uploaded = _run(pg, tmp_path, "dl1", _doc("0100000206"))
    assert len(uploaded) == 1 and G_BAD in uploaded[0]


def test_a_codex_missing_line_on_a_remembered_non_warehouse_supplier_is_held_not_skipped(
        pg, tmp_path):
    """A line the model positively tied to a catalog card is an undecided WAREHOUSE item —
    never the #314 'remembered non-warehouse supplier, no catalog match' silent skip."""
    from app.orders import dl_nonwarehouse
    _snapshot(pg)
    _codex(pg)
    dl_nonwarehouse.remember(pg, SUPPLIER_EAN, "Pekáreň Lunys", "")
    doc = _doc("0100000207")
    doc["documents"][0]["items"] = doc["documents"][0]["items"][:1]
    doc["documents"][0]["documentTotalWithoutVAT"] = doc["documents"][0]["items"][0][
        "totalPrice"]
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    uploaded = []
    dl_worker.tick(pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)),
                   client=FakeClient({"dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
                                      "dl_item": [_llm(G_BAD)]}),
                   upload=lambda c, name, content, dir_override=None: uploaded.append(content))
    assert uploaded == []
    assert len(_open_questions(pg)) == 1
    assert pg.execute("SELECT proc_status FROM messages WHERE message_id='dl1'"
                      ).fetchone()[0] != "not_warehouse"


def test_shadow_is_byte_identical_whatever_the_codex_list_says(pg, tmp_path):
    """The e2e-dl corpus runs in shadow and measures MATCHING — the CODEX check is a live
    ship policy, so a shadow run of the same document still reports the model's card."""
    from app.orders import dl_document
    _snapshot(pg)
    _codex(pg)
    catalog = dl_snapshot.load_catalog(pg, dl_snapshot.latest_snapshot_id(pg))
    suppliers = dl_snapshot.load_suppliers(pg, dl_snapshot.latest_snapshot_id(pg))
    doc = _doc("0100000208")["documents"][0]
    items: list[dict] = []
    res = dl_document._process_document(
        pg, _cfg(), FakeClient({"dl_supplier": [SUPPLIER_MATCHED],
                                "dl_item": [_llm(G_BAD), _llm(G_BREAD, 0.97)]}),
        {"message_id": "dl-shadow", "subject": "", "from_addr": "dodavatel@lunys.sk"},
        doc, catalog, suppliers, True, items)
    assert res["outcome"] == "ok"
    assert {i["gtin"] for i in res["items"]} == {G_BAD, G_BREAD}
    assert {i["rule"] for i in items} == {"llm_sure"}
    assert _open_questions(pg) == []


# --- review findings (same branch) ------------------------------------------------------

def _rename_good_card(pg, name):
    pg.execute("UPDATE dl_catalog_snapshot SET name = %s WHERE gtin = %s", (name, G_GOOD))


def test_the_answer_is_honoured_on_later_deliveries_even_when_names_share_no_word(
        pg, tmp_path, monkeypatch):
    """Review 🟡: the drifted-name case this ticket targets — OUR name for the valid card
    shares no word with the wording („Bagetka kečupová" vs „Rožok so slaninou…"). The next
    delivery must not trip the #465 lexical-gap conflict and be held AGAIN: the card's CODEX
    name counts for the lexical check (a codex answer itself is NOT a standing confirmation —
    see the misclick test below)."""
    _snapshot(pg)
    _rename_good_card(pg, "Bagetka kečupová 80 gr")
    _codex(pg)
    _run(pg, tmp_path, "dl1", _doc("0100000211"))
    qid = _open_questions(pg)[0][0]
    _answer_through_the_app_path(pg, monkeypatch, qid, G_GOOD)
    shipped = _release(pg, tmp_path, qid, _doc("0100000211"))
    assert len(shipped) == 1 and G_GOOD in shipped[0]
    nxt = _run(pg, tmp_path, "dl2", _doc("0100000212"))
    assert len(nxt) == 1 and G_GOOD in nxt[0] and G_BAD not in nxt[0]
    assert _open_questions(pg) == [], "the sklad already settled this line — never re-asked"


def test_a_misclick_on_a_codex_question_is_not_trusted_on_later_deliveries(
        pg, tmp_path, monkeypatch):
    """Review 2 🟡: a codex question is just a list of cards — its answer must NOT count as the
    board's standing confirmation (#465: a plain answer from another message never does), or a
    misclick („Rožok…" answered with the bread card) would ship silently on every later
    delivery. The reprocess of the answered message ships it once; the NEXT delivery is held
    and re-asked (#465 lexical conflict)."""
    _snapshot(pg)
    _codex(pg)
    _run(pg, tmp_path, "dl1", _doc("0100000216"))
    qid = _open_questions(pg)[0][0]
    _answer_through_the_app_path(pg, monkeypatch, qid, G_BREAD)   # the misclick
    nxt = _run(pg, tmp_path, "dl2", _doc("0100000217"))
    assert nxt == [], "a lexically unrelated misclick never ships silently on a later DL"
    rows = _open_questions(pg)
    assert len(rows) == 1 and rows[0][1] == ROLL


def test_the_answer_supersedes_the_dead_human_answer_and_undo_restores_it(
        pg, tmp_path, monkeypatch):
    """Answering the codex question retires the human answer that taught the dead code
    (soft, audited, Kôš-restorable) — the same board-settled semantics as #465; undo brings it
    back and reopens the question."""
    _snapshot(pg)
    _codex(pg)
    dl_memory.remember(pg, SUPPLIER_EAN, ROLL, G_BAD, BAD_CARD, "2026-09-24", source="human")
    _run(pg, tmp_path, "dl1", _doc("0100000213"))
    qid, _w, _c, _r = _open_questions(pg)[0]
    assert teach.get(pg, qid)["payload"].get("codex_missing") is True
    _answer_through_the_app_path(pg, monkeypatch, qid, G_GOOD)
    live = {g for (g,) in pg.execute("SELECT gtin FROM dl_item_memory WHERE source='human' "
                                     "AND deleted_at IS NULL").fetchall()}
    assert live == {G_GOOD}, "the human answer to the dead code is superseded"
    teach.KINDS["dl_item"].undo(pg, teach.get(pg, qid))
    live = {g for (g,) in pg.execute("SELECT gtin FROM dl_item_memory WHERE source='human' "
                                     "AND deleted_at IS NULL").fetchall()}
    assert live == {G_BAD} and teach.get(pg, qid)["status"] == "open"


def test_an_older_open_question_the_codex_ask_dedupes_onto_is_upgraded(pg, tmp_path):
    """Review 🔵: a plain question for the same (supplier, wording) raised earlier (e.g. while
    the list was stale) is the dedupe target. It must get the CODEX-valid cards first, lose the
    dead ones, and show the CODEX reason — not keep offering the dead card."""
    _snapshot(pg)
    _codex(pg)
    _msg(pg, mid="dl0")
    pg.execute("UPDATE messages SET processed = true WHERE message_id = 'dl0'")
    qid0 = teach.ask_generic(pg, "dl_item", "dl0", teach.dl_item_key(SUPPLIER_EAN, ROLL), ROLL,
                             [{"value": G_BAD, "label": BAD_CARD}], "stará otázka",
                             {"supplier_ean": SUPPLIER_EAN, "supplier_name": "Pekáreň Lunys"})
    _run(pg, tmp_path, "dl1", _doc("0100000214"))
    rows = _open_questions(pg)
    assert [r[0] for r in rows] == [qid0], "deduped onto the existing open question"
    _qid, _w, cands, reason = rows[0]
    values = [c["value"] for c in cands]
    assert values[0] == G_GOOD and G_BAD not in values
    assert "CODEX" in reason and reason != "stará otázka"
    assert teach.get(pg, qid0)["payload"].get("codex_missing") is True


def test_a_memory_conflict_question_keeps_the_codex_name_on_its_head_cards(pg, tmp_path):
    """Review 3 🔵: the conflicting cards put first on a #465 question must keep the CODEX
    name of a drifted card (the board shows it and its misclick check uses it)."""
    _snapshot(pg)
    _rename_good_card(pg, "Bagetka kečupová 80 gr")
    _codex(pg)
    dl_memory.remember(pg, SUPPLIER_EAN, ROLL, G_GOOD, "Bagetka kečupová 80 gr", "2026-09-08",
                       source="human")
    dl_memory.remember(pg, SUPPLIER_EAN, ROLL, G_BREAD, BREAD_CARD, "2026-09-09",
                       source="human")
    _run(pg, tmp_path, "dl1", _doc("0100000218"), roll_pick=G_BAD)
    cands = _open_questions(pg)[0][2]
    good = next(c for c in cands if c["value"] == G_GOOD)
    assert good.get("codex_name") == GOOD_CARD_CODEX


def test_the_hold_message_tells_the_sklad_to_delete_the_dead_card(pg, tmp_path):
    """Review 🔵: the dead card stays in our catalog and keeps pulling the model (and would ship
    again the moment the list goes stale) — the hold reason says to delete it on Produkty sklad."""
    _snapshot(pg)
    _codex(pg)
    _run(pg, tmp_path, "dl1", _doc("0100000215"))
    outcome = pg.execute("SELECT outcome FROM email_events WHERE message_id='dl1' "
                         "AND detail->>'held' = 'true'").fetchone()[0]
    assert "Produkty sklad" in outcome and "zmaž" in outcome
