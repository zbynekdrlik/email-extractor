"""DL worker (#204, DL migration F5) — worker loop, shadow mode, announced-vs-attached
(spec §4), duplicate visibility (W7), R17/W9 retry semantics. All fixtures below are
SYNTHETIC — constructed to match the documented template shapes, never real customer
mail (this repo is public).
"""
from __future__ import annotations

import os
import threading
import time

import psycopg
import pytest
from psycopg.types.json import Json

from app import store
from app.config import Config
from app.orders import desadv, dl_memory, dl_snapshot, dl_supplier_memory, dl_worker, reliability

DL_CATALOG_CSV = ("GTIN,Názov,doplnok,hmotnost,Sklad,Cena\n"
                  "8588000000001,Rožok 50g,,0.05,1,0.50\n")
OBJ_CATALOG_CSV = "GTIN,Sklad,Názov,doplnok\n"
SUPPLIERS_CSV = ("Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,"
                 "Číslo mobilu,E-mail\n"
                 "Pekáreň Lunys,2000000000864,Prešov,Košútka 1,,,dodavatel@lunys.sk\n")

SUPPLIER_EAN = "2000000000864"
ITEM_GTIN = "8588000000001"


def _cfg(**kw):
    # #240: release_for_question() opens its own separate DB connection (the sibling-
    # question advisory lock) via cfg.pg_dsn — a bare "" here would connect via psycopg's
    # own libpq env defaults instead of the actual test Postgres, so every call site now
    # needs the real DSN, same convention test_orders_teach_kinds.py's own _cfg() (which
    # exercises hold.py's identical dual-connection pattern) already uses.
    base = dict(pg_dsn=os.environ.get("PG_TEST_DSN", ""), data_dir="/tmp",
                delivery_notes_engine="n8n", delivery_notes_shadow=False)
    base.update(kw)
    return Config(**base)


def _snapshot(pg):
    return dl_snapshot.import_snapshot(pg, DL_CATALOG_CSV, OBJ_CATALOG_CSV, SUPPLIERS_CSV)


def _msg(pg, mid="dl1", subject="Dodací list", from_addr="dodavatel@lunys.sk",
        has_attachments=True):
    pg.execute(
        """INSERT INTO messages (message_id, category, subject, from_addr,
                                 combined_text, has_attachments, processed)
           VALUES (%s, 'dodacie_listy', %s, %s, '', %s, false)""",
        (mid, subject, from_addr, has_attachments))
    return mid


def _attach(pg, tmp_path, mid, idx=0, filename="dl.pdf", text="dodaci list text",
           mime="application/pdf", method=""):
    # #247: `method` mirrors `app/extract.py`'s own ingest-time classification stored
    # on the real `attachments.method` column (e.g. `'skipped'` for a decorative/tiny
    # image, `flag='skipped_tiny_image'`) — default "" matches every PRE-#247 caller
    # unchanged (NULL/"" both read back as "" via `_read_attachments`'s `method or ""`).
    pg.execute(
        """INSERT INTO attachments (message_id, idx, filename, mime, extracted_text, method)
           VALUES (%s, %s, %s, %s, %s, %s)""", (mid, idx, filename, mime, text, method))
    d = store.message_dir(str(tmp_path), mid)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"att{idx}__{filename}").write_bytes(b"%PDF-1.4 no embedded jpeg here\n")


def _doc(doc_number="0100000001", total=5.0, items=None):
    return {"documents": [{
        "supplierName": "Pekáreň Lunys", "supplierCity": "Prešov",
        "supplierEmail": "dodavatel@lunys.sk", "docNumber": doc_number,
        "deliveryDate": "01.08.2026", "documentTotalWithoutVAT": total,
        "items": items or [{"name": "Rožok 50g", "quantity": 10, "unit": "ks",
                            "unitPrice": 0.5, "totalPrice": 5.0, "vatRate": 10}]}]}


SUPPLIER_MATCHED = {"matched": True, "ean_edi": SUPPLIER_EAN, "name": "Pekáreň Lunys",
                    "matchConfidence": 0.95, "matchReason": "presná zhoda"}
ITEM_MATCHED = {"gtin": ITEM_GTIN, "matchedCatalogName": "Rožok 50g",
                "matchConfidence": 0.97, "matchReason": "presná zhoda", "mass": 0.05}


class FakeClient:
    """Scripted answers keyed by the `name=` the worker's json_call passes — a FIFO
    queue per name, so a document with several items can script several `dl_item`
    answers in a row."""

    def __init__(self, answers: dict[str, list[dict]]):
        self._answers = {k: list(v) for k, v in answers.items()}
        self.calls: list[str] = []
        self.last_prompt_hash = ""

    def json_call(self, system, user, schema, name="result"):
        self.calls.append(name)
        self.last_prompt_hash = name
        queue = self._answers.get(name)
        if not queue:
            raise AssertionError(f"no scripted answer left for {name!r}")
        return queue.pop(0)

    def vision_call(self, *a, **kw):
        raise AssertionError("vision must not be called when machine_text is present (W13)")


class RaisingClient:
    """Extraction succeeds; the SUPPLIER match call raises — used for R17/W9 retry
    semantics tests."""

    def __init__(self, message: str, doc_number="0100000009"):
        self.message = message
        self.calls = 0
        self.last_prompt_hash = ""

    def json_call(self, system, user, schema, name="result"):
        self.calls += 1
        self.last_prompt_hash = name
        if name == "dl_documents":
            return _doc(doc_number="0100000009")
        raise Exception(self.message)

    def vision_call(self, *a, **kw):
        raise AssertionError


# --- engine/shadow gating (mirrors static_worker's own defaults-must-do-nothing test) --

def test_default_engine_and_shadow_touch_nothing(pg, tmp_path):
    _snapshot(pg)
    _msg(pg)
    _attach(pg, tmp_path, "dl1")
    assert dl_worker.tick(pg, _cfg(data_dir=str(tmp_path)),
                          client=FakeClient({})) == 0
    row = pg.execute("SELECT processing_at, processed FROM messages").fetchone()
    assert row == (None, False)
    assert pg.execute("SELECT count(*) FROM order_runs").fetchone()[0] == 0


def test_resolve_engine_is_reused_from_worker():
    with pytest.raises(ValueError):
        dl_worker.resolve_engine("postgres-please")
    assert dl_worker.resolve_engine("python") == "python"
    assert dl_worker.resolve_engine("") == "n8n"


def test_idle_without_a_catalog_snapshot(pg):
    _msg(pg)
    cfg = _cfg(delivery_notes_engine="python")
    assert dl_worker.tick(pg, cfg, client=FakeClient({})) == 0


# --- R15: no usable attachment is a review, not an error ---------------------

def test_no_attachment_at_all_is_review_not_error(pg, tmp_path):
    _snapshot(pg)
    _msg(pg, has_attachments=False)
    posted = []
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(pg, cfg, client=FakeClient({}), post=lambda c, h: posted.append(h))
    assert n == 1
    row = pg.execute(
        "SELECT processed, processed_by FROM messages WHERE message_id='dl1'").fetchone()
    assert row == (True, "dodacie_listy")
    assert len(posted) == 1 and "bez prílohy" in posted[0]


def test_a_non_pdf_non_image_attachment_is_treated_as_no_usable_attachment(pg, tmp_path):
    _snapshot(pg)
    _msg(pg)
    _attach(pg, tmp_path, "dl1", filename="podpis.docx",
           mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    n = dl_worker.tick(pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)),
                       client=FakeClient({}))
    assert n == 1
    row = pg.execute("SELECT processed FROM messages WHERE message_id='dl1'").fetchone()
    assert row[0] is True


# --- #247: a decorative/tiny attachment (extract.py's own ingest-time
# method='skipped') must never reach dl_extract/vision at all ----------------

def test_a_decorative_skipped_attachment_never_reaches_vision_and_reviews_cleanly(
        pg, tmp_path):
    """Live incident: ALL 13 stored HK LOAN (gnip@hkloan.eu) attachments are the exact
    same 2472-byte/150x76px signature logo, and `extract.py`'s own ingest already
    classifies every one of them `method='skipped'` (`flag='skipped_tiny_image'`).
    `dl_worker._read_attachments` used to hand its raw bytes to `dl_extract` anyway --
    `dl_extract.extract_attachment` has no image-vs-PDF distinction of its own (its
    module docstring leaves "attachment selection" entirely to this worker), so a tiny
    image with no machine_text falls into the "digital PDF, no text -> vision fallback"
    branch and sends the raw JPEG bytes to OpenAI labelled as a PDF file
    (`llm.vision_call(pdf_bytes=...)`) -- which OpenAI rejects with a 400 "could not be
    processed", exactly the log line `DL attachment idx=0 filename='00000I0G.jpeg'
    failed to extract` from the ticket. Because this is the message's ONLY attachment,
    no document is ever produced, so `_process_document` (where supplier lookup runs)
    is never even called -- the pipeline "crashes" before it gets there.

    FakeClient({})'s `vision_call` always raises `AssertionError` -- if the worker
    still calls it for a skipped attachment, that raised message leaks into the Odoo
    review reason instead of a clean, actionable one.
    """
    _snapshot(pg)
    _msg(pg, mid="dl1", from_addr="gnip@example-supplier.sk",
        subject="Avizacia G-P")
    _attach(pg, tmp_path, "dl1", filename="00000I0G.jpeg", mime="image/jpeg",
           text="", method="skipped")
    posted = []
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(pg, cfg, client=FakeClient({}),
                       post=lambda c, h: posted.append(h))
    assert n == 1
    row = pg.execute(
        "SELECT processed, processed_by FROM messages WHERE message_id='dl1'").fetchone()
    assert row == (True, "dodacie_listy")
    assert len(posted) == 1
    # the crash symptom must be GONE -- no LLM/vision failure text leaking through
    assert "vision must not be called" not in posted[0]
    assert "sa nepodarilo spracovať" not in posted[0]
    # a clear, actionable reason instead (a warehouse-readable review, never silent)
    assert any(w in posted[0] for w in ("drobný", "logo", "podpis"))


