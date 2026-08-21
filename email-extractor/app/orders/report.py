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


def sklad_link(cfg) -> str:
    """The warehouse's `/sklad/<key>` link, built with no HTTP request (the order worker
    runs on its own thread) — see `linkutil.sklad_url`'s docstring for why this is NOT
    `cfg.public_base_url`. Returns "" when `dashboard_base_url` is unset."""
    return linkutil.sklad_url(cfg)


def dl_sklad_link(cfg) -> str:
    """The DELIVERY-NOTES-ONLY nástenka link (#231) — `/sklad-dl/<key>`, a genuinely
    separate signed link/page from `sklad_link` above, so a DL Odoo review message never
    sends the warehouse to a page mixed with unrelated AI-orders questions. Same "no HTTP
    request" reasoning and the same "" fallback as `sklad_link`."""
    return linkutil.dl_url(cfg)


def dashboard_link(cfg) -> str:
    """#336: the admin dashboard base URL (the ops fix-queue / message list) — the "miesto
    akcie" a grouped OPERATOR alert points at, where an operator reclassifies / marks a
    stuck message done. Distinct from `sklad_link`/`dl_sklad_link` (the warehouse nástenka):
    an unclassifiable / never-started / upload-failed message is an operator concern, not a
    nástenka question. Returns "" when `dashboard_base_url` is unset (the header then simply
    omits the action link, same "" fallback as the sklad links)."""
    return (getattr(cfg, "dashboard_base_url", "") or "").rstrip("/")


