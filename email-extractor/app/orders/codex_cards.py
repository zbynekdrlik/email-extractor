"""The CODEX stock-card list (#467) — is a DL card's code a real CODEX EAN kód?

CODEX (the warehouse ERP) imports our DESADV files by hand and REJECTS THE WHOLE delivery
note when a single line carries an EAN kód that no stock card has („V zásobách sa nenachádza
skladová karta s EAN kódom 3698" — DL 126049732 sat in `in_DL`, code 3698 that card 27 carried
only 24.-28.9.). Nothing in the add-on knew which codes CODEX has, so neither the board's
„➕ Nová karta" nor the DL engine could notice a dead code.

`tools/codex_cards_push.py` (dev2 systemd timer, the #342 push pattern) reads `raw.sm002`
read-only from the codex-bridge DuckDB and POSTs the FULL list to `POST /api/codex/cards`;
`replace_cards` swaps it in atomically (a REPLACE, never a merge — a code that left CODEX must
leave here too, the exact incident class). Consumers:

- the board refuses to save a DL card number CODEX lacks (`check_card_code`, 409 + CODEX cards
  with a similar name and their code),
- the live DL path leaves a line matched to such a card without a card (`dl_match.decide_item`'s
  `codex=` guard) → the existing #365 HOLD + a dl_item question whose candidates are only cards
  CODEX has, ranked by the CODEX name too (`question_candidates`),
- Produkty sklad flags a card whose name drifted from CODEX's (`annotate`).

**Fail OPEN, never closed.** A list that never arrived, or whose CODEX snapshot is older than
`STALE_HOURS`, turns every check OFF (`live_guard` → None + `log.warning`) — a stopped push must
never hold every delivery note. `stale_sweep` (worker tick) raises ONE ops alert for it.
`STALE_HOURS = 30`: the codex-bridge ETL loads sm002 at 14:15 and 18:00 Europe/Prague and the
push follows at 14:42 / 18:27, so the longest NORMAL data age is ~20.5 h (18:00 → next 14:42);
30 h tolerates one missed slot plus ETL jitter, two missed slots cross it.

**What "exists" means:** the code is the NEANKOD of ANY pushed sm002 row — any stredisko/sklad,
active or inactive. The incident class is a code CODEX has nowhere; a stricter per-sklad rule
would falsely hold cards whose NEANKOD lives on another sklad row of the same card (measured
2026-09-29: 480 of our 483 DL cards match on this rule, all on stredisko-1 rows).

Codes are compared as canonical integer text (NEANKOD is a DOUBLE in CODEX, so leading zeros
and a trailing `.0` never survive there) — `normalize_code` is the ONE normalizer both sides go
through. Everything here is a read-model over our own table; nothing writes to CODEX or ORION.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from . import dl_match

log = logging.getLogger("orders.codex_cards")

STALE_HOURS = 30
# A push carrying fewer than this share of the previous push's codes is refused (a half-loaded
# ETL snapshot would otherwise turn most real codes "missing" and hold every DL). `force`
# overrides it for a genuine mass removal.
MIN_KEEP_RATIO = 0.5
ALERT_KIND = "codex_cards_stale"
# pending_alerts.message_id dedup key PREFIX (+ ":<stale snapshot time>", one per episode) —
# there is no mail behind this alert.
ALERT_KEY = "codex-cards"
SIMILAR_LIMIT = 5
# `dl_match._score_item` scale (0-99): 30 ≈ half the words shared — below that a "similar"
# card is noise, not a suggestion.
SIMILAR_MIN_SCORE = 30.0
_REVISION_NAME = "add_codex_stock_cards"
_LOCAL_TZ = ZoneInfo("Europe/Bratislava")
_CODE_RE = re.compile(r"(\d+)(?:\.0+)?")
_UNIT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(kg|gr|g|ml|l)\b")


class ReplaceRefused(Exception):
    """The push was refused; `status` is the HTTP code the endpoint answers with."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class CardRefused(Exception):
    """A DL card number the board must not save; `payload` is the 409 JSON body (`error`, and
    `existing` when the number already has a card)."""

    def __init__(self, payload: dict):
        super().__init__(payload.get("error", ""))
        self.payload = payload


