"""The unified nástenka blueprint (#442 lane 1, spec §3).

`register_board(app, deps)` mounts a Flask blueprint owning `/nastenka*` (the tabbed page)
and `/api/board/*` (its JSON API — lane 1 exposes only a `ping` health probe). This is the
ONE place the app uses a blueprint (the older split modules use `register(app, deps)`) — a
deliberate, owner-approved choice for the new subsystem so its templates + static assets live
in files, not as HTML strings in Python (spec §1/§3). Templates render from
`app/templates/board/` and static assets serve from `app/static/board/` via Flask's own
defaults (`Flask(__name__)` roots both at the `app/` package), so the add-on's
`COPY app/ ./app/` ships them with no Dockerfile change.

The tabs are lane-1 PLACEHOLDERS ("Pripravujeme") — each later lane fills in its own tab. The
gate for every board path lives in `board/auth.py` and is delegated to from `httpapi._gate`.
"""
from __future__ import annotations

from flask import Blueprint, abort, jsonify, render_template

from .. import __version__
from .auth import ADMIN, board_role

# (slug, label) in display order. Spec §4 — the sklad sees all of these; admin additionally
# gets a "Maily" tab (the current dashboard at `/`), added in the template, not this list.
TABS: list[tuple[str, str]] = [
    ("otazky-sklad", "Otázky sklad"),
    ("otazky-objednavky", "Otázky objednávky"),
    ("produkty-sklad", "Produkty sklad"),
    ("produkty-objednavky", "Produkty objednávky"),
    ("naucene-sklad", "Naučené sklad"),
    ("naucene-objednavky", "Naučené objednávky"),
    ("zakaznici", "Zákazníci"),
    ("dodavatelia", "Dodávatelia"),
    ("historia-objednavok", "História objednávok"),
    ("historia-dl", "História dodacích listov"),
    ("kos", "Kôš"),
]
_SLUGS = frozenset(s for s, _ in TABS)
DEFAULT_TAB = TABS[0][0]


def render_board(active: str, *, tab_template: str | None = None):
    """Render the tabbed layout with `active` selected. A lane fills in its own tab by passing
    `tab_template` (a Jinja partial included inside `<main>`); with none, `<main>` shows the
    "Pripravujeme…" placeholder. Module-level (not a `register_board` closure) so each lane's
    own route module (`trash.py`, later `questions_*.py`) can import and call it — one place
    builds the tab bar + version, every tab reuses it. Runs inside a request (uses the session
    for `board_role()`)."""
    role = board_role()
    tabs = [{"slug": s, "label": label, "url": f"/nastenka/{s}", "active": s == active}
            for s, label in TABS]
    return render_template(
        "board/layout.html",
        version=__version__, tabs=tabs, active_tab=active,
        active_label=dict(TABS).get(active, ""),
        role=role, is_admin=(role == ADMIN), tab_template=tab_template,
    )


def register_board(app, deps) -> None:
    # `deps` is accepted for consistency with the other register() modules and is threaded to
    # each lane's own route registrar (`register_trash` etc.) for its service DB access.
    bp = Blueprint("board", __name__)

    @bp.get("/nastenka")
    def board_home():
        return render_board(DEFAULT_TAB)

    @bp.get("/nastenka/<tab>")
    def board_tab(tab: str):
        if tab not in _SLUGS:
            abort(404)
        return render_board(tab)

    @bp.get("/api/board/ping")
    def board_ping():
        return jsonify(ok=True, version=__version__, role=board_role())

    # Lane 3 (#444): the Kôš tab page + its audit list/restore API. Registered on the SAME
    # blueprint; its specific `/nastenka/kos` GET rule out-ranks the generic `/nastenka/<tab>`
    # above (a lane fills in one tab, the rest stay placeholders — spec §8 rollout model).
    from .trash import register_trash
    register_trash(bp, deps)

    app.register_blueprint(bp)
