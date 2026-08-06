"""The matching ladder (#62): order wording -> catalog card, with an explicit precedence.

In n8n these rules are nested `if`s spread over three Code nodes, so which rule wins is
an accident of statement order and nothing tests it — the direct cause of "improve one
order type, break five others". Here they are an ordered list with declared overrides,
each decision carries the rule that fired and its inputs, and `tests/test_orders_match.py`
pins both the rungs and the pairs whose precedence matters.

The ladder, highest first. The first three rungs need NO model call at all
(`decide_without_model`, #86) — the model is only paid for from rung 1 down:

  0 catalog_name      the wording IS a catalog card name (unique)
  0 alias_exact       the wording IS one of a card's alias parts (unique)
  0 history_sure      unanimous delivery history, 3+ days, weights agree
  0 global_taught     the WAREHOUSE taught this wording for every customer (#102), below the
                       three rungs above (catalog truth and this customer's own real shipping
                       evidence outrank a generic crowd answer) but still needs no model call
  1 alias_exact_weight  alias IS the customer's wording AND states a weight -> beats weight guard
  2 history_weight      unanimous history, 3+ delivery days                 -> beats weight guard
  3 alias_customer      the card's alias names the ordering customer        -> beats the gate
  4 history             history (below the gate)                            -> beats the gate
  5 llm_sure            model confidence >= 0.85                           -> unless #186 below fires
  5 llm_sure_alias_conflict  #186: rung 3's alias-bias finding blocks a SURE model answer too
  6 unique_card         exactly one card of that kind in the catalog        -> beats weight guard
  7 llm_borderline      model confidence 0.70-0.85                          -> flagged
  8 sibling             same wording resolved elsewhere in the same email    (applied per order)
    unmatched           nothing above fired — the reason is kept and reported

One deliberate exception to that order (#103): when the model is BELOW the sure gate and rung
6 holds — the catalog has exactly one card of that kind, and it is the card the model picked
— rung 6 decides instead of rung 7. A borderline score there is doubt about the gramáž, and
the gramáž is not in dispute when there is no other card to confuse it with. A different
FLAVOUR is not the same product, so `unique_core_card` still refuses it and the line reaches
a human.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field

log = logging.getLogger("orders.match")

# A match below this is the model guessing; between GATE_MIN and GATE_SURE it passes but
# is flagged. Values carried over from the live pipeline (0.85 -> 0.75 -> 0.70 by user
# decision, after the warehouse confirmed the model was right on 3 of 5 disputed items).
GATE_MIN = 0.70
GATE_SURE = 0.85
# The weight guard: ordered weight and card weight must agree within this ratio.
WEIGHT_TOLERANCE = 0.1
# "Chlieb 5 kg" against a 1000 g card is not a mistyped spec, it is the total quantity —
# beyond this ratio the only-card rule must not fire.
UNIQUE_MAX_RATIO = 3
CANDIDATES = 25
# #160: the score `candidates()` already computes per card is what tells a genuinely
# related alternative (a name/alias substring hit, or a known SYNONYMS family match —
# both land >= 60 on real catalog data) apart from a coincidental single shared word
# (a generic style adjective like "kváskový" shared by every sourdough-style product
# regardless of category — lands ~15). 50 sits with headroom below the genuine-match
# range and above the coincidental one; below it a card is noise, not an alternative.
PLAUSIBLE_CANDIDATE_SCORE = 50.0
CUSTOMER_STOPWORDS = {"as", "sro", "spol", "ltd", "pobocka", "prevadzka", "sklad", "stores"}
SYNONYMS = [
    (("strudla", "strudlia"), ("zavin",)),
    (("slimak",), ("uzol",)),
    (("croisant", "croissant", "krosant"), ("croissant",)),
]


@dataclass
class Decision:
    item_name: str
    gtin: str | None
    card: str
    confidence: float
    rule: str
    note: str
    review: bool = False
    trace: dict = field(default_factory=dict)
    # carried through from the extracted item so one object describes the whole line
    quantity: float | None = None
    unit: str = "ks"


# --- text helpers --------------------------------------------------------

def _fold(text: str) -> str:
    s = unicodedata.normalize("NFD", str(text or "").lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def _norm(text: str) -> str:
    """Fold and strip weights — what is left is the product's wording."""
    s = re.sub(r"\d+[.,]?\d*\s*(kg|gr|g|ml|l)\b", " ", _fold(text))
    return re.sub(r"[^a-z\s]", " ", s).strip()