def test_a_real_attachment_still_passes_alongside_a_decorative_one(pg, tmp_path):
    """Owner's own acceptance criterion on #247: 'ak je v maili aj použiteľná
    príloha, doklad musí prejsť z nej' -- a decorative attachment sitting next to a
    real, usable one must not affect the real one's processing at all."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1", idx=0, filename="podpis.jpeg", mime="image/jpeg",
           text="", method="skipped")
    _attach(pg, tmp_path, "dl1", idx=1, filename="dl.pdf", mime="application/pdf",
           text="dodaci list text", method="pdf-text")
    client = FakeClient({"dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED]})
    uploaded, posted = [], []
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(
        pg, cfg, client=client,
        upload=lambda c, name, content, dir_override=None:
            uploaded.append((name, content, dir_override)),
        post=lambda c, html: posted.append(html))
    assert n == 1
    assert len(uploaded) == 1
    assert uploaded[0][0].startswith("Z-DESADV_")
    assert "dl_documents" in client.calls
    row = pg.execute("SELECT processed FROM messages WHERE message_id='dl1'").fetchone()
    assert row[0] is True


# --- live engine: the happy path --------------------------------------------

def test_live_engine_matches_uploads_and_marks_processed(pg, tmp_path):
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    client = FakeClient({"dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED]})
    uploaded, posted = [], []
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(
        pg, cfg, client=client,
        upload=lambda c, name, content, dir_override=None:
            uploaded.append((name, content, dir_override)),
        post=lambda c, html: posted.append(html))

    assert n == 1
    assert len(uploaded) == 1
    assert uploaded[0][0].startswith("Z-DESADV_")
    assert uploaded[0][2] == cfg.orion_dl_dir
    assert len(posted) == 1
    assert "spracovaný a nahratý do ORIONu" in posted[0]
    assert "Rožok 50g" in posted[0]

    row = pg.execute(
        "SELECT processed, processed_by, processing_at FROM messages "
        "WHERE message_id='dl1'").fetchone()
    assert row[0] is True and row[1] == "dodacie_listy" and row[2] is None

    assert pg.execute(
        "SELECT count(*) FROM desadv_sent WHERE supplier_ean=%s AND doc_number=%s "
        "AND uploaded_at IS NOT NULL", (SUPPLIER_EAN, "0100000001")).fetchone()[0] == 1
    assert pg.execute(
        "SELECT count(*) FROM dl_item_memory WHERE source='ship'").fetchone()[0] == 1

    run = pg.execute("SELECT status, shadow, snapshot_id, result->>'kind' "
                     "FROM order_runs").fetchone()
    assert run[0] == "ok" and run[1] is False and run[2] is None and run[3] == "dl"

    items = pg.execute("SELECT gtin, rule FROM order_items").fetchall()
    assert items == [(ITEM_GTIN, "llm_sure")]


# --- duplicate document (W7): visible, never silent -------------------------

def test_duplicate_document_is_skipped_visibly_not_silently(pg, tmp_path):
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    desadv.claim_send(pg, SUPPLIER_EAN, "0100000001", "already.txt")
    desadv.confirm_sent(pg, SUPPLIER_EAN, "0100000001")

    client = FakeClient({"dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED]})
    uploaded, posted = [], []
    n = dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), client=client,
        upload=lambda *a, **k: uploaded.append(a), post=lambda c, h: posted.append(h))

    assert n == 1
    assert uploaded == [], "a duplicate must never re-upload"
    assert not any("spracovaný a nahratý do ORIONu" in h for h in posted), \
        "R32: a duplicate is quiet — no immediate success message either"
    ev = pg.execute(
        "SELECT stage, rollup FROM email_events WHERE message_id='dl1' "
        "AND stage='duplicate_skip'").fetchone()
    assert ev == ("duplicate_skip", False)
    row = pg.execute("SELECT processed FROM messages WHERE message_id='dl1'").fetchone()
    assert row[0] is True


# --- #216: retry after a partial ship must not log a FALSE duplicate_skip ----

def test_retry_after_partial_ship_logs_a_self_retry_not_a_false_duplicate(pg, tmp_path):
    """A message with 2+ documents: document A already shipped (claimed + confirmed by
    THIS SAME message_id, e.g. from an earlier attempt that then hit R17's transient
    retry on a later document and got re-claimed for a fresh pass). Re-processing
    document A on the retry must be reported as a SELF-retry, never counted the same
    way as a genuine W7 cross-message duplicate — the whole point of desadv_sent
    gaining a message_id column (#216)."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    desadv.claim_send(pg, SUPPLIER_EAN, "0100000001", "already.txt", message_id="dl1")
    desadv.confirm_sent(pg, SUPPLIER_EAN, "0100000001")

    client = FakeClient({"dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED]})
    uploaded, posted = [], []
    n = dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), client=client,
        upload=lambda *a, **k: uploaded.append(a), post=lambda c, h: posted.append(h))

    assert n == 1
    assert uploaded == [], "a self-retry must never re-upload"
    ev = pg.execute(
        "SELECT stage, rollup FROM email_events WHERE message_id='dl1' "
        "AND stage='already_shipped_this_run'").fetchone()
    assert ev == ("already_shipped_this_run", False)
    dup_count = pg.execute(
        "SELECT count(*) FROM email_events WHERE message_id='dl1' "
        "AND stage='duplicate_skip'").fetchone()[0]
    assert dup_count == 0, \
        "a self-retry must NOT ALSO be logged as a genuine cross-message duplicate"

    stats = reliability.dl_provenance_stats_for_day(pg)
    assert stats["duplicates"] == 0, \
        "the daily digest's duplicates bucket must not count a message retrying itself"


def test_a_genuine_cross_message_duplicate_still_counts_when_claimant_is_unknown(pg,
                                                                                 tmp_path):
    """A legacy claim with NO recorded message_id (predates #216, or was claimed by a
    process that never passed one) must still be reported — and COUNTED — as a real
    duplicate: the atomic claim/identify read reports "" for it, which can never equal
    a real message_id, so the safe default stays 'genuine duplicate', never silently
    downgraded to a self-retry it cannot actually prove. NOTE: this specific setup
    (no message_id passed to the pre-seeded claim) is a valid call under BOTH the old
    and new `desadv.claim_send()` signature, so on its own it does not discriminate
    pre-#216 from post-#216 behaviour — see the sibling test right below, which pins
    a KNOWN but DIFFERENT claimant, for a case that actually exercises the comparison
    itself. `test_retry_after_partial_ship_logs_a_self_retry_not_a_false_duplicate`
    above is the one that is genuinely RED pre-fix (fails on TypeError)."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    desadv.claim_send(pg, SUPPLIER_EAN, "0100000001", "already.txt")   # no message_id
    desadv.confirm_sent(pg, SUPPLIER_EAN, "0100000001")

    client = FakeClient({"dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED]})
    n = dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), client=client,
        upload=lambda *a, **k: None, post=lambda c, h: None)

    assert n == 1
    stats = reliability.dl_provenance_stats_for_day(pg)
    assert stats["duplicates"] == 1


def test_a_genuine_cross_message_duplicate_still_counts_with_a_known_different_claimant(
        pg, tmp_path):
    """The comparison itself, not just the "unknown claimant" fallback: a DIFFERENT,
    KNOWN message_id ("dl0") already holds the claim while THIS message ("dl1") is
    being processed. A wrong/inverted equality check (e.g. always True, or comparing
    the wrong fields) would fail this test even on the fixed code — unlike the
    unknown-claimant sibling above, this one genuinely discriminates the comparison
    logic in `dl_worker.py`'s `if holder == message["message_id"]:` check."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    desadv.claim_send(pg, SUPPLIER_EAN, "0100000001", "already.txt", message_id="dl0")
    desadv.confirm_sent(pg, SUPPLIER_EAN, "0100000001")

    client = FakeClient({"dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED]})
    n = dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), client=client,
        upload=lambda *a, **k: None, post=lambda c, h: None)

    assert n == 1
    ev = pg.execute(
        "SELECT stage FROM email_events WHERE message_id='dl1' "
        "AND stage='duplicate_skip'").fetchone()
    assert ev == ("duplicate_skip",)
    none = pg.execute(
        "SELECT count(*) FROM email_events WHERE message_id='dl1' "
        "AND stage='already_shipped_this_run'").fetchone()[0]
    assert none == 0
    stats = reliability.dl_provenance_stats_for_day(pg)
    assert stats["duplicates"] == 1


# --- supplier / item not matched: nástenka questions, never silent ----------

def test_unmatched_supplier_raises_a_nastenka_question_and_reviews(pg, tmp_path):
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    client = FakeClient({
        "dl_documents": [_doc()],
        "dl_supplier": [{"matched": False, "matchReason": "nie je v zozname"}]})
    posted = []
    n = dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), client=client,
        post=lambda c, h: posted.append(h))
    assert n == 1
    q = pg.execute(
        "SELECT kind, message_id FROM order_questions WHERE kind='dl_supplier'").fetchone()
    assert q == ("dl_supplier", "dl1")
    assert any("nie je v zozname" in h for h in posted)
    row = pg.execute("SELECT processed FROM messages WHERE message_id='dl1'").fetchone()
    assert row[0] is True


# --- dashboard link only when there is real board action (#229 follow-up 2) --

def test_a_clean_ok_run_carries_no_dashboard_link(pg, tmp_path):
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    client = FakeClient({"dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED]})
    posted = []
    dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path),
                dashboard_base_url="http://x.example"), client=client,
        upload=lambda *a, **k: None, post=lambda c, h: posted.append(h))
    assert len(posted) == 1
    assert "<a href" not in posted[0], \
        "a clean, fully-resolved DL must not carry a dashboard link"


