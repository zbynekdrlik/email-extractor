"""Extraction: email text -> orders (#61).

Authority order, deliberately:

1. **A recognized tabular attachment is parsed by CODE and overrides the model.** A grid
   is deterministic; the model reading a grid is not. The live pipeline already does this
   (buried inside its citation validator) — here it is the first thing that happens.
2. **Whatever the model returns is citation-checked** against the source text, so an
   invented item cannot reach ORION.
3. **An unverifiable item is reported, never dropped.** The warehouse adds it by hand.

The prompt lives in `prompts/extract.md` so a prompt change shows up as a diff and as a
cache miss; its hash is recorded on the run.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger("orders.extract")

PROMPT_PATH = Path(__file__).with_name("prompts") / "extract.md"
# A template read as an order shows up as many items all with quantity 1.
PRICELIST_MIN_ITEMS = 10
ZERO_WIDTH = dict.fromkeys(
    [0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0x2061, 0x2062, 0x2063, 0x2064,
     0xFEFF, 0x00AD, 0x180E], " ")

ORDER_SCHEMA = {
    "type": "object",
    "properties": {
        "senderName": {"type": "string"},
        "senderEmail": {"type": "string"},
        "companyName": {"type": "string"},
        "isChangeRequest": {"type": "boolean"},
        "notes": {"type": "string"},
        "orders": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "orderNumber": {"type": "string"},
                "deliveryDate": {"type": "string"},
                "recipientGroup": {"type": "string"},
                # The block header of this order's own half of a multi-shop attachment
                # (#101) — it is what separates two branches registered under one address.
                "store": {"type": "string"},
                "items": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "quantity": {"type": "number"},
                        "unit": {"type": "string"},
                        "sourceQuote": {"type": "string"},
                    },
                    "required": ["name", "quantity", "unit", "sourceQuote"],
                }},
            },
            "required": ["deliveryDate", "items"],
        }},
    },
    "required": ["orders"],
}


def clean_text(text: str) -> str:
    """Strip invisible format characters.

    Some mail clients send a zero-width space inside "45​ks"; the model, the citation
    check and the parser must all see the same string or a real item silently becomes
    unverified (#41, the AGEL incident).
    """
    return str(text or "").translate(ZERO_WIDTH)


def prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


DAYS_SK = ("pondelok", "utorok", "streda", "štvrtok", "piatok", "sobota", "nedeľa")


def today_header(today=None) -> str:
    """The date line the prompt counts relative dates from.

    Without it the model dates "zajtra" / "pondelok" from its own training cutoff, which
    is a silently wrong delivery date — found in the first live run of this stage.
    Passing `today` in makes a golden-corpus case reproducible years later.
    """
    import datetime
    if isinstance(today, str) and today:
        day = datetime.date.fromisoformat(today)
    elif isinstance(today, datetime.date):
        day = today
    else:
        day = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=2))).date()
    return (f"Dnešný dátum: {day.strftime('%d.%m.%Y')}, deň: {DAYS_SK[day.weekday()]}, "
            f"rok: {day.year}. Relatívne aj predvolené dátumy (zajtra, pondelok, "
            f"„na 26.6\", default tomorrow) počítaj VŽDY od tohto dňa; nikdy nehádaj.")


# --- 1) deterministic table parsing --------------------------------------

def _attachment_block(text: str) -> list[str] | None:
    idx = text.find("Attachments:")
    if idx == -1:
        return None
    return text[idx:].splitlines()


def parse_table(text: str) -> list[dict] | None:
    """Items from a recognized tabular attachment, or None when there is no such table."""
    lines = _attachment_block(clean_text(text))
    if not lines:
        return None
    for i, line in enumerate(lines):
        if "Č.mat.dodavat." in line or ("EAN kus" in line and "Název" in line):
            return _parse_kosik(lines, i)
        if "%DPH" in line and ("VO bez DPH" in line or "VO s DPH" in line):
            return _parse_pricelist(lines, i)
    return None


def _rows_after(lines: list[str], header_idx: int):
    for line in lines[header_idx + 1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("====="):
            break
        yield stripped


def _parse_kosik(lines: list[str], header_idx: int) -> list[dict] | None:
    header = [h.strip() for h in lines[header_idx].split(",")]
    try:
        name_i, qty_i = header.index("Název"), header.index("Množství")
    except ValueError:
        return None
    items = []
    for row in _rows_after(lines, header_idx):
        cols = row.split(",")
        if len(cols) <= max(name_i, qty_i):
            continue
        name = cols[name_i].strip().strip('"')
        qty = _number(cols[qty_i])
        if name and qty and qty > 0:
            items.append({"name": name, "quantity": round(qty), "unit": "ks"})
    return items or None


def _parse_pricelist(lines: list[str], header_idx: int) -> list[dict] | None:
    """The order quantity is the column AFTER the last labelled header column.

    Templates differ in how many price columns they carry (5 or 7), so the header row
    decides — a fixed index reads a price as a quantity, which is how a 1.50 € price
    once became "1.5 pieces".
    """
    header = lines[header_idx].split("\t")
    last_labelled = max((i for i, h in enumerate(header) if h.strip()), default=0)
    qty_i = last_labelled + 1
    items = []
    for row in _rows_after(lines, header_idx):
        cols = row.split("\t")
        name = (cols[0] if cols else "").strip().strip('"')
        if not name:
            continue
        # A product row carries prices; a note or a section title does not.
        if not any((_number(c) or 0) > 0 for c in cols[1:last_labelled + 1]):
            continue
        qty = _number(cols[qty_i]) if len(cols) > qty_i else None
        if qty and qty > 0:
            items.append({"name": name, "quantity": round(qty), "unit": "ks"})
    return items or None


def _number(value) -> float | None:
    try:
        return float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


# --- 2) citation checking ------------------------------------------------

def _fold(text: str) -> str:
    s = unicodedata.normalize("NFD", clean_text(text).lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip()


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _fold(text))


def quote_in_source(quote: str, source: str) -> bool:
    if not quote or len(quote) < 5:
        return False
    folded_source = _fold(source)
    if _fold(quote) in folded_source:
        return True
    squashed = _squash(quote)
    if len(squashed) >= 8 and squashed in _squash(source):
        return True
    # The model sometimes quotes a whole table row; half its cells matching the source is
    # enough proof that the row exists.
    parts = [p.strip() for p in re.split(r"[\t,;|]+", _fold(quote)) if len(p.strip()) > 3]
    if not parts:
        return False
    hits = sum(1 for p in parts if p in folded_source)
    return hits >= -(-len(parts) // 2)


def item_in_source(item: dict, source: str) -> bool:
    """The item LINE proves the item even when the quote does not exist contiguously.

    VERIFY sometimes glues a section header onto the item line; ČSB (2026-07-23) shipped
    1 of 9 items because of it. A phantom item still fails — it is nowhere in the source.
    """
    name = _fold(item.get("name", ""))
    qty = _number(item.get("quantity"))
    if len(name) < 4 or not qty or qty <= 0:
        return False
    folded_source = _fold(source)
    n = str(round(qty))
    for candidate in (f"{n}x {name}", f"{n} x {name}", f"{n}x{name}",
                      f"{n}ks {name}", f"{n} ks {name}", f"{name} {n}"):
        if candidate in folded_source:
            return True
    squashed = _squash(f"{n}x{name}")
    return len(squashed) >= 8 and squashed in _squash(source)


def verify(extracted: dict, source: str) -> dict:
    """Keep only items provable from the source; report the rest.

    An order left with no provable item is dropped entirely — there is nothing to ship —
    but its items still appear in `unverified`, so nothing disappears quietly.
    """
    orders, unverified = [], []
    for order in extracted.get("orders", []) or []:
        kept = []
        for item in (order.get("items") or []) + (order.get("missedItems") or []):
            if str(item.get("status", "")).upper() == "PHANTOM":
                unverified.append(_plain(item))
                continue
            if quote_in_source(item.get("sourceQuote", ""), source) or \
                    item_in_source(item, source):
                kept.append(_plain(item))
            else:
                unverified.append(_plain(item))
        if kept:
            orders.append({"orderNumber": order.get("orderNumber", "") or "",
                           "deliveryDate": order.get("deliveryDate", "") or "",
                           "recipientGroup": order.get("recipientGroup", "") or "",
                           "store": order.get("store", "") or "",
                           "items": kept})
    if unverified:
        log.warning("%d item(s) could not be verified against the email: %s",
                    len(unverified), [i["name"] for i in unverified])
    return {
        "senderName": extracted.get("senderName", "") or "",
        "senderEmail": extracted.get("senderEmail", "") or "",
        "companyName": extracted.get("companyName", "") or "",
        "isChangeRequest": bool(extracted.get("isChangeRequest")),
        "notes": extracted.get("notes", "") or "",
        "orders": orders,
        "unverified": unverified,
    }


def _plain(item: dict) -> dict:
    return {"name": item.get("name", ""), "quantity": item.get("quantity"),
            "unit": item.get("unit", "ks") or "ks"}


# --- 3) sanity guard -----------------------------------------------------

def sanity(extracted: dict) -> str | None:
    """A reason to refuse the whole extraction, or None when it looks like an order."""
    items = [i for o in extracted.get("orders", []) or [] for i in (o.get("items") or [])]
    if len(items) > PRICELIST_MIN_ITEMS and all(
            _number(i.get("quantity")) == 1 for i in items):
        return (f"Podozrenie na cenník — všetky položky ({len(items)}) majú množstvo 1. "
                "Pravdepodobne sa spracovala šablóna cenníka namiesto objednávky.")
    return None


# --- the stage -----------------------------------------------------------

def run(client, email: dict) -> dict:
    """Extract orders from one email. `client` is an `llm.Client`.

    A recognized table replaces the model's item list but keeps its header fields (dates,
    PO number, sender) — those are not in the grid.
    """
    source = unquote_fully_quoted(clean_text(email.get("combined_text") or ""),
                                  today=email.get("today") or "")
    user = (f"{today_header(email.get('today'))}\n\n"
            f"FROM: {email.get('from_name', '')} <{email.get('from_addr', '')}>\n"
            f"SUBJECT: {email.get('subject', '')}\n\n--- ORDER EMAIL ---\n{source}\n--- END ---")
    extracted = client.json_call(prompt(), user, ORDER_SCHEMA, name="orders")

    refusal = sanity(extracted)
    if refusal:
        return {"orders": [], "unverified": [], "refusal": refusal,
                "isChangeRequest": bool(extracted.get("isChangeRequest")),
                "notes": extracted.get("notes", "") or "",
                "senderName": extracted.get("senderName", "") or "",
                "senderEmail": extracted.get("senderEmail", "") or "",
                "companyName": extracted.get("companyName", "") or "",
                "prompt_hash": client.last_prompt_hash}

    table = parse_table(source)
    if table:
        head = (extracted.get("orders") or [{}])[0]
        log.info("tabular attachment recognized: %d item(s) parsed by code (model said %d)",
                 len(table), len(head.get("items") or []))
        result = {
            "senderName": extracted.get("senderName", "") or "",
            "senderEmail": extracted.get("senderEmail", "") or "",
            "companyName": extracted.get("companyName", "") or "",
            "isChangeRequest": bool(extracted.get("isChangeRequest")),
            "notes": extracted.get("notes", "") or "",
            "orders": [{"orderNumber": head.get("orderNumber", "") or "",
                        "deliveryDate": head.get("deliveryDate", "") or "",
                        "recipientGroup": head.get("recipientGroup", "") or "",
                        "store": head.get("store", "") or "",
                        "items": table}],
            "unverified": [],
            "source": "table",
        }
    else:
        result = dict(verify(extracted, source), source="model")

    # #163: an order whose deliveryDate the source text never wrote is not a real order —
    # the model can invent a plausible-looking future day (e.g. re-dating a stale quoted
    # order onto "next Saturday") with nothing in the mail to back it up. Drop it here,
    # BEFORE it ever reaches pipeline.py's date_conflict() as if it were a genuine day.
    grounded, ungrounded = [], []
    for order in result["orders"]:
        (grounded if date_grounded(order.get("deliveryDate", ""), source)
         else ungrounded).append(order)
    if ungrounded:
        dates = [o.get("deliveryDate", "") for o in ungrounded]
        log.warning("%d order(s) dropped: deliveryDate not found in the source text: %s",
                   len(ungrounded), dates)
        # review finding (#163): a log line alone leaves no trace anywhere a human ever
        # reads (Odoo, `order_runs.result`) — fold it into `notes` too, the one field that
        # already survives into both, without inventing a new pipeline.py/report.py field.
        note = ("Dátum dodania sa nenašiel v texte e-mailu, objednávka nebola vytvorená: "
                + ", ".join(dates))
        result["notes"] = f"{result['notes']} {note}".strip() if result.get("notes") else note
    result["orders"] = grounded

    result["prompt_hash"] = client.last_prompt_hash
    return result


# --- the subject and the body must agree about the day (#81.5) -------------

_SUBJ_DAY = re.compile(r"\b(\d{1,2})\s*\.\s*(\d{1,2})\s*\.(\s*(\d{4}|\d{2}))?")
_RANGE = re.compile(r"\d{1,2}\s*\.\s*\d{1,2}\s*\.?\s*(-|–|do)\s*\d{1,2}\s*\.\s*\d{1,2}")


_WEEKDAY = re.compile(
    r"\b(pondel|utor|stred|štvrt|stvrt|piat|sobot|nedeľ|nedel)\w*", re.IGNORECASE)


def _body_only(text: str) -> str:
    """The message text WITHOUT the `Subject:` / `From:` header block.

    `combined_text` is stored as "Subject: …\\nFrom: …\\nBody: …", so a naive scan finds the
    subject's day inside the body and every subject-vs-body contradiction looks like a mail
    that simply named two days.
    """
    text = text or ""
    marker = re.search(r"^Body:\s*", text, re.MULTILINE)
    if marker:
        return text[marker.end():]
    return re.sub(r"^(Subject|From|To|Date):.*$", "", text, flags=re.MULTILINE)


def _days_in(text: str) -> set[tuple[int, int]]:
    """Every explicit day.month written in the text."""
    return {(int(d), int(m)) for d, m, _, _ in _SUBJ_DAY.findall(text or "")}


# --- a body that is quoted from line one is the order itself (#155) ---------

_QUOTE_PREFIX = re.compile(r"^\s*(?:>\s*)+")
# The envelope THIS add-on writes around the customer's text, plus the `===== file =====`
# attachment separator. None of it is customer content, so none of it may decide whether
# the customer's own text is quoted.
_ENVELOPE_LINE = re.compile(r"^(?:Subject|From|To|Date|Body|Attachments)\s*:", re.IGNORECASE)


def _today_day_month(today: str) -> tuple[int, int] | None:
    """(day, month) from `today`, accepting both `DD.MM.YYYY` and `YYYY-MM-DD`."""
    s = str(today or "").strip()
    parts = re.findall(r"\d+", s)
    if len(parts) < 3:
        return None
    if re.match(r"^\d{4}\b", s):
        return int(parts[2]), int(parts[1])
    return int(parts[0]), int(parts[1])


def _orders_a_day_still_ahead(text: str, today: str) -> bool:
    """Does the text ask for a day that has NOT already passed?

    The guard for the one case where unquoting would be harmful: an empty reply whose
    client quoted an OLD thread. That body is last week's order, and a past delivery date
    is not refused downstream — it ships (`pipeline` only skips the hold). So a quoted body
    is only accepted when at least one day it names is still ahead; otherwise the message
    keeps its old outcome, "no order found → enter by hand", which is the safe one.

    A relative-date order ("na pondelok") names no day to judge, and without a reference
    day nothing can be judged — both stay accepted rather than being blocked on a guess.
    """
    ref = _today_day_month(today)
    days = _days_in(_body_only(text))
    if not ref or not days:
        return True
    tday, tmonth = ref
    # `month + 6 < tmonth` = the order wraps into next year (December mail, January order).
    return any((m, d) >= (tmonth, tday) or m + 6 < tmonth for d, m in days)


def _unquote_line(line: str) -> str:
    """Drop the quote marker, keeping an envelope prefix (`Body: > …`) intact."""
    if _ENVELOPE_LINE.match(line.strip()):
        head, sep, rest = line.partition(":")
        return f"{head}{sep} {_QUOTE_PREFIX.sub('', rest.lstrip())}".rstrip()
    return _QUOTE_PREFIX.sub("", line)


def unquote_fully_quoted(text: str, today: str = "") -> str:
    """Strip `>` markers when the customer's WHOLE body is quoted (#155).

    Some mail clients quote every line of the message being composed, and customers do
    re-send a week's order that way. `prompts/extract.md` tells the model to ignore lines
    starting with `>` — correctly, so that a reply's old thread is not read as today's
    order — so such an email arrives at the model as nothing at all: CÉDER's four-day
    order (5.–8.8.) came back as "no order found" and was never entered.

    Markers are stripped ONLY when the body has no unquoted text of its own. A normal
    reply that adds fresh lines above an old thread keeps them, so the history goes on
    being ignored.

    This runs on the SAME string the model, the citation check and the table parser all
    read, so an item the model quotes without its `>` stays findable in the source —
    unquoting for the model alone would drop real items as unverifiable, which is the
    zero-width-space failure (#41, the AGEL order) all over again.
    """
    raw = str(text or "")
    lines = raw.splitlines()
    content = [s for s in (ln.strip() for ln in lines)
               if s and not _ENVELOPE_LINE.match(s) and not s.startswith("=====")]
    if not content or not all(_QUOTE_PREFIX.match(s) for s in content):
        return raw
    unquoted = "\n".join(_unquote_line(ln) for ln in lines)
    return unquoted if _orders_a_day_still_ahead(unquoted, today) else raw


# --- the delivery date itself must be grounded (#163) -----------------------

def _range_days(source: str) -> set[tuple[int, int]]:
    """Every (day, month) spanned by an explicit range in the text ("06.07. - 11.07."),
    inclusive of both ends.

    A week written as a range and then broken down by weekday name ("Pondelok: ...
    Utorok: ...") DERIVES its individual days from that range — the same "reading the
    email, not inventing days" reasoning `date_conflict()` already applies (its own
    `derives_days`), extended here into the actual calendar days a delivery date is
    allowed to land on.
    """
    days: set[tuple[int, int]] = set()
    for m in _RANGE.finditer(source):
        # `_RANGE`'s own match may end right at the second date's month digits with no
        # trailing dot ("11.07" — the range regex does not require one), so `_SUBJ_DAY`
        # (which DOES require a trailing dot) can miss that endpoint; a plain day.month
        # pair-finder is used here instead, scoped to just this one matched span.
        span = re.findall(r"(\d{1,2})\s*\.\s*(\d{1,2})", m.group(0))
        if len(span) != 2:
            continue
        # 2000 and 2004 are both leap years, four apart — a placeholder pair that lets a
        # literal "29.02." endpoint construct cleanly in EITHER branch below, instead of
        # raising ValueError and silently dropping the whole range.
        try:
            start = date(2000, int(span[0][1]), int(span[0][0]))
            end = date(2000, int(span[1][1]), int(span[1][0]))
        except ValueError:
            continue
        if end < start:
            end = date(2004, end.month, end.day)   # wraps into the next year (Dec -> Jan)
        cur = start
        while cur <= end and (cur - start).days < 40:   # a "week" range, not unbounded
            days.add((cur.day, cur.month))
            cur += timedelta(days=1)
    return days


def date_grounded(date_str: str, source: str) -> bool:
    """Is this delivery date actually written (or derivable) in the source text?

    The same citation requirement `verify()` already applies to every ITEM
    (`quote_in_source`/`item_in_source`) — applied here to the date itself. A delivery date
    is grounded when:

    - its (day, month) appears as a written day.month token ANYWHERE in the source
      (subject + body, quoted content included: a stale quoted date is exactly the
      evidence that proves an order is stale, so dropping it here would hide the
      staleness), OR
    - it falls inside an explicit range written in the text ("od 06.07. - 11.07." legitimately
      derives every day 06.07.-11.07., even though a per-weekday breakdown never repeats each
      one's digits — `_range_days` above), OR
    - the source names no explicit day.month token (and no range) at all — the ordinary
      relative-date case ("zajtra", "pondelok", the prompt's own "no date at all -> tomorrow"
      default) has nothing written to hold the computed date accountable against, so it is
      accepted as before.

    Only fails when the text DOES name explicit day(s)/a range and the returned date matches
    NONE of them — the exact shape of a model re-dating a stale quoted order onto an
    unwritten day (#163, msg 5679: text says "25.7." with no range, the model invented
    "08.08.2026" — 8.8 occurs nowhere in the mail and no range could derive it either).
    """
    claimed = _days_in(str(date_str or ""))
    if not claimed:
        return True   # unparseable — nothing to hold accountable, don't invent a reject
    written = _days_in(source) | _range_days(source)
    if not written:
        return True   # no explicit day or range anywhere -> ordinary relative-date order
    return bool(claimed & written)


def date_conflict(subject: str, dates: list[str], body: str = "") -> str:
    """The problem to report when the stated day and the ordered day(s) disagree.

    Two independent checks, both ending in "a human decides instead of the model guessing":

    1. The SUBJECT names ONE day and the order is for another (#81.5). Real mail: subject
       "Objednávka 29.6.2026", body "na 28.6.2026" — a Sunday. A RANGE in the subject
       ("od 06.07. - 11.07.") is not a contradiction, nor is a subject day among the ordered days.
    2. The BODY names exactly ONE delivery day but the order came back with SEVERAL. The same
       AGEL Levoča mail flipped between live corpus runs (2026-08-01): sometimes the model
       refused the contradiction, sometimes it "solved" it by shipping BOTH days as separate
       orders — which check 1 cannot see, because the subject's 29.6. IS then among the ordered
       days. A day nobody wrote is a day nobody ordered; this is the item `sourceQuote` rule
       applied to dates. Only a body that spells out exactly one day can prove this, so a
       relative-date mail ("na pondelok", no day written) is untouched.
    """
    subject = subject or ""
    if not dates:
        return ""
    ordered_days = set()
    for d in dates:
        parts = re.findall(r"\d+", str(d))
        if len(parts) >= 2:
            ordered_days.add((int(parts[0]), int(parts[1])))
    text = _body_only(body)
    body_days = _days_in(text)
    # A mail that names the week once and then lists "Pondelok:/Utorok:/…", or states a
    # range, DERIVES its extra days from what it says — that is reading the email, not
    # inventing days. Only a body with neither may be held to its single written day.
    derives_days = bool(_WEEKDAY.search(text) or _RANGE.search(text))
    if not derives_days and len(body_days) == 1 and len(ordered_days) > 1:
        only = next(iter(body_days))
        return (f"E-mail hovorí o jedinom dni {only[0]}.{only[1]}., ale objednávka vyšla na "
                f"{', '.join(dates)} — ostatné dni nikto nenapísal, treba potvrdiť")
    if _RANGE.search(subject):
        return ""
    found = _SUBJ_DAY.findall(subject)
    if len(found) != 1:
        return ""                       # no day, or several: not a single stated day
    day, month = int(found[0][0]), int(found[0][1])
    if not ordered_days or (day, month) in ordered_days:
        return ""
    return (f"Predmet e-mailu hovorí {found[0][0]}.{found[0][1]}., ale objednávka je na "
            f"{', '.join(dates)} — dva rôzne dni, treba potvrdiť")
