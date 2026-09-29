"""#470: the add-on behind the Cloudflare tunnel (https://email-pz.newlevel.media).

The tunnel terminates TLS at Cloudflare's edge and talks plain HTTP to the add-on's
internal `:8099`, sending `X-Forwarded-Proto` / `X-Forwarded-For` (and the public `Host`).
The app must:

(a) trust ONE proxy hop (`werkzeug.middleware.proxy_fix.ProxyFix`) so `request.host_url` —
    the base the dashboard builds the warehouse links from — is the public https address;
(b) 301 to https ONLY when the tunnel says the visitor came over plain http
    (`X-Forwarded-Proto: http`); n8n's internal calls carry no such header and must keep
    getting their normal answer (no redirect, no change);
and it must hold on the REAL production server, not just the Flask test client:
waitress 3.x strips every `X-Forwarded-*` header from the environ by default
(`clear_untrusted_proxy_headers=True`), so the last test serves the app through a real
waitress server built with the exact kwargs `httpapi.start()` passes.

All hosts below are synthetic stand-ins: `email-pz.newlevel.media` is the public tunnel
hostname from the ticket, `e0ac7775-email-extractor:8099` the add-on's internal docker name.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

import requests

from app import httpapi
from app.config import Config
from app.httpapi import create_app

PUBLIC = "email-pz.newlevel.media"
INTERNAL = "http://e0ac7775-email-extractor:8099"
HTTPS = {"X-Forwarded-Proto": "https"}
PLAIN = {"X-Forwarded-Proto": "http"}


def _client():
    cfg = Config(api_token="secret", dash_password="pw", secret_key="t",
                 pg_dsn="postgresql://unused", data_dir="/tmp", public_base_url=INTERNAL)
    app = create_app(cfg)
    app.testing = True
    return app.test_client()


# ---- (a) ProxyFix: the dashboard links are the public https address ------------------


def test_dashboard_links_use_https_behind_the_tunnel():
    c = _client()
    base = f"http://{PUBLIC}"       # the tunnel speaks plain http to the add-on
    login = c.post("/login", data={"password": "pw"}, base_url=base, headers=HTTPS)
    assert login.status_code == 302
    body = c.get("/", base_url=base, headers=HTTPS).data.decode()
    assert f"https://{PUBLIC}/sklad/" + httpapi.sklad_key("t") in body
    assert f"https://{PUBLIC}/sklad-dl/" + httpapi.dl_key("t") in body
    assert f"http://{PUBLIC}/sklad" not in body


def test_dashboard_links_honour_the_forwarded_host():
    """A proxy that rewrites Host to the internal name still names the public host in
    `X-Forwarded-Host` — one trusted hop, so that value wins."""
    c = _client()
    hdrs = {**HTTPS, "X-Forwarded-Host": PUBLIC}
    c.post("/login", data={"password": "pw"}, base_url=INTERNAL, headers=hdrs)
    body = c.get("/", base_url=INTERNAL, headers=hdrs).data.decode()
    assert f"https://{PUBLIC}/sklad/" + httpapi.sklad_key("t") in body
    assert "e0ac7775-email-extractor:8099/sklad" not in body


def test_the_forwarded_client_address_is_what_gets_logged(caplog):
    """x_for=1: a refused warehouse link logs the VISITOR, not the tunnel's own address."""
    c = _client()
    with caplog.at_level("WARNING", logger="email_extractor.httpapi"):
        r = c.get("/sklad/" + "0" * 32, base_url=f"http://{PUBLIC}",
                  headers={**HTTPS, "X-Forwarded-For": "198.51.100.23"})
    assert r.status_code == 403
    assert any("198.51.100.23" in rec.getMessage() for rec in caplog.records)


# ---- (b) https redirect only for a tunnel request that arrived over plain http --------


def test_plain_http_through_the_tunnel_is_redirected_to_https():
    c = _client()
    r = c.get("/health", base_url=f"http://{PUBLIC}", headers=PLAIN)
    assert r.status_code == 301
    assert r.headers["Location"] == f"https://{PUBLIC}/health"


def test_the_redirect_keeps_the_path_and_query_and_runs_before_the_auth_gate():
    c = _client()
    # A gated page: the visitor is sent to https FIRST (not to /login over http).
    r = c.get("/nastenka/otazky-sklad?q=12", base_url=f"http://{PUBLIC}", headers=PLAIN)
    assert r.status_code == 301
    assert r.headers["Location"] == f"https://{PUBLIC}/nastenka/otazky-sklad?q=12"


def test_an_internal_call_without_the_header_is_never_redirected():
    """n8n (and every internal docker-network caller) sends no X-Forwarded-Proto."""
    c = _client()
    r = c.get("/health", base_url=INTERNAL)
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    assert c.get("/version", base_url=INTERNAL).status_code == 200


def test_https_through_the_tunnel_is_served_not_redirected():
    c = _client()
    r = c.get("/health", base_url=f"http://{PUBLIC}", headers=HTTPS)
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


# ---- the production chain: a REAL waitress server with start()'s own kwargs ----------


def _start_kwargs() -> dict:
    """The exact keyword arguments `httpapi.start()` hands to `waitress.serve()`."""
    cfg = Config(api_token="secret", dash_password="pw", secret_key="t",
                 pg_dsn="postgresql://unused", data_dir="/tmp", public_base_url=INTERNAL)
    cfg.http_port = 18099   # never bound — waitress.serve is patched
    calls = []
    with patch.object(httpapi.waitress, "serve",
                      side_effect=lambda app, **kw: calls.append(kw)) as mock_serve:
        httpapi.start(cfg)
        for _ in range(50):
            if mock_serve.called:
                break
            time.sleep(0.05)
    assert calls, "httpapi.start() never called waitress.serve()"
    kw = dict(calls[0])
    kw.pop("host", None)
    kw.pop("port", None)
    return kw


def test_the_real_waitress_server_passes_the_tunnel_headers_through():
    import waitress.server as ws

    app = create_app(Config(api_token="secret", dash_password="pw", secret_key="t",
                            pg_dsn="postgresql://unused", data_dir="/tmp",
                            public_base_url=INTERNAL))
    srv = ws.create_server(app, host="127.0.0.1", port=0, **_start_kwargs())
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.effective_port}"
    try:
        # (b) over plain http through the tunnel -> 301 to the public https URL
        r = requests.get(f"{base}/health", headers={"Host": PUBLIC, **PLAIN},
                         allow_redirects=False, timeout=5)
        assert r.status_code == 301, r.status_code
        assert r.headers["Location"] == f"https://{PUBLIC}/health"

        # internal call, no header -> 200, untouched
        r = requests.get(f"{base}/health", allow_redirects=False, timeout=5)
        assert r.status_code == 200 and r.json()["ok"] is True

        # (a) the dashboard, reached over https through the tunnel -> https links
        s = requests.Session()
        login = s.post(f"{base}/login", data={"password": "pw"},
                       headers={"Host": PUBLIC, **HTTPS}, allow_redirects=False, timeout=5)
        assert login.status_code == 302
        body = s.get(f"{base}/", headers={"Host": PUBLIC, **HTTPS}, timeout=5).text
        assert f"https://{PUBLIC}/sklad/" + httpapi.sklad_key("t") in body
        assert f"https://{PUBLIC}/sklad-dl/" + httpapi.dl_key("t") in body
    finally:
        srv.close()
        thread.join(timeout=5)
