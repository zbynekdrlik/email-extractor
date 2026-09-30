"""The per-file detail of an import CARRYOVER alert (#476): which files are really still
waiting in ORION, and — for a delivery note — why CODEX will never take it.

The live bug (30.9.): the delivery-notes channel was reminded „⚠️ Stále 4 dodacie listy
neprevzatých v ORIONe" twice a day while 3 of the 4 had been imported two days earlier — the
reminder counted every file that EVER joined the incident. `confirm.py` now selects the files
that are REALLY still waiting (no terminal status AND still in the queued folder of the sweep's
own listing); this module turns those rows into the lines the warehouse reads:

- one line per waiting file — „DL <číslo> (<dodávateľ>)" for a DESADV, „objednávka na <deň>
  (<odberateľ>)" for an ORDER file;
- for a waiting DESADV, WHY it will never import when that is knowable: CODEX rejects a WHOLE
  delivery note when one LIN line carries a code no stock card has (#467 — DL 126049732 sat in
  `in_DL` on such a code for days). Its LIN codes are read READ-ONLY from ORION (`read_files`,
  `upload.read_files` in production) and checked against the CODEX stock-card list
  (`codex_cards.live_guard`); an unknown code becomes „… CODEX ho neprevezme — kód X v CODEXe
  neexistuje. Zadajte ho ručne." — that file is listed first, and the caller notes how old
  the card list is (a card created in CODEX after the last push is not in it yet).

Fail-open, the #467 rule: the detail NEVER blocks or loses the alert. A stale / never-pushed
CODEX list turns the code check off BEFORE anything is read; a failing name lookup, CODEX
lookup or ORION read (all logged) only drops the names / the code lines — the alert still goes
out with at least „DL <číslo>" per file. Nothing here writes anywhere (DB, ORION, CODEX).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from html import escape

from . import codex_cards, desadv_edi, dl_snapshot, snapshot

log = logging.getLogger("orders.confirm_carryover")

# `read_files(wire names) -> {wire name: text}` — `upload.read_files` bound to `in_DL`.
ReadFiles = Callable[[list[str]], dict[str, str]]


@dataclass(frozen=True)
class Detail:
    """`lines`: one ready `<li>` per waiting file; `codex_as_of`: the CODEX card list's
    „D.M. HH:MM" when at least one code line is there (else "")."""
    lines: list[str]
    codex_as_of: str = ""


@dataclass(frozen=True)
class _Dead:
    codes: dict[int, list[str]]
    as_of: str = ""


def _names(conn, source: str) -> dict[str, str]:
    """EAN → display name: the DL supplier list for a DESADV row, the customer list for an
    ORDER row (both the effective snapshot + overrides view the board shows). Branches sharing
    one EAN keep the first name."""
    rows = (dl_snapshot.dl_suppliers_for_management(conn) if source == "desadv"
            else snapshot.customers_for_management(conn))
    out: dict[str, str] = {}
    for r in rows:
        ean, name = str(r.get("ean_edi") or ""), str(r.get("name") or "").strip()
        if ean and name:
            out.setdefault(ean, name)
    return out


def label(row: dict, source: str, names: dict[str, str]) -> str:
    """„DL 126049732 (Dobrota …)" / „objednávka na 04.08.2026 (Bistro …)" — plain text; the
    name in parentheses is left out when the EAN has no card."""
    if source == "desadv":
        base = f"DL {row.get('doc_number') or '?'}"
        who = names.get(str(row.get("supplier_ean") or ""), "")
    else:
        base = f"objednávka na {row.get('delivery_date') or '?'}"
        who = names.get(str(row.get("customer_ean") or ""), "")
    return f"{base} ({who})" if who else base


def missing_codes(conn, rows: list[dict], *, wire_prefix: str, read_files: ReadFiles,
                  now: datetime) -> _Dead:
    """Row id → the LIN codes no CODEX stock card has, for each waiting DESADV row whose file
    was readable (+ the list's date). Empty when the CODEX list is not live — decided BEFORE
    any ORION read — or when the files cannot be read at all."""
    cards = codex_cards.live_guard(conn, now)     # logs its own warning when it is off
    if cards is None:
        return _Dead({})
    wanted = {f"{wire_prefix}{r['filename']}": r["id"] for r in rows if r.get("filename")}
    if not wanted:
        return _Dead({})
    try:
        contents = read_files(sorted(wanted))
    except Exception:
        log.warning("carryover alert: could not read %d waiting DESADV file(s) from ORION — "
                    "the CODEX code check is skipped this time (fail-open)", len(wanted),
                    exc_info=True)
        return _Dead({})
    out: dict[int, list[str]] = {}
    for name, rid in wanted.items():
        content = contents.get(name)
        if content is None:
            log.warning("carryover alert: %s could not be read — no CODEX code check for it",
                        name)
            continue
        dead = [code for code in desadv_edi.lin_codes(content) if not cards.has(code)]
        if dead:
            out[rid] = dead
            log.warning("carryover alert: %s carries code(s) %s that no CODEX stock card has — "
                        "CODEX will reject the whole delivery note", name, dead)
    return _Dead(out, cards.meta()["as_of_local"] or "") if out else _Dead({})


def _codes_text(dead: list[str]) -> str:
    if len(dead) == 1:
        return f"kód {dead[0]} v CODEXe neexistuje"
    return f"kódy {', '.join(dead)} v CODEXe neexistujú"


def items(conn, rows: list[dict], source: str, *, wire_prefix: str, read_files: ReadFiles,
          now: datetime) -> Detail:
    """One HTML-escaped `<li>` per waiting row, in the given order except that a file CODEX
    will never take goes first (the one the warehouse must act on by hand)."""
    try:
        names = _names(conn, source)
    except Exception:
        log.exception("carryover alert: %s names could not be loaded — listing numbers only "
                      "(fail-open)", source)
        names = {}
    dead = _Dead({})
    if source == "desadv":
        try:
            dead = missing_codes(conn, rows, wire_prefix=wire_prefix, read_files=read_files,
                                 now=now)
        except Exception:
            log.exception("carryover alert: the CODEX code check failed — alert goes out "
                          "without code lines (fail-open)")
    lines = []
    for row in sorted(rows, key=lambda r: r["id"] not in dead.codes):   # stable sort
        text = label(row, source, names)
        if row["id"] in dead.codes:
            text += (f": CODEX ho neprevezme — {_codes_text(dead.codes[row['id']])}. "
                     "Zadajte ho ručne.")
        lines.append(f"<li>{escape(text)}</li>")
    return Detail(lines, dead.as_of)
