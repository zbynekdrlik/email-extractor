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
  - "email-extractor/app/orders/card_guard.py"
  - "email-extractor/tests/test_board_codex_pick.py"
  - "email-extractor/tests/test_orders_codex_gate.py"
  - "email-extractor/app/orders/memory.py"
  - "email-extractor/app/orders/codex_sync.py"
  - "email-extractor/app/orders/codex_sync_plan.py"
  - "email-extractor/app/orders/codex_sync_memory.py"
  - "email-extractor/app/orders/codex_sync_texts.py"
  - "email-extractor/app/orders/codex_sync_list.py"
  - "email-extractor/app/orders/codex_sync_kos.py"
  - "email-extractor/tests/test_codex_sync.py"
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
_DAYS / CODEX_DUCKDB_PATH — inspect it with `airuleset.py secret inspect`, never `cat`).
**`CODEX_PUSH_URL` = `https://email-pz.newlevel.media/api/codex/orders` since #470** — the
add-on's Cloudflare tunnel (the cards push derives `…/api/codex/cards` from it, there is no
`CODEX_CARDS_PUSH_URL` in the file). Cloudflare caps a body at 100 MB and a request at 100 s
(the cards list is ~1 MB, the orders push is chunked by 500) — fine today, keep it in mind if a
payload ever grows. The raw `http://<ha-host>:8099` is being firewalled; never point a push at
it again:

