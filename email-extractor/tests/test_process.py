"""Tests for full-message processing (parse + extract + combined text)."""
import io
from email.message import EmailMessage

from app import process


def _docx_bytes(text: str) -> bytes:
    import docx
    d = docx.Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _build_email(body="Dobrý deň, posielam objednávku.", attachments=()):
    m = EmailMessage()
    m["From"] = "Acme s.r.o. <orders@supplier.example>"
    m["To"] = "sklad@company.example"
    m["Subject"] = "Objednávka 1030"
    m["Message-ID"] = "<test-123@supplier.example>"
    m["Date"] = "Wed, 25 Jun 2026 13:00:00 +0200"
    m.set_content(body)
    for fn, mime, data in attachments:
        maintype, _, subtype = mime.partition("/")
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=fn)
    return m.as_bytes()


def test_headers_and_identity():
    rec = process.process_raw(_build_email())
    assert rec["identity"] == "<test-123@supplier.example>"
    assert rec["headers"]["from_addr"] == "orders@supplier.example"
    assert rec["headers"]["to_addrs"] == ["sklad@company.example"]
    assert rec["headers"]["subject"] == "Objednávka 1030"
    assert rec["has_attachments"] is False


def test_combined_text_includes_body_and_docx():
    docx = _docx_bytes("Chlieb 15 ks, Bageta 7 ks")
    rec = process.process_raw(_build_email(attachments=[
        ("objednavka.docx",
         "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         docx),
    ]))
    assert rec["has_attachments"] is True
    ct = rec["combined_text"]
    assert "Objednávka 1030" in ct          # subject
    assert "posielam objednávku" in ct      # body
    assert "Chlieb 15 ks" in ct             # attachment text
    assert "objednavka.docx" in ct


def test_combined_text_excludes_junk_attachment():
    rec = process.process_raw(_build_email(attachments=[
        ("contact.vcf", "text/vcard", b"BEGIN:VCARD\nFN:X\nEND:VCARD"),
    ]))
    # vcf is junk -> skipped -> must NOT pollute combined text
    assert "BEGIN:VCARD" not in rec["combined_text"]
    assert rec["attachments"][0]["flag"] == "skipped_junk"


def test_raw_bytes_attached_for_filestore():
    docx = _docx_bytes("x")
    rec = process.process_raw(_build_email(attachments=[
        ("a.docx",
         "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         docx),
    ]))
    assert rec["attachments"][0]["_data"] == docx
