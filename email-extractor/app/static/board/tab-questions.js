// Otázky sklad + Otázky objednávky tab (#443). Uses the ONE copy of api.js/ui.js helpers.
// Renders one card per question with the SAME choices the old boards offer; answers/undo
// delegate to /api/board/questions/<id>/answer|undo (which delegate to the real teach/hold
// machinery). Expired → „Znovu otvoriť"; answered → „Vrátiť odpoveď". No HTML strings — all
// nodes are built via ui.js `el()`. Refresh-safety: a card being edited (data-open) is never
// wiped by the periodic refresh.
import { apiGet, apiPost, clear, debounce, el, toast } from "./ui.js";

const main = document.getElementById("board-main");
const SCOPE = (main && main.dataset.scope) || "orders";
const listEl = document.getElementById("q-list");
const emptyEl = document.getElementById("q-empty");
const searchEl = document.getElementById("q-search");

// #467: `codexHints` — the CODEX refusal help per question id, re-rendered by every refresh
// (a hint never freezes the list, and the list never wipes a hint).
const state = { status: "open", q: "", cardActions: {}, codexHints: {} };

function editingOpen() {
  // Skip the periodic refresh only while something is ACTIVELY being edited — never freeze
  // the whole list on a stale marker. An open inline form, the search box focused, or any
  // qty/price/free input that is focused OR already holds text all count; an abandoned
  // (empty, blurred) focus does not, so the list resumes refreshing on its own.
  if (searchEl && document.activeElement === searchEl) return true;
  if (!listEl) return false;
  if (listEl.querySelector(".q-inline-form")) return true;
  for (const inp of listEl.querySelectorAll(".q-qty, .q-price, .q-in, .q-massin")) {
    if (document.activeElement === inp) return true;
    if (inp.value && inp.value.trim()) return true;
  }
  return false;
}

// ---- answer-body shapers per kind (candidate click) -------------------------------
function candidateButton(q, cand) {
  const kind = q.kind;
  let label, body;
  if (kind === "item") {
    label = cand.name || cand.gtin;
    body = () => ({ gtin: cand.gtin, card: cand.name || "", ...lineEdits(q) });
  } else if (kind === "customer") {
    label = cand.name || cand.ean_edi;
    body = () => ({ ean_edi: cand.ean_edi, name: cand.name || "" });
  } else {
    label = cand.label || cand.value;
    body = () => ({ choice: cand.value });
  }
  // #467: a card whose OUR name drifted from CODEX's shows the CODEX name too (and counts for
  // the misclick check) — the sklad recognises the card by what CODEX calls it.
  const aka = kind === "dl_item" && cand.codex_name
    ? el("span", { class: "q-cand-codex" }, ` · CODEX: ${cand.codex_name}`) : null;
  return el("button", { class: "q-btn q-btn--cand", type: "button",
    onclick: () => {
      if (kind === "dl_item" && !confirmUnrelated(q.wording, label,
        [cand.alias, cand.codex_name].filter(Boolean).join(" "))) return;
      submit(q.id, body(), q);
    } }, [label, aka]);
}

// ---- #477: „Vybrať kartu z CODEXu" — the ONE way a card that is not offered reaches the
// question. The server lists the pushed CODEX cards (sklad-scoped per question kind); a pick
// selects our card when we already have the code, else adds exactly that CODEX card (code +
// CODEX name) to the catalog, then answers through the normal path. Nothing is ever typed as
// a new card (owner order 2026-09-30). -------------------------------------------------------
const PICK_SCOPE = { item: "orders", dl_item: "dl" };

function codexStatusText(m) {
  if (!m || m.never) return "⚠ Zoznam kariet z CODEXu ešte neprišiel — výber zatiaľ nie je možný.";
  if (m.stale) {
    return `⚠ Zoznam kariet z CODEXu je zastaraný (stav k ${m.as_of_local}) — karty založené `
      + "v CODEXe odvtedy tu ešte nie sú.";
  }
  return `Karty z CODEXu: stav k ${m.as_of_local} — hľadaj podľa názvu alebo kódu.`;
}

