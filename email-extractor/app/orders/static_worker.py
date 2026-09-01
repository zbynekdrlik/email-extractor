"""Static-orders worker (#68 groundwork, #133 the real cutover): wires `static_parse` +
`static_ean` + `static_edi` into the worker loop for `category='static_orders'`.

Three modes, the same shape `worker.py` already uses for `ai_orders` (see that module's
docstring) — because n8n still owns the "Static auto orders" workflow (`O8IYhUESjaWmPMTI`)
until the user flips the switch:

- `static_orders_engine=n8n`, `static_orders_shadow=false` (DEFAULT) — completely inert.
- `static_orders_engine=n8n`, `static_orders_shadow=true` — parses with `static_parse` and
  builds the EDI with `static_edi` for comparison only: **claims nothing and marks
  nothing** — the message must stay exactly as n8n expects to find it. Since #133 the
  comparison is no longer one-sided: n8n's OWN "Check Already Sent"/"Claim Send" Postgres
  nodes write into the SAME `edi_sent` table the Python engine uses, with the IDENTICAL
  `content_sha256` algorithm (SHA256 of the document with bytes 47:55 — the header's date
  field — blanked first; verified LIVE via the n8n MCP against the real workflow, 2026-
  08-05 — this is exactly `edi.content_hash`). So when a real n8n upload for the same
  order (`customer_ean=store_ean`, `delivery_date`) already exists, the Python-built
  content is compared to it BYTE-FOR-BYTE, and the verdict is stored in
  `order_runs.result->>'shadow_verdict'` (`match` / `mismatch` / `would_fallback` /
  `empty_order` / `no_n8n_output` — see `run_shadow`). A multi-day clean window is
  provable straight from the DB:
  `SELECT result->>'shadow_verdict', count(*) FROM order_runs r JOIN messages m
     USING (message_id) WHERE r.shadow AND m.category='static_orders' GROUP BY 1;`
  `no_n8n_output` is EXPECTED, not a failure: the shadow tick picks the newest
  not-yet-shadowed message the moment it appears, which can race ahead of n8n's own
  (independent, slower) claim/dispatch — it means "not yet comparable", never "wrong".
- `static_orders_engine=python` — claims with the SAME protocol `worker._claim` uses
  (`processing_at`/`attempts`, the `held_orders`/`mail`-question guards) and owns the
  message. Parses, resolves every item's EAN; only when EVERY item resolves does it build
  + upload, through the SAME two-phase `edi_sent` ledger the AI engine (and n8n) already
  use (`edi.claim_send`/`confirm_sent`/`release_send`) — a claim taken before the upload
  is released on EVERY failure path. If parsing fails, the order is a photo, the header is
  incomplete (`static_edi.build`'s own hard-fail guards), or ANY single item cannot be
  resolved to an EAN, the WHOLE message falls back to the AI pipeline (`pipeline.run`,
  under the SAME claim) — there is deliberately NO silent per-item skip (today's n8n
  behaviour — dropping an unresolved line from the EDI — is the defect this ticket
  removes, not a shape to preserve). The AI pipeline holds the order and asks the
  warehouse via the nástenka, which teaches the wording forever. An Odoo alert is posted
  ONLY on a genuine upload error — never on a routine clean upload (see the digest
  below). Extra-content detection is RECALIBRATED, not dropped ("ZMENA ROZHODNUTIA",
  2026-08-05): `static_extra.residual_text` deterministically subtracts the recognized
  template from the raw mail text; only when meaningful text remains does ONE LLM call
  (`static_extra.py`) judge whether it is an actionable customer addition (a different
  delivery place, a date/quantity change, a question) — n8n's old branch was USEFUL but
  fired on almost every mail, which this fixes without losing the capability. An
  actionable note gets its own immediate Odoo message, quoting the residual text.

  A clean, fresh upload with NO actionable note never posts its own message either —
  it is queued into a durable digest (`static_digest.py`, "DOPLNENIE ROZHODNUTIA",
  2026-08-05) and reported as ONE grouped Odoo message once the batch fills or the
  queue has sat idle past a timeout, both tunable via config.
"""
from __future__ import annotations

