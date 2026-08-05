"""Import CONFIRMATION (#151, revised #133 2026-08-05): uploading an EDI file to ORION's
`in/` folder over SFTP is NOT proof it ever reached ORION — files move OUT of `in/` ONLY
when pani skladníčka (the warehouse) manually clicks "prijať objednávky z ORIONu" in
CODEX, once each morning when she arrives. There is NO automatic sweep. A file
legitimately sits in `in/` all evening, overnight, and over the weekend — that is NORMAL,
not a stuck import.

**Correction, 2026-08-05:** this module's original ~60-minute "Communicator will pick it
up automatically" model was WRONG (so was the `n8n-workflow-edits.md` claim it was based
on — corrected there too). Real incident: the old code alerted 5 SEPARATE times at 18:18
for one order's files sitting unaccepted since the afternoon — a false alarm, deleted by
the user from the Odoo channel. This module now distinguishes NORMAL waiting from a
genuine problem, and alerts are grouped per INCIDENT, never one message per file.

The real signal, read straight off the three folders Communicator/Codex actually use:

  * present in `in/archCodex` (with OR without the `Z-` prefix) -> IMPORTED — she accepted
    it. The `Z-` rename is a separate, infrequent, uncontrolled batch job; never key on it.
  * present in `in/unconfirmed` -> import FAILED — a genuine anomaly, not a timing
    question. Terminal, alert (grouped).
  * gone from all three -> UNKNOWN — a genuine anomaly. Terminal, alert (grouped).
  * still only in `in/` -> NORMAL while it's simply waiting for her morning click. It only
    becomes alert-worthy as a CARRYOVER: uploaded before TODAY (Europe/Bratislava) and it
    is now past the configured morning-check hour (`import_morning_check_hour`, default
    10:00), skipping Saturday/Sunday by default (the warehouse doesn't work weekends — a
    Friday-evening or weekend upload is first checked the following Monday morning). A
    carryover row is deliberately NEVER given a terminal `import_status` — it stays in
    `due_rows()`'s rotation so it self-heals to `imported` the moment she accepts it,
    whatever day that turns out to be.

Every alert-worthy condition (`carryover`/`failed`/`unknown`) is grouped into a durable,
per-(channel, kind) INCIDENT (`import_alert_incidents`) instead of one message per file:
the FIRST detection posts ONE grouped message and opens the incident; while it stays
open, further detections of the SAME kind are silently folded in (at most one reminder
after `import_alert_reminder_hours`, default 4h); once resolved (something has been
confirmed imported SINCE the incident opened, proving the pipeline works), ONE short
all-clear closes it. No in-memory state anywhere — every decision reads straight from the
DB, so this survives an add-on restart.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from . import report
from . import upload as upload_mod

log = logging.getLogger("orders.confirm")

DEFAULT_INTERVAL_MINUTES = 5
DEFAULT_MORNING_CHECK_HOUR = 10
DEFAULT_REMINDER_HOURS = 4
LOCAL_TZ = ZoneInfo("Europe/Bratislava")

# Terminal, genuinely-anomalous outcomes — never a timing question, always grouped-alerted.
_ALERT_STATUSES = ("failed", "unknown")


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
    `interval_minutes` — the per-row throttle that keeps a file waiting on her morning
    click from triggering an SFTP `listdir` (archCodex alone runs ~21k entries) on every
    worker tick."""
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


def _decide(row: dict, dirs: dict) -> str | None:
    """Returns a terminal status ('imported'/'failed'/'unknown'), or None when the row is
    still sitting in `in/` — NORMAL by default; `_is_carryover` (called separately, only
    once the morning check is active) decides whether THAT case is alert-worthy."""
    name = row["filename"]
    if not name:
        return "unknown"          # nothing to look for — can never be verified
    if name in dirs.get("archCodex", ()) or f"Z-{name}" in dirs.get("archCodex", ()):
        return "imported"
    if name in dirs.get("unconfirmed", ()):
        return "failed"
    if name in dirs.get("in", ()):
        return None              # normal — waiting for her morning click
    return "unknown"              # gone from in, archCodex AND unconfirmed


