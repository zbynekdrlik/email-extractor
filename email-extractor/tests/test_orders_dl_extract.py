"""Delivery-notes (DL) extraction — Vision + multi-document schema (#201, DL migration F2).

Fixes four structural losses in the live n8n "Dodacie Listy EDI" pipeline, all confirmed
against the binding spec (docs/superpowers/specs/2026-08-07-delivery-notes-python-design.md):

- **W1a** — every attachment of the mail is processed (`extract_email`), not just the
  first (n8n's `Get Attachment Meta ... LIMIT 1`).
- **W1b** — one attachment can yield MULTIPLE documents (`{"documents": [...]}`), not the
  single-document schema that silently dropped a second DL sharing one PDF.
- **W1c** — a multi-page scan transcribes EVERY embedded JPEG, not just the largest one.
- **W13** — a digital PDF with real extracted text never pays for a vision call at all.

No DB, no worker/claim wiring — deliberately (the worker integration is #204). Everything
here is pure functions plus a scripted fake LLM client (no network in tests).
"""
import io
import shutil

import pytest
from PIL import Image

from app.orders import dl_extract

# A JPEG SOI (\xff\xd8) ... EOI (\xff\xd9) span — real byte length, not a real image; the
# scan-detection code only ever looks at the marker bytes (mirrors the n8n heuristic).
SMALL_JPEG = b"\xff\xd8\xff\xe0" + b"x" * 100 + b"\xff\xd9"
LARGE_JPEG = b"\xff\xd8\xff\xe0" + b"x" * 25_000 + b"\xff\xd9"
LARGE_JPEG_2 = b"\xff\xd8\xff\xe0" + b"y" * 22_000 + b"\xff\xd9"


def _real_jpeg(seed: int, min_size: int = 25_000) -> bytes:
    """A genuinely decodable JPEG (#224) — noise so it doesn't compress away below the
    20 kB scan threshold, unlike a flat-color image."""
    img = Image.effect_noise((300, 300), 30 + seed).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    data = buf.getvalue()
    assert len(data) > min_size, f"fixture too small ({len(data)}), raise noise/quality"
    return data


def _real_small_jpeg(seed: int = 0) -> bytes:
    """A genuinely decodable but TINY JPEG (#224) — well under the 20 kB scan
    threshold, standing in for a supplier's letterhead logo caught by the byte scan
    alongside real garbage."""
    img = Image.effect_noise((12, 12), 30 + seed).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    data = buf.getvalue()
    assert len(data) < dl_extract.SCAN_JPEG_THRESHOLD_BYTES, f"fixture too large ({len(data)})"
    return data


def _real_pdf_with_page(seed: int = 0) -> bytes:
    """A real, poppler-renderable single-page PDF (#224) — mirrors test_extract.py's own
    `img.convert("RGB").save(buf, format="PDF")` pattern."""
    img = Image.effect_noise((400, 300), 40 + seed).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PDF")
    return buf.getvalue()


class FakeClient:
    """Scripted client: vision_call answers come off one queue, json_call off another —
    mirrors ScriptedClient in test_orders_pipeline.py, extended with vision_call."""

    last_prompt_hash = "dlprompthash"

    def __init__(self, vision_answers=(), json_answers=()):
        self.vision_answers = list(vision_answers)
        self.json_answers = list(json_answers)
        self.vision_calls = []
        self.json_calls = []

    def vision_call(self, prompt, images=None, pdf_bytes=None, n=2):
        self.vision_calls.append({"prompt": prompt, "images": images,
                                  "pdf_bytes": pdf_bytes, "n": n})
        if not self.vision_answers:
            raise AssertionError("vision_call invoked with no scripted answer left")
        return self.vision_answers.pop(0)

    def json_call(self, system, user, schema, name="result"):
        self.json_calls.append({"system": system, "user": user, "schema": schema,
                                "name": name})
        if not self.json_answers:
            raise AssertionError("json_call invoked with no scripted answer left")
        return self.json_answers.pop(0)


