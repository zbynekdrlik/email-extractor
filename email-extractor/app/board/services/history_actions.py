"""História actions — „spustiť znova" + „zadané ručne" (#448 lane 7, spec §5).

The two safety-critical actions. Both go through the SANCTIONED engine paths and NEVER touch
an ORION ledger (`edi_sent`/`desadv_sent`) or upload anything — the whole point of the ticket
(memory `reprocess-safety`, #51: DB state alone is not proof nothing shipped; a duplicate
upload = a duplicate physical delivery that cannot be undone).

- `rerun` = the sanctioned message reset (`processed=false … attempts=0`, #431) — allowed ONLY
  when the document provably never uploaded: a busy claim, a non-`error` terminal state, an
  uploaded `edi_sent`/`desadv_sent` row, OR live ORION presence each refuse with a 409 + a
  clear reason. `error` is the only state safe without exception (reprocess-safety).
- `manual` = „zadané ručne" via `hold._resolve_one_manually` — a HOLD RELEASE WITHOUT SHIP,
  message-scoped (never a bare per-question release that could free a foreign mail's order).
"""
from __future__ import annotations

import logging

from ... import db
from ...orders import report
from . import audit, history

log = logging.getLogger("board.history_actions")

# proc_status values that PROVE nothing safe-to-rerun. Per reprocess-safety (#51):
#   ok/partial  -> shipped;  manual -> hand-entered into CODEX;
#   review/held/not_warehouse/sklad_unknown -> a human may have been INSTRUCTED to act, or a
#   shipped sibling hides behind the aggregate status.
# Only `error` is safe without exception.
_RERUN_REFUSE_REASON: dict[str, str] = {
    "ok": "doklad už odišiel do ORIONu — spustiť znova by ho poslalo druhýkrát",
    "partial": "doklad je čiastočne odoslaný do ORIONu — spustiť znova by poslalo duplikát",
    "manual": "doklad bol vyriešený ručne — spustiť znova sa nedá",
    "review": "doklad je na kontrole — sklad ho mohol zadať ručne; spustiť znova nie je bezpečné",
    "held": "doklad ešte čaká — spustiť znova nie je namieste (rieš cez otázky/„zadané ručne“)",
    "not_warehouse": "doklad je uzavretý ako „netýka sa skladu“ — spustiť znova sa nedá",
    "sklad_unknown": "doklad je odložený skladom — spustiť znova sa nedá",
}


def _latest_result(conn, message_id: str) -> dict:
    r = conn.execute(
        "SELECT result FROM order_runs WHERE message_id = %s AND shadow = false "
        "ORDER BY id DESC LIMIT 1", (message_id,)).fetchone()
    return (r[0] or {}) if r else {}


def _ledger_uploaded(conn, scope: str, message_id: str) -> bool:
    """True if a CONFIRMED (uploaded_at IS NOT NULL) ORION ledger row exists for this
    document — the DB half of the never-ship guard (the ORION SFTP read is the other half)."""
    result = _latest_result(conn, message_id)
    if scope == "dl":
        for d in (result.get("documents") or []):
            ean, doc = d.get("supplier_ean"), str(d.get("doc_number") or "")
            if ean and doc and conn.execute(
                    "SELECT 1 FROM desadv_sent WHERE supplier_ean = %s AND doc_number = %s "
                    "AND uploaded_at IS NOT NULL", (ean, doc)).fetchone():
                return True
        return False
    edi_file = _msg_edi_file(conn, message_id)
    # #51/#239: check EVERY recoverable EDI filename, not only messages.edi_file (NULL on an
    # upload-failure error state whose bytes may still have landed).
    for name in history.orders_edi_names(result, edi_file):
        if conn.execute(
                "SELECT 1 FROM edi_sent WHERE filename = %s AND uploaded_at IS NOT NULL",
                (name,)).fetchone():
            return True
    ean, dd = result.get("customer_ean"), result.get("delivery_date")
    if ean and dd and conn.execute(
            "SELECT 1 FROM edi_sent WHERE customer_ean = %s AND delivery_date = %s "
            "AND uploaded_at IS NOT NULL", (ean, dd)).fetchone():
        return True
    return False


def _msg_edi_file(conn, message_id: str) -> str:
    r = conn.execute("SELECT edi_file FROM messages WHERE message_id = %s",
                     (message_id,)).fetchone()
    return (r[0] or "") if r else ""


class HistoryActionError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def rerun_precheck(conn, scope: str, message_id: str) -> tuple[bool, str]:
    """DB-only verdict (no ORION SFTP, no busy check — the endpoint does those). Used for the
    detail-view hint AND as the endpoint's first gate."""
    row = conn.execute("SELECT proc_status FROM messages WHERE message_id = %s",
                       (message_id,)).fetchone()
    if not row:
        return False, "doklad neexistuje"
    ps = row[0] or ""
    if ps in _RERUN_REFUSE_REASON:
        return False, _RERUN_REFUSE_REASON[ps]
    if _ledger_uploaded(conn, scope, message_id):
        return False, "v evidencii je nahraný doklad do ORIONu — spustiť znova sa nedá"
    if ps != "error":
        return False, f"stav „{ps or 'bez výsledku'}“ nie je bezpečný na spustenie znova"
    return True, "doklad nič nenahral — spustiť znova je bezpečné"


