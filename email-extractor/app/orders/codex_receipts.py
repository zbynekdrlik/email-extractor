"""CODEX supplier receipts (príjemky) — what the warehouse already took in (#485).

`tools/codex_receipts_push.py` (dev2 systemd timer, the #342/#467 push pattern) reads the last
60 days of receipt headers from the codex-bridge DuckDB read-only and POSTs them to
`POST /api/codex/receipts`; `replace_receipts` swaps the whole copy in atomically (a REPLACE,
never a merge — a receipt deleted in CODEX must leave here too, or it would block an invoice
forever). One row per receipt: supplier (IČO + the EDI EAN = our `supplier_ean`), receipt date
(range), the DL number the warehouse typed (often the INVOICE number), the linked invoice
number(s), the receipt total (Σ NSUMAP) and the linked invoice's total without VAT.

The ONE consumer is the invoice-as-DL dedup gate (`invoice_dedup`): an invoice whose delivery
CODEX already has is never shipped as a DESADV. Our own imported DESADVs show up here too
(CODEX stores our DL number in NCDLIST on import), so the gate also sees what we shipped.

**Fail CLOSED, unlike the stock-card list (#467 is fail-open).** A missing invoice-as-DL is
handled by hand exactly as before the feature; a DUPLICATE delivery note in ORION is the harm
this exists to stop. So `live()` returns None when nothing was pushed yet or the CODEX data is
older than `STALE_HOURS` — and the DL engine then claims NO invoice at all until a fresh push
(they wait in the queue, no model call). `stale_sweep` raises ONE ops alert per stale episode
(durable `pending_alerts`, the #467 cadence) — only while some supplier actually has
`invoice_is_delivery_note` on (nothing is held otherwise). The DL path never reads this table.

Everything here is a read-model over our own table; nothing writes to CODEX or ORION.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from html import escape

from . import codex_cards

log = logging.getLogger("orders.codex_receipts")

# The same ETL cadence as the stock-card list (codex-bridge loads sp001 at ~14:20 / ~18:05,
# the push follows at 14:50 / 18:35): the longest NORMAL data age is ~20.5 h, 30 h tolerates
# one missed slot. Shared with `codex_cards` on purpose — one staleness rule per ETL.
STALE_HOURS = codex_cards.STALE_HOURS
# A push carrying fewer than this share of the previous push's receipts is refused (a
# half-loaded ETL snapshot would let a duplicate invoice through). `force` overrides it.
MIN_KEEP_RATIO = 0.5
ALERT_KIND = "codex_receipts_stale"
ALERT_KEY = "codex-receipts"          # + ":<stale snapshot time>", one key per episode
_REVISION_NAME = "add_codex_receipts"


class ReplaceRefused(Exception):
    """The push was refused; `status` is the HTTP code the endpoint answers with."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Receipt:
    receipt_number: str
    supplier_ean: str
    receipt_date: date
    receipt_date_to: date
    dl_number: str
    invoice_number: str
    invoice_vs: str
    total: float | None
    invoice_total: float | None


@dataclass(frozen=True)
class Receipts:
    """The receipts the gate may trust (fresh), with the data age."""
    as_of: datetime
    synced_at: datetime

    def for_supplier(self, conn, supplier_ean: str) -> list[Receipt]:
        rows = conn.execute(
            "SELECT receipt_number, supplier_ean, receipt_date, receipt_date_to, dl_number, "
            "invoice_number, invoice_vs, total, invoice_total FROM codex_receipts "
            "WHERE supplier_ean = %s ORDER BY receipt_date, receipt_number",
            (str(supplier_ean or ""),)).fetchall()
        return [Receipt(r[0], r[1], r[2], r[3] or r[2], r[4] or "", r[5] or "", r[6] or "",
                        float(r[7]) if r[7] is not None else None,
                        float(r[8]) if r[8] is not None else None) for r in rows]


