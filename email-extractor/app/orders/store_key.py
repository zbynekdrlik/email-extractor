"""Which SHOP of a multi-shop customer one order is for — the order's store key (#493).

A customer with several shops sends ONE mail with one attachment per shop (the 2026-10-16
incident: five xlsx files, headers „BISTRO", „Predajňa: Tržnica", „ŽILINA", „ŠKOLA, …" and an
empty „Predajňa:" whose only identifier is the file name „… PR.xlsx"). The extraction gives
each order its block header (`store`, #101) and — since #493 — the attachment it was read
from (`sourceFile`, located here by the header, else the item quotes). This module turns those
two into a normalized, deterministic identity the warehouse's learned shop rules are matched
against (`match_rule`) and the per-shop question is keyed on; card names only RANK that
question's candidates.

Pure: no DB, no model. Folding goes through `dl_match.fold` — the ONE diacritic folder of the
package (never re-derived, the #265 Slovak-stem lesson) — and stems use the corpus-validated
4-letter prefix (`match.STEM_PREFIX`, #195).

Normalization drops words that never identify a shop: order/document words, OUR OWN brand
(customers name their files „OBJ Pekárová žena …" — an order TO us), week/day/month words
that change every week (the learned rule must survive next week's file), legal forms and
pure numbers. Words every order of the mail shares („OBJ Pekárová žena" in all five file
names, after the brand drop whatever else repeats) are removed from the minimal `key` — they
cannot tell two shops apart.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import dl_match
from .match import STEM_PREFIX as STEM  # the corpus-validated Slovak stem length (#195)

_SECTION_RE = re.compile(r"^=====\s*(.+?)\s*=====[ \t]*$", re.MULTILINE)
_EXT_RE = re.compile(r"\.(xlsx|xlsm|xls|ods|fods|csv|pdf|docx?|odt|txt|png|jpe?g|eml)$",
                     re.IGNORECASE)
_WEEK_CODE_RE = re.compile(r"^(kw|t|tyz|w|wk)\d+$")
_SPACES_RE = re.compile(r"\s+")
# what a HEADER's numbers must not keep (review round 5: „BISTRO 16.10." changed its identity
# every mail, so its rule never matched again): a date („16.10.", „16. 10. 2026", „16.10") or
# date range („16.-20.10.", round 6), a calendar week („42. týždeň", „43 KT", „KW 42") — on the
# folded text; an order number after „Obj." and numbers of 6+ digits are dropped (`tokens`).
# Everything shorter stays — a house number („Hlavná 1713" / „Hlavná 2210" are two shops,
# round 6) and a year too: a year in a header re-asks once a year, dropping it could make two
# shops one. A range needs its dot („16.-20.10."): „BISTRO 1 - 16.10." keeps its 1 (round 7).
_HEADER_NOISE_RE = re.compile(
    r"(?<!\d)\d{1,2}\s*/\s*\d{1,2}\s*/\s*(?:\d{4}|\d{2})(?!\d)"
    r"|(?<!\d)\d{1,2}\s*\.\s*\d{1,2}(?:\s*\.\s*(?:\d{4}|\d{2})(?!\d))?\.?(?!\d)"
    r"|(?<!\d)\d{1,2}\s*\.\s*[-\u2013\u2014]\s*(?=\d{1,2}\s*\.\s*\d{1,2})"
    r"|\bobj(?!ekt)\w*\s*\.?\s*(?:c\s*\.?\s*|cislo\s*)?\d{4,}"
    r"|(?<!\d)\d{1,2}\s*\.?\s*(?:tyzden\w*|tyzd\w*|tyz|week|kw|kt)\b"
    r"|\b(?:kw|kt|week|tyzden)\s*\.?\s*\d{1,2}(?!\d)(?!\s*\.\s*\d{1,2}(?!\d))")
MAX_HEADER_NUMBER = 5   # digits of a number a header keeps (an order number is longer)

# generic shop words — „Predajňa: Tržnica" is the Tržnica shop; alone („Predajňa:") they name
# no shop, yet a block of its own (`names_a_block`)
SHOP_WORDS = frozenset({
    "predajna", "predajne", "predajni", "predajnu", "prevadzka", "prevadzky", "prevadzku",
    "pobocka", "pobocky", "filialka",
})
# order / document words
DOCUMENT_WORDS = frozenset({
    "obj", "objed", "objednavka", "objednavky", "objednavku", "objednavok", "objednavkovy",
    "order", "zoznam", "tabulka", "harok", "sheet", "list",
})
# our own brand: the supplier, never a customer's shop
BRAND_WORDS = frozenset({"pekarova", "pekarovej", "pekarovu", "zena", "zeny", "slovnormal"})
# week / day words — change every week, never identify a shop; month names in the NOMINATIVE
# only („október" in a file name) — the genitive forms are street names („29. augusta")
TIME_WORDS = frozenset({
    "tyzden", "tyzdna", "tyzdni", "tyzdne", "tyz", "tyzd", "week", "kw",
    "pondelok", "utorok", "streda", "stvrtok", "piatok", "sobota", "nedela",
    "januar", "februar", "marec", "april", "maj", "jun", "jul", "august", "september",
    "oktober", "november", "december",
})
# legal forms / connectors
FILLER_WORDS = frozenset({"sro", "spol", "as", "na", "pre", "do", "od",
                          "a", "s", "r", "o", "v", "z", "k"})
STOPWORDS = SHOP_WORDS | DOCUMENT_WORDS | BRAND_WORDS | TIME_WORDS | FILLER_WORDS


def _squash(text: str) -> str:
    return _SPACES_RE.sub(" ", dl_match.fold(text)).strip()


def tokens(text, keep_numbers: bool = False) -> list[str]:
    """Normalized words of a header / file name / card text, in order, de-duplicated:
    folded, split on every non-alphanumeric, without stopwords, week codes („kw42") and —
    unless `keep_numbers` (a shop HEADER: „Predajňa 1" / „Predajňa 2" are two shops) — pure
    numbers. Short words stay (the „PR" / „ZA" shop codes); the question ranking compares
    only words of `STEM`+ letters. A header keeps standalone numbers of up to
    `MAX_HEADER_NUMBER` digits — never a date, a week number or an order number
    (`_HEADER_NOISE_RE`)."""
    out: list[str] = []
    folded = dl_match.fold(text)
    if keep_numbers:
        folded = _HEADER_NOISE_RE.sub(" ", folded)
    for t in re.split(r"[^a-z0-9]+", folded):
        if t.isdigit() and keep_numbers and len(t) <= MAX_HEADER_NUMBER:
            if t not in out:
                out.append(t)
            continue
        if len(t) < 2 or t.isdigit() or t in STOPWORDS or _WEEK_CODE_RE.match(t):
            continue
        if t not in out:
            out.append(t)
    return out


def stems(words) -> set[str]:
    """The 4-letter stems of the words long enough to carry one (`STEM`+ letters)."""
    return {w[:STEM] for w in words if len(w) >= STEM and not w.isdigit()}


def file_stem(name: str) -> str:
    """„OBJ Pekárová žena PR.xlsx" -> „OBJ Pekárová žena PR"."""
    return _EXT_RE.sub("", str(name or "").strip())


# --- which attachment an order was read from -------------------------------------------

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _words_text(text: str) -> str:
    """Folded text with every non-alphanumeric run turned into ONE space („Chlieb | 3" and
    „Chlieb\t3" both become „chlieb 3")."""
    return " " + " ".join(_NON_ALNUM_RE.split(dl_match.fold(text))).strip() + " "


def attachment_sections(source: str) -> list[tuple[str, str]]:
    """The `===== <file name> =====` sections `process._combined_text` appends after
    „Attachments:" — [(file name, its words text — `_words_text`)], in order."""
    text = str(source or "")
    marks = list(_SECTION_RE.finditer(text))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end]
        out.append((m.group(1).strip(), _words_text(body)))
    return out


