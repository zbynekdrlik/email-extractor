---
name: odoo-reported-issues
description: Use when the user says a problem/error is described in an Odoo Discuss channel (dodacie listy ch.243, objednávky ch.152, reklamácie ch.368) — often by warehouse staff, often with photos. Covers reading the channel + photos, mapping the complaint to the pipeline execution, root-causing, fixing, and live-verifying.
---

# Odoo-reported pipeline issues — investigation & fix procedure

The user (or warehouse staff — pani skladníčka / Maria Spišská) reports problems by
writing into an Odoo Discuss channel, often attaching PHOTOS (screenshots of ORION,
photos of paper DLs). The trigger phrase is some form of "v Odoo je popísaná chyba".
Follow this procedure end-to-end; do not stop at diagnosis — the user expects fix +
live verification + report.

## Channels

| Channel | id | Topic |
|---|---|---|
| AI dodacie listy | 243 | delivery-note EDI pipeline |
| objednávky | 152 | AI + static orders pipeline |
| reklamácie | 368 | complaints pipeline |

URL pattern: `https://erp.slovnormal.sk/odoo/discuss?active_id=discuss.channel_<id>`

## Step 1 — read the channel messages + photos

Access routes, in order of preference:

1. **claude.ai Odoo MCP** (`mcp__claude_ai_odoo_slovnormal__*`) — if authorized
   (user runs `/mcp` → "claude.ai odoo slovnormal" once). Then read
   `discuss.channel` / `mail.message` records directly.
2. **Odoo API key** (stored in LOCAL MEMORY only — never in git). Load the
   `odoo-json2-api` skill for endpoint shapes. Reading messages:
   `POST https://erp.slovnormal.sk/json/2/mail.message/search_read` with headers
   `Authorization: Bearer <key>`, `X-Odoo-Database: odoo`, body filtering
   `[["model","=","discuss.channel"],["res_id","=",<channel_id>]]`, fields
   `["id","date","author_id","body","attachment_ids"]`. Attachments:
   `ir.attachment` `search_read` on the ids, then download raw bytes via
   `GET /web/content/<attachment_id>?access_token=...` or the JSON API `datas`
   field (base64).
3. **Fallback — the extractor mailbox**: Odoo notifies channel followers by
   email; if `automation@slovnormal.sk` follows the channel, the messages (incl.
   image attachments) land in the extractor DB (`messages` from
   `notifications@slovnormal.sk`, `message_id` contains `discuss.channel`) and
   files come from `http://email-extractor:8099/files/<message_id>/<idx>?token=…`.
   Verified 2026-07-10: automation@ is currently NOT a follower of ch.243, so
   this route found nothing — prefer routes 1–2.

**Photos**: download each image locally (scratchpad), then Read them (Claude is
multimodal — read the pixels, extract DL numbers, product names, quantities,
ORION screenshots' values).

## Step 2 — map each complaint to the pipeline run

- Extract identifiers from the text/photo: DL number (docNumber), supplier,
  date, product names, quantities.
- Find the message + outcome in the extractor DB (SSH to HA box, creds in local
  memory): `SELECT * FROM messages WHERE combined_text ILIKE '%<docNumber>%'`,
  plus `email_events` for its message_id (stage/outcome timeline).
- Find the n8n execution: `search_executions` on the consumer workflow around
  `last_event_at` (times are UTC). Long runs (>5 s) are real processing; 0.4 s
  runs are empty claims. Pull node outputs with `get_execution` +
  `nodeNames` filter; oversized results land in a file — probe with jq.

## Step 3 — root cause

Read the ACTUAL data at each stage (Vision transcript → AI EXTRACT output →
VERIFY → VALIDATE → match → EDI content) and find the first stage where the
data went wrong. Known failure classes + their fixes live in local memory
(`email-extractor-deploy.md`) — check it first so you don't re-derive:
zero-width chars, thousands separators, citation drops, duplicate-DL loops,
zero-item extractions, kg/sklad conversion, catalog gaps (GTIN = Codex
NEANKOD), Vision numeric hallucination (dual-transcript cross-check).

## Step 4 — fix (the standing quality bar)

- Fix the ROOT CAUSE in the n8n workflow code/prompts (scratchpad → node --check
  → local repro on the real execution data RED/GREEN → update_workflow →
  re-fetch + diff → publish). Sub-workflows publish BEFORE the parent.
- NEVER silently drop data — every skipped/unverified item must surface in the
  Odoo message.
- Models: the pipelines run the top OpenAI tier (gpt-5.4, reasoningEffort high,
  responsesApiEnabled) by the user's explicit standing decision (2026-07-10,
  "najdrahšie modely") — do not downgrade to save cost.

## Step 5 — live verify + close the loop

- Safe reprocess rule: ONLY messages whose run NEVER uploaded to ORION
  (review/failed paths) may be re-run (`UPDATE messages SET processed=false,
  processing_at=NULL, processed_by=NULL WHERE id=<id>`); NEVER re-run an
  uploaded one (ORION duplicate). Wait for the dispatcher (≤1 min) and verify
  the outcome in `messages.proc_outcome` + the execution's node data.
- If the complaint's DL was uploaded WRONG into ORION: the ORION-side correction
  is manual (tell the user exactly what to fix there); the pipeline fix only
  protects future documents.
- Update local memory (`email-extractor-deploy.md`) with the new failure class.
- Report in Slovak: root cause in plain words, what was fixed, live-verification
  evidence, any manual ORION steps left for the user.