import dataclasses
import logging
from datetime import UTC, datetime
from html import escape

from . import (
    edi,
    llm,
    report,
    snapshot,
    static_digest,
    static_ean,
    static_edi,
    static_extra,
    static_parse,
    static_retry,
    worker,
)
from . import upload as upload_mod

log = logging.getLogger("orders.static_worker")

CATEGORY = "static_orders"
SHADOW_DAYS = 3
WORKFLOW = "static_orders"

# Reused as-is (worker.py's engine validation has no ai_orders-specific coupling): "n8n" and
# "python" are the only recognized values, anything else raises.
resolve_engine = worker.resolve_engine

# The extractor's three "cannot proceed" exceptions all mean the same thing: report a
# reviewable run in shadow, or fall back to the AI pipeline in the live engine — never
# crash the tick.
_PARSE_ERRORS = (static_parse.MissingInputText, static_parse.PhotoOrderNeedsVision,
                 static_parse.OrderExtractionError)


def _peek_for_shadow(conn, days: int = SHADOW_DAYS) -> dict | None:
    """Pick a `static_orders` message the shadow run has not seen yet, WITHOUT touching its
    state — same shape as `worker._peek_for_shadow`, plus `has_attachments` (`static_parse`
    needs it for the photo-only guard, which `worker._as_message` never had to carry)."""
    row = conn.execute(
        """SELECT m.message_id, m.subject, m.from_addr, m.from_name,
                  m.combined_text, m.body_text, m.has_attachments
             FROM messages m
            WHERE m.category = %s
              AND m.created_at > now() - make_interval(days => %s)
              AND NOT EXISTS (SELECT 1 FROM order_runs r
                               WHERE r.message_id = m.message_id AND r.shadow)
            ORDER BY m.created_at DESC LIMIT 1""",
        (CATEGORY, max(1, int(days or SHADOW_DAYS)))).fetchone()
    if not row:
        return None
    return {"message_id": row[0], "subject": row[1] or "", "from_addr": row[2] or "",
            "from_name": row[3] or "", "combined_text": row[4] or row[5] or "",
            "has_attachments": bool(row[6])}


def _items_with_ean(items: list[dict], catalog: list[dict]) -> list[dict]:
    out = []
    for it in items:
        gtin = static_ean.resolve_ean(it, catalog, code_map=static_edi.PRODUCT_EAN_BY_CODE,
                                      name_map=static_edi.PRODUCT_EAN_BY_NAME)
        out.append({"name": it.get("description", ""), "quantity": it.get("quantity"),
                    "unit": it.get("unit", "ks"), "gtin": gtin})
    return out


# --- shadow diff against the REAL n8n upload (#133) -------------------------

def _n8n_sent(conn, customer_ean: str, delivery_date: str) -> dict | None:
    """The most recent `edi_sent` row n8n's OWN "Claim Send" node wrote for this identity.
    Same table, same `content_sha256` algorithm as `edi.content_hash` — see this module's
    own docstring for the live-verified proof."""
    row = conn.execute(
        """SELECT content_sha256, filename FROM edi_sent
            WHERE customer_ean = %s AND delivery_date = %s
            ORDER BY id DESC LIMIT 1""",
        (str(customer_ean or ""), str(delivery_date or ""))).fetchone()
    if not row:
        return None
    return {"content_sha256": row[0], "filename": row[1]}


def _diff_against_n8n(conn, built, delivery_date: str) -> tuple[str, str]:
    real = _n8n_sent(conn, built.store_ean, delivery_date)
    if real is None:
        return ("no_n8n_output",
                "n8n zatiaľ túto objednávku (podľa EAN prevádzky + dátumu) neposlalo")
    ours = edi.content_hash(built.content)
    if ours == real["content_sha256"]:
        return "match", ""
    return "mismatch", f"iný obsah než n8n (súbor n8n: {real['filename']!r})"


