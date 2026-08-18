"""Conservative promo / newsletter detection (#342 req 5).

A wholesale flyer or newsletter reliably produces "AI nenašla žiadnu objednávku", so today
it raises a `mail`-kind board question ("je toto vôbec objednávka?") — noise the warehouse
does not need. This decides, HIGH-PRECISION, whether a no-order mail is obviously bulk
marketing so the pipeline can route it straight to `no_processing` instead of asking.

Precision over recall, by design ("konzervatívne: pri pochybnosti otázku položiť"):

- The STRONGEST signal is the mail's own `List-Unsubscribe` bulk header. A transactional
  order mail never carries it, so its mere presence is enough on its own.
- Absent that header, we only call it promo when a BULK SENDER local-part (newsletter@,
  noreply@, marketing@, …) AND a PROMO SUBJECT marker (akcia, leták, zľava, výpredaj,
  novinky, newsletter) BOTH hold — either alone is too weak to risk dropping a real order.

Pure and stateless: no DB, no model. The subject markers are diacritic-folded STEMS
(matched as substrings), verified against their real inflected forms in tests — never a
bare ASCII stem assumed to cover a Slovak word family (the #265/orders-corpus stem-drift
lesson).
"""
from __future__ import annotations

import unicodedata

# Sender local-parts that only ever send bulk mail. Matched as a whole word or a prefix
# (`newsletter`, `newsletter-sk`), never a substring of an unrelated address.
_BULK_LOCALPARTS = (
    "newsletter", "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "mailing", "mailer", "marketing", "news", "notifications", "notification",
    "campaign", "promo", "akcie", "akcia",
)

# Diacritics-folded lowercase STEMS that mark a promotional subject. Substrings, so
# `zlav` covers zľava/zľavy/zľavnené, `akci` covers akcia/akcie/akciový, `letak` covers
# leták/letáky, `vypredaj` covers výpredaj/výpredaji, `novink` covers novinka/novinky.
_PROMO_SUBJECT_STEMS = (
    "newsletter", "akci", "letak", "zlav", "vypredaj", "novink", "unsubscribe",
    "odhlas", "odber noviniek",
)


def _fold(s: str) -> str:
    """Lowercase + strip diacritics so a folded STEM matches its accented Slovak forms."""
    return "".join(c for c in unicodedata.normalize("NFD", str(s or "").lower())
                   if unicodedata.category(c) != "Mn")


def looks_like_promo(subject: str, from_addr: str, body: str = "",
                     list_unsubscribe: str = "") -> bool:
    """True only for an OBVIOUS bulk marketing / newsletter mail (see module docstring)."""
    if str(list_unsubscribe or "").strip():
        return True
    local = str(from_addr or "").split("@", 1)[0].lower()
    bulk_sender = any(local == p or local.startswith(p + "-") or local.startswith(p + "_")
                      or local.startswith(p + ".") for p in _BULK_LOCALPARTS)
    subj = _fold(subject)
    promo_subject = any(stem in subj for stem in _PROMO_SUBJECT_STEMS)
    return bulk_sender and promo_subject
