# email-extractor — project instructions

Home Assistant add-on (Python) that extracts text from IMAP emails + attachments
(native parse + OCR, with AI-Vision routing) and writes PostgreSQL for n8n to read.
Replaces the fragile extraction that lived inside n8n.

## Deploy target

Runs as an HA add-on on the **Home Assistant OS** box that also hosts n8n
(`46.224.130.35`, amd64). n8n reads the Postgres over the local docker network.
SSH + IMAP + server creds are in **local memory** (not git) — never commit secrets;
the live values live only in the add-on options / `.env`.

## Architecture

- **Extractor (this add-on)** — read-only on IMAP, polls configured folders, dedups
  by `message_id`, extracts, stores originals on its volume, writes Postgres.
- **Postgres** — `messages` (extractor-owned), `attachments` (text + file URL +
  `needs_vision`), `processed` (each terminal n8n workflow writes `message_id` when
  done). NO IMAP folder moves anywhere.
- **n8n** — reads new messages (`status='new'` / not in `processed`), classifies
  (existing `Email Sorting`), runs AI Vision selectively on files fetched by URL,
  writes `processed`. Forwards via SMTP using stored `raw_eml`.

## Extraction strategy (validated by the 100-email spike)

Native text first (text-layer PDF, docx, xlsx, txt). PDF with no text OR garbage
(`(cid:`, mojibake, alpha-ratio < 0.55) → OCR. Images → OCR (`ces+slk+eng`, 300 DPI).
Low OCR confidence on image/scan → `needs_vision` (drop noisy text, route to Vision).
Skip decorative/banner/tiny images. See `docs/superpowers/specs/`.

## Dev workflow (airruleset)

- Two branches: `main` (production), `dev` (development). Work on `dev`, PR to `main`.
- Bump version first. CI (GitHub Actions): ruff + pytest + coverage; later builds the
  add-on image to GHCR. All gates green before merge; auto-merge on green (default).
- No real email data in git (`_spike_*`, `pipeline.py` are gitignored). Synthetic
  fixtures only in tests.

## Playbook router

Load the matching skill BEFORE working on that area (don't re-derive):
- n8n workflows / nodes / MCP → load `using-n8n-skills` then the matching `n8n-*` skill
- extraction quality / OCR tuning → see `docs/superpowers/specs/` + the spike memory
