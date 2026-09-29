---
paths:
  - "email-extractor/app/orders/codex_orders.py"
  - "email-extractor/app/orders/codex_cards.py"
  - "email-extractor/tools/codex_orders_push.py"
  - "email-extractor/tools/codex_cards_push.py"
  - "email-extractor/tools/systemd/**"
  - "email-extractor/app/httpapi_codex.py"
  - "email-extractor/tests/test_codex_orders.py"
  - "email-extractor/tests/test_codex_orders_push.py"
  - "email-extractor/tests/test_codex_cards.py"
  - "email-extractor/tests/test_codex_cards_push.py"
  - "email-extractor/tests/test_dl_codex_hold.py"
  - "email-extractor/tests/test_board_codex.py"
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

## How the push tools are installed on dev2 (not in the add-on image)

Both tools run on **dev2** (the box that owns `/var/lib/codex-bridge/codex.duckdb`) as plain
copies next to each other, with SYSTEM systemd units (`/etc/systemd/system/`), user `newlevel`,
system `/usr/bin/python3` (it has `duckdb` + `requests`), and ONE shared mode-600
`EnvironmentFile=/home/newlevel/.secrets/codex-orders-push.env` (CODEX_PUSH_URL / _TOKEN /
_DAYS / CODEX_DUCKDB_PATH — inspect it with `airuleset.py secret inspect`, never `cat`):

