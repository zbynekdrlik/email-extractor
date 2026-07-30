---
paths:
  - "email-extractor/app/extract.py"
  - "email-extractor/tests/test_extract*.py"
---

# Attachment extraction — format gotchas that cost us real orders

Everything here was a live data-loss bug (#23, #41, #37). Read before touching a
handler; do not re-derive it from the library docs.

## The declared MIME lies — sniff the bytes

Senders mislabel spreadsheets constantly. `application/vnd.ms-excel` regularly
arrives on real OOXML or CSV, and pushing that into `xlrd` raises → `flag='error'`
and **the whole order text is lost** (no classifier, no Vision, nothing).

Dispatch on magic bytes: `PK` → `_extract_xlsx`, `\xd0\xcf\x11\xe0` (OLE2) → `_extract_xls`,
otherwise decode as text. Same reflex for any new format: trust bytes, not headers,
not extensions.

## Spreadsheet grids: empty cells are DATA

A blank trailing "quantity" column must stay blank so the columns line up — the AI
reads the grid positionally. Hence `_grid_rows()` pads rows and preserves empty cells.

Two consequences:

- **ODF `number-columns-repeated` MUST be expanded** (`_odf_row_cells`). Calc
  run-length-encodes runs of identical cells; reading a run as one cell shifts every
  following column LEFT and the quantity is read from the wrong column.
- **Cap the expansion** (`ODF_MAX_REPEAT = 256`) and trim only *trailing* padding —
  Calc pads rows to 1024+ empty columns, which would otherwise bloat `combined_text`.

## ODF comes in two containers

`odf.opendocument.load()` reads ONLY zip-packaged ODF. Flat single-XML ODF
(`.fods`/`.fodt`, what LibreOffice writes on "flat XML" export) raises `BadZipFile`.
`_extract_flat_odf()` handles it with ElementTree. If you add an ODF path, decide the
container first (`data[:2] == b"PK"`).

## Legacy `.xls` dates are floats

`xlrd` returns date cells as serials, so a delivery date silently became `46237`.
Convert `XL_CELL_DATE` via `xlrd.xldate.xldate_as_datetime(v, book.datemode)`
(`_xls_cell`); time-only serials (< 1) render as `HH:MM`; an out-of-range serial falls
back to the raw value rather than raising.

## Invisible characters break quantities

Strip zero-width characters (U+200B & co.) at ingest — `45​ks` parsed as a different
number (#41, AGEL incident).

## Testing rules for this file

- Fixtures are **synthetic and generated in the test** (odfpy / openpyxl / xlwt), never
  real customer files. `xlwt` is in `requirements-dev.txt` purely to author `.xls`.
- One fixture + one assertion per sub-bug, RED committed before GREEN.
- Assert on the tab-joined grid (`_grid_rows` joins with `\t`), and assert the quantity
  lands in the expected column index — that is the thing that actually regresses.
- Never let a handler swallow an exception into `flag='error'` silently in a test: assert
  `r["flag"] != "error"` with `r.get("error")` in the message, or the next regression
  looks like a pass.
