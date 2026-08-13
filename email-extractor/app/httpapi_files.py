"""Token-gated file-serving endpoints — /files/<mid>/<idx>, /eml/<mid> (#268 krok 5).

Moved VERBATIM out of `app/httpapi.py` (no behavior change) — see the design comment on
#268 for exactly what moved and why. Machine endpoints n8n uses for AI-Vision routing /
SMTP forwarding: a logged-in dashboard session OR a valid machine token (`X-Token`
header or `?token=` query, checked against `cfg.api_token`) may fetch an original
attachment or the raw .eml. No open-by-default — if neither is configured the endpoint
stays closed.
"""
from __future__ import annotations

from flask import Flask, abort, request, send_file, session

from .httpapi_common import Deps
from .store import message_dir


def register(app: Flask, deps: Deps) -> None:
    def _token_ok():
        tok = request.args.get("token") or request.headers.get("X-Token")
        return bool(deps.cfg.api_token) and tok == deps.cfg.api_token

    def _auth():
        # File APIs (/files, /eml): a logged-in human OR a valid machine token.
        # No open-by-default — if neither is configured the endpoint stays closed.
        if not (session.get("auth") or _token_ok()):
            abort(403)

    @app.get("/files/<mid>/<int:idx>")
    def get_file(mid: str, idx: int):
        _auth()
        matches = sorted(message_dir(str(deps.data_dir), mid).glob(f"att{idx}__*"))
        if not matches:
            abort(404)
        return send_file(matches[0])

    @app.get("/eml/<mid>")
    def get_eml(mid: str):
        _auth()
        path = message_dir(str(deps.data_dir), mid) / "raw.eml"
        if not path.exists():
            abort(404)
        return send_file(path, mimetype="message/rfc822")
