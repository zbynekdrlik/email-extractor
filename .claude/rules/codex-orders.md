---
paths:
  - "email-extractor/app/orders/codex_orders.py"
  - "email-extractor/tools/codex_orders_push.py"
  - "email-extractor/app/httpapi_codex.py"
  - "email-extractor/tests/test_codex_orders.py"
  - "email-extractor/tests/test_codex_orders_push.py"
---

# CODEX order evidence + the auto-resolve sweep (#342)

The warehouse enters every order into CODEX by hand; `tools/codex_orders_push.py` (dev-box
systemd timer) reads those headers from the codex-bridge DuckDB read-only and POSTs them to
`POST /api/codex/orders`; the worker sweep (`codex_orders.resolve_mail_questions`) uses them
to neutrally close open `mail`-kind board questions. Read this before touching any of it.

## The codex-bridge DuckDB — join shape that DOESN'T explode, verified live (2026-08-18)

`/var/lib/codex-bridge/codex.duckdb` (on dev2, read-only: `duckdb.connect(path,
read_only=True)`). The customer identity mapping and a real fanout trap:

- **`raw.firma.NICO` is NOT unique** (1708 rows / 814 distinct NICO — branches share a NICO),
  and **`raw.sp002.ICDOBJEDNAV` is not unique either** (81753 / 81051). A naive
  `sp002 JOIN firma ON NICO` over a 7-day window blew 1088 distinct orders up to **103158
  rows**. Always dedup BOTH sides first: `GROUP BY ICDOBJEDNAV` on sp002 (`ANY_VALUE(NICO)`,
  `MAX(DATVYST)`), and a `firm` CTE `GROUP BY NICO` picking `MAX(AEDIEAN)` — see
  `codex_orders_push._SQL` for the exact working query.
- **The customer identity bridge is NICO → AEDIEAN via `raw.firma`.** `AEDIEAN` (VARCHAR,
  13-digit EDI EAN, 1497/1708 populated) is the SAME value the add-on's own customer cards
  carry (`customer_snapshot.ean_edi`) — so the push stores `customer_ean` and the sweep
  matches on an exact string. There is NO IČO column in the add-on's tables; do NOT try to
  bridge on IČO. 24/814 NICO have conflicting AEDIEAN (multi-branch) — `MAX` picks one, so a
  rare branch order may not match: a SAFE miss (question stays open), never a wrong-close.
- **Order-header columns:** `ICDOBJEDNAV` order number, `NICO` customer number, `DATVYST`
  issue date (~97%), `DATDODAV` actual delivery date (~95%). **NEVER `DODTERMIN`** (~22%).
  Line aggregate from `meta.sp003_dedup` (join on `ICDOBJEDNAV`), NOT `raw.sp003` directly.
- Parameterized lookback interval in DuckDB: `current_date - (CAST(? AS INTEGER) * INTERVAL 1
  DAY)` works; a bare `INTERVAL (?) DAY` bind does not.

## Neutrally closing a `mail`-kind question WITHOUT teaching a rule (#341 safety)

A `mail` question's two normal answers (`not_order`/`manual`) BOTH write a permanent
`mail_rules(sender_norm, subject_key, action)` row via `teach._apply_mail` — applied to every
future mail of that shape from the sender. So an AUTO close must NEVER go through
`teach.KINDS['mail'].apply`. The neutral shape (`codex_orders._close_mail_question`), reusable
for any future auto-close of this kind:
1. guarded `UPDATE order_questions SET status='answered', answer=%s, answered_by='codex-auto'
   WHERE id=%s AND status='open' RETURNING id` — a concurrent human answer wins (0 rows → do
   nothing, return False; #323 pattern);
2. `UPDATE messages SET processed=true, processing_at=NULL` — or the message is re-claimed and
   the question re-asks forever (#307);
3. an honest rollup `report.log_event(status='review', ...)` — never an ok/upload event;
4. ZERO writes to `mail_rules` or any teach/memory table.
The connection is autocommit (`db.connect`), so (1) commits before (2)/(3) with no explicit tx.

## `teach.KINDS['mail'].apply` does NOT set `order_questions.status` — the httpapi wrapper does

`_apply_mail` writes the `mail_rules` row + marks the message processed + logs the event, but
the `status='answered'` transition lives in `httpapi_orders_questions._api_orders_answer_generic`
(a guarded UPDATE), NOT inside `.apply`. So a test that calls `teach.KINDS['mail'].apply(...)`
directly and then asserts the question is `answered` will FAIL (it stays `open`). To simulate a
human answer in a test, write the status transition directly:
`UPDATE order_questions SET status='answered', answered_by='sklad', answered_at=now() WHERE id=%s`.

## "Under which card did the warehouse book supplier item X?" — príjemky live in `raw.sp001`,
## and `sm002.NEANKOD` EQUALS the add-on's dl-catalog gtin/candidate value (verified 2026-08-20)

The reusable lookup that resolved the "Múka zytnia typ 720" dl_item question (#87 on the board):
supplier mails → our EDI ships partial → warehouse adds the missing line by hand in CODEX →
the manual line reveals the card they actually use. Steps, all read-only:
1. Supplier NICO: `raw.firma` by name OR by `AEDIEAN` = the add-on's `supplier_ean` (DUOPACK
   47977892 ↔ 2000000000655). HK LOAN-style trading names may not exist in firma at all —
   search by the EDI EAN first.
2. Receipt lines: `raw.sp001 WHERE NICO=<ico> AND SDPOH IN (10,12,14)` (10=nákup na faktúru;
   see `meta.pohyb_codes`). `NCDLIST` = the príjemka doc number, `ACSKLP` = card code,
   `AMATERNS` = card name, `UDATUMAKT` = when the operator entered it (`DDATUCT` is an
   accounting-batch date, unusable for orientation; `NMNOZ` quantities are BE-double garbage).
3. Card → catalog: `raw.sm002 WHERE ACSKLP=<code>` → `NEANKOD` is EXACTLY the value the
   add-on's `dl_catalog_snapshot.gtin` / board-question candidate `value` carries (100005
   "T 930 - ražná múka" → NEANKOD 1571 = candidate value "1571"). Prove the mapping on ≥2
   deliveries (a receipt without the item must lack the card line) before teaching it.
4. Teach via the app's OWN path, never SQL: `POST /login` (dash password) →
   `POST /api/orders/question/<qid>/answer` `{"choice":"<value>"}` — writes `dl_item_memory`
   (source=human) + `release_for_question`; an already-uploaded doc re-resolves as
   `duplicate` (#239 claim guard), so no double-ship — the taught mapping applies from the
   NEXT delivery.

## The push tool stays CI-testable without duckdb/requests

`tools/codex_orders_push.py` lazy-imports `duckdb`/`requests` INSIDE `query_duckdb`/
`_requests_post` only, and `run(query=..., poster=...)` is a DI seam — tests feed synthetic rows
and capture the POST (`build_orders` is the pure normalization core). Keep that shape for any
future addition; never import duckdb/requests at module top. The token comes from an
`EnvironmentFile` (`CODEX_PUSH_TOKEN`), never committed.