def weight_grams(name: str) -> float | None:
    """Weight stated in a name, in grams. Multipacks ("6x1l") are not a unit weight."""
    s = str(name or "")
    if re.search(r"\d+\s*[x×*]\s*[\d.,]+\s*(kg|g|l|ml)\b", s, re.I):
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(kg|g|gr)\b", s, re.I)
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    return value * 1000 if m.group(2).lower() == "kg" else value


def _fmt_weight(grams: float) -> str:
    return f"{grams / 1000:g} kg" if grams >= 1000 else f"{grams:g} g"


def _weights_disagree(ordered: float | None, card: float | None) -> bool:
    if not (ordered and card):
        return False
    ratio = ordered / card
    return ratio > 1 + WEIGHT_TOLERANCE or ratio < 1 - WEIGHT_TOLERANCE


def customer_tokens(name: str) -> list[str]:
    return [w for w in re.sub(r"[^a-z0-9\s]", " ", _fold(name)).split()
            if len(w) >= 4 and w not in CUSTOMER_STOPWORDS]


def _core_tokens(name: str) -> list[str]:
    s = re.sub(r"\d+(?:[.,]\d+)?\s*(kg|gr|g|ml|l)\b", " ", _fold(name))
    return [w for w in re.sub(r"[^a-z0-9]+", " ", s).split() if len(w) >= 3]


# --- candidate scoring ---------------------------------------------------

def _score(item_name: str, card: dict, customer_toks: list[str], memory_gtin: str) -> float:
    if memory_gtin and str(card.get("gtin")) == str(memory_gtin):
        # A card confirmed by history must never fall out of the list — the model can
        # only choose from what it is shown.
        return 100.0
    item, name = _norm(item_name), _norm(card.get("name", ""))
    alias = _norm(card.get("alias", ""))
    if alias and len(alias) > 3 and alias in _fold(item_name):
        return 99.0
    if item and item == name:
        return 98.0

    score = 0.0
    if name and name in item:
        score = 70.0
    if item and item in name:
        score = max(score, 65.0)

    iw = [w for w in item.split() if len(w) > 2]
    for other, weight in ((name, 60.0), (alias, 70.0)):
        ow = [w for w in other.split() if len(w) > 2]
        if iw and ow:
            hits = sum(1 for w in iw if any(w in o or o in w for o in ow))
            score = max(score, hits / len(iw) * weight)

    for words, targets in SYNONYMS:
        if any(w in item for w in words) and any(t in name for t in targets):
            score = max(score, 75.0)

    # Bonus, not a floor: a card whose alias names THIS customer stays in the list, but
    # only when some wording already matched — otherwise the customer's cards would be
    # pushed in front of completely unrelated items.
    if score > 0 and alias and any(t in alias for t in customer_toks):
        score += 10.0
    return score


def alias_parts(card: dict) -> list[str]:
    """The alias column is a comma/semicolon list of customer wordings for this card."""
    return [p.strip() for p in re.split(r"[,;/]+", _fold(card.get("alias", "") or ""))
            if len(p.strip()) >= 4]


# Generic product-CATEGORY words: common to dozens of catalog cards, so they discriminate
# nothing between them. "chlieb" is common to every bread card in the catalog; the CÉDER
# incident (#157) needed exactly this excluded, since both the ordered wording AND the
# wrongly-confirmed card's own name contained it.
GENERIC_PRODUCT_WORDS = {"chlieb", "chleba", "chlebom", "chlebu"}


def _distinctive_words(name: str) -> set[str]:
    """The words that actually tell one product apart from another: folded (diacritics/
    case-insensitive), weights stripped (`_norm`), short and generic-category words
    dropped. Used to decide whether a customer's wording and a catalog card's name are
    genuinely talking about the SAME product, not just loosely resembling each other."""
    return {w for w in _norm(name).split() if len(w) >= 4 and w not in GENERIC_PRODUCT_WORDS}


