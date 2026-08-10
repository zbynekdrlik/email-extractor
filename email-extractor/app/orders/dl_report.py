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
  (the Lunys "IS KARAT" incident this whole phase exists to close). Corrected (#229
  follow-up): this IS posted as its own immediate Odoo message, same as `build_success`/
  `build_review` — only the plain W7 `duplicate_skip` event (below) is digest-only, never
  posted immediately. `build_announced_mismatch` now ALSO opens with a short outcome line
  per document that WAS extracted (`documents=`), because a reader who only sees this
  message could not otherwise tell whether the attached DL was processed at all (live
  complaint on run 406: "preco nenapisalo do odoo ze dodaci list bol spracovany").

Duplicate documents (W7) are NEVER posted as their own immediate message — visibility is
the DAILY digest (`reliability.dl_provenance_stats_for_day`, extended #204) via
`email_events`, per the binding spec's explicit "hlásenie v dennom sumári, nie ticho"
(report in the daily summary, never silently). `log_duplicate` below only writes that
event; it never calls Odoo. `log_already_shipped_this_run` (#216) is a THIRD, deliberately
different, non-Odoo, non-counted event: a retry after a partial ship re-skipping a
document THIS SAME message already sent — never a real W7 cross-message duplicate, so it
must not inflate that count. (A duplicate document's outcome CAN still surface as one of
`build_announced_mismatch`'s per-document outcome lines when the SAME message also
triggers an announced-vs-attached mismatch — that message is being posted regardless, so
stating the duplicate's own outcome there is not a new immediate post, just a clearer
existing one.)

The dashboard link (#229 follow-up 2) is included ONLY when there is something the reader
can actually go resolve — a genuine `review` outcome, or a `partial` success that raised a
real `dl_item` board question (never a clean `ok`, even one carrying a purely informational
note) — mirrors `report.build_summary`'s own `has_board_item`/`has_other_action` split for
the orders pipeline, via the shared `report.link_line()` helper so the two notify paths
never render the link differently.
"""
from __future__ import annotations

from html import escape

from . import report

WORKFLOW = "delivery_notes"

# #229 follow-up: per-document outcome icons shared by build_success's headline and the
# outcome lines build_announced_mismatch prepends — one place to keep the two consistent.
_OUTCOME_ICON = {"ok": "&#9989;", "partial": "&#9888;&#65039;", "review": "&#10071;",
                 "duplicate": "&#128257;"}


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
    `report.build_summary`'s own "never a trace/JSON, only short prose" discipline).

    #229 follow-up: the headline now states the doc number and item count explicitly
    ("Dodací list <číslo> spracovaný a nahratý do ORIONu (<N> položiek)") — a reader must
    never have to guess from a bare "spracovaný" whether THIS document went through.
    `link` is rendered ONLY when `unmatched_items` is non-empty — the SAME list
    `dl_worker._process_document` builds `dl_item` board questions from (both driven by
    `not decision.gtin`), so this is exact, not derived from `partial` — a purely
    informational note (borderline confidence, a weight-history override, a price
    backfilled from the catalog) never gets a link, since there is nothing to action on
    the board for it. Deliberately NOT derived from `partial` itself
    (`bool(desadv_edi.build(...).items_skipped_no_match)`): that computation excludes a
    zero-quantity item even when unmatched, so a document whose ONLY unmatched item also
    has quantity 0 can have `partial=False` while a real `dl_item` question was still
    raised — `unmatched_items` has no such gap (`dl_worker`'s own teach.ask_dl_item call
    fires for ANY unmatched item regardless of quantity). `_outcome_needs_link` below
    mirrors this exact precision via `documents_out`'s own `unmatched_items` key, not the
    less precise `outcome` string alone (review finding, PR #232)."""
    n_items = len(shipped_items or [])
    if partial:
        headline = (f"{_OUTCOME_ICON['partial']} Dodací list {escape(doc_number or '?')} "
                   f"spracovaný ČIASTOČNE &mdash; nahratý do ORIONu, chýbajú niektoré "
                   f"položky ({n_items} položiek)")
    else:
        headline = (f"{_OUTCOME_ICON['ok']} Dodací list {escape(doc_number or '?')} "
                   f"spracovaný a nahratý do ORIONu ({n_items} položiek)")
    parts = [f"<p><b>{headline}</b></p>"]
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
    if unmatched_items and link:
        parts.append(report.link_line(link))
    return "".join(p for p in parts if p)


# --- R96: needs-review message ----------------------------------------------

def build_review(reason: str, supplier_name: str = "", doc_number: str = "",
                 delivery_date: str = "", from_addr: str = "", subject: str = "",
                 link: str = "") -> str:
    """#229 follow-up: `link` is rendered whenever given — a review outcome ALWAYS means
    a human has something to check, unlike `build_success` where the link is
    conditional on there being real board action."""
    parts = ["<p><b>&#10071; Dodací list potrebuje kontrolu</b></p>"]
    parts.append(_meta_lines(**{"Od": from_addr, "Predmet": subject,
                                "Dodávateľ": supplier_name, "Číslo DL": doc_number,
                                "Dátum dodania": delivery_date}))
    parts.append(f"<p>{escape(reason or 'Neznámy dôvod')}</p>")
    if link:
        parts.append(report.link_line(link))
    return "".join(p for p in parts if p)


# --- per-document outcome line, shared by build_announced_mismatch ----------

def _outcome_line(doc: dict) -> str:
    """A single short, self-contained Slovak sentence stating whether THIS document
    was actually processed (#229 follow-up) — a reader must never see only the
    announced-but-missing warning below and be left guessing whether the attached DL
    went through. `doc` is one of `_process_document`'s own returned outcome dicts —
    no new data, just rendering what the worker already computed."""
    outcome = doc.get("outcome") or "review"
    doc_number = doc.get("doc_number") or ""
    label = f"Dodací list {escape(doc_number)}" if doc_number else "Táto správa"
    n = len(doc.get("items") or [])
    if outcome == "ok":
        return (f"{_OUTCOME_ICON['ok']} {label} spracovaný a nahratý do ORIONu "
               f"({n} položiek).")
    if outcome == "partial":
        return (f"{_OUTCOME_ICON['partial']} {label} spracovaný ČIASTOČNE &mdash; "
               f"nahratý do ORIONu, chýbajú niektoré položky ({n} položiek).")
    if outcome == "duplicate":
        return (f"{_OUTCOME_ICON['duplicate']} {label} bol už spracovaný skôr "
               "&mdash; duplicitné oznámenie, nič sa neposiela znova.")
    reason = escape(doc.get("reason") or "potrebuje kontrolu")
    return f"{_OUTCOME_ICON['review']} {label} NEBOL spracovaný &mdash; {reason}."


def _outcome_needs_link(doc: dict) -> bool:
    """Mirrors `build_success`'s own link condition EXACTLY: `review` always; otherwise
    only when `doc["unmatched_items"]` is non-empty (the SAME list `_process_document`
    now carries on every live outcome dict, review finding PR #232 — reading the
    `outcome` string alone ("partial") would miss a document whose only unmatched item
    also has zero quantity, see `build_success`'s own docstring for why)."""
    if doc.get("outcome") == "review":
        return True
    return bool(doc.get("unmatched_items"))


# --- spec §4: announced-vs-attached mismatch --------------------------------

def build_announced_mismatch(subject: str, from_addr: str, announced: list[str],
                             attached: list[str], documents: list[dict] | None = None,
                             link: str = "") -> str:
    """`announced`/`attached` keep their existing meaning (see the call site in
    `dl_worker._process_message`: `announced` actually receives the MISSING subset,
    `attached` the extracted numbers). `documents` (#229 follow-up) is the SAME
    per-document outcome list `_process_message` already built via `_process_document`
    — rendered FIRST as short outcome lines, before the reworded missing-doc warning,
    so this message is self-sufficient even when it is the ONLY one a reader sees."""
    parts = [f"<p>{_outcome_line(d)}</p>" for d in (documents or [])]
    miss = ", ".join(escape(a) for a in announced) or "?"
    parts.append(
        "<p>&#9888;&#65039; V predmete e-mailu bol ohlásený aj doklad "
        f"{miss}, ktorý v prílohe nebol. Ak má existovať, treba ho vyžiadať od "
        "dodávateľa.</p>")
    parts.append(f"<p>Od: {escape(from_addr or '')}<br>Predmet: {escape(subject or '')}</p>")
    if link and any(_outcome_needs_link(d) for d in (documents or [])):
        parts.append(report.link_line(link))
    return "".join(parts)


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