def _doc(**kw):
    base = {"supplierName": "Pekáreň Test s.r.o.", "supplierCity": "Martin",
            "supplierEmail": "sklad@test.sk", "docNumber": "9999999999",
            "deliveryDate": "07.08.2026", "deliveryTime": "", "documentTotalWithoutVAT": 45.60,
            "items": [{"name": "Rožok štandart 50g", "quantity": 120, "unit": "ks",
                      "unitPrice": 0.38, "totalPrice": 45.60, "vatRate": 5}]}
    base.update(kw)
    return base


# --- 1) scan detection (R40, fixes W1c) -----------------------------------

def test_no_embedded_jpeg_means_a_digital_pdf():
    assert dl_extract.extract_embedded_jpegs(b"%PDF-1.4 nothing but text streams") == []


def test_every_embedded_jpeg_is_found_not_just_the_largest():
    pdf = b"prefix" + SMALL_JPEG + b"middle" + LARGE_JPEG + b"tail"
    jpegs = dl_extract.extract_embedded_jpegs(pdf)
    assert jpegs == [SMALL_JPEG, LARGE_JPEG]


def test_a_soi_with_no_matching_eoi_is_not_a_jpeg():
    pdf = b"\xff\xd8\xff\xe0" + b"truncated, no EOI marker at all"
    assert dl_extract.extract_embedded_jpegs(pdf) == []


def test_a_document_with_no_jpeg_over_20kb_is_not_scanned():
    assert dl_extract.is_scanned([SMALL_JPEG]) is False


def test_a_document_with_no_jpeg_at_all_is_not_scanned():
    assert dl_extract.is_scanned([]) is False


def test_a_document_with_one_large_jpeg_is_scanned():
    assert dl_extract.is_scanned([SMALL_JPEG, LARGE_JPEG]) is True


def test_multi_page_scan_keeps_every_page_not_just_the_largest():
    """W1c: the classifier only needs the LARGEST page over threshold, but every page
    must still reach the vision call — extract_embedded_jpegs (not is_scanned) is what
    a caller transcribes."""
    pdf = LARGE_JPEG + LARGE_JPEG_2
    jpegs = dl_extract.extract_embedded_jpegs(pdf)
    assert len(jpegs) == 2
    assert dl_extract.is_scanned(jpegs) is True


# --- 2) text-source priority (R42, fixes W13) -----------------------------

def test_scanned_document_prefers_the_vision_transcript():
    got = dl_extract.choose_source_text(True, "machine ocr noise", "vision transcript")
    assert got == "vision transcript"


def test_scanned_document_falls_back_to_machine_text_if_vision_failed():
    got = dl_extract.choose_source_text(True, "machine ocr noise", None)
    assert got == "machine ocr noise"


def test_digital_document_prefers_the_machine_text_over_vision():
    got = dl_extract.choose_source_text(False, "real extracted text", "vision transcript")
    assert got == "real extracted text"


def test_digital_document_with_no_machine_text_falls_back_to_vision():
    got = dl_extract.choose_source_text(False, "", "vision fallback transcript")
    assert got == "vision fallback transcript"


# --- 3) cross-check combination (R43) -------------------------------------

def test_a_lone_primary_transcript_needs_no_markers():
    got = dl_extract.combine_transcripts("", "primary text", None)
    assert got == "primary text"


def test_a_differing_secondary_transcript_is_appended_and_labelled():
    got = dl_extract.combine_transcripts("", "primary text one", "totally different text two")
    assert "primary text one" in got
    assert "totally different text two" in got
    assert "DRUHY NEZAVISLY VISION PREPIS" in got


def test_an_identical_secondary_transcript_is_not_appended_twice():
    got = dl_extract.combine_transcripts("", "same head of the document", "same head of the document")
    assert got.count("same head of the document") == 1


def test_a_differing_machine_ocr_is_appended_and_labelled():
    got = dl_extract.combine_transcripts("machine ocr text, quite different", "vision primary", None)
    assert "machine ocr text, quite different" in got
    assert "ALTERNATIVNY STROJOVY OCR PREPIS" in got


def test_machine_ocr_identical_to_primary_is_not_appended():
    got = dl_extract.combine_transcripts("identical text here", "identical text here", None)
    assert got.count("identical text here") == 1


def test_empty_machine_text_is_never_appended():
    got = dl_extract.combine_transcripts("", "primary only", None)
    assert "ALTERNATIVNY" not in got