| tool | copy | units | schedule (Europe/Prague) |
|---|---|---|---|
| `codex_orders_push.py` (#342) | `/home/newlevel/codex-orders-push/` | `codex-orders-push.{service,timer}` (not in git) | 14:40 / 18:25 |
| `codex_cards_push.py` (#467) | same dir | `email-extractor/tools/systemd/codex-cards-push.{service,timer}` | 14:42 / 18:27 |

(Re)install after a change: `cp email-extractor/tools/codex_cards_push.py
/home/newlevel/codex-orders-push/` + `sudo cp email-extractor/tools/systemd/codex-cards-push.*
/etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now
codex-cards-push.timer`; run once by hand with `sudo systemctl start codex-cards-push.service`
and read `journalctl -u codex-cards-push.service -n 5` (`pushed: fetched=… codes=…`). A
`--dry-run` (`/home/newlevel/codex-orders-push/run.sh`-style env + `--dry-run`) counts without
POSTing. The add-on image never contains `tools/` (Dockerfile copies `app/` only).

## The CODEX stock-card list + the card-code check (#467)

CODEX rejects a WHOLE delivery-note import when ONE DESADV line carries an EAN kód no stock card
has (DL 126049732 stuck in `in_DL` on code 3698, which card 27 carried only 24.-28.9.; the
board's „➕ Nová karta" had accepted it). `codex_cards_push.py` sends every `raw.sm002` row
with `NEANKOD > 0` (grouped per code × card × stredisko × sklad, ~6k rows / ~2.7k codes, ~1 MB,
ONE POST) + `source_as_of` = `max(meta.etl_runs.finished_at WHERE table_name='sm002')`
(naive UTC). `POST /api/codex/cards` → `codex_cards.replace_cards` REPLACES the table in one
transaction (a code that left CODEX leaves here too — a merge/upsert would keep 3698 "valid"
forever). Reusable rules:

- **`NEANKOD` is a DOUBLE** — compare codes only through `codex_cards.normalize_code`
  (canonical integer text, strips `.0` + leading zeros); the push tool mirrors it (`_code`) since
  it runs standalone on dev2 and cannot import the add-on.
- **"Exists" = NEANKOD on ANY pushed row** (any stredisko/sklad, active or inactive). Measured
  2026-09-29: 480 of our 483 DL cards match that way (477 on the same sklad as our card's
  `sklad`); a per-sklad rule would falsely hold cards whose NEANKOD sits on another sklad row.
  NEANKOD itself is not unique (17 codes on 2+ cards) and one card carries it only on SOME of its
  sklad rows (card 27: sklad 1/1 yes, 4 and 600 no).
- **Fail OPEN, never closed**: `codex_cards.live_guard` returns None (checks OFF, `log.warning`)
  when nothing was ever pushed or the CODEX data is older than `STALE_HOURS = 30` (ETL 14:15 /
  18:00 → longest normal age ~20.5 h; one missed slot tolerated). `stale_sweep` (worker tick,
  `if dl_python:`) enqueues ONE ops alert per stale episode (`pending_alerts` kind
  `codex_cards_stale`, key `codex-cards:<as_of>`, `reminder_suppressed` cadence: the first alert
  of an episode at once, reminders once per workday morning); a never-pushed list gets the same
  30 h grace from the revision-17 `schema_version.applied_at` (no alert right after a deploy).
- **Shrink guard**: an empty push → 400, a push with < 50 % of the previous codes → 409 (a
  half-loaded ETL would otherwise hold every DL); `?force=1` for a genuine mass removal.
- **Where it bites** — all LIVE only (shadow / the e2e-dl corpus pass no guard, byte-identical):
  - `dl_match.decide_item(codex=)`: a model pick of a code CODEX lacks is nulled like a #245
    overflow (rule `codex_missing`, code in the note), a remembered one is never rescued, but a
    remembered VALID card still rescues — that is what makes the sklad's answer ship on the
    reprocess instead of looping on the model's (still sure) invalid pick.
  - `_process_document` drops dead codes from `catalog_gtins`, so a human answer that taught
    3698 neither rescues nor blocks re-asking (`ask_dl_item`'s human-taught pre-check).
  - the dl_item question offers ONLY CODEX cards, ranked by the better of our name and the
    CODEX name (`codex_cards.question_candidates`) — the right card surfaces even under a stale
    name — and carries `codex_name` for the board; the doc is HELD with `_codex_hold_reason`
    (which also says to delete the dead card on Produkty sklad).
  - **the codex question shares the #465 question-row mechanics** (`payload.codex_missing`,
    `dl_item_conflict.board_settled`): its answer supersedes the human answer that taught the
    dead code (undo restores it) and a deduped older plain question is upgraded
    (`flag_question(flag=, keep=codex.has)`: CODEX cards first, dead ones dropped, new reason).
    It is deliberately **NOT a standing confirmation** in `dl_memory._board_confirmed` (review 2
    🟡, probe-reproduced: that made a misclick on a codex question ship silently on every later
    delivery) — only the reprocess of the answered message trusts it. The drifted-name case
    (our name shares no word with the wording) is solved instead by the CODEX name counting for
    the R73 lexical plausibility (`_memory_conflict(alt_name=codex.name_for(...))`).
  - board: `check_card_code` → 409 `{error, codex:{code, missing, as_of, similar:[{code, name,
    in_catalog, catalog_gtin?, catalog_name?}]}}` on Produkty sklad create/edit, the inline
    „➕ Nová karta", any dl_item pick (checked BEFORE a free/search pick is legitimised — a
    refused dead code never lingers as an offered button), and legacy
    `POST /api/znalosti/dl-products`. The one-click pick sends `catalog_gtin` (OUR exact
    number), never the normalized CODEX code. „Nová karta" with a number we ALREADY have →
    409 `existing` (it used to UPSERT with blank mass/sklad/cena); with a number of a card
    deleted to the Kôš → 409 „obnov ju na záložke Kôš"; Produkty „Nová karta" sends
    `new: true` → `CardExists` 409 in BOTH scopes (the orders form would clear the alias).
  - the questions tab keeps a refusal hint in `state.codexHints` and re-renders it on every
    refresh — a hint never freezes the 8 s refresh (only an open inline form does).
  - `/api/codex/cards` takes the token from the `X-Token` header ONLY (constant-time compare),
    refuses a body > 16 MB (413); a refused push is `log.warning`ed; the data age is
    `min(source_as_of, synced_at)` (a future push time can never pin the list fresh); the
    stale ops alert keys on the stuck snapshot (`codex-cards:<as_of>`) — one episode, one
    first alert at once + morning reminders.
- **A card created in CODEX this morning is refused until the next ETL+push** — by design the
  refusal text says the list is as of X and refreshes ~14:45 / ~18:30; never add a bypass.
- **Name drift** (`codex_cards.name_key`: fold + `gr`→`g` + word ORDER ignored + 1-letter words
  dropped) — on 2026-09-29 63 of 480 cards differed by plain fold, most cosmetic; the key keeps
  real renames (e.g. „Bagetka s kečupom…" vs CODEX „Rožok so slaninou…"). Fix a drifted name via
  Produkty sklad („Prevziať názov z CODEXu" → Uložiť) — the app path, audited, never SQL.
- **Live check** (dev2, read-only): `SELECT count(*), count(DISTINCT code) FROM
  codex_stock_cards` + `SELECT * FROM codex_card_syncs ORDER BY id DESC LIMIT 1` on the add-on
  DB; the Produkty sklad toolbar shows „Karty z CODEXu: stav k …".
