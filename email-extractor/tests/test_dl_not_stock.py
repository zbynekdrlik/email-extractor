"""#488 — a persistent line-level „Nie je skladová položka — vždy vynechať" rule for recurring
service lines on delivery notes / invoices taken as delivery notes (the EKVIA „PREPRAVNÉ"
transport line held EVERY weekly invoice on a `dl_item` question, because „pošli bez tejto
položky" applies to ONE mail only).

The sklad answers a `dl_item` question once with `not_stock`: a supplier-scoped rule lands in
`dl_item_memory` (sentinel, never a card, source human, audited, listed in Naučené sklad,
Kôš-restorable) and the answering mail ships without the line. Every later document of THAT
supplier excludes the line before matching — no model call, no question, no hold; another
supplier with the same wording is still asked.

Drives the REAL board endpoints + worker (`dl_worker.tick` / `release_for_question`) against a
real Postgres with scripted model answers. Synthetic data only — made-up GTINs/EANs/cards;
the supplier name/address are the shared `test_dl_worker` fixtures (as every DL test uses), no
real customer mail — this repo is public.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

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

from app.board.services import audit, rules
from app.config import Config
from app.httpapi import create_app, dl_key
from app.orders import codex_cards, dl_memory, dl_snapshot, dl_worker, memory, teach

G_ROLL = "8588000000301"
G_BREAD = "8588000000302"
ROLL = "Rožok štandart 50g"
BREAD = "Chlieb biely 500g"
SERVICE = "PREPRAVNÉ"
OTHER_SUPPLIER = "2000000000993"
NOT_STOCK = "not_stock"
LABEL = "Nie je skladová položka — vždy vynechať"

CATALOG_CSV = ("GTIN,Názov,doplnok,hmotnost,Sklad,Cena\n"
               f"{G_ROLL},{ROLL},,0.05,1,0.50\n"
               f"{G_BREAD},{BREAD},,0.5,1,0.80\n")

CODEX = [
    {"code": G_ROLL, "card_code": "301", "stredisko": 1, "sklad": 1, "name": ROLL,
     "inactive": False, "changed_at": None},
    {"code": G_BREAD, "card_code": "302", "stredisko": 1, "sklad": 1, "name": BREAD,
     "inactive": False, "changed_at": None},
]


def _snapshot(pg):
    return dl_snapshot.import_snapshot(pg, CATALOG_CSV, "GTIN,Sklad,Názov,doplnok\n",
                                       SUPPLIERS_CSV)


def _doc(doc_number):
    """A delivery with one real warehouse line and one service line (transport)."""
    lines = [{"name": ROLL, "quantity": 10, "unit": "ks", "unitPrice": 0.5,
              "totalPrice": 5.0, "vatRate": 10},
             {"name": SERVICE, "quantity": 1, "unit": "ks", "unitPrice": 12.0,
              "totalPrice": 12.0, "vatRate": 20}]
    return {"documents": [{
        "supplierName": "Pekáreň Lunys", "supplierCity": "Prešov",
        "supplierEmail": "dodavatel@lunys.sk", "docNumber": doc_number,
        "deliveryDate": _DL_DELIVERY_DATE,
        "documentTotalWithoutVAT": round(sum(x["totalPrice"] for x in lines), 2),
        "items": lines}]}


def _llm(gtin, conf=0.97):
    return {"gtin": gtin, "matchConfidence": conf, "matchReason": "scripted"}


NO_MATCH = {"gtin": "NO_MATCH", "matchConfidence": 0.0, "matchReason": "žiadna zhoda"}


def _client(doc, *item_answers):
    return FakeClient({"dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
                       "dl_item": list(item_answers)})


def _tick(pg, tmp_path, mid, client):
    """One live worker pass over a fresh DL mail `mid`. Returns (uploaded contents, posts)."""
    _msg(pg, mid=mid)
    _attach(pg, tmp_path, mid)
    uploaded, posted = [], []
    dl_worker.tick(pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)),
                   client=client,
                   upload=lambda c, name, content, dir_override=None: uploaded.append(content),
                   post=lambda c, h: posted.append(h))
    return uploaded, posted


def _open_dl_items(pg):
    return pg.execute("SELECT id, wording FROM order_questions "
                      "WHERE kind='dl_item' AND status='open' ORDER BY id").fetchall()


def _board():
    app = create_app(Config(pg_dsn=_cfg().pg_dsn, data_dir="/tmp", api_token="tok",
                            dash_password="secret", secret_key="test-secret"))
    app.testing = True
    c = app.test_client()
    c.get("/sklad-dl/" + dl_key("test-secret"))
    return c


def _answer(pg, monkeypatch, qid, body):
    """The real board answer (`/api/board/questions/<id>/answer`); its reprocess is captured
    (driven by the test where it matters). Returns (response, captured release qids)."""
    released = []
    monkeypatch.setattr(dl_worker, "release_for_question",
                        lambda conn, cfg, q, **kw: released.append(q) or [])
    r = _board().post(f"/api/board/questions/{qid}/answer", json=body)
    monkeypatch.undo()
    return r, released


def _ask(pg, supplier_ean=SUPPLIER_EAN, mid="q-src", **kw):
    """An open dl_item question for SERVICE of `supplier_ean`, on its own (already processed —
    a question is raised while its mail is processed) mail."""
    pg.execute("INSERT INTO messages (message_id, category, subject, from_addr, processed) "
               "VALUES (%s, 'dodacie_listy', 'DL', 'dodavatel@lunys.sk', true) "
               "ON CONFLICT DO NOTHING", (mid,))
    qid = teach.ask_dl_item(pg, mid, supplier_ean, "Pekáreň Lunys", SERVICE, 1, "ks",
                            [{"gtin": G_ROLL, "name": ROLL}], **kw)
    assert qid is not None
    return qid


def _teach_not_stock(pg, monkeypatch, supplier_ean=SUPPLIER_EAN, mid="q-src", **kw):
    qid = _ask(pg, supplier_ean, mid, **kw)
    r, _ = _answer(pg, monkeypatch, qid, {"choice": NOT_STOCK})
    assert r.status_code == 200, r.get_data(as_text=True)
    return qid


def _rule_rows(pg, supplier_ean=SUPPLIER_EAN, live=True):
    return pg.execute(
        "SELECT id, source, card FROM dl_item_memory WHERE supplier_ean = %s AND item_key = %s"
        " AND gtin = %s AND (deleted_at IS NULL) = %s ORDER BY id",
        (supplier_ean, memory.item_key(SERVICE), NOT_STOCK, live)).fetchall()


# --- the answer ------------------------------------------------------------------------------

def test_the_dl_item_card_offers_the_not_stock_answer(pg):
    _ask(pg)
    r = _board().get("/api/board/questions?scope=dl&status=open")
    assert r.status_code == 200
    ops = {a["op"]: a["label"] for a in r.get_json()["meta"]["card_actions"]["dl_item"]}
    assert ops.get("not_stock") == LABEL
    assert "ship_without" in ops, "the one-mail answer stays — a different decision"


def test_the_answer_learns_an_audited_supplier_rule_listed_in_naucene_sklad(pg, monkeypatch):
    qid = _ask(pg)
    r, released = _answer(pg, monkeypatch, qid, {"choice": NOT_STOCK})
    assert r.status_code == 200, r.get_data(as_text=True)
    q = teach.get(pg, qid)
    assert q["status"] == "answered" and q["answer"]["choice"] == NOT_STOCK
    assert released == [qid], "the answering mail gets its second chance, like ship_without"
    rows = _rule_rows(pg)
    assert len(rows) == 1, "exactly one live rule for (supplier, wording)"
    rid, source, card = rows[0]
    assert source == "human" and card == LABEL
    assert pg.execute(
        "SELECT count(*) FROM audit_log WHERE table_name='dl_item_memory' AND row_id=%s "
        "AND action='create' AND question_id=%s", (str(rid), qid)).fetchone()[0] == 1
    listed = rules.list_rules(pg, scope="dl", kind="dl_alias")["items"]
    row = next(x for x in listed if x["id"] == rid)
    assert row["target"] == LABEL and row["key"]["wording"] == SERVICE
    assert row["origin"]["question_id"] == qid, "Naučené links the rule to its question"
    found = rules.list_rules(pg, scope="dl", kind="dl_alias", q="vynechat")["items"]
    assert [x["id"] for x in found] == [rid], "the rule is found by its label"


def test_the_rule_is_never_read_as_a_card(pg, monkeypatch):
    """The sentinel never leaves `dl_item_memory` as a card — not even through an UNFILTERED
    resolve (no catalog filter), so the memory-conflict verdict, the CODEX guard and the ask
    pre-check can never see it as a taught gtin."""
    _teach_not_stock(pg, monkeypatch)
    assert dl_memory.resolve(pg, SUPPLIER_EAN, SERVICE, catalog_gtins=None) is None


# --- the engine --------------------------------------------------------------------------------

def test_the_answering_mail_ships_without_the_line_no_loop(pg, tmp_path, monkeypatch):
    """The first invoice is held on the service line; answering „vždy vynechať" ships THAT mail
    without it (complete, not partial) and asks nothing again."""
    _snapshot(pg)
    uploaded, _ = _tick(pg, tmp_path, "dl1", _client(_doc("0100000881"), _llm(G_ROLL), NO_MATCH))
    assert uploaded == [] and [w for _, w in _open_dl_items(pg)] == [SERVICE]
    qid = _open_dl_items(pg)[0][0]
    r, released = _answer(pg, monkeypatch, qid, {"choice": NOT_STOCK})
    assert r.status_code == 200 and released == [qid]
    shipped = []
    out = dl_worker.release_for_question(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), qid,
        client=_client(_doc("0100000881"), _llm(G_ROLL)),
        upload=lambda c, name, content, dir_override=None: shipped.append(content))
    assert len(shipped) == 1 and G_ROLL in shipped[0]
    assert out and out[0]["outcome"] == "ok", "the service line is not a missing stock item"
    assert _open_dl_items(pg) == [], "no question re-raised"


def test_the_next_document_of_the_supplier_ships_without_the_line_and_asks_nothing(
        pg, tmp_path, monkeypatch):
    _snapshot(pg)
    _teach_not_stock(pg, monkeypatch)
    client = _client(_doc("0100000882"), _llm(G_ROLL))   # ONE item answer: the roll only
    uploaded, posted = _tick(pg, tmp_path, "dl2", client)
    assert len(uploaded) == 1 and G_ROLL in uploaded[0]
    assert client.calls.count("dl_item") == 1, "the ruled line never reaches the model"
    assert _open_dl_items(pg) == [], "no question, no hold"
    assert pg.execute("SELECT processed, proc_status FROM messages WHERE message_id='dl2'"
                      ).fetchone() == (True, "ok")
    assert posted and "šlo BEZ nich" not in posted[-1]
    items = dict(pg.execute("SELECT name, rule FROM order_items").fetchall())
    assert items[SERVICE] == "not_stock", "História shows WHY the line is not on the EDI"
    assert items[ROLL] != "not_stock"


def test_another_supplier_with_the_same_wording_is_still_asked(pg, tmp_path, monkeypatch):
    _snapshot(pg)
    _teach_not_stock(pg, monkeypatch, supplier_ean=OTHER_SUPPLIER)
    uploaded, _ = _tick(pg, tmp_path, "dl3",
                        _client(_doc("0100000883"), _llm(G_ROLL), NO_MATCH))
    assert uploaded == [], "a rule of ANOTHER supplier never drops this supplier's line"
    assert [w for _, w in _open_dl_items(pg)] == [SERVICE]


def test_a_conflicting_history_and_a_live_codex_guard_never_see_the_rule(
        pg, tmp_path, monkeypatch):
    """The wording carries a contradictory human history (two cards — the #465 memory-conflict
    shape) and the CODEX guard is live (#467). The newer rule still decides the line: no
    memory-conflict question, no codex_missing hold, no model call for it."""
    _snapshot(pg)
    codex_cards.replace_cards(pg, CODEX, source_as_of=datetime.now(UTC) - timedelta(hours=1))
    dl_memory.remember(pg, SUPPLIER_EAN, SERVICE, G_ROLL, ROLL, "2026-09-01", source="human")
    dl_memory.remember(pg, SUPPLIER_EAN, SERVICE, G_BREAD, BREAD, "2026-09-02", source="human")
    # the sklad answers the memory-conflict question (both cards on screen) with the rule
    _teach_not_stock(pg, monkeypatch, memory_conflict=True)
    client = _client(_doc("0100000884"), _llm(G_ROLL))
    uploaded, _ = _tick(pg, tmp_path, "dl4", client)
    assert len(uploaded) == 1 and G_BREAD not in uploaded[0]
    assert client.calls.count("dl_item") == 1
    assert _open_dl_items(pg) == []


def test_a_newer_real_card_for_the_wording_overrides_the_rule(pg, tmp_path, monkeypatch):
    """The newest curated decision for (supplier, wording) wins — a card taught AFTER the rule
    (a Produkty alias) sends the line back to the matcher, never silently dropped by the older
    rule. (What the matcher then decides is its own business — this synthetic wording shares no
    word with the card, so the #236/#465 lexical guards keep it off the EDI; the point here is
    only that the rule no longer decides it.)"""
    _snapshot(pg)
    _teach_not_stock(pg, monkeypatch)
    assert dl_memory.add_dl_alias(pg, SUPPLIER_EAN, SERVICE, G_BREAD, BREAD) is not None
    client = _client(_doc("0100000885"), _llm(G_ROLL), _llm(G_BREAD))
    _tick(pg, tmp_path, "dl5", client)
    assert client.calls.count("dl_item") == 2, "the line reached the model again"
    items = dict(pg.execute("SELECT name, rule FROM order_items").fetchall())
    assert items[SERVICE] != "not_stock"


# --- taking it back --------------------------------------------------------------------------

def test_undoing_the_answer_removes_the_rule_and_the_line_is_asked_again(
        pg, tmp_path, monkeypatch):
    _snapshot(pg)
    qid = _teach_not_stock(pg, monkeypatch)
    rid = _rule_rows(pg)[0][0]
    r = _board().post(f"/api/board/questions/{qid}/undo")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert teach.get(pg, qid)["status"] == "open"
    assert _rule_rows(pg) == [] and [x[0] for x in _rule_rows(pg, live=False)] == [rid], \
        "soft-deleted (Kôš), never a hard delete"
    assert pg.execute(
        "SELECT count(*) FROM audit_log WHERE table_name='dl_item_memory' AND row_id=%s "
        "AND action='delete' AND question_id=%s", (str(rid), qid)).fetchone()[0] == 1
    uploaded, _ = _tick(pg, tmp_path, "dl6",
                        _client(_doc("0100000886"), _llm(G_ROLL), NO_MATCH))
    assert uploaded == [], "no rule any more — the line is held + asked again"
    # answering again revives the SAME rule row (its UNIQUE identity), audited again
    _answer(pg, monkeypatch, qid, {"choice": NOT_STOCK})
    assert [x[0] for x in _rule_rows(pg)] == [rid]


def test_the_kos_removes_the_rule_but_the_answering_mail_keeps_its_ship_without(
        pg, tmp_path, monkeypatch):
    """Kôš „Vrátiť" on the rule's `create` row soft-deletes it: a NEW mail is asked again. The
    answering mail itself keeps its own answer (like ship_without) — its reprocess ships
    without the line and raises nothing."""
    _snapshot(pg)
    _tick(pg, tmp_path, "dl7", _client(_doc("0100000887"), _llm(G_ROLL), NO_MATCH))
    qid = _open_dl_items(pg)[0][0]
    _answer(pg, monkeypatch, qid, {"choice": NOT_STOCK})
    rid = _rule_rows(pg)[0][0]
    created = pg.execute("SELECT id FROM audit_log WHERE table_name='dl_item_memory' "
                         "AND row_id=%s AND action='create'", (str(rid),)).fetchone()[0]
    assert audit.restore(pg, created, by="admin") is True
    assert _rule_rows(pg) == []
    shipped = []
    dl_worker.release_for_question(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), qid,
        client=_client(_doc("0100000887"), _llm(G_ROLL), NO_MATCH),
        upload=lambda c, name, content, dir_override=None: shipped.append(content))
    assert len(shipped) == 1 and _open_dl_items(pg) == []
    uploaded, _ = _tick(pg, tmp_path, "dl8",
                        _client(_doc("0100000888"), _llm(G_ROLL), NO_MATCH))
    assert uploaded == [] and [w for _, w in _open_dl_items(pg)] == [SERVICE]


# --- review round 1 (same branch) --------------------------------------------------------------

def test_a_document_of_only_ruled_lines_is_a_silent_not_warehouse_skip(pg, tmp_path, monkeypatch):
    """A transport-only document of a ruled supplier has nothing for the warehouse: a terminal,
    digest-visible `not_warehouse` skip — never a ❗ „0 z 0" review nobody can answer, never a
    question, never an upload, never a model call for the line."""
    _snapshot(pg)
    _teach_not_stock(pg, monkeypatch)
    doc = _doc("0100000889")
    doc["documents"][0]["items"] = doc["documents"][0]["items"][1:]          # the service line
    doc["documents"][0]["documentTotalWithoutVAT"] = 12.0
    client = _client(doc)
    uploaded, posted = _tick(pg, tmp_path, "dl9", client)
    assert uploaded == [] and posted == [], "nothing to ship, nothing to tell the sklad"
    assert "dl_item" not in client.calls
    assert _open_dl_items(pg) == []
    assert pg.execute("SELECT processed, proc_status FROM messages WHERE message_id='dl9'"
                      ).fetchone() == (True, "not_warehouse")
    outcome = pg.execute("SELECT outcome FROM email_events WHERE message_id='dl9' "
                         "AND stage='not_warehouse'").fetchone()[0]
    assert "neskladové" in outcome


def test_re_asserting_a_dormant_rule_is_audited_and_its_undo_only_reverts_the_re_assert(
        pg, monkeypatch):
    """A card taught after the rule makes it dormant; a later question answered „vždy vynechať"
    again RE-ASSERTS it (the newest decision) — audited as an `update`. Undoing THAT answer puts
    the rule back to dormant; the rule the first question created stays."""
    q1 = _teach_not_stock(pg, monkeypatch)
    rid = _rule_rows(pg)[0][0]
    assert dl_memory.add_dl_alias(pg, SUPPLIER_EAN, SERVICE, G_BREAD, BREAD) is not None
    key = memory.item_key(SERVICE)
    assert dl_memory.not_stock_keys(pg, SUPPLIER_EAN) == set(), "the newer card wins"
    q2 = _teach_not_stock(pg, monkeypatch, mid="q-src2", memory_conflict=True)
    assert dl_memory.not_stock_keys(pg, SUPPLIER_EAN) == {key}, "re-asserted as newest"
    assert [x[0] for x in _rule_rows(pg)] == [rid], "never a second rule row"
    assert pg.execute("SELECT action, question_id FROM audit_log WHERE table_name="
                      "'dl_item_memory' AND row_id=%s ORDER BY id", (str(rid),)
                      ).fetchall() == [("create", q1), ("update", q2)]
    r = _board().post(f"/api/board/questions/{q2}/undo")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert [x[0] for x in _rule_rows(pg)] == [rid], "q1's rule stays"
    assert dl_memory.not_stock_keys(pg, SUPPLIER_EAN) == set(), "dormant again under the card"
    assert teach.get(pg, q1)["status"] == "answered"


def test_undoing_a_plain_card_answer_never_removes_the_rule(pg, monkeypatch):
    """The plain dl_item undo deletes the human rows of its wording — never the rule, which
    leaves only by its own undo / the Kôš / Naučené."""
    _teach_not_stock(pg, monkeypatch)
    q2 = _ask(pg, mid="q-src2")          # the rule is never a taught card: asked again
    r, _ = _answer(pg, monkeypatch, q2, {"choice": G_ROLL})
    assert r.status_code == 200, r.get_data(as_text=True)
    r = _board().post(f"/api/board/questions/{q2}/undo")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert len(_rule_rows(pg)) == 1, "the rule survived another question's undo"


def test_naucene_refuses_typing_the_sentinel_into_an_alias(pg, monkeypatch):
    """A typed number never turns a card alias into a silent-drop rule (no confirmation there);
    editing the rule row itself (its wording) still works."""
    rid = dl_memory.add_dl_alias(pg, SUPPLIER_EAN, "Rožok", G_ROLL, ROLL)
    c = _board()
    r = c.post(f"/api/board/rules/dl_alias/{rid}",
               json={"wording": "Rožok", "gtin": NOT_STOCK, "card": ""})
    assert r.status_code == 400, r.get_data(as_text=True)
    assert pg.execute("SELECT gtin FROM dl_item_memory WHERE id=%s", (rid,)).fetchone()[0] \
        == G_ROLL
    _teach_not_stock(pg, monkeypatch)
    rule = _rule_rows(pg)[0][0]
    # review 2: nor is a rule retargeted to another wording (a real stock line would then be
    # dropped silently) or turned into a typed number — delete + answer instead
    for body in ({"wording": ROLL, "gtin": NOT_STOCK, "card": LABEL},
                 {"wording": SERVICE, "gtin": G_ROLL, "card": ROLL}):
        r = c.post(f"/api/board/rules/dl_alias/{rule}", json=body)
        assert r.status_code == 400, r.get_data(as_text=True)
    assert dl_memory.not_stock_keys(pg, SUPPLIER_EAN) == {memory.item_key(SERVICE)}
    r = c.post(f"/api/board/rules/dl_alias/{rule}",       # the label text alone may change
               json={"wording": SERVICE, "gtin": NOT_STOCK, "card": "Doprava — vynechať"})
    assert r.status_code == 200, r.get_data(as_text=True)


def test_the_rule_leaves_the_line_off_in_shadow_too(pg, tmp_path, monkeypatch):
    """Shadow (the preview / eval path) reads the learned rule like human memory: the line is
    off, no model call for it — and shadow still writes nothing."""
    _snapshot(pg)
    _teach_not_stock(pg, monkeypatch)
    _msg(pg, mid="dl10")
    _attach(pg, tmp_path, "dl10")
    client = _client(_doc("0100000890"), _llm(G_ROLL))
    uploaded, posted = [], []
    dl_worker.tick(pg, _cfg(delivery_notes_engine="n8n", delivery_notes_shadow=True,
                            data_dir=str(tmp_path)), client=client,
                   upload=lambda *a, **k: uploaded.append(a),
                   post=lambda *a, **k: posted.append(a))
    assert uploaded == [] and posted == []
    assert client.calls.count("dl_item") == 1
    run = pg.execute("SELECT id, shadow, result->'documents'->0->>'outcome' FROM order_runs"
                     ).fetchone()
    assert run[1] is True and run[2] == "ok"
    items = dict(pg.execute("SELECT name, rule FROM order_items WHERE run_id=%s",
                            (run[0],)).fetchall())
    assert items[SERVICE] == "not_stock"


def test_weekly_invoices_of_a_ruled_supplier_ship_without_the_service_line(
        pg, tmp_path, monkeypatch):
    """The ticket's own flow (#485 invoice taken as a delivery note): two invoices of one
    supplier hold on ONE shared transport-line question; „vždy vynechať" re-queues and ships
    BOTH without it; next week's invoice ships with the line never reaching the model and no
    question."""
    from test_invoice_dedup_edges import _ago
    from test_invoice_dedup_regression import ITEM_MATCHED as INV_ITEM
    from test_invoice_dedup_regression import FakeClient as InvoiceClient
    from test_invoice_dedup_regression import _message, _push_receipts, _setup, _supplier
    from test_invoice_dedup_regression import _tick as _invoice_tick
    from test_invoice_dedup_requeue import TWO_DAYS_AGO, YESTERDAY, _lines, _wire_release
    from test_invoice_dedup_requeue import _answer as _invoice_answer

    _setup(pg)
    _message(pg, tmp_path, "inv-b", created_at=_ago(hours=3))
    _message(pg, tmp_path, "inv-a", created_at=_ago(hours=2))
    _push_receipts(tmp_path)
    doc_a = _lines(YESTERDAY, "4400000002", "2400000002", ["Rožok 50g", SERVICE])
    doc_b = _lines(TWO_DAYS_AGO, "4400000001", "2400000001", ["Rožok 50g", SERVICE])
    doc_b["documents"][0]["items"][0].update(quantity=30, totalPrice=15.0)
    doc_b["documents"][0]["documentTotalWithoutVAT"] = 20.0
    client = InvoiceClient([doc_a, doc_b, doc_a, doc_b], runs=4)
    client._answers["dl_item"] = [INV_ITEM, NO_MATCH, INV_ITEM, NO_MATCH]
    uploads, posts = [], []
    assert _invoice_tick(pg, tmp_path, client, uploads, posts) == 1
    assert _invoice_tick(pg, tmp_path, client, uploads, posts) == 1
    assert uploads == [] and len(_open_dl_items(pg)) == 1, "both held on ONE question"
    _wire_release(monkeypatch, client, uploads, posts)
    client._answers["dl_item"] = [INV_ITEM] * 2          # the service line is never matched
    _invoice_answer(pg, tmp_path, SERVICE, choice=teach.DL_ITEM_NOT_STOCK)
    _push_receipts(tmp_path)
    for _ in range(4):
        _invoice_tick(pg, tmp_path, client, uploads, posts)
    assert len(uploads) == 2, "both held invoices shipped without the transport line"
    assert _open_dl_items(pg) == []
    _message(pg, tmp_path, "inv-c", created_at=_ago(minutes=30))
    doc_c = _lines(_ago(days=3), "4400000003", "2400000003", ["Rožok 50g", SERVICE])
    client._answers["dl_documents"].append(doc_c)
    client._answers["dl_item"] = [INV_ITEM]
    client._answers["dl_supplier"] = [_supplier()]
    calls_before = client.calls.count("dl_item")
    _push_receipts(tmp_path)
    assert _invoice_tick(pg, tmp_path, client, uploads, posts) == 1
    assert len(uploads) == 3, "next week's invoice ships at once"
    assert client.calls.count("dl_item") - calls_before == 1, "only the stock line is matched"
    assert _open_dl_items(pg) == []


# --- review round 2 (same branch) --------------------------------------------------------------

def test_undoing_an_older_answer_never_removes_a_rule_a_later_answer_owns(pg, monkeypatch):
    """q1 learns the rule, the Kôš removes it, q2 learns it again (the same row revived). Undoing
    q1 now must leave q2's rule alone — q1's write is no longer the newest on that row."""
    q1 = _teach_not_stock(pg, monkeypatch)
    rid = _rule_rows(pg)[0][0]
    created = pg.execute("SELECT id FROM audit_log WHERE table_name='dl_item_memory' "
                         "AND row_id=%s AND action='create'", (str(rid),)).fetchone()[0]
    assert audit.restore(pg, created, by="admin") is True
    assert _rule_rows(pg) == []
    q2 = _teach_not_stock(pg, monkeypatch, mid="q-src2")
    assert [x[0] for x in _rule_rows(pg)] == [rid], "the same row revived for q2"
    r = _board().post(f"/api/board/questions/{q1}/undo")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert [x[0] for x in _rule_rows(pg)] == [rid], "q2's rule survived q1's undo"
    assert teach.get(pg, q2)["status"] == "answered"


def test_a_repeat_kos_restore_of_a_re_assert_is_a_clean_409(pg, monkeypatch):
    """The Kôš „already reverted?" guard compares the timestamp with its recorded ISO string as a
    time — a second „Vrátiť" of the same re-assert is refused, never a second write."""
    import pytest

    _teach_not_stock(pg, monkeypatch)
    rid = _rule_rows(pg)[0][0]
    assert dl_memory.add_dl_alias(pg, SUPPLIER_EAN, SERVICE, G_BREAD, BREAD) is not None
    _teach_not_stock(pg, monkeypatch, mid="q-src2", memory_conflict=True)
    upd = pg.execute("SELECT id FROM audit_log WHERE table_name='dl_item_memory' AND row_id=%s "
                     "AND action='update'", (str(rid),)).fetchone()[0]
    assert audit.restore(pg, upd, by="admin") is True
    assert dl_memory.not_stock_keys(pg, SUPPLIER_EAN) == set(), "dormant again"
    with pytest.raises(audit.RestoreError) as e:
        audit.restore(pg, upd, by="admin")
    assert e.value.status == 409
    assert pg.execute("SELECT count(*) FROM audit_log WHERE action='restore' AND row_id=%s",
                      (str(rid),)).fetchone()[0] == 1
