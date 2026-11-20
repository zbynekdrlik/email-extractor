"""IceWarp SmartAttach: an attachment delivered as a download LINK instead of a MIME part (#506).

IceWarp webmail can move a large attachment to the sender's server and put only a link in the
mail body („This email contains attachments that are delivered … using IceWarp SmartAttach
technology … 1/1: <file> (271 kB): https://icw.li/<code>"). Without this module the extractor
saw only that text, the order engine found no order, and the warehouse typed every such order
by hand (Brezno, 4 mails since 13.11.2026).

`find_links(body)` lists the (filename, url) pairs; `fetch(url)` downloads ONE file under strict
rules and returns its bytes + the server's filename/content type. `process.process_raw` turns the
result into an ordinary attachment, so every engine downstream sees it like a real one.

Security boundary (the file comes from the internet on a link a mail named):
- only a mail carrying the SmartAttach marker, only links starting `https://icw.li/`;
- every redirect (at most `MAX_REDIRECTS`) must stay https and never reach a private, loopback,
  link-local, multicast or reserved address (SSRF) — checked for every hop's resolved IPs;
- `MAX_BYTES` read in chunks, `TIMEOUT` per request, only `ALLOWED_EXT` file types;
- nothing is ever executed — the bytes only go to the existing parsers (openpyxl data_only etc.).
Residuals (accepted): DNS rebinding between the check and the connect (the first hop is pinned to
icw.li, the parsers never execute content); a server that dribbles its response HEADERS is bounded
only by the per-operation `TIMEOUT`, not by `DEADLINE` (which covers the body). A failed link is
a note in `combined_text` (the order engines); the DL engine reads only PDF/image attachments, so
a DL sent this way that fails to download shows only on the dashboard (#506).
"""
from __future__ import annotations

import http.client
import io
import ipaddress
import logging
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from email.message import Message

log = logging.getLogger("smartattach")

MARKER = "IceWarp SmartAttach"
START_HOST = "icw.li"
MAX_REDIRECTS = 3
MAX_BYTES = 25 * 1024 * 1024
MAX_UNZIPPED = 200 * 1024 * 1024   # an OOXML/ODF zip that expands past this is refused
TIMEOUT = 30                       # per socket operation
DEADLINE = 120                     # whole download (a slow-drip server cannot hold ingest)
MAX_LINKS = 5                      # per message
ALLOWED_EXT = {"xlsx", "xls", "xlsm", "ods", "csv", "pdf", "docx", "doc", "odt",
               "jpg", "jpeg", "png"}

# magic bytes per type — the server's bytes must BE the type the name claims
_ZIP_EXT = {"xlsx", "xlsm", "docx", "ods", "odt"}
_MAGIC = {"pdf": (b"%PDF",), "xls": (b"\xd0\xcf\x11\xe0",), "doc": (b"\xd0\xcf\x11\xe0",),
          "jpg": (b"\xff\xd8",), "jpeg": (b"\xff\xd8",), "png": (b"\x89PNG",)}

# „1/1: VO Pekarová žena utorok 48 tyžden(1).xlsx (271 kB): https://icw.li/tRVOyq6vR"
_LINK_RE = re.compile(
    r"^\s*\d+/\d+:\s*(?P<name>.+?)\s*\((?P<size>[\d.,]+\s*[kKmMgG]?B)\)\s*:\s*"
    r"(?P<url>https?://\S+)\s*$", re.MULTILINE)


class FetchError(Exception):
    """A link that was not downloaded — the message is the human-readable reason."""


@dataclass
class Fetched:
    filename: str
    mime: str
    data: bytes


def has_marker(body: str) -> bool:
    return bool(body) and MARKER.lower() in body.lower()


def find_links(body: str) -> list[tuple[str, str]]:
    """(filename, url) for every SmartAttach line of a mail that carries the marker. A mail
    without the marker gets nothing — a random link in a mail body is never downloaded."""
    if not has_marker(body):
        return []
    return [(m.group("name").strip(), m.group("url").strip().rstrip(".,;>)"))
            for m in _LINK_RE.finditer(body)]


def file_ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in (name or "") else ""


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _public(ip) -> bool:
    """True only for a globally routable unicast address — an IPv4 address hidden inside an
    IPv6 one (mapped, 6to4, Teredo) is judged by the IPv4 it carries."""
    if isinstance(ip, ipaddress.IPv6Address):
        inner = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if inner is None and (int(ip) >> 32) == 0:                    # IPv4-compatible ::a.b.c.d
            inner = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if inner is None and ip in _NAT64:                             # 64:ff9b::a.b.c.d
            inner = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if inner is not None:
            return _public(inner)
    return ip.is_global and not ip.is_multicast


def _check_url(url: str, *, first: bool) -> None:
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https":
        raise FetchError(f"odkaz nie je https ({p.scheme or 'bez schémy'})")
    host = (p.hostname or "").lower()
    if not host:
        raise FetchError("odkaz nemá adresu servera")
    if first and host != START_HOST:
        raise FetchError(f"odkaz nevedie na {START_HOST} ({host})")
    try:
        port = p.port or 443
    except ValueError as e:
        raise FetchError(f"odkaz má neplatný port ({e})") from e
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise FetchError(f"server {host} sa nedá nájsť ({e})") from e
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        except ValueError as e:
            raise FetchError(f"server {host} vrátil neplatnú adresu ({e})") from e
        if not _public(ip):
            raise FetchError(f"server {host} je na neverejnej adrese {ip}")


