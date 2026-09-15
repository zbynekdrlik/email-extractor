"""#437: CMR (medzinarodny nakladny list) zo skenera spracovany ako dodaci list.

Vsetky fixture su SYNTETICKE (polsky-styl CMR) — nikdy realne zakaznicke data
(tento repozitar je verejny). Tri roviny:

- `dl_extract._looks_like_cmr` detektor (deterministicky, z prepisaneho textu),
- `dl_extract.extract_prompt(cmr_mode=True)` prompt varianta + vyber v `extract_attachment`,
- e2e: CMR sprava cez `dl_worker.tick` -> 1 dokument, polozka sparovana s kg-kartou,
  mnozstvo 1000 kg, cena z katalogu (R85), DESADV nahraty, Odoo sprava s "(z CMR)".
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from psycopg.types.json import Json

from app import store
from app.config import Config
from app.orders import dl_extract, dl_snapshot, dl_worker

# A CMR transcript WITHOUT the literal "CMR" abbreviation — a real scan can print only
# "MEDZINÁRODNÝ NÁKLADNÝ LIST" / "LIST PRZEWOZOWY". `_looks_like_cmr` alone MISSES this
# (it requires the token); only the classifier's forced verdict recovers it (#437 F1).
_CMR_TEXT_NO_TOKEN = (
    "MIEDZYNARODOWY SAMOCHODOWY LIST PRZEWOZOWY\n"
    "1. Nadawca / Odosielatel: Mlyn Kapka Sp. z o.o., Jaroslaw\n"
    "2. Odbiorca / Prijemca: DUOPACK SLOVAKIA s.r.o.\n"
    "9. Oznaczenie towaru: Muka pszenna typ 500\n"
    "12. Waga netto: 1000 kg\n24. Zasielku prevzal: 15.09.2026\n"
)

# A synthetic Polish-style CMR transcript: the standard "CMR" token + international
# consignment-note markers no ordinary Slovak delivery note carries.
_CMR_TEXT = (
    "MIEDZYNARODOWY SAMOCHODOWY LIST PRZEWOZOWY\nCMR\n"
    "1. Nadawca / Odosielatel: Mlyn Kapka Sp. z o.o., Jaroslaw\n"
    "2. Odbiorca / Prijemca: DUOPACK SLOVAKIA s.r.o.\n"
    "4. Miejsce zaladowania: Jaroslaw, 14.09.2026\n"
    "9. Oznaczenie towaru: Muka pszenna typ 500\n"
    "11. Waga brutto / 12. Waga netto: 1000 kg\n"
    "24. Przesylke otrzymano / Zasielku prevzal: 15.09.2026\n"
)

# A normal Slovak delivery note — must NEVER be misdetected as a CMR.
_NORMAL_DL_TEXT = (
    "Dodaci list c. 0100000123\nDodavatel: Pekaren Lunys, Presov\n"
    "Rozok 50g   10 ks   0,50   5,00\nSpolu bez DPH: 5,00\n"
)

_DELIV = (datetime.now(UTC) - timedelta(days=1)).strftime("%d.%m.%Y")

DL_CATALOG_CSV = ("GTIN,Názov,doplnok,hmotnost,Sklad,Cena\n"
                  "8588000001557,Muka psenicna typ 500,,,100,0.368\n")
OBJ_CATALOG_CSV = "GTIN,Sklad,Názov,doplnok\n"
SUPPLIERS_CSV = ("Názov organizácie,EAN kód EDI,Obec,Ulica,Meno pre fakturáciu,"
                 "Číslo mobilu,E-mail\n"
                 "DUOPACK SLOVAKIA,2000000000655,Granc-Petrovce,,,,gnip@hkloan.eu\n")

DUOPACK_EAN = "2000000000655"
FLOUR_GTIN = "8588000001557"
SCANNER = "tlaciaren@slovnormal.sk"


def _cfg(**kw):
    base = dict(pg_dsn=os.environ.get("PG_TEST_DSN", ""), data_dir="/tmp",
                delivery_notes_engine="python", delivery_notes_shadow=False,
                delivery_notes_scanner_senders=SCANNER)
    base.update(kw)
    return Config(**base)


class _CapturingClient:
    """Scripts json_call answers by `name` AND captures the SYSTEM prompt each got —
    so a test can prove WHICH extraction prompt (base vs CMR) was actually used."""

    def __init__(self, answers):
        self._answers = {k: list(v) for k, v in answers.items()}
        self.systems: dict[str, str] = {}
        self.last_prompt_hash = ""

    def json_call(self, system, user, schema, name="result"):
        self.systems[name] = system
        self.last_prompt_hash = name
        queue = self._answers.get(name)
        if not queue:
            raise AssertionError(f"no scripted answer left for {name!r}")
        return queue.pop(0)

    def vision_call(self, *a, **kw):
        raise AssertionError("vision must not be called when machine_text is present (W13)")


# --- 1) the CMR text detector ------------------------------------------------

def test_looks_like_cmr_detects_a_cmr_and_ignores_a_normal_delivery_note():
    assert dl_extract._looks_like_cmr(_CMR_TEXT) is True
    assert dl_extract._looks_like_cmr(_NORMAL_DL_TEXT) is False
    assert dl_extract._looks_like_cmr("") is False
    # the bare token "CMR" alone (no consignment-note phrase) is NOT enough — a normal
    # DL might mention it in passing; a genuine CMR carries the transport form structure.
    assert dl_extract._looks_like_cmr("Poznamka: prilozene CMR") is False


# --- 2) the CMR extraction prompt variant ------------------------------------

def test_extract_prompt_has_a_distinct_cmr_variant():
    base = dl_extract.extract_prompt()
    cmr = dl_extract.extract_prompt(cmr_mode=True)
    assert cmr != base
    low = cmr.lower()
    assert "cmr" in low
    assert "kol. 2" in low           # supplier = consignee (kol. 2) rule present
    assert "netto" in low            # NETTO kg rule present


def test_extract_attachment_selects_the_cmr_prompt_on_cmr_text():
    doc = {"supplierName": "DUOPACK SLOVAKIA", "supplierCity": "Granc-Petrovce",
           "supplierEmail": "", "docNumber": "", "deliveryDate": _DELIV,
           "items": [{"name": "Muka psenicna typ 500", "quantity": 1000, "unit": "kg"}]}
    client = _CapturingClient({"dl_documents": [{"documents": [doc]}]})
    result = dl_extract.extract_attachment(client, b"%PDF-1.4 no jpeg\n",
                                           machine_text=_CMR_TEXT)
    assert client.systems["dl_documents"] == dl_extract.extract_prompt(cmr_mode=True)
    assert len(result["documents"]) == 1


def test_extract_attachment_keeps_the_base_prompt_on_a_normal_delivery_note():
    client = _CapturingClient({"dl_documents": [{"documents": []}]})
    dl_extract.extract_attachment(client, b"%PDF-1.4 no jpeg\n",
                                  machine_text=_NORMAL_DL_TEXT)
    assert client.systems["dl_documents"] == dl_extract.extract_prompt()


def test_extract_attachment_tags_a_cmr_document_with_source_kind():
    doc = {"supplierName": "DUOPACK SLOVAKIA", "supplierCity": "", "supplierEmail": "",
           "docNumber": "", "deliveryDate": _DELIV,
           "items": [{"name": "Muka psenicna typ 500", "quantity": 1000, "unit": "kg"}]}
    client = _CapturingClient({"dl_documents": [{"documents": [doc]}]})
    email = dl_extract.extract_email(
        client, [{"idx": 0, "filename": "cmr.pdf", "pdf_bytes": b"%PDF-1.4 no jpeg\n",
                  "machine_text": _CMR_TEXT}])
    assert len(email["documents"]) == 1
    assert email["documents"][0].get("source_kind") == "cmr"


# --- 3) e2e: a CMR message ships a DESADV with kg from the catalog price ------

def _snapshot(pg):
    return dl_snapshot.import_snapshot(pg, DL_CATALOG_CSV, OBJ_CATALOG_CSV, SUPPLIERS_CSV)


def _cmr_msg(pg, tmp_path, mid="cmrmsg", text=_CMR_TEXT):
    pg.execute(
        """INSERT INTO messages (message_id, category, subject, from_addr,
                                 combined_text, has_attachments, processed)
           VALUES (%s, 'dodacie_listy', 'sken', %s, '', true, false)""", (mid, SCANNER))
    pg.execute(
        """INSERT INTO attachments (message_id, idx, filename, mime, extracted_text, method)
           VALUES (%s, 0, 'cmr.pdf', 'application/pdf', %s, 'ocr')""", (mid, text))
    d = store.message_dir(str(tmp_path), mid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "att0__cmr.pdf").write_bytes(b"%PDF-1.4 no embedded jpeg here\n")
    return mid


def _cmr_doc():
    return {"documents": [{
        "supplierName": "DUOPACK SLOVAKIA", "supplierCity": "Granc-Petrovce",
        "supplierEmail": "", "docNumber": "", "deliveryDate": _DELIV,
        "documentTotalWithoutVAT": 0,
        "items": [{"name": "Muka psenicna typ 500", "quantity": 1000, "unit": "kg"}]}]}


def test_cmr_message_ships_a_desadv_with_kg_and_catalog_price(pg, tmp_path):
    _snapshot(pg)
    _cmr_msg(pg, tmp_path)
    client = _CapturingClient({
        "dl_documents": [_cmr_doc()],
        "dl_supplier": [{"matched": True, "ean_edi": DUOPACK_EAN,
                         "name": "DUOPACK SLOVAKIA", "matchConfidence": 0.96,
                         "matchReason": "presna zhoda mena"}],
        "dl_item": [{"gtin": FLOUR_GTIN, "matchedCatalogName": "Muka psenicna typ 500",
                     "matchConfidence": 0.97, "matchReason": "presna zhoda", "mass": 0}]})
    uploaded, posted = [], []
    cfg = _cfg(data_dir=str(tmp_path))
    n = dl_worker.tick(
        pg, cfg, client=client,
        upload=lambda c, name, content, dir_override=None: uploaded.append((name, content)),
        post=lambda c, h: posted.append(h))
    assert n == 1
    # the CMR extraction prompt was used (not the base one)
    assert client.systems["dl_documents"] == dl_extract.extract_prompt(cmr_mode=True)
    # exactly one DESADV uploaded to ORION
    assert len(uploaded) == 1, uploaded
    name, content = uploaded[0]
    assert name.startswith("Z-DESADV_")
    # R84: kg-tracked card + unit kg -> quantity ships as 1000 kg
    assert "1000.000" in content
    # R85: no printed price -> catalog cena 0.368 EUR/kg substituted
    assert "0.368" in content
    # the Odoo (243) success message carries the "(z CMR)" marker
    assert len(posted) == 1
    assert "(z CMR)" in posted[0]
    row = pg.execute(
        "SELECT processed, processed_by, proc_status FROM messages "
        "WHERE message_id='cmrmsg'").fetchone()
    assert row[0] is True and row[1] == "dodacie_listy"
    assert row[2] in ("ok", "partial")


def test_non_cmr_dl_still_ships_without_the_cmr_marker(pg, tmp_path):
    """Guard: a normal delivery note (no CMR text) still uses the BASE prompt and its
    Odoo message never carries the CMR marker — the CMR path is additive, not a
    behaviour change for ordinary DLs."""
    _snapshot(pg)
    pg.execute(
        """INSERT INTO messages (message_id, category, subject, from_addr,
                                 combined_text, has_attachments, processed)
           VALUES ('dl_ok', 'dodacie_listy', 'DL', %s, '', true, false)""", (SCANNER,))
    pg.execute(
        """INSERT INTO attachments (message_id, idx, filename, mime, extracted_text, method)
           VALUES ('dl_ok', 0, 'dl.pdf', 'application/pdf', %s, 'ocr')""", (_NORMAL_DL_TEXT,))
    d = store.message_dir(str(tmp_path), "dl_ok")
    d.mkdir(parents=True, exist_ok=True)
    (d / "att0__dl.pdf").write_bytes(b"%PDF-1.4 no embedded jpeg here\n")
    doc = {"documents": [{
        "supplierName": "DUOPACK SLOVAKIA", "supplierCity": "Granc-Petrovce",
        "supplierEmail": "", "docNumber": "0100000123", "deliveryDate": _DELIV,
        "documentTotalWithoutVAT": 368.0,
        "items": [{"name": "Muka psenicna typ 500", "quantity": 1000, "unit": "kg",
                   "unitPrice": 0.368, "totalPrice": 368.0}]}]}
    client = _CapturingClient({
        "dl_documents": [doc],
        "dl_supplier": [{"matched": True, "ean_edi": DUOPACK_EAN,
                         "name": "DUOPACK SLOVAKIA", "matchConfidence": 0.96,
                         "matchReason": "x"}],
        "dl_item": [{"gtin": FLOUR_GTIN, "matchedCatalogName": "Muka psenicna typ 500",
                     "matchConfidence": 0.97, "matchReason": "x", "mass": 0}]})
    posted = []
    n = dl_worker.tick(
        pg, _cfg(data_dir=str(tmp_path)), client=client,
        upload=lambda c, name, content, dir_override=None: None,
        post=lambda c, h: posted.append(h))
    assert n == 1
    assert client.systems["dl_documents"] == dl_extract.extract_prompt()
    assert len(posted) == 1
    assert "(z CMR)" not in posted[0]


# --- review finding 1: honour the classifier's CMR verdict (forced, not re-guessed) ------

def test_a_rescued_cmr_forces_the_cmr_prompt_even_without_the_literal_token(pg, tmp_path):
    """#437 review finding 1: when human_processing rescued a scan as a CMR (a durable
    `rescued` event with `verdict_category='cmr'`), the DL engine FORCES the CMR extraction
    variant from that verdict — even when the transcript itself lacks the literal 'CMR'
    token, so `_looks_like_cmr` alone would miss it and (money gate skipped) a base-prompt
    misread of the supplier/quantity could ship a wrong EDI. RED before the fix: the base
    prompt is used because auto-detection fails on `_CMR_TEXT_NO_TOKEN`."""
    assert dl_extract._looks_like_cmr(_CMR_TEXT_NO_TOKEN) is False   # auto-detect misses it
    _snapshot(pg)
    mid = _cmr_msg(pg, tmp_path, mid="cmrnotok", text=_CMR_TEXT_NO_TOKEN)
    # simulate the human_processing rescue's durable verdict event
    pg.execute(
        "INSERT INTO email_events (message_id, workflow, stage, status, outcome, detail, "
        "rollup) VALUES (%s, 'human_processing', 'rescued', 'ok', 'rescued', %s, false)",
        (mid, Json({"to": "dodacie_listy", "verdict_category": "cmr"})))
    client = _CapturingClient({
        "dl_documents": [_cmr_doc()],
        "dl_supplier": [{"matched": True, "ean_edi": DUOPACK_EAN,
                         "name": "DUOPACK SLOVAKIA", "matchConfidence": 0.96,
                         "matchReason": "x"}],
        "dl_item": [{"gtin": FLOUR_GTIN, "matchedCatalogName": "Muka psenicna typ 500",
                     "matchConfidence": 0.97, "matchReason": "x", "mass": 0}]})
    uploaded, posted = [], []
    n = dl_worker.tick(
        pg, _cfg(data_dir=str(tmp_path)), client=client,
        upload=lambda c, name, content, dir_override=None: uploaded.append(name),
        post=lambda c, h: posted.append(h))
    assert n == 1
    # the classifier's verdict forced the CMR prompt despite the missing token
    assert client.systems["dl_documents"] == dl_extract.extract_prompt(cmr_mode=True)
    assert len(uploaded) == 1
    assert len(posted) == 1 and "(z CMR)" in posted[0]


def test_extract_attachment_detects_cmr_from_the_vision_transcript(pg, tmp_path):
    """#437 review finding 2: a genuinely SCANNED CMR (needs_vision → vision transcript,
    no machine OCR) still selects the CMR prompt via auto-detection on the transcribed
    text — the real scanned-CMR path, distinct from the digital-text W13 path the other
    e2e uses."""
    class _VisionClient(_CapturingClient):
        def vision_call(self, *a, **kw):
            return [_CMR_TEXT]     # the vision transcript itself is a CMR

    doc = {"supplierName": "DUOPACK SLOVAKIA", "supplierCity": "", "supplierEmail": "",
           "docNumber": "", "deliveryDate": _DELIV,
           "items": [{"name": "Muka psenicna typ 500", "quantity": 1000, "unit": "kg"}]}
    client = _VisionClient({"dl_documents": [{"documents": [doc]}]})
    result = dl_extract.extract_attachment(client, b"%PDF-1.4 no jpeg\n", machine_text="",
                                           needs_vision=True)
    assert result["vision_used"] is True
    assert client.systems["dl_documents"] == dl_extract.extract_prompt(cmr_mode=True)
    assert result.get("cmr_used") is True