def _better_alias_candidate(item_name: str, llm_card: dict | None,
                            catalog: list[dict]) -> dict | None:
    """A catalog card whose OWN name matches the customer's distinctive wording better
    than the model's pick does — the sign that an alias 'names_customer' note is about
    to confirm the WRONG card (#157).

    A card's alias note ("objednava ... CÉDER") is proof that customer buys THAT card
    for SOME wording — never proof that THIS particular line is it. "Chlieb olivovo
    paradajkový" shares its distinctive words ('olivovo', 'paradajkový') with card 253's
    own name and shares NONE with card 192's, even though card 192 carries the note; the
    note therefore confirms nothing here. The identical mechanism must keep confirming
    card 284 ("Vianočka 400g") for the same customer on the same day, because there the
    wording and the card's OWN name genuinely agree and no other card scores higher.

    Returns None (nothing overrides the model's pick) both in the ordinary case — no
    other card matches better — and when the wording carries no distinctive word at all
    (too little signal in the wording to compare anything against)."""
    item_words = _distinctive_words(item_name)
    if not item_words:
        return None
    llm_hits = len(item_words & _distinctive_words((llm_card or {}).get("name", "")))
    best_card, best_hits = None, llm_hits
    for card in catalog:
        if llm_card is not None and str(card.get("gtin")) == str(llm_card.get("gtin")):
            continue
        hits = len(item_words & _distinctive_words(card.get("name", "")))
        if hits > best_hits:
            best_card, best_hits = card, hits
    return best_card


# Slovak wordings are heavily inflected — a customer's short noun form ("oliva",
# "tekvička") and the catalog card's adjective form ("olivovo", "tekvicový") are the
# SAME product but share no exact token. Proven against the real 35-case eval corpus
# (#195): exact set-equality between distinctive words produced 2 false positives
# there (both genuine matches, both blocked). A shared prefix of this length is
# treated as the same stem — long enough that the two real incidents this ticket
# documents ("olivovo"/"paradajkový" vs "tradičny"/"kváskový"/"pšenično"/"ražný",
# "multicereálny" vs the same) still share none, short enough to absorb ordinary
# Slovak inflection.
STEM_PREFIX = 4


def _lexical_overlap(item_words: set[str], card_words: set[str]) -> set[str]:
    """Item words that share a stem (`STEM_PREFIX`-length prefix) with some card word —
    used by the #195 lexical-gap guard, never by `_better_alias_candidate` above (that
    mechanism is pre-existing, #157/#186, and already corpus-validated as exact-match)."""
    return {w for w in item_words if any(w[:STEM_PREFIX] == c[:STEM_PREFIX] for c in card_words)}


def _card_reference_words(card: dict, customer_name: str) -> set[str]:
    """The words describing the ARTICLE printed on this card: its own name, plus any
    alias phrase that describes the GOODS — never a phrase that merely names the
    ordering customer (#195 point 3). An alias like "objednava ... CÉDER" is proof the
    customer buys this card for SOME wording, never proof of this one — crediting it
    toward a lexical-overlap check would defeat the point of the check."""
    words = set(_distinctive_words(card.get("name", "")))
    cust_toks = customer_tokens(customer_name)
    for part in alias_parts(card):
        if cust_toks and any(t in part for t in cust_toks):
            continue
        words |= _distinctive_words(part)
    return words


def _wordings_differ(a: str, b: str) -> bool:
    """True when two customer wordings share NO distinctive word at all — the sign that
    two DIFFERENT products both resolved to the same card (#157: "Chlieb olivovo
    paradajkový" and "Chlieb pšenično ražný" share nothing), not that the same product
    was simply written twice. Either wording carrying no distinctive word at all (too
    short/generic to compare) is treated as agreeing — there is nothing to disagree
    ABOUT, and the ordinary "same wording repeated" case must keep merging."""
    wa, wb = _distinctive_words(a), _distinctive_words(b)
    if not wa or not wb:
        return False
    return not (wa & wb)


