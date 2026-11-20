"""#506: IceWarp SmartAttach — an order attachment delivered as a download link.

Synthetic data only (no real customer mail). The network is never touched: DNS is faked via
`socket.getaddrinfo`, the HTTP response via a fake opener, and `process_raw` gets a fake fetch.
"""
import io
import socket
from email.message import EmailMessage, Message

import openpyxl
import pytest

from app import process, smartattach

BODY = (
    "This email contains attachments that are delivered for download using IceWarp "
    "SmartAttach technology, more at http://www.icewarp.com/SmartAttach/.\n\n"
    "Click the links below to download the attachment(s).\n\n"
    "1/1: VO cennik utorok 48 tyzden(1).xlsx (271 kB): https://icw.li/AbC123xyz\n\n"
    "Dobry den, posielam objednavku na stvrtok 26.11 2026.\n")


def _xlsx_bytes() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Názov tovaru", "%DPH", "VO bez DPH", "VO s DPH", "Trvanlivosť"])
    ws.append(["Bageta testovacia 250g", 19, 1, 1.19, 1, 2])
    ws.append(["Rožok testovací 70g", 5, 0.38, 0.4, 1, 30])
    ws.append(["Chlieb neobjednaný 500g", 5, 1.8, 1.89, 1])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _raw(body: str) -> bytes:
    m = EmailMessage()
    m["From"] = "Predajna Test <objednavky@predajna-test.sk>"
    m["To"] = "predaj@example.sk"
    m["Subject"] = "Objednávka štvrtok"
    m["Message-ID"] = "<smartattach-test@predajna-test.sk>"
    m.set_content(body)
    return m.as_bytes()


def _dns(ip: str):
    def fake(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
    return fake


# --- finding the links ---------------------------------------------------------------------

def test_find_links_reads_the_smartattach_line():
    assert smartattach.find_links(BODY) == [
        ("VO cennik utorok 48 tyzden(1).xlsx", "https://icw.li/AbC123xyz")]


def test_a_link_without_the_smartattach_marker_is_never_followed():
    body = "1/1: subor.xlsx (271 kB): https://icw.li/AbC123xyz\n"
    assert smartattach.find_links(body) == []
    assert smartattach.find_links("") == []


# --- the security checks ---------------------------------------------------------------------

def test_the_first_hop_must_be_https_icw_li(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _dns("93.184.216.34"))
    with pytest.raises(smartattach.FetchError, match="https"):
        smartattach.fetch("http://icw.li/x", "a.xlsx")
    with pytest.raises(smartattach.FetchError, match="icw.li"):
        smartattach.fetch("https://evil.example/x", "a.xlsx")


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.10", "169.254.169.254",
                                "::1", "172.16.0.1"])