def _orion_has_document(conn, cfg, scope: str, message_id: str) -> bool:
    """Live READ-ONLY ORION presence check. Only invoked when there is an identity to look
    for (a built EDI file / a document number); a failure to reach ORION is treated as
    PRESENT (fail-safe → refuse the rerun), never as absent."""
    from ...orders import desadv_edi
    from ...orders import upload as upload_mod
    result = _latest_result(conn, message_id)
    if scope == "dl":
        docs = [d for d in (result.get("documents") or [])
                if d.get("supplier_ean") and d.get("doc_number")]
        if not docs:
            return False
        try:
            dirs = upload_mod.list_dirs(cfg)
        except Exception:
            log.warning("ORION list_dirs failed for %s — refusing rerun (fail-safe)", message_id)
            return True
        return any(desadv_edi.already_landed(dirs, d["supplier_ean"], str(d["doc_number"]))
                   for d in docs)
    # #51/#239: the EDI filename is set in the run's order_results the moment the EDI is BUILT
    # — BEFORE the upload — so a failed upload whose bytes may have landed still has a
    # recoverable filename even though messages.edi_file (set only on a CONFIRMED upload) is
    # NULL. Check every recoverable name against ORION, never just messages.edi_file.
    names = history.orders_edi_names(result, _msg_edi_file(conn, message_id))
    if not names:
        # No EDI filename anywhere. If the run nonetheless intended to ship we cannot prove
        # absence → fail-safe refuse; otherwise no EDI was ever built → nothing to be present.
        if result.get("would_ship"):
            log.warning("no EDI name but would_ship for %s — refusing rerun (fail-safe)",
                        message_id)
            return True
        return False
    try:
        dirs = upload_mod.list_dirs(cfg)
    except Exception:
        log.warning("ORION list_dirs failed for %s — refusing rerun (fail-safe)", message_id)
        return True
    for folder in ("in", "archCodex", "unconfirmed"):
        for name in (dirs or {}).get(folder) or ():
            if any(desadv_edi.matches_wire_name(name, n) for n in names):
                return True
    return False


def rerun(conn, cfg, scope: str, message_id: str, actor: str) -> dict:
    """The sanctioned reset — only after EVERY never-ship guard passes. Raises
    `HistoryActionError` (404/409) otherwise; nothing is ever uploaded here."""
    row = conn.execute("SELECT id, proc_status FROM messages WHERE message_id = %s",
                       (message_id,)).fetchone()
    if not row:
        raise HistoryActionError(404, "doklad neexistuje")
    mid_int, ps = row
    ok, reason = rerun_precheck(conn, scope, message_id)
    if not ok:
        raise HistoryActionError(409, reason)
    if db.active_claim(conn, mid_int) is not None:
        raise HistoryActionError(409, "doklad sa práve spracúva — skús to o chvíľu")
    if _orion_has_document(conn, cfg, scope, message_id):
        raise HistoryActionError(409, "doklad je už v ORIONe — spustiť znova by ho poslalo druhýkrát")
    conn.execute(
        "UPDATE messages SET processed = false, processed_at = NULL, processed_by = NULL, "
        "processing_at = NULL, error = NULL, attempts = 0 WHERE id = %s", (mid_int,))
    wf = report.WORKFLOW if scope != "dl" else "dodacie_listy"
    report.log_event(conn, message_id, stage="requeued", status="ok",
                     outcome="Spustené znova z histórie (sklad/admin)", rollup=False,
                     workflow=wf)
    audit.record(conn, actor=actor, table="messages", row_id=mid_int, action="rerun",
                 message_id=message_id, before={"proc_status": ps}, after={"proc_status": None})
    return {"ok": True}


def manual_precheck(conn, scope: str, message_id: str) -> tuple[bool, str]:
    if scope == "dl":
        return False, "pre dodacie listy nie je „zadané ručne“ cez históriu — rieš cez Otázky sklad"
    n = conn.execute("SELECT count(*) FROM held_orders WHERE message_id = %s AND status = 'held'",
                    (message_id,)).fetchone()[0]
    if not n:
        return False, "žiadna zadržaná objednávka na označenie ako zadané ručne"
    return True, f"{n} zadržaných objednávok — dá sa označiť ako zadané ručne (nič sa neodošle)"


def manual(conn, cfg, scope: str, message_id: str, actor: str) -> dict:
    """„Zadané ručne": release every HELD order of THIS message WITHOUT any ORION upload, via
    the sanctioned `hold._resolve_one_manually` (message-scoped by construction). Audited."""
    ok, reason = manual_precheck(conn, scope, message_id)
    if not ok:
        raise HistoryActionError(409, reason)
    from ...orders import hold
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM held_orders WHERE message_id = %s AND status = 'held'",
        (message_id,)).fetchall()]
    resolved = 0
    for hid in ids:
        res = hold._resolve_one_manually(
            conn, cfg, hid, post=lambda c, html, **kw: report.post_from_config(c, html))
        if res:
            resolved += 1
            audit.record(conn, actor=actor, table="held_orders", row_id=hid, action="manual",
                         message_id=message_id, before={"status": "held"},
                         after={"status": "manual"})
    if not resolved:
        raise HistoryActionError(409, "objednávky sa medzičasom uvoľnili — nič sa neoznačilo")
    return {"ok": True, "resolved": resolved}