def _in_section(needle: str, words: str) -> bool:
    """Is one item text in this section? Compared word by word (every separator — space,
    tab, „|", „," — counts the same, like a quoted table row in `extract.quote_in_source`)
    and only as WHOLE words: „chlieb 120" is never found inside „chlieb 1200"."""
    want = _words_text(needle)
    return len(want.strip()) >= 3 and want in words


def locate_file(texts, source: str = "", sections=None, header: str = "",
                body: str | None = None) -> str:
    """The attachment an order was read from: the ONE section its block `header` is in, else
    the section holding the most of its items (a quote or a name each) — only a STRICT
    winner; "" otherwise (no sections, no hit, a tie, or the header and the lines naming two
    different files). Best effort and never guessed: an order whose file stays unknown keeps
    only its block header as the store key. The header comes first (review round 5): the
    incident's files share most rows, so the lines tied and one shop's orders got different
    identities day by day. Pass `sections` (computed ONCE per mail with
    `attachment_sections`) and the mail's `body` (`body_text`) when locating several orders
    of one mail."""
    if sections is None:
        sections = attachment_sections(source)
    if body is None:
        body = body_text(source)
    if not sections:
        return ""
    by_lines = _by_lines(texts, sections, body)
    by_header = ""
    if str(header or "").strip():
        hits = [name for name, words in sections if _in_section(header, words)]
        by_header = hits[0] if len(hits) == 1 else ""
    if by_header and by_lines and by_header != by_lines:
        return ""
    return by_header or by_lines


