"""Customer matching (#62, customer half).

An unmatched customer stops the whole document — there is nowhere to address the order —
so the authority order matters:

0. a multi-site FAMILY (cards of one company with several branches — shared sender e-mail
   OR a shared distinctive name stem, #435) is decided by the DELIVERY ADDRESS in the mail
   BEFORE any model pick is trusted: a confident model pick of the central card must never
   silently win when the text names another site; no unambiguous address -> a board
   question. A sender that uniquely owns exactly ONE card skips this (it is not ambiguous);
1. the model, when it is SURE (>= 0.85) and names an EAN that exists in the snapshot —
   one address may legitimately order for a different branch than it is registered to;
2. an address written in the customer table that belongs to exactly ONE customer — a
   hand-written address is the warehouse stating whose it is (the 30.07.2026 PNO Martin
   order fell over because the model, seeing only a public gmail address, refused to
   guess at 0.08 while the table knew the answer all along);
3. otherwise nothing — a wrongly addressed order is worse than one sent for review.

#493 — a customer with SEVERAL SHOPS ordering in one mail (one attachment per shop). Above
everything: a shop rule the warehouse taught for EXACTLY this shop (sender + shop key → card,
`store_rule`). Within a family the shop is decided by the street in its header (#101,
`store_address`), then by the shop key against the card's name / trade name / city / street
(`store_name`, a UNIQUE hit only); the mail-level delivery text (#418) is NOT used when the
mail carries several shops — it cannot tell them apart. `collapsed_shops` is the poistka: in
a mail of several shops only a shop rung may decide a shop — anything the model pick, the
sender address or the delivery text decided is asked instead (the 2026-10-16 incident: 21
files of 5 shops shipped to one card).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from . import dl_match, store_key

log = logging.getLogger("orders.customer")

GATE_SURE = 0.85
# #493: the rules that looked at the SHOP the order is for — the poistka never undoes these.
STORE_RULES = frozenset({"store_rule", "store_address", "store_name"})
CANDIDATES = 10
# Free providers: a domain match on them would attach an order to a random customer.
GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
    "yahoo.com", "yahoo.sk", "centrum.sk", "azet.sk", "post.sk", "pobox.sk", "zoznam.sk",
    "email.cz", "seznam.cz", "centrum.cz", "icloud.com", "me.com", "mac.com",
    "protonmail.com", "proton.me", "aol.com",
}
_EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}")

# #435: a customer "family" is a set of cards belonging to one company with several
# operating sites (Košík.sk Košice/Žilina/Zvolen). The e-mail-only definition (`owners`,
# shared sender address) missed the incident, where the real sender `supply@kosik.sk` is on
# NO card at all — so the family is ALSO found by a shared, DISTINCTIVE name stem. The stem
# must be distinctive so two genuinely separate orgs that merely share a generic
# institutional prefix ("Centrum pre deti a rodiny <miesto>") are NOT welded into one family
# and forced into a needless question — hence the generic-word drop below.
_GENERIC_NAME_WORDS = frozenset({
    # legal forms / connectors
    "s", "r", "o", "sro", "a", "as", "spol", "spolocnost", "druzstvo", "co", "ltd",
    "n", "no", "k",
    # institution / descriptor types
    "centrum", "pre", "deti", "rodiny", "rodina", "nemocnica", "poliklinika",
    "materska", "skola", "skolka", "zakladna", "stredna",
    "cafe", "kaviaren", "restauracia", "pizzeria", "hotel", "penzion",
    "obchod", "obchodik", "potraviny", "market", "supermarket", "hypermarket",
    "velkoobchod", "maloobchod", "online", "store", "shop", "predajna", "prevadzka",
    "mesto", "obec", "ulica", "namestie", "nam",
})


def _name_stem(name) -> str:
    """The distinctive family stem of a customer name (#435): fold diacritics via
    `dl_match.fold` — the ONE folding helper in the package, never re-derived here (the
    #265 Slovak-stem lesson) — then the FIRST token that is neither a pure number nor a
    generic institution/legal-form word. Groups the branches of one brand ("Košík.sk MAKRO
    store Žilina/Zvolen" + "Košík.sk Online Supermarket … Košice" all -> "kosik") while
    keeping two separate orgs that merely share a generic prefix apart ("Centrum pre deti a
    rodiny Kolinovce/Lipová" -> "kolinovce"/"lipova"). Empty when the name carries no
    distinctive token — an empty stem NEVER groups (see `_stem_family`)."""
    for t in re.sub(r"[^a-z0-9]+", " ", dl_match.fold(name)).split():
        if t not in _GENERIC_NAME_WORDS and not t.isdigit():
            return t
    return ""


# Public alias: the board Zákazníci tab groups cards into families by the SAME stem the
# resolver uses (#446) — reuse this ONE definition, never re-derive the folding/stop-word
# logic elsewhere (#265). A pure re-export: no behaviour change, corpus-neutral.
name_stem = _name_stem


def _stem_family(customers: list[dict], anchor: dict) -> list[dict]:
    """Cards sharing `anchor`'s distinctive name stem (#435). Empty stem -> no family."""
    key = _name_stem(anchor.get("name"))
    if not key:
        return [anchor]
    return [c for c in customers if _name_stem(c.get("name")) == key]


@dataclass
class Matched:
    ean_edi: str
    name: str
    confidence: float
    rule: str
    note: str


def _addresses(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        joined = " ".join(str(v) for v in value)
    else:
        joined = str(value or "")
    return _EMAIL_RE.findall(joined.lower())


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[\s,.\-]+", str(text or "").lower()) if len(w) > 2]


def _score(cust: dict, sender_addr: str, sender_domain: str, sender_name: str,
           company: str) -> tuple[float, bool]:
    emails = _addresses(cust.get("emails"))
    exact = bool(sender_addr) and sender_addr in emails
    score = 0.0
    if exact:
        score += 100.0
    elif sender_domain and sender_domain not in GENERIC_DOMAINS:
        if any(e.split("@")[-1] == sender_domain for e in emails):
            score += 80.0

    name = str(cust.get("name") or "").lower()
    if company:
        if company in name or name in company:
            score += 60.0
        else:
            cw, nw = _words(company), _words(name)
            hits = [w for w in cw if any(w in n or n in w for n in nw)]
            if hits:
                score += 15.0 + len(hits) / max(len(cw), 1) * 35.0
    if sender_name:
        sw, nw = _words(sender_name), _words(name)
        hits = [w for w in sw if any(w in n or n in w for n in nw)]
        if hits:
            score += 5.0 + len(hits) / max(len(sw), 1) * 15.0
    return score, exact


def candidates(customers: list[dict], sender_email: str, sender_name: str,
               company_name: str, limit: int = CANDIDATES) -> list[dict]:
    """The shortlist shown to the model, best first, exact address matches marked."""
    addr = (_addresses(sender_email) or [""])[0]
    domain = addr.split("@")[-1] if "@" in addr else ""
    company = str(company_name or "").lower().strip()
    scored = []
    for c in customers:
        score, exact = _score(c, addr, domain, str(sender_name or "").lower(), company)
        scored.append(dict(c, score=score, exact_email=exact))
    scored.sort(key=lambda c: -c["score"])
    return scored[:limit]


def _by_delivery_address(owners: list[dict], delivery_text: str) -> dict | None:
    """Which of the branches sharing one address does the delivery text name? (#418)

    When two+ customer cards share a sender email and _by_store() cannot decide (no
    multi-shop block header), try to match the delivery city/street from the raw email
    text against each card's `city`/`street` fields. Exactly one hit -> that card;
    zero or multiple -> None (falls through to the customer question).

    Matching is case-insensitive substring, same approach candidates_for_question()
    already uses successfully for ranking.
    """
    text = _norm_text(delivery_text)
    if not text:
        return None
    scored = []
    for c in owners:
        city = _norm_text(c.get("city", ""))
        street = _norm_text(c.get("street", ""))
        city_hit = bool(city and len(city) > 2 and re.search(r"(?<!\w)" + re.escape(city) + r"(?!\w)", text))
        street_hit = bool(street and len(street) > 3 and street in text)
        if city_hit or street_hit:
            scored.append((c, city_hit, street_hit))
    if len(scored) == 1:
        winner = scored[0][0]
        parts = [scored[0][0].get("city", ""), scored[0][0].get("street", "")]
        addr = ", ".join(p for p in parts if p)
        log.info("address shared by %d customers; delivery text matches %s (%s, %s)",
                 len(owners), winner.get("ean_edi"), winner.get("name"), addr)
        return winner
    # Multiple city hits -- can the street break the tie?
    if len(scored) > 1:
        street_hits = [s for s in scored if s[2]]
        if len(street_hits) == 1:
            winner = street_hits[0][0]
            log.info("address shared by %d customers; multiple city matches but street "
                     "breaks tie -> %s (%s, %s)", len(owners), winner.get("ean_edi"),
                     winner.get("name"), winner.get("street"))
            return winner
    return None


def _by_store(owners: list[dict], store: str) -> dict | None:
    """Which of the branches sharing one address does this block header name? (#101)

    Matched on the STREET, never the name: the live table calls both Gazdovský trh rows
    "GT1", so the name proves nothing, while the header over each half of the file
    ("GT2- 29 augusta 19 BB") carries the street verbatim. Only a UNIQUE hit counts —
    a header naming both branches, or neither, decides nothing.
    """
    want = _words(store)
    if not want:
        return None
    hits = []
    for c in owners:
        street = _words(c.get("street", ""))
        if street and all(w in want for w in street):
            hits.append(c)
    return hits[0] if len(hits) == 1 else None


def _card_words(c: dict) -> set[str]:
    """The shop-identifying stems of a card: its name, CODEX trade name, city and street."""
    return store_key.stems(store_key.tokens(" ".join(
        str(c.get(f) or "") for f in ("name", "trade_name", "city", "street"))))


def _by_store_name(family: list[dict], key: store_key.StoreKey | None) -> dict | None:
    """Which card of the family does this order's SHOP KEY name? (#493)

    The incident's headers carry no street („BISTRO", „Predajňa: Tržnica", „ŽILINA") and one
    carries nothing at all („Predajňa:" — the shop is only in the file name „… PR.xlsx"), so
    #101's street match decides nothing. The key's 4-letter stems are compared with each
    card's name + trade name + city + street; a stem every card of the family shares is no
    signal and is dropped. Exactly ONE card hit → that card; none or several (two shops in
    one city: „ŽILINA") → None, and the order is asked — never a guess.

    Both views of the shop must name that SAME one card (review round 5): `key` (without the
    words every shop of the mail shares) and `own` (all its words) — and that card must carry
    EVERY distinguishing word of `own` (review round 6: one hit word is no proof — „Kaufland
    Martin" hit only Kaufland Žilina when no family card says Martin, a new „BISTRO Trnava"
    hit the known BISTRO Dolina). Anything less is asked, never shipped to a namesake."""
    if key is None or len(family) < 2:
        return None
    words = [_card_words(c) for c in family]
    common: set[str] = set(words[0]).intersection(*words[1:])
    own = key.own_stems() - common
    narrow = _one_card_hit(words, key.stems() - common, key.text, family)
    whole = _one_card_hit(words, own, key.text, family)
    if narrow is not None and whole is not None and narrow == whole and own <= words[whole]:
        return family[whole]
    if narrow is not None or whole is not None:
        log.info("shop key %s: no card carries all its words in both views (%s / %s, own "
                 "words %s) — asking", key.text, narrow is not None and
                 family[narrow].get("ean_edi"), whole is not None and
                 family[whole].get("ean_edi"), sorted(own))
    return None


def _one_card_hit(words: list, want: set[str], label: str, family: list[dict]) -> int | None:
    """The index of the ONE family card any of the `want` stems hits, or None (no stem, no
    hit, several)."""
    if not want:
        return None
    hits = [i for i, w in enumerate(words) if want & w]
    if len(hits) == 1:
        return hits[0]
    if hits:
        log.info("shop key %s hits %d cards of the family (%s) — not unique", label,
                 len(hits), [family[i].get("ean_edi") for i in hits])
    return None


def _by_shop_rule(hit: store_key.RuleHit, key: store_key.StoreKey, by_ean: dict,
                  addr: str) -> tuple[bool, Matched | None]:
    """The learned-rule rung (#493) -> (decides, result): (True, Matched); (True, None) = ASK
    — two rules of the same shop disagree; (False, None) = the rule's card is gone, fall
    through to the other rungs. A rule matches only its exact shop (`store_key.match_rule`),
    so it can never capture another one."""
    if hit.conflict:
        log.warning("shop %s of %s: learned rules disagree — asking", key.text, addr)
        return True, None
    taught = by_ean.get(hit.ean)
    if taught is None:
        log.info("shop rule %s -> %s of %s names no current card — ignored", key.text,
                 hit.ean, addr)
        return False, None
    log.info("shop %s of %s -> %s (%s) by the warehouse's shop rule", key.text, addr,
             taught.get("ean_edi"), taught.get("name"))
    return True, Matched(
        ean_edi=str(taught.get("ean_edi") or ""), name=taught.get("name", ""),
        confidence=0.99, rule="store_rule",
        note=(f"Predajňu {key.label} sklad už raz priradil ku karte "
              f"„{taught.get('name', '')}“."))


def collapsed_shops(keys: list, matched: list, multi: bool, has_rules: bool = False
                    ) -> set[int]:
    """The poistka (#493): indexes of the orders that must be ASKED instead of shipped.

    A shop is shipped ONLY when a SHOP rung decided it (`STORE_RULES`: a learned shop rule,
    the street, the shop name) whenever it is a shop question at all: in a mail of several
    shops (`multi`) EVERY order decided by a rule that never looked at the shop (`llm`,
    `exact_email`, `delivery_address`) is asked — also a lone new shop on its own card and
    an order that names no shop; for a sender with learned shop rules (`has_rules`) the same
    holds for any order that names a shop (owner decision 2: an unknown shop is asked once)
    or whose attachment could not be told (`file_unknown`, review round 6: its shop is
    unknown, the sender's address must not decide it). Converges: one question per shop,
    then its rule."""
    out = {i for i, m in enumerate(matched)
           if m is not None and m.rule not in STORE_RULES
           and (multi or (has_rules and (keys[i].identity or keys[i].file_unknown)))}
    if out:
        log.warning("poistka: %d order(s) were decided by a rule that ignores the shop (%s) "
                    "although %s — held and asked per shop", len(out),
                    sorted({(keys[i].label, matched[i].rule) for i in out}),
                    "the mail carries several shops" if multi
                    else "the sender has learned shop rules")
    return out


def resolve(customers: list[dict], sender_email: str, sender_name: str,
            company_name: str, llm: dict | None = None,
            store: str = "", delivery_text: str = "",
            key: store_key.StoreKey | None = None, rules=(),
            multi_store: bool = False, use_names: bool = False) -> Matched | None:
    """Decide who ordered, or None when it cannot be decided safely.

    `store` is the block header of the order's own half of a multi-shop attachment; it is
    the only thing that separates two branches registered under one e-mail address.

    `delivery_text` (#418) is the raw email text (subject + body) used to match a delivery
    city/street against customer cards when two+ cards share one sender address and the
    store header cannot decide.

    #493: `key` is the order's shop key (header + attachment, `store_key.for_orders`),
    `rules` the warehouse's learned shop rules of THIS sender ([(words, ean)]), and
    `multi_store` says the mail carries several different shops — then the mail-level
    delivery text is never used to pick one shop's card. `use_names` enables the shop-name
    rung — only in a multi-shop mail (and the worker sweep of a shop question); any other
    mail keeps exactly the pre-#493 rungs (review rounds 3-4).
    """
    llm = llm or {}
    store = store or (key.header if key is not None else "")
    conf = float(llm.get("confidence") or 0)
    if conf > 1:
        conf = conf / 100
    by_ean = {str(c.get("ean_edi")): c for c in customers if c.get("ean_edi")}
    picked = by_ean.get(str(llm.get("ean_edi") or ""))

    addr = (_addresses(sender_email) or [""])[0]
    owners = [c for c in customers if addr and addr in _addresses(c.get("emails"))]

    # #493: a shop the warehouse already answered for THIS sender wins over everything — it
    # is a human decision about exactly this (sender, shop), written only by a board answer.
    hit = store_key.match_rule(rules, key) if rules else None
    if hit is not None and key is not None:
        applies, decided = _by_shop_rule(hit, key, by_ean, addr)
        if applies:
            return decided

    # #435: within a multi-site FAMILY the delivery address decides ABOVE the llm pick — a
    # confident model pick of the central card must never silently win when the mail names
    # another site (the Košík.sk incident). Two ways a family applies:
    #   * the sender e-mail is shared by >1 card (`owners`), or
    #   * the sender is on NO card, yet the model confidently picked a card whose
    #     distinctive name stem is shared by >1 card (the real sender `supply@kosik.sk` is
    #     on none of the Košík cards).
    # A sender that resolves to exactly ONE card (`owners == 1`) is deliberately excluded
    # from the stem path — the unique address already disambiguates, and the confident-llm
    # / exact_email hierarchy below keeps deciding it unchanged.
    family = None
    if len(owners) > 1:
        family = owners
    elif not owners and picked and conf >= GATE_SURE:
        stem_family = _stem_family(customers, picked)
        if len(stem_family) > 1:
            family = stem_family

    if family:
        branch = _by_store(family, store)
        if branch:
            # The EAN, not just the name: the live table calls both Gazdovský trh rows
            # "GT1", so a name-only log line reads as if the wrong branch was picked.
            log.info("family of %d cards; the block header %r picks %s (%s, %s)",
                     len(family), store, branch.get("ean_edi"), branch.get("name"),
                     branch.get("street"))
            return Matched(
                ean_edi=str(branch.get("ean_edi") or ""), name=branch.get("name", ""),
                confidence=0.99, rule="store_address",
                note=(f"Odosielateľ má viac prevádzkových kariet; táto časť súboru je "
                      f"nadpísaná „{store}“, čo sedí na adresu „{branch.get('street', '')}“."))
        named = _by_store_name(family, key) if use_names else None
        if named and key is not None:
            log.info("family of %d cards; the shop key %r picks %s (%s) by name/city",
                     len(family), key.text if key else "", named.get("ean_edi"),
                     named.get("name"))
            return Matched(
                ean_edi=str(named.get("ean_edi") or ""), name=named.get("name", ""),
                confidence=0.95, rule="store_name",
                note=(f"Odosielateľ má viac prevádzkových kariet; predajňa {key.label} sedí "
                      f"na názov/mesto karty „{named.get('name', '')}“."))
        # #493: several shops in one mail — the mail-level delivery text cannot say which
        # of them it describes, so it never picks a card for one of them
        site = None if multi_store else _by_delivery_address(family, delivery_text)
        if site:
            parts = [site.get("city", ""), site.get("street", "")]
            addr_note = ", ".join(p for p in parts if p)
            log.info("family of %d cards; delivery text picks %s (%s), model gave %s at %.2f",
                     len(family), site.get("ean_edi"), addr_note, llm.get("ean_edi"), conf)
            return Matched(
                ean_edi=str(site.get("ean_edi") or ""), name=site.get("name", ""),
                confidence=0.95, rule="delivery_address",
                note=(f"Odosielateľ má viac prevádzkových kariet („rodina“); adresa "
                      f"doručenia v maile sedí na „{addr_note}“."))
        # A multi-site family the delivery address cannot pin -> ASK on the board, never the
        # model's central pick (#435: "bez jednoznačnej zhody -> customer otázka").
        log.info("family of %d cards but the delivery address decides nothing (model gave "
                 "%s at %.2f, store hint %r) — asking", len(family), llm.get("ean_edi"),
                 conf, store)
        return None

    if picked and conf >= GATE_SURE:
        return Matched(ean_edi=str(picked["ean_edi"]), name=picked.get("name", ""),
                       confidence=conf, rule="llm",
                       note=f"Spárované modelom (istota {round(conf * 100)} %).")

    if len(owners) == 1:
        owner = owners[0]
        log.info("customer by exact address %s -> %s (model gave %s at %.2f)",
                 addr, owner.get("name"), llm.get("ean_edi"), conf)
        return Matched(
            ean_edi=str(owner.get("ean_edi") or ""), name=owner.get("name", ""),
            confidence=0.99, rule="exact_email",
            note=(f"E-mail odosielateľa {addr} je v tabuľke zákazníkov zapísaný "
                  f"práve u „{owner.get('name', '')}“ a u nikoho iného."))

    if picked:
        log.info("customer match refused: %s at %.2f (below %.2f)",
                 picked.get("name"), conf, GATE_SURE)
    return None


# --- #159: ranking candidates for the WAREHOUSE's "who is this customer?" question ----
#
# `candidates_for_question` is DELIBERATELY SEPARATE from `candidates()` above.
# `candidates()` feeds `_customer_input()`'s MODEL prompt — reordering or rescoring it
# would change the exact text sent to the model and invalidate the corpus's recorded
# `llm-cache` (never allowed). This function's output only ever reaches a human's screen.

_PSC_LINE_RE = re.compile(r"\b\d{3}\s?\d{2}\b")


def _norm_text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def guess_delivery_address(free_text: str) -> str:
    """Best-effort delivery-address LINE straight from the e-mail's own raw text (#159) —
    never from the model, so it can never touch `prompt_hash`. Slovak order signatures
    routinely put the delivery address on its own line together with a PSČ (postal code);
    when one is found, that whole line is shown to the warehouse next to the ranked
    candidates as a plain reading aid — never used to auto-match, only to let a human
    eyeball it against the candidate below. Empty when no such line exists."""
    for line in str(free_text or "").splitlines():
        line = line.strip()
        if line and _PSC_LINE_RE.search(line):
            return line[:120]
    return ""


def candidates_for_question(customers: list[dict], sender_email: str, sender_name: str,
                            company_name: str, free_text: str = "",
                            limit: int = 5,
                            key: store_key.StoreKey | None = None) -> list[dict]:
    """Ranked customer candidates for the WAREHOUSE's "who is this?" question (#159).

    Adds a signal `candidates()` does not have: does this customer's OWN street or city
    text appear anywhere in the e-mail's own free text (subject + body)? The 2026-08-03
    incident showed this ranks the right customer FIRST even when e-mail/name signals are
    weak or absent — "Na bráne 4, 010 01 Žilina" in the mail matched EAN 2000000000861's
    street "na bráne 4" and city "Žilina" on the nose, yet street/city was never used as a
    matching key at all before this. Never an auto-match key — an unmatched sender still
    ALWAYS gets asked (#62's existing GATE_SURE design already applies to the customer
    boundary); this only orders the candidate LIST a human is shown.

    #493: a question asked for ONE SHOP (`key`) ranks the cards whose name/city share a
    stem with the shop key first, and always lists every card of the sender's family.
    """
    addr = (_addresses(sender_email) or [""])[0]
    domain = addr.split("@")[-1] if "@" in addr else ""
    company = str(company_name or "").lower().strip()
    text_norm = _norm_text(free_text)
    scored = []
    for c in customers:
        score, exact = _score(c, addr, domain, str(sender_name or "").lower(), company)
        street = _norm_text(c.get("street", ""))
        city = _norm_text(c.get("city", ""))
        address_match = False
        if text_norm:
            if street and len(street) > 3 and street in text_norm:
                score += 70.0
                address_match = True
            if city and len(city) > 2 and city in text_norm:
                score += 20.0
                address_match = True
        if key is not None and key.stems() & _card_words(c):
            score += 40.0
        scored.append(dict(c, score=score, exact_email=exact, address_match=address_match))
    scored.sort(key=lambda c: -c["score"])
    if key is not None:
        limit = max(limit, sum(1 for c in scored if c["exact_email"]) + 2)
    return scored[:limit]
