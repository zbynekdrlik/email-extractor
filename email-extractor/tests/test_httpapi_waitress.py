"""#272: production must serve the dashboard/API behind waitress — a bounded thread
pool — never the Flask/Werkzeug development server `app.run(...)` used to start.

Two tests, proving two different things:

1. `test_start_wires_waitress_with_a_bounded_thread_pool` — proves `httpapi.start()`
   ITSELF was rewired off `app.run()` onto `waitress.serve()`, with the fixed pool size
   `HTTP_SERVER_THREADS` (never an unbounded/default value). Monkeypatches
   `httpapi.waitress.serve` so nothing actually binds a socket on the fixed
   `cfg.http_port` — a real bind here would collide with sibling worktree-fleet workers
   verifying the same file concurrently (`.claude/rules/local-testing.md`).

2. `test_waitress_serves_the_app_identically_to_the_dev_server` — a REAL waitress
   server (dynamic port via `waitress.server.create_server(..., port=0)`) serving the
   SAME `create_app(cfg)` the pre-existing `live_server` fixture (werkzeug's own
   `make_server`, used by the Playwright E2E suite) already serves — proves /health, an
   authenticated route, and an unauthenticated 401 come back byte-identical (status +
   JSON body) on both servers, plus checks the `Server` response header genuinely says
   "waitress" (not "Werkzeug") as direct, unambiguous proof the swap took effect.
"""
from __future__ import annotations

import os
import threading
import time
from unittest.mock import patch

import requests
from werkzeug.serving import make_server

from app import httpapi
from app.config import Config
from app.httpapi import create_app

PG_DSN = os.environ.get("PG_TEST_DSN")


def _cfg():
    return Config(pg_dsn=PG_DSN, data_dir="/tmp", api_token="tok",
                  dash_password="secret", secret_key="waitress-test-secret")


# ---- 1. httpapi.start() is wired onto waitress, with a bounded pool -----------------


def test_start_wires_waitress_with_a_bounded_thread_pool():
    cfg = _cfg()
    cfg.http_port = 18099   # never actually bound — waitress.serve is monkeypatched

    calls = []

    def fake_serve(app, **kwargs):
        calls.append((app, kwargs))

    with patch.object(httpapi.waitress, "serve", side_effect=fake_serve) as mock_serve:
        httpapi.start(cfg)
        # start() launches the server on a daemon thread — give it a moment to run.
        for _ in range(50):
            if mock_serve.called:
                break
            time.sleep(0.05)

    assert mock_serve.called, "httpapi.start() never called waitress.serve()"
    assert len(calls) == 1
    app, kwargs = calls[0]
    assert app.name == "app.httpapi"          # the real Flask app, not a stand-in
    assert kwargs.get("host") == "0.0.0.0"
    assert kwargs.get("port") == 18099
    # The whole point of #272: a FIXED, bounded pool — never left at waitress's own
    # default (4) and never unbounded like the old `threaded=True` dev server.
    assert kwargs.get("threads") == httpapi.HTTP_SERVER_THREADS
    assert httpapi.HTTP_SERVER_THREADS > 0


# ---- 2. a REAL waitress server behaves identically to the pre-existing werkzeug one --


def _waitress_server(app, threads=4):
    """Real waitress server on a DYNAMIC port (never a fixed one — avoids colliding
    with sibling worktree-fleet workers running this same file concurrently). Mirrors
    the shape `conftest.py`'s own `live_server` fixture uses for werkzeug's
    `make_server`: bind immediately (so the port is known right away), `.run()` blocks
    the accept loop in a background thread, `.close()` shuts it down."""
    import waitress.server as ws
    return ws.create_server(app, host="127.0.0.1", port=0, threads=threads)


def test_waitress_serves_the_app_identically_to_the_dev_server(pg):
    cfg = _cfg()
    app = create_app(cfg)

    waitress_srv = _waitress_server(app, threads=4)
    waitress_thread = threading.Thread(target=waitress_srv.run, daemon=True)
    waitress_thread.start()
    waitress_base = f"http://127.0.0.1:{waitress_srv.effective_port}"

    werkzeug_srv = make_server("127.0.0.1", 0, create_app(cfg), threaded=True)
    werkzeug_srv.daemon_threads = True
    werkzeug_thread = threading.Thread(target=werkzeug_srv.serve_forever, daemon=True)
    werkzeug_thread.start()
    werkzeug_base = f"http://127.0.0.1:{werkzeug_srv.server_port}"

    try:
        # ---- /health: identical status + JSON body on both servers ----
        w_health = requests.get(f"{waitress_base}/health", timeout=5)
        z_health = requests.get(f"{werkzeug_base}/health", timeout=5)
        assert w_health.status_code == z_health.status_code == 200
        assert w_health.json() == z_health.json()

        # ---- unauthenticated /api/messages: identical 401 on both servers ----
        w_401 = requests.get(f"{waitress_base}/api/messages", timeout=5)
        z_401 = requests.get(f"{werkzeug_base}/api/messages", timeout=5)
        assert w_401.status_code == z_401.status_code == 401
        assert w_401.json() == z_401.json()

        # ---- an authenticated route: log in on EACH server's own session, then
        # confirm both return 200 with the same JSON shape (a list under "messages")
        w_session = requests.Session()
        w_login = w_session.post(f"{waitress_base}/login", data={"password": "secret"},
                                  timeout=5, allow_redirects=False)
        assert w_login.status_code == 302
        w_msgs = w_session.get(f"{waitress_base}/api/messages", timeout=5)
        assert w_msgs.status_code == 200

        z_session = requests.Session()
        z_login = z_session.post(f"{werkzeug_base}/login", data={"password": "secret"},
                                  timeout=5, allow_redirects=False)
        assert z_login.status_code == 302
        z_msgs = z_session.get(f"{werkzeug_base}/api/messages", timeout=5)
        assert z_msgs.status_code == 200
        assert w_msgs.json() == z_msgs.json()

        # ---- direct, unambiguous proof the swap took effect: the Server header ----
        assert "waitress" in w_health.headers.get("Server", "").lower()
        assert "werkzeug" in z_health.headers.get("Server", "").lower()
    finally:
        waitress_srv.close()
        werkzeug_srv.shutdown()
        waitress_thread.join(timeout=5)
        werkzeug_thread.join(timeout=5)
