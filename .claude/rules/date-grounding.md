---
paths:
  - "email-extractor/app/orders/extract.py"
  - "email-extractor/app/orders/pipeline.py"
  - "email-extractor/tests/test_orders_extract.py"
---

# Delivery-date grounding & the typo/invented-date HOLD (#163 → #420)

The delivery date the model returns is checked against what the mail actually WROTE.
`extract._days_in()` collects every written `(day, month)` (strict dotted `_SUBJ_DAY` +
announced-undotted `_ANNOUNCED_DAY`); `_range_days()` adds every day spanned by an explicit
range. `extract.date_ground_conflict(date_str, source)` is the single decision:

- `None` (**grounded**, accept as before) when the date is unparseable, when the source names
  NO explicit day/range at all (ordinary relative-date order — "na pondelok", "zajtra"), or
  when the extracted date matches a written day/range.
- a **non-empty set of written `(day, month)`** ONLY in the #163 shape: the text DID name
  explicit day(s) and the returned date matches NONE of them.

`date_grounded()` is a thin bool wrapper (`date_ground_conflict(...) is None`) — its
semantics are UNCHANGED, so don't re-add a grounding check elsewhere.

## #420: a conflict is HELD + asked, NEVER dropped

The #163 shape is BOTH a model-invented date (msg 5679: text "25.7.", model "08.08." —
nowhere in the mail) AND a corrected customer typo at a stated weekday (msg 11059: body
"na pondelok 14.8." — 14.8.2026 is a Friday, so the model correctly re-dates to the next
Monday-the-14th, 14.09.2026). #163's silent DROP lost the typo case entirely →
`orders=[]` → "(nezistený zákazník)" (13× in 60 days). So:

- `extract.run()` KEEPS the conflicting order and sets
  `result["date_conflict"] = {written, model_dates, candidates, reason}` (it does NOT add
  the old "Dátum dodania sa nenašiel" note any more).
- `pipeline._run()` folds `extracted["date_conflict"]` into the SAME existing subject/body
  `date_conflict` HOLD path (before product matching): customer + items resolve normally,
  the order is HELD, and `teach.ask_date` raises a `date` board question with the candidates.
  The grounding reason takes precedence over a subject/body reason when both fire, so the
  question wording, candidates and summary note stay consistent. Shadow stays `review`.
- Answering ships via the existing `teach.KINDS['date']` → `hold.set_delivery_date` →
  `hold.release_for_question`; the free "iný dátum" input is already accepted by
  `_validate_date`. Nothing new was built — this only ROUTES the case into machinery #164
  already had.

## Candidate computation (`date_conflict_candidates`)

Order: (1) the next-FUTURE occurrence of each written day.month, **soonest first** (chronological,
NOT day-major); (2) the model's own date; (3) mail+7 days. Deduped; written-derived
candidates capped at 6 (a written RANGE could otherwise produce dozens of buttons).
`_next_future_day_month(d, m, ref)` keeps the written MONTH when it is still ahead (20.12. →
20.12.), else walks forward to the next month containing that day (14.8. in September → 14.9.)
— the typo is almost always in the month, the day is right; never "next year's exact 14.8."
Junk day.month the strict scan can produce ("verzia 1.13.") is range-filtered (1–31 / 1–12)
in the candidate builder.

## Corpus (mandatory)

A conflict case is a `should_review: true` (+ optional `notes_contains`) corpus case: in
shadow the branch returns `review` (no ship). ORDER_SCHEMA and `prompts/extract.md` are
unchanged by this feature, so the 30+ existing cache entries stay valid — only the new case
needs its own `--live` cache entry (see `orders-corpus.md`). Regression coverage: the RED test
must assert the order is KEPT + `date_conflict` present (not `orders == []`); pin the full
candidate list on a fixture where the model date differs from the next-future written date,
or ordering is untested (both #420 fixtures where model == next-future left it vacuous).
