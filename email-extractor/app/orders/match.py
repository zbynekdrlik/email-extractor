"""The matching ladder (#62): order wording -> catalog card, with an explicit precedence.

In n8n these rules are nested `if`s spread over three Code nodes, so which rule wins is
an accident of statement order and nothing tests it — the direct cause of "improve one
order type, break five others". Here they are an ordered list with declared overrides,
each decision carries the rule that fired and its inputs, and `tests/test_orders_match.py`
pins both the rungs and the pairs whose precedence matters.

The ladder, highest first:

  1 alias_exact_weight  alias IS the customer's wording AND states a weight -> beats weight guard
  2 history_weight      unanimous history, 3+ delivery days                 -> beats weight guard
  3 alias_customer      the card's alias names the ordering customer        -> beats the gate
  4 history             history (below the gate)                            -> beats the gate
  5 llm_sure            model confidence >= 0.85
  6 unique_card         exactly one card of that kind in the catalog        -> beats weight guard, flagged
  7 llm_borderline      model confidence 0.70-0.85                          -> flagged
  8 sibling             same wording resolved elsewhere in the same email    (applied per order)
    unmatched           nothing above fired — the reason is kept and reported
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


def candidates(item_name: str, catalog: list[dict], customer_name: str = "",
               memory_gtin: str = "", limit: int = CANDIDATES) -> list[dict]:
    toks = customer_tokens(customer_name)
    scored = [dict(c, score=_score(item_name, c, toks, memory_gtin)) for c in catalog]
    scored.sort(key=lambda c: -c["score"])
    return scored[:limit]


def _card(catalog: list[dict], gtin) -> dict | None:
    return next((c for c in catalog if str(c.get("gtin")) == str(gtin or "")), None)


def unique_core_card(item_name: str, catalog: list[dict]) -> dict | None:
    """The only card of that kind, ignoring the weight.

    Needs 2+ core tokens: one-word orders ("rožok", "šiška") have several catalog
    variants distinguished precisely by weight, and letting the rule fire there brings
    back the Céder incident.
    """
    want = _core_tokens(item_name)
    if len(want) < 2:
        return None
    hits = []
    for card in catalog:
        have = _core_tokens(card.get("name", ""))
        if len(have) < 2:
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
    alias_parts = [p.strip() for p in re.split(r"[,;/]+", alias) if len(p.strip()) >= 4]
    matched_parts = [p for p in alias_parts if p in _fold(item_name)]
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

    # 5 / 7 — the model, once the weight guard agrees.
    if llm_gtin and conf >= GATE_MIN and not weight_conflict:
        if conf >= GATE_SURE:
            return done("llm_sure", str(llm_gtin), llm_card["name"], conf,
                        llm.get("reason") or "Spárované modelom.")
        return done("llm_borderline", str(llm_gtin), llm_card["name"], conf,
                    f"Prešlo na hranici istoty ({round(conf * 100)} %, pod 85 %), kandidát "
                    f"„{llm_card['name']}“ — prosím prekontrolujte.", review=True)

    # 6 — nothing above held: is there exactly one card of this kind?
    only = unique_core_card(item_name, catalog)
    if only:
        card_w = weight_grams(only["name"])
        detail = ""
        if _weights_disagree(ordered_w, card_w):
            detail = (f"gramáž objednávky {_fmt_weight(ordered_w)} vs karta "
                      f"{_fmt_weight(card_w)} — ")
        return done("unique_card", str(only["gtin"]), only["name"], max(conf, 0.9),
                    f"Prešlo ako jediný produkt toho druhu v katalógu ({detail}"
                    f"„{only['name']}“) — prosím prekontrolujte gramáž.", review=True)

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
    by_gtin: dict[str, Decision] = {}
    for d in decisions:
        if not d.gtin:
            out.append(d)
            continue
        first = by_gtin.get(d.gtin)
        if first is None:
            by_gtin[d.gtin] = d
            out.append(d)
            continue
        first.quantity = (first.quantity or 0) + (d.quantity or 0)
        if d.item_name and d.item_name != first.item_name:
            first.item_name = f"{first.item_name} + {d.item_name}"
        first.trace = {**(first.trace or {}), "merged_with": d.item_name,
                       "merged_rule": d.rule}
    return out
