"""The warehouse-link role/security boundary (#268 krok 3).

Moved VERBATIM out of `app/httpapi.py` lines 80-152 (no behavior change) — see the
design comment on #268 for exactly what moved and why. This is the security-critical
half of what used to live mixed in with the HTTP route bodies: the two unauthenticated
nástenka links (`SKLAD_ROLE`/`SKLAD_DL_ROLE`) and everything that decides what each one
may reach. `httpapi.py`'s own `_gate()` (`before_request`) imports these constants;
`api_orders_questions`/`api_orders_answer`/`api_orders_taught`/`api_orders_undo` import
`_role_kinds` — ONE definition, multiple importers, never a second copy (see the design
comment: a second `_role_kinds` would be exactly the kind of drift this boundary must
never have).
"""
from __future__ import annotations

import re

from flask import session

from .orders import teach as _teach

SKLAD_ROLE = "sklad"
# What the warehouse link may reach — the questions surface, nothing else. It is an
# UNAUTHENTICATED link, so this list is the whole security boundary: never widen it to
# anything that reads mails, files or spend. `/api/orders/held` (#93) is order metadata of
# the same shape as questions/taught (customer name, delivery date, question ids) — no mail
# body, no attachment, no spend — and IS meant to be sklad-visible: the `/otazky` panel
# fetches it so the warehouse sees what it is holding up, review finding on PR #116 (the
# panel silently 401'd and never rendered for the sklad role without this).
SKLAD_PATHS = ("/otazky", "/api/orders/questions", "/api/orders/taught", "/api/orders/held")
SKLAD_ACTION = re.compile(r"^/api/orders/question/\d+/(answer|undo)$")
# #104: the same warehouse link also reaches the knowledge-base page. Same boundary rule as
# SKLAD_PATHS above — wording/gtin/card metadata only, never a mail body or an attachment.
SKLAD_ZNALOSTI_PAGE = re.compile(r"^/znalosti(/[^/]+)?$")
# #235: narrowed to the ORDERS-only knowledge (global/catalog/customers/products/clients) —
# `dl-products`/`dl-suppliers` used to be alternatives here too (since #223's dashboard-
# editing rollout), which meant the orders SKLAD_ROLE already had a real, unintended write
# path into the DL supplier/catalog data — a pre-existing gap #235's own boundary
# requirement ("the orders role must equally not gain DL write access") closes. DL
# knowledge now has its own, separate allowlist below (SKLAD_DL_ZNALOSTI_API).
SKLAD_ZNALOSTI_API = re.compile(
    r"^/api/znalosti/(global(/\d+)?|catalog|customers|customer/[^/]+(/\d+)?"
    r"|products(/[^/]+)?|clients)$")
# #235: the DL nástenka's own API-only reach — deliberately NOT the `/znalosti` PAGE (that
# template also renders orders-domain boxes: catalog/customers/clients search+edit — giving
# SKLAD_DL_ROLE the page would either expose that dead-end UI or, if the API were widened to
# match, be a real widening of her role into the orders agenda). Only the two DL-specific
# endpoints her question card's new-entry form actually calls.
SKLAD_DL_ZNALOSTI_API = re.compile(r"^/api/znalosti/(dl-products(/[^/]+)?|dl-suppliers)$")

# #231: a SECOND, independent unauthenticated link — the delivery-notes-only nástenka.
# `order_questions.kind` is the ONE discriminator between the two agendas
# (`teach.KINDS`): ORDERS_KINDS are every kind the AI-orders pipeline raises, DL_KINDS are
# the two DL ones (#202). `/api/orders/questions`/`/api/orders/taught` are DELIBERATELY
# the SAME shared endpoints both roles use (and the full-admin dashboard, unrestricted) —
# `_role_kinds()` below decides what each role is actually allowed to see/touch, so the
# security boundary never depends on which URL a client happens to call.
ORDERS_KINDS = ("item", "customer", "mail", "date", "line")
DL_KINDS = ("dl_item", "dl_supplier")
SKLAD_DL_ROLE = "sklad_dl"
SKLAD_DL_PATHS = ("/otazky-dl", "/api/orders/questions", "/api/orders/taught",
                  "/api/orders/dl/stats")
# Review finding on the #231 PR: nothing enforced that these two tuples actually
# partition EVERY registered `teach.KINDS` entry. A future kind added to that registry
# but forgotten here would silently NEVER reach either unauthenticated nástenka link
# (fail-safe direction — full admin login still sees it — but nobody would notice why
# the warehouse never gets asked). Fail loudly at import time instead, mirroring
# `teach.py`'s own `KINDS` completeness assertion right after its dict definition.
assert set(ORDERS_KINDS) | set(DL_KINDS) == set(_teach.KINDS), (
    "every teach.KINDS entry must be routed to exactly one of ORDERS_KINDS/DL_KINDS")


def _role_kinds(role: str | None) -> tuple[str, ...] | None:
    """The `kind` values a session's role may see/answer/undo. `None` = unrestricted.

    A real dash_password login (`session["auth"]`) is ALWAYS unrestricted, regardless of
    whatever `role` the SAME session might also carry — `auth` and `role` are independent
    session keys, and a real browser can end up with BOTH set: the admin dashboard's own
    link panel (`showSkladLink()`) renders both nástenka links as clickable
    `target="_blank"` `<a>` tags specifically so the operator can preview/copy them, and
    opening either one in the same cookie jar sets `role` WITHOUT ever clearing `auth`.
    `_gate()` already treats `auth` as the overriding signal (checked first, before
    `role`, in the SAME `before_request` handler) — this function must use the identical
    precedence, or a logged-in admin who merely clicked their own dashboard's link would
    silently start seeing a role-filtered question list and getting 403s on answer/undo
    (review finding on the #231 PR — caught before merge, no live incident)."""
    if session.get("auth"):
        return None
    if role == SKLAD_DL_ROLE:
        return DL_KINDS
    if role == SKLAD_ROLE:
        return ORDERS_KINDS
    return None