def _ts(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            log.warning("codex receipts: unparsable timestamp %r ignored", value)
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _day(value) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _money(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return round(f, 2) if f == f else None


def _text(value, limit: int = 64) -> str:
    return str(value or "").strip()[:limit]


# --- the push: a full, atomic replace ----------------------------------------------------

def latest_sync(conn) -> dict | None:
    row = conn.execute(
        "SELECT id, synced_at, source_as_of, row_count FROM codex_receipt_syncs "
        "ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    return {"id": row[0], "synced_at": row[1], "source_as_of": row[2], "row_count": row[3]}


def _data_as_of(sync: dict) -> datetime:
    """The CODEX data-age anchor — never later than when the push reached us (a future
    `source_as_of` from a clock bug must not pin the snapshot fresh)."""
    src, got = sync["source_as_of"], sync["synced_at"]
    return min(src, got) if src else got


def replace_receipts(conn, receipts: list, *, source_as_of=None, days: int | None = None,
                     force: bool = False) -> dict:
    """Swap the whole copy for `receipts` in ONE transaction (+ a ledger row). A row without a
    receipt number or a date is skipped; duplicates collapse on (receipt, supplier IČO), the
    last wins. Raises `ReplaceRefused` (400) for an empty list and (409) for a push with fewer
    than `MIN_KEEP_RATIO` of the previous rows unless `force`. Returns {stored}."""
    rows: dict[tuple, tuple] = {}
    for r in receipts or []:
        if not isinstance(r, dict):
            continue
        number, day = _text(r.get("receipt_number")), _day(r.get("receipt_date"))
        if not number or day is None:
            continue
        key = (number, _text(r.get("supplier_ico")))
        rows[key] = (_text(r.get("supplier_ean")), _text(r.get("supplier_name"), 300), day,
                     _day(r.get("receipt_date_to")) or day, _text(r.get("dl_number")),
                     _text(r.get("invoice_number")), _text(r.get("invoice_vs")),
                     _money(r.get("total")), _money(r.get("invoice_total")),
                     int(r.get("line_count") or 0), _ts(r.get("entered_at")))
    if not rows:
        log.warning("codex receipts push refused: no usable row among %d received",
                    len(receipts or []))
        raise ReplaceRefused("zoznam príjemiek z CODEXu je prázdny — nič sa nenahrádza", 400)
    with conn.transaction():
        # serialize concurrent pushes; plain readers (ACCESS SHARE) are never blocked
        conn.execute("LOCK TABLE codex_receipts IN EXCLUSIVE MODE")
        prev = latest_sync(conn)
        if prev and not force and len(rows) < prev["row_count"] * MIN_KEEP_RATIO:
            log.warning("codex receipts push refused: %d receipts vs %d last time (shrink "
                        "guard)", len(rows), prev["row_count"])
            raise ReplaceRefused(
                f"push má len {len(rows)} príjemiek oproti {prev['row_count']} naposledy — "
                f"pravdepodobne neúplný export z CODEXu, nič sa nenahrádza (force=1 ak je to "
                f"zámer)", 409)
        conn.execute("DELETE FROM codex_receipts")
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO codex_receipts (receipt_number, supplier_ico, supplier_ean, "
                "supplier_name, receipt_date, receipt_date_to, dl_number, invoice_number, "
                "invoice_vs, total, invoice_total, line_count, entered_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [(*k, *v) for k, v in rows.items()])
        conn.execute(
            "INSERT INTO codex_receipt_syncs (source_as_of, row_count, days) VALUES (%s, %s, %s)",
            (_ts(source_as_of), len(rows), int(days) if days else None))
    log.info("codex receipts replaced: %d receipts (source as of %s, %s days, force=%s)",
             len(rows), source_as_of, days, force)
    return {"stored": len(rows)}


# --- the read model -----------------------------------------------------------------------

_WARN_EVERY = timedelta(minutes=30)
_last_warned: dict[str, datetime] = {}


def _warn_hold(key: str, now: datetime, msg: str, *args) -> None:
    """The hold warning, at most once per `_WARN_EVERY` per reason — the DL worker asks on
    every idle tick (~15 s) while an invoice flag is on."""
    last = _last_warned.get(key)
    if last is None or now - last >= _WARN_EVERY:
        _last_warned[key] = now
        log.warning(msg, *args)


def live(conn, now: datetime | None = None) -> Receipts | None:
    """The receipts the invoice gate may trust, or None = NOT fresh (never pushed, older than
    `STALE_HOURS`) — the caller then fails CLOSED (holds the invoice), with a warning."""
    now = now or datetime.now(UTC)
    sync = latest_sync(conn)
    if sync is None:
        _warn_hold("never", now, "CODEX receipts were never pushed — invoice-as-DL is held "
                   "(fail-closed) until the first push (#485)")
        return None
    as_of = _data_as_of(sync)
    if now - as_of > timedelta(hours=STALE_HOURS):
        _warn_hold("stale", now, "CODEX receipts are stale (CODEX data as of %s, > %d h) — "
                   "invoice-as-DL is held (fail-closed) until a fresh push (#485)",
                   as_of, STALE_HOURS)
        return None
    return Receipts(as_of=as_of, synced_at=sync["synced_at"])


def invoice_dl_enabled(conn) -> bool:
    """Does any live supplier card take its invoices as delivery notes? (the only reason the
    receipts matter — #406's own short-circuit query)."""
    return conn.execute(
        "SELECT 1 FROM dl_supplier_overrides WHERE invoice_is_delivery_note AND NOT retired "
        "AND deleted_at IS NULL LIMIT 1").fetchone() is not None


# --- the ops alert for a stopped push -------------------------------------------------------

def _installed_at(conn) -> datetime | None:
    row = conn.execute("SELECT applied_at FROM schema_version WHERE name = %s",
                       (_REVISION_NAME,)).fetchone()
    return row[0] if row else None


def stale_sweep(conn, cfg, now: datetime | None = None) -> bool:
    """Enqueue ONE ops alert (durable `pending_alerts`) while the receipts are older than
    `STALE_HOURS` — or never arrived that long after the feature went live — AND an invoice
    flag is on (otherwise nothing is held). Re-reminded at most once per workday morning
    (`dl_alerts.reminder_suppressed`). Returns True when it enqueued."""
    from . import dl_alerts, report
    if not invoice_dl_enabled(conn):
        return False
    now = now or datetime.now(UTC)
    sync = latest_sync(conn)
    anchor = _data_as_of(sync) if sync else _installed_at(conn)
    if anchor is None or now - anchor <= timedelta(hours=STALE_HOURS):
        return False
    key = f"{ALERT_KEY}:{anchor.isoformat()}"
    if dl_alerts.reminder_suppressed(conn, cfg, ALERT_KIND, key, now=now):
        return False
    hours = int((now - anchor).total_seconds() // 3600)
    state = (f"sú zastarané (údaje z CODEXu k {escape(codex_cards._local(anchor))}, pred "
             f"{hours} h)" if sync else f"ešte nikdy neprišli ({hours} h od nasadenia)")
    body = (f"<p>&#9888;&#65039; Príjemky z CODEXu {state} &mdash; faktúry ako dodací list "
            f"(#485) sa preto NEspracúvajú (radšej čakať než nahrať duplicitu do ORIONu); "
            f"počkajú vo fronte, kým príde čerstvý zoznam. Dodacie listy bežia ďalej. "
            "Skontroluj na dev2 <code>codex-receipts-push.timer</code> a codex-bridge ETL.</p>")
    dl_alerts.enqueue(conn, report.ops_channel(cfg), ALERT_KIND, body, message_id=key)
    log.warning("CODEX receipts %s — ops alert enqueued", "stale" if sync else "missing")
    return True
