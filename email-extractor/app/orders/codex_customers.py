"""CODEX customer cards — the order engine's customers come from CODEX too (#493).

`tools/codex_customers_push.py` (dev2, the #467/#485 push pattern; run when the codex-bridge ETL
replaced its DuckDB — `tools/codex_push_after_etl.py`) reads `raw.firma` read-only and POSTs
every customer card with an EDI EAN to `POST /api/codex/customers`; `replace_customers` swaps
the whole copy in atomically (a REPLACE — a card removed from CODEX leaves here too) and the
endpoint re-freezes the order snapshot, whose customer list is layered
(`customer_layers.apply_codex`): the frozen base ← CODEX (adds missing EANs, unions addresses,
fills a blank name/city/street/zip/trade name) ← the board's own edits on top.

Before #493 a customer card existed for the order engine only once the warehouse typed it on
the nástenka a second time; three of five shops of one customer were in CODEX only and every
order of the customer went to one card (the 2026-10-16 incident).

**Fail OPEN**: the layer always uses the last copy that arrived, however old — never an empty
list because a push stopped (`stale_sweep` tells ops once per stale episode). An EAN CODEX has
on more than one firm/branch is never stored (the push reports it in `ambiguous`; the endpoint
drops a duplicate EAN of the body the same way) and ops gets ONE alert per change of that set,
so the warehouse fixes it in CODEX — a guessed card routes orders to the wrong customer.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime
from html import escape

from psycopg.types.json import Json

from . import codex_snapshot

log = logging.getLogger("orders.codex_customers")

STALE_HOURS = codex_snapshot.STALE_HOURS
# A push carrying fewer than this share of the previous push's customers is refused (a
# half-loaded ETL would otherwise drop most shops from the order engine). `force` overrides.
MIN_KEEP_RATIO = 0.5
STALE_KIND = "codex_customers_stale"
STALE_KEY = "codex-customers"                 # + ":<stale snapshot time>", one per episode
AMBIGUOUS_KIND = "codex_customers_ambiguous"
AMBIGUOUS_KEY = "codex-customers-ambiguous"   # + ":<hash of the EAN set>", one per change
_REVISION_NAME = "add_codex_customers"
ReplaceRefused = codex_snapshot.ReplaceRefused
_EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}")
_EAN_STRIP_RE = re.compile(r"[\s\-]")
_COLS = ("ean_edi", "name", "trade_name", "emails", "city", "street", "zip", "codex_id")


def _text(value, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


def _ean(value) -> str:
    code = _EAN_STRIP_RE.sub("", str(value or ""))
    return code if code.isdigit() else ""


def _emails(value) -> list[str]:
    items = value if isinstance(value, (list, tuple)) else [value]
    out: list[str] = []
    for v in items:
        for m in _EMAIL_RE.findall(str(v or "").lower()):
            if m not in out:
                out.append(m)
    return out


def latest_sync(conn) -> dict | None:
    row = conn.execute(
        "SELECT id, synced_at, source_as_of, row_count, ambiguous FROM codex_customer_syncs "
        "ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    return {"id": row[0], "synced_at": row[1], "source_as_of": row[2], "row_count": row[3],
            "ambiguous": row[4] or []}


def _ambiguous_eans(items) -> list[str]:
    return sorted({_ean(a.get("ean_edi")) for a in items or [] if isinstance(a, dict)} - {""})


def replace_customers(conn, customers: list, *, ambiguous: list | None = None,
                      source_as_of=None, force: bool = False) -> dict:
    """Swap the whole copy for `customers` in ONE transaction (+ a ledger row). A row without a
    usable EAN or name is skipped; an EAN that occurs twice in the body is dropped for both
    and reported as ambiguous. Raises `ReplaceRefused` (400) for an empty list and (409) for
    fewer than `MIN_KEEP_RATIO` of the previous rows unless `force`. Returns {stored,
    ambiguous (list of reports), ambiguous_changed}."""
    rows: dict[str, tuple] = {}
    dup: set[str] = set()
    reports = [a for a in ambiguous or [] if isinstance(a, dict) and _ean(a.get("ean_edi"))]
    for c in customers or []:
        if not isinstance(c, dict):
            continue
        code, name = _ean(c.get("ean_edi")), _text(c.get("name"))
        if not code or not name:
            continue
        if code in rows:
            dup.add(code)
            continue
        rows[code] = (code, name, _text(c.get("trade_name")), _emails(c.get("emails")),
                      _text(c.get("city"), 120), _text(c.get("street"), 200),
                      _text(c.get("zip"), 20), _text(c.get("codex_id"), 60))
    for code in sorted(dup):
        del rows[code]
        reports.append({"ean_edi": code, "firms": [{"name": "(v pushi viackrát)",
                                                    "city": "", "codex_id": ""}]})
        log.warning("codex customers push: EAN %s occurs more than once in the body — not "
                    "stored (ambiguous)", code)
    if not rows:
        log.warning("codex customers push refused: no usable row among %d received",
                    len(customers or []))
        raise ReplaceRefused("zoznam zákazníkov z CODEXu je prázdny — nič sa nenahrádza", 400)
    with conn.transaction():
        # serialize concurrent pushes; plain readers (ACCESS SHARE) are never blocked
        conn.execute("LOCK TABLE codex_customers IN EXCLUSIVE MODE")
        prev = latest_sync(conn)
        if prev and not force and len(rows) < prev["row_count"] * MIN_KEEP_RATIO:
            log.warning("codex customers push refused: %d customers vs %d last time (shrink "
                        "guard)", len(rows), prev["row_count"])
            raise ReplaceRefused(
                f"push má len {len(rows)} zákazníkov oproti {prev['row_count']} naposledy — "
                f"pravdepodobne neúplný export z CODEXu, nič sa nenahrádza (force=1 ak je to "
                f"zámer)", 409)
        conn.execute("DELETE FROM codex_customers")
        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO codex_customers ({', '.join(_COLS)}) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", list(rows.values()))
        conn.execute(
            "INSERT INTO codex_customer_syncs (source_as_of, row_count, ambiguous) "
            "VALUES (%s, %s, %s)",
            (codex_snapshot.parse_ts(source_as_of), len(rows), Json(reports)))
    changed = _ambiguous_eans(reports) != _ambiguous_eans(prev["ambiguous"] if prev else [])
    log.info("codex customers replaced: %d customers, %d ambiguous EAN(s) %s%s (source as of "
             "%s, force=%s)", len(rows), len(reports), _ambiguous_eans(reports),
             " — CHANGED" if changed else "", source_as_of, force)
    return {"stored": len(rows), "ambiguous": reports, "ambiguous_changed": changed}


def layer(conn) -> dict[str, dict]:
    """The CODEX layer for the effective customer list: {ean: card} — always the last copy
    that arrived, never emptied because it is old (fail-open)."""
    rows = conn.execute(f"SELECT {', '.join(_COLS)} FROM codex_customers").fetchall()
    return {r[0]: dict(zip(_COLS, r, strict=True), emails=list(r[3] or [])) for r in rows}


# --- the ops alerts -------------------------------------------------------------------------

def ambiguity_alert(conn, cfg, res: dict) -> bool:
    """ONE ops alert per CHANGE of the set of EANs CODEX has on several firms (the push's
    `ambiguous`). Silent while the set is unchanged or empty. Returns True when it enqueued."""
    from . import dl_alerts, report
    reports = res.get("ambiguous") or []
    if not res.get("ambiguous_changed") or not reports:
        return False
    eans = _ambiguous_eans(reports)
    key = f"{AMBIGUOUS_KEY}:{hashlib.sha256(','.join(eans).encode()).hexdigest()[:12]}"
    if dl_alerts.already_pending(conn, AMBIGUOUS_KIND, key):
        return False
    lines = []
    for a in sorted(reports, key=lambda a: _ean(a.get("ean_edi"))):
        firms = " / ".join(
            escape(f"{f.get('name', '')}" + (f" ({f['city']})" if f.get("city") else ""))
            for f in a.get("firms") or [])
        lines.append(f"<li>EAN {escape(_ean(a.get('ean_edi')))}: {firms}</li>")
    body = (f"<p>&#9888;&#65039; V CODEXe majú tieto EAN kódy EDI viac rôznych firiem/pobočiek "
            f"({len(eans)}) &mdash; do objednávok sa preto NEPREBERAJÚ (objednávka by mohla ísť "
            f"na nesprávneho zákazníka). Oprav EAN kód EDI v CODEXe; po ďalšom prenose z CODEXu "
            f"sa karta prevezme sama.</p><ul>{''.join(lines)}</ul>")
    dl_alerts.enqueue(conn, report.ops_channel(cfg), AMBIGUOUS_KIND, body, message_id=key)
    log.warning("CODEX customers: %d ambiguous EAN(s) %s — ops alert enqueued", len(eans), eans)
    return True


def stale_sweep(conn, cfg, now: datetime | None = None) -> bool:
    """ONE ops alert per stale episode while the customer copy is older than `STALE_HOURS` —
    or never arrived that long after the feature went live. The orders keep working on the
    last copy (fail-open); a shop created in CODEX since then is just not known yet."""
    now = now or datetime.now(UTC)
    sync = latest_sync(conn)
    anchor = (codex_snapshot.data_as_of(sync["source_as_of"], sync["synced_at"]) if sync
              else codex_snapshot.installed_at(conn, _REVISION_NAME))
    if anchor is None:
        return False
    hours = int((now - anchor).total_seconds() // 3600)
    state = (f"sú zastaraní (údaje z CODEXu k {escape(codex_snapshot.local_label(anchor))}, "
             f"pred {hours} h)" if sync else f"ešte nikdy neprišli ({hours} h od nasadenia)")
    body = (f"<p>&#9888;&#65039; Zákazníci z CODEXu {state} &mdash; objednávky bežia ďalej s "
            f"posledným známym zoznamom, ale predajňa založená v CODEXe odvtedy ešte nie je "
            f"známa. Skontroluj na dev2 <code>codex-push-after-etl.path</code> / "
            f"<code>.timer</code> (<code>journalctl -u codex-push-after-etl.service</code>) a "
            f"codex-bridge ETL.</p>")
    if not codex_snapshot.stale_alert(conn, cfg, kind=STALE_KIND, key_prefix=STALE_KEY,
                                      anchor=anchor, body=body, now=now):
        return False
    log.warning("CODEX customers %s — ops alert enqueued", "stale" if sync else "missing")
    return True