def decide_without_model(item_name: str, catalog: list[dict], recalled=None,
                         global_recalled=None) -> Decision | None:
    """The rungs that need no model call — or None when the line genuinely needs one (#86).

    89 % of the engine's model spend is one call per ordered line, and it was made
    unconditionally: the model was asked even when the wording IS a catalog card. Measured on
    the 30-email corpus (2026-07-31), 115 of 473 lines are answerable here, and in **115 of
    115** the answer was the same card the model-driven ladder shipped. So this is not a
    cheaper guess — it is the same answer, unpaid for.

    Certainty is the bar, not likeness: a unique exact hit, or an unanimous history of 3+
    delivery days (the ladder's own bar), and the stated weights must agree. Anything short
    of that returns None and the full ladder runs.

    `global_recalled` (#102, `memory.resolve_global`) is what the warehouse taught for THIS
    wording across every customer — checked LAST among the no-model rungs, i.e. only after
    this customer's own taught mapping (`recalled.human`, checked first, above this whole
    function's other rungs) and after the catalog-certain / this-customer's-own-history rungs
    have all had their turn. Like a taught mapping, it overrides the weight guard: it IS a
    human decision, just one that applies more broadly.
    """
    ordered_w = weight_grams(item_name)
    want = _fold(item_name)

    def _ok(card_name: str) -> bool:
        return not _weights_disagree(ordered_w, weight_grams(card_name))

    def _done(rule: str, gtin, card: str, note: str) -> Decision:
        return Decision(item_name=item_name, gtin=str(gtin), card=card, confidence=1.0,
                        rule=rule, note=note, review=False,
                        trace={"rule": rule, "llm": None, "free": True})

    # The warehouse ANSWERED this wording (#88). That outranks everything, including the
    # weight guard: the guard stops the model guessing, it does not overrule a human.
    if recalled is not None and getattr(recalled, "human", False):
        return _done("human_taught", recalled.gtin, recalled.card,
                     f"Priradené skladom — „{item_name}“ je „{recalled.card}“.")
    by_name = [c for c in catalog if _fold(c.get("name", "")) == want]
    if len(by_name) == 1 and _ok(by_name[0].get("name", "")):
        return _done("catalog_name", by_name[0]["gtin"], by_name[0]["name"],
                     "Znenie objednávky je presne názov karty v katalógu.")

    by_alias = [c for c in catalog if want in alias_parts(c)]
    if len(by_alias) == 1 and _ok(by_alias[0].get("name", "")):
        return _done("alias_exact", by_alias[0]["gtin"], by_alias[0]["name"],
                     f"Znenie objednávky je presne alias karty („{item_name}“) — "
                     "ľudské priradenie zo skladu.")

    # The history bar is the ladder's own: unanimous AND 3+ distinct delivery days.
    if recalled and recalled.unanimous and recalled.strength >= 3 and _ok(recalled.card):
        return _done("history_sure", recalled.gtin, recalled.card,
                     f"Potvrdené jednohlasnou históriou dodávok ({recalled.note}) — "
                     f"tomuto zákazníkovi sme pre „{item_name}“ dodávali vždy tú istú kartu.")

    # The warehouse taught this wording for EVERY customer (#102) — the weakest of the
    # no-model rungs, tried only once nothing customer-specific or catalog-certain fired.
    if global_recalled is not None:
        return _done("global_taught", global_recalled.gtin, global_recalled.card,
                     f"Naučené skladom pre všetkých zákazníkov — „{item_name}“ je "
                     f"„{global_recalled.card}“.")
    return None


def candidates(item_name: str, catalog: list[dict], customer_name: str = "",
               memory_gtin: str = "", limit: int = CANDIDATES) -> list[dict]:
    toks = customer_tokens(customer_name)
    scored = [dict(c, score=_score(item_name, c, toks, memory_gtin)) for c in catalog]
    scored.sort(key=lambda c: -c["score"])
    return scored[:limit]


def proposed_gtin(decision) -> str:
    """The gtin the engine itself proposed for THIS decision, even when it was rejected.

    `decision.gtin` already carries it for `llm_borderline`/`history_weight` (#147's ladder
    rungs that ask the warehouse but still keep a gtin). For `unmatched` (rejected on
    confidence or weight) `decision.gtin` is None, but the model's raw answer is still in
    `decision.trace["llm"]["gtin"]` — that raw value is what the question's own rejection
    note names ("kandidát „...“"), so it must be the same card offered as a button.
    """
    if decision.gtin:
        return str(decision.gtin)
    llm = (decision.trace or {}).get("llm") or {}
    return str(llm.get("gtin") or "")


def candidates_for_question(item_cands: list[dict], catalog: list[dict],
                            decision) -> list[dict]:
    """What the warehouse is shown for a question — the engine's own proposed candidate
    ALWAYS first, then the nearest other candidates of the same match (#147).

    `item_cands` is scored and truncated BEFORE the model call (it is the model's INPUT,
    built from wording+catalog alone), so it has no way to know which card the model will
    actually name — a scoring quirk (e.g. the SYNONYMS rule in `_score()` boosting an
    unrelated card family) can silently rank the model's own answer below the cutoff.
    Re-heading the list with the actual decision, computed AFTER the model call, guarantees
    the one card the warehouse needs to confirm is always clickable.
    """
    gtin = proposed_gtin(decision)
    if not gtin:
        return item_cands
    card = _card(catalog, gtin)
    if not card:
        return item_cands
    rest = [c for c in item_cands if str(c.get("gtin")) != gtin]
    return [card] + rest


