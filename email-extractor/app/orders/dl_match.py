"""The DL matching ladder (#202, DL migration F3): supplier + item wording -> catalog card,
mirroring `app/orders/match.py` (items) and `app/orders/customer.py` (suppliers) — same
architecture, DL's own rules (R60-R76,
docs/superpowers/specs/2026-08-07-delivery-notes-python-design.md).

Pure and DB-free, exactly like `match.py`/`customer.py`: every function here takes the model's
already-parsed answer as a plain `dict` (`llm`) and returns a `Decision`/`SupplierDecision` — it
never calls the model itself and owns no prompt file. The prompt + `client.json_call(...)` glue
is a `pipeline.py`-level concern (see `app/orders/pipeline.py`'s `_product_input`/
`_customer_input` for the AI-orders precedent) and is deliberately out of scope until the DL
worker/pipeline phase (#203/#204) — see the design comment on #202 for why building it now would
risk redoing it once that phase's own architecture (same `pipeline.py` or a parallel one) is
decided.

Supplier ladder (R60-R61):
  `supplier_candidates()` — deterministic pre-score, top 10 shown to the model (R60).
  `decide_supplier()` — interprets the model's matched/ean_edi/confidence answer; an
    EAN the model names that is not in our own supplier snapshot is never trusted (R61: "supplier
    not matched -> no EDI, whole document to review").

Item ladder (R62-R76):
  `candidates()` — deterministic pre-score, top 15 shown to the model (R62-R65).
  `decide_item()` — the post-match gate ladder (R70-R76): confidence bands, ALIAS RESCUE,
    MEMORY RESCUE (never overrides a SURE model match), the WEIGHT-CONFLICT guard with its
    `memWeightOverride` escape, and a final lexical zero-overlap tripwire copying `match.py`'s
    own proven `#195` pattern (fixed-length stem prefix, not R64's `w_eq` formula — R64's formula
    is for CANDIDATE SCORING only, see `w_eq`'s own docstring).
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field

from .desadv_edi import GTIN_FIELD_WIDTH, gtin14_to_gtin13

log = logging.getLogger("orders.dl_match")

# R71 confidence bands.
GATE_MIN = 0.70
GATE_SURE = 0.85
# R74: 1 kg == 1000 gr; a stated weight/volume outside this ratio of the card's own is a
# genuine size mismatch, not print noise.
WEIGHT_TOLERANCE = 0.1
# R65: top N item candidates shown to the model.
ITEM_CANDIDATES = 15
# #225: a word shorter than this never participates in the _score_item() word-overlap
# ratio (either side) — a bare 1-2 char Slovak preposition/conjunction ("s", "a", "v", "z",
# "o"...) is near-guaranteed to appear as a SUBSTRING of some unrelated candidate's longer
# word via w_eq()'s cheap "a in b or b in a" check, which both inflates unrelated candidates
# and dilutes the real match's own hit ratio (verified live: this pushed the correct card
# out of the top-15 shown to the model for 4 of 5 broken production wordings). 3 keeps
# genuinely meaningful short Slovak words (e.g. "syr" = cheese) scorable.
_MIN_SCORABLE_WORD_LEN = 3
# R60: top N supplier candidates shown to the model.
SUPPLIER_CANDIDATES = 10
# R60/R72: company-suffix tokens that discriminate nothing between two real companies —
# excluded from both the supplier name-overlap score and the R72 alias-names-partner check.
SUPPLIER_STOPWORDS = {"as", "sro", "spol", "ltd", "pobocka", "prevadzka", "sklad", "stores"}
# R63: brand words the warehouse's own wording routinely carries that say nothing about WHICH
# product it is — dropped before scoring, never delete them from the surrounding text itself.
BRAND_WORDS = {"bertosi", "kolios", "rovagnati", "franz", "josef", "foodwithyou", "mr"}
# R63: packaging/cut words — same reasoning, packaging is not identity.
PACKAGING_WORDS = {"platky", "narez", "cele", "krajane", "lupane", "balenie"}

# R62: known scan misreads, corrected before ANYTHING else touches the wording. A list (not a
# dict) because a fix is a regex-pattern -> literal replacement, extensible as more are found.
OCR_NAME_FIX: list[tuple[re.Pattern, str]] = [
    (re.compile(r"cere[áa]lna kocka sypan[áa]", re.I), "Cereálna kocka syrová 60g"),
]


def apply_ocr_fix(name: str) -> str:
    """R62 — the FIRST thing done to a raw DL item wording, before scoring OR deciding.
    Both `candidates()` and `decide_item()` apply this internally so a caller never has to
    remember to; the corrected wording is also what ends up in the `Decision.item_name`
    downstream code (EDI, memory) sees."""
    s = str(name or "")
    for pat, repl in OCR_NAME_FIX:
        s = pat.sub(repl, s)
    return s


# --- text helpers ---------------------------------------------------------

def _fold(text: str) -> str:
    s = unicodedata.normalize("NFD", str(text or "").lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def _norm(text: str) -> str:
    """R63: lowercase + diacritics stripped, weights dropped, brand/packaging words dropped,
    every other non-letter turned into a SPACE (never deleted — "Čučoriedka/PL" must stay two
    words, not collapse into one)."""
    s = re.sub(r"\d+(?:[.,]\d+)?\s*(kg|gr|g|ml|l)\b", " ", _fold(text))
    s = re.sub(r"[^a-z\s]", " ", s)
    words = [w for w in s.split() if w not in BRAND_WORDS and w not in PACKAGING_WORDS]
    return " ".join(words).strip()


def w_eq(a: str, b: str) -> bool:
    """R64's `wEq`: substring match, or a common-stem match — a shared prefix of at least
    `max(4, len-2)` characters on BOTH words (the shorter word's own length decides the bar,
    since a prefix can never exceed it). Catches ordinary Slovak declension
    (čučoriedka/čučoriedky/čučoriedok); a genuine STEM CHANGE (chlieb/chleba) is a known,
    accepted limit — that is what the alias/doplnok column is for (R64's own docstring in the
    spec). Looser matching is safe here because this only ranks CANDIDATES for the model, never
    decides a match on its own — contrast the stricter, fixed-length tripwire `_lexical_overlap`
    below, which DOES gate a final SURE decision and deliberately does not reuse this formula."""
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    k = max(4, min(len(a), len(b)) - 2)
    if k >= min(len(a), len(b)):
        return a == b
    return a[:k] == b[:k]


# --- weight/mass helpers ---------------------------------------------------

_MULTIPACK_RE = re.compile(r"\d+\s*[x×*]\s*[\d.,]+\s*(kg|g|l|ml)\b", re.I)
_MASS_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(kg|gr|g|ml|l)\b", re.I)


def mass_grams(name: str) -> float | None:
    """The weight/volume a NAME states, in grams (a litre is treated as 1000 g by the same
    1:1 convention R84 already uses elsewhere in this spec for "1 kg == 1000 gr" mass
    conversions — this is a deliberate simplification for the CONFLICT ratio check only, not a
    real density conversion). None for a weightless name, or a multipack name ("6x1l", R74's own
    exemption) — both are exempt from the WEIGHT-CONFLICT guard, never treated as a mismatch."""
    s = str(name or "")
    if _MULTIPACK_RE.search(s):
        return None
    m = _MASS_RE.search(s)
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    unit = m.group(2).lower()
    return value * 1000 if unit in ("kg", "l") else value


def _weights_disagree(ordered: float | None, card: float | None) -> bool:
    if not (ordered and card):
        return False
    ratio = ordered / card
    return ratio > 1 + WEIGHT_TOLERANCE or ratio < 1 - WEIGHT_TOLERANCE


def _mass_kg(card: dict | None, ordered_grams: float | None) -> float:
    """R67's own mass precedence: the matched catalog entry's own `mass` (kg) first, else
    parsed from the wording itself, else 0 — this is what a later phase's kg-conversion (R84)
    will read straight off the `Decision`, no re-derivation needed."""
    if card is not None and card.get("mass") is not None:
        return float(card["mass"])
    if ordered_grams:
        return round(ordered_grams / 1000.0, 4)
    return 0.0


# --- item candidate scoring (R65) -----------------------------------------

def _score_item(item_name_norm: str, card: dict, memory_gtin: str = "") -> float:
    name = _norm(card.get("name", ""))
    alias = _norm(card.get("doplnok", ""))
    item = item_name_norm
    score = 0.0
    if alias and len(alias) >= 4 and alias in item:
        score = 99.0
    elif item and item == name:
        score = 98.0
    else:
        if name and name in item:
            score = 70.0
        if item and item in name:
            score = max(score, 65.0)
        # #225: short filler words (1-2 chars) never enter the word-overlap ratio —
        # see _MIN_SCORABLE_WORD_LEN's own docstring.
        iw = [w for w in item.split() if len(w) >= _MIN_SCORABLE_WORD_LEN]
        for other, weight in ((name, 60.0), (alias, 70.0)):
            ow = [w for w in other.split() if len(w) >= _MIN_SCORABLE_WORD_LEN]
            if iw and ow:
                hits = sum(1 for w in iw if any(w_eq(w, o) for o in ow))
                score = max(score, hits / len(iw) * weight)
    # R65: "the history card is floored at 90 so the model always sees it" — a FLOOR, not an
    # override, so a card that also scores 99/98 on its own merits keeps that higher score.
    if memory_gtin and str(card.get("gtin")) == str(memory_gtin):
        score = max(score, 90.0)
    return score


def candidates(item_name: str, catalog: list[dict], memory_gtin: str = "",
               limit: int = ITEM_CANDIDATES) -> list[dict]:
    """R65's candidate shortlist, best first, top `limit` (15) shown to the model. Applies the
    R62 OCR fix to `item_name` first, same as `decide_item()` — both must score/decide against
    the identical corrected wording."""
    item_norm = _norm(apply_ocr_fix(item_name))
    scored = [dict(c, score=_score_item(item_norm, c, memory_gtin)) for c in catalog]
    scored.sort(key=lambda c: -c["score"])
    return scored[:limit]


# #337: an unmatched item is recognized as a RETIRED card — known-but-manual, routed to
# review with no board question — when its best retired-card name score reaches this bar
# (name-in-item / item-in-name / exact / alias — a genuine name correspondence, above the
# pure word-overlap tiers `_score_item` tops out around 60-70), OR ship-history points at a
# retired GTIN. Conservative on purpose: a false positive only routes a genuinely-unknown
# item to review (safe, and aligned with #341's less-board-noise intent), never ships
# anything wrong.
RETIRED_MATCH_MIN_SCORE = 65.0


def match_retired(item_name: str, retired_cards: list[dict],
                  recall_gtin: str = "") -> dict | None:
    """#337: return the RETIRED catalog card this item corresponds to, or None. Two
    deterministic signals: `recall_gtin` (a ship-history/alias recall the CALLER has
    already confirmed belongs to a retired card — the exact, strongest signal) wins first;
    otherwise a name score against the retired cards (`_score_item`, the SAME scorer the
    live candidate shortlist uses). Touches ONLY the retired-card list, never the active
    catalog, so it can never change a shippable decision — it is meant to run ONLY for an
    item that already failed to match an active card (active-match-first, enforced by the
    caller)."""
    if not retired_cards:
        return None
    if recall_gtin:
        for c in retired_cards:
            if str(c.get("gtin")) == str(recall_gtin):
                return c
    best = candidates(item_name, retired_cards, limit=1)
    if best and float(best[0].get("score", 0.0)) >= RETIRED_MATCH_MIN_SCORE:
        return best[0]
    return None


# --- supplier candidate scoring (R60) --------------------------------------

_EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}")


def _first_email(value: str) -> str:
    m = _EMAIL_RE.search(str(value or "").lower())
    return m.group(0) if m else ""


def _supplier_words(text: str) -> list[str]:
    return [w for w in re.sub(r"[^a-z0-9\s]", " ", _fold(text)).split() if len(w) > 2]


def _score_supplier(supplier_name: str, supplier_email: str, supplier_city: str,
                    row: dict) -> float:
    name = _fold(supplier_name).strip()
    rname = _fold(row.get("name", "")).strip()
    score = 0.0
    # R60: name is a TIERED score ("exact 100 / substring 60 / word-overlap...") — only the
    # best-fitting tier counts, they are not additive.
    if name and rname:
        if name == rname:
            score = 100.0
        elif name in rname or rname in name:
            score = 60.0
        else:
            nw, rw = set(_supplier_words(name)), set(_supplier_words(rname))
            overlap = nw & rw
            if nw and overlap:
                score = 20.0 + len(overlap) / len(nw) * 40.0
    # R60: email is stated as two separate ADDITIVE bonuses ("same domain +30, exact +20") — an
    # exact address match trivially also matches on domain, so it earns both (50 total),
    # correctly outranking a domain-only match (30).
    addr = _first_email(supplier_email)
    domain = addr.split("@")[-1] if "@" in addr else ""
    remails = [e.lower() for e in (row.get("emails") or [])]
    if domain and any(e.split("@")[-1] == domain for e in remails):
        score += 30.0
    if addr and addr in remails:
        score += 20.0
    # R60: city is the same TIERED shape as name (exact implies substring, so only the best
    # tier counts — the "+15/+10" phrasing distinguishes the TIER, not a stack).
    city = _fold(supplier_city).strip()
    rcity = _fold(row.get("city", "")).strip()
    if city and rcity:
        if city == rcity:
            score += 15.0
        elif city in rcity or rcity in city:
            score += 10.0
    return score


def supplier_candidates(supplier_name: str, supplier_email: str, supplier_city: str,
                        suppliers: list[dict], limit: int = SUPPLIER_CANDIDATES) -> list[dict]:
    """R60's shortlist, best first, top `limit` (10) shown to the model."""
    scored = [dict(s, score=_score_supplier(supplier_name, supplier_email, supplier_city, s))
             for s in suppliers]
    scored.sort(key=lambda s: -s["score"])
    return scored[:limit]


