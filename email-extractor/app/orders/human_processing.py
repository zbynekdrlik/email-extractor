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
  render/API failure) raises a durable OPERATOR/triage alert through the `dl_alerts`
  outbox to the OPS channel (#308 incident correction — human_processing is a catch-all
  dominated by non-warehouse mail, so "couldn't classify" is an operator concern, never a
  warehouse one; held when the ops channel is unset, NEVER on 243/152). Deduped per
  message, runs unconditionally. Only messages received on/after `BACKLOG_CUTOFF` are ever
  touched — the pre-existing historical backlog must never auto-enter a pipeline/channel.

Injection seams (`classify=`) mirror `dl_worker.tick`/`worker.tick` so the whole sweep is
testable offline with a scripted fake — no network, no poppler in the unit path.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from datetime import time as _dtime

from .. import db
from . import confirm, dl_alerts, dl_extract, dl_worker, llm, report

log = logging.getLogger("orders.human_processing")

# Deliberately generous: a message the classifier put in human_processing is a FINAL
# decision (not a transient stage), but a small delay lets a quick manual reclassify (or
# a classifier re-run) settle before we spend a vision call / raise an ops alert.
STUCK_MINUTES = 15

# #308 incident (2026-08-14): human_processing turned out to be a large CATCH-ALL (846
# messages accumulated since June — payslips, job-board replies, HR mail, NOT stuck
# warehouse documents), not the small "a human must handle this" bucket the ticket
# assumed. The first live sweep began back-filling that whole historical backlog into a
# channel + (via vision rescue) into live pipelines. This HARD cutoff is the guard: only
# messages received on/after the day the sweep first went live are ever rescued OR
# notified — the pre-existing backlog must NEVER auto-enter a pipeline or channel; it
# belongs at most in a future ops digest / dashboard listing, reviewed deliberately.
# A fixed date (not a rolling window) is deliberate: the backlog stays excluded forever.
BACKLOG_CUTOFF = "2026-08-14"

# #385: the sweep's Layer-2 net used to re-notify a stuck mail once per working-day
# morning FOREVER — two body-only "Re: objednávka" acknowledgment replies (correctly
# parked in human_processing by the n8n sorter, since they carry no processable order)
# were re-asked into the ops digest 36 times over 3 weeks. Owner directive
# `two-workday-horizon` (memory, 2026-08-18): "nevybavené správy staršie ako 2 PRACOVNÉ
# dni … nikdy nenaháňať" — a message older than 2 WORKING days is moot (already handled
# manually by the warehouse's daily pass). So the sweep now takes ONLY messages within
# this many working days of `now`: a fresh stuck mail is surfaced promptly and reminded
# once per working-day morning until it crosses the horizon, then goes quiet (it stays in
# human_processing, still visible on the dashboard for a deliberate operator reclassify —
# only the Odoo nag stops). This upper bound ALSO fixes a latent starvation: the candidate
# query is `ORDER BY created_at ASC LIMIT 10`, so without it a growing set of
# permanently-stuck old mails would fill every slot and starve newer stuck mails of their
# first alert. Overridable per the confirm.py getattr convention (no config.yaml change).
REMINDER_MAX_WORKING_DAYS = 2

# The categories that actually have a processor — the ONLY targets a rescue may route to.
# `dodacie_listy`/`ai_orders`/`static_orders` are owned by the Python engines here;
# `invoices`/`reklamacie` are handled by live n8n workflows ("Invoices Forward v2",
# "Reklamacie"). INVARIANT: every category in this set must have a live processor — a
# rescue moves the message OUT of human_processing (past the Layer-2 net), so routing to a
# category whose processor is gone would silently recreate the exact pit this closes. If a
# processor is ever retired, drop its category here. `human_processing`/`no_processing` are
# terminal by definition and are NEVER a rescue target.
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
    except Exception:  # noqa: BLE001 - any failure (LlmError/CacheMiss/network) degrades to notify
        log.warning("human_processing vision classify failed for a message — "
                    "falling back to notification")
        return None
    return _parse_classification(texts[0]) if texts else None


def _rescue(conn, cfg, message: dict, classify) -> bool:
    """Try Layer 1 on ONE message. Returns True iff it was reclassified (rescued)."""
    if not (message["needs_vision"] and message["has_attachments"]):
        return False
    # Deliberate reuse of dl_worker's own attachment reader (a `_`-private helper): it
    # already encodes the #297 spreadsheet-exclusion + on-disk byte loading correctly, and
    # duplicating that here would be a second copy to keep in sync. Read-only, no DL state.
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


def _recently_rescued(conn, message_id: str) -> bool:
    """True when this message was already rescued within the dedup window. A rescue
    changes the message's category, so it normally leaves this sweep's candidate set for
    good — but if it is ever RETURNED to human_processing (a manual dashboard reclassify,
    an incident revert), the sweep would re-rescue it and re-raise the same downstream
    question, a re-ask loop (observed live 2026-08-14: an incident revert fed a message
    straight back to the sweep, raising a duplicate dl_supplier question 10s later). This
    guard makes a rescue idempotent per message: rescued once, never fought again within
    the window."""
    row = conn.execute(
        "SELECT 1 FROM email_events WHERE message_id = %s AND workflow = 'human_processing' "
        "AND stage = 'rescued' AND ts > now() - make_interval(hours => %s) LIMIT 1",
        (message_id, dl_alerts.DEDUP_WINDOW_HOURS)).fetchone()
    return row is not None


