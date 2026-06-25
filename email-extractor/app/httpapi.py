"""Internal HTTP API so n8n can fetch original files (for AI Vision) + the raw .eml."""
from __future__ import annotations

import threading
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file

from . import __version__
from .store import safe_id


def create_app(cfg) -> Flask:
    app = Flask(__name__)
    data_dir = Path(cfg.data_dir)

    def _auth():
        if cfg.api_token:
            tok = request.args.get("token") or request.headers.get("X-Token")
            if tok != cfg.api_token:
                abort(403)

    @app.get("/health")
    def health():
        return jsonify(ok=True, version=__version__)

    @app.get("/version")
    def version():
        return __version__

    @app.get("/files/<mid>/<int:idx>")
    def get_file(mid: str, idx: int):
        _auth()
        folder = data_dir / safe_id(mid)
        matches = sorted(folder.glob(f"att{idx}__*"))
        if not matches:
            abort(404)
        return send_file(matches[0])

    @app.get("/eml/<mid>")
    def get_eml(mid: str):
        _auth()
        path = data_dir / safe_id(mid) / "raw.eml"
        if not path.exists():
            abort(404)
        return send_file(path, mimetype="message/rfc822")

    return app


def start(cfg) -> None:
    app = create_app(cfg)
    t = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=cfg.http_port, threaded=True),
        daemon=True,
    )
    t.start()
