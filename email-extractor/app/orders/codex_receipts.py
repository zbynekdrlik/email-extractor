"""CODEX supplier receipts (príjemky) — what the warehouse already took in (#485).

`tools/codex_receipts_push.py` (dev2, the #342/#467 push pattern; run when the codex-bridge ETL
replaced its DuckDB — `tools/codex_push_after_etl.py`) reads the last
60 days of receipt headers from the codex-bridge DuckDB read-only and POSTs them to
`POST /api/codex/receipts`; `replace_receipts` swaps the whole copy in atomically (a REPLACE,
never a merge — a receipt deleted in CODEX must leave here too, or it would block an invoice
forever). One row per receipt: supplier (IČO + EVERY EDI EAN CODEX has for it — `raw.firma`
repeats a NICO per branch and 24 NICO carry conflicting AEDIEAN, a single picked EAN would hide
a flagged supplier's receipts), receipt date (range), the DL number(s) the warehouse typed (often
the INVOICE number), the linked invoice number(s), the receipt total (Σ NSUMAP) and the linked
invoice's total without VAT. The shared snapshot mechanics (refusal type, data age, staleness,
the stale ops alert) live in `codex_snapshot`, one copy with the stock-card list (#467).

The ONE consumer is the invoice-as-DL dedup gate (`invoice_dedup`, wired in `dl_invoice`): an
invoice whose delivery CODEX already has is never shipped as a DESADV. Our own imported DESADVs
show up here too (CODEX stores our DL number in NCDLIST on import).

**Fail CLOSED, unlike the stock-card list (#467 is fail-open).** A missing invoice-as-DL is
handled by hand exactly as before the feature; a DUPLICATE delivery note in ORION is the harm
this exists to stop. So `live()` returns None when nothing was pushed yet or the CODEX data is
older than `codex_snapshot.STALE_HOURS` — and the DL engine then claims NO invoice at all until
a fresh push (they wait in the queue, no model call). Even a fresh copy only covers what CODEX
had at `as_of`: an invoice that arrived later waits for the next push
(`delivery_notes_invoice_wait_for_codex`, `dl_message._claim_invoice`), so a receipt typed by
hand the same morning is visible before its invoice is judged. `stale_sweep` raises ONE ops
alert per stale episode and `missing_supplier_sweep` one per flagged supplier CODEX has no
receipt for — both only while some supplier has `invoice_is_delivery_note` on.

Everything here is a read-model over our own table; nothing writes to CODEX or ORION.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from html import escape

from . import codex_snapshot

log = logging.getLogger("orders.codex_receipts")

STALE_HOURS = codex_snapshot.STALE_HOURS
# A push carrying fewer than this share of the previous push's receipts is refused (a
# half-loaded ETL snapshot would let a duplicate invoice through). `force` overrides it.
MIN_KEEP_RATIO = 0.5
ALERT_KIND = "codex_receipts_stale"
ALERT_KEY = "codex-receipts"              # + ":<stale snapshot time>", one key per episode
MISSING_KIND = "codex_receipts_no_supplier"
MISSING_KEY = "codex-receipts-ean"        # + ":<supplier ean>"
_REVISION_NAME = "add_codex_receipts"
ReplaceRefused = codex_snapshot.ReplaceRefused


@dataclass(frozen=True)
class Receipt:
    receipt_number: str
    supplier_eans: tuple[str, ...]
    receipt_date: date
    receipt_date_to: date
    dl_numbers: tuple[str, ...]
    invoice_number: str
    invoice_vs: str
    total: float | None
    invoice_total: float | None


@dataclass(frozen=True)
class Receipts:
    """The receipts the gate may trust (fresh): `as_of` = how far CODEX's data reaches."""
    as_of: datetime
    synced_at: datetime

    def for_supplier(self, conn, supplier_ean: str) -> list[Receipt]:
        rows = conn.execute(
            "SELECT receipt_number, supplier_eans, receipt_date, receipt_date_to, dl_numbers, "
            "invoice_number, invoice_vs, total, invoice_total FROM codex_receipts "
            "WHERE supplier_eans @> ARRAY[%s]::text[] ORDER BY receipt_date, receipt_number",
            (str(supplier_ean or ""),)).fetchall()
        return [Receipt(r[0], tuple(r[1] or ()), r[2], r[3] or r[2], tuple(r[4] or ()),
                        r[5] or "", r[6] or "",
                        float(r[7]) if r[7] is not None else None,
                        float(r[8]) if r[8] is not None else None) for r in rows]

    def covers(self, conn, supplier_ean: str) -> bool:
        """Does any receipt carry this supplier's EAN? If not, the gate cannot see its CODEX
        receipts at all (an EAN that differs from every `raw.firma.AEDIEAN`) — fail-closed."""
        return conn.execute(
            "SELECT 1 FROM codex_receipts WHERE supplier_eans @> ARRAY[%s]::text[] LIMIT 1",
            (str(supplier_ean or ""),)).fetchone() is not None


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


