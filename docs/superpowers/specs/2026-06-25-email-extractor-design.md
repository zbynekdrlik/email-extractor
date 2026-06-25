# Email OCR Extractor — Design Spec

Date: 2026-06-25 · Status: approved (quality spike validated the extraction approach)

## Problem

Email data extraction inside n8n is fragile (single-file extractor nodes, SSH-driven
OCR, split-then-merge that double-processed multi-attachment emails). Move extraction
into a dedicated, robust service; let n8n focus on classification + business actions.

## Goal

A Home Assistant add-on that, for every incoming email, extracts the body and **all**
attachments at maximum quality and writes one row per email to PostgreSQL, which n8n
reads to classify and process. Each email exposes enough info (incl. an id + the raw
.eml) to be forwarded later, and the original attachment files are reachable so n8n can
run expensive AI Vision selectively on the documents that need it.

## Decisions (from brainstorming)

1. **Storage:** PostgreSQL (n8n has a native node; handles big text + JSONB; reliable
   dedup). Not n8n Data Tables (too limited), not SQLite (concurrency), not a custom API.
2. **Responsibility:** the add-on is **read-only** on the mailbox. n8n owns all logic.
   No IMAP folder moves anywhere (deprecate the category folders).
3. **State without moves:** `messages` (extractor-owned) + `processed` (each terminal
   n8n workflow writes the `message_id` when finished). "New" = `status='new'` and not
   in `processed`. A `status` claim prevents double-pick during in-flight processing.
4. **Topology:** everything runs on the HA OS box that hosts n8n (amd64); n8n reaches
   Postgres over the local docker network.
5. **Files for AI Vision:** original attachments are stored on the add-on volume and
   served over an internal token-protected HTTP API; Postgres holds metadata + URLs,
   not big BYTEA. n8n downloads a file by URL when a workflow needs Vision.
6. **GitHub CI/CD (airruleset):** two-branch (`main`/`dev`), ruff + pytest + coverage,
   build the add-on image to GHCR; repo doubles as an HA add-on repository.

## Extraction strategy (validated, 100-email spike)

- Native text first: text-layer PDF (`pdfplumber`), `.docx`, `.xlsx`, `.txt/.csv`.
- A PDF that yields **no text OR garbage** (`(cid:`, replacement chars, alpha-ratio
  < 0.55) → OCR. Images → OCR (Tesseract `ces+slk+eng`, 300 DPI, psm 6, oem 1).
- Low OCR confidence (< 72) on an image/scan → `needs_vision`: drop the noisy OCR text
  (keep a placeholder), n8n routes the original to AI Vision.
- Skip decorative junk: tiny/small images, wide signature/marketing banners, vcf/ics/
  signature blobs.

Spike result: machine-readable docs **100% faithful** (matched independent `pdftotext`);
phone-photographed / handwritten delivery notes correctly flagged `needs_vision`;
attachment-only "static order" emails legitimately have no body. Two fixes folded in:
decorative-banner skip, and drop-noisy-OCR-on-needs_vision.

## Data model

- **messages**: `id, message_id (unique), imap_uid, imap_uidvalidity, folder,
  from_addr, from_name, to_addrs, cc_addrs, subject, sent_at, body_text, body_source,
  combined_text, has_attachments, needs_vision, status (new|processing|done|error),
  error, raw_eml_path, created_at, processed_at`.
- **attachments**: `id, message_id (fk), filename, mime, size, sha256, method,
  ocr_conf, pages, chars, needs_vision, flag, file_path, file_url`.
- **processed**: `id, message_id, handled_by, category, result, processed_at`.

## Components (add-on)

- `extract.py` — attachment → text (native/OCR), quality metrics, flags. **(done)**
- `mailparse.py` — raw → headers, body, attachments, stable identity. **(done)**
- `process.py` — raw → full record + `combined_text`. **(done)**
- `config.py` — HA add-on options / env. **(done)**
- `db.py` — Postgres schema + upsert/dedup. *(runtime slice)*
- `store.py` — save raw .eml + attachment files; path/url helpers. *(runtime slice)*
- `imap_poll.py` — connect, poll folders, yield new raws. *(runtime slice)*
- `httpapi.py` — `/health`, `/version`, `/files/<mid>/<aid>`, `/eml/<mid>`. *(runtime slice)*
- `main.py` — orchestrate poll → process → store → upsert; run HTTP server. *(runtime slice)*

## n8n changes (later, separate work)

Retire `Email Pulling`, `Email Extraction`, `File to Text`, and all `moveEmail` nodes.
New intake: Postgres read of new messages → existing `Email Sorting` (classify) →
terminal workflows write `processed`. Forward via SMTP using `raw_eml`.

## Out of scope (now)

n8n rewiring; bulk consolidation of existing folders into INBOX (done at cutover);
AI Vision implementation (lives in n8n); multi-arch images (amd64 only).

## Security

Secrets (IMAP, Postgres, API token) come from add-on options / env — never committed.
Recommend rotating the IMAP + SSH passwords that were shared in chat.