def test_a_private_or_loopback_address_is_refused(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", _dns(ip))
    with pytest.raises(smartattach.FetchError, match="neverejnej"):
        smartattach.fetch("https://icw.li/x", "a.xlsx")


def test_a_redirect_to_http_or_a_private_address_is_refused(monkeypatch):
    h = smartattach._Redirects()
    monkeypatch.setattr(socket, "getaddrinfo", _dns("93.184.216.34"))
    with pytest.raises(smartattach.FetchError, match="https"):
        h.redirect_request(None, None, 302, "Found", {}, "http://mail.example.sk/f")
    monkeypatch.setattr(socket, "getaddrinfo", _dns("10.1.2.3"))
    with pytest.raises(smartattach.FetchError, match="neverejnej"):
        h.redirect_request(None, None, 302, "Found", {}, "https://intranet.example.sk/f")


def test_too_many_redirects_are_refused(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _dns("93.184.216.34"))
    h = smartattach._Redirects()
    h.hops = smartattach.MAX_REDIRECTS
    with pytest.raises(smartattach.FetchError, match="presmerovan"):
        h.redirect_request(None, None, 302, "Found", {}, "https://mail.example.sk/f")


class _Resp:
    def __init__(self, data: bytes, ctype: str, filename: str, url="https://mail.example.sk/f"):
        self._buf = io.BytesIO(data)
        self._url = url
        self.headers = Message()
        self.headers["Content-Type"] = ctype
        if filename:
            self.headers["Content-Disposition"] = f'attachment; filename="{filename}"'

    def read(self, n=-1):
        return self._buf.read(n)

    def read1(self, n=-1):
        return self._buf.read1(n)

    def geturl(self):
        return self._url

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _opener(monkeypatch, *resps):
    """A fake opener answering the calls in order (the last response repeats); records URLs."""
    urls = []

    class _FakeOpener:
        def open(self, req, timeout=None):
            assert timeout == smartattach.TIMEOUT
            urls.append(req.full_url)
            return resps[min(len(urls), len(resps)) - 1]
    monkeypatch.setattr(smartattach.urllib.request, "build_opener", lambda *h: _FakeOpener())
    return urls


XLSX_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_fetch_returns_the_file_with_the_servers_name(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _dns("93.184.216.34"))
    data = _xlsx_bytes()
    _opener(monkeypatch, _Resp(data, XLSX_CT, "objednavka.xlsx"))
    got = smartattach.fetch("https://icw.li/x", "")
    assert got.data == data and got.filename == "objednavka.xlsx" and got.mime == XLSX_CT
    # the mail's own (UTF-8) name wins over the server header
    mojibake = "Pekarov\u00c3\u00a1.xlsx"
    _opener(monkeypatch, _Resp(data, XLSX_CT, mojibake))
    assert smartattach.fetch("https://icw.li/x", "Pekarová žena.xlsx").filename == \
        "Pekarová žena.xlsx"
    _opener(monkeypatch, _Resp(data, XLSX_CT, mojibake))
    assert smartattach.fetch("https://icw.li/x", "").filename == "Pekarová.xlsx"


def test_fetch_refuses_a_web_page_a_wrong_type_and_a_huge_file(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _dns("93.184.216.34"))
    _opener(monkeypatch, _Resp(b"<html>expired</html>", "text/html", ""))
    with pytest.raises(smartattach.FetchError, match="webovú stránku"):
        smartattach.fetch("https://icw.li/x", "a.xlsx")
    _opener(monkeypatch, _Resp(b"MZ\x90\x00", "application/octet-stream", "setup.exe"))
    with pytest.raises(smartattach.FetchError, match="nepodporovaný"):
        smartattach.fetch("https://icw.li/x", "setup.exe")
    monkeypatch.setattr(smartattach, "MAX_BYTES", 1000)
    _opener(monkeypatch, _Resp(b"x" * 5000, XLSX_CT, "big.xlsx"))
    with pytest.raises(smartattach.FetchError, match="väčší"):
        smartattach.fetch("https://icw.li/x", "big.xlsx")


def test_fetch_rechecks_the_final_address(monkeypatch):
    calls = []

    def dns(host, port, *a, **kw):
        calls.append(host)
        ip = "10.9.9.9" if host == "internal.example.sk" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    _opener(monkeypatch, _Resp(b"PK..", XLSX_CT, "a.xlsx",
                               url="https://internal.example.sk/f"))
    with pytest.raises(smartattach.FetchError, match="neverejnej"):
        smartattach.fetch("https://icw.li/x", "a.xlsx")
    assert calls == ["icw.li", "internal.example.sk"]


PREVIEW = ("https://mail.example.sk/collaboration/?ticket=TCK123&"
           "url=https%3A//mail.example.sk/teamchatapi/")


def test_an_icewarp_preview_page_is_followed_to_its_direct_download(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _dns("93.184.216.34"))
    data = _xlsx_bytes()
    urls = _opener(monkeypatch, _Resp(b"<html>IceWarp Collaboration</html>",
                                      "text/html; charset=UTF-8", "", url=PREVIEW),
                   _Resp(data, XLSX_CT, "objednavka.xlsx",
                         url="https://mail.example.sk/teamchatapi/files.download?ticket=TCK123"))
    got = smartattach.fetch("https://icw.li/x", "")
    assert got.data == data and got.filename == "objednavka.xlsx"
    assert urls == ["https://icw.li/x",
                    "https://mail.example.sk/teamchatapi/files.download?ticket=TCK123"]


def test_the_direct_download_must_stay_on_the_preview_pages_host():
    assert smartattach.download_url(PREVIEW) == (
        "https://mail.example.sk/teamchatapi/files.download?ticket=TCK123")
    other = ("https://mail.example.sk/collaboration/?ticket=T&"
             "url=https%3A//evil.example/teamchatapi/")
    assert smartattach.download_url(other) is None
    plain = "https://mail.example.sk/collaboration/?ticket=T&url=http%3A//mail.example.sk/x/"
    assert smartattach.download_url(plain) is None
    assert smartattach.download_url("https://mail.example.sk/other/?ticket=T") is None
    assert smartattach.download_url("https://mail.example.sk/collaboration/?url=x") is None


# --- wired into ingest -----------------------------------------------------------------------

def test_process_raw_adds_the_downloaded_xlsx_as_an_attachment():
    data = _xlsx_bytes()
    seen = []

    def fake_fetch(url, name):
        seen.append((url, name))
        return smartattach.Fetched(filename=name, mime=XLSX_CT, data=data)

    rec = process.process_raw(_raw(BODY), fetch=fake_fetch)
    assert seen == [("https://icw.li/AbC123xyz", "VO cennik utorok 48 tyzden(1).xlsx")]
    att = [a for a in rec["attachments"] if a["filename"].endswith(".xlsx")]
    assert len(att) == 1 and att[0]["method"] == "xlsx" and att[0]["_data"] == data
    assert rec["has_attachments"] is True
    # the order lines (with the quantity column) reach the text the engines read
    assert "Rožok testovací 70g" in rec["combined_text"]
    line = next(x for x in rec["combined_text"].splitlines() if "Rožok testovací" in x)
    assert line.split("\t")[-1] == "30"


def test_a_failed_download_is_a_visible_note_never_a_silent_loss():
    def failing(url, name):
        raise smartattach.FetchError("server vrátil webovú stránku namiesto súboru")

    rec = process.process_raw(_raw(BODY), fetch=failing)
    notes = [a for a in rec["attachments"] if a["flag"] == "link_failed"]
    assert len(notes) == 1
    assert "nepodarilo stiahnuť" in notes[0]["text"]
    assert "nepodarilo stiahnuť" in rec["combined_text"]


def test_a_mail_without_smartattach_never_calls_fetch():
    def boom(url, name):
        raise AssertionError("must not fetch")
    rec = process.process_raw(_raw("Dobrý deň, 10 ks rožok https://icw.li/abc"), fetch=boom)
    assert rec["attachments"] == []


# --- backfill of an already-ingested message ---------------------------------------------------

def test_reextract_adds_the_link_file_to_an_already_ingested_message(pg, tmp_path):
    from app import db, reextract
    from app.config import Config

    raw = _raw(BODY)
    # the pre-#506 ingest: no link attachment, only the notice text
    old = process.process_raw(raw, fetch=lambda u, n: (_ for _ in ()).throw(
        smartattach.FetchError("x")))
    old["attachments"] = [a for a in old["attachments"] if a.get("method") != "link"]
    old["combined_text"] = process._combined_text(old["headers"], old["body_text"], [])
    raw_path = tmp_path / "raw.eml"
    raw_path.write_bytes(raw)
    assert db.insert_message(pg, old, "INBOX", 1, 1, str(raw_path), [])
    msg_id = pg.execute("SELECT id FROM messages WHERE message_id = %s",
                        (old["identity"],)).fetchone()[0]
    cfg = Config(pg_dsn="", data_dir=str(tmp_path), public_base_url="http://ee")
    data = _xlsx_bytes()

    def fake_fetch(url, name):
        return smartattach.Fetched(filename=name, mime=XLSX_CT, data=data)

    res = reextract.add_link_attachments(pg, cfg, msg_id, fetch=fake_fetch)
    assert res["added"] == 1
    rows = pg.execute("SELECT idx, filename, method, file_path FROM attachments "
                      "WHERE message_id = %s ORDER BY idx", (old["identity"],)).fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [
        (0, "VO cennik utorok 48 tyzden(1).xlsx", "xlsx")]
    from pathlib import Path
    assert Path(rows[0][3]).read_bytes() == data
    combined, has_att = pg.execute(
        "SELECT combined_text, has_attachments FROM messages WHERE id = %s",
        (msg_id,)).fetchone()
    assert "Rožok testovací 70g" in combined and has_att is True
    # idempotent: a second run adds nothing
    assert reextract.add_link_attachments(pg, cfg, msg_id, fetch=fake_fetch)["added"] == 0
    assert pg.execute("SELECT count(*) FROM attachments WHERE message_id = %s",
                      (old["identity"],)).fetchone()[0] == 1


# --- review round 1: every failure is a note, never a crash; tighter SSRF + content checks -------

@pytest.mark.parametrize("ip", ["::ffff:127.0.0.1", "::ffff:10.0.0.1", "2002:7f00:1::1",
                                "::7f00:1", "::a00:1", "64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe",
                                "fe80::1%eth0", "::", "0.0.0.0", "224.0.0.1",
                                "2001:0:4136:e378:8000:63bf:f5ff:fffe"])
def test_hidden_private_ipv4_and_odd_addresses_are_refused(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", _dns(ip))
    with pytest.raises(smartattach.FetchError, match="neverejnej"):
        smartattach.fetch("https://icw.li/x", "a.xlsx")


def test_an_invalid_port_or_address_is_a_fetch_error_not_a_crash(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _dns("93.184.216.34"))
    with pytest.raises(smartattach.FetchError, match="port"):
        smartattach.fetch("https://icw.li:99999/x", "a.xlsx")
    monkeypatch.setattr(socket, "getaddrinfo", _dns("not-an-ip"))
    with pytest.raises(smartattach.FetchError, match="neplatnú adresu"):
        smartattach.fetch("https://icw.li/x", "a.xlsx")


def test_a_broken_http_reply_is_a_fetch_error(monkeypatch):
    import http.client
    monkeypatch.setattr(socket, "getaddrinfo", _dns("93.184.216.34"))

    class _Broken(_Resp):
        def read1(self, n=-1):
            raise http.client.IncompleteRead(b"PK", 100)
    _opener(monkeypatch, _Broken(b"", XLSX_CT, "a.xlsx"))
    with pytest.raises(smartattach.FetchError, match="IncompleteRead"):
        smartattach.fetch("https://icw.li/x", "a.xlsx")


def test_a_slow_drip_download_stops_at_the_deadline(monkeypatch):
    # read1 (one recv per call) is what makes the deadline reachable on a real socket
    monkeypatch.setattr(socket, "getaddrinfo", _dns("93.184.216.34"))
    clock = iter(range(0, 10_000, 50))
    monkeypatch.setattr(smartattach.time, "monotonic", lambda: next(clock))
    _opener(monkeypatch, _Resp(b"PK\x03\x04" + b"x" * 400_000, XLSX_CT, "a.xlsx"))
    with pytest.raises(smartattach.FetchError, match="dlhšie"):
        smartattach.fetch("https://icw.li/x", "a.xlsx")


def test_bytes_must_match_the_type_and_a_zip_bomb_is_refused(monkeypatch):
    import zipfile
    monkeypatch.setattr(socket, "getaddrinfo", _dns("93.184.216.34"))
    _opener(monkeypatch, _Resp(b"MZ\x90\x00 not a spreadsheet", XLSX_CT, "a.xlsx"))
    with pytest.raises(smartattach.FetchError, match="nie je platný xlsx"):
        smartattach.fetch("https://icw.li/x", "a.xlsx")
    _opener(monkeypatch, _Resp(b"<html>", "application/pdf", "a.pdf"))
    with pytest.raises(smartattach.FetchError, match="nie je platný pdf"):
        smartattach.fetch("https://icw.li/x", "a.pdf")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/big.xml", b"\0" * (3 * 1024 * 1024))
    monkeypatch.setattr(smartattach, "MAX_UNZIPPED", 1024 * 1024)
    _opener(monkeypatch, _Resp(buf.getvalue(), XLSX_CT, "bomb.xlsx"))
    with pytest.raises(smartattach.FetchError, match="rozbalí"):
        smartattach.fetch("https://icw.li/x", "bomb.xlsx")


def test_the_direct_download_must_keep_the_preview_pages_port():
    odd = ("https://mail.example.sk/collaboration/?ticket=T&"
           "url=https%3A//mail.example.sk%3A8443/teamchatapi/")
    assert smartattach.download_url(odd) is None
    explicit = ("https://mail.example.sk/collaboration/?ticket=T&"
                "url=https%3A//mail.example.sk%3A443/teamchatapi/")
    assert smartattach.download_url(explicit) == (
        "https://mail.example.sk:443/teamchatapi/files.download?ticket=T")


def test_a_zip_whose_directory_understates_its_size_is_still_measured(monkeypatch):
    import struct
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/big.xml", b"\0" * (3 * 1024 * 1024))
    data = bytearray(buf.getvalue())
    # forge the central-directory uncompressed size to 10 bytes
    cd = data.rfind(b"PK\x01\x02")
    struct.pack_into("<I", data, cd + 24, 10)
    monkeypatch.setattr(smartattach, "MAX_UNZIPPED", 1024 * 1024)
    with pytest.raises(smartattach.FetchError):
        smartattach._check_bytes("xlsx", bytes(data))


def test_an_unexpected_fetch_exception_never_crashes_ingest():
    def boom(url, name):
        raise RuntimeError("socket exploded")
    rec = process.process_raw(_raw(BODY), fetch=boom)
    notes = [a for a in rec["attachments"] if a["flag"] == "link_failed"]
    assert len(notes) == 1 and "socket exploded" in notes[0]["text"]


def test_a_marker_without_a_recognised_link_is_a_visible_note():
    body = BODY.replace("(271 kB)", "")
    rec = process.process_raw(_raw(body), fetch=lambda u, n: pytest.fail("must not fetch"))
    notes = [a for a in rec["attachments"] if a["flag"] == "link_failed"]
    assert len(notes) == 1 and "nenašiel" in notes[0]["text"]


def test_at_most_max_links_are_downloaded(monkeypatch):
    lines = "".join(f"{i}/7: subor{i}.xlsx (1 kB): https://icw.li/k{i}\n" for i in range(1, 8))
    body = BODY.split("1/1:")[0] + lines
    calls = []

    def fake(url, name):
        calls.append(url)
        return smartattach.Fetched(filename=name, mime=XLSX_CT, data=_xlsx_bytes())
    rec = process.process_raw(_raw(body), fetch=fake)
    assert len(calls) == smartattach.MAX_LINKS
    notes = [a for a in rec["attachments"] if a["flag"] == "link_failed"]
    assert len(notes) == 1 and "subor6.xlsx, subor7.xlsx" in notes[0]["text"]


def test_reextract_supersedes_an_earlier_failed_download_note(pg, tmp_path):
    from app import db, reextract
    from app.config import Config

    raw = _raw(BODY)
    failed = process.process_raw(raw, fetch=lambda u, n: (_ for _ in ()).throw(
        smartattach.FetchError("odkaz dočasne nedostupný")))
    raw_path = tmp_path / "raw.eml"
    raw_path.write_bytes(raw)
    assert db.insert_message(pg, failed, "INBOX", 2, 1, str(raw_path), [])
    msg_id = pg.execute("SELECT id FROM messages WHERE message_id = %s",
                        (failed["identity"],)).fetchone()[0]
    cfg = Config(pg_dsn="", data_dir=str(tmp_path), public_base_url="http://ee")
    data = _xlsx_bytes()
    res = reextract.add_link_attachments(
        pg, cfg, msg_id,
        fetch=lambda u, n: smartattach.Fetched(filename=n, mime=XLSX_CT, data=data))
    assert res["added"] == 1
    flags = pg.execute("SELECT filename, flag FROM attachments WHERE message_id = %s "
                       "ORDER BY idx", (failed["identity"],)).fetchall()
    assert flags == [("VO cennik utorok 48 tyzden(1).xlsx.odkaz.txt", "skipped_link_superseded"),
                     ("VO cennik utorok 48 tyzden(1).xlsx", "ok")]
    combined = pg.execute("SELECT combined_text FROM messages WHERE id = %s",
                          (msg_id,)).fetchone()[0]
    assert "nepodarilo stiahnuť" not in combined and "Rožok testovací 70g" in combined


def test_reextract_dedupes_a_downloaded_file_by_content_not_name(pg, tmp_path):
    from app import db, reextract
    from app.config import Config

    raw = _raw(BODY)
    data = _xlsx_bytes()
    rec = process.process_raw(raw, fetch=lambda u, n: smartattach.Fetched(
        filename=n, mime=XLSX_CT, data=data))
    raw_path = tmp_path / "raw.eml"
    raw_path.write_bytes(raw)
    from app import store
    _, files = store.save_message(str(tmp_path), rec["identity"], raw, rec["attachments"],
                                  "http://ee")
    assert db.insert_message(pg, rec, "INBOX", 3, 1, str(raw_path), files)
    msg_id = pg.execute("SELECT id FROM messages WHERE message_id = %s",
                        (rec["identity"],)).fetchone()[0]
    cfg = Config(pg_dsn="", data_dir=str(tmp_path), public_base_url="http://ee")
    other = bytes(data)   # the same bytes again -> already stored
    assert reextract.add_link_attachments(
        pg, cfg, msg_id, fetch=lambda u, n: smartattach.Fetched(
            filename=n, mime=XLSX_CT, data=other))["added"] == 0