def test_an_unmatched_supplier_review_run_carries_the_dashboard_link(pg, tmp_path):
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    client = FakeClient({
        "dl_documents": [_doc()],
        "dl_supplier": [{"matched": False, "matchReason": "nie je v zozname"}]})
    posted = []
    dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path),
                dashboard_base_url="http://x.example"), client=client,
        post=lambda c, h: posted.append(h))
    assert len(posted) == 1
    assert "<a href" in posted[0], \
        "a review outcome always needs somewhere to go resolve it"


def test_the_dashboard_link_is_the_dl_only_nastenka_never_the_orders_one(pg, tmp_path):
    """#231: DL Odoo review messages must point at `/sklad-dl/<key>` (the DELIVERY-NOTES-
    only board) — never `/sklad/<key>` (the AI-orders board, `report.sklad_link`), which
    is exactly what a DL message linked to before this ticket."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    client = FakeClient({
        "dl_documents": [_doc()],
        "dl_supplier": [{"matched": False, "matchReason": "nie je v zozname"}]})
    posted = []
    dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path),
                dashboard_base_url="http://x.example"), client=client,
        post=lambda c, h: posted.append(h))
    assert len(posted) == 1
    assert "/sklad-dl/" in posted[0], posted[0]
    # the path segment is exact — "/sklad-dl/" must not be reachable by a stray substring
    # match against "/sklad/" (e.g. "sklad/dl-..."), so also pin the OLD orders link is gone
    assert "http://x.example/sklad/" not in posted[0], posted[0]


def test_unmatched_item_ships_a_partial_edi_and_raises_a_nastenka_question(pg, tmp_path):
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    doc = _doc(total=8.0, items=[
        {"name": "Rožok 50g", "quantity": 10, "unit": "ks", "unitPrice": 0.5,
         "totalPrice": 5.0},
        {"name": "Neznámy chlebík", "quantity": 3, "unit": "ks", "unitPrice": 1.0,
         "totalPrice": 3.0}])
    client = FakeClient({
        "dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
        "dl_item": [ITEM_MATCHED,
                   {"gtin": "NO_MATCH", "matchConfidence": 0.0,
                    "matchReason": "žiadna zhoda"}]})
    uploaded, posted = [], []
    n = dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), client=client,
        upload=lambda c, name, content, dir_override=None: uploaded.append(name),
        post=lambda c, h: posted.append(h))
    assert n == 1
    assert len(uploaded) == 1, "R81: partial EDI still ships"
    success = [h for h in posted if "spracovaný ČIASTOČNE" in h][0]
    assert "ČIASTOČNE" in success
    assert "Nespárované" in success and "Neznámy chlebík" in success
    q = pg.execute(
        "SELECT kind FROM order_questions WHERE kind='dl_item'").fetchone()
    assert q is not None


# --- shadow mode: never claims, uploads, teaches, or marks -------------------

def test_shadow_never_claims_uploads_or_teaches(pg, tmp_path):
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    client = FakeClient({"dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED]})
    uploaded, posted = [], []
    cfg = _cfg(delivery_notes_engine="n8n", delivery_notes_shadow=True,
              data_dir=str(tmp_path))
    n = dl_worker.tick(pg, cfg, client=client,
                       upload=lambda *a, **k: uploaded.append(a),
                       post=lambda *a, **k: posted.append(a))
    assert n == 1
    assert uploaded == [] and posted == []
    row = pg.execute(
        "SELECT processed, processing_at, attempts FROM messages "
        "WHERE message_id='dl1'").fetchone()
    assert row == (False, None, 0)
    assert pg.execute("SELECT count(*) FROM desadv_sent").fetchone()[0] == 0
    assert pg.execute("SELECT count(*) FROM dl_item_memory").fetchone()[0] == 0
    assert pg.execute("SELECT count(*) FROM order_questions").fetchone()[0] == 0
    run = pg.execute("SELECT shadow, result->>'kind' FROM order_runs").fetchone()
    assert run == (True, "dl")


def test_shadow_reports_duplicate_via_a_read_only_check(pg, tmp_path):
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    desadv.claim_send(pg, SUPPLIER_EAN, "0100000001", "f.txt")
    desadv.confirm_sent(pg, SUPPLIER_EAN, "0100000001")
    client = FakeClient({"dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED]})
    cfg = _cfg(delivery_notes_engine="n8n", delivery_notes_shadow=True,
              data_dir=str(tmp_path))
    dl_worker.tick(pg, cfg, client=client)
    result = pg.execute("SELECT result FROM order_runs").fetchone()[0]
    assert result["documents"][0]["outcome"] == "duplicate"
    # still exactly ONE ledger row (the pre-seeded one) — shadow never inserted a claim.
    assert pg.execute("SELECT count(*) FROM desadv_sent").fetchone()[0] == 1


# --- spec §4: announced-vs-attached ------------------------------------------

def test_subject_doc_numbers_extracts_the_lunys_shape():
    subj = ("IS KARAT: Tlač: Dodací list SK Signatus (2610LT0100000002) - "
            "Dodací list SK Signatus 2610LT0100000001")
    assert dl_worker._subject_doc_numbers(subj) == ["0100000002", "0100000001"]


def test_subject_doc_numbers_empty_for_an_unrelated_subject():
    assert dl_worker._subject_doc_numbers("Faktúra 123/2026") == []


def test_item_match_prompt_states_the_same_weight_tolerance_the_code_applies_fixes_225():
    """#225: a real production wording ("110g") was 10 % off its correct card's own
    stated weight ("100g") — exactly WEIGHT_TOLERANCE, so the deterministic
    _weights_disagree() gate would have accepted it, but the model (told only a vague
    "significantly different weight = different product", no number) returned
    NO_MATCH at confidence 0.38. The prompt must state the actual tolerance so the
    model's own judgement doesn't reject something the code would accept anyway."""
    from app.orders import dl_match
    prompt = dl_worker._item_prompt()
    tolerance_pct = round(dl_match.WEIGHT_TOLERANCE * 100)
    assert f"{tolerance_pct} %" in prompt or f"{tolerance_pct}%" in prompt


def test_announced_but_not_attached_dl_is_flagged_not_silently_lost(pg, tmp_path):
    """The exact incident spec §4 documents: the subject names TWO DL numbers, only
    ONE PDF (and therefore one extracted docNumber) ever arrives."""
    _snapshot(pg)
    _msg(pg, mid="dl1",
        subject="IS KARAT: Tlač: Dodací list SK Signatus (2610LT0100000002) - "
                "Dodací list SK Signatus 2610LT0100000001")
    _attach(pg, tmp_path, "dl1")
    client = FakeClient({"dl_documents": [_doc(doc_number="0100000001")],
                         "dl_supplier": [SUPPLIER_MATCHED], "dl_item": [ITEM_MATCHED]})
    posted = []
    dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), client=client,
        upload=lambda *a, **k: None, post=lambda c, h: posted.append(h))

    mismatch = [h for h in posted if "ohlásený aj doklad" in h]
    assert mismatch, "the announced-but-unattached DL number must be flagged"
    assert "0100000002" in mismatch[0]
    # #229 follow-up: the message must ALSO state the outcome of the doc that WAS
    # attached, and state it FIRST — a reader must never be left guessing whether
    # 0100000001 (the one PDF that did arrive) was actually processed.
    assert "Dodací list 0100000001" in mismatch[0]
    assert "spracovaný a nahratý do ORIONu" in mismatch[0]
    assert (mismatch[0].index("Dodací list 0100000001") <
           mismatch[0].index("ohlásený aj doklad"))
    ev = pg.execute(
        "SELECT detail FROM email_events WHERE message_id='dl1' "
        "AND stage='announced_mismatch'").fetchone()
    assert ev is not None
    assert ev[0]["announced"] == ["0100000002"]
    # #238 requirement #2: a run with a genuinely missing announced document must
    # NEVER roll up as "ok" — the whole point of an audit-by-proc_status is that
    # proc_status itself must be honest, not just the separate Odoo alert.
    row = pg.execute(
        "SELECT proc_status FROM messages WHERE message_id='dl1'").fetchone()
    assert row[0] == "partial", \
        "proc_status must reflect the missing announced document, not just the " \
        "one doc that happened to arrive"


def test_an_attachment_that_yields_no_document_is_flagged_not_silently_lost(pg, tmp_path):
    """#238: message 6202's own confirmed shape (verified live, read-only, against
    production) — TWO real attachments, the OLD n8n workflow's `LIMIT 1` fetch only
    ever read the first one and the mail rolled up as `proc_status='ok'` regardless.
    The current Python engine already reads every attachment (fixes W1a) — but has
    no check that a successfully-read attachment (no exception at all) actually
    CONTRIBUTED a document. This reproduces the current engine's own analogue of the
    same loss: the SECOND attachment's extraction call genuinely returns zero
    documents (a plain LLM omission, no error raised), universal — no subject-format
    dependency, unlike the Lunys-only announced-vs-attached check above."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1", idx=0, filename="prvy.pdf")
    _attach(pg, tmp_path, "dl1", idx=1, filename="druhy.pdf")
    client = FakeClient({
        "dl_documents": [_doc(doc_number="0100000001"), {"documents": []}],
        "dl_supplier": [SUPPLIER_MATCHED], "dl_item": [ITEM_MATCHED]})
    posted = []
    dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), client=client,
        upload=lambda *a, **k: None, post=lambda c, h: posted.append(h))

    row = pg.execute(
        "SELECT proc_status FROM messages WHERE message_id='dl1'").fetchone()
    assert row[0] == "partial", \
        "one processed attachment must never make the WHOLE run 'ok' when another " \
        "attachment silently yielded no document"
    flagged = [h for h in posted
              if "druhy.pdf" in h and "nenašiel sa v nej žiadny dodací list" in h]
    assert flagged, "the empty attachment must be individually visible with a reason"
    ev = pg.execute(
        "SELECT detail FROM email_events WHERE message_id='dl1' "
        "AND status='review' AND outcome LIKE '%druhy.pdf%'").fetchone()
    assert ev is not None and ev[0]["idx"] == 1


