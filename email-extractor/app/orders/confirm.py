"""Import CONFIRMATION (#151, revised #133 2026-08-05, extended #203 for DESADV/in_DL):
uploading an EDI file to ORION's `in/` (or, for a DESADV document, `in_DL/`) folder over
SFTP is NOT proof it ever reached ORION — files move OUT of `in/` ONLY when pani
skladníčka (the warehouse) manually clicks "prijať objednávky z ORIONu" in CODEX, once
each morning when she arrives. There is NO automatic sweep. A file legitimately sits in
`in/`/`in_DL/` all evening, overnight, and over the weekend — that is NORMAL, not a
stuck import.

**Correction, 2026-08-05:** this module's original ~60-minute "Communicator will pick it
up automatically" model was WRONG (so was the `n8n-workflow-edits.md` claim it was based
on — corrected there too). Real incident: the old code alerted 5 SEPARATE times at 18:18
for one order's files sitting unaccepted since the afternoon — a false alarm, deleted by
the user. This module now distinguishes NORMAL waiting from a genuine problem, and
alerts are grouped per INCIDENT, never one message per file.

**Extended 2026-08-07 (#203, DL migration F4):** two independent upload ledgers now
share this sweep — `edi_sent` (this add-on's own ORDER_* uploads to `in`) and
`desadv_sent` (DESADV_* uploads to `in_DL`, #200/#203). Verified LIVE 2026-08-07
against the real ORION box: `in_DL` is a SIBLING of `in`, with NO `archCodex`/
`unconfirmed` of its own (`FileNotFoundError` on both) — a DESADV file's post-import
status is read from `in`'s SHARED `archCodex`/`unconfirmed`, same as an ORDER_ file's,
confirmed by 190 real `Z-DESADV_*` entries already sitting there. R89: a DESADV upload
writes its ON-WIRE name with a `Z-` prefix (`Z-DESADV_...`), unlike ORDER_ uploads,
which carry no prefix at write time (any `Z-` seen on an ORDER_ file in `archCodex` is
Communicator's own separate, uncontrolled rename job — see `_decide()`'s tolerant
archCodex check, unchanged from before this extension). The `_Ledger` split below keeps
the two upload kinds' rows, wording ("objednávka" vs "dodací list" — a delivery note is
never an order) and incident membership tables completely separate, while sharing one
SFTP `list_dirs()` round trip and the whole grouped-incident/carryover machinery.

The real signal, read straight off the folders Communicator/Codex actually use:

  * present in `in/archCodex` (with OR without an EXTRA `Z-` prefix) -> IMPORTED — she
    accepted it. The extra `Z-` rename is a separate, infrequent, uncontrolled batch job
    Communicator/WINCODEX runs independently of the actual import; never key on it.
  * present in `in/unconfirmed` -> import FAILED — a genuine anomaly, not a timing
    question. Terminal, alert (grouped).
  * gone from all three -> UNKNOWN — a genuine anomaly. Terminal, alert (grouped).
  * still only in `in`/`in_DL` -> NORMAL while it's simply waiting for her morning
    click. It only becomes alert-worthy as a CARRYOVER: uploaded before TODAY
    (Europe/Bratislava) and it is now past the configured morning-check hour
    (`import_morning_check_hour`, default 10:00), skipping Saturday/Sunday by default
    (the warehouse doesn't work weekends — a Friday-evening or weekend upload is first
    checked the following Monday morning). A carryover row is deliberately NEVER given
    a terminal `import_status` — it stays in `due_rows()`'s rotation so it self-heals to
    `imported` the moment she accepts it, whatever day that turns out to be.

Every alert-worthy condition (`carryover`/`failed`/`unknown`) is grouped into a durable,
per-(channel, kind, source) INCIDENT (`import_alert_incidents`) instead of one message
per file: the FIRST detection posts ONE grouped message and opens the incident; while it
stays open, further detections of the SAME kind+source are silently folded in (at most
one reminder after `import_alert_reminder_hours`, default 4h); once resolved (something
has been confirmed imported SINCE the incident opened, proving the pipeline works), ONE
short all-clear closes it. No in-memory state anywhere — every decision reads straight
from the DB, so this survives an add-on restart.

**Extended 2026-08-13 (#255) — a SAME-DAY rejection, gated on an ACTIVITY signal.** The
morning carryover check only catches a file left over from a PRIOR day — a rejection
CODEX makes on the very day the file was uploaded (e.g. a missing stock card) looks
IDENTICAL to "not yet checked" from `_decide()`'s point of view (both return `None`,
still sitting in the queued folder), so it would otherwise sit unnoticed until the
FOLLOWING morning. A bare fixed evening threshold ("alert anything still queued by
18:00") was rejected — it is exactly the false-alarm shape #151/#133 already got bitten
by (5 separate messages at 18:18 for files that simply hadn't been processed yet).

The fix instead requires a genuine ACTIVITY SIGNAL before it will ever alert same-day:
`evening_check_active()` (mirrors `morning_check_active()`, its own configurable hour —
default 18:00 — and Saturday/Sunday skip) gates a NEW `_activity_today_at()` read: the
most recent `import_confirmed_at` (across BOTH ledgers — `edi_sent` is shared with n8n's
own "Static auto orders" workflow, so this reflects ANY import that happened, not only
ours) within today's local calendar date. A row is "same-day stuck"
(`_is_same_day_stuck()`) only when it was uploaded TODAY, is still sitting in its queued
folder, AND its `uploaded_at` is BEFORE that activity — this is what correctly keeps
silent the completely normal race the ticket's own decision names ("sklad importuje o
07:30, my nahráme o 07:31"): an upload AFTER the day's only import pass is simply
waiting for TOMORROW's click, not evidence of a rejection. No activity signal at all
(weekend, holiday, no import ran) → nothing to compare against → silent, same as the
existing carryover check already is on those days.

A same-day-stuck row is folded into the SAME `'carryover'` incident kind/grouping the
morning check already uses (same underlying condition — a file stuck longer than
reasonable — just a different trigger) — deliberately NOT a new incident `kind`, which
would need a `db.py` CHECK-constraint migration; see the #255 design comment for the
rejected alternatives.

**#476 — a carryover alert counts and names ONLY the files still waiting.** The reminder
used to print the incident's whole membership (`_open_incident`'s count): live 30.9. it said
„Stále 4 dodacie listy neprevzatých" twice a day while 3 of the 4 had been imported two days
earlier. Imported members stay in the incident (its history, and `_check_incidents_for_clear`'s
per-member scope), but a carryover alert is now built from `_pending_members` — no terminal
status AND `_decide` on THIS sweep's listing still finds the file in its queued folder — and
lists each one (DL number + supplier / order day + customer, `confirm_carryover`). For a
waiting DESADV it also says why CODEX will never take it when a LIN code has no CODEX stock
card (#467 list, read-only file read, fail-open). A carryover alert names EVERY file of its
channel still waiting, not only the throttled rows due this sweep. Once nothing waits there
is no carryover group at all, so no reminder — the incident closes through
`_check_incidents_for_clear` with its one all-clear.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from . import confirm_carryover, report
from . import upload as upload_mod

log = logging.getLogger("orders.confirm")

# #476: at most this many waiting files are listed in one carryover alert (+ „… a ešte N").
LIST_LIMIT = 15
DEFAULT_INTERVAL_MINUTES = 5
DEFAULT_MORNING_CHECK_HOUR = 10
DEFAULT_REMINDER_HOURS = 4
# #255: the evening/same-day check's own default hour — well past a normal working day,
# so a slow-completing import pass never gets mistaken for a genuine same-day rejection.
DEFAULT_EVENING_CHECK_HOUR = 18
LOCAL_TZ = ZoneInfo("Europe/Bratislava")

# Terminal, genuinely-anomalous outcomes — never a timing question, always grouped-alerted.
_ALERT_STATUSES = ("failed", "unknown")


# --- the two upload ledgers this sweep watches (#203) -----------------------

@dataclass(frozen=True)
class _Ledger:
    """Per-source config for one upload ledger. `identity_cols` names the two extra
    columns (beyond id/filename/uploaded_at) `due_rows()` selects — used only for
    logging, never for the decide/alert logic itself, which is identical across
    ledgers."""
    source: str                # 'edi' | 'desadv'
    table: str                 # 'edi_sent' | 'desadv_sent'
    identity_cols: tuple[str, str]
    queued_key: str            # upload.list_dirs() key meaning "still queued, normal"
    wire_prefix: str           # "" (edi) or "Z-" (desadv, R89 upload-time prefix)
    members_table: str
    members_fk_col: str


EDI_LEDGER = _Ledger(
    source="edi", table="edi_sent", identity_cols=("customer_ean", "delivery_date"),
    queued_key="in", wire_prefix="",
    members_table="import_alert_incident_members", members_fk_col="edi_sent_id")
DESADV_LEDGER = _Ledger(
    source="desadv", table="desadv_sent", identity_cols=("supplier_ean", "doc_number"),
    queued_key="in_DL", wire_prefix="Z-",
    members_table="import_alert_incident_desadv_members",
    members_fk_col="desadv_sent_id")

# Every `ledger`-taking function below defaults to EDI_LEDGER solely so the pre-#203
# test suite (which predates DESADV entirely) keeps working unmodified — `sweep()` and
# `_check_incidents_for_clear()`, the only PRODUCTION call sites, always pass `ledger`
# explicitly (verified: grep for every call site below). A future DESADV-context call
# site that forgets the argument fails SILENTLY (queries edi_sent instead of
# desadv_sent) rather than erroring — review finding on #203. Always pass `ledger`
# explicitly for any new call site; never rely on this default outside a test.


def _channel_for(filename: str, cfg) -> int:
    """`ORDER_*` is this add-on's own upload (`edi.filename`, #67); `DESADV_*` is the
    DL pipeline's file landing in `in_DL` (#203; before that, the live n8n "Dodacie
    Listy EDI" workflow's own upload) — routed to the delivery-notes channel per the
    existing per-workflow channel split (never invented ad hoc). Falls back to
    `orders_channel_id` when `delivery_notes_channel_id` is unset, and for any other/
    unknown prefix — better a message in the wrong-but-configured channel than none."""
    if filename.startswith("DESADV_"):
        dn = int(getattr(cfg, "delivery_notes_channel_id", 0) or 0)
        if dn:
            return dn
    return int(getattr(cfg, "orders_channel_id", 0) or 0)


# --- selecting what to check ----------------------------------------------

def due_rows(conn, interval_minutes: int, ledger: _Ledger = EDI_LEDGER) -> list[dict]:
    """Every confirmed-uploaded, not-yet-resolved row (for the given ledger) not
    checked within the last `interval_minutes` — the per-row throttle that keeps a file
    waiting on her morning click from triggering an SFTP `listdir` (archCodex alone runs
    ~21k entries) on every worker tick."""
    c1, c2 = ledger.identity_cols
    rows = conn.execute(
        f"""SELECT id, {c1}, {c2}, filename, uploaded_at
             FROM {ledger.table}
            WHERE uploaded_at IS NOT NULL
              AND import_status IS NULL
              AND (import_checked_at IS NULL
                   OR import_checked_at < now() - make_interval(mins => %s))
            ORDER BY uploaded_at""",
        (max(1, int(interval_minutes or DEFAULT_INTERVAL_MINUTES)),)).fetchall()
    return _row_dicts(rows, ledger)


def _row_dicts(rows, ledger: _Ledger) -> list[dict]:
    """(id, identity col 1, identity col 2, filename, uploaded_at) tuples → the row dicts
    every function here passes around."""
    c1, c2 = ledger.identity_cols
    return [{"id": r[0], c1: r[1], c2: r[2], "filename": r[3] or "", "uploaded_at": r[4]}
            for r in rows]


def _unresolved_rows(conn, ledger: _Ledger) -> list[dict]:
    """Every confirmed-uploaded, not-yet-resolved row of the ledger, WITHOUT the per-row
    throttle (#476 review): once a sweep holds a listing, a carryover alert names every file
    still waiting, not only the rows whose 5-minute re-check happened to fall on this sweep.
    Only rows without a terminal status — normally just the handful really waiting."""
    c1, c2 = ledger.identity_cols
    rows = conn.execute(
        f"""SELECT id, {c1}, {c2}, filename, uploaded_at
             FROM {ledger.table}
            WHERE uploaded_at IS NOT NULL AND import_status IS NULL
            ORDER BY uploaded_at, id""").fetchall()
    return _row_dicts(rows, ledger)


def _decide(row: dict, dirs: dict, ledger: _Ledger = EDI_LEDGER) -> str | None:
    """Returns a terminal status ('imported'/'failed'/'unknown'), or None when the row is
    still sitting in its queued directory — NORMAL by default; `_is_carryover` (called
    separately, only once the morning check is active) decides whether THAT case is
    alert-worthy.

    `ledger.wire_prefix` accounts for R89: a DESADV upload's ON-WIRE name already
    carries a `Z-` prefix the ledger's own `filename` column does NOT (that column is
    the human-facing/registry name, same convention `edi_sent.filename` already uses
    for ORDER_ files). archCodex's own tolerant extra-`Z-` check is unchanged from
    before this extension — it accounts for Communicator's separate, uncontrolled
    rename job, independent of what name a file was uploaded under."""
    name = row["filename"]
    if not name:
        return "unknown"          # nothing to look for — can never be verified
    wire_name = f"{ledger.wire_prefix}{name}"
    if (wire_name in dirs.get("archCodex", ())
            or f"Z-{wire_name}" in dirs.get("archCodex", ())):
        return "imported"
    if wire_name in dirs.get("unconfirmed", ()):
        return "failed"
    if wire_name in dirs.get(ledger.queued_key, ()):
        return None               # normal — waiting for her morning click
    return "unknown"              # gone from queued, archCodex AND unconfirmed


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


# --- the evening / same-day check (#255) -------------------------------------

def evening_check_active(cfg, now_utc: datetime) -> bool:
    """True once it is worth asking "did today's own import pass leave one of OUR files
    behind" — past the configured local hour (default 18:00, well after a normal working
    day so a slow-completing import pass is never mistaken for a genuine rejection), and
    not a day the warehouse doesn't work. Mirrors `morning_check_active` exactly (same
    shape, its own config knobs) — the activity gate in `_activity_today_at` already makes
    a weekend/holiday naturally silent on its own (nobody clicks "prijať", so there is no
    activity to compare against), but skipping explicitly too costs nothing and keeps this
    consistent with the existing convention."""
    local = _local(now_utc)
    hour = int(getattr(cfg, "import_evening_check_hour", DEFAULT_EVENING_CHECK_HOUR)
              or DEFAULT_EVENING_CHECK_HOUR)
    if local.hour < hour:
        return False
    weekday = local.weekday()  # Monday=0 ... Sunday=6
    if weekday == 5 and bool(getattr(cfg, "import_evening_check_skip_saturday", True)):
        return False
    if weekday == 6 and bool(getattr(cfg, "import_evening_check_skip_sunday", True)):
        return False
    return True


def _activity_today_at(conn, now_utc: datetime) -> datetime | None:
    """The most recent moment, TODAY (local calendar date), that ANY of our own uploads —
    across BOTH ledgers, since a single warehouse "prijať" click processes whatever is
    queued in `in`/`in_DL` together — was actually observed landing in ORION's archCodex
    (`import_confirmed_at`, written by `sweep()` itself, from the injected `now`, so this
    is deterministic under test). `edi_sent` is shared with n8n's own "Static auto orders"
    workflow (`.claude/rules/orders-corpus.md`), so this reflects ANY import that happened
    today, not only ours — exactly the "iné súbory sa v ten deň pohli do archCodex" signal
    the #255 decision names.

    `None` when nothing was confirmed imported yet today: no evidence an import pass has
    happened at all, so `_is_same_day_stuck` has nothing to compare against and stays
    silent — this is what makes a weekend/holiday/vacation day naturally quiet with no
    extra day-of-week logic of its own."""
    today_start_local = _local(now_utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = today_start_local.astimezone(UTC)
    # #255 review finding: name the two tables via the SAME `_Ledger.table` the rest of
    # this file already uses (`due_rows`, `sweep`), never a hardcoded literal — an f-string
    # is the only way to parameterize a table name at all (psycopg %s is value-only), same
    # pattern `due_rows`'s own `f"...FROM {ledger.table}..."` already establishes.
    row = conn.execute(
        f"""SELECT max(t) FROM (
               SELECT import_confirmed_at AS t FROM {EDI_LEDGER.table}
                WHERE import_confirmed_at >= %s
               UNION ALL
               SELECT import_confirmed_at AS t FROM {DESADV_LEDGER.table}
                WHERE import_confirmed_at >= %s
           ) activity""",
        (today_start_utc, today_start_utc)).fetchone()
    return row[0] if row else None


def _is_same_day_stuck(row: dict, now_utc: datetime, activity_at: datetime | None) -> bool:
    """Uploaded TODAY (local calendar date — an older row is already the morning carryover
    check's own concern, never double-counted here), still sitting in its queued folder,
    and its `uploaded_at` is BEFORE the most recent import activity observed today. A row
    uploaded AFTER that activity is the completely normal next-morning case (she already
    clicked; anything uploaded later simply waits for tomorrow) and must never be flagged —
    this is what correctly keeps silent the race the #255 decision names ("sklad importuje
    o 07:30, my nahráme o 07:31")."""
    if activity_at is None:
        return False
    uploaded = row.get("uploaded_at")
    if not uploaded:
        return False
    if _local(uploaded).date() != _local(now_utc).date():
        return False
    return uploaded < activity_at


# --- alert text (plain Slovak) ----------------------------------------------

def _fmt_since(ts, now: datetime) -> str:
    """When an incident opened: `HH:MM` if that was today (local), else `D.M. HH:MM` — a bare
    „od 18:03" five days later reads as if it were today (#476)."""
    try:
        loc = _local(ts)
        today = _local(now).date()
    except Exception:
        log.warning("import alert: unusable incident time %r", ts)
        return "?"
    if loc.date() == today:
        return loc.strftime("%H:%M")
    return f"{loc.day}.{loc.month}. {loc:%H:%M}"


def _plural(n: int, source: str = "edi") -> str:
    """The noun a grouped alert names, source-aware (#203): a DESADV row is a dodací
    list (delivery note), never an "objednávka" (order) — mislabeling it would report
    correct information under the wrong Slovak word."""
    if source == "desadv":
        if n == 1:
            return "dodací list"
        if 2 <= n <= 4:
            return "dodacie listy"
        return "dodacích listov"
    if n == 1:
        return "objednávka"
    if 2 <= n <= 4:
        return "objednávky"
    return "objednávok"


def _agree(n: int, source: str, forms: tuple[str, str, str, str]) -> str:
    """The word agreeing with `_plural(n, source)` (#476 — „1 dodací list … neprevzatých",
    „2 objednávky skončilo" read wrong). `forms` = (1 masculine — a dodací list, 1 feminine
    — an objednávka, 2-4, 5+)."""
    if n == 1:
        return forms[0] if source == "desadv" else forms[1]
    if 2 <= n <= 4:
        return forms[2]
    return forms[3]


_UNACCEPTED = ("neprevzatý", "neprevzatá", "neprevzaté", "neprevzatých")
_ENDED = ("skončil", "skončila", "skončili", "skončilo")
_VANISHED = ("zmizol", "zmizla", "zmizli", "zmizlo")
# „… a ešte N ďalší/ďalšia/ďalšie/ďalších" under a capped list (report.capped_list)
_FURTHER = {"desadv": ("ďalší", "ďalšie", "ďalších"), "edi": ("ďalšia", "ďalšie", "ďalších")}


def _carryover_head(n: int, source: str, since, now: datetime) -> str:
    """The head of a carryover alert for `n` files REALLY still waiting — the opening alert
    (`since=None`) or a reminder („od" = when the oldest of them was uploaded). #255 review
    finding: the text stays TRIGGER-NEUTRAL (never „z predošlého dňa") — a same-day-stuck
    file joins the same incident."""
    noun = _plural(n, source)
    adj = _agree(n, source, _UNACCEPTED)
    them = "ich" if n != 1 else ("ho" if source == "desadv" else "ju")
    if since is None:
        verb = "sú" if 2 <= n <= 4 else "je"
        return (f"&#9888;&#65039; {n} {noun} {verb} stále {adj} v ORIONe — treba {them} "
                "prijať v Codexe.")
    return (f"&#9888;&#65039; Stále {n} {noun} {adj} v ORIONe (od {_fmt_since(since, now)}) "
            f"— treba {them} prijať v Codexe.")


def _carryover_html(conn, rows: list[dict], ledger: _Ledger, *, reminder: bool, read_files,
                    now: datetime) -> str:
    """A carryover alert about `rows` — ONLY the files really still waiting (#476): the head
    counts them (a reminder also says since when: the oldest upload among them, never the
    incident's opening, which a never-importing file can keep open for days while newer
    files join), then one line per file (at most `LIST_LIMIT`, a file CODEX will never take
    first) from `confirm_carryover.items`, + the CODEX list's date when a code line is
    there."""
    n = len(rows)
    since = None
    if reminder:
        uploads = [r["uploaded_at"] for r in rows if r.get("uploaded_at")]
        since = min(uploads) if uploads else now
    detail = confirm_carryover.items(conn, rows, ledger.source,
                                     wire_prefix=ledger.wire_prefix,
                                     read_files=read_files, now=now)
    parts = [f"<p>{_carryover_head(n, ledger.source, since, now)}</p>",
             report.capped_list(detail.lines, LIST_LIMIT, *_FURTHER[ledger.source])]
    if detail.codex_as_of:
        parts.append(f"<p>(Zoznam kariet z CODEXu je k {escape(detail.codex_as_of)} — kartu, "
                     "ktorú ste v CODEXe založili neskôr, ešte nevidíme.)</p>")
    return "".join(parts)


def _group_html(kind: str, rows: list[dict], source: str = "edi") -> str:
    """The opening alert of a `failed`/`unknown` incident (a carryover one is
    `_carryover_html`)."""
    n = len(rows)
    noun = _plural(n, source)
    if kind == "failed":
        return (f"<p>&#9888;&#65039; {n} {noun} {_agree(n, source, _ENDED)} v priečinku "
               "&quot;unconfirmed&quot; — import zlyhal, treba nahrať do ORIONu "
               "ručne.</p>")
    return (f"<p>&#9888;&#65039; {n} {noun} {_agree(n, source, _VANISHED)} zo všetkých "
           "sledovaných priečinkov ORIONu — nedá sa overiť import, treba skontrolovať "
           "ručne priamo na serveri.</p>")


def _reminder_html(incident: dict, now: datetime, source: str = "edi") -> str:
    """The reminder of a `failed`/`unknown` incident. Those members get a terminal status and
    never self-heal, so the incident's whole membership (`file_count`) IS what still has a
    problem — unlike a carryover reminder, which counts `_pending_members` (#476)."""
    n = incident["file_count"]
    noun = _plural(n, source)
    since = _fmt_since(incident["opened_at"], now)
    return (f"<p>&#9888;&#65039; Stále {n} {noun} s problémom importu "
           f"do ORIONu (od {since}) — treba to skontrolovať.</p>")


def _all_clear_html(kind: str, source: str = "edi") -> str:
    if kind == "carryover":
        noun = "Dodacie listy" if source == "desadv" else "Objednávky"
        return (f"<p>&#9989; {noun} boli prijaté v Codexe — import do ORIONu je v "
               "poriadku.</p>")
    return "<p>&#9989; Import do ORIONu je opäť v poriadku.</p>"


# --- incidents (durable, per channel + kind + source) -----------------------
#
# Review finding on PR #184: the first cut cleared/counted incidents off a GLOBAL proxy
# ("something, somewhere, was imported" / a blindly-incremented counter). Reproduced live:
# an unrelated healthy order importing on a DIFFERENT channel falsely closed a still-open
# incident (a genuine stuck file never got a second alert), and a persistently-rediscovered
# carryover row inflated its own incident's reported count without bound (≈49 after one
# reminder cycle for a SINGLE stuck file). Both are fixed the same way: incident membership
# is tracked per ROW, per LEDGER (`import_alert_incident_members`/
# `import_alert_incident_desadv_members`, each PRIMARY KEY-deduped per incident), so both
# "how many files, really" and "is THIS incident's own set of files actually resolved" are
# answered from the incident's own members, never a proxy. #203 adds the `source` column so
# a DESADV incident can never share (channel, kind) with an ORDER_ one.

def _open_incident(conn, channel_id: int, kind: str,
                   ledger: _Ledger = EDI_LEDGER) -> dict | None:
    """The open incident + `file_count` = its WHOLE membership, imported members included —
    right for a `failed`/`unknown` reminder, never the count a carryover alert prints
    (`_pending_members`, #476)."""
    row = conn.execute(
        """SELECT id, opened_at, last_alert_at FROM import_alert_incidents
            WHERE channel_id = %s AND kind = %s AND source = %s AND closed_at IS NULL
            ORDER BY id DESC LIMIT 1""", (channel_id, kind, ledger.source)).fetchone()
    if not row:
        return None
    count = conn.execute(
        f"SELECT count(*) FROM {ledger.members_table} WHERE incident_id = %s",
        (row[0],)).fetchone()[0]
    return {"id": row[0], "opened_at": row[1], "last_alert_at": row[2],
            "file_count": int(count)}


def _add_members(conn, incident_id: int, rows: list[dict],
                 ledger: _Ledger = EDI_LEDGER) -> None:
    """De-duplicated by construction (PRIMARY KEY (incident_id, <ledger fk col>)) — a
    carryover row rediscovered on every throttle cycle while still unresolved is only
    ever counted ONCE, which is what keeps `file_count` accurate."""
    for row in rows:
        conn.execute(
            f"""INSERT INTO {ledger.members_table} (incident_id, {ledger.members_fk_col})
               VALUES (%s, %s) ON CONFLICT DO NOTHING""", (incident_id, row["id"]))


def _pending_members(conn, incident_id: int, dirs: dict, ledger: _Ledger) -> list[dict]:
    """The incident's members REALLY still waiting right now (#476): no terminal status in
    the ledger AND `_decide` on this sweep's own listing still finds the file in its queued
    folder. An imported member stays in the incident but is never counted or listed again
    (the live bug reminded „4" for one waiting file) — including one whose own throttled
    re-check has not run yet while its file already sits in archCodex."""
    c1, c2 = ledger.identity_cols
    rows = conn.execute(
        f"""SELECT e.id, e.{c1}, e.{c2}, e.filename, e.uploaded_at
              FROM {ledger.members_table} m
              JOIN {ledger.table} e ON e.id = m.{ledger.members_fk_col}
             WHERE m.incident_id = %s AND e.import_status IS NULL
             ORDER BY e.uploaded_at, e.id""", (incident_id,)).fetchall()
    return [m for m in _row_dicts(rows, ledger) if _decide(m, dirs, ledger) is None]


def _check_incidents_for_clear(conn, cfg, post, now: datetime) -> None:
    """An incident closes once EVERY row that was ever part of it has left the pending
    state — self-healed to `imported`, or reclassified under a DIFFERENT kind's own
    incident (either way, no longer THIS incident's concern) — a per-incident,
    member-scoped check, never a global "something, somewhere, was imported" proxy.

    Only `carryover` incidents ever auto-clear this way. `failed`/`unknown` are genuine
    anomalies (a file that landed in "unconfirmed", or vanished entirely) with no
    automatic re-resolution signal at all — auto-clearing them off an unrelated
    coincidence would be the exact same false-signal bug in a different shape, so they
    stay open (silently deduped, periodically reminded) until a human resolves them.
    """
    open_incidents = conn.execute(
        """SELECT id, channel_id, source FROM import_alert_incidents
            WHERE closed_at IS NULL AND kind = 'carryover'""").fetchall()
    for incident_id, channel_id, source in open_incidents:
        ledger = DESADV_LEDGER if source == "desadv" else EDI_LEDGER
        still_pending = conn.execute(
            f"""SELECT 1 FROM {ledger.members_table} m
                JOIN {ledger.table} e ON e.id = m.{ledger.members_fk_col}
               WHERE m.incident_id = %s AND e.import_status IS NULL
               LIMIT 1""", (incident_id,)).fetchone()
        if still_pending:
            continue
        html = _all_clear_html("carryover", ledger.source)
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
        log.info("import-alert incident #%s (carryover/%s/%s) cleared", incident_id,
                 channel_id, ledger.source)


def _handle_group(conn, cfg, post, channel_id: int, kind: str, rows: list[dict],
                  now: datetime, reminder_hours: int,
                  ledger: _Ledger = EDI_LEDGER, *, dirs: dict, read_files) -> bool:
    """Returns whether this group is now safely ACCOUNTED FOR — either a fresh incident's
    opening alert genuinely delivered, or it was folded into an already-open incident
    (which already communicated the condition once; a failed REMINDER is a lesser,
    retryable concern that must never block marking these rows). False only means "the
    very first alert for this condition has not reached anyone yet" — the caller must
    leave those rows retryable, never mark them terminal.

    A carryover alert (#476) counts + lists only the files still waiting: the opening one
    its own `rows` (every file of this channel/source still waiting — `sweep` widens a
    carryover group past the throttled due rows), a reminder the incident's
    `_pending_members` against this sweep's `dirs`. There is no "reminder about nothing":
    a carryover group exists only while some file is still waiting, and those rows are
    members by the time the reminder is built; once nothing waits there is no group, and
    `_check_incidents_for_clear` closes the incident. `read_files` reads a waiting DESADV
    for the CODEX code check (`confirm_carryover`)."""
    incident = _open_incident(conn, channel_id, kind, ledger)
    if incident is None:
        if kind == "carryover":
            html = _carryover_html(conn, rows, ledger, reminder=False,
                                   read_files=read_files, now=now)
        else:
            html = _group_html(kind, rows, ledger.source)
        try:
            result = post(cfg, html, channel_id=channel_id)
        except Exception:
            log.exception("posting the grouped import-alert failed (%s/%s/%s) — will "
                          "retry next sweep", kind, channel_id, ledger.source)
            return False
        if result is None:
            log.warning("grouped import-alert (%s/%s/%s) was not delivered (Odoo not "
                       "configured?) — will retry next sweep", kind, channel_id,
                       ledger.source)
            return False
        new_id = conn.execute(
            """INSERT INTO import_alert_incidents (channel_id, kind, source, opened_at,
                                                   last_alert_at)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (channel_id, kind, ledger.source, now, now)).fetchone()[0]
        _add_members(conn, int(new_id), rows, ledger)
        log.info("opened import-alert incident (%s/%s/%s): %d file(s)", kind, channel_id,
                 ledger.source, len(rows))
        return True

    _add_members(conn, incident["id"], rows, ledger)
    due_for_reminder = (now - incident["last_alert_at"]) >= timedelta(hours=reminder_hours)
    if due_for_reminder:
        if kind == "carryover":
            # AFTER adding members, so this sweep's own stuck rows are among them (never
            # empty: the group's rows were just decided still queued against the same dirs).
            waiting = _pending_members(conn, incident["id"], dirs, ledger)
            log.info("import-alert incident #%s (carryover/%s): reminding about %d waiting "
                     "file(s)", incident["id"], ledger.source, len(waiting))
            html = _carryover_html(conn, waiting, ledger, reminder=True,
                                   read_files=read_files, now=now)
        else:
            # Re-read AFTER adding members so the reminder text's count is accurate.
            current = _open_incident(conn, channel_id, kind, ledger)
            html = _reminder_html(current or incident, now, ledger.source)
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
    return True


# --- the sweep -------------------------------------------------------------

def sweep(conn, cfg, listdir=None, post=None, now=None, read_files=None) -> int:
    """One pass over every confirmed-uploaded, unresolved row in BOTH ledgers (#203:
    edi_sent's ORDER_* uploads and desadv_sent's DESADV_* uploads share one SFTP
    listing). Returns the number of rows that reached a terminal status this pass — a
    carryover row is NEVER terminal, so it never counts even while it's being alerted
    on.

    `read_files(names)` (#476, default `upload.read_files` on `in_DL`, READ-ONLY) is only
    called while a carryover alert about a waiting DESADV is being built, and only when the
    CODEX card list is live — never on an ordinary sweep."""
    interval = int(getattr(cfg, "import_confirm_interval_minutes", DEFAULT_INTERVAL_MINUTES)
                   or DEFAULT_INTERVAL_MINUTES)
    post = post or (lambda c, html, **kw: report.post_from_config(
        c, html, channel_id=kw.get("channel_id")))
    read_files = read_files or (lambda names: upload_mod.read_files(
        cfg, getattr(cfg, "orion_dl_dir", upload_mod.DL_DIR), names))
    now = now or datetime.now(UTC)
    reminder_hours = int(getattr(cfg, "import_alert_reminder_hours", DEFAULT_REMINDER_HOURS)
                         or DEFAULT_REMINDER_HOURS)

    edi_rows = due_rows(conn, interval, EDI_LEDGER)
    desadv_rows = due_rows(conn, interval, DESADV_LEDGER)
    if not edi_rows and not desadv_rows:
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
        if edi_rows:
            conn.execute(
                "UPDATE edi_sent SET import_checked_at = now() WHERE id = ANY(%s)",
                ([r["id"] for r in edi_rows],))
        if desadv_rows:
            conn.execute(
                "UPDATE desadv_sent SET import_checked_at = now() WHERE id = ANY(%s)",
                ([r["id"] for r in desadv_rows],))
        return 0

    morning_active = morning_check_active(cfg, now)
    # #255: only pay for the extra read when the evening check could even fire this pass —
    # the common case (most of the day) skips it entirely.
    evening_active = evening_check_active(cfg, now)
    activity_at = _activity_today_at(conn, now) if evening_active else None

    def _stuck(row: dict) -> bool:
        # #255: same 'carryover' kind/grouping either way — a morning-leftover row and a
        # same-day-stuck row are the SAME underlying condition (stuck longer than
        # reasonable), just a different trigger. See the module docstring.
        return ((morning_active and _is_carryover(row, now))
                or (evening_active and _is_same_day_stuck(row, now, activity_at)))

    changed = 0
    groups: dict[tuple[int, str, str], list[dict]] = {}
    for ledger, rows in ((EDI_LEDGER, edi_rows), (DESADV_LEDGER, desadv_rows)):
        c1, c2 = ledger.identity_cols
        for row in rows:
            status = _decide(row, dirs, ledger)
            if status is None:
                conn.execute(
                    f"UPDATE {ledger.table} SET import_checked_at = now() WHERE id = %s",
                    (row["id"],))
                if _stuck(row):
                    ch = _channel_for(row["filename"], cfg)
                    groups.setdefault((ch, "carryover", ledger.source), []).append(row)
                continue
            if status in _ALERT_STATUSES:
                # NOT marked terminal here — stays pending until its group is genuinely
                # accounted for (see _handle_group's docstring), so a failed FIRST alert
                # never silently loses the row the way #151/PR#179 already fixed once.
                ch = _channel_for(row["filename"], cfg)
                groups.setdefault((ch, status, ledger.source), []).append(row)
                conn.execute(
                    f"UPDATE {ledger.table} SET import_checked_at = now() WHERE id = %s",
                    (row["id"],))
                continue
            # imported — always safe to mark immediately, no alert dependency. Uses the
            # injected `now` (not bare SQL now()) — this is the value
            # `_check_incidents_for_clear` (called below, AFTER this loop) compares
            # against each open incident's `opened_at`, also written from `now` — both
            # sides of that comparison must agree on what "now" means, including in
            # tests.
            conn.execute(
                f"""UPDATE {ledger.table}
                      SET import_status = %s, import_confirmed_at = %s,
                          import_checked_at = now()
                    WHERE id = %s""", (status, now, row["id"]))
            changed += 1
            log.info("%s import check: %s (%s / %s, %s #%s) -> %s", ledger.source,
                     row["filename"], row.get(c1), row.get(c2), ledger.table, row["id"],
                     status)

    # #476 review: each row sits on its OWN 5-minute re-check cycle, so the due rows that
    # trigger a carryover group are often only 1 of N files waiting on that channel. The
    # listing is already in hand, so the alert names (and the incident records) every file
    # still waiting — never a partial list the warehouse would read as complete.
    for (channel_id, kind, source), group_rows in groups.items():
        if kind != "carryover":
            continue
        ledger = DESADV_LEDGER if source == "desadv" else EDI_LEDGER
        seen = {r["id"] for r in group_rows}
        for row in _unresolved_rows(conn, ledger):
            if (row["id"] not in seen and _decide(row, dirs, ledger) is None and _stuck(row)
                    and _channel_for(row["filename"], cfg) == channel_id):
                group_rows.append(row)
        group_rows.sort(key=lambda r: (r["uploaded_at"], r["id"]))

    for (channel_id, kind, source), group_rows in groups.items():
        ledger = DESADV_LEDGER if source == "desadv" else EDI_LEDGER
        accounted_for = _handle_group(conn, cfg, post, channel_id, kind, group_rows, now,
                                      reminder_hours, ledger, dirs=dirs,
                                      read_files=read_files)
        if kind == "carryover":
            continue  # never terminal, whatever happens — self-heals via due_rows
        if accounted_for:
            for row in group_rows:
                conn.execute(
                    f"UPDATE {ledger.table} SET import_status = %s WHERE id = %s",
                    (kind, row["id"]))
                changed += 1

    # Runs AFTER this sweep's own row processing (not before) — a row that just got
    # marked 'imported' in THIS pass can immediately clear an open incident in the SAME
    # call, rather than waiting one extra tick.
    _check_incidents_for_clear(conn, cfg, post, now)
    return changed
