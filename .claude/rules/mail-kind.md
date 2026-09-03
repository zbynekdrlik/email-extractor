---
paths:
  - "email-extractor/app/orders/mail_kind.py"
  - "email-extractor/app/orders/pipeline.py"
  - "email-extractor/app/httpapi_dashboard_data.py"
---

# AI safe-discard of non-order / non-DL mails (#376)

Karmen inspectors forward infomails (no order in them). The extractor has NO "is this even an
order?" verdict, so `orders == []` used to ask the warehouse "is this an order?" over and over
— the learned `mail_rules(sender, subject)` rule never generalizes because the subject changes
every time. `app/orders/mail_kind.py` adds the missing verdict as a SEPARATE gated model call
and lets the pipeline discard a genuine infomail to `no_processing` — safely.

## The gate is a big AND — bias ALWAYS toward asking

A mail is auto-discarded (`pipeline._mail_kind_discard_reason` returns a non-empty reason)
ONLY when EVERY condition holds; any failure returns `""` = ask the warehouse (today's
behavior). TWO independent NOs are required: the extractor's empty `orders` AND the
classifier's `other` verdict at `confidence >= mail_kind.NOT_ORDER_MIN_CONFIDENCE` (0.85).
Conditions, in the order the code evaluates them (cheap first):

1. **Restore-loop guard (rule 6)** — `NOT EXISTS` an `email_events` row with `stage='restore'`
   for the message. A mail the operator already restored from an earlier discard is NEVER
   auto-discarded a second time. The restore endpoint writes exactly this event.
2. **Deterministic veto wall (`mail_kind.veto_reason`, rules 3+4)** — any one vetoes:
   readability (a `needs_vision` attachment, or a pdf/docx/xlsx with empty `extracted_text` —
   AI may only discard what it SAW), a structured attachment (xlsx/xls/csv/ods/fods), a
   document identifier in the text, or `>= 2` item lines.
3. **Classifier verdict (rules 2+5)** — `mail_kind.classify` returns `None` on ANY failure
   (exception, non-object, unknown `kind`, non-numeric confidence) → ask; only `other` at
   `>= 0.85` is a discard candidate. `delivery_note`/`change_request`/`order` are never
   discarded.

## Reusable gotchas (each cost a review round or a live incident elsewhere)

- **Diacritic-fold the haystack BEFORE matching ASCII stems (the #265 lesson, applied here).**
  A plain-ASCII regex stem (`objedn`, `dodac`, `av[ií]zo`) can NEVER match its own Slovak
  diacritic forms (`č/ľ/ĺ/ň/í`). `mail_kind.structural_veto` folds subject+text via the
  package's ONE normalizer `dl_match.fold` (promoted from the private `_fold` in #376, `_fold`
  kept as a byte-identical alias — same promotion precedent as `matches_wire_prefix`) and then
  matches ASCII patterns. `test_orders_mail_kind.py` probes EVERY cited inflection
  (`objednávka č./no/nr/#`, `dodací list`, `dodacích listov`, `DL č.`, `DESADV`, `avízo`) AND
  the folding normalizer itself — never trust a stem "looks right", prove it against the real
  forms. A BARE word "objednávka" in prose is deliberately NOT a veto (the `objedn…` stem
  requires an actual č/no/nr/# + digit after it) so Karmen "nedodaný tovar z objednávky" stays
  discardable.

- **A discard path returns BEFORE `_finish`, exactly like the promo carve-out.** `_finish`'s
  #164 invariant would otherwise RAISE the very warehouse question the discard is avoiding (a
  non-technical review with NO board question → its CRITICAL fallback fires). Both the promo
  filter and the AI-not-order discard funnel through ONE shared helper
  `pipeline._discard_no_processing(conn, message, outcome, marker, notes, detail)` — never a
  third copy of the UPDATE+event. `TECHNICAL_REASONS` is NOT extended (that contract is for
  `_finish`-based lifecycle reviews, not for a mail that leaves the pipeline entirely). Test:
  `test_a_discard_raises_no_critical_finish_fallback` (caplog, CRITICAL).

- **The test ScriptedClient serves the classifier BY NAME (`mail_kind`), not positionally.**
  The classifier call is inserted on the no-orders branch; serving it from a separate
  `mail_kind=` queue keeps it from shifting the positional extract/customer/item answers every
  other test consumes. An UNSCRIPTED `mail_kind` call raises WITHOUT touching the positional
  queue — and `mail_kind.classify` catches it → `None` → ask — so a test that expects no
  discard (a veto case, a manual rule, shadow) needs to script nothing AND can assert
  `"mail_kind" not in client.asked` to prove the classifier was short-circuited.

- **Rollout switch `ai_not_order_discard` (config option, default FALSE = DRY-RUN).** With it
  off, the WHOLE gate still runs on every no-order mail, but the mail still goes to the
  warehouse question and the `_finish` review-event outcome carries `AI by zahodilo (...)` (a
  `reject_reason` suffix) — that dry-run trace is the `✅ Výstup` evidence the owner compares
  against the sklad's real answers for ~a week before flipping it on. Flip = a deliberate
  operator decision, never automatic.

- **NEVER touch `extract.py` / `ORDER_SCHEMA` for this — the classifier is a SEPARATE call.**
  Changing `ORDER_SCHEMA` changes the extraction prompt/schema hash and forces a `--live`
  re-record of the whole e2e-orders corpus (`orders-corpus.md`). The classifier is gated
  `if not shadow`, so the corpus/replay (forced-shadow) makes ZERO classifier calls and stays
  byte-identical. Test: `test_shadow_never_calls_the_classifier`.

- **A discard reached via the STATIC fallback must survive the static tick's terminal
  UPDATE — the #342 class, one engine over (review 🔴1).** `static_worker.tick`'s "mark
  processed" UPDATE re-stamps `processed_by=CATEGORY` after `run_live`; when the unparseable
  mail fell through `_fallback_to_ai` → `pipeline.run` → `_discard_no_processing`
  (`processed_by='ai-not-order'`), that re-stamp silently overwrote the discard's attribution
  and hid it from the "Zahodené AI" tab. The fix is the SAME `AND processed = false` guard
  `worker.tick` already carries for the promo carve-out (#342) — any engine that terminalizes
  a message inside `pipeline.run` and then has its OWN "mark processed" UPDATE needs this guard.
- **A CHANGE REQUEST is never discarded — gate on `not extracted.isChangeRequest` BEFORE the
  classifier, not just on the `other` verdict (review 🔴2, design B.1).** A change to an
  already-placed order can extract `orders == []` yet must always reach a human; the classifier
  is short-circuited for it (never even called).
- **Restore endpoint + the "Zahodené AI (14 dní)" dashboard tab.** `POST
  /api/message/<id>/restore` restores `category = COALESCE(original_category, category)`,
  `processed=false`, re-queues, and writes the `stage='restore'` event the loop guard reads —
  scoped `WHERE category='no_processing'` (404 otherwise, so it can never disturb a live
  message). The list is keyed on the STABLE `processed_by='ai-not-order'` marker (never a text
  `LIKE`), so promo-filtered / hand-reclassified `no_processing` mails never appear. Adding the
  two routes required updating `EXPECTED_ROUTES` AND re-pinning the DASH_HTML sha256 (with
  `# airuleset:secret-ok`, a page-hash not a credential) in `test_httpapi_characterization.py`
  — the same re-pin discipline as `board-line-edit.md`.