def test_no_lunys_shaped_subject_flags_nothing(pg, tmp_path):
    _snapshot(pg)
    _msg(pg, mid="dl1", subject="Faktúra 123/2026")
    _attach(pg, tmp_path, "dl1")
    client = FakeClient({"dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED]})
    dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), client=client,
        upload=lambda *a, **k: None, post=lambda *a, **k: None)
    ev = pg.execute(
        "SELECT count(*) FROM email_events WHERE stage='announced_mismatch'").fetchone()
    assert ev[0] == 0


# --- R17/W9: transient vs non-transient retry semantics ----------------------

def test_transient_llm_failure_retries_without_marking_processed(pg, tmp_path):
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    client = RaisingClient("rate limit exceeded")
    n = dl_worker.tick(pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)),
                       client=client)
    assert n == 0
    row = pg.execute(
        "SELECT processed, processing_at, attempts FROM messages "
        "WHERE message_id='dl1'").fetchone()
    assert row[0] is False
    assert row[1] is not None, "the claim stays set — the 30-min stale window reclaims it"
    assert row[2] == 1
    status = pg.execute("SELECT status FROM order_runs").fetchone()[0]
    assert status == "retry"


def test_non_transient_llm_failure_goes_to_review_immediately(pg, tmp_path):
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    client = RaisingClient("schema validation failed")
    posted = []
    n = dl_worker.tick(pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)),
                       client=client, post=lambda c, h: posted.append(h))
    assert n == 1
    row = pg.execute("SELECT processed FROM messages WHERE message_id='dl1'").fetchone()
    assert row[0] is True
    assert len(posted) == 1


def test_attempts_3_or_more_goes_to_review_even_for_a_transient_reason(pg, tmp_path):
    """W9: attempts is already incremented by the claim, so the retry gate `<3` gives
    retries on attempts 1-2, review on attempts 3-4."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    pg.execute("UPDATE messages SET attempts = 2 WHERE message_id = 'dl1'")
    client = RaisingClient("rate limit exceeded")
    n = dl_worker.tick(pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)),
                       client=client)
    assert n == 1
    row = pg.execute(
        "SELECT processed, attempts FROM messages WHERE message_id='dl1'").fetchone()
    assert row == (True, 3)


# --- #239 class 2: upload-failure durable alert (never fire-and-forget, never a
# --- silent automatic re-upload) -----------------------------------------------------

def test_a_timed_out_upload_is_never_auto_retried_so_orion_can_never_get_two_copies(
        pg, tmp_path):
    """#239, found by independent verification of this ticket's own PR: an upload
    failure must never be re-uploaded automatically.

    `upload.put()` writes straight to the FINAL `in_DL\\<name>` with no temp-write +
    rename, so a transfer that lands its bytes and only loses the reply (`timed out` --
    which `TRANSIENT_RE` matches) leaves a complete, validly-named file on ORION.
    `desadv_edi.filename()` then stamps a retry with a fresh `HHMMSSmmm`, so the second
    attempt cannot collide with the first, and `desadv.release_send()` has already
    DELETED the ledger row -- so `claim_send_or_identify()`, the one atomic
    anti-double-upload backstop, has nothing left to guard and `confirm.py` never sees
    the orphan either. The warehouse's next manual morning import would take in BOTH
    copies: a real duplicate delivery.

    So exactly ONE upload attempt must be made, the message must end terminal (not
    re-armed for the 30-minute stale reclaim), and the durable alert -- the half of
    #239 that is correct -- must still be enqueued so the failure stays visible.
    """
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    client = FakeClient({"dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED]})
    tries = []

    def _timed_out_upload(c, name, content, dir_override=None):
        tries.append(name)
        raise OSError("connection timed out")

    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(pg, cfg, client=client, upload=_timed_out_upload)
    assert n == 1
    assert len(tries) == 1, "a second upload of the same document is a duplicate delivery"
    row = pg.execute(
        "SELECT processed, attempts FROM messages WHERE message_id='dl1'").fetchone()
    assert row == (True, 1), "terminal, never re-armed for an automatic second upload"
    assert pg.execute(
        "SELECT count(*) FROM pending_alerts WHERE kind='dl_upload_failed'"
    ).fetchone()[0] == 1, "the failure must still be visible, just not retried"


def test_non_transient_upload_failure_enqueues_a_durable_alert_not_a_direct_post(pg, tmp_path):
    """Requirement 3 of #239: the upload-failure alert must be DURABLE (retried until
    Odoo confirms delivery), never a fire-and-forget `post()` call that silently loses
    the alert if Odoo happens to be down at that exact moment."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    client = FakeClient({"dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED]})

    def _raise_upload(c, name, content, dir_override=None):
        raise OSError("disk quota exceeded on remote host")

    posted = []
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(pg, cfg, client=client, upload=_raise_upload,
                       post=lambda c, h: posted.append(h))
    assert n == 1
    assert posted == [], "must NOT go through the immediate best-effort post at all"
    row = pg.execute(
        "SELECT processed FROM messages WHERE message_id='dl1'").fetchone()
    assert row[0] is True

    alert = pg.execute(
        "SELECT channel_id, kind, message_id, delivered_at, body_html "
        "FROM pending_alerts").fetchone()
    assert alert[0] == cfg.delivery_notes_channel_id
    assert alert[1] == "dl_upload_failed"
    assert alert[2] == "dl1"
    assert alert[3] is None, "not yet delivered — flush_pending delivers it later"
    assert "Odoslanie dodacieho listu do ORIONu zlyhalo" in alert[4]

    # the claim was released — a later successful reprocess must not be blocked
    assert pg.execute(
        "SELECT count(*) FROM desadv_sent WHERE uploaded_at IS NOT NULL"
    ).fetchone()[0] == 0


def test_upload_failure_alert_actually_delivers_once_flushed(pg, tmp_path):
    """End-to-end proof: the enqueued alert survives to a real (grouped) Odoo post via
    the SAME flush sweep worker.run_forever wires in."""
    from app.orders import dl_alerts

    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    client = FakeClient({"dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED]})

    def _raise_upload(c, name, content, dir_override=None):
        raise OSError("disk quota exceeded on remote host")

    dl_worker.tick(pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)),
                   client=client, upload=_raise_upload)

    posted = []

    def _fake_post(c, h, **kw):
        posted.append((h, kw.get("channel_id")))
        return {"id": 1}

    n = dl_alerts.flush_pending(pg, cfg=None, post=_fake_post)
    assert n == 1
    assert len(posted) == 1 and posted[0][1] == 243


def test_upload_failure_enqueues_immediately_whatever_the_attempt_count(pg, tmp_path):
    """An upload failure is terminal at EVERY attempt count — there is no retry window
    for uploads (see the duplicate-delivery reasoning in the no-auto-retry test above),
    so a transient-looking reason alerts immediately here exactly as it does on the
    first attempt. Kept as a distinct case because it also pins that the claim's own
    attempts counter still passes through untouched. (W9's `<3` retry gate remains real
    for LLM/vision failures — `test_attempts_3_or_more_goes_to_review_even_for_a_
    transient_reason` covers that path, which this change did not touch.)"""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    pg.execute("UPDATE messages SET attempts = 2 WHERE message_id = 'dl1'")
    client = FakeClient({"dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED]})

    def _raise_upload(c, name, content, dir_override=None):
        raise OSError("connection timed out")

    n = dl_worker.tick(pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)),
                       client=client, upload=_raise_upload)
    assert n == 1
    row = pg.execute(
        "SELECT processed, attempts FROM messages WHERE message_id='dl1'").fetchone()
    assert row == (True, 3)
    assert pg.execute("SELECT count(*) FROM pending_alerts").fetchone()[0] == 1


def test_upload_failure_with_no_channel_configured_never_enqueues_a_stuck_alert(pg, tmp_path):
    """Deep-review finding on #239's own PR: `stuck_classified_sweep` already bails
    when `delivery_notes_channel_id` resolves to 0 — the upload-failure call site must
    have the SAME guard, or it would enqueue a row with `channel_id=0` that can never
    be delivered (nothing posts to channel 0) and sits pending forever."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    client = FakeClient({"dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED]})

    def _raise_upload(c, name, content, dir_override=None):
        raise OSError("disk quota exceeded on remote host")

    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path),
              delivery_notes_channel_id=0)
    n = dl_worker.tick(pg, cfg, client=client, upload=_raise_upload)
    assert n == 1
    assert pg.execute("SELECT count(*) FROM pending_alerts").fetchone()[0] == 0


# --- #239 class 3: classified as DL but never even attempted -----------------

def test_stuck_classified_sweep_stamps_a_detection_time_alongside_the_received_time(pg):
    """Deep-review finding on #239's own PR: `flush_pending` may deliver this alert
    long after detection (a queued Odoo outage) — by then the message could already be
    processed. Stamping the DETECTION time lets a reader judge staleness for
    themselves, distinct from the message's own `created_at`."""
    _msg(pg, mid="dl1")
    pg.execute(
        "UPDATE messages SET created_at = now() - interval '31 minutes' "
        "WHERE message_id = 'dl1'")
    dl_worker.stuck_classified_sweep(pg, _cfg())
    html = pg.execute(
        "SELECT body_html FROM pending_alerts WHERE message_id='dl1'").fetchone()[0]
    assert "prijaté:" in html
    assert "zistené:" in html