def body_text(source: str) -> str:
    """The words text of the mail itself — everything before its first attachment section."""
    text = str(source or "")
    first = _SECTION_RE.search(text)
    return _words_text(text[:first.start()] if first else text)


def _by_lines(texts, sections, body: str = "") -> str:
    """The section holding the most of the items — a strict winner, else "". An item is a
    text or a tuple of alternatives, the first one found ANYWHERE deciding: its quote, and its
    name only when the quote is nowhere (review round 7: a quote the model reformatted matched
    nothing; round 8: the incident's order forms share their product rows — a name is in
    every file and must never tie a located quote). A text found in the mail `body` is no
    attachment's — checked FIRST, a form may list the same product (rounds 9-10: a body
    order's quote or name in a form put it into that form's shop)."""
    counts = [0] * len(sections)
    found = False
    for x in texts or ():
        for t in (x,) if isinstance(x, str) else x:
            if len(_squash(t)) < 3:
                continue
            if body and _in_section(t, body):
                break   # in the mail itself — counts for no file, even if a form has it too
            hits = [i for i, (_name, words) in enumerate(sections) if _in_section(t, words)]
            if hits:
                for i in hits:
                    counts[i] += 1
                found = True
                break
    if not found:
        return ""
    best = max(counts)
    winners = [i for i, n in enumerate(counts) if n == best]
    return sections[winners[0]][0] if len(winners) == 1 else ""


# --- the key ----------------------------------------------------------------------------

@dataclass(frozen=True)
class StoreKey:
    """One order's shop identity. `tokens` = every normalized word of its header (numbers
    kept) + its file name — independent of the rest of the mail: it is the shop's IDENTITY,
    what a learned rule and the per-shop question are keyed on (`text`); `key` = `tokens`
    minus the words every order of the mail shares and the sender's own company / name words
    — only what RANKS the cards of the per-shop question (`customer.candidates_for_question`);
    a card's name never decides a shop (review round 7)."""
    header: str = ""
    source_file: str = ""
    tokens: frozenset = frozenset()
    key: frozenset = frozenset()
    # the mail has attachments and this order's could not be told (`extract._located`)
    file_unknown: bool = False

    @property
    def text(self) -> str:
        """The stored form of the shop identity (sorted `tokens`, space-joined) — rule key and
        question dedupe. Never the mail-relative `key` (review round 3: „Tesco Žilina" +
        „Kaufland Žilina" and „Tesco Martin" + „Kaufland Martin" both reduce to {tesco}/
        {kaufland}, and a rule stored that way captured the other town's shops)."""
        return " ".join(sorted(self.tokens))

    @property
    def label(self) -> str:
        """What the warehouse sees: „BISTRO" (príloha OBJ ….xlsx)."""
        head = self.header.strip() or file_stem(self.source_file)
        if self.source_file:
            return f"„{head}“ (príloha {self.source_file})"
        return f"„{head}“"

    def stems(self) -> set[str]:
        return stems(self.key)

    @property
    def identity(self) -> frozenset:
        """Which shop this order says it is (= `tokens`). Empty = it names no shop. Two files
        of one shop for two days („… pondelok", „… streda") are one identity (day words are
        stopwords); two „ŽILINA" files of different names are two."""
        return self.tokens


def file_tokens(name: str) -> frozenset:
    """The words a file name contributes to a shop identity."""
    return frozenset(tokens(file_stem(name)))


def for_orders(orders: list[dict], drop=()) -> list[StoreKey]:
    """The store key of every order of ONE mail, in order. `key` (the question ranking's
    view) drops the words every distinct token set of the mail shares and `drop` (the
    sender's own company / name words) — empty when nothing distinguishing is left (a brand
    word „Kvetka …" in every file must not rank the head-office card first, round 4)."""
    raw = []
    for o in orders:
        header = str(o.get("store") or "").strip()
        src = str(o.get("sourceFile") or "").strip()
        raw.append((header, src, frozenset(tokens(header, keep_numbers=True))
                    | file_tokens(src), bool(o.get("fileUnknown"))))
    distinct = {t for _h, _s, t, _u in raw if t}
    common = frozenset.intersection(*distinct) if len(distinct) >= 2 else frozenset()
    brand = frozenset(drop)
    return [StoreKey(header=header, source_file=src, tokens=toks, key=toks - common - brand,
                     file_unknown=unknown)
            for header, src, toks, unknown in raw]