# --- supplier decision (R61) ------------------------------------------------

@dataclass
class SupplierDecision:
    matched: bool
    ean_edi: str
    name: str
    confidence: float
    note: str
    review: bool = False


def decide_supplier(llm: dict, suppliers: list[dict]) -> SupplierDecision:
    """R61: the model decides `matched`/`ean_edi` directly (no deterministic gate ladder for
    suppliers, unlike items) — this only (a) normalizes confidence, and (b) refuses to trust an
    `ean_edi` the model invented that is not in OUR OWN supplier whitelist (a hallucinated EAN
    must never look like a real match). `matched=False` here is R61's "supplier not matched ->
    no EDI, whole document to review"."""
    raw_conf = llm.get("matchConfidence")
    if raw_conf is None:
        raw_conf = llm.get("confidence") or 0
    conf = float(raw_conf)
    if conf > 1:
        conf /= 100
    ean = str(llm.get("ean_edi") or "")
    matched = bool(llm.get("matched")) and bool(ean)
    if not matched:
        log.info("dl supplier not matched: %r", llm.get("matchReason") or llm.get("reason"))
        return SupplierDecision(
            matched=False, ean_edi="", name="", confidence=conf,
            note=str(llm.get("matchReason") or llm.get("reason")
                     or "Dodávateľ nebol nájdený v databáze — celý dokument na kontrolu."),
            review=True)
    row = next((s for s in suppliers if str(s.get("ean_edi")) == ean), None)
    if not row:
        log.warning("dl supplier match refused: model proposed ean %r, not in whitelist", ean)
        return SupplierDecision(
            matched=False, ean_edi="", name="", confidence=conf,
            note=f"Model navrhol EAN „{ean}“, ktorý nie je v tabuľke dodávateľov.", review=True)
    return SupplierDecision(matched=True, ean_edi=ean, name=row.get("name", ""),
                            confidence=conf,
                            note=str(llm.get("matchReason") or "Spárované modelom."))