def test_stuck_classified_sweep_alerts_a_message_with_no_order_runs_row(pg):
    _msg(pg, mid="dl1")
    pg.execute(
        "UPDATE messages SET created_at = now() - interval '31 minutes' "
        "WHERE message_id = 'dl1'")
    n = dl_worker.stuck_classified_sweep(pg, _cfg())
    assert n == 1
    alert = pg.execute(
        "SELECT channel_id, kind, message_id, delivered_at FROM pending_alerts"
    ).fetchone()
    assert alert == (_cfg().delivery_notes_channel_id, "dl_stuck_classified", "dl1", None)


def test_stuck_classified_sweep_ignores_a_message_within_the_threshold(pg):
    _msg(pg, mid="dl1")  # created_at = now(), well within 30 minutes
    n = dl_worker.stuck_classified_sweep(pg, _cfg())
    assert n == 0
    assert pg.execute("SELECT count(*) FROM pending_alerts").fetchone()[0] == 0


def test_stuck_classified_sweep_ignores_a_message_with_any_order_runs_row(pg):
    """A shadow peek run (or a real one) proves the message WAS attempted at least
    once — never the class this sweep exists to catch."""
    _msg(pg, mid="dl1")
    pg.execute(
        "UPDATE messages SET created_at = now() - interval '1 hour' "
        "WHERE message_id = 'dl1'")
    pg.execute(
        "INSERT INTO order_runs (message_id, shadow, status) VALUES (%s, true, 'review')",
        ("dl1",))
    n = dl_worker.stuck_classified_sweep(pg, _cfg())
    assert n == 0


def test_stuck_classified_sweep_deduplicates_across_repeated_sweeps(pg):
    """A persistently-stuck message must be alerted exactly ONCE, not every ~15s tick
    (never `messages.alerted_stuck` — that flag belongs to the n8n watchdog's own
    dedup, see the design comment on #239)."""
    _msg(pg, mid="dl1")
    pg.execute(
        "UPDATE messages SET created_at = now() - interval '1 hour' "
        "WHERE message_id = 'dl1'")
    n1 = dl_worker.stuck_classified_sweep(pg, _cfg())
    n2 = dl_worker.stuck_classified_sweep(pg, _cfg())
    assert (n1, n2) == (1, 0)
    assert pg.execute("SELECT count(*) FROM pending_alerts").fetchone()[0] == 1
    row = pg.execute("SELECT alerted_stuck FROM messages WHERE message_id='dl1'").fetchone()
    assert row[0] is False, "alerted_stuck belongs to the n8n watchdog, never set here"


def test_stuck_classified_sweep_ignores_an_already_processed_message(pg):
    _msg(pg, mid="dl1")
    pg.execute(
        "UPDATE messages SET created_at = now() - interval '1 hour', processed = true "
        "WHERE message_id = 'dl1'")
    n = dl_worker.stuck_classified_sweep(pg, _cfg())
    assert n == 0


# --- refresh_due (mirrors worker.refresh_due) --------------------------------

def test_refresh_due_returns_none_when_no_snapshot_exists_yet(pg):
    assert dl_worker.refresh_due(pg, _cfg()) is None


def test_refresh_due_never_touches_the_network_even_when_configured(pg, monkeypatch):
    """#129: the DL catalog/supplier sheet is permanently disabled too — it must never
    be fetched again, even when the frozen dl_snapshot is old. dl_worker.py must not
    break the LIVE delivery_notes_shadow window (#205) — it now just reports whatever
    dl_snapshot is currently frozen. (The catalog_sheet_id/gid options that used to
    configure the fetch stay declared on Config/config.yaml per #129's own precedent —
    see test_config.py — but nothing here, or anywhere else, reads them any more.)"""
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("must never fetch the DL sheet (#129)"))
    sid = _snapshot(pg)
    cfg = _cfg()
    assert dl_worker.refresh_due(pg, cfg) == sid

    pg.execute("UPDATE dl_snapshots SET checked_at = now() - interval '2 hours'")
    assert dl_worker.refresh_due(pg, cfg) == sid


# --- deep-review regression tests (#204's own PR review) ---------------------

def test_shadow_never_writes_email_events_not_even_non_rollup_ones(pg, tmp_path):
    """Deep-review finding: shadow's 'marks/writes nothing' guarantee must cover
    EVERY email_events write in this module, not only the two `rollup=True` ones."""
    _snapshot(pg)
    _msg(pg, mid="dl1", has_attachments=False)  # R15 path — cheapest event-log write
    cfg = _cfg(delivery_notes_engine="n8n", delivery_notes_shadow=True,
              data_dir=str(tmp_path))
    n = dl_worker.tick(pg, cfg, client=FakeClient({}))
    assert n == 1
    assert pg.execute(
        "SELECT count(*) FROM email_events WHERE message_id='dl1'").fetchone()[0] == 0


def test_shadow_never_corrupts_an_already_delivered_messages_dashboard_state(pg, tmp_path):
    """The EXACT deep-review reproduction: a shadow peek picking a message n8n had
    ALREADY finished must never overwrite its proc_status/proc_outcome (the
    email_events rollup=True trigger) — a shadow tick was reproduced silently turning
    an 'uploaded_orion/ok' message back into 'review' before this fix."""
    _snapshot(pg)
    _msg(pg, mid="dl1", has_attachments=False)
    from app.orders import report as report_mod
    report_mod.log_event(pg, "dl1", stage="uploaded_orion", status="ok",
                         outcome="EDI vytvorené: Z-DESADV_x.txt", rollup=True,
                         workflow="delivery_notes")
    before = pg.execute(
        "SELECT proc_status, proc_outcome FROM messages WHERE message_id='dl1'"
        ).fetchone()
    assert before == ("ok", "EDI vytvorené: Z-DESADV_x.txt")

    cfg = _cfg(delivery_notes_engine="n8n", delivery_notes_shadow=True,
              data_dir=str(tmp_path))
    dl_worker.tick(pg, cfg, client=FakeClient({}))

    after = pg.execute(
        "SELECT proc_status, proc_outcome FROM messages WHERE message_id='dl1'"
        ).fetchone()
    assert after == before, \
        "shadow must never overwrite an already-delivered message's dashboard state"


def test_lexical_gap_item_is_visible_in_the_message_and_raises_a_question(pg, tmp_path):
    """Deep-review finding: an item excluded from the EDI by the R75 lexical-gap
    tripwire (`llm_sure_lexical_gap`) — or this worker's own `match_failed` fallback —
    was previously invisible: the gate only checked `rule == 'unmatched'`, so neither
    the Odoo message nor a nástenka question ever mentioned it."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    doc = _doc(total=8.0, items=[
        {"name": "Rožok 50g", "quantity": 10, "unit": "ks", "unitPrice": 0.5,
         "totalPrice": 5.0},
        {"name": "Úplne iný produkt", "quantity": 3, "unit": "ks", "unitPrice": 1.0,
         "totalPrice": 3.0}])
    client = FakeClient({
        "dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
        "dl_item": [ITEM_MATCHED,
                   {"gtin": ITEM_GTIN, "matchedCatalogName": "Rožok 50g",
                    "matchConfidence": 0.97, "matchReason": "istý, no odlišné slová"}]})
    uploaded, posted = [], []
    n = dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), client=client,
        upload=lambda c, name, content, dir_override=None: uploaded.append(name),
        post=lambda c, h: posted.append(h))
    assert n == 1
    assert len(uploaded) == 1, "the first item still ships (partial EDI, R81)"
    success = [h for h in posted if "spracovaný ČIASTOČNE" in h][0]
    assert "ČIASTOČNE" in success
    assert "Nespárované" in success and "Úplne iný produkt" in success
    q = pg.execute(
        "SELECT kind FROM order_questions WHERE kind='dl_item'").fetchone()
    assert q is not None

    items = pg.execute("SELECT gtin, rule FROM order_items ORDER BY id").fetchall()
    assert ("8588000000001", "llm_sure") in items
    assert (None, "llm_sure_lexical_gap") in items


def test_a_hard_pipeline_failure_stays_tagged_as_a_dl_run_not_an_orders_run(
        pg, tmp_path, monkeypatch):
    """Deep-review finding: the generic exception handler in tick() used to pass
    `result=None` to `worker._finish_run`, leaving `result->>'kind'` NULL — which
    `reliability.provenance_stats_for_day`'s own `IS DISTINCT FROM 'dl'` exclusion
    treats as an ORDERS run, miscounting a hard DL failure into the wrong digest."""
    from app.orders import dl_extract

    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")

    def _boom(client, attachments):
        raise RuntimeError("kaboom — unexpected, not caught anywhere upstream")

    monkeypatch.setattr(dl_extract, "extract_email", _boom)
    n = dl_worker.tick(pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)),
                       client=FakeClient({}))
    assert n == 0
    row = pg.execute("SELECT status, result->>'kind' FROM order_runs").fetchone()
    assert row == ("error", "dl")


# --- #240: answering a dl_item/dl_supplier question finishes the DOCUMENT that raised it -
#
# Before this fix, `teach._apply_dl_item`/`_apply_dl_supplier` only wrote the taught
# memory and stopped — the document that raised the question stayed unfinished forever,
# even after the exact wording/address that blocked it was taught. `release_for_question`
# is the fix: it reprocesses the SAME message once every dl_item/dl_supplier question it
# still has open is answered, reusing the ordinary (already-tested) `_process_message`
# pipeline — so a now-resolvable document actually ships, and a still-unresolvable one
# raises a fresh, visible question instead of hanging silently.

class _NeverCalledClient:
    """Proves a code path returns BEFORE ever touching the LLM — used where reprocessing
    must not happen at all (a sibling question still open)."""

    def json_call(self, *a, **kw):
        raise AssertionError("must not call the model — this path must not reprocess")

    def vision_call(self, *a, **kw):
        raise AssertionError("must not call the model — this path must not reprocess")


def test_release_for_question_ships_a_previously_blocked_dl_item_document(pg, tmp_path):
    """The live production incident this ticket closes: a document whose ONLY item never
    matched (0 of 1 items with a GTIN) never even reaches `can_create` — nothing ships,
    nothing is left to finish it. Teaching the wording (a real human answer) must now
    make THIS document ship, not just help the next one."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    doc = _doc(total=3.0, items=[{"name": "Neznámy chlebík", "quantity": 3, "unit": "ks",
                                  "unitPrice": 1.0, "totalPrice": 3.0}])
    client1 = FakeClient({"dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [{"gtin": "NO_MATCH", "matchConfidence": 0.0,
                                     "matchReason": "žiadna zhoda"}]})
    uploaded, posted = [], []
    n = dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), client=client1,
        upload=lambda c, name, content, dir_override=None: uploaded.append(name),
        post=lambda c, h: posted.append(h))
    assert n == 1
    assert uploaded == [], "nothing shippable on the first pass"
    qid = pg.execute(
        "SELECT id FROM order_questions WHERE kind='dl_item'").fetchone()[0]

    # The answer: teach the wording (what `_apply_dl_item` does), then mark the question
    # answered (what the real HTTP dispatch does BEFORE calling apply()).
    dl_memory.remember(pg, SUPPLIER_EAN, "Neznámy chlebík", ITEM_GTIN, "Rožok 50g",
                       "2026-08-10", source="human")
    pg.execute("UPDATE order_questions SET status='answered', answer=%s WHERE id=%s",
              (Json({"choice": ITEM_GTIN}), qid))

    # The model is asked again (it never learned anything) and still says NO_MATCH — the
    # document only finishes because R73's memory rescue now has a human-taught row to
    # use, exactly the mechanism `hold.py`'s own AI-orders release relies on.
    client2 = FakeClient({"dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [{"gtin": "NO_MATCH", "matchConfidence": 0.0,
                                     "matchReason": "žiadna zhoda"}]})
    uploaded2, posted2 = [], []
    released = dl_worker.release_for_question(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), qid,
        client=client2,
        upload=lambda c, name, content, dir_override=None: uploaded2.append(name),
        post=lambda c, h: posted2.append(h))
    assert len(uploaded2) == 1, "the document must now ship"
    assert released and released[0]["outcome"] == "ok"
    assert pg.execute(
        "SELECT count(*) FROM desadv_sent WHERE supplier_ean=%s AND doc_number=%s "
        "AND uploaded_at IS NOT NULL", (SUPPLIER_EAN, "0100000001")).fetchone()[0] == 1
    # a second order_runs row records the reprocess — the first attempt is not overwritten
    assert pg.execute("SELECT count(*) FROM order_runs").fetchone()[0] == 2


