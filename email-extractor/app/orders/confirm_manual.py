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
— digits only, leading zeros dropped, at least 5 digits; CODEX stores numbers as DOUBLE, so a
printed `0100…` arrives without its zero) and booked no earlier than the row's delivery day
(`desadv_sent.delivery_date`, written by `record_facts` since #485) minus
`invoice_dedup.DATE_WINDOW_DAYS` means the delivery is already in CODEX. A receipt cannot
predate the delivery it books: an older one with the same digits is a reused counter or an
invoice number the warehouse typed into CODEX's DL field — never this delivery. A row without
the delivery fact is never marked (it keeps waiting). Such a still-queued row gets
`import_status = 'manual'` (+ `import_confirmed_at`): terminal, so it leaves every carryover
alert (`_unresolved_rows` / `_pending_members`), a later disappearance of its file is never the
`unknown` alarm (a terminal row is never decided again), and its carryover incident closes like
any other (`_check_incidents_for_clear`). `confirm._activity_today_at` reads only `imported`
rows — a manual mark is no proof that an import pass ran (#255).

Exactly ONE ops alert per file (`ALERT_KIND`, the ops channel — NEVER the warehouse thread):
check the receipt, delete the file from `in_DL`, or the next morning import takes it a second
time. It is written in the SAME transaction as the guarded status change (`WHERE import_status
IS NULL`), so a `manual` row never exists without its outbox row and never gets a second one;
the grouped post names every file (`dl_alerts.UNCAPPED_KINDS` — no „… a ešte N" cut). With no
ops channel configured the row is HELD and `dl_alerts.purge_held` drops it after
`HELD_RETENTION_DAYS` — the ops channel must be set (it is on prod). The app never touches the
file on ORION (the design rejected an automatic delete outside the upload, #239) — a human
deletes it on the alert.

No freshness gate: a receipt is evidence of PRESENCE, an older copy is still true. No copy / no
match / a failing lookup = behaviour unchanged (the row keeps waiting, logged). Only the DESADV
ledger: a receipt is a SUPPLIER delivery, an ORDER file to a customer never has one.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from . import codex_receipts, confirm_carryover, dl_alerts, invoice_dedup, report

log = logging.getLogger("orders.confirm_manual")

STATUS = "manual"
ALERT_KIND = "dl_manual_in_codex"
ALERT_KEY = "desadv-manual"            # + ":<desadv_sent id>" — one alert per file


def receipt_for(conn, row: dict, delivery: date | None) -> codex_receipts.Receipt | None:
    """The CODEX receipt of this DESADV row's supplier whose DL number is the row's DL number
    and that was booked no earlier than `delivery` - `DATE_WINDOW_DAYS`, or None. A number with
    fewer than `invoice_dedup.MIN_NUMBER_DIGITS` digits never matches (a short counter must not
    meet another document); no `delivery` = nothing to tie a receipt to = None."""
    number = invoice_dedup.digits(row.get("doc_number"))
    if not number or delivery is None:
        return None
    earliest = delivery - timedelta(days=invoice_dedup.DATE_WINDOW_DAYS)
    for receipt in codex_receipts.for_supplier(conn, str(row.get("supplier_ean") or "")):
        if number not in invoice_dedup.numbers_of(receipt.dl_numbers):
            continue
        if receipt.receipt_date_to < earliest:
            log.debug("manual-in-CODEX check: receipt %s carries DL %s but is dated %s, "
                      "before delivery %s — another document, not this one",
                      receipt.receipt_number, row.get("doc_number"), receipt.receipt_date_to,
                      delivery)
            continue
        return receipt
    return None


def _line(row: dict, receipt: codex_receipts.Receipt, names: dict[str, str],
          wire_prefix: str) -> str:
    """One grouped-alert item line (`dl_alerts.item_line`; the header with the instruction is
    `dl_alerts.GROUPED_ITEM_KINDS[ALERT_KIND]`): „• DL <číslo> (<dodávateľ>) — príjemka
    <číslo>, súbor Z-DESADV_…"."""
    return dl_alerts.item_line(
        confirm_carryover.label(row, "desadv", names),
        f"príjemka {receipt.receipt_number}, súbor {wire_prefix}{row['filename']}")


def _delivery_dates(conn, rows: list[dict]) -> dict[int, date | None]:
    """`desadv_sent.delivery_date` per row id — the delivery FACT `record_facts` writes right
    after the claim (#485); the sweep's own row dicts do not carry it."""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT id, delivery_date FROM desadv_sent WHERE id = ANY(%s)",
        ([r["id"] for r in rows],)).fetchall()}


def _supplier_names(conn) -> dict[str, str]:
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
    if not rows:
        return marked
    names: dict[str, str] | None = None
    try:
        deliveries = _delivery_dates(conn, rows)
    except Exception:
        log.exception("manual-in-CODEX check: delivery dates of %d waiting DESADV row(s) could "
                      "not be read — they keep waiting (fail-open)", len(rows))
        return marked
    for row in rows:
        delivery = deliveries.get(row["id"])
        if delivery is None:
            log.debug("manual-in-CODEX check: desadv_sent #%s has no delivery date — never "
                      "marked, it keeps waiting", row.get("id"))
            continue
        try:
            receipt = receipt_for(conn, row, delivery)
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
            names = _supplier_names(conn)
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