def plausible_candidates(ask_cands: list[dict], limit: int = 6) -> list[dict]:
    """What the warehouse is actually SHOWN as buttons for a question (#160): the
    engine's own proposed candidate (`ask_cands[0]`, always re-headed there by
    `candidates_for_question` — always kept so the warehouse can confirm it, regardless
    of its own score) plus only the OTHER candidates whose `score` clears a real
    relevance floor (`PLAUSIBLE_CANDIDATE_SCORE`) — never padded to `limit` with
    whatever ranked next.

    The incident this fixes: "Kváskový slimák s pizzovou plnkou" has no catalog card.
    The old `ask_cands[:6]` slice padded the shortlist out to six with
    `Chlieb tradičny kváskový pšenično-ražný 700gr` — a 700 g bread loaf that shares
    only the generic style word "kváskový" with the order (`_score()` == 15, the same
    a genuinely unrelated card gets from one incidental word) — sitting right next to
    the model's own honest low-confidence guess. That turned "this product does not
    exist" into "pick one of these six", and the warehouse picked the wrong card.

    When nothing beyond the proposed candidate clears the bar, the list stays short —
    often just the one card. That is not a dead end: every item question on the real
    warehouse page also carries a full-catalog search box (#149) and a "databáza
    znalostí" link to add a genuinely missing card by hand — the existing, already-
    unconditional escape, never bypassed by a shorter list.
    """
    if not ask_cands:
        return []
    head, rest = ask_cands[0], ask_cands[1:]
    plausible = [c for c in rest
                if float(c.get("score", 0) or 0) >= PLAUSIBLE_CANDIDATE_SCORE]
    return [head] + plausible[:max(0, limit - 1)]


def _card(catalog: list[dict], gtin) -> dict | None:
    return next((c for c in catalog if str(c.get("gtin")) == str(gtin or "")), None)


def _unique_note(card: dict, ordered_w) -> str:
    """Why the only-card rule fired, and — when it differs — the gramáž it overrode.

    The line no longer stops for a human (#103), so this note is the only place the
    difference is recorded: it is what the Odoo report and the dashboard show.
    """
    card_w = weight_grams(card.get("name", ""))
    if _weights_disagree(ordered_w, card_w):
        return (f"Jediný produkt toho druhu v katalógu — objednané "
                f"{_fmt_weight(ordered_w)}, dodáva sa {_fmt_weight(card_w)} "
                f"(„{card['name']}“).")
    return f"Jediný produkt toho druhu v katalógu („{card['name']}“)."


def unique_core_card(item_name: str, catalog: list[dict]) -> dict | None:
    """The only card of that kind, ignoring the weight.

    User decision 2026-08-02 (#140): what decides is the number of REAL candidates in
    the catalog, not the number of words in the wording — "ak napisu babovka a my mame
    iba jednu babovku... to je jedno ze zakaznik nenapisal gramaz". A one-word order
    ("babovka", "slimák") whose catalog has exactly one matching card must not be asked
    about. "rožok"/"šiška" still have several catalog variants distinguished only by
    weight, and must still ask — that safety now comes from the candidate COUNT below
    (2+ hits -> None), not from a minimum word count. Letting it fire unconditionally on
    a single word is exactly the Céder incident (2026-07-24: "Rožok 70g" shipped as
    štandart 50g) — `tests/test_orders_match.py`'s
    `test_a_single_word_order_with_several_catalog_candidates_still_asks` pins that this
    cannot recur.

    Known, accepted tradeoff (code review on #140): dropping the card-side floor to >= 1
    token means a single-core-token card can now be reached by the SUPERSET-containment
    branch below too (`all(t in want for t in have)`), so a wording with an extra
    unrecognised qualifier word can still resolve to it — the same tradeoff
    `app.orders.static_ean.catalog_match` already accepts for its own matcher. See
    `tests/test_orders_match.py`'s
    `test_KNOWN_TRADEOFF_a_single_core_token_card_can_absorb_an_extra_unmatched_qualifier`.
    """
    want = _core_tokens(item_name)
    if not want:
        return None
    hits = []
    for card in catalog:
        have = _core_tokens(card.get("name", ""))
        if not have:
            continue
        if all(t in have for t in want) or all(t in want for t in have):
            hits.append(card)
    if len(hits) != 1:
        return None
    ordered, card_w = weight_grams(item_name), weight_grams(hits[0].get("name", ""))
    if ordered and card_w:
        ratio = ordered / card_w
        if ratio > UNIQUE_MAX_RATIO or ratio < 1 / UNIQUE_MAX_RATIO:
            return None       # a total quantity, not a mistyped spec
    return hits[0]