def test_release_for_question_ships_a_previously_blocked_dl_supplier_document_without_asking_the_model_again(
        pg, tmp_path):
    """#240: `_match_supplier`'s new memory-rescue rung is what makes THIS reprocess
    actually finish — without it, asking the model again for the same address would get
    back the exact same "not matched" verdict (the model has no way to know a human just
    resolved it), and the document could never ship. The scripted client below has NO
    `dl_supplier` answer at all — if the model were asked again, the test would fail with
    a "no scripted answer left" error, proving the model genuinely was never called."""
    _snapshot(pg)
    _msg(pg, mid="dl2", from_addr="neznamy@somewhere.sk")
    _attach(pg, tmp_path, "dl2")
    doc = _doc()
    doc["documents"][0]["supplierEmail"] = "neznamy@somewhere.sk"
    doc["documents"][0]["supplierName"] = "Neznáma pekáreň s.r.o."
    client1 = FakeClient({
        "dl_documents": [doc],
        "dl_supplier": [{"matched": False, "matchReason": "nie je v zozname"}]})
    uploaded, posted = [], []
    n = dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), client=client1,
        upload=lambda c, name, content, dir_override=None: uploaded.append(name),
        post=lambda c, h: posted.append(h))
    assert n == 1
    assert uploaded == [], "the item loop never even runs while the supplier is unknown"
    qid = pg.execute(
        "SELECT id FROM order_questions WHERE kind='dl_supplier'").fetchone()[0]

    dl_supplier_memory.remember(pg, "neznamy@somewhere.sk", SUPPLIER_EAN, "Pekáreň Lunys")
    pg.execute("UPDATE order_questions SET status='answered', answer=%s WHERE id=%s",
              (Json({"choice": SUPPLIER_EAN}), qid))

    client2 = FakeClient({"dl_documents": [doc], "dl_item": [ITEM_MATCHED]})
    uploaded2, posted2 = [], []
    released = dl_worker.release_for_question(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), qid,
        client=client2,
        upload=lambda c, name, content, dir_override=None: uploaded2.append(name),
        post=lambda c, h: posted2.append(h))
    assert len(uploaded2) == 1, "the document must now ship"
    assert released and released[0]["outcome"] == "ok"
    assert pg.execute(
        "SELECT count(*) FROM desadv_sent WHERE supplier_ean=%s AND doc_number=%s "
        "AND uploaded_at IS NOT NULL", (SUPPLIER_EAN, "0100000001")).fetchone()[0] == 1


def test_release_for_question_waits_for_every_sibling_dl_item_question_before_reprocessing(
        pg, tmp_path):
    """Two unmatched items on the SAME document raise TWO separate dl_item questions.
    Answering only one must not trigger a reprocess yet — mirrors `hold.
    release_for_question`'s own "every question_id answered" gate; reprocessing on a
    partial answer would just re-raise the exact same still-open question for nothing."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    doc = _doc(total=6.0, items=[
        {"name": "Neznámy chlebík", "quantity": 3, "unit": "ks", "unitPrice": 1.0,
         "totalPrice": 3.0},
        {"name": "Tajomný koláč", "quantity": 3, "unit": "ks", "unitPrice": 1.0,
         "totalPrice": 3.0}])
    client1 = FakeClient({"dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [{"gtin": "NO_MATCH", "matchConfidence": 0.0,
                                     "matchReason": "žiadna zhoda"},
                                    {"gtin": "NO_MATCH", "matchConfidence": 0.0,
                                     "matchReason": "žiadna zhoda"}]})
    n = dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), client=client1,
        upload=lambda *a, **k: None, post=lambda c, h: None)
    assert n == 1
    qid_a = pg.execute(
        "SELECT id FROM order_questions WHERE kind='dl_item' AND wording='Neznámy "
        "chlebík'").fetchone()[0]
    qid_b = pg.execute(
        "SELECT id FROM order_questions WHERE kind='dl_item' AND wording='Tajomný "
        "koláč'").fetchone()[0]

    dl_memory.remember(pg, SUPPLIER_EAN, "Neznámy chlebík", ITEM_GTIN, "Rožok 50g",
                       "2026-08-10", source="human")
    pg.execute("UPDATE order_questions SET status='answered', answer=%s WHERE id=%s",
              (Json({"choice": ITEM_GTIN}), qid_a))

    released = dl_worker.release_for_question(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), qid_a,
        client=_NeverCalledClient())
    assert released == [], "the sibling question (koláč) is still open — must not reprocess"
    assert pg.execute("SELECT count(*) FROM order_runs").fetchone()[0] == 1, \
        "no second run — the reprocess genuinely never started"

    dl_memory.remember(pg, SUPPLIER_EAN, "Tajomný koláč", ITEM_GTIN, "Rožok 50g",
                       "2026-08-10", source="human")
    pg.execute("UPDATE order_questions SET status='answered', answer=%s WHERE id=%s",
              (Json({"choice": ITEM_GTIN}), qid_b))

    client2 = FakeClient({"dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [{"gtin": "NO_MATCH", "matchConfidence": 0.0,
                                     "matchReason": "žiadna zhoda"},
                                    {"gtin": "NO_MATCH", "matchConfidence": 0.0,
                                     "matchReason": "žiadna zhoda"}]})
    uploaded2 = []
    released = dl_worker.release_for_question(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), qid_b,
        client=client2,
        upload=lambda c, name, content, dir_override=None: uploaded2.append(name),
        post=lambda c, h: None)
    assert len(uploaded2) == 1, "now that BOTH siblings are answered, it ships"
    assert released and released[0]["outcome"] == "ok"


def test_release_for_question_raises_a_fresh_question_when_still_unresolved(pg, tmp_path):
    """Requirement 2: when the document STILL cannot fully resolve after the answer (a
    second, genuinely different wording was never taught), it must not silently hang —
    it ships what it can (existing "ship what matched" behaviour, unchanged) AND raises a
    FRESH, visible question for what remains, so the warehouse is never left guessing."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    doc = _doc(total=6.0, items=[
        {"name": "Neznámy chlebík", "quantity": 3, "unit": "ks", "unitPrice": 1.0,
         "totalPrice": 3.0},
        {"name": "Úplne iný produkt", "quantity": 3, "unit": "ks", "unitPrice": 1.0,
         "totalPrice": 3.0}])
    client1 = FakeClient({"dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [{"gtin": "NO_MATCH", "matchConfidence": 0.0,
                                     "matchReason": "žiadna zhoda"},
                                    {"gtin": "NO_MATCH", "matchConfidence": 0.0,
                                     "matchReason": "žiadna zhoda"}]})
    n = dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), client=client1,
        upload=lambda *a, **k: None, post=lambda c, h: None)
    assert n == 1
    qid_a = pg.execute(
        "SELECT id FROM order_questions WHERE kind='dl_item' AND wording='Neznámy "
        "chlebík'").fetchone()[0]
    qid_b = pg.execute(
        "SELECT id FROM order_questions WHERE kind='dl_item' AND wording='Úplne iný "
        "produkt'").fetchone()[0]

    # "Neznámy chlebík" gets a real, currently-valid answer. "Úplne iný produkt" ALSO
    # gets a real, non-blank pick — but the card it named has since been retired from the
    # catalog (review finding, #240: a blank/"neviem" choice is unreachable through the
    # real HTTP path — `_api_orders_answer_generic` never marks THAT kind of answer
    # 'answered' at all, per its own docstring — so a genuinely production-representative
    # "still unresolved after a real answer" trigger is a taught card the catalog no
    # longer has, which `dl_memory.resolve()`'s own `catalog_gtins` filter correctly
    # refuses to rescue with).
    RETIRED_GTIN = "9999999999999"
    dl_memory.remember(pg, SUPPLIER_EAN, "Neznámy chlebík", ITEM_GTIN, "Rožok 50g",
                       "2026-08-10", source="human")
    dl_memory.remember(pg, SUPPLIER_EAN, "Úplne iný produkt", RETIRED_GTIN,
                       "Karta, ktorá už nie je v katalógu", "2026-08-10", source="human")
    pg.execute("UPDATE order_questions SET status='answered', answer=%s WHERE id=%s",
              (Json({"choice": ITEM_GTIN}), qid_a))
    pg.execute("UPDATE order_questions SET status='answered', answer=%s WHERE id=%s",
              (Json({"choice": RETIRED_GTIN}), qid_b))

    client2 = FakeClient({"dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [{"gtin": "NO_MATCH", "matchConfidence": 0.0,
                                     "matchReason": "žiadna zhoda"},
                                    {"gtin": "NO_MATCH", "matchConfidence": 0.0,
                                     "matchReason": "stále žiadna zhoda"}]})
    uploaded2, posted2 = [], []
    released = dl_worker.release_for_question(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), qid_a,
        client=client2,
        upload=lambda c, name, content, dir_override=None: uploaded2.append(name),
        post=lambda c, h: posted2.append(h))
    assert len(uploaded2) == 1, "the resolvable item still ships (partial EDI, unchanged)"
    assert released and released[0]["outcome"] == "partial"
    fresh = pg.execute(
        "SELECT id, status FROM order_questions WHERE kind='dl_item' AND "
        "wording='Úplne iný produkt' ORDER BY id").fetchall()
    assert len(fresh) == 2, "a brand-new question was raised — the old one is not reused"
    assert fresh[0] == (qid_b, "answered")
    assert fresh[1][1] == "open", "the still-unresolved item is visibly asked about again"
    assert any("Nespárované" in h and "Úplne iný produkt" in h for h in posted2), \
        "also visible in the Odoo message, not just the nástenka question"