# a header's words that never name a shop block on their own („Objednávka 04.08.", „Hárok 1",
# „v pondelok", „a.s.") — the SAME lists `tokens` drops, plus the number words „č." / „no."
_NOT_A_BLOCK = DOCUMENT_WORDS | BRAND_WORDS | TIME_WORDS | FILLER_WORDS | {"c", "cislo", "no",
                                                                          "nr", "kt"}
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")


def names_a_block(key: StoreKey) -> bool:
    """The order sits under a header block of its own: its header carries a generic shop word
    („Predajňa:"), a word that is no document / time / filler word (a name), a lone CAPITAL
    letter (a shop code „A"), or a number no document word owns („č. 2"). A header of only
    noise — a date („04/08/2026" too), a calendar week („T42"), „Objednávka č. 123", „Hárok 1",
    „v pondelok", „a.s.", a bare year — names no block (review rounds 10-13)."""
    folded = _HEADER_NOISE_RE.sub(" ", dl_match.fold(key.header))
    words = [w for w in _NON_ALNUM_RE.split(folded) if w]
    if SHOP_WORDS & set(words):
        return True
    capitals = {c.lower() for c in re.findall(r"(?<![^\W\d_])[A-Z](?![^\W\d_])", key.header)}
    owned = bool(DOCUMENT_WORDS & set(words))
    for w in words:
        if len(w) == 1 and w.isalpha():
            if w in capitals:
                return True
            continue
        if w in _NOT_A_BLOCK or _WEEK_CODE_RE.match(w) or _YEAR_RE.match(w):
            continue
        if w.isdigit():
            if not owned and len(w) <= MAX_HEADER_NUMBER:
                return True
            continue
        return True
    return False


def is_multi_store(keys: list[StoreKey], shop_evidence: bool = False) -> bool:
    """The mail carries orders of ≥2 different shops (`identity`; header blocks naming nothing
    count as one more) AND the shops are real:
    the model reported a shop header block on some order (even an empty „Predajňa:"), or
    `shop_evidence` (the sender has learned shop rules). File names alone never make shops:
    a customer may send one file per day / per kind of goods, and that must stay one order
    (#81.1) — exactly as before #493."""
    shops = len({k.identity for k in keys if k.identity})
    # orders that name no shop count too, one per distinct (block, file) — under a block of
    # their own („Predajňa:", review round 8), from an attachment that could not be told
    # (round 11: the PR form's line was also in the body) or from a file whose name has no
    # shop words (round 12: „OBJ Pekárová žena.xlsx" next to the Tržnica form; „Predajňa A"
    # / „Predajňa B" counted once); an untold one always counts on its own (round 13: two
    # blank „Predajňa:" forms whose files were untold counted once) — else both shops went to
    # the sender's card
    shops += len({(_squash(k.header), k.source_file or ("untold", i))
                  for i, k in enumerate(keys)
                  if not k.identity and (names_a_block(k) or k.file_unknown or k.source_file)})
    return (shop_evidence or any(names_a_block(k) for k in keys)) and shops >= 2


def from_context(ctx: dict) -> StoreKey | None:
    """Rebuild the identity a store-scoped `customer` question was asked with (its
    `context`) — what its answer teaches a rule for and what the worker sweep matches the
    sender's rules against."""
    toks = frozenset(ctx.get("store_tokens") or (ctx.get("store_key") or "").split())
    if not toks:
        return None
    return StoreKey(header=str(ctx.get("store") or ""),
                    source_file=str(ctx.get("store_file") or ""), tokens=toks)


@dataclass(frozen=True)
class RuleHit:
    """What the sender's learned shop rules say about one order: the card (`ean`), the words
    of the deciding rule(s), or `conflict` — rules that apply and disagree."""
    ean: str = ""
    words: frozenset = frozenset()
    conflict: bool = False


def match_rule(rules, key: StoreKey | None) -> RuleHit | None:
    """The warehouse's learned answer for this shop, or None when no rule applies. `rules` =
    [(key words, ean)] of THIS sender. A rule applies only to EXACTLY the shop it was taught
    for — its words equal the order's whole identity (`tokens`), which does not depend on
    the other orders of the mail (review rounds 2-3: a subset or a mail-relative key captured
    other shops). A re-ask when a shop's header/file gains a word is the safe direction. Two
    live rules of the same words that disagree (a Kôš restore) are a `conflict` — asked
    again, never resolved by recency."""
    if key is None or not key.tokens:
        return None
    hits = {(frozenset(w), ean) for w, ean in rules if w and frozenset(w) == key.tokens}
    if not hits:
        return None
    eans = {ean for _w, ean in hits}
    if len(eans) != 1:
        return RuleHit(words=key.tokens, conflict=True)
    return RuleHit(ean=next(iter(eans)), words=key.tokens)
