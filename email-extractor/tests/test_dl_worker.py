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
from _race import run_racers
from psycopg.types.json import Json

from app import store
from app.config import Config
from app.orders import (
    desadv,
    desadv_edi,
    dl_memory,
    dl_nonwarehouse,
    dl_snapshot,
    dl_supplier_memory,
    dl_worker,
    reliability,
    report,
    teach,
)

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
        has_attachments=True, combined_text=""):
    pg.execute(
        """INSERT INTO messages (message_id, category, subject, from_addr,
                                 combined_text, has_attachments, processed)
           VALUES (%s, 'dodacie_listy', %s, %s, %s, %s, false)""",
        (mid, subject, from_addr, combined_text, has_attachments))
    return mid


def _attach(pg, tmp_path, mid, idx=0, filename="dl.pdf", text="dodaci list text",
           mime="application/pdf", method="", raw_bytes: bytes | None = None):
    # #247: `method` mirrors `app/extract.py`'s own ingest-time classification stored
    # on the real `attachments.method` column (e.g. `'skipped'` for a decorative/tiny
    # image, `flag='skipped_tiny_image'`) — default "" matches every PRE-#247 caller
    # unchanged (NULL/"" both read back as "" via `_read_attachments`'s `method or ""`).
    # #297: `raw_bytes` lets a test control exactly what sits on disk for this
    # attachment (default unchanged from every pre-#297 caller) — used to prove a
    # spreadsheet's ON-DISK bytes are never read as `pdf_bytes` even when they
    # coincidentally look like a real embedded JPEG.
    pg.execute(
        """INSERT INTO attachments (message_id, idx, filename, mime, extracted_text, method)
           VALUES (%s, %s, %s, %s, %s, %s)""", (mid, idx, filename, mime, text, method))
    d = store.message_dir(str(tmp_path), mid)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"att{idx}__{filename}").write_bytes(
        raw_bytes if raw_bytes is not None else b"%PDF-1.4 no embedded jpeg here\n")


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

# #297 — SYNTHETIC paraphrase of the real Bardusch (#241/#297) .xls machine_text SHAPE
# (grid-style, tab-joined columns, per extraction-formats.md's own testing rules) — NEVER
# the real customer file. Bardusch's actual delivery notes are work-clothing ISSUANCE
# records (Hungarian headers), not classic goods lines; this fixture only proves the
# WORKER now hands a text-bearing .xls to extraction at all — matching/EDI viability for
# that real document shape is a separate, already-tracked question (see #297's own
# comment thread), out of scope for this fix.
XLS_MACHINE_TEXT = (
    "[Sheet: Munka1]\n"
    "Dodavatel Vzor Kft.\t\tTelefon: 00/000-000\t\t\n"
    "9000 Vzorove Mesto\t\twww.example.test\t\t\n"
    "Ulica 8\t\tservis@example.test\t\t\n"
    "Datum : 2026.07.06\t\t\t\t\n"
    "SLOVNORMAL, s.r.o.\t\t\t\t\n"
    "MENO\tCISLO\tPOLOZKA\tPOZNAMKA\t\n"
    "Vzorovy Zamestnanec\t100\tPracovny odev vzor\tOk\t\n"
)


class FakeClient:
    """Scripted answers keyed by the `name=` the worker's json_call passes — a FIFO
    queue per name, so a document with several items can script several `dl_item`
    answers in a row."""

    def __init__(self, answers: dict[str, list[dict]]):
        self._answers = {k: list(v) for k, v in answers.items()}
        self.calls: list[str] = []
        # #258 deep-review: (name, user) per call — lets a test assert on what text a
        # call actually received, e.g. that the body-text fallback never leaks an
        # attachment's own extracted text into the "mail body" it sends the model.
        self.users: list[tuple[str, str]] = []
        self.last_prompt_hash = ""

    def json_call(self, system, user, schema, name="result"):
        self.calls.append(name)
        self.users.append((name, user))
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


# --- #297: a spreadsheet (.xls/.xlsx) delivery note attachment ---------------

def test_read_attachments_recognizes_xls_with_machine_text_and_forces_pdf_bytes_empty(
        pg, tmp_path):
    """#297: an .xls attachment must now be RECOGNIZED by `_read_attachments` (unlike
    before, where the PDF/image-only filter silently dropped it entirely — proven live
    on Bardusch's real message 2183, 0/4 usable attachments, STEP 0 evidence on the
    ticket) — but its raw on-disk bytes must NEVER reach `dl_extract` as `pdf_bytes`: a
    spreadsheet is never a scan and is never sent to Vision (owner's binding #297
    decision). Proven adversarially: the file on disk here is deliberately POISONED
    with a byte sequence that LOOKS like a real embedded JPEG over the scan threshold
    (a coincidental match inside the real OLE2/xlsx container is exactly the risk this
    guards against) — `_read_attachments` must force `pdf_bytes` empty for it
    regardless of what actually sits on disk."""
    mid = _msg(pg, mid="dl1")
    poisoned = b"\xff\xd8" + (b"\x00" * 25_000) + b"\xff\xd9"
    _attach(pg, tmp_path, mid, idx=0, filename="Slovnormal_630935.xls",
           mime="application/vnd.ms-excel", text=XLS_MACHINE_TEXT, method="xls",
           raw_bytes=poisoned)
    cfg = _cfg(data_dir=str(tmp_path))
    out = dl_worker._read_attachments(cfg, mid, pg)
    assert len(out) == 1
    assert out[0]["pdf_bytes"] == b""
    assert out[0]["machine_text"] == XLS_MACHINE_TEXT
    assert out[0]["is_spreadsheet"] is True


def test_an_xls_attachment_with_machine_text_flows_through_the_dl_pipeline(pg, tmp_path):
    """#297 (ROZHODNUTÉ 2026-08-13): a spreadsheet delivery note now reaches extraction
    like any PDF/image — matching, EDI build and upload stay exactly the SAME pipeline.
    RED on pre-fix code: `_read_attachments` drops the .xls entirely (0 usable
    attachments), `client.calls` stays empty and nothing is ever uploaded."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1", idx=0, filename="dodaci.xls",
           mime="application/vnd.ms-excel", text=XLS_MACHINE_TEXT, method="xls")
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
    assert "dl_documents" in client.calls
    assert len(uploaded) == 1
    assert uploaded[0][0].startswith("Z-DESADV_")
    row = pg.execute("SELECT processed FROM messages WHERE message_id='dl1'").fetchone()
    assert row[0] is True


def test_an_xls_attachment_with_no_machine_text_reviews_honestly_without_calling_the_model(
        pg, tmp_path):
    """#297: an .xls whose native extraction produced NO text at all (a genuinely
    unreadable file, or a blank sheet) must never reach `dl_extract` — there is no PDF
    fallback to send it to Vision with (`pdf_bytes` is always forced empty for a
    spreadsheet). Must review honestly, with ZERO model/vision calls — `FakeClient({})`'s
    `vision_call` raises `AssertionError` if the worker ever tried it (mirrors the #247
    crash-detector pattern)."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1", idx=0, filename="prazdny.xls",
           mime="application/vnd.ms-excel", text="", method="")
    posted = []
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(pg, cfg, client=FakeClient({}),
                       post=lambda c, h: posted.append(h))
    assert n == 1
    row = pg.execute(
        "SELECT processed, processed_by FROM messages WHERE message_id='dl1'").fetchone()
    assert row == (True, "dodacie_listy")
    assert len(posted) == 1
    assert "prazdny.xls" in posted[0]
    # the crash symptom must be GONE -- no leaked vision-must-not-be-called assertion,
    # and this is NOT the generic per-attachment extraction-error wording either (this
    # attachment was never even sent to dl_extract).
    assert "vision must not be called" not in posted[0]
    assert "sa nepodarilo spracovať" not in posted[0]


