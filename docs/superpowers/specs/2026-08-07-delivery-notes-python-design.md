# Dodacie listy (DL) pipeline — complete business-rules map (migration spec input)

> **Status: BINDING SPEC** for the delivery-notes (dodacie listy, "DL") Python migration (#200 and all later phases). Mapped 2026-08-07 from the LIVE n8n workflows + 10 real executions + prod Postgres ground truth (read-only). This is a SANITIZED copy — every real DL document number and execution id has been replaced with a clearly-fake placeholder of the same format; supplier/brand names are kept (existing repo precedent, e.g. `.claude/rules/orders-corpus.md`). The original, unsanitized mapping (with the real raw n8n dumps, prompts and execution JSON it was built from) lives outside git in the scratchpad it was produced in — never commit those files verbatim, this repo is public.

---


Mapped 2026-08-07 from the LIVE n8n workflows (read-only). Sources: workflow dumps + 10 real
executions + prod Postgres ground truth. Raw dumps + full prompt texts live next to this file
(`wf_*.json`, `p_*/`, `prompt_*.txt`, `exec_*.json`, `sub3_edi_code.js`).
NOTE: contains supplier names + real DL numbers — do NOT commit verbatim into the public repo.

Workflows:

| Role | Name | ID | Version seen |
|---|---|---|---|
| Dispatcher | Email Dispatcher | `TjIzExr4uUs5f4Ci` | c11f00b1 |
| Parent | Dodacie Listy EDI | `1R4WcUFhpIPwEJX1` | 407efb5d |
| Sub1 | DL — Vision + Extract | `pr78VAnncEF71kWK` | f129fb56 |
| Sub2 | DL — Match | `3lXM3piULXIBm5CS` | 2a5ce61a |
| Sub3 | DL — Assemble EDI | `9wqajTlFayjdnlvb` | d73558e6 |
| Error handler | Error to discord | `pmzKaZXBdziRwnR8` | errorTrigger → Discord ping |

External state:

- Postgres (email-extractor add-on): `messages`, `attachments`, `email_events`.
- n8n Data Table **"dodacie listy"** `sn0R7JbZVrHjE4yG` — sent-DL registry, single column `docNumber`.
- n8n Data Table **"dodacie_pamat_poloziek"** `MBCwHVhzsKjbQkVl` — item-match history (cust, item, gtin, card, at, src, cnt).
- Google Sheet **"Slovnormal"** `1m12ognEGs93t8WxtLw576XoEhCS2FE37tovFtVPSO00`: tabs `produkty dodacie listy` (gid 1437442607), `produkty objednavky` (gid 957145124), `customers` (gid 501932372).
- Google Sheet forecast log `14UYxEMwOZnas2CWweBDevRoznPYSmzPx7ty4PeVoHTE` (tab gid 0) — one row per shipped item.
- Odoo Discuss channel **243** (`https://erp.slovnormal.sk/json/2/discuss.channel/message_post`, header `X-Odoo-Database: odoo`, Bearer auth).
- ORION target: SSH cred "Granc server", upload dir `C:\ORION\COMMUNICATOR\data\in_DL`.

Models: **every** LLM call is `gpt-5.4`, `reasoning effort high`, timeout 300 s (user's standing
directive: most expensive models, never downgrade). The 3 LangChain chainLlm nodes use the
Responses API + structured output parser + `retryOnFail max 3, wait 5 s` + `onError:
continueErrorOutput`. The Vision transcription is a raw HTTP `POST /v1/chat/completions` with
`"n": 2` (two independent samples), no temperature. (Sub1 sticky notes still say gpt-4o /
gpt-4.1-mini — stale, ignore.)

---

## 1. End-to-end flow

### Dispatcher (every minute)

- **Get Pending** (R1): `SELECT id, category FROM messages WHERE processed=false AND
  COALESCE(attempts,0)<5 AND (processing_at IS NULL OR processing_at < now()-interval '10
  minutes') AND category IN ('invoices','dodacie_listy','reklamacie') ORDER BY created_at ASC
  LIMIT 10` → Limit 10 → Switch by category → **Trigger Dodacie** (Execute Workflow, `mode:
  each`, `waitForSubWorkflow: false`, passes NO data — the consumer pulls its own).
  ai_orders/static branches are disabled (moved off n8n / cutover in progress).

### Parent: Dodacie Listy EDI

1. **Triggered by Dispatcher** (executeWorkflowTrigger, passthrough — data unused).
2. **Get Dodacie** — atomic per-MESSAGE claim (R10): `UPDATE messages SET processing_at=now(),
   attempts=COALESCE(attempts,0)+1 WHERE id=(SELECT id FROM messages WHERE
   category='dodacie_listy' AND processed=false AND COALESCE(attempts,0)<5 AND (processing_at
   IS NULL OR processing_at < now()-interval '30 minutes') ORDER BY created_at ASC LIMIT 1 FOR
   UPDATE SKIP LOCKED) RETURNING id, message_id, subject, from_addr, from_name, to_addrs,
   sent_at, body_text, combined_text, has_attachments, attempts`.
3. **Claimed a row?** filter: `message_id` notEmpty — empty claim ends gracefully (R14).
4. **Has attachment?** IF `has_attachments` — false → **Build No-Attachment Review** (fixed
   needs-review payload "Email bez prílohy…") → Odoo Needs Review → Mark Processed + Log
   Review Event (R15).
5. **Get Attachment Meta**: `SELECT idx FROM attachments WHERE message_id=$1 ORDER BY (mime
   ILIKE '%pdf%' OR filename ILIKE '%.pdf') DESC, idx ASC LIMIT 1` — picks exactly ONE
   attachment, PDF preferred (R16 — a multi-attachment loss point, see W1).
6. **Build Email** — rebuilds the legacy contract `{id, message_id, email{uid,from,fromName,
   fromEmail,to,subject,date,bodyText,textContent}, combinedText, hasAttachments,
   attachmentCount(0|1 hardcoded), aiClassification:'dodacie_listy', isEval:false, _attIdx}`.
7. Catalog fan-out (runs before Sub1 by canvas order):
   `produkty dodacie listy` → **Edit Fields** (keepOnlySet: GTIN, Názov, doplnok, hmotnost,
   Sklad, Cena — anything not listed here is silently dropped; the 07-22 "cena fallback dead"
   incident) → Merge(append) ← `produkty objednavky` (raw) → Aggregate→`products`;
   `customers` → Aggregate1→`customers`; Merge Catalog → **BUILD CATALOG** →
   `{catalog:[{name,gtin,mass,doplnok,sklad,cena}], customers:[{name('Názov organizácie'),
   ean_edi('EAN kód EDI'), email('E-mail'), city('Obec')}]}` (R20, R21).
8. **Fetch Attachment**: `GET http://e0ac7775-email-extractor:8099/files/<message_id>/<_attIdx>`
   (httpHeaderAuth) → binary `attachment_0`.
9. **Execute Sub1** (passthrough incl. binary) → **Needs review (Sub1)?**
   (`validOrNeedsReview == 'needsReview'`) → yes: **Retry transient?**; no: **Build Sub2
   input** `{extraction, catalog, customers}` → **Execute Sub2** → **Needs review (Sub2)?**
   (same test) → no: **Build Sub3 input** `{matchedItems, header, extraction, catalog, email}`
   → **Execute Sub3**.
10. **Dedup gate** (R30–R32): Execute Sub3 output fans to Merge1[in0] + `Get row(s)` (reads the
    ENTIRE "dodacie listy" registry table) → Merge1[in1] & Duplicate DL?[in1].
    - **Merge1**: combine on `docNumber`, `keepNonMatches`, output input1 → only a docNumber
      NOT yet in the registry reaches **Can Create EDI?**.
    - **Duplicate DL?**: mirror merge `keepMatches` → **Log Duplicate Skip** (email_events
      `skip`, rollup) + **Mark Processed**. DELIBERATELY quiet — no Odoo message (Lunys
      re-announcement mails are routine).
11. **Can Create EDI?** (`canCreateEDI === true`):
    - TRUE, branch order top→bottom: (a) **Convert to File1** (toText from `wincodexContent`)
      → **Upload a file** (SSH, dir `C:\ORION\COMMUNICATOR\data\in_DL`, fileName
      **`Z-<wincodexFilename>`**) → **Mark Processed** (`processed=true,
      processed_by='dodacie_listy'`) + **Log Success Event** (`uploaded_orion/ok`, rollup,
      detail {docNumber, customer, items, edi_file}) → **ZOSTAV RIADKY PAMATE DL** → **ZAPIS
      PAMAT DL**; (b) **Odoo Success** post to ch.243; (c) **Upsert row(s)** (registry upsert
      docNumber) → **PREPARE SHEET DATA** → **Append to Google Sheet** (forecast log).
    - FALSE → **Odoo Needs Review** → Mark Processed + **Log Review Event** (`review`, rollup).
12. **Retry transient?** (R17): `reason` matches transient regex (`service failed to
    process|timed out|timeout|rate limit|too many requests|overloaded|dočasný
    výpadok|service unavailable|internal server error|bad gateway|econnreset|socket hang up`,
    case-insensitive) AND `attempts < 3` → **Log Retry Event** (rollup=false) and STOP without
    marking → the 30-min stale-reclaim retries. Else → Odoo Needs Review (→ Mark Processed).

### Sub1: DL — Vision + Extract

1. **VISION: PDF to base64** (binary `attachment_0` → `pdfBase64`).
2. **Detect scan image** (R40): scans the PDF bytes for JPEG SOI/EOI markers, takes the
   LARGEST embedded JPEG; if > 20 kB → scanned DL → vision input = that JPEG as `image_url`
   `detail: high`; else digital PDF → whole PDF as `file` input. Builds `visionContent` =
   [image/file part, Slovak transcription prompt] (full text: `prompt_vision_transcribe.txt`).
3. **VISION: OpenAI transcribe** (R41): gpt-5.4, `reasoning_effort: high`, **`n: 2`** — two
   independent transcripts in one call; `onError: continueRegularOutput` (vision failure falls
   back to machine OCR text).
4. **VISION: set combinedText** (R42): scan → `choices[0]` (fallback raw combined_text);
   digital PDF → raw `combined_text` from the extractor WINS (vision only a fallback).
   `combinedTextB` = `choices[1]`.
5. **BUILD EMAIL** (R43): `textContent` = primary transcript, + `combinedTextB` appended as
   `--- DRUHY NEZAVISLY VISION PREPIS ...` when its head differs, + the extractor's raw
   machine-OCR `Attachments:` section appended as `--- ALTERNATIVNY STROJOVY OCR PREPIS ...`
   when not already the same text ⇒ up to THREE transcripts of the same document for
   cross-checking.
6. **AI EXTRACT [v1]** (R44): chainLlm gpt-5.4 high + structured parser. SINGLE-document
   schema: `{supplierName, supplierCity, supplierEmail, docNumber, deliveryDate(DD.MM.YYYY),
   deliveryTime, documentTotalWithoutVAT, items[{name, quantity, unit, unitPrice, totalPrice,
   vatRate}]}`. Full prompt: `prompt_sub1_extract_system.txt` (rules summarized in §2).
7. **VALIDATE EXTRACTION** (R50–R51): per-line quantity self-correction + the 0.50 € money
   gate; on gate breach THROWS (context first, numbers last — n8n keeps only the message tail)
   → error output → **AI Failure → Needs Review**.
8. **ASSEMBLE OUTPUT** (R52): 0 extracted items ⇒ needsReview ("Dokument neobsahuje žiadne
   položky…"); else `{email, extraction, validOrNeedsReview:'valid'}`.
9. **AI Failure → Needs Review**: surfaces the REAL error string + passes through
   supplier/docNumber/date from AI EXTRACT so review pings are not blank.

### Sub2: DL — Match

1. **PREPARE CUSTOMER CANDIDATES** (R60): deterministic scoring of the `customers` sheet vs
   extraction supplier → top 10 → text list.
2. **AI MATCH CUSTOMER** (R61): gpt-5.4 high → `{matched, ean_edi, name,
   matchConfidence(0–100), matchReason}`. Prompts: `prompt_sub2_match_customer_*.txt`.
3. **PAMAT DL**: Data Table get — history rows for `cust = matched ean_edi` (returnAll).
   MUST sit at a smaller canvas-Y than PREPARE (n8n runs branches by Y; PREPARE reads it via
   `$('PAMAT DL')` in try/catch — silently empty if not run).
4. **PREPARE PRODUCT CANDIDATES** (R62–R66): per item — OCR name fix map, normalization, stem
   compare, history resolution (`memResolve`), deterministic candidate scoring → top 15.
5. **Split Out** items → **AI MATCH PRODUCT** per item (R67): gpt-5.4 high →
   `{gtin|'NO_MATCH', matchedCatalogName, matchConfidence(0–1), matchReason, mass}`.
   Prompts: `prompt_sub2_match_product_*.txt`.
6. **ENRICH MATCH** per item (R70–R76): confidence gate + alias rescue + memory rescue +
   borderline band + weight-conflict guard (all deterministic; details §2). Positional
   `$itemIndex` lookup against Split Out (pairedItem is broken by chainLlm+continueErrorOutput).
7. **Aggregate2** → **ASSEMBLE OUTPUT**: `{header{customerName, customerEanEdi,
   supplierMatched, matchConfidence}, matchedItems, extraction, catalog}`.
8. **AI Failure → Needs Review**: same contract as Sub1's.

### Sub3: DL — Assemble EDI

One Code node (`sub3_edi_code.js`), v27. Gates, conversions, DESADV format: §2 R80–R95, §3.

---

## 2. Business rules (numbered)

### Ingest / claim / retry

- **R1** Dispatcher wakes a consumer only when an unprocessed, unquarantined
  (`attempts < 5`), unclaimed-or-stale (`processing_at` NULL or > 10 min old) row exists;
  max 10 wakeups per minute tick, one Execute-Workflow call per pending row (`mode: each`).
- **R10** Claim is per MESSAGE (not per document): atomic
  `UPDATE … WHERE id=(SELECT … LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING …`, oldest first,
  increments `attempts`, sets `processing_at`. Stale-reclaim window **30 min** (dispatcher's
  own filter is 10 min — see W8).
- **R11** Quarantine: `attempts >= 5` rows are never picked again (anti-loop cap; a stuck
  message burns max ~5 AI cycles). A separate hourly "Stuck message watchdog" workflow
  (`EPe5WWMVZR0lzUld`) posts ONE Slovak Odoo alert per message at `attempts>=3 &&
  processed=false && alerted_stuck=false`.
- **R12** Success/review/duplicate all end in `Mark Processed` (`processed=true,
  processed_at=now(), processed_by='dodacie_listy'` by row `id`). Only the transient-retry
  path leaves the row unmarked on purpose.
- **R13** Every terminal outcome writes `email_events` (rollup=true → DB trigger denormalizes
  onto `messages.proc_status/proc_outcome`): `uploaded_orion/ok`, `review/review`,
  `skip/skip` (duplicate), plus non-rollup `retry/retry`.
- **R14** An empty claim (race / no eligible row) must terminate gracefully (filter on
  `message_id` notEmpty) — never reach a node with `$1 = undefined`.
- **R15** A claimed message with `has_attachments=false` is NOT an error: fixed needs-review
  message to Odoo ("Email bez prílohy — pravdepodobne bežná správa"), then marked processed.
- **R16** Document selection: exactly ONE attachment per message is processed — `ORDER BY
  (is-pdf) DESC, idx ASC LIMIT 1`. There is no loop over attachments (W1).
- **R17** Transient-failure retry: needs-review reasons matching the transient regex AND
  `attempts < 3` → log retry, leave unmarked, let the 30-min reclaim re-run the whole
  pipeline. Attempts 3–4 or non-transient reason → Odoo review. (Transient regex must never
  contain bare digits — reasons carry money amounts.)

### Catalog & customers

- **R20** Catalog = union of sheet tabs `produkty dodacie listy` (through Edit Fields
  keep-only: GTIN, Názov, doplnok, hmotnost, Sklad, Cena) + `produkty objednavky`. Fields:
  `name, gtin, mass (kg per piece), doplnok (warehouse alias — highest-authority mapping),
  sklad (warehouse code; '100' = kg-tracked), cena (last purchase price from Codex, in ORION
  unit)`. The GTIN column == Codex `raw.sm002.NEANKOD` (can be a short internal code like
  3445 — valid). New-product onboarding order is CRITICAL: (1) assign NEANKOD in Codex,
  (2) then add the sheet row. A product without NEANKOD in Codex cannot be onboarded → stays
  NO_MATCH forever (this is the "catalog gap" class).
- **R21** Customers tab = SUPPLIERS with EDI EAN (`Názov organizácie`, `EAN kód EDI`,
  `E-mail`, `Obec`). This is the supplier whitelist: no row ⇒ DL can never create EDI.

### Vision / transcription (Sub1)

- **R40** Scan detection: largest embedded JPEG in the PDF > 20 kB ⇒ scanned document ⇒
  transcribe the JPEG at `detail: high`. Otherwise send the whole PDF as a file. Only ONE
  image is ever transcribed (W2).
- **R41** Transcription prompt (full text `prompt_vision_transcribe.txt`): plain-text table,
  one product line per row, columns Č.pol | Kód | Názov (incl. gramáž) | Množstvo | Cena/MJ
  bez DPH | Celkom bez DPH | DPH%; header (dodávateľ, číslo DL, dátum dodania) + footer with
  total; transcribe names EXACTLY char-by-char, never substitute similar-sounding words
  (explicit "Dalamanka ≠ Dalmátska"); numbers verbatim, never "normalize" unusual values
  (explicit 10 × 97,40 example); Množstvo × Cena = Celkom must hold as printed; when multiple
  quantity columns exist (Netto kg / KS / KAR), transcribe ALL with labels.
- **R42** Text-source priority: scan → vision transcript (fallback raw OCR); digital PDF →
  raw extractor text (vision only a fallback when raw is empty).
- **R43** Dual/triple transcript cross-check: second vision sample (`n: 2`) + raw machine OCR
  appended as clearly-labelled ALTERNATIVE transcripts of the SAME document (never new
  items). A silent numeric misread now requires the same error in two independent samples AND
  consistent equations.

### Extraction rules (Sub1, prompt `prompt_sub1_extract_system.txt`)

- **R44** Single-document contract: one supplier, one docNumber, one delivery date, one item
  list per run (W1/W3 root).
- **R45** Item name includes the weight from the adjacent column ("Lúpačka 75" → "Lúpačka
  75g"), never duplicated if already present.
- **R46** Price semantics: unitPrice = per-pack price AS PRINTED (never derived by dividing),
  totalPrice = printed line total; unitPrice × quantity ≈ totalPrice; smaller number =
  unit price; read column headers, don't assume order.
- **R47** docNumber: strip the `LT` prefix ("2610LT9999999999" → "9999999999");
  deliveryDate in DD.MM.YYYY; documentTotalWithoutVAT from the footer (without-VAT variant).
- **R48** Table-format rules: multi-line rows (product spans 2–3 lines); number before
  "ks/kg" on a price line is the UNIT PRICE; quantities are decimals on separate lines; skip
  `Šarža` batch lines; keep duplicate products as separate items.
- **R49a** THOUSANDS-SEPARATOR rule: space as thousands separator ("1 133,00KS" = 1133 ks);
  decide with quantity = totalPrice ÷ unitPrice; ALWAYS take totalPrice from the printed
  line-total column, never compute it.
- **R49b** MULTI-QUANTITY-COLUMN rule (LESAFFRE/HOPI): when a Netto-kg column exists, the
  delivered quantity is the NET KG (`unit: kg`), never the KS/KAR carton counts
  (incident: 16 cartons shipped instead of 160 kg).
- **R49c** CROSS-CHECK rule: reconcile every digit across all transcripts via the line and
  document equations; on unresolvable conflict prefer machine-OCR digits and let the money
  gate route to review; never normalize an unusual value.

### Validation (Sub1)

- **R50** Quantity self-correction: if unitPrice>0 and totalPrice>0 and the derived quantity
  (totalPrice/unitPrice; rounded to integer for piece units ks/ba/kt/empty, 3 decimals
  otherwise) differs from the read quantity by > 0.5 (pieces) / 0.005 (kg) AND the derived
  quantity satisfies the line equation within max(0.02 €, 2 %), REPLACE the quantity (original
  kept in `_qtyOcr`).
- **R51** Money gate: `|Σ line totalPrice − documentTotalWithoutVAT| ≤ 0.50 €` (only when a
  doc total > 0 was read). Breach ⇒ throw ⇒ needs-review. Rationale: the EDI is built from
  line items, so a misread SUMMARY digit must not block (tolerance 0.50), but a
  missing/extra/mistranscribed LINE must. No-price scans have no money gate — covered by the
  dual transcript + the match gate.
- **R52** Zero extracted items ⇒ needsReview (not a DL — prevents the 0-item dead-end loop).

### Supplier (customer) matching (Sub2)

- **R60** Deterministic pre-score (top 10 to the model): name exact 100 / substring 60 /
  word-overlap 20 + ratio×40; same email domain +30, exact email +20; city exact +15,
  substring +10.
- **R61** Model rules: match by name similarity (handle OCR errors, s.r.o./a.s.
  abbreviations), city, email domain; `matched=true` for any reasonable match (even partial
  name overlap); `matched=false` only when clearly not in the list. Supplier not matched ⇒
  no EDI (whole document to review).

### Product matching (Sub2)

- **R62** OCR_NAME_FIX map: known scan misreads corrected before matching (currently
  `cere[áa]lna kocka sypan[áa]` → "Cereálna kocka syrová 60g").
- **R63** Normalization for candidate scoring: lowercase, strip diacritics, drop brand words
  (Bertosi, Kolios, Rovagnati, Franz Josef, foodwithyou, mr., …), drop weights
  (`\d+(kg|g|ml|l)`), drop packaging words (platky/narez/cele/krajane/lupane/balenie),
  non-letters → SPACE (never delete — "Čučoriedka/PL" must stay two words).
- **R64** Word equality `wEq`: substring match, or common-stem match — shared prefix ≥
  max(4, len−2) on BOTH words (catches Slovak declension čučoriedka/čučoriedky/čučoriedok;
  known limit: internal stem change chlieb/chleba — that's what the alias column is for).
  Looser matching is safe HERE because the prefilter only ranks candidates.
- **R65** Candidate scoring: alias (`doplnok`) contained in the DL name (≥4 chars) = 99;
  exact normalized name = 98; name substring 70/65; word-overlap ratio×60; alias-word
  overlap ratio×70; the history card is floored at 90 so the model always sees it. Top 15 go
  to the model.
- **R66** History (`memResolve`, table `dodacie_pamat_poloziek`, key = supplier EAN + EXACT
  normalized item wording INCLUDING gramáž): unanimous single card → take it; mixed history →
  take the NEWEST record's card only if it carries ≥ 60 % of all deliveries (weighted by
  `cnt`; duplicate rows deduped per gtin+day+cnt); else memory stays silent. Strength =
  distinct days (or seed `cnt`). `weightOverride` allowed only when unanimous AND ≥ 3
  deliveries. Memory learns ONLY from actually-shipped EDI (`src='ship'`), written after
  upload; catalog-card disappearance invalidates the memory.
- **R67** Model matching rules (full prompt `prompt_sub2_match_product_system.txt`): ignore
  brands/packaging; alias column OVERRIDES all other doubts; an alias naming the PARTNER on
  this document makes that card binding (conf ≥ 0.95) — the partner name is injected into
  the prompt ("PARTNER ON THIS DOCUMENT"); core product type must be identical (NO_MATCH
  rather than nearest different product); SIZE rule — substantially different stated weights
  = different cards (< 0.85 or NO_MATCH) unless alias confirms; variant words
  (odtučnený/tučný/hrudkový) are identity; calibration: 0.95+ same item / 0.85–0.95 minor
  doubts / < 0.85 = uncertain, don't inflate. Mass: from the matched catalog entry, else
  parse from name, else 0.

### Post-match gates (Sub2 ENRICH MATCH — all deterministic)

- **R70** Scale normalization: confidence > 1 → divide by 100.
- **R71** Confidence bands: ≥ 0.85 accepted; **0.70–0.85 accepted but flagged
  `borderline`** (Odoo "⚠️ Prešlo na hranici istoty"); **< 0.70 demoted to NO_MATCH** with
  the candidate name preserved in matchReason (item goes to "Nespárované", EDI ships without
  it).
- **R72** ALIAS RESCUE: if the chosen card's alias contains a token (≥ 4 chars, stoplist
  as/sro/spol/pobocka/…) of the partner name → conf := 0.95, gate cannot discard it
  ("POTVRDENÉ ALIASOM"). (The orders-side aliasExact/weight-bypass variant deliberately does
  NOT exist in DL — mass drives the kg conversion here.)
- **R73** MEMORY RESCUE: below the 0.85 gate, or on model NO_MATCH, an applicable history
  card takes over: gtin/card/mass switched to the history card, conf := 0.95 ("POTVRDENÉ
  HISTÓRIOU DODÁVOK"). Memory NEVER overrides a confident (≥ 0.85) model match.
- **R74** WEIGHT-CONFLICT guard: when BOTH the DL name and the card name state a weight and
  the ratio is outside 0.9–1.1 (1 kg == 1000 gr; liquid multipacks `Nx1l` and weightless
  names exempt) → NO_MATCH — EXCEPT `memWeightOverride` (unanimous history, ≥ 3 deliveries)
  → accepted with `historyOverride` flag (Odoo "📘 gramáž NESÚHLASÍ" block). On any card
  switch the `mass` MUST be taken from the new card (kg conversion depends on it).
- **R75** Every rescue/demote is console-logged with item, cards, confidences (diagnosis
  depends on it).
- **R76** Match-chain AI failure (after 3 retries) → needs-review with the real error;
  transient errors then hit R17's retry.

### EDI assembly & gating (Sub3)

- **R80** Items with `quantity == 0` are dropped up front (silently — W10).
- **R81** `supplierMatched` = a customer EAN is present. `canCreateEDI = supplierMatched &&
  ≥ 1 item with a real GTIN`. **Partial EDI is allowed and normal**: NO_MATCH items are
  skipped from the EDI, shipped lines go out, unmatched items are surfaced in the SAME ✅
  Odoo message ("⚠️ Nespárované položky (N/M) — EDI šlo BEZ nich, doplniť do katalógu").
- **R82** Reject reasons (whole document to review): supplier not matched ("Dodavatel nebol
  najdeny v databaze"), zero matched items ("Ziadne polozky s GTIN: 0 z N"), all quantities
  zero.
- **R83** docNumber: extraction's docNumber, else generated `DL-<SUPPLIER8>-<MMDD>-<HHMM>`
  (`docNumberAutoGenerated` flag). **EDI CONTENT carries digits-only** (`replace(/[^0-9]/g)`)
  because ORION parses HDR NCDLIST as a float — a letter prefix (e.g. MPC's own doc numbers — a real one, redacted here as "P00099999") crashes the
  import. Human-facing docNumber (Odoo, registry, filename) keeps the original.
- **R84** Quantity/unit conversion per line, in precedence order:
  1. **Liquid multipack** (the ORIGINAL supplier name matches `N x <size> (ml|l)`, liquids
     only): outQty = qty × N × litres, unitPrice ÷= total litres, unit forced to `L`.
     Takes precedence over the kg rule.
  2. **kg-tracked** (`sklad == '100'` and name does not contain "vajcia"/eggs): unit already
     kg → unchanged; else mass > 0 → outQty = qty × mass (kg), unitPrice ÷= mass; mass
     unknown → unchanged.
  3. All other sklad values: piece count unchanged.
  The LIN unit column keeps the ORIGINAL unit text except the multipack `L` (ORION keys on
  the card, not the unit text).
- **R85** PRICE FALLBACK (after conversion, so units line up): when the catalog `cena` (> 0)
  exists and the line's unitPrice is missing OR ≥ 5× OR ≤ 1/5 of it → substitute the catalog
  price; each substitution reported in the ✅ Odoo message ("💶 Cena doplnená/opravená z
  tabuľky"). Normal price movement (< 5×) is never overwritten.
- **R86** Item `mass` = matched value ∥ parsed from name (`extractMass`: kg → as-is, g →
  /1000) ∥ 0.
- **R87** Diacritics are ASCII-folded (`toWin1250` map á→a č→c …) in ALL EDI text fields.
- **R88** Dates in EDI = `YYYYMMDD` parsed ONLY from `DD.MM.YYYY` (2-digit year → `20xx`);
  unparseable → 8 spaces. HDR docDate = delivery date (not today).
- **R89** Filename: `DESADV_<last 6 of buyer EAN>_<docNumber alnum, max 10>_<YYYYMMDD from
  deliveryDate>_<HHMMSSmmm>.txt` (+ `_N` for orderIndex > 0 — never used, single doc).
  Upload writes it as **`Z-<filename>`** into `C:\ORION\COMMUNICATOR\data\in_DL`.
  (`wincodexPath` in the output says `data\in\` — cosmetic leftover, the SSH node's dir wins.)
- **R90** Registry/dedup (per DOCUMENT, not per message): key = human docNumber alone
  (no supplier scoping — W7). Read: whole table after Sub3; write: upsert on the
  canCreateEDI-true branch. Duplicate ⇒ quiet skip + mark processed (R32).
- **R91** Success side effects, in execution order: upload → mark processed → success event →
  item-history write (`ZOSTAV RIADKY PAMATE DL`: one row per shipped matched item, cnt=1,
  src='ship', skipped when EDI didn't ship or supplier EAN missing) → Odoo ✅ → registry
  upsert → forecast-sheet append (one row per item: date, supplier, EAN EDI, DL number, DL
  name, catalog name, GTIN, Sklad, qty, MJ, unit price, total, VAT, mass kg, confidence).

### Odoo notifications (channel 243)

- **R95** ✅ Success message (single message per DL — partial included): header "✅ Dodací
  list spracovaný[ — ČIASTOČNE…]", From/Subject, supplier, DL number, delivery date, item
  list ("• name (qty unit)"), optional sections: 💶 price substitutions, ⚠️ unmatched items
  (count, "doplniť do katalógu"), ⚠️ borderline items, 📘 history-override items, filename.
- **R96** ❌ Needs-review message: reads BOTH payload shapes (success-shape names ∥
  AI-failure names ∥ extraction fallbacks); reason chain `rejectReason ∥ reason ∥
  unmatched-explanation ∥ 'Neznamy dovod'`; details supplier/EAN/DL number/date + summary.
- **R97** Both Odoo posts are `onError: continueRegularOutput` — a flaky Odoo never blocks
  Mark Processed (no reprocess loop). Review posts also log `review` events with
  subject+from for triage.

---

## 3. EDI DESADV output format (exact)

- Plain text, lines joined `\r\n`, ASCII-folded (R87). One HDR + N LIN.
- **HDR** (fixed 1157 chars): `HDR` + docNumber(15) + docDate(8, YYYYMMDD = delivery date) +
  delivDate(8) + 33sp + orderNumber(30, = digits-only docNumber) + 24sp + buyerEAN(17) ×3 +
  supplierEAN `8586013743063`(17) + 21sp + buyerName(105) + 105sp + 38sp +
  `SLOVNORMAL, s.r.o.`(105) + 167sp + `Druzstevna 170`(38) + `Grance - Petrovce`(27) +
  `053 05`(11) + 160sp + 66sp.
- **LIN** (209–221 chars): `LIN` + lineNum(6, right) + GTIN(13) + `0`(14, right) + 23sp +
  `Z` + 22sp + unitPrice(9, right, 3 dec) + `5`(5, right) + qty(12, right, 3 dec) + unit(3) +
  35sp + orderNumber(30) + 45sp.
- Naming + upload target: R89 (uploaded as `Z-DESADV_… .txt` to
  `C:\ORION\COMMUNICATOR\data\in_DL`). in_DL is normally EMPTY (Communicator queues files
  immediately); import confirmation semantics live in `.claude/rules/n8n-workflow-edits.md`
  (archCodex presence, manual morning import).

---

## 4. TODAY'S INCIDENT — where the "second DL in one mail" is lost

Ground-truth verified (a real execution (id redacted) + prod DB + raw .eml):

Lunys "IS KARAT" print mails have subject `IS KARAT: Tlač: Dodací list SK Signatus
(2610LT<X>) - Dodací list SK Signatus 2610LT<Y>` — TWO DL numbers per mail — but carry
exactly **ONE PDF attachment, and it is always Y** (verified: raw.eml has 1 `application/pdf`
part; both mails of a pair attach the SAME document Y, byte-different regenerated print).
Pattern repeats for every pair since at least 07-28 (6+ real doc-number pairs observed across two weeks — specific numbers redacted, not needed to reconstruct the rule):

1. Mail 1 `( X1 ) - Y`: pipeline extracts Y from the PDF → EDI Y uploaded ✅.
2. Mail 2 `( X2 ) - Y`: pipeline extracts Y again → registry hit → **quiet duplicate skip**
   (no Odoo message, R32) → mark processed.
3. **DL X1 and X2 are never processed** — their PDFs never arrived in any mail. Today:
   X=0100000001 (and 0100000002 from the evening pair) exist only in the subject; the
   warehouse got the goods, ORION got nothing, and no notification fired anywhere.

The n8n workflow's contributing losses (beyond the supplier's broken mailer):

- Nothing ever compares the SUBJECT's DL number(s) to the extracted `docNumber` — an
  announced-but-not-attached DL is undetectable today (the subject IS parsed into the LLM
  input, but no rule uses it).
- The duplicate branch is deliberately silent, so the only remaining signal (a second mail
  whose subject names a new DL) is suppressed.

**Structural multi-DL losses even when the PDFs DO arrive** (all confirmed in config):

- **W1a** `Get Attachment Meta` takes `LIMIT 1` (first PDF): a mail with 2 PDF DLs processes
  only attachment idx 0; Mark Processed covers the whole message → attachment 2 lost silently.
- **W1b** AI EXTRACT's schema is single-document (R44): one PDF containing 2 DLs yields one
  extraction; the money gate may even pass (model reads one doc consistently) → doc 2 lost.
  For digital PDFs the input text (`combined_text`) contains ALL attachments' text — two
  digital DLs in one mail feed the model two documents against a one-document contract.
- **W1c** `Detect scan image` transcribes only the LARGEST embedded JPEG (R40): a multi-page
  scan batch (2 scanned DLs in one PDF) transcribes one page.
- **W1d** Claim, attempts, processed, review, retry are all per MESSAGE; registry is per
  docNumber; there is exactly one EDI upload per execution. The Python rebuild should make
  the DOCUMENT the unit of work (per-document claim/registry/outcome, N documents per
  message), which is the task's "per-document vs per-message claim question" — answer: today
  everything except the sent-registry is per-message.

## 5. Other known weak points (W…)

- **W2** Sent-registry write ordering: registry upsert + Odoo ✅ run AFTER upload+mark (branch
  order). If the execution dies between `Upload a file` and `Upsert row(s)` (e.g. Mark
  Processed DB error, n8n crash, ZAPIS PAMAT failure — no onError on those), the DL is
  uploaded but never registered → a later re-announcement mail re-uploads it under a NEW
  timestamped filename = genuine ORION duplicate. (Mirror of `.claude/rules` rule 2 — here
  the "claim" is taken AFTER the side effect.)
- **W3** Registry dedup race: the registry is read pre-upload, written post-upload; two
  same-docNumber messages claimed in the same dispatcher tick both pass `keepNonMatches` →
  double upload. (Lunys pairs arrive hours apart in practice.)
- **W4** Registry key is bare docNumber with no supplier scoping (R90): short doc numbers
  (e.g. Jackulík's own short doc numbers — a real one, redacted here as "50123") can collide across suppliers over time → false "duplicate" quiet skip =
  lost DL.
- **W5** `ZAPIS PAMAT DL` currently has `options.optimizeBulk: true` — the exact
  N-items × N-rows duplication bug documented as fixed in history-memory (must be
  `options: {}`); `memResolve`'s per-day dedup bounds the damage but the table bloats.
- **W6** `Get row(s)` reads the ENTIRE registry table every run (returnAll, and it runs once
  per input item on data tables) — unbounded growth, eventual data-table row caps.
- **W7** Duplicate skip logs `docNumber` from the merge output — quiet by design; combined
  with the Lunys pattern it silences real losses (see §4).
- **W8** Dispatcher stale filter (10 min) vs claim stale window (30 min) mismatch: rows
  10–30 min into processing wake the consumer for a guaranteed-empty claim every minute
  (harmless but noisy).
- **W9** A needs-review path exists where `Retry transient?` reads
  `$('Get Dodacie').first().json.attempts` — attempts is already incremented by the claim, so
  retry gate `< 3` gives retries on attempts 1–2, review on 3–4, quarantine at 5. Correct but
  non-obvious; keep semantics in the rebuild.
- **W10** Sub3 silently drops quantity==0 lines (R80) — no report anywhere.
- **W11** LIN unit column carries the original unit text after kg conversion (R84) — works
  only because ORION ignores it; encode as an explicit contract in the rebuild.
- **W12** `formatDate` accepts only `DD.MM.YYYY`; an ISO date (model drift) silently blanks
  both HDR dates (the orders pipeline had exactly this bug).
- **W13** Digital PDFs still pay a gpt-5.4 `n=2` vision call whose output is usually unused
  (R42) — pure cost.
- **W14** `Edit Fields` keep-only mapping silently drops any NEW catalog column (bit the Cena
  rollout, 07-22); same trap class as autoMapInputData schema caching.
- **W15** Error paths that bypass Odoo review entirely: an execution-level crash (e.g. SSH
  down) only pings Discord via the global error workflow; the message retries via reclaim and
  quarantines at 5 attempts with only the hourly stuck-watchdog Odoo alert (R11).
- **W16** Vision/no-price scans have NO money gate (documentTotal absent) — protection is
  only the dual transcript + match gates + price fallback (R85).

## 6. Prompt files saved (full texts, scratchpad)

- `prompt_vision_transcribe.txt` — Slovak vision transcription instruction (R41).
- `prompt_sub1_extract_system.txt` / `prompt_sub1_extract_user.txt` — extraction rules
  R44–R49c.
- `prompt_sub2_match_customer_system.txt` / `_user.txt` — supplier match R61.
- `prompt_sub2_match_product_system.txt` / `_user.txt` — product match R67 (+ parser examples
  in `p_sub2/Structured_Output_Parser*.params.json`).
- Full node params: `p_disp/`, `p_parent/`, `p_sub1/`, `p_sub2/`, `p_sub3/`; EDI generator:
  `sub3_edi_code.js`; executions: `exec_*.json`, `transcript_<redacted-exec-id>.txt` (real data — do not
  commit).

## 7. FÁZA 1 (#200) — čo pristálo a prečo (implementation log)

Foundation-only phase: schema + config + this spec. No pipeline code (extraction,
matching, EDI writer, worker) exists yet — every table below is inert until a later
phase writes to it.

- **`delivery_notes_engine`/`delivery_notes_shadow`/`delivery_notes_shadow_days`**
  (`app/config.py`) — same trio shape as `ai_orders_engine`/`static_orders_engine`.
  Default `n8n` = the live "Dodacie Listy EDI" workflow keeps running completely
  untouched.
- **`desadv_sent`** (`app/orders/desadv.py`) — two-phase claim→confirm ledger, keyed
  `(supplier_ean, doc_number)` instead of a bare doc number (fixes W4), claimed
  BEFORE the upload from day one (fixes W2/W3). No content hash — DL identity is the
  document, not its bytes (R90).
- **`dl_item_memory`** (`app/orders/dl_memory.py`) — `item_memory`'s sibling
  (`db.py:397-410`), keyed by supplier, PLUS a `cnt` column that `item_memory` does
  not have: R66's weighted-majority rule needs the n8n table's own per-row delivery
  count preserved verbatim, not re-derived as distinct days the way
  `item_memory.resolve()` does. One-shot n8n import: `scripts/import_dl_item_memory.py`
  (verified against a local test DB in #200; the real run against
  `dodacie_pamat_poloziek` / `MBCwHVhzsKjbQkVl` happens at cutover, NOT now).
- **`dl_snapshots`/`dl_catalog_snapshot`/`dl_supplier_snapshot`**
  (`app/orders/dl_snapshot.py`) — content-addressed, a SEPARATE versioning line from
  `order_snapshots` (the DL catalog's shape — R20's mass/doplnok/sklad/cena — differs
  from the orders catalog's gtin/name/alias). Catalog = straight union of `produkty
  dodacie listy` + `produkty objednavky` (mirrors the n8n `Merge(append)` node, no
  GTIN dedup). Suppliers reuse `snapshot.parse_customers` verbatim (R21: same
  physical `customers` tab AI orders already reads). No static keep-only column
  list (W14) — every R20 field is read by name explicitly.
- **`order_runs` — reused with ZERO schema change.** A DL run will use the SAME
  `order_runs` table a later phase's DL worker calls `_start_run`/`_finish_run`
  on unmodified; the DL/orders distinction lives inside `result` (JSONB), e.g. a
  `"kind": "dl"` key, never a new column. Decided in #200's design comment rather
  than built, since nothing calls `_start_run` for DL yet in this phase.