function toggleCodexPicker(q) {
  const card = document.getElementById(`q-card-${q.id}`);
  if (!card) return;
  let form = card.querySelector(".q-inline-form");
  if (form) { form.remove(); card.removeAttribute("data-open"); return; }
  card.setAttribute("data-open", "1");
  const search = el("input", { class: "q-in q-codex-search", type: "search",
    placeholder: "Hľadaj v CODEXe — názov alebo kód karty", autocomplete: "off" });
  const status = el("div", { class: "q-codex-status" }, "Načítavam zoznam kariet z CODEXu…");
  const results = el("div", { class: "q-codex-results" });
  const close = () => { form.remove(); card.removeAttribute("data-open"); };
  form = el("div", { class: "q-inline-form q-codex-picker" }, [
    search, status, results,
    el("button", { class: "q-btn", type: "button", onclick: close }, "Zrušiť"),
  ]);
  let seq = 0;
  const run = async () => {
    const mine = ++seq;
    const text = search.value.trim();
    try {
      const params = new URLSearchParams({ scope: PICK_SCOPE[q.kind] || "dl", q: text });
      const data = await apiGet(`/codex-cards?${params.toString()}`);
      if (mine !== seq) return;   // a newer search already answered — never show a stale list
      const items = data.items || [];
      let line = codexStatusText(data.codex);
      if (text && !items.length) line += " Nič sa nenašlo.";
      else if (data.total > items.length) line += ` Zobrazených ${items.length} z ${data.total} — spresni hľadanie.`;
      status.textContent = line;
      status.classList.toggle("is-warn", !data.codex || !data.codex.active);
      clear(results);
      for (const c of items) results.appendChild(codexChoice(q, c));
    } catch (e) { toast(e.message, { error: true }); }
  };
  search.addEventListener("input", debounce(run, 300));
  card.appendChild(form);
  run();          // the list's freshness (a stale-list warning) shows before any typing
  search.focus();
}

function codexChoice(q, c) {
  // the CODEX sklad is only what a NEW card gets — our own / Kôš card keeps its stored data
  const isNew = !c.in_catalog && !c.in_trash;
  const facts = [c.card_code ? `karta ${c.card_code}` : null,
    isNew && q.kind === "dl_item" ? `sklad ${c.sklad}` : null];
  if (c.in_catalog) facts.push(`u nás: ${c.catalog_name}`);
  else if (c.in_trash) {
    facts.push(`u nás v Koši${c.trash_name ? `: ${c.trash_name}` : ""} — výber ju obnoví`);
  } else facts.push("nová karta");
  const label = c.in_catalog ? "Vybrať" : (c.in_trash ? "Obnoviť a vybrať" : "Pridať a vybrať");
  const ourName = c.in_catalog ? c.catalog_name : (c.in_trash && c.trash_name) || c.name;
  const pick = el("button", {
    class: "q-btn q-btn--primary q-codex-pick", type: "button", "data-code": c.code,
    onclick: () => pickCodex(q, c.code, ourName, c.name),
  }, label);
  return el("div", { class: "q-codex-choice", "data-code": c.code }, [
    el("span", { class: "q-codex-choice-name" }, `${c.code} — ${c.name}`),
    el("span", { class: "q-codex-choice-facts" }, facts.filter(Boolean).join(" · ")),
    pick,
  ]);
}

function pickCodex(q, code, cardName, codexName) {
  if (q.kind === "dl_item" && !confirmUnrelated(q.wording, cardName, codexName)) return;
  const body = { codex_card: { code } };
  if (q.kind === "item") Object.assign(body, lineEdits(q));
  submit(q.id, body, q);
}

