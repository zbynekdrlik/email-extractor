"""#465 — the `dl_item` board-question side of a DL memory CONFLICT.

`dl_match.decide_item` refuses a silent R73 memory rescue on an ambiguous history (conflicting
human answers, a newer contrary ship majority, zero lexical overlap item↔card) and leaves the
line unmatched with both candidate cards; `dl_document` then asks ONE `dl_item` question flagged
`payload.memory_conflict` (`teach.ask_dl_item(memory_conflict=True)`) and the #365 hold holds
the document. This module owns the two question-row operations that flag needs, kept out of
the already-oversized `teach.py`:

- `flag_question` — the ask may DEDUPE onto an already-open PLAIN question for the same
  (supplier, wording); upgrade it so its answer supersedes the misclick and counts as the
  sklad's confirmation (`dl_memory._board_confirmed`).
- `undo_answer` — undoing a conflict answer removes only what THAT answer taught and restores
  the human answers it superseded (`dl_memory.restore_superseded`, via the Kôš's own restore).

The memory side (verdict, supersede, restore) lives in `dl_memory`.
"""
from __future__ import annotations

import logging

from psycopg.types.json import Json

from . import dl_memory, memory

log = logging.getLogger("orders.teach")


def flag_question(conn, qid: int, options: list[dict]) -> None:
    """Flag an OPEN `dl_item` question `memory_conflict` and put the conflicting cards
    (`options`, already conflict-first) ahead of its old candidates, which are kept. A no-op
    on a row that is already flagged (the fresh-insert case) or no longer open."""
    row = conn.execute("SELECT payload, candidates FROM order_questions "
                       "WHERE id = %s AND status = 'open'", (qid,)).fetchone()
    if not row or (row[0] or {}).get("memory_conflict"):
        return
    seen = {str(o["value"]) for o in options}
    merged = options + [c for c in (row[1] or []) if str(c.get("value")) not in seen]
    conn.execute("UPDATE order_questions SET payload = payload || %s::jsonb, candidates = %s "
                 "WHERE id = %s AND status = 'open'",
                 (Json({"memory_conflict": True}), Json(merged), qid))
    log.warning("dl_item question %s upgraded to a memory conflict (candidates %s)",
                qid, [str(c.get("value")) for c in merged])


def undo_answer(conn, q: dict) -> None:
    """Remove the human answer this conflict question taught — only rows created since it was
    answered (the older human answers predate the question and stay), never a soft-deleted
    (Kôš) row — then restore every answer it superseded. The caller reopens the question."""
    payload = q.get("payload") or {}
    removed = conn.execute(
        "DELETE FROM dl_item_memory WHERE supplier_ean = %s AND item_key = %s "
        "AND source = 'human' AND deleted_at IS NULL AND created_at >= %s RETURNING id",
        (payload.get("supplier_ean", ""), memory.item_key(q.get("wording", "")),
         q.get("answered_at"))).fetchall()
    restored = dl_memory.restore_superseded(conn, q["id"])
    log.info("dl_item conflict answer %s undone: removed %s, restored %s", q["id"],
             [int(r[0]) for r in removed], restored)
