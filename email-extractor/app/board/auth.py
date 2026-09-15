"""The ONE gate for the unified nástenka (#442 lane 1, spec §6).

Replaces the three separate regex allow-lists the OLD boards use (`SKLAD_PATHS`/
`SKLAD_DL_PATHS`/`SKLAD_ZNALOSTI_*` in `httpapi_security.py`) with a single decision for
everything under `/nastenka*` and `/api/board/*`:

- role `sklad` — a session that came in through EITHER signed HMAC key (`/sklad/<k>` sets
  `SKLAD_ROLE`, `/sklad-dl/<k>` sets `SKLAD_DL_ROLE`); BOTH map to the board's one `sklad`
  role. The DL/orders distinction is made by the TAB, never by which key was used, so a DL
  session keeps full access to every tab (it must NOT lose access — the whole point of §6).
- role `admin` — a real `dash_password` login (`session["auth"]`), which is always
  unrestricted (same precedence `_role_kinds` already uses: `auth` beats `role`).
- anyone else — a page request is redirected to `/login`; an `/api/board/*` request gets a
  401 (the same shape the old `_gate` returns for an unauthenticated `/api/*` call).

`httpapi._gate` delegates the board paths here (it runs `before_request` for ALL paths, so
the delegation, not a blueprint `before_request`, is what actually guards these routes). This
module imports ONLY the role CONSTANTS from `httpapi_security` — never Flask app state — so it
stays a thin, independently-auditable boundary.
"""
from __future__ import annotations

from flask import jsonify, redirect, request, session

from ..httpapi_security import SKLAD_DL_ROLE, SKLAD_ROLE

ADMIN = "admin"
SKLAD = "sklad"


def board_role() -> str | None:
    """`admin` for a dash_password session, `sklad` for either warehouse key, else None.

    `auth` beats `role` — a logged-in admin who also clicked a nástenka link (setting
    `role` in the same cookie jar) is still admin, never a role-filtered sklad session.
    """
    if session.get("auth"):
        return ADMIN
    if session.get("role") in (SKLAD_ROLE, SKLAD_DL_ROLE):
        return SKLAD
    return None


def board_gate():
    """None to allow the request; a redirect/401 response to refuse it."""
    if board_role() is not None:
        return None
    if request.path.startswith("/api/"):
        return jsonify(error="auth required"), 401
    return redirect("/login")


def actor() -> str:
    """The audit-log actor for the current session (`admin` | `sklad` | `anon`)."""
    return board_role() or "anon"
