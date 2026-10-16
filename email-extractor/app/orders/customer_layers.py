"""The CODEX layer of the effective customer list (#493) — pure, no DB.

The order engine's customer list is built in three layers (`snapshot._layered_customers`):

1. the frozen BASE — the pre-CODEX snapshot rows (the old sheet era + edits baked in then);
2. the board's own edits (`customer_overrides`, `snapshot._merge_customers`): a live override
   replaces its base row, a retired one removes it;
3. CODEX (`codex_customers`), applied HERE on top of that merge:
   - a card whose EAN CODEX has gets CODEX's addresses ADDED (a union — never removes one),
     and only BLANK fields filled (name / city / street / zip / trade name): whatever the
     board or the base wrote wins;
   - an address deliberately removed on the board (`customer_overrides.removed_emails`) is
     never put back on that card;
   - an EAN CODEX has and the list has not gets its own card — unless the board retired
     (deleted) that EAN: a deleted card stays deleted.

Applied on every rebuild to the pre-CODEX base (`order_snapshots.customer_base_id`), never
to an already-layered snapshot, so a CODEX change or removal is never baked in for good.
"""
from __future__ import annotations

_FILL = ("name", "city", "street", "zip", "trade_name")


def apply_codex(rows: list[dict], codex: dict[str, dict], overrides: list[dict]) -> list[dict]:
    """`rows` = `snapshot._merge_customers` output (base + live overrides, each with its
    `override_id`/`orig_*` identity), `codex` = {ean: card}, `overrides` = every override row
    (incl. retired ones). Returns the effective list, every row carrying `trade_name`."""
    if not codex:
        return [dict(r, trade_name=r.get("trade_name") or "") for r in rows]
    removed = {o["id"]: {str(e).lower() for e in o.get("removed_emails") or []}
               for o in overrides}
    live_eans = {o["ean_edi"] for o in overrides if not o["retired"]}
    deleted = {e for o in overrides if o["retired"]
               for e in (o.get("ean_edi"), o.get("orig_ean_edi")) if e} - live_eans
    out: list[dict] = []
    present: set[str] = set()
    for r in rows:
        ean = str(r.get("ean_edi") or "")
        present.add(ean)
        card = codex.get(ean) if ean else None
        if card is None:
            out.append(dict(r, trade_name=r.get("trade_name") or ""))
            continue
        emails = list(r.get("emails") or [])
        have = {str(e).lower() for e in emails}
        skip = removed.get(r.get("override_id"), set())
        emails += [e for e in card["emails"] if e not in have and e not in skip]
        filled = {f: r.get(f) or card.get(f) or "" for f in _FILL}
        out.append(dict(r, emails=emails, **filled))
    for ean in sorted(codex):
        if ean in present or ean in deleted:
            continue
        card = codex[ean]
        out.append({"ean_edi": ean, "name": card["name"], "emails": list(card["emails"]),
                    "city": card.get("city") or "", "street": card.get("street") or "",
                    "zip": card.get("zip") or "", "trade_name": card.get("trade_name") or "",
                    "override_id": None, "orig_ean_edi": ean,
                    "orig_street": card.get("street") or "", "codex": True})
    return out