class CodexRefusal(CardRefused):
    """The number is no CODEX stock card's EAN kód; the payload adds
    `codex: {code, missing, as_of, similar}`."""


def normalize_code(value) -> str | None:
    """A CODEX NEANKOD (a DOUBLE) or one of our card `gtin`s → canonical integer text, or
    None when it is not a usable positive integer code."""
    if value is None or isinstance(value, bool):
        return None
    m = _CODE_RE.fullmatch(str(value).strip())
    if not m:
        return None
    digits = m.group(1).lstrip("0")
    return digits or None


def name_key(name: str) -> str:
    """The name-drift comparison key: diacritics/case folded, weight units unified
    („80 gr" = „80g"), word ORDER ignored, 1-letter words dropped. Cosmetic differences are
    not drift; a different product name is."""
    s = _UNIT_RE.sub(lambda m: m.group(1).replace(",", ".")
                     + ("g" if m.group(2) in ("g", "gr") else m.group(2)),
                     dl_match.fold(name))
    return " ".join(sorted({t for t in re.findall(r"[a-z0-9.]+", s) if len(t) > 1}))


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _ts(value) -> datetime | None:
    """ISO text / datetime → an aware datetime (a naive one is taken as UTC); else None."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            log.warning("codex cards: unparsable timestamp %r ignored", value)
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _local(dt: datetime | None) -> str:
    if dt is None:
        return "?"
    loc = dt.astimezone(_LOCAL_TZ)
    return f"{loc.day}.{loc.month}. {loc:%H:%M}"


# --- the push: a full, atomic replace ----------------------------------------------------

def _data_as_of(sync: dict) -> datetime:
    """The CODEX data-age anchor: the ETL snapshot time, never later than when the push reached
    us — a future `source_as_of` (a clock/timezone bug in the push) must not pin it fresh."""
    src, got = sync["source_as_of"], sync["synced_at"]
    return min(src, got) if src else got


def latest_sync(conn) -> dict | None:
    row = conn.execute(
        "SELECT id, synced_at, source_as_of, row_count, code_count FROM codex_card_syncs "
        "ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    return {"id": row[0], "synced_at": row[1], "source_as_of": row[2],
            "row_count": row[3], "code_count": row[4]}


def replace_cards(conn, cards: list, *, source_as_of=None, force: bool = False) -> dict:
    """Swap the whole CODEX list for `cards` in ONE transaction (+ a ledger row). Rows without
    a usable code are skipped; duplicates collapse on (code, card, stredisko, sklad), the last
    wins. Raises `ReplaceRefused` (400) for an empty list and (409) for a push that drops more
    than `1 - MIN_KEEP_RATIO` of the previous codes unless `force`. Returns {rows, codes}."""
    rows: dict[tuple, tuple] = {}
    for c in cards or []:
        if not isinstance(c, dict):
            continue
        code = normalize_code(c.get("code"))
        if not code:
            continue
        key = (code, str(c.get("card_code") or "").strip(), _int(c.get("stredisko")),
               _int(c.get("sklad")))
        rows[key] = (str(c.get("name") or "").strip()[:300], bool(c.get("inactive")),
                     _ts(c.get("changed_at")))
    if not rows:
        log.warning("codex cards push refused: no usable row among %d received",
                    len(cards or []))
        raise ReplaceRefused("zoznam kariet z CODEXu je prázdny — nič sa nenahrádza", 400)
    codes = len({k[0] for k in rows})
    with conn.transaction():
        # serialize concurrent pushes; plain readers (ACCESS SHARE) are never blocked
        conn.execute("LOCK TABLE codex_stock_cards IN EXCLUSIVE MODE")
        prev = latest_sync(conn)
        if prev and not force and codes < prev["code_count"] * MIN_KEEP_RATIO:
            log.warning("codex cards push refused: %d codes vs %d last time (shrink guard)",
                        codes, prev["code_count"])
            raise ReplaceRefused(
                f"push má len {codes} kódov oproti {prev['code_count']} naposledy — pravdepodobne "
                f"neúplný export z CODEXu, zoznam sa nenahrádza (force=1 ak je to zámer)", 409)
        conn.execute("DELETE FROM codex_stock_cards")
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO codex_stock_cards (code, card_code, stredisko, sklad, name, "
                "inactive, changed_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [(*k, *v) for k, v in rows.items()])
        conn.execute(
            "INSERT INTO codex_card_syncs (source_as_of, row_count, code_count) "
            "VALUES (%s, %s, %s)", (_ts(source_as_of), len(rows), codes))
    log.info("codex cards replaced: %d rows, %d codes (source as of %s, force=%s)",
             len(rows), codes, source_as_of, force)
    return {"rows": len(rows), "codes": codes}


# --- the read model -----------------------------------------------------------------------

@dataclass(frozen=True)
class CodexCards:
    """The current list: code → its CODEX names (the authoritative sklad-1/1 name first,
    then newest), plus the freshness facts."""
    names: dict[str, tuple[str, ...]]
    as_of: datetime | None
    synced_at: datetime | None
    stale: bool

    def has(self, code) -> bool:
        c = normalize_code(code)
        return bool(c) and c in self.names

    def name_for(self, code) -> str:
        names = self.names.get(normalize_code(code) or "", ())
        return names[0] if names else ""

    def name_status(self, code, our_name: str) -> str:
        """"ok" (our name is one of CODEX's, cosmetics aside), "drift" or "missing"."""
        names = self.names.get(normalize_code(code) or "")
        if not names:
            return "missing"
        ours = name_key(our_name)
        return "ok" if any(name_key(n) == ours for n in names) else "drift"

    def similar(self, *texts: str, limit: int = SIMILAR_LIMIT) -> list[dict]:
        """CODEX cards whose name resembles any of `texts`, best first — each {code, name}.
        Scored with the DL candidate scorer (`dl_match.candidates`), one code once."""
        pseudo = [{"gtin": code, "name": n, "doplnok": ""}
                  for code, names in self.names.items() for n in names]
        best: dict[tuple[str, str], float] = {}
        for text in texts:
            if not str(text or "").strip():
                continue
            for c in dl_match.candidates(text, pseudo, limit=len(pseudo)):
                if c["score"] < SIMILAR_MIN_SCORE:
                    break
                key = (c["gtin"], c["name"])
                best[key] = max(best.get(key, 0.0), c["score"])
        out: list[dict] = []
        for (code, name), _score in sorted(best.items(), key=lambda kv: (-kv[1], kv[0][1])):
            if any(o["code"] == code for o in out):
                continue
            out.append({"code": code, "name": name})
            if len(out) >= limit:
                break
        return out

    def meta(self) -> dict:
        return {"active": not self.stale, "stale": self.stale, "codes": len(self.names),
                "as_of": self.as_of.isoformat() if self.as_of else None,
                "as_of_local": _local(self.as_of),
                "synced_at": self.synced_at.isoformat() if self.synced_at else None}


def load(conn, now: datetime | None = None) -> CodexCards | None:
    """The list as stored (stale or not) — None only when nothing was ever pushed."""
    sync = latest_sync(conn)
    if sync is None:
        return None
    rows = conn.execute(
        "SELECT code, name, (stredisko = 1 AND sklad = 1) AS central, changed_at "
        "FROM codex_stock_cards").fetchall()
    rows.sort(key=lambda r: (r[0], not r[2],
                             -(r[3].timestamp() if r[3] else 0.0), r[1]))
    names: dict[str, list[str]] = {}
    for code, name, _central, _changed in rows:
        bucket = names.setdefault(code, [])
        if name and name not in bucket:
            bucket.append(name)
    as_of = _data_as_of(sync)
    now = now or datetime.now(UTC)
    return CodexCards(names={k: tuple(v) for k, v in names.items()}, as_of=as_of,
                      synced_at=sync["synced_at"],
                      stale=(now - as_of) > timedelta(hours=STALE_HOURS))


def live_guard(conn, now: datetime | None = None) -> CodexCards | None:
    """The list the CHECKS may trust, or None = checks OFF (fail-open) with a warning: never
    pushed, stale beyond `STALE_HOURS`, or empty."""
    cards = load(conn, now)
    if cards is None:
        log.warning("CODEX stock-card list was never pushed — card-code check is OFF "
                    "(fail-open) until the first push (#467)")
        return None
    if cards.stale:
        log.warning("CODEX stock-card list is stale (CODEX data as of %s, > %d h) — card-code "
                    "check is OFF (fail-open) until a fresh push (#467)",
                    cards.as_of, STALE_HOURS)
        return None
    if not cards.names:
        return None
    return cards


def check_card_code(conn, code, *texts: str, catalog=None, now=None) -> None:
    """Raise `CodexRefusal` when `code` is not a CODEX stock card's EAN kód (the list being
    fresh). `texts` (the card name typed, the delivery-note wording) pick the similar CODEX
    cards shown; `catalog` (our effective DL catalog) marks which of them we already have.
    A missing/stale list passes everything (fail-open)."""
    cards = live_guard(conn, now)
    if cards is None or cards.has(code):
        return
    # our card per normalized code — `catalog_gtin` is OUR exact number (what the answer path
    # and the catalog key on), never the normalized CODEX code
    ours = {normalize_code(r.get("gtin")): r for r in (catalog or [])}
    similar = []
    for s in cards.similar(*texts):
        entry = dict(s, in_catalog=s["code"] in ours)
        if entry["in_catalog"]:
            entry["catalog_gtin"] = str(ours[s["code"]].get("gtin"))
            entry["catalog_name"] = ours[s["code"]].get("name", "")
        similar.append(entry)
    log.warning("card code %s refused — no CODEX stock card has it (similar: %s)", code,
                [s["code"] for s in similar])
    raise CodexRefusal({
        "error": (f"Kód {code} v CODEXe neexistuje — žiadna skladová karta ho nemá ako EAN "
                  f"kód, takže CODEX by pri importe odmietol celý dodací list s touto kartou. "
                  f"Použi kód karty z CODEXu (zoznam kariet je k {_local(cards.as_of)} a "
                  f"obnovuje sa dvakrát denne, okolo 14:45 a 18:30 — kartu, ktorú si v CODEXe "
                  f"založil práve teraz, uvidíme až po tejto aktualizácii)."),
        "codex": {"code": str(code), "missing": True,
                  "as_of": cards.as_of.isoformat() if cards.as_of else None,
                  "similar": similar}})


def question_candidates(wording: str, catalog: list[dict], codex: CodexCards,
                        memory_gtin: str = "") -> list[dict]:
    """The dl_item question's candidate cards when the list is live: ONLY cards CODEX has,
    ranked by the better of our name and CODEX's name (a card whose OUR name went stale — the
    incident's „Bagetka s kečupom…" for CODEX's „Rožok so slaninou…" — still surfaces). A
    drifted card carries `codex_name` for the board. Returns our own card dicts (copies)."""
    valid = [c for c in catalog if codex.has(c.get("gtin"))]
    score = {str(c["gtin"]): c["score"] for c in
             dl_match.candidates(wording, valid, memory_gtin=memory_gtin, limit=len(valid))}
    drift = {str(c["gtin"]): codex.name_for(c["gtin"]) for c in valid
             if codex.name_status(c["gtin"], c.get("name", "")) == "drift"}
    pseudo = [{"gtin": g, "name": n, "doplnok": ""} for g, n in drift.items()]
    for c in dl_match.candidates(wording, pseudo, limit=len(pseudo)):
        score[str(c["gtin"])] = max(score.get(str(c["gtin"]), 0.0), c["score"])
    ranked = sorted(valid, key=lambda c: -score.get(str(c["gtin"]), 0.0))
    ranked = ranked[:dl_match.ITEM_CANDIDATES]
    return [dict(c, codex_name=drift[str(c["gtin"])]) if str(c["gtin"]) in drift else dict(c)
            for c in ranked]


def annotate(rows: list[dict], codex: CodexCards | None) -> list[dict]:
    """Each DL catalog row + `codex: {status[, name]}` — status ok/drift/missing, or
    "unknown" when no list was ever pushed."""
    out = []
    for r in rows:
        if codex is None:
            info: dict = {"status": "unknown"}
        else:
            info = {"status": codex.name_status(r.get("gtin"), r.get("name", ""))}
            if info["status"] == "drift":
                info["name"] = codex.name_for(r.get("gtin"))
        out.append(dict(r, codex=info))
    return out


def meta_for(cards: CodexCards | None) -> dict:
    """The board's status line for the list (`CodexCards.meta`, or the never-pushed shape)."""
    if cards is None:
        return {"active": False, "stale": False, "codes": 0, "as_of": None,
                "as_of_local": None, "synced_at": None, "never": True}
    return cards.meta()


# --- the ops alert for a stopped push -------------------------------------------------------

def _installed_at(conn) -> datetime | None:
    row = conn.execute("SELECT applied_at FROM schema_version WHERE name = %s",
                       (_REVISION_NAME,)).fetchone()
    return row[0] if row else None


def stale_sweep(conn, cfg, now: datetime | None = None) -> bool:
    """Enqueue ONE ops alert (the durable `pending_alerts` outbox) when the CODEX list is older
    than `STALE_HOURS` — or never arrived that long after the feature went live (the grace
    right after a deploy, before the first push). Re-reminded at most once per workday
    morning (`dl_alerts.reminder_suppressed`). Returns True when it enqueued."""
    from . import dl_alerts, report
    now = now or datetime.now(UTC)
    sync = latest_sync(conn)
    anchor = _data_as_of(sync) if sync else _installed_at(conn)
    if anchor is None or now - anchor <= timedelta(hours=STALE_HOURS):
        return False
    # one dedup key per stale EPISODE (the snapshot it is stuck on): a later episode alerts at
    # once instead of waiting for the next morning as a "reminder" of an old delivered alert
    key = f"{ALERT_KEY}:{anchor.isoformat()}"
    if dl_alerts.reminder_suppressed(conn, cfg, ALERT_KIND, key, now=now):
        return False
    hours = int((now - anchor).total_seconds() // 3600)
    state = (f"je zastaraný (údaje z CODEXu k {escape(_local(anchor))}, pred {hours} h)"
             if sync else f"ešte nikdy neprišiel ({hours} h od nasadenia)")
    body = (f"<p>&#9888;&#65039; Zoznam skladových kariet z CODEXu {state} &mdash; kontrola "
            "kódov kariet dodacích listov (#467) je dočasne VYPNUTÁ: nástenka prijme aj kód, "
            "ktorý v CODEXe neexistuje, a dodací list s takou kartou CODEX pri importe odmietne. "
            "Skontroluj na dev2 <code>codex-cards-push.timer</code> a codex-bridge ETL.</p>")
    dl_alerts.enqueue(conn, report.ops_channel(cfg), ALERT_KIND, body, message_id=key)
    log.warning("CODEX stock-card list %s — ops alert enqueued", "stale" if sync else "missing")
    return True