def _notify(conn, cfg, message: dict) -> None:
    """Layer 2: a durable OPERATOR/triage alert. #308 incident correction — "the system
    could not classify this" is an operator concern, NOT a warehouse one (human_processing
    is a catch-all dominated by non-warehouse mail), so it routes to the OPS channel via
    the same hold #310 built: `report.ops_channel` returns 0 when unset, and
    `dl_alerts.flush_pending` HOLDS a channel-0 group (counted on the dashboard, never
    delivered) — NEVER the warehouse (243) / sales (152) channels. A genuine document is
    already routed to its engine by the Layer-1 vision rescue; this net catches only the
    residual an operator must triage."""
    channel = report.ops_channel(cfg)
    # #336: the body is now just ONE short line (`• odosielateľ — predmet (prijaté D.M.)`);
    # the explanation sentence + the dashboard action link live ONCE in the per-kind header
    # `dl_alerts.flush_pending` builds for the whole group (`GROUPED_ITEM_KINDS`), never
    # repeated per message — that repetition (plus the microsecond `prijaté` timestamp this
    # drops) was the pre-#336 3000-char wall.
    line = dl_alerts.item_line(message["from_addr"], message["subject"],
                               message.get("created_at"))
    dl_alerts.enqueue(conn, channel, ALERT_KIND, line,
                      message_id=message["message_id"])


def _horizon_cutoff(now: datetime, working_days: int) -> datetime:
    """The earliest `created_at` a message may have and still be a sweep candidate: the
    START (local midnight, Europe/Bratislava) of the date `working_days` WORKING days
    before `now`'s local date. Saturdays and Sundays never count (the warehouse does not
    work weekends — same convention `confirm.morning_check_active` uses), so a Friday mail
    is still within a 2-working-day horizon the following Monday. Returned as UTC so it
    drops straight into the candidate query as a bind parameter. A message OLDER than this
    has passed the owner's 2-working-day horizon (`two-workday-horizon`) and must never be
    chased again (#385). `working_days <= 0` yields today's local midnight (only today's
    mail stays)."""
    d = now.astimezone(confirm.LOCAL_TZ).date()
    counted = 0
    while counted < max(0, working_days):
        d -= timedelta(days=1)
        if d.weekday() < 5:            # Monday=0 … Friday=4
            counted += 1
    start_local = datetime.combine(d, _dtime.min, tzinfo=confirm.LOCAL_TZ)
    return start_local.astimezone(UTC)


def sweep(conn, cfg, classify=None, now=None) -> int:
    """One pass over `human_processing` messages older than `STUCK_MINUTES` AND within the
    2-working-day horizon (`REMINDER_MAX_WORKING_DAYS`, #385 — a mail past the horizon is
    moot and is never chased again; it stays in the pit, visible on the dashboard, only the
    ops-digest nag stops). Each candidate is rescued (Layer 1) or notified (Layer 2),
    exactly once per message (deduped via `dl_alerts.already_pending`/`reminder_suppressed`
    on the notify kind — a rescued message leaves the pit, a notified one is skipped next
    pass). Returns how many messages were handled (rescued or newly notified) this pass.
    Never raises — a per-message failure is logged and the pass continues, mirroring
    `worker.run_forever`'s other sweeps. `now` (defaults to the wall clock) is injectable
    for deterministic horizon tests."""
    classify = classify or _vision_classify
    now = now or datetime.now(UTC)
    # #385: never chase a mail past the 2-working-day horizon (see REMINDER_MAX_WORKING_DAYS).
    max_wd = int(getattr(cfg, "human_processing_reminder_max_working_days",
                         REMINDER_MAX_WORKING_DAYS) or REMINDER_MAX_WORKING_DAYS)
    horizon_cutoff = _horizon_cutoff(now, max_wd)
    # LIMIT paces the per-tick work: a first exposure to a backlog does at most this many
    # vision calls per ~15s tick, the rest drain over the next ticks. Total cost is
    # bounded by the dedup regardless (each message gets ONE vision attempt per 4h
    # window), so a small limit just smooths the burst — it never skips a message.
    rows = conn.execute(
        """SELECT message_id, subject, from_addr, has_attachments, needs_vision,
                  created_at
             FROM messages
            WHERE category = 'human_processing' AND processed = false
              AND created_at >= %s                          -- never the pre-sweep backlog
              AND created_at >= %s                          -- never past the working-day horizon (#385)
              AND created_at < now() - make_interval(mins => %s)
            ORDER BY created_at ASC LIMIT 10""",
        (BACKLOG_CUTOFF, horizon_cutoff, STUCK_MINUTES)).fetchall()
    handled = 0
    for message_id, subject, from_addr, has_attachments, needs_vision, created_at in rows:
        # #336: the first notification for a stuck message fires promptly; a RE-reminder
        # for a still-unresolved one is throttled to ONCE per morning (skipping weekends)
        # by `reminder_suppressed`, instead of the old ~4h re-ask — never re-spend a vision
        # call or re-notify off-cadence. `_recently_rescued` additionally blocks a re-rescue
        # loop for a message returned to human_processing after an earlier rescue (see its
        # docstring).
        if (dl_alerts.reminder_suppressed(conn, cfg, ALERT_KIND, message_id)
                or _recently_rescued(conn, message_id)):
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
