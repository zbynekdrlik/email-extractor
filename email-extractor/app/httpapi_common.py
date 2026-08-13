"""Pure helpers + shared constants + the `Deps` carrier used across the HTTP API
surface (#268 kroky 2, 5).

No Flask, no DB — these are leaf-level string/date helpers (plus, since krok 5, one
small dataclass) so `httpapi.py` and every future split module can import them without
pulling in the whole app, and without risking a circular import (every `register(app,
deps)` route module needs `Deps`, and `httpapi.py` itself builds one and imports every
route module — this module sits BELOW all of them). The string/date helpers were moved
VERBATIM out of `httpapi.py` (no behavior change) — see the design comment on #268 for
exactly what moved and why.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

CATEGORIES = ["ai_orders", "invoices", "reklamacie", "dodacie_listy",
              "static_orders", "human_processing", "no_processing"]
PROBLEM_TYPES = ["mis_sorted", "mis_processed", "other"]
FIX_STATUSES = ["open", "in_progress", "fixed", "wontfix"]


def _valid_date(s: str) -> bool:
    """True iff s is a real ISO date (YYYY-MM-DD); rejects bad months/days."""
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


def _escape_like(s: str) -> str:
    """Escape LIKE/ILIKE metacharacters so user input is a literal substring."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# #234: the exact same EAN normalization/validation `snapshot.normalize_ean` uses,
# duplicated here (not imported) so both HTTP entry points can return their own precise
# 400 body BEFORE ever calling into the DB layer.
_EAN_STRIP_RE = re.compile(r"[\s\-]")


def _fold(s: str) -> str:
    """Diacritics- and case-insensitive substring match for the /znalosti card/customer
    search — a warehouse worker types "rozok" and must still find "Rožok"."""
    return "".join(c for c in unicodedata.normalize("NFD", str(s or "").lower())
                   if unicodedata.category(c) != "Mn")


def _parse_emails_field(v) -> list[str]:
    """Accept either a JSON list of email strings or a comma-separated string.

    #268: moved here VERBATIM from a nested closure inside `create_app()` (was defined
    at ~line 1180 but called at ~617/~759 — worked only because everything was a
    closure in one giant function). As a plain top-level function it needs no such
    ordering — `httpapi.py` imports it once and every route that needs it resolves it
    the normal way, via module scope.
    """
    if isinstance(v, list):
        return [str(e).strip() for e in v if str(e).strip()]
    return [e.strip() for e in str(v or "").split(",") if e.strip()]


@dataclass
class Deps:
    """What a split-out `register(app, deps)` route module may use from `create_app`
    (#268 krok 5) — never more. `db`/`db_tx` are the EXACT `_db`/`_db_tx` closures
    `create_app` defines once (one pair of Postgres connection factories, shared by
    every split module — never redefined per module); `cfg` is the raw `Config`
    object, for anything a route needs off it directly (`cfg.api_token`,
    `cfg.orders_spend_cap_eur`, ...); `data_dir` is `create_app`'s own already-resolved
    `Path(cfg.data_dir)`.

    Deliberately loosely typed (`Any` / a bare `Callable`) — this keeps the module
    leaf-level (no Flask, no DB import) precisely so every split module, INCLUDING
    `httpapi.py` itself, can import `Deps` with zero circular-import risk.
    """
    cfg: Any
    db: Callable[[], Any]
    db_tx: Callable[[], Any]
    data_dir: Path