def test_a_differing_item_line_past_a_shared_80_char_header_is_still_caught():
    """Deep-review finding (Critical): the old head-comparison truncated to 80 chars,
    so two real transcripts sharing a header (supplier/city/email/docNumber/date —
    exactly what fills the first ~80 chars of a real delivery note) silently dropped
    the SECOND transcript even when it disagreed on a quantity later in the document —
    precisely the digit misread R43 exists to catch."""
    header = ("Pekáreň Test s.r.o., Martin, sklad@test.sk, DL 9999999999, "
              "dátum 07.08.2026 dodania tovaru zákazníkovi na sklad")
    primary = header + "\n1 | Rožok štandart 50g | 120 ks | 0.38 | 45.60 | 5%"
    secondary = header + "\n1 | Rožok štandart 50g | 12 ks | 0.38 | 45.60 | 5%"
    assert len(header) > 80   # the exact condition that defeated the old truncation
    got = dl_extract.combine_transcripts("", primary, secondary)
    assert "12 ks" in got
    assert "DRUHY NEZAVISLY VISION PREPIS" in got


# --- 4) date normalization (fixes W12) ------------------------------------

def test_dmy_date_passes_through_zero_padded():
    assert dl_extract.normalize_delivery_date("7.8.2026") == "07.08.2026"


def test_dmy_date_already_padded_is_unchanged():
    assert dl_extract.normalize_delivery_date("07.08.2026") == "07.08.2026"


def test_iso_date_is_converted_to_dmy():
    assert dl_extract.normalize_delivery_date("2026-08-07") == "07.08.2026"


def test_an_unparseable_date_returns_none_never_blank_silently():
    assert dl_extract.normalize_delivery_date("next tuesday") is None


def test_an_empty_date_returns_none():
    assert dl_extract.normalize_delivery_date("") is None


# --- 5) doc number LT-prefix strip (R47) ----------------------------------

def test_lt_prefix_is_stripped_keeping_only_the_digits_after_it():
    assert dl_extract.strip_lt_prefix("2610LT9999999999") == "9999999999"


def test_lowercase_lt_prefix_is_also_stripped():
    assert dl_extract.strip_lt_prefix("2610lt9999999999") == "9999999999"


def test_a_doc_number_without_lt_is_unchanged():
    assert dl_extract.strip_lt_prefix("50123") == "50123"


def test_an_empty_doc_number_stays_empty():
    assert dl_extract.strip_lt_prefix("") == ""


def test_lt_appearing_mid_word_in_a_non_matching_doc_number_is_left_alone():
    """Deep-review finding (Important): the old implementation was an UNANCHORED
    substring search for 'LT' anywhere in the string — it corrupted any doc number
    that merely contains those two letters without being the documented
    <digits>LT<digits> shape (R47's own example: '2610LT9999999999')."""
    assert dl_extract.strip_lt_prefix("SALT-2026-0001") == "SALT-2026-0001"
    assert dl_extract.strip_lt_prefix("MULTI2026") == "MULTI2026"


def test_lt_prefix_with_no_leading_digits_is_still_stripped():
    assert dl_extract.strip_lt_prefix("LT9999999999") == "9999999999"


# --- 6) quantity self-correction (R50) ------------------------------------

def test_a_misread_piece_quantity_is_corrected_from_the_line_equation():
    item = {"name": "Rožok", "quantity": 12, "unit": "ks", "unitPrice": 0.38, "totalPrice": 45.60}
    got = dl_extract.self_correct_quantity(item)
    assert got["quantity"] == 120
    assert got["_qtyOcr"] == 12


def test_a_correctly_read_quantity_is_left_alone():
    item = {"name": "Rožok", "quantity": 120, "unit": "ks", "unitPrice": 0.38, "totalPrice": 45.60}
    got = dl_extract.self_correct_quantity(item)
    assert got["quantity"] == 120
    assert "_qtyOcr" not in got


def test_a_piece_quantity_at_exactly_the_tolerance_boundary_is_not_corrected():
    # derived = round(46.75 / 0.38) = round(123.026...) = 123; |123 - 122.5| = 0.5 == tolerance
    item = {"name": "Rožok", "quantity": 122.5, "unit": "ks", "unitPrice": 0.38, "totalPrice": 46.75}
    got = dl_extract.self_correct_quantity(item)
    assert got["quantity"] == 122.5
    assert "_qtyOcr" not in got


