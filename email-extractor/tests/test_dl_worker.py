"""DL worker (#204, DL migration F5) — worker loop, shadow mode, announced-vs-attached
(spec §4), duplicate visibility (W7), R17/W9 retry semantics. All fixtures below are
SYNTHETIC — constructed to match the documented template shapes, never real customer
mail (this repo is public).
"""
from __future__ import annotations

import pytest

from app import store
from app.config import Config
from app.orders import desadv, dl_snapshot, dl_worker, reliability

DL_CATALOG_CSV = ("GTIN,Názov,doplnok,hmotnost,Sklad,Cena\n"
                  "8588000000001,Rožok 50g,,0.05,1,0.50\n")
OBJ_CATALOG_CSV = "GTIN,Sklad,Názov,doplnok\n"
SUPPLIERS_CSV = ("Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,"
                 "Číslo mobilu,E-mail\n"
                 "Pekáreň Lunys,2000000000864,Prešov,Košútka 1,,,dodavatel@lunys.sk\n")

SUPPLIER_EAN = "2000000000864"
ITEM_GTIN = "8588000000001"


def _cfg(**kw):
    base = dict(pg_dsn="", data_dir="/tmp", delivery_notes_engine="n8n",
                delivery_notes_shadow=False)
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
           mime="application/pdf"):
    pg.execute(
        """INSERT INTO attachments (message_id, idx, filename, mime, extracted_text)
           VALUES (%s, %s, %s, %s, %s)""", (mid, idx, filename, mime, text))
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
    assert "Dodací list spracovaný" in posted[0]
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
    assert not any("Dodací list spracovaný" in h for h in posted), \
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
    success = [h for h in posted if "Dodací list spracovaný" in h][0]
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

    mismatch = [h for h in posted if "ohlásil viac dodacích listov" in h]
    assert mismatch, "the announced-but-unattached DL number must be flagged"
    assert "0100000002" in mismatch[0]
    ev = pg.execute(
        "SELECT detail FROM email_events WHERE message_id='dl1' "
        "AND stage='announced_mismatch'").fetchone()
    assert ev is not None
    assert ev[0]["announced"] == ["0100000002"]


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


# --- refresh_due (mirrors worker.refresh_due) --------------------------------

def test_refresh_due_returns_none_when_no_snapshot_exists_yet(pg):
    assert dl_worker.refresh_due(pg, _cfg()) is None


def test_refresh_due_never_touches_the_network_even_when_configured(pg, monkeypatch):
    """#129: the DL catalog/supplier sheet is permanently disabled too — it must never
    be fetched again, even when catalog_sheet_id/gids are still populated (as they are
    on the live add-on today, dl_catalog_gid included) and the frozen dl_snapshot is
    old. dl_worker.py must not break the LIVE delivery_notes_shadow window (#205) — it
    now just reports whatever dl_snapshot is currently frozen."""
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("must never fetch the DL sheet (#129)"))
    sid = _snapshot(pg)
    cfg = _cfg(catalog_sheet_id="doc", dl_catalog_gid="1", catalog_gid="2",
              customer_gid="3")
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
    success = [h for h in posted if "Dodací list spracovaný" in h][0]
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
