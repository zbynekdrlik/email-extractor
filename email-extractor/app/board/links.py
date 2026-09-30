"""#459: the ONE builder for a warehouse-board link that lands on the right nástenka tab.

Every Odoo message / reminder / DL post that carried a bare `/sklad/<k>` or `/sklad-dl/<k>`
link now routes the warehouse straight to the correct unified-nástenka tab via `?next=`
(orders → Otázky objednávky, DL → Otázky sklad), optionally deep-linking one question
(`?q=<id>`, honoured by `tab-questions.js`).

Leaf module (spec §3 "audit.py is a LEAF" doctrine): it imports ONLY `linkutil` — never
`app.board` internals or `app.orders` — so `orders.report` can call it LAZILY with no import
cycle. The key derivation + the human-facing base URL are reused VERBATIM from `linkutil`
(`sklad_key`/`dl_key`/`resolve_secret` + `dashboard_base_url`), NEVER `public_base_url` (that
is the machine address n8n uses over the docker network — the 0.9.10 bug), so the two callers
(this builder and `httpapi`/`linkutil.sklad_url`) can never drift apart on either the key or
the base.

`safe_next` is the open-redirect guard the signed key routes apply to a `?next` value: only
an internal `/nastenka…` path is honoured, everything else falls back to the per-key default.

#473: two HISTORY kinds (`orders_history` → História objednávok, `dl_history` → História
dodacích listov) with a `message_id` seed (`?q=<message_id>`, opened as that mail's detail by
`tab-history.js`). They are the warehouse's password-free link for a mail that needs a human
but has NO board question (review / error / "treba zadať ručne") — the question kinds would
land on a tab where that mail is not listed, and the bare `dashboard_base_url` is the
password-gated admin dashboard (→ /login). A question tab takes a `question_id` seed, a
history tab a `message_id` seed — mixing them is a caller bug (`ValueError`).
"""
from __future__ import annotations

from urllib.parse import quote

from .. import linkutil

ORDERS_TAB = "/nastenka/otazky-objednavky"
DL_TAB = "/nastenka/otazky-sklad"
# #473: the real `board.TABS` slugs (NOT `historia-dodacich-listov` — a slug the board does
# not know hard-404s, the lane-6 `otazky-dl` trap).
ORDERS_HISTORY_TAB = "/nastenka/historia-objednavok"
DL_HISTORY_TAB = "/nastenka/historia-dl"

# kind -> (URL route segment, key-derivation fn, default tab)
_KINDS = {
    "orders": ("sklad", linkutil.sklad_key, ORDERS_TAB),
    "dl": ("sklad-dl", linkutil.dl_key, DL_TAB),
    "orders_history": ("sklad", linkutil.sklad_key, ORDERS_HISTORY_TAB),
    "dl_history": ("sklad-dl", linkutil.dl_key, DL_HISTORY_TAB),
}
_HISTORY_KINDS = frozenset({"orders_history", "dl_history"})


def safe_next(nxt: str | None) -> str | None:
    """The `?next` a signed key route may honour, or `None` to fall back to the default.

    Only an INTERNAL board path is allowed: exactly `/nastenka`, or a `/nastenka/`… /
    `/nastenka?`… path. Everything else — an empty value, a non-board path, a
    protocol-relative `//host`, an absolute `scheme://…` URL, or anything with a backslash —
    is refused. A single leading slash plus the `/nastenka` prefix is always same-origin, so
    this can never become an open redirect."""
    if not nxt or "\\" in nxt or nxt.startswith("//"):
        return None
    if nxt == "/nastenka" or nxt.startswith("/nastenka/") or nxt.startswith("/nastenka?"):
        return nxt
    return None


def board_link(cfg, kind: str, question_id: int | None = None,
               message_id: str | None = None) -> str:
    """The full warehouse link that lands the warehouse on the right nástenka tab.

    `<dashboard_base_url>/sklad/<key>?next=/nastenka/otazky-objednavky` (orders) or
    `…/sklad-dl/<key>?next=/nastenka/otazky-sklad` (dl); with `question_id`, the tab path
    carries `?q=<id>` (URL-encoded so it stays part of the `next` VALUE). #473: the history
    kinds (`orders_history` / `dl_history`) land on the História tab and take a `message_id`
    seed instead — the Message-ID is encoded as the q VALUE first (`<`, `@`, `$`, `+`, `/`
    must survive; a raw `+` would decode to a space) and then, with the rest of the tab path,
    as the `next` value, so it round-trips the signed-key redirect exactly. Returns `""` when
    no human-facing base URL is configured (same fallback as `linkutil.sklad_url`)."""
    try:
        route, key_fn, tab = _KINDS[kind]
    except KeyError:
        raise ValueError(f"neznámy board_link kind {kind!r}") from None
    history = kind in _HISTORY_KINDS
    if (question_id is not None and history) or (message_id and not history):
        raise ValueError(f"board_link kind {kind!r}: question_id patrí k záložke otázok, "
                         "message_id k záložke histórie")
    base = (getattr(cfg, "dashboard_base_url", "") or "").rstrip("/")
    if not base:
        return ""
    key = key_fn(linkutil.resolve_secret(cfg))
    seed = message_id if history else (None if question_id is None else str(question_id))
    nxt = f"{tab}?q={quote(seed, safe='')}" if seed else tab
    return f"{base}/{route}/{key}?next={quote(nxt, safe='/')}"