# --- the morning carryover check --------------------------------------------

def _local(ts: datetime) -> datetime:
    return ts.astimezone(LOCAL_TZ)


def morning_check_active(cfg, now_utc: datetime) -> bool:
    """True once it is genuinely worth asking "is anything left over from before today" —
    past the configured local hour, and not a day the warehouse doesn't work."""
    local = _local(now_utc)
    hour = int(getattr(cfg, "import_morning_check_hour", DEFAULT_MORNING_CHECK_HOUR)
              or DEFAULT_MORNING_CHECK_HOUR)
    if local.hour < hour:
        return False
    weekday = local.weekday()  # Monday=0 ... Sunday=6
    if weekday == 5 and bool(getattr(cfg, "import_morning_check_skip_saturday", True)):
        return False
    if weekday == 6 and bool(getattr(cfg, "import_morning_check_skip_sunday", True)):
        return False
    return True


def _is_carryover(row: dict, now_utc: datetime) -> bool:
    """Uploaded before TODAY (local calendar date) — a Friday-evening or weekend upload is
    only a carryover once Monday's local date has begun, never mid-weekend even if the
    morning-check hour would otherwise be reached (Saturday/Sunday are skip days, so
    `morning_check_active` already gates the caller before this is ever consulted)."""
    uploaded = row.get("uploaded_at")
    if not uploaded:
        return False
    return _local(uploaded).date() < _local(now_utc).date()


# --- alert text (plain Slovak) ----------------------------------------------

def _fmt_hm(ts) -> str:
    try:
        return _local(ts).strftime("%H:%M")
    except Exception:
        return "?"


def _plural_objednavok(n: int) -> str:
    if n == 1:
        return "objednávka"
    if 2 <= n <= 4:
        return "objednávky"
    return "objednávok"


def _group_html(kind: str, rows: list[dict]) -> str:
    n = len(rows)
    if kind == "carryover":
        return (f"<p>&#9888;&#65039; {n} {_plural_objednavok(n)} z predošlého dňa je "
               "stále neprevzatých v ORIONe — treba ich prijať v Codexe.</p>")
    if kind == "failed":
        return (f"<p>&#9888;&#65039; {n} {_plural_objednavok(n)} skončilo v priečinku "
               "&quot;unconfirmed&quot; — import zlyhal, treba nahrať do ORIONu "
               "ručne.</p>")
    return (f"<p>&#9888;&#65039; {n} {_plural_objednavok(n)} zmizlo zo všetkých "
           "sledovaných priečinkov ORIONu — nedá sa overiť import, treba skontrolovať "
           "ručne priamo na serveri.</p>")


def _reminder_html(kind: str, incident: dict) -> str:
    n = incident["file_count"]
    since = _fmt_hm(incident["opened_at"])
    if kind == "carryover":
        return (f"<p>&#9888;&#65039; Stále {n} {_plural_objednavok(n)} neprevzatých v "
               f"ORIONe (od {since}) — treba ich prijať v Codexe.</p>")
    return (f"<p>&#9888;&#65039; Stále {n} {_plural_objednavok(n)} s problémom importu "
           f"do ORIONu (od {since}) — treba to skontrolovať.</p>")


def _all_clear_html(kind: str) -> str:
    if kind == "carryover":
        return ("<p>&#9989; Objednávky boli prijaté v Codexe — import do ORIONu je v "
               "poriadku.</p>")
    return "<p>&#9989; Import do ORIONu je opäť v poriadku.</p>"


# --- incidents (durable, per channel + kind) --------------------------------

def _open_incident(conn, channel_id: int, kind: str) -> dict | None:
    row = conn.execute(
        """SELECT id, opened_at, last_alert_at, file_count FROM import_alert_incidents
            WHERE channel_id = %s AND kind = %s AND closed_at IS NULL
            ORDER BY id DESC LIMIT 1""", (channel_id, kind)).fetchone()
    if not row:
        return None
    return {"id": row[0], "opened_at": row[1], "last_alert_at": row[2], "file_count": row[3]}