// ---- #467: a card number CODEX has no stock card for — the server refuses it (409) and
// names CODEX cards with a similar name + code; one click uses the right card -------------
function showCodexHint(q, data) {
  state.codexHints[q.id] = data;
  const card = document.getElementById(`q-card-${q.id}`);
  if (!card) return;
  const old = card.querySelector(".q-codex-hint");
  if (old) old.remove();
  card.appendChild(codexHint(q, data));
}

function codexHint(q, data) {
  const pick = (code, cardName, codexName) => {
    if (!confirmUnrelated(q.wording, cardName, codexName)) return;
    submit(q.id, { choice: code }, q);
  };
  const rows = [];
  const similar = (data.codex && data.codex.similar) || [];
  for (const s of similar) {
    // #477: a CODEX card we do not have yet is ADDED through the same pick as the picker —
    // never a typed card — but only one the picker offers (`pickable`: stredisko 1, active)
    let btn = null;
    if (s.in_catalog) {
      btn = el("button", { class: "q-btn q-codex-use", type: "button", "data-code": s.code,
        onclick: () => pick(s.catalog_gtin || s.code, s.catalog_name, s.name) },
      `Použiť kartu „${s.catalog_name}“`);
    } else if (s.pickable) {
      btn = el("button", { class: "q-btn q-codex-new", type: "button", "data-code": s.code,
        onclick: () => pickCodex(q, s.code, s.name, s.name) }, "Pridať kartu z CODEXu");
    }
    rows.push(el("div", { class: "q-codex-row" }, [
      el("span", {}, `${s.code} — ${s.name}`
        + (s.in_catalog || s.pickable ? "" : " (z CODEXu sa nedá vybrať — neaktívna, mimo "
          + "skladov strediska 1 alebo kód dlhší ako 13 znakov)")),
      btn]));
  }
  if (data.codex && !similar.length) {
    rows.push(el("div", { class: "q-codex-row" },
      "V CODEXe sme nenašli kartu s podobným názvom — skontroluj EAN kód karty v CODEXe."));
  }
  const hint = el("div", { class: "q-codex-hint" }, [
    el("div", { class: "q-codex-msg" }, data.error || ""), ...rows]);
  hint.appendChild(el("button", { class: "q-btn q-codex-close", type: "button",
    onclick: () => { delete state.codexHints[q.id]; hint.remove(); } }, "Zavrieť"));
  return hint;
}

// ---- #465: a dl_item pick sharing NO word with the delivery-note line --------------------
// One misclick („Rožok oravský" answered as „Ovocie – Zlaté jablko pražené") used to teach the
// matcher a wrong card that then shipped silently on later deliveries. Mirrors the server's
// R75 lexical measure (dl_match._distinctive_words/_lexical_overlap, card name + alias —
// the alias rides on the candidate as `alias`): diacritics folded,
// weights + non-letters dropped, words of 4+ letters minus generic bread words, 4-letter stem.
const GENERIC_WORDS = new Set(["chlieb", "chleba", "chlebom", "chlebu"]);

