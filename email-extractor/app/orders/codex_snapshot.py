"""The shared mechanics of a pushed CODEX snapshot — the stock-card list (#467,
`codex_cards`) and the supplier receipts (#485, `codex_receipts`).

Both are a full copy of a codex-bridge DuckDB table that a dev2 push tool POSTs after each ETL
run (14:15 / 18:00 Europe/Prague) and the add-on REPLACES atomically, with an append-only
ledger whose `source_as_of` is when the ETL loaded the data. The ONE place for what the two
share: the refusal type the endpoint maps to an HTTP code, timestamp parsing, the data-age
anchor (never later than when the push reached us), the staleness rule, the "feature went
live" anchor of a never-pushed snapshot (its migration revision), the operator's local time
label, and the one-alert-per-stale-episode ops message. What each snapshot MEANS — fail open
(cards) or closed (receipts), what it guards — stays in its own module.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger("orders.codex_snapshot")

# The ETL starts at 14:15 and 18:00 and the pushes follow the moment it replaced its DuckDB
# (~15:05 / ~18:50, #485), so the longest NORMAL data age is ~21 h (an 18:0x load → next
# ~15:05); 30 h tolerates one missed slot plus ETL jitter.
STALE_HOURS = 30
_LOCAL_TZ = ZoneInfo("Europe/Bratislava")


class ReplaceRefused(Exception):
    """The push was refused; `status` is the HTTP code the endpoint answers with."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def parse_ts(value) -> datetime | None:
    """ISO text / datetime → an aware datetime (a naive one is taken as UTC); else None."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            log.warning("codex snapshot: unparsable timestamp %r ignored", value)
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def data_as_of(source_as_of: datetime | None, synced_at: datetime) -> datetime:
    """The CODEX data-age anchor: the ETL snapshot time, never later than when the push
    reached us — a future `source_as_of` (a clock/timezone bug in the push) must not pin the
    snapshot fresh."""
    return min(source_as_of, synced_at) if source_as_of else synced_at


def is_stale(as_of: datetime, now: datetime | None = None) -> bool:
    return (now or datetime.now(UTC)) - as_of > timedelta(hours=STALE_HOURS)


def local_label(dt: datetime | None) -> str:
    """`5.10. 14:35` in the warehouse's wall-clock time ("?" when unknown)."""
    if dt is None:
        return "?"
    loc = dt.astimezone(_LOCAL_TZ)
    return f"{loc.day}.{loc.month}. {loc:%H:%M}"


def installed_at(conn, revision_name: str) -> datetime | None:
    """When the feature's migration revision was applied — the grace anchor of a snapshot
    that was never pushed (no alert right after a deploy, before the first push)."""
    row = conn.execute("SELECT applied_at FROM schema_version WHERE name = %s",
                       (revision_name,)).fetchone()
    return row[0] if row else None


def stale_alert(conn, cfg, *, kind: str, key_prefix: str, anchor: datetime | None,
                body: str, now: datetime | None = None) -> bool:
    """Enqueue ONE ops alert (durable `pending_alerts`) for a snapshot whose data `anchor` is
    older than `STALE_HOURS` — one dedup key per stale EPISODE (the snapshot it is stuck on: a
    later episode alerts at once instead of waiting as a "reminder"), re-reminded at most once
    per workday morning (`dl_alerts.reminder_suppressed`). Returns True when it enqueued."""
    from . import dl_alerts, report
    now = now or datetime.now(UTC)
    if anchor is None or now - anchor <= timedelta(hours=STALE_HOURS):
        return False
    key = f"{key_prefix}:{anchor.isoformat()}"
    if dl_alerts.reminder_suppressed(conn, cfg, kind, key, now=now):
        return False
    dl_alerts.enqueue(conn, report.ops_channel(cfg), kind, body, message_id=key)
    return True
