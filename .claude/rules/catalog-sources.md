---
paths:
  - "email-extractor/app/orders/snapshot.py"
  - "email-extractor/app/httpapi_znalosti.py"
  - "email-extractor/app/orders/teach.py"
  - "email-extractor/app/orders/card_guard.py"
  - "email-extractor/app/board/services/catalog.py"
---

# The ONLY source of catalog cards is Postgres — the Google Sheet is RETIRED (#383)

Owner decision 2026-09-04 (verbatim „TABULKU ZRUSIT!!!"): the Google Sheet „EAN slovnormal"
is **dead as a card source**. It has not been *read* since #129 (Postgres-only), but the
warehouse kept editing it and the app never saw those edits, so orders held for days (the
Ciabatta 3636/3643 incident: two cards added to the sheet only → 2 orders held 2 days).

## The rule

- **NEVER read the Google Sheet — not even „to check" a card.** There is no fetch path in the
  code (`snapshot.fetch_csv`/`sheet_csv_url`/`refresh` were removed in #129) and there must
  never be one again. If a card „is in the sheet" that means the app does NOT have it.
- **The catalog is Postgres only:** the frozen `catalog_snapshot` (of the latest
  `order_snapshots` row) PLUS `catalog_overrides` merged on top (`snapshot._merge_catalog`).
  The effective catalog the pipeline matches against is `snapshot.catalog_for_management` /
  `load_catalog` + overrides. DL has its own parallel line (`dl_catalog_snapshot` +
  `dl_catalog_overrides`).
- **A NEW card enters the catalog ONLY by „Vybrať kartu z CODEXu" on a board question (#477,
  owner order 2026-09-30)** — `orders/card_guard.add_from_codex` writes exactly the picked CODEX
  code + CODEX name (orders = CODEX stredisko 1 / sklad 1; DL = stredisko 1, `sklad` from CODEX),
  audited, one card per human pick (never a bulk import, #337). EVERY typed creation answers 403
  „Nové karty sa pridávajú len výberom z CODEXu": the Produkty tabs (no „Pridať" any more), the
  question's `new_product`/`new_item` bodies, and a NEW number on `POST /api/znalosti/products` /
  `dl-products`. The nástenka „Produkty" tabs (`/nastenka/produkty-objednavky` /
  `produkty-sklad`) and those two endpoints now only EDIT an existing card
  (`snapshot.upsert_catalog_card` / `dl_snapshot.upsert_dl_catalog_card`). Retire a card via
  `DELETE /api/znalosti/products/<gtin>` (`retire_catalog_card`, a `retired=true` override).
  (#449 lane 8: the old `/znalosti` PAGE is retired — it 302s to the Produkty tab.)
- `snapshot.import_snapshot`/`import_files`/`parse_catalog`/`parse_customers` are kept ONLY
  because the offline eval corpus (`eval_run.py`/`dl_eval_run.py`) seeds its frozen snapshot
  from a CSV fixture that way. They are pure network-free CSV-text importers; nothing in the
  live pipeline calls them. Do NOT wire them to any live/remote sheet.

## The card `alias` / `doplnok` (a real matching signal) lives in the override too (#383)

`catalog_snapshot.alias` (`doplnok`) is a comma/semicolon-separated list of goods-phrases and
IS used in matching (`match.py::alias_exact` etc.). It used to come only from the sheet, so an
override-only card always had `alias=""`. Since #383, `catalog_overrides.alias` (a nullable
column, migrate revision 8) makes it editable via `/znalosti` products.

- **Tri-state** (`snapshot._merge_catalog` / `upsert_catalog_card`): override alias `NULL` =
  „don't touch, inherit the snapshot row's baked-in alias"; a non-NULL string (incl `""`) =
  „override wins" (`""` = an explicit clear). The merged alias is always `or ""`-guarded so
  `None` never reaches `match.py`.
- **API tri-state** (`POST /api/znalosti/products`): the `alias`/`doplnok` KEY being ABSENT →
  don't touch (pass `alias=None`); PRESENT (even `""`) → set/clear it. The `/znalosti` UI
  prefills the input with the current effective alias and always sends it (a name-only UI edit
  therefore pins the current value — acceptable, the sheet is dead). A programmatic name-only
  edit omits the key so an existing alias survives.

## Reopening an auto-expired (#341) question when a card arrives late

A board question the warehouse couldn't answer (card didn't exist yet) auto-expires after 2
working days (#341, `status='expired'`). When the card is finally added via `/znalosti`, reopen
the question so it can be answered (then it ships / releases its held order):

```sql
UPDATE order_questions
   SET status='open', answer=NULL, answered_at=NULL, answered_by=NULL
 WHERE id=<qid> AND status='expired';
```

Then answer it through the board API (`POST /api/orders/question/<qid>/answer`), never a direct
`item_memory` write. (Since #477 the answer itself brings the card in: reopen the expired
question and answer it with `{"codex_card": {"code": "<CODEX EAN kód>"}}` — the card is added
from CODEX and the held order ships in the same click.)

## How a card gets in programmatically (never a direct INSERT, never a typed card — #477)

```bash
# session cookie via /login (admin) or the sklad link, then find the CODEX card:
curl -s -b cookies.txt "<base>/api/board/codex-cards?scope=orders&q=<name-or-code>"
# and answer the open question with it (adds the card from CODEX, audited, then answers):
curl -s -b cookies.txt -X POST <base>/api/board/questions/<qid>/answer \
  -H 'Content-Type: application/json' -d '{"codex_card": {"code": "<code from the list>"}}'
```

There is deliberately NO path that adds a card without a question (the owner's #477 order: a
card is added when the warehouse needs it, one human-picked CODEX card at a time). A card that
CODEX does not have cannot be added at all — the warehouse creates it in CODEX first; it shows in
the picker after the next CODEX push (~14:45 / ~18:30).