def run_shadow(conn, message: dict, snapshot_id: int) -> dict:
    """Parse + build for ONE message. Never uploads, never posts, never writes message
    state — the caller (`tick`) only ever records the result into `order_runs`/`order_items`.
    """
    text = message.get("combined_text", "")
    try:
        parsed = static_parse.parse_static_order(
            text, has_attachments=bool(message.get("has_attachments")))
    except _PARSE_ERRORS as e:
        return {"status": "review", "items": [], "reject_reason": str(e),
                "shadow_verdict": "would_fallback", "shadow_note": str(e)}

    if parsed.get("skip"):
        # A valid header with zero item lines never needs the catalog (nothing to match) —
        # skip the load, not just the match.
        return {"status": "review", "items": [], "reject_reason": parsed.get("skipReason", ""),
                "partner": parsed.get("partner", ""),
                "shadow_verdict": "empty_order", "shadow_note": ""}

    catalog = snapshot.load_catalog(conn, snapshot_id)
    items = _items_with_ean(parsed.get("items") or [], catalog)

    try:
        built = static_edi.build(parsed, catalog)
    except ValueError as e:
        return {"status": "review", "items": items, "reject_reason": str(e),
                "partner": parsed.get("partner", ""),
                "shadow_verdict": "would_fallback", "shadow_note": str(e)}

    status = "ok" if built.items_skipped == 0 else "partial"
    if built.items_skipped:
        # #133: the live engine never uploads a partial EDI — an unresolved item routes
        # the whole message to the AI pipeline instead. So a shadow run that WOULD have
        # skipped an item is "would_fallback", never compared against n8n's own output.
        verdict = "would_fallback"
        note = (f"{built.items_skipped} položka/y bez EAN — Python engine by objednávku "
                "poslal na AI, nie čiastočný EDI")
    else:
        verdict, note = _diff_against_n8n(conn, built, parsed.get("deliveryDate", ""))
    return {"status": status, "items": items, "partner": parsed.get("partner", ""),
            "delivery_date": parsed.get("deliveryDate", ""),
            "order_number": parsed.get("fullOrderNumber", ""),
            "edi_filename": built.filename, "edi_preview": built.content,
            "items_with_ean": built.items_with_ean, "items_skipped": built.items_skipped,
            "shadow_verdict": verdict, "shadow_note": note}


# --- engine=python: real claim, same protocol worker._claim uses -----------

def _claim(conn) -> dict | None:
    row = conn.execute(
        f"""UPDATE messages SET processing_at = now(), attempts = COALESCE(attempts, 0) + 1
             WHERE id = (SELECT id FROM messages
                          WHERE category = %s AND processed = false
                            AND COALESCE(attempts, 0) < %s
                            AND (processing_at IS NULL
                                 OR processing_at < now()
                                    - interval '{worker.CLAIM_STALE_MINUTES} minutes')
                            -- same #93/#164 guards worker._claim uses: a message still
                            -- waiting on a warehouse answer (from a fallback that held it)
                            -- is WAITING, not stuck — never re-claim it.
                            AND NOT EXISTS (
                                SELECT 1 FROM held_orders h
                                 WHERE h.message_id = messages.message_id
                                   AND h.status = 'held')
                            AND NOT EXISTS (
                                SELECT 1 FROM order_questions q
                                 WHERE q.message_id = messages.message_id
                                   AND q.kind = 'mail' AND q.status = 'open')
                          ORDER BY created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED)
         RETURNING message_id, subject, from_addr, from_name, combined_text, body_text,
                   has_attachments, attempts""",
        (CATEGORY, worker.MAX_ATTEMPTS)).fetchone()
    return _as_message(row)


def _as_message(row) -> dict | None:
    if not row:
        return None
    # #117 (mirrored from worker._as_message): "today" is what feeds memory.resolve's
    # as_of date fence and hold.is_past_deadline downstream, inside the AI-pipeline
    # fallback — without it those silently no-op.
    return {"message_id": row[0], "subject": row[1] or "", "from_addr": row[2] or "",
            "from_name": row[3] or "", "combined_text": row[4] or row[5] or "",
            "has_attachments": bool(row[6]), "attempts": int(row[7] or 0),
            "today": datetime.now(UTC).date().isoformat()}


