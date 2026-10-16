"""Which SHOP of a multi-shop customer one order is for — the order's store key (#493).

A customer with several shops sends ONE mail with one attachment per shop (the 2026-10-16
incident: five xlsx files, headers „BISTRO", „Predajňa: Tržnica", „ŽILINA", „ŠKOLA, …" and an
empty „Predajňa:" whose only identifier is the file name „… PR.xlsx"). The extraction gives
each order its block header (`store`, #101) and — since #493 — the attachment it was read
from (`sourceFile`, located here from the item quotes). This module turns those two into a
normalized, deterministic key the customer resolver can match against card names / cities
(`customer._by_store_name`) and the warehouse's learned shop rules (`match_rule`).

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

STOPWORDS = frozenset({
    # order / document words
    "obj", "objed", "objednavka", "objednavky", "objednavku", "objednavok", "objednavkovy",
    "order", "zoznam", "tabulka", "harok", "sheet", "list",
    # generic shop words — „Predajňa: Tržnica" is the Tržnica shop
    "predajna", "predajne", "predajni", "predajnu", "prevadzka", "prevadzky", "prevadzku",
    "pobocka", "pobocky", "filialka",
    # our own brand: the supplier, never a customer's shop
    "pekarova", "pekarovej", "pekarovu", "zena", "zeny", "slovnormal",
    # week / day words — change every week, never identify a shop
    "tyzden", "tyzdna", "tyzdni", "tyzdne", "tyz", "tyzd", "week", "kw",
    "pondelok", "utorok", "streda", "stvrtok", "piatok", "sobota", "nedela",
    # month names in the NOMINATIVE only („október" in a file name); the genitive forms stay
    # — they are street names („29. augusta", „1. mája")
    "januar", "februar", "marec", "april", "maj", "jun", "jul", "august", "september",
    "oktober", "november", "december",
    # legal forms / connectors
    "sro", "spol", "as", "na", "pre", "do", "od", "a", "s", "r", "o", "v", "z", "k",
})


def _squash(text: str) -> str:
    return _SPACES_RE.sub(" ", dl_match.fold(text)).strip()


def tokens(text, keep_numbers: bool = False) -> list[str]:
    """Normalized words of a header / file name / card text, in order, de-duplicated:
    folded, split on every non-alphanumeric, without stopwords, week codes („kw42") and —
    unless `keep_numbers` (a shop HEADER: „Predajňa 1" / „Predajňa 2" are two shops) — pure
    numbers. Short words stay (the „PR" / „ZA" shop codes); the name rung itself only
    compares words of `STEM`+ letters."""
    out: list[str] = []
    for t in re.split(r"[^a-z0-9]+", dl_match.fold(text)):
        if t.isdigit() and keep_numbers and t not in out:
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

def attachment_sections(source: str) -> list[tuple[str, str]]:
    """The `===== <file name> =====` sections `process._combined_text` appends after
    „Attachments:" — [(file name, that section's folded text)], in order."""
    text = str(source or "")
    marks = list(_SECTION_RE.finditer(text))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append((m.group(1).strip(), _squash(text[m.end():end])))
    return out


def locate_file(texts, source: str = "", sections=None) -> str:
    """The attachment the given item texts (quotes, else names) were read from — the section
    containing the most of them, only when that section is a STRICT winner; "" otherwise
    (no sections, no hit, a tie). Best effort and never guessed: an order whose file stays
    unknown keeps only its block header as the store key. Pass `sections` (computed ONCE per
    mail with `attachment_sections`) when locating several orders of one mail."""
    if sections is None:
        sections = attachment_sections(source)
    if not sections:
        return ""
    needles = [n for n in (_squash(t) for t in texts or ()) if len(n) >= 3]
    if not needles:
        return ""
    scored = sorted(((sum(1 for n in needles if n in body), name) for name, body in sections),
                    key=lambda s: -s[0])
    best = scored[0]
    if best[0] == 0 or (len(scored) > 1 and scored[1][0] == best[0]):
        return ""
    return best[1]


# --- the key ----------------------------------------------------------------------------

@dataclass(frozen=True)
class StoreKey:
    """One order's shop identity. `tokens` = every normalized word of its header + file
    name; `key` = the minimal distinguishing words (mail-common words removed) — what a
    learned rule stores and what the name rung compares; `header_key` = the header's own
    words, which is what decides whether two orders are DIFFERENT shops (the poistka): a file
    name alone never does — a one-shop customer may send one file per day."""
    header: str = ""
    source_file: str = ""
    tokens: frozenset = frozenset()
    key: frozenset = frozenset()
    header_key: frozenset = frozenset()

    @property
    def text(self) -> str:
        """The stored form of `key` (sorted, space-joined)."""
        return " ".join(sorted(self.key))

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
        """Which shop this order says it is: every word of its header (numbers included) and
        of its file — independent of the other orders of the mail. Empty = it names no shop.
        Two files of one shop for two days („… pondelok", „… streda") are one identity (day
        words are stopwords); two „ŽILINA" files of different names are two."""
        return self.tokens


def for_orders(orders: list[dict]) -> list[StoreKey]:
    """The store key of every order of ONE mail, in order. Words shared by every distinct
    token set of the mail are dropped from `key` — unless that would leave an order with no
    key at all (its full tokens are kept then)."""
    raw = []
    for o in orders:
        header = str(o.get("store") or "").strip()
        src = str(o.get("sourceFile") or "").strip()
        head_t = tokens(header, keep_numbers=True)
        raw.append((header, src, frozenset(head_t + tokens(file_stem(src))),
                    frozenset(head_t)))
    distinct = {t for _h, _s, t, _hk in raw if t}
    common = frozenset.intersection(*distinct) if len(distinct) >= 2 else frozenset()
    out = []
    for header, src, toks, head_key in raw:
        key = (toks - common) or toks
        out.append(StoreKey(header=header, source_file=src, tokens=toks, key=key,
                            header_key=head_key))
    return out


def is_multi_store(keys: list[StoreKey], shop_sender: bool = False) -> bool:
    """The mail carries orders of ≥2 different shops (`identity`) AND the shops are real:
    the model reported a shop header block on some order (even an empty „Predajňa:"), or the
    sender is a known multi-shop sender (`shop_sender`: its address is on several cards, or
    the warehouse already taught it shop rules) — then its FILES are shops even with no
    header. A one-card sender's file names alone never make shops: it may send one file per
    day, which must stay one order (#81.1)."""
    return ((shop_sender or any(k.header.strip() for k in keys))
            and len({k.identity for k in keys if k.identity}) >= 2)


def from_context(ctx: dict) -> StoreKey | None:
    """Rebuild the key a store-scoped `customer` question was asked with (its `context`)."""
    toks = frozenset(ctx.get("store_tokens") or ())
    key = frozenset((ctx.get("store_key") or "").split())
    if not key:
        return None
    return StoreKey(header=str(ctx.get("store") or ""),
                    source_file=str(ctx.get("store_file") or ""),
                    tokens=toks or key, key=key,
                    header_key=frozenset(tokens(ctx.get("store") or "", keep_numbers=True)))


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
    for: its words equal the order's key (`key`, mail-common words removed) or all of its
    words (`tokens`) — never a subset (review round 2: a narrow {dolina} captured „BISTRO
    Dolina", the other shop; a re-ask is safe, a capture is the incident). Two live rules of
    the same words that disagree (a Kôš restore) are a `conflict` — asked again, never
    resolved by recency."""
    if key is None or not key.tokens:
        return None
    hits = {(frozenset(w), ean) for w, ean in rules
            if w and frozenset(w) in (key.key, key.tokens)}
    if not hits:
        return None
    eans = {ean for _w, ean in hits}
    words = frozenset().union(*(w for w, _e in hits))
    if len(eans) != 1:
        return RuleHit(words=words, conflict=True)
    return RuleHit(ean=next(iter(eans)), words=words)
