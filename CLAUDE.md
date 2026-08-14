# email-extractor — project instructions

Home Assistant add-on (Python) that extracts text from IMAP emails + attachments
(native parse + OCR, with AI-Vision routing) and writes PostgreSQL for n8n to read.
Replaces the fragile extraction that lived inside n8n.

## Deploy target

Runs as an HA add-on (amd64) on the **Home Assistant OS** box that also hosts n8n;
n8n reads the Postgres over the local docker network. The server host/IP, SSH and
IMAP credentials live in **local memory only** (not git) — never commit them; the
live values belong in the add-on options / `.env`.

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
- **After `git commit -F <file>`, verify `git log -1 --format="%B"` matches the file
  you wrote** (#268 krok 1, 2026-08-12: one commit landed with the correct staged
  diff but a completely unrelated, hallucinated message — recovered via a sanctioned
  `git reset --soft HEAD~1` + re-commit, never `--amend`). Not reproduced on retry with
  everything on ONE bash line (stage, commit, any hook-bypass comment, no separate
  lines) — prefer that shape and always verify immediately after.
- **The git repo root is THIS directory** (`email_extract/`, containing this
  `CLAUDE.md`, `docs/`, `.claude/`) — ONE LEVEL ABOVE the `email-extractor/`
  subdirectory that `.github/workflows/ci.yml` treats as its `working-directory`
  (`app/`, `tests/`, `config.yaml`, `requirements*.txt` all live inside
  `email-extractor/`). `docs/autopilot-log.md` and every `.claude/rules/*.md` live at
  THIS root, not inside `email-extractor/docs/` — a session whose cwd is already
  `email-extractor/` (e.g. because it's mid-ticket there) must `cd ..` or use the
  absolute repo-root path to find/update them.

## Playbook router

Load the matching skill BEFORE working on that area (don't re-derive):
- n8n workflows / nodes / MCP → load `using-n8n-skills` then the matching `n8n-*` skill
- úprava n8n objednávkových workflow (statické objednávky, dodacie, faktúry, reklamácie) →
  `.claude/rules/n8n-workflow-edits.md` — načítaj PRED úpravou (nemá `paths:`, tie workflow
  nežijú v repozitári, takže sa nenačíta sám)
- problém/chyba popísaná v Odoo Discuss (dodacie/objednávky/reklamácie, aj s fotkami) → load `.claude/skills/odoo-reported-issues`
- extraction quality / OCR tuning → see `docs/superpowers/specs/` + the spike memory
- prílohy / formáty (xlsx, xls, ods, fods, csv) → `.claude/rules/extraction-formats.md` (auto-loads on `app/extract.py`)
- AI objednávky (matching, korpus, CI gate) → `.claude/rules/orders-corpus.md` (auto-loads on `app/orders/**`)
- nasadenie na živý HA add-on → `.claude/rules/deploy.md` (auto-loads on config.yaml/app/__init__.py/Dockerfile)
- lokálne spúšťanie pytest proti dev1 test-Postgres → `.claude/rules/local-testing.md` (auto-loads on `email-extractor/tests/**`)
- `git commit -F <scratch-file>` bezpečnosť (stale scratchpad obsah) → `.claude/rules/git-commit-hygiene.md` (auto-loads on `email-extractor/app/**`, `tests/**`, `config.yaml`)
- rozdelenie `app/httpapi.py` (#268) / charakterizačné testy → `.claude/rules/httpapi-characterization.md` (auto-loads on `app/httpapi*.py` a `test_httpapi_characterization.py`)
- PR hlási `mergeable_state: "dirty"` hoci lokálny merge je čistý (criss-cross merge-base z fleet integrácie) → `.claude/rules/pr-merge-mechanics.md` (auto-loads on `app/**`, `tests/**`, `config.yaml`, `docs/autopilot-log.md`)
- typová kontrola (mypy brána v CI, oprava nálezu bez potlačenia, chirurgický per-modulový override) → `.claude/rules/type-checking.md` (auto-loads on `pyproject.toml`, `requirements-dev.txt`)
- schéma / migrácie (verzovaný `migrate.py` engine, `schema_version` ledger, pridanie novej revízie, self-healing baseline, `reapply_schema` test fixture) → `.claude/rules/schema-migrations.md` (auto-loads on `app/migrate.py`, `app/db.py`, `test_migrate.py`)
- `human_processing` sweep (#308 tichá jama — catch-all reality, BACKLOG_CUTOFF, ops routing, re-ask loop) → `.claude/rules/human-processing-sweep.md` (auto-loads on `app/orders/human_processing.py`)