def _fallback_to_ai(conn, cfg, message: dict, snapshot_id: int, note: str,
                    pipeline=None) -> dict:
    """Route this ALREADY-CLAIMED message through the AI pipeline instead of a silent
    per-item skip or a dead end (#133 design decision, 2026-08-05). Same claim — the AI
    pipeline neither re-claims nor releases `messages.processing_at`; it holds unresolved
    lines and asks the warehouse via the nástenka, teaching the wording forever.

    The AI pipeline's REAL-vs-SHADOW behaviour is driven entirely by `cfg.orders_shadow`
    (`pipeline._run`) — a flag that belongs to the AI engine's OWN, separate, still
    undecided cutover (`config.py`) and has nothing to do with `static_orders_engine`.
    If that flag happened to be `True` while `static_orders_engine=python` is live, an
    unmodified `cfg` would silently run this fallback in SHADOW mode: no claim, no hold,
    no teach, no upload, no Odoo post, no event logged — `tick`'s own `has_open` check
    would then see nothing held and mark the message processed anyway, permanently
    losing the order with zero trace (review finding on PR #181). Forcing a LIVE copy of
    `cfg` here, independent of whatever `orders_shadow` happens to be set to, is what
    makes this fallback safe regardless of the AI engine's own unrelated toggle.
    """
    log.info("static order %s falls back to the AI pipeline: %s", message["message_id"], note)
    if pipeline is None:
        from . import pipeline as pipeline_mod
        pipeline = pipeline_mod.run
    live_cfg = dataclasses.replace(cfg, orders_shadow=False)
    result = dict(pipeline(conn, live_cfg, message, snapshot_id) or {})
    result["static_fallback"] = note
    return result


def _merge_spend(a: dict | None, b: dict | None) -> dict | None:
    """Add two `llm.Client.spend()` dicts together — needed when BOTH the extra-content
    check and an AI-pipeline fallback call the model for the SAME message; `spend.record`
    overwrites `order_runs`' spend columns rather than accumulating, so losing either
    side here would silently under-report that run's real cost."""
    if not a:
        return b
    if not b:
        return a
    out = dict(a)
    for key in ("calls", "cached_calls", "tokens_in", "tokens_cached", "tokens_out",
               "tokens_reasoning"):
        out[key] = int(a.get(key, 0) or 0) + int(b.get(key, 0) or 0)
    out["cost_usd"] = float(a.get("cost_usd", 0) or 0) + float(b.get("cost_usd", 0) or 0)
    out["model"] = a.get("model") or b.get("model")
    return out