# --- #322: deterministic CODEX-card supplier resolution ---------------------

def supplier_name_key(name: str) -> str:
    """A CONSERVATIVE exact-match key for a supplier name: fold diacritics + lowercase,
    every non-alphanumeric run -> ONE space, whitespace collapsed. Legal-form tokens
    (`s.r.o.`, `a.s.`, ...) are DELIBERATELY kept (never dropped, unlike `_score_supplier`'s
    R60 word-overlap tier) — a deterministic auto-match must not silently equate two distinct
    legal entities that share a core name ('ABC s.r.o.' vs 'ABC a.s.'). If two forms differ
    they simply don't match here (safe); if they collide they are AMBIGUOUS and fall through
    to the board question. 'Forbak, s. r. o.'/'FORBAK s.r.o.' both fold to 'forbak s r o'."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", _fold(name)).split())


def resolve_supplier_from_cards(doc: dict, cards: list[dict],
                                sender_email: str = "") -> SupplierDecision | None:
    """#322: a CONSERVATIVE, model-free identity match of the document's supplier against the
    effective CODEX supplier list (`dl_snapshot.dl_suppliers_for_management` — frozen snapshot
    ∪ /znalosti overrides). Returns a matched `SupplierDecision` ONLY on an UNAMBIGUOUS
    identity match (exactly one distinct EAN), else `None` (the caller keeps the existing
    ask/review path). NEVER guesses on ambiguity — a false supplier match ships a
    wrongly-addressed EDI to ORION.

    Used to rescue a MODEL MISS before firing a `dl_supplier` board question (Marek's #322
    decision: a card in CODEX must stand on its own, no nástenka question). Priority:
      1. EAN, when the document carries one (`supplierEanEdi`) — DORMANT today (the extraction
         schema has no supplier EAN), but ready for a future field, and the strongest key;
      2. an email the document (`supplierEmail`) or the envelope (`sender_email`) carries,
         appearing in exactly one card's `emails` — an address is a unique identifier;
      3. the document's normalized supplier NAME equal to exactly one card's normalized name;
         when 2+ cards share the name, the document's city breaks a genuine tie, else ambiguous.

    Ambiguity is ALWAYS measured across DISTINCT `ean_edi` values over the FULL card list at
    every stage (never a per-EAN-collapsed dict — that would discard a same-EAN branch's city
    and let a genuinely city-ambiguous identity resolve, #322 review 🔴): two branch cards
    sharing one EAN are ONE delivery target, never an ambiguity. A card with a blank `ean_edi`
    is skipped entirely — an EDI cannot be built without one, so such a card can never be a
    real match."""
    usable = [c for c in cards if str(c.get("ean_edi") or "").strip()]
    if not usable:
        return None

    def _make(card: dict, note: str) -> SupplierDecision:
        return SupplierDecision(matched=True, ean_edi=str(card["ean_edi"]),
                                name=card.get("name", ""), confidence=1.0, note=note)

    def _eans(cs: list[dict]) -> set[str]:
        return {str(c["ean_edi"]) for c in cs}

    # The document's own printed name, matched across ALL cards (never collapsed per EAN —
    # the city tiebreak in rung 3 needs every branch's city; #322 review 🔴). Reused by the
    # rung-2 contradiction guard too.
    name_key = supplier_name_key(doc.get("supplierName", ""))
    name_cards = ([c for c in usable
                   if supplier_name_key(c.get("name", "")) == name_key] if name_key else [])
    name_eans = _eans(name_cards)

    # (1) EAN — dormant (no supplier EAN in today's extraction schema), but ready. Every card
    # sharing this EAN is one delivery target, so any of them is a safe, unambiguous match.
    doc_ean = str(doc.get("supplierEanEdi") or doc.get("supplierEan") or "").strip()
    if doc_ean:
        ean_cards = [c for c in usable if str(c["ean_edi"]) == doc_ean]
        if ean_cards:
            return _make(ean_cards[0],
                         "Priradené podľa EAN dodávateľa z dokumentu (CODEX karta).")

    # (2) email — a unique identifier — UNLESS the document's own printed name unambiguously
    # name-keys to a DIFFERENT card: a shared forwarding/3PL address registered on one card
    # must not silently route another NAMED supplier's document to that card's EAN (the
    # #307/#314 "tlaciaren@ forwards everything" risk; #322 review 🔵). On such a name-vs-email
    # contradiction, defer to the name rung below, which matches the printed name.
    wanted = {e for e in (str(doc.get("supplierEmail") or "").strip().lower(),
                          str(sender_email or "").strip().lower()) if e}
    if wanted:
        email_cards = [c for c in usable
                       if wanted & {str(e).strip().lower() for e in (c.get("emails") or [])}]
        email_eans = _eans(email_cards)
        contradicts = (len(name_eans) == 1 and len(email_eans) == 1
                       and name_eans != email_eans)
        if len(email_eans) == 1 and not contradicts:
            return _make(email_cards[0],
                         "Priradené podľa e-mailu dodávateľa (CODEX karta).")

    # (3) normalized name, unambiguous — the document's city breaks a genuine tie, counted by
    # DISTINCT ean_edi over the FULL name-matching card list (#322 review 🔴).
    if name_eans:
        if len(name_eans) == 1:
            return _make(name_cards[0], "Priradené podľa názvu dodávateľa (CODEX karta).")
        city_key = supplier_name_key(doc.get("supplierCity", ""))
        if city_key:
            city_cards = [c for c in name_cards
                          if supplier_name_key(c.get("city", "")) == city_key]
            if len(_eans(city_cards)) == 1:
                return _make(city_cards[0],
                             "Priradené podľa názvu + mesta dodávateľa (CODEX karta).")
        # 2+ distinct EANs share the name and no unique city -> AMBIGUOUS, never guess.

    return None


# --- item decision ladder (R70-R76) -----------------------------------------

@dataclass
class Decision:
    item_name: str
    gtin: str | None
    card: str
    mass: float
    confidence: float
    rule: str
    note: str
    review: bool = False
    trace: dict = field(default_factory=dict)


def _card(catalog: list[dict], gtin) -> dict | None:
    return next((c for c in catalog if str(c.get("gtin")) == str(gtin or "")), None)


def _gtin_edi_overflow(gtin) -> bool:
    """#245: a catalog card's own GTIN can legitimately be a valid GTIN-14 (a bulk/
    wholesale trade unit, e.g. a 30kg block) that does NOT fit the DESADV LIN record's
    FIXED 13-character GTIN field (`desadv_edi.GTIN_FIELD_WIDTH`). `desadv_edi._pad()`
    silently TRUNCATES anything longer instead of erroring, corrupting the code into a
    value that matches no real ORION stock card — this is exactly what shipped
    18585037201518 ("Margarín stolný - Favorit") as the truncated 1858503720151 and left
    the document permanently stuck, rejected on every CODEX import attempt. A card this
    wide can never be represented through this channel — treat it the same as "no card
    at all", never let it ship."""
    return len(str(gtin or "")) > GTIN_FIELD_WIDTH


