"""Reporting: the Odoo message and the event timeline (#65, shortened #139).

**Exactly ONE message per processed e-mail (#139).** The old shape posted one message per
order AND one per new warehouse question — a single e-mail with 5 delivery dates and 4
questions produced 6 separate Odoo messages within 3 seconds, read on the phone as "a lot
of orders failed". `build_summary` replaces that: one short headline for the whole e-mail,
counted by outcome, with a link to the warehouse's nástenka (`/sklad/<key>`) whenever
anything needs a human. Item-level detail — names, traces, JSON, run ids — never reaches
Odoo at all; it lives on the linked page. Nothing is silently dropped by shortening: every
unresolved order and every open question is still COUNTED here, and the linked page is
where it is actually resolved (`pipeline.py` is what accumulates the counts and posts
exactly once per run — see its docstring).

Delivery is Odoo's `discuss.channel/message_post` with `body_is_html` — `mail.message/
create` posts without notifying anyone.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from html import escape

from psycopg.types.json import Json

from .. import linkutil

log = logging.getLogger("orders.report")

WORKFLOW = "ai_orders"
TIMEOUT = 30

STATUS_ICON = {"ok": "&#9989;", "partial": "&#9888;&#65039;", "held": "&#8987;",
               "review": "&#10071;", "error": "&#128721;"}
STATUS_LABEL = {"ok": "nahraté do ORIONu", "partial": "neúplných (chýba časť položiek)",
                "held": "čaká na odpoveď skladu", "review": "treba zadať ručne",
                "error": "zlyhalo pri odosielaní"}
# Orders in these states are the reason a warehouse action is needed — everything else
# (a clean "ok") never needs the link.
NEEDS_ACTION = ("partial", "held", "review", "error")


def sklad_link(cfg) -> str:
    """The warehouse's `/sklad/<key>` link, built with no HTTP request (the order worker
    runs on its own thread) — see `linkutil.sklad_url`'s docstring for why this is NOT
    `cfg.public_base_url`. Returns "" when `dashboard_base_url` is unset."""
    return linkutil.sklad_url(cfg)


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Slovak has three plural forms for a small count: 1 / 2-4 / 0,5+."""
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def build_summary(customer_name: str, orders: list[dict], new_questions: int = 0,
                  unverified_count: int = 0, link: str = "") -> str:
    """The ONE Odoo message for a whole processed e-mail.

    `orders` is a list of AGGREGATE per-order summaries — never raw decisions or items:
    `{"status": "ok"|"partial"|"held"|"review"|"error", "delivery_date": str,
    "item_count": int, "missing_count": int, "reject_reason": str}`. That shape is what
    makes it structurally impossible for an item name, a trace or a run id to leak into
    Odoo — the function simply never receives them.

    `unverified_count` (#139 review finding) is the AGEL-incident phantom-item safeguard
    (`extract.py`'s `unverified` — a model-claimed item the e-mail text does not prove) —
    it is an E-MAIL-level count, not per-order (the same list is shared by every order
    derived from one e-mail), so the caller sums it ONCE, not per order.
    """
    orders = orders or []
    counts: dict[str, int] = {}
    total_items = 0
    total_missing = 0
    dates: list[str] = []
    reasons: list[str] = []
    for o in orders:
        status = o.get("status") or "review"
        counts[status] = counts.get(status, 0) + 1
        total_items += int(o.get("item_count") or 0)
        if status == "partial":
            total_missing += int(o.get("missing_count") or 0)
        d = str(o.get("delivery_date") or "")
        if d and d not in dates:
            dates.append(d)
        reason = str(o.get("reject_reason") or "")
        if reason and reason not in reasons:
            reasons.append(reason)

    who = escape(customer_name or "(nezistený zákazník)")
    head = f"<b>{who}</b>"
    n = len(orders)
    if n:
        head += (f" &mdash; {n} " + _plural(n, "objednávka", "objednávky", "objednávok"))
        if total_items:
            head += (f", {total_items} " +
                     _plural(total_items, "položka", "položky", "položiek"))
        if dates:
            head += ", termín " + ", ".join(escape(d) for d in dates)
    parts = [f"<p>{head}</p>"]

    bits = []
    for status in ("ok", "partial", "held", "review", "error"):
        if not counts.get(status):
            continue
        if status == "partial" and total_missing:
            bits.append(f"{STATUS_ICON['partial']} {counts['partial']} neúplných "
                        f"(spolu chýba {total_missing} " +
                        _plural(total_missing, "položka", "položky", "položiek") + ")")
        else:
            bits.append(f"{STATUS_ICON[status]} {counts[status]} {STATUS_LABEL[status]}")
    if new_questions:
        bits.append(f"&#10067; {new_questions} " +
                    _plural(new_questions, "nová otázka", "nové otázky", "nových otázok") +
                    " pre sklad")
    if unverified_count:
        bits.append(f"&#128269; {unverified_count} " +
                    _plural(unverified_count, "položka sa nedala overiť v texte",
                            "položky sa nedali overiť v texte",
                            "položiek sa nedalo overiť v texte"))
    if bits:
        parts.append("<p>" + " &nbsp;|&nbsp; ".join(bits) + "</p>")

    # Short, already-human reasons (never a traceback/JSON — those never reach this
    # function) — capped, so a pathological number of distinct failures still stays short.
    for reason in reasons[:3]:
        parts.append(f"<p>{escape(reason)}</p>")

    needs_link = (bool(new_questions) or bool(unverified_count)
                 or any(counts.get(s) for s in NEEDS_ACTION))
    if needs_link:
        if link:
            parts.append(f'<p>&#128203; Rieš na nástenke: '
                         f'<a href="{escape(link)}">{escape(link)}</a></p>')
        else:
            parts.append("<p>&#128203; Treba doriešiť — otvor dashboard extraktora.</p>")

    return "".join(parts)


