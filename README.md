# email-extractor

Home Assistant add-on that reads an IMAP mailbox, extracts text from the body and
**every attachment** (native parse for text PDFs / Word / Excel; OCR for
scanned/photographed PDFs and images — Tesseract `ces+slk+eng`), and writes one
row per email into PostgreSQL. n8n then reads the table, classifies, and processes
— no fragile extraction inside n8n, no moving emails between folders.

- **Pictures / handwritten / photographed tables** (low OCR confidence) are flagged
  `needs_vision`: the noisy OCR text is dropped and n8n routes the original file to
  an AI Vision model for exact data.
- **Files stay accessible**: originals are stored on the add-on volume and served
  over an internal HTTP API so n8n can fetch a PDF/image for AI Vision on demand.
- **State without folder moves**: a `messages` table (extractor-owned) + a
  `processed` table that each terminal n8n workflow writes when done.

## Layout

```
repository.yaml            # HA add-on repository manifest (added later)
email-extractor/           # the add-on
  app/                     # Python: extract, mailparse, process, config, runtime
  tests/                   # synthetic-fixture unit tests (no real data)
  config.yaml Dockerfile   # HA add-on packaging (added in the runtime slice)
.github/workflows/ci.yml   # lint + test + (later) build image -> GHCR
docs/superpowers/specs/    # design spec
```

## Dev

Two-branch flow (`main` = production, `dev` = development). CI runs ruff + pytest
on every push. See `CLAUDE.md`.

```bash
cd email-extractor
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
ruff check . && pytest
```

Status: **building** — extraction core + tests landed; runtime (IMAP/Postgres/HTTP)
and HA packaging in progress on `dev`.