def _partner_tokens(name: str) -> list[str]:
    return [w for w in re.sub(r"[^a-z0-9\s]", " ", _fold(name)).split()
            if len(w) >= 4 and w not in SUPPLIER_STOPWORDS]


def _alias_names_partner(alias_norm: str, partner_name: str) -> bool:
    toks = _partner_tokens(partner_name)
    return bool(toks) and any(t in alias_norm for t in toks)


# R75's lexical tripwire (final safety net on a SURE llm decision) copies `match.py`'s own
# proven, corpus-validated `#195` pattern exactly: a FIXED stem-prefix, not R64's `w_eq`
# formula — `w_eq` is deliberately looser (it only ranks candidates); this gate DECIDES whether
# a confident answer ships, so it needs the stricter, already-validated constant.
_STEM_PREFIX = 4
_GENERIC_WORDS = {"chlieb", "chleba", "chlebom", "chlebu"}


def _distinctive_words(name: str) -> set[str]:
    return {w for w in _norm(name).split() if len(w) >= 4 and w not in _GENERIC_WORDS}


def _lexical_overlap(item_words: set[str], card_words: set[str]) -> set[str]:
    return {w for w in item_words
           if any(w[:_STEM_PREFIX] == c[:_STEM_PREFIX] for c in card_words)}


