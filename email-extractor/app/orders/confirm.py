"""Import CONFIRMATION (#151): uploading an EDI file to ORION's `in/` folder over SFTP is
NOT proof Communicator ever took it — only that the bytes now sit in a Windows directory.
This module answers the real question by listing the same folder read-only and
classifying every uploaded-but-unresolved `edi_sent` row.

**The signal is NOT the `Z-` filename prefix** (the ticket's own original proposal — see
the #151 design discussion). Live evidence: a file already sitting in `in/archCodex`
(therefore imported) went 5+ hours with no `Z-` prefix, while a batch job renamed OLDER
`archCodex` entries hours earlier. The rename is a separate, infrequent, uncontrolled job
— keying on it would report a safely-imported order as "unconfirmed" for hours, false-
alarming exactly the failure this module exists to catch honestly.

The real signal, read straight off the three folders Communicator actually uses:

  * present in `in/archCodex` (with OR without the `Z-` prefix) → IMPORTED.
  * present in `in/unconfirmed` → import FAILED — alert immediately, no timeout wait.
  * still only in `in/`, younger than the timeout → normal (Communicator sweeps roughly
    every 25-30 minutes) — say nothing, keep watching.
  * still only in `in/`, older than the timeout → NOT taken — alert.
  * gone from all three → deliberately treated as UNRESOLVED, alerted immediately, never
    as silent success. SFTP `listdir` is atomic against the Windows rename that moves a
    file `in -> archCodex` or `in -> unconfirmed`, so a file genuinely mid-transition is
    always found in exactly one of the three at any instant; being in NONE of them is
    outside that normal lifecycle (manual deletion/move) and cannot be confirmed as a
    successful import without evidence.

Every terminal state (`imported`/`failed`/`timeout`/`unknown`) permanently drops the row
out of `due_rows`' filter (`import_status IS NULL`) — so a file is alerted at most once,
with no separate "already alerted" bookkeeping needed.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from . import report
from . import upload as upload_mod

log = logging.getLogger("orders.confirm")

DEFAULT_TIMEOUT_MINUTES = 60
DEFAULT_INTERVAL_MINUTES = 5

# Terminal outcomes that need the warehouse alerted (never silently).
_ALERT_STATUSES = ("failed", "timeout", "unknown")


def _channel_for(filename: str, cfg) -> int:
    """`ORDER_*` is this add-on's own upload (`edi.filename`, #67); `DESADV_*` is the n8n
    "Dodacie Listy EDI" workflow's file landing in the SAME ORION folder — routed to the
    delivery-notes channel per the existing per-workflow channel split (never invented ad
    hoc). Falls back to `orders_channel_id` when `delivery_notes_channel_id` is unset, and
    for any other/unknown prefix — better a message in the wrong-but-configured channel
    than none at all."""
    if filename.startswith("DESADV_"):
        dn = int(getattr(cfg, "delivery_notes_channel_id", 0) or 0)
        if dn:
            return dn
    return int(getattr(cfg, "orders_channel_id", 0) or 0)


# --- selecting what to check ----------------------------------------------

def due_rows(conn, interval_minutes: int) -> list[dict]:
    """Every confirmed-uploaded, not-yet-resolved row not checked within the last
    `interval_minutes` — the per-row throttle that keeps a file waiting out the timeout
    from triggering an SFTP `listdir` (archCodex alone runs ~21k entries) on every worker
    tick."""
    rows = conn.execute(
        """SELECT id, customer_ean, delivery_date, filename, uploaded_at
             FROM edi_sent
            WHERE uploaded_at IS NOT NULL
              AND import_status IS NULL
              AND (import_checked_at IS NULL
                   OR import_checked_at < now() - make_interval(mins => %s))
            ORDER BY uploaded_at""",
        (max(1, int(interval_minutes or DEFAULT_INTERVAL_MINUTES)),)).fetchall()
    return [{"id": r[0], "customer_ean": r[1], "delivery_date": r[2], "filename": r[3] or "",
             "uploaded_at": r[4]} for r in rows]


def _decide(row: dict, dirs: dict, timeout_minutes: int, now: datetime) -> str | None:
    """Returns a terminal status, or None when the row is still legitimately pending
    (nothing to record or alert yet, only the throttle timestamp advances)."""
    name = row["filename"]
    if not name:
        return "unknown"          # nothing to look for — can never be verified
    if name in dirs.get("archCodex", ()) or f"Z-{name}" in dirs.get("archCodex", ()):
        return "imported"
    if name in dirs.get("unconfirmed", ()):
        return "failed"
    if name in dirs.get("in", ()):
        age = now - row["uploaded_at"]
        return "timeout" if age >= timedelta(minutes=timeout_minutes) else None
    return "unknown"              # gone from in, archCodex AND unconfirmed


# --- alert text (plain Slovak: which order/file, since when, what to do) -------------

def _fmt_dt(ts) -> str:
    try:
        return ts.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(ts or "")


def _text_failed(row: dict, _timeout: int) -> str:
    return (f"Import objednávky <b>{row['filename']}</b> (zákazník {row['customer_ean']}, "
            f"dodanie {row['delivery_date'] or '?'}) <b>ZLYHAL</b> — súbor skončil v "
            f"priečinku &quot;unconfirmed&quot;. Treba ho skontrolovať a nahrať do "
            f"ORIONu ručne.")


def _text_timeout(row: dict, timeout_minutes: int) -> str:
    return (f"Objednávka <b>{row['filename']}</b> (zákazník {row['customer_ean']}, "
            f"dodanie {row['delivery_date'] or '?'}) je nahratá od "
            f"{_fmt_dt(row['uploaded_at'])}, ale Communicator ju do {timeout_minutes} "
            f"minút neprevzal. Skontroluj, či Communicator na serveri beží, a súbor v "
            f"priečinku &quot;in&quot;.")


def _text_unknown(row: dict, _timeout: int) -> str:
    when = _fmt_dt(row["uploaded_at"]) if row.get("uploaded_at") else "?"
    return (f"Súbor <b>{row['filename'] or '(bez mena)'}</b> (zákazník "
            f"{row['customer_ean']}, dodanie {row['delivery_date'] or '?'}, nahraté "
            f"{when}) zmizol zo všetkých sledovaných priečinkov ORIONu (in, archCodex aj "
            f"unconfirmed) — nedá sa overiť, či sa naozaj naimportoval. Treba to "
            f"skontrolovať ručne priamo na serveri.")


_ALERT_TEXT = {"failed": _text_failed, "timeout": _text_timeout, "unknown": _text_unknown}


def _alert(conn, cfg, post, row: dict, status: str, timeout_minutes: int) -> bool:
    """Returns whether the alert was genuinely delivered. Review finding (PR #179): the
    row used to be marked terminal REGARDLESS of whether this succeeded — a transient
    Odoo failure, or Odoo simply being unconfigured (`post_from_config` returns `None`
    with only a `log.warning`, no exception), silently and PERMANENTLY lost the one alert
    for that file, which is exactly the "never silent" failure this whole ticket exists to
    close, just moved one layer up. The caller now only marks a row terminal once this
    returns True — a failed/undelivered alert leaves the row pending, so the NEXT sweep
    re-decides and re-attempts delivery instead of losing it."""
    html = "<p>" + _ALERT_TEXT[status](row, timeout_minutes) + "</p>"
    try:
        result = post(cfg, html, channel_id=_channel_for(row["filename"], cfg))
    except Exception:
        log.exception("posting the import-confirmation alert failed (edi_sent #%s, %s) — "
                      "will retry next sweep", row["id"], status)
        return False
    if result is None:
        log.warning("import-confirmation alert for edi_sent #%s (%s) was not delivered "
                    "(Odoo not configured?) — will retry next sweep", row["id"], status)
        return False
    return True


# --- the sweep -------------------------------------------------------------

def sweep(conn, cfg, listdir=None, post=None, now=None) -> int:
    """One pass over every confirmed-uploaded, unresolved `edi_sent` row. Returns the
    number of rows that reached a terminal status this pass (0 when nothing is pending —
    in that case SFTP is never even contacted)."""
    interval = int(getattr(cfg, "import_confirm_interval_minutes", DEFAULT_INTERVAL_MINUTES)
                   or DEFAULT_INTERVAL_MINUTES)
    rows = due_rows(conn, interval)
    if not rows:
        return 0

    timeout = int(getattr(cfg, "import_confirm_timeout_minutes", DEFAULT_TIMEOUT_MINUTES)
                 or DEFAULT_TIMEOUT_MINUTES)
    listdir = listdir or (lambda: upload_mod.list_dirs(cfg))
    post = post or (lambda c, html, **kw: report.post_from_config(
        c, html, channel_id=kw.get("channel_id")))
    now = now or datetime.now(UTC)

    try:
        dirs = listdir()
    except Exception:
        log.exception("import-confirmation sweep: could not list ORION directories — "
                      "will retry next sweep")
        # Review finding (PR #179): without this, a due row whose import_checked_at is
        # already stale stays immediately due again — a sustained ORION-side outage would
        # retry the SFTP connection on every single worker tick (~15s) with no backoff.
        # Advancing checked_at here makes a listdir failure back off by the SAME
        # import_confirm_interval_minutes throttle a normal check already respects.
        conn.execute("UPDATE edi_sent SET import_checked_at = now() WHERE id = ANY(%s)",
                    ([r["id"] for r in rows],))
        return 0

    changed = 0
    for row in rows:
        status = _decide(row, dirs, timeout, now)
        if status is None:
            log.debug("EDI import check: %s (edi_sent #%s) still pending", row["filename"],
                      row["id"])
            conn.execute("UPDATE edi_sent SET import_checked_at = now() WHERE id = %s",
                        (row["id"],))
            continue
        if status in _ALERT_STATUSES and not _alert(conn, cfg, post, row, status, timeout):
            # Undelivered — stay pending (import_status left NULL) so the next due sweep
            # re-decides and retries delivery instead of losing this alert forever. Still
            # advance checked_at so this respects the normal throttle, not a tight retry.
            conn.execute("UPDATE edi_sent SET import_checked_at = now() WHERE id = %s",
                        (row["id"],))
            continue
        conn.execute(
            """UPDATE edi_sent
                  SET import_status = %s, import_confirmed_at = now(),
                      import_checked_at = now()
                WHERE id = %s""", (status, row["id"]))
        changed += 1
        log.info("EDI import check: %s (%s / %s, edi_sent #%s) -> %s", row["filename"],
                 row["customer_ean"], row["delivery_date"], row["id"], status)
    return changed
