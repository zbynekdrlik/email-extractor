"""A delivery note the warehouse typed into CODEX BY HAND while our DESADV still waits in ORION
`in_DL` (#494) — the terminal import status `manual`.

Live incident (13.10.): CODEX would not take four DESADVs of one supplier (one card they all
shared), so the warehouse typed the four DLs in by hand. Our files stayed in `in_DL`, and
`confirm.sweep` — which knows only archCodex = imported, unconfirmed = failed, otherwise
waiting — kept them in the morning carryover reminder („treba ich prijať v CODEXe"). Following
it would book every delivery a SECOND time the day CODEX takes the files. Deleting them was no
way out either: a file gone from every watched folder is the `unknown` alarm („zmizol zo
všetkých priečinkov").

The signal is the CODEX receipts read-model (`codex_receipts`, #485): a receipt of the SAME
supplier (`supplier_eans @> [supplier_ean]`) carrying the SAME DL number (`invoice_dedup.digits`
— digits only, leading zeros dropped, at least 5 digits; CODEX stores `0100304144` as
`100304144`) means the delivery is already in CODEX. Such a still-queued row gets
`import_status = 'manual'` (+ `import_confirmed_at`): terminal, so it leaves every carryover
alert (`_unresolved_rows` / `_pending_members`), a later disappearance of its file is never the
`unknown` alarm (a terminal row is never decided again), and its carryover incident closes like
any other (`_check_incidents_for_clear`). `confirm._activity_today_at` reads only `imported`
rows — a manual mark is no proof that an import pass ran (#255).

Exactly ONE ops alert per file (`ALERT_KIND`, the ops channel — NEVER the warehouse thread):
delete the file from `in_DL`, or the next morning import takes it a second time. It is written
in the SAME transaction as the guarded status change (`WHERE import_status IS NULL`), so it can
neither be lost nor repeated. The app never touches the file on ORION (the design rejected an
automatic delete outside the upload, #239) — a human deletes it on the alert.

No freshness gate: a receipt is evidence of PRESENCE, an older copy is still true. No copy / no
match / a failing lookup = behaviour unchanged (the row keeps waiting, logged). Only the DESADV
ledger: a receipt is a SUPPLIER delivery, an ORDER file to a customer never has one.
"""
from __future__ import annotations

import logging
from datetime import datetime
from html import escape

from . import codex_receipts, confirm_carryover, dl_alerts, invoice_dedup, report

log = logging.getLogger("orders.confirm_manual")

STATUS = "manual"
ALERT_KIND = "dl_manual_in_codex"
ALERT_KEY = "desadv-manual"            # + ":<desadv_sent id>" — one alert per file


def receipt_for(conn, row: dict) -> codex_receipts.Receipt | None:
    """The CODEX receipt of this DESADV row's supplier whose DL number is the row's DL number,
    or None. A number with fewer than `invoice_dedup.MIN_NUMBER_DIGITS` digits never matches
    (a short counter must not meet another document)."""
    number = invoice_dedup.digits(row.get("doc_number"))
    if not number:
        return None
    for receipt in codex_receipts.for_supplier(conn, str(row.get("supplier_ean") or "")):
        if number in invoice_dedup.numbers_of(receipt.dl_numbers):
            return receipt
    return None


def _line(row: dict, receipt: codex_receipts.Receipt, names: dict[str, str],
          wire_prefix: str) -> str:
    """One grouped-alert item line (`dl_alerts.GROUPED_ITEM_KINDS` carries the header):
    „• DL 0100304144 (Dodávateľ) — príjemka 261005071, súbor Z-DESADV_…"."""
    who = confirm_carryover.label(row, "desadv", names)
    return (f"<p>&#8226; {escape(who)} &mdash; príjemka {escape(receipt.receipt_number)}, "
            f"súbor <code>{escape(wire_prefix + row['filename'])}</code></p>")


def _names(conn) -> dict[str, str]:
    try:
        return confirm_carryover.display_names(conn, "desadv")
    except Exception:
        log.exception("manual-in-CODEX alert: supplier names could not be loaded — the alert "
                      "names the DL number only (fail-open)")
        return {}


def mark(conn, cfg, rows: list[dict], *, wire_prefix: str, now: datetime) -> set[int]:
    """Mark every given STILL-QUEUED DESADV row whose DL CODEX already has as `manual` and
    enqueue its one ops alert; returns the ids marked. Each row on its own: a failing lookup
    or write is logged and leaves that row waiting (unchanged behaviour), never the sweep."""
    marked: set[int] = set()
    names: dict[str, str] | None = None
    for row in rows:
        try:
            receipt = receipt_for(conn, row)
        except Exception:
            log.exception("manual-in-CODEX check: receipts lookup failed for desadv_sent #%s "
                          "(%s) — it keeps waiting (fail-open)", row.get("id"),
                          row.get("filename"))
            continue
        if receipt is None:
            log.debug("manual-in-CODEX check: no CODEX receipt for DL %s (desadv_sent #%s)",
                      row.get("doc_number"), row.get("id"))
            continue
        if names is None:
            names = _names(conn)
        body = _line(row, receipt, names, wire_prefix)
        channel = report.ops_channel(cfg)
        try:
            with conn.transaction():
                hit = conn.execute(
                    """UPDATE desadv_sent
                          SET import_status = %s, import_confirmed_at = %s,
                              import_checked_at = now()
                        WHERE id = %s AND import_status IS NULL RETURNING id""",
                    (STATUS, now, row["id"])).fetchone()
                if hit is not None:
                    dl_alerts.enqueue(conn, channel, ALERT_KIND, body,
                                      message_id=f"{ALERT_KEY}:{row['id']}")
        except Exception:
            log.exception("manual-in-CODEX: marking desadv_sent #%s failed — it keeps waiting",
                          row.get("id"))
            continue
        if hit is None:
            log.info("manual-in-CODEX: desadv_sent #%s already resolved meanwhile — no alert",
                     row["id"])
            continue
        marked.add(row["id"])
        log.warning("DL %s (supplier %s, desadv_sent #%s) is already in CODEX by hand (receipt "
                    "%s, CODEX DL %s) while %s%s still waits in ORION in_DL — import_status "
                    "'manual', ops alert enqueued to channel %s", row.get("doc_number"),
                    row.get("supplier_ean"), row["id"], receipt.receipt_number,
                    ", ".join(receipt.dl_numbers), wire_prefix, row["filename"], channel)
    return marked