def _check_bytes(ext: str, data: bytes) -> None:
    """The bytes must be what the name says, and a zip container must not expand past
    `MAX_UNZIPPED` (a zip bomb would otherwise blow up the parser)."""
    if ext in _ZIP_EXT:
        if not data.startswith(b"PK\x03\x04"):
            raise FetchError(f"súbor nie je platný {ext}")
        # count the REAL inflated bytes — the central directory's sizes are the sender's word
        total = 0
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for info in z.infolist():
                    with z.open(info) as member:
                        while True:
                            block = member.read(1024 * 1024)
                            if not block:
                                break
                            total += len(block)
                            if total > MAX_UNZIPPED:
                                raise FetchError("súbor sa rozbalí na viac ako "
                                                 f"{MAX_UNZIPPED // (1024 * 1024)} MB")
        except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError,
                NotImplementedError, RuntimeError) as e:
            raise FetchError(f"súbor nie je platný {ext} ({e})") from e
    elif ext in _MAGIC and not data.startswith(_MAGIC[ext]):
        raise FetchError(f"súbor nie je platný {ext}")


class _Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self):
        super().__init__()
        self.hops = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.hops += 1
        if self.hops > MAX_REDIRECTS:
            raise FetchError(f"príliš veľa presmerovaní (> {MAX_REDIRECTS})")
        _check_url(newurl, first=False)
        log.info("smartattach: redirect %d (%s) -> %s", self.hops, code,
                 urllib.parse.urlsplit(newurl).hostname)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _filename(headers: Message, expected: str) -> str:
    """The mail's own name for the file first — the server's `filename=` carries raw UTF-8
    bytes that http.client decodes as latin-1 („PekarovÃ¡"); the header is only the fallback,
    repaired back to UTF-8 when it decodes as such."""
    if (expected or "").strip():
        return expected.strip()
    name = (headers.get_filename() if hasattr(headers, "get_filename") else None) or ""
    try:
        name = name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return name.strip()


def download_url(page_url: str) -> str | None:
    """The direct file URL behind an IceWarp „Collaboration" preview page, or None.

    For a browser-like client the link lands on `https://<srv>/collaboration/?ticket=T&url=
    https://<srv>/teamchatapi/` — an HTML page whose script then downloads
    `<url>files.download?ticket=T`. We build that URL ourselves instead of running the page,
    and only when `url` points back to the SAME https host the page came from."""
    p = urllib.parse.urlsplit(page_url)
    if "/collaboration" not in p.path:
        return None
    q = urllib.parse.parse_qs(p.query)
    ticket, api = (q.get("ticket") or [""])[0], (q.get("url") or [""])[0]
    a = urllib.parse.urlsplit(api)
    try:
        same = (a.hostname or "") == (p.hostname or "") and (a.port or 443) == (p.port or 443)
    except ValueError:
        return None
    if not ticket or a.scheme != "https" or not same:
        return None
    base = api if api.endswith("/") else api + "/"
    return base + "files.download?" + urllib.parse.urlencode({"ticket": ticket})


def _open(opener, url: str):
    return opener.open(urllib.request.Request(url, headers={"User-Agent": "email-extractor"}),
                       timeout=TIMEOUT)


def fetch(url: str, expected_name: str = "") -> Fetched:
    """Download ONE SmartAttach file under the module's rules. Raises `FetchError` with a
    Slovak reason on any refusal or failure — never returns partial data."""
    # no env proxy: the IP checks must judge the host we actually connect to
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _Redirects())
    deadline = time.monotonic() + DEADLINE
    try:
        _check_url(url, first=True)
        resp = _open(opener, url)
        try:
            _check_url(resp.geturl(), first=False)
            mime = (resp.headers.get_content_type() or "").lower()
            if mime.startswith("text/html"):
                direct = download_url(resp.geturl())
                if not direct:
                    raise FetchError("server vrátil webovú stránku namiesto súboru "
                                     "(odkaz pravdepodobne expiroval)")
                resp.close()
                _check_url(direct, first=False)
                log.info("smartattach: preview page -> direct download on %s",
                         urllib.parse.urlsplit(direct).hostname)
                resp = _open(opener, direct)
                _check_url(resp.geturl(), first=False)
                mime = (resp.headers.get_content_type() or "").lower()
                if mime.startswith("text/html"):
                    raise FetchError("server vrátil webovú stránku namiesto súboru "
                                     "(odkaz pravdepodobne expiroval)")
            name = _filename(resp.headers, expected_name)
            ext = file_ext(name) or file_ext(expected_name)
            if ext not in ALLOWED_EXT:
                raise FetchError(f"nepodporovaný typ súboru „{name}“")
            chunks, total = [], 0
            while True:
                chunk = resp.read1(64 * 1024)   # one recv — the deadline is checked per chunk
                if not chunk:
                    break
                total += len(chunk)
                if time.monotonic() > deadline:
                    raise FetchError(f"sťahovanie trvá dlhšie ako {DEADLINE} s")
                if total > MAX_BYTES:
                    raise FetchError(f"súbor je väčší ako {MAX_BYTES // (1024 * 1024)} MB")
                chunks.append(chunk)
        finally:
            resp.close()
    except FetchError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError, ValueError,
            http.client.HTTPException) as e:
        raise FetchError(f"sťahovanie zlyhalo ({type(e).__name__}: {e})") from e
    data = b"".join(chunks)
    if not data:
        raise FetchError("server vrátil prázdny súbor")
    _check_bytes(ext, data)
    log.info("smartattach: downloaded %s (%d B, %s)", name, len(data), mime)
    return Fetched(filename=name, mime=mime, data=data)