def _last_import_confirmed_at(conn):
    row = conn.execute(
        "SELECT max(import_confirmed_at) FROM edi_sent WHERE import_status = 'imported'"
    ).fetchone()
    return row[0] if row else None


def _check_incidents_for_clear(conn, cfg, post, now: datetime) -> None:
    """Independent of whatever this sweep pass found — an incident closes the moment
    something has been confirmed imported SINCE it opened, proving the pipeline works
    again (she's back and clicking, or Codex/ORION recovered)."""
    last_import = _last_import_confirmed_at(conn)
    if not last_import:
        return
    open_incidents = conn.execute(
        """SELECT id, channel_id, kind FROM import_alert_incidents
            WHERE closed_at IS NULL AND opened_at < %s""", (last_import,)).fetchall()
    for incident_id, channel_id, kind in open_incidents:
        html = _all_clear_html(kind)
        try:
            result = post(cfg, html, channel_id=channel_id)
        except Exception:
            log.exception("posting the import-alert all-clear failed (incident #%s) — "
                          "will retry next sweep", incident_id)
            continue
        if result is None:
            log.warning("import-alert all-clear for incident #%s was not delivered "
                       "(Odoo not configured?) — will retry next sweep", incident_id)
            continue
        conn.execute("UPDATE import_alert_incidents SET closed_at = %s WHERE id = %s",
                    (now, incident_id))
        log.info("import-alert incident #%s (%s/%s) cleared", incident_id, kind, channel_id)


def _handle_group(conn, cfg, post, channel_id: int, kind: str, rows: list[dict],
                  now: datetime, reminder_hours: int) -> bool:
    """Returns whether this group is now safely ACCOUNTED FOR — either a fresh incident's
    opening alert genuinely delivered, or it was folded into an already-open incident
    (which already communicated the condition once; a failed REMINDER is a lesser,
    retryable concern that must never block marking these rows). False only means "the
    very first alert for this condition has not reached anyone yet" — the caller must
    leave those rows retryable, never mark them terminal."""
    incident = _open_incident(conn, channel_id, kind)
    if incident is None:
        html = _group_html(kind, rows)
        try:
            result = post(cfg, html, channel_id=channel_id)
        except Exception:
            log.exception("posting the grouped import-alert failed (%s/%s) — will retry "
                          "next sweep", kind, channel_id)
            return False
        if result is None:
            log.warning("grouped import-alert (%s/%s) was not delivered (Odoo not "
                       "configured?) — will retry next sweep", kind, channel_id)
            return False
        conn.execute(
            """INSERT INTO import_alert_incidents
                   (channel_id, kind, file_count, opened_at, last_alert_at)
               VALUES (%s, %s, %s, %s, %s)""",
            (channel_id, kind, len(rows), now, now))
        log.info("opened import-alert incident (%s/%s): %d file(s)", kind, channel_id,
                 len(rows))
        return True

    due_for_reminder = (now - incident["last_alert_at"]) >= timedelta(hours=reminder_hours)
    if due_for_reminder:
        html = _reminder_html(kind, incident)
        try:
            result = post(cfg, html, channel_id=channel_id)
            delivered = result is not None
        except Exception:
            log.exception("posting the import-alert reminder failed (incident #%s)",
                          incident["id"])
            delivered = False
        if delivered:
            conn.execute(
                "UPDATE import_alert_incidents SET last_alert_at = %s WHERE id = %s",
                (now, incident["id"]))
    conn.execute(
        "UPDATE import_alert_incidents SET file_count = file_count + %s WHERE id = %s",
        (len(rows), incident["id"]))
    return True


# --- the sweep -------------------------------------------------------------