def test_a_kg_quantity_within_tolerance_is_not_corrected():
    item = {"name": "Múka", "quantity": 2.003, "unit": "kg", "unitPrice": 1.0, "totalPrice": 2.0}
    got = dl_extract.self_correct_quantity(item)
    assert got["quantity"] == 2.003
    assert "_qtyOcr" not in got


def test_correction_is_skipped_when_the_line_equation_still_would_not_hold():
    """Unrelated unitPrice/totalPrice — a derived quantity that ALSO fails the line
    equation must never overwrite what was actually read."""
    item = {"name": "X", "quantity": 3, "unit": "ks", "unitPrice": 9.99, "totalPrice": 4.20}
    got = dl_extract.self_correct_quantity(item)
    assert got["quantity"] == 3
    assert "_qtyOcr" not in got


def test_missing_prices_leave_the_quantity_untouched():
    item = {"name": "X", "quantity": 5, "unit": "ks"}
    got = dl_extract.self_correct_quantity(item)
    assert got["quantity"] == 5
    assert "_qtyOcr" not in got


def test_a_non_numeric_price_never_raises_it_just_skips_correction():
    """Defensive: the JSON schema enforces number types on a real model answer, but a
    hand-built/malformed document must never crash validation over one bad value."""
    item = {"name": "X", "quantity": 5, "unit": "ks", "unitPrice": "not-a-number",
            "totalPrice": 45.60}
    got = dl_extract.self_correct_quantity(item)
    assert got["quantity"] == 5
    assert "_qtyOcr" not in got


# --- 7) money gate (R51) ---------------------------------------------------

def test_money_gate_passes_when_lines_sum_to_the_document_total():
    doc = _doc()
    assert dl_extract.money_gate(doc) is None


def test_money_gate_breaches_over_50_cents():
    doc = _doc(documentTotalWithoutVAT=50.00)   # lines sum 45.60, diff 4.40 > 0.50
    reason = dl_extract.money_gate(doc)
    assert reason is not None
    assert "0.50" in reason


def test_money_gate_tolerates_up_to_50_cents():
    doc = _doc(documentTotalWithoutVAT=46.00)   # diff exactly 0.40, within tolerance
    assert dl_extract.money_gate(doc) is None


def test_money_gate_passes_at_exactly_the_50_cent_boundary():
    doc = _doc(documentTotalWithoutVAT=45.60 + 0.50)   # diff exactly 0.50 -> not a breach
    assert dl_extract.money_gate(doc) is None


def test_money_gate_breaches_just_past_the_50_cent_boundary():
    doc = _doc(documentTotalWithoutVAT=45.60 + 0.51)
    assert dl_extract.money_gate(doc) is not None


def test_money_gate_is_skipped_when_no_document_total_was_read():
    doc = _doc(documentTotalWithoutVAT=0)
    assert dl_extract.money_gate(doc) is None


# --- 8) per-document validation (R50-R52) ---------------------------------

def test_a_missing_delivery_date_forces_review():
    """Deep-review finding (Important): a delivery date that never parsed is
    functionally the same downstream symptom W12 was fixed to prevent (a document
    reaching EDI/ORION with no usable date) — it must not silently pass as valid."""
    doc = _doc(deliveryDate="")
    got = dl_extract.validate_document(doc)
    assert got["status"] == "needsReview"
    assert got["reviewReason"]


def test_run_extraction_routes_an_unparseable_date_to_review():
    client = FakeClient(json_answers=[{"documents": [
        _doc(deliveryDate="not a real date")]}])
    got = dl_extract.run_extraction(client, "source text")
    assert got["documents"][0]["deliveryDate"] == ""
    assert got["documents"][0]["status"] == "needsReview"


def test_zero_items_forces_review():
    doc = _doc(items=[])
    got = dl_extract.validate_document(doc)
    assert got["status"] == "needsReview"
    assert "žiadne položky" in got["reviewReason"]


def test_a_money_gate_breach_forces_review():
    doc = _doc(documentTotalWithoutVAT=99.00)
    got = dl_extract.validate_document(doc)
    assert got["status"] == "needsReview"
    assert got["reviewReason"]