def test_an_xls_attachment_that_yields_no_document_is_flagged_consistently_with_pdf(
        pg, tmp_path):
    """#297: the #238 universal completeness check must treat a text-bearing .xls
    EXACTLY like a PDF/image attachment — processed, but contributed zero documents ->
    flagged individually, `proc_status` must never roll up as a clean 'ok'. Mirrors
    `test_an_attachment_that_yields_no_document_is_flagged_not_silently_lost` with the
    second attachment as an .xls instead of a PDF."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1", idx=0, filename="prvy.pdf")
    _attach(pg, tmp_path, "dl1", idx=1, filename="druhy.xls",
           mime="application/vnd.ms-excel", text=XLS_MACHINE_TEXT, method="xls")
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
        "one processed .pdf must never make the WHOLE run 'ok' when the .xls " \
        "sibling silently yielded no document"
    flagged = [h for h in posted
              if "druhy.xls" in h and "nenašiel sa v nej žiadny dodací list" in h]
    assert len(flagged) == 1, "the empty .xls result must be flagged EXACTLY once"
    ev = pg.execute(
        "SELECT detail FROM email_events WHERE message_id='dl1' "
        "AND status='review' AND outcome LIKE '%druhy.xls%'").fetchone()
    assert ev is not None and ev[0]["idx"] == 1


def test_decorative_image_and_text_bearing_xls_coexist_without_interference(pg, tmp_path):
    """#297: the #247 decorative-attachment filter (image `method='skipped'`) must stay
    completely untouched by the new .xls handling — a signature logo sitting alongside a
    real .xls delivery note must not affect either path."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1", idx=0, filename="podpis.jpeg", mime="image/jpeg",
           text="", method="skipped")
    _attach(pg, tmp_path, "dl1", idx=1, filename="dodaci.xls",
           mime="application/vnd.ms-excel", text=XLS_MACHINE_TEXT, method="xls")
    client = FakeClient({"dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED]})
    uploaded = []
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(
        pg, cfg, client=client,
        upload=lambda c, name, content, dir_override=None:
            uploaded.append((name, content, dir_override)))
    assert n == 1
    assert len(uploaded) == 1
    assert "dl_documents" in client.calls


# --- #258: no usable attachment, but a delivery note in the mail's own BODY TEXT --

# SYNTHETIC — constructed to match the documented template shape (see the module
# docstring), never real customer mail. Mirrors the HK LOAN live incident's own shape
# (a plain-prose delivery notice, no attachment at all) without reusing any real text.
BODY_TEXT_DL = (
    "Dobrý deň,\n\n"
    "posielame avizáciu dodania priamo v texte, bez prílohy:\n\n"
    "Dodávateľ: Pekáreň Lunys, Prešov, dodavatel@lunys.sk\n"
    "Dodací list č. 0100000001, dátum dodania 01.08.2026\n"
    "Rožok 50g / 10 ks / 0,50 €/ks / 5,00 €\n\n"
    "S pozdravom"
)


def test_body_text_delivery_note_is_extracted_when_there_is_no_attachment_at_all(
        pg, tmp_path):
    """#258 live incident (HK LOAN, gnip@hkloan.eu): the supplier never attaches a real
    document at all — the delivery note is written directly in the mail BODY TEXT. Before
    this fix, `_process_message` never even looked at `combined_text` once
    `usable_attachments` was empty; it declared review immediately, with
    `dl_extract.extract_email` never called at all — RED on the pre-fix code (client.calls
    stays empty, nothing uploaded)."""
    _snapshot(pg)
    _msg(pg, mid="dl1", has_attachments=False, combined_text=BODY_TEXT_DL)
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
    assert "dl_documents" in client.calls
    assert len(uploaded) == 1
    assert uploaded[0][0].startswith("Z-DESADV_")
    assert len(posted) == 1
    assert "spracovaný a nahratý do ORIONu" in posted[0]
    row = pg.execute(
        "SELECT processed, processed_by FROM messages WHERE message_id='dl1'").fetchone()
    assert row == (True, "dodacie_listy")


def test_body_text_delivery_note_is_extracted_when_the_only_attachment_is_decorative(
        pg, tmp_path):
    """Same #258 fix, but combined with the #247 shape: a decorative/skipped attachment
    (e.g. a signature logo) sits ALONGSIDE a real delivery note in the body text — the
    decorative attachment must not stop the body-text extraction from being tried."""
    _snapshot(pg)
    _msg(pg, mid="dl1", combined_text=BODY_TEXT_DL)
    _attach(pg, tmp_path, "dl1", filename="podpis.jpeg", mime="image/jpeg",
           text="", method="skipped")
    client = FakeClient({"dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED]})
    uploaded = []
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(
        pg, cfg, client=client,
        upload=lambda c, name, content, dir_override=None:
            uploaded.append((name, content, dir_override)))
    assert n == 1
    assert len(uploaded) == 1
    assert "dl_documents" in client.calls


def test_body_text_with_no_real_delivery_note_still_reviews_cleanly(pg, tmp_path):
    """The body-text fallback must not fabricate a document out of ordinary prose — when
    extraction over the text genuinely finds nothing, the message still reviews cleanly,
    with wording that reflects the mail TEXT was checked (not the old, now-stale
    'ak je dokument v texte e-mailu, treba ho spracovať ručne' hint, which used to tell a
    human to do by hand exactly what this fix now does automatically)."""
    _snapshot(pg)
    _msg(pg, mid="dl1", has_attachments=False,
        combined_text="Dobrý deň, potvrdzujeme prijatie faktúry. S pozdravom.")
    client = FakeClient({"dl_documents": [{"documents": []}]})
    posted = []
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(pg, cfg, client=client, post=lambda c, h: posted.append(h))
    assert n == 1
    assert "dl_documents" in client.calls
    assert len(posted) == 1
    assert "texte e-mailu" in posted[0]
    assert "ak je dokument v texte e-mailu, treba ho spracovať ručne" not in posted[0]
    row = pg.execute(
        "SELECT processed, processed_by FROM messages WHERE message_id='dl1'").fetchone()
    assert row == (True, "dodacie_listy")


def test_a_dl_message_older_than_the_cutoff_routes_to_review_never_auto_uploads(
        pg, tmp_path):
    """#339 safety gap: an OLD stuck dodacie_listy message that becomes claimable again
    (a fresh _claim, a _release_stuck_siblings reset, or a release_for_question reprocess)
    must route to MANUAL REVIEW with an honest reason — NEVER auto-upload a months-old
    delivery note to ORION (the #338 duplicate-delivery risk: 3 DLs from 7.7/17.7
    auto-uploaded 15.8). RED on the pre-fix code: with no age gate a fully-shippable old
    message reaches _process_document -> desadv claim -> upload. The gate short-circuits
    BEFORE extraction, so not even a model call fires."""
    _snapshot(pg)
    _msg(pg, mid="old1", has_attachments=False, combined_text=BODY_TEXT_DL)
    pg.execute("UPDATE messages SET created_at = now() - interval '40 days' "
               "WHERE message_id = 'old1'")
    client = FakeClient({"dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED]})
    uploaded, posted = [], []
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(
        pg, cfg, client=client,
        upload=lambda c, name, content, dir_override=None: uploaded.append(name),
        post=lambda c, h: posted.append(h))
    assert n == 1
    assert uploaded == [], "an over-cutoff DL must NEVER auto-upload to ORION"
    assert client.calls == [], "the age gate must short-circuit BEFORE extraction"
    assert len(posted) == 1
    assert "z bezpečnosti sa NEnahráva automaticky do ORIONu" in posted[0]
    row = pg.execute(
        "SELECT processed, processed_by, proc_status FROM messages "
        "WHERE message_id = 'old1'").fetchone()
    assert row == (True, "dodacie_listy", "review")


def test_release_for_question_on_an_over_cutoff_message_routes_to_review_never_uploads(
        pg, tmp_path):
    """#339: the age gate sits at the shared `_process_message` choke point, so it also
    covers the `release_for_question` re-entry path — a blocked DL whose item question is
    finally answered WEEKS later must NOT silently ship a now-stale delivery note."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    doc = _doc(total=3.0, items=[{"name": "Neznámy chlebík", "quantity": 3, "unit": "ks",
                                  "unitPrice": 1.0, "totalPrice": 3.0}])
    client1 = FakeClient({"dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
                          "dl_item": [{"gtin": "NO_MATCH", "matchConfidence": 0.0,
                                       "matchReason": "žiadna zhoda"}]})
    uploaded = []
    n = dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), client=client1,
        upload=lambda c, name, content, dir_override=None: uploaded.append(name))
    assert n == 1 and uploaded == [], "nothing shippable on the first pass"
    qid = pg.execute("SELECT id FROM order_questions WHERE kind='dl_item'").fetchone()[0]
    # It sat unanswered so long it is now over the cutoff.
    pg.execute("UPDATE messages SET created_at = now() - interval '40 days' "
               "WHERE message_id = 'dl1'")
    dl_memory.remember(pg, SUPPLIER_EAN, "Neznámy chlebík", ITEM_GTIN, "Rožok 50g",
                       "2026-08-10", source="human")
    pg.execute("UPDATE order_questions SET status='answered', answer=%s WHERE id=%s",
              (Json({"choice": ITEM_GTIN}), qid))
    client2 = FakeClient({"dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
                          "dl_item": [ITEM_MATCHED]})
    uploaded2, posted2 = [], []
    released = dl_worker.release_for_question(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), qid,
        client=client2,
        upload=lambda c, name, content, dir_override=None: uploaded2.append(name),
        post=lambda c, h: posted2.append(h))
    assert uploaded2 == [], "an over-cutoff DL must not ship even after its answer arrives"
    assert client2.calls == [], "the age gate short-circuits before extraction"
    assert released and released[0]["outcome"] == "review"
    assert pg.execute(
        "SELECT count(*) FROM desadv_sent WHERE uploaded_at IS NOT NULL").fetchone()[0] == 0


def test_delivery_notes_max_age_days_configures_and_disables_the_cutoff(pg, tmp_path):
    """#339: the cutoff is configurable (a 40-day DL still ships under a 90-day window) and
    0 disables the guard entirely (an old DL ships) — a deployment can tune or turn off the
    safety valve deliberately."""
    _snapshot(pg)
    # (a) 40 days old, cutoff 90 -> within the window -> ships normally.
    _msg(pg, mid="within", has_attachments=False, combined_text=BODY_TEXT_DL)
    pg.execute("UPDATE messages SET created_at = now() - interval '40 days' "
               "WHERE message_id = 'within'")
    client_a = FakeClient({"dl_documents": [_doc(doc_number="0100000091")],
                           "dl_supplier": [SUPPLIER_MATCHED], "dl_item": [ITEM_MATCHED]})
    up_a = []
    dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path),
                 delivery_notes_max_age_days=90),
        client=client_a,
        upload=lambda c, name, content, dir_override=None: up_a.append(name))
    assert len(up_a) == 1, "within the configured window an old DL still ships"

    # (b) 40 days old, cutoff 0 (disabled) -> ships normally.
    _msg(pg, mid="disabled", has_attachments=False, combined_text=BODY_TEXT_DL)
    pg.execute("UPDATE messages SET created_at = now() - interval '40 days' "
               "WHERE message_id = 'disabled'")
    client_b = FakeClient({"dl_documents": [_doc(doc_number="0100000092")],
                           "dl_supplier": [SUPPLIER_MATCHED], "dl_item": [ITEM_MATCHED]})
    up_b = []
    dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path),
                 delivery_notes_max_age_days=0),
        client=client_b,
        upload=lambda c, name, content, dir_override=None: up_b.append(name))
    assert len(up_b) == 1, "max_age_days=0 disables the guard"


def test_empty_body_text_and_no_attachment_still_reviews_without_calling_the_model(
        pg, tmp_path):
    """No attachment AND no body text either -- must stay a cheap, immediate review (no
    LLM call at all), exactly like before this fix."""
    _snapshot(pg)
    _msg(pg, mid="dl1", has_attachments=False, combined_text="")
    posted = []
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(pg, cfg, client=FakeClient({}),
                       post=lambda c, h: posted.append(h))
    assert n == 1
    assert len(posted) == 1 and "bez prílohy" in posted[0]


def test_a_non_pdf_attachments_own_extracted_text_never_leaks_into_the_body_text_source(
        pg, tmp_path):
    """Deep-review finding on #258's own PR: `app/process.py`'s `_combined_text` folds
    ANY successfully-read, non-skipped attachment's own extracted text (docx/xlsx/csv/...
    -- not just PDF/image) into `messages.combined_text` as a trailing "Attachments:\\n..."
    block. `_read_attachments`'s own PDF/image-only filter (this module's documented scope
    decision -- ".docx ... is skipped rather than fed to Vision") already keeps such an
    attachment OUT of `usable_attachments` -- but without stripping that trailing block
    first, the #258 body-text fallback would silently start reading the docx's own text
    anyway (through combined_text), quietly reversing that scope decision. Confirms the
    fallback only ever sees "Subject/From/Body", never the attachment block, by capturing
    the actual extraction-call input text."""
    _snapshot(pg)
    combined_text = (
        "Subject: Objednávka + dodací list príloha\n\n"
        "From: dodavatel@lunys.sk\n\n"
        "Body: v prílohe posielame dodací list.\n\n"
        "Attachments:\n"
        "===== dodaci_list.docx =====\n"
        "Dodávateľ: Pekáreň Lunys, Prešov, dodavatel@lunys.sk\n"
        "Dodací list č. 0100000001, dátum dodania 01.08.2026\n"
        "Rožok 50g / 10 ks / 0,50 €/ks / 5,00 €")
    _msg(pg, mid="dl1", combined_text=combined_text)
    _attach(pg, tmp_path, "dl1", filename="dodaci_list.docx",
           mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
           text="Dodávateľ: Pekáreň Lunys ...", method="docx")
    client = FakeClient({"dl_documents": [{"documents": []}]})
    posted = []
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(pg, cfg, client=client, post=lambda c, h: posted.append(h))
    assert n == 1
    doc_calls = [u for name, u in client.users if name == "dl_documents"]
    assert len(doc_calls) == 1
    sent = doc_calls[0]
    assert "0100000001" not in sent
    assert "dodaci_list.docx" not in sent
    assert "Rožok 50g" not in sent
    assert "v prílohe posielame dodací list" in sent


# --- #265: a mail-body-sourced correction/amendment mail never auto-ships -----------

# SYNTHETIC — paraphrased from the ticket's own quoted wording (mail 6389 "OPRAVA
# HMOTNOSTI"), never the real customer mail verbatim (this repo is public).
CORRECTION_BODY_TEXT = (
    "Dobrý deň,\n\n"
    "z dôvodu chýbajúceho miesta v sile nevyložil včera šofér všetku múku, "
    "posielam Vám novú hmotnosť na múku pšeničnú T650 = 15,88 ton (nie 17,74 ton).\n"
    "Zvyšok dodania bez zmien.\n\n"
    "S pozdravom"
)


def test_correction_mail_never_auto_ships_goes_to_review_with_manual_codex_wording(
        pg, tmp_path):
    """#265 live incident (HK LOAN, mail 6389 "OPRAVA HMOTNOSTI"): a short follow-up
    mail that only restates ONE changed line of an earlier, separately-sent full
    delivery ("Zvyšok dodania bez zmien") must NEVER be auto-shipped — the engine has
    no cross-message memory (#236) and extracting it alone would silently produce a
    document missing the delivery's other items. Per the owner's binding #265 decision
    this ALWAYS goes to manual review, with wording that plainly says a document
    already imported into CODEX must be corrected there BY HAND — never a new ORION
    upload. The model is never even called (`_NeverCalledClient` proves it — defined
    further down this file, used the same way `test_release_for_question_waits_for_
    every_sibling_dl_item_question_before_reprocessing` already does)."""
    _snapshot(pg)
    _msg(pg, mid="dl1", subject="OPRAVA HMOTNOSTI", has_attachments=False,
        combined_text=CORRECTION_BODY_TEXT)
    uploaded, posted = [], []
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(
        pg, cfg, client=_NeverCalledClient(),
        upload=lambda c, name, content, dir_override=None: uploaded.append(name),
        post=lambda c, h: posted.append(h))
    assert n == 1
    assert uploaded == [], "a correction mail must NEVER auto-ship"
    assert len(posted) == 1
    html = posted[0]
    assert "RUČNE" in html and "CODEX" in html, \
        "must explicitly say an already-imported document needs a MANUAL CODEX fix"
    assert "naimportovaný" in html
    assert "múku pšeničnú T650" in html, "quotes the mail's own text, not an AI summary"
    row = pg.execute(
        "SELECT processed, proc_status FROM messages WHERE message_id='dl1'").fetchone()
    assert row == (True, "review")
    assert pg.execute("SELECT count(*) FROM desadv_sent").fetchone()[0] == 0
    assert pg.execute("SELECT count(*) FROM order_questions").fetchone()[0] == 0


def test_empty_spreadsheet_flag_survives_a_correction_mail_early_return(pg, tmp_path):
    """#297 review finding: the #265 correction-detection early return used to build a
    brand-new `documents` list, discarding any entries `documents_out` already held from
    the #297 empty-spreadsheet flagging earlier in this function — reachable whenever a
    message has an unreadable/empty .xls attachment AND falls back to mail-body text
    that itself reads as a correction/amendment. The Odoo post for the spreadsheet flag
    already fires independently (this test also confirms exactly 2 posts happen) — this
    proves `order_runs.result['documents']` (the project's own persisted source of
    truth, see `.claude/rules/n8n-workflow-edits.md`'s #145 pattern) also reflects BOTH
    entries, not just the correction one."""
    _snapshot(pg)
    _msg(pg, mid="dl1", subject="OPRAVA HMOTNOSTI", combined_text=CORRECTION_BODY_TEXT)
    _attach(pg, tmp_path, "dl1", idx=0, filename="prazdny.xls",
           mime="application/vnd.ms-excel", text="", method="")
    uploaded, posted = [], []
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(
        pg, cfg, client=_NeverCalledClient(),
        upload=lambda c, name, content, dir_override=None: uploaded.append(name),
        post=lambda c, h: posted.append(h))
    assert n == 1
    assert uploaded == [], "a correction mail must NEVER auto-ship"
    assert len(posted) == 2, "one post for the empty .xls flag, one for the correction"
    result = pg.execute("SELECT result FROM order_runs").fetchone()[0]
    docs = result["documents"]
    assert len(docs) == 2, \
        "the empty-spreadsheet flag must survive the #265 early return, not be discarded"
    assert any(d.get("synthetic") and "prazdny.xls" in (d.get("reason") or "")
              for d in docs), "the .xls flag entry must still be present"
    assert any(d.get("correction_detected") for d in docs), \
        "the correction entry must still be present"


def test_an_ordinary_body_text_delivery_note_still_ships_normally_265(pg, tmp_path):
    """#265: the correction detector must not swallow an ORDINARY full delivery written
    directly into the mail body (#258/#262, unrelated to any earlier mail) — it must
    still extract and ship exactly as before this ticket (regression guard for the
    detector's own false-positive risk)."""
    _snapshot(pg)
    _msg(pg, mid="dl1", subject="Avizácia dodania", has_attachments=False,
        combined_text=BODY_TEXT_DL)
    client = FakeClient({"dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED]})
    uploaded = []
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(
        pg, cfg, client=client,
        upload=lambda c, name, content, dir_override=None: uploaded.append(name))
    assert n == 1
    assert len(uploaded) == 1, "an ordinary body-text delivery must still ship"


def test_innocent_zmena_wording_does_not_trip_the_correction_detector(pg, tmp_path):
    """#265: 'zmena' alone is deliberately NOT a trigger word (too common in unrelated
    administrative mail — see the design comment on #265) — a full, ordinary delivery
    mail that happens to mention an innocent 'zmena' (a change of BILLING address, not
    a correction of an already-sent document) must still ship normally, not be
    diverted to manual review."""
    _snapshot(pg)
    body = (
        "Dobrý deň,\n\n"
        "upozorňujeme na zmenu fakturačnej adresy od budúceho mesiaca.\n\n"
        "Dodávateľ: Pekáreň Lunys, Prešov, dodavatel@lunys.sk\n"
        "Dodací list č. 0100000001, dátum dodania 01.08.2026\n"
        "Rožok 50g / 10 ks / 0,50 €/ks / 5,00 €\n\n"
        "S pozdravom")
    _msg(pg, mid="dl1", subject="Zmena fakturačných údajov", has_attachments=False,
        combined_text=body)
    client = FakeClient({"dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED]})
    uploaded = []
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(
        pg, cfg, client=client,
        upload=lambda c, name, content, dir_override=None: uploaded.append(name))
    assert n == 1
    assert len(uploaded) == 1, \
        "a bare 'zmena' mention must not trip the correction detector"


# --- #337: a RETIRED catalog card is a KNOWN-but-manual product, never an unknown ----
#
# After the 9 beverage cards were retired (dl_catalog_overrides.retired=true), a delivery
# of them finds no card in the frozen catalog -> unmatched -> a dl_item question EVERY
# delivery (the flood). The engine must instead RECOGNIZE the retired card: route to
# review, never auto-upload, never a per-delivery board question. All SYNTHETIC.
RETIRED_BEV_GTIN = "8580000009999"
RETIRED_BEV_NAME = "Kombucha zázvor"


def _retire_card(pg, gtin, name):
    """A card that exists in dl_catalog_overrides as RETIRED — so it is absent from the
    frozen catalog (load_catalog) but present in dl_snapshot.retired_dl_cards."""
    pg.execute(
        "INSERT INTO dl_catalog_overrides (gtin, name, doplnok, mass, sklad, cena, "
        "retired, updated_at) VALUES (%s, %s, '', NULL, '', NULL, true, now())",
        (gtin, name))


def _bev_doc(doc_number="0100000050", items=None):
    items = items or [{"name": "Kombucha zázvor 250ml", "quantity": 6, "unit": "ks",
                       "unitPrice": 2.0, "totalPrice": 12.0, "vatRate": 10}]
    # total MUST equal the line sum or the money gate reviews the whole doc before matching
    total = round(sum(float(it.get("totalPrice") or 0) for it in items), 2)
    return {"documents": [{
        "supplierName": "Pekáreň Lunys", "supplierCity": "Prešov",
        "supplierEmail": "dodavatel@lunys.sk", "docNumber": doc_number,
        "deliveryDate": "01.08.2026", "documentTotalWithoutVAT": total, "items": items}]}


def _dl_question_count(pg):
    return int(pg.execute(
        "SELECT count(*) FROM order_questions WHERE kind='dl_item'").fetchone()[0])


def test_a_delivery_of_only_a_retired_card_product_reviews_no_upload_no_question(pg, tmp_path):
    """#337: a delivery of ONLY a retired-card product -> review, ZERO uploads, ZERO
    dl_item board questions (recognized as known-but-manual, not treated as unknown)."""
    _snapshot(pg)
    _retire_card(pg, RETIRED_BEV_GTIN, RETIRED_BEV_NAME)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    client = FakeClient({"dl_documents": [_bev_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [{"gtin": "NO_MATCH", "matchConfidence": 0.0,
                                      "matchReason": "karta vyradená z katalógu"}]})
    uploaded, posted = [], []
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(pg, cfg, client=client,
                       upload=lambda c, name, content, dir_override=None: uploaded.append(name),
                       post=lambda c, h: posted.append(h))
    assert n == 1
    assert uploaded == [], "a retired-card product must never auto-upload"
    assert int(pg.execute("SELECT count(*) FROM desadv_sent").fetchone()[0]) == 0
    assert _dl_question_count(pg) == 0, "recognized as a retired card, not flooded as unknown"
    row = pg.execute(
        "SELECT processed, proc_status FROM messages WHERE message_id='dl1'").fetchone()
    assert row[0] is True and row[1] == "review"


def test_an_active_product_still_ships_when_a_retired_one_shares_the_document(pg, tmp_path):
    """#337: recognition is ACTIVE-MATCH-FIRST — an active-card product in the same
    document still ships (partial EDI); only the retired product is diverted, with no
    board question raised for it."""
    _snapshot(pg)
    _retire_card(pg, RETIRED_BEV_GTIN, RETIRED_BEV_NAME)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    doc = _bev_doc(items=[
        {"name": "Rožok 50g", "quantity": 10, "unit": "ks", "unitPrice": 0.5,
         "totalPrice": 5.0, "vatRate": 10},
        {"name": "Kombucha zázvor 250ml", "quantity": 6, "unit": "ks", "unitPrice": 2.0,
         "totalPrice": 12.0, "vatRate": 10}])
    client = FakeClient({"dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED, {"gtin": "NO_MATCH",
                                                    "matchConfidence": 0.0,
                                                    "matchReason": "karta vyradená"}]})
    uploaded = []
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(pg, cfg, client=client,
                       upload=lambda c, name, content, dir_override=None: uploaded.append(name))
    assert n == 1
    assert len(uploaded) == 1, "the active-card product still ships (partial EDI)"
    assert _dl_question_count(pg) == 0, "the retired product raises NO board question"


def test_ship_history_on_a_retired_gtin_never_resurrects_an_upload(pg, tmp_path):
    """#337 bod 2: learned ship-history on a retired card's GTIN must NOT re-enable
    auto-upload (the catalog_gtins filter blocks the memory rescue); the product is
    recognized as retired-manual (review, no board question) instead."""
    _snapshot(pg)
    _retire_card(pg, RETIRED_BEV_GTIN, RETIRED_BEV_NAME)
    for day in ("2026-08-10", "2026-08-11", "2026-08-12"):
        dl_memory.remember(pg, SUPPLIER_EAN, "Kombucha zázvor 250ml", RETIRED_BEV_GTIN,
                           RETIRED_BEV_NAME, day, source="ship")
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    client = FakeClient({"dl_documents": [_bev_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [{"gtin": "NO_MATCH", "matchConfidence": 0.0,
                                      "matchReason": "karta vyradená"}]})
    uploaded = []
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(pg, cfg, client=client,
                       upload=lambda c, name, content, dir_override=None: uploaded.append(name))
    assert n == 1
    assert uploaded == [], "history on a retired GTIN must never re-enable auto-upload"
    assert int(pg.execute("SELECT count(*) FROM desadv_sent").fetchone()[0]) == 0
    assert _dl_question_count(pg) == 0, "recognized as retired, not asked about"


# --- #365: a shippable DL doc with an unmatched WAREHOUSE item is HELD, not partial-shipped -
#
# Before #365: a document with ≥1 matched AND ≥1 genuinely-unmatched (not retired) item
# uploaded a PARTIAL EDI to ORION immediately, dropping the unmatched line, while raising a
# `dl_item` board question that could only teach the FUTURE — the current document's
# completeness was already lost and the warehouse had to add the row in ORION by hand (the
# live msg 8804 / question 101 incident). It must instead HOLD (no claim, no upload) and be
# revisited when the answer arrives. All fixtures SYNTHETIC.

def test_a_shippable_doc_with_an_unmatched_item_holds_instead_of_partial_shipping(
        pg, tmp_path):
    """#365 core: one item matches, one is genuinely unmatched (NOT retired). The document
    is HELD — no claim, no upload — the `dl_item` question is raised, and the ❗ 'potrebuje
    kontrolu / Rieš na nástenke' message is posted (never the ⚠️ 'EDI šlo BEZ nich'
    partial-upload one). `messages` stays processed/review so an answer revisits it."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    doc = _bev_doc(items=[
        {"name": "Rožok 50g", "quantity": 10, "unit": "ks", "unitPrice": 0.5,
         "totalPrice": 5.0, "vatRate": 10},
        {"name": "Neznámy nápoj XYZ", "quantity": 6, "unit": "ks", "unitPrice": 2.0,
         "totalPrice": 12.0, "vatRate": 10}])
    client = FakeClient({"dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED, {"gtin": "NO_MATCH",
                                                    "matchConfidence": 0.0,
                                                    "matchReason": "žiadna zhoda"}]})
    uploaded, posted = [], []
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path),
               dashboard_base_url="http://board.test")
    n = dl_worker.tick(pg, cfg, client=client,
                       upload=lambda c, name, content, dir_override=None: uploaded.append(name),
                       post=lambda c, h: posted.append(h))
    assert n == 1
    assert uploaded == [], "#365: a shippable doc with an unmatched item is HELD, not shipped"
    assert int(pg.execute("SELECT count(*) FROM desadv_sent").fetchone()[0]) == 0, \
        "no claim is taken while held"
    assert _dl_question_count(pg) == 1, "the dl_item board question is raised, as before"
    row = pg.execute(
        "SELECT processed, proc_status FROM messages WHERE message_id='dl1'").fetchone()
    assert row[0] is True and row[1] == "review", "held = processed review, revisited on answer"
    assert posted, "the sklad is told on the delivery-notes channel"
    assert "potrebuje kontrolu" in posted[-1] and "Rieš na nástenke" in posted[-1], \
        "the ❗ hold message with the board link, not the ⚠️ partial-upload success message"
    assert "šlo BEZ nich" not in posted[-1], "the partial-upload wording must NOT appear"


def test_answering_a_held_dl_item_with_a_card_ships_the_complete_edi(pg, tmp_path):
    """#365: the sklad finds/teaches the card → `release_for_question` reprocesses → the
    line now matches (memory rescue) → the COMPLETE EDI ships, nothing dropped."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    doc = _bev_doc(items=[
        {"name": "Rožok 50g", "quantity": 10, "unit": "ks", "unitPrice": 0.5,
         "totalPrice": 5.0, "vatRate": 10},
        {"name": "Neznámy nápoj XYZ", "quantity": 6, "unit": "ks", "unitPrice": 2.0,
         "totalPrice": 12.0, "vatRate": 10}])
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    client1 = FakeClient({"dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
                          "dl_item": [ITEM_MATCHED, {"gtin": "NO_MATCH",
                                                     "matchConfidence": 0.0,
                                                     "matchReason": "žiadna zhoda"}]})
    up1 = []
    dl_worker.tick(pg, cfg, client=client1,
                   upload=lambda c, name, content, dir_override=None: up1.append(name))
    assert up1 == [], "held on the first pass"
    qid = pg.execute("SELECT id FROM order_questions WHERE kind='dl_item'").fetchone()[0]
    # The sklad answers with a card: the human teaching + the answered row (mirrors the
    # real /otazky-dl answer path, exercised directly with a FakeClient like the existing
    # release tests). On reprocess the item is rescued from `dl_item_memory`.
    dl_memory.remember(pg, SUPPLIER_EAN, "Neznámy nápoj XYZ", ITEM_GTIN, "Rožok 50g",
                       "2026-08-10", source="human")
    pg.execute("UPDATE order_questions SET status='answered', answer=%s WHERE id=%s",
               (Json({"choice": ITEM_GTIN}), qid))
    client2 = FakeClient({"dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
                          "dl_item": [ITEM_MATCHED, {"gtin": "NO_MATCH",
                                                     "matchConfidence": 0.0,
                                                     "matchReason": "žiadna zhoda"}]})
    up2 = []
    released = dl_worker.release_for_question(
        pg, cfg, qid, client=client2,
        upload=lambda c, name, content, dir_override=None: up2.append(name))
    assert len(up2) == 1, "#365: the COMPLETE EDI ships once the card is known"
    assert released and released[0]["outcome"] == "ok", "no unmatched item left -> full ship"
    assert int(pg.execute(
        "SELECT count(*) FROM desadv_sent WHERE uploaded_at IS NOT NULL").fetchone()[0]) == 1


def test_answering_a_held_dl_item_with_ship_without_ships_partial_no_loop(pg, tmp_path):
    """#365: "nemá kartu — pošli bez tejto položky" → `release_for_question` reprocesses →
    the line is deliberately EXCLUDED and the doc ships PARTIAL (human-confirmed, honest
    message). Crucially NO new question is re-raised and the doc does NOT re-hold — the
    reprocess reads the ship-without answer back off the question row and skips the line."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    doc = _bev_doc(items=[
        {"name": "Rožok 50g", "quantity": 10, "unit": "ks", "unitPrice": 0.5,
         "totalPrice": 5.0, "vatRate": 10},
        {"name": "Neznámy nápoj XYZ", "quantity": 6, "unit": "ks", "unitPrice": 2.0,
         "totalPrice": 12.0, "vatRate": 10}])
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    client1 = FakeClient({"dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
                          "dl_item": [ITEM_MATCHED, {"gtin": "NO_MATCH",
                                                     "matchConfidence": 0.0,
                                                     "matchReason": "žiadna zhoda"}]})
    up1 = []
    dl_worker.tick(pg, cfg, client=client1,
                   upload=lambda c, name, content, dir_override=None: up1.append(name))
    assert up1 == [], "held on the first pass"
    qid = pg.execute("SELECT id FROM order_questions WHERE kind='dl_item'").fetchone()[0]
    # The sklad answers "pošli bez tejto položky" — the sentinel is stored on the question
    # row exactly as _api_orders_answer_generic does (`{"choice": ship_without}`).
    pg.execute("UPDATE order_questions SET status='answered', answer=%s WHERE id=%s",
               (Json({"choice": teach.DL_ITEM_SHIP_WITHOUT}), qid))
    client2 = FakeClient({"dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
                          "dl_item": [ITEM_MATCHED, {"gtin": "NO_MATCH",
                                                     "matchConfidence": 0.0,
                                                     "matchReason": "žiadna zhoda"}]})
    up2, posted2 = [], []
    released = dl_worker.release_for_question(
        pg, cfg, qid, client=client2,
        upload=lambda c, name, content, dir_override=None: up2.append(name),
        post=lambda c, h: posted2.append(h))
    assert len(up2) == 1, "#365: a partial EDI ships once the sklad confirms ship-without"
    assert released and released[0]["outcome"] == "partial"
    assert int(pg.execute(
        "SELECT count(*) FROM order_questions WHERE kind='dl_item' AND status='open'"
    ).fetchone()[0]) == 0, "no NEW question re-raised — no loop"
    assert int(pg.execute(
        "SELECT count(*) FROM order_questions WHERE kind='dl_item'").fetchone()[0]) == 1, \
        "still exactly the one, now-answered question"
    assert posted2 and "šlo BEZ nich" in posted2[-1], \
        "the honest partial message says the item went WITHOUT — human-confirmed"


def test_a_document_with_ALL_items_matched_still_ships_normally_no_hold(pg, tmp_path):
    """#365 no-regression contract: a document whose items ALL match is UNCHANGED — it
    ships the full EDI immediately, never held."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    doc = _bev_doc(items=[
        {"name": "Rožok 50g", "quantity": 10, "unit": "ks", "unitPrice": 0.5,
         "totalPrice": 5.0, "vatRate": 10}])
    client = FakeClient({"dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED]})
    uploaded = []
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(pg, cfg, client=client,
                       upload=lambda c, name, content, dir_override=None: uploaded.append(name))
    assert n == 1
    assert len(uploaded) == 1, "an all-matched document ships immediately — never held"
    assert _dl_question_count(pg) == 0, "no board question for an all-matched document"
    row = pg.execute(
        "SELECT processed, proc_status FROM messages WHERE message_id='dl1'").fetchone()
    assert row[1] == "ok", "a clean full ship stays 'ok', not 'review'"


def test_a_human_taught_but_still_unmatched_line_ships_partial_not_a_deadend_hold(
        pg, tmp_path):
    """#365 review finding: a line whose wording is already human-taught (so `ask_dl_item`
    REFUSES to ask) yet still comes back unmatched (the #236 R75 lexical-gap class — a
    confident model pick that trips the tripwire, which bypasses the memory rescue) must NOT
    become a permanent DEAD-END hold: with no question row, `release_for_question` is
    structurally unreachable and the doc would strand forever. It ships PARTIAL (the line
    excluded), exactly as pre-#365 — the hold is reserved for board-resolvable lines."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    # The wording is human-taught to a valid catalog GTIN → ask_dl_item's recalled.human
    # pre-check refuses to ask for it.
    dl_memory.remember(pg, SUPPLIER_EAN, "Úplne iný produkt", ITEM_GTIN, "Rožok 50g",
                       "2026-08-10", source="human")
    doc = _doc(total=8.0, items=[
        {"name": "Rožok 50g", "quantity": 10, "unit": "ks", "unitPrice": 0.5,
         "totalPrice": 5.0},
        {"name": "Úplne iný produkt", "quantity": 3, "unit": "ks", "unitPrice": 1.0,
         "totalPrice": 3.0}])
    # The model is CONFIDENT on the taught line (so R73 memory-rescue does NOT fire) but its
    # pick trips the R75 lexical tripwire → gtin=None, unmatched (the #236 class).
    client = FakeClient({
        "dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
        "dl_item": [ITEM_MATCHED,
                   {"gtin": ITEM_GTIN, "matchedCatalogName": "Rožok 50g",
                    "matchConfidence": 0.97, "matchReason": "istý, no odlišné slová"}]})
    uploaded = []
    n = dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), client=client,
        upload=lambda c, name, content, dir_override=None: uploaded.append(name))
    assert n == 1
    assert len(uploaded) == 1, "#365: an ask-refused line ships partial, never a dead-end hold"
    assert _dl_question_count(pg) == 0, "no board question was raised (ask refused) → not held"
    # The R75-tripwire line is still visible in order_items — never silently misclassified.
    assert (None, "llm_sure_lexical_gap") in pg.execute(
        "SELECT gtin, rule FROM order_items").fetchall()


def test_answering_a_held_dl_item_also_releases_a_deduped_same_sender_sibling(pg, tmp_path):
    """#365 review finding: two messages from the SAME supplier with the SAME unknown wording
    — the second's `dl_item` question DEDUPES onto the first's (no own `order_questions` row).
    Answering the first's question must ALSO re-queue the second (`_release_stuck_siblings`,
    now fired for dl_item too), or the second strands FOREVER now that it HOLDS instead of
    partial-shipping."""
    _snapshot(pg)
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    items = [{"name": "Rožok 50g", "quantity": 10, "unit": "ks", "unitPrice": 0.5,
              "totalPrice": 5.0, "vatRate": 10},
             {"name": "Neznámy nápoj XYZ", "quantity": 6, "unit": "ks", "unitPrice": 2.0,
              "totalPrice": 12.0, "vatRate": 10}]
    _dl_ans = [ITEM_MATCHED, {"gtin": "NO_MATCH", "matchConfidence": 0.0,
                             "matchReason": "žiadna zhoda"}]
    # M1 held — question Q raised, tied to dlA.
    _msg(pg, mid="dlA")
    _attach(pg, tmp_path, "dlA")
    up_a = []
    dl_worker.tick(pg, cfg, client=FakeClient(
        {"dl_documents": [_bev_doc(doc_number="0100000060", items=items)],
         "dl_supplier": [SUPPLIER_MATCHED], "dl_item": list(_dl_ans)}),
        upload=lambda c, name, content, dir_override=None: up_a.append(name))
    assert up_a == []
    # M2 — same supplier, same wording. Its ask dedupes onto Q, so it has NO own question row,
    # yet it HOLDS (the line has a board question, tied to dlA).
    _msg(pg, mid="dlB")
    _attach(pg, tmp_path, "dlB")
    up_b = []
    dl_worker.tick(pg, cfg, client=FakeClient(
        {"dl_documents": [_bev_doc(doc_number="0100000061", items=items)],
         "dl_supplier": [SUPPLIER_MATCHED], "dl_item": list(_dl_ans)}),
        upload=lambda c, name, content, dir_override=None: up_b.append(name))
    assert up_b == [], "M2 is also held"
    assert pg.execute(
        "SELECT count(*) FROM order_questions WHERE message_id='dlB'").fetchone()[0] == 0, \
        "M2's ask deduped onto M1's question — no own row"
    assert pg.execute(
        "SELECT processed, proc_status FROM messages WHERE message_id='dlB'").fetchone() \
        == (True, "review")
    # Answer Q (tied to dlA) with a card → dlA ships; dlB must be re-queued, not stranded.
    qid = pg.execute("SELECT id FROM order_questions WHERE message_id='dlA' "
                     "AND kind='dl_item'").fetchone()[0]
    dl_memory.remember(pg, SUPPLIER_EAN, "Neznámy nápoj XYZ", ITEM_GTIN, "Rožok 50g",
                       "2026-08-10", source="human")
    pg.execute("UPDATE order_questions SET status='answered', answer=%s WHERE id=%s",
               (Json({"choice": ITEM_GTIN}), qid))
    dl_worker.release_for_question(pg, cfg, qid, client=FakeClient(
        {"dl_documents": [_bev_doc(doc_number="0100000060", items=items)],
         "dl_supplier": [SUPPLIER_MATCHED], "dl_item": list(_dl_ans)}),
        upload=lambda *a, **k: None)
    assert pg.execute(
        "SELECT processed FROM messages WHERE message_id='dlB'").fetchone()[0] is False, \
        "#365: the deduped sibling is re-queued into the claim pool, never stranded forever"


def test_looks_like_correction_matches_both_real_265_incidents():
    assert dl_worker._looks_like_correction("OPRAVA HMOTNOSTI", "")
    assert dl_worker._looks_like_correction(
        "Fw:Avizacia/24.7.2026/ + oprava v dátume dodania", "")


def test_looks_like_correction_matches_dopln_stem_in_subject():
    assert dl_worker._looks_like_correction("Doplnenie k dodávke", "")


def test_looks_like_correction_matches_dopln_diacritic_forms():
    """Deep-review finding (#265): the FIRST cut's plain-ASCII "dopln" stem missed its
    own most natural Slovak forms — every one of these replaces the plain l/n with the
    diacritic letters ľ/ĺ/ň, and this ticket's OWN issue text names exactly this shape
    ("DOPLŇUJÚCE/OPRAVNÉ maily") as the risk class. All four must match."""
    assert dl_worker._looks_like_correction("DOPLŇUJÚCE informácie", "")
    assert dl_worker._looks_like_correction("doplňujúce údaje k dodávke", "")
    assert dl_worker._looks_like_correction("dopĺňame hmotnosť", "")
    assert dl_worker._looks_like_correction("doplňte prosím", "")


def test_looks_like_correction_ignores_dopln_in_body_only():
    """Deep-review finding (#265): "dopln"/"doplnok" is ordinary Slovak vocabulary
    ("doplnok stravy" = dietary supplement, a real product category) that can
    legitimately appear as a delivered ITEM's own name inside a mail-body-sourced
    delivery note's body text — checking it only in the SUBJECT (never the body)
    avoids permanently misrouting a supplier who happens to sell such products. Both
    real #265 incidents' own signal lives entirely in the subject anyway."""
    assert not dl_worker._looks_like_correction(
        "Avizácia dodania", "Doplnok stravy horčík 60 tbl / 5 ks")


def test_looks_like_correction_matches_korekcia_stem():
    assert dl_worker._looks_like_correction("Korekcia dodávky", "")


def test_looks_like_correction_matches_korektura_synonym():
    """Deep-review finding (#265): "korektúra"/"korektúru" is a genuine Slovak
    synonym for "correction" that the first cut's bare "korekci" stem missed."""
    assert dl_worker._looks_like_correction("Korektúra dodacieho listu", "")
    assert dl_worker._looks_like_correction("posielame korektúru množstva", "")


def test_looks_like_correction_ignores_a_bare_zmena_mention():
    assert not dl_worker._looks_like_correction(
        "Zmena fakturačných údajov", "upozorňujeme na zmenu adresy")


def test_looks_like_correction_ignores_ordinary_prose_with_no_stems():
    assert not dl_worker._looks_like_correction(
        "Avizácia dodania", BODY_TEXT_DL)


def test_correction_review_reason_truncates_a_very_long_excerpt():
    long_text = "Dobrý deň. " + ("x" * 1000)
    reason = dl_worker._correction_review_reason(long_text)
    assert len(reason) < 1000 + 400, "must not embed the whole excerpt unbounded"
    assert reason.rstrip().endswith("(...)")


def test_correction_review_reason_collapses_multiline_whitespace():
    """Deep-review finding (#265): `build_review` wraps the reason in a single `<p>`
    with no `nl2br` — a raw multi-line excerpt would render as one visually
    run-together paragraph. The excerpt is collapsed to single spaces before being
    embedded, without dropping any of its actual words."""
    reason = dl_worker._correction_review_reason("Riadok jeden\n\nRiadok   dva\ttab")
    assert "\n" not in reason and "\t" not in reason
    assert "Riadok jeden Riadok dva tab" in reason


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


# --- #262: a document with NO extracted docNumber (an informal delivery announcement
# in mail body text) must get a STABLE synthesized identity across retries ----------

def test_a_numberless_document_gets_a_stable_doc_number_so_a_retry_never_double_ships(
        pg, tmp_path):
    """A document whose extraction found no docNumber at all (an informal delivery
    announcement, #262) must be assigned the SAME synthesized identity every time the
    SAME message is reprocessed — a stale-claim reclaim (R10, 30 min) or an R17
    transient retry calls the worker again for the SAME message, and if the
    synthesized doc_number changed between attempts, `desadv.
    claim_send_or_identify()`'s (supplier_ean, doc_number) key would treat the retry
    as a BRAND NEW document and genuinely re-upload it to ORION — the same class of
    bug #239 fixed one layer up (the upload-retry path itself), here one layer
    earlier (deciding the document's identity before the first claim attempt).

    Simulated the same way a real stale-claim reclaim looks from the worker's own
    point of view: `tick()` runs the message to completion once, then the message's
    claim state is reset exactly like `_claim()`'s own reclaim SQL would leave it
    (`processed=false, processing_at=NULL`) and `tick()` runs again."""
    _snapshot(pg)
    _msg(pg, mid="dl1", has_attachments=False,
        combined_text="Dobrý deň avizácia na vykládku, dodanie zajtra.")
    doc = _doc(doc_number="", total=0,
              items=[{"name": "Rožok 50g", "quantity": 10, "unit": "ks"}])
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))

    client1 = FakeClient({"dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
                          "dl_item": [ITEM_MATCHED]})
    uploaded, posted = [], []
    n1 = dl_worker.tick(
        pg, cfg, client=client1,
        upload=lambda c, name, content, dir_override=None:
            uploaded.append((name, content, dir_override)),
        post=lambda c, html: posted.append(html))
    assert n1 == 1
    assert len(uploaded) == 1, "the first attempt must ship normally"

    row = pg.execute(
        "SELECT doc_number FROM desadv_sent WHERE supplier_ean=%s", (SUPPLIER_EAN,)
    ).fetchone()
    assert row is not None
    first_doc_number = row[0]
    assert first_doc_number == desadv_edi.generate_stable_doc_number("dl1")
    assert first_doc_number.startswith("AVIZO")

    # Simulate the SAME message being reprocessed (a stale-claim reclaim / R17 retry).
    pg.execute("UPDATE messages SET processed=false, processing_at=NULL "
              "WHERE message_id='dl1'")

    client2 = FakeClient({"dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
                          "dl_item": [ITEM_MATCHED]})
    n2 = dl_worker.tick(
        pg, cfg, client=client2,
        upload=lambda c, name, content, dir_override=None:
            uploaded.append((name, content, dir_override)),
        post=lambda c, html: posted.append(html))
    assert n2 == 1
    assert len(uploaded) == 1, (
        "a retry of the SAME message must be recognized as already-shipped under "
        "the SAME synthesized identity, never re-uploaded a second time")

    ev = pg.execute(
        "SELECT stage FROM email_events WHERE message_id='dl1' "
        "AND stage='already_shipped_this_run'").fetchone()
    assert ev is not None, "the retry must be logged as a self-retry, not silently dropped"


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
    # #322: a supplier genuinely NOT in the effective CODEX list, so the new deterministic
    # rung cannot rescue it — the whole point of #322 is that a KNOWN card no longer fires
    # this question, so this test must exercise a truly-unknown supplier.
    _snapshot(pg)
    _msg(pg, mid="dl1", from_addr="neznamy@nowhere.sk")
    _attach(pg, tmp_path, "dl1")
    client = FakeClient({
        "dl_documents": [_unknown_supplier_doc("neznamy@nowhere.sk")],
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
    # #322: unknown supplier (not in the effective CODEX list) so the deterministic rung
    # cannot rescue it and the review path is genuinely exercised.
    _snapshot(pg)
    _msg(pg, mid="dl1", from_addr="neznamy@nowhere.sk")
    _attach(pg, tmp_path, "dl1")
    client = FakeClient({
        "dl_documents": [_unknown_supplier_doc("neznamy@nowhere.sk")],
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
    # #322: unknown supplier (not in the effective CODEX list) so the deterministic rung
    # cannot rescue it and the review path is genuinely exercised.
    _snapshot(pg)
    _msg(pg, mid="dl1", from_addr="neznamy@nowhere.sk")
    _attach(pg, tmp_path, "dl1")
    client = FakeClient({
        "dl_documents": [_unknown_supplier_doc("neznamy@nowhere.sk")],
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


def test_unmatched_item_holds_the_document_and_raises_a_nastenka_question(pg, tmp_path):
    """#365 (was: partial-ship — the R81 policy this reverses): a shippable document with a
    genuinely unmatched warehouse item is HELD — no claim, no upload — and the `dl_item`
    board question is raised, so the document is later completed rather than shipped
    incomplete."""
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
    assert uploaded == [], "#365: HELD, not partial-shipped"
    assert pg.execute("SELECT count(*) FROM desadv_sent").fetchone()[0] == 0, \
        "no claim while held"
    review = [h for h in posted if "potrebuje kontrolu" in h][0]
    assert "Neznámy chlebík" in review, "the held item is named in the ❗ message"
    assert "spracovaný ČIASTOČNE" not in review, "not the partial-upload message"
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


def test_shadow_mode_still_extracts_a_correction_mail_for_comparison(pg, tmp_path):
    """Deep-review finding on this ticket's own PR (#265): the correction-detection
    gate is `not shadow`-gated, matching this project's own documented rule
    (`.claude/rules/orders-corpus.md`: a short-circuit that skips calling the model
    needs the same `not shadow` gate, precedent `pipeline._mail_rule`) and this
    module's own "shadow runs the FULL pipeline... for comparison" contract. In shadow
    the message still goes through extraction (proven here — the model IS called),
    even though it would never auto-ship live; nothing is claimed/uploaded/taught
    either way (the pre-existing shadow guarantees, unaffected)."""
    _snapshot(pg)
    _msg(pg, mid="dl1", subject="OPRAVA HMOTNOSTI", has_attachments=False,
        combined_text=CORRECTION_BODY_TEXT)
    client = FakeClient({"dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED]})
    cfg = _cfg(delivery_notes_engine="n8n", delivery_notes_shadow=True,
              data_dir=str(tmp_path))
    n = dl_worker.tick(pg, cfg, client=client)
    assert n == 1
    assert "dl_documents" in client.calls, \
        "shadow must still extract a correction mail for comparison, never skip it"
    assert pg.execute("SELECT count(*) FROM desadv_sent").fetchone()[0] == 0
    assert pg.execute("SELECT count(*) FROM order_questions").fetchone()[0] == 0


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


def test_announced_but_not_attached_dl_is_logged_but_not_announced(pg, tmp_path):
    """spec §4 detection stays, but the per-mail Odoo warning was removed as noise on the
    owner's request (#358): the subject names TWO DL numbers, only ONE PDF (and therefore
    one extracted docNumber) ever arrives. The mismatch must NO LONGER be posted to Odoo,
    while the internal signal — the `announced_mismatch` email_events row and a
    `proc_status` of "partial" — is preserved unchanged."""
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

    # #358: the announced-but-unattached warning must NOT be posted to Odoo any more.
    assert not [h for h in posted if "ohlásený aj doklad" in h], \
        "the per-mail announced-mismatch Odoo warning was removed on the owner's request"
    # the change is surgical — the DL that DID arrive still gets its own success post.
    assert any("Dodací list 0100000001" in h and "spracovaný a nahratý do ORIONu" in h
               for h in posted), \
        "the attached document's own success message must still be posted"
    # detection is kept: the internal announced_mismatch event is still written.
    ev = pg.execute(
        "SELECT detail FROM email_events WHERE message_id='dl1' "
        "AND stage='announced_mismatch'").fetchone()
    assert ev is not None
    assert ev[0]["announced"] == ["0100000002"]
    # #238 requirement #2: a run with a genuinely missing announced document must
    # NEVER roll up as "ok" — proc_status itself must be honest, independent of the
    # (now removed) Odoo alert.
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

def test_a_timed_out_upload_falls_back_to_no_retry_when_orion_host_is_unconfigured(
        pg, tmp_path):
    """Renamed + docstring corrected in the SAME commit as #239 finding 6's remainder
    (own commit, justified — the test's own assertions are unchanged, only the
    explanation of WHY they hold was stale): the original text claimed `upload.put()`
    "writes straight to the FINAL ... with no temp-write + rename" — that infrastructure
    has since shipped (see `upload.py`'s own module docstring) and is no longer true.
    It also framed "an upload failure must never be re-uploaded automatically" as an
    unconditional rule — finding 6's remainder makes that conditional: a TRANSIENT
    failure whose stable-identity presence check proves ABSENCE now gets exactly one
    safe retry (see the `test_a_transient_upload_failure_*` tests above).

    This test's own scenario still correctly pins the NO-retry outcome, but for the
    real reason: `cfg` here has NO `orion_host` configured (`_cfg()`'s default), and no
    `list_dirs` fake is injected either — so `_check_landed()` calls the REAL
    `upload_mod.list_dirs(cfg)`, which fails immediately (`_connect()` raises before any
    network I/O) exactly like a genuinely misconfigured add-on would. That is the
    "presence check unavailable" branch (see also
    `test_a_transient_upload_failure_falls_back_when_the_presence_check_is_unavailable`,
    which pins the SAME branch via an explicit raising `list_dirs` fake instead — kept
    as two separate regression pins because "orion_host was never configured" and "the
    SFTP connection is down right now" are two distinct real operational causes for the
    identical safe fallback).

    So: exactly ONE upload attempt must be made, the message must end terminal (not
    re-armed for the 30-minute stale reclaim), and the durable alert must still be
    enqueued so the failure stays visible — the v0.9.70 duplicate-delivery incident
    this whole ticket exists to prevent never gets a chance to recur here."""
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
    # #336: the enqueued body is ONE short line naming the supplier + delivery note; the
    # "nahranie zlyhalo" explanation lives once in the flush-time header, not per row.
    assert "dodací list" in alert[4] and "Pekáreň Lunys" in alert[4]

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


# --- #239 finding 6 (remainder): safe automatic retry, built on the already-shipped
# --- temp-write+rename upload + stable-identity presence-check primitives -----------

def _stable_prefix():
    return desadv_edi.stable_prefix(SUPPLIER_EAN, "0100000001")


def _dirs(*, in_dl=(), arch=(), unconfirmed=()):
    return {"in": set(), "in_DL": set(in_dl), "archCodex": set(arch),
           "unconfirmed": set(unconfirmed)}


def test_a_transient_upload_failure_confirms_instead_of_reuploading_when_already_landed(
        pg, tmp_path):
    """#239 finding 6, branch 1 (bytes-landed-reply-lost): a TRANSIENT upload failure
    (`timed out`) whose stable-identity presence check proves the document is ALREADY on
    ORION under an earlier attempt's name (the exact ambiguity `upload.put()`'s
    temp-write+rename makes provable) must NEVER trigger a second upload — that would be
    the live v0.9.70 duplicate-delivery incident all over again. The claim must be kept
    (confirmed, never released) and the document must finish as a genuine success."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    client = FakeClient({"dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED]})
    tries = []

    def _timed_out_upload(c, name, content, dir_override=None):
        tries.append(name)
        raise OSError("connection timed out")

    list_dirs_calls = []

    def _fake_list_dirs(cfg):
        list_dirs_calls.append(cfg)
        return _dirs(in_dl=[f"Z-{_stable_prefix()}20260801_120000000.txt"])

    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(pg, cfg, client=client, upload=_timed_out_upload,
                       list_dirs=_fake_list_dirs)
    assert n == 1
    assert len(tries) == 1, "a landed document must never be re-uploaded"
    assert len(list_dirs_calls) == 1
    row = pg.execute(
        "SELECT processed FROM messages WHERE message_id='dl1'").fetchone()
    assert row == (True,)
    assert pg.execute(
        "SELECT count(*) FROM desadv_sent WHERE uploaded_at IS NOT NULL"
    ).fetchone()[0] == 1, "the claim must be CONFIRMED, never released"
    assert pg.execute(
        "SELECT count(*) FROM pending_alerts").fetchone()[0] == 0, \
        "a genuine success must never enqueue an upload-failure alert"
    event = pg.execute(
        "SELECT stage, status FROM email_events WHERE message_id='dl1' "
        "AND stage='uploaded_orion'").fetchone()
    assert event == ("uploaded_orion", "ok")


def test_a_transient_upload_failure_retries_exactly_once_with_the_same_claim_when_absent(
        pg, tmp_path):
    """#239 finding 6, branch 2 (absent, safe retry): when the presence check proves the
    document is genuinely NOT on ORION yet, exactly ONE retry is safe — same claim held
    throughout (never release-then-reclaim, which is what removed the protection in the
    v0.9.70 incident this whole ticket exists to fix)."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    client = FakeClient({"dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED]})
    tries = []

    def _flaky_upload(c, name, content, dir_override=None):
        tries.append(name)
        if len(tries) == 1:
            raise OSError("connection timed out")
        return True

    list_dirs_calls = []

    def _fake_list_dirs(cfg):
        list_dirs_calls.append(cfg)
        return _dirs()

    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(pg, cfg, client=client, upload=_flaky_upload,
                       list_dirs=_fake_list_dirs)
    assert n == 1
    assert len(tries) == 2, "exactly one retry, never a loop"
    assert len(list_dirs_calls) == 1, "the presence check runs once, before the retry"
    row = pg.execute(
        "SELECT processed FROM messages WHERE message_id='dl1'").fetchone()
    assert row == (True,)
    assert pg.execute(
        "SELECT count(*) FROM desadv_sent WHERE uploaded_at IS NOT NULL"
    ).fetchone()[0] == 1, "the SAME claim survived (never released) and got confirmed"
    assert pg.execute(
        "SELECT count(*) FROM pending_alerts").fetchone()[0] == 0


def test_a_transient_upload_failure_when_the_retry_also_fails_alerts_without_looping(
        pg, tmp_path):
    """#239 finding 6, branch 3 (retry-fails): the single retry is BOUNDED, never a
    loop — when it also fails, this falls back to the existing durable-alert path
    (release the claim, enqueue durably), unchanged from before finding 6."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    client = FakeClient({"dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED]})
    tries = []

    def _always_timed_out_upload(c, name, content, dir_override=None):
        tries.append(name)
        raise OSError("connection timed out")

    def _fake_list_dirs(cfg):
        return _dirs()

    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(pg, cfg, client=client, upload=_always_timed_out_upload,
                       list_dirs=_fake_list_dirs)
    assert n == 1
    assert len(tries) == 2, "the original attempt plus exactly one bounded retry"
    row = pg.execute(
        "SELECT processed FROM messages WHERE message_id='dl1'").fetchone()
    assert row == (True,)
    assert pg.execute("SELECT count(*) FROM desadv_sent").fetchone()[0] == 0, \
        "a genuinely failed retry releases the claim, same as today's no-retry path"
    assert pg.execute(
        "SELECT count(*) FROM pending_alerts WHERE kind='dl_upload_failed'"
    ).fetchone()[0] == 1


def test_a_transient_upload_failure_falls_back_when_the_presence_check_is_unavailable(
        pg, tmp_path):
    """#239 finding 6, branch 4 (check-unavailable): the SFTP connection that just
    failed the upload is very likely down for the presence check too — when the check
    itself raises, NO retry is attempted (a blind retry with no absence proof is exactly
    the v0.9.70 duplicate-delivery bug), falling back to today's alert-and-release
    path."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    client = FakeClient({"dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED]})
    tries = []

    def _timed_out_upload(c, name, content, dir_override=None):
        tries.append(name)
        raise OSError("connection timed out")

    def _broken_list_dirs(cfg):
        raise OSError("SFTP unavailable")

    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(pg, cfg, client=client, upload=_timed_out_upload,
                       list_dirs=_broken_list_dirs)
    assert n == 1
    assert len(tries) == 1, "no retry without an absence proof"
    row = pg.execute(
        "SELECT processed FROM messages WHERE message_id='dl1'").fetchone()
    assert row == (True,)
    assert pg.execute("SELECT count(*) FROM desadv_sent").fetchone()[0] == 0
    assert pg.execute(
        "SELECT count(*) FROM pending_alerts WHERE kind='dl_upload_failed'"
    ).fetchone()[0] == 1


def test_a_transient_upload_failure_never_trusts_a_stable_prefix_collision(pg, tmp_path):
    """#239 finding 6, review finding (own commit, RED->GREEN): `desadv_edi.
    stable_prefix()` truncates `doc_number` to its first 10 alnum characters (R89's
    on-wire filename budget) — two GENUINELY DIFFERENT doc numbers from the SAME
    supplier that only differ past position 10 collide onto the IDENTICAL stable
    prefix. Without a guard, a transient upload failure for document B would see
    document A's (a different, already-CONFIRMED document) file in the directory
    listing, `already_landed()` would report `True`, and B would be silently
    confirmed as shipped without ever actually reaching ORION — the mirror image of
    the v0.9.70 duplicate-upload incident this whole ticket exists to prevent, just
    silent LOSS instead of silent DUPLICATION."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    other_doc_number = "01000000011"
    this_doc_number = "01000000012"
    assert desadv_edi.stable_prefix(SUPPLIER_EAN, other_doc_number) == \
        desadv_edi.stable_prefix(SUPPLIER_EAN, this_doc_number), \
        "the fixture must genuinely collide on the first 10 alnum chars"
    other_filename = desadv_edi.filename(SUPPLIER_EAN, "01.08.2026", other_doc_number,
                                         stamp="090000000")
    pg.execute(
        """INSERT INTO desadv_sent (supplier_ean, doc_number, filename, uploaded_at)
           VALUES (%s, %s, %s, now())""",
        (SUPPLIER_EAN, other_doc_number, other_filename))

    client = FakeClient({"dl_documents": [_doc(doc_number=this_doc_number)],
                         "dl_supplier": [SUPPLIER_MATCHED], "dl_item": [ITEM_MATCHED]})
    tries = []

    def _timed_out_upload(c, name, content, dir_override=None):
        tries.append(name)
        raise OSError("connection timed out")

    def _fake_list_dirs(cfg):
        # Document A's own file, genuinely sitting on ORION, matches document B's
        # stable prefix too — the exact collision this test proves is refused.
        return _dirs(in_dl=[f"Z-{other_filename}"])

    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(pg, cfg, client=client, upload=_timed_out_upload,
                       list_dirs=_fake_list_dirs)
    assert n == 1
    assert len(tries) == 1, "a collision must never be trusted as a landed proof"
    assert pg.execute(
        "SELECT uploaded_at FROM desadv_sent WHERE doc_number=%s", (this_doc_number,)
    ).fetchone() is None, "the DIFFERENT document must never be confirmed"
    assert pg.execute(
        "SELECT uploaded_at IS NOT NULL FROM desadv_sent WHERE doc_number=%s",
        (other_doc_number,)).fetchone() == (True,), "the OTHER document stays confirmed"
    assert pg.execute(
        "SELECT count(*) FROM pending_alerts WHERE kind='dl_upload_failed'"
    ).fetchone()[0] == 1


# --- #239 class 3: classified as DL but never even attempted -----------------

def test_stuck_classified_sweep_uses_a_short_line_with_no_microsecond_timestamps(pg):
    """#336: the enqueued body is ONE short line (`• odosielateľ — predmet (prijaté D.M.)`);
    the explanation sentence + the dashboard action link live ONCE in the flush-time header
    (`GROUPED_ITEM_KINDS`), and the old microsecond `prijaté:`/`zistené:` timestamps are gone."""
    _msg(pg, mid="dl1")
    pg.execute(
        "UPDATE messages SET created_at = now() - interval '31 minutes' "
        "WHERE message_id = 'dl1'")
    dl_worker.stuck_classified_sweep(pg, _cfg())
    html = pg.execute(
        "SELECT body_html FROM pending_alerts WHERE message_id='dl1'").fetchone()[0]
    assert html.startswith("<p>&#8226; ")                        # a short bullet line
    assert "dodavatel@lunys.sk" in html
    assert "(prijaté " in html                                    # short D.M. suffix
    assert "prijaté:" not in html and "zistené:" not in html      # no microsecond timestamps
    assert "spracovanie sa vôbec nezačalo" not in html            # explanation is in the header


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
    # #310: an engine-liveness/staleness alert is OPERATOR-facing, so it routes to the
    # ops channel (report.ops_channel), NEVER delivery_notes_channel_id (243, warehouse).
    # Default cfg leaves ops unset (0) — the row is still recorded durably, just never on
    # a warehouse channel. Full "never 243/152" guarantee: test_operator_alert_routing.py.
    assert alert == (report.ops_channel(_cfg()), "dl_stuck_classified", "dl1", None)


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
    the Odoo message nor a nástenka question ever mentioned it. #365: the excluded item
    now HOLDS the document (was: partial-shipped) — it is still visible in `order_items`
    and named in the ❗ hold message + a board question, never silently dropped."""
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
    assert uploaded == [], "#365: HELD (was: partial EDI) — not shipped incomplete"
    review = [h for h in posted if "potrebuje kontrolu" in h][0]
    assert "Úplne iný produkt" in review, "the R75-gap item is named in the ❗ hold message"
    q = pg.execute(
        "SELECT kind FROM order_questions WHERE kind='dl_item'").fetchone()
    assert q is not None

    # The R75-tripwire item is still VISIBLE in order_items with its rule (the original
    # deep-review finding this test protects) — the hold does not hide it.
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
    it raises a FRESH, visible question for what remains, so the warehouse is never left
    guessing. #365: it now HOLDS (was: shipped what it could as a partial EDI) — the still-
    unresolved item is asked about again AND the document stays held until it is complete."""
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
    assert uploaded2 == [], "#365: HELD (was: partial ship) — not shipped while unresolved"
    assert released and released[0]["outcome"] == "review"
    fresh = pg.execute(
        "SELECT id, status FROM order_questions WHERE kind='dl_item' AND "
        "wording='Úplne iný produkt' ORDER BY id").fetchall()
    assert len(fresh) == 2, "a brand-new question was raised — the old one is not reused"
    assert fresh[0] == (qid_b, "answered")
    assert fresh[1][1] == "open", "the still-unresolved item is visibly asked about again"
    assert any("potrebuje kontrolu" in h and "Úplne iný produkt" in h for h in posted2), \
        "also visible in the ❗ hold message, not just the nástenka question"


def test_release_for_question_never_reuploads_an_already_partially_shipped_document(
        pg, tmp_path):
    """HARD SAFETY (#240, updated for #365 — the msg 8804 / question 101 incident): a
    document that ALREADY shipped must NEVER be re-uploaded just because its excluded item's
    dl_item question later gets answered. Pre-#365 the first pass shipped a partial EDI to
    set this up; #365 now HOLDS instead, so the already-shipped state is injected directly (a
    confirmed `desadv_sent` claim, exactly as a pre-#365 partial ship or any earlier process
    would have left it). The guard is the SAME `desadv.claim_send_or_identify` ledger every
    `_process_document` call already makes — not new code — proven to hold across the
    reprocess-on-answer path."""
    _snapshot(pg)
    _msg(pg, mid="dl3")
    _attach(pg, tmp_path, "dl3")
    doc = _doc(total=8.0, items=[
        {"name": "Rožok 50g", "quantity": 10, "unit": "ks", "unitPrice": 0.5,
         "totalPrice": 5.0},
        {"name": "Neznámy chlebík", "quantity": 3, "unit": "ks", "unitPrice": 1.0,
         "totalPrice": 3.0}])
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    client1 = FakeClient({"dl_documents": [doc], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED,
                                    {"gtin": "NO_MATCH", "matchConfidence": 0.0,
                                     "matchReason": "žiadna zhoda"}]})
    uploaded = []
    n = dl_worker.tick(pg, cfg, client=client1,
                       upload=lambda c, name, content, dir_override=None: uploaded.append(name))
    assert n == 1
    assert uploaded == [], "#365: HELD on the first pass — no partial ship, no claim"
    # Inject the ALREADY-shipped state: a confirmed desadv_sent claim held by THIS message,
    # exactly the msg 8804 situation (a document that shipped before #365 deployed, whose
    # dl_item question is still open). The reprocess below must not re-upload it.
    desadv.claim_send_or_identify(
        pg, SUPPLIER_EAN, "0100000001", "DESADV_synthetic.txt", message_id="dl3")
    desadv.confirm_sent(pg, SUPPLIER_EAN, "0100000001")
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
        pg, cfg, qid, client=client2,
        upload=lambda c, name, content, dir_override=None: uploaded2.append(name),
        post=lambda c, h: posted2.append(h))
    assert uploaded2 == [], "the already-shipped document must NEVER be re-uploaded"
    assert released and released[0]["outcome"] == "duplicate"
    assert pg.execute(
        "SELECT count(*) FROM desadv_sent WHERE uploaded_at IS NOT NULL").fetchone()[0] == 1, \
        "still exactly the ONE original confirmed claim — no second upload"
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

    threads = [threading.Thread(target=_racer, args=(i,), name=f"racer-{i}")
              for i in (1, 2)]
    # #291: the old `assert not any(t.is_alive() ...)` DETECTED a hang but never
    # cleaned it up — a genuinely stalled racer's connection would still hold the
    # advisory lock open, wedging every later test. run_racers fails loudly AND
    # terminates the stray backend so the suite is never wedged.
    run_racers(pg, threads, timeout=15, label="dl_advisory_lock")
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


# --- #265 gap 2: release_for_question also releases orphaned same-sender siblings ---

def _unknown_supplier_doc(sender_email, name="Neznáma pekáreň s.r.o."):
    """A document with NO printed doc number — mirrors the real HK LOAN shape (an
    informal mail-body announcement), so `desadv_edi.generate_stable_doc_number`
    synthesizes a DIFFERENT stable identity per message_id (#262) and several sibling
    messages can each ship without colliding on the same (supplier_ean, doc_number)."""
    return {"documents": [{
        "supplierName": name, "supplierCity": "", "supplierEmail": sender_email,
        "docNumber": "", "deliveryDate": "01.08.2026", "documentTotalWithoutVAT": 5.0,
        "items": [{"name": "Rožok 50g", "quantity": 10, "unit": "ks", "unitPrice": 0.5,
                  "totalPrice": 5.0}]}]}


def test_release_for_question_also_ships_orphaned_same_sender_sibling_messages(
        pg, tmp_path):
    """#265 gap 2 (live evidence: HK LOAN had 5 `dodacie_listy` messages from the same
    still-unregistered sender, but `ask_dl_supplier`'s per-sender dedupe means only the
    FIRST ever gets its own `order_questions` row — the other 4 sat `processed=true`
    forever, with nothing tied to answer). Answering the ONE open `dl_supplier` question
    must now also give every OTHER orphaned same-sender message a fresh chance — proven
    end-to-end here via two more `tick()` calls after the release."""
    _snapshot(pg)
    sender = "neznamy@somewhere.sk"
    doc = _unknown_supplier_doc(sender)
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    for mid in ("sib1", "sib2", "sib3"):
        _msg(pg, mid=mid, from_addr=sender, has_attachments=True)
        _attach(pg, tmp_path, mid)
        n = dl_worker.tick(
            pg, cfg,
            client=FakeClient({"dl_documents": [doc],
                               "dl_supplier": [{"matched": False,
                                                "matchReason": "nie je v zozname"}]}),
            upload=lambda *a, **k: None, post=lambda c, h: None)
        assert n == 1

    qids = pg.execute(
        "SELECT id FROM order_questions WHERE kind='dl_supplier'").fetchall()
    assert len(qids) == 1, "the per-sender dedupe means only ONE question exists"
    qid = qids[0][0]

    rows = pg.execute(
        "SELECT processed, proc_status FROM messages WHERE from_addr=%s "
        "ORDER BY message_id", (sender,)).fetchall()
    assert rows == [(True, "review")] * 3, \
        "setup: all three sibling messages are stuck in review"
    assert pg.execute(
        "SELECT count(*) FROM order_questions oq WHERE oq.message_id IN "
        "('sib1', 'sib2', 'sib3')").fetchone()[0] == 1, \
        "setup: only ONE of the three is actually tied to a question"

    tied_message_id = pg.execute(
        "SELECT message_id FROM order_questions WHERE id=%s", (qid,)).fetchone()[0]

    dl_supplier_memory.remember(pg, sender, SUPPLIER_EAN, "Pekáreň Lunys")
    pg.execute("UPDATE order_questions SET status='answered', answer=%s WHERE id=%s",
              (Json({"choice": SUPPLIER_EAN}), qid))

    released = dl_worker.release_for_question(
        pg, cfg, qid, client=FakeClient({"dl_documents": [doc], "dl_item": [ITEM_MATCHED]}),
        upload=lambda *a, **k: None, post=lambda c, h: None)
    assert released and released[0]["outcome"] == "ok", "the tied message ships"

    stuck = pg.execute(
        "SELECT message_id, processing_at, attempts FROM messages WHERE from_addr=%s "
        "AND processed=false ORDER BY message_id", (sender,)).fetchall()
    stuck_ids = sorted(r[0] for r in stuck)
    expected_orphans = sorted({"sib1", "sib2", "sib3"} - {tied_message_id})
    assert stuck_ids == expected_orphans, \
        "exactly the two orphaned siblings (never the already-finished tied one) " \
        "are released back into the claim pool"
    assert all(r[1] is None and r[2] == 0 for r in stuck), \
        "reset rows must have a clean processing_at/attempts, ready for a fresh claim"

    for _ in range(2):
        n = dl_worker.tick(
            pg, cfg,
            client=FakeClient({"dl_documents": [doc], "dl_item": [ITEM_MATCHED]}),
            upload=lambda *a, **k: None, post=lambda c, h: None)
        assert n == 1
    assert pg.execute(
        "SELECT count(*) FROM messages WHERE from_addr=%s AND processed=true "
        "AND proc_status='ok'", (sender,)).fetchone()[0] == 3, \
        "all three sibling messages eventually ship, none lost"
    assert pg.execute(
        "SELECT count(*) FROM desadv_sent WHERE supplier_ean=%s "
        "AND uploaded_at IS NOT NULL", (SUPPLIER_EAN,)).fetchone()[0] == 3, \
        "three distinct stable doc numbers -- no collision, no duplicate skip"


def test_release_for_question_sibling_widening_never_touches_unrelated_messages(
        pg, tmp_path):
    """The widening proven above must be precisely scoped: a DIFFERENT sender's own
    stuck message, an ALREADY-SHIPPED message from the SAME sender, a message from the
    SAME sender that already has its OWN tied `dl_item` question, and a message from
    the SAME sender whose upload genuinely FAILED (#265 deep-review 🔴 finding — must
    never be silently reset into an automatic retry, see `.claude/rules/
    n8n-workflow-edits.md`'s "#239" section) must all be left completely untouched.

    Deep-review finding on this ticket's own PR (#265): the FIRST cut of this test
    proved only "nothing bad happens" — every assertion here would ALSO hold if
    `_release_stuck_siblings` were a complete no-op (a blank key, a wrong column, an
    early return). A POSITIVE control (`orphan1`, a genuinely clean same-sender orphan
    with none of the disqualifying properties) proves the widening actually DOES
    something, not just that it stays out of the way."""
    _snapshot(pg)
    sender = "neznamy3@somewhere.sk"
    other_sender = "inysender@example.sk"
    _msg(pg, mid="other1", from_addr=other_sender)
    pg.execute(
        "UPDATE messages SET processed=true, proc_status='review' WHERE message_id=%s",
        ("other1",))
    _msg(pg, mid="shipped1", from_addr=sender)
    pg.execute(
        "UPDATE messages SET processed=true, proc_status='ok' WHERE message_id=%s",
        ("shipped1",))
    _msg(pg, mid="tied1", from_addr=sender)
    pg.execute(
        "UPDATE messages SET processed=true, proc_status='review' WHERE message_id=%s",
        ("tied1",))
    teach.ask_dl_item(
        pg, message_id="tied1", supplier_ean=SUPPLIER_EAN, supplier_name="Pekáreň Lunys",
        wording="Iný nespárovaný tovar", quantity=1, unit="ks",
        candidates=[{"gtin": ITEM_GTIN, "name": "Rožok 50g"}])
    _msg(pg, mid="failedupload1", from_addr=sender)
    pg.execute(
        "UPDATE messages SET processed=true, proc_status='review' WHERE message_id=%s",
        ("failedupload1",))
    pg.execute(
        """INSERT INTO email_events (message_id, workflow, stage, status, outcome)
           VALUES ('failedupload1', 'delivery_notes', 'review', 'error',
                   'Odoslanie dodacieho listu do ORIONu zlyhalo')""")
    _msg(pg, mid="orphan1", from_addr=sender)
    pg.execute(
        "UPDATE messages SET processed=true, proc_status='review' WHERE message_id=%s",
        ("orphan1",))

    _msg(pg, mid="qmsg", from_addr=sender, has_attachments=True)
    _attach(pg, tmp_path, "qmsg")
    doc = _unknown_supplier_doc(sender)
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(
        pg, cfg,
        client=FakeClient({"dl_documents": [doc],
                           "dl_supplier": [{"matched": False,
                                            "matchReason": "nie je v zozname"}]}),
        upload=lambda *a, **k: None, post=lambda c, h: None)
    assert n == 1
    qid = pg.execute(
        "SELECT id FROM order_questions WHERE kind='dl_supplier'").fetchone()[0]

    dl_supplier_memory.remember(pg, sender, SUPPLIER_EAN, "Pekáreň Lunys")
    pg.execute("UPDATE order_questions SET status='answered', answer=%s WHERE id=%s",
              (Json({"choice": SUPPLIER_EAN}), qid))
    released = dl_worker.release_for_question(
        pg, cfg, qid, client=FakeClient({"dl_documents": [doc], "dl_item": [ITEM_MATCHED]}),
        upload=lambda *a, **k: None, post=lambda c, h: None)
    assert released and released[0]["outcome"] == "ok"

    untouched = pg.execute(
        "SELECT message_id, processed FROM messages WHERE message_id IN "
        "('other1', 'shipped1', 'tied1', 'failedupload1') "
        "ORDER BY message_id").fetchall()
    assert untouched == [("failedupload1", True), ("other1", True), ("shipped1", True),
                         ("tied1", True)], \
        "none of the four unrelated/already-linked/failed messages may be reset"
    positive = pg.execute(
        "SELECT processed FROM messages WHERE message_id='orphan1'").fetchone()
    assert positive == (False,), \
        "the genuinely clean orphan (positive control) MUST be released"


def test_release_for_question_sibling_widening_keys_on_envelope_from_addr(
        pg, tmp_path):
    """Deep-review finding on this ticket's own PR (#265) — a REAL, proven scoping
    bug: `_process_document` sets `order_questions.payload['sender_email']` to
    `doc.get('supplierEmail') or from_addr` — the DOCUMENT-EXTRACTED address, which
    can genuinely differ from the raw envelope `messages.from_addr` (e.g. a 3PL/
    warehouse operator's own contact email printed inside the mail body). The sibling
    widening must key on the TIED message's own envelope `from_addr` (what `_release_
    stuck_siblings` actually searches by), never the payload's extracted address, or
    it would silently no-op — or worse, match the WRONG sender — whenever the two
    diverge."""
    _snapshot(pg)
    envelope_addr = "raw-envelope@somewhere.sk"
    extracted_addr = "different-contact@elsewhere.sk"
    doc = {"documents": [{
        "supplierName": "Neznáma pekáreň s.r.o.", "supplierCity": "",
        "supplierEmail": extracted_addr, "docNumber": "",
        "deliveryDate": "01.08.2026", "documentTotalWithoutVAT": 5.0,
        "items": [{"name": "Rožok 50g", "quantity": 10, "unit": "ks", "unitPrice": 0.5,
                  "totalPrice": 5.0}]}]}
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    for mid in ("tiedmsg", "siborphan"):
        _msg(pg, mid=mid, from_addr=envelope_addr, has_attachments=True)
        _attach(pg, tmp_path, mid)
        n = dl_worker.tick(
            pg, cfg,
            client=FakeClient({"dl_documents": [doc],
                               "dl_supplier": [{"matched": False,
                                                "matchReason": "nie je v zozname"}]}),
            upload=lambda *a, **k: None, post=lambda c, h: None)
        assert n == 1
    qid = pg.execute(
        "SELECT id FROM order_questions WHERE kind='dl_supplier'").fetchone()[0]
    payload = pg.execute(
        "SELECT payload FROM order_questions WHERE id=%s", (qid,)).fetchone()[0]
    assert payload["sender_email"] == extracted_addr, \
        "setup: the question is keyed on the DOCUMENT-extracted address, not the " \
        "envelope one"

    dl_supplier_memory.remember(pg, extracted_addr, SUPPLIER_EAN, "Pekáreň Lunys")
    pg.execute("UPDATE order_questions SET status='answered', answer=%s WHERE id=%s",
              (Json({"choice": SUPPLIER_EAN}), qid))
    released = dl_worker.release_for_question(
        pg, cfg, qid, client=FakeClient({"dl_documents": [doc], "dl_item": [ITEM_MATCHED]}),
        upload=lambda *a, **k: None, post=lambda c, h: None)
    assert released and released[0]["outcome"] == "ok"

    sibling = pg.execute(
        "SELECT processed FROM messages WHERE message_id='siborphan'").fetchone()
    assert sibling == (False,), \
        "the sibling (same ENVELOPE address) must be released even though the " \
        "answered question was keyed on a different, document-extracted address"


# --- Integration round B: #239 (safe automatic ORION upload retry) and #265
# --- (correction-mail detection + sibling-release widening) were built in separate
# --- worktree branches and merged into the SAME PR — this proves the composed safety
# --- property through the REAL merged code path, not through a hand-built fixture ----

def test_sibling_release_still_excludes_a_message_whose_upload_genuinely_failed_through_the_merged_retry_path(
        pg, tmp_path):
    """Composed regression for the exact interaction the integration round B dispatch
    named explicitly: #265's `_release_stuck_siblings` excludes any same-sender message
    with a logged `email_events.status='error'` row, specifically so it never re-enables
    the automatic upload retry #239 deliberately wired in (a released claim + a fresh
    per-attempt filename would let a genuinely-failed upload's retry re-ship a document
    that may already be on ORION under an earlier name).

    The existing #265 test proving this exclusion
    (`test_release_for_question_sibling_widening_never_touches_unrelated_messages`,
    `failedupload1`) manually `INSERT`s the `email_events` row — it pins the SQL
    predicate but never actually drives a real upload failure through
    `_process_document`'s upload except-block, so it cannot prove the exclusion still
    holds against #239's OWN restructuring of that block into the
    `_check_landed`/`_finish_shipped`/`_alert_and_release` closures. This test drives a
    genuine failure through the real merged pipeline instead: the initial upload AND
    #239's one safe retry both raise a transient `OSError` (`list_dirs` proves the
    document is genuinely absent from ORION, so `landed is False` and the bounded retry
    fires — see `test_a_transient_upload_failure_when_the_retry_also_fails_alerts_
    without_looping`), landing in `_alert_and_release`, the ACTUAL closure that logs
    `status='error'` post-merge. Only then is a sibling `dl_supplier` question answered
    and `_release_stuck_siblings` checked against the row that closure produced."""
    _snapshot(pg)
    sender = "compose-check@somewhere.sk"
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))

    _msg(pg, mid="failreal", from_addr=sender, has_attachments=True)
    _attach(pg, tmp_path, "failreal")
    tries = []

    def _always_timed_out_upload(c, name, content, dir_override=None):
        tries.append(name)
        raise OSError("connection timed out")

    def _fake_list_dirs(cfg):
        return _dirs()

    n = dl_worker.tick(
        pg, cfg,
        client=FakeClient({"dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                           "dl_item": [ITEM_MATCHED]}),
        upload=_always_timed_out_upload, list_dirs=_fake_list_dirs)
    assert n == 1
    assert len(tries) == 2, \
        "setup: the original attempt plus exactly one bounded retry, both failed"

    row = pg.execute(
        "SELECT processed, proc_status FROM messages WHERE message_id='failreal'"
    ).fetchone()
    assert row == (True, "review"), \
        "setup: the real upload failure (through the merged retry path) ends " \
        "terminal-review, same shape a genuine dl_supplier-orphan review ends in"
    # #239's own `_run_and_finish` also logs a SEPARATE rollup summary event with
    # `stage='review', status='review'` right after `_alert_and_release` returns (its
    # own `status="review"` comes from `_aggregate_status`, not from the failure) — so
    # the LATEST `stage='review'` row is that rollup, never `_alert_and_release`'s own
    # `status='error'` row. `_release_stuck_siblings`'s own predicate checks EXISTENCE
    # of any `status='error'` row, never "the latest one" -- mirror that here.
    assert pg.execute(
        "SELECT 1 FROM email_events WHERE message_id='failreal' AND status='error'"
    ).fetchone() is not None, \
        "setup: _alert_and_release -- #239's restructured closure -- logged the " \
        "status='error' event _release_stuck_siblings' exclusion depends on"
    assert pg.execute(
        "SELECT count(*) FROM order_questions WHERE message_id='failreal'"
    ).fetchone()[0] == 0, "setup: an upload failure never raises a question"
    assert pg.execute(
        "SELECT count(*) FROM desadv_sent WHERE uploaded_at IS NOT NULL"
    ).fetchone()[0] == 0, "setup: nothing was ever confirmed shipped"

    # The TIED message: an unmatched supplier from the SAME sender -- answering its
    # dl_supplier question is what triggers the sibling-release widening.
    doc = _unknown_supplier_doc(sender)
    _msg(pg, mid="qmsg-compose", from_addr=sender, has_attachments=True)
    _attach(pg, tmp_path, "qmsg-compose")
    n = dl_worker.tick(
        pg, cfg,
        client=FakeClient({"dl_documents": [doc],
                           "dl_supplier": [{"matched": False,
                                            "matchReason": "nie je v zozname"}]}),
        upload=lambda *a, **k: None, post=lambda c, h: None)
    assert n == 1
    qid = pg.execute(
        "SELECT id FROM order_questions WHERE kind='dl_supplier'").fetchone()[0]

    dl_supplier_memory.remember(pg, sender, SUPPLIER_EAN, "Pekáreň Lunys")
    pg.execute("UPDATE order_questions SET status='answered', answer=%s WHERE id=%s",
              (Json({"choice": SUPPLIER_EAN}), qid))
    released = dl_worker.release_for_question(
        pg, cfg, qid, client=FakeClient({"dl_documents": [doc], "dl_item": [ITEM_MATCHED]}),
        upload=lambda *a, **k: None, post=lambda c, h: None)
    assert released and released[0]["outcome"] == "ok", "the tied message ships"

    still_stuck = pg.execute(
        "SELECT processed FROM messages WHERE message_id='failreal'").fetchone()
    assert still_stuck == (True,), \
        "the genuinely-failed message -- produced by the REAL merged " \
        "_alert_and_release path, not a synthetic fixture -- must stay excluded " \
        "from the sibling release: resetting it would re-enable exactly the " \
        "automatic retry #239 wired in, via a second, independent-attempt upload " \
        "with a fresh filename (real duplicate-delivery risk in ORION)"


def test_release_stuck_siblings_never_releases_a_message_with_a_logged_upload_failure(
        pg):
    """Deep-review finding on this ticket's own PR (#265) — the PROVEN safety bug: a
    message whose ORION upload genuinely failed (`_process_document`'s upload-except
    branch logs `status='error'` and calls `desadv.release_send()`, deleting its
    claim) is otherwise INDISTINGUISHABLE from a genuine unmatched-supplier orphan by
    `processed=true AND proc_status='review' AND no order_questions row` alone.
    Resetting it here would re-enable exactly the automatic upload retry #239
    deliberately removed — a released claim + a fresh per-attempt filename means a
    second attempt can genuinely re-upload a document that already landed (two copies
    in ORION, both taken in at the next manual import)."""
    sender = "failed-upload@somewhere.sk"
    pg.execute(
        """INSERT INTO messages (message_id, category, subject, from_addr,
                                 combined_text, has_attachments, processed,
                                 proc_status)
           VALUES ('failed1', 'dodacie_listy', 'x', %s, '', false, true, 'review')""",
        (sender,))
    pg.execute(
        """INSERT INTO email_events (message_id, workflow, stage, status, outcome)
           VALUES ('failed1', 'delivery_notes', 'review', 'error',
                   'Odoslanie dodacieho listu do ORIONu zlyhalo')""")
    n = dl_worker._release_stuck_siblings(pg, "exclude-me", sender)
    assert n == 0, "a message with a logged upload failure must NEVER be reset"
    row = pg.execute(
        "SELECT processed FROM messages WHERE message_id='failed1'").fetchone()
    assert row == (True,), "stays exactly as it was — never reclaimed for a retry"


def test_release_stuck_siblings_still_releases_a_clean_orphan_alongside_a_failed_one(
        pg):
    """The exclusion proven above must be PRECISE — a genuinely clean orphan (never
    failed, just never got its own question) from the SAME sender must still be
    released even when a failed sibling exists too."""
    sender = "mixed-siblings@somewhere.sk"
    pg.execute(
        """INSERT INTO messages (message_id, category, subject, from_addr,
                                 combined_text, has_attachments, processed,
                                 proc_status)
           VALUES ('failed2', 'dodacie_listy', 'x', %s, '', false, true, 'review')""",
        (sender,))
    pg.execute(
        """INSERT INTO email_events (message_id, workflow, stage, status, outcome)
           VALUES ('failed2', 'delivery_notes', 'review', 'error', 'zlyhalo')""")
    pg.execute(
        """INSERT INTO messages (message_id, category, subject, from_addr,
                                 combined_text, has_attachments, processed,
                                 proc_status)
           VALUES ('clean1', 'dodacie_listy', 'x', %s, '', false, true, 'review')""",
        (sender,))
    n = dl_worker._release_stuck_siblings(pg, "exclude-me", sender)
    assert n == 1
    rows = pg.execute(
        "SELECT message_id, processed FROM messages WHERE from_addr=%s "
        "ORDER BY message_id", (sender,)).fetchall()
    assert rows == [("clean1", False), ("failed2", True)]


def test_release_stuck_siblings_is_bounded_so_a_large_backlog_never_storms(pg):
    """#265 gap 2: a sender with dozens of orphaned stuck messages must not have them
    ALL reset in one shot inside the request answering the nástenka question — only up
    to `_STUCK_SIBLING_LIMIT` rows get flipped per call; the rest wait for a LATER
    answered question for the same sender (or the existing `stuck_classified_sweep`/
    hourly n8n watchdog). The real reprocessing work always stays rate-limited by the
    normal `_claim()` one-message-per-tick loop regardless — this bound only caps how
    many rows get flipped per call, closing off any "storm" risk structurally."""
    sender = "veela@somewhere.sk"
    total = dl_worker._STUCK_SIBLING_LIMIT + 5
    for i in range(total):
        mid = f"bulk{i}"
        pg.execute(
            """INSERT INTO messages (message_id, category, subject, from_addr,
                                     combined_text, has_attachments, processed,
                                     proc_status)
               VALUES (%s, 'dodacie_listy', 'x', %s, '', false, true, 'review')""",
            (mid, sender))
    n = dl_worker._release_stuck_siblings(pg, "exclude-me", sender)
    assert n == dl_worker._STUCK_SIBLING_LIMIT
    reset = pg.execute(
        "SELECT count(*) FROM messages WHERE from_addr=%s AND processed=false",
        (sender,)).fetchone()[0]
    assert reset == dl_worker._STUCK_SIBLING_LIMIT
    still_stuck = pg.execute(
        "SELECT count(*) FROM messages WHERE from_addr=%s AND processed=true",
        (sender,)).fetchone()[0]
    assert still_stuck == 5, "the overflow waits for a later call, never dropped"


def test_release_stuck_siblings_does_nothing_when_sender_is_blank(pg):
    assert dl_worker._release_stuck_siblings(pg, "exclude-me", "") == 0
    assert dl_worker._release_stuck_siblings(pg, "exclude-me", "   ") == 0


# --- #312: raw exception text must never leak into the warehouse channel/board -------
#
# Channel 243 (delivery_notes_channel_id) and the /sklad-dl board are USER (warehouse)
# surfaces. A raw Python exception repr (`f"...: {e}"`, or an attachment's own `str(e)`)
# tells the skladníčka nothing and leaks internal class/path detail across the app→user
# boundary. After the fix the technical detail lives ONLY in the log + email_events.detail,
# and the warehouse sees a clean, actionable sentence. These assert the raw token is gone
# from every warehouse surface — they FAIL on the pre-fix `{e}` interpolation ([red]).

_RAW_EXC = "SecretInternalBoom KeyError AEDIEAN at dl_match.py:412"


class _ItemRaisingClient:
    """Extraction + supplier match succeed; the ITEM match call raises (dl_worker:688)."""

    def __init__(self, message):
        self.message = message
        self.last_prompt_hash = ""

    def json_call(self, system, user, schema, name="result"):
        self.last_prompt_hash = name
        if name == "dl_documents":
            return _doc()
        if name == "dl_supplier":
            return SUPPLIER_MATCHED
        raise Exception(self.message)

    def vision_call(self, *a, **kw):
        raise AssertionError


class _ExtractRaisingClient:
    """Every extraction call raises → the attachment gets an `error` (dl_worker:1146/1148)."""

    def __init__(self, message):
        self.message = message
        self.last_prompt_hash = ""

    def json_call(self, system, user, schema, name="result"):
        self.last_prompt_hash = name
        raise Exception(self.message)

    def vision_call(self, *a, **kw):
        raise Exception(self.message)


def test_supplier_match_failure_never_leaks_the_raw_exception_to_243(pg, tmp_path):
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    posted = []
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    # RaisingClient: extraction succeeds, the SUPPLIER match call raises
    n = dl_worker.tick(pg, cfg, client=RaisingClient(_RAW_EXC),
                       post=lambda c, h: posted.append(h))
    assert n == 1 and len(posted) == 1
    # the raw exception repr must be GONE from the warehouse channel
    assert _RAW_EXC not in posted[0]
    assert "SecretInternalBoom" not in posted[0] and "dl_match.py" not in posted[0]
    # the specific clean, warehouse-actionable sentence (not merely the meta label)
    assert "nepodarilo sa priradiť dodávateľa" in posted[0].lower()
    # ...but the technical detail is preserved internally in email_events.detail (never 243)
    ev = pg.execute("SELECT detail::text FROM email_events WHERE message_id='dl1' "
                    "AND status='error' ORDER BY id DESC LIMIT 1").fetchone()
    assert ev and _RAW_EXC in ev[0]


def test_item_match_failure_never_leaks_the_raw_exception_to_the_board(pg, tmp_path):
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    posted = []
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(pg, cfg, client=_ItemRaisingClient(_RAW_EXC),
                       post=lambda c, h: posted.append(h))
    assert n == 1
    # the dl_item question raised for the unmatched line must not carry the raw exception
    reason = pg.execute("SELECT reason FROM order_questions WHERE message_id='dl1' "
                        "AND kind='dl_item' ORDER BY id DESC LIMIT 1").fetchone()
    assert reason is not None
    assert _RAW_EXC not in (reason[0] or "") and "SecretInternalBoom" not in (reason[0] or "")
    # and it never leaks into the channel post either
    assert all(_RAW_EXC not in h for h in posted)


def test_attachment_extraction_error_never_leaks_the_raw_error_to_243(pg, tmp_path):
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    posted = []
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(pg, cfg, client=_ExtractRaisingClient(_RAW_EXC),
                       post=lambda c, h: posted.append(h))
    assert n == 1 and posted
    assert all(_RAW_EXC not in h and "SecretInternalBoom" not in h for h in posted)


def test_upload_failure_alert_never_leaks_the_raw_error_to_243(pg, tmp_path):
    """#312 (4th boundary site, review finding): the durable upload-failure alert
    (`_alert_and_release` → `dl_alerts.enqueue`, channel 243) must not carry the raw
    SFTP/ORION exception either — a clean sentence only, raw error to the log."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    client = FakeClient({"dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
                         "dl_item": [ITEM_MATCHED]})

    def _raise_upload(c, name, content, dir_override=None):
        raise OSError("paramiko auth failed for user granc at 10.9.9.9:22 __RAW_UP__")

    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    n = dl_worker.tick(pg, cfg, client=client, upload=_raise_upload)
    assert n == 1
    body = pg.execute(
        "SELECT body_html FROM pending_alerts WHERE kind='dl_upload_failed'").fetchone()
    assert body is not None
    # #336: the enqueued body is a clean short line (supplier + delivery note); the raw
    # exception is NEVER in it (the "nahranie zlyhalo" sentence lives in the flush header).
    assert "dodací list" in body[0]
    assert "__RAW_UP__" not in body[0] and "paramiko" not in body[0] and "10.9.9.9" not in body[0]
    # ...but preserved internally in email_events.detail (never on the channel)
    ev = pg.execute("SELECT detail::text FROM email_events WHERE message_id='dl1' "
                    "AND status='error' ORDER BY id DESC LIMIT 1").fetchone()
    assert ev and "__RAW_UP__" in ev[0]


# --- #314: non-warehouse supplier memory — stop generating questions forever ----------

def test_remembered_nonwarehouse_supplier_stops_generating_questions(pg, tmp_path):
    """#314 (RED->GREEN): once the warehouse marks a DL question 'Netyka sa skladu'
    (#307's `close_message_not_warehouse`), the SUPPLIER is remembered — a LATER mail
    from the same supplier produces ZERO new board questions and is handled terminally
    (`not_warehouse`, no upload), instead of re-asking the same non-warehouse item forever.
    Before the fix the second mail re-raised an identical dl_item question."""
    _snapshot(pg)
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    nw_item = [{"name": "Pracovny odev vzor", "quantity": 1, "unit": "ks",
                "unitPrice": 9.0, "totalPrice": 9.0}]
    nw_answer = {"gtin": "NO_MATCH", "matchConfidence": 0.0,
                 "matchReason": "ziadna zhoda — nie skladovy tovar"}

    # 1) a registered supplier whose item does NOT match the bakery catalog -> dl_item Q
    _msg(pg, mid="nw1")
    _attach(pg, tmp_path, "nw1")
    dl_worker.tick(pg, cfg, client=FakeClient({
        "dl_documents": [_doc(total=9.0, items=nw_item)],
        "dl_supplier": [SUPPLIER_MATCHED], "dl_item": [nw_answer]}),
        post=lambda c, h: None)
    qid = pg.execute("SELECT id FROM order_questions WHERE kind='dl_item' "
                     "AND status='open'").fetchone()[0]

    # 2) the warehouse marks it 'Netyka sa skladu' (#307) -> MUST remember the supplier
    dl_worker.close_message_not_warehouse(pg, qid)

    # 3) a SECOND mail from the SAME supplier, same non-warehouse item
    _msg(pg, mid="nw2")
    _attach(pg, tmp_path, "nw2")
    uploaded: list = []
    dl_worker.tick(pg, cfg, client=FakeClient({
        "dl_documents": [_doc(total=9.0, items=nw_item)],
        "dl_supplier": [SUPPLIER_MATCHED], "dl_item": [nw_answer]}),
        upload=lambda c, name, content, dir_override=None: uploaded.append(name),
        post=lambda c, h: None)

    # ZERO new questions, nothing uploaded, message handled terminally as not_warehouse
    assert pg.execute("SELECT count(*) FROM order_questions WHERE status='open'"
                      ).fetchone()[0] == 0
    assert uploaded == []
    row = pg.execute("SELECT processed, proc_status FROM messages "
                     "WHERE message_id='nw2'").fetchone()
    assert row[0] is True
    assert row[1] == "not_warehouse"


def test_remembered_nonwarehouse_supplier_with_catalog_match_still_ships(pg, tmp_path):
    """#314 req 4 (GTIN-match SAFETY OVERRIDE): a mail from a remembered non-warehouse
    supplier that DOES carry a catalog item is NEVER silently dropped — it goes through
    normal processing (builds + uploads the EDI), not the not_warehouse short-circuit. This
    is what keeps a mixed supplier (EKVIA/Messer send BOTH režíjne faktúry AND real
    deliveries) safe: only their non-warehouse mail is skipped, never a real delivery."""
    _snapshot(pg)
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    # the supplier is remembered non-warehouse (by its registry ean + name)
    dl_nonwarehouse.remember(pg, SUPPLIER_EAN, "Pekáreň Lunys", "")
    _msg(pg, mid="ov1")
    _attach(pg, tmp_path, "ov1")
    uploaded: list = []
    dl_worker.tick(pg, cfg, client=FakeClient({
        "dl_documents": [_doc()], "dl_supplier": [SUPPLIER_MATCHED],
        "dl_item": [ITEM_MATCHED]}),
        upload=lambda c, name, content, dir_override=None: uploaded.append(name),
        post=lambda c, h: None)

    # the real catalog item shipped despite the supplier being remembered non-warehouse
    assert len(uploaded) == 1
    assert pg.execute("SELECT proc_status FROM messages WHERE message_id='ov1'"
                      ).fetchone()[0] != "not_warehouse"


def test_a_non_remembered_supplier_still_asks_its_dl_item_question(pg, tmp_path):
    """#314 must not change the behaviour for a supplier that was NEVER marked
    not_warehouse: an unmatched item still raises its dl_item board question exactly as
    before (the deferred-ask refactor is behaviour-preserving)."""
    _snapshot(pg)
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    nw_item = [{"name": "Pracovny odev vzor", "quantity": 1, "unit": "ks",
                "unitPrice": 9.0, "totalPrice": 9.0}]
    _msg(pg, mid="reg1")
    _attach(pg, tmp_path, "reg1")
    dl_worker.tick(pg, cfg, client=FakeClient({
        "dl_documents": [_doc(total=9.0, items=nw_item)],
        "dl_supplier": [SUPPLIER_MATCHED],
        "dl_item": [{"gtin": "NO_MATCH", "matchConfidence": 0.0,
                     "matchReason": "ziadna zhoda"}]}),
        post=lambda c, h: None)
    assert pg.execute("SELECT count(*) FROM order_questions WHERE kind='dl_item' "
                      "AND status='open'").fetchone()[0] == 1


SUPPLIER_UNMATCHED = {"matched": False, "matchReason": "dodávateľ nenájdený v databáze"}


def test_aggregate_status_mixed_not_warehouse_and_review_is_review():
    """#314 review 🟡: a mixed message (one auto-skipped not_warehouse doc + one doc that
    needs a human) must NOT roll up to 'ok'."""
    assert dl_worker._aggregate_status(
        [{"outcome": "not_warehouse"}, {"outcome": "review"}]) == "review"
    assert dl_worker._aggregate_status([{"outcome": "not_warehouse"}]) == "not_warehouse"
    assert dl_worker._aggregate_status(
        [{"outcome": "not_warehouse"}, {"outcome": "duplicate"}]) == "not_warehouse"


def test_remembered_unmatched_supplier_with_no_catalog_match_is_skipped(pg, tmp_path):
    """#314 review 🟡 (case B coverage): an UNREGISTERED (unmatched) supplier remembered as
    non-warehouse, whose document has no catalog match, is terminally skipped — no
    dl_supplier question, no upload."""
    _snapshot(pg)
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    dl_nonwarehouse.remember(pg, "", "Pekáreň Lunys", "")   # remembered by extracted name
    nw_item = [{"name": "Pracovny odev", "quantity": 1, "unit": "ks",
                "unitPrice": 9.0, "totalPrice": 9.0}]
    _msg(pg, mid="cb1")
    _attach(pg, tmp_path, "cb1")
    uploaded: list = []
    dl_worker.tick(pg, cfg, client=FakeClient({
        "dl_documents": [_doc(total=9.0, items=nw_item)],
        "dl_supplier": [SUPPLIER_UNMATCHED],
        "dl_item": [{"gtin": "NO_MATCH", "matchConfidence": 0.0, "matchReason": "nič"}]}),
        upload=lambda c, name, content, dir_override=None: uploaded.append(name),
        post=lambda c, h: None)
    assert pg.execute("SELECT count(*) FROM order_questions WHERE status='open'"
                      ).fetchone()[0] == 0
    assert uploaded == []
    assert pg.execute("SELECT proc_status FROM messages WHERE message_id='cb1'"
                      ).fetchone()[0] == "not_warehouse"


def test_remembered_unmatched_supplier_with_a_catalog_match_still_asks(pg, tmp_path):
    """#314 review 🔴/🟡 (case B safety override): an UNREGISTERED remembered supplier whose
    document DOES carry a catalog item is NOT dropped — it still raises the dl_supplier
    question so a human can identify the supplier and ship the genuine goods."""
    _snapshot(pg)
    cfg = _cfg(delivery_notes_engine="python", data_dir=str(tmp_path))
    # #322: a genuinely UNREGISTERED supplier (not in the effective CODEX list), matching
    # this test's own docstring — so the new deterministic rung cannot resolve it and the
    # #314 safety override (a catalog-matching item still asks) is what's under test.
    dl_nonwarehouse.remember(pg, "", "Neznáma pekáreň s.r.o.", "")
    _msg(pg, mid="cb2", from_addr="neznamy@nowhere.sk")
    _attach(pg, tmp_path, "cb2")
    dl_worker.tick(pg, cfg, client=FakeClient({
        "dl_documents": [_unknown_supplier_doc("neznamy@nowhere.sk")],
        "dl_supplier": [SUPPLIER_UNMATCHED], "dl_item": [ITEM_MATCHED]}),
        post=lambda c, h: None)
    assert pg.execute("SELECT count(*) FROM order_questions WHERE kind='dl_supplier' "
                      "AND status='open'").fetchone()[0] == 1
    assert pg.execute("SELECT proc_status FROM messages WHERE message_id='cb2'"
                      ).fetchone()[0] != "not_warehouse"


# --- #322: CODEX-first supplier resolution (deterministic rung rescues a model miss) -----

def test_a_model_miss_resolves_from_an_existing_codex_card_without_a_question(pg, tmp_path):
    """#322: the supplier's card IS in the effective CODEX list (the snapshot seeds
    'Pekáreň Lunys'), but the MODEL misses (matched=False). The deterministic identity
    rung must rescue it — no dl_supplier board question, the document ships normally."""
    _snapshot(pg)
    _msg(pg, mid="dl1")
    _attach(pg, tmp_path, "dl1")
    client = FakeClient({
        "dl_documents": [_doc()],          # supplier 'Pekáreň Lunys' IS in the snapshot
        "dl_supplier": [{"matched": False, "matchReason": "nie som si istý"}],
        "dl_item": [ITEM_MATCHED]})
    uploaded, posted = [], []
    n = dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), client=client,
        upload=lambda c, name, content, dir_override=None: uploaded.append(name),
        post=lambda c, h: posted.append(h))
    assert n == 1
    assert pg.execute(
        "SELECT count(*) FROM order_questions WHERE kind='dl_supplier'").fetchone()[0] == 0, \
        "an existing CODEX card must NOT fire a dl_supplier question"
    assert len(uploaded) == 1, "the document ships — supplier resolved deterministically"
    assert pg.execute(
        "SELECT count(*) FROM desadv_sent WHERE supplier_ean=%s AND uploaded_at IS NOT NULL",
        (SUPPLIER_EAN,)).fetchone()[0] == 1


def test_a_model_miss_with_no_codex_card_still_raises_the_question(pg, tmp_path):
    """#322: model miss AND no matching CODEX card -> the dl_supplier board question still
    fires (acceptance criterion 2 — the question stays for a genuinely-unknown supplier)."""
    _snapshot(pg)
    _msg(pg, mid="dl1", from_addr="anon@nowhere.sk")
    _attach(pg, tmp_path, "dl1")
    doc = _unknown_supplier_doc("neznamy@nowhere.sk")   # supplier NOT in the snapshot
    client = FakeClient({
        "dl_documents": [doc],
        "dl_supplier": [{"matched": False, "matchReason": "nie je v zozname"}]})
    n = dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), client=client,
        post=lambda c, h: None)
    assert n == 1
    assert pg.execute(
        "SELECT count(*) FROM order_questions WHERE kind='dl_supplier'").fetchone()[0] == 1


def test_an_ambiguous_codex_name_match_still_raises_the_question(pg, tmp_path):
    """#322 safety: two effective CODEX cards share the document's supplier name (different
    EANs, no unique city, no email link) — the deterministic rung must NOT guess (a false
    supplier match ships a wrongly-addressed EDI); the dl_supplier question still fires."""
    _snapshot(pg)   # seeds 'Pekáreň Lunys' (2000000000864)
    dl_snapshot.upsert_dl_supplier(
        pg, override_id=None, orig_ean_edi=None, orig_city=None,
        ean_edi="2000000000111", name="Pekáreň Lunys", emails=[], city="Bratislava")
    dl_snapshot.dl_rebuild_from_overrides(pg)
    # a doc whose email/sender match NEITHER card, so the ambiguous NAME is what decides.
    _msg(pg, mid="dl1", from_addr="anon@nowhere.sk")
    _attach(pg, tmp_path, "dl1")
    doc = {"documents": [{
        "supplierName": "Pekáreň Lunys", "supplierCity": "", "supplierEmail": "",
        "docNumber": "0100000001", "deliveryDate": "01.08.2026",
        "documentTotalWithoutVAT": 5.0,
        "items": [{"name": "Rožok 50g", "quantity": 10, "unit": "ks", "unitPrice": 0.5,
                  "totalPrice": 5.0}]}]}
    client = FakeClient({
        "dl_documents": [doc],
        "dl_supplier": [{"matched": False, "matchReason": "nejednoznačné"}]})
    n = dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), client=client,
        post=lambda c, h: None)
    assert n == 1
    assert pg.execute(
        "SELECT count(*) FROM order_questions WHERE kind='dl_supplier'").fetchone()[0] == 1, \
        "ambiguous name (2 cards, no unique city) must still ask — never guess"


def test_release_for_supplier_card_releases_orphaned_stuck_siblings(pg, tmp_path):
    """#322 retro-release: adding/editing a CODEX supplier card releases orphaned same-
    sender stuck messages (no order_questions row) via the reused #265 machinery, without
    needing a nástenka answer."""
    _snapshot(pg)
    sender = "dodavatel@lunys.sk"
    for mid in ("orph1", "orph2"):
        _msg(pg, mid=mid, from_addr=sender)
        pg.execute("UPDATE messages SET processed=true, proc_status='review' "
                   "WHERE message_id=%s", (mid,))
    released = dl_worker.release_for_supplier_card(
        pg, _cfg(delivery_notes_engine="python"), SUPPLIER_EAN, "Pekáreň Lunys",
        [sender])
    assert released == 2
    rows = pg.execute(
        "SELECT processed, processing_at, attempts FROM messages "
        "WHERE message_id IN ('orph1','orph2')").fetchall()
    assert all(r == (False, None, 0) for r in rows), \
        "both orphaned siblings reset cleanly into the claim pool"


def test_release_for_supplier_card_honors_the_error_event_exclusion(pg, tmp_path):
    """#322: the reused #265 error-event exclusion must survive — a message whose ORION
    upload genuinely FAILED (a status='error' event) must NEVER be reset into an automatic
    retry (the #239 double-upload risk).

    NOTE (#322 review 🔵): this pins the SQL predicate via a hand-inserted email_events row.
    The producer<->consumer agreement (that a genuine upload failure really logs
    status='error' into this SAME reused `_release_stuck_siblings` path) is already covered
    end-to-end by `test_release_for_question_sibling_widening_never_touches_unrelated_messages`
    and `test_sibling_release_still_excludes_a_message_whose_upload_genuinely_failed_through_the_merged_retry_path`,
    which drive a real failure through the merged code — this card-add-triggered test only
    needs to prove the SAME predicate is honored from the new entry point."""
    _snapshot(pg)
    sender = "dodavatel@lunys.sk"
    _msg(pg, mid="orph1", from_addr=sender)
    pg.execute("UPDATE messages SET processed=true, proc_status='review' "
               "WHERE message_id='orph1'")
    _msg(pg, mid="failed1", from_addr=sender)
    pg.execute("UPDATE messages SET processed=true, proc_status='review' "
               "WHERE message_id='failed1'")
    pg.execute(
        "INSERT INTO email_events (message_id, workflow, stage, status, outcome) "
        "VALUES ('failed1','delivery_notes','review','error','ORION zlyhalo')")
    released = dl_worker.release_for_supplier_card(
        pg, _cfg(delivery_notes_engine="python"), SUPPLIER_EAN, "Pekáreň Lunys",
        [sender])
    assert released == 1
    assert pg.execute("SELECT processed FROM messages WHERE message_id='orph1'"
                      ).fetchone()[0] is False
    assert pg.execute("SELECT processed FROM messages WHERE message_id='failed1'"
                      ).fetchone()[0] is True, \
        "a genuinely-failed upload must never be reset into an auto-retry"


# --- #323: adding a CODEX card also (1) auto-closes an OPEN dl_supplier question of that
# supplier and releases its message, and (2) releases orphaned stuck siblings by an
# UNAMBIGUOUS normalized from_name for an emails=[] card. All #265 exclusions unchanged. ---

def _add_dl_supplier_card(pg, ean_edi, name, emails=None, city=""):
    dl_snapshot.upsert_dl_supplier(
        pg, override_id=None, orig_ean_edi=None, orig_city=None,
        ean_edi=ean_edi, name=name, emails=emails or [], city=city)
    dl_snapshot.dl_rebuild_from_overrides(pg)


def _stuck(pg, mid, from_addr, from_name=None):
    """A message stuck in review with no order_questions row of its own (the #265 orphan
    shape) — optionally carrying an envelope display `from_name` for the #323 name rung."""
    _msg(pg, mid=mid, from_addr=from_addr)
    pg.execute("UPDATE messages SET processed=true, proc_status='review', from_name=%s "
               "WHERE message_id=%s", (from_name, mid))


def test_release_for_supplier_card_auto_closes_a_matching_open_dl_supplier_question(
        pg, tmp_path, monkeypatch):
    """#323 residual 1: an OPEN dl_supplier question whose extracted identity the newly-added
    card UNAMBIGUOUSLY resolves (here by normalized NAME — the card carries emails=[]) is
    auto-answered through the SAME app path a human answer takes: answered_by
    'codex-card-auto', dl_supplier_memory learned, and release_for_question reprocesses the
    tied message AND releases its orphaned same-sender sibling — no nástenka click needed."""
    from app.orders import llm
    monkeypatch.setattr(llm, "from_config", lambda *a, **k: FakeClient({}))
    _snapshot(pg)
    sender = "office@duopack.sk"
    # the message that raised the still-open dl_supplier question (a skipped attachment so
    # the reprocess needs no model call — residual 1's own additions are what's under test).
    _msg(pg, mid="tied", from_addr=sender)
    _attach(pg, tmp_path, "tied", method="skipped")
    pg.execute("UPDATE messages SET processed=true, proc_status='review' "
               "WHERE message_id='tied'")
    qid = teach.ask_dl_supplier(pg, "tied", sender, candidates=[],
                                supplier_name="Duopack s.r.o.", supplier_city="Bratislava")
    assert qid is not None
    # an orphaned same-sender sibling with no question of its own (#265 shape).
    _stuck(pg, "orph", sender)

    _add_dl_supplier_card(pg, "1111111111111", "Duopack s.r.o.", emails=[], city="Bratislava")
    released = dl_worker.release_for_supplier_card(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)),
        "1111111111111", "Duopack s.r.o.", [])

    assert released == 1, "exactly one open dl_supplier question was auto-closed"
    assert pg.execute("SELECT status, answered_by FROM order_questions WHERE id=%s",
                      (qid,)).fetchone() == ("answered", "codex-card-auto")
    taught = dl_supplier_memory.resolve(pg, sender)
    assert taught is not None and taught["ean_edi"] == "1111111111111", \
        "the auto-answer went through the real app path -> dl_supplier_memory learned it"
    assert pg.execute("SELECT processed FROM messages WHERE message_id='orph'"
                      ).fetchone()[0] is False, \
        "release_for_question released the orphaned same-sender sibling by from_addr"


def test_release_for_supplier_card_leaves_an_ambiguous_supplier_question_open(pg, tmp_path):
    """#323 residual 1 safety: when the card's normalized name is shared by 2+ distinct-ean
    cards and no city breaks the tie, the deterministic rung refuses to resolve — the open
    dl_supplier question STAYS open (the system never answers for the warehouse on a guess)."""
    _snapshot(pg)
    _add_dl_supplier_card(pg, "1111111111111", "ABC obchod s.r.o.", emails=[],
                          city="Bratislava")
    _add_dl_supplier_card(pg, "2222222222222", "ABC obchod s.r.o.", emails=[], city="Košice")
    _msg(pg, mid="tied", from_addr="x@abc.sk")
    pg.execute("UPDATE messages SET processed=true, proc_status='review' "
               "WHERE message_id='tied'")
    qid = teach.ask_dl_supplier(pg, "tied", "x@abc.sk", candidates=[],
                                supplier_name="ABC obchod s.r.o.", supplier_city="")
    assert qid is not None

    released = dl_worker.release_for_supplier_card(
        pg, _cfg(delivery_notes_engine="python"), "2222222222222", "ABC obchod s.r.o.",
        [])

    assert released == 0
    assert pg.execute("SELECT status, answered_by FROM order_questions WHERE id=%s",
                      (qid,)).fetchone() == ("open", None), \
        "an ambiguous (2 cards, one name, no unique city) match must leave the question open"


def test_release_for_supplier_card_name_rung_releases_an_emails_empty_orphan(pg, tmp_path):
    """#323 residual 2: an emails=[] card releases an orphaned stuck message whose normalized
    envelope from_name matches the card's UNAMBIGUOUS normalized name — the root-cause
    HK LOAN/Duopack case where no card email exists to match from_addr. A differently-named
    orphan is left untouched."""
    _snapshot(pg)
    _add_dl_supplier_card(pg, "1111111111111", "Duopack s.r.o.", emails=[], city="Bratislava")
    _stuck(pg, "orphname", "whatever@x.sk", from_name="Duopack s.r.o.")
    _stuck(pg, "other", "y@y.sk", from_name="Iná firma s.r.o.")

    released = dl_worker.release_for_supplier_card(
        pg, _cfg(delivery_notes_engine="python"), "1111111111111", "Duopack s.r.o.",
        [])

    assert released == 1
    assert pg.execute("SELECT processed FROM messages WHERE message_id='orphname'"
                      ).fetchone()[0] is False, "matched-by-name orphan released"
    assert pg.execute("SELECT processed FROM messages WHERE message_id='other'"
                      ).fetchone()[0] is True, "a differently-named orphan is never touched"


def test_release_for_supplier_card_name_rung_honors_the_error_event_exclusion(pg, tmp_path):
    """#323 residual 2: the #265 error-event exclusion survives on the name rung too — an
    orphan whose ORION upload genuinely FAILED (a status='error' event) must NEVER be reset
    into an automatic retry (the #239 double-upload risk), even when its from_name matches."""
    _snapshot(pg)
    _add_dl_supplier_card(pg, "1111111111111", "Duopack s.r.o.", emails=[], city="Bratislava")
    _stuck(pg, "clean1", "a@x.sk", from_name="Duopack s.r.o.")
    _stuck(pg, "failed1", "b@x.sk", from_name="Duopack s.r.o.")
    pg.execute(
        "INSERT INTO email_events (message_id, workflow, stage, status, outcome) "
        "VALUES ('failed1','delivery_notes','review','error','ORION zlyhalo')")

    released = dl_worker.release_for_supplier_card(
        pg, _cfg(delivery_notes_engine="python"), "1111111111111", "Duopack s.r.o.",
        [])

    assert released == 1
    assert pg.execute("SELECT processed FROM messages WHERE message_id='clean1'"
                      ).fetchone()[0] is False
    assert pg.execute("SELECT processed FROM messages WHERE message_id='failed1'"
                      ).fetchone()[0] is True, \
        "a genuinely-failed upload must never be name-rung-reset into an auto-retry"


def test_release_for_supplier_card_release_does_not_bypass_correction_routing(pg, tmp_path):
    """#323: a released orphan that is a mail-body-sourced CORRECTION mail still lands in
    review when the worker reprocesses it — the #265 correction routing is unchanged and the
    release never causes a correction to auto-ship (no model call, nothing to ORION)."""
    _snapshot(pg)
    _add_dl_supplier_card(pg, "1111111111111", "Duopack s.r.o.", emails=[], city="Bratislava")
    _msg(pg, mid="corr1", from_addr="c@x.sk", subject="OPRAVA HMOTNOSTI",
         has_attachments=False,
         combined_text="Dobrý deň, OPRAVA HMOTNOSTI: Múka T650 = 15,88 t (nie 17,74 t). "
                       "Zvyšok bez zmien. S pozdravom, Duopack")
    pg.execute("UPDATE messages SET processed=true, proc_status='review', "
               "from_name='Duopack s.r.o.' WHERE message_id='corr1'")

    released = dl_worker.release_for_supplier_card(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)),
        "1111111111111", "Duopack s.r.o.", [])
    assert released == 1
    assert pg.execute("SELECT processed FROM messages WHERE message_id='corr1'"
                      ).fetchone()[0] is False, "the correction orphan was released by name"

    client = FakeClient({})
    n = dl_worker.tick(
        pg, _cfg(delivery_notes_engine="python", data_dir=str(tmp_path)), client=client,
        upload=lambda *a, **k: None, post=lambda c, h: None)
    assert n == 1
    assert client.calls == [], "a correction mail is routed to review BEFORE any model call"
    assert pg.execute("SELECT proc_status FROM messages WHERE message_id='corr1'"
                      ).fetchone()[0] == "review", "the reprocessed correction stays in review"
    assert pg.execute("SELECT count(*) FROM desadv_sent").fetchone()[0] == 0, \
        "a released correction mail must never ship to ORION"