# --- the ladder ----------------------------------------------------------

def decide(item_name: str, llm: dict, catalog: list[dict], recalled=None,
           customer_name: str = "") -> Decision:
    conf = float(llm.get("confidence") or 0)
    if conf > 1:
        conf = conf / 100
    llm_gtin = llm.get("gtin") or None
    llm_card = _card(catalog, llm_gtin)
    # A code that resolves to no card in the catalog is not an answer — it used to be
    # dereferenced and killed the whole run with 'NoneType' is not subscriptable. The rungs
    # that do not need the model (history, unique card) still get their turn below.
    unknown_gtin = bool(llm_gtin) and llm_card is None
    if unknown_gtin:
        llm_gtin = ""
    ordered_w = weight_grams(item_name)

    cust_toks = customer_tokens(customer_name)
    alias = _fold((llm_card or {}).get("alias", ""))
    matched_parts = [p for p in alias_parts(llm_card or {}) if p in _fold(item_name)]
    alias_exact_weight = any(re.search(r"\d+(?:[.,]\d+)?\s*(kg|g|gr)\b", p, re.I)
                             for p in matched_parts)
    alias_names_customer = bool(alias) and any(t in alias for t in cust_toks)
    # #186: computed ONCE, used by BOTH the alias_customer rung (3) below AND the SURE
    # model rung (5) further down — an alias note that merely names the customer proves
    # the customer buys llm_card for SOME wording, never that THIS wording is it; a
    # DIFFERENT card whose own name fits the wording better means the note (and the
    # model's own confidence, which the same prompt instruction biases) confirms nothing,
    # at ANY confidence level (2026-08-06, CÉDER run 241: 0.96/0.97 confidence, still
    # wrong — #157's original fix only gated rung 3, never rung 5).
    alias_better = (_better_alias_candidate(item_name, llm_card, catalog)
                    if alias_names_customer else None)

    trace = {"llm": {"gtin": llm.get("gtin"), "confidence": llm.get("confidence"),
                     "unknown_gtin": unknown_gtin},
             "alias": {"exact_parts": matched_parts, "names_customer": alias_names_customer,
                      "overridden_by": alias_better.get("name") if alias_better else None},
             "history": None if not recalled else {
                 "gtin": recalled.gtin, "days": recalled.strength,
                 "unanimous": recalled.unanimous, "weight_override": recalled.weight_override},
             "weight": {"ordered": ordered_w,
                        "card": weight_grams((llm_card or {}).get("name", ""))}}

    def done(rule, gtin, card, confidence, note, review=False) -> Decision:
        trace["rule"] = rule
        return Decision(item_name=item_name, gtin=gtin, card=card, confidence=confidence,
                        rule=rule, note=note, review=review, trace=dict(trace))

    # 1 — the warehouse wrote the customer's exact wording, weight included, into the
    #     alias. That is a deliberate human mapping and beats the weight guard.
    if llm_gtin and alias_exact_weight:
        return done("alias_exact_weight", str(llm_gtin), llm_card["name"], max(conf, 0.95),
                    f"Alias karty je presné znenie objednávky („{matched_parts[0]}“).")

    # 2 — an unanimous history of 3+ delivery days may ship a different gramáž.
    if recalled and recalled.weight_override:
        card_w = weight_grams((llm_card or {}).get("name", ""))
        target_w = weight_grams(recalled.card)
        if _weights_disagree(ordered_w, card_w) or _weights_disagree(ordered_w, target_w):
            return done("history_weight", recalled.gtin, recalled.card, max(conf, 0.95),
                        f"Odoslané podľa histórie dodávok ({recalled.note}) — gramáž "
                        f"objednávky {_fmt_weight(ordered_w)} nesúhlasí s kartou.",
                        review=True)

    weight_conflict = _weights_disagree(
        ordered_w, weight_grams((llm_card or {}).get("name", "")))

    # 3 — the card's alias names the ordering customer: it IS that customer's card.
    if llm_gtin and conf > 0 and alias_names_customer and not weight_conflict:
        # #157: the note only proves the customer buys llm_card for SOME wording — if
        # another card's own name matches THIS wording better, the note confirms
        # nothing; fall through to the ordinary ladder below (history / unique-card /
        # the model's own raw confidence), exactly as if there were no alias help.
        if not alias_better:
            return done("alias_customer", str(llm_gtin), llm_card["name"], max(conf, 0.95),
                        f"Potvrdené aliasom karty — alias menuje zákazníka "
                        f"„{customer_name}“ (pôvodná istota modelu "
                        f"{round(conf * 100)} %).")

    # 4 — history below the gate (including when the model matched nothing at all).
    if recalled and (conf < GATE_SURE or not llm_gtin):
        if not _weights_disagree(ordered_w, weight_grams(recalled.card)):
            return done("history", recalled.gtin, recalled.card, 0.95,
                        f"Potvrdené históriou dodávok — tomuto zákazníkovi sme pre "
                        f"„{item_name}“ dodávali „{recalled.card}“ ({recalled.note}).")

    # 6a — the model hesitated, but there is nothing to hesitate BETWEEN.
    # User decision 2026-08-01 (#103): a product we make in exactly one gramáž is decided,
    # not asked. A borderline score on such a card is doubt about the weight, and the weight
    # is not in dispute — 'Croissant pistácia 120g' went out flagged four times in one order
    # against our only pistachio croissant. This runs BEFORE the model rung so the borderline
    # branch never gets the chance to flag it.
    only = unique_core_card(item_name, catalog)
    if only and conf < GATE_SURE and (not llm_gtin or str(llm_gtin) == str(only["gtin"])):
        return done("unique_card", str(only["gtin"]), only["name"], max(conf, 0.9),
                    _unique_note(only, ordered_w))

    # 5 / 7 — the model, once the weight guard agrees.
    if llm_gtin and conf >= GATE_MIN and not weight_conflict:
        if conf >= GATE_SURE:
            # #186: rung 3 already found a better-fitting card for this wording and fell
            # through (see `alias_better` above) — a SURE raw model confidence must not
            # ship the same alias-biased answer either. The gtin is cleared (like
            # `unmatched`) rather than kept: the model's proposed candidate stays visible
            # to the warehouse via `trace["llm"]["gtin"]` (`proposed_gtin()`), but nothing
            # here claims it is correct.
            if alias_better:
                return done(
                    "llm_sure_alias_conflict", None, "", conf,
                    f"Model si je istý ({round(conf * 100)} %), ale alias karty len "
                    f"menuje zákazníka „{customer_name}“ — karta "
                    f"„{alias_better['name']}“ sedí so znením lepšie (pôvodný kandidát "
                    f"modelu „{llm_card['name']}“) — prosím prekontrolujte.",
                    review=True)
            # #195: a class-level, cause-independent tripwire — regardless of WHY the
            # model is confident, a wording sharing literally no distinctive content
            # word with the card it names (its own name, or a non-customer-naming
            # alias, #195 point 3) must not auto-ship. Both real incidents this
            # ticket cites are alias-caused and already blocked by #186 above; this
            # is the safety net for every OTHER cause a future confident-but-wrong
            # answer could have.
            item_words = _distinctive_words(item_name)
            card_words = _card_reference_words(llm_card, customer_name)
            overlap = _lexical_overlap(item_words, card_words)
            lexical_gap = bool(item_words and card_words and not overlap)
            trace["lexical_guard"] = {"item_words": sorted(item_words),
                                      "card_words": sorted(card_words),
                                      "overlap": sorted(overlap), "fired": lexical_gap}
            if lexical_gap:
                return done(
                    "llm_sure_lexical_gap", None, "", conf,
                    f"Model si je istý ({round(conf * 100)} %), ale znenie "
                    f"„{item_name}“ nemá s kartou „{llm_card['name']}“ spoločné "
                    "žiadne rozlišujúce slovo — prosím prekontrolujte.", review=True)
            return done("llm_sure", str(llm_gtin), llm_card["name"], conf,
                        llm.get("reason") or "Spárované modelom.")
        return done("llm_borderline", str(llm_gtin), llm_card["name"], conf,
                    f"Prešlo na hranici istoty ({round(conf * 100)} %, pod 85 %), kandidát "
                    f"„{llm_card['name']}“ — prosím prekontrolujte.", review=True)

    # 6b — nothing above held: is there exactly one card of this kind?
    if only:
        return done("unique_card", str(only["gtin"]), only["name"], max(conf, 0.9),
                    _unique_note(only, ordered_w))

    if weight_conflict:
        card_w = weight_grams(llm_card["name"])
        return done("unmatched", None, "", conf,
                    f"Zamietnuté — gramáž objednávky {_fmt_weight(ordered_w)} vs karta "
                    f"{_fmt_weight(card_w)} (kandidát „{llm_card['name']}“).")
    return done("unmatched", None, "", conf,
                f"Zamietnuté — istota {round(conf * 100)} % je pod hranicou 70 %"
                + (f" (kandidát „{llm_card['name']}“)." if llm_card else "."))