def _maybe_notify_extra_content(conn, cfg, message: dict, text: str, parsed: dict,
                                post=None, llm_client=None) -> dict:
    """Deterministic pre-filter first, LLM only when it finds something (#133 "ZMENA
    ROZHODNUTIA", 2026-08-05). Runs independently of whatever the ship/fallback outcome
    ends up being — the notification is an add-on, never a blocker. Never raises: a
    failed check degrades to "not actionable", exactly like a failed Odoo post degrades
    to "not delivered" elsewhere in this module.

    Returns `{"actionable": bool, "spend": dict|None}`.
    """
    residual = static_extra.residual_text(text)
    if not static_extra.has_meaningful_residual(residual):
        return {"actionable": False, "spend": None}

    client = llm_client or llm.from_config(cfg)
    try:
        answer = client.json_call(static_extra.prompt(), residual, static_extra.SCHEMA,
                                  name="extra_content")
    except Exception:
        log.exception("extra-content check failed for %s — treating as non-actionable",
                      message["message_id"])
        spend = client.spend() if hasattr(client, "spend") else None
        return {"actionable": False, "spend": spend}
    spend = client.spend() if hasattr(client, "spend") else None
    if not answer.get("actionable"):
        return {"actionable": False, "spend": spend}

    post = post or (lambda c, html: report.post_from_config(c, html))
    quote = residual[:400]
    reason = str(answer.get("reason") or "")
    html = (f"<p><b>{escape(parsed.get('partner') or '')} "
           f"{escape(parsed.get('fullOrderNumber') or '')}</b> &mdash; zákazník dopísal "
           f"do mailu: „{escape(quote)}“</p>")
    # #182 review finding: a failed OR undelivered ("Odoo not configured" — post_from_
    # config returns None, no exception) alert must never be logged as a genuine "ok"
    # success — this note was ALREADY judged actionable, so silently mislabeling its
    # delivery outcome would hide exactly the kind of miss this feature exists to catch
    # (same "never mark terminal on an undelivered alert" principle confirm.py's sweep
    # already applies — this one-shot check has no natural retry loop, so the honest fix
    # here is an honest status, not a fabricated success).
    delivered = True
    try:
        result = post(cfg, html)
        delivered = result is not None
    except Exception:
        log.exception("posting the extra-content alert failed for %s", message["message_id"])
        delivered = False
    if not delivered:
        log.warning("extra-content alert for %s was NOT delivered (post failed or Odoo "
                   "not configured) — the actionable note is only recorded here, not "
                   "on the phone", message["message_id"])
    report.log_event(
        conn, message["message_id"], stage="extra_content",
        status="ok" if delivered else "error",
        outcome=(f"Zákazník dopísal text: {quote}" if delivered else
                f"Zákazník dopísal text (UPOZORNENIE SA NEPOSLALO): {quote}"),
        detail={"residual": residual, "reason": reason, "delivered": delivered},
        workflow=WORKFLOW, rollup=False)
    return {"actionable": True, "spend": spend}


