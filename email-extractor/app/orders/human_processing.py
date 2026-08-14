"""#308: no message may sit SILENTLY in the terminal `human_processing` pit.

The n8n "Email Sorting" classifier owns `messages.category`; the app only PROCESSES by
category (dl_worker=`dodacie_listy`, static_worker=`static_orders`, worker=`ai_orders`).
`human_processing` is a TERMINAL category with NO processor — a message the classifier
could not place (e.g. a scan whose OCR came back near-empty, `needs_vision=true`) lands
there `processed=false` and nothing ever touches it again: no notification, no board
question, the DL engine never sees it (live incident 7178 / DL 26041774).

This sweep closes that pit in two layers (see the #308 design comment):

- **Layer 1 — vision-assisted rescue.** For the near-empty-scan signature
  (`needs_vision AND has_attachments`), a vision classification (the standard
  `llm.Client.vision_call`, gpt-5.4 reasoning high, NEVER downgraded) decides the real
  category; a confident processor category auto-reclassifies the message so the right
  engine picks it up — no human burden for the common case.
- **Layer 2 — mandatory net.** Anything NOT rescued (vision unsure, no signature, no key,
  render/API failure) raises a warehouse-actionable Odoo alert through the durable
  `dl_alerts` outbox, deduped per message. Runs unconditionally — defence in depth even
  when Layer 1 is wrong or the API is down. NO `human_processing` message ends silently.

Injection seams (`classify=`) mirror `dl_worker.tick`/`worker.tick` so the whole sweep is
testable offline with a scripted fake — no network, no poppler in the unit path.
"""
from __future__ import annotations

import json
import logging
from html import escape

from .. import db
from . import dl_alerts, dl_extract, dl_worker, llm

log = logging.getLogger("orders.human_processing")

# Deliberately generous: a message the classifier put in human_processing is a FINAL
# decision (not a transient stage), but a small delay lets a quick manual reclassify (or
# a classifier re-run) settle before we spend a vision call / raise a warehouse alert.
STUCK_MINUTES = 15

# The categories that actually have a processor (app engine or a live n8n workflow) — the
# only targets a rescue may route to. `human_processing`/`no_processing` are terminal by
# definition and are NEVER a rescue target.
PROCESSOR_CATEGORIES = {
    "dodacie_listy", "invoices", "reklamacie", "ai_orders", "static_orders",
}

# Below this the vision verdict is treated as "unsure" — the message is NOT reclassified,
# it falls through to the Layer-2 notification instead (a wrong auto-reclassify is worse
# than asking a human).
RESCUE_CONFIDENCE = 0.6

ALERT_KIND = "human_processing_review"

_CLASSIFY_PROMPT = (
    "Si klasifikátor prichádzajúcej firemnej pošty. Na obrázku je príloha e-mailu, "
    "často nečitateľný alebo slabý scan. Rozhodni, do ktorej kategórie príloha patrí, a "
    "odpovedz IBA jedným JSON objektom, bez akéhokoľvek iného textu:\n"
    '{"category": "<jedna z: dodacie_listy, invoices, reklamacie, ai_orders, '
    'static_orders, human_processing, no_processing>", "confidence": <0.0 az 1.0>, '
    '"reason": "<kratke zdovodnenie>"}\n'
    "dodacie_listy = dodaci list; invoices = faktura; reklamacie = reklamacia; "
    "ai_orders alebo static_orders = objednavka; human_processing = nevies rozhodnut; "
    "no_processing = nic na spracovanie. Ak je scan uplne necitatelny, daj nizku "
    "confidence a category human_processing."
)


def _vision_images(raw: bytes) -> list[bytes]:
    """Vision-ready image bytes for one attachment. A PDF is rasterized page-by-page
    (the #224-proven scan path — the file-part path returns garbage for a pure scan,
    which is exactly what a near-empty-OCR attachment is); a raw image is used as-is.
    Never raises — an empty list means "nothing to look at" and the caller falls to
    Layer 2."""
    if not raw:
        return []
    if raw[:5] == b"%PDF-":
        return dl_extract.render_pdf_pages(raw)[:3]   # a doc to classify is 1-3 pages
    return [raw]