def _texts(value) -> list[str]:
    """A list field of the push (a bare string is one element) → distinct non-empty texts."""
    items = value if isinstance(value, (list, tuple)) else [value]
    return sorted({t for t in (_text(v) for v in items) if t})


# --- the push: a full, atomic replace ----------------------------------------------------

def latest_sync(conn) -> dict | None:
    row = conn.execute(
        "SELECT id, synced_at, source_as_of, row_count FROM codex_receipt_syncs "
        "ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    return {"id": row[0], "synced_at": row[1], "source_as_of": row[2], "row_count": row[3]}


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
        rows[key] = (_texts(r.get("supplier_eans")), _text(r.get("supplier_name"), 300), day,
                     _day(r.get("receipt_date_to")) or day, _texts(r.get("dl_numbers")),
                     _text(r.get("invoice_number")), _text(r.get("invoice_vs")),
                     _money(r.get("total")), _money(r.get("invoice_total")),
                     int(r.get("line_count") or 0),
                     codex_snapshot.parse_ts(r.get("entered_at")))
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
                "INSERT INTO codex_receipts (receipt_number, supplier_ico, supplier_eans, "
                "supplier_name, receipt_date, receipt_date_to, dl_numbers, invoice_number, "
                "invoice_vs, total, invoice_total, line_count, entered_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [(*k, *v) for k, v in rows.items()])
        conn.execute(
            "INSERT INTO codex_receipt_syncs (source_as_of, row_count, days) VALUES (%s, %s, %s)",
            (codex_snapshot.parse_ts(source_as_of), len(rows), int(days) if days else None))
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
    if sync["source_as_of"] is None:
        # a push that cannot say how old CODEX's data is must never pass as "fresh as of the
        # push" — fail-closed means an unknown age is too old (the cards list may fail open)
        _warn_hold("unknown", now, "CODEX receipts carry no ETL time (source_as_of) — "
                   "invoice-as-DL is held (fail-closed) until a push that has one (#485)")
        return None
    as_of = codex_snapshot.data_as_of(sync["source_as_of"], sync["synced_at"])
    if codex_snapshot.is_stale(as_of, now):
        _warn_hold("stale", now, "CODEX receipts are stale (CODEX data as of %s, > %d h) — "
                   "invoice-as-DL is held (fail-closed) until a fresh push (#485)",
                   as_of, STALE_HOURS)
        return None
    return Receipts(as_of=as_of, synced_at=sync["synced_at"])


def flagged_suppliers(conn) -> list[tuple[str, str]]:
    """(ean, name) of every live supplier card that takes its invoices as delivery notes."""
    return [(r[0] or "", r[1] or "") for r in conn.execute(
        "SELECT ean_edi, name FROM dl_supplier_overrides WHERE invoice_is_delivery_note "
        "AND NOT retired AND deleted_at IS NULL ORDER BY id").fetchall()]


# --- the ops alerts -------------------------------------------------------------------------

def stale_sweep(conn, cfg, now: datetime | None = None) -> bool:
    """ONE ops alert per stale episode while the receipts are older than `STALE_HOURS` — or
    never arrived that long after the feature went live — AND an invoice flag is on
    (otherwise nothing is held). Returns True when it enqueued."""
    if not flagged_suppliers(conn):
        return False
    now = now or datetime.now(UTC)
    sync = latest_sync(conn)
    anchor = (codex_snapshot.data_as_of(sync["source_as_of"], sync["synced_at"]) if sync
              else codex_snapshot.installed_at(conn, _REVISION_NAME))
    if anchor is None:
        return False
    hours = int((now - anchor).total_seconds() // 3600)
    state = (f"sú zastarané (údaje z CODEXu k {escape(codex_snapshot.local_label(anchor))}, "
             f"pred {hours} h)" if sync else f"ešte nikdy neprišli ({hours} h od nasadenia)")
    body = (f"<p>&#9888;&#65039; Príjemky z CODEXu {state} &mdash; faktúry ako dodací list "
            f"(#485) sa preto NEspracúvajú (radšej čakať než nahrať duplicitu do ORIONu); "
            f"počkajú vo fronte, kým príde čerstvý zoznam. Dodacie listy bežia ďalej. "
            "Skontroluj na dev2 <code>codex-push-after-etl.path</code> / <code>.timer</code> "
            "(<code>journalctl -u codex-push-after-etl.service</code>) a codex-bridge ETL.</p>")
    if not codex_snapshot.stale_alert(conn, cfg, kind=ALERT_KIND, key_prefix=ALERT_KEY,
                                      anchor=anchor, body=body, now=now):
        return False
    log.warning("CODEX receipts %s — ops alert enqueued", "stale" if sync else "missing")
    return True


def covered_eans(conn) -> set[str]:
    """Every supplier EAN at least one receipt of the copy carries."""
    return {str(r[0]) for r in conn.execute(
        "SELECT DISTINCT unnest(supplier_eans) FROM codex_receipts").fetchall()}


def missing_supplier_sweep(conn, cfg, now: datetime | None = None) -> int:
    """A flagged supplier with NO receipt in the (fresh) copy means the gate cannot see that
    supplier's CODEX receipts at all — most likely its EDI EAN on our card differs from every
    `raw.firma.AEDIEAN` of its IČO. Fail-closed like a stale copy: its invoices wait
    (`dl_worker._tick_invoice` claims none, `Receipts.covers`), and ops must know — one alert
    per supplier, workday-morning reminders. Returns how many enqueued."""
    from . import dl_alerts, report
    if live(conn, now) is None:
        return 0
    n = 0
    covered = covered_eans(conn)
    for ean, name in flagged_suppliers(conn):
        if ean in covered:
            continue
        key = f"{MISSING_KEY}:{ean}"
        if dl_alerts.reminder_suppressed(conn, cfg, MISSING_KIND, key, now=now):
            continue
        body = (f"<p>&#9888;&#65039; Dodávateľ <b>{escape(name)}</b> (EAN {escape(ean)}) má "
                f"zapnutú faktúru ako dodací list (#485), ale v príjemkách z CODEXu za posledných "
                f"60 dní nemá ani jednu príjemku pod týmto EAN &mdash; kontrola duplicity voči "
                f"CODEXu ho nevidí, jeho faktúry preto čakajú. Over v CODEXe EAN kód EDI dodávateľa (raw.firma AEDIEAN) "
                f"oproti karte dodávateľa na nástenke.</p>")
        dl_alerts.enqueue(conn, report.ops_channel(cfg), MISSING_KIND, body, message_id=key)
        log.warning("CODEX receipts: flagged supplier %s (%s) has no receipt in the copy — "
                    "ops alert enqueued", ean, name)
        n += 1
    return n