function distinctiveWords(text) {
  const s = String(text || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
    .replace(/\d+(?:[.,]\d+)?\s*(kg|gr|g|ml|l)\b/g, " ").replace(/[^a-z\s]/g, " ");
  return s.split(/\s+/).filter((w) => w.length >= 4 && !GENERIC_WORDS.has(w));
}

function sharesWord(wording, cardName, alias) {
  const item = distinctiveWords(wording);
  const card = distinctiveWords(cardName).concat(distinctiveWords(alias));
  if (!item.length || !card.length) return true;  // nothing to compare — never block
  return item.some((w) => card.some((c) => w.slice(0, 4) === c.slice(0, 4)));
}

function confirmUnrelated(wording, cardName, alias) {
  if (sharesWord(wording, cardName, alias)) return true;
  return window.confirm(`Naozaj priradiť „${wording}“ ku karte „${cardName}“?\n\n`
    + "Názvy nemajú ani jedno spoločné slovo — skontroluj, či si neklikol vedľa. "
    + "Toto priradenie sa naučí aj pre ďalšie dodacie listy.");
}

function lineEdits(q) {
  const card = document.getElementById(`q-card-${q.id}`);
  if (!card) return {};
  const out = {};
  const qty = card.querySelector(".q-qty");
  const price = card.querySelector(".q-price");
  if (qty && qty.value.trim()) out.quantity = qty.value.trim();
  if (price && price.value.trim()) out.unit_price = price.value.trim();
  return out;
}

// ---- action buttons (op -> body / inline form) ------------------------------------
const SIMPLE_OPS = {
  manual: () => ({ manual: true }),
  unknown_customer: () => ({ unknown: true }),
  not_order: () => ({ not_order: true }),
  mail_not_order: () => ({ choice: "not_order" }),
  mail_manual: () => ({ choice: "manual" }),
  line_keep: () => ({ choice: "keep" }),
  line_drop: () => ({ choice: "drop" }),
  ship_without: () => ({ choice: "ship_without" }),
  not_warehouse: () => ({ not_warehouse: true }),
  dl_unknown: () => ({ choice: "unknown" }),
};

// Partners only — a product card is never typed (#477, `codex_pick` below).
const FORM_OPS = {
  new_customer: { key: "new_customer", fields: [["ean_edi", "EAN zákazníka"], ["name", "Názov"]] },
  new_supplier: { key: "new_supplier", fields: [["ean_edi", "EAN dodávateľa"], ["name", "Názov"]] },
};

function actionButton(q, act) {
  if (act.op === "codex_pick") {
    return el("button", { class: "q-btn q-btn--act q-codex-open", type: "button",
      onclick: () => toggleCodexPicker(q) }, act.label);
  }
  if (SIMPLE_OPS[act.op]) {
    return el("button", { class: "q-btn q-btn--act", type: "button",
      onclick: () => submit(q.id, { ...SIMPLE_OPS[act.op](), ...(q.kind === "item" ? lineEdits(q) : {}) }) },
      act.label);
  }
  if (FORM_OPS[act.op]) {
    return el("button", { class: "q-btn q-btn--act", type: "button",
      onclick: (e) => toggleForm(q, FORM_OPS[act.op], e.target) }, act.label);
  }
  return null;
}

function toggleForm(q, spec, btn) {
  const card = document.getElementById(`q-card-${q.id}`);
  if (!card) return;
  let form = card.querySelector(".q-inline-form");
  if (form) { form.remove(); card.removeAttribute("data-open"); return; }
  card.setAttribute("data-open", "1");
  const inputs = spec.fields.map(([name, ph]) =>
    el("input", { class: `q-in q-in-${name}`, type: "text", placeholder: ph, autocomplete: "off" }));
  form = el("div", { class: "q-inline-form" }, [
    ...inputs,
    el("button", { class: "q-btn q-btn--primary", type: "button", onclick: () => {
      const payload = {};
      spec.fields.forEach(([name], i) => { payload[name] = inputs[i].value.trim(); });
      submit(q.id, { [spec.key]: payload }, q);
    } }, "Uložiť"),
    el("button", { class: "q-btn", type: "button",
      onclick: () => { form.remove(); card.removeAttribute("data-open"); } }, "Zrušiť"),
  ]);
  card.appendChild(form);
  if (inputs[0]) inputs[0].focus();
}

// ---- one card ---------------------------------------------------------------------
function card(q) {
  const box = el("section", { class: "q-card", id: `q-card-${q.id}`,
    "data-kind": q.kind, "data-qid": q.id });
  const title = q.wording || q.customer_name || `#${q.id}`;
  box.appendChild(el("div", { class: "q-head" }, [
    el("span", { class: "q-kind" }, q.kind),
    el("span", { class: "q-title" }, title),
    q.customer_name ? el("span", { class: "q-cust" }, q.customer_name) : null,
  ]));

  if (state.status === "answered") {
    box.appendChild(el("div", { class: "q-answer" },
      `Odpovedané: ${q.answer_card || q.answer_gtin || (q.answer && q.answer.choice) || "—"}`));
    box.appendChild(el("div", { class: "q-actions" }, [
      el("button", { class: "q-btn q-btn--undo", type: "button",
        onclick: () => act(q.id, "undo") }, "Vrátiť odpoveď"),
      previewButton(q),
    ]));
    return box;
  }

  // open / expired: candidates + line edits + actions
  const cands = (q.candidates || []).map((c) => candidateButton(q, c));
  if (cands.length) box.appendChild(el("div", { class: "q-cands" }, cands));

  if (q.kind === "item") {
    box.appendChild(el("div", { class: "q-lineedit" }, [
      el("label", {}, ["Množstvo ", el("input", { class: "q-qty", type: "text",
        value: q.quantity != null ? String(q.quantity) : "" })]),
      el("label", {}, ["Cena/ks ", el("input", { class: "q-price", type: "text",
        value: q.unit_price != null ? String(q.unit_price) : "" })]),
    ]));
  }
  // #477: no free „iné číslo položky" box — a card that is not offered is found with
  // „Vybrať kartu z CODEXu" (search by name or code), never typed.
  // #462: a dl_mass question is answered with a plain number (kg per piece), not a card.
  if (q.kind === "dl_mass") {
    box.appendChild(el("div", { class: "q-massedit" }, [
      el("label", {}, ["Koľko kg má 1 kus/kartón? ",
        el("input", { class: "q-massin", type: "text", inputmode: "decimal",
          placeholder: "napr. 10", autocomplete: "off" })]),
      el("button", { class: "q-btn q-btn--primary", type: "button", onclick: () => {
        const v = box.querySelector(".q-massin").value.trim();
        if (!v) { toast("Zadaj hmotnosť za kus (kg)", { error: true }); return; }
        submit(q.id, { choice: v });
      } }, "Uložiť hmotnosť"),
    ]));
  }

  const acts = (state.cardActions[q.kind] || []).map((a) => actionButton(q, a)).filter(Boolean);
  const actionsRow = el("div", { class: "q-actions" }, acts);
  if (state.status === "expired") {
    actionsRow.appendChild(el("button", { class: "q-btn q-btn--reopen", type: "button",
      onclick: () => act(q.id, "reopen") }, "Znovu otvoriť"));
  }
  actionsRow.appendChild(previewButton(q));
  box.appendChild(actionsRow);
  if (state.codexHints[q.id]) box.appendChild(codexHint(q, state.codexHints[q.id]));
  return box;
}

function previewButton(q) {
  return el("button", { class: "q-btn q-btn--preview", type: "button",
    onclick: () => togglePreview(q) }, "Náhľad originálu");
}

async function togglePreview(q) {
  const card = document.getElementById(`q-card-${q.id}`);
  if (!card) return;
  const existing = card.querySelector(".q-preview");
  if (existing) { existing.remove(); return; }
  try {
    const pv = await apiGet(`/questions/${q.id}/preview`);
    const atts = (pv.attachments || []).map((a) =>
      el("a", { class: "q-att", href: a.url, target: "_blank", rel: "noopener" },
        a.filename || `príloha ${a.idx}`));
    if (pv.eml_url) atts.push(el("a", { class: "q-att", href: pv.eml_url, target: "_blank",
      rel: "noopener" }, "raw .eml"));
    card.appendChild(el("div", { class: "q-preview" }, [
      el("div", { class: "q-pv-line" }, `Predmet: ${pv.subject || "—"}`),
      el("div", { class: "q-pv-line" }, `Od: ${pv.from_name || ""} <${pv.from_addr || ""}>`),
      el("div", { class: "q-pv-line" }, `Dátum: ${pv.sent_at || pv.created_at || "—"}`),
      atts.length ? el("div", { class: "q-atts" }, atts) : el("div", { class: "q-pv-line" }, "Bez príloh"),
    ]));
  } catch (e) { toast(e.message, { error: true }); }
}

// ---- network actions --------------------------------------------------------------
async function submit(qid, body, q = null) {
  try {
    await apiPost(`/questions/${qid}/answer`, body);
    delete state.codexHints[qid];
    toast("Uložené");
    await load();
  } catch (e) {
    // #467: a dl_item refusal with structured help (a card number CODEX lacks + similar CODEX
    // cards) is shown on the card with one-click fixes, not only as a toast.
    if (q && q.kind === "dl_item" && e.data && e.data.codex) {
      showCodexHint(q, e.data);
    }
    toast(e.message, { error: true });
  }
}

async function act(qid, op) {
  try {
    await apiPost(`/questions/${qid}/${op}`, {});
    toast(op === "undo" ? "Odpoveď vrátená" : "Znovu otvorené");
    await load();
  } catch (e) { toast(e.message, { error: true }); }
}

// ---- load + render ----------------------------------------------------------------
async function load({ periodic = false } = {}) {
  try {
    const params = new URLSearchParams({ scope: SCOPE, status: state.status });
    if (state.q) params.set("q", state.q);
    const data = await apiGet(`/questions?${params.toString()}`);
    // a PERIODIC refresh whose fetch was in flight when an editor/form opened must not
    // rebuild the list under it (the „element was detached" race)
    if (periodic && editingOpen()) return;
    state.cardActions = (data.meta && data.meta.card_actions) || {};
    clear(listEl);
    const items = data.items || [];
    emptyEl.hidden = items.length > 0;
    for (const q of items) listEl.appendChild(card(q));
    focusQuestion(false);   // re-apply the deep-link highlight after a refresh (no re-scroll)
  } catch (e) { toast(e.message, { error: true }); }
}

// #459: a deep link from an Odoo message — `?q=<question_id>` — scrolls to + highlights that
// ONE question card. Distinct from the #447 search-seed below (a NON-numeric `q`, e.g. a
// message_id from the „Naučené" origin link, still seeds the search box). `scroll` is true
// only on the first load, so a periodic refresh keeps the highlight without re-scrolling.
function focusQuestion(scroll) {
  if (!_focusId) return;
  const el2 = document.getElementById(`q-card-${_focusId}`);
  if (!el2) return;
  el2.classList.add("q-card--focus");
  if (scroll) el2.scrollIntoView({ behavior: "smooth", block: "center" });
}

// ---- wiring -----------------------------------------------------------------------
document.querySelectorAll(".q-chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    document.querySelectorAll(".q-chip").forEach((c) => c.classList.remove("is-active"));
    chip.classList.add("is-active");
    state.status = chip.dataset.status;
    load();
  });
});
if (searchEl) {
  searchEl.addEventListener("input", debounce(() => { state.q = searchEl.value.trim(); load(); }, 300));
}

// #447: honour a deep link from the „Naučené" tab origin link
// (/nastenka/otazky-<scope>?q=<message_id>&status=answered) — seed the filter + search box so
// the tab opens already filtered to that rule's originating question. Additive: no params =
// unchanged default (open questions, empty search).
const _init = new URLSearchParams(location.search);
const _initStatus = _init.get("status");
const _initQ = _init.get("q");
// #459: a purely-numeric `q` is a question-id DEEP LINK (scroll + highlight), never a search
// filter — seeding it into the search box would filter the list empty (search matches wording/
// message_id, not the DB id). A non-numeric `q` stays the #447 search-seed (a message_id is
// never a bare integer, so that path is unchanged).
const _focusId = _initQ && /^\d+$/.test(_initQ) ? _initQ : null;
if (["open", "expired", "answered"].includes(_initStatus)) state.status = _initStatus;
if (_initQ && !_focusId) state.q = _initQ;
if (searchEl && state.q) searchEl.value = state.q;
document.querySelectorAll(".q-chip").forEach((c) => {
  c.classList.toggle("is-active", c.dataset.status === state.status);
});

setInterval(() => { if (!editingOpen()) load({ periodic: true }); }, 8000);
load().then(() => focusQuestion(true));