def test_release_for_question_never_reuploads_an_already_partially_shipped_document(
        pg, tmp_path):
    """HARD SAFETY (#240): a document that already shipped (partial — one item excluded)
    must NEVER be re-uploaded just because its excluded item's dl_item question later
    gets answered. The guard is the SAME `desadv.claim_send_or_identify` ledger every
    `_process_document` call already makes — not new code added for this ticket — this
    test proves it holds across the reprocess-on-answer path too, exactly as required
    ("the re-run path must itself carry the ORION/registry check as a guard in code")."""
    _snapshot(pg)
    _msg(pg, mid="dl3")
    _attach(pg, tmp_path, "dl3")
    doc = _doc(total=8.0, items=[
        {"name": "Rožok 50g", "quantity": 10, "unit": "ks", "unitPrice": 0.5,
         "totalPrice": 5.0},
        {"name": "Neznámy chlebík", "quantity": 3, "unit": "ks", "unitPrice": 1.0,
         "totalPrice": 3.0}])
    client1 = FakeClient({"dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED,
                                    {"gtin": "NO_MATCH", "matchConfidence": 0.0,
                                     "matchReason": "žiadna zhoda"}]})
    uploaded, posted = [], []
    n = dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), client=client1,
        upload=lambda c, name, content, dir_override=None: uploaded.append(name),
        post=lambda c, h: posted.append(h))
    assert n == 1
    assert len(uploaded) == 1, "partial EDI still ships today (R81, unchanged)"
    assert pg.execute(
        "SELECT count(*) FROM desadv_sent WHERE supplier_ean=%s AND doc_number=%s "
        "AND uploaded_at IS NOT NULL", (SUPPLIER_EAN, "0100000001")).fetchone()[0] == 1

    qid = pg.execute(
        "SELECT id FROM order_questions WHERE kind='dl_item'").fetchone()[0]
    dl_memory.remember(pg, SUPPLIER_EAN, "Neznámy chlebík", ITEM_GTIN, "Rožok 50g",
                       "2026-08-10", source="human")
    pg.execute("UPDATE order_questions SET status='answered', answer=%s WHERE id=%s",
              (Json({"choice": ITEM_GTIN}), qid))

    client2 = FakeClient({"dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED,
                                    {"gtin": "NO_MATCH", "matchConfidence": 0.0,
                                     "matchReason": "žiadna zhoda"}]})
    uploaded2, posted2 = [], []
    released = dl_worker.release_for_question(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), qid,
        client=client2,
        upload=lambda c, name, content, dir_override=None: uploaded2.append(name),
        post=lambda c, h: posted2.append(h))
    assert uploaded2 == [], "the already-shipped document must NEVER be re-uploaded"
    assert released and released[0]["outcome"] == "duplicate"
    assert pg.execute(
        "SELECT count(*) FROM desadv_sent WHERE supplier_ean=%s AND doc_number=%s",
        (SUPPLIER_EAN, "0100000001")).fetchone()[0] == 1, \
        "still exactly one ledger row — no second claim was ever created"
    ev = pg.execute(
        "SELECT stage FROM email_events WHERE message_id='dl3' AND "
        "stage='already_shipped_this_run'").fetchone()
    assert ev == ("already_shipped_this_run",)


def test_release_for_question_hard_failure_re_arms_processed_for_reclaim(
        pg, tmp_path, monkeypatch):
    """Review finding (#240, this ticket's own second round): `_run_and_finish`'s
    `except Exception` (hard-failure) branch had the IDENTICAL strand-forever bug the
    `_RetryLater` branch was just fixed for, but the fix was not extended to it — a
    reprocess (the message already has `processed=true` from its earlier, successful
    first pass) that hits a genuine, non-transient, STRUCTURAL failure left `processed`
    untouched, permanently excluding the message from `_claim()`'s own `WHERE processed
    = false` filter, exactly like the retry bug did.

    `_match_supplier`'s and `extract_email`'s own per-document/per-attachment guards
    already swallow an ordinary LLM failure into a graceful "review" outcome (the happy
    path — see `test_non_transient_llm_failure_goes_to_review_immediately` above, which
    never reaches this branch at all) — reaching the TRUE hard-failure branch needs a
    structural failure BELOW those guards, simulated here by monkeypatching
    `dl_extract.extract_email` itself to raise, the same way a real bug in
    `desadv_edi.build()`/a DB error would."""
    _snapshot(pg)
    _msg(pg, mid="dlhard")
    _attach(pg, tmp_path, "dlhard")
    doc = _doc(total=3.0, items=[{"name": "Neznámy chlebík", "quantity": 3, "unit": "ks",
                                  "unitPrice": 1.0, "totalPrice": 3.0}])
    n = dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)),
        client=FakeClient({"dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
                          "dl_item": [{"gtin": "NO_MATCH", "matchConfidence": 0.0,
                                      "matchReason": "žiadna zhoda"}]}),
        upload=lambda *a, **k: None, post=lambda c, h: None)
    assert n == 1
    qid = pg.execute("SELECT id FROM order_questions WHERE kind='dl_item'").fetchone()[0]
    dl_memory.remember(pg, SUPPLIER_EAN, "Neznámy chlebík", ITEM_GTIN, "Rožok 50g",
                       "2026-08-10", source="human")
    pg.execute("UPDATE order_questions SET status='answered', answer=%s WHERE id=%s",
              (Json({"choice": ITEM_GTIN}), qid))
    row = pg.execute(
        "SELECT processed FROM messages WHERE message_id='dlhard'").fetchone()
    assert row[0] is True, "setup: the message must already be fully processed"

    def _boom(client, attachments):
        raise RuntimeError("structural bug below the per-document guards")

    monkeypatch.setattr(dl_worker.dl_extract, "extract_email", _boom)
    released = dl_worker.release_for_question(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), qid,
        client=FakeClient({}))
    assert released == []
    row = pg.execute(
        "SELECT processed, processing_at FROM messages WHERE message_id='dlhard'"
    ).fetchone()
    assert row == (False, None), (
        "a hard failure during reprocess must re-arm processed=false so _claim() can "
        "reclaim the message — it must never stay permanently processed=true")
    assert pg.execute(
        "SELECT status FROM order_runs ORDER BY id DESC LIMIT 1").fetchone()[0] == "error"