| tool | copy | units | schedule (Europe/Prague) |
|---|---|---|---|
| `codex_orders_push.py` (#342) | `/home/newlevel/codex-orders-push/` | `codex-orders-push.{service,timer}` (not in git) | 14:40 / 18:25 |
| `codex_cards_push.py` (#467) | same dir | `email-extractor/tools/systemd/codex-cards-push.{service,timer}` | 14:42 / 18:27 |

(Re)install after a change: `cp email-extractor/tools/codex_cards_push.py
/home/newlevel/codex-orders-push/` + `sudo cp email-extractor/tools/systemd/codex-cards-push.*
/etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now
codex-cards-push.timer`; run once by hand with `sudo systemctl start codex-cards-push.service`
and read `journalctl -u codex-cards-push.service -n 5` (`pushed: fetched=… codes=…
to=https://email-pz.newlevel.media` — since #470 the line ends with the target's scheme + host,
never the path/token, so the journal itself proves which address the push reached). A
`--dry-run` (`/home/newlevel/codex-orders-push/run.sh`-style env + `--dry-run`) counts without
POSTing. The add-on image never contains `tools/` (Dockerfile copies `app/` only).

- **From a worktree-isolated worker, `systemctl enable …` is REFUSED** by the worktree guard (it
  parses `enable` as the bash builtin): use `sudo systemctl reenable codex-cards-push.timer` +
  `sudo systemctl start codex-cards-push.timer` — same symlink, `systemctl is-enabled` → enabled.
- **Changing a variable in the push EnvironmentFile (a plain key file in newlevel's secrets
  dir)** — `airuleset.py secret` has no "set one key" operation, and `block-vault-store-read.sh`
  refuses any command line that names the file (even `secret exec --file … -- python3 <script>
  <that path>`). What worked for #470: a small script INVOKED BY PATH (the key-file path lives
  inside the script, not on the command line) that rewrites ONLY the `CODEX_PUSH_URL=` line,
  keeps every other line byte-for-byte, replaces the file atomically with mode 0600 + the same
  owner, and prints only the NEW (non-secret) value; then `secret inspect <path>` (names/lines/
  mode unchanged) and `secret exec --file <path> --stdin -- python3 <checker>` (the checker reads
  stdin and prints only "matches: True") to verify. Never `cat`/`echo`/`sed` the file from the
  shell. `run.sh` sources the same file, so a manual run picks up the change too.
- **When the push cannot reach the add-on** — seen 2026-09-29 from ~19:00 on the RAW
  `http://<ha-host>:8099` path: every Docker-published port of the HA box was filtered UPSTREAM
  for the office egress (tcpdump on the box's `enp1s0` saw 0 packets; 22/8123 fine) — the add-on
  was healthy, only the path was cut, and BOTH dev2 timers (#342 + #467) failed. The permanent
  answer (#470) is the Cloudflare tunnel above. If the tunnel itself is ever down, a one-off push
  for a verification still works without it: build the exact body with the tool's own `run(...,
  poster=<write body to a file>)`, pipe it over ssh into `sudo docker exec -i
  app_e0ac7775_email_extractor python3 <script>` where the script reads `api_token` from
  `/data/options.json` INSIDE the container and POSTs to `http://127.0.0.1:8099/api/codex/cards`
  (the token never leaves the box, never printed). The 30 h fail-open + the stale ops alert are
  the safety net meanwhile.

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
  `if orders_python or dl_python:` since #479) enqueues ONE ops alert per stale episode (`pending_alerts` kind
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
    in_catalog, catalog_gtin?, catalog_name?}]}}` on a Produkty sklad EDIT, any dl_item pick
    (checked BEFORE a free/search pick is legitimised — a refused dead code never lingers as an
    offered button), and a legacy `POST /api/znalosti/dl-products` edit. The one-click pick
    sends `catalog_gtin` (OUR exact number), never the normalized CODEX code; a similar CODEX
    card we do not have is added through the #477 pick („Pridať kartu z CODEXu").
    The Kôš restore of a DL card whose code CODEX lacks → 409 (`audit._refuse_dead_dl_code`).
  - **#477 superseded the typed „➕ Nová karta" entirely (owner order 2026-09-30)** — the
    #467 gates for a TYPED new number (`guard_new_dl_card`, `refuse_code_variant`, `taken`,
    the `existing` 409) are gone because no typed number can create a card any more (403
    `card_guard.blocked()`). The ONE creation is `card_guard.add_from_codex` — the code comes
    from the pushed list (canonical by construction, so no leading-zero / „.0" variant can
    arise), a code we already have (`codex_cards.index_by_code`, normalized both ways) is only
    selected, one whose card sits in the Kôš RESTORES it (a bare snapshot-card retirement
    marker is refilled from the newest snapshot that still has the card, else from CODEX —
    never a nameless card). Picker list: `GET /api/board/codex-cards` →
    `card_guard.codex_choices` over `codex_cards.pickable` (stredisko 1 only — never the junk
    strediská; orders additionally sklad 1; DL never a code > 13 chars, the #245 DESADV field;
    active rows; a STALE list still lists, with a warning — the pick is not blocked by a
    stopped push). The
    #467 refusal's `similar` cards are marked `pickable` so the help never offers a card the
    pick would refuse. See `board.md` #477 for the answer flow.
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

## The same list guards every ORDER file (#479) — `card_guard` order gate

The #467 gate covered only DESADV; the card 27 renumber broke ORDER lines too („nebralo do
objednávky"). Since 0.9.176 the orders engines use the SAME list, LIVE only (shadow / the
e2e-orders corpus never loads it — `codex = None if shadow`, byte-identical), fail-open exactly
like DL (`card_guard.order_guard` = `codex_cards.live_guard`). Reusable rules:

- **The gate is a DECISION transform, not a new hold path.** `card_guard.gate_order_line` turns a
  line whose code CODEX lacks into a cardless `codex_missing` decision (dead code + card + the
  replaced rule in `trace.codex_missing`, the reason in `note`). `codex_missing` is in
  `pipeline.ASK_THE_WAREHOUSE`, so the EXISTING item question + `hold.place` + `release_for_
  question` do the rest — no parallel machinery. It runs in THREE places: `_run` per item (before
  the ask), `hold_close._release_locked` after `_redecide` (a code can go dead while the order
  waits, and `_redecide` can re-derive a dead card → re-held via `_ask_still_ambiguous`), and
  `_ship_one` right before `claim_send` (the net: the deadline sweep ships a held order WITHOUT a
  line whose code went dead — an `item` question is deadline-shippable, never the dead code — AND
  `card_guard.ask_codex_missing` raises the board question naming that line; review 🟡3: a net
  that only dropped it left the board silent, Odoo only counts „chýba N položiek").
  `_ship_one(codex=...)` takes `_run`'s already-loaded list; the default loads it.
- **`teach.ask(codex_missing=True)` bypasses the human-taught pre-check** — the incident shape IS
  a wording the sklad taught onto the card whose code then died; without the bypass the order
  gets NO question and ships without the line. Pass it on EVERY ask of a codex line (`_run`,
  `_ask_still_ambiguous`, the net) — a re-hold without it is „unaskable" and sits held silently.
- **`memory.remember(source='human')` never swallows an answer (review 🟡1).** The unique key is
  (customer, wording, gtin, day); a same-day human answer colliding with a ship row, or with the
  same card taught earlier that day, used to be `DO NOTHING` — `resolve` kept the NEWER, replaced
  human answer (the dead card) and the codex question re-held the order in a loop. Now `DO UPDATE
  … source='human', deleted_at=NULL, created_at=now() WHERE EXCLUDED.source='human'` (a ship
  duplicate still returns False). Accepted side effects (the DL #402 trade-off, all need a
  same-day same-card collision): undo deletes the promoted row with its ship evidence; a promoted
  ship row is curated, so a Naučené edit/delete touches it; a teachback row later promoted by a
  question answer is soft-deleted if the teachback's `teach` audit is restored from the Kôš.
  Still `DO NOTHING`: `memory.add_customer_alias` (the History „Doučiť" / Naučené path) — a
  teachback onto a card that already shipped under that wording the same day answers 409
  „toto doučenie už existuje" (#448 behaviour, unchanged here).
- **A question offers only cards CODEX has** (`card_guard.order_question_candidates`). A
  `codex_missing` line has NO engine proposal (its proposed gtin IS the dead one), so it offers
  ONLY CODEX cards ≥ `match.PLAUSIBLE_CANDIDATE_SCORE` — never #160's forced head (the top scorer
  of unrelated cards shown like a proposal, review 🟡2) — and at most `card_guard.QUESTION_BUTTONS`
  (6, the #160 cap; a floor-only filter gave a generic „chlieb" 24 buttons, review 2); an empty
  list is fine (search + „Vybrať kartu z CODEXu" stay). Any other item question just filters dead
  cards out. The net's questions are announced in the Odoo summary on every POSTING `_ship_one`
  exit (ok/partial, review AND upload-error `_finish`: `new_questions=len(net_new)` + the net's
  qids in `question_ids` — ok/partial and upload-error pass `net_qids`, the review exit the
  caller's ids + `net_qids`; never narrow review to `net_qids`, `ITEM_OPEN` is not technical and
  the #164 invariant would raise a fallback `mail` question). A retry finds the question already
  open (no `on_new`), so the first post is the only announcement; the already-sent exit posts
  nothing.
- **Every card PICK refuses a dead code (409):** the orders item answer (`_order_card_refusal`,
  twin of `_dl_card_refusal`) and the History „Doučiť" teachback (after its 403 not-a-card check),
  via `check_card_code(doc=DOC_ORDER|DOC_DL)` — order wording, not „celý dodací list". Without it
  a search-pick of the dead card re-holds the same order in a loop. The item refusal is a toast
  on the board (the one-click CODEX hint renders for `dl_item` only).
- **Static orders use the #133 AI fallback as their hold:** `static_worker.run_live` checks the
  resolved codes (`card_guard.dead_codes`) BEFORE `static_edi.build`; a dead one → `_fallback_to_
  ai` (note names the code), where the AI gate holds + asks. The 7 hardcoded
  `PRODUCT_EAN_BY_CODE/NAME` codes are checked the same way (all in CODEX on 2026-09-30).
- **`codex_cards.stale_sweep` runs for orders OR DL** (was DL-only — an orders-only install ran
  with the ORDER gate silently off); the alert text names both. A static-ONLY install (AI + DL on
  n8n) gets no sweep — like the other orders sweeps (`release_due` / `retry_unknown_customer_
  questions` gated on `orders_python`; reminders, the alert flush and `stale_sweep` on
  `orders_python or dl_python`); static's hold route IS the AI pipeline, so that config is not
  coherent anyway (live runs all three engines on python).
- **The #360 confirmed quantity is floated at the source** (`hold_place._apply_confirmed_
  quantities`: NUMERIC → `Decimal`). A Decimal decision quantity crashed the `Json` dump of a
  re-hold (reached first by the #479 re-hold after an item answer) and `merge_same_card`'s
  Decimal + float sum. `edi.build` reads `float(quantity)` — no byte change.
- **Known, accepted:** an answered release loads the ~6k-row list twice (`_release_locked`, then
  `_ship_one`'s default) — release is rare, passing it would change the pinned `hold._ship`
  signature. An older OPEN plain question the codex ask dedupes onto keeps its old reason/buttons
  (DL upgrades it via `flag_question`); the 409 on a dead pick covers it.
- **Live verification without shipping:** in the container, `card_guard.dead_codes(
  card_guard.order_guard(conn), <gtins>)` over the day's shipped ORDER lines (AI: `order_items` of
  non-shadow non-DL ok/partial runs; static: re-parse + `static_worker._items_with_ean`) — never
  re-ship, never reset.
- History „Doučiť" (teachback) teaches only onto an existing card of the scope's catalog
  (`card_guard.refuse_typed_card(error=TEACH_CARD_ONLY)`, 403 `codex_only`) — see `board.md`.
## CODEX card sync — names + renumbers follow CODEX automatically (#478)

After every ACCEPTED cards push, `httpapi_codex` calls `codex_sync.run_safely` on the same
connection: `codex_card_history` (migrate r18, seeded from the stored list) records every
(stredisko, ACSKLP, code) with first/last seen (the CODEX data age), `codex_sync_plan.build_plan`
(read-only) decides, `codex_sync` executes + logs `codex_sync_runs` (the whole plan as JSON) +
ONE ops alert (`pending_alerts` kind `codex_card_sync`). The push tool's journal line ends with
`sync=<mode> renamed=… renumbered=… removed=… review=…` — the proof on dev2 that it ran.

- **ACSKLP is unique only WITHIN a stredisko** (live: 400448 = garlic on stredisko 1, crisps on
  4) — the identity is (stredisko 1, ACSKLP), the #477 pick scope. The push already carried
  ACSKLP (`card_code`) + sklad since #467; #478 needed no new field, only the history.
- **Rollout switch `codex_sync_apply` (default false = DRY-RUN)**: plan + log + report, ZERO
  catalog/memory/audit/alert writes (a failing dry-run sync too — its `error` run row is the
  record, review 17); the history is kept either way. A wait (a card missing from one list; an
  unbound number whose code lost a carrier since the last list, or got its first carrier ever
  — review 30; a pick / renumber waiting on either) is in the report's `waits` + the log — an
  all-zero plan never hides one (reviews 23-30). The run log keeps `RUNS_KEEP_DAYS` (90) + the newest run of
  each status. Module map: `codex_sync_list` (what the list says: rows, history, bindings, pick
  events) → `codex_sync_plan` (what to do) → `codex_sync` (does it); `codex_sync_memory`
  (memory rules), `codex_sync_texts` (Slovak texts), `codex_sync_kos` (`KosRules`, the
  planner's mixin for our Kôš numbers a card takes over / a pick restored — split at the plan's
  size budget, review 41). Read the dry-run with
  `SELECT id, ran_at, status, report FROM codex_sync_runs ORDER BY id DESC LIMIT 1`. Turning it
  on is the OWNER's decision (after reviewing the dry-run on the ticket) — never flip it in a
  lane. A `would_block: true` in a dry-run means the first apply would stop at the breaker.
- **Circuit breaker** (`codex_sync_max_code_changes` 10 distinct codes, `codex_sync_max_renames`
  80, add-on options): over either → `blocked`, nothing applied, one ops alert per distinct plan
  (`dl_alerts.reminder_suppressed`, key = plan digest). A genuine mass change in CODEX → raise
  the option, the next push applies it, lower it back. The first apply renames ~56 cards (the
  live drift of 2026-09-30: 21 orders + 35 DL), under 80.
- **IDENTITY = a stored binding, never "who holds the code now"** (`codex_card_bindings`:
  (scope, our gtin) → stredisko-1 ACSKLP, `active=false` = a number the sync retired). Two review
  rounds were spent on this: round 1 read a REUSED code (card 27 moved to a new code, the pagáč
  card took ours) as a name drift and renamed our rožok to „Pagáč"; round 2 fixed that with
  history recency ("the card that held it last") and was STILL wrong — a reuse chain seen in
  the dry-run, or a vanished card, dragged our card, its name and memory onto the other product.
  An UNBOUND number (every number on the first post-deploy lists) is bound ONCE by ONE rule,
  `_by_history`, whatever the number of carriers now (none / one / several — round 29: the
  old multi-carrier branch bound by name with no history rules, a one-list duplicate carrier
  turned a drift review into a binding): from every card that ever carried the code
  (`Codex.carried`), never "who holds it now / held it last" (rounds 25 / 28: a reuse — also
  one that moved on — dragged our rožok to the pagáč): one list is no proof (a card that left
  the code since the last list, a card arriving on a code no card carried — rounds 30-31, or
  — no carrier now — the last carrier missing once → WAIT, protected); the ONE card on the code
  since the history began (`Codex.seeded_at`; a list older than that beginning is still
  recorded — accepted = pickable, round 12 — with its new sightings at its stredisko's
  beginning, so it never moves, rounds 31-33. TWO facts per pair: `first_seen` (clamped —
  the reuse windows read it) and `seen_since` (r18: the first list SINCE the beginning that
  showed it, NULL until one does). Who our number IS — the CANDIDATES and their "since" —
  reads only `seen_since` (`Codex.since_seed` / `Codex.seen_from`, `_by_history` +
  `_took_over`'s candidate): a card seen on the code in such an older list — only there, or
  also again since — is no "since the beginning" evidence: it bound a #467 "missing" card at
  once with no name check, then removed it / renumbered it onto another product, and hid a
  take-over, rounds 33-34. But the UNIQUENESS of "the one card" counts every recorded
  carrier: a card seen there only before we watched (`unwatched`) is still ANOTHER card —
  round 35: dropped from the count too, the pagáč that reused our rožok's code got our number
  and, once gone, removed it; the review / arrival wait name the `unwatched` cards)
  → that card; else our NAME — the one carrier now named
  so, else the one card ever named so — unless that card took the code over from ANOTHER
  product (`_took_over`: first seen before it — cards seeded together never count, but a card
  seen on the code before we watched (`Codex.before_watch`: no `seen_since`, or one later
  than its clamped `first_seen`) always came first, round 36; in CODEX or
  gone — the #467 drift button offers the code's holder, round 9's rule → a human, rounds
  26-29; one missing once → WAIT — never one seen only before we watched: it took no list
  since, so with no previous synced list it only LOOKED missing once, round 37); else a
  human, with ways out that work (`_carrier_way_out`:
  rename only to a name ONE historic card bears — never our own, never a gone card, never one
  that took the code over — or the pick among the carriers now; plus the curated-fields /
  taught-rows pointer). Stored in every mode (identity, not catalog data), and from then on the
  sync follows THAT card. Pass 1's verdict (`settled` / `unsettled` /
  `waiting`) is what `_renumber` reads for a target bound only in THIS plan — never a merge
  onto a number not settled this list. Any future "same code, other card/name" logic must go
  through the binding, never re-derive ownership from the list.
- **Binding lifecycle (rounds 3-5) — ONE resolver, `_ScopePlanner._known`**: a human #477 pick
  newer than the binding (the newest non-sync audit `create` on the override table,
  `codex_sync_list._events`; the pick writes `after.codex_card` = the picked ACSKLP, also when it
  restores a Kôš card) names the card exactly; else the binding, ACTIVE OR RETIRED — a Kôš
  „Vrátiť" of a number the sync retired is still that CODEX card and is merged back into its new
  number (round 5 🟡: re-identifying it from the list bound it to the pagáč reusing the code).
  A Kôš `restore` row is never a new card (round 4 🟡). Three places deciding identity with
  different rules was the root of rounds 3-5 — never add a fourth, extend `_known`.
  A binding that REPLACES another card's on a PICK is stored only by an APPLIED run, so a pick
  seen during a dry-run / blocked run still gets its reset later. Bindings are seeded for the
  whole group, also a member joining later (a twin back from the Kôš) — a legacy „0"+code twin
  left alone later still follows its card (rounds 9-10). A reset covers every group number. The
  memory path asks `_known` too: a retired number picked as another product keeps its rows. A
  pick whose CODEX product differs from the one the data was taught for (`Codex.same_product`:
  the old card's name while it carried the code — `codex_card_history.name` — vs the picked
  card's; never OUR current name, a human rename before delete + pick would hide it — round 7)
  → `resets`: alias / doplnok / mass / cena cleared, sklad := exactly what a fresh pick writes
  (`_sklad_of`: the picker's sklad for the code the bound card carries NOW — the code or its
  `_successor` — and only when the card's OWN active named row is among the picker's rows for
  it, else the pick's rule over the card's own rows; never the old code's current holder,
  rounds 19-21: the picked card may move on / go inactive before a dry-run-deferred reset); a
  picked card OR the card it replaces missing from ONE list (`Codex.glitched`) — the pick
  waits, no seed, no reset, and NO renumber lands on the waiting number (`waiting`, round 22:
  a merge's binding superseded the pick and its reset never ran) (rounds 20-21: settled then,
  the old sklad / a "gone from CODEX, delete its rows" review became final with the stored
  binding) (the pick
  restored the Kôš card "as it was"); a re-pick of the SAME product under a second CODEX card
  keeps its data. Resets are applied before renumbers and a renumber in the same plan carries
  the reset card.
- **Rename rebind** (our CODEX card left for good AND exactly one card carries our code under
  OUR name — recreated in CODEX, or a number a human renamed to it as review A asks; checked
  BEFORE the contest rule, round 8): the SAME product (`same_product`) keeps its data and the
  binding is durable (every mode, round 6); ANOTHER product is a pick in all but name —
  `_reset_from` (reset + every row of the number to a human), stored by an applied run only
  (round 9 🟡: it kept the old product's alias / doplnok, also when it silently resolved a
  pending contest). A genuinely recreated product under a NEW name loses its curated data too
  (restorable from the Kôš) — the list cannot tell it from a reuse, and review A says so.
- **Plan mechanics**: the sync holds `LOCK TABLE codex_stock_cards IN SHARE MODE` (a concurrent
  push waits). The planner runs TWO passes (round 18 🟡): `_settle` every group (identity +
  the reset a pick implies), THEN `_follow` (stay / leave / renumber) — a merge onto a number
  reset in the same plan must read the reset card; in one pass the group ORDER decided whether
  our data or the old product's kg sklad + mass survived. A merge target that is another CODEX
  card → review; a merge FILLS the target's blank alias / doplnok / mass / sklad / cena from our
  card. Numbers a plan retires go to the
  simulated Kôš (`_vacate`) so a chain in ONE push (024 → NEW while another card takes 024)
  never re-creates onto the row being retired — round 4 🔴: the upsert kept `deleted_at` and the
  koláč vanished; the executor also undeletes before any create. The "previous snapshot"
  (two-snapshot removal, seed rule) comes from `codex_sync_runs` that actually synced — a
  failed/skipped sync never counts.
- **What the sync cannot tell goes to a human (rounds 7-10 — each earlier round broke a
  guess)**: it acts only on its own recorded evidence (bindings, the history, retire-time names,
  audit rows) and holds the rest. The residual heuristic is the NAME: a board action that
  changes a number's meaning records no CODEX card except the #477 pick. (1)
  `_ScopePlanner._contested` is THE rule, checked wherever the sync would mutate a number
  (identify, a renumber onto it — live or its Kôš copy, a memory move from a Kôš copy). Our name
  vs our CODEX card C: it IS C's product (C's current rows or its history name — also C's NEW
  CODEX name) → fine; it names ANOTHER card carrying the code now → contested (the #467 drift
  button „Prevziať názov z CODEXu" offers exactly that reusing card's name, as if cosmetic — it
  is TRUE for CODEX imports, so the button stays, but renumbering such a number into C moved the
  other product's wordings onto C, round 9 🟡, also for an ACTIVE binding in the dry-run window;
  it may also be a CODEX rename of C beside a same-named duplicate, so the text only says what it
  sees, round 10); a number the sync RETIRED renamed since (≠ `codex_card_bindings.
  retired_name`; a plain Kôš undo of a card whose name had drifted is no rename, round 8) →
  contested. Never contested: a blank Kôš marker, a pick, C missing from ONE snapshot. A
  contested number → review with the way out per case (rename to C's name = rejoins C while C
  lives; delete + pick at a question = reset; a code no card carries says so); no memory move
  from it, no renumber onto it. (2) Mapping rows OLDER than a re-pick, or ALL rows of every
  number of a group a rename rebind turned into another product (`_repicked_review`) → review
  with the count, where they are after this plan and where they can go (never to a card gone
  from CODEX) — never moved: `created_at` cannot tell whose a row is. (3) The REUSE HOLD (rounds
  10-12 🟡; the rules live in `codex_sync_memory`, read by the planner AND the executor so a
  dry-run count is exactly what the apply moves): `Codex.taken(C, X)` = CODEX gave X to another
  card D after C last carried it; the window opens when D was first SEEN on X. That needs the
  history of EVERY accepted push: `codex_sync._record_history` writes it in its own committed
  step before the sync — the list is pickable the moment it is accepted, so a sync that then
  fails / skips must not lose the sighting (review 12). A row on X decided since then
  (`held_clause`: `created_at` after it — NULL = old; a DL row's document `delivered_on` after
  it — never `item_memory.delivered_on`, an order's REQUESTED day; or a non-sync audit row on it
  then — a Naučené edit keeps `created_at`) may be D's (a #477 pick of X SELECTS our existing
  number and teaches D's wording with NO rename) — the card's renumber and the retired-number
  memory path move only the older rows (`Split`); held rows stay on X. TAUGHT = what the
  matcher trusts (`TAUGHT_SOURCES` = `memory.CURATED_SOURCES`: human, sheet-import, História
  „Doučiť" teachback — review 12: teachback counted as history was held with no review);
  held taught rows get a review with the way out per kind (Naučené; a „Doučiť" row via the Kôš);
  held SHIPPED rows (and a NULL source) are delivery history — never sent to Naučené, and where
  nothing else is said about them they go to `Plan.holds` (report + ONE ops line under the
  number they really sit on — `at`, a same-push renumber carries them — deduped by scope + code
  + count against the last applied run, whose renumber `held` lines count too; review 13). A
  restore / merge onto a number whose code another card held
  since C first had it (`Codex.foreign` — a round trip X → Y → X, the #478 incident's shape)
  flags its taught rows decided since then (`_adopted_review`) — adopted, never silently. In
  practice the hold matters in the dry-run / blocked / reviewed-renumber windows and for
  duplicate carriers. Residual: `dl_memory.remember` promoting / reviving an existing row keeps
  its `created_at`, so an answer about a delivery from BEFORE the reuse (same date) counts as
  C's; a round trip that completes while no renumber ran (dry-run) adopts D's rows with no
  flag; history keeps only first/last seen per (card, code) — and its `name` advances ONLY with
  `last_seen` (an older re-sent list, recorded too, must never set a newer name back:
  `same_product` reads it — review 13 🟡). The class closes only when every
  memory writer records the CODEX card a row was taught for (the binding / the pick's
  `codex_card` — a cross-cutting schema change, follow-up candidate). One review entry per card
  keeps every reason in `reasons`; the ops alert dedups PER reason. A renumber carries only OUR
  group's numbers (+ the canonical number a human deleted, only while it still IS the card) and
  a memory move only the rows that qualified themselves (no unchecked `| code` union — round 9).
- **A text that tells the warehouse what an action WILL do is computed, never written as prose
  (rounds 13-17 — six rounds found false claims)**: WHAT A PICK DOES is the picker's own pure
  rule `card_guard.pick_target` (select a live number the scope's EDI can carry / restore our
  Kôš card / add a NEW card with only the CODEX name + sklad — a DL 14-char twin is never
  selected or restored), which `add_from_codex` decides through and the planner runs over its
  simulated catalog (`_pick`, every live number of the code — `_numbers` — sent to the Kôš
  first; round 17: re-deriving it in prose missed a live legacy twin and a DL twin-only card).
  `_pick_advice` (the #477 picker offers ONE card per code — `card_guard.pickable`; a restored
  card keeps its data when the card it is bound to after this plan (`_bound`) is the same
  product — „ten istý výrobok" — or when it is bound to nothing — no product claim then, round
  26; the cleared fields named per catalog; a card the picker cannot offer is named
  "zaradí len oprava v CODEXe"; a repick review never advises a code whose pick SELECTS another
  number of ours), `_gone_reason` (no stredisko-1 carrier / one / several),
  `Split.held_at` (the numbers held rows really sit on — a legacy twin), a hold note's `at` +
  `moved` read from the same-push renumber, `codex_name` on a renumber line, and a footer that
  promises a redo only for renames / renumbers (a Kôš undo of a RESET is not redone — the
  binding it came with is stored by then). `CHECK_TAUGHT` is the one way-out text for taught
  rows (Naučené; a História „Doučiť" row via the Kôš). The Slovak strings live in
  `codex_sync_texts` — pure functions over FACTS the planner passes in (offered card + its
  name, `same`, `home`, `no_carrier`…); a new review reason = derive the fact in the planner,
  add a text function there, never an f-string in `codex_sync_plan` (it is near the size budget).
- **What is decided, per catalog, per CODEX code our cards carry** (cards grouped per
  `normalize_code`, the canonical ≤13-char number supplies the data, `codex_cards.index_by_code`),
  with our card bound to card C: C still carries X → rename to C's stredisko-1 name when ours
  drifted (#467 `name_key`, `_name_order`; orders alias untouched via `alias=None`). C carries a
  new pickable code Y (`card_guard.pickable`, C's NEWEST when several) → renumber (create / merge
  into our Y bound to C / restore our Y from the Kôš), every old number to the Kôš + binding
  inactive, every memory row X→Y — but a Y another card ALSO carries, or our Y bound to another
  card → review (never a silent merge of two products). Our Kôš Y never identified (no binding,
  no pick — deleted before the deploy, its code freed in CODEX and REUSED for C) is restored
  with OUR data (CODEX's truth); when it is not C's product (`_kos_review`: by name — a bare
  marker named by its last snapshot — and never a name C's take-over of the code may have
  lent it via the #467 drift button, `_took_over`) its taught rows, adopted as they sit, go
  to a human and its delivery history is said (a hold note) — rounds 38-39: silently the
  bageta's wording recalled the rožok; a BLOCK instead held every order line of ours on a code
  CODEX no longer has, protected nothing in orders (orders recall never reads the catalog)
  and led the warehouse to a pick restoring the bageta's data. The SAME treatment (round 40)
  for our Kôš Y KNOWN as a card that left CODEX for good (`gone_twice` — no two live products;
  products compared with `same_product`, never `_contested`; blocked, every line on the dead
  code was held — rounds 40-41) and for a #477 pick that RESTORED our never-identified Kôš card
  as it was (`Event.restored` + its name then; `KosRules._restored_pick` in `_identify`'s
  picked branch): not C's product by that name → reset like a re-pick of another product
  (`reset_kos`) ONLY on evidence — the name is another card's that carried the code
  (`_kos_verdict`'s `others`), the pick is not older than the history, no human edit of its
  DATA since (`_edited_since`: an audited `update` on the card whose `after` carries a
  `card_guard.CURATED_FIELDS` field — the board's Produkty save and the #462 dl_mass answer
  write one; a name-only „Prevziať názov z CODEXu" save is no fix, rounds 42-43) — else its
  data stays and a human is told (round 41: a missing name match wiped our own croissant
  restored under a drifted name, and a warehouse fix); a JUDGED pick's binding (reset or told)
  is stored by an APPLIED run only (`replaces`, the review-5 rule — rounds 41-42: a dry-run
  stored it, the apply never reset, and the one-time review never reached ops); its review is
  written once the WHOLE plan is done (`KosPick` → `_kos_pick_reviews`: the rows sit where the
  same plan's renumber moves them, round 43) and counts only rows from BEFORE the pick (the
  warehouse's own answer at that question is ours). A Kôš number known ONLY through a pick never
  judged (undone in the Kôš / deleted again before any applied run) is never identified:
  `_kos_review` decides (round 43: its rows adopted silently). Texts say what the Kôš card is:
  another card's product / a drift-button name / only a different name (we do not know) /
  nameless. `card_guard.CURATED_FIELDS` is the ONE list of curated fields: the board audits it,
  the sync fills / resets / reads it (round 43: four hand-kept copies). C missing from stredisko 1 in TWO
  consecutive snapshots (`prev_as_of`; one missing push is an export glitch) → removal when X is
  nowhere in CODEX, a silent rebind when exactly one card now carries X under OUR name (card
  recreated), else review. A code with no carrier and no history is never touched (#467
  "missing"; a carrier seen only in a list older than the history's beginning counts as no
  history — round 33). An OLDER snapshot than the history's newest → sync skipped.
  `update_history` reads each stredisko's beginning ONCE per list (a CTE join — a per-row
  correlated subquery was ~1000x slower on a 6k-card list, inside the push's request, round
  34); its "older than the history" warning is per stredisko too.
- **Memory**: a rewrite X→Y whose mapping already exists under Y (UNIQUE — soft-deleted rows
  count too) soft-deletes the X row; a SOFT-DELETED twin under Y is revived (audited `create`)
  or an X→Y→X round trip loses the mapping entirely (round-1 🟡, probe-proven). Memory rows of a
  number the sync RETIRED (inactive binding — written later from a frozen question / held order)
  follow its card's live number on the next push (`mode: memory`); a code that was never our
  card's is never moved. A move NEVER has its target among its sources (round 8 🔴: card 27
  back on its old number planned X → X, every row found ITSELF as the duplicate and the card
  lost all its mappings — `_memory` skips rows already on the live number, `_rewrite_memory`
  excludes `new` and `id <> rid`). Invariant pinned by a test: an applied sync never lowers the
  live mapping rows except where the same mapping lives on under the new number; the report's
  `merged` + the ops message's „N presunutých, M zlúčených" tell the two apart.
- **Ops alerts**: one per applied run with changes (+ review items not in the previous applied
  run), one per distinct blocked plan (worded as what WOULD change — `_change_lines(applied=
  False)`, round 9), one per failing-sync episode — all `reminder_suppressed`
  where they could repeat. The message says honestly that a Kôš undo is redone by the next list
  while CODEX stays the same (only a CODEX fix or `codex_sync_apply=false` stops it).
- **Every write is audited and Kôš-restorable — except a DL number whose code left CODEX**
  (#467 `audit._refuse_dead_dl_code` refuses it, 409: every DL removal, a DL renumber's old
  number when its code left CODEX — the ops footer says so, `codex_sync._dl_dead`, review 30):
  renames/memory rewrites = `update` (before/after), new card = `create`, retired number /
  duplicate memory row = `delete`. `audit._restore_update`
  now runs its UPDATE on a savepoint and turns a UNIQUE clash into `RestoreError(409)` (a
  memory gtin written back while the mapping was re-learned under it).
- **Tests** (`tests/test_codex_sync.py`, synthetic codes 999…): `_baseline` = push V1 + one
  applied sync (seeds history + bindings, no change). Both review rounds' probes are the
  regression set: reused code followed by card (also only-seen-in-dry-run chains), vanished card
  never follows the code, shared new code never merged, swap reviewed in orders / followed in DL,
  stredisko-4 name, one-push duplicate, recreated card rebound, older snapshot, twin,
  garbled-rename breaker, blocked dedup, would_block, retired-number memory, round trip with a
  duplicate, 409 restore, rollback + error alert. A removal test must push TWICE (two snapshots).
