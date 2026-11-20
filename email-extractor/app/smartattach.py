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
Residual: DNS rebinding between the check and the connect; accepted, the first hop is pinned to
icw.li and the parsers never execute content.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import Message

log = logging.getLogger("smartattach")

MARKER = "IceWarp SmartAttach"
START_HOST = "icw.li"
MAX_REDIRECTS = 3
MAX_BYTES = 25 * 1024 * 1024
TIMEOUT = 30
ALLOWED_EXT = {"xlsx", "xls", "xlsm", "ods", "csv", "pdf", "docx", "doc", "odt",
               "jpg", "jpeg", "png"}

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


def find_links(body: str) -> list[tuple[str, str]]:
    """(filename, url) for every SmartAttach line of a mail that carries the marker. A mail
    without the marker gets nothing — a random link in a mail body is never downloaded."""
    if not body or MARKER.lower() not in body.lower():
        return []
    return [(m.group("name").strip(), m.group("url").strip().rstrip(".,;>)"))
            for m in _LINK_RE.finditer(body)]


def file_ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in (name or "") else ""


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
        infos = socket.getaddrinfo(host, p.port or 443, proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise FetchError(f"server {host} sa nedá nájsť ({e})") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise FetchError(f"server {host} je na neverejnej adrese {ip}")


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
    if not ticket or a.scheme != "https" or (a.hostname or "") != (p.hostname or ""):
        return None
    base = api if api.endswith("/") else api + "/"
    return base + "files.download?" + urllib.parse.urlencode({"ticket": ticket})


def _open(opener, url: str):
    return opener.open(urllib.request.Request(url, headers={"User-Agent": "email-extractor"}),
                       timeout=TIMEOUT)


def fetch(url: str, expected_name: str = "") -> Fetched:
    """Download ONE SmartAttach file under the module's rules. Raises `FetchError` with a
    Slovak reason on any refusal or failure — never returns partial data."""
    _check_url(url, first=True)
    opener = urllib.request.build_opener(_Redirects())
    try:
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
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_BYTES:
                    raise FetchError(f"súbor je väčší ako {MAX_BYTES // (1024 * 1024)} MB")
                chunks.append(chunk)
        finally:
            resp.close()
    except FetchError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise FetchError(f"sťahovanie zlyhalo ({e})") from e
    data = b"".join(chunks)
    if not data:
        raise FetchError("server vrátil prázdny súbor")
    log.info("smartattach: downloaded %s (%d B, %s)", name, len(data), mime)
    return Fetched(filename=name, mime=mime, data=data)
