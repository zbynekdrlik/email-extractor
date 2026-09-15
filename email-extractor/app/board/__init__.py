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

# #443 lane 2: each tab may fill in its own content template + JS module; unfilled tabs keep
# the lane-1 placeholder. `scope` (orders|dl) is what the question tabs pass to the board API.
# (slug -> (content_template, tab_script, scope|None)). Only the two question tabs are wired
# in lane 2 — later lanes add their own rows here.
_TAB_CONTENT: dict[str, tuple[str, str, str | None]] = {
    "otazky-objednavky": ("board/questions.html", "/static/board/tab-questions.js", "orders"),
    "otazky-sklad": ("board/questions.html", "/static/board/tab-questions.js", "dl"),
    # #445 lane 4: the two product tabs — one template + one JS, scope from the tab.
    "produkty-objednavky": ("board/products.html", "/static/board/tab-products.js", "orders"),
    "produkty-sklad": ("board/products.html", "/static/board/tab-products.js", "dl"),
}


def register_board(app, deps, questions_api=None) -> None:
    # `deps` carries the shared DB/cfg the tab SERVICES use; `questions_api` is the
    # answer/undo dispatch that `httpapi_orders_questions.register` returned, so the board
    # DELEGATES to the exact same logic instead of duplicating it (#443, spec §3).
    bp = Blueprint("board", __name__)

    def _render(active: str):
        role = board_role()
        tabs = [{"slug": s, "label": label, "url": f"/nastenka/{s}", "active": s == active}
                for s, label in TABS]
        content_template, tab_script, scope = _TAB_CONTENT.get(
            active, ("board/_placeholder.html", None, None))
        return render_template(
            "board/layout.html",
            version=__version__, tabs=tabs, active_tab=active,
            active_label=dict(TABS).get(active, ""),
            role=role, is_admin=(role == ADMIN),
            content_template=content_template, tab_script=tab_script, tab_scope=scope,
        )

    @bp.get("/nastenka")
    def board_home():
        return _render(DEFAULT_TAB)

    @bp.get("/nastenka/<tab>")
    def board_tab(tab: str):
        if tab not in _SLUGS:
            abort(404)
        return _render(tab)

    @bp.get("/api/board/ping")
    def board_ping():
        return jsonify(ok=True, version=__version__, role=board_role())

    # #443 lane 2: the Otázky sklad + Otázky objednávky API (list/filter/search, answer/undo
    # delegated to `questions_api`, reopen, scope-guarded original preview) — a thin route
    # layer over `services/questions.py`. Registered on the SAME single board blueprint.
    from . import questions_orders
    questions_orders.register(bp, deps, questions_api)

    # #445 lane 4: the Produkty sklad + Produkty objednávky API (list/search/paging,
    # create/update/delete soft+audit, per-card aliases) — a thin route layer over
    # `services/catalog.py` (which DELEGATES to snapshot/dl_snapshot + memory/dl_memory).
    from . import products_orders
    products_orders.register(bp, deps)

    app.register_blueprint(bp)