def _ship(conn, cfg, message: dict, parsed: dict, built, upload=None, post=None,
          actionable_note: bool = False, list_dirs=None) -> dict:
    """Upload ONE fully-resolved static order — the only path that ever writes to ORION
    from this module. Mirrors `pipeline._ship_one`'s claim/upload/confirm shape."""
    upload = upload or (lambda c, name, content: upload_mod.put(c, name, content))
    post = post or (lambda c, html: report.post_from_config(c, html))
    store_ean = built.store_ean
    delivery = parsed.get("deliveryDate", "")
    order_no = parsed.get("fullOrderNumber", "")
    partner = parsed.get("partner", "")

    claimed, confirmed = edi.claim_send_or_identify(
        conn, store_ean, delivery, built.content, built.filename)
    if not claimed:
        # #133: a duplicate/already-sent skip must never look, in the timeline, like a
        # fresh genuine upload — its own stage, never "uploaded_orion" — and it is NOT a
        # fresh upload, so it never enters the digest count either.
        #
        # #271: `confirmed` tells apart a GENUINE duplicate (someone already uploaded
        # this exact document) from a merely fresh/UNCONFIRMED claim held by another
        # concurrent run (which may be mid-upload right now — nothing has necessarily
        # reached ORION yet). Reporting both the same way ("already sent") was the
        # documented "theoretically same gap" as #216's own desadv fix — never a
        # double-upload risk (the underlying claim stays atomic either way), but a
        # false claim in the event log/digest for the unconfirmed case.
        if confirmed:
            outcome = f"EDI už bolo odoslané skôr: {built.filename} — preskočené"
            log.warning("static EDI for %s / %s already sent — not uploading again",
                        store_ean, delivery)
        else:
            outcome = (f"EDI pre {built.filename} práve spracúva iný beh (zámer ešte "
                       "nepotvrdený) — preskočené, aby nevzniklo duplicitné odoslanie")
            log.warning("static EDI for %s / %s has a fresh, unconfirmed claim held "
                        "elsewhere — not uploading again", store_ean, delivery)
        report.log_event(
            conn, message["message_id"], stage="duplicate_skip", status="ok",
            outcome=outcome, detail={"filename": built.filename, "confirmed": confirmed},
            workflow=WORKFLOW)
        return {"status": "ok", "items": [], "shipped": True, "edi_filename": built.filename,
                "partner": partner, "order_number": order_no, "delivery_date": delivery}

    list_dirs = list_dirs or upload_mod.list_dirs

    def _finish_shipped() -> dict:
        """The shared "order is CONFIRMED on ORION" tail — called after a clean first-try
        upload, AND (#372) after a transient failure whose presence check proved the bytes
        already landed (reply lost), or whose single bounded retry succeeded. Extracted so
        every success path shares EXACTLY one shape, never three independently-maintained
        copies (the same reasoning `dl_document._finish_shipped` documents for #239).
        Captures conn/cfg/message/store_ean/delivery/order_no/partner/built/actionable_note/
        post from the enclosing scope."""
        edi.confirm_sent(conn, store_ean, delivery, built.content,
                         pg_dsn=getattr(cfg, "pg_dsn", ""))
        outcome = f"EDI vytvorené: {built.filename}"
        report.log_event(
            conn, message["message_id"], stage="uploaded_orion", status="ok", outcome=outcome,
            detail={"edi_file": built.filename, "orion_path": edi.orion_path(built.filename)},
            workflow=WORKFLOW)
        if not actionable_note:
            # #133 "DOPLNENIE ROZHODNUTIA": a clean upload with nothing extra to say never
            # posts its own message — it joins the durable digest instead (batch/idle flush).
            static_digest.queue(conn, message["message_id"], built.filename)
            static_digest.maybe_flush_batch(conn, cfg, post=post)
        return {"status": "ok", "items": [], "shipped": True, "edi_filename": built.filename,
                "partner": partner, "order_number": order_no, "delivery_date": delivery}

    def _alert_and_release(err: Exception) -> dict:
        """The pre-#372 "upload genuinely failed" tail, unchanged: release the claim so the
        order can be retried by a LATER independent attempt (a human reprocess, or a stale-
        claim reclaim), post one best-effort Odoo alert, and log a reviewable error event.
        Reached whenever a safe retry was not possible: a NON-transient failure, a transient
        one whose presence check proved the document genuinely absent AND whose single retry
        also failed, or a transient one whose presence check itself could not be attempted /
        was ambiguous (#372). Must be called from inside an active `except` so
        `log.exception` captures the real traceback."""
        edi.release_send(conn, store_ean, delivery, built.content)
        log.exception("static order upload of %s failed", built.filename)
        note = ("Odoslanie statickej objednávky do ORIONu zlyhalo — skús znova alebo "
               "nahlás administrátorovi")
        html = (f"<p><b>{escape(partner)} {escape(order_no)}</b> &mdash; {note} "
               f"({escape(built.filename)})</p>")
        try:
            post(cfg, html)
        except Exception:
            log.exception("posting the static-order upload-failure alert failed")
        report.log_event(
            conn, message["message_id"], stage="review", status="error", outcome=note,
            detail={"error": repr(err), "filename": built.filename}, workflow=WORKFLOW)
        return {"status": "error", "items": [], "shipped": False, "reject_reason": note,
                "partner": partner, "order_number": order_no, "delivery_date": delivery}

    try:
        upload(cfg, built.filename, built.content)
    except Exception as e:
        # #372: a TRANSIENT failure gets a stable-identity presence check BEFORE deciding
        # what to do — the exact #239 shape ported from `dl_document`. A NON-transient
        # failure (`static_retry.is_upload_transient(e)` False) skips the check entirely
        # and keeps the pre-#372 behaviour — `landed` stays `None`.
        landed = (static_retry.check_landed(conn, cfg, list_dirs, built.filename,
                                            built.content)
                  if static_retry.is_upload_transient(e) else None)
        if landed is True:
            # The reply was lost, but the bytes are already on ORION under this exact
            # (stable) filename — confirming, never re-uploading, is what actually prevents
            # a duplicate delivery in the warehouse (the v0.9.70 incident class).
            log.warning(
                "static order upload of %s: the reply was lost but the document is already "
                "on ORION (stable-name match) — confirming instead of re-uploading (%s)",
                built.filename, e)
            return _finish_shipped()
        if landed is False:
            # Genuinely absent everywhere a static EDI could sit — exactly ONE retry is
            # safe here, bounded (never a loop), with the SAME claim held throughout (never
            # release-then-reclaim, which is precisely what removed the anti-duplicate
            # protection in the v0.9.70 incident this port exists to prevent).
            log.info(
                "static order upload of %s: transient failure (%s), document not yet on "
                "ORION — retrying exactly once with the same claim", built.filename, e)
            try:
                upload(cfg, built.filename, built.content)
            except Exception as e2:
                log.exception("static order upload retry of %s also failed (original: %s)",
                              built.filename, e)
                return _alert_and_release(e2)
            return _finish_shipped()
        # `landed is None`: either non-transient, or the presence check itself could not be
        # attempted (the SFTP connection that just failed the upload is very likely down for
        # a follow-up listing too) or a presence match was ambiguous (a different confirmed
        # order occupies the name) — no safe retry is possible, so this keeps the pre-#372
        # behaviour exactly.
        return _alert_and_release(e)

    return _finish_shipped()


