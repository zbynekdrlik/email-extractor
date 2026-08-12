"""Pure helpers + shared constants used across the HTTP API surface (#268 krok 2).

No Flask, no DB — these are leaf-level string/date helpers so `httpapi.py` and every
future split module can import them without pulling in the whole app. Moved VERBATIM
out of `httpapi.py` (no behavior change) — see the design comment on #268 for exactly
what moved and why.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date

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