def test_a_clean_document_is_valid_and_keeps_its_items():
    doc = _doc()
    got = dl_extract.validate_document(doc)
    assert got["status"] == "valid"
    assert got["reviewReason"] == ""
    assert len(got["items"]) == 1


def test_validation_applies_quantity_self_correction_per_item():
    doc = _doc(items=[{"name": "Rožok", "quantity": 12, "unit": "ks",
                       "unitPrice": 0.38, "totalPrice": 45.60}])
    got = dl_extract.validate_document(doc)
    assert got["items"][0]["quantity"] == 120


# --- 9) LLM extraction call (multi-document schema, fixes W1b) -----------

def test_run_extraction_normalizes_dates_and_doc_numbers_per_document():
    client = FakeClient(json_answers=[{"documents": [
        _doc(docNumber="2610LT9999999999", deliveryDate="2026-08-07")]}])
    got = dl_extract.run_extraction(client, "source text")
    assert len(got["documents"]) == 1
    doc = got["documents"][0]
    assert doc["docNumber"] == "9999999999"
    assert doc["deliveryDate"] == "07.08.2026"
    assert doc["status"] == "valid"
    assert got["prompt_hash"] == "dlprompthash"


def test_run_extraction_handles_two_documents_in_one_call_fixes_w1b():
    client = FakeClient(json_answers=[{"documents": [
        _doc(docNumber="1111111111"), _doc(docNumber="2222222222")]}])
    got = dl_extract.run_extraction(client, "source text with two delivery notes")
    assert [d["docNumber"] for d in got["documents"]] == ["1111111111", "2222222222"]


def test_run_extraction_with_zero_documents_returns_an_empty_list():
    client = FakeClient(json_answers=[{"documents": []}])
    got = dl_extract.run_extraction(client, "no delivery note here")
    assert got["documents"] == []


# --- 10) one attachment end-to-end (vision routing) -----------------------

def test_a_scanned_attachment_calls_vision_with_every_page():
    # Real, genuinely decodable JPEGs (#224) — the two pages must both reach vision
    # unchanged when local extraction actually found valid images (W1c preserved).
    page1, page2 = _real_jpeg(1), _real_jpeg(2)
    pdf = page1 + page2
    client = FakeClient(
        vision_answers=[["primary transcript", "secondary transcript"]],
        json_answers=[{"documents": [_doc()]}])
    result = dl_extract.extract_attachment(client, pdf, machine_text="")
    assert result["scanned"] is True
    assert result["vision_used"] is True
    assert len(client.vision_calls) == 1
    assert client.vision_calls[0]["images"] == [page1, page2]
    assert client.vision_calls[0]["pdf_bytes"] is None
    assert len(result["documents"]) == 1


# --- #224: a marker-only "JPEG" that isn't decodable must never reach vision ------

def test_marker_only_candidates_are_not_decodable_real_images():
    """Documents the actual production bug (#224): a byte span that satisfies the
    SOI/EOI marker scan is NOT necessarily a real image — real supplier scans wrap the
    JPEG in an extra FlateDecode layer (`/Filter [/FlateDecode /DCTDecode]`), so the
    raw bytes `extract_embedded_jpegs` finds are garbage, not decodable pixels."""
    assert dl_extract._is_real_image(LARGE_JPEG) is False
    assert dl_extract._is_real_image(_real_jpeg(9)) is True


def test_undecodable_scan_candidates_fall_back_to_rendering_real_pages_fixes_224(monkeypatch):
    pdf = LARGE_JPEG + LARGE_JPEG_2   # marker-only garbage, mirrors the real bug
    rendered_page = _real_jpeg(3)
    monkeypatch.setattr(dl_extract, "render_pdf_pages", lambda pdf_bytes: [rendered_page])
    client = FakeClient(
        vision_answers=[["primary transcript", "secondary transcript"]],
        json_answers=[{"documents": [_doc()]}])
    result = dl_extract.extract_attachment(client, pdf, machine_text="")
    assert result["scanned"] is True
    assert result["vision_used"] is True
    assert client.vision_calls[0]["images"] == [rendered_page]
    assert client.vision_calls[0]["images"] != [LARGE_JPEG, LARGE_JPEG_2]
    assert client.vision_calls[0]["pdf_bytes"] is None