def run_live(conn, cfg, message: dict, snapshot_id: int, pipeline=None, upload=None,
            post=None, llm_client=None, list_dirs=None) -> dict:
    """The claimed message's real, live outcome — either a genuine ORION upload, a benign
    no-op (empty order / duplicate), or a fallback to the AI pipeline. Never silently
    drops an item."""
    text = message.get("combined_text", "")
    try:
        parsed = static_parse.parse_static_order(
            text, has_attachments=bool(message.get("has_attachments")))
    except _PARSE_ERRORS as e:
        # No recognized template to diff against — the whole message (extraction AND
        # its own notification) becomes the AI pipeline's job.
        return _fallback_to_ai(conn, cfg, message, snapshot_id, str(e), pipeline=pipeline)

    # #133 "ZMENA ROZHODNUTIA": the extra-content check runs independently of the ship
    # outcome below — an actionable note is reported even if the order itself falls back
    # or errors ("notifikácia je doplnok, nie blokáda").
    extra = _maybe_notify_extra_content(conn, cfg, message, text, parsed, post=post,
                                        llm_client=llm_client)

    if parsed.get("skip"):
        # A valid header, zero item lines (KARMEN's "BEZ OBJEDNAVKY") — a genuine empty
        # order, not a parsing failure. Nothing to ship, nothing to ask about.
        note = "Objednávka bez položiek (BEZ OBJEDNAVKY) — nič na odoslanie."
        report.log_event(conn, message["message_id"], stage="empty_order", status="ok",
                         outcome=note, detail={"partner": parsed.get("partner", "")},
                         workflow=WORKFLOW)
        result = {"status": "ok", "items": [], "shipped": False, "reject_reason": note,
                 "partner": parsed.get("partner", "")}
        if extra["spend"]:
            result["spend"] = extra["spend"]
        return result

    catalog = snapshot.load_catalog(conn, snapshot_id)
    items = _items_with_ean(parsed.get("items") or [], catalog)
    missing = [it["name"] for it in items if not it["gtin"]]
    if missing:
        # #133: NO silent per-item skip — ANY unresolved item sends the WHOLE order to the
        # AI pipeline, which holds it and asks the warehouse instead of dropping the line.
        note = f"{len(missing)} položka/y bez EAN: {', '.join(missing[:3])}"
        result = _fallback_to_ai(conn, cfg, message, snapshot_id, note, pipeline=pipeline)
        merged = _merge_spend(result.get("spend"), extra["spend"])
        if merged:
            result["spend"] = merged
        return result

    try:
        built = static_edi.build(parsed, catalog)
    except ValueError as e:
        # static_edi.build's own hard-fail guards (missing prevNumber, or — belt and
        # braces — no item resolved an EAN, which the pre-check above already prevents).
        result = _fallback_to_ai(conn, cfg, message, snapshot_id, str(e), pipeline=pipeline)
        merged = _merge_spend(result.get("spend"), extra["spend"])
        if merged:
            result["spend"] = merged
        return result

    result = _ship(conn, cfg, message, parsed, built, upload=upload, post=post,
                   actionable_note=extra["actionable"], list_dirs=list_dirs)
    if extra["spend"]:
        result["spend"] = extra["spend"]
    return result


