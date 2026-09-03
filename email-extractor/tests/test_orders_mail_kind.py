"""#376: the is-it-an-order? classifier + the deterministic safe-discard vetoes.

Pure-function tests (no DB, no pipeline) — the pipeline integration lives in
`test_orders_pipeline.py`. The load-bearing part here is F.12: every Slovak diacritic
inflection of a document-identifier stem is probed against the REAL veto, because a
plain-ASCII regex stem can only match `č/ľ/ĺ/ň/í` forms if the text is folded first
(the #265 lesson).
"""
import pytest

from app.orders import dl_match, mail_kind


class FakeClient:
    last_prompt_hash = "p"

    def __init__(self, answer):
        self.answer = answer
        self.asked = []

    def json_call(self, system, user, schema, name="result"):
        self.asked.append(name)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


# --- classify -------------------------------------------------------------

def test_classify_reads_a_clean_other_verdict():
    v = mail_kind.classify(
        FakeClient({"kind": "other", "confidence": 0.93, "reason": "info", "evidence": ["x"]}),
        "Fwd: info", "len na vedomie")
    assert v is not None
    assert v.kind == "other" and v.confidence == 0.93 and v.reason == "info"
    assert v.evidence == ["x"]


def test_classify_lowercases_and_trims_the_kind():
    v = mail_kind.classify(FakeClient({"kind": " Order ", "confidence": 0.5}), "s", "t")
    assert v is not None and v.kind == "order"


@pytest.mark.parametrize("bad", [
    {"confidence": 0.9},                      # no kind
    {"kind": "nonsense", "confidence": 0.9},  # invalid kind
    {"kind": "other", "confidence": "veľa"},  # non-numeric confidence
    ["not", "an", "object"],                  # non-object
    "plain string",
])
def test_classify_returns_none_on_unreadable_result(bad):
    assert mail_kind.classify(FakeClient(bad), "s", "t") is None


def test_classify_returns_none_on_exception():
    assert mail_kind.classify(FakeClient(RuntimeError("model down")), "s", "t") is None


# --- the diacritic-folding normalizer (F.12 foundation) -------------------

def test_fold_strips_every_slovak_caron_and_acute():
    # ľ ĺ ň č í á ô ä ž š ť ď ú é ó — none may survive folding, or an ASCII stem misses them.
    assert dl_match.fold("Č ľ Ĺ ň í á ô ž š ť ď") == "c l l n i a o z s t d"


# --- structural veto: document identifiers, every cited inflection (F.12) --

@pytest.mark.parametrize("text", [
    "Objednávka č. 12345",
    "objednávka Č.12345",
    "objednávka no 5",
    "Objednávka nr 5",
    "objednávka #5",
    "Dodací list k tovaru",
    "dodacích listov je viac",
    "DL č. 900",
    "posielam DESADV 7788",
    "avízo o dodávke",
    "AVÍZO 55",
])
def test_structural_veto_fires_on_a_document_identifier(text):
    assert mail_kind.structural_veto("", text), f"veto must fire for {text!r}"


@pytest.mark.parametrize("prose", [
    "nedodaný tovar z objednávky",             # bare word, no identifier -> NOT a veto
    "ďakujem za objednávku, ozvem sa",
    "prosím o info k objednávke 12345",        # a number without č/no/nr/# is not an identifier
    "Dobrý deň, posielam foto z predajne.",
])
def test_a_bare_order_word_in_prose_is_not_a_veto(prose):
    assert mail_kind.structural_veto("", prose) == "", f"{prose!r} must stay discardable"


def test_two_item_lines_are_a_veto_but_one_is_not():
    assert mail_kind.structural_veto("", "prosím 10 ks rožok a 5 kg múka")
    assert mail_kind.structural_veto("", "iba 10 ks rožok, inak nič") == ""


# --- readability veto -----------------------------------------------------

def test_needs_vision_attachment_is_a_readability_veto():
    assert mail_kind.readability_veto(
        [{"filename": "sken.pdf", "mime": "application/pdf", "needs_vision": True,
          "extracted_text": ""}])


def test_an_unread_pdf_is_a_veto_but_a_read_one_is_not():
    assert mail_kind.readability_veto(
        [{"filename": "d.pdf", "mime": "application/pdf", "needs_vision": False,
          "extracted_text": ""}])
    assert mail_kind.readability_veto(
        [{"filename": "d.pdf", "mime": "application/pdf", "needs_vision": False,
          "extracted_text": "riadny text"}]) == ""


def test_a_plain_image_without_text_is_not_a_readability_veto():
    # an image whose text is empty but which is NOT flagged needs_vision (e.g. a decorative
    # banner the ingest skipped) is not a document we depend on having read.
    assert mail_kind.readability_veto(
        [{"filename": "logo.jpg", "mime": "image/jpeg", "needs_vision": False,
          "extracted_text": ""}]) == ""


# --- structured attachment veto -------------------------------------------

@pytest.mark.parametrize("att", [
    {"filename": "objednavka.xlsx", "mime": ""},
    {"filename": "data.CSV", "mime": ""},
    {"filename": "list.ods", "mime": ""},
    {"filename": "x", "mime": "application/vnd.ms-excel"},
    {"filename": "x", "mime": "text/csv"},
])
def test_a_spreadsheet_attachment_is_a_veto(att):
    assert mail_kind.structured_attachment_veto([att])


def test_a_pdf_attachment_is_not_a_structured_veto():
    assert mail_kind.structured_attachment_veto(
        [{"filename": "d.pdf", "mime": "application/pdf"}]) == ""


# --- veto_reason composition ----------------------------------------------

def test_veto_reason_is_empty_for_a_clean_infomail_with_no_attachments():
    assert mail_kind.veto_reason("Fwd: info", "Dobrý deň, len na vedomie.", []) == ""


def test_veto_reason_fires_on_any_single_veto():
    assert mail_kind.veto_reason("", "10 ks a 5 kg", [])           # structural
    assert mail_kind.veto_reason("", "clean", [{"filename": "a.xlsx", "mime": ""}])  # attachment
    assert mail_kind.veto_reason("", "clean",
                                 [{"filename": "s.pdf", "mime": "application/pdf",
                                   "needs_vision": True, "extracted_text": ""}])     # readability
