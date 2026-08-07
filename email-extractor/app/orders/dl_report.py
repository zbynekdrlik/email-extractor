"""Odoo reporting for delivery notes (DL) — #204, DL migration F5.

**Deliberately a SEPARATE module from `app/orders/report.py`, not a reuse of
`build_summary` (#139's "exactly ONE message per processed E-MAIL").** R95/R96 (the
binding spec, `docs/superpowers/specs/2026-08-07-delivery-notes-python-design.md`) is a
genuinely DIFFERENT policy for DL: ONE Odoo message per DOCUMENT (a DL), not per e-mail —
one mail can legitimately carry several delivery notes across its attachments (F2's own
multi-document fix, W1a/W1b), and each gets its own success/review message. Conflating the
two would force `build_summary` to reason about a policy it was explicitly built to NOT
have (same "keep unrelated rules apart" reasoning `dl_extract.py`'s own module docstring
gives for staying standalone from `app/orders/extract.py`).

Three message shapes:

- `build_success` (R95) — one ✅ per successfully-built DESADV (partial EDI included,
  R81: unmatched items are surfaced in the SAME message, never silently dropped).
- `build_review` (R96) — one ❗ when a document could not become an EDI at all (supplier
  not matched, zero real items, extraction-level `needsReview`, a service failure that
  exhausted its retries).
- `build_announced_mismatch` (spec §4) — the "second DL in one mail" fix: a supplier's own
  subject line announces MORE delivery-note numbers than were actually attached/extracted
  (the Lunys "IS KARAT" incident this whole phase exists to close).

Duplicate documents (W7) and announced/attached mismatches are NEVER posted here as their
own immediate message — visibility for those is the DAILY digest
(`reliability.dl_provenance_stats_for_day`, extended #204) via `email_events`, per the
binding spec's explicit "hlásenie v dennom sumári, nie ticho" (report in the daily
summary, never silently) for W7 specifically. `log_duplicate`/`log_announced_mismatch`
below only write that event; they never call Odoo. `log_already_shipped_this_run`
(#216) is a THIRD, deliberately different, non-Odoo, non-counted event: a retry after
a partial ship re-skipping a document THIS SAME message already sent — never a real
W7 cross-message duplicate, so it must not inflate that count.
"""
from __future__ import annotations

from html import escape

from . import report

WORKFLOW = "delivery_notes"


def _channel(cfg) -> int:
    """Falls back to `orders_channel_id` inside `report.post_from_config` itself when
    unset — same convention `orders/confirm.py`'s own `_channel_for` already uses."""
    return int(getattr(cfg, "delivery_notes_channel_id", 0) or 0)


def _fmt_qty(value) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value if value is not None else "")
    if f == int(f):
        return str(int(f))
    return f"{f:.3f}".rstrip("0").rstrip(".")


def _meta_lines(**fields) -> str:
    bits = [f"{label}: {escape(str(value))}" for label, value in fields.items() if value]
    return "<p>" + "<br>".join(bits) + "</p>" if bits else ""


# --- R95: one success message per DL ----------------------------------------

def build_success(supplier_name: str, doc_number: str, delivery_date: str,
                  from_addr: str, subject: str, shipped_items: list[dict],
                  unmatched_items: list[str] | None = None,
                  borderline_notes: list[str] | None = None,
                  history_notes: list[str] | None = None,
                  price_substitutions: list[str] | None = None,
                  filename: str = "", partial: bool = False, link: str = "") -> str:
    """`shipped_items`: `[{"name", "quantity", "unit"}]` — only what actually shipped in
    the EDI. The other four lists are already-human-readable Slovak sentences (mirrors
    `report.build_summary`'s own "never a trace/JSON, only short prose" discipline)."""
    partial_tag = " &mdash; ČIASTOČNE (chýbajú niektoré položky)" if partial else ""
    parts = [f"<p><b>&#9989; Dodací list spracovaný{partial_tag}</b></p>"]
    parts.append(_meta_lines(**{"Od": from_addr, "Predmet": subject,
                                "Dodávateľ": supplier_name,
                                "Číslo DL": doc_number or "?",
                                "Dátum dodania": delivery_date or "?"}))
    if shipped_items:
        lines = "".join(
            f"<li>{escape(str(i.get('name') or ''))} "
            f"({_fmt_qty(i.get('quantity'))} {escape(str(i.get('unit') or ''))})</li>"
            for i in shipped_items)
        parts.append(f"<p>Položky:</p><ul>{lines}</ul>")
    if price_substitutions:
        lines = "".join(f"<li>{escape(s)}</li>" for s in price_substitutions)
        parts.append(f"<p>&#128182; Cena doplnená/opravená z tabuľky:</p><ul>{lines}</ul>")
    if unmatched_items:
        n = len(unmatched_items)
        lines = "".join(f"<li>{escape(s)}</li>" for s in unmatched_items)
        parts.append(f"<p>&#9888;&#65039; Nespárované položky ({n}) &mdash; EDI šlo BEZ "
                     f"nich, doplniť do katalógu:</p><ul>{lines}</ul>")
    if borderline_notes:
        lines = "".join(f"<li>{escape(s)}</li>" for s in borderline_notes)
        parts.append(f"<p>&#9888;&#65039; Prešlo na hranici istoty:</p><ul>{lines}</ul>")
    if history_notes:
        lines = "".join(f"<li>{escape(s)}</li>" for s in history_notes)
        parts.append("<p>&#128216; Gramáž nesúhlasí s kartou, ale história dodávok "
                     f"potvrdzuje:</p><ul>{lines}</ul>")
    if filename:
        parts.append(f"<p>Súbor: {escape(filename)}</p>")
    if link:
        parts.append(f'<p>&#128203; Nástenka: '
                     f'<a href="{escape(link)}">{escape(link)}</a></p>')
    return "".join(p for p in parts if p)


