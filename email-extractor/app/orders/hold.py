"""Hold an order while its question is unanswered — but only until the delivery date (#93).

Shipping the matched part of an order now and the taught line later would write TWO ORION
documents for ONE delivery day — the exact defect fixed in #81.1 (40 and 10 delivered instead
of 50). So a pending question (#88's teach-once loop) holds its WHOLE order: nothing is built,
nothing uploaded, until either the question is answered or the delivery date arrives.

Two release paths, both funnelled through the SAME `pipeline._ship_one` / `edi.claim_send`
ledger the live pipeline already ships through, so a late answer arriving after the deadline
has already shipped can never double-upload:

- `release_for_question` — fires right after `teach.answer` settles a question. Only once
  EVERY question id a held order is waiting on has been answered does it re-check its stored
  decisions against fresh memory (no LLM call — `match.decide_without_model` again) and ship.
- `release_due` — the deadline backstop, a periodic sweep. Ships whatever matched using the
  ORIGINALLY stored decisions, unchanged — today's "ship what matched, name what's missing"
  behaviour, now gated on the delivery date instead of firing immediately.

`worker._claim` excludes any message with an open (`status='held'`) row here, so a held
message is never silently re-run through the LLM while it waits.
"""

# #424: hold.py split into concern modules by responsibility
# (module-split-refactor.md facade + re-export). This module is the thin FACADE —
# every public name AND every private helper reached via `hold.X` (incl. by tests)
# is re-exported here so NO caller changes.
from __future__ import annotations

from .hold_close import (  # noqa: F401 (facade re-export)
    _do_release,
    _do_release_locked,
    _has_non_shippable_open_question,
    _release_locked,
    _resolve_one_manually,
    close_expired_holds,
    release_due,
    release_for_question,
    release_to_review,
    release_unknown_customer,
    reopen_expired,
    resolve_manually,
    retry_unknown_customer_questions,
    set_customer,
    set_delivery_date,
    unresolve_manually,
)
from .hold_place import (  # noqa: F401 (facade re-export)
    _COLS,
    _apply_confirmed_quantities,
    _db_today,
    _dump_decisions,
    _load_decisions,
    _row,
    get,
    has_open,
    is_past_deadline,
    list_held,
    log,
    place,
)
from .hold_redecide import (  # noqa: F401 (facade re-export)
    _ask_still_ambiguous,
    _current_catalog,
    _mark_message_done_if_clear,
    _post_still_held,
    _redecide,
    _ship,
)
