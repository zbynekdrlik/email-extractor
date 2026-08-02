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
  5 llm_sure            model confidence >= 0.85
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
    return str((decision.trace or {}).get("llm", {}).get("gtin") or "") or ""


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

    trace = {"llm": {"gtin": llm.get("gtin"), "confidence": llm.get("confidence"),
                     "unknown_gtin": unknown_gtin},
             "alias": {"exact_parts": matched_parts, "names_customer": alias_names_customer},
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
        return done("alias_customer", str(llm_gtin), llm_card["name"], max(conf, 0.95),
                    f"Potvrdené aliasom karty — alias menuje zákazníka „{customer_name}“ "
                    f"(pôvodná istota modelu {round(conf * 100)} %).")

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
    """One card is ONE order line: repeated cards add up.

    Two recipient groups, or two wordings that resolve to the same card, used to produce two
    LIN lines for one GTIN — a double order line in ORION and, when a reader keeps only one
    of them, a lost quantity (#81.1). Unmatched items are left alone: they are reported by
    name, and different unmatched wordings are different problems.
    """
    out: list[Decision] = []
    by_gtin: dict[tuple[str, str], Decision] = {}
    for d in decisions:
        if not d.gtin:
            out.append(d)
            continue
        # The UNIT is part of the identity: adding 2 kg to 3 ks would ship "5" of something
        # ambiguous. Only lines that agree on the unit may be added up.
        key = (d.gtin, (d.unit or "").strip().lower())
        first = by_gtin.get(key)
        if first is None:
            by_gtin[key] = d
            out.append(d)
            continue
        first.quantity = (first.quantity or 0) + (d.quantity or 0)
        if d.item_name and d.item_name != first.item_name:
            first.item_name = f"{first.item_name} + {d.item_name}"
        first.trace = {**(first.trace or {}), "merged_with": d.item_name,
                       "merged_rule": d.rule}
    return out
