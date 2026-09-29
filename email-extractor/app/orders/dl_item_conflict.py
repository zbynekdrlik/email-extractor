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

#467 reuses the question-row half for a `codex_missing` question (the line's card has a code
CODEX has no stock card for): its answer supersedes the human answer that taught the dead code,
undo restores it, and a deduped older question is upgraded — `board_settled()` is the predicate
for "either flag", `flag_question(flag=...)` the one upgrade path (with `keep` dropping cards
CODEX lacks from the old candidate list). DELIBERATELY NOT shared: only a memory CONFLICT
answer is a standing confirmation in `dl_memory._board_confirmed` — a codex answer is a plain
list pick, so a misclick there is caught on the next delivery like any other.
"""
from __future__ import annotations

import logging

from psycopg.types.json import Json

from . import dl_memory, memory

log = logging.getLogger("orders.teach")


BOARD_SETTLED_FLAGS = ("memory_conflict", "codex_missing")


def board_settled(payload: dict | None) -> bool:
    """A `dl_item` question whose answer is the sklad's explicit, superseding decision."""
    return any((payload or {}).get(f) for f in BOARD_SETTLED_FLAGS)


def option(card: dict) -> dict:
    """A catalog card -> the stored `{value, label}` candidate; the card alias (#465) and a
    drifted card's CODEX name (#467) ride along for the board's lexical misclick check."""
    return {"value": str(card.get("gtin")), "label": card.get("name") or str(card.get("gtin")),
            **({"alias": card["doplnok"]} if card.get("doplnok") else {}),
            **({"codex_name": card["codex_name"]} if card.get("codex_name") else {})}


def flag_question(conn, qid: int, options: list[dict], reason: str = "",
                  flag: str = "memory_conflict", keep=None) -> None:
    """Flag an OPEN `dl_item` question (`flag`) and put `options` (already ordered) ahead of its
    old candidates — kept, except those `keep(value)` rejects (#467: cards CODEX lacks) — and
    show `reason` instead of the stale plain one. A no-op on a row that already carries the
    flag (the fresh-insert case) or is no longer open."""
    row = conn.execute("SELECT payload, candidates FROM order_questions "
                       "WHERE id = %s AND status = 'open'", (qid,)).fetchone()
    if not row or (row[0] or {}).get(flag):
        return
    seen = {str(o["value"]) for o in options}
    merged = options + [c for c in (row[1] or []) if str(c.get("value")) not in seen
                        and (keep is None or keep(str(c.get("value"))))]
    conn.execute("UPDATE order_questions SET payload = payload || %s::jsonb, candidates = %s, "
                 "reason = COALESCE(NULLIF(%s, ''), reason) WHERE id = %s AND status = 'open'",
                 (Json({flag: True}), Json(merged), reason or "", qid))
    log.warning("dl_item question %s upgraded (%s, candidates %s)",
                qid, flag, [str(c.get("value")) for c in merged])


def undo_answer(conn, q: dict) -> None:
    """Remove the human answer this conflict question taught — only rows of the ANSWERED card
    created since it was answered (older human answers predate the question and stay; a later
    answer for another card is not ours), never a soft-deleted (Kôš) row — then restore every
    answer it superseded. The caller reopens the question. Known residual: a same-day row the
    answer PROMOTED/REVIVED keeps its old created_at and stays taught — never a silent ship,
    the restored superseded answers re-open the conflict on the next delivery."""
    payload = q.get("payload") or {}
    choice = str((q.get("answer") or {}).get("choice") or "")
    removed = conn.execute(
        "DELETE FROM dl_item_memory WHERE supplier_ean = %s AND item_key = %s "
        "AND gtin = %s AND source = 'human' AND deleted_at IS NULL AND created_at >= %s "
        "RETURNING id",
        (payload.get("supplier_ean", ""), memory.item_key(q.get("wording", "")), choice,
         q.get("answered_at"))).fetchall()
    restored = dl_memory.restore_superseded(conn, q["id"])
    log.info("dl_item conflict answer %s undone: removed %s, restored %s", q["id"],
             [int(r[0]) for r in removed], restored)
