"""DL worker — supplier/item LLM matching glue (schemas, prompts, inputs)."""
from __future__ import annotations

import logging
from pathlib import Path

from . import dl_match, dl_supplier_memory

log = logging.getLogger("orders.dl_worker")

PROMPTS_DIR = Path(__file__).with_name("prompts")


# --- LLM matching glue (deferred by dl_match.py's own docstring to this phase) ----

SUPPLIER_SCHEMA = {
    "type": "object",
    "properties": {
        "matched": {"type": "boolean"},
        "ean_edi": {"type": "string"},
        "name": {"type": "string"},
        "matchConfidence": {"type": "number"},
        "matchReason": {"type": "string"},
    },
    "required": ["matched"],
}

ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "gtin": {"type": "string"},
        "matchedCatalogName": {"type": "string"},
        "matchConfidence": {"type": "number"},
        "matchReason": {"type": "string"},
        "mass": {"type": "number"},
    },
    "required": ["gtin"],
}


def _supplier_prompt() -> str:
    return (PROMPTS_DIR / "dl_match_supplier.md").read_text(encoding="utf-8")


def _item_prompt() -> str:
    return (PROMPTS_DIR / "dl_match_item.md").read_text(encoding="utf-8")


def _supplier_input(doc: dict, cands: list[dict]) -> str:
    lines = [f"Dodávateľ na dokumente: {doc.get('supplierName', '')} / mesto "
             f"{doc.get('supplierCity', '')} / e-mail {doc.get('supplierEmail', '')}",
             "", "Kandidáti:"]
    for c in cands:
        lines.append(f"- EAN {c.get('ean_edi')}: {c.get('name')} ({c.get('city', '')}, "
                     f"{', '.join(c.get('emails') or [])})")
    return "\n".join(lines)


def _item_input(item: dict, cands: list[dict], partner_name: str) -> str:
    lines = [f"Položka: {item.get('name', '')} ({item.get('quantity')} "
             f"{item.get('unit', '')})",
             f"Dodávateľ tohto dokumentu: {partner_name}", "", "Kandidáti:"]
    for c in cands:
        lines.append(f"- GTIN {c.get('gtin')}: {c.get('name')} "
                     f"(alias: {c.get('doplnok') or '-'})")
    return "\n".join(lines)


def _match_supplier(conn, client, doc: dict, suppliers: list[dict],
                    sender_email: str = "") -> dl_match.SupplierDecision:
    """#240: a HUMAN-taught address (`dl_supplier_memory`, written the moment a
    `dl_supplier` nástenka question is answered) is checked FIRST, before the model is
    ever asked — mirrors AI-orders' own `human_taught` rung sitting above the whole
    `decide_without_model` ladder (`match.py`). Without this, `release_for_question`'s
    reprocess-on-answer would just ask the model again and get the exact same "not
    matched" verdict it got the first time (the model has no way to know a human already
    resolved this address) — the document would never actually finish. A taught EAN that
    no longer exists in the CURRENT supplier snapshot (a retired card) falls through to
    the model exactly as before, the same defensive shape `decide_item`'s own
    `unknown_gtin` guard already uses."""
    if sender_email:
        recalled = dl_supplier_memory.resolve(conn, sender_email)
        if recalled:
            row = next((s for s in suppliers
                       if str(s.get("ean_edi")) == str(recalled["ean_edi"])), None)
            if row:
                log.info("dl supplier memory rescue: %r -> %s (%s)", sender_email,
                         recalled["ean_edi"], recalled.get("name"))
                return dl_match.SupplierDecision(
                    matched=True, ean_edi=row["ean_edi"], name=row.get("name", ""),
                    confidence=0.95,
                    note="Potvrdené naučenou adresou — sklad ju už raz priradil.")
    cands = dl_match.supplier_candidates(doc.get("supplierName", ""),
                                         doc.get("supplierEmail", ""),
                                         doc.get("supplierCity", ""), suppliers)
    answer = client.json_call(_supplier_prompt(), _supplier_input(doc, cands),
                              SUPPLIER_SCHEMA, name="dl_supplier")
    return dl_match.decide_supplier(answer, suppliers)


def _match_item(client, item: dict, catalog: list[dict], recalled,
                partner_name: str) -> dl_match.Decision:
    cands = dl_match.candidates(item.get("name", ""), catalog,
                                memory_gtin=(recalled.gtin if recalled else ""))
    answer = client.json_call(_item_prompt(), _item_input(item, cands, partner_name),
                              ITEM_SCHEMA, name="dl_item")
    return dl_match.decide_item(item.get("name", ""), answer, catalog, recalled=recalled,
                                partner_name=partner_name)
