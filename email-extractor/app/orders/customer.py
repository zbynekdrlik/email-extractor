"""Customer matching (#62, customer half).

An unmatched customer stops the whole document — there is nowhere to address the order —
so the authority order matters:

1. the model, when it is SURE (>= 0.85) and names an EAN that exists in the snapshot —
   one address may legitimately order for a different branch than it is registered to;
2. an address written in the customer table that belongs to exactly ONE customer — a
   hand-written address is the warehouse stating whose it is (the 30.07.2026 PNO Martin
   order fell over because the model, seeing only a public gmail address, refused to
   guess at 0.08 while the table knew the answer all along);
3. otherwise nothing — a wrongly addressed order is worse than one sent for review.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger("orders.customer")

GATE_SURE = 0.85
CANDIDATES = 10
# Free providers: a domain match on them would attach an order to a random customer.
GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
    "yahoo.com", "yahoo.sk", "centrum.sk", "azet.sk", "post.sk", "pobox.sk", "zoznam.sk",
    "email.cz", "seznam.cz", "centrum.cz", "icloud.com", "me.com", "mac.com",
    "protonmail.com", "proton.me", "aol.com",
}
_EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}")


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


def resolve(customers: list[dict], sender_email: str, sender_name: str,
            company_name: str, llm: dict | None = None,
            store: str = "") -> Matched | None:
    """Decide who ordered, or None when it cannot be decided safely.

    `store` is the block header of the order's own half of a multi-shop attachment; it is
    the only thing that separates two branches registered under one e-mail address.
    """
    llm = llm or {}
    conf = float(llm.get("confidence") or 0)
    if conf > 1:
        conf = conf / 100
    by_ean = {str(c.get("ean_edi")): c for c in customers if c.get("ean_edi")}
    picked = by_ean.get(str(llm.get("ean_edi") or ""))

    if picked and conf >= GATE_SURE:
        return Matched(ean_edi=str(picked["ean_edi"]), name=picked.get("name", ""),
                       confidence=conf, rule="llm",
                       note=f"Spárované modelom (istota {round(conf * 100)} %).")

    addr = (_addresses(sender_email) or [""])[0]
    if addr:
        owners = [c for c in customers if addr in _addresses(c.get("emails"))]
        if len(owners) == 1:
            owner = owners[0]
            log.info("customer by exact address %s -> %s (model gave %s at %.2f)",
                     addr, owner.get("name"), llm.get("ean_edi"), conf)
            return Matched(
                ean_edi=str(owner.get("ean_edi") or ""), name=owner.get("name", ""),
                confidence=0.99, rule="exact_email",
                note=(f"E-mail odosielateľa {addr} je v tabuľke zákazníkov zapísaný "
                      f"práve u „{owner.get('name', '')}“ a u nikoho iného."))
        if len(owners) > 1:
            branch = _by_store(owners, store)
            if branch:
                log.info("address %s is shared by %d customers; the block header %r picks "
                         "%s", addr, len(owners), store, branch.get("name"))
                return Matched(
                    ean_edi=str(branch.get("ean_edi") or ""), name=branch.get("name", ""),
                    confidence=0.99, rule="store_address",
                    note=(f"E-mail {addr} patrí viacerým predajniam; táto časť súboru je "
                          f"nadpísaná „{store}“, čo sedí na adresu "
                          f"„{branch.get('street', '')}“."))
            log.info("address %s belongs to %d customers — not guessing (store hint %r)",
                     addr, len(owners), store)

    if picked:
        log.info("customer match refused: %s at %.2f (below %.2f)",
                 picked.get("name"), conf, GATE_SURE)
    return None