def tick(conn, cfg, pipeline=None, upload=None, post=None, llm_client=None,
         list_dirs=None) -> int:
    """Process at most one `static_orders` message. Returns 0 or 1 (whether a MESSAGE was
    handled — a digest flush alone, with no message, still returns 0).

    `pipeline`/`upload`/`post`/`llm_client` are injected so the claim/fallback/upload/
    extra-content guarantees can be tested without the LLM stack or a real ORION host;
    production passes the real ones.
    """
    # #133 "DOPLNENIE ROZHODNUTIA": the idle-flush check runs on EVERY tick, regardless
    # of engine/shadow — if the engine is ever flipped back to n8n with rows still
    # pending, they must still eventually flush rather than sit forever.
    static_digest.flush_idle(conn, cfg, post=post)

    engine = resolve_engine(getattr(cfg, "static_orders_engine", "n8n"))
    shadow = bool(getattr(cfg, "static_orders_shadow", False))
    if engine != "python" and not shadow:
        return 0

    snapshot_id = snapshot.latest_snapshot_id(conn)
    if not snapshot_id:
        log.warning("no catalog snapshot yet — static worker idle")
        return 0

    if engine == "python":
        message = _claim(conn)
        if not message:
            return 0
        run_id = worker._start_run(conn, message["message_id"], snapshot_id, shadow=False)
        try:
            result = run_live(conn, cfg, message, snapshot_id, pipeline=pipeline,
                              upload=upload, post=post, llm_client=llm_client,
                              list_dirs=list_dirs)
        except Exception as e:
            log.exception("static order pipeline failed for %s", message["message_id"])
            worker._finish_run(conn, run_id, "error", None, error=repr(e))
            # Release the claim: a crashed run must be retryable and must never look
            # processed — mirrors worker.tick's own exception handling exactly.
            conn.execute(
                "UPDATE messages SET processing_at = NULL WHERE message_id = %s",
                (message["message_id"],))
            # #330: `_claim` already incremented `attempts`; once it reaches MAX_ATTEMPTS
            # the `_claim` guard (`attempts < MAX_ATTEMPTS`) stops reprocessing this
            # message, so it would otherwise vanish with NO human-visible diagnostic —
            # exactly the undiagnosable "Chyba: neznáma [line 150]" class the n8n engine
            # produced. On that FINAL attempt, surface a REAL error (rollup=True ->
            # proc_status='error') carrying the exception type + message + stage. The
            # first MAX_ATTEMPTS-1 crashes stay silent-and-retryable (a transient crash
            # must still auto-recover), so this fires at most once per message — never a
            # per-tick email_events flood on a deterministic crash.
            if int(message.get("attempts") or 0) >= worker.MAX_ATTEMPTS:
                report.log_event(
                    conn, message["message_id"], stage="error", status="error",
                    outcome=report.crash_outcome(e, "run_live"),
                    detail={"error": repr(e), "stage": "run_live",
                            "attempts": int(message.get("attempts") or 0)},
                    workflow=WORKFLOW)
            return 0
        worker._finish_run(conn, run_id, result.get("status", "ok"), result)
        worker._check_spend_cap(conn, cfg, shadow=False)
        from .hold import has_open
        # A run that just HELD this message (via the AI-pipeline fallback) must not be
        # marked processed — same #93 contract worker.tick already follows.
        if not has_open(conn, message["message_id"]):
            conn.execute(
                """UPDATE messages
                      SET processed = true, processed_at = now(), processed_by = %s,
                          processing_at = NULL
                    WHERE message_id = %s""",
                (CATEGORY, message["message_id"]))
        return 1

    message = _peek_for_shadow(conn, getattr(cfg, "static_orders_shadow_days", SHADOW_DAYS))
    if not message:
        return 0
    run_id = worker._start_run(conn, message["message_id"], snapshot_id, shadow=True)
    result = run_shadow(conn, message, snapshot_id)
    worker._finish_run(conn, run_id, result.get("status", "ok"), result)
    return 1
