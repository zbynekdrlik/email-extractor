---
paths:
  - "email-extractor/app/orders/snapshot.py"
  - "email-extractor/app/httpapi_znalosti.py"
  - "email-extractor/app/orders/teach.py"
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
- **Cards are added/edited ONLY via `/znalosti`** → `POST /api/znalosti/products`
  (`snapshot.upsert_catalog_card`); DL cards via `/znalosti` DL-products. Retire a card via
  `DELETE /api/znalosti/products/<gtin>` (`retire_catalog_card`, a `retired=true` override).
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
`item_memory` write. (Live remediation shape used in #383: add the card via
`POST /api/znalosti/products`, reopen the expired question, answer it → held order ships.)

## How to add a card programmatically (never a direct INSERT)

```bash
# session cookie via /login (admin), then:
curl -s -b cookies.txt -X POST <base>/api/znalosti/products \
  -H 'Content-Type: application/json' \
  -d '{"gtin":"<gtin>","name":"<name>","doplnok":"<alias or omit the key>"}'
# read back:
curl -s -b cookies.txt "<base>/api/znalosti/catalog?q=<gtin-or-name>"
```
