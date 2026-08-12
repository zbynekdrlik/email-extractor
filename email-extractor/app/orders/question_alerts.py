"""Stale-question reminders (#237): a warehouse board question (`order_questions`) that
stays `open` too long gets NO further signal today — the ONLY thing that ever notifies
Odoo about it is the one-shot `on_new` callback `teach.ask`/`ask_generic`/`ask_dl_item`/
`ask_dl_supplier` fire at CREATION time. Live evidence (see the #237 issue comments for
the full validation + design write-up): HK LOAN (`gnip@hkloan.eu`) failed the same
unregistered-supplier hole 6 times between 2026-07-06 and 2026-08-07; the one formal
board question it eventually raised (`dl_supplier`, 2026-08-11) sat open with zero
reminder ever sent.

Design (short version — the #237 design comment has the full root-cause/approach/
rejected-alternative write-up):

  * GROUPED — at most ONE Odoo message per (audience, level) per sweep, never one per
    question. Same discipline `confirm.py`/`dl_alerts.py` already enforce (the
    2026-08-05 flood of 5 separate alerts the user deleted is the standing precedent
    this whole codebase avoids repeating).
  * Two-level cadence tracked directly on the row — `order_questions.reminder_sent_at`/
    `escalated_at`, two new nullable columns, no separate ledger table (a question
    already IS its own durable, uniquely-identified row). A 'reminder' fires the FIRST
    time a question has been open across >= `question_stale_working_days` distinct
    weekdays; an 'escalation' fires the FIRST time it reaches
    >= `question_escalate_working_days`, and ONLY once at least one full calendar day
    has passed since the reminder (so the two never fire back-to-back in one sweep) —
    then SILENT, no daily nag, until the question is answered.
  * "Working days" = distinct Mon-Fri calendar dates (Europe/Bratislava) the question
    has been open across, INCLUSIVE of both its creation date and today
    (`_weekdays_touched`) — reuses `confirm.morning_check_active` (the SAME
    skip-Saturday/skip-Sunday/morning-hour config `confirm.py`'s own import-carryover
    check already uses) to gate the WHOLE sweep: nothing is evaluated before the
    warehouse's working day has actually started, and nothing fires on a skipped
    weekend day — a reminder that landed Saturday evening would be exactly the noise
    #237 explicitly calls out.
  * Delivery reuses `dl_alerts`'s existing durable retry-until-delivered outbox
    (`pending_alerts`, via `dl_alerts.enqueue`/`flush_pending`) — a reminder that fails
    to post (Odoo down) is never silently lost; the SAME worker tick that already
    flushes DL alerts retries it.
  * A REPEAT is `count(*)` of every `order_questions` row (open + answered) this add-on
    has EVER raised for the exact same identity (`kind`, `customer_ean`, `item_key`) —
    proven from the DB, never a hardcoded number (this project's own "assert only what
    you can prove" convention, `.claude/rules/orders-corpus.md`).
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from html import escape

from . import confirm, dl_alerts, report

log = logging.getLogger("orders.question_alerts")

DEFAULT_STALE_WORKING_DAYS = 2
DEFAULT_ESCALATE_WORKING_DAYS = 4

# Kinds answered on the DELIVERY-NOTES-only nástenka (`/sklad-dl`, `report.dl_sklad_link`)
# and routed to `delivery_notes_channel_id` — every other kind is an AI-orders question,
# routed to `orders_channel_id`. Mirrors `confirm.py`'s own `_channel_for` split.
_DL_KINDS = ("dl_item", "dl_supplier")

_WHAT = {
    "item": "neznáme znenie položky — treba priradiť kartu",
    "customer": "neznámy zákazník — treba priradiť alebo založiť",
    "mail": "je toto vôbec objednávka? — treba potvrdiť",
    "date": "ktorý dátum dodania platí? — treba potvrdiť",
    "line": "patrí tento riadok do objednávky? — treba potvrdiť",
    "dl_item": "neznáme znenie položky na dodacom liste — treba priradiť kartu",
    "dl_supplier": "dodávateľ dodacích listov nie je v databáze — treba ho priradiť "
                  "alebo pridať",
}

_COLS = ("id, kind, customer_ean, customer_name, wording, item_key, context, payload, "
        "created_at, reminder_sent_at, escalated_at")


def _row(r) -> dict:
    return {"id": int(r[0]), "kind": r[1] or "item", "customer_ean": r[2] or "",
            "customer_name": r[3] or "", "wording": r[4] or "", "item_key": r[5] or "",
            "context": r[6] or {}, "payload": r[7] or {}, "created_at": r[8],
            "reminder_sent_at": r[9], "escalated_at": r[10]}


def _local_date(ts: datetime) -> date:
    return ts.astimezone(confirm.LOCAL_TZ).date()


def _weekdays_touched(start: datetime, end: datetime) -> int:
    """Distinct Mon-Fri calendar dates (Europe/Bratislava) from `start` to `end`,
    INCLUSIVE of both endpoints — a question opened Tuesday afternoon and still open
    Wednesday morning has touched 2 working days. This is deliberately "how many
    distinct working days has this survived", not "how many elapsed hours" — see the
    #237 design comment for why."""
    d0, d1 = _local_date(start), _local_date(end)
    if d1 < d0:
        return 0
    n = 0
    d = d0
    while d <= d1:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def _is_dl(kind: str) -> bool:
    return kind in _DL_KINDS