def decide_item(item_name: str, llm: dict, catalog: list[dict], recalled=None,
                partner_name: str = "") -> Decision:
    """R70-R76's post-match gate ladder. `recalled` is a `dl_memory.Recalled` (or `None`) — see
    `dl_memory.resolve()` (R66); `partner_name` is the ALREADY-matched supplier's name (R67's
    "PARTNER ON THIS DOCUMENT" — R72's alias rescue checks the card's alias against tokens of
    THIS name, mirroring `match.py`'s customer-naming alias rung)."""
    item_name = apply_ocr_fix(item_name)
    raw_conf = llm.get("matchConfidence")
    if raw_conf is None:
        raw_conf = llm.get("confidence") or 0
    conf = float(raw_conf)
    if conf > 1:
        conf /= 100
    raw_gtin = llm.get("gtin")
    llm_gtin = None if (not raw_gtin or str(raw_gtin).upper() == "NO_MATCH") else str(raw_gtin)
    llm_card = _card(catalog, llm_gtin) if llm_gtin else None
    # A gtin that resolves to no card in OUR catalog is not a real answer (mirrors match.py's
    # own `unknown_gtin` guard — never dereference it, never let it silently NoneType-crash).
    unknown_gtin = bool(llm_gtin) and llm_card is None
    if unknown_gtin:
        llm_gtin, llm_card = None, None

    # #245: a card that DOES exist but whose gtin cannot fit the DESADV LIN field must be
    # treated the same as "no card" — never silently truncated downstream. Capture the
    # card's name BEFORE resetting so the final unmatched note can still tell the warehouse
    # what the model actually proposed (mirrors R71's own "kept the rejected name" pattern).
    # `llm_card is None` already implies `llm_gtin` is falsy (set together two lines above),
    # so `_gtin_edi_overflow(llm_gtin)` alone would short-circuit False the same way — the
    # explicit `is not None` is kept only so `llm_card["name"]` below can never be reached
    # on a None card, even if that invariant ever changes upstream (review finding, #245).
    gtin_overflow_card = None
    gtin_overflow_gtin = None
    if llm_card is not None and llm_gtin is not None and _gtin_edi_overflow(llm_gtin):
        gtin_overflow_card = llm_card["name"]
        gtin_overflow_gtin = llm_gtin
        log.warning("dl gtin edi overflow: %r card %r gtin %s is %d chars, DESADV LIN "
                   "field is %d — cannot ship, routing to review", item_name,
                   llm_card["name"], llm_gtin, len(llm_gtin), GTIN_FIELD_WIDTH)
        llm_gtin, llm_card = None, None

    ordered_w = mass_grams(item_name)
    card_w = mass_grams((llm_card or {}).get("name", "")) if llm_card else None
    weight_conflict = _weights_disagree(ordered_w, card_w)

    alias = _norm((llm_card or {}).get("doplnok", "")) if llm_card else ""
    alias_names_partner = bool(alias) and _alias_names_partner(alias, partner_name)

    trace = {
        "llm": {"gtin": raw_gtin, "confidence": conf, "unknown_gtin": unknown_gtin},
        "weight": {"ordered": ordered_w, "card": card_w, "conflict": weight_conflict},
        "alias": {"names_partner": alias_names_partner},
        "history": None if not recalled else {
            "gtin": recalled.gtin, "unanimous": recalled.unanimous,
            "weight_override": recalled.weight_override, "note": recalled.note},
    }

    def done(rule, gtin, card, mass, confidence, note, review=False) -> Decision:
        trace["rule"] = rule
        return Decision(item_name=item_name, gtin=gtin, card=card, mass=mass,
                        confidence=confidence, rule=rule, note=note, review=review,
                        trace=dict(trace))

    # R72 — ALIAS RESCUE. Unlike orders' equivalent rung, R67 explicitly says the alias
    # confirmation overrides even the SIZE/weight rule ("unless alias confirms"), so this runs
    # BEFORE the weight-conflict guard, unconditionally. `conf > 0` is a defensive floor (R72's
    # own text states no minimum) mirroring match.py's alias_customer rung: it only excludes
    # the degenerate case of a `gtin` the model returned alongside a literal 0 confidence,
    # never a realistic proposal.
    if llm_gtin and conf > 0 and alias_names_partner:
        assert llm_card is not None  # llm_gtin truthy ⟹ card resolved (unknown_gtin/overflow above)
        log.info("dl alias rescue: %r -> %s (partner %r, raw conf %.2f)",
                 item_name, llm_gtin, partner_name, conf)
        return done("alias_rescue", llm_gtin, llm_card["name"],
                    _mass_kg(llm_card, ordered_w), max(conf, 0.95),
                    f"Potvrdené aliasom karty — alias menuje dodávateľa „{partner_name}“ "
                    f"(pôvodná istota modelu {round(conf * 100)} %).")

    # R73 — MEMORY RESCUE. Fires below the sure gate, or on an outright model NO_MATCH; NEVER
    # overrides a confident (>=0.85) model match — that condition is right here in the guard.
    if recalled and (conf < GATE_SURE or not llm_gtin):
        rec_card = _card(catalog, recalled.gtin)
        # #245: a remembered gtin that overflows the DESADV field is exactly as unshippable
        # as a fresh model answer that does — never resurrect it either.
        if rec_card and _gtin_edi_overflow(recalled.gtin):
            log.warning("dl memory rescue skipped: %r card %r gtin %s is %d chars, DESADV "
                       "LIN field is %d — cannot ship, routing to review", item_name,
                       rec_card["name"], recalled.gtin, len(str(recalled.gtin)),
                       GTIN_FIELD_WIDTH)
            rec_card = None
        if rec_card:
            log.info("dl memory rescue: %r -> %s (%s)", item_name, recalled.gtin, recalled.note)
            return done("memory_rescue", recalled.gtin, recalled.card,
                        _mass_kg(rec_card, ordered_w), 0.95,
                        f"Potvrdené históriou dodávok — tomuto dodávateľovi sme pre "
                        f"„{item_name}“ evidovali „{recalled.card}“ ({recalled.note}).")

    # R74 — WEIGHT-CONFLICT guard, with the memWeightOverride escape.
    if llm_gtin and weight_conflict:
        assert llm_card is not None  # llm_gtin truthy ⟹ card resolved
        if recalled and recalled.weight_override and str(recalled.gtin) == str(llm_gtin):
            log.info("dl weight override: %r -> %s despite weight conflict (%s)",
                     item_name, llm_gtin, recalled.note)
            return done("weight_override", llm_gtin, llm_card["name"],
                        _mass_kg(llm_card, ordered_w), max(conf, 0.95),
                        f"Gramáž objednávky nesúhlasí s kartou, ale história dodávok "
                        f"({recalled.note}) potvrdzuje tú istú kartu — akceptované.",
                        review=True)
        log.info("dl weight conflict: %r rejected candidate %s (ordered=%s, card=%s)",
                 item_name, llm_gtin, ordered_w, card_w)
        return done("unmatched", None, "", 0.0, conf,
                    f"Zamietnuté — gramáž objednávky nesúhlasí s kartou "
                    f"(kandidát „{llm_card['name']}“).")

    if not llm_gtin:
        if gtin_overflow_card:
            # #246: if the overflowing code is a VALID GTIN-14, hand the warehouse its
            # computed 13-digit GS1 sibling to verify in CODEX — the exact manual step the
            # owner did for the 9 confirmed Mana Roots products. `gtin14_to_gtin13` returns
            # None for a 14-char code that is NOT a valid GTIN-14 (the warehouse-confirmed
            # 14-only "Korenie" case), so a plausible-but-false naive strip is never shown.
            # This only enriches the note; the decision stays `unmatched` and never ships.
            sibling = gtin14_to_gtin13(gtin_overflow_gtin)
            hint = (f" Vypočítaný 13-miestny variant je {sibling} — over v CODEXe, či karta "
                    f"existuje pod týmto kratším kódom; ak áno, doplň ho do katalógu namiesto "
                    f"14-miestneho." if sibling else "")
            return done("unmatched", None, "", 0.0, conf,
                        f"Karta „{gtin_overflow_card}“ má v CODEXe EAN dlhší ako "
                        f"{GTIN_FIELD_WIDTH} znakov — do DESADV takto poslať nemožno "
                        "(skrátenie by kód poškodilo). Treba doplniť/opraviť skladovú "
                        "kartu v CODEXe alebo označiť správny kód." + hint)
        return done("unmatched", None, "", 0.0, conf,
                    str(llm.get("matchReason") or "Model nenašiel zhodu (NO_MATCH)."))

    # R70/R71 — confidence bands. Past the `if not llm_gtin` return above, llm_gtin is
    # truthy ⟹ llm_card resolved.
    assert llm_card is not None
    if conf >= GATE_SURE:
        item_words = _distinctive_words(item_name)
        card_words = _distinctive_words(llm_card.get("name", "")) | _distinctive_words(alias)
        overlap = _lexical_overlap(item_words, card_words)
        lexical_gap = bool(item_words and card_words and not overlap)
        trace["lexical_guard"] = {"item_words": sorted(item_words),
                                  "card_words": sorted(card_words),
                                  "overlap": sorted(overlap), "fired": lexical_gap}
        if lexical_gap:
            log.info("dl lexical tripwire: %r vs %r share no distinctive word",
                     item_name, llm_card["name"])
            return done("llm_sure_lexical_gap", None, "", 0.0, conf,
                        f"Model si je istý ({round(conf * 100)} %), ale znenie "
                        f"„{item_name}“ nemá s kartou „{llm_card['name']}“ spoločné "
                        "žiadne rozlišujúce slovo — prosím prekontrolujte.", review=True)
        return done("llm_sure", llm_gtin, llm_card["name"], _mass_kg(llm_card, ordered_w),
                    conf, str(llm.get("matchReason") or "Spárované modelom."))
    if conf >= GATE_MIN:
        return done("llm_borderline", llm_gtin, llm_card["name"], _mass_kg(llm_card, ordered_w),
                    conf,
                    f"Prešlo na hranici istoty ({round(conf * 100)} %, pod 85 %), kandidát "
                    f"„{llm_card['name']}“ — prosím prekontrolujte.", review=True)
    return done("unmatched", None, "", 0.0, conf,
                f"Zamietnuté — istota {round(conf * 100)} % je pod hranicou 70 % "
                f"(kandidát „{llm_card['name']}“).")