def sweep(conn, cfg, listdir=None, post=None, now=None) -> int:
    """One pass over every confirmed-uploaded, unresolved `edi_sent` row. Returns the
    number of rows that reached a terminal status this pass — a carryover row is NEVER
    terminal, so it never counts even while it's being alerted on."""
    interval = int(getattr(cfg, "import_confirm_interval_minutes", DEFAULT_INTERVAL_MINUTES)
                   or DEFAULT_INTERVAL_MINUTES)
    post = post or (lambda c, html, **kw: report.post_from_config(
        c, html, channel_id=kw.get("channel_id")))
    now = now or datetime.now(UTC)
    reminder_hours = int(getattr(cfg, "import_alert_reminder_hours", DEFAULT_REMINDER_HOURS)
                         or DEFAULT_REMINDER_HOURS)

    rows = due_rows(conn, interval)
    if not rows:
        # Still worth checking: an incident opened by an EARLIER sweep may have been
        # resolved by activity this sweep has no rows of its own to process.
        _check_incidents_for_clear(conn, cfg, post, now)
        return 0

    listdir = listdir or (lambda: upload_mod.list_dirs(cfg))
    try:
        dirs = listdir()
    except Exception:
        log.exception("import-confirmation sweep: could not list ORION directories — "
                      "will retry next sweep")
        # Review finding (PR #179): without this, a due row whose import_checked_at is
        # already stale stays immediately due again — a sustained ORION-side outage would
        # retry the SFTP connection on every single worker tick (~15s) with no backoff.
        conn.execute("UPDATE edi_sent SET import_checked_at = now() WHERE id = ANY(%s)",
                    ([r["id"] for r in rows],))
        return 0

    morning_active = morning_check_active(cfg, now)
    changed = 0
    groups: dict[tuple[int, str], list[dict]] = {}
    for row in rows:
        status = _decide(row, dirs)
        if status is None:
            conn.execute("UPDATE edi_sent SET import_checked_at = now() WHERE id = %s",
                        (row["id"],))
            if morning_active and _is_carryover(row, now):
                ch = _channel_for(row["filename"], cfg)
                groups.setdefault((ch, "carryover"), []).append(row)
            continue
        if status in _ALERT_STATUSES:
            # NOT marked terminal here — stays pending until its group is genuinely
            # accounted for (see _handle_group's docstring), so a failed FIRST alert
            # never silently loses the row the way #151/PR#179 already fixed once.
            ch = _channel_for(row["filename"], cfg)
            groups.setdefault((ch, status), []).append(row)
            conn.execute("UPDATE edi_sent SET import_checked_at = now() WHERE id = %s",
                        (row["id"],))
            continue
        # imported — always safe to mark immediately, no alert dependency. Uses the
        # injected `now` (not bare SQL now()) — this is the value `_check_incidents_
        # for_clear` (called below, AFTER this loop) compares against each open
        # incident's `opened_at`, also written from `now` — both sides of that
        # comparison must agree on what "now" means, including in tests.
        conn.execute(
            """UPDATE edi_sent
                  SET import_status = %s, import_confirmed_at = %s,
                      import_checked_at = now()
                WHERE id = %s""", (status, now, row["id"]))
        changed += 1
        log.info("EDI import check: %s (%s / %s, edi_sent #%s) -> %s", row["filename"],
                 row["customer_ean"], row["delivery_date"], row["id"], status)

    for (channel_id, kind), group_rows in groups.items():
        accounted_for = _handle_group(conn, cfg, post, channel_id, kind, group_rows, now,
                                      reminder_hours)
        if kind == "carryover":
            continue  # never terminal, whatever happens — self-heals via due_rows
        if accounted_for:
            for row in group_rows:
                conn.execute("UPDATE edi_sent SET import_status = %s WHERE id = %s",
                            (kind, row["id"]))
                changed += 1

    # Runs AFTER this sweep's own row processing (not before) — a row that just got
    # marked 'imported' in THIS pass can immediately clear an open incident in the SAME
    # call, rather than waiting one extra tick.
    _check_incidents_for_clear(conn, cfg, post, now)
    return changed
