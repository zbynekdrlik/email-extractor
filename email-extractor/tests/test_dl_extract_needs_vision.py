"""#392 RED test: extract_attachment must take the vision path when needs_vision=True,
even when is_scanned() returns False (no embedded JPEG in the PDF bytes).

Pre-fix: extract_attachment ignores needs_vision entirely — the placeholder text
"[needs AI Vision: scan.pdf]" is fed to json_call as real document text, and the
model correctly returns 0 documents. The vision_call is never invoked.

Post-fix: extract_attachment detects needs_vision=True (or the placeholder pattern)
and forces the vision/render path, invoking vision_call.
"""
import pytest
from app.orders import dl_extract


class _TrackingClient:
    """Tracks which calls are made — vision_call records the call instead of raising."""
    def __init__(self, extraction_result=None):
        self.json_calls = []
        self.vision_calls = []
        self.last_prompt_hash = ""
        self._extraction = extraction_result or {"documents": []}

    def json_call(self, system, user, schema, name="result"):
        self.json_calls.append((name, user[:80]))
        self.last_prompt_hash = name
        return self._extraction

    def vision_call(self, *a, **kw):
        self.vision_calls.append(("vision", a, kw))
        return ["Dodaci list c. 126046383\nPolozky:\n1. Mlieko 1L  10 ks  0.77 EUR  7.70 EUR"]


def test_extract_attachment_forces_vision_when_needs_vision_true():
    """needs_vision=True + placeholder text + PDF without embedded JPEG ->
    must invoke vision_call, NOT feed the placeholder to json_call."""
    client = _TrackingClient({"documents": [{
        "supplierName": "DOBROTA Orava",
        "docNumber": "126046383",
        "deliveryDate": "03.09.2026",
        "documentTotalWithoutVAT": 7.70,
        "items": [{"name": "Mlieko 1L", "quantity": 10, "unit": "ks",
                   "unitPrice": 0.77, "totalPrice": 7.70}],
    }]})
    # PDF bytes with NO embedded JPEG — is_scanned() returns False
    pdf_bytes = b"%PDF-1.4 no embedded jpeg\n"
    placeholder = "[needs AI Vision: scan.pdf]"

    result = dl_extract.extract_attachment(
        client, pdf_bytes, machine_text=placeholder, needs_vision=True)

    # Post-fix: vision_call MUST have been invoked
    assert len(client.vision_calls) > 0, (
        "vision_call was never invoked — needs_vision=True was ignored")
    # The source text should NOT be the raw placeholder
    assert placeholder not in (result.get("source_text") or ""), (
        "placeholder text was used as extraction source — vision was skipped")


def test_extract_attachment_forces_vision_on_placeholder_pattern_alone():
    """Even without needs_vision=True, the placeholder pattern [needs AI Vision: ...]
    should trigger the vision path as a belt-and-suspenders fallback."""
    client = _TrackingClient({"documents": []})
    pdf_bytes = b"%PDF-1.4 no embedded jpeg\n"
    placeholder = "[needs AI Vision: dodaci_list.pdf]"

    result = dl_extract.extract_attachment(
        client, pdf_bytes, machine_text=placeholder)

    assert len(client.vision_calls) > 0, (
        "vision_call was not invoked for placeholder text pattern")