# --- delivery ------------------------------------------------------------

def _http(url: str, headers: dict, payload: dict) -> dict:
    req = urllib.request.Request(url, method="POST", data=json.dumps(payload).encode(),
                                 headers={**headers, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:   # noqa: S310 (configured host)
        body = resp.read().decode()
    return json.loads(body) if body.strip() else {}


def post(url: str, api_key: str, db: str, channel_id: int, html: str,
         transport=None) -> dict:
    """Post the message into an Odoo Discuss channel.

    `message_post` (not `mail.message/create`) is what notifies the followers, and
    `body_is_html` is required or the tags show up as raw text.
    """
    endpoint = url.rstrip("/") + "/json/2/discuss.channel/message_post"
    headers = {"Authorization": f"Bearer {api_key}", "X-Odoo-Database": db}
    payload = {"ids": [int(channel_id)], "body": html, "body_is_html": True,
               "message_type": "comment", "subtype_xmlid": "mail.mt_comment"}
    return (transport or _http)(endpoint, headers, payload)


def post_from_config(cfg, html: str, transport=None) -> dict | None:
    """Post using the add-on options; returns None when Odoo is not configured."""
    if not (getattr(cfg, "odoo_url", "") and getattr(cfg, "odoo_api_key", "")
            and getattr(cfg, "orders_channel_id", 0)):
        log.warning("Odoo not configured — report not delivered")
        return None
    return post(cfg.odoo_url, cfg.odoo_api_key, getattr(cfg, "odoo_db", "odoo"),
                cfg.orders_channel_id, html, transport=transport)


# --- timeline ------------------------------------------------------------

def log_event(conn, message_id: str, stage: str, status: str, outcome: str = "",
              detail: dict | None = None, rollup: bool = True) -> None:
    """One row in the shared `email_events` timeline.

    The existing rollup trigger copies stage/status/outcome (and edi_file/orion_path)
    onto the message, which is what the dashboard reads — so this is also how the
    Python engine keeps the dashboard truthful.
    """
    conn.execute(
        """INSERT INTO email_events
               (message_id, workflow, stage, status, outcome, detail, rollup)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (message_id, WORKFLOW, stage, status, outcome, Json(detail or {}), rollup))