def apply_siblings(decisions: list[Decision]) -> list[Decision]:
    """Rung 8: the same wording resolved elsewhere in the SAME email decides the rest.

    2026-07-30 (CDR Lipová 6, ČSB): identical wording and identical card scored 0.88
    (accepted) and 0.84/0.83 (rejected) in one email, and since an order needs all its
    items, the whole document fell over. This is not a discount on the gate — the card
    already passed both the gate and the weight guard on another line; it removes a coin
    flip on identical input.
    """
    from .memory import item_key

    best: dict[str, Decision] = {}
    for d in decisions:
        if not d.gtin:
            continue
        key = item_key(d.item_name)
        if key and (key not in best or d.confidence > best[key].confidence):
            best[key] = d

    out = []
    for d in decisions:
        twin = best.get(item_key(d.item_name))
        if d.gtin or not twin:
            out.append(d)
            continue
        note = (f"Prešlo podľa zhodnej položky v tom istom maile — „{d.item_name}“ bola "
                f"na inom riadku spárovaná s kartou „{twin.card}“ "
                f"(istota {round(twin.confidence * 100)} %).")
        trace = dict(d.trace, rule="sibling", sibling={"gtin": twin.gtin,
                                                       "confidence": twin.confidence})
        out.append(Decision(item_name=d.item_name, gtin=twin.gtin, card=twin.card,
                            confidence=twin.confidence, rule="sibling",
                            note=note + " " + d.note, review=True, trace=trace,
                            quantity=d.quantity, unit=d.unit))
        log.info("sibling rescue: %r -> %s", d.item_name, twin.gtin)
    return out