# --- R96: needs-review message ----------------------------------------------

def build_review(reason: str, supplier_name: str = "", doc_number: str = "",
                 delivery_date: str = "", from_addr: str = "", subject: str = "") -> str:
    parts = ["<p><b>&#10071; Dodací list potrebuje kontrolu</b></p>"]
    parts.append(_meta_lines(**{"Od": from_addr, "Predmet": subject,
                                "Dodávateľ": supplier_name, "Číslo DL": doc_number,
                                "Dátum dodania": delivery_date}))
    parts.append(f"<p>{escape(reason or 'Neznámy dôvod')}</p>")
    return "".join(p for p in parts if p)


# --- spec §4: announced-vs-attached mismatch --------------------------------

def build_announced_mismatch(subject: str, from_addr: str, announced: list[str],
                             attached: list[str]) -> str:
    ann = ", ".join(escape(a) for a in announced) or "?"
    att = ", ".join(escape(a) for a in attached) or "žiadne"
    return ("<p><b>&#9888;&#65039; Dodávateľ ohlásil viac dodacích listov, než "
           "priložil</b></p>"
           f"<p>Od: {escape(from_addr or '')}<br>Predmet: {escape(subject or '')}</p>"
           f"<p>Ohlásené čísla v predmete: {ann}<br>Skutočne priložené: {att}</p>"
           "<p>Chýbajúce dodacie listy treba od dodávateľa vyžiadať znova.</p>")


# --- delivery ----------------------------------------------------------------

def post(cfg, html: str, transport=None) -> dict | None:
    """Post through the delivery-notes channel (falls back to `orders_channel_id` when
    unset, same convention `confirm.py`'s `_channel_for` already uses)."""
    return report.post_from_config(cfg, html, transport=transport, channel_id=_channel(cfg))


# --- W7: duplicate documents reported in the DAILY digest, never immediately ------

def log_duplicate(conn, message_id: str, doc_number: str, supplier_ean: str) -> None:
    report.log_event(
        conn, message_id, stage="duplicate_skip", status="ok",
        outcome=f"Dodací list {doc_number} od dodávateľa {supplier_ean} už bol odoslaný "
                "skôr — preskočené (nahlásené v dennom súhrne)",
        detail={"doc_number": doc_number, "supplier_ean": supplier_ean},
        rollup=False, workflow=WORKFLOW)


def log_already_shipped_this_run(conn, message_id: str, doc_number: str,
                                 supplier_ean: str) -> None:
    """#216: THIS SAME message already shipped this document in an earlier attempt —
    R17's transient retry re-processes the WHOLE message, including documents that
    already succeeded before a later document in the same mail hit a transient
    failure. This is a self-caused re-skip, never a genuine W7 cross-message
    duplicate, so it gets its OWN stage — deliberately NOT `duplicate_skip` — so
    `reliability.dl_provenance_stats_for_day()`'s `duplicates` count (filtered on
    `stage = 'duplicate_skip'`) stays a trustworthy signal of real supplier
    re-announcements only."""
    report.log_event(
        conn, message_id, stage="already_shipped_this_run", status="ok",
        outcome=f"Dodací list {doc_number} od dodávateľa {supplier_ean} bol v tomto "
                "behu už odoslaný skôr (opakovaný pokus po čiastočnom odoslaní) — "
                "preskočené",
        detail={"doc_number": doc_number, "supplier_ean": supplier_ean},
        rollup=False, workflow=WORKFLOW)


def log_announced_mismatch(conn, message_id: str, subject: str, announced: list[str],
                           attached: list[str]) -> None:
    report.log_event(
        conn, message_id, stage="announced_mismatch", status="review",
        outcome=f"Predmet ohlásil {len(announced)} DL, priložených bolo "
                f"{len(attached)} — chýba {sorted(set(announced) - set(attached))}",
        detail={"subject": subject, "announced": announced, "attached": attached},
        rollup=False, workflow=WORKFLOW)
