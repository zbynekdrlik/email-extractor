"""Questions service for the unified nástenka lane 2 (#443, spec §4).

Logic + SQL for the two question tabs (Otázky sklad = DL, Otázky objednávky = orders).
Everything here is a THIN pohľad over machinery that already exists — it CALLS
`orders.teach` (list/reopen helpers) and `orders.hold` (put a held order back), never
copies their logic. The answer/undo dispatch is NOT here at all: the route delegates to
the exact callables `httpapi_orders_questions.register` returned. What IS genuinely new:

  * `list_questions` — one scope-filtered, status-bucketed, searchable read over the three
    existing teach getters (open / expired / answered).
  * `reopen` — bring an auto-EXPIRED question (#341) back: `status='open'` + the held order
    back to `held` (via `hold.reopen_expired`) + an `audit_log` `reopen` row.
  * the original PREVIEW (subject/from/date + attachment list + /files, /eml links) and its
    SCOPE GUARD: the board serves an original only for a message that actually CARRIES a
    question, so the warehouse role reaches an attachment WITHOUT widening the legacy
    token-only `/files`/`/eml` gate.
"""
from __future__ import annotations

from urllib.parse import quote

from ...httpapi_common import _fold
from ...httpapi_security import DL_KINDS, ORDERS_KINDS
from ...orders import hold, teach
from . import audit

# spec §3/§6: the TAB decides scope, and the kind partition is owned by httpapi_security
# (its own import-time completeness assert) — never re-derived here.
SCOPE_KINDS: dict[str, tuple[str, ...]] = {"orders": ORDERS_KINDS, "dl": DL_KINDS}
STATUSES = ("open", "expired", "answered")


def list_questions(conn, *, scope: str, status: str = "open", q: str = "") -> list[dict]:
    """Questions for one tab, one status bucket, optionally search-filtered.

    Raises `ValueError` for an unknown scope/status (the route turns it into a 400)."""
    if scope not in SCOPE_KINDS:
        raise ValueError(f"neznámy scope {scope!r}")
    if status not in STATUSES:
        raise ValueError(f"neznámy status {status!r}")
    kinds = SCOPE_KINDS[scope]
    if status == "open":
        rows = teach.open_questions(conn, limit=500, kinds=kinds)
    elif status == "expired":
        rows = teach.expired_questions(conn, limit=500, kinds=kinds)
    else:  # answered
        rows = teach.recently_taught(conn, limit=500, kinds=kinds)
    if q:
        needle = _fold(q)
        rows = [r for r in rows if _matches(r, needle)]
    return rows


def _matches(row: dict, needle: str) -> bool:
    hay = _fold(" ".join(str(row.get(k) or "") for k in
                         ("wording", "customer_name", "customer_ean", "message_id")))
    return needle in hay


def reopen(conn, cfg, qid: int, actor: str) -> dict | None:
    """„Znovu otvoriť" an EXPIRED question (spec §5). Returns the reopened question dict, or
    a `{"error": ...}` sentinel the route maps to a 4xx, or `None` when the question is
    unknown (→ 404)."""
    qrow = teach.get(conn, qid)
    if not qrow:
        return None
    if qrow.get("status") != "expired":
        return {"error": "not_expired"}
    # atomic guard: only flip a row that is STILL expired (a concurrent reopen/answer loses).
    flipped = conn.execute(
        """UPDATE order_questions
              SET status = 'open', answer = NULL, answer_gtin = NULL, answer_card = NULL,
                  answered_by = NULL, answered_at = NULL, reminder_sent_at = NULL,
                  escalated_at = NULL
            WHERE id = %s AND status = 'expired'
            RETURNING id""", (qid,)).fetchone()
    if not flipped:
        return {"error": "not_expired"}
    # put any held order this question was gating back to 'held' (a DL/no-hold question
    # matches nothing → no-op) so a later answer ships from its stored decisions.
    hold.reopen_expired(conn, qid)
    audit.record(conn, actor=actor, table="order_questions", row_id=qid, action="reopen",
                 question_id=qid, message_id=qrow.get("message_id"),
                 before={"status": "expired"}, after={"status": "open"})
    return {"question": teach.get(conn, qid)}


# --- original preview + scope-guarded file access ----------------------------------

def message_has_question(conn, message_id: str) -> bool:
    """The scope guard: the board serves an original ONLY for a message that carries a
    question — so a warehouse session never reaches an arbitrary mail's attachment."""
    return conn.execute(
        "SELECT 1 FROM order_questions WHERE message_id = %s LIMIT 1",
        (message_id,)).fetchone() is not None


def preview(conn, data_dir, qid: int) -> dict | None:
    """The original mail beside a question: subject/from/date + attachment list + board
    (scope-guarded) /files and /eml links. `None` when the question is unknown (→ 404)."""
    from ...store import message_dir
    qrow = teach.get(conn, qid)
    if not qrow:
        return None
    mid = qrow.get("message_id") or ""
    m = conn.execute(
        "SELECT subject, from_addr, from_name, sent_at, created_at "
        "FROM messages WHERE message_id = %s", (mid,)).fetchone()
    subject, from_addr, from_name, sent_at, created_at = m if m else ("", "", "", "", None)
    atts = conn.execute(
        "SELECT idx, filename, mime FROM attachments WHERE message_id = %s ORDER BY idx",
        (mid,)).fetchall()
    enc = quote(mid, safe="")
    attachments = [{"idx": a[0], "filename": a[1] or "", "mime": a[2] or "",
                    "url": f"/api/board/files/{enc}/{a[0]}"} for a in atts]
    eml_exists = (message_dir(str(data_dir), mid) / "raw.eml").exists()
    return {
        "question_id": qrow["id"], "message_id": mid,
        "subject": subject or "", "from_addr": from_addr or "", "from_name": from_name or "",
        "sent_at": sent_at or "",
        "created_at": created_at.isoformat() if created_at else None,
        "attachments": attachments,
        "eml_url": f"/api/board/eml/{enc}" if eml_exists else None,
    }


def file_path(conn, data_dir, message_id: str, idx: int):
    """Resolve one attachment's on-disk path, but ONLY for a message that carries a
    question (scope guard). `None` → the route 404s (never leaks another mail's file)."""
    from ...store import message_dir
    if not message_has_question(conn, message_id):
        return None
    matches = sorted(message_dir(str(data_dir), message_id).glob(f"att{idx}__*"))
    return matches[0] if matches else None


def eml_path(conn, data_dir, message_id: str):
    """The raw .eml path, scope-guarded exactly like `file_path`."""
    from ...store import message_dir
    if not message_has_question(conn, message_id):
        return None
    p = message_dir(str(data_dir), message_id) / "raw.eml"
    return p if p.exists() else None
