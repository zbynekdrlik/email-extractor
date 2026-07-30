"""Reporting: the Odoo message and the event timeline (#65).

The message is the warehouse's only view of what the automat did, so two things are
structural rather than cosmetic:

- **Nothing is dropped silently.** Every item that did not reach the EDI file is listed
  with the reason it did not, and every item that passed on a weaker rule (borderline
  confidence, only-card-of-its-kind, history overriding the weight, a sibling line) is
  named with what decided it.
- **A partial order says so at the TOP.** An incomplete order still ships (user decision
  2026-07-30, after the warehouse retyped 11-14 items because one item failed), so the
  gap must be impossible to overlook.

Delivery is Odoo's `discuss.channel/message_post` with `body_is_html` — `mail.message/
create` posts without notifying anyone.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from html import escape

from psycopg.types.json import Json

log = logging.getLogger("orders.report")

WORKFLOW = "ai_orders"
TIMEOUT = 30

# Rules that ship the item but deserve a human glance, with the heading they appear under.
FLAGGED_RULES = {
    "llm_borderline": "Prešlo na hranici istoty — prosím prekontrolujte",
    "unique_card": "Prešlo ako jediný produkt toho druhu v katalógu — prekontrolujte gramáž",
    "history_weight": "Prešlo podľa histórie dodávok, hoci gramáž nesúhlasí",
    "history": "Prešlo podľa histórie dodávok",
    "sibling": "Prešlo podľa zhodnej položky v tom istom maile",
    "alias_exact_weight": "Prešlo podľa aliasu karty (alias obsahuje gramáž)",
    "alias_customer": "Prešlo podľa aliasu karty, ktorý menuje zákazníka",
}


def _item_line(item: dict, with_reason: bool = True) -> str:
    card = item.get("card") or ""
    head = escape(str(item.get("name") or "(bez názvu)"))
    if card:
        head += " &rarr; " + escape(str(card))
    qty = item.get("quantity")
    if qty is not None:
        head += f' ({escape(str(qty))} {escape(str(item.get("unit") or "ks"))})'
    if with_reason and item.get("note"):
        head += " &mdash; " + escape(" ".join(str(item["note"]).split())[:220])
    return "&bull; " + head


def _section(title: str, lines: list[str]) -> str:
    if not lines:
        return ""
    return f"<p><b>{title}:</b><br>" + "<br>".join(lines) + "</p>"


def build(result: dict) -> str:
    """The Odoo message body (HTML, Slovak)."""
    customer = result.get("customer") or {}
    items = result.get("items") or []
    unverified = result.get("unverified") or []
    shipped = bool(result.get("shipped"))

    missing = [i for i in items if not i.get("gtin")]
    ok = [i for i in items if i.get("gtin")]
    partial = shipped and bool(missing)

    parts: list[str] = []

    # --- the header carries the bad news first ---------------------------
    if shipped:
        head = "Objednávka odoslaná do ORIONu"
        if partial:
            head = "⚠️ NEÚPLNÁ objednávka odoslaná do ORIONu"
        parts.append(f"<h3>{head}</h3>")
    else:
        parts.append("<h3>⚠️ Objednávka NEBOLA odoslaná — treba ju zadať ručne</h3>")

    if partial:
        parts.append(
            "<p><b>Tieto položky v odoslanej objednávke CHÝBAJÚ</b> — treba ich do ORIONu "
            "doplniť ručne:<br>"
            + "<br>".join(_item_line(i) for i in missing) + "</p>")
    if not shipped:
        reason = result.get("reject_reason") or ""
        if reason:
            parts.append(f"<p><b>Dôvod:</b> {escape(reason)}</p>")
        if result.get("is_change_request"):
            prefix = result.get("change_prefix") or ""
            hint = ("Automat objednávku zámerne NEvytvoril — inak by v ORIONe vznikla "
                    "objednávka navyše. Oprav ju prosím <b>ručne</b> v ORIONe")
            if prefix:
                hint += (f": pôvodný súbor od nás začína <b>{escape(prefix)}</b>")
            parts.append(f"<p>{hint}.</p>")
        if missing:
            parts.append(_section("Položky, ktoré sa nedali priradiť",
                                  [_item_line(i) for i in missing]))

    # --- what the customer is / what was ordered ------------------------
    meta = [f"<b>Zákazník:</b> {escape(customer.get('name') or '(nenájdený)')}"]
    if customer.get("ean_edi"):
        meta.append(f"<b>EAN:</b> {escape(str(customer['ean_edi']))}")
    if result.get("delivery_date"):
        meta.append(f"<b>Dátum dodania:</b> {escape(str(result['delivery_date']))}")
    if result.get("order_number"):
        meta.append(f"<b>Číslo objednávky:</b> {escape(str(result['order_number']))}")
    if result.get("edi_filename"):
        meta.append(f"<b>Súbor:</b> {escape(str(result['edi_filename']))}")
    parts.append("<p>" + " &nbsp;|&nbsp; ".join(meta) + "</p>")

    if ok:
        parts.append(_section("Odoslané položky",
                              [_item_line(i, with_reason=False) for i in ok]))

    # --- everything that needs a human glance ---------------------------
    for rule, title in FLAGGED_RULES.items():
        hits = [i for i in items if i.get("rule") == rule and i.get("review")]
        parts.append(_section(title, [_item_line(i) for i in hits]))

    if unverified:
        parts.append(_section(
            "Položky, ktoré sa nedali overiť v texte e-mailu (treba doplniť ručne)",
            ["&bull; " + escape(f'{i.get("name", "?")} ({i.get("quantity", "?")} '
                                f'{i.get("unit", "ks")})') for i in unverified]))

    if result.get("notes"):
        parts.append(f"<p><b>Poznámky z e-mailu:</b> {escape(str(result['notes']))}</p>")

    return "".join(p for p in parts if p)


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