def test_release_for_question_waits_for_a_mixed_dl_supplier_and_dl_item_sibling_pair(
        pg, tmp_path):
    """Review finding (#240): the sibling gate is tested elsewhere only with two `dl_item`
    questions — a single MESSAGE can just as easily raise one `dl_supplier` question
    (its first document's supplier unmatched) and one `dl_item` question (a DIFFERENT
    document in the SAME message, whose OWN supplier already matched) at once. Both are
    `kind IN ('dl_item', 'dl_supplier')` siblings of the same message_id — the generic
    SQL gate must wait for both regardless of kind, never just same-kind pairs."""
    _snapshot(pg)
    _msg(pg, mid="dlmix")
    _attach(pg, tmp_path, "dlmix")
    two_docs = {"documents": [
        {"supplierName": "Neznáma pekáreň s.r.o.", "supplierCity": "",
         "supplierEmail": "neznamy2@somewhere.sk", "docNumber": "0100000005",
         "deliveryDate": "01.08.2026", "documentTotalWithoutVAT": 5.0,
         "items": [{"name": "Rožok 50g", "quantity": 10, "unit": "ks", "unitPrice": 0.5,
                   "totalPrice": 5.0}]},
        {"supplierName": "Pekáreň Lunys", "supplierCity": "Prešov",
         "supplierEmail": "dodavatel@lunys.sk", "docNumber": "0100000006",
         "deliveryDate": "01.08.2026", "documentTotalWithoutVAT": 3.0,
         "items": [{"name": "Neznámy chlebík", "quantity": 3, "unit": "ks",
                   "unitPrice": 1.0, "totalPrice": 3.0}]},
    ]}
    client1 = FakeClient({
        "dl_documents": [two_docs],
        "dl_supplier": [{"matched": False, "matchReason": "nie je v zozname"},
                        SUPPLIER_MATCHED],
        "dl_item": [{"gtin": "NO_MATCH", "matchConfidence": 0.0,
                    "matchReason": "žiadna zhoda"}]})
    n = dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), client=client1,
        upload=lambda *a, **k: None, post=lambda c, h: None)
    assert n == 1
    qid_supplier = pg.execute(
        "SELECT id FROM order_questions WHERE kind='dl_supplier'").fetchone()[0]
    qid_item = pg.execute(
        "SELECT id FROM order_questions WHERE kind='dl_item'").fetchone()[0]

    # Answer only the dl_supplier question — the dl_item sibling (a DIFFERENT kind, on a
    # DIFFERENT document, but the SAME message) must still block reprocessing.
    dl_supplier_memory.remember(pg, "neznamy2@somewhere.sk", SUPPLIER_EAN, "Pekáreň Lunys")
    pg.execute("UPDATE order_questions SET status='answered', answer=%s WHERE id=%s",
              (Json({"choice": SUPPLIER_EAN}), qid_supplier))
    released = dl_worker.release_for_question(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), qid_supplier,
        client=_NeverCalledClient())
    assert released == [], "the dl_item sibling is still open — must not reprocess yet"
    assert pg.execute("SELECT count(*) FROM order_runs").fetchone()[0] == 1

    # Now answer the dl_item sibling too — every question on the message is answered.
    dl_memory.remember(pg, SUPPLIER_EAN, "Neznámy chlebík", ITEM_GTIN, "Rožok 50g",
                       "2026-08-10", source="human")
    pg.execute("UPDATE order_questions SET status='answered', answer=%s WHERE id=%s",
              (Json({"choice": ITEM_GTIN}), qid_item))
    client2 = FakeClient({
        "dl_documents": [two_docs],
        "dl_supplier": [SUPPLIER_MATCHED],   # doc1 is memory-rescued, never asks the
                                             # model again — only doc2 needs a fresh call
        "dl_item": [ITEM_MATCHED,            # doc1's own item, matched fresh
                   {"gtin": "NO_MATCH", "matchConfidence": 0.0,
                    "matchReason": "žiadna zhoda"}]})  # doc2's item, memory-rescued
    uploaded2 = []
    released = dl_worker.release_for_question(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), qid_item,
        client=client2,
        upload=lambda c, name, content, dir_override=None: uploaded2.append(name),
        post=lambda c, h: None)
    assert len(uploaded2) == 2, "both documents now ship"
    assert {d["outcome"] for d in released} == {"ok"}
    assert pg.execute(
        "SELECT count(*) FROM desadv_sent WHERE supplier_ean=%s AND doc_number IN "
        "('0100000005', '0100000006') AND uploaded_at IS NOT NULL",
        (SUPPLIER_EAN,)).fetchone()[0] == 2


def test_release_for_question_advisory_lock_serializes_two_genuinely_concurrent_racers(
        pg, tmp_path):
    """Review finding (#240, this ticket's own second round): every other test in this
    file exercises `release_for_question` SEQUENTIALLY (one Python call after another),
    which proves the sibling-gate LOGIC but nothing about whether
    `pg_advisory_xact_lock(hashtext(message_id))` actually SERIALIZES two callers that
    are genuinely running AT THE SAME TIME — sequential calls would look identical
    whether the lock does anything at all. This test spawns two real OS threads, each on
    its OWN Postgres connection (a single psycopg connection is not safe for concurrent
    use across threads), both calling `release_for_question` for the SAME already-fully-
    answered question at once. A `_TimedClient` records the wall-clock SPAN of every
    LLM call — since the entire reprocess pipeline runs INSIDE `release_for_question`'s
    `with lock_tx:` block, the lock's guarantee is exactly "these two spans never
    overlap." Deliberately timing-based rather than a fixed sleep+assert (`no-timeout-
    band-aids.md`): the assertion is on the RECORDED spans after both threads finish, not
    on which one "wins" a race, so it is not flaky under CI scheduling variance — a
    missing/broken lock would show up as an actual timestamp overlap, not as a hang."""
    _snapshot(pg)
    _msg(pg, mid="dlrace")
    _attach(pg, tmp_path, "dlrace")
    doc = _doc(total=3.0, items=[{"name": "Neznámy chlebík", "quantity": 3, "unit": "ks",
                                  "unitPrice": 1.0, "totalPrice": 3.0}])
    n = dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)),
        client=FakeClient({"dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
                          "dl_item": [{"gtin": "NO_MATCH", "matchConfidence": 0.0,
                                      "matchReason": "žiadna zhoda"}]}),
        upload=lambda *a, **k: None, post=lambda c, h: None)
    assert n == 1
    qid = pg.execute("SELECT id FROM order_questions WHERE kind='dl_item'").fetchone()[0]
    dl_memory.remember(pg, SUPPLIER_EAN, "Neznámy chlebík", ITEM_GTIN, "Rožok 50g",
                       "2026-08-10", source="human")
    pg.execute("UPDATE order_questions SET status='answered', answer=%s WHERE id=%s",
              (Json({"choice": ITEM_GTIN}), qid))

    pg_dsn = os.environ["PG_TEST_DSN"]
    spans_lock = threading.Lock()
    spans: list[tuple[int, float, float]] = []   # (thread index, call start, call end)
    errors: list[Exception] = []

    class _TimedClient(FakeClient):
        def __init__(self, answers, idx):
            super().__init__(answers)
            self._idx = idx

        def json_call(self, *a, **kw):
            start = time.monotonic()
            time.sleep(0.2)   # widen the window deliberately — makes a real overlap,
                              # if the lock were missing, easy to observe rather than
                              # depending on the two threads happening to be scheduled
                              # at exactly the same instant
            result = super().json_call(*a, **kw)
            with spans_lock:
                spans.append((self._idx, start, time.monotonic()))
            return result

    def _racer(idx):
        conn = psycopg.connect(pg_dsn, autocommit=True)
        try:
            client = _TimedClient(
                {"dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
                 "dl_item": [{"gtin": "NO_MATCH", "matchConfidence": 0.0,
                             "matchReason": "žiadna zhoda"}]}, idx)
            dl_worker.release_for_question(
                conn, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), qid,
                client=client, upload=lambda *a, **k: None, post=lambda c, h: None)
        except Exception as e:  # pragma: no cover - surfaced via `errors`, not swallowed
            errors.append(e)
        finally:
            conn.close()

    threads = [threading.Thread(target=_racer, args=(i,)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert not any(t.is_alive() for t in threads), "a racer thread hung"
    assert errors == [], f"a racer raised: {errors}"

    # Both racers must have genuinely reached the model (proves the sibling-gate passed
    # for BOTH — the lock does not skip the second one, see dl_worker.py's own corrected
    # docstring) — group each thread's own calls into its [earliest start, latest end]
    # window and assert the two windows never overlap.
    assert len(spans) == 6, f"expected 3 LLM calls per racer, got {spans}"
    windows = {}
    for idx, start, end in spans:
        lo, hi = windows.get(idx, (start, end))
        windows[idx] = (min(lo, start), max(hi, end))
    (w1_lo, w1_hi), (w2_lo, w2_hi) = sorted(windows.values())
    assert w2_lo >= w1_hi, (
        f"the two reprocess attempts overlapped ({w1_lo:.3f}-{w1_hi:.3f} vs "
        f"{w2_lo:.3f}-{w2_hi:.3f}) — the advisory lock did not serialize them")
    # And the SAME real safety net as every other test in this file — even racing, the
    # document is never uploaded twice.
    assert pg.execute(
        "SELECT count(*) FROM desadv_sent WHERE supplier_ean=%s AND doc_number=%s "
        "AND uploaded_at IS NOT NULL", (SUPPLIER_EAN, "0100000001")).fetchone()[0] == 1
    # Both racers DID run a full reprocess (a real, if wasteful, race outcome per the
    # corrected docstring) — two order_runs rows, not one, and not a crash from a
    # doubled upload attempt.
    assert pg.execute("SELECT count(*) FROM order_runs").fetchone()[0] == 3  # tick() + 2


def test_release_for_question_is_a_safe_no_op_when_the_message_row_is_gone(pg):
    """`teach.ask_dl_item`/`ask_dl_supplier` (and the existing #235 tests) are exercised
    directly with synthetic message ids that were never inserted into `messages` at all —
    `release_for_question` must degrade to a harmless empty release, never raise, so
    every existing dl_item/dl_supplier answer flow keeps working exactly as before."""
    from app.orders import teach
    qid = teach.ask_dl_item(pg, message_id="ghost-message", supplier_ean="S1",
                            supplier_name="X", wording="čosi", quantity=1, unit="ks",
                            candidates=[{"gtin": "G1", "name": "Karta"}])
    pg.execute("UPDATE order_questions SET status='answered' WHERE id=%s", (qid,))
    assert dl_worker.release_for_question(pg, _cfg(), qid) == []