def _who(q: dict) -> str:
    kind = q["kind"]
    if kind == "dl_supplier":
        return q["payload"].get("sender_email") or q["wording"] or "?"
    if kind == "dl_item":
        return q["payload"].get("supplier_name") or q["payload"].get("supplier_ean") or "?"
    if kind == "customer":
        return q["context"].get("sender_email") or q["wording"] or "?"
    if kind == "item":
        return q["customer_name"] or q["customer_ean"] or "?"
    return q["wording"] or "?"


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def _repeat_count(conn, kind: str, customer_ean: str, item_key: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM order_questions WHERE kind = %s AND customer_ean = %s "
        "AND item_key = %s", (kind, customer_ean, item_key)).fetchone()
    return int(row[0] or 1)


def _describe(q: dict, repeat: int) -> str:
    who = escape(str(_who(q)))
    what = _WHAT.get(q["kind"], "otázka na nástenke čaká na odpoveď")
    extra = ""
    if q["kind"] in ("item", "dl_item") and q["wording"]:
        extra = f" &mdash; „{escape(str(q['wording']))}“"
    since = _local_date(q["created_at"]).strftime("%d.%m.")
    line = f"<b>{who}</b>{extra} ({what}, otvorené od {since})"
    if repeat > 1:
        line += f" &mdash; <b>toto sa opakuje už {repeat}&times;</b>"
    return f"<li>{line}</li>"


def _group_html(level: str, rows: list[dict], repeats: dict[int, int], stale_days: int,
                escalate_days: int, link: str) -> str:
    n = len(rows)
    noun = _plural(n, "otázka", "otázky", "otázok")
    if level == "escalation":
        head = (f"<p>&#128680; {n} {noun} na nástenke je STÁLE nezodpovedaných "
               f"(už {escalate_days}+ pracovných dní) &mdash; treba to vyriešiť čo "
               "najskôr:</p>")
    else:
        head = (f"<p>&#9200; {n} {noun} na nástenke čaká na odpoveď dlhšie ako "
               f"{stale_days} pracovné dni:</p>")
    shown = sorted(rows, key=lambda q: q["created_at"])[:15]
    items = "".join(_describe(q, repeats.get(q["id"], 1)) for q in shown)
    parts = [head, f"<ul>{items}</ul>"]
    if n > len(shown):
        parts.append(f"<p>&#8230; a ešte {n - len(shown)} ďalších.</p>")
    link_html = report.link_line(link)
    if link_html:
        parts.append(link_html)
    return "".join(parts)


def sweep(conn, cfg, now: datetime | None = None) -> int:
    """One pass over every OPEN `order_questions` row: find the ones that just crossed
    the reminder or escalation threshold, group them by (audience, level), enqueue ONE
    grouped alert per group via `dl_alerts.enqueue` (durable — a failed post is retried
    by the next `dl_alerts.flush_pending()` tick, never lost), and mark the rows so this
    same threshold never fires twice for them. Returns how many rows were marked.

    Gated on `confirm.morning_check_active` (#237: reuse the SAME "the warehouse works
    weekday mornings only" model `confirm.py`'s own import-carryover check already
    uses) — a stale-question reminder is exactly as much a "nobody is here to read this
    yet" concern as a carryover ORION file already is.
    """
    now = now or datetime.now(UTC)
    if not confirm.morning_check_active(cfg, now):
        return 0

    stale_days = int(getattr(cfg, "question_stale_working_days",
                             DEFAULT_STALE_WORKING_DAYS) or DEFAULT_STALE_WORKING_DAYS)
    escalate_days = int(getattr(cfg, "question_escalate_working_days",
                                DEFAULT_ESCALATE_WORKING_DAYS)
                        or DEFAULT_ESCALATE_WORKING_DAYS)

    raw_rows = conn.execute(
        f"SELECT {_COLS} FROM order_questions WHERE status = 'open'").fetchall()
    if not raw_rows:
        return 0

    due: dict[tuple[bool, str], list[dict]] = {}
    for raw in raw_rows:
        q = _row(raw)
        touched = _weekdays_touched(q["created_at"], now)
        level = None
        if q["reminder_sent_at"] is None:
            if touched >= stale_days:
                level = "reminder"
        elif q["escalated_at"] is None:
            if (touched >= escalate_days
                    and _local_date(q["reminder_sent_at"]) < _local_date(now)):
                level = "escalation"
        if level is None:
            continue
        due.setdefault((_is_dl(q["kind"]), level), []).append(q)

    if not due:
        return 0

    marked = 0
    for (is_dl, level), group_rows in due.items():
        channel = int(getattr(cfg, "delivery_notes_channel_id" if is_dl
                              else "orders_channel_id", 0) or 0)
        link = report.dl_sklad_link(cfg) if is_dl else report.sklad_link(cfg)
        repeats = {q["id"]: _repeat_count(conn, q["kind"], q["customer_ean"],
                                          q["item_key"]) for q in group_rows}
        html = _group_html(level, group_rows, repeats, stale_days, escalate_days, link)
        dl_alerts.enqueue(conn, channel, f"question_{level}", html)
        column = "reminder_sent_at" if level == "reminder" else "escalated_at"
        ids = [q["id"] for q in group_rows]
        conn.execute(
            f"UPDATE order_questions SET {column} = %s WHERE id = ANY(%s)", (now, ids))
        marked += len(ids)
        log.info("enqueued %s question alert (%s): %d question(s)", level,
                 "dl" if is_dl else "orders", len(ids))
    return marked