def _parse_classification(text: str) -> dict | None:
    """Tolerant parse of the model's JSON verdict (strips a ```json fence). Returns None
    on anything unusable, so a malformed answer degrades to Layer 2, never crashes."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1] if t.count("```") >= 2 else t.strip("`")
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    try:
        obj = json.loads(t)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _vision_classify(cfg, attachments: list[dict]) -> dict | None:
    """Default Layer-1 classifier: ONE vision call over the first readable attachment.
    Returns {category, confidence, reason} or None (no readable attachment, no API key,
    offline cache miss, render/API failure) — every failure degrades to Layer 2."""
    att = next((a for a in attachments if a.get("pdf_bytes")), None)
    if not att:
        return None
    images = _vision_images(att["pdf_bytes"])
    if not images:
        return None
    try:
        texts = llm.from_config(cfg).vision_call(_CLASSIFY_PROMPT, images=images, n=1)
    except (llm.LlmError, llm.CacheMiss, Exception):  # noqa: BLE001 - never crash the sweep
        log.warning("human_processing vision classify failed for a message — "
                    "falling back to notification")
        return None
    return _parse_classification(texts[0]) if texts else None


def _rescue(conn, cfg, message: dict, classify) -> bool:
    """Try Layer 1 on ONE message. Returns True iff it was reclassified (rescued)."""
    if not (message["needs_vision"] and message["has_attachments"]):
        return False
    attachments = dl_worker._read_attachments(cfg, message["message_id"], conn)
    verdict = classify(cfg, attachments)
    if not verdict:
        return False
    new_cat = str(verdict.get("category") or "")
    conf = float(verdict.get("confidence") or 0)
    if new_cat not in PROCESSOR_CATEGORIES or conf < RESCUE_CONFIDENCE:
        return False
    # Mirror /api/message/<id>/reclassify: keep the original for audit, re-open for the
    # engine that owns the new category. The category-change trigger logs its own
    # timeline event; this one records WHY (vision), rollup=False so it never overwrites
    # the pipeline-owned proc_status.
    conn.execute(
        """UPDATE messages
              SET original_category = COALESCE(original_category, category),
                  category = %s, processed = false, processed_at = NULL,
                  processed_by = NULL, processing_at = NULL, error = NULL
            WHERE message_id = %s""", (new_cat, message["message_id"]))
    db.log_event(conn, message["message_id"], "human_processing", "rescued", "ok",
                 outcome=f"vision preklasifikovalo nečitateľný scan → {new_cat}",
                 detail={"to": new_cat, "confidence": conf,
                         "reason": verdict.get("reason", "")}, rollup=False)
    log.info("human_processing rescue: %s → %s (conf %.2f)",
             message["message_id"], new_cat, conf)
    return True


def _notify(conn, cfg, message: dict) -> None:
    """Layer 2: a durable, warehouse-actionable alert. Routes to the warehouse
    delivery-notes channel (staff who handle incoming documents can act — check it,
    reclassify it on the dashboard) — this is NOT an operator/diagnostic alert, so it
    correctly stays on the warehouse channel (see #310)."""
    channel = int(getattr(cfg, "delivery_notes_channel_id", 0) or 0)
    html = (
        "<p>&#128194; Prišiel doklad, ktorý systém nevedel automaticky zaradiť "
        "(nečitateľný / prázdny scan) &mdash; treba ho skontrolovať a prípadne ručne "
        "preklasifikovať na dashboarde.</p>"
        f"<p>Od: {escape(message['from_addr'] or '-')} / "
        f"Predmet: {escape(message['subject'] or '-')} / "
        f"prijaté: {escape(str(message['created_at']))}</p>")
    dl_alerts.enqueue(conn, channel, ALERT_KIND, html,
                      message_id=message["message_id"])


def sweep(conn, cfg, classify=None) -> int:
    """One pass over `human_processing` messages older than `STUCK_MINUTES`. Each is
    rescued (Layer 1) or notified (Layer 2), exactly once per message (deduped via
    `dl_alerts.already_pending` on the notify kind — a rescued message leaves the pit,
    a notified one is skipped next pass). Returns how many messages were handled (rescued
    or newly notified) this pass. Never raises — a per-message failure is logged and the
    pass continues, mirroring `worker.run_forever`'s other sweeps."""
    classify = classify or _vision_classify
    rows = conn.execute(
        """SELECT message_id, subject, from_addr, has_attachments, needs_vision,
                  created_at
             FROM messages
            WHERE category = 'human_processing' AND processed = false
              AND created_at < now() - make_interval(mins => %s)
            ORDER BY created_at ASC LIMIT 20""", (STUCK_MINUTES,)).fetchall()
    handled = 0
    for message_id, subject, from_addr, has_attachments, needs_vision, created_at in rows:
        # Already handled within the dedup window (notified last pass, or reprocessed and
        # still stuck) — never re-spend a vision call or re-notify.
        if dl_alerts.already_pending(conn, ALERT_KIND, message_id):
            continue
        message = {"message_id": message_id, "subject": subject, "from_addr": from_addr,
                   "has_attachments": bool(has_attachments),
                   "needs_vision": bool(needs_vision), "created_at": created_at}
        try:
            if _rescue(conn, cfg, message, classify):
                handled += 1
                continue
            _notify(conn, cfg, message)
            handled += 1
        except Exception:
            log.exception("human_processing sweep failed for %s", message_id)
    if handled:
        log.warning("human_processing: %d stuck message(s) rescued or notified", handled)
    return handled
