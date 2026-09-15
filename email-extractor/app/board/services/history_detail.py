"""História detail — one document's items + match trace + timeline + original (#448 lane 7).

A THIN read: the latest NON-shadow `order_runs` for the message gives the email-level partner
+ doc numbers (`result`) and, via `order_items`, the per-item match trace (matched card,
rule, confidence) — written UNMODIFIED by BOTH engines (#200), so this is uniform for orders
and DL. The `email_events` timeline and the attachment list (with board-gated, history-scoped
preview links) come straight from their tables. The rerun/manual availability shown here is a
DB-only HINT (`history_actions.*_precheck`); the action ENDPOINTS re-check authoritatively
(incl. the live ORION presence read) before doing anything.
"""
from __future__ import annotations

from urllib.parse import quote

from . import history, history_actions


def _num(v):
    return float(v) if v is not None else None


def document_detail(conn, data_dir, scope: str, message_id: str) -> dict | None:
    """The full detail for one history document, or `None` when the message does not exist or
    is not a document of this scope (→ 404). Raises `ValueError` (→ 400) for an unknown scope."""
    cats = history.scope_categories(scope)
    m = conn.execute(
        "SELECT id, message_id, subject, from_addr, from_name, sent_at, created_at, "
        "category, proc_status, proc_outcome, edi_file, orion_path "
        "FROM messages WHERE message_id = %s", (message_id,)).fetchone()
    if not m or m[7] not in cats:
        return None
    run = conn.execute(
        "SELECT id, result FROM order_runs WHERE message_id = %s AND shadow = false "
        "ORDER BY id DESC LIMIT 1", (message_id,)).fetchone()
    result = (run[1] or {}) if run else {}
    items = []
    if run:
        rows = conn.execute(
            "SELECT name, quantity, unit, gtin, card, confidence, rule, trace "
            "FROM order_items WHERE run_id = %s ORDER BY id", (run[0],)).fetchall()
        items = [{"name": r[0] or "", "quantity": _num(r[1]), "unit": r[2] or "",
                  "gtin": r[3] or "", "card": r[4] or "", "confidence": _num(r[5]),
                  "rule": r[6] or "", "trace": r[7] or {}} for r in rows]
    name, ean, numbers = history.partner_and_docs(scope, result, m[4] or "")
    events = conn.execute(
        "SELECT ts, workflow, stage, status, outcome, detail FROM email_events "
        "WHERE message_id = %s ORDER BY ts, id", (message_id,)).fetchall()
    enc = quote(message_id, safe="")
    atts = conn.execute(
        "SELECT idx, filename, mime FROM attachments WHERE message_id = %s ORDER BY idx",
        (message_id,)).fetchall()
    attachments = [{"idx": a[0], "filename": a[1] or "", "mime": a[2] or "",
                    "url": f"/api/board/history/{enc}/files/{a[0]}"} for a in atts]
    from ...store import message_dir
    eml_exists = (message_dir(str(data_dir), message_id) / "raw.eml").exists()
    rerun_ok, rerun_reason = history_actions.rerun_precheck(conn, scope, message_id)
    manual_ok, manual_reason = history_actions.manual_precheck(conn, scope, message_id)
    return {
        "id": m[0], "message_id": m[1], "scope": scope,
        "subject": m[2] or "", "from_addr": m[3] or "", "from_name": m[4] or "",
        "sent_at": m[5] or "", "date": m[6].isoformat() if m[6] else None,
        "category": m[7], "proc_status": m[8], "status_label": history.status_label(m[8]),
        "outcome": m[9] or "", "edi_file": m[10] or "", "orion_path": m[11] or "",
        "partner": {"name": name, "ean": ean}, "doc_numbers": numbers,
        "items": items,
        "events": [{"ts": e[0].isoformat() if e[0] else None, "workflow": e[1],
                    "stage": e[2], "status": e[3], "outcome": e[4] or "",
                    "detail": e[5] or {}} for e in events],
        "attachments": attachments,
        "eml_url": f"/api/board/history/{enc}/eml" if eml_exists else None,
        "rerun": {"allowed": rerun_ok, "reason": rerun_reason},
        "manual": {"allowed": manual_ok, "reason": manual_reason},
    }


def file_path(conn, data_dir, scope: str, message_id: str, idx: int):
    """One attachment's on-disk path, but ONLY for a message that is a history document of
    this scope (the scope guard). `None` → the route 404s (never leaks another mail's file)."""
    from ...store import message_dir
    if not history.is_history_document(conn, message_id, scope):
        return None
    matches = sorted(message_dir(str(data_dir), message_id).glob(f"att{idx}__*"))
    return matches[0] if matches else None


def eml_path(conn, data_dir, scope: str, message_id: str):
    """The raw .eml path, history-scoped exactly like `file_path`."""
    from ...store import message_dir
    if not history.is_history_document(conn, message_id, scope):
        return None
    p = message_dir(str(data_dir), message_id) / "raw.eml"
    return p if p.exists() else None