def test_undecodable_scan_with_failed_rendering_falls_back_to_whole_pdf_fixes_224(monkeypatch):
    pdf = LARGE_JPEG   # marker-only garbage, and rendering ALSO fails (corrupt PDF)
    monkeypatch.setattr(dl_extract, "render_pdf_pages", lambda pdf_bytes: [])
    client = FakeClient(
        vision_answers=[["fallback transcript"]],
        json_answers=[{"documents": [_doc()]}])
    result = dl_extract.extract_attachment(client, pdf, machine_text="")
    assert result["scanned"] is True
    assert result["vision_used"] is True
    assert client.vision_calls[0]["images"] is None
    assert client.vision_calls[0]["pdf_bytes"] == pdf


def test_a_small_valid_logo_alone_is_not_enough_evidence_of_real_page_content():
    """A genuinely decodable but SMALL image (e.g. a supplier's letterhead logo caught
    by the byte scan alongside garbage) must not be mistaken for the real scanned page
    — only a candidate that is BOTH decodable AND over the scan-size threshold counts."""
    small_real = _real_small_jpeg(4)
    assert len(small_real) < dl_extract.SCAN_JPEG_THRESHOLD_BYTES
    assert dl_extract._decodable_large_jpegs([small_real, LARGE_JPEG]) == []


def test_render_pdf_pages_returns_valid_jpeg_bytes_for_a_real_pdf():
    # poppler (pdftoppm) is a hard requirement of this test — the Dockerfile and CI's
    # `test` job both install it unconditionally, same as app/extract.py's own OCR path
    # already relies on.
    assert shutil.which("pdftoppm"), "poppler (pdftoppm) is required, not optional"
    pdf = _real_pdf_with_page()
    pages = dl_extract.render_pdf_pages(pdf)
    assert len(pages) == 1
    img = Image.open(io.BytesIO(pages[0]))
    img.load()
    assert img.format == "JPEG"


def test_render_pdf_pages_never_raises_on_unrenderable_bytes():
    assert dl_extract.render_pdf_pages(b"not a pdf at all") == []
    assert dl_extract.render_pdf_pages(b"") == []


def test_a_digital_attachment_with_real_text_never_calls_vision_fixes_w13():
    pdf = b"%PDF-1.4 all text, no embedded images"
    client = FakeClient(json_answers=[{"documents": [_doc()]}])
    result = dl_extract.extract_attachment(client, pdf, machine_text="real extracted text here")
    assert result["scanned"] is False
    assert result["vision_used"] is False
    assert client.vision_calls == []
    assert "real extracted text here" in client.json_calls[0]["user"]


def test_a_digital_attachment_with_no_text_falls_back_to_vision_on_the_whole_pdf():
    pdf = b"%PDF-1.4 empty extraction, no embedded jpegs"
    client = FakeClient(
        vision_answers=[["fallback transcript"]],
        json_answers=[{"documents": [_doc()]}])
    result = dl_extract.extract_attachment(client, pdf, machine_text="")
    assert result["scanned"] is False
    assert result["vision_used"] is True
    assert client.vision_calls[0]["pdf_bytes"] == pdf
    assert client.vision_calls[0]["images"] is None


def test_extract_attachment_cross_checks_vision_secondary_against_machine_ocr():
    pdf = LARGE_JPEG
    client = FakeClient(
        vision_answers=[["vision primary transcript", "vision SECOND sample, differs"]],
        json_answers=[{"documents": [_doc()]}])
    result = dl_extract.extract_attachment(client, pdf, machine_text="raw machine ocr, also differs")
    source = result["source_text"]
    assert "vision primary transcript" in source
    assert "vision SECOND sample, differs" in source
    assert "raw machine ocr, also differs" in source


# --- 11) every attachment of the mail (fixes W1a) --------------------------

def test_every_attachment_is_processed_not_just_the_first():
    client = FakeClient(json_answers=[
        {"documents": [_doc(docNumber="1111111111")]},
        {"documents": [_doc(docNumber="2222222222")]},
    ])
    attachments = [
        {"idx": 0, "filename": "dl1.pdf", "pdf_bytes": b"%PDF digital one", "machine_text": "text one"},
        {"idx": 1, "filename": "dl2.pdf", "pdf_bytes": b"%PDF digital two", "machine_text": "text two"},
    ]
    result = dl_extract.extract_email(client, attachments)
    assert [d["docNumber"] for d in result["documents"]] == ["1111111111", "2222222222"]
    assert client.vision_calls == []   # both digital with real text — no vision cost
    assert len(result["attachments"]) == 2