def merge_same_card(decisions: list[Decision]) -> list[Decision]:
    """One card is ONE order line: repeated cards add up — but only when the customer's
    OWN wording agrees they are the same product.

    Two recipient groups, or two wordings that resolve to the same card, used to produce two
    LIN lines for one GTIN — a double order line in ORION and, when a reader keeps only one
    of them, a lost quantity (#81.1). Unmatched items are left alone: they are reported by
    name, and different unmatched wordings are different problems.

    #157 (CÉDER, 2026-08-03): a same-gtin match is not always a genuine duplicate — it can
    be the RESULT of two independently wrong decisions over two different wordings (three
    different breads all mis-resolved to one card, then summed into one line of 5 pieces,
    silently dropping the other two products). A gtin collision between MATERIALLY
    different wordings (`_wordings_differ`, #157) is treated as a signal, not a duplicate:
    those lines stay separate, each keeping its own quantity, instead of being silently
    collapsed into one. A wording repeated verbatim (or near enough — sharing at least one
    distinctive word) still merges exactly as before.
    """
    out: list[Decision] = []
    # Several INDEPENDENT accumulator buckets can exist per (gtin, unit): one per distinct
    # wording group that has appeared so far. A new decision joins the first bucket whose
    # wording does not materially differ from it; if none matches, it starts its own.
    buckets: dict[tuple[str, str], list[Decision]] = {}
    for d in decisions:
        if not d.gtin:
            out.append(d)
            continue
        # The UNIT is part of the identity: adding 2 kg to 3 ks would ship "5" of something
        # ambiguous. Only lines that agree on the unit may be added up.
        key = (d.gtin, (d.unit or "").strip().lower())
        group = buckets.setdefault(key, [])
        first = next((g for g in group if not _wordings_differ(g.item_name, d.item_name)),
                    None)
        if first is None:
            group.append(d)
            out.append(d)
            continue
        first.quantity = (first.quantity or 0) + (d.quantity or 0)
        if d.item_name and d.item_name != first.item_name:
            first.item_name = f"{first.item_name} + {d.item_name}"
        first.trace = {**(first.trace or {}), "merged_with": d.item_name,
                       "merged_rule": d.rule}
    return out