def link_line(link: str) -> str:
    """The ONE shared "go resolve this on the nástenka" line — the exact markup/wording
    both this module's own `build_summary` (orders) and `dl_report.py` (DL, #229
    follow-up) use, so the two notify paths can never drift on how a "you have
    something to resolve" hint is phrased. The CALLER decides whether this message
    actually needs it (empty link -> no line); this function only owns the rendering."""
    if not link:
        return ""
    return f'<p>&#128203; Rieš na nástenke: <a href="{escape(link)}">{escape(link)}</a></p>'


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Slovak has three plural forms for a small count: 1 / 2-4 / 0,5+."""
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def build_summary(customer_name: str, orders: list[dict], new_questions: int = 0,
                  unverified_count: int = 0, link: str = "", notes: str = "",
                  cfg=None) -> str:
    """The ONE Odoo message for a whole processed e-mail.

    `orders` is a list of AGGREGATE per-order summaries — never raw decisions or items:
    `{"status": "ok"|"partial"|"held"|"review"|"error", "delivery_date": str,
    "item_count": int, "missing_count": int, "reject_reason": str, "change": bool}`.
    `change` (#159) marks a "review" order whose reason is a change-of-order — it is
    ALWAYS resolved by hand in ORION and NOTHING is ever queued for it, so it gets its own
    wording and neither link, never the generic "review" bucket. That shape is what makes
    it structurally impossible for a TRACE or a RUN ID to leak into Odoo — the function
    simply never receives them. The one deliberate, sanctioned exception (#162):
    `reject_reason` MAY carry a specific ITEM WORDING when an order stays held on a line
    that could not even be turned into a warehouse question (`hold._post_still_held`) —
    the warehouse genuinely needs to see WHICH line is stuck, and #162's own "never
    silently drop a line" requirement outranks the general no-item-detail rule for that
    one narrow case. `reject_reason` is still always `escape()`d below, so this is a
    visibility choice, never an injection risk.

    `unverified_count` (#139 review finding) is the AGEL-incident phantom-item safeguard
    (`extract.py`'s `unverified` — a model-claimed item the e-mail text does not prove) —
    it is an E-MAIL-level count, not per-order (the same list is shared by every order
    derived from one e-mail), so the caller sums it ONCE, not per order.

    `notes` (#187 review finding) is `extract.run()`'s own short, already-human-readable
    notice — e.g. a still-ahead date named only in quoted ('>') text that never became an
    order (#187), or a deliveryDate the source text never wrote (#163). Before this
    parameter existed the value was computed and stored in `order_runs.result`/
    `held_orders`, but never actually rendered anywhere a human reads outcomes — silently
    write-only. Same short-plain-Slovak-sentence shape as `reject_reason`, so it renders
    the same way: `escape()`d, its own paragraph, never a raw trace/JSON/run id.

    `cfg` (#359) is used ONLY to render a clickable admin-dashboard link on the
    "treba doriešiť" fallback line (via `dashboard_link(cfg)`) when a review/error/
    unverified outcome carries no `/sklad` `link` of its own. `None` (the default) or an
    unset `dashboard_base_url` keeps the plain no-link sentence, so every existing caller
    that omits it is unaffected.
    """
    orders = orders or []
    counts: dict[str, int] = {}
    change_count = 0
    total_items = 0
    total_missing = 0
    dates: list[str] = []
    reasons: list[str] = []
    for o in orders:
        status = o.get("status") or "review"
        is_change_order = status == "review" and bool(o.get("change"))
        if is_change_order:
            change_count += 1
        else:
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
    if change_count:
        bits.append(f"{STATUS_ICON['review']} {change_count} " +
                    _plural(change_count, "žiadosť o zmenu", "žiadosti o zmenu",
                            "žiadostí o zmenu") + " &mdash; uprav ju ručne v ORIONe")
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

    # #187 review finding: the extraction stage's own notice (a dropped quoted order, an
    # ungrounded date, ...) must actually reach a human, not just order_runs.result/logs.
    if notes:
        parts.append(f"<p>&#128221; {escape(notes)}</p>")

    # #159: the link routes to where something is ACTUALLY waiting.
    #   - held / partial / a fresh question this run: by construction there is a real,
    #     genuinely open `order_questions`/`held_orders` row — the /sklad link (or, when
    #     no dashboard_base_url is configured, the same generic hint as the other case).
    #   - review / error / unverified-only: none of these ever write anything to the
    #     board (an unmatched customer past its deadline, an extraction-level reject, a
    #     failed upload, a phantom-item warning the ADMIN dashboard resolves) — point at
    #     the dashboard generically, never claim something is "waiting" on the sklad key.
    #   - a change-of-order ALONE: nothing is EVER queued for it (always a human editing
    #     ORION by hand, already stated in its own reason paragraph above) — neither link.
    has_board_item = bool(counts.get("held") or counts.get("partial") or new_questions)
    has_other_action = bool(counts.get("review") or counts.get("error") or unverified_count)
    if has_board_item or has_other_action:
        if has_board_item and link:
            parts.append(link_line(link))
        else:
            # #359: carry the CLICKABLE admin-dashboard URL when one is configured, the same
            # way `dl_alerts._format_grouped` does — every actionable Odoo message must carry
            # its functional URL. Falls back to the plain sentence only when no
            # `dashboard_base_url` is configured (or `cfg` was not passed).
            base = dashboard_link(cfg)
            if base:
                parts.append('<p>&#128203; Treba doriešiť — otvor dashboard: '
                             f'<a href="{escape(base)}">{escape(base)}</a></p>')
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


def post_from_config(cfg, html: str, transport=None, channel_id: int | None = None) -> dict | None:
    """Post using the add-on options; returns None when Odoo is not configured.

    `channel_id` (#151) overrides `cfg.orders_channel_id` — a delivery-note import alert
    routes to the delivery-notes channel instead of the orders one. Omitted/falsy keeps
    the original behaviour (every existing caller), so this is purely additive."""
    channel = int(channel_id) if channel_id else int(getattr(cfg, "orders_channel_id", 0) or 0)
    if not (getattr(cfg, "odoo_url", "") and getattr(cfg, "odoo_api_key", "") and channel):
        log.warning("Odoo not configured — report not delivered")
        return None
    return post(cfg.odoo_url, cfg.odoo_api_key, getattr(cfg, "odoo_db", "odoo"),
                channel, html, transport=transport)


def ops_channel(cfg) -> int:
    """#310: the Odoo channel an OPERATOR/diagnostic alert (engine-liveness/staleness,
    the monthly spend-cap tripwire, internal watchdogs) must route to — NEVER the
    warehouse (243) / sales (152) channels a real person reads and cannot act on.

    Returns `ops_channel_id` (0 when unset). A caller passing 0 to `post_from_config`
    resolves to "Odoo not configured" -> the alert stays in the app log (+ a durable
    outbox row where the caller uses one), never on a warehouse channel. Deliberately
    NOT a fallback to `orders_channel_id`: an operator alert on the sales desk is the
    exact bug this ticket fixes.
    """
    return int(getattr(cfg, "ops_channel_id", 0) or 0)


# --- timeline ------------------------------------------------------------

def log_event(conn, message_id: str, stage: str, status: str, outcome: str = "",
              detail: dict | None = None, rollup: bool = True,
              workflow: str | None = None) -> None:
    """One row in the shared `email_events` timeline.

    The existing rollup trigger copies stage/status/outcome (and edi_file/orion_path)
    onto the message, which is what the dashboard reads — so this is also how the
    Python engine keeps the dashboard truthful.

    `workflow` (#133) overrides the module default (`WORKFLOW = "ai_orders"`) — the
    static-orders engine passes `workflow="static_orders"` so the admin timeline never
    mislabels a static order's own event as belonging to the AI pipeline. Every existing
    caller keeps its old behaviour unchanged (still tags "ai_orders") by simply not
    passing it.
    """
    conn.execute(
        """INSERT INTO email_events
               (message_id, workflow, stage, status, outcome, detail, rollup)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (message_id, workflow or WORKFLOW, stage, status, outcome, Json(detail or {}), rollup))


def crash_outcome(exc: BaseException, stage: str) -> str:
    """A human-visible outcome line for an UNEXPECTED crash (#330) — always carries the
    exception TYPE + MESSAGE + the stage it happened in, so a message that a worker's
    catch-all `except` leaves stranded is never an undiagnosable "neznáma". Even an
    exception with an empty message still names its type, so the "neznáma"/empty result
    the n8n "Static auto orders" workflow used to produce is structurally impossible.
    Truncated to keep `proc_outcome` short."""
    msg = str(exc).strip() or "(bez textu)"
    return f"Spracovanie zlyhalo v kroku {stage}: {type(exc).__name__}: {msg}"[:300]