def test_a_second_dl_hiding_in_the_same_attachment_is_kept_fixes_w1b():
    client = FakeClient(json_answers=[{"documents": [
        _doc(docNumber="1111111111"), _doc(docNumber="2222222222")]}])
    attachments = [{"idx": 0, "filename": "two_dls.pdf",
                    "pdf_bytes": b"%PDF one file, two documents", "machine_text": "both DLs' text"}]
    result = dl_extract.extract_email(client, attachments)
    assert [d["docNumber"] for d in result["documents"]] == ["1111111111", "2222222222"]
    assert all(d["source_attachment_filename"] == "two_dls.pdf" for d in result["documents"])


def test_documents_are_tagged_with_their_source_attachment():
    client = FakeClient(json_answers=[
        {"documents": [_doc(docNumber="1111111111")]},
        {"documents": [_doc(docNumber="2222222222")]},
    ])
    attachments = [
        {"idx": 0, "filename": "a.pdf", "pdf_bytes": b"%PDF a", "machine_text": "text a"},
        {"idx": 1, "filename": "b.pdf", "pdf_bytes": b"%PDF b", "machine_text": "text b"},
    ]
    result = dl_extract.extract_email(client, attachments)
    assert result["documents"][0]["source_attachment_idx"] == 0
    assert result["documents"][1]["source_attachment_idx"] == 1


def test_no_attachments_means_no_documents():
    client = FakeClient()
    result = dl_extract.extract_email(client, [])
    assert result == {"documents": [], "attachments": []}


class _RaisingClient(FakeClient):
    """Fails on the FIRST attachment it is asked to extract from, then behaves
    normally for the rest — simulates one truly-broken/empty attachment or a
    transient API failure on ONE document out of several."""

    def json_call(self, system, user, schema, name="result"):
        if "boom" in user:
            raise RuntimeError("simulated failure on this one attachment")
        return super().json_call(system, user, schema, name)


def test_one_bad_attachment_does_not_lose_the_others_documents():
    """Deep-review finding (Important): a truly empty attachment, or any transport/
    API failure, must not abort every remaining attachment of the same mail."""
    client = _RaisingClient(json_answers=[
        {"documents": [_doc(docNumber="1111111111")]},
        {"documents": [_doc(docNumber="3333333333")]},
    ])
    attachments = [
        {"idx": 0, "filename": "good1.pdf", "pdf_bytes": b"%PDF good", "machine_text": "text one"},
        {"idx": 1, "filename": "bad.pdf", "pdf_bytes": b"%PDF boom", "machine_text": "boom"},
        {"idx": 2, "filename": "good2.pdf", "pdf_bytes": b"%PDF good", "machine_text": "text three"},
    ]
    result = dl_extract.extract_email(client, attachments)
    assert [d["docNumber"] for d in result["documents"]] == ["1111111111", "3333333333"]
    assert len(result["attachments"]) == 3
    failed = [a for a in result["attachments"] if a["idx"] == 1][0]
    assert failed["error"]
    ok = [a for a in result["attachments"] if a["idx"] in (0, 2)]
    assert all(a["error"] is None for a in ok)


# --- 12) prompts exist and are non-empty ------------------------------------

def test_extraction_prompt_is_non_empty_slovak_text():
    text = dl_extract.extract_prompt()
    assert len(text) > 100
    assert "documents" in text or "dodac" in text.lower()


def test_vision_prompt_is_non_empty():
    text = dl_extract.vision_prompt()
    assert len(text) > 50


def test_json_call_receives_the_multi_document_schema():
    client = FakeClient(json_answers=[{"documents": []}])
    dl_extract.run_extraction(client, "text")
    schema = client.json_calls[0]["schema"]
    assert schema["properties"]["documents"]["type"] == "array"


@pytest.mark.parametrize("bad", [None, "", b""])
def test_extract_embedded_jpegs_never_raises_on_falsy_input(bad):
    assert dl_extract.extract_embedded_jpegs(bad) == []
